"""Explicit live integration test using a newly created, disposable DynamoDB table.

No shipment records, Lambda invocations, S3 objects, or ClickUp writes are used.
Run with PYTHONPATH=src python tools/test_job_journal_aws.py --profile PROFILE.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from mtm_hbl.aws_handlers.job_journal import JobJournal, JobBusy, ReconciliationRequired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    db = session.client('dynamodb')
    name = 'codex-hbl-recovery-test-' + uuid4().hex[:12]
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'table': name,
              'scope': 'real DynamoDB; synthetic jobs; injected lease expiry; no Lambda/SQS/ClickUp execution',
              'tests': [], 'cleanup': 'not created'}
    created = False

    def passed(label):
        report['tests'].append({'name': label, 'result': 'passed'})
        print('PASS:', label, flush=True)

    def expires(table, key):
        table.update_item(Key={'job_id': key}, UpdateExpression='SET lease_until = :n',
                          ExpressionAttributeValues={':n': 1})

    def must_raise(error, call):
        try:
            call()
        except error:
            return
        raise AssertionError('Expected ' + error.__name__)

    try:
        db.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                        KeySchema=[{'AttributeName': 'job_id', 'KeyType': 'HASH'}],
                        AttributeDefinitions=[{'AttributeName': 'job_id', 'AttributeType': 'S'}],
                        Tags=[{'Key': 'Purpose', 'Value': 'disposable-hbl-recovery-test'}])
        created = True
        report['cleanup'] = 'pending deletion'
        db.get_waiter('table_exists').wait(TableName=name, WaiterConfig={'Delay': 1, 'MaxAttempts': 60})
        table = session.resource('dynamodb').Table(name)
        first = JobJournal(table, 'lease', 'synthetic', 'issue')
        must_raise(JobBusy, lambda: JobJournal(table, 'lease', 'synthetic', 'issue'))
        passed('live lease excludes another owner')
        expires(table, 'lease')
        second = JobJournal(table, 'lease', 'synthetic', 'issue')
        assert second.owner != first.owner
        must_raise(ClientError, lambda: first.save('REGISTERING', {}))
        passed('expired lease reclaimed and stale owner rejected')

        JobJournal(table, 'race', 'synthetic', 'issue').fail()
        barrier = Barrier(8)
        # Each contender gets its own boto3 resource/session.
        tables = [boto3.Session(profile_name=args.profile, region_name=args.region).resource('dynamodb').Table(name)
                  for _ in range(8)]
        def claim(t):
            barrier.wait(timeout=30)
            try:
                JobJournal(t, 'race', 'synthetic', 'issue')
                return True
            except JobBusy:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = list(pool.map(claim, tables))
        assert sum(winners) == 1, winners
        passed('eight simultaneous retry claimants produce exactly one owner')

        for phase in ['REGISTERING', 'UPLOADING', 'UPDATING_STATUS', 'POSTING_COMMENT']:
            job = JobJournal(table, phase, 'synthetic', 'issue')
            job.save(phase, {'package_id': 'TEST-NOT-A-SHIPMENT'})
            expires(table, phase)
            must_raise(ReconciliationRequired, lambda: JobJournal(table, phase, 'synthetic', 'issue'))
            item = table.get_item(Key={'job_id': phase}, ConsistentRead=True)['Item']
            assert item['status'] == 'NEEDS_REVIEW'
            must_raise(ReconciliationRequired, lambda: JobJournal(table, phase, 'synthetic', 'issue'))
            passed('interrupted ' + phase + ' quarantines and blocks replay')

        for phase in ['START', 'READY', 'ATTACHED', 'STATUS_UPDATED', 'COMPLETE']:
            key = 'safe-' + phase
            job = JobJournal(table, key, 'synthetic', 'issue')
            data = {'result': {'package_id': 'TEST-NOT-A-SHIPMENT'}}
            job.save(phase, data)
            expires(table, key)
            resumed = JobJournal(table, key, 'synthetic', 'issue')
            assert resumed.phase == phase and resumed.artifact == data
            passed('confirmed ' + phase + ' preserves artifact on takeover')

        job = JobJournal(table, 'done', 'synthetic', 'issue')
        job.save('COMPLETE', {'result': {'package_id': 'TEST-NOT-A-SHIPMENT'}})
        job.complete('issue')
        assert JobJournal(table, 'done', 'synthetic', 'issue').done
        passed('completed job is skipped')
        table.put_item(Item={'job_id': 'legacy', 'status': 'FAILED'})
        must_raise(ReconciliationRequired, lambda: JobJournal(table, 'legacy', 'synthetic', 'issue'))
        passed('legacy failed job cannot blindly reissue')
        report['result'] = 'passed'
    except Exception as exc:
        report['result'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        try:
            if created:
                db.delete_table(TableName=name)
                db.get_waiter('table_not_exists').wait(TableName=name, WaiterConfig={'Delay': 1, 'MaxAttempts': 60})
                report['cleanup'] = 'deleted and absence verified'
        finally:
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + '\n')
            print('Report:', args.report, flush=True)


if __name__ == '__main__':
    main()
