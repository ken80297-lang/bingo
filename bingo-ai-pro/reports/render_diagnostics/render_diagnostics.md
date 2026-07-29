# Render Usage Diagnostics

Generated at: 2026-07-29T02:27:05.253167+00:00

## Repo Snapshot

- Git top-level: `C:/Users/ken80297/Documents/GitHub/bingo`
- Branch: `main`
- Commit: `7e25c7812539dbb9af7344c9ae1be71bd894c8f1`
- Working tree dirty: `True`

Latest commits:

```text
7e25c78 feat: add Phase 3.1 desktop replay simulator GUI
3e9a5a4 Refine dashboard card 1 UI
ce14816 Fix dashboard deploy without rule snapshot modules
5622079 Phase 30 dashboard card 1 integration
f4c6d18 fix: regenerate stale fast path predictions after strategy update
```

Working tree status:

```text
M  backend/api/collector.py
MM backend/app.py
 M backend/database/analysis_store.py
 M backend/database/operations_store.py
M  backend/services/catch_up_service.py
M  backend/services/collector_gap_service.py
M  backend/services/http_client.py
M  backend/services/next_prediction_center.py
 M backend/services/recommendation_center.py
 M backend/static/admin.html
M  backend/static/dashboard.html
M  backend/tests/test_api_smoke.py
M  backend/tests/test_collector_safety.py
M  backend/tests/test_http_client.py
M  backend/tests/test_prediction_service.py
 M backend/tests/test_recommendation_engine_output.py
A  reports/render_diagnostics/render_diagnostics.json
A  reports/render_diagnostics/render_diagnostics.md
A  scripts/diagnose_render_usage.py
 M "\345\225\237\345\213\225\346\241\214\351\235\242\346\250\241\346\223\254\345\231\250.bat"
?? ../bingo-ai-pro.rar
?? backend/api/rule_snapshots.py
?? backend/database/rule_snapshot_store.py
?? backend/services/rule_snapshot.py
?? backend/tests/test_admin_static.py
?? backend/tests/test_rule_snapshot.py
?? backend/tests/test_rule_snapshot_api.py
?? backend/tests/test_rule_snapshot_store.py
?? docs/
```

## Render Configuration

- Plan: `free`
- Start command: `uvicorn backend.app:app --host 0.0.0.0 --port $PORT`
- Health check: No explicit healthCheckPath found; Render may use default root unless configured in dashboard.

## Scheduler Jobs

| Job | Trigger | Cadence | max_instances | coalesce | misfire_grace_time | Line |
|---|---|---|---:|---|---:|---:|
| `collector_official_catch_up_startup` | `date` | startup one-shot/date | 1 | True | 90 | 250 |
| `collector_official_catch_up` | `interval` | every 5 minutes | 1 | True | 90 | 260 |
| `first_refresh` | `date` | startup one-shot/date |  |  |  | 464 |
| `refresh_job` | `interval` | every 5 minutes |  |  |  | 472 |
| `system_health_cache_startup` | `date` | startup one-shot/date |  |  |  | 361 |
| `system_health_cache_refresh` | `interval` | every 5 minutes | 1 | True |  | 368 |
| `system_status_runtime_cache_startup` | `date` | startup one-shot/date | 1 | True | 90 | 377 |
| `system_status_runtime_cache_refresh` | `interval` | every 60 seconds | 1 | True | 90 | 387 |
| `collector_pilio_startup` | `date` | startup one-shot/date |  |  |  | 398 |
| `collector_kuaishou_snapshot` | `interval` | every 5 minutes |  |  |  | 405 |
| `collector_pilio_today` | `interval` | every 1 hours |  |  |  | 412 |
| `collector_official_today` | `interval` | every 2 minutes | 1 | True | 90 | 419 |
| `data_quality_startup` | `date` | startup one-shot/date | 1 | True |  | 429 |
| `data_quality_daily` | `cron` | cron hour=3 minute=0 | 1 | True |  | 438 |
| `daily_recovery` | `cron` | cron hour=DAILY_RECOVERY_HOUR minute=DAILY_RECOVERY_MINUTE | 1 | True | 300 | 449 |

## Resource Request Matrix

| Component | Cadence | Runs/day | Max HTTP/run | Max DB/run | Risk |
|---|---:|---:|---:|---:|---:|
| `collector_official_catch_up_startup` | startup one-shot/date | startup/one-shot | up to CATCH_UP_MAX_SOURCE_PAGES + downstream verification | unknown | P0 |
| `collector_official_catch_up` | every 5 minutes | 288.0 | up to CATCH_UP_MAX_SOURCE_PAGES + downstream verification | unknown | P0 |
| `first_refresh` | startup one-shot/date | startup/one-shot | 0 | unknown | P2 |
| `refresh_job` | every 5 minutes | 288.0 | 0 | unknown | P1 |
| `system_health_cache_startup` | startup one-shot/date | startup/one-shot | 0 | unknown | P2 |
| `system_health_cache_refresh` | every 5 minutes | 288.0 | 0 | unknown | P2 |
| `system_status_runtime_cache_startup` | startup one-shot/date | startup/one-shot | 0 | unknown | P2 |
| `system_status_runtime_cache_refresh` | every 60 seconds | 1440.0 | 0 | unknown | P2 |
| `collector_pilio_startup` | startup one-shot/date | startup/one-shot | 1+ provider calls | unknown | P2 |
| `collector_kuaishou_snapshot` | every 5 minutes | 288.0 | 1+ provider calls | unknown | P2 |
| `collector_pilio_today` | every 1 hours | 24.0 | 1+ provider calls | unknown | P2 |
| `collector_official_today` | every 2 minutes | 720.0 | 1+ official API calls | unknown | P1 |
| `data_quality_startup` | startup one-shot/date | startup/one-shot | 0 | unknown | P2 |
| `data_quality_daily` | cron hour=3 minute=0 | 1 | 0 | unknown | P2 |
| `daily_recovery` | cron hour=DAILY_RECOVERY_HOUR minute=DAILY_RECOVERY_MINUTE | 1 | 0 | unknown | P2 |
| `dashboard_polling_per_open_browser` | 60000ms | 1440.0 | 4 | endpoint dependent | P1 |
| `dashboard_polling_per_open_browser` | 300000ms | 288.0 | 9 | endpoint dependent | P1 |
| `dashboard_polling_per_open_browser` | 300000ms | 288.0 | 1 | endpoint dependent | P1 |

## Dashboard Polling

- `() => loadApiGroup(fastApiKeys)` every `60000ms`: 4 APIs/cycle, approx `5760.0` API calls/day per open browser.
- `() => loadApiGroup(slowApiKeys)` every `300000ms`: 9 APIs/cycle, approx `2592.0` API calls/day per open browser.
- `() => loadApiGroup(["wakeStatus"], false)` every `300000ms`: 1 APIs/cycle, approx `288.0` API calls/day per open browser.

Configured dashboard APIs:
- `playerSummary`: `/api/dashboard/player-summary`
- `next`: `/api/next-prediction`
- `system`: `/api/system/status`
- `official`: `/api/official/statistics`
- `health`: `/api/pipeline/health`
- `operations`: `/api/operations/summary`
- `evolution`: `/api/strategy-evolution/latest`
- `evolutionHistory`: `/api/strategy-evolution/history`
- `collectorStatus`: `/api/collector/status`
- `collectorGaps`: `/api/collector/gaps`
- `latestSync`: `/api/collector/latest-sync`
- `wakeStatus`: `/api/health/wake-status`
- `analysisLatest`: `/api/analysis/latest`

## Findings

### P0 - Official catch-up is scheduled at startup and every 5 minutes

Impact: catch_up_missing_issues can fetch official pages and run downstream verification/prediction after writes; cooldown and no-gap guards now limit repeated no-op runs.

Evidence:
- `backend/app.py:250` scheduler.add_job(
- `backend/app.py:260` scheduler.add_job(

Recommendation: Keep observing Render metrics. Prefer a background worker ownership model before increasing catch-up cadence on the Render web service.

### P1 - Dashboard uses split polling instead of a single 30-second all-API loop

Impact: One visible browser is now estimated at about 8640.0 API requests/day across fast and slow timers, before page-visibility throttling.

Evidence:
- `backend/static/dashboard.html:827` pollState.fastTimer = setInterval(() => loadApiGroup(fastApiKeys), 60000);
- `backend/static/dashboard.html:828` pollState.slowTimer = setInterval(() => loadApiGroup(slowApiKeys), 5 * 60 * 1000);
- `backend/static/dashboard.html:833` pollState.hiddenTimer = setInterval(() => loadApiGroup(["wakeStatus"], false), 5 * 60 * 1000);

Recommendation: Continue tracking Render metrics; move more slow diagnostic cards to manual refresh if usage remains high.

### P1 - Official HTTP client retries SSL failures with verify=False

Impact: SSL fallback doubles request attempts on certificate failures and can mask upstream problems while increasing bandwidth/runtime.

Evidence:
- `backend/services/http_client.py:82` "data": request_json(verify=False, timeout_value=timeout),

Recommendation: Keep the diagnostic, but add bounded backoff/circuit breaker after repeated SSL failures and avoid tight retry loops.

### P2 - Legacy refresh_data job still runs every 5 minutes

Impact: refresh_data fetches latest draws, runs analysis, builds recommendation, and writes statistics alongside newer official collector/lifecycle jobs.

Evidence:
- `backend/app.py:472` scheduler.add_job(
- `backend/app.py:284` def refresh_data() -> dict[str, object]:

Recommendation: Confirm whether refresh_data is still needed. If not, disable it in production to avoid duplicate collector/analysis work.

## Suggested Fix Order

1. P0: Protect request-time catch-up, reduce 30-second status refresh pressure, and add stronger catch-up backoff.
2. P1: Split dashboard polling cadence and cache gap scan / next prediction reads.
3. P2: Review legacy refresh_data, logging volume, and query/index coverage.

## Non-Actions Confirmed

- No commit was created.
- No push was performed.
- No deploy was performed.
- No Supabase or Render API was called.
