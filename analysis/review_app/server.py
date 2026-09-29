"""Serve the Homework 4 review UI with the shared Trace Lab backend.

The backend remains shared with the reference-derived server because it owns
the safe file persistence and Langfuse synchronization behavior.  This entry
point deliberately swaps only the static application directory so the
submitted interface lives under ``analysis/review_app/`` as required.
"""

from __future__ import annotations

from pathlib import Path

from analysis import server as trace_server


# The handler reads this module-level path at request time. Pointing it at the
# submitted app avoids copying the backend and keeps one persistence contract.
trace_server.UI_DIR = Path(__file__).resolve().parent


def main() -> None:
    """Run the shared backend with the Homework 4 interface."""
    trace_server.main()


if __name__ == "__main__":
    main()

