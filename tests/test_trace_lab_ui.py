"""Static guardrails for the scenario-centered Trace Lab review workflow."""

from pathlib import Path


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
