# Render isolated smoke evidence
Date: 2026-10-23 (Asia/Taipei)
Tested code: 9921abc3421e57c1d381c5ffc92418931a8110f8
Workspace: chan's workspace (tea-d8u9lt8js32c739gp8h0)
Service: bingo-top-k-isolated-validation (srv-dbdcah5g1s2s73b0cac0)
Plan: free; region: singapore; auto-deploy: off
Deploy: dep-dbdccbdg1s2s73b0ie2g, terminal status live
Result: https://bingo-top-k-isolated-validation.onrender.com/result

## Confirmed result
HTTP 200; report commit matches the deployed code; captured at 2026-10-23T02:29:08.291484+00:00.
Smoke profile: 24 passed, 1 skipped, 4 warnings; exit_code=0; timed_out=false;
elapsed_seconds=10.019; peak_child_rss_kib_linux=53984 (52.72 MiB);
stderr empty; deadline 60 seconds. The Postgres parameter is intentionally skipped
on Render because this service has no database credentials.

The same code's full isolated Postgres 17 suite completed with 105 passed,
exit 0, 7.941 seconds, peak child RSS 87,280 KiB:
https://github.com/ken80297-lang/bingo/actions/runs/39889697279
Learning regression also succeeded:
https://github.com/ken80297-lang/bingo/actions/runs/39889700983

## Important limits
This proves that the bounded isolated test subprocess exits on a Render
container. It does not run Bingo app startup or the production business Cron
against real draws, and does not prove real prediction-time persistence,
production-cloud lifecycle performance, OOS improvement, or profitability.
The HTTP wrapper stays alive solely to serve an immutable result; it has no
rerun route or data-directory listing. No external DB config is accepted.
The reported child RSS excludes wrapper/platform overhead.

An earlier deployment of the full 104-test profile hit its 25-second limit:
dc500351c508b50e80e7473450e66eef2a7920ac /
dep-dbdcahlg1s2s73b0cc1g. It exited 124 and was not counted as success.
Render smoke is now explicitly limited to the relevant Top-k contracts with
a bounded 60-second deadline; the full suite remains in cloud CI.

No production web/Cron deployment, production DB write/backfill, new weight
activation, or delayed-worker activation occurred. Only the new isolated free
service was deployed. Real frozen samples remain the remaining empirical gate;
starting metadata capture requires an explicit expansion of the read-only scope.

