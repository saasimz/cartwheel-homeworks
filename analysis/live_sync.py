"""Incrementally mirror Cartwheel Langfuse traces into Trace Lab state.

Langfuse is canonical for traces.  This module only updates the trace-facing
files (samples, graph, run index, scenario context, and sync status); it never
touches annotations, labels, patterns, or suggestions.  That separation is
why an automatic refresh cannot erase a reviewer's work.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from analysis.helpers import _state
from analysis.helpers.normalization import normalize_trace


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "json") and hasattr(value, "dict"):
        return json.loads(value.json(by_alias=True))
    if hasattr(value, "dict"):
        return value.dict(by_alias=True)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _fingerprint(value: Any) -> str:
    raw = json.dumps(_jsonable(value), sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def _sample(trace: dict[str, Any], reason: str = "synchronized from Langfuse") -> dict[str, Any]:
    """Convert the shared normalized trace shape into a UI sample record."""
    return {
        "trace_id": trace["trace_id"],
        "reason": reason,
        "trace": trace["trace"],
        "text": trace["text"],
        "features": trace["features"],
        "meta": trace["meta"],
        "timestamp": trace.get("timestamp"),
        "models": trace.get("models", []),
        "observations": trace.get("observations", []),
        "input": trace.get("input"),
        "output": trace.get("output"),
        "metadata": trace.get("metadata", {}),
        "permalink": trace.get("permalink"),
        "flags": [],
    }


def merge_samples(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    """Upsert samples by immutable trace id, returning rows/additions/updates."""
    by_id = {
        str(row["trace_id"]): dict(row)
        for row in existing
        if isinstance(row, dict) and row.get("trace_id")
    }
    added = updated = 0
    for trace in incoming:
        row = _sample(trace)
        trace_id = str(row["trace_id"])
        old = by_id.get(trace_id)
        if old is None:
            added += 1
        elif old != row:
            # Preserve reviewer-facing flags when refreshing provider data.
            row["flags"] = old.get("flags", row["flags"])
            updated += 1
        else:
            continue
        by_id[trace_id] = row
    rows = list(by_id.values())
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), str(row["trace_id"])))
    return rows, added, updated


def _legacy_namespace(scenario_id: str | None) -> str:
    if not scenario_id:
        return "manual"
    if scenario_id.startswith("pilot-"):
        return "pilot"
    if scenario_id.startswith("support-"):
        return "support"
    return scenario_id.rsplit("-", 1)[0] or "legacy"


def sample_run_id(sample: dict[str, Any]) -> tuple[str, bool]:
    run_id = (sample.get("meta") or {}).get("run_id")
    if run_id:
        return str(run_id), False
    namespace = _legacy_namespace((sample.get("meta") or {}).get("scenario_id"))
    return f"inferred-{namespace}", True


def build_runs(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build stable run summaries without dropping retries or multi-turn traces."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    inferred: dict[str, bool] = {}
    for sample in samples:
        run_id, is_inferred = sample_run_id(sample)
        grouped.setdefault(run_id, []).append(sample)
        inferred[run_id] = is_inferred

    runs: list[dict[str, Any]] = []
    for run_id, rows in grouped.items():
        timestamps = sorted(str(row.get("timestamp")) for row in rows if row.get("timestamp"))
        scenario_ids = {
            str((row.get("meta") or {}).get("scenario_id"))
            for row in rows
            if (row.get("meta") or {}).get("scenario_id")
        }
        models = sorted(
            {
                str(model)
                for row in rows
                for model in (row.get("models") or [])
                if model
            }
        )
        namespace = _legacy_namespace(next(iter(scenario_ids), None))
        runs.append(
            {
                "run_id": run_id,
                "name": f"{namespace} (inferred)" if inferred[run_id] else run_id,
                "inferred": inferred[run_id],
                "scenario_source": namespace,
                "started_at": timestamps[0] if timestamps else None,
                "ended_at": timestamps[-1] if timestamps else None,
                "scenario_count": len(scenario_ids),
                "trace_count": len(rows),
                "models": models,
                "status": "synchronized",
            }
        )
    runs.sort(key=lambda run: str(run.get("started_at") or ""), reverse=True)
    return runs


def _tool_names(sample: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(message.get("name"))
            for message in sample.get("trace") or []
            if message.get("role") == "tool_call" and message.get("name")
        )
    )


def update_context(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge synchronized trace facts into existing scenario context."""
    path = _state.state_path("scenario_context.json")
    current = _state.read_json(path, default={}) or {}
    scenarios = current.get("scenarios") if isinstance(current, dict) else {}
    if not isinstance(scenarios, dict):
        scenarios = {}
    traces = current.get("traces") if isinstance(current, dict) else {}
    if not isinstance(traces, dict):
        traces = {}
    for sample in samples:
        trace_id = str(sample["trace_id"])
        run_id, inferred = sample_run_id(sample)
        old = traces.get(trace_id) if isinstance(traces.get(trace_id), dict) else {}
        traces[trace_id] = {
            **old,
            "scenario_id": (sample.get("meta") or {}).get("scenario_id"),
            "run_id": run_id,
            "run_inferred": inferred,
            "timestamp": sample.get("timestamp"),
            "models": sample.get("models", []),
            "tools": _tool_names(sample),
            "model_call_count": sum(
                message.get("role") == "model_call" for message in sample.get("trace") or []
            ),
        }
    return {"scenarios": scenarios, "traces": traces}


class TraceSync:
    """Own one background sync loop and reject overlapping manual refreshes."""

    def __init__(self, interval: float = 60.0, client: Any | None = None) -> None:
        self.interval = max(float(interval), 5.0)
        self.client = client
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def status_path(self) -> Path:
        return _state.state_path("sync_status.json")

    def _status(self, **updates: Any) -> dict[str, Any]:
        status = _state.read_json(self.status_path, default={}) or {}
        status.update(updates)
        _state.write_json(self.status_path, status)
        return status

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        from analysis.helpers import langfuse_io

        return langfuse_io._client()

    def _fetch_changed(self) -> list[dict[str, Any]]:
        """Fetch new/changed traces and a small recent overlap for late spans."""
        client = self._client()
        index_path = _state.state_path("sync_index.json")
        index = _state.read_json(index_path, default={}) or {}
        summaries: list[Any] = []
        page = 1
        limit = int(os.environ.get("CARTWHEEL_TRACE_SYNC_LIMIT", "2000"))
        while len(summaries) < limit:
            response = client.api.trace.list(page=page, limit=min(100, limit - len(summaries)))
            batch = list(response.data or [])
            summaries.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        recent_overlap = int(os.environ.get("CARTWHEEL_TRACE_SYNC_RECENT", "25"))
        normalized: list[dict[str, Any]] = []
        new_index = dict(index)
        for position, summary in enumerate(summaries):
            summary_data = _jsonable(summary)
            trace_id = str(summary_data.get("id") if isinstance(summary_data, dict) else getattr(summary, "id"))
            fingerprint = _fingerprint(summary_data)
            if index.get(trace_id) == fingerprint and position >= recent_overlap:
                continue
            full = client.api.trace.get(trace_id)
            try:
                trace = normalize_trace(_jsonable(full))
            except ValueError:
                new_index[trace_id] = fingerprint
                continue
            if trace.get("meta", {}).get("scenario_id"):
                normalized.append(trace)
            new_index[trace_id] = fingerprint
        _state.write_json(index_path, new_index)
        return normalized

    def run_once(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return self._status(in_progress=True, message="sync already in progress")
        started = _utcnow()
        self._status(in_progress=True, last_started_at=started, error=None)
        try:
            incoming = self._fetch_changed()
            samples_path = _state.state_path("samples.json")
            existing = _state.read_json(samples_path, default=[]) or []
            samples, added, updated = merge_samples(existing, incoming)
            # Write each derived artifact only after a successful provider read.
            _state.write_json(samples_path, samples)
            _state.write_json(_state.state_path("runs.json"), build_runs(samples))
            _state.write_json(_state.state_path("scenario_context.json"), update_context(samples))
            from analysis.prepare_review import _projection

            _state.write_json(_state.state_path("graph.json"), _projection(samples))
            return self._status(
                in_progress=False,
                message="Langfuse synchronization completed",
                last_success_at=_utcnow(),
                added=added,
                updated=updated,
                trace_count=len(samples),
                error=None,
            )
        except Exception as exc:
            self._status(in_progress=False, error=str(exc), last_failed_at=_utcnow())
            raise
        finally:
            self._lock.release()

    def request(self) -> bool:
        """Start one asynchronous sync; return False when one is already active."""
        if self._lock.locked():
            return False
        threading.Thread(target=self._run_safely, daemon=True).start()
        return True

    def _run_safely(self) -> None:
        try:
            self.run_once()
        except Exception:
            # The status file carries the actionable error; the server stays up.
            return

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._run_safely()
        while not self._stop.wait(self.interval):
            self._run_safely()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
