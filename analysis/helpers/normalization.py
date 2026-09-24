"""Normalize Module 1 and Langfuse traces for Module 2 review.

Every Module 2 component consumes one record shape. The original Langfuse
identifier remains the primary identifier, so a human judgment can be written
back to the trace as a score.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any


_SENSITIVE_KEY_PARTS = ("authorization", "api_key", "apikey", "password", "secret", "token", "cookie")


def _redact(value: Any) -> Any:
    """Remove credential-shaped metadata before it reaches the review UI."""
    value = _data(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
            else _redact(item)
            for key, item in value.items()
        }
    return value


def _data(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "dict"):
        return value.dict(by_alias=True)
    return value


def _text(value: Any) -> str:
    value = _data(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                for p in item.get("parts") or []:
                    if isinstance(p, dict) and p.get("content"):
                        parts.append(str(p["content"]))
        if parts:
            return "\n".join(parts)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _message_text(value: Any) -> str:
    """Extract readable prose from provider message/part containers.

    Langfuse preserves OpenAI messages as nested ``role -> parts -> content``
    objects. Reviewers need the sentence, while the raw objects remain intact
    in the normalized input/output fields for audit and debugging.
    """
    value = _data(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_message_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("content", "text"):
            if key in value:
                extracted = _message_text(value[key])
                if extracted:
                    return extracted
        if "parts" in value:
            extracted = _message_text(value["parts"])
            if extracted:
                return extracted
    return _text(value)


def _reasoning_summaries(value: Any) -> list[str]:
    """Extract only provider-published reasoning summaries.

    Raw reasoning tokens and encrypted reasoning are intentionally ignored.
    A review UI may display a provider's explicit summary, but it must never
    relabel opaque/private chain-of-thought as observable evidence.
    """
    value = _data(value)
    if isinstance(value, list):
        summaries: list[str] = []
        for item in value:
            summaries.extend(_reasoning_summaries(item))
        return summaries
    if not isinstance(value, dict):
        return []
    if str(value.get("type") or "").lower() == "reasoning":
        summary = _data(value.get("summary"))
        parts = summary if isinstance(summary, list) else [summary]
        return [
            text
            for part in parts
            if isinstance(part, (dict, str))
            and (text := _message_text(part)).strip()
        ]
    summaries = []
    # Provider wrappers commonly nest response items under these keys. Keep
    # the traversal narrow so ordinary fields named "reason" are not mistaken
    # for a model-generated reasoning summary.
    for key in ("output", "items", "response"):
        if key in value:
            summaries.extend(_reasoning_summaries(value[key]))
    return summaries


def _timestamp(value: Any) -> str | None:
    """Return an ISO timestamp without dropping an existing string value."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _observation_record(value: Any) -> dict[str, Any] | None:
    """Keep the observation fields needed by monitoring and cost review."""
    observation = _data(value)
    if not isinstance(observation, dict):
        return None
    metadata = _data(observation.get("metadata"))
    attributes = _metadata({"metadata": metadata})
    model = (
        observation.get("model")
        or observation.get("model_id")
        or attributes.get("gen_ai.request.model")
        or attributes.get("gen_ai.response.model")
    )
    provider = (
        attributes.get("gen_ai.provider.name")
        or attributes.get("gen_ai.system")
        or observation.get("provider")
    )
    return {
        "id": observation.get("id"),
        "trace_id": observation.get("trace_id") or observation.get("traceId"),
        "parent_observation_id": observation.get("parent_observation_id")
        or observation.get("parentObservationId"),
        "type": observation.get("type"),
        "name": observation.get("name"),
        "start_time": _timestamp(
            observation.get("start_time") or observation.get("startTime")
        ),
        "end_time": _timestamp(
            observation.get("end_time") or observation.get("endTime")
        ),
        "model": model,
        "provider": provider,
        "operation": attributes.get("gen_ai.operation.name"),
        "model_parameters": _redact(
            observation.get("model_parameters") or observation.get("modelParameters")
        ),
        "input": _data(observation.get("input")),
        "output": _data(observation.get("output")),
        "usage_details": _data(
            observation.get("usage_details")
            or observation.get("usageDetails")
            or observation.get("usage")
        ),
        "cost_details": _data(
            observation.get("cost_details") or observation.get("costDetails")
        ),
        "total_cost": observation.get("total_cost")
        if observation.get("total_cost") is not None
        else observation.get("totalCost"),
        "latency_seconds": observation.get("latency"),
        "time_to_first_token_seconds": observation.get("time_to_first_token")
        if observation.get("time_to_first_token") is not None
        else observation.get("timeToFirstToken"),
        "status": observation.get("status") or observation.get("level"),
        "status_message": observation.get("status_message")
        or observation.get("statusMessage"),
        "completion_start_time": _timestamp(
            observation.get("completion_start_time")
            or observation.get("completionStartTime")
        ),
        "metadata": _redact(metadata),
    }


def _is_model_observation(observation: dict[str, Any]) -> bool:
    """Identify model spans from Langfuse type, name, or OTel attributes."""
    kind = str(observation.get("type") or "").lower()
    name = str(observation.get("name") or "").lower()
    attributes = _metadata({"metadata": observation.get("metadata")})
    operation = str(attributes.get("gen_ai.operation.name") or "").lower()
    if kind in {"generation", "model"}:
        return True
    if "openai.response" in name or "model" in name:
        return True
    return bool(attributes.get("gen_ai.request.model")) and operation != "execute_tool"


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Return trace metadata with nested OpenTelemetry attributes merged in.

    Langfuse stores span attributes under ``metadata.attributes`` (a JSON
    string in ClickHouse, a dict from the API). Top level keys win when both
    are present.
    """
    metadata = _data(record.get("metadata"))
    if not isinstance(metadata, dict):
        return {}
    attributes = metadata.get("attributes")
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except ValueError:
            attributes = None
    merged = dict(attributes) if isinstance(attributes, dict) else {}
    merged.update(metadata)
    return merged


def _observation_message(observation: Any) -> list[dict[str, Any]]:
    obs = _data(observation)
    if not isinstance(obs, dict):
        return []
    name = str(obs.get("name") or obs.get("type") or "step")
    inp = obs.get("input")
    out = obs.get("output")
    lowered = name.lower()
    messages: list[dict[str, Any]] = []
    # Container spans repeat the trace-level input/output and would make the
    # reviewer see the same final answer twice. Their timing and metadata stay
    # available in ``observations``; the readable timeline shows only content
    # produced by a model, retrieval step, or tool.
    if name == "cartwheel.session_message" or obs.get("type") in {"AGENT"}:
        return []
    if lowered == "agent workflow":
        return []
    if "tool" in lowered or obs.get("type") in {"TOOL", "tool"}:
        if inp is not None:
            messages.append(
                {"role": "tool_call", "name": name, "arguments": _data(inp)}
            )
        if out is not None:
            messages.append(
                {"role": "tool_result", "name": name, "content": _data(out)}
            )
    elif _is_model_observation(obs):
        record = _observation_record(obs) or {}
        messages.append(
            {
                "role": "model_call",
                "label": name,
                "name": name,
                "input": _data(inp),
                "output": _data(out),
                "model_call": record,
            }
        )
        for summary in _reasoning_summaries(out):
            messages.append(
                {
                    "role": "reasoning_summary",
                    "label": "published reasoning summary",
                    "text": summary,
                    "observation_id": record.get("id"),
                }
            )
    elif out is not None:
        messages.append({"role": "observation", "label": name, "text": _text(out)})
    return messages


def _messages(record: dict[str, Any]) -> list[dict[str, Any]]:
    existing = record.get("trace")
    if isinstance(existing, list):
        return [dict(_data(item)) for item in existing if isinstance(_data(item), dict)]

    turns = record.get("turns")
    if isinstance(turns, list):
        messages: list[dict[str, Any]] = []
        for turn in turns:
            item = _data(turn)
            if not isinstance(item, dict):
                continue
            if item.get("user") is not None:
                messages.append({"role": "user", "text": _message_text(item["user"])})
            if item.get("agent") is not None:
                messages.append(
                    {"role": "assistant", "text": _message_text(item["agent"])}
                )
        if messages:
            return messages

    messages = []
    if record.get("input") is not None:
        messages.append(
            {"role": "user", "text": _message_text(record.get("input"))}
        )
    # Langfuse's API returns a span tree, not guaranteed chronological order.
    # Sorting is essential for causal review: a lookup must appear before the
    # cancellation or refund decision that consumed its result.
    observations = sorted(
        record.get("observations") or [],
        key=lambda item: str(
            _data(item).get("start_time")
            or _data(item).get("startTime")
            or ""
        )
        if isinstance(_data(item), dict)
        else "",
    )
    for observation in observations:
        messages.extend(_observation_message(observation))
        obs = _data(observation)
        if not isinstance(obs, dict) or obs.get("type") != "GENERATION":
            continue
        # Keep the model-call record used by TraceLab, while also exposing
        # provider text as a readable assistant step for Homework 4 review.
        out = obs.get("output")
        if isinstance(out, list):
            existing = {
                message.get("text")
                for message in messages
                if message.get("role") == "assistant"
            }
            for item in out:
                if not isinstance(item, dict):
                    continue
                for part in item.get("parts") or []:
                    if not isinstance(part, dict) or part.get("type") != "text":
                        continue
                    text = str(part.get("content") or "").strip()
                    if text and text not in existing:
                        messages.append({"role": "assistant", "text": text})
                        existing.add(text)
    if record.get("output") is not None:
        text = _message_text(record.get("output"))
        existing = {m.get("text") for m in messages if m.get("role") == "assistant"}
        if text and text not in existing:
            messages.append({"role": "assistant", "text": text})

    segments = record.get("segments")
    if not messages and isinstance(segments, dict):
        for name, value in segments.items():
            messages.append({"role": "observation", "label": str(name), "text": _text(value)})
    if not messages and record.get("text") is not None:
        messages.append({"role": "observation", "label": "trace", "text": _text(record["text"])})
    return messages


def _flatten(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "step")
        if role == "tool_call":
            content = _text(message.get("arguments"))
        else:
            content = _text(message.get("text", message.get("content")))
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def normalize_trace(value: Any) -> dict[str, Any]:
    """Return one trace in the shared Module 2 and Module 3 representation.

    The compact message and feature fields support error analysis. The time,
    model, input, output, and observation fields let a monitoring job select a
    time window and prepare the exact evidence needed by a saved judge.
    """
    raw = _data(value)
    if not isinstance(raw, dict):
        raise ValueError("a trace record must be an object")
    metadata = _metadata(raw)
    trace_id = raw.get("id") or raw.get("trace_id")
    if not trace_id:
        raise ValueError("a trace record has no stable id or trace_id")
    messages = _messages(raw)
    if not messages and not isinstance(raw.get("features"), dict):
        raise ValueError(f"trace {trace_id} contains no renderable messages or segments")

    tool_calls = [m for m in messages if m.get("role") == "tool_call"]
    tools = {str(m.get("name")) for m in tool_calls if m.get("name")}
    retrieval = any(
        "retriev" in str(m.get("label", m.get("name", ""))).lower()
        or "policy" in str(m.get("label", m.get("name", ""))).lower()
        for m in messages
    )
    text = _flatten(messages)
    usage = _data(raw.get("usage"))
    token_total = 0
    if isinstance(usage, dict):
        token_total = int(usage.get("total_tokens") or usage.get("total") or 0)
    supplied_features = raw.get("features")
    features = dict(supplied_features) if isinstance(supplied_features, dict) else {}
    features.update(
        {
            "turn_count": sum(m.get("role") in {"user", "assistant"} for m in messages),
            "tool_call_count": len(tool_calls),
            "distinct_tools": len(tools),
            "has_retrieval": int(retrieval),
            "tokens": int(features.get("tokens") or token_total or len(text.split())),
        }
    )
    meta = {
        "role": metadata.get("cartwheel.user_role") or metadata.get("role"),
        "store": metadata.get("cartwheel.store_id") or metadata.get("store_id"),
        "prompt_version": metadata.get("cartwheel.prompt_version")
        or metadata.get("prompt_version"),
        "scenario_id": raw.get("cartwheel_scenario_id")
        or metadata.get("cartwheel.scenario_id")
        or metadata.get("scenario_id"),
        "run_id": raw.get("cartwheel_run_id")
        or metadata.get("cartwheel.run_id")
        or metadata.get("run_id"),
    }
    supplied_segments = raw.get("segments")
    segments = dict(supplied_segments) if isinstance(supplied_segments, dict) else {}
    segments.update({key: val for key, val in meta.items() if val is not None})
    observations = [
        record
        for observation in raw.get("observations") or []
        if (record := _observation_record(observation)) is not None
    ]
    models = list(
        dict.fromkeys(
            str(observation["model"])
            for observation in observations
            if observation.get("model")
        )
    )
    return {
        "id": str(trace_id),
        "trace_id": str(trace_id),
        "timestamp": _timestamp(raw.get("timestamp")),
        "models": models,
        "input": _data(raw.get("input")),
        "output": _data(raw.get("output")),
        "observations": observations,
        "trace": messages,
        "text": text,
        "features": features,
        "meta": {key: val for key, val in meta.items() if val is not None},
        "segments": segments,
        "metadata": _redact(metadata),
        "permalink": raw.get("permalink") or raw.get("url"),
    }


def _merge_multi_turn(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge traces that share a scenario_id into one conversation."""
    from collections import defaultdict

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    no_scenario: list[dict[str, Any]] = []
    for trace in traces:
        sid = trace["meta"].get("scenario_id")
        if sid:
            by_scenario[sid].append(trace)
        else:
            no_scenario.append(trace)

    merged: list[dict[str, Any]] = list(no_scenario)
    for sid, group in by_scenario.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        group.sort(key=lambda t: t.get("timestamp") or "")
        first = dict(group[0])
        for later in group[1:]:
            first["trace"].extend(later["trace"])
            first["observations"].extend(later["observations"])
            first["models"] = list(
                dict.fromkeys(first["models"] + later["models"])
            )
        first["text"] = _flatten(first["trace"])
        first["features"]["turn_count"] = sum(
            m.get("role") in {"user", "assistant"} for m in first["trace"]
        )
        first["features"]["tool_call_count"] = sum(
            m.get("role") == "tool_call" for m in first["trace"]
        )
        merged.append(first)
    return merged


def normalize_traces(values: list[Any]) -> list[dict[str, Any]]:
    """Normalize, merge multi-turn conversations, reject duplicate ids."""
    normalized = [normalize_trace(value) for value in values]
    merged = _merge_multi_turn(normalized)
    ids = [record["id"] for record in merged]
    if len(ids) != len(set(ids)):
        raise ValueError("the trace source contains duplicate identifiers")
    return merged
