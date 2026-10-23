# Public shadow validation view

`/shadow-ranking` is linked from the existing dashboard. Its only data request
is GET `/api/top-k-shadow`; refreshing never invokes ingestion, recommendation,
verification, learning or weight updates. The production Cron branch is not
modified by this main-based change.

The endpoint reads at most 200 saved shadow snapshots with official results
in one read-only PostgreSQL transaction, with a two-second statement timeout.
It does not initialize schemas or fall back to SQLite. Failures return a
sanitized 503 and the browser retains its previous successful data.

Only V2 snapshots passing hash, exact ranking replay, parent/source/target,
timing and official-result checks contribute to the averages. Pending records
have no hit count. Duplicate targets and invalid records are excluded, never
imputed as zero. JSON numeric scores are restored to the writer's float type
before canonical hash replay; scores are never sent to the browser.

The UI labels the window as recent saved snapshots, not all target opportunities.
It reports both uniform 80-number expectations (1.25/1.50) and expectations for
random selection within the same parent 20. Database timestamps and hashes
are not independent pre-draw archives: the public averages are descriptive,
not proof of completed OOS/walk-forward validation, superiority or profitability.

Validation: 11 Python tests cover official-result mismatches, late snapshots,
tampering, duplicates, reranking, pending records, float restoration, read-only
query constraints and sanitized database errors. Browser checks cover 390px
and 768px widths, pending picks and data retention after a failed refresh.
