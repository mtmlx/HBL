# Draft readiness guard

ClickUp draft requests previously accepted incomplete fallback data and could upload a mostly blank PDF while reporting success. The September 23 overnight audit also found a draft request with an invalid null prepaid amount.

Before rendering a ClickUp-backed draft, the generator now requires:

- The configured Ready For Draft checkbox (field ID `e51205ba-ea9d-4755-a3fe-1648770b6671`) to be explicitly checked (`true` boolean or string).
- Valid canonical HBL JSON. Missing canonical data cannot fall back to a partial review packet for a draft request.
- Matching task/HBL identities, required parties, routing, vessel/voyage, cargo, container/seal details, and positive numeric cargo quantities.
- Consistent container/cargo totals and no hard QA errors from either the canonical model or ClickUp's QA Hard Errors field.
- At least one visible charge line with description, rate, currency and a valid non-negative prepaid or collect amount. The established total-only package-count exception remains supported.

The explicit draft path and automatic fallback to draft both apply the guard. Original issuance keeps its existing approval and recovery behavior.

Before uploading a prepared draft, the generator re-reads ClickUp, revalidates readiness, compares reviewed content, and verifies the local PDF hash. A withdrawn checkbox, changed data, corrupted PDF or legacy prepared draft without readiness evidence stops attachment. Already attached legacy files are not automatically replaced.

This validates readiness, structure and internal consistency. It does not determine commercial freight authorization or reconcile a vessel against external shipment/source documents. Those remain part of the human review before checking Ready For Draft.

## Validation rejection handling, September 25, 2026

The initial guard rejected canonical IDs written as `task:<id>`, even when the ID matched the HBL task. It now accepts that exact representation while still rejecting unrelated IDs and mismatched HBL numbers.

Draft readiness and canonical-data errors now raise `DraftValidationBlocked`. Only draft requests still at the journal's `START` phase are acknowledged as `BLOCKED`, with the reason retained in the journal and an explanatory ClickUp comment attempted once. Duplicate deliveries of that request stay blocked; after correction, an operator must submit a new request. Malformed JSON and schema errors report field locations without copying raw customer data.

Errors after preparation, uncertain writes, service failures and Original issuance retain their recovery behavior and alarms. Prepared drafts are still revalidated before attachment. No historical DLQ messages are automatically redriven or purged.

- Full suite: **204 passed**. Added coverage for the prefixed task ID, wrong IDs, blocked-request redelivery, fresh requests after a block, and preservation of recovery for prepared drafts and Originals.
- Read-only validation against the current GOSZX26061961 task passes after normalization. GOSZX26071299 remains blocked with Ready For Draft unchecked; its freight fields also remain incomplete. Both tasks lack Original approval.
- Worker version **4**, deployed September 25 at 19:51 UTC. AWS package SHA-256: `yRCiYDtxSN2sSC1cPOcfaU4vZdm9qLewKoURHhMEBdw=`.
- The package replaces only the generator, worker handler and job journal modules. All dependencies, rendering assets and configuration are preserved. This incident correction makes no typography or layout changes; the existing PDF renderer still uses Helvetica.
- Rollback ZIP and verification evidence are retained under `runs/hbl-incident-20260925/` in the parent workspace. When rolling back, retain BLOCKED job records; old code does not understand that terminal status, so do not redrive those requests into the old worker.

## Validation and deployment, September 23, 2026

- Full local suite: `AWS_EC2_METADATA_DISABLED=true .venv/bin/python -m pytest tests -q` — **196 passed**. Existing datetime deprecation warnings remain.
- Regression cases cover unchecked/missing readiness, missing/malformed canonical JSON, null prepaid amounts, incomplete but schema-valid data, hard QA, invalid charges, changes between rendering and upload, legacy recovery, and valid reviewed drafts. Existing Original issuance and crash-recovery tests pass.
- Fresh read-only copies of the three incident tasks were all blocked by readiness. In-memory-only readiness overrides also reproduced the null prepaid schema error. No canary wrote to ClickUp or generated live documents.
- Updated only `mtm-hbl-original-worker-dev` in account `525753067477`, region `us-east-1`; published version **3**, serving the unqualified worker used by the enabled SQS mapping.
- The deployment ZIP differs from the prior live ZIP only in `mtm_hbl/clickup_hbl_generator.py`. Dependencies, packaged config, other application modules, environment, IAM and event-source settings were retained.
- Deployed ZIP SHA-256 (AWS base64): `i4mdq8Dg4/LAhDEbWBnEVjElu5VsbPBA/NSNdFtIWjE=`. Deployed source SHA-256: `ed37bf34472427b687cd62d7ec4400980b674ada4ecd94fa6fbb3202d9b417de`.
- AWS advanced the managed Python 3.11 runtime build during the code update. Operational configuration comparison against the prior published version passed after accounting for that managed-runtime change.
- Lambda readback: Active / Successful; downloaded deployed ZIP and source hashes verified. Empty-worker smoke invocation returned HTTP 200 with `{"results": []}` and no FunctionError. Normal queue empty, event mapping enabled; the existing one-message DLQ was left intact.

Local rollback ZIP, release manifest, before/after receipts and canary evidence are retained under the parent workspace's `runs/hbl-draft-guard-release-20260923/`. Before rollback, inspect queue/journal state and preserve any new readiness evidence; restore the saved prior ZIP with a fresh Lambda revision check and verify its checksum. No incident document was regenerated, replaced, or issued by this deployment.
