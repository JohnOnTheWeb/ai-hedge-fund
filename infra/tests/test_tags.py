"""Synth-time tag enforcement tests.

Verifies the RequireTag aspect fails when a taggable resource is missing
UsedBy=AIHedge, and passes when ApplyDefaultTag has been applied at the root.
"""
from __future__ import annotations

import pytest
import aws_cdk as cdk
from aws_cdk import aws_s3 as s3

from aspects.apply_default_tag import ApplyDefaultTag
from aspects.require_tag import MissingRequiredTagError, RequireTag


def _mini_stack_with_bucket() -> cdk.App:
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack")
    s3.Bucket(stack, "B")
    return app


def test_require_tag_passes_when_default_applied():
    app = _mini_stack_with_bucket()
    cdk.Aspects.of(app).add(ApplyDefaultTag(key="UsedBy", value="AIHedge"))
    cdk.Aspects.of(app).add(RequireTag(key="UsedBy", value="AIHedge"))
    # Triggering synth executes aspects; should not raise.
    app.synth()


def test_require_tag_fails_when_no_tag():
    app = _mini_stack_with_bucket()
    cdk.Aspects.of(app).add(RequireTag(key="UsedBy", value="AIHedge"))
    with pytest.raises(MissingRequiredTagError):
        app.synth()


def test_require_tag_fails_when_wrong_value():
    app = cdk.App()
    stack = cdk.Stack(app, "TestStack")
    bucket = s3.Bucket(stack, "B")
    cdk.Tags.of(bucket).add("UsedBy", "SomethingElse")
    cdk.Aspects.of(app).add(RequireTag(key="UsedBy", value="AIHedge"))
    with pytest.raises(MissingRequiredTagError):
        app.synth()
