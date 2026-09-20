"""File-backed review server for the error-analysis skill.

Adapted from the instructor's error-discovery skill. This is the review
interface's backend: a Python standard-library HTTP server (no dependencies)
that serves the single-file HTML app in ``ui/`` and a small JSON API over the
plain files in ``analysis/state/``. The human annotates in the browser, the
app auto-saves every change here, and the coding agent watches
``state/annotations.json`` on a 2-second poll loop (see review-loop.md).

Langfuse is the canonical store for traces and accepted labels. The state
files are an inspectable local mirror and hold workflow state that does not
belong in the trace store. The offline demonstration uses only these files,
and its replay path fabricates a live session without a Langfuse connection.

API (kept compatible with the error-discovery skill so the same UI works):

    GET  /                    the HTML review app
    GET  /api/samples         current sample set (+ manifest of why picked)
    POST /api/samples         push a new or updated sample set
    GET  /api/annotations     current human annotations
    POST /api/annotations     save annotations (the app posts on every change)
    GET  /api/graph           the 2D projection of all traces for the map view
    GET  /api/patterns        the taxonomy as the agent currently holds it
    POST /api/patterns        push the updated taxonomy
    GET  /api/suggestions     agent depth-scan suggestions awaiting accept/reject
    POST /api/suggestions     push suggestions
    GET  /api/runs            timestamped scenario-run summaries
    GET  /api/sync-status     live Langfuse mirror status
    POST /api/sync            request an immediate Langfuse refresh

Run it:

    python analysis/server.py                       # serve on :8020
    python analysis/server.py --port 8021
    python analysis/server.py --replay state/demo_annotations.json

The ``--replay`` flag replays a canned annotations file on a timer, appending
one annotation every few seconds, so the watcher, the grouping, and the
suggestion pipeline all fire on stage even if live annotation fails. The canned
file (``state/demo_annotations.json``) is produced by the course seed and
committed with the repo.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# A separate state root lets a real review session coexist with the committed
# demonstration fixtures.  The helpers use the same environment variable.
STATE_DIR = Path(os.environ.get("CARTWHEEL_ANALYSIS_STATE", HERE / "state"))
UI_DIR = HERE / "ui"

# API path -> the state file that backs it. GET reads the file, POST overwrites
# it. Keeping this a plain table makes the whole contract inspectable and keeps
# the handler tiny.
API_FILES: dict[str, Path] = {
    "/api/samples": STATE_DIR / "samples.json",
    "/api/annotations": STATE_DIR / "annotations.json",
    "/api/graph": STATE_DIR / "graph.json",
    "/api/patterns": STATE_DIR / "patterns.json",
    "/api/suggestions": STATE_DIR / "suggestions.json",
    "/api/context": STATE_DIR / "scenario_context.json",
    "/api/runs": STATE_DIR / "runs.json",
    "/api/sync-status": STATE_DIR / "sync_status.json",
}

# Default empty document per endpoint, so a fresh checkout serves valid JSON
# before the agent has written anything. samples/annotations/suggestions are
# lists; graph and patterns are objects.
API_DEFAULTS: dict[str, Any] = {
    "/api/samples": [],
    "/api/annotations": [],
    "/api/graph": {"nodes": [], "clusters": []},
    "/api/patterns": {},
    "/api/suggestions": [],
    "/api/context": {"scenarios": {}, "traces": {}},
    "/api/runs": [],
    "/api/sync-status": {
        "enabled": False,
        "in_progress": False,
        "message": "Langfuse synchronization is not configured",
    },
}

SYNC_MANAGER: Any | None = None


def _read_json(path: Path, default: Any) -> Any:
    """Return the parsed JSON at ``path``, or ``default`` if missing or bad.

    A half-written file (the app crashed mid-save) reads as the default rather
    than crashing the server; the next good POST repairs it.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _json_response(data: Any, accept_encoding: str = "") -> tuple[bytes, bool]:
    """Serialize an API response and compress large payloads when supported.

    Trace samples can exceed ten megabytes. Gzip keeps the complete evidence
    set while avoiding slow or reset SSH-tunnel transfers in the local UI.
    """
    body = json.dumps(data).encode()
    should_compress = len(body) >= 1024 and "gzip" in accept_encoding.lower()
    return (gzip.compress(body, compresslevel=5), True) if should_compress else (body, False)


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` atomically (write temp, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the UI and the file-backed JSON API."""

    # Quiet by default; the agent narrates the session, not the access log.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    # -- helpers ----------------------------------------------------------

    def _send_json(self, data: Any, status: int = 200) -> None:
        body, compressed = _json_response(data, self.headers.get("Accept-Encoding", ""))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        # Local single-user tool; permissive CORS keeps a file:// or
        # different-port UI from tripping over the browser same-origin check.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json({"error": f"not found: {path.name}"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # -- routes -----------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802  (http.server naming)
        self._send_json({}, status=204)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            self._send_file(UI_DIR / "index.html", "text/html; charset=utf-8")
            return

        # Any other static asset the UI references (kept single-file by
        # default, but this lets an adapted UI ship a companion file).
        if path.startswith("/ui/"):
            asset = UI_DIR / path[len("/ui/"):]
            if asset.is_file() and UI_DIR in asset.resolve().parents:
                self._send_file(asset, _guess_type(asset))
                return

        if path in API_FILES:
            data = _read_json(API_FILES[path], API_DEFAULTS[path])
            self._send_json(data)
            return

        self._send_json({"error": f"unknown path: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/sync":
            if SYNC_MANAGER is None:
                self._send_json(
                    {"error": "Langfuse synchronization is not configured"}, status=503
                )
                return
            started = SYNC_MANAGER.request()
            self._send_json(
                {"ok": True, "started": started}, status=202 if started else 200
            )
            return
        if path not in API_FILES:
            self._send_json({"error": f"cannot POST to {path}"}, status=404)
            return
        data = self._read_body()
        if data is None:
            self._send_json({"error": "expected a JSON body"}, status=400)
            return
        synced = 0
        new_labels: list[dict[str, Any]] = []
        if path == "/api/annotations":
            new_labels = _new_structured_labels(data)
            try:
                synced = _sync_annotation_scores(new_labels)
            except Exception as exc:  # pragma: no cover - network-only path
                # Preserve a resumable local copy, but tell the client that
                # the canonical write did not complete.
                _write_json(API_FILES[path], data)
                _persist_label_history(new_labels)
                self._send_json(
                    {
                        "error": f"Langfuse score write failed: {exc}",
                        "cached_locally": True,
                    },
                    status=502,
                )
                return
        _write_json(API_FILES[path], data)
        if new_labels:
            _persist_label_history(new_labels)
        result = {"ok": True, "count": _count(data)}
        if synced:
            result["langfuse_scores_written"] = synced
        self._send_json(result)


def _count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return len(data)
    return 0


def _annotation_list(data: Any) -> list[dict[str, Any]]:
    """Normalize the annotations payload (a bare list or ``{"annotations": []}``)."""
    if isinstance(data, dict):
        data = data.get("annotations", [])
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def _new_structured_labels(data: Any) -> list[dict[str, Any]]:
    """Return structured decisions not already present in the local mirror.

    The browser posts the complete annotation array on every save. Comparing
    stable annotation ids prevents an unrelated free-text note from sending
    every historical score to Langfuse again.
    """
    current = _annotation_list(
        _read_json(API_FILES["/api/annotations"], API_DEFAULTS["/api/annotations"])
    )
    existing_ids = {str(item.get("id")) for item in current if item.get("id")}
    out = []
    for annotation in _annotation_list(data):
        if str(annotation.get("id")) in existing_ids:
            continue
        if annotation.get("mode") and annotation.get("label") in (0, 1, "0", "1"):
            out.append(annotation)
    return out


def _persist_label_history(labels: list[dict[str, Any]]) -> None:
    """Append structured decisions to one inspectable JSONL file per mode.

    When a reviewer changes a decision, the previous live row is retained and
    marked with ``superseded_by``. This preserves the human edit history while
    making the newest decision unambiguous to downstream helpers.
    """
    labels_dir = STATE_DIR / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    for annotation in labels:
        mode = str(annotation["mode"])
        safe_mode = re.sub(r"[^a-zA-Z0-9_-]+", "_", mode).strip("_") or "mode"
        path = labels_dir / f"{safe_mode}.jsonl"
        rows: list[dict[str, Any]] = []
        if path.exists():
            for raw in path.read_text().splitlines():
                if raw.strip():
                    rows.append(json.loads(raw))
        record_id = str(annotation.get("id") or f"label-{time.time_ns()}")
        trace_id = str(annotation["trace_id"])
        for row in reversed(rows):
            if row.get("trace_id") == trace_id and not row.get("superseded_by"):
                row["superseded_by"] = record_id
                break
        rows.append(
            {
                "id": record_id,
                "trace_id": trace_id,
                "label": int(annotation["label"]),
                "source": annotation.get("source", "human_structured_label"),
                "note": annotation.get("note"),
                "ts": annotation.get("ts"),
            }
        )
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(row) + "\n" for row in rows))
        tmp.replace(path)


def _sync_annotation_scores(data: Any) -> int:
    """Write labeled annotations to Langfuse scores when configured.

    Langfuse is canonical when configured, while ``annotations.json`` is the
    local mirror. The function does nothing when the ``LANGFUSE_*``
    environment is absent, so the
    offline demo, the ``--replay`` path, and the test suite never make a
    network call here. An annotation is written as a score only when it
    carries both a ``mode`` and a 0/1 ``label`` (a per-trace binary verdict);
    free-text-only notes are stored on disk but have nothing to score against.

    Return the number of scores written, or zero in offline mode. Propagate a
    Langfuse error so the server can report that the canonical write failed.
    """
    try:
        from analysis.helpers import langfuse_io
    except Exception:
        return 0
    if not langfuse_io.is_configured():
        return 0

    written = 0
    client = langfuse_io._client()
    for ann in _annotation_list(data):
        trace_id = ann.get("trace_id")
        mode = ann.get("mode")
        label = ann.get("label")
        if not trace_id or not mode or label not in (0, 1, "0", "1"):
            continue
        langfuse_io.write_label_score(
            trace_id=str(trace_id),
            mode=str(mode),
            label=int(label),
            comment=ann.get("note"),
            client=client,
        )
        written += 1
    return written


def _guess_type(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "text/javascript",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }.get(path.suffix, "application/octet-stream")


# ---------------------------------------------------------------------------
# Demo replay: append canned annotations on a timer.
# ---------------------------------------------------------------------------


def _load_canned(replay_path: Path) -> list[Any]:
    """Read the canned annotations, accepting either on-disk shape.

    The committed demo file wraps its list as ``{"annotations": [...]}`` (it
    carries a little metadata alongside), while an ad-hoc file may be a bare
    list. Accept both so the demo fallback does not care which the course seed
    produced. Each annotation is given a stable ``id`` if it lacks one, so the
    UI's id-based merge and de-duplication work on replayed items.
    """
    raw = _read_json(replay_path, None)
    if isinstance(raw, dict):
        raw = raw.get("annotations", [])
    if not isinstance(raw, list):
        return []
    out: list[Any] = []
    for i, ann in enumerate(raw):
        if isinstance(ann, dict) and "id" not in ann:
            ann = {**ann, "id": f"replay-{i}"}
        out.append(ann)
    return out


def _replay_annotations(replay_path: Path, interval: float) -> None:
    """Append the canned annotations to state/annotations.json on a timer.

    Reads the whole canned list up front, then adds one annotation every
    ``interval`` seconds. The watcher and the UI both poll annotations.json, so
    grouping and the suggestion pipeline fire exactly as they would in a live
    session. Idempotent enough for a demo: it starts from an empty live file so
    a re-run replays from the top.
    """
    canned = _load_canned(replay_path)
    if not canned:
        print(f"[replay] nothing to replay from {replay_path}")
        return
    live_path = API_FILES["/api/annotations"]
    _write_json(live_path, [])
    print(f"[replay] replaying {len(canned)} annotations, one per {interval:g}s")
    accumulated: list[Any] = []
    for i, ann in enumerate(canned, start=1):
        time.sleep(interval)
        accumulated.append(ann)
        _write_json(live_path, accumulated)
        note = ann.get("note", "") if isinstance(ann, dict) else ""
        print(f"[replay] {i}/{len(canned)}: {note[:70]}")
    print("[replay] done")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--replay",
        metavar="PATH",
        help="canned annotations file to replay on a timer (e.g. "
        "state/demo_annotations.json), for the demo fallback",
    )
    parser.add_argument(
        "--replay-interval",
        type=float,
        default=4.0,
        help="seconds between replayed annotations (default 4)",
    )
    parser.add_argument(
        "--sync-interval",
        type=float,
        default=float(os.environ.get("CARTWHEEL_TRACE_SYNC_INTERVAL", "60")),
        help="seconds between Langfuse refreshes (default 60)",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="serve only the existing cache even when Langfuse is configured",
    )
    args = parser.parse_args()

    # The review server is launched independently from the Cartwheel API, so
    # load the same repository .env before attempting Langfuse score writes.
    # Missing Langfuse values still leave the local/offline workflow usable.
    try:
        from observability.instrument import load_env

        load_env()
    except Exception:
        pass

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    global SYNC_MANAGER
    if not args.no_sync:
        try:
            from analysis.helpers import langfuse_io

            if langfuse_io.is_configured():
                from analysis.live_sync import TraceSync

                SYNC_MANAGER = TraceSync(interval=args.sync_interval)
                _write_json(
                    API_FILES["/api/sync-status"],
                    {
                        "enabled": True,
                        "in_progress": False,
                        "message": "waiting for initial Langfuse synchronization",
                    },
                )
                SYNC_MANAGER.start()
        except Exception as exc:
            _write_json(
                API_FILES["/api/sync-status"],
                {"enabled": False, "in_progress": False, "error": str(exc)},
            )

    if args.replay:
        replay_path = Path(args.replay)
        if not replay_path.is_absolute():
            replay_path = HERE / replay_path
        thread = threading.Thread(
            target=_replay_annotations,
            args=(replay_path, args.replay_interval),
            daemon=True,
        )
        thread.start()

    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"review interface on {url}")
    print(f"serving state from {STATE_DIR}")
    print("open the URL, read a trace, select the failing text, type a note.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()
    finally:
        if SYNC_MANAGER is not None:
            SYNC_MANAGER.stop()


if __name__ == "__main__":
    main()
