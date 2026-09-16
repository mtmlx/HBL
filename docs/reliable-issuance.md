# Reliable issuance: candidate, not deployed

This change builds on the captured AWS source in PR #2. The historical baseline
is intentionally unchanged. The three changed runtime files and new job journal
will not match that baseline until a reviewed release is deployed and recaptured.

## Guarantees within the queued issuer

- One conditional DynamoDB claim per task; another invocation cannot claim a
  live lease. Every checkpoint verifies ownership and lease validity.
- The 960-second lease exceeds Lambda's maximum 900-second execution lifetime.
  An expired owner cannot checkpoint or begin a new guarded mutation.
- Failed invocations raise to SQS. This works with the current batch size 1 and
  without partial-batch configuration. Processing stops on failure for FIFO safety.
- Confirmed boundaries persist an artifact and the original package ID. Recovery
  downloads the existing PDF from its recorded S3 location and checks SHA-256;
  it does not regenerate or register another package.
- Registration, upload, status, and comment writes are marked in-flight before
  being attempted. An interrupted/uncertain write becomes NEEDS_REVIEW and cannot
  be replayed automatically. The package ID is saved before registration starts.
- Attachment replacement uploads/adds the new attachment, confirms its exact ID,
  then removes only IDs captured before replacement. Upload/readback failure keeps
  the prior attachment. Concurrent additions are not cleared.
- The original three-line assigned completion comment remains unchanged.

## Recovery states

| State | Recovery |
| --- | --- |
| START | Retry preparation; no registration has begun |
| READY | Download and hash-check the existing package, then attach |
| ATTACHED | Continue status and comment |
| STATUS_UPDATED | Continue assigned completion comment |
| COMPLETE | Finish job record without repeating external writes |
| REGISTERING / UPLOADING / UPDATING_STATUS / POSTING_COMMENT | Reconcile external outcome; do not repeat automatically |
| Legacy FAILED/RUNNING without a phase | Reconcile; the old code did not record enough evidence |

Drafts have no durable S3 artifact. A draft interrupted at READY is explicitly
flagged for reconciliation; this change does not claim automatic draft recovery.

## Required release checks

### Test evidence (2026-09-16 UTC)

- All 118 local tests pass, including five full-worker fault simulations using
  the real PDF renderer/registration code, AWS emulation, and a fake ClickUp client.
  Restart/replay retained one package ID, six verification records, matching PDF
  SHA-256, one upload/status/comment at confirmed boundaries, and the assigned
  three-line comment. An uncertain comment boundary quarantined instead of replaying.
- Fourteen checks passed against a newly created real DynamoDB table. Eight
  concurrent retry claimants produced one owner; stale owners were rejected;
  safe checkpoints preserved artifacts; uncertain checkpoints quarantined;
  completed and legacy jobs could not blindly repeat issuance. The table was
  deleted and absence verified. These tests inject expired leases; they do not
  run a real Lambda timeout or queue redrive. The reusable opt-in runner is
  `tools/test_job_journal_aws.py`.
- A live ClickUp trial remains pending a designated sandbox task/list. No customer
  task, live HBL, verification record, queue setting, or deployed Lambda was changed.

### Deployment checklist

Live configuration inspected during development: worker timeout 180 seconds,
queue visibility 180 seconds, batch size 1, DLQ redrive after three receives.
Before deploying this candidate:

1. Set queue visibility to at least 1080 seconds (six times current timeout and
   longer than the 960-second lease), plus any batch window. Otherwise crashes
   can exhaust receives before a lease expires. Retain the DLQ and alarm on its
   visible-message count. Alert on NEEDS_REVIEW / reconciliation-required logs.
2. Verify worker IAM permits existing jobs-table GetItem/PutItem/UpdateItem and
   GetObject for issued S3 PDFs. Do not broaden access beyond required resources.
3. Reconcile existing RUNNING/FAILED jobs before releasing them. Never delete a
   job lock or reset it to START merely to retry an issued package.
4. Run a designated test-task issuance and inject failures at each external
   boundary. Check one package ID, six verification records, PDF SHA-256, exact
   attachment ID, task status, and the assigned three-line comment.
5. Replay the same queue message and confirm it makes no new package or comment.
   Crash a worker between confirmed checkpoints and verify recovery.
6. Review/merge the source change, deploy an identified revision, then compare
   deployed source hashes and update the source baseline. Preserve prior artifact
   for rollback, but do not roll back to an issuer that ignores new job states
   while pending work exists; pause consumption and reconcile first.

## Reconciliation procedure

Stop automatic attempts for NEEDS_REVIEW. Read the job's phase/artifact JSON;
inspect the saved package's S3 objects, hashes, all verification records, ClickUp
attachment IDs, status, and comments. A timeout is not proof a write failed.
For registration ambiguity, repair/complete the same package or void its partial
records through an audited operation. Never generate a replacement merely to
repair its comment. Only after readback may an operator advance the checkpoint
under a conditional write and redrive the existing job. Do not blindly reset it.

## Limits

These are failure-containment guarantees, not a claim of exactly-once execution
across independent services. The six verification writes are still sequential;
an interrupted registration can leave a partial package requiring reconciliation.
Ambiguous ClickUp responses require readback by an operator. The dormant manager
reissue route and direct local issuance tools do not use the queued-worker journal;
do not expose/automate them as a resilient reissue service. A manager reissue release
still needs a shared operation lock, signed request identity, and an atomic
old/new verification transition. AWS and ClickUp staging fault tests remain a
release requirement; mocked tests alone do not establish production readiness.
