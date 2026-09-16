from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from mtm_hbl.clickup_connector.client import ClickUpClient
from mtm_hbl.config import AppConfig, Settings
from mtm_hbl.models.canonical import CanonicalHblData
from mtm_hbl.models.clickup import ClickUpTaskData
from mtm_hbl.pdf.hbl_package import generate_bill_of_lading_draft, generate_bill_of_lading_package
from mtm_hbl.pipeline import PipelineInput, build_review_packet
from mtm_hbl.verification.aws_repository import AwsVerificationConfig, register_issued_package


GenerationMode = Literal["auto", "draft", "issue"]


class ApprovalDecision(BaseModel):
    approved: bool = False
    approval_value: str = ""
    approved_by: str = ""
    approved_at: str = ""
    reason: str = ""


class ClickUpHblGenerationResult(BaseModel):
    task_id: str
    hbl_number: str = ""
    mode_requested: GenerationMode = "auto"
    mode_generated: Literal["draft", "issue"]
    approval: ApprovalDecision = Field(default_factory=ApprovalDecision)
    pdf_path: str
    review_path: str
    package_id: str = ""
    verification_urls: dict[str, str] = Field(default_factory=dict)
    pdf_sha256: str = ""
    canonical_json_sha256: str = ""
    clickup_attachment_uploaded: bool = False
    clickup_output_field_id: str = ""
    clickup_status_updated_to: str = ""
    clickup_comment_posted: bool = False
    clickup_comment_assignee_id: str = ""
    warnings: list[str] = Field(default_factory=list)


def parse_clickup_task_id(task_ref: str) -> str:
    value = task_ref.strip()
    if not value:
        raise ValueError("ClickUp task link or ID is required.")
    if "/" not in value:
        return value

    value = value.split("?", maxsplit=1)[0].rstrip("/")
    match = re.search(r"/t/(?:[^/]+/)?([^/#?]+)$", value)
    if not match:
        raise ValueError(f"Could not parse ClickUp task ID from: {task_ref}")
    return match.group(1)


async def generate_hbl_from_clickup(
    *,
    task_ref: str,
    client: ClickUpClient,
    settings: Settings,
    app_config: AppConfig,
    mode: GenerationMode = "auto",
    output_dir: Path | None = None,
    logo_path: Path | None = None,
    attach_to_clickup: bool = False,
    post_comment: bool = False,
    verification_base_url: str = "",
    bucket: str = "",
    table: str = "",
    region: str = "",
    issued_by: str = "Andrea Piedad Velasquez Castellon",
    prevent_original_overwrite: bool = False,
) -> ClickUpHblGenerationResult:
    task_id = parse_clickup_task_id(task_ref)
    task = await client.get_task(task_id)
    clickup_values = client.extract_configured_fields(task, app_config)
    data = _data_from_clickup_task(task, clickup_values, app_config)
    _enforce_clickup_hbl_number(data, clickup_values)

    approval = evaluate_hbl_approval(task, app_config)
    warnings = [issue.message for issue in data.qa.soft_warnings]
    if data.qa.hard_errors:
        warnings.extend(issue.message for issue in data.qa.hard_errors)

    generated_mode = _select_generation_mode(mode, approval, data)
    planned_output_field_id = _output_field_id_for_mode(generated_mode, app_config)
    if (
        generated_mode == "issue"
        and attach_to_clickup
        and prevent_original_overwrite
        and planned_output_field_id
        and _attachment_output_field_exists(task, planned_output_field_id)
    ):
        raise ValueError(
            "Cannot issue HBL automatically because the ClickUp original field already "
            "contains an attachment. Use the controlled reissue/void flow."
        )
    base_output_dir = output_dir or _default_output_dir(settings.runs_dir, task_id, data)
    base_output_dir.mkdir(parents=True, exist_ok=True)
    review_path = base_output_dir / "approved_review_from_clickup.json"
    review_path.write_text(data.model_dump_json(indent=2), encoding="utf-8")

    registration = None
    if generated_mode == "issue":
        verification_base_url = verification_base_url or settings.hbl_verification_base_url
        bucket = bucket or settings.hbl_verification_bucket
        table = table or settings.hbl_verification_table
        region = region or settings.aws_region
        _require_issuance_config(verification_base_url, bucket, table)
        package_id = f"pkg_{uuid4().hex}"
        verification_id_suffix = _verification_id_suffix(package_id)
        pdf_path = base_output_dir / f"HBL_Package_{data.shipment.mtm_hbl_no}.pdf"
        generate_bill_of_lading_package(
            data,
            pdf_path,
            logo_path=logo_path,
            draft=False,
            verification_base_url=verification_base_url,
            verification_id_suffix=verification_id_suffix,
        )
        registration = register_issued_package(
            data,
            pdf_path,
            AwsVerificationConfig(
                bucket_name=bucket,
                table_name=table,
                region_name=region,
                verification_base_url=verification_base_url,
            ),
            status="ISSUED",
            package_id=package_id,
            verification_id_suffix=verification_id_suffix,
            issued_by=issued_by,
        )
    else:
        pdf_path = base_output_dir / f"Draft_{data.shipment.mtm_hbl_no or task_id}_v1.pdf"
        generate_bill_of_lading_draft(data, pdf_path, logo_path=logo_path)

    clickup_attachment_uploaded = False
    clickup_output_field_id = ""
    clickup_status_updated_to = ""
    clickup_comment_posted = False
    clickup_comment_assignee_id = ""
    if attach_to_clickup:
        clickup_output_field_id = planned_output_field_id
        if clickup_output_field_id:
            await client.upload_attachment_to_custom_field(task_id, clickup_output_field_id, str(pdf_path))
            await client.verify_attachment_custom_field(task_id, clickup_output_field_id, pdf_path.name)
            if generated_mode == "issue":
                clickup_status_updated_to = _status_after_original_upload(app_config)
                if clickup_status_updated_to:
                    await client.update_task_status(task_id, clickup_status_updated_to)
        else:
            await client.upload_attachment(task_id, str(pdf_path))
        clickup_attachment_uploaded = True
    if post_comment:
        clickup_comment_assignee_id = _comment_assignee_id(task)
        await client.post_comment(
            task_id,
            _comment_text(generated_mode, data, registration),
            assignee_id=clickup_comment_assignee_id,
        )
        clickup_comment_posted = True

    return ClickUpHblGenerationResult(
        task_id=task_id,
        hbl_number=data.shipment.mtm_hbl_no,
        mode_requested=mode,
        mode_generated=generated_mode,
        approval=approval,
        pdf_path=str(pdf_path),
        review_path=str(review_path),
        package_id=registration.package_id if registration else "",
        verification_urls=registration.verification_urls if registration else {},
        pdf_sha256=registration.pdf_sha256 if registration else "",
        canonical_json_sha256=registration.canonical_json_sha256 if registration else "",
        clickup_attachment_uploaded=clickup_attachment_uploaded,
        clickup_output_field_id=clickup_output_field_id,
        clickup_status_updated_to=clickup_status_updated_to,
        clickup_comment_posted=clickup_comment_posted,
        clickup_comment_assignee_id=clickup_comment_assignee_id,
        warnings=warnings,
    )


def evaluate_hbl_approval(task: ClickUpTaskData, app_config: AppConfig) -> ApprovalDecision:
    config = app_config.clickup_fields.get("approval", {})
    approved_values = {_norm(value) for value in config.get("approved_values", [])}
    approval_field = ClickUpClient._find_configured_field(task, config)
    approval_value = ClickUpClient._stringify_field_value(
        approval_field.value if approval_field else task.status
    )
    approved_by = _configured_value(task, config.get("approved_by_field_names", []))
    approved_at = _configured_value(task, config.get("approved_at_field_names", []))

    if _norm(approval_value) not in approved_values:
        return ApprovalDecision(
            approved=False,
            approval_value=approval_value,
            approved_by=approved_by,
            approved_at=approved_at,
            reason="ClickUp approval value is not approved.",
        )
    if config.get("require_approved_by") and not approved_by:
        return ApprovalDecision(
            approved=False,
            approval_value=approval_value,
            approved_by=approved_by,
            approved_at=approved_at,
            reason="Approval requires approved-by field.",
        )
    if config.get("require_approved_at") and not approved_at:
        return ApprovalDecision(
            approved=False,
            approval_value=approval_value,
            approved_by=approved_by,
            approved_at=approved_at,
            reason="Approval requires approved-at field.",
        )
    return ApprovalDecision(
        approved=True,
        approval_value=approval_value,
        approved_by=approved_by,
        approved_at=approved_at,
        reason="ClickUp approval value allows issuance.",
    )


def _data_from_clickup_task(
    task: ClickUpTaskData,
    clickup_values: dict[str, str],
    app_config: AppConfig,
) -> CanonicalHblData:
    canonical = _canonical_json_from_task(task, app_config)
    if canonical:
        canonical.shipment.clickup_task_id = task.id
        return canonical
    return build_review_packet(
        PipelineInput(clickup_task_id=task.id, clickup_values=clickup_values),
        app_config=app_config,
    )


def _canonical_json_from_task(task: ClickUpTaskData, app_config: AppConfig) -> CanonicalHblData | None:
    config = app_config.clickup_fields.get("canonical_hbl_json", {})
    field = ClickUpClient._find_configured_field(task, config)
    raw = ClickUpClient._stringify_field_value(field.value if field else None)
    if not raw:
        raw = _json_block_from_text(task.description)
    if not raw:
        return None
    payload = _extract_json_payload(raw)
    return CanonicalHblData.model_validate_json(payload)


def _extract_json_payload(raw: str) -> str:
    value = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        value = value[start : end + 1]
    json.loads(value)
    return value


def _json_block_from_text(text: str) -> str:
    if not text:
        return ""
    if "{" not in text:
        return ""
    try:
        return _extract_json_payload(text)
    except json.JSONDecodeError:
        return ""


def _enforce_clickup_hbl_number(data: CanonicalHblData, clickup_values: dict[str, str]) -> None:
    clickup_hbl = clickup_values.get("hbl_number", "").strip()
    if clickup_hbl:
        data.shipment.mtm_hbl_no = clickup_hbl


def _select_generation_mode(
    mode: GenerationMode,
    approval: ApprovalDecision,
    data: CanonicalHblData,
) -> Literal["draft", "issue"]:
    if mode == "draft":
        return "draft"
    if mode == "issue":
        if not approval.approved:
            raise ValueError(f"Cannot issue HBL: {approval.reason}")
        if data.qa.hard_errors:
            raise ValueError("Cannot issue HBL while hard QA errors exist.")
        return "issue"
    if approval.approved and not data.qa.hard_errors:
        return "issue"
    return "draft"


def _default_output_dir(runs_dir: Path, task_id: str, data: CanonicalHblData) -> Path:
    suffix = data.shipment.mtm_hbl_no or "hbl"
    return runs_dir / "clickup_hbl_data" / f"{task_id}_{suffix}"


def _require_issuance_config(verification_base_url: str, bucket: str, table: str) -> None:
    missing = [
        name
        for name, value in [
            ("verification_base_url", verification_base_url),
            ("bucket", bucket),
            ("table", table),
        ]
        if not value
    ]
    if missing:
        raise ValueError(f"Missing issuance configuration: {', '.join(missing)}")


def _configured_value(task: ClickUpTaskData, field_names: list[str]) -> str:
    for name in field_names:
        field = task.field_by_name(name)
        if field:
            value = ClickUpClient._stringify_field_value(field.value)
            if value:
                return value
    return ""


def _comment_text(generated_mode: str, data: CanonicalHblData, registration) -> str:
    if generated_mode == "issue" and registration:
        first_url = next(iter(registration.verification_urls.values()), "")
        return (
            f"HBL package issued for {data.shipment.mtm_hbl_no}.\n"
            f"Package ID: {registration.package_id}\n"
            f"Verification: {first_url}"
        )
    return f"Draft HBL generated for {data.shipment.mtm_hbl_no}. Review required before issuance."


def _comment_assignee_id(task: ClickUpTaskData) -> str:
    for assignee in task.assignees:
        if assignee.id:
            return assignee.id
    return ""


def _verification_id_suffix(package_id: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", package_id.replace("pkg_", ""))
    return compact[:8].upper()


def _output_field_id_for_mode(generated_mode: str, app_config: AppConfig) -> str:
    outputs = app_config.clickup_fields.get("hbl_outputs", {})
    if generated_mode == "issue":
        return str(outputs.get("original_pdf", {}).get("field_id", "")).strip()
    return str(outputs.get("draft_pdf", {}).get("field_id", "")).strip()


def _status_after_original_upload(app_config: AppConfig) -> str:
    outputs = app_config.clickup_fields.get("hbl_outputs", {})
    return str(
        outputs.get("original_pdf", {}).get("status_after_upload", "READY FOR ORIGINAL")
    ).strip()


def _attachment_output_field_exists(task: ClickUpTaskData, field_id: str) -> bool:
    field = task.field_by_id(field_id)
    value = field.value if field else None
    if isinstance(value, list):
        return any(isinstance(item, dict) and item.get("id") for item in value)
    return bool(value)


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()
