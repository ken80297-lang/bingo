# Top 5 / Top 6 shadow validation

Follow-up to PR181 at `2afd142`; PR180 remains the safety reference for one-shot Cron.
This patch is for review only: no deploy, schema migration, weight activation, or
production data backfill is performed.

## Implemented

- Both SELECT variants read the frozen JSON metadata. The standard source/target
  reader no longer silently drops an existing snapshot.
- Verification requires exactly five and six nested picks, within the saved 20.
- V2 freezes exact prediction-time scores for all 20, source/target, UTC time,
  version, and a canonical SHA-256. Verification checks hash and replays ranking.
  A hash detects inconsistency; it does **not** prove an externally supplied
  timestamp is authentic. Retain independent pre-draw captures.
- Snapshot work is synchronous and bounded to 20 candidates. It adds no worker
  or extra database write; it uses the existing prediction insert JSON column.
- Core-only Cron skips the existing delayed shadow worker before constructing an
  executor. No new production weights or promotion eligibility are enabled.
- Generation tests compare production 20, confidence, super recommendation,
  model scores, and provenance with shadow ranking available/unavailable.
- Real save/read/result-update SQL is exercised on temporary SQLite; duplicates
  preserve the original snapshot. Cloud Postgres is **not** proved by SQLite.

## Run

From `bingo-ai-pro/backend`, with project dependencies and pytest installed:

```bash
python scripts/run_top_k_validation.py
python scripts/validate_top_k_readonly.py exported_pairs.json
```

The first command removes DB/API credentials from its child environment,
disables dotenv, sets core-only mode, and applies a 25-second deadline. It reports
exit code, elapsed time, and Linux child peak RSS. It is a local validation
benchmark, not a production Render Cron benchmark. The selected 104 tests passed
with exit code 0; one measured run took 7.049 seconds and 71,368 KiB peak RSS.

The audit command reads only local JSON and writes stdout. Input examples are
in `tests/test_top_k_readonly_audit.py`; those are synthetic, not historical
performance. Each pair contains a pre-draw record and capture time, target draw
time, the post-verification record, and the official draw issue/numbers. The
top-level plan records `plan_locked_at` and `oos_start_issue`.

Strict OOS eligibility requires a V2 snapshot, retained before the target draw,
after the plan was locked, unchanged through verification, and matched to the
official target. Duplicate targets are excluded entirely. Old V1 snapshots may
be descriptively scored by the legacy helper, but do not satisfy strict
reproducibility. No missing snapshot is reconstructed after the outcome.

## Comparisons and conclusions

Precommit the ranking version, training/weight cutoff, issue range, minimum
sample size, and primary endpoint. Evaluate all eligible issues chronologically;
report exclusion counts. To tune across walk-forward folds, each fold must use
only earlier draws and then freeze its next holdout. The supplied tool scores
existing prospective captures; it does not claim to replay model training.

For independent uniform draws of 20 from 80, expected hits are 1.25 (Top5) and
1.50 (Top6). Also compare against a random subset of the same production 20:
`k * hits20 / 20`, to distinguish ranking lift from parent-20 selection lift.
The audit reports means, both deltas, and conservative simultaneous intervals
for two means under independent-draw assumptions. These intervals do not prove
the paired ranking delta is positive. Serial dependence, repeated tuning, and
multiple models require additional block/multiple-comparison analysis.

The previously queried Supabase store had 9,964 predictions and zero frozen
Top-k rows. Its identity as the current Render data source is not yet confirmed.
Therefore the observed strict real-data sample size remains zero, means/deltas
are unavailable, and no improvement/full-hit/profitability claim is justified.
Synthetic test hits prove arithmetic only.

## Remaining merge/release gates

1. Review the patch and pass CI on the exact commit. Local selected tests are not
   the entire repository suite.
2. Confirm the intended Render workspace and service/database identity; verify
   isolated Postgres roundtrip and the actual one-shot Cron RSS/exit behavior.
3. Supply independently retained pre-draw V2 snapshots plus matching official
   results and a precommitted OOS plan. Run the audit and fold validation; no
   retroactive reselection.
4. UI is outside this read-only task and stays unimplemented. PR181's existing
   UI/OOS TODO must not be described as completed by this patch. Keep production
   weights unchanged and do not merge/deploy the broader feature as complete.
