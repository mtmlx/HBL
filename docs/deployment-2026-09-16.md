# AWS deployment — 2026-09-16 UTC

Runtime revision: `55abd79560b403d970f27ac920252b0bd1adbe5f` (PR #3).
The user explicitly approved direct AWS deployment. PR #3 remains subject to
GitHub independent approval before merge; branch protection was not changed.

Updated the original worker, original webhook, and verification service in
us-east-1. Each published version is `1`; unqualified handlers use the same
updated code. Queue consumption was paused with an empty queue, then restored
after all runtime checks passed. Environment, IAM, routes and task records were
not changed. The manager portal was not exposed.

Validation: deployed ZIP hashes match the release artifacts; all 58 issuer files
match on each issuer function and all five verification files match. The empty
worker event, unauthorized webhook check, authenticated webhook dry run, and
existing public verification page passed. The ClickUp original field, status and
assigned completion comment were unchanged. No issuance/reissue/void was invoked.
Full live attachment replacement/fault injection was not performed. Prior evidence
includes 140 unit/integration tests and 17 disposable-table AWS checks.

SNS email subscription for mario@mtmlogix.com is confirmed. Both recovery alarms
are configured and queue visibility is 1080 seconds. Recovery comments now use the
deployed code; no synthetic failure comment was posted to a customer task.

Rollback artifacts and full deployment receipts are retained outside the repository
in `hbl-release-55abd79/`. Pause consumption and reconcile any new journal states
before restoring the old worker; never blindly redrive legacy failed jobs.

The historical `aws-source-baseline.json` remains the pre-release capture. Use
`aws-release-55abd79.json` for this deployment's verified artifact hashes.
