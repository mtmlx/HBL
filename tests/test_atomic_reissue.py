import asyncio
import importlib
from hashlib import sha256
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from mtm_hbl.config import Settings
from mtm_hbl.aws_handlers.job_journal import JobJournal, ReconciliationRequired
from mtm_hbl.verification.aws_repository import activate_replacement, AwsVerificationConfig
from tests.test_aws_source_sync import approved_client


@pytest.fixture
def environment(monkeypatch, app_config):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('HBL_ISSUER_JOBS_TABLE', 'jobs')
    with mock_aws():
        db = boto3.resource('dynamodb', region_name='us-east-1')
        for name, key in [('jobs', 'job_id'), ('verification', 'verification_id')]:
            db.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}])
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='atomic-reissue-test')
        admin = importlib.import_module('mtm_hbl.aws_handlers.hbl_admin')
        monkeypatch.setattr(admin, 'dynamodb', db)
        monkeypatch.setattr(admin, '_session_secret', lambda: 'unit-test-secret')
        client = approved_client(app_config, existing_original=True)
        hbl = client.values['hbl_number']
        data = admin._data_from_clickup_task(client.task, client.values, app_config)
        admin._enforce_clickup_hbl_number(data, client.values)
        data_hash = admin.canonical_fingerprint(data)
        old = [admin.ActiveVerificationRecord('old-' + str(i), 'pkg_old', 'ISSUED') for i in range(6)]
        table = db.Table('verification')
        for r in old:
            table.put_item(Item={'verification_id': r.verification_id, 'package_id': r.package_id,
                                'hbl_number': hbl, 'status': 'ISSUED'})
        plan = {'task_id': 'task-1', 'hbl_number': hbl, 'old': [[r.verification_id, r.package_id] for r in old],
                'data_hash': data_hash, 'reason': 'customer mistake', 'email': 'test@example.com',
                'nonce': 'unique', 'exp': admin._unix_now() + 600}
        token = admin._sign_token(plan, 'unit-test-secret')
        form = {'task_ref': 'task-1', 'expected_hbl_number': hbl, 'confirmation': 'REISSUE ' + hbl,
                'operation_token': token}
        async def preview(_):
            active = [r for r in table.scan()['Items'] if r['status'] == 'ISSUED']
            return {'task_id': 'task-1', 'hbl_number': hbl, 'data_hash': data_hash,
                    'active_records': [admin.ActiveVerificationRecord(r['verification_id'], r['package_id'], r['status']) for r in active]}
        monkeypatch.setattr(admin, '_form_data', lambda _: form)
        monkeypatch.setattr(admin, '_load_reissue_preview', preview)
        settings = Settings(hbl_verification_bucket='atomic-reissue-test', hbl_verification_table='verification',
                            hbl_verification_base_url='https://test.invalid', aws_region='us-east-1')
        monkeypatch.setattr(admin, '_lambda_settings', lambda: settings)
        monkeypatch.setattr(admin, '_clickup_access_token', lambda: 'test')
        monkeypatch.setattr(admin, 'ClickUpClient', lambda *a: client)
        monkeypatch.setattr(admin, '_verification_config', lambda: AwsVerificationConfig('atomic-reissue-test', 'verification'))
        yield SimpleNamespace(admin=admin, db=db, table=table, client=client, plan=plan, token=token,
                              form=form, hbl=hbl, old=old, s3=s3)


def test_manager_prepares_switches_atomically_completes_and_replay_is_noop(environment):
    e = environment
    response = asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    assert response['statusCode'] == 200
    rows = e.table.scan()['Items']
    assert len(rows) == 12
    new = [r for r in rows if r['status'] == 'ISSUED']
    assert len(new) == 6 and len({r['package_id'] for r in new}) == 1
    assert all(r['void_reason'] == 'customer mistake' for r in rows if r['status'] == 'VOID')
    assert len([x for x in e.client.events if x[0] == 'comment']) == 2
    before = list(e.client.events)
    asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    assert before == e.client.events
    assert len(e.table.scan()['Items']) == 12


@pytest.mark.parametrize('change', ['token', 'identity', 'stale', 'expired'])
def test_invalid_confirmations_make_no_new_package(environment, change):
    e = environment
    email = 'test@example.com'
    if change == 'token':
        e.form['operation_token'] += 'tampered'
    elif change == 'identity':
        email = 'someoneelse@example.com'
    elif change == 'expired':
        e.form['operation_token'] = e.admin._sign_token({**e.plan, 'exp': e.admin._unix_now() - 1}, 'unit-test-secret')
    else:
        e.table.update_item(Key={'verification_id': e.old[0].verification_id},
                            UpdateExpression='SET #s = :v', ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':v': 'VOID'})
    with pytest.raises(ValueError):
        asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser(email)))
    assert not e.client.events
    assert len(e.table.scan()['Items']) == 6


def test_failure_after_activation_keeps_one_active_package_and_blocks_replay(environment):
    e = environment
    async def fail(*a, **kw):
        raise TimeoutError('ClickUp upload unavailable')
    e.client.upload_attachment_to_custom_field = fail
    with pytest.raises(TimeoutError):
        asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    rows = e.table.scan()['Items']
    assert len(rows) == 12
    assert len([r for r in rows if r['status'] == 'ISSUED']) == 6
    with pytest.raises(ReconciliationRequired):
        asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    assert len(e.table.scan()['Items']) == 12


def test_transition_conflict_rolls_back_every_record(environment):
    e = environment
    new_ids = {f'new-{i}': 'https://test.invalid' for i in range(6)}
    for vid in new_ids:
        e.table.put_item(Item={'verification_id': vid, 'status': 'PREPARED', 'package_id': 'pkg_new',
                              'hbl_number': e.hbl, 'pdf_sha256': 'digest'})
    e.table.update_item(Key={'verification_id': e.old[-1].verification_id}, UpdateExpression='SET #s = :v',
                       ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':v': 'VOID'})
    result = SimpleNamespace(verification_urls=new_ids, hbl_number=e.hbl, package_id='pkg_new', pdf_sha256='digest')
    with pytest.raises(ClientError):
        activate_replacement(AwsVerificationConfig('atomic-reissue-test', 'verification'), e.old, result, reason='test')
    rows = e.table.scan()['Items']
    assert len([r for r in rows if r['status'] == 'PREPARED']) == 6
    assert len([r for r in rows if r['status'] == 'ISSUED']) == 5


def test_worker_and_manager_share_operation_lock(environment):
    e = environment
    jobs = e.db.Table('jobs')
    journal = JobJournal(jobs, 'original#task-1', 'task-1', 'issue')
    with pytest.raises(ReconciliationRequired):
        asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    assert not e.client.events


def test_completed_checkpoint_recovers_without_replaying_manager(environment, monkeypatch):
    e = environment
    class Crash(BaseException):
        pass
    original = JobJournal.save
    def crash(self, phase, artifact):
        original(self, phase, artifact)
        if phase == 'COMPLETE':
            raise Crash()
    monkeypatch.setattr(JobJournal, 'save', crash)
    with pytest.raises(Crash):
        asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    monkeypatch.setattr(JobJournal, 'save', original)
    e.db.Table('jobs').update_item(Key={'job_id': 'original#task-1'}, UpdateExpression='SET lease_until = :n',
                                 ExpressionAttributeValues={':n': 1})
    before = list(e.client.events)
    asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    assert e.client.events == before
    assert len(e.table.scan()['Items']) == 12


def test_changed_data_between_confirmation_and_render_is_blocked(environment):
    e = environment
    field = e.client.task.field_by_name('Canonical HBL JSON')
    import json
    data = json.loads(field.value)
    data['shipment']['voyage'] = 'CHANGED'
    field.value = json.dumps(data)
    with pytest.raises(ValueError, match='changed after preview'):
        asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    assert len(e.table.scan()['Items']) == 6
    assert not e.client.events


def test_existing_package_object_cannot_be_overwritten(environment):
    e = environment
    asyncio.run(e.admin._confirm_reissue({}, e.admin.AdminUser('test@example.com')))
    import json
    from pathlib import Path
    from mtm_hbl.verification.aws_repository import register_issued_package
    saved = json.loads(e.db.Table('jobs').get_item(Key={'job_id': 'original#task-1'})['Item']['artifact_json'])['result']
    pdf_before = e.s3.get_object(Bucket='atomic-reissue-test', Key=saved['pdf_s3_key'])['Body'].read()
    data = e.admin._data_from_clickup_task(e.client.task, e.client.values, e.admin.AppConfig(Path('config')))
    with pytest.raises(ClientError) as error:
        register_issued_package(data, Path(saved['pdf_path']), AwsVerificationConfig('atomic-reissue-test', 'verification'),
                                package_id=saved['package_id'])
    assert error.value.response['Error']['Code'] == 'PreconditionFailed'
    assert e.s3.get_object(Bucket='atomic-reissue-test', Key=saved['pdf_s3_key'])['Body'].read() == pdf_before
    assert len(e.table.scan()['Items']) == 12
