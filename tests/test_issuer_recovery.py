import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from mtm_hbl.aws_handlers.job_journal import JobJournal, JobBusy, ReconciliationRequired
from mtm_hbl.clickup_hbl_generator import complete_clickup_artifact


@pytest.fixture
def jobs():
    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        yield resource.create_table(TableName='jobs', KeySchema=[{'AttributeName': 'job_id', 'KeyType': 'HASH'}],
                                    AttributeDefinitions=[{'AttributeName': 'job_id', 'AttributeType': 'S'}],
                                    BillingMode='PAY_PER_REQUEST')


def acquire(jobs):
    return JobJournal(jobs, 'original#task', 'task', 'issue')


def expire(jobs):
    jobs.update_item(Key={'job_id': 'original#task'}, UpdateExpression='SET lease_until = :n',
                     ExpressionAttributeValues={':n': 1})


def artifact():
    return {'result': {'task_id': 'task', 'mode_generated': 'issue', 'pdf_path': '/tmp/test.pdf',
                       'review_path': '/tmp/review.json', 'package_id': 'pkg_existing',
                       'pdf_s3_key': 'issued/test.pdf', 'pdf_sha256': sha256(b'pdf').hexdigest(),
                       'clickup_output_field_id': 'field', 'clickup_comment_assignee_id': '123'},
            'attach': True, 'post_comment': True, 'status': 'ready for original', 'bucket': 'bucket', 'region': 'us-east-1',
            'comment': 'HBL package issued for TEST.\nPackage ID: pkg_existing\nVerification: https://example.test'}


def test_only_one_owner_and_stale_owner_cannot_checkpoint(jobs):
    first = acquire(jobs)
    with pytest.raises(JobBusy):
        acquire(jobs)
    expire(jobs)
    second = acquire(jobs)
    assert first.owner != second.owner
    with pytest.raises(ClientError):
        first.save('REGISTERING', {})


def test_concurrent_failed_job_claim_has_one_winner(jobs):
    acquire(jobs).fail()
    barrier = Barrier(2)
    def claim(_):
        barrier.wait()
        try:
            acquire(jobs)
            return True
        except JobBusy:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]


@pytest.mark.parametrize('phase', ['REGISTERING', 'UPLOADING', 'UPDATING_STATUS', 'POSTING_COMMENT'])
def test_crash_during_write_is_quarantined(jobs, phase):
    journal = acquire(jobs)
    journal.save(phase, artifact())
    expire(jobs)
    with pytest.raises(ReconciliationRequired):
        acquire(jobs)
    assert jobs.get_item(Key={'job_id': journal.job_id})['Item']['status'] == 'NEEDS_REVIEW'
    with pytest.raises(ReconciliationRequired):
        acquire(jobs)


def test_legacy_failed_job_is_not_reissued(jobs):
    jobs.put_item(Item={'job_id': 'original#task', 'status': 'FAILED'})
    with pytest.raises(ReconciliationRequired):
        acquire(jobs)


def test_completed_job_is_skipped(jobs):
    journal = acquire(jobs)
    journal.save('COMPLETE', artifact())
    journal.complete('issue')
    assert acquire(jobs).done


@pytest.mark.parametrize('phase,upload,status,comment', [
    ('READY', 1, 1, 1), ('ATTACHED', 0, 1, 1), ('STATUS_UPDATED', 0, 0, 1), ('COMPLETE', 0, 0, 0),
])
def test_confirmed_boundaries_resume_without_repeating_steps(jobs, phase, upload, status, comment):
    journal = acquire(jobs)
    journal.save(phase, artifact())
    expire(jobs)
    recovered = acquire(jobs)
    client = AsyncMock()
    result = asyncio.run(complete_clickup_artifact(client, recovered.artifact,
                        checkpoint=recovered.save, phase=recovered.phase))
    assert result.package_id == 'pkg_existing'
    assert client.upload_attachment_to_custom_field.await_count == upload
    assert client.update_task_status.await_count == status
    assert client.post_comment.await_count == comment
    assert recovered.phase == 'COMPLETE'


def test_lost_comment_response_is_not_reposted(jobs):
    journal = acquire(jobs)
    journal.save('STATUS_UPDATED', artifact())
    client = AsyncMock()
    client.post_comment.side_effect = TimeoutError('response lost after server accepted write')
    with pytest.raises(TimeoutError):
        asyncio.run(complete_clickup_artifact(client, journal.artifact,
                    checkpoint=journal.save, phase='STATUS_UPDATED'))
    journal.fail()
    with pytest.raises(ReconciliationRequired):
        acquire(jobs)
    assert client.post_comment.await_count == 1


def worker(monkeypatch):
    monkeypatch.setattr(boto3, 'client', Mock())
    monkeypatch.setattr(boto3, 'resource', Mock())
    return importlib.import_module('mtm_hbl.aws_handlers.original_issuer')


def test_queue_failure_is_not_acknowledged(monkeypatch):
    module = worker(monkeypatch)
    process = AsyncMock(side_effect=TimeoutError('temporary'))
    monkeypatch.setattr(module, '_process_message', process)
    with pytest.raises(TimeoutError):
        module.worker_handler({'Records': [{'body': '{}'}, {'body': '{}'}]}, None)
    assert process.await_count == 1


@pytest.mark.parametrize('content', [b'pdf', b'corrupt'])
def test_worker_resume_downloads_existing_package_never_generates(jobs, monkeypatch, tmp_path, content):
    module = worker(monkeypatch)
    journal = acquire(jobs)
    journal.save('READY', artifact())
    journal.fail()
    from mtm_hbl.config import Settings
    monkeypatch.setattr(module, '_jobs_table', lambda: jobs)
    monkeypatch.setattr(module, '_lambda_settings', lambda: Settings(hbl_verification_bucket='bucket'))
    monkeypatch.setattr(module, '_clickup_access_token', lambda: 'test')
    client = AsyncMock()
    monkeypatch.setattr(module, 'ClickUpClient', lambda *a: client)
    generate = AsyncMock()
    monkeypatch.setattr(module, 'generate_hbl_from_clickup', generate)
    from pathlib import Path
    boto3.client.return_value.download_file.side_effect = lambda b, k, p: Path(p).write_bytes(content)
    if content == b'corrupt':
        with pytest.raises(RuntimeError, match='hash mismatch'):
            asyncio.run(module._process_message({'task_id': 'task'}))
        generate.assert_not_called()
        client.upload_attachment_to_custom_field.assert_not_called()
        assert client.post_comment.await_count == 1
        assert 'Recovery' in client.post_comment.call_args.args[1]
        with pytest.raises(RuntimeError, match='hash mismatch'):
            asyncio.run(module._process_message({'task_id': 'task'}))
        assert client.post_comment.await_count == 1
        return
    result = asyncio.run(module._process_message({'task_id': 'task'}))
    assert result['package_id'] == 'pkg_existing'
    generate.assert_not_called()
    assert acquire(jobs).done
