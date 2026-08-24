from __future__ import annotations

import re
from datetime import date
from io import StringIO
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

import runner

_original_score = runner.score_race


def _priority(strength: float) -> str:
    return 'S' if strength >= 3.0 else ('A' if strength >= 1.75 else 'B')


def corrected_score_race(r: dict[str, Any], v2: dict[str, Any], v21: dict[str, Any]) -> dict[str, Any]:
    out = _original_score(r, v2, v21)
    entrant_by_lane = {int(e['lane']): e for e in out['entrants']}
    for e in out['entrants']:
        missing_comment = bool(e.get('comment_missing'))
        missing_recent5 = bool(e.get('motor_recent5_missing'))
        correct = max(0.60, 1.0 - 0.15 * int(missing_comment) - 0.25 * int(missing_recent5))
        e['evidence_confidence'] = correct
    for signal in out['signals']:
        e = entrant_by_lane[int(signal['lane'])]
        correct = float(e['evidence_confidence'])
        original_conf = 0.75 if e.get('motor_recent5_missing') else 1.0
        if original_conf > 0:
            signal['strength'] = float(signal['strength']) * correct / original_conf
        signal['priority'] = _priority(float(signal['strength']))
    out['race_grade'] = _priority(max((float(s['strength']) for s in out['signals']), default=0.0))
    return out


def _norm(s: Any) -> str:
    return str(s).translate(str.maketrans('０１２３４５６７８９：Ｒ', '0123456789:R'))


def _lane(value: Any) -> int | None:
    match = re.search(r'(?<!\d)([1-6])(?!\d)', _norm(value))
    return int(match.group(1)) if match else None


def _official_names(html: str) -> dict[int, str]:
    names: dict[int, str] = {}
    soup = BeautifulSoup(html, 'html.parser')
    for anchor in soup.find_all('a', href=re.compile(r'toban=\d{4}')):
        match = re.search(r'toban=(\d{4})', str(anchor.get('href', '')))
        text = ' '.join(anchor.stripped_strings).strip()
        if match and text:
            names[int(match.group(1))] = re.sub(r'\s+', ' ', text)
    return names


def corrected_extract_entrants(html: str) -> list[Any]:
    """Parse exactly one official row per lane under pandas 3 rowspan expansion."""
    best = None
    for frame in pd.read_html(StringIO(html)):
        if len(frame) < 6:
            continue
        columns = runner.flatten_cols(frame)
        joined = ' '.join(columns)
        if ('選手' in joined or '登録' in joined) and ('全国' in joined or '勝率' in joined) and 'モーター' in joined:
            best = frame.copy()
            best.columns = columns
            break
    if best is None:
        raise ValueError('RACELIST_TABLE_NOT_FOUND')

    columns = list(best.columns)
    lane_index = runner.first_col(columns, '枠')
    if lane_index is None:
        lane_index = 0

    selected: list[tuple[int, Any]] = []
    seen_lanes: set[int] = set()
    for _, row in best.iterrows():
        lane = _lane(row.iloc[lane_index])
        if lane is None or lane in seen_lanes:
            continue
        selected.append((lane, row))
        seen_lanes.add(lane)
    selected.sort(key=lambda item: item[0])
    lanes = [lane for lane, _ in selected]
    if lanes != [1, 2, 3, 4, 5, 6]:
        raise ValueError(f'SIX_UNIQUE_LANES_REQUIRED:{lanes}')

    name_index = runner.first_col(columns, '選手')
    if name_index is None:
        name_index = runner.first_col(columns, '登録')
    national_index = runner.first_col(columns, '全国', '勝率')
    if national_index is None:
        national_index = runner.first_col(columns, '全国')
    st_index = runner.first_col(columns, '平均', 'ST')
    motor_index = runner.first_col(columns, 'モーター')
    names = _official_names(html)

    entrants: list[Any] = []
    for lane, row in selected:
        cells = [str(value) for value in row.tolist()]
        all_text = ' '.join(cells)
        identity = str(row.iloc[name_index]) if name_index is not None else all_text
        number_match = re.search(r'\b(\d{4})\b', identity)
        class_match = re.search(r'\b(A1|A2|B1|B2)\b', identity)
        if not (number_match and class_match):
            number_match = re.search(r'\b(\d{4})\b', all_text)
            class_match = re.search(r'\b(A1|A2|B1|B2)\b', all_text)
        if not (number_match and class_match):
            raise ValueError(f'ENTRANT_ID_PARSE_FAILED:{lane}')

        racer_no = int(number_match.group(1))
        racer_class = class_match.group(1)
        name_parts = re.findall(r'[一-龯ぁ-んァ-ヶー]{2,}(?:\s+[一-龯ぁ-んァ-ヶー]{1,})?', identity)
        racer_name = names.get(racer_no) or (name_parts[0].strip() if name_parts else f'選手{racer_no}')

        national_win = runner.num(row.iloc[national_index]) if national_index is not None else None
        if national_win is None:
            candidates = [runner.num(value) for value in cells]
            candidates = [value for value in candidates if value is not None and 0 <= value <= 10]
            national_win = candidates[0] if candidates else None
        if national_win is None:
            raise ValueError(f'NATIONAL_WIN_PARSE_FAILED:{lane}')

        st_text = str(row.iloc[st_index]) if st_index is not None else all_text
        f_match = re.search(r'F\s*(\d+)', st_text)
        l_match = re.search(r'L\s*(\d+)', st_text)
        st_values = re.findall(r'(?<!\d)(?:0)?\.(\d{1,2})(?!\d)', _norm(st_text))
        avg_st = int(st_values[-1].ljust(2, '0')) if st_values else None

        motor_no = None
        motor_two_rate = None
        if motor_index is not None:
            motor_text = str(row.iloc[motor_index])
            motor_values = re.findall(r'\d+(?:\.\d+)?', _norm(motor_text))
            if motor_values:
                motor_no = int(float(motor_values[0]))
            if len(motor_values) > 1:
                motor_two_rate = float(motor_values[1])

        entrants.append(runner.Entrant(
            lane,
            racer_no,
            racer_name,
            racer_class,
            float(national_win),
            avg_st,
            int(f_match.group(1)) if f_match else 0,
            int(l_match.group(1)) if l_match else 0,
            motor_no,
            motor_two_rate,
        ))

    if len(entrants) != 6 or len({entrant.racer_no for entrant in entrants}) != 6:
        raise ValueError('SIX_UNIQUE_ENTRANTS_REQUIRED')
    return entrants


def _race_no(value: Any) -> int | None:
    text = _norm(' '.join(str(x) for x in value) if isinstance(value, tuple) else str(value))
    match = re.search(r'(?<!\d)(1[0-2]|[1-9])\s*R(?!\d)', text, re.I)
    return int(match.group(1)) if match else None


def _clock(value: Any) -> str | None:
    text = _norm(str(value))
    match = re.search(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (8 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f'{hour:02d}:{minute:02d}:00'


def deadline_map(racelist_html: str) -> dict[int, str]:
    """Extract official 1R..12R scheduled deadlines from a racelist page."""
    soup = BeautifulSoup(racelist_html, 'html.parser')
    result: dict[int, str] = {}

    for table in soup.find_all('table'):
        race_nos = [rno for cell in table.find_all(['th', 'td']) if (rno := _race_no(' '.join(cell.stripped_strings)))]
        race_nos = list(dict.fromkeys(race_nos))
        if not race_nos:
            continue
        for row in table.find_all('tr'):
            row_text = _norm(' '.join(row.stripped_strings))
            if '締切予定' not in row_text:
                continue
            times = [clock for cell in row.find_all(['th', 'td']) if (clock := _clock(' '.join(cell.stripped_strings)))]
            if len(times) >= len(race_nos):
                for rno, clock in zip(race_nos, times):
                    result.setdefault(rno, clock)

    if len(result) != 12:
        try:
            for frame in pd.read_html(StringIO(racelist_html)):
                for _, row in frame.iterrows():
                    row_text = _norm(' '.join(str(x) for x in row.tolist()))
                    if '締切予定' not in row_text:
                        continue
                    for column, value in zip(frame.columns, row.tolist()):
                        rno = _race_no(column)
                        clock = _clock(value)
                        if rno is not None and clock is not None:
                            result.setdefault(rno, clock)
        except Exception:
            pass

    missing = [rno for rno in range(1, 13) if rno not in result]
    if missing:
        raise ValueError(f'DEADLINE_MAP_INCOMPLETE:{len(result)}:missing={missing}')
    return result


def production_acquire(target: date) -> tuple[list[dict[str, Any]], list[str]]:
    ds = target.strftime('%Y%m%d')
    races: list[dict[str, Any]] = []
    failures: list[str] = []
    for venue, event_day in runner.discover(target):
        first_url = f'https://www.boatrace.jp/owpc/pc/race/racelist?hd={ds}&jcd={venue:02d}&rno=1'
        try:
            first_html = runner.get(first_url)
            deadlines = deadline_map(first_html)
        except Exception as exc:
            failures.append(f'{venue:02d}-DEADLINES:{type(exc).__name__}:{exc}')
            continue
        for rno in range(1, 13):
            url = f'https://www.boatrace.jp/owpc/pc/race/racelist?hd={ds}&jcd={venue:02d}&rno={rno}'
            try:
                html = first_html if rno == 1 else runner.get(url)
                entrants = runner.extract_entrants(html)
                races.append({'venue_code': venue, 'race_no': rno, 'event_day': event_day,
                    'deadline_time_jst': deadlines[rno], 'source_url': url,
                    'entrants': [entrant.__dict__ for entrant in entrants]})
            except Exception as exc:
                failures.append(f'{venue:02d}-{rno:02d}:{type(exc).__name__}:{exc}')
    return races, failures


runner.score_race = corrected_score_race
runner.extract_entrants = corrected_extract_entrants
runner.acquire = production_acquire

if __name__ == '__main__':
    raise SystemExit(runner.main())
