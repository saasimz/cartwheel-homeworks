"""Static guardrails for the scenario-centered Trace Lab review workflow."""

import gzip
import json
from pathlib import Path

from analysis.server import _json_response


UI = Path(__file__).parents[1] / "analysis" / "ui" / "index.html"


def test_review_navigation_groups_traces_by_scenario() -> None:
    html = UI.read_text()

    assert "function visibleScenarios()" in html
    assert "`${state.cur + 1} / ${visibleScenarios().length} scenarios`" in html
    assert "group.samples.some(sample => sample.trace_id === tid)" in html
    assert "supporting traces" in html


def test_annotations_carry_scenario_and_run_identity() -> None:
    html = UI.read_text()

    assert "scenario_id: pendingCtx.scenario_id" in html
    assert "run_id: pendingCtx.run_id" in html
    assert "source: 'scenario_comment'" in html
    assert "Save scenario comment" in html


def test_large_trace_payloads_are_gzipped_for_the_browser() -> None:
    payload = [{"trace": "evidence" * 500}]

    body, compressed = _json_response(payload, "br, gzip, deflate")

    assert compressed is True
    assert json.loads(gzip.decompress(body)) == payload


def test_timeline_distinguishes_model_tools_summary_and_answer() -> None:
    html = UI.read_text()

    assert "Model decision" in html
    assert "Published reasoning summary" in html
    assert "Tool call" in html
    assert "Tool output" in html
    assert "Final answer" in html
    assert "Provider-published summary — not raw chain-of-thought" in html


def test_browser_retries_tunneled_api_reads_without_blanking_all_state() -> None:
    html = UI.read_text()

    assert "for (let attempt = 0; attempt < 3; attempt++)" in html
    assert "const safe = (path, fallback)" in html
    assert "some TraceLab data could not be loaded" in html
