"""Test replacement transactions in a disposable table containing synthetic records only."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from mtm_hbl.verification.aws_repository import AwsVerificationConfig, activate_replacement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    boto3.setup_default_session(profile_name=args.profile, region_name='us-east-1')
    db = boto3.resource('dynamodb')
    name = 'codex-hbl-atomic-test-' + uuid4().hex[:12]
    report = {'table': name, 'scope': 'synthetic records only; no documents, ClickUp or operational tables', 'checks': {}}
    created = False
    try:
        table = db.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                    KeySchema=[{'AttributeName': 'verification_id', 'KeyType': 'HASH'}],
                    AttributeDefinitions=[{'AttributeName': 'verification_id', 'AttributeType': 'S'}],
                    Tags=[{'Key': 'Purpose', 'Value': 'disposable-atomic-switch-test'}])
        created = True
        report['cleanup'] = 'pending'
        table.meta.client.get_waiter('table_exists').wait(TableName=name, WaiterConfig={'Delay': 1, 'MaxAttempts': 60})
        old = [SimpleNamespace(verification_id=f'test-old-{i}', package_id='test-pkg-old') for i in range(6)]
        new = {f'TEST-NOT-A-SHIPMENT-{kind}{i}-NEW': 'https://test.invalid' for kind in ['O', 'C'] for i in range(1, 4)}
        for r in old:
            table.put_item(Item={'verification_id': r.verification_id, 'package_id': r.package_id,
                                 'hbl_number': 'TEST-NOT-A-SHIPMENT', 'status': 'ISSUED'})
        for vid in new:
            table.put_item(Item={'verification_id': vid, 'package_id': 'test-pkg-new', 'pdf_sha256': 'synthetic',
                                 'hbl_number': 'TEST-NOT-A-SHIPMENT', 'status': 'PREPARED'})
        result = SimpleNamespace(verification_urls=new, package_id='test-pkg-new', pdf_sha256='synthetic',
                                 hbl_number='TEST-NOT-A-SHIPMENT')
        config = AwsVerificationConfig('unused-no-s3-access', name)
        def status(vid, value):
            table.update_item(Key={'verification_id': vid}, UpdateExpression='SET #s = :v',
                               ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':v': value})
        status(old[-1].verification_id, 'VOID')
        try:
            activate_replacement(config, old, result, reason='synthetic rollback test')
        except ClientError as exc:
            assert exc.response['Error']['Code'] == 'TransactionCanceledException'
        else:
            raise AssertionError('Conflicting transaction unexpectedly succeeded')
        rows = table.scan(ConsistentRead=True)['Items']
        assert sum(r['status'] == 'PREPARED' for r in rows) == 6
        assert sum(r['status'] == 'ISSUED' for r in rows) == 5
        report['checks']['conflict_rolls_back_all_other_records'] = True
        print('PASS: conflict rolls back entire switch', flush=True)
        status(old[-1].verification_id, 'ISSUED')
        activate_replacement(config, old, result, reason='synthetic successful switch')
        rows = table.scan(ConsistentRead=True)['Items']
        assert sum(r['status'] == 'ISSUED' and r['package_id'] == 'test-pkg-new' for r in rows) == 6
        assert sum(r['status'] == 'VOID' and r['superseded_by'] == 'test-pkg-new' for r in rows) == 6
        report['checks']['all_six_old_void_and_all_six_new_active'] = True
        try:
            activate_replacement(config, old, result, reason='synthetic duplicate switch')
        except ClientError as exc:
            assert exc.response['Error']['Code'] == 'TransactionCanceledException'
        else:
            raise AssertionError('Duplicate switch unexpectedly succeeded')
        assert sorted(table.scan(ConsistentRead=True)['Items'], key=lambda r: r['verification_id']) == sorted(rows, key=lambda r: r['verification_id'])
        report['checks']['duplicate_switch_rejected_without_changes'] = True
        print('PASS: successful switch and duplicate rejection', flush=True)
    finally:
        try:
            if created:
                table.delete()
                table.meta.client.get_waiter('table_not_exists').wait(TableName=name, WaiterConfig={'Delay': 1, 'MaxAttempts': 60})
                report['cleanup'] = 'deleted and absence verified'
        finally:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
