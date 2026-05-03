#!/usr/bin/env python3
"""CDK entrypoint for the AI-HedgeFund AWS deployment.

Two stacks:
  AIHedge-Platform-Stack — long-lived (VPC, OpenSearch, AMP, OSIS, ECR, CodeBuild,
                            Gateway shell, KMS, Secrets, Config rule).
  AIHedge-App-Stack      — push-button teardown (AgentCore Runtime, Fargate,
                            Step Fn, Lambdas, APIGW, EventBridge, DynamoDB,
                            dashboards).

Two-phase rollout driven by context flags:
  cdk deploy -c agentCoreEnabled=false                    # first pass (no Runtime yet)
  # trigger CodeBuild → image in ECR
  cdk deploy -c agentCoreEnabled=true                     # Runtime + Gateway targets
  cdk deploy -c agentCoreEnabled=true -c observabilityEnabled=true   # OTel wiring
"""
import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagSuppressions

from aspects.require_tag import RequireTag
from stacks.app_stack import AppStack
from stacks.platform_stack import PlatformStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT", "590183796434"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

tag_key = app.node.try_get_context("aihedge:tag") or "UsedBy"
tag_value = app.node.try_get_context("aihedge:tagValue") or "AIHedge"

# Apply the mandatory tag at the app root BEFORE instantiating stacks, so the
# tag is baked into every taggable construct's properties at construct time.
# Using cdk.Tags.of(...).add(...) directly is the supported pattern; a visitor
# aspect runs late and the RequireTag aspect sees it pre-tag.
cdk.Tags.of(app).add(tag_key, tag_value)

platform = PlatformStack(
    app,
    "AIHedge-Platform-Stack",
    env=env,
    description="AI-HedgeFund platform: VPC, OpenSearch, AMP, OSIS, ECR, CodeBuild, Gateway shell",
)

app_stack = AppStack(
    app,
    "AIHedge-App-Stack",
    env=env,
    platform=platform,
    description="AI-HedgeFund app: AgentCore Runtime + Gateway targets + Fargate + Step Fn + APIGW",
)
app_stack.add_dependency(platform)

# Synth-time tag enforcement: after stacks are instantiated, verify every
# taggable resource carries the tag. ApplyDefaultTag was folded into the
# pre-stack-instantiation cdk.Tags.of(app).add(...) above; this aspect only
# validates — it doesn't mutate.
cdk.Aspects.of(app).add(RequireTag(key=tag_key, value=tag_value))

# cdk-nag surfaces obvious security findings at synth. Suppressions are narrow
# and carry reasons — mirrors the TauricResearch pattern.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

for stack in (platform, app_stack):
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-IAM4",
                "reason": "CDK L2 Lambda constructs attach AWSLambdaBasicExecutionRole automatically; swapping for a hand-rolled policy adds drift risk for no meaningful blast-radius reduction.",
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": "Wildcards come from CDK-generated grants (grantPullPush / grantInvoke / grantReadWrite) scoped to a single resource ARN with :* suffix, or narrow Bedrock inference-profile patterns.",
            },
            {
                "id": "AwsSolutions-L1",
                "reason": "Lambda runtimes pinned to Python 3.12 (latest with CDK bundling support at write time).",
            },
            {
                "id": "AwsSolutions-S1",
                "reason": "Server access logging unnecessary for small config + artifact buckets. Versioning + CloudTrail data events provide the forensic trail.",
            },
            {
                "id": "AwsSolutions-SMG4",
                "reason": "Secrets (FinancialDatasets key, MD-Store token, OpenSearch master) are issued by external services or human operators — Secrets Manager automatic rotation doesn't apply.",
            },
            {
                "id": "AwsSolutions-CB3",
                "reason": "Privileged mode is required: the CodeBuild project runs docker buildx to produce the AgentCore/Fargate and Lambda container images.",
            },
            {
                "id": "AwsSolutions-CB4",
                "reason": "CodeBuild artifacts bucket uses SSE-S3 with bucket-owner-enforced; CMK for the build environment adds cost without meaningful additional protection.",
            },
            {
                "id": "AwsSolutions-SF1",
                "reason": "State machine logging set to ERROR for cost control; per-Lambda CloudWatch logs cover per-step detail.",
            },
            {
                "id": "AwsSolutions-ECS4",
                "reason": "Container Insights disabled for cost control; per-task logs + task metrics sufficient for one-task-per-ticker daily batch.",
            },
            {
                "id": "AwsSolutions-ECS2",
                "reason": "Task env vars hold only non-sensitive identifiers. Per-run values arrive via Step Functions containerOverrides.",
            },
            {
                "id": "AwsSolutions-OS1",
                "reason": "Observability domain is deliberately public-access so the OSIS pipeline (AWS-managed) can write without VPC peering. FGAC + IAM guard access.",
            },
            {
                "id": "AwsSolutions-OS3",
                "reason": "OSIS source IPs aren't stable; IP allowlisting incompatible. FGAC + SigV4 is the auth.",
            },
            {
                "id": "AwsSolutions-OS4",
                "reason": "Single data node by design (D1 small-dev profile); dedicated masters unnecessary.",
            },
            {
                "id": "AwsSolutions-OS7",
                "reason": "Zone-awareness requires even node count. Single-node domain by design for cost.",
            },
            {
                "id": "AwsSolutions-OS9",
                "reason": "Slow-log publishing doubles CW Logs cost; operators can query the domain directly.",
            },
            {
                "id": "AwsSolutions-APIG1",
                "reason": "HTTP API is single-operator IAM-auth only; CloudTrail + per-Lambda logs cover access trail.",
            },
            {
                "id": "AwsSolutions-APIG4",
                "reason": "All routes use AWS_IAM authorization; cdk-nag's pattern match requires an explicit authorizer resource which HTTP API doesn't model.",
            },
            {
                "id": "AwsSolutions-SNS3",
                "reason": "Email-only fan-out, no cross-account publishers; SSL enforcement blocks some SES subscription deliveries.",
            },
        ],
    )

app.synth()
