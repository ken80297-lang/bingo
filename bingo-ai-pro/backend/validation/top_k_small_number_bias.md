# Small-number bias diagnostic — 2026-10-23

Status: research only. No runtime files, production scores, saved selections,
weights, worker settings or database rows are changed by this branch.

## Confirmed mechanism

`build_analysis_record()` sorts the actual 20 numbers ascending before calling
`database.analysis_store._patch_numbers()`. That function emits neighbors from
each source in sequence and returns only the first 12 unique candidates. The
limit is applied before later source numbers can contribute. It is therefore
an input-order-dependent selection, rather than an all-source support ranking.

Those candidates enter both `model_a_laowanjia()` and `model_d_pattern()`.
The latter counts their recurrence across 20 analysis rows. Recommendation
scoring adds model confidence/support plus a decreasing candidate-position
bonus, amplifying already skewed candidate sets. V2 shadow selects the six
highest resulting scores within the saved production 20.

The display-only numeric sort is downstream of this process and does not
change which six numbers were selected.

## Observed data

The 100 production analysis rows 115059847–115059946 contain 2,000 draw
numbers and 1,200 patch candidates. Local reproduction matches all 100 stored
patch lists exactly. Reversing the same 20-number input changes the selected
candidate set in 100/100 rows.

| Measurement | Share in 01–20 |
| --- | ---: |
| Actual source draw numbers | 24.95% |
| Existing patch candidates | 89.58% |
| Research all-source support candidates | 24.42% |

The research helper processes every source first, counts circular neighbors
at ±1/2/9/10/11, excludes present numbers, then selects 12 by support. Equal
support uses a fixed issue-specific SHA256 tie break rather than numeric
priority. Reversing inputs changes 0/100 research lists. This diagnoses and
removes this particular order dependence; its observed distribution is not
proof of forecasting skill or a guarantee of uniformity on every dataset.

## Frozen-score retrospective ablations

All 16 V2 snapshots 115059932–115059947 pass hash/ranking replay. There are
15 verified target results and one pending result; pending has no hit score.
The original saved parent 20 is held fixed for every variant. Contributions
reconstruct the saved scores with maximum residual 7.11e-15.

| Top 6 diagnostic variant | 01–20 share, all 16 snapshots | Mean hits, 15 verified |
| --- | ---: | ---: |
| Original frozen V2 | 88.54% | 1.533 |
| Remove candidate-position bonus | 86.46% | 1.867 |
| Remove Pattern contribution | 57.29% | 1.733 |
| Remove both | 56.25% | 1.600 |

Uniform 80-number Top 6 expectation: 1.500 hits. Random selection inside the
same saved parent 20: 1.600 hits on this verified cohort. Candidate shares
include the pending snapshot; mean hits exclude it. The Pattern model's own
candidate share in 01–20 is 93.44% on these 16 snapshots.

Removing position bonuses alone does not remove the main candidate bias.
Removing Pattern also leaves the old patch-dependent laowanjia candidates,
old parent 20, mature-rule order and other model tie rules unchanged. It is
not a complete repaired-pipeline replay.

## What remains unproved

These variants were chosen after observing the bias and outcomes. Their mean
differences are exploratory and cannot be counted as newly frozen prospective
OOS picks. They do not establish that disabling Pattern is best, that corrected
patches improve hits, or that higher mean hits imply profitability.

Before replacing live shadow rankings: register a distinct ranking version,
rebuild the affected candidate features from prediction-time history, preserve
the original V2 snapshots, capture both versions before each draw, and evaluate
a new fixed holdout with the same parent-20 comparator and complete exclusions.
Changing production parent-20 scoring or weights is outside this diagnostic.

## Reproduction and checks

Run `scripts/audit_top_k_number_bias.py SNAPSHOTS.json --analysis ANALYSIS.json`
using privately retained SELECT exports. Inputs contain the saved model scores,
multipliers, frozen snapshot and actual target result; the script imports no
database/worker module and makes no network requests. Do not publish raw inputs.

Five tests pass: exact legacy-function reproduction, order invariance without
mutation, circular support equivariance over all 80 rotations, 80↔1 neighbors,
and score reconstruction/pending exclusions/tamper rejection.
