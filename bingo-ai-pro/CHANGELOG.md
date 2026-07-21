# Changelog

## Phase 28 - v28.0.0

### Production Reset
- Starts the current production generation at issue `115040780`.
- Excludes legacy, test, pending, and invalid-generation records from production learning, statistics, recommendation, prediction history, and dashboard reads.

### Prediction Lock
- Adds single-entry prediction execution locking with stale-lock recovery.
- Prevents read/status polling from inflating `attempt_count` when reconcile is already running.

### Learning Reset
- Adds production generation and sample metadata to learning records.
- Keeps pending issue snapshots traceable while excluding them from current production learned/latest statistics.

### Daily Recovery
- Adds Taiwan-time daily recovery at `00:30` for recent lifecycle repair.
- Produces Daily Recovery and AI Health reports without creating synthetic live predictions.

### Release Registry
- Adds release registry APIs and runtime fallback metadata for `v28.0.0`.
- Adds release manifest and latest release pointer files with pending commit placeholders until the final commit exists.

### Dashboard Diagnostics
- Adds active release, production scope, and prediction traceability metadata to system, next prediction, and player dashboard responses.

### Validation Fixes
- Keeps official HTTP collection on `verify=True`; SSL failures remain pending/error diagnostics and are not treated as official verified results.
- Ensures Release Registry and Daily Recovery report reads work with both SQLite and Postgres/Supabase.
