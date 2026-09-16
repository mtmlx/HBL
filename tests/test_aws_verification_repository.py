from pathlib import Path
from types import SimpleNamespace

import boto3

from mtm_hbl.verification.aws_repository import (
    AwsVerificationConfig,
    register_issued_package,
    void_verification_records,
)
from tests.test_hbl_package_pdf import package_data
from mtm_hbl.pdf.hbl_package import generate_bill_of_lading_package


class FakeS3Client:
    def __init__(self) -> None:
        self.uploads = []
        self.objects = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploads.append(
            {
                "filename": filename,
                "bucket": bucket,
                "key": key,
                "extra_args": ExtraArgs or {},
            }
        )

    def put_object(self, **kwargs):
        self.objects.append(kwargs)


class FakeTable:
    def __init__(self) -> None:
        self.items = []
        self.updates = []

    def put_item(self, Item):
        self.items.append(Item)

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


class FakeDynamoResource:
    def __init__(self, table: FakeTable) -> None:
        self.table = table
        self.meta = SimpleNamespace(client=self)

    def transact_write_items(self, TransactItems):
        for write in TransactItems:
            assert write["Put"]["ConditionExpression"] == "attribute_not_exists(verification_id)"
            self.table.items.append(write["Put"]["Item"])

    def Table(self, _name):
        return self.table


def test_register_issued_package_uploads_encrypted_private_artifacts(monkeypatch, tmp_path):
    data = package_data()
    pdf_path = tmp_path / "HBL_Package_WH26040006.pdf"
    generate_bill_of_lading_package(data, pdf_path, verification_base_url="https://verify.example.com/")

    fake_s3 = FakeS3Client()
    fake_table = FakeTable()

    monkeypatch.setattr(boto3, "client", lambda service, region_name=None: fake_s3)
    monkeypatch.setattr(
        boto3,
        "resource",
        lambda service, region_name=None: FakeDynamoResource(fake_table),
    )

    registration = register_issued_package(
        data,
        pdf_path,
        AwsVerificationConfig(
            bucket_name="test-bucket",
            table_name="test-table",
            region_name="us-east-1",
            verification_base_url="https://verify.example.com/",
        ),
        package_id="pkg_test",
        status="issued",
    )

    assert len(fake_s3.objects) == 2
    assert fake_s3.objects[0]["ContentType"] == "application/pdf"
    assert all(o["ServerSideEncryption"] == "AES256" and o["IfNoneMatch"] == "*" for o in fake_s3.objects)
    assert len(fake_table.items) == 6
    assert fake_table.items[0]["status"] == "ISSUED"
    assert fake_table.items[0]["terms_version"] == "3.0"
    assert fake_table.items[0]["terms_effective_date"] == "2026-06-11"
    assert fake_table.items[0]["terms_effective_date_display"] == "11-JUN-2026"
    assert fake_table.items[0]["verification_url"] == "https://verify.example.com/verify/WH26040006-O1"
    assert registration.verification_urls["WH26040006-O1"] == (
        "https://verify.example.com/verify/WH26040006-O1"
    )
    assert registration.pdf_sha256
    assert registration.canonical_json_sha256


def test_register_issued_package_can_use_unique_verification_suffix(monkeypatch, tmp_path):
    data = package_data()
    pdf_path = tmp_path / "HBL_Package_WH26040006.pdf"
    generate_bill_of_lading_package(
        data,
        pdf_path,
        verification_base_url="https://verify.example.com/",
        verification_id_suffix="ABCD1234",
    )
    fake_s3 = FakeS3Client()
    fake_table = FakeTable()
    monkeypatch.setattr(boto3, "client", lambda service, region_name=None: fake_s3)
    monkeypatch.setattr(
        boto3,
        "resource",
        lambda service, region_name=None: FakeDynamoResource(fake_table),
    )

    registration = register_issued_package(
        data,
        pdf_path,
        AwsVerificationConfig("test-bucket", "test-table", verification_base_url="https://verify.example.com/"),
        package_id="pkg_test",
        verification_id_suffix="ABCD1234",
    )

    assert "WH26040006-O1-ABCD1234" in registration.verification_urls
    assert fake_table.items[0]["verification_id"] == "WH26040006-O1-ABCD1234"
    assert fake_table.items[0]["verification_id_suffix"] == "ABCD1234"


def test_void_verification_records_marks_records_void(monkeypatch):
    fake_table = FakeTable()
    monkeypatch.setattr(
        boto3,
        "resource",
        lambda service, region_name=None: FakeDynamoResource(fake_table),
    )

    void_verification_records(
        AwsVerificationConfig("test-bucket", "test-table"),
        ["WH26040006-O1"],
        superseded_by="pkg_new",
    )

    assert len(fake_table.updates) == 1
    update = fake_table.updates[0]
    assert update["Key"] == {"verification_id": "WH26040006-O1"}
    assert update["ExpressionAttributeValues"][":status"] == "VOID"
    assert update["ExpressionAttributeValues"][":superseded_by"] == "pkg_new"
