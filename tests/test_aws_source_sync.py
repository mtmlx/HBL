"""Behavioral coverage for source recovered from the deployed AWS issuer."""

import asyncio
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from pypdf import PdfReader

from mtm_hbl.clickup_hbl_generator import generate_hbl_from_clickup
from mtm_hbl.config import Settings
from mtm_hbl.models.canonical import CanonicalHblData, ChargeLine
from mtm_hbl.models.clickup import ClickUpCustomField, ClickUpTaskData, ClickUpUser
from mtm_hbl.pdf.hbl_package import generate_bill_of_lading_package
from tests.test_clickup_hbl_generator import FakeClickUpClient
from tests.test_hbl_package_pdf import package_data


def approved_client(app_config, *, existing_original=False, fail_verification=False):
    data = package_data()
    original_id = app_config.clickup_fields['hbl_outputs']['original_pdf']['field_id']
    task = ClickUpTaskData(
        id='task-1',
        assignees=[ClickUpUser(id='12345'), ClickUpUser(id='67890')],
        custom_fields=[
            ClickUpCustomField(id='canonical', name='Canonical HBL JSON', value=data.model_dump_json()),
            ClickUpCustomField(id='approval', name='HBL Approval Status', value='Approved'),
            ClickUpCustomField(id='approved-by', name='HBL Approved By', value='Operator'),
            ClickUpCustomField(id='approved-at', name='HBL Approved At', value='2026-05-26'),
            ClickUpCustomField(id=original_id, name='HBL Original', value=[{'id': 'old.pdf'}] if existing_original else []),
        ],
    )

    class Client(FakeClickUpClient):
        events = None

        async def upload_attachment_to_custom_field(self, task_id, field_id, path):
            self.events.append(('upload', field_id))
            return await super().upload_attachment_to_custom_field(task_id, field_id, path)

        async def verify_attachment_custom_field(self, task_id, field_id, expected_filename):
            self.events.append(('verify', field_id))
            if fail_verification:
                raise ValueError('Attachment readback failed')
            await super().verify_attachment_custom_field(task_id, field_id, expected_filename)

        async def update_task_status(self, task_id, status):
            self.events.append(('status', status))

        async def post_comment(self, task_id, comment_text, *, assignee_id='', notify_all=False):
            self.events.append(('comment', comment_text, assignee_id))

    client = Client(task, {'hbl_number': data.shipment.mtm_hbl_no, 'owner_country': 'Guatemala'})
    client.events = []
    return client


def run_issue(client, tmp_path, app_config):
    return asyncio.run(generate_hbl_from_clickup(
        task_ref='task-1', client=client, settings=Settings(runs_dir=tmp_path),
        app_config=app_config, mode='issue', output_dir=tmp_path,
        attach_to_clickup=True, post_comment=True, prevent_original_overwrite=True,
        bucket='test-bucket', table='test-table', verification_base_url='https://verify.example.com',
    ))


def registration_stub(monkeypatch):
    register = Mock(return_value=SimpleNamespace(
        package_id='pkg_test', pdf_sha256='pdfhash', canonical_json_sha256='jsonhash',
        verification_urls={'WH26040006-O1': 'https://verify.example.com/verify/WH26040006-O1'},
    ))
    monkeypatch.setattr('mtm_hbl.clickup_hbl_generator.register_issued_package', register)
    return register


def test_existing_original_blocks_before_render_registration_or_clickup(monkeypatch, tmp_path, app_config):
    register = registration_stub(monkeypatch)
    client = approved_client(app_config, existing_original=True)
    with pytest.raises(ValueError, match='controlled reissue/void flow'):
        run_issue(client, tmp_path, app_config)
    register.assert_not_called()
    assert client.events == []
    assert not list(tmp_path.glob('*.pdf'))


def test_original_completion_orders_upload_readback_status_and_assigned_comment(monkeypatch, tmp_path, app_config):
    registration_stub(monkeypatch)
    client = approved_client(app_config)
    result = run_issue(client, tmp_path, app_config)
    assert [event[0] for event in client.events] == ['upload', 'verify', 'status', 'comment']
    assert client.events[2] == ('status', 'READY FOR ORIGINAL')
    assert client.events[3] == (
        'comment',
        'HBL package issued for WH26040006.\nPackage ID: pkg_test\n'
        'Verification: https://verify.example.com/verify/WH26040006-O1',
        '12345',
    )
    assert result.clickup_status_updated_to == 'READY FOR ORIGINAL'


def test_failed_attachment_readback_does_not_post_completion(monkeypatch, tmp_path, app_config):
    registration_stub(monkeypatch)
    client = approved_client(app_config, fail_verification=True)
    with pytest.raises(ValueError, match='Attachment readback failed'):
        run_issue(client, tmp_path, app_config)
    assert [event[0] for event in client.events] == ['upload', 'verify']


def test_numeric_aliases_and_charge_visibility_survive_canonical_roundtrip():
    data = CanonicalHblData.model_validate({
        'cargo': {'cbm': 12.5},
        'containers': [{'container_number': 'ABCD1234567', 'seal': 12345, 'volume': 12.5}],
        'charges': {'line_items': [{'description': 'Internal cost', 'show_on_hbl': False, 'include_in_total': False}]},
    })
    restored = CanonicalHblData.model_validate_json(data.model_dump_json())
    assert restored.cargo.measurement == '12.5'
    assert restored.containers[0].container_no == 'ABCD1234567'
    assert restored.containers[0].seal_no == '12345'
    assert restored.containers[0].measurement == '12.5'
    assert not restored.charges.line_items[0].show_on_hbl
    assert not restored.charges.line_items[0].include_in_total


def test_freight_continuation_preserves_all_visible_charges_and_terms(tmp_path):
    data = package_data()
    data.charges.line_items = [ChargeLine(description=f'Visible charge {i}', collect_amount='10.00') for i in range(5)]
    data.charges.line_items.append(ChargeLine(description='Hidden internal cost', collect_amount='999.00', show_on_hbl=False))
    path = tmp_path / 'freight-continuation.pdf'
    generate_bill_of_lading_package(data, path)
    pages = PdfReader(path).pages
    assert len(pages) == 12
    first, second = pages[0].extract_text(), pages[1].extract_text()
    assert 'Visible charge 0' in first and 'Visible charge 4' in second
    assert 'Hidden internal cost' not in first + second
    assert '50.00' in second
    assert all('TERMS & CONDITIONS / DOCUMENT VALIDATION' in page.extract_text() for page in pages)


@pytest.mark.parametrize('replacement_fails', [False, True])
def test_manager_flow_voids_only_after_replacement_and_then_posts_audit(monkeypatch, replacement_fails):
    # Import deployed handlers without accessing credentials or AWS services.
    monkeypatch.setattr(boto3, 'client', Mock())
    monkeypatch.setattr(boto3, 'resource', Mock())
    admin = importlib.import_module('mtm_hbl.aws_handlers.hbl_admin')
    calls = []
    record = admin.ActiveVerificationRecord('old-O1', 'pkg_old', 'ISSUED')

    async def preview(task):
        return {'hbl_number': 'TEST-HBL', 'task_id': 'task-1', 'active_records': [record]}

    async def issue(task, user):
        calls.append('issue')
        if replacement_fails:
            raise ValueError('Replacement upload failed')
        return SimpleNamespace(hbl_number='TEST-HBL', package_id='pkg_new', verification_urls={'new-O1': 'https://verify.example.com/new-O1'})

    def void(config, ids, *, superseded_by, reason):
        assert ids == ['old-O1'] and superseded_by == 'pkg_new'
        calls.append('void')

    async def audit(task, user, records, result):
        assert records == [record] and result.package_id == 'pkg_new'
        calls.append('audit')

    monkeypatch.setattr(admin, '_form_data', lambda event: {'task_ref': 'task-1', 'expected_hbl_number': 'TEST-HBL', 'confirmation': 'REISSUE TEST-HBL'})
    monkeypatch.setattr(admin, '_load_reissue_preview', preview)
    monkeypatch.setattr(admin, '_issue_replacement', issue)
    monkeypatch.setattr(admin, '_verification_config', lambda: None)
    monkeypatch.setattr(admin, 'void_verification_records', void)
    monkeypatch.setattr(admin, '_post_reissue_comment', audit)
    operation = admin._confirm_reissue({}, admin.AdminUser('manager@example.com'))
    if replacement_fails:
        with pytest.raises(ValueError, match='Replacement upload failed'):
            asyncio.run(operation)
        assert calls == ['issue']
    else:
        response = asyncio.run(operation)
        assert response['statusCode'] == 200
        assert calls == ['issue', 'void', 'audit']


def test_verification_bundle_contains_all_terms_and_displays_registered_hash(monkeypatch):
    monkeypatch.setenv('TABLE_NAME', 'test-table')
    monkeypatch.setattr(boto3, 'resource', Mock())
    path = Path(__file__).resolve().parents[1] / 'aws/verification-service/src/app.py'
    spec = importlib.util.spec_from_file_location('verification_sync_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for language in module.TERMS_LANGUAGES:
        terms = module.load_terms(language)
        assert terms['content']
        assert (module.TERMS_DIR / f'mtm-logix-terms-v3.0-{language}.md').is_file()
    html = module.render_html('TEST-O1', {
        'status': 'ISSUED', 'hbl_number': 'TEST-HBL', 'package_id': 'pkg_test',
        'pdf_sha256': 'a' * 64, 'terms_version': '3.0',
    })
    assert 'a' * 64 in html and 'pkg_test' in html and 'ISSUED' in html
