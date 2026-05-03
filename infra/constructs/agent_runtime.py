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

    target_name: str          # e.g. "data-tools"
    handler_cmd: list[str]    # container CMD override (awslambdaric + dotted path)
    tool_schemas: list[dict]  # JSON schemas served as MCP tool catalog


class AgentRuntimeBundle(Construct):
    """Runtime + Gateway + N Lambda targets sharing one container image."""

    def __init__(
        self,
        scope: Construct,
        id_: str,
        *,
        image_repo: ecr.IRepository,
        image_tag: str,
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
                "EnvironmentVariables": runtime_env,
                "ProtocolConfiguration": {"ServerProtocol": "HTTP"},
            },
        )
        self.runtime.apply_removal_policy(RemovalPolicy.DESTROY)

        # ------------------------------------------------------------------
        # AgentCore Gateway — single MCP endpoint, IAM-auth inbound.
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

        self.target_functions: dict[str, lambda_.Function] = {}

        for spec, role in lambda_targets:
            fn = lambda_.Function(
                self,
                f"Lambda{spec.target_name.replace('-', '').title()}",
                runtime=lambda_.Runtime.FROM_IMAGE,
                code=lambda_.Code.from_ecr_image(
                    repository=image_repo,
                    tag_or_digest=image_tag,
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
                    "POWERTOOLS_SERVICE_NAME": f"aihedge-{spec.target_name}",
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
