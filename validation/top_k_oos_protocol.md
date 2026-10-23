# Prospective Top-k pilot protocol

This protocol defines a future pilot; it is not evidence that sampling is active.
It authorizes no production write, deployment, or weight change. Its Git commit
timestamp is the registration anchor and must precede every eligible capture.

## Fixed procedure

- Use the existing PR182 `top-k-shadow-v2` ranking: exact prediction-time scores,
  descending score with ascending-number tie break, nested Top5/Top6 strictly
  within the immutable production 20. Do not tune this rule during the pilot.
- Keep production recommendation and weight/promotion eligibility unchanged.
  Record the existing prediction-time weight/version provenance; future outcomes
  may not be used to rewrite earlier picks or scores.
- Start at the first independently retained V2 prediction captured after this
  protocol's commit and before its target draw. This prospective start rule must
  not be chosen after seeing its result.
- Evaluate the first 1,000 official target issues beginning at that anchor.
  Keep a roster of all opportunities, including missing/late/invalid snapshots;
  do not replace exclusions with later issues. The first 200 are a data-integrity
  checkpoint only, not a basis for tuning or stopping on favorable performance.
- Before scoring, require pre-draw capture evidence, matching source/target,
  unchanged 20/scores/selection/hash, exact 5/6 lengths, target-issue official
  results, and correct verification issue. Duplicate target issues are rejected.
- If capture coverage is below 95%, mark the pilot incomplete and report the
  missingness pattern. Do not imply excluded outcomes had zero hits.

## Fixed reporting

Report sample/coverage counts, exclusions by reason, full issue interval, mean
Top5 and Top6 hits, and differences from 1.25 and 1.50 under the independent
uniform 20-of-80 null. Also report `k * hits20 / 20` for random selection inside
the same parent 20, and its paired mean difference, to separate parent-pool
quality from ranking quality. Include chronological results and day summaries.

The existing audit supplies descriptive deltas and conservative simultaneous
intervals for the two raw means under independent-draw assumptions. It does
not test the paired ranking delta or serial dependence; those must not be
advertised as statistically proved. Full-hit counts and profitability are not
primary endpoints and must not be inferred from average hits alone.

This fixed-ranking prospective pilot is OOS scoring. It is not a claim that a
train/tune/test walk-forward replay has been completed. Any later tuning needs
a new registered version and a fresh holdout rather than reuse of this pilot.

## Current blocker

Production currently emits no V2 frozen snapshots. A decision to allow separate
shadow capture or metadata writes is required before this protocol can start;
the existing read-only scope must not be silently expanded. Historical rows
cannot be backfilled into prospective eligibility.
