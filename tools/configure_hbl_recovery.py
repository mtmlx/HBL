"""Inspect HBL queue/monitoring; apply only with an explicit SNS alert destination.

Does not deploy code, invoke Lambda, redrive messages, or touch shipment records.
"""
import argparse
import json

import boto3

from mtm_hbl.aws_handlers.job_journal import LEASE_SECONDS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--worker', default='mtm-hbl-original-worker-dev')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--alarm-topic')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.alarm_topic:
        parser.error('--apply requires --alarm-topic with the approved SNS destination')
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    lmb, sqs, cw, logs = (session.client(s) for s in ['lambda', 'sqs', 'cloudwatch', 'logs'])
    config = lmb.get_function_configuration(FunctionName=args.worker)
    if config['Handler'] != 'mtm_hbl.aws_handlers.original_issuer.worker_handler':
        raise ValueError('Refusing to configure an unrelated function.')
    if config['Timeout'] >= LEASE_SECONDS:
        raise ValueError('Function can outlive the job lease; adjust the code and revalidate first.')
    mappings = [m for m in lmb.list_event_source_mappings(FunctionName=args.worker)['EventSourceMappings']
                if ':sqs:' in m['EventSourceArn']]
    if len(mappings) != 1:
        raise ValueError('Expected exactly one SQS mapping; inspect configuration manually.')
    mapping = mappings[0]
    queue_name = mapping['EventSourceArn'].split(':')[-1]
    url = sqs.get_queue_url(QueueName=queue_name)['QueueUrl']
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['VisibilityTimeout', 'RedrivePolicy'])['Attributes']
    redrive = json.loads(attrs.get('RedrivePolicy', '{}'))
    if not redrive.get('deadLetterTargetArn'):
        raise ValueError('No dead-letter queue; configure one before release.')
    visibility = max(int(attrs['VisibilityTimeout']), 6 * config['Timeout'] + mapping.get('MaximumBatchingWindowInSeconds', 0), LEASE_SECONDS + 60)
    proposal = {'worker': args.worker, 'queue': queue_name, 'visibility_current': attrs['VisibilityTimeout'],
                'visibility_proposed': visibility, 'dlq': redrive['deadLetterTargetArn'], 'applied': False}
    if args.apply:
        session.client('sns').get_topic_attributes(TopicArn=args.alarm_topic)
        sqs.set_queue_attributes(QueueUrl=url, Attributes={'VisibilityTimeout': str(visibility)})
        logs.put_metric_filter(logGroupName='/aws/lambda/' + args.worker, filterName='HBLRecoveryRequired',
            filterPattern='"HBL_QUEUE_RETRY_OR_RECONCILIATION_REQUIRED"',
            metricTransformations=[{'metricName': 'RecoveryRequired', 'metricNamespace': 'MTM/HBL', 'metricValue': '1', 'defaultValue': 0.0}])
        common = dict(ComparisonOperator='GreaterThanOrEqualToThreshold', EvaluationPeriods=1,
                      Period=60, Threshold=1.0, TreatMissingData='notBreaching', AlarmActions=[args.alarm_topic])
        cw.put_metric_alarm(AlarmName=args.worker + '-dlq', Namespace='AWS/SQS',
            MetricName='ApproximateNumberOfMessagesVisible', Statistic='Maximum',
            Dimensions=[{'Name': 'QueueName', 'Value': redrive['deadLetterTargetArn'].split(':')[-1]}], **common)
        cw.put_metric_alarm(AlarmName=args.worker + '-recovery', Namespace='MTM/HBL',
                            MetricName='RecoveryRequired', Statistic='Sum', **common)
        actual = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['VisibilityTimeout'])['Attributes']
        assert int(actual['VisibilityTimeout']) == visibility
        alarms = cw.describe_alarms(AlarmNames=[args.worker + '-dlq', args.worker + '-recovery'])['MetricAlarms']
        assert len(alarms) == 2 and all(a['AlarmActions'] == [args.alarm_topic] for a in alarms)
        proposal['applied'] = True
    print(json.dumps(proposal, indent=2))


if __name__ == '__main__':
    main()
