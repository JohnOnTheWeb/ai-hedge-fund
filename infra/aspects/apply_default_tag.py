"""Apply the mandatory UsedBy tag to every resource in the app."""
import jsii
from aws_cdk import IAspect, Tags
from constructs import IConstruct


@jsii.implements(IAspect)
class ApplyDefaultTag:
    """Adds `UsedBy=AIHedge` at the app root. Per-resource overrides still win."""

    def __init__(self, key: str, value: str) -> None:
        self._key = key
        self._value = value

    def visit(self, node: IConstruct) -> None:
        Tags.of(node).add(self._key, self._value)
