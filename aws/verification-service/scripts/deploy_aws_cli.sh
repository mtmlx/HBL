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

echo "Deploying MTM HBL verification service"
echo "Account: ${ACCOUNT_ID}"
echo "Region: ${REGION}"
echo "Environment: ${ENVIRONMENT}"

# The ordinary deploy identity cannot modify IAM permissions.
if ! ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query Role.Arn --output text)"; then
  echo "Missing execution role. An administrator must run scripts/bootstrap_iam.sh first." >&2
  exit 1
fi

mkdir -p "${BUILD_DIR}"

if aws s3api head-bucket --bucket "${BUCKET_NAME}" >/dev/null 2>&1; then
  echo "S3 bucket exists: ${BUCKET_NAME}"
else
  echo "Creating S3 bucket: ${BUCKET_NAME}"
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${BUCKET_NAME}" --region "${REGION}" >/dev/null
  else
    aws s3api create-bucket \
      --bucket "${BUCKET_NAME}" \
      --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
fi

aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled >/dev/null

if aws dynamodb describe-table --table-name "${TABLE_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  echo "DynamoDB table exists: ${TABLE_NAME}"
else
  echo "Creating DynamoDB table: ${TABLE_NAME}"
  aws dynamodb create-table \
    --table-name "${TABLE_NAME}" \
    --attribute-definitions AttributeName=verification_id,AttributeType=S \
    --key-schema AttributeName=verification_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "${REGION}" >/dev/null
  aws dynamodb wait table-exists --table-name "${TABLE_NAME}" --region "${REGION}"
fi

echo "Packaging Lambda function"
rm -f "${ZIP_PATH}"
(cd "${ROOT_DIR}/src" && zip -q -r "${ZIP_PATH}" .)

if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  echo "Updating Lambda function: ${FUNCTION_NAME}"
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --region "${REGION}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${REGION}"
  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --runtime python3.11 \
    --handler app.lambda_handler \
    --role "${ROLE_ARN}" \
    --environment "Variables={TABLE_NAME=${TABLE_NAME}}" \
    --timeout 10 \
    --memory-size 128 \
    --region "${REGION}" >/dev/null
else
  echo "Creating Lambda function: ${FUNCTION_NAME}"
  sleep 10
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime python3.11 \
    --handler app.lambda_handler \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --environment "Variables={TABLE_NAME=${TABLE_NAME}}" \
    --timeout 10 \
    --memory-size 128 \
    --region "${REGION}" >/dev/null
fi
aws lambda wait function-active --function-name "${FUNCTION_NAME}" --region "${REGION}"

FUNCTION_ARN="$(aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" --query Configuration.FunctionArn --output text)"

API_ID="$(aws apigatewayv2 get-apis --region "${REGION}" --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
if [[ "${API_ID}" == "None" || -z "${API_ID}" ]]; then
  echo "Creating HTTP API: ${API_NAME}"
  API_ID="$(aws apigatewayv2 create-api \
    --name "${API_NAME}" \
    --protocol-type HTTP \
    --region "${REGION}" \
    --query ApiId \
    --output text)"
else
  echo "HTTP API exists: ${API_NAME} (${API_ID})"
fi

INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "${API_ID}" --region "${REGION}" --query "Items[?IntegrationUri=='${FUNCTION_ARN}'].IntegrationId | [0]" --output text)"
if [[ "${INTEGRATION_ID}" == "None" || -z "${INTEGRATION_ID}" ]]; then
  echo "Creating Lambda integration"
  INTEGRATION_ID="$(aws apigatewayv2 create-integration \
    --api-id "${API_ID}" \
    --integration-type AWS_PROXY \
    --integration-uri "${FUNCTION_ARN}" \
    --payload-format-version "2.0" \
    --region "${REGION}" \
    --query IntegrationId \
    --output text)"
fi

ensure_route() {
  local route_key="$1"
  local route_id
  route_id="$(aws apigatewayv2 get-routes --api-id "${API_ID}" --region "${REGION}" --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text)"
  if [[ "${route_id}" == "None" || -z "${route_id}" ]]; then
    echo "Creating route: ${route_key}"
    aws apigatewayv2 create-route \
      --api-id "${API_ID}" \
      --route-key "${route_key}" \
      --target "integrations/${INTEGRATION_ID}" \
      --region "${REGION}" >/dev/null
  else
    echo "Route exists: ${route_key}"
    aws apigatewayv2 update-route \
      --api-id "${API_ID}" \
      --route-id "${route_id}" \
      --target "integrations/${INTEGRATION_ID}" \
      --region "${REGION}" >/dev/null
  fi
}

ensure_route "GET /verify/{verification_id}"
ensure_route "GET /api/verify/{verification_id}"

STAGE_ID="$(aws apigatewayv2 get-stages --api-id "${API_ID}" --region "${REGION}" --query "Items[?StageName=='\$default'].StageName | [0]" --output text)"
if [[ "${STAGE_ID}" == "None" || -z "${STAGE_ID}" ]]; then
  echo "Creating default stage"
  aws apigatewayv2 create-stage \
    --api-id "${API_ID}" \
    --stage-name '$default' \
    --auto-deploy \
    --region "${REGION}" >/dev/null
else
  aws apigatewayv2 update-stage \
    --api-id "${API_ID}" \
    --stage-name '$default' \
    --auto-deploy \
    --region "${REGION}" >/dev/null
fi

STATEMENT_ID="AllowHttpApiInvoke-${ENVIRONMENT}"
aws lambda remove-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "${STATEMENT_ID}" \
  --region "${REGION}" >/dev/null 2>&1 || true
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "${STATEMENT_ID}" \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*/*" \
  --region "${REGION}" >/dev/null

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com"

cat <<EOF

Deployment complete.

S3 bucket:
${BUCKET_NAME}

DynamoDB table:
${TABLE_NAME}

Lambda function:
${FUNCTION_NAME}

Verification API URL:
${API_URL}

HTML test:
${API_URL}/verify/TEST

JSON test:
${API_URL}/api/verify/TEST
EOF
