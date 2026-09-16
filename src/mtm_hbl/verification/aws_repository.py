from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import boto3

from mtm_hbl.models.canonical import CanonicalHblData
from mtm_hbl.pdf.hbl_package import (
    build_document_page_set,
    validate_bill_of_lading_package,
    verification_id_for_page,
    verification_url_for_page,
)

TERMS_VERSION = "3.0"
TERMS_EFFECTIVE_DATE = "2026-06-11"
TERMS_EFFECTIVE_DATE_DISPLAY = "11-JUN-2026"


@dataclass(frozen=True)
class AwsVerificationConfig:
    bucket_name: str
    table_name: str
    region_name: str = "us-east-1"
    verification_base_url: str = ""


@dataclass(frozen=True)
class IssuedPackageRegistration:
    package_id: str
    pdf_s3_key: str
    canonical_json_s3_key: str
    pdf_sha256: str
    canonical_json_sha256: str
    verification_urls: dict[str, str]


def register_issued_package(
    data: CanonicalHblData,
    pdf_path: Path,
    config: AwsVerificationConfig,
    *,
    status: str = "ISSUED",
    package_id: str | None = None,
    verification_id_suffix: str = "",
    issued_by: str = "Andrea Piedad Velasquez Castellon",
) -> IssuedPackageRegistration:
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF package not found: {pdf_path}")
    if not data.shipment.mtm_hbl_no:
        raise ValueError("HBL number is required to register a verification package.")
    validate_bill_of_lading_package(pdf_path)

    package_id = package_id or f"pkg_{uuid4().hex}"
    issued_at = datetime.now(timezone.utc).isoformat()
    year = _issue_year(data) or datetime.now(timezone.utc).strftime("%Y")
    base_key = f"issued/{year}/{data.shipment.mtm_hbl_no}/{package_id}"
    pdf_s3_key = f"{base_key}/{pdf_path.name}"
    canonical_json_s3_key = f"{base_key}/canonical.json"

    canonical_json = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, indent=2).encode(
        "utf-8"
    )
    pdf_digest = sha256(pdf_path.read_bytes()).hexdigest()
    canonical_digest = sha256(canonical_json).hexdigest()

    s3 = boto3.client("s3", region_name=config.region_name)
    dynamodb = boto3.resource("dynamodb", region_name=config.region_name)
    table = dynamodb.Table(config.table_name)

    s3.upload_file(
        str(pdf_path),
        config.bucket_name,
        pdf_s3_key,
        ExtraArgs={
            "ServerSideEncryption": "AES256",
            "ContentType": "application/pdf",
            "Metadata": {
                "hbl-number": data.shipment.mtm_hbl_no,
                "package-id": package_id,
                "sha256": pdf_digest,
            },
        },
    )
    s3.put_object(
        Bucket=config.bucket_name,
        Key=canonical_json_s3_key,
        Body=canonical_json,
        ServerSideEncryption="AES256",
        ContentType="application/json",
        Metadata={
            "hbl-number": data.shipment.mtm_hbl_no,
            "package-id": package_id,
            "sha256": canonical_digest,
        },
    )

    verification_urls: dict[str, str] = {}
    normalized_status = status.upper()
    for page_config in build_document_page_set(data):
        verification_id = verification_id_for_page(data, page_config, suffix=verification_id_suffix)
        verification_url = verification_url_for_page(
            data,
            page_config,
            config.verification_base_url,
            suffix=verification_id_suffix,
        )
        verification_urls[verification_id] = verification_url
        table.put_item(
            Item={
                "verification_id": verification_id,
                "verification_url": verification_url,
                "package_id": package_id,
                "hbl_number": data.shipment.mtm_hbl_no,
                "mbl_number": data.shipment.mbl_no,
                "document_type": page_config.type,
                "sequence": page_config.sequence,
                "sequence_total": page_config.total,
                "page_number": page_config.page_number,
                "page_total": page_config.page_total,
                "status": normalized_status,
                "verification_id_suffix": verification_id_suffix,
                "pdf_s3_key": pdf_s3_key,
                "canonical_json_s3_key": canonical_json_s3_key,
                "pdf_sha256": pdf_digest,
                "canonical_json_sha256": canonical_digest,
                "issued_at": issued_at,
                "issued_by": issued_by,
                "terms_version": TERMS_VERSION,
                "terms_effective_date": TERMS_EFFECTIVE_DATE,
                "terms_effective_date_display": TERMS_EFFECTIVE_DATE_DISPLAY,
                "clickup_task_id": data.shipment.clickup_task_id,
                "voided_at": "",
                "superseded_by": "",
            }
        )

    return IssuedPackageRegistration(
        package_id=package_id,
        pdf_s3_key=pdf_s3_key,
        canonical_json_s3_key=canonical_json_s3_key,
        pdf_sha256=pdf_digest,
        canonical_json_sha256=canonical_digest,
        verification_urls=verification_urls,
    )


def void_verification_records(
    config: AwsVerificationConfig,
    verification_ids: list[str],
    *,
    superseded_by: str = "",
    reason: str = "Replacement original issued.",
) -> None:
    dynamodb = boto3.resource("dynamodb", region_name=config.region_name)
    table = dynamodb.Table(config.table_name)
    voided_at = datetime.now(timezone.utc).isoformat()
    for verification_id in verification_ids:
        table.update_item(
            Key={"verification_id": verification_id},
            UpdateExpression=(
                "SET #status = :status, voided_at = :voided_at, "
                "superseded_by = :superseded_by, void_reason = :reason"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "VOID",
                ":voided_at": voided_at,
                ":superseded_by": superseded_by,
                ":reason": reason,
            },
        )


def _issue_year(data: CanonicalHblData) -> str:
    for token in str(data.shipment.issue_date).split():
        if token.isdigit() and len(token) == 4:
            return token
    return ""
