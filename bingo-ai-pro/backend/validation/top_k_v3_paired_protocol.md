# Registered paired shadow protocol: patch-support-v3-20261023

The timestamp of the first remote commit containing this protocol is the
registration lock. It must precede every eligible V3 frozen capture. No past
V2 row may be backfilled with a V3 selection and counted prospectively.

## Fixed V3 definition

Use the existing 100-row prediction-time V7 history, ending exactly at the
source issue. Reject missing, duplicate or future history. Deep-copy it and
replace only each row's patch candidates with circular ±1/2/9/10/11 support
computed from all 20 actual source numbers before taking 12. Equal support
uses SHA256 of `patch-support-research-v1:issue:number`.

Run the same five V7 model functions on that copied history. Each selected
candidate receives `max(1, confidence/20) * existing_model_multiplier`.
Multipliers are the existing frozen prediction-time values clamped to
0.5–1.5. There is no candidate-position bonus and no mature-rule contribution
in this V3 research ranking. Ties use SHA256 of
`patch-support-v3-20261023:source:target:number`.

Choose nested Top5/Top6 strictly inside the unchanged production 20. Freeze
the exact scores, corrected model candidates/confidences/multipliers, history
issue range/hash, timestamp, version and canonical snapshot hash. Persist V2
and V3 in the same existing prediction metadata insert. Do not change V2,
production candidate generation, weight updates or promotion eligibility.
The existing core-only one-shot Cron remains core-only; no delayed worker is
enabled. V3 uses bounded synchronous CPU and the already-loaded history.

This definition bundles patch correction, position-bonus removal, tie handling
and mature-rule exclusion. A comparison cannot attribute a hit difference to
one change alone. Old laowanjia/pattern functions and parent-20 bias remain
possible limitations. V3 does not force any small/large quota.

## Fixed prospective cohort

Anchor at the first independently retained V3 capture after registration and
before its target draw. Evaluate the first 1,000 official target opportunities
starting there; keep every opportunity and all missing/late/invalid/duplicate
exclusions, without replacing them with later issues. The first 200 are an
integrity checkpoint, not a favorable-result stop or tuning opportunity.
Coverage below 95% means the cohort is incomplete. Do not score missing rows
as zero hits. Do not tune or promote based on this cohort and then reuse it
as the tuned model's holdout.

For eligible paired records require same row, source, target and parent 20;
both selections independently retained before the draw; unchanged snapshots,
scores and model provenance after verification; correct target official draw;
validated result and verification timestamps after the draw. Hash validation
alone is not independent pre-draw retention.

Report V2 and V3 Top6 means on exactly the same targets, their paired delta,
random 80-number expectation 1.50, and same-parent-20 expectation
`6 * parent20_hits / 20`. Top5, distribution of low/high numbers and full-hit
counts are secondary diagnostics. Preserve chronological/day summaries and
the complete opportunity roster. Do not infer profitability from mean hits.

The public page reports only a recent 200-snapshot descriptive window with
verified pairs. It is not the complete registered cohort or a certified OOS
report. Train/tune/test walk-forward replay remains a separate uncompleted
validation task.

## Failure and rollback

V3 failures must skip V3 capture without changing production output or V2.
Default V3 capture is off outside the authorized Cron child. In that child,
`TOP_K_SHADOW_V3_CAPTURE_ENABLED=0` overrides its default-on capture and stops
future V3 snapshots. Existing snapshots must remain untouched. No schema,
backfill or production weight activation is part of this rollout.
