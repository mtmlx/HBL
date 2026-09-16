# Reliable issuance

**Deployed 2026-09-16 UTC at runtime revision `55abd79` after explicit user approval.**
See [deployment record](deployment-2026-09-16.md). The development/release checklist
below records the preceding stages and remaining live fault-injection limits.

This change builds on the captured AWS source in PR #2. The historical baseline
is intentionally unchanged. The changed runtime files and new job journal
will not match that baseline until a reviewed release is deployed and recaptured.

## Guarantees within the queued issuer

- One conditional DynamoDB claim per task; another invocation cannot claim a
  live lease. Every checkpoint verifies ownership and lease validity.
- The 960-second lease exceeds the standard Lambda 900-second execution limit.
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

- All 140 local tests pass, including five full-worker fault simulations using
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
  task, live HBL, verification record, or deployed Lambda was changed by tests.
  The separately authorized queue/alarm configuration is recorded below.

### Deployment checklist

Current configuration: worker timeout 180 seconds, queue visibility 1080 seconds,
batch size 1, DLQ redrive after three receives.
Before deploying this candidate:

1. Preserve queue visibility of at least 1080 seconds (six times current timeout and
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
For registration ambiguity, inspect the complete transaction outcome and the
existing S3 objects; reconcile the same package through an audited operation. Never generate a replacement merely to
repair its comment. Only after readback may an operator advance the checkpoint
under a conditional write and redrive the existing job. Do not blindly reset it.

## Manager reissue safeguards

The manager confirmation now carries a signed, expiring preview binding the user,
task, HBL, prior record IDs/package IDs, shipment fingerprint, and customer reason.
The shipment fingerprint is rechecked immediately before generation. Normal worker
issuance and manager reissue share the same task lock. Repeated confirmations reuse
the completed result; another operation cannot replace an in-progress/uncertain one.

A replacement is registered as PREPARED. One conditional DynamoDB transaction voids
all signed prior records and activates all six replacement records. A conflict rolls
back the entire switch. Only then does ClickUp attachment/status/comment completion
run. The old attachment remains if uploading the replacement fails, but its prior
verification records are then VOID; the existing replacement must be reconciled.
The manager operation is quarantined on an uncertain failure; it is never blindly
reissued. Customer reason is recorded in the void records and assigned audit comment.

Registration itself now writes the complete verification set in one transaction.
PDF and canonical S3 objects use conditional creation and cannot overwrite an
existing package object. PREPARED and every non-ISSUED verification status displays
an explicit warning. There is no all-service transaction across S3, DynamoDB and
ClickUp: prepared/orphaned objects and uncertain ClickUp outcomes require readback.

## Additional test evidence

The manager flow is tested with synthetic data for successful atomic switching,
duplicate confirmations, shared worker/manager exclusion, expired/interrupted work,
stale/tampered/wrong-user previews, shipment changes, transaction conflicts, and
ClickUp failure after activation. Three additional checks passed against a disposable
real DynamoDB table: conflict rollback, complete 6-old/6-new switch, and duplicate
rejection. The table was deleted and absence verified. No actual HBL was generated
or reissued by the live test. Use `tools/test_atomic_switch_aws.py` to repeat it.

## Configuration and release

Current configuration update: queue visibility was raised from 180 to 1080 seconds
and read back. SNS topic `mtm-hbl-recovery-alerts-dev` and worker DLQ/recovery alarms
were created and verified. The topic currently has no subscribers; its delivery
email/endpoint is pending. The code also makes one assigned ClickUp recovery-comment
attempt per failed operation. It claims the attempt durably before posting so a
lost response cannot cause duplicate comments. An unsuccessful/ambiguous comment
attempt is handled through the SNS/log alarm rather than blindly reposted.
These code changes are not active until deployment. No existing task was commented
on or changed by configuration/testing.

`PYTHONPATH=src python tools/configure_hbl_recovery.py --profile PROFILE` inspects the
live queue. Applying requires `--apply --alarm-topic SNS_TOPIC_ARN`; it sets at least
1080 seconds for the inspected 180-second worker, installs an error metric filter,
creates DLQ/recovery alarms, and reads back the settings. It never invokes Lambda,
redrives jobs, deploys code, or changes a shipment. The alert destination is pending.
If a manager Lambda is deployed later, add the same alerting for its
HBL_REISSUE_RECONCILIATION_REQUIRED and HBL_CHECKPOINT_FAILURE log markers.

Read-only preflight found 52 legacy FAILED jobs, zero legacy RUNNING jobs, and no
existing HBL alarms. These failed jobs are not safe to automatically replay. Existing
role permissions currently permit the required operations; its broad managed admin
policy is outside this patch and should be replaced through a separately reviewed
least-privilege change across all functions sharing the role.

## Remaining operational limits

No deployment or live shipment mutation has occurred. The manager route is still
not exposed by a deployed handler/API route; this patch does not enable one. Direct
low-level issuance tools do not acquire the task journal and must not be used as a
retry mechanism. The lock coordinates operations on the same ClickUp task, not
independent tasks with an accidentally duplicated HBL number. External edits can
still race with ClickUp writes. Ambiguous results remain fail-closed for operator
reconciliation. Live ClickUp fault injection requires an isolated test task; the
read-only CANEI276138722 checks do not substitute for that release test. Independent
review, monitored staging, configuration, and deployment are still required.
