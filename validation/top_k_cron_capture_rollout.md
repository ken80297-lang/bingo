# Metadata-only Top-k capture on the existing AI Cron

The user explicitly authorized saving pre-draw shadow snapshots in existing
prediction metadata on 2026-10-23. This patch targets the actual deployed Cron
base `ac082dc`, not the divergent main/PR181 branch.

## Scope

- Preserve the Cron's existing super recommendation, production 20, confidence,
  model scores, adaptive-weight updates, promotion eligibility, schedule and
  core-only lifecycle behavior. No new learning/production weight gate is enabled.
- Add exact prediction-time Top5/Top6 selections, 20 score values, source/target,
  UTC frozen time, version and SHA-256 under `fast_path_metadata.top_k_shadow`.
- Use the existing prediction insert; no new table, extra insert, historical
  reselection/backfill, delayed shadow executor or UI change.
- `TOP_K_SHADOW_CAPTURE_ENABLED` is off outside explicitly enabled callers. The
  existing Cron child defaults it to 1, respecting an explicit override of 0.
  `AI_LIFECYCLE_CRON_CORE_ONLY=1` remains set. The web service is not deployed.
- Readers preserve the metadata and can score only frozen target-matched picks
  after the record is verified. Scoring itself is read-only.

## Validation before rollout

The bounded runner covers helper integrity, actual temporary SQLite/Postgres
save/read/verification, production-output invariance with capture on/off/failing,
learning regression, Cron readiness/deadline protection, metadata capture default
and override, and worker endpoint authentication. PostgreSQL cloud validation
must pass on the exact source before the Cron branch is advanced.

## Deployment and verification

The existing `bingo-ai-lifecycle-worker` tracks `fix-learning-snapshot-integrity`
with auto-deploy enabled. Merge only this reviewed Cron-targeted patch after CI;
do not merge PR181/182 or switch the production Cron to the main-derived branch.
Wait for the specific Render deploy to be live, then let the next natural Cron
run finish (do not manually trigger and cancel an active scheduled run).

Read the first new snapshot before its official target draw; retain independent
capture evidence, including capture time and hash. After the target result is
published, check official issue/numbers and the unchanged snapshot, recompute
hits, and compare descriptively with 1.25/1.50 and same-parent-20 random picks.
A first sample validates the chain; it cannot establish better prediction.

Keep the previously registered OOS protocol (PR182 commit `51656bd`) and collect
the prospective roster rather than rebuilding historical picks. Record rollout
SHA, target issues, times, Cron exit/RSS, coverage, and exclusions.

## Stop / rollback

An explicit `TOP_K_SHADOW_CAPTURE_ENABLED=0` on the Cron disables further capture
after configuration deployment while preserving production predictions. Revert
the rollout commit if broader rollback is required. Existing frozen snapshots
must remain intact; do not delete or replace them to improve reported results.
