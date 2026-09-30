#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
REGION="${AWS_REGION:-$(aws configure get region)}"
REGION="${REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

BUCKET_NAME="mtm-hbl-documents-${ENVIRONMENT}-${ACCOUNT_ID}"
TABLE_NAME="mtm-hbl-verification-${ENVIRONMENT}"
FUNCTION_NAME="mtm-hbl-verification-${ENVIRONMENT}"
ROLE_NAME="mtm-hbl-verification-lambda-role-${ENVIRONMENT}"
POLICY_NAME="mtm-hbl-verification-lambda-policy-${ENVIRONMENT}"
API_NAME="mtm-hbl-verification-api-${ENVIRONMENT}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/.build"
ZIP_PATH="${BUILD_DIR}/function.zip"

# Run only with separately authorized administrator credentials.
# Ordinary deployment credentials must never receive role mutation permissions.
mkdir -p "${BUILD_DIR}"
TRUST_POLICY="${BUILD_DIR}/trust-policy.json"
cat > "${TRUST_POLICY}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "IAM role exists: ${ROLE_NAME}"
else
  echo "Creating IAM role: ${ROLE_NAME}"
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${TRUST_POLICY}" >/dev/null
fi

aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null

INLINE_POLICY="${BUILD_DIR}/lambda-policy.json"
cat > "${INLINE_POLICY}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}"
    }
  ]
}
JSON
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "file://${INLINE_POLICY}" >/dev/null

ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query Role.Arn --output text)"

echo "IAM bootstrap completed for ${ROLE_NAME}. Use restricted credentials for deployment."
