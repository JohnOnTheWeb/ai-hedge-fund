"""End-to-end synth test. Exercises both stacks with the two context flags off.

Does not deploy; just verifies the templates can be rendered.
"""
from __future__ import annotations

import os
import sys

import aws_cdk as cdk
from aws_cdk.assertions import Template

# Make the infra/ package importable when pytest runs from repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aspects.require_tag import RequireTag  # noqa: E402
from stacks.app_stack import AppStack  # noqa: E402
from stacks.platform_stack import PlatformStack  # noqa: E402


def test_synth_first_pass():
    """First-pass deploy: agentCoreEnabled/observabilityEnabled = false."""
    app = cdk.App(
        context={
            "aihedge:tag": "UsedBy",
            "aihedge:tagValue": "AIHedge",
            "aihedge:emailTo": "jotw@amazon.com",
            "aihedge:mdStorePrefix": "AIHedge",
            "aihedge:logRetentionDays": 7,
            "aihedge:repoOwner": "JohnOnTheWeb",
            "aihedge:repoName": "ai-hedge-fund",
            "aihedge:repoBranch": "main",
            "aihedge:models": {
                "deep": "anthropic.claude-opus-4-7-20251015-v1:0",
                "persona": "anthropic.claude-sonnet-4-5-20250929-v1:0",
                "analytical": "anthropic.claude-haiku-4-5-20251001",
            },
            "aihedge:modelAssignments": {},
        }
    )
    env = cdk.Environment(account="111122223333", region="us-east-1")

    platform = PlatformStack(app, "AIHedge-Platform-Stack", env=env)
    app_stack = AppStack(app, "AIHedge-App-Stack", env=env, platform=platform)
    app_stack.add_dependency(platform)

    cdk.Tags.of(app).add("UsedBy", "AIHedge")
    cdk.Aspects.of(app).add(RequireTag(key="UsedBy", value="AIHedge"))

    # Will raise MissingRequiredTagError if any taggable resource slips through.
    platform_template = Template.from_stack(platform)
    app_template = Template.from_stack(app_stack)

    # Platform has two ECR repos: aihedge-app (slim base for Runtime/Fargate)
    # and aihedge-lambda (public.ecr.aws/lambda/python base).
    platform_template.resource_count_is("AWS::ECR::Repository", 2)

    # First-pass app stack is empty (agentCoreEnabled default-false): no
    # Step Functions, no Lambdas — just the bootstrap-status output.
    app_template.resource_count_is("AWS::StepFunctions::StateMachine", 0)
