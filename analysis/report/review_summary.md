# Homework 4 review summary

## Reviewed sample

The final reviewed sample contains **133 traces**. The review process combined the original
sample, targeted depth searches for candidate modes and close negatives, and a final uniformly
sampled batch of 15 traces. Because targeted retrieval changed the composition of the reviewed
set, the quantities below are **sample fractions**, not prevalence estimates.

### Composition by role

| Role | Traces | Sample fraction |
|---|---:|---:|
| Shopper | 69 | 51.88% |
| Merchant | 34 | 25.56% |
| Support | 30 | 22.56% |

The sample contains 79 coverage traces (59.40%) and 54 challenge traces (40.60%). It contains
130 traces from inferred pilot/support runs and 3 traces from the Homework 4 session-verification
run.

### Composition by intent

| Intent | Traces |
|---|---:|
| Search product | 37 |
| Refund order | 22 |
| Order status | 15 |
| Cancel order | 12 |
| Resolve dispute | 10 |
| Policy question | 10 |
| Return eligibility | 7 |
| Dispute clarification | 6 |
| Find order | 6 |
| Return deadline | 5 |
| List orders | 2 |
| Out of scope | 1 |

## Final taxonomy and sample fractions

Every reviewed trace received a present or absent judgment for every final mode: **798 of 798**
trace-mode decisions are recorded.

| Failure mode | Requirement source | Present | Absent | Sample fraction | Likely evaluator |
|---|---|---:|---:|---:|---|
| `non_customer_facing_policy_or_tool_details` | RESP-5, RESP-7 | 47 | 86 | 35.34% | LLM judge |
| `missing_decision_rationale` | RESP-5 | 28 | 105 | 21.05% | LLM judge |
| `dispute_workflow_mishandled` | SCOPE-1, TOOL-2, TOOL-7, ESC-3, DISPUTE-1 | 10 | 123 | 7.52% | Code check |
| `redundant_tool_calls` | TOOL-R1 | 28 | 105 | 21.05% | LLM judge |
| `store_policy_override_ignored` | POLICY-1 | 7 | 126 | 5.26% | LLM judge |
| `response_scope_mismatch` | RESP-9 | 7 | 126 | 5.26% | LLM judge |

Each mode has at least three confirmed positive traces and three human-confirmed close negatives.
Definitions, boundaries, example trace identifiers, close negatives, evaluator types, and
requirement mappings are stored in `analysis/state/patterns.json`.

## Final stability check

The final stability batch used 15 uniformly sampled traces with seed `4042026`. It produced
**one previously unseen consequential mode**, `response_scope_mismatch`. Because the final batch
produced one new mode rather than several, the handout did not require another uniformly sampled
batch. The new mode was added to the taxonomy, checked against positives and close negatives, and
reapplied to all 133 reviewed traces.

A follow-up stability audit found 85 missing decisions for the original five modes on 17 newer
traces. Those decisions were proposed by the coding agent, reviewed by the human, and accepted or
corrected before this summary was finalized. The audit also surfaced 18 earlier absent decisions
for `response_scope_mismatch` that merited reinspection after the late specification revision.
The human reviewed all 18 and confirmed that the mode was absent. No additional unseen failure
mode remained after these checks.

The resulting six-mode taxonomy is reasonably stable for this reviewed sample. This conclusion
does not claim population prevalence; Homework 5 will evaluate modes over the complete trace store.

## Taxonomy revision

The first stable draft contained five modes. During the final-batch review, the human identified
responses that either omitted the expected order-information fields or added unrelated records or
unrequested derived information. That behavior did not fit the existing modes: it was not merely
an internal-policy reference, missing decision rationale, a wrong policy override, or a redundant
tool call.

The behavior was first recorded as a specification revision, **RESP-9**, and then added as
`response_scope_mismatch`. Seven positive examples were confirmed. The first three proposed close
negatives were rejected because the human found the failure present in all three; they were
reclassified as positives. Replacement close negatives from `support-0082`, `support-0083`, and
`support-0095` were inspected and confirmed absent. This revision demonstrates why retrieval
suggestions were treated as proposals rather than labels.

The motivating final-batch annotations include:

- `support-0174`, trace `96d4d0ae167d1780a0b64e9125ecdffb`: unrequested refund eligibility.
- `support-0175`, trace `08f05a7a25d7eca90e8e73881b0a3244`: incomplete order summary.
- `support-0197`, trace `ea25f374d13a6e36b1a249c126e6357e`: irrelevant information.
- `support-0202`, trace `a405b9fc7abc76b0c23fde693fbb41a6`: an unrelated product anomaly.

## Rejected search suggestion

For `redundant_tool_calls`, trace `2535f6b74caf323b0814889e4cc46e94` (`support-0245`) was
retrieved as a possible positive and rejected. It is a close negative because the lookup did not
repeat an already sufficient call. This preserves the mode boundary: a relevant lookup is not a
failure merely because a tool was used; the call must be unnecessary or duplicative.

## AgentDebug comparison

AgentDebug was used as a source of hypotheses, not labels. Its inefficient-planning concept
supported the interpretation of `redundant_tool_calls`, while generic constraint-ignorance ideas
helped clarify the dispute and store-policy modes. The Cartwheel-specific names were retained
because they state observable product behavior more precisely. No AgentDebug category was added
without supporting Cartwheel annotations.

## Langfuse synchronization

All **798** final human-approved trace-mode decisions are mirrored in Langfuse as numeric scores,
where `1` means the failure is present and `0` means it is absent. The final reconciliation found
573 matching scores already present and wrote 225 missing scores. A subsequent read-back verified
798 matching latest scores, with zero missing and zero conflicting values. Matching JSONL records
are retained under `analysis/state/labels/`, and the reconciliation receipt is stored in
`analysis/state/langfuse_label_sync.json`.
