# Cartwheel Homework 4 review app

Run the interface against the live Homework 4 state:

```bash
CARTWHEEL_ANALYSIS_STATE=analysis/state/live \
  uv run python -m analysis.review_app.server --host 127.0.0.1 --port 8021
```

The interface treats one server-issued `cartwheel.session_id` as one
conversation and displays its traces in chronological order. Historical
Module 1 traces were recorded before session IDs existed, so they remain
reviewable through an explicitly marked `run_id + scenario_id` fallback. The
fallback is never presented as an observed user session.

Free-form human notes are saved under the selected analysis state directory.
After the human taxonomy is finalized, the Labels view requires one
present/absent decision for every trace and mode. Those accepted binary
judgments are saved locally and written to Langfuse as scores when Langfuse is
configured. Automatic trace synchronization never overwrites annotations,
taxonomy work, or label history.

