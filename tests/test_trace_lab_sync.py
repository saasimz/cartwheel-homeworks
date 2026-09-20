"""Offline tests for safe Trace Lab synchronization and run grouping."""

from __future__ import annotations

import json
from types import SimpleNamespace

from analysis.live_sync import TraceSync, build_runs, merge_samples


def _trace(trace_id: str, scenario_id: str, run_id: str | None = None) -> dict:
    meta = {"scenario_id": scenario_id, "role": "shopper"}
    if run_id:
        meta["run_id"] = run_id
    return {
        "trace_id": trace_id,
        "trace": [{"role": "user", "text": "hello"}],
        "text": "user: hello",
        "features": {"turn_count": 1},
        "meta": meta,
        "timestamp": "2026-09-20T12:00:00+00:00",
        "models": ["gpt-5.5"],
        "observations": [],
        "input": "hello",
        "output": "hi",
        "metadata": {},
        "permalink": None,
    }


def test_merge_upserts_without_removing_old_samples_or_flags() -> None:
    old = {
        "trace_id": "old",
        "reason": "selected",
        "trace": [],
        "text": "",
        "features": {},
        "meta": {"scenario_id": "pilot-001"},
        "flags": ["reviewed"],
    }
    updated = _trace("old", "pilot-001")
    new = _trace("new", "support-0001", "run-final")
    rows, added, changed = merge_samples([old], [updated, new])
    assert {row["trace_id"] for row in rows} == {"old", "new"}
    assert added == 1
    assert changed == 1
    assert next(row for row in rows if row["trace_id"] == "old")["flags"] == ["reviewed"]


def test_runs_keep_recorded_and_legacy_batches_separate() -> None:
    traces = [_trace("p1", "pilot-001"), _trace("s1", "support-0001")]
    samples, _, _ = merge_samples([], traces + [_trace("s2", "support-0002", "run-final")])
    runs = {run["run_id"]: run for run in build_runs(samples)}
    assert set(runs) == {"inferred-pilot", "inferred-support", "run-final"}
    assert runs["inferred-pilot"]["inferred"] is True
    assert runs["run-final"]["inferred"] is False


def test_sync_writes_trace_indexes_without_touching_annotations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(tmp_path))
    annotations = [{"id": "human-1", "trace_id": "trace-1", "note": "keep me"}]
    (tmp_path / "annotations.json").write_text(json.dumps(annotations))

    raw_trace = {
        "id": "trace-1",
        "timestamp": "2026-09-20T12:00:00+00:00",
        "input": "hello",
        "output": "hi",
        "metadata": {
            "attributes": {
                "cartwheel.scenario_id": "support-0001",
                "cartwheel.run_id": "run-one",
            }
        },
        "observations": [],
    }

    class FakeTraceApi:
        def list(self, *, page, limit):
            data = [raw_trace] if page == 1 else []
            return SimpleNamespace(data=data)

        def get(self, trace_id):
            assert trace_id == "trace-1"
            return raw_trace

    client = SimpleNamespace(api=SimpleNamespace(trace=FakeTraceApi()))
    status = TraceSync(interval=60, client=client).run_once()

    assert status["trace_count"] == 1
    assert json.loads((tmp_path / "annotations.json").read_text()) == annotations
    assert json.loads((tmp_path / "runs.json").read_text())[0]["run_id"] == "run-one"
    assert json.loads((tmp_path / "samples.json").read_text())[0]["trace_id"] == "trace-1"
