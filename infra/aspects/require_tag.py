"""Synth-time guard: fail the build if any taggable resource lacks UsedBy=AIHedge.

Runs after ApplyDefaultTag so the common path is clean; this aspect exists to
catch resources that explicitly strip the tag or use non-standard tag props.
"""
from __future__ import annotations

import jsii
from aws_cdk import CfnResource, IAspect
from constructs import IConstruct

# AWS resource types that don't support resource-level tagging. Adding the tag
# via Tags.of() is a no-op for these; we allow-list them instead of failing.
_UNTAGGABLE_TYPES: frozenset[str] = frozenset(
    {
        # AgentCore Gateway target — tags live on the parent gateway.
        "AWS::BedrockAgentCore::GatewayTarget",
        # EventBridge Scheduler — tags live on the scheduler role.
        "AWS::Scheduler::Schedule",
        # Lambda permissions / event source mappings inherit from function.
        "AWS::Lambda::Permission",
        "AWS::Lambda::EventSourceMapping",
        # Route53 records, IAM policy attachments etc.
        "AWS::IAM::Policy",
        "AWS::IAM::ManagedPolicy",
        # Service-linked roles are AWS-managed.
        "AWS::IAM::ServiceLinkedRole",
        # APIGW integrations/routes inherit from the API.
        "AWS::ApiGatewayV2::Integration",
        "AWS::ApiGatewayV2::Route",
        "AWS::ApiGatewayV2::Deployment",
        "AWS::ApiGatewayV2::Stage",
        # SSM parameters are taggable via properties only, not Tags.of().
        "AWS::SSM::Parameter",
        # S3 bucket policies, KMS key policies, etc.
        "AWS::S3::BucketPolicy",
        "AWS::KMS::Alias",
        # Step Fn activities (we don't use), CFN outputs, conditions.
        "AWS::CloudFormation::CustomResource",
        "Custom::AWS",
        "Custom::AWSCDK",
    }
)


class MissingRequiredTagError(Exception):
    """Raised at synth when a taggable resource is missing the mandatory tag."""


@jsii.implements(IAspect)
class RequireTag:
    """Fails synth if any taggable CfnResource lacks the required tag."""

    def __init__(self, key: str, value: str) -> None:
        self._key = key
        self._value = value

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, CfnResource):
            return
        if node.cfn_resource_type in _UNTAGGABLE_TYPES:
            return
        tags = node.tags.render_tags() if node.tags else None
        if not tags:
            raise MissingRequiredTagError(
                f"{node.node.path} ({node.cfn_resource_type}) has no tags; "
                f"expected {self._key}={self._value}"
            )
        for tag in tags:
            if tag.get("key") == self._key or tag.get("Key") == self._key:
                actual = tag.get("value") or tag.get("Value")
                if actual == self._value:
                    return
                raise MissingRequiredTagError(
                    f"{node.node.path} ({node.cfn_resource_type}) has "
                    f"{self._key}={actual}; expected {self._value}"
                )
        raise MissingRequiredTagError(
            f"{node.node.path} ({node.cfn_resource_type}) is missing "
            f"required tag {self._key}={self._value}"
        )
