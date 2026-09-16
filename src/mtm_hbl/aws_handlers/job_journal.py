"""DynamoDB ownership and crash boundaries for the Lambda issuance worker.

A lease exceeds Lambda's maximum 900 second invocation lifetime. Only confirmed
boundaries can be resumed. An in-flight external mutation is quarantined instead
of being repeated: ClickUp and DynamoDB do not share a transaction.
"""
import json
import time
from uuid import uuid4

from botocore.exceptions import ClientError


SAFE_PHASES = {"START", "READY", "ATTACHED", "STATUS_UPDATED", "COMPLETE"}
LEASE_SECONDS = 960


class JobBusy(RuntimeError):
    pass


class ReconciliationRequired(RuntimeError):
    pass


class JobJournal:
    def __init__(self, table, job_id, task_id, mode):
        self.table, self.job_id = table, job_id
        self.owner = uuid4().hex
        now = int(time.time())
        self.deadline = now + LEASE_SECONDS
        item = dict(job_id=job_id, task_id=task_id, mode=mode, status="RUNNING",
                    owner=self.owner, lease_until=self.deadline, phase="START", attempts=1)
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing = table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item", {})
            if existing.get("status") in {"ISSUED", "GENERATED"}:
                self.item = existing
                self.done = True
                return
            if existing.get("status") == "NEEDS_REVIEW" or "phase" not in existing:
                raise ReconciliationRequired("Legacy or uncertain job requires reconciliation; do not reissue.")
            try:
                response = table.update_item(
                    Key={"job_id": job_id},
                    ConditionExpression="lease_until < :now AND #s IN (:running, :retry)",
                    UpdateExpression="SET #s = :running, #owner = :owner, lease_until = :lease ADD attempts :one",
                    ExpressionAttributeNames={"#s": "status", "#owner": "owner"},
                    ExpressionAttributeValues={":now": now, ":running": "RUNNING", ":retry": "RETRYABLE",
                                               ":owner": self.owner, ":lease": self.deadline, ":one": 1},
                    ReturnValues="ALL_NEW",
                )
                item = response["Attributes"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                raise JobBusy("Another invocation owns this job; retry later.") from exc
        self.item, self.done = item, False
        if self.phase not in SAFE_PHASES:
            self.fail()
            raise ReconciliationRequired("Interrupted external write requires reconciliation; do not reissue.")

    @property
    def phase(self):
        return self.item.get("phase", "START")

    @property
    def artifact(self):
        return json.loads(self.item.get("artifact_json", "{}"))

    def save(self, phase, artifact):
        # Set local phase first. A timed-out checkpoint write is itself uncertain.
        self.item["phase"] = phase
        self.item["artifact_json"] = json.dumps(artifact)
        self._update("RUNNING", phase, self.item["artifact_json"], self.deadline)

    def _update(self, status, phase, artifact, lease):
        now = int(time.time())
        self.table.update_item(
            Key={"job_id": self.job_id},
            ConditionExpression="#owner = :owner AND lease_until > :now",
            UpdateExpression="SET #s = :status, phase = :phase, artifact_json = :artifact, lease_until = :lease",
            ExpressionAttributeNames={"#owner": "owner", "#s": "status"},
            ExpressionAttributeValues={":owner": self.owner, ":now": now, ":status": status,
                                       ":phase": phase, ":artifact": artifact, ":lease": lease},
        )

    def fail(self):
        status = "RETRYABLE" if self.phase in SAFE_PHASES else "NEEDS_REVIEW"
        self._update(status, self.phase, self.item.get("artifact_json", "{}"), 0)

    def complete(self, mode):
        self._update("ISSUED" if mode == "issue" else "GENERATED", "COMPLETE",
                     self.item["artifact_json"], self.deadline)
