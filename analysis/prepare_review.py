"""Prepare real Cartwheel traces for the custom review interface.

The error-analysis skill deliberately keeps selection separate from display.
This command calls the supplied diversity selector, then adds the scenario
context that makes Module 1 traces easier to review: exact user turns,
expected evidence, retry markers, observed models, and tools.  It also writes
the two-dimensional structural projection consumed by the map view.

Example:
    uv run python -m analysis.prepare_review \
      traces/support_traces.json scenarios/support_scenarios.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from analysis.helpers import select_traces
from analysis.helpers import _state
from analysis.helpers import selection


FEATURE_NAMES = (
    "turn_count",
    "tool_call_count",
    "distinct_tools",
    "has_retrieval",
    "tokens",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL artifact and fail on malformed records.

    A review UI must not silently omit a scenario because one line was bad.
    """
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        records.append(record)
    return records


def _attribute_map(metadata: Any) -> dict[str, Any]:
    """Return OpenTelemetry attributes without exposing unrelated metadata."""
    if not isinstance(metadata, dict):
        return {}
    attributes = metadata.get("attributes")
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except ValueError:
            attributes = None
    return attributes if isinstance(attributes, dict) else {}


def _scenario_id(trace: dict[str, Any]) -> str | None:
    """Find the stable scenario id stamped by the Cartwheel endpoint."""
    direct = trace.get("cartwheel_scenario_id")
    if direct:
        return str(direct)
    attributes = _attribute_map(trace.get("metadata"))
    value = attributes.get("cartwheel.scenario_id")
    return str(value) if value else None


def _text_parts(value: Any) -> list[str]:
    """Extract text recursively from Langfuse's message-part representation."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_text_parts(item))
        return out
    if isinstance(value, dict):
        out = []
        for key in ("content", "text", "parts"):
            if key in value:
                out.extend(_text_parts(value[key]))
        return out
    return []


def _trace_tools(trace: dict[str, Any]) -> list[str]:
    """Return tool names in first-seen order for compact trace headers."""
    names: list[str] = []
    for observation in trace.get("observations") or []:
        if not isinstance(observation, dict):
            continue
        attributes = _attribute_map(observation.get("metadata"))
        is_tool = observation.get("type") == "TOOL" or attributes.get(
            "traceloop.span.kind"
        ) == "tool"
        name = observation.get("name")
        if is_tool and name and str(name) not in names:
            names.append(str(name))
    return names


def _trace_models(trace: dict[str, Any]) -> list[str]:
    """Return observed model identifiers without repeating the same model."""
    models: list[str] = []
    for observation in trace.get("observations") or []:
        if not isinstance(observation, dict):
            continue
        attributes = _attribute_map(observation.get("metadata"))
        model = observation.get("model") or attributes.get("gen_ai.request.model")
        if model and str(model) not in models:
            models.append(str(model))
    return models


def _scenario_context(
    traces: list[dict[str, Any]], scenarios: list[dict[str, Any]]
) -> dict[str, Any]:
    """Join trace identifiers to scenario turns and mark repeated attempts."""
    scenario_map = {str(item["id"]): item for item in scenarios}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        scenario_id = _scenario_id(trace)
        if scenario_id:
            grouped[scenario_id].append(trace)

    trace_context: dict[str, Any] = {}
    scenario_context: dict[str, Any] = {}
    for scenario_id, scenario in scenario_map.items():
        messages = [scenario.get("opening_message", "")]
        messages.extend(scenario.get("followups") or [])
        expected = scenario.get("expected") or {}
        records = sorted(grouped.get(scenario_id, []), key=lambda row: row.get("timestamp") or "")
        attempts: dict[int, int] = defaultdict(int)
        trace_ids: list[str] = []
        for trace in records:
            input_text = "\n".join(_text_parts(trace.get("input")))
            turn_index = 0
            for index, message in enumerate(messages):
                if message and message in input_text:
                    turn_index = index
                    break
            attempts[turn_index] += 1
            trace_id = str(trace.get("id") or trace.get("trace_id"))
            trace_ids.append(trace_id)
            trace_context[trace_id] = {
                "scenario_id": scenario_id,
                "timestamp": trace.get("timestamp"),
                "turn_index": turn_index + 1,
                "turn_count": len(messages),
                "attempt": attempts[turn_index],
                "is_retry": attempts[turn_index] > 1,
                "models": _trace_models(trace),
                "tools": _trace_tools(trace),
            }
        scenario_context[scenario_id] = {
            "scenario_group": scenario.get("scenario_group"),
            "role": (scenario.get("tuple") or {}).get("role"),
            "messages": messages,
            "expected_outcome": expected.get("outcome"),
            "expected": expected.get("reason") or expected.get("criterion"),
            "evidence": expected.get("source"),
            "trace_ids": trace_ids,
        }
    return {"scenarios": scenario_context, "traces": trace_context}


def _projection(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Project structural trace features to two dimensions using PCA.

    PCA is used only for navigation; a cluster is a prompt to inspect a trace,
    never evidence that the trace failed.
    """
    if not traces:
        return {"nodes": [], "clusters": [], "feature_names": list(FEATURE_NAMES)}
    vectors = np.asarray(
        [
            [float(trace.get("features", {}).get(name, 0)) for name in FEATURE_NAMES]
            for trace in traces
        ],
        dtype=float,
    )
    means = vectors.mean(axis=0)
    scales = vectors.std(axis=0)
    scales[scales == 0] = 1.0
    standardized = (vectors - means) / scales
    _, _, right = np.linalg.svd(standardized, full_matrices=False)
    basis = right[: min(2, right.shape[0])].T
    coords = standardized @ basis
    if coords.shape[1] == 1:
        coords = np.column_stack([coords[:, 0], np.zeros(len(traces))])
    clusters = selection._kmeans(standardized.tolist(), k=min(8, len(traces)))
    nodes = []
    for index, trace in enumerate(traces):
        nodes.append(
            {
                "trace_id": trace["trace_id"],
                "x": round(float(coords[index, 0]), 6),
                "y": round(float(coords[index, 1]), 6),
                "cluster": int(clusters[index]),
                "scenario_id": trace.get("meta", {}).get("scenario_id"),
                "role": trace.get("meta", {}).get("role"),
                "features": trace.get("features", {}),
            }
        )
    return {
        "nodes": nodes,
        "clusters": sorted(set(clusters)),
        "feature_names": list(FEATURE_NAMES),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_export", type=Path)
    parser.add_argument("scenarios", type=Path)
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--strategy", choices=("diversity", "random", "outlier"), default="diversity")
    args = parser.parse_args()

    scenarios = _load_jsonl(args.scenarios)
    samples = select_traces(
        args.trace_export,
        k=args.sample_size,
        strategy=args.strategy,
    )
    traces = selection.load_traces(args.trace_export)
    raw_payload = json.loads(args.trace_export.read_text())
    raw_traces = raw_payload.get("traces", raw_payload) if isinstance(raw_payload, dict) else raw_payload
    if not isinstance(raw_traces, list):
        raise ValueError("trace export must contain a list of traces")

    _state.write_json(
        _state.state_path("scenario_context.json"),
        _scenario_context(raw_traces, scenarios),
    )
    _state.write_json(_state.state_path("graph.json"), _projection(traces))
    print(
        f"Prepared {len(samples)} review samples from {len(traces)} traces; "
        f"state: {_state.state_root()}"
    )


if __name__ == "__main__":
    main()
