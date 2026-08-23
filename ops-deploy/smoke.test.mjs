import test from "node:test";
import assert from "node:assert/strict";
import worker, { VERSION, SOURCE_REF, dueStage } from "../source/index.js";

test("frozen free runner identity", () => {
  assert.equal(VERSION, "v1.0.0-free-20260823");
  assert.equal(SOURCE_REF, "cloudflare-free:v2.1-frozen-20260823-001");
});

test("health endpoint and JST scheduler", async () => {
  const response = await worker.fetch(
    new Request("https://worker.example/health"),
    { MORNING_ENABLED: "true" },
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.status, "ok");
  assert.equal(body.enabled, true);
  assert.equal(dueStage(new Date("2026-08-22T22:00:00.000Z"), { EARLY: [], LATE: [] }), "EARLY");
});
