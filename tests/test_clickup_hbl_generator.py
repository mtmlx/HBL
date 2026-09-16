import asyncio

from mtm_hbl.clickup_hbl_generator import (
    evaluate_hbl_approval,
    generate_hbl_from_clickup,
    parse_clickup_task_id,
)
from mtm_hbl.config import AppConfig, Settings
from mtm_hbl.models.clickup import ClickUpCustomField, ClickUpTaskData, ClickUpUser
from tests.test_hbl_package_pdf import package_data


class FakeClickUpClient:
    def __init__(self, task: ClickUpTaskData, values: dict[str, str]) -> None:
        self.task = task
        self.values = values
        self.uploaded = False
        self.output_field_id = ""
        self.commented = False
        self.comment_assignee_id = ""

    async def get_task(self, task_id: str) -> ClickUpTaskData:
        assert task_id == self.task.id
        return self.task

    def extract_configured_fields(self, task: ClickUpTaskData, app_config: AppConfig) -> dict[str, str]:
        return self.values

    async def upload_attachment(self, task_id: str, path: str):
        self.uploaded = True
        return {}

    async def upload_attachment_to_custom_field(self, task_id: str, field_id: str, path: str):
        self.uploaded = True
        self.output_field_id = field_id
        return {"id": "attachment.pdf"}

    async def verify_attachment_custom_field(self, task_id: str, field_id: str, expected_filename: str):
        assert field_id == self.output_field_id

    async def post_comment(
        self,
        task_id: str,
        comment_text: str,
        *,
        assignee_id: str = "",
        notify_all: bool = False,
    ):
        self.commented = True
        self.comment_assignee_id = assignee_id
        return {}


def test_parse_clickup_task_id_handles_workspace_and_custom_domain_urls():
    assert parse_clickup_task_id("https://app.clickup.com/t/8451352/MTMLXGT-25972") == "MTMLXGT-25972"
    assert parse_clickup_task_id("https://mtmlx.clickup.com/t/86e1hdk49") == "86e1hdk49"
    assert parse_clickup_task_id("86e1hdk49") == "86e1hdk49"


def test_evaluate_hbl_approval_uses_configured_field(app_config):
    task = ClickUpTaskData(
        id="task-1",
        custom_fields=[
            ClickUpCustomField(id="approval", name="HBL Approval Status", value="Ready to Issue"),
            ClickUpCustomField(id="approved-by", name="HBL Approved By", value="Operator"),
            ClickUpCustomField(id="approved-at", name="HBL Approved At", value="2026-05-26"),
        ],
    )

    decision = evaluate_hbl_approval(task, app_config)

    assert decision.approved is True
    assert decision.approval_value == "Ready to Issue"


def test_evaluate_hbl_approval_accepts_approved_for_original_boolean(app_config):
    task = ClickUpTaskData(
        id="task-1",
        custom_fields=[
            ClickUpCustomField(id="approval", name="Approved for Original", value=True),
            ClickUpCustomField(id="approved-by", name="Approved By", value="Operator"),
            ClickUpCustomField(id="approved-at", name="Approved At", value="2026-06-03"),
        ],
    )

    decision = evaluate_hbl_approval(task, app_config)

    assert decision.approved is True
    assert decision.approval_value == "True"


def test_generate_from_clickup_creates_one_page_draft_when_not_approved(tmp_path, app_config):
    data = package_data()
    task = ClickUpTaskData(
        id="task-1",
        custom_fields=[
            ClickUpCustomField(
                id="canonical",
                name="Canonical HBL JSON",
                value=data.model_dump_json(),
            ),
            ClickUpCustomField(id="approval", name="HBL Approval Status", value="Pending Approval"),
        ],
    )
    client = FakeClickUpClient(
        task,
        {"hbl_number": "WH26040006", "owner_country": "Guatemala"},
    )

    result = asyncio.run(
        generate_hbl_from_clickup(
            task_ref="https://mtmlx.clickup.com/t/task-1",
            client=client,
            settings=Settings(runs_dir=tmp_path),
            app_config=app_config,
            mode="auto",
            output_dir=tmp_path,
        )
    )

    assert result.mode_generated == "draft"
    assert result.verification_urls == {}
    assert result.pdf_path.endswith("Draft_WH26040006_v1.pdf")


def test_generate_from_clickup_issues_when_approved(monkeypatch, tmp_path, app_config):
    data = package_data()
    task = ClickUpTaskData(
        id="task-1",
        custom_fields=[
            ClickUpCustomField(
                id="canonical",
                name="Canonical HBL JSON",
                value=data.model_dump_json(),
            ),
            ClickUpCustomField(id="approval", name="HBL Approval Status", value="Approved"),
            ClickUpCustomField(id="approved-by", name="HBL Approved By", value="Operator"),
            ClickUpCustomField(id="approved-at", name="HBL Approved At", value="2026-05-26"),
        ],
    )
    client = FakeClickUpClient(
        task,
        {"hbl_number": "WH26040006", "owner_country": "Guatemala"},
    )

    class FakeRegistration:
        pdf_s3_key = "issued/test.pdf"
        package_id = "pkg_test"
        verification_urls = {"WH26040006-O1": "https://verify.example.com/verify/WH26040006-O1"}
        pdf_sha256 = "pdfhash"
        canonical_json_sha256 = "jsonhash"

    monkeypatch.setattr(
        "mtm_hbl.clickup_hbl_generator.register_issued_package",
        lambda *args, **kwargs: FakeRegistration(),
    )

    result = asyncio.run(
        generate_hbl_from_clickup(
            task_ref="task-1",
            client=client,
            settings=Settings(runs_dir=tmp_path),
            app_config=app_config,
            mode="auto",
            output_dir=tmp_path,
            verification_base_url="https://verify.example.com",
            bucket="bucket",
            table="table",
        )
    )

    assert result.mode_generated == "issue"
    assert result.package_id == "pkg_test"
    assert result.verification_urls["WH26040006-O1"].endswith("/WH26040006-O1")


def test_clickup_attachment_uses_draft_output_field(tmp_path, app_config):
    data = package_data()
    task = ClickUpTaskData(
        id="task-1",
        custom_fields=[
            ClickUpCustomField(
                id="canonical",
                name="Canonical HBL JSON",
                value=data.model_dump_json(),
            ),
            ClickUpCustomField(id="approval", name="HBL Approval Status", value="Pending Approval"),
        ],
    )
    client = FakeClickUpClient(
        task,
        {"hbl_number": "WH26040006", "owner_country": "Guatemala"},
    )

    result = asyncio.run(
        generate_hbl_from_clickup(
            task_ref="task-1",
            client=client,
            settings=Settings(runs_dir=tmp_path),
            app_config=app_config,
            mode="auto",
            output_dir=tmp_path,
            attach_to_clickup=True,
        )
    )

    assert result.clickup_attachment_uploaded is True
    assert result.clickup_output_field_id == "85b0aff3-ccc5-4f90-b625-ed55592e07b7"
    assert client.output_field_id == "85b0aff3-ccc5-4f90-b625-ed55592e07b7"


def test_generated_comment_is_assigned_to_task_assignee(tmp_path, app_config):
    data = package_data()
    task = ClickUpTaskData(
        id="task-1",
        assignees=[ClickUpUser(id="12345", username="Operator")],
        custom_fields=[
            ClickUpCustomField(
                id="canonical",
                name="Canonical HBL JSON",
                value=data.model_dump_json(),
            ),
            ClickUpCustomField(id="approval", name="HBL Approval Status", value="Pending Approval"),
        ],
    )
    client = FakeClickUpClient(
        task,
        {"hbl_number": "WH26040006", "owner_country": "Guatemala"},
    )

    result = asyncio.run(
        generate_hbl_from_clickup(
            task_ref="task-1",
            client=client,
            settings=Settings(runs_dir=tmp_path),
            app_config=app_config,
            mode="auto",
            output_dir=tmp_path,
            post_comment=True,
        )
    )

    assert result.clickup_comment_posted is True
    assert result.clickup_comment_assignee_id == "12345"
    assert client.comment_assignee_id == "12345"
