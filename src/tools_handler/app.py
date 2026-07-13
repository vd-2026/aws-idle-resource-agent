"""
Idle Resource Agent - Tools Lambda

Single Lambda backing the Bedrock Agent action group "idle-resource-tools".
Bedrock invokes this function with an event describing which apiPath/method
was called; this handler dispatches to the matching read-only check and
returns the result in the response envelope Bedrock Agents expect.

All AWS calls here are read-only (Describe/List/Get) except sendReport,
which publishes a plain-text report to an SNS topic (email subscriber).
No resource is ever stopped, modified, or deleted by this code.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import boto3

REPORT_TOPIC_ARN = os.environ.get("REPORT_TOPIC_ARN")

ce = boto3.client("ce")
ec2 = boto3.client("ec2")
rds = boto3.client("rds")
elbv2 = boto3.client("elbv2")
lambda_client = boto3.client("lambda")
cloudwatch = boto3.client("cloudwatch")
sns = boto3.client("sns")

LOOKBACK_DAYS = 30


def _dates():
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.isoformat(), end.isoformat()


def get_cost_by_service():
    start, end = _dates()
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    costs = []
    for group in resp.get("ResultsByTime", [{}])[0].get("Groups", []):
        service = group["Keys"][0]
        amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
        if amount > 0:
            costs.append({"service": service, "cost_usd": round(amount, 2)})
    costs.sort(key=lambda c: c["cost_usd"], reverse=True)
    return {"period_days": LOOKBACK_DAYS, "cost_by_service": costs}


def _avg_cpu(instance_id):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Average"],
    )
    points = resp.get("Datapoints", [])
    if not points:
        return None
    return sum(p["Average"] for p in points) / len(points)


def get_ec2_low_utilization():
    findings = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                state = inst["State"]["Name"]
                instance_id = inst["InstanceId"]
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    instance_id,
                )
                if state == "stopped":
                    findings.append(
                        {
                            "instance_id": instance_id,
                            "name": name,
                            "state": state,
                            "reason": "Instance stopped (still incurs EBS/EIP cost)",
                        }
                    )
                elif state == "running":
                    avg_cpu = _avg_cpu(instance_id)
                    if avg_cpu is not None and avg_cpu < 5.0:
                        findings.append(
                            {
                                "instance_id": instance_id,
                                "name": name,
                                "state": state,
                                "avg_cpu_percent": round(avg_cpu, 2),
                                "reason": "Running with average CPU below 5% over 30 days",
                            }
                        )
    return {"idle_ec2_instances": findings}


def get_unattached_ebs_volumes():
    findings = []
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for vol in page["Volumes"]:
            findings.append(
                {
                    "volume_id": vol["VolumeId"],
                    "size_gb": vol["Size"],
                    "volume_type": vol["VolumeType"],
                    "create_time": vol["CreateTime"].isoformat(),
                }
            )
    return {"unattached_ebs_volumes": findings}


def get_unassociated_eips():
    resp = ec2.describe_addresses()
    findings = [
        {
            "public_ip": addr["PublicIp"],
            "allocation_id": addr.get("AllocationId"),
        }
        for addr in resp["Addresses"]
        if "InstanceId" not in addr and "NetworkInterfaceId" not in addr
    ]
    return {"unassociated_eips": findings}


def get_idle_rds():
    findings = []
    paginator = rds.get_paginator("describe_db_instances")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    for page in paginator.paginate():
        for db in page["DBInstances"]:
            db_id = db["DBInstanceIdentifier"]
            resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName="DatabaseConnections",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
            points = resp.get("Datapoints", [])
            avg_conn = sum(p["Average"] for p in points) / len(points) if points else 0
            if avg_conn < 1.0:
                findings.append(
                    {
                        "db_instance_id": db_id,
                        "engine": db.get("Engine"),
                        "avg_connections": round(avg_conn, 2),
                        "reason": "Near-zero database connections over 30 days",
                    }
                )
    return {"idle_rds_instances": findings}


def get_idle_load_balancers():
    findings = []
    lb_paginator = elbv2.get_paginator("describe_load_balancers")
    for lb_page in lb_paginator.paginate():
        for lb in lb_page["LoadBalancers"]:
            lb_arn = lb["LoadBalancerArn"]
            tgs = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)["TargetGroups"]
            has_healthy_target = False
            for tg in tgs:
                health = elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                if any(
                    t["TargetHealth"]["State"] == "healthy"
                    for t in health["TargetHealthDescriptions"]
                ):
                    has_healthy_target = True
                    break
            if not tgs or not has_healthy_target:
                findings.append(
                    {
                        "load_balancer_name": lb["LoadBalancerName"],
                        "load_balancer_arn": lb_arn,
                        "reason": "No target groups or no healthy targets registered",
                    }
                )
    return {"idle_load_balancers": findings}


def get_stale_lambdas():
    findings = []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    paginator = lambda_client.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            fn_name = fn["FunctionName"]
            resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                StartTime=start,
                EndTime=end,
                Period=86400 * LOOKBACK_DAYS,
                Statistics=["Sum"],
            )
            total_invocations = sum(p["Sum"] for p in resp.get("Datapoints", []))
            if total_invocations == 0:
                findings.append(
                    {
                        "function_name": fn_name,
                        "last_modified": fn.get("LastModified"),
                        "reason": "Zero invocations over 30 days",
                    }
                )
    return {"stale_lambda_functions": findings}


def get_old_snapshots():
    end = datetime.now(timezone.utc)
    cutoff = end - timedelta(days=90)
    images = ec2.describe_images(Owners=["self"])["Images"]
    referenced_snapshot_ids = set()
    for image in images:
        for bdm in image.get("BlockDeviceMappings", []):
            ebs = bdm.get("Ebs")
            if ebs and "SnapshotId" in ebs:
                referenced_snapshot_ids.add(ebs["SnapshotId"])

    findings = []
    paginator = ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"]):
        for snap in page["Snapshots"]:
            if snap["StartTime"] < cutoff and snap["SnapshotId"] not in referenced_snapshot_ids:
                findings.append(
                    {
                        "snapshot_id": snap["SnapshotId"],
                        "volume_size_gb": snap["VolumeSize"],
                        "start_time": snap["StartTime"].isoformat(),
                        "reason": "Older than 90 days and not referenced by any AMI",
                    }
                )
    return {"old_orphaned_snapshots": findings}


def send_report(subject, message):
    sns.publish(TopicArn=REPORT_TOPIC_ARN, Subject=subject[:100], Message=message)
    return {"status": "sent", "topic_arn": REPORT_TOPIC_ARN}


DISPATCH = {
    "/cost-by-service": lambda params, body: get_cost_by_service(),
    "/ec2-low-utilization": lambda params, body: get_ec2_low_utilization(),
    "/unattached-ebs-volumes": lambda params, body: get_unattached_ebs_volumes(),
    "/unassociated-eips": lambda params, body: get_unassociated_eips(),
    "/idle-rds": lambda params, body: get_idle_rds(),
    "/idle-load-balancers": lambda params, body: get_idle_load_balancers(),
    "/stale-lambdas": lambda params, body: get_stale_lambdas(),
    "/old-snapshots": lambda params, body: get_old_snapshots(),
    "/send-report": lambda params, body: send_report(
        body.get("subject", "Idle Resource Report"), body.get("message", "")
    ),
}


def _extract_body(event):
    """Bedrock Agents send request body fields under requestBody.content.<mime>.properties"""
    request_body = event.get("requestBody", {})
    content = request_body.get("content", {})
    props = content.get("application/json", {}).get("properties", [])
    return {p["name"]: p["value"] for p in props}


def lambda_handler(event, context):
    api_path = event.get("apiPath", "/")
    http_method = event.get("httpMethod", "GET")
    parameters = event.get("parameters", [])
    body = _extract_body(event)

    handler = DISPATCH.get(api_path)
    if handler is None:
        result = {"error": f"Unknown apiPath: {api_path}"}
        status = 400
    else:
        try:
            result = handler(parameters, body)
            status = 200
        except Exception as exc:  # surface errors to the agent instead of failing silently
            result = {"error": str(exc)}
            status = 500

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", "idle-resource-tools"),
            "apiPath": api_path,
            "httpMethod": http_method,
            "httpStatusCode": status,
            "responseBody": {"application/json": {"body": json.dumps(result)}},
        },
    }


if __name__ == "__main__":
    # Local smoke test, e.g.: python app.py cost
    import sys

    op = sys.argv[1] if len(sys.argv) > 1 else "cost"
    fn_map = {
        "cost": get_cost_by_service,
        "ec2": get_ec2_low_utilization,
        "ebs": get_unattached_ebs_volumes,
        "eip": get_unassociated_eips,
        "rds": get_idle_rds,
        "elb": get_idle_load_balancers,
        "lambda": get_stale_lambdas,
        "snapshots": get_old_snapshots,
    }
    print(json.dumps(fn_map[op](), indent=2, default=str))
