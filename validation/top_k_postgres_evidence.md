# Isolated PostgreSQL validation evidence
Date: 2026-10-23 (Asia/Taipei)
Tested source commit: b3f424b85ab6c8eea60154bfe3195f8b83b2c422
PR: https://github.com/ken80297-lang/bingo/pull/182

## Confirmed
- GitHub-hosted job, disposable PostgreSQL 17 service. No production DB credentials or Render settings used.
- Run https://github.com/ken80297-lang/bingo/actions/runs/39888567796 completed successfully.
- Job 120202502140: 105 passed, no skipped tests; exit code 0; timed_out=false; empty stderr.
- Bounded validation subprocess elapsed 7.844 seconds, peak child RSS 87,160 KiB (85.12 MiB). This excludes the PostgreSQL service container and is not a Render Cron memory benchmark.
- Both SQLite and PostgreSQL parameters passed. Real production store SQL saved/read JSONB metadata and verified the target draw in a unique temporary test schema. Duplicate inserts preserved original selection; SQLite fallback was forbidden in the Postgres parameter.
- Temporary schema cleanup succeeded and the workflow's Stop containers step succeeded.
- Production-output invariance and core-only delayed-worker guards passed in the same suite.
- Existing learning closed-loop regression run https://github.com/ken80297-lang/bingo/actions/runs/39888572730 also completed successfully on this source commit.

## Still unproved
- Render runtime integration/RSS/exit behavior on this patch. The known successful Render Cron observations concern older deployed code.
- Real pre-draw frozen selections, prospective OOS hit rates and empirical walk-forward advantage. The last read-only live dataset check found zero frozen Top-k rows.
- Feature-flag UI, outside the read-only validation scope.

## Cannot conclude
Passing synthetic database and arithmetic tests does not establish predictive improvement, increased full-hit probability, or profitability. The real-data OOS means/deltas remain unavailable. Old records must not be re-ranked after their outcomes and reported as prospective samples.

No merge, Render deploy, production DB write/backfill, new weight activation, or delayed shadow worker activation was performed. This evidence closes the isolated PostgreSQL contract-test gap only.
