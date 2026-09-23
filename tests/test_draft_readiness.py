"""Draft requests must fail before PDF creation or external document writes."""
import asyncio
import json
from unittest.mock import Mock

import pytest

from mtm_hbl.clickup_hbl_generator import generate_hbl_from_clickup, complete_clickup_artifact
from mtm_hbl.config import Settings
from mtm_hbl.models.clickup import ClickUpCustomField, ClickUpTaskData
from tests.test_clickup_hbl_generator import FakeClickUpClient, ready_data


TRIGGER = "e51205ba-ea9d-4755-a3fe-1648770b6671"


def client_for(data=None, ready=True):
    data = ready_data() if data is None else data
    task = ClickUpTaskData(id="task-1", custom_fields=[
        ClickUpCustomField(id=TRIGGER, name="Ready For Draft", value=ready),
        ClickUpCustomField(id="canonical", name="Canonical HBL JSON", value=data.model_dump_json()),
    ])
    return FakeClickUpClient(task, {"hbl_number": "WH26040006"})


def run(client, tmp_path, app_config, **kwargs):
    return asyncio.run(generate_hbl_from_clickup(
        task_ref="task-1", client=client, settings=Settings(runs_dir=tmp_path),
        app_config=app_config, output_dir=tmp_path / "output", mode=kwargs.pop("mode", "draft"),
        attach_to_clickup=True, post_comment=True, **kwargs,
    ))


@pytest.mark.parametrize("mode", ["draft", "auto"])
@pytest.mark.parametrize("value", [None, False, "false", "", "yes", "checked", 1])
def test_unreviewed_request_never_renders_or_writes(tmp_path, app_config, monkeypatch, mode, value):
    client = client_for(ready=value)
    render = Mock()
    monkeypatch.setattr("mtm_hbl.clickup_hbl_generator.generate_bill_of_lading_draft", render)
    with pytest.raises(ValueError, match="Ready For Draft"):
        run(client, tmp_path, app_config, mode=mode)
    render.assert_not_called()
    assert not client.uploaded and not client.commented
    assert not (tmp_path / "output").exists()


def test_trigger_must_have_the_configured_id(tmp_path, app_config):
    client = client_for()
    client.task.custom_fields[0].id = "wrong-field"
    with pytest.raises(ValueError, match="Ready For Draft"):
        run(client, tmp_path, app_config)
    assert not client.uploaded


@pytest.mark.parametrize("payload", [None, "", "not JSON", '{"charges":{"line_items":[{"prepaid_amount":null}]}}'])
def test_absent_or_invalid_canonical_never_falls_back_to_blank_pdf(tmp_path, app_config, payload):
    client = client_for()
    client.task.custom_fields[1].value = payload
    with pytest.raises(ValueError):
        run(client, tmp_path, app_config)
    assert not client.uploaded and not client.commented
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("field,value", [
    ("shipment.mtm_hbl_no", ""), ("shipment.mbl_no", ""),
    ("shipment.vessel", ""), ("shipment.voyage", ""), ("shipment.freight_term", ""),
    ("shipment.clickup_task_id", "another-task"),
    ("parties.shipper.raw_text", ""), ("parties.consignee.raw_text", ""),
    ("routing.port_of_loading", ""), ("routing.port_of_discharge", ""),
    ("cargo.description_raw", ""), ("cargo.total_packages", "999"),
    ("cargo.gross_weight", ""), ("cargo.measurement", "0"),
    ("containers", []), ("scope.owner_country", ""),
])
def test_schema_valid_but_incomplete_data_blocks_draft(tmp_path, app_config, field, value):
    data = ready_data()
    parent = data
    *parts, leaf = field.split(".")
    for part in parts:
        parent = getattr(parent, part)
    setattr(parent, leaf, value)
    client = client_for(data)
    with pytest.raises(ValueError, match="Draft blocked"):
        run(client, tmp_path, app_config)
    assert not client.uploaded and not client.commented
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("source", ["canonical", "clickup"])
def test_existing_hard_qa_blocks_even_if_required_fields_present(tmp_path, app_config, source):
    client = client_for()
    if source == "canonical":
        raw = json.loads(client.task.custom_fields[1].value)
        raw["qa"] = {"hard_errors": [{"id": "source_conflict", "severity": "hard_error",
            "field": "shipment", "message": "Unresolved source conflict"}]}
        client.task.custom_fields[1].value = json.dumps(raw)
    else:
        client.task.custom_fields.append(ClickUpCustomField(name="QA Hard Errors", value="Source conflict"))
    with pytest.raises(ValueError):
        run(client, tmp_path, app_config)
    assert not client.uploaded


@pytest.mark.parametrize("problem", ["no_lines", "no_amount", "bad_amount", "negative_amount", "no_currency"])
def test_unreviewed_charge_data_blocks(tmp_path, app_config, problem):
    data = ready_data()
    if problem == "no_lines":
        data.charges.line_items = []
    else:
        line = data.charges.line_items[0]
        if problem == "no_currency":
            line.currency = ""
        else:
            line.collect_amount = {"no_amount": "", "bad_amount": "TBD", "negative_amount": "-10"}[problem]
    with pytest.raises(ValueError, match="Draft blocked"):
        run(client_for(data), tmp_path, app_config)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("change", ["withdraw_ready", "edit_data", "corrupt_pdf"])
def test_change_after_render_stops_before_upload(tmp_path, app_config, change):
    client = client_for()
    def checkpoint(phase, artifact):
        if phase == "READY":
            if change == "withdraw_ready":
                client.task.custom_fields[0].value = False
            elif change == "edit_data":
                raw = json.loads(client.task.custom_fields[1].value)
                raw["shipment"]["voyage"] = "NEW"
                client.task.custom_fields[1].value = json.dumps(raw)
            else:
                from pathlib import Path
                Path(artifact["result"]["pdf_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        run(client, tmp_path, app_config, checkpoint=checkpoint)
    assert not client.uploaded and not client.commented


def test_legacy_prepared_draft_cannot_bypass_guard(tmp_path, app_config):
    client = client_for()
    artifact = {"result": {"task_id": "task-1", "mode_generated": "draft", "pdf_path": "legacy.pdf",
        "review_path": "legacy.json"}, "attach": True}
    with pytest.raises(ValueError, match="lacks readiness evidence"):
        asyncio.run(complete_clickup_artifact(client, artifact))
    assert not client.uploaded


@pytest.mark.parametrize("ready", [True, "true"])
def test_reviewed_complete_draft_renders_attaches_and_comments(tmp_path, app_config, ready):
    client = client_for(ready=ready)
    result = run(client, tmp_path, app_config)
    assert result.mode_generated == "draft"
    assert result.hbl_number == "WH26040006"
    assert client.uploaded and client.commented
    assert result.package_id == ""


def test_json_without_audit_timestamp_has_stable_upload_check(tmp_path, app_config):
    client = client_for()
    raw = json.loads(client.task.custom_fields[1].value)
    del raw["audit"]
    client.task.custom_fields[1].value = json.dumps(raw)
    result = run(client, tmp_path, app_config)
    assert result.clickup_attachment_uploaded


def test_auto_original_approval_does_not_bypass_draft_checks_on_qa_fallback(tmp_path, app_config):
    client = client_for(ready=False)
    raw = json.loads(client.task.custom_fields[1].value)
    raw["qa"] = {"hard_errors": [{"id": "conflict", "severity": "hard_error", "message": "Conflict"}]}
    client.task.custom_fields[1].value = json.dumps(raw)
    client.task.custom_fields.extend([
        ClickUpCustomField(name="Approved for Original", value=True),
        ClickUpCustomField(name="Approved By", value="Reviewer"),
        ClickUpCustomField(name="Approved At", value="2026-09-23"),
    ])
    with pytest.raises(ValueError, match="Ready For Draft"):
        run(client, tmp_path, app_config, mode="auto")
    assert not client.uploaded
