"""Homework 4 review-app contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from analysis import server
from analysis.helpers import langfuse_io
from analysis.helpers.normalization import _merge_multi_turn


ROOT = Path(__file__).parents[1]
UI = ROOT / "analysis" / "review_app" / "index.html"


def _trace(
    trace_id: str,
    *,
    session_id: str | None,
    scenario_id: str = "support-0001",
    run_id: str = "run-one",
    timestamp: str,
) -> dict:
    meta = {"scenario_id": scenario_id, "run_id": run_id}
    if session_id:
        meta["session_id"] = session_id
    return {
        "id": trace_id,
        "trace_id": trace_id,
        "timestamp": timestamp,
        "trace": [{"role": "user", "text": trace_id}],
        "observations": [],
        "models": [],
        "text": trace_id,
        "features": {"turn_count": 1, "tool_call_count": 0},
        "meta": meta,
    }


def test_review_app_groups_by_real_session_and_marks_legacy_fallback() -> None:
    html = UI.read_text()

    assert "if (sessionId) return `session::${sessionId}`" in html
    assert "legacy grouping: run + scenario" in html
    assert "group.samples.sort" in html
    assert "session: ${escape(group.sessionId)}" in html


def test_review_app_exposes_required_part_a_workflows() -> None:
    html = UI.read_text()

    assert "Save scenario comment" in html
    assert 'id="review-progress"' in html
    assert "reviewed traces" in html
    assert "structured judgments" in html
    assert "state.suggestions" in html
    assert "Accept selected" in html
    assert "Dismiss selected" in html
    assert "visibleSamples().map(sample =>" in html
    assert "structuredDecision(sample.trace_id, mode.name)" in html


def test_multi_turn_merge_prefers_session_over_scenario() -> None:
    rows = _merge_multi_turn(
        [
            _trace("one", session_id="session-a", timestamp="2026-09-20T10:00:00Z"),
            _trace("two", session_id="session-a", timestamp="2026-09-20T10:01:00Z"),
            _trace("three", session_id="session-b", timestamp="2026-09-20T10:02:00Z"),
            _trace("four", session_id=None, run_id="legacy-a", timestamp="2026-09-20T10:03:00Z"),
            _trace("five", session_id=None, run_id="legacy-b", timestamp="2026-09-20T10:04:00Z"),
        ]
    )

    assert len(rows) == 4
    real = next(row for row in rows if row["meta"].get("session_id") == "session-a")
    assert [message["text"] for message in real["trace"]] == ["one", "two"]
    assert real["meta"]["session_inferred"] is False
    legacy = [row for row in rows if row["meta"].get("session_inferred") is True]
    assert len(legacy) == 2


def test_structured_label_writes_local_history_and_langfuse_score(
    tmp_path: Path, monkeypatch
) -> None:
    annotation = {
        "id": "label-1",
        "trace_id": "trace-1",
        "mode": "example_failure",
        "label": 1,
        "note": "present",
        "source": "human_structured_label",
        "ts": "2026-09-29T00:00:00Z",
    }
    writes: list[dict] = []
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(langfuse_io, "is_configured", lambda: True)
    monkeypatch.setattr(langfuse_io, "_client", lambda: object())
    monkeypatch.setattr(
        langfuse_io,
        "write_label_score",
        lambda **kwargs: writes.append(kwargs),
    )

    server._persist_label_history([annotation])
    written = server._sync_annotation_scores([annotation])

    history = tmp_path / "labels" / "example_failure.jsonl"
    assert json.loads(history.read_text().strip())["trace_id"] == "trace-1"
    assert written == 1
    assert writes[0]["mode"] == "example_failure"
    assert writes[0]["label"] == 1

