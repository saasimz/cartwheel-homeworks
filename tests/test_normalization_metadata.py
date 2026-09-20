"""The Module 2 normalizer must read attributes nested by Langfuse."""

from __future__ import annotations

import json

from analysis.helpers.normalization import _metadata, normalize_trace


def test_metadata_merges_nested_langfuse_attributes() -> None:
    record = {
        "metadata": {
            "scope": {},
            "attributes": json.dumps(
                {"cartwheel.scenario_id": "support-9", "cartwheel.user_role": "shopper"}
            ),
        }
    }
    merged = _metadata(record)
    assert merged["cartwheel.scenario_id"] == "support-9"
    assert merged["cartwheel.user_role"] == "shopper"


def test_metadata_keeps_flat_keys_authoritative() -> None:
    record = {
        "metadata": {
            "cartwheel.scenario_id": "flat",
            "attributes": {"cartwheel.scenario_id": "nested"},
        }
    }
    assert _metadata(record)["cartwheel.scenario_id"] == "flat"


def test_model_spans_and_run_id_are_preserved_for_trace_lab() -> None:
    trace = normalize_trace(
        {
            "id": "trace-1",
            "input": "Where is my order?",
            "output": "It shipped.",
            "metadata": {
                "attributes": {
                    "cartwheel.scenario_id": "support-0001",
                    "cartwheel.run_id": "run-one",
                }
            },
            "observations": [
                {
                    "id": "generation-1",
                    "parentObservationId": "agent-1",
                    "type": "GENERATION",
                    "name": "openai.response",
                    "input": {"messages": ["Where is my order?"]},
                    "output": {"tool_call": "get_order"},
                    "metadata": {
                        "attributes": {
                            "gen_ai.request.model": "gpt-5.5",
                            "gen_ai.provider.name": "openrouter",
                            "authorization": "must-not-leak",
                        }
                    },
                }
            ],
        }
    )
    assert trace["meta"]["run_id"] == "run-one"
    model_message = next(row for row in trace["trace"] if row["role"] == "model_call")
    assert model_message["model_call"]["model"] == "gpt-5.5"
    assert model_message["model_call"]["parent_observation_id"] == "agent-1"
    attrs = model_message["model_call"]["metadata"]["attributes"]
    assert attrs["authorization"] == "[redacted]"
