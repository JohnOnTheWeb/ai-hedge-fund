"""L3 construct: AgentCore Runtime + Gateway + Lambda-backed MCP targets.

Wraps the raw `AWS::BedrockAgentCore::*` Cfn resources so app_stack stays
readable. Designed to no-op when `agentCoreEnabled=false` on first-pass deploy
(the caller skips instantiation — this construct assumes enablement).
"""
from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import CfnResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct


@dataclass(frozen=True)
class LambdaTargetSpec:
    """One Lambda-backed MCP target registered on the gateway."""

    target_name: str          # MCP target name, e.g. "data-tools"
    function_name: str        # Stable Lambda function name — used by buildspec rolls
    handler_cmd: list[str]    # container CMD — dotted handler path
    tool_schemas: list[dict]  # JSON schemas served as MCP tool catalog


# Lambda container images from public.ecr.aws/lambda/python:* ship with
# awslambdaric pre-installed as the default ENTRYPOINT. We don't override it;
# each function just sets CMD to the dotted handler path.


class AgentRuntimeBundle(Construct):
    """Runtime + Gateway + N Lambda targets sharing one container image."""

    def __init__(
        self,
        scope: Construct,
        id_: str,
        *,
        image_repo: ecr.IRepository,
        image_tag: str,
        image_digest: str,
        lambda_repo: ecr.IRepository,
        lambda_digest: str,
        runtime_role: iam.IRole,
        gateway_role: iam.IRole,
        lambda_targets: list[tuple[LambdaTargetSpec, iam.IRole]],
        runtime_env: dict[str, str],
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id_)

        stack = Stack.of(self)
        image_uri = f"{image_repo.repository_uri}:{image_tag}"

        # ------------------------------------------------------------------
        # Gateway FIRST — so the Runtime env can reference its URL as a CFN
        # token. The agents running inside the Runtime read AIHEDGE_GATEWAY_URL
        # to route every tool call through the Gateway.
        # ------------------------------------------------------------------
        self.gateway = CfnResource(
            self,
            "Gateway",
            type="AWS::BedrockAgentCore::Gateway",
            properties={
                "Name": "aihedge-gw",
                "RoleArn": gateway_role.role_arn,
                "ProtocolType": "MCP",
                "AuthorizerType": "AWS_IAM",
            },
        )
        self.gateway.apply_removal_policy(RemovalPolicy.DESTROY)

        self.gateway_id = self.gateway.get_att("GatewayIdentifier").to_string()
        self.gateway_url = self.gateway.get_att("GatewayUrl").to_string()

        # Build the final env for the Runtime: whatever the caller passed PLUS
        # the Gateway URL + region. Any empty/placeholder AIHEDGE_GATEWAY_URL
        # the caller provided is overwritten.
        runtime_env_final = {
            **{k: v for k, v in (runtime_env or {}).items() if k not in ("AIHEDGE_GATEWAY_URL", "AIHEDGE_GATEWAY_REGION")},
            "AIHEDGE_GATEWAY_URL": self.gateway_url,
            "AIHEDGE_GATEWAY_REGION": stack.region,
        }

        # ------------------------------------------------------------------
        # AgentCore Runtime — hosts FastAPI container with LangGraph pipeline.
        # ------------------------------------------------------------------
        self.runtime = CfnResource(
            self,
            "Runtime",
            type="AWS::BedrockAgentCore::Runtime",
            properties={
                "AgentRuntimeName": "aihedge_runtime",
                "RoleArn": runtime_role.role_arn,
                "AgentRuntimeArtifact": {
                    "ContainerConfiguration": {
                        "ContainerUri": image_uri,
                    },
                },
                "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
                "EnvironmentVariables": runtime_env_final,
                "ProtocolConfiguration": {"ServerProtocol": "HTTP"},
            },
        )
        self.runtime.apply_removal_policy(RemovalPolicy.DESTROY)
        self.runtime.add_dependency(self.gateway)

        self.target_functions: dict[str, lambda_.Function] = {}

        for spec, role in lambda_targets:
            fn = lambda_.Function(
                self,
                f"Lambda{spec.target_name.replace('-', '').title()}",
                function_name=spec.function_name,
                runtime=lambda_.Runtime.FROM_IMAGE,
                # Use the Lambda-specific image (public.ecr.aws/lambda/python base).
                # Generic slim bases fail every invocation with Runtime.InvalidEntrypoint
                # regardless of chmod (TauricResearch 2026-05-02).
                code=lambda_.Code.from_ecr_image(
                    repository=lambda_repo,
                    tag_or_digest=lambda_digest,
                    cmd=spec.handler_cmd,
                ),
                handler=lambda_.Handler.FROM_IMAGE,
                architecture=lambda_.Architecture.ARM_64,
                memory_size=1024,
                timeout=Duration.seconds(60),
                role=role,
                log_retention=log_retention,
                environment={
                    "AIHEDGE_IN_CLUSTER": "1",
                    "POWERTOOLS_SERVICE_NAME": spec.function_name,
                },
            )
            self.target_functions[spec.target_name] = fn

            gateway_role.add_to_principal_policy(
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[fn.function_arn, f"{fn.function_arn}:*"],
                )
            )

            CfnResource(
                self,
                f"Target{spec.target_name.replace('-', '').title()}",
                type="AWS::BedrockAgentCore::GatewayTarget",
                properties={
                    "GatewayIdentifier": self.gateway_id,
                    "Name": spec.target_name,
                    "TargetConfiguration": {
                        "Mcp": {
                            "Lambda": {
                                "LambdaArn": fn.function_arn,
                                "ToolSchema": {"InlinePayload": spec.tool_schemas},
                            }
                        }
                    },
                    "CredentialProviderConfigurations": [
                        {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                    ],
                },
            ).apply_removal_policy(RemovalPolicy.DESTROY)
