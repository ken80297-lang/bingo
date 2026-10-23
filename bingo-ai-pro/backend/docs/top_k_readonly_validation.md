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

The lifecycle test now also has a Postgres parameter, skipped unless
`TOP_K_TEST_POSTGRES_DSN` explicitly identifies loopback and the dedicated
`top_k_validation` database. It creates a unique temporary schema, runs the real
JSONB insert/read/update paths, rejects SQLite fallback, and drops the schema in
fixture cleanup. The isolated GitHub Actions workflow provisions a disposable
Postgres 17 service with no production credentials, runs the bounded suite,
and then destroys the service. This tests PostgreSQL behavior without changing
Render or production tables. Workflow execution status must be checked before
claiming cloud Postgres validation passed.

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

The Supabase store now has 9,969 predictions and zero frozen Top-k rows.
Three current record fingerprints match the live production HTTP API exactly
(IDs, source/target, prediction/creation timestamps, all 20 picks, hit counts,
and writer commit). This corroborates the shared live dataset without reading
secret environment variables; the configured DSN itself was not inspected.
Therefore the observed strict real-data sample size remains zero, means/deltas
are unavailable, and no improvement/full-hit/profitability claim is justified.
Synthetic test hits prove arithmetic only.

## Live read-only evidence, 2026-10-23 (Asia/Taipei)

The user confirmed `chan's workspace` (`tea-d8u9lt8js32c739gp8h0`).
Render lists no managed Postgres instances in this workspace. Reads used the
existing Supabase connection and production HTTP API; no DB schema/data,
service settings, or weights were changed.

| Resource | Live commit / evidence |
| --- | --- |
| `bingo-ai-pro`, `srv-d925llsm0tmc73djvme0` | main, `4c995cc35bc00785ce4387e9b09bcee120bbe717`; HTTP diagnostics agree, Postgres read succeeded without fallback |
| `bingo-ai-lifecycle-worker`, `crn-db1uohbbc2fs73ei9gv0` | fix-learning-snapshot-integrity, `ac082dce7d824c0e738b59d92267d24fb5919389` |
| 10:00 slot, target `115059922` | worker summary ok, 40.450 s, peak 90.45 MiB; Render successful finish at 10:02:14 |
| 10:05 slot, target `115059923` | worker summary ok, 40.179 s, peak 87.57 MiB; Render successful finish at 10:07:13 |

Both Cron summaries show prediction save to cloud, delayed shadow generation
deferred, shadow executors stopped, DB pools closed, and verification ok.
These are observations of the **old deployed code**, not a cloud test of this
patch. The 40-second metric excludes the Cron wrapper's wait for draw readiness.
Neither deployed commit contains PR181/PR182, explaining why no frozen Top-k
rows are being produced. No deployment was triggered.

Live API `/api/prediction-history/history?limit=3` and Supabase both returned:

| Record ID | Source | Target | Created (UTC) | Parent-20 hits at read |
| --- | --- | --- | --- | --- |
| 10187 | 115059920 | 115059921 | 2026-10-23 01:56:52.268115 | 6 |
| 10188 | 115059921 | 115059922 | 2026-10-23 02:01:52.033444 | 5 |
| 10189 | 115059922 | 115059923 | 2026-10-23 02:06:51.793222 | waiting draw |

The parent-20 hits are **not** Top5/Top6 results. The read-only aggregate was
`count(*)=9969`, `count(*) filter (where fast_path_metadata ? 'top_k_shadow')=0`.
Late reconstruction of these missing picks would not create eligible OOS data.

## Remaining merge/release gates

1. Review the patch and pass CI on the exact commit. Local selected tests are not
   the entire repository suite.
2. Workspace, services and live dataset fingerprints are now checked. Still
   verify isolated Postgres roundtrip and one-shot Cron RSS/exit on the exact
   patched commit; observed old-version success is not a substitute.
3. Supply independently retained pre-draw V2 snapshots plus matching official
   results and a precommitted OOS plan. Run the audit and fold validation; no
   retroactive reselection.
4. UI is outside this read-only task and stays unimplemented. PR181's existing
   UI/OOS TODO must not be described as completed by this patch. Keep production
   weights unchanged and do not merge/deploy the broader feature as complete.
