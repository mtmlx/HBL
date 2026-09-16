"""Full worker + real renderer/registration, with AWS emulation and fake ClickUp.

Hard-stop injection bypasses normal exception handling to exercise restart logic.
This is not a live ClickUp or Lambda test.
"""
import asyncio
from hashlib import sha256
import importlib

import boto3
import pytest
from moto import mock_aws

from mtm_hbl.aws_handlers.job_journal import JobJournal, ReconciliationRequired
from mtm_hbl.config import Settings
from tests.test_aws_source_sync import approved_client


class InjectedProcessDeath(BaseException):
    pass


@pytest.mark.parametrize('stop_phase', ['READY', 'ATTACHED', 'STATUS_UPDATED', 'COMPLETE', 'POSTING_COMMENT'])
def test_worker_crash_recovery_never_registers_second_package(monkeypatch, tmp_path, app_config, stop_phase):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        db = boto3.resource('dynamodb', region_name='us-east-1')
        def table(name, key):
            return db.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}])
        jobs = table('test-jobs', 'job_id')
        records = table('test-verification', 'verification_id')
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='hbl-fault-test')
        module = importlib.import_module('mtm_hbl.aws_handlers.original_issuer')
        settings = Settings(runs_dir=tmp_path, hbl_verification_bucket='hbl-fault-test',
                            hbl_verification_table='test-verification', hbl_verification_base_url='https://test.invalid',
                            aws_region='us-east-1')
        client = approved_client(app_config)
        monkeypatch.setattr(module, '_jobs_table', lambda: jobs)
        monkeypatch.setattr(module, '_lambda_settings', lambda: settings)
        monkeypatch.setattr(module, '_clickup_access_token', lambda: 'test-token')
        monkeypatch.setattr(module, 'ClickUpClient', lambda *args: client)
        original_save = JobJournal.save
        def interrupted_save(self, phase, artifact):
            original_save(self, phase, artifact)
            if phase == stop_phase:
                raise InjectedProcessDeath()
        monkeypatch.setattr(JobJournal, 'save', interrupted_save)
        with pytest.raises(InjectedProcessDeath):
            asyncio.run(module._process_message({'task_id': 'task-1'}))
        issued = records.scan()['Items']
        assert len(issued) == 6
        package_ids = {r['package_id'] for r in issued}
        assert len(package_ids) == 1
        pdf = s3.get_object(Bucket='hbl-fault-test', Key=issued[0]['pdf_s3_key'])['Body'].read()
        assert sha256(pdf).hexdigest() == issued[0]['pdf_sha256']

        monkeypatch.setattr(JobJournal, 'save', original_save)
        jobs.update_item(Key={'job_id': 'original#task-1'}, UpdateExpression='SET lease_until = :n',
                         ExpressionAttributeValues={':n': 1})
        if stop_phase == 'POSTING_COMMENT':
            with pytest.raises(ReconciliationRequired):
                asyncio.run(module._process_message({'task_id': 'task-1'}))
            assert jobs.get_item(Key={'job_id': 'original#task-1'})['Item']['status'] == 'NEEDS_REVIEW'
        else:
            result = asyncio.run(module._process_message({'task_id': 'task-1'}))
            assert result['package_id'] in package_ids
            count = len(client.events)
            assert asyncio.run(module._process_message({'task_id': 'task-1'}))['status'] == 'SKIPPED'
            assert len(client.events) == count
            assert sum(e[0] == 'upload' for e in client.events) == 1
            assert sum(e[0] == 'status' for e in client.events) == 1
            comments = [e for e in client.events if e[0] == 'comment']
            assert len(comments) == 1
            assert comments[0][2] == '12345'
            assert len(comments[0][1].splitlines()) == 3
            assert 'Package ID: ' + result['package_id'] in comments[0][1]
        assert len(records.scan()['Items']) == 6
        assert {r['package_id'] for r in records.scan()['Items']} == package_ids
        assert len(s3.list_objects_v2(Bucket='hbl-fault-test')['Contents']) == 2
