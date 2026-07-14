# AWS Idle Resource Agent

A small **agentic GenAI** project: an **Amazon Bedrock Agent** that inspects
your AWS account, figures out where money is actually being spent (via **Cost
Explorer**), and then decides for itself which idle-resource checks are worth
running — instead of blindly checking everything. It's **read-only**: it only
reports and recommends, never deletes or stops anything. The final report is
emailed via **SNS**.

## Why this is "agentic" (not just a script)

A traditional script would run all checks every time. This project gives the
Bedrock Agent (Claude) a single Python Lambda exposing 9 read-only "tools"
(via a Bedrock Agent Action Group) and lets the model reason about which
tools to call and in what order:

1. Always calls `getCostByService` first (Cost Explorer, last 30 days).
2. Based on which services show real spend, it **chooses** which of the
   remaining 7 checks are worth running (e.g. skips RDS checks entirely if
   RDS costs are ~$0).
3. Cross-references findings against the cost data to estimate savings.
4. Classifies each finding's confidence.
5. Calls `sendReport` with a written summary, which publishes to SNS (email).

## Architecture

```mermaid
flowchart LR
    subgraph Trigger
        EB[EventBridge<br/>weekly schedule]
        User[Manual test /<br/>InvokeAgent call]
    end

    Agent[Bedrock Agent<br/>Claude foundation model]

    subgraph "Action Group: idle-resource-tools (1 Lambda, 9 operations)"
        L[Tools Lambda<br/>src/tools_handler/app.py]
    end

    CE[(Cost Explorer)]
    EC2[(EC2 API +<br/>CloudWatch)]
    EBSAPI[(EC2: Volumes,<br/>EIPs, Snapshots)]
    RDSAPI[(RDS API +<br/>CloudWatch)]
    ELBAPI[(ELBv2 API)]
    LambdaAPI[(Lambda API +<br/>CloudWatch)]

    SNS[/SNS Topic/]
    Email([Email inbox])

    EB --> Agent
    User --> Agent
    Agent -->|1: getCostByService| L
    Agent -->|2-8: idle checks,<br/>chosen dynamically| L
    Agent -->|9: sendReport| L

    L --> CE
    L --> EC2
    L --> EBSAPI
    L --> RDSAPI
    L --> ELBAPI
    L --> LambdaAPI
    L -->|publish report| SNS
    SNS --> Email
```

**Agent decision flow** (why this is "agentic" and not a fixed pipeline):

```mermaid
sequenceDiagram
    participant U as User / EventBridge
    participant A as Bedrock Agent
    participant L as Tools Lambda
    participant S as SNS

    U->>A: "Find idle resources, estimate savings, email report"
    A->>L: getCostByService()
    L-->>A: cost by service (last 30 days)
    Note over A: Agent reasons: which services<br/>have non-trivial spend?
    A->>L: getEc2LowUtilization() (if EC2 spend > threshold)
    L-->>A: idle EC2 findings
    A->>L: getIdleRds() (skipped if RDS spend ~$0)
    L-->>A: (skipped)
    A->>L: getUnattachedEbsVolumes(), getUnassociatedEips(), ...
    L-->>A: findings
    Note over A: Cross-reference findings with cost data,<br/>classify confidence, estimate savings
    A->>L: sendReport(subject, message)
    L->>S: sns:Publish
    S-->>U: Email with report
```

All 9 "tools" are operations on **one** Lambda function
(`src/tools_handler/app.py`), routed by `apiPath`. This keeps the project
simple to deploy while still exposing a rich toolset to the agent.

## Repo layout

```
template.json                Plain CloudFormation (JSON): Lambda, IAM, SNS topic, Bedrock Agent + Action Group
src/tools_handler/app.py    All read-only checks + SNS report publisher
src/tools_handler/requirements.txt
scripts/deploy.ps1          Zip, upload to S3, and deploy (PowerShell)
scripts/deploy.sh           Zip, upload to S3, and deploy (bash)
```

## Prerequisites

- AWS account with **Bedrock model access enabled** for
  `anthropic.claude-3-5-sonnet-20240620-v1:0` (or change `BedrockModelId`)
  in the Bedrock console → Model access.
- AWS CLI configured with credentials that can deploy CloudFormation.
- An S3 bucket you can upload the Lambda deployment package to (any bucket
  in the same region works).
- Python 3.12.

This project uses **plain CloudFormation** (`template.json`, no
`Transform: AWS::Serverless-2016-10-31`, no SAM CLI required). Because
there's no SAM packaging step, the Lambda code must be zipped and uploaded
to S3 before `aws cloudformation deploy` runs — the provided scripts do
both steps for you.

## Deploy

**PowerShell:**
```powershell
.\scripts\deploy.ps1 -Bucket my-deploy-bucket -Email you@example.com
```

**bash:**
```bash
./scripts/deploy.sh my-deploy-bucket you@example.com
```

Both scripts: zip `src/tools_handler/`, upload it to
`s3://<bucket>/idle-resource-agent/tools_handler.zip`, then run
`aws cloudformation deploy` with `NotificationEmail`, `CodeS3Bucket`,
`CodeS3Key`, and `BedrockModelId` as parameters.

You must **confirm the SNS subscription email** AWS sends you before
reports will arrive. After deploy, note the `AgentId` stack output.

To redeploy after changing `src/tools_handler/app.py`, just re-run the
same script — it re-zips, re-uploads, and updates the stack (Lambda code
changes are picked up automatically by CloudFormation on redeploy).

## Try it

In the Bedrock console → Agents → your agent → **Test**, or via CLI:

```bash
aws bedrock-agent-runtime invoke-agent \
  --agent-id <AgentId> \
  --agent-alias-id TSTALIASID \
  --session-id demo-session-1 \
  --input-text "Find unused AWS resources from the last 30 days, estimate wasted spend, and email me the report." \
  output.json
```

## Local smoke test (no agent needed)

Run individual checks directly against your AWS account/credentials:

```bash
cd src/tools_handler
pip install -r requirements.txt
python app.py cost        # cost by service
python app.py ec2         # idle EC2 instances
python app.py ebs         # unattached volumes
python app.py eip         # unassociated Elastic IPs
python app.py rds         # idle RDS instances
python app.py elb         # idle load balancers
python app.py lambda      # stale Lambda functions
python app.py snapshots   # old orphaned snapshots
```

## Safety

Every IAM permission granted to the Lambda is `Describe*` / `List*` / `Get*`
plus `sns:Publish` to the report topic only. There is no `Stop*`,
`Terminate*`, or `Delete*` permission anywhere in `template.json` — the
agent's instructions also explicitly forbid recommending automated remediation.

## Possible extensions

- Schedule via EventBridge for a weekly automated report.
- Store historical reports in S3 and build a small trend dashboard.
- Add a "dry-run remediation" action group gated behind human approval.
