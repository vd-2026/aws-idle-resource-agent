#!/usr/bin/env bash
# Zips the Lambda source, uploads it to S3, and deploys the plain
# CloudFormation stack (no SAM transform is used in template.yaml, so
# CloudFormation needs the code already sitting in S3 before deploy).
#
# Usage:
#   ./scripts/deploy.sh my-deploy-bucket you@example.com
#
# Requires: AWS CLI configured with deploy permissions, zip.
set -euo pipefail

BUCKET="${1:?Usage: deploy.sh <s3-bucket> <notification-email> [stack-name] [s3-key] [bedrock-model-id]}"
EMAIL="${2:?Usage: deploy.sh <s3-bucket> <notification-email> [stack-name] [s3-key] [bedrock-model-id]}"
STACK_NAME="${3:-idle-resource-agent}"
KEY="${4:-idle-resource-agent/tools_handler.zip}"
BEDROCK_MODEL_ID="${5:-anthropic.claude-3-5-sonnet-20240620-v1:0}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/src/tools_handler"
ZIP_PATH="$ROOT_DIR/tools_handler.zip"

rm -f "$ZIP_PATH"
echo "Packaging $SRC_DIR -> $ZIP_PATH"
(cd "$SRC_DIR" && zip -r -q "$ZIP_PATH" .)

echo "Uploading to s3://$BUCKET/$KEY"
aws s3 cp "$ZIP_PATH" "s3://$BUCKET/$KEY"

echo "Deploying CloudFormation stack '$STACK_NAME'"
aws cloudformation deploy \
    --template-file "$ROOT_DIR/template.yaml" \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        NotificationEmail="$EMAIL" \
        CodeS3Bucket="$BUCKET" \
        CodeS3Key="$KEY" \
        BedrockModelId="$BEDROCK_MODEL_ID"

echo "Done. Fetching stack outputs..."
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query "Stacks[0].Outputs" --output table
