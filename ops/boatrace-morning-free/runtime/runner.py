from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import requests
from bs4 import BeautifulSoup
from psycopg.types.json import Jsonb

JST = ZoneInfo("Asia/Tokyo")
STAGE_POLICY = "deadline-partition-1000-jst-20260812-001"
MORNING_POLICY = "s-a-20260812-001"
PURCHASE_POLICY = "s-only-20260809-001"
SOURCE_REF = "github-spec-rebuild:v2.1-frozen-neon-20260818"
SOURCE_MANIFEST_SCHEMA = "boatrace-official-http-manifest-v1"
MODEL_OOD_EVENT_DAY = "MODEL_OOD_EVENT_DAY"
MODEL_APPLICABILITY_FROZEN = "FROZEN_DAY_1_2"
MODEL_APPLICABILITY_OOD = "OOD_EVENT_DAY"
UA = "Mozilla/5.0 BOAT-RACE-Morning-Spec-Rebuild/1.0"

_source_artifacts: dict[str, dict[str, Any]] = {}


def canonical(v: Any) -> bytes:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(v: Any) -> str:
    b = v if isinstance(v, (bytes, bytearray)) else canonical(v)
    return hashlib.sha256(b).hexdigest()


def reset_source_manifest() -> None:
    _source_artifacts.clear()


def _record_source_artifact(
    url: str,
    body: bytes | bytearray,
    *,
    retrieved_at_utc: datetime | None = None,
) -> None:
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("SOURCE_ARTIFACT_BYTES_REQUIRED")
    raw = bytes(body)
    observed_at = retrieved_at_utc or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("SOURCE_RETRIEVAL_TIME_MUST_BE_AWARE")
    identity = {
        "source_url": url,
        "content_sha256": sha(raw),
        "byte_length": len(raw),
    }
    previous = _source_artifacts.get(url)
    if previous is not None:
        if any(previous.get(key) != value for key, value in identity.items()):
            raise RuntimeError(f"SOURCE_BYTES_CHANGED_DURING_RUN:{url}")
        return
    _source_artifacts[url] = {
        **identity,
        "retrieved_at_utc": observed_at.astimezone(timezone.utc).isoformat(),
    }


def source_manifest() -> dict[str, Any]:
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "artifacts": [_source_artifacts[url] for url in sorted(_source_artifacts)],
    }


def source_manifest_sha256() -> str:
    manifest = source_manifest()
    if not manifest["artifacts"]:
        raise RuntimeError("SOURCE_MANIFEST_EMPTY")
    return sha(manifest)


def build_handoff_entrants(raw_entrants: Any) -> list[dict[str, Any]]:
    """Validate and retain six entrants with complete P1/P2/P3 probabilities."""
    positions = ("1", "2", "3")
    if not isinstance(raw_entrants, list) or len(raw_entrants) != 6:
        raise ValueError("HANDOFF_ENTRANTS_INVALID")
    entrants: list[dict[str, Any]] = []
    lanes: set[int] = set()
    position_sums = {
        kind: {position: 0.0 for position in positions}
        for kind in ("market_probability", "performance_probability")
    }
    for raw in raw_entrants:
        if not isinstance(raw, dict):
            raise ValueError("HANDOFF_ENTRANTS_INVALID")
        lane = raw.get("lane")
        if (
            not isinstance(lane, int)
            or isinstance(lane, bool)
            or lane not in range(1, 7)
            or lane in lanes
        ):
            raise ValueError("HANDOFF_ENTRANTS_INVALID")
        lanes.add(lane)
        entrant = {
            "lane": lane,
            "racer_registration_no": raw["racer_no"],
            "racer_name": raw["racer_name"],
        }
        for kind in ("market_probability", "performance_probability"):
            probabilities = raw.get(kind)
            if not isinstance(probabilities, dict) or set(probabilities) != set(positions):
                raise ValueError("HANDOFF_PROBABILITY_INVALID")
            normalized: dict[str, float] = {}
            for position in positions:
                value = probabilities[position]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError("HANDOFF_PROBABILITY_INVALID")
                probability = float(value)
                if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                    raise ValueError("HANDOFF_PROBABILITY_INVALID")
                normalized[position] = probability
                position_sums[kind][position] += probability
            entrant[kind] = normalized
        entrants.append(entrant)
    if lanes != set(range(1, 7)):
        raise ValueError("HANDOFF_ENTRANTS_INVALID")
    for sums in position_sums.values():
        if any(
            not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9)
            for total in sums.values()
        ):
            raise ValueError("HANDOFF_PROBABILITY_SUM_INVALID")
    return sorted(entrants, key=lambda entrant: entrant["lane"])


def softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    es = [math.exp(x - m) for x in xs]
    s = sum(es)
    return [x / s for x in es]


def flatten_cols(df: pd.DataFrame) -> list[str]:
    out=[]
    for c in df.columns:
        if isinstance(c, tuple): out.append(" ".join(str(x) for x in c if str(x) != "nan").strip())
        else: out.append(str(c))
    return out


def num(v: Any) -> float | None:
    if v is None: return None
    m=re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",",""))
    return float(m.group()) if m else None


def integer(v: Any) -> int | None:
    x=num(v); return int(x) if x is not None else None


def first_col(cols: list[str], *parts: str) -> int | None:
    for i,c in enumerate(cols):
        if all(p in c for p in parts): return i
    return None


@dataclass
class Entrant:
    lane:int; racer_no:int; racer_name:str; racer_class:str; national_win:float
    avg_st_hundredths:int|None; f_count:int|None; l_count:int|None; motor_no:int|None; raw_two_rate_pct:float|None
    pretest_rank:int|None=None; comment_score:float=0.0; comment_missing:bool=True


@dataclass(frozen=True)
class MeetingContext:
    """Official target-day meeting context; unknown final-day numbers stay null."""

    event_day: int | None
    event_phase: str
    event_day_label: str


_FULLWIDTH_EVENT_TRANSLATION = str.maketrans(
    "０１２３４５６７８９／－",
    "0123456789/-",
)


def _target_date_scopes(text: str, target: date) -> list[tuple[str, int]]:
    normalized = text.translate(_FULLWIDTH_EVENT_TRANSLATION)
    date_patterns = (
        rf"{target.year}\s*年\s*0?{target.month}\s*月\s*0?{target.day}\s*日",
        rf"(?<!\d)0?{target.month}\s*月\s*0?{target.day}\s*日",
        rf"(?<!\d){target.year}[/-]0?{target.month}[/-]0?{target.day}(?!\d)",
        rf"(?<!\d){target.strftime('%Y%m%d')}(?!\d)",
    )
    locations: list[tuple[int, int]] = []
    for pattern in date_patterns:
        locations.extend(match.span() for match in re.finditer(pattern, normalized))
    if not locations:
        return []
    scopes: list[tuple[str, int]] = []
    for start_at, end_at in sorted(set(locations)):
        start = max(0, start_at - 60)
        end = min(len(normalized), end_at + 320)
        scopes.append((normalized[start:end], ((start_at + end_at) // 2) - start))
    return scopes


_KANJI_EVENT_DAYS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}
_EVENT_DAY_PATTERN = re.compile(
    r"初日|最終日|"
    r"(?<!\d)(?:第\s*)?(1[0-2]|[1-9])\s*日目(?!\d)|"
    r"(?<!\d)第\s*(1[0-2]|[1-9])\s*日(?!\d)|"
    r"(?:第\s*)?(十二|十一|十|[一二三四五六七八九])\s*日目|"
    r"第\s*(十二|十一|十|[一二三四五六七八九])\s*日"
)


def _nearest_event_day(scope: str, anchor: int) -> tuple[int | None, str] | None:
    matches = list(_EVENT_DAY_PATTERN.finditer(scope))
    if not matches:
        return None
    match = min(
        matches,
        key=lambda candidate: abs(((candidate.start() + candidate.end()) // 2) - anchor),
    )
    token = match.group(0)
    if token == "初日":
        return 1, "初日"
    if token == "最終日":
        return None, "最終日"
    if match.group(1) or match.group(2):
        event_day = int(match.group(1) or match.group(2))
    else:
        event_day = _KANJI_EVENT_DAYS[match.group(3) or match.group(4)]
    return event_day, f"{event_day}日目"


def event_context_from_text(text: str, target: date) -> MeetingContext | None:
    """Parse only the official page region bound to the requested target date."""

    plain = " ".join(BeautifulSoup(text, "html.parser").stripped_strings)
    scopes = _target_date_scopes(plain, target)
    if not scopes:
        return None

    parsed: list[MeetingContext] = []
    for scope, anchor in scopes:
        day = _nearest_event_day(scope, anchor)
        if day:
            event_day, label = day
            phase = "FINAL" if label == "最終日" else "REGULAR"
            parsed.append(MeetingContext(event_day, phase, label))
        elif "準優勝戦" in scope:
            parsed.append(MeetingContext(None, "SEMIFINAL", "準優勝戦日"))
        elif "優勝戦" in scope:
            parsed.append(MeetingContext(None, "FINAL", "優勝戦日"))

    if not parsed:
        return None
    event_days = {context.event_day for context in parsed if context.event_day is not None}
    event_day = next(iter(event_days)) if len(event_days) == 1 else None
    phase = "FINAL" if any(
        context.event_phase == "FINAL" for context in parsed
    ) else (
        "SEMIFINAL" if any(
            context.event_phase == "SEMIFINAL" for context in parsed
        ) else "REGULAR"
    )
    if event_day is not None:
        label = "初日" if event_day == 1 else f"{event_day}日目"
    elif phase == "FINAL":
        label = "最終日"
    else:
        label = "準優勝戦日"
    return MeetingContext(event_day, phase, label)


def get(url:str)->str:
    r=requests.get(url,headers={"User-Agent":UA},timeout=12)
    r.raise_for_status()
    _record_source_artifact(url, r.content)
    r.encoding=r.apparent_encoding
    return r.text


def event_day_from_text(text:str,target:date)->int|None:
    context = event_context_from_text(text, target)
    return context.event_day if context is not None else None


def discover(
    target: date,
    source_failures: list[str] | None = None,
) -> list[tuple[int, MeetingContext]]:
    ds=target.strftime("%Y%m%d"); result=[]
    for venue in range(1,25):
        try: html=get(f"https://www.boatrace.jp/owpc/pc/race/raceindex?hd={ds}&jcd={venue:02d}")
        except Exception as exc:
            if source_failures is not None:
                source_failures.append(f"{venue:02d}-INDEX:{type(exc).__name__}:{exc}")
            continue
        context = event_context_from_text(html, target)
        if context is not None:
            result.append((venue, context))
    return result


def parse_deadline(html:str)->time:
    text=" ".join(BeautifulSoup(html,"html.parser").stripped_strings)
    pats=[r"締切予定\s*(\d{1,2}):(\d{2})",r"締切\s*(\d{1,2}):(\d{2})",r"(\d{1,2}):(\d{2})\s*締切"]
    for p in pats:
        m=re.search(p,text)
        if m:return time(int(m.group(1)),int(m.group(2)))
    raise ValueError("DEADLINE_NOT_FOUND")


def extract_entrants(html:str)->list[Entrant]:
    tables=pd.read_html(html)
    best=None
    for df in tables:
        if len(df)<6: continue
        cols=flatten_cols(df)
        joined=" ".join(cols)
        if ("選手" in joined or "登録" in joined) and ("全国" in joined or "勝率" in joined) and ("モーター" in joined):
            best=df.copy(); best.columns=cols; break
    if best is None: raise ValueError("RACELIST_TABLE_NOT_FOUND")
    df=best.iloc[:6].copy(); cols=list(df.columns)
    namei=first_col(cols,"選手")
    if namei is None: namei=first_col(cols,"登録")
    nationi=first_col(cols,"全国","勝率")
    if nationi is None: nationi=first_col(cols,"全国")
    sti=first_col(cols,"平均","ST")
    moti=first_col(cols,"モーター")
    out=[]
    for pos,(_,row) in enumerate(df.iterrows(),start=1):
        cells=[str(x) for x in row.tolist()]
        alltxt=" ".join(cells)
        ident=(str(row.iloc[namei]) if namei is not None else alltxt)
        no_m=re.search(r"\b(\d{4})\b",ident)
        cls_m=re.search(r"\b(A1|A2|B1|B2)\b",ident)
        if not(no_m and cls_m):
            no_m=re.search(r"\b(\d{4})\b",alltxt); cls_m=re.search(r"\b(A1|A2|B1|B2)\b",alltxt)
        if not(no_m and cls_m): raise ValueError(f"ENTRANT_ID_PARSE_FAILED:{pos}")
        racer_no=int(no_m.group(1)); racer_class=cls_m.group(1)
        # Name is the non-numeric Japanese fragment nearest the identity cell.
        name_parts=re.findall(r"[一-龯ぁ-んァ-ヶー]{2,}(?:\s+[一-龯ぁ-んァ-ヶー]{1,})?",ident)
        racer_name=name_parts[0].strip() if name_parts else f"選手{racer_no}"
        nw=num(row.iloc[nationi]) if nationi is not None else None
        if nw is None:
            candidates=[num(x) for x in cells]
            candidates=[x for x in candidates if x is not None and 0<=x<=10]
            nw=candidates[0] if candidates else None
        if nw is None: raise ValueError(f"NATIONAL_WIN_PARSE_FAILED:{pos}")
        avg=None
        if sti is not None:
            st=num(row.iloc[sti]); avg=round(st*100) if st is not None and st<1 else (round(st) if st is not None else None)
        f_m=re.search(r"F\s*(\d+)",alltxt); l_m=re.search(r"L\s*(\d+)",alltxt)
        motor_no=None; raw=None
        if moti is not None:
            mt=str(row.iloc[moti]); vals=re.findall(r"\d+(?:\.\d+)?",mt)
            if vals: motor_no=int(float(vals[0]))
            if len(vals)>1: raw=float(vals[1])
        out.append(Entrant(pos,racer_no,racer_name,racer_class,float(nw),avg,int(f_m.group(1)) if f_m else 0,int(l_m.group(1)) if l_m else 0,motor_no,raw))
    if len(out)!=6: raise ValueError("SIX_ENTRANTS_REQUIRED")
    return out


def acquire(target:date)->tuple[list[dict[str,Any]],list[str]]:
    reset_source_manifest()
    ds=target.strftime("%Y%m%d"); races=[]; failures=[]
    for venue,context in discover(target, failures):
        for rno in range(1,13):
            url=f"https://www.boatrace.jp/owpc/pc/race/racelist?hd={ds}&jcd={venue:02d}&rno={rno}"
            try:
                html=get(url); deadline=parse_deadline(html); entrants=extract_entrants(html)
                races.append({"venue_code":venue,"race_no":rno,"event_day":context.event_day,"event_phase":context.event_phase,"event_day_label":context.event_day_label,"deadline_time_jst":deadline.isoformat(),"source_url":url,"entrants":[e.__dict__ for e in entrants]})
            except Exception as exc: failures.append(f"{venue:02d}-{rno:02d}:{type(exc).__name__}:{exc}")
    return races,failures


def load_config(cur)->tuple[dict[str,Any],dict[str,Any]]:
    cur.execute("SELECT config_json FROM public.extraction_runs WHERE config_version='v2_phase3_20260808_001' AND status IN ('completed','stopped') ORDER BY created_at DESC LIMIT 1")
    v2=cur.fetchone()[0]
    cur.execute("SELECT config_json FROM public.extraction_runs WHERE config_version='v21_phase3_20260809_001' AND status='completed' ORDER BY created_at DESC LIMIT 1")
    v21=cur.fetchone()[0]
    return v2,v21


def features(e:Entrant,venue:int,event_day:int)->list[float]:
    f=[1.0 if e.racer_class=='A1' else 0.0,1.0 if e.racer_class=='A2' else 0.0,1.0 if e.racer_class=='B2' else 0.0,(e.national_win-5.0)/2.0]
    f += [1.0 if e.lane==lane else 0.0 for lane in range(2,7)]
    for v in range(2,25): f += [1.0 if venue==v and e.lane==lane else 0.0 for lane in range(2,7)]
    f += [1.0 if event_day==2 and e.lane==lane else 0.0 for lane in range(2,7)]
    return f


def pretest_point(rank:int|None)->float:
    if rank is None:return 0.0
    if rank==1:return 1.0
    if 2<=rank<=10:return .5
    if 11<=rank<=30:return 0.0
    return -.5


def motor_point(e:Entrant)->tuple[float,bool]:
    # Frozen rule: unverifiable recent-5 history => entire motor component zero.
    return 0.0,True


def frozen_model_event_eligible(race: dict[str, Any]) -> bool:
    event_day = race.get("event_day")
    return (
        isinstance(event_day, int)
        and not isinstance(event_day, bool)
        and event_day in (1, 2)
        and race.get("event_phase", "REGULAR") == "REGULAR"
    )


def ood_event_day_skip(race: dict[str, Any]) -> dict[str, Any]:
    """Keep an official race auditable without applying an out-of-domain model."""

    entrants = race.get("entrants")
    if not isinstance(entrants, list) or len(entrants) != 6:
        raise ValueError("SIX_ENTRANTS_REQUIRED")
    return {
        **race,
        "signals": [],
        "tickets": [],
        "race_grade": "B",
        "decision_state": "SKIP",
        "purchase_decision": "SKIP",
        "model_applicability": MODEL_APPLICABILITY_OOD,
        "reason_codes": [MODEL_OOD_EVENT_DAY],
        "feature_degraded": True,
        "degradation_codes": [MODEL_OOD_EVENT_DAY],
    }


def score_race(r:dict[str,Any],v2:dict[str,Any],v21:dict[str,Any])->dict[str,Any]:
    if not frozen_model_event_eligible(r):
        return ood_event_day_skip(r)
    entrants=[Entrant(**x) for x in r['entrants']]
    betas=v2['models']['market']['frozen_beta']; deltas=v2['models']['performance']['base_delta']; gammas=v2['models']['performance']['evaluation_gamma']
    evals=[]; conf=[]
    for e in entrants:
        pp=pretest_point(e.pretest_rank); mp,missing=motor_point(e); ev=pp+mp+e.comment_score
        evals.append((pp,mp,ev,missing)); conf.append(max(.6,1.0-(.25 if missing else 0.0)))
    market={}; perf={}
    for p in (1,2,3):
        X=[features(e,r['venue_code'],r['event_day']) for e in entrants]
        b=betas[str(p)]; d=deltas[str(p)]; g=gammas[str(p)]
        market[str(p)]=softmax([sum(a*z for a,z in zip(b,x)) for x in X])
        perf[str(p)]=softmax([sum((a+dd)*z for a,dd,z in zip(b,d,x))+g*evals[i][2] for i,x in enumerate(X)])
    pos_cfg=v2['extraction']['positive']['by_position']; down_cfg=v2['extraction']['downside']['by_position']
    signals=[]; pos_keys=set(); down_keys=set()
    for i,e in enumerate(entrants):
        ev=evals[i][2]
        if e.racer_class!='B2' and e.lane!=1 and e.national_win>3.0 and ev>=v2['extraction']['positive']['condition_min']:
            for p in (1,2,3):
                pc=pos_cfg[str(p)]; m=market[str(p)][i]; q=perf[str(p)][i]
                ratio=q/m if m>0 else 999
                if q>=pc['min_performance_probability'] and ratio>=pc['ratio_perf_over_market_gte']:
                    strength=conf[i]*(ratio-1)/(pc['ratio_perf_over_market_gte']-1)
                    pri='S' if strength>=3 else ('A' if strength>=1.75 else 'B')
                    signals.append({"lane":e.lane,"direction":"value","scope":str(p),"ratio":ratio,"priority":pri,"strength":strength}); pos_keys.add((e.lane,p))
        if e.racer_class in v2['extraction']['downside']['subject_classes'] and ev<=v2['extraction']['downside']['condition_max']:
            for p in (1,3):
                dc=down_cfg[str(p)]; m=market[str(p)][i]; q=perf[str(p)][i]; ratio=m/q if q>0 else 999
                if dc.get('enabled') and m>=dc['min_market_probability'] and ratio>=dc['ratio_market_over_perf_gte']:
                    strength=conf[i]*(ratio-1)/(dc['ratio_market_over_perf_gte']-1)
                    pri='S' if strength>=3 else ('A' if strength>=1.75 else 'B')
                    signals.append({"lane":e.lane,"direction":"downside","scope":str(p),"ratio":ratio,"priority":pri,"strength":strength}); down_keys.add((e.lane,p))
    mins={1:.12,2:.18,3:.15}; tickets=[]
    for trip in itertools.permutations(range(1,7),3):
        ok=True; positive=False
        for p,lane in enumerate(trip,1):
            i=lane-1
            if perf[str(p)][i]<mins[p] or (lane,p) in down_keys: ok=False; break
            positive=positive or ((lane,p) in pos_keys)
        if ok and positive:
            tickets.append({"combo":"-".join(map(str,trip)),"market_model_strength":market['1'][trip[0]-1]*market['2'][trip[1]-1]*market['3'][trip[2]-1],"performance_model_strength":perf['1'][trip[0]-1]*perf['2'][trip[1]-1]*perf['3'][trip[2]-1]})
    tickets.sort(key=lambda x:x['market_model_strength'],reverse=True)
    entrants_out=[]
    for i,e in enumerate(entrants):
        pp,mp,ev,missing=evals[i]
        mr={str(p):market[str(p)][i] for p in (1,2,3)}; pr={str(p):perf[str(p)][i] for p in (1,2,3)}
        entrants_out.append({"lane":e.lane,"racer_no":e.racer_no,"racer_name":e.racer_name,"racer_class":e.racer_class,"national_win":e.national_win,"pretest_point":pp,"motor_point":mp,"comment_score":e.comment_score,"evaluation_score":ev,"evidence_confidence":conf[i],"motor_recent5_missing":missing,"comment_missing":e.comment_missing,"market_probability":mr,"performance_probability":pr,"market_top3_raw":sum(mr.values()),"performance_top3_raw":sum(pr.values()),"market_top3":sum(mr.values()),"performance_top3":sum(pr.values())})
    max_strength=max((x['strength'] for x in signals),default=0.0); grade='S' if max_strength>=3 else ('A' if max_strength>=1.75 else 'B')
    return {**r,"signals":signals,"tickets":tickets,"entrants":entrants_out,"race_grade":grade,"model_applicability":MODEL_APPLICABILITY_FROZEN,"feature_degraded":True,"degradation_codes":["PRETEST_OPTIONAL_NEUTRAL_IF_MISSING","MOTOR_RECENT5_UNAVAILABLE","COMMENT_OPTIONAL_NEUTRAL","V21_TOP3_STRUCTURAL_OVERLAY_SUPPRESSED"]}


def dt_deadline(target:date,tstr:str)->datetime:
    hh,mm,*ss=map(int,tstr.split(':')); return datetime.combine(target,time(hh,mm,ss[0] if ss else 0),JST)


def create_run(cur,target:date,stage:str,scheduled:datetime)->int:
    logic='v2.1-prod-20260809-003' if stage=='EARLY' else 'v2.1-prod-reporter-20260812-004'
    cur.execute("SELECT COALESCE(max(attempt_no),0)+1 FROM ux_app.run_executions WHERE target_date=%s AND stage=%s",(target,stage)); attempt=cur.fetchone()[0]
    key=sha({"target_date":target.isoformat(),"stage":stage,"attempt":attempt,"scheduled_for":scheduled.isoformat(),"runner":SOURCE_REF})
    cur.execute("INSERT INTO ux_app.run_executions(execution_key,target_date,stage,attempt_no,scheduled_for,logic_version,stage_policy_id,morning_notification_policy,purchase_notification_policy) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",(key,target,stage,attempt,scheduled,logic,STAGE_POLICY,MORNING_POLICY,PURCHASE_POLICY)); rid=cur.fetchone()[0]
    cur.execute("INSERT INTO ux_app.run_events(run_execution_id,event_seq,event_at,lifecycle_status,details) VALUES(%s,1,clock_timestamp(),'RUNNING',%s)",(rid,Jsonb({"runner":SOURCE_REF,"stage":stage})))
    return rid


def persist(cur,rid:int,target:date,stage:str,scored:list[dict[str,Any]],source_failures:list[str],now:datetime)->dict[str,int]:
    source_evidence=source_manifest(); source_artifact_sha256=source_manifest_sha256()
    scorer_artifact_sha256=sha({"schema_version":"boatrace-morning-scorer-output-manifest-v1","races":scored})
    owned=[]
    for r in scored:
        dl=dt_deadline(target,r['deadline_time_jst']); h=dl.astimezone(JST).hour
        if (stage=='EARLY' and h<10) or (stage=='LATE' and h>=10): owned.append((r,dl))
    actionable=[(r,dl) for r,dl in owned if now < dl-timedelta(minutes=10)]
    candidates=[(r,dl) for r,dl in actionable if r['race_grade'] in ('S','A')]
    s=[x for x in candidates if x[0]['race_grade']=='S']; a=[x for x in candidates if x[0]['race_grade']=='A']
    for r,dl in candidates:
        payload={"schema_version":"boatrace-ux-morning-projection-v1","target_date":target.isoformat(),"stage":stage,"venue_code":r['venue_code'],"race_no":r['race_no'],"morning_grade":r['race_grade'],"deadline_time_jst":r['deadline_time_jst'],"signals":r['signals'],"tickets":r['tickets'],"entrants":r['entrants'],"feature_degraded":True,"degradation_codes":r['degradation_codes']}
        ph=sha(payload); pk=sha({"kind":"MORNING","target_date":target.isoformat(),"venue":r['venue_code'],"race":r['race_no'],"revision":1})
        cur.execute("INSERT INTO ux_app.race_projections(projection_key,run_execution_id,revision,projection_kind,target_date,venue_code,race_no,official_deadline_at,morning_grade,canonical_decision,reason_codes,attempt_present,publishable_plan_verified,operational_code,display_state,classifier_policy_id,performance_kind,source_ref,source_artifact_sha256,payload,payload_sha256,spend_yen,projected_at,actionable_until) VALUES(%s,%s,1,'MORNING',%s,%s,%s,%s,%s,NULL,'[]'::jsonb,false,false,NULL,%s,NULL,NULL,%s,%s,%s,%s,0,clock_timestamp(),NULL) ON CONFLICT (projection_kind,target_date,venue_code,race_no,revision) DO NOTHING",(pk,rid,target,r['venue_code'],r['race_no'],dl,r['race_grade'],r['race_grade'],SOURCE_REF,source_artifact_sha256,Jsonb(payload),ph))
    handoffs=0
    logic='v2.1-prod-20260809-003' if stage=='EARLY' else 'v2.1-prod-reporter-20260812-004'
    for r,dl in s:
        if not r['tickets']: continue
        entrants=build_handoff_entrants(r['entrants'])
        payload={"schema_version":"purchase-assist-handoff-v2","target_date":target.isoformat(),"venue_code":r['venue_code'],"race_no":r['race_no'],"deadline_at":dl.isoformat(),"logic_version":logic,"notification_policy":PURCHASE_POLICY,"race_grade":"S","morning_hypothesis":"Morning V2.1 frozen-spec value signal","scorer_tickets":r['tickets'],"entrants":entrants,"source_scorer_ref":SOURCE_REF}
        psha=sha(payload); hkey=sha({"target":target.isoformat(),"venue":r['venue_code'],"race":r['race_no'],"logic":logic,"source":SOURCE_REF})
        cur.execute("INSERT INTO purchase_assist.handoffs(handoff_key,schema_version,target_date,venue_code,race_no,deadline_at,logic_version,notification_policy,source_scorer_ref,source_scorer_sha256,source_scorer_artifact_id,race_grade,morning_hypothesis,scorer_tickets,entrants,canonical_payload,payload_sha256) VALUES(%s,'purchase-assist-handoff-v2',%s,%s,%s,%s,%s,%s,%s,%s,NULL,'S',%s,%s,%s,%s,%s) ON CONFLICT (handoff_key) DO NOTHING RETURNING id",(hkey,target,r['venue_code'],r['race_no'],dl,logic,PURCHASE_POLICY,SOURCE_REF,scorer_artifact_sha256,payload['morning_hypothesis'],Jsonb(r['tickets']),Jsonb(entrants),Jsonb(payload),psha))
        if cur.fetchone(): handoffs+=1
    source_state='SOURCE_OK' if not source_failures else 'PARTIAL_SOURCE'
    outcome='CANDIDATES_FOUND' if candidates else ('PARTIAL_SOURCE' if source_failures else 'NO_CANDIDATES')
    cur.execute("INSERT INTO ux_app.run_events(run_execution_id,event_seq,event_at,lifecycle_status,outcome,source_state,history_state,feature_state,inspected_count,actionable_count,s_count,a_count,handoff_count,source_artifact_ref,source_artifact_sha256,details,finished_at) VALUES(%s,2,clock_timestamp(),'SUCCEEDED',%s,%s,'DEGRADED_HISTORY','FEATURE_DEGRADED',%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",(rid,outcome,source_state,len(owned),len(actionable),len(s),len(a),handoffs,SOURCE_REF,source_artifact_sha256,Jsonb({"stage":stage,"runner":SOURCE_REF,"full_inspected_count":len(scored),"full_actionable_count":len(actionable),"source_failures":source_failures[:100],"source_manifest":source_evidence,"source_manifest_sha256":source_artifact_sha256,"source_artifact_count":len(source_evidence["artifacts"]),"scorer_artifact_sha256":scorer_artifact_sha256,"degraded":True,"top3_structural_overlay_suppressed":True})))
    return {"inspected":len(owned),"actionable":len(actionable),"s":len(s),"a":len(a),"handoff":handoffs}


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--target-date'); ap.add_argument('--stage',choices=['EARLY','LATE'],required=True); ap.add_argument('--scheduled-for'); args=ap.parse_args()
    now=datetime.now(JST); target=date.fromisoformat(args.target_date) if args.target_date else now.date(); scheduled=datetime.fromisoformat(args.scheduled_for) if args.scheduled_for else now
    if scheduled.tzinfo is None: scheduled=scheduled.replace(tzinfo=JST)
    db=os.environ['DATABASE_URL']
    races,failures=acquire(target)
    if not races: raise SystemExit(f"NO_OFFICIAL_RACES: failures={failures[:5]}")
    with psycopg.connect(db) as conn:
        with conn.cursor() as cur:
            v2,v21=load_config(cur); scored=[score_race(r,v2,v21) for r in races]; rid=create_run(cur,target,args.stage,scheduled); result=persist(cur,rid,target,args.stage,scored,failures,now); conn.commit()
    print(json.dumps({"run_id":rid,"target_date":target.isoformat(),"stage":args.stage,"result":result,"source_failures":len(failures)},ensure_ascii=False))
    return 0

if __name__=='__main__': raise SystemExit(main())
