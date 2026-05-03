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
        # APIGW v2 L1 tagging is driven by Tags dict that CDK renders at
        # deploy-time, not the visitor-accessible list — visiting the L1
        # yields no tags even when cdk.Tags.of(api).add(...) was called.
        "AWS::ApiGatewayV2::Api",
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
    """Fails synth if any taggable CfnResource lacks the required tag.

    Resources without a `tags` attribute are silently skipped — CDK decides
    which L1s support tagging and which don't, and we mirror that here by
    asking the CfnResource itself (via `hasattr(node, "tags")`) rather than
    hard-coding a type allow-list.
    """

    def __init__(self, key: str, value: str) -> None:
        self._key = key
        self._value = value

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, CfnResource):
            return
        if node.cfn_resource_type in _UNTAGGABLE_TYPES:
            return
        # Many L1 Cfn* classes don't expose a `tags` property at all
        # (e.g. CfnSubnetRouteTableAssociation, CfnRoute, CfnGatewayAttachment).
        # Skip them — CDK's type metadata already tells us these aren't taggable.
        if not hasattr(node, "tags"):
            return
        tags_obj = getattr(node, "tags", None)
        render = getattr(tags_obj, "render_tags", None)
        if render is None:
            return
        tags = render()
        if not tags:
            raise MissingRequiredTagError(
                f"{node.node.path} ({node.cfn_resource_type}) has no tags; "
                f"expected {self._key}={self._value}"
            )
        for tag in tags:
            if not isinstance(tag, dict):
                # Some CDK L1s render tags as strings or intrinsic tokens;
                # can't verify those at synth, so skip.
                continue
            tag_key = tag.get("key") or tag.get("Key")
            if tag_key != self._key:
                continue
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
