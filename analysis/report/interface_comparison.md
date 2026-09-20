# Cartwheel Trace Lab interface comparison

## Reference design retained

The customized interface retains the reference trace timeline and its visual
grammar: message role is encoded by color, tool calls and tool results share a
container, long structured results collapse, and human annotations appear as
in-place highlights with persistent margin notes. This encoding keeps the
underlying tool evidence more prominent than a model-written summary.

## Adaptation for observed Cartwheel traces

The pilot run showed that one scenario may produce multiple Langfuse traces,
one per conversation turn, and that retries may produce another trace for the
same turn. Cartwheel Trace Lab therefore joins every trace to its
`cartwheel.scenario_id`, shows turn and retry badges, lists observed models and
tools in the header, and provides the exact scenario conversation above the
timeline. The expected outcome and its evidence source are available only in a
collapsed panel so the reviewer can inspect the behavior before seeing the
answer key. A preparation command creates a diverse sample, a reproducible
manifest, scenario context, and a PCA map from the real Langfuse export.

The fourth structured-labeling view was also added. It remains empty until the
human-authored taxonomy has modes; afterward, present/absent decisions are
saved locally as append-only JSONL histories and synchronized to Langfuse
scores when Langfuse is configured.

## Automatic Langfuse synchronization

Langfuse remains the canonical trace store. While Cartwheel Trace Lab is
running, it automatically performs an initial backfill and then polls
Langfuse for every new or updated Cartwheel trace. A reviewer must not need to
rerun `scenarios.export_langfuse` or restart Trace Lab to see new traces.
Trace Lab upserts records by the immutable Langfuse trace identifier, so a
late-arriving span updates the existing trace instead of creating a duplicate.

The local files under `analysis/state/` are a durable review cache, not a
second source of truth. Synchronization preserves existing annotations,
structured labels, taxonomy work, and older runs. A failed or incomplete
Langfuse request must never empty or replace a valid local cache. The browser
shows the last successful sync time, whether synchronization is in progress,
the number of traces added or updated, and any retryable error. It also
provides a manual **Sync now** control. When the `LANGFUSE_*` settings are not
available, Trace Lab remains usable from its last successful local cache.

Synchronization is bidirectional only for review data. Traces flow from
Langfuse into Trace Lab, while accepted structured labels continue to flow
from Trace Lab back to Langfuse as scores. Free-text notes and incomplete
edits remain local until they become accepted structured labels.

## Separate views for scenario runs

Trace Lab retains every synchronized run instead of refreshing the interface
by removing the preceding run. Each scenario-runner execution receives a
stable `cartwheel.run_id`, generated once for the whole batch and attached to
every scenario turn and retry in that batch. This makes one 250-scenario run,
its 460-plus turn and retry traces, a single selectable run rather than an
undifferentiated collection of trace rows.

The browser adds a **Runs** view alongside the existing trace, map, progress,
and labels views. Each row represents one timestamped execution and shows its
run name or identifier, start and end time, scenario source, model, scenario
count, trace count, and completion status. Pilot, final, monitoring, and later
runs remain separately selectable. Opening a run filters all existing views
to that run while preserving scenario-level grouping of multiple turns and
retries. An **All traces** option removes the run filter, and the selected run
is stored in the URL so the view can be bookmarked.

Existing Langfuse traces that predate `cartwheel.run_id` are retained and
placed into clearly marked *inferred runs* using their scenario identifier
namespace and trace timestamps. Trace Lab must label this grouping as inferred
rather than presenting it as metadata originally recorded by the runner.

Scenario expected outcomes continue to come from the matching local scenario
JSONL file, never from the model output. A synchronized trace without matching
scenario context remains visible, but its expected-result panel is marked
unavailable.

## Model-call visibility and the agent handshake

The trace timeline must show the complete observable agent handshake, not only
tool executions. For each conversation turn it renders the ordered sequence:

1. the authenticated user's message;
2. the first model call and its response, including any tool request;
3. each selected tool call and structured tool result;
4. every subsequent model call that interprets a tool result or requests
   another tool; and
5. the model call that produces the final assistant response.

Model calls appear as visually distinct, expandable cards in the existing
timeline and in a dedicated **Model calls** view. Parent and child span
identifiers preserve the relationship between the agent span, model spans,
and tool spans, so the interface does not infer ordering merely from display
text. A turn containing two model calls around one tool therefore displays as
`User -> Model 1 -> Tool -> Model 2 -> Assistant`, while multi-tool turns retain
every intermediate model decision that the provider recorded.

Each model-call card shows the observable request and response content plus
all metadata actually supplied by OpenTelemetry or Langfuse, including when
available: provider, model identifier, operation name, request parameters,
response identifier, start time, duration, status, error, finish reason,
input tokens, output tokens, total tokens, and cost. Standard `gen_ai.*`
fields and application `cartwheel.*` fields remain separately labelled. A
collapsed **Raw metadata** panel preserves additional provider fields without
overloading the main timeline, and secrets or authorization values must never
be displayed.

The **Model calls** view supports the same run, scenario, role, model, status,
and time filters as the other views. It also summarizes model-call count,
latency, token use, cost, and errors for the selected run. Missing provider
fields are shown as unavailable rather than estimated or invented.

This visibility covers model inputs, observable outputs, tool requests, and
published metadata. It does not expose private chain-of-thought or hidden
reasoning that the model provider did not include in the trace. The interface
must state this boundary clearly so an absent reasoning transcript is not
mistaken for a missing trace span.

## Remaining limitation

The browser shows observable model inputs, tool choices, tool results, and
assistant outputs, but it cannot show private model chain-of-thought. It also
groups turns through scenario metadata rather than reconstructing a single
provider-native conversation object. The turn and retry badges make that
boundary explicit, but a future version could add a scenario-level timeline
that renders all turn traces on one page.
