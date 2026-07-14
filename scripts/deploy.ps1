# Zips the Lambda source, uploads it to S3, and deploys the plain
# CloudFormation stack (no SAM transform is used in template.json, so
# CloudFormation needs the code already sitting in S3 before deploy).
#
# Usage:
#   .\scripts\deploy.ps1 -Bucket my-deploy-bucket -Email you@example.com
#
# Requires: AWS CLI configured with deploy permissions, PowerShell 5+.

param(
    [Parameter(Mandatory = $true)][string]$Bucket,
    [Parameter(Mandatory = $true)][string]$Email,
    [string]$StackName = "idle-resource-agent",
    [string]$Key = "idle-resource-agent/tools_handler.zip",
    [string]$BedrockModelId = "anthropic.claude-3-5-sonnet-20240620-v1:0"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "src\tools_handler"
$zipPath = Join-Path $root "tools_handler.zip"

if (Test-Path $zipPath) { Remove-Item $zipPath }

Write-Host "Packaging $src -> $zipPath"
Compress-Archive -Path (Join-Path $src '*') -DestinationPath $zipPath -Force

Write-Host "Uploading to s3://$Bucket/$Key"
aws s3 cp $zipPath "s3://$Bucket/$Key"
if ($LASTEXITCODE -ne 0) { throw "s3 upload failed" }

Write-Host "Deploying CloudFormation stack '$StackName'"
aws cloudformation deploy `
    --template-file (Join-Path $root "template.json") `
    --stack-name $StackName `
    --capabilities CAPABILITY_IAM `
    --parameter-overrides `
        NotificationEmail=$Email `
        CodeS3Bucket=$Bucket `
        CodeS3Key=$Key `
        BedrockModelId=$BedrockModelId
if ($LASTEXITCODE -ne 0) { throw "cloudformation deploy failed" }

Write-Host "Done. Fetching stack outputs..."
aws cloudformation describe-stacks --stack-name $StackName --query "Stacks[0].Outputs" --output table
