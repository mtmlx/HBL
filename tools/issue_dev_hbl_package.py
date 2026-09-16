from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from mtm_hbl.clickup_hbl_generator import _verification_id_suffix
from mtm_hbl.models.canonical import CanonicalHblData
from mtm_hbl.pdf.hbl_package import generate_bill_of_lading_package
from mtm_hbl.verification.aws_repository import AwsVerificationConfig, register_issued_package


DEFAULT_API_BASE_URL = ""
DEFAULT_BUCKET = ""
DEFAULT_TABLE = ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and register a dev HBL verification package.")
    parser.add_argument("--review-json", required=True)
    parser.add_argument("--output-pdf", required=True)
    parser.add_argument("--logo-path", default="assets/mtm_logix_logo.png")
    parser.add_argument("--api-base-url", default=os.getenv("HBL_VERIFICATION_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--bucket", default=os.getenv("HBL_VERIFICATION_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--table", default=os.getenv("HBL_VERIFICATION_TABLE", DEFAULT_TABLE))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--status", default="ISSUED")
    parser.add_argument("--issued-by", default="Andrea Piedad Velasquez Castellon")
    parser.add_argument("--package-id", default=None)
    args = parser.parse_args()

    missing = [
        name
        for name, value in {
            "HBL_VERIFICATION_BASE_URL or --api-base-url": args.api_base_url,
            "HBL_VERIFICATION_BUCKET or --bucket": args.bucket,
            "HBL_VERIFICATION_TABLE or --table": args.table,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit("Missing required verification configuration: " + ", ".join(missing))

    review_path = Path(args.review_json)
    output_pdf = Path(args.output_pdf)
    data = CanonicalHblData.model_validate_json(review_path.read_text(encoding="utf-8"))
    package_id = args.package_id or f"pkg_{uuid4().hex}"
    verification_id_suffix = _verification_id_suffix(package_id)

    generate_bill_of_lading_package(
        data,
        output_pdf,
        logo_path=Path(args.logo_path),
        draft=False,
        verification_base_url=args.api_base_url,
        verification_id_suffix=verification_id_suffix,
    )
    registration = register_issued_package(
        data,
        output_pdf,
        AwsVerificationConfig(
            bucket_name=args.bucket,
            table_name=args.table,
            region_name=args.region,
            verification_base_url=args.api_base_url,
            allowed_pdf_root=output_pdf.resolve().parent,
        ),
        status=args.status,
        package_id=package_id,
        verification_id_suffix=verification_id_suffix,
        issued_by=args.issued_by,
    )

    summary_path = output_pdf.with_suffix(".verification.json")
    summary_path.write_text(
        json.dumps(registration.__dict__, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(registration.__dict__, indent=2, ensure_ascii=False))
    first_url = next(iter(registration.verification_urls.values()), "")
    if first_url:
        print(f"First verification URL: {first_url}")
    print(f"Summary written to {summary_path}")


if __name__ == "__main__":
    main()
