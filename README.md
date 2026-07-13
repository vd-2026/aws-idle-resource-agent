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

```
EventBridge (weekly)  ──▶  Bedrock Agent (Claude)
                              │
                              ├─▶ getCostByService        (Cost Explorer)
                              ├─▶ getEc2LowUtilization    (EC2 + CloudWatch)
                              ├─▶ getUnattachedEbsVolumes (EC2)
                              ├─▶ getUnassociatedEips     (EC2)
                              ├─▶ getIdleRds              (RDS + CloudWatch)
                              ├─▶ getIdleLoadBalancers    (ELBv2)
                              ├─▶ getStaleLambdas         (Lambda + CloudWatch)
                              ├─▶ getOldSnapshots         (EC2)
                              └─▶ sendReport               ──▶ SNS Topic ──▶ Email
```

All 9 "tools" are operations on **one** Lambda function
(`src/tools_handler/app.py`), routed by `apiPath`. This keeps the project
simple to deploy while still exposing a rich toolset to the agent.

## Repo layout

```
template.yaml              SAM template: Lambda, IAM, SNS topic, Bedrock Agent + Action Group
src/tools_handler/app.py   All read-only checks + SNS report publisher
src/tools_handler/requirements.txt
```

## Prerequisites

- AWS account with **Bedrock model access enabled** for
  `anthropic.claude-3-5-sonnet-20240620-v1:0` (or change `BedrockModelId`)
  in the Bedrock console → Model access.
- AWS CLI configured with credentials that can deploy CloudFormation/SAM.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed.
- Python 3.12.

## Deploy

```bash
sam build
sam deploy --guided
```

You'll be prompted for:
- `NotificationEmail` — where the report gets emailed (you must **confirm
  the SNS subscription email** AWS sends you before reports will arrive).
- `BedrockModelId` — defaults to Claude 3.5 Sonnet.

After deploy, note the `AgentId` output.

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
`Terminate*`, or `Delete*` permission anywhere in `template.yaml` — the
agent's instructions also explicitly forbid recommending automated remediation.

## Possible extensions

- Schedule via EventBridge for a weekly automated report.
- Store historical reports in S3 and build a small trend dashboard.
- Add a "dry-run remediation" action group gated behind human approval.
