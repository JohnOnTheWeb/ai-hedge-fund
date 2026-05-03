"""Long-lived platform stack.

Creates the resources that outlive a single feature iteration: VPC, endpoints,
ECR, CodeBuild (direct GitHub source), OpenSearch + AMP + OSIS, Secrets, KMS,
AgentCore Gateway shell (runtime + targets are built in app_stack once the
image exists), Config rule, and the permission boundary policy document.

OpenSearch / AMP / OSIS default to `RemovalPolicy.RETAIN` so historical traces
survive app-stack teardown. Pass `-c platformDestroy=true` on `cdk destroy` to
wipe them too.
"""
from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_aps as aps
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from aihedge_constructs.otel_pipeline import OtelPipeline

_POLICY_DOC = Path(__file__).resolve().parent.parent / "policies" / "required_tag_boundary.json"


class PlatformStack(Stack):
    """Foundation resources shared across iterations."""

    def __init__(self, scope: Construct, id_: str, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)

        log_retention_days = int(self.node.try_get_context("aihedge:logRetentionDays") or 7)
        log_retention = logs.RetentionDays.ONE_WEEK if log_retention_days == 7 else logs.RetentionDays.TWO_WEEKS

        platform_destroy = str(self.node.try_get_context("platformDestroy")).lower() == "true"
        data_removal = RemovalPolicy.DESTROY if platform_destroy else RemovalPolicy.RETAIN

        observability_enabled = str(self.node.try_get_context("observabilityEnabled")).lower() == "true"

        repo_owner = self.node.try_get_context("aihedge:repoOwner")
        repo_name = self.node.try_get_context("aihedge:repoName")
        repo_branch = self.node.try_get_context("aihedge:repoBranch")
        email_to = self.node.try_get_context("aihedge:emailTo")

        # ------------------------------------------------------------------
        # KMS CMK — envelope key for secrets, logs, S3, SNS.
        # ------------------------------------------------------------------
        self.cmk = kms.Key(
            self,
            "PlatformCmk",
            alias="alias/aihedge-platform",
            enable_key_rotation=True,
            removal_policy=data_removal,
            pending_window=Duration.days(7),
            description="AI-HedgeFund platform CMK",
        )

        # ------------------------------------------------------------------
        # VPC — 2 AZ, single NAT, private + public subnets.
        # ------------------------------------------------------------------
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="app", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22),
            ],
            flow_logs={
                "all": ec2.FlowLogOptions(
                    destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                        logs.LogGroup(
                            self,
                            "VpcFlowLogs",
                            retention=log_retention,
                            removal_policy=RemovalPolicy.DESTROY,
                        )
                    ),
                    traffic_type=ec2.FlowLogTrafficType.REJECT,
                )
            },
        )

        # Interface endpoints keep Bedrock / ECR / Logs / Secrets / STS traffic
        # off the NAT.
        for name, service in [
            ("Bedrock", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("BedrockAgentCore", ec2.InterfaceVpcEndpointAwsService("bedrock-agentcore")),
            ("EcrApi", ec2.InterfaceVpcEndpointAwsService.ECR),
            ("EcrDkr", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
            ("Logs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
            ("Secrets", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("Sts", ec2.InterfaceVpcEndpointAwsService.STS),
            ("States", ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS),
            ("Xray", ec2.InterfaceVpcEndpointAwsService.XRAY),
        ]:
            self.vpc.add_interface_endpoint(f"{name}Endpoint", service=service, private_dns_enabled=True)

        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        # ------------------------------------------------------------------
        # ECR — TWO repos. Lambda requires public.ecr.aws/lambda/python:* as
        # the base image; the agentcore image uses a slim base for smaller
        # container size and faster Runtime/Fargate cold starts.
        # ------------------------------------------------------------------
        def _repo(name: str, repo_id: str) -> ecr.Repository:
            return ecr.Repository(
                self,
                repo_id,
                repository_name=name,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                image_scan_on_push=True,
                encryption=ecr.RepositoryEncryption.KMS,
                encryption_key=self.cmk,
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_images=True,
                lifecycle_rules=[
                    ecr.LifecycleRule(
                        max_image_count=20,
                        rule_priority=1,
                        description="Keep last 20 images",
                    )
                ],
            )

        # AgentCore Runtime + Fargate driver (built from Dockerfile.agentcore).
        self.image_repo = _repo("aihedge-app", "AppRepo")
        # All 7 Lambda targets (built from Dockerfile.lambda).
        self.lambda_repo = _repo("aihedge-lambda", "LambdaRepo")

        # ------------------------------------------------------------------
        # S3 buckets — config (watchlist + per-run JSONs).
        # ------------------------------------------------------------------
        self.config_bucket = s3.Bucket(
            self,
            "ConfigBucket",
            bucket_name=f"aihedge-config-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
        )

        # ------------------------------------------------------------------
        # Secrets — FinancialDatasets API key, MD-Store bearer token.
        # Bedrock uses IAM task role; no bearer secret.
        # ------------------------------------------------------------------
        self.financial_datasets_secret = secretsmanager.Secret(
            self,
            "FinancialDatasetsSecret",
            secret_name="aihedge/financial-datasets",
            description="FinancialDatasets.ai API key (seeded via scripts/seed_secrets.py)",
            encryption_key=self.cmk,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.md_store_secret = secretsmanager.Secret(
            self,
            "MdStoreSecret",
            secret_name="aihedge/md-store-token",
            description="MD-Store bearer token for AIHedge/ reports",
            encryption_key=self.cmk,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # Permission boundary — denies creates/untags without UsedBy=AIHedge.
        # ------------------------------------------------------------------
        boundary_doc = json.loads(_POLICY_DOC.read_text())
        self.permission_boundary = iam.ManagedPolicy(
            self,
            "PermissionBoundary",
            managed_policy_name="AIHedgePermissionBoundary",
            description="Deny create/untag actions that lack UsedBy=AIHedge",
            document=iam.PolicyDocument.from_json(boundary_doc),
        )

        # ------------------------------------------------------------------
        # CodeBuild — standalone project with a direct GitHub source.
        #
        # No CodePipeline / CodeConnections wrapper: that path requires a
        # one-time console OAuth approval (PENDING connection). Standalone
        # CodeBuild clones GitHub over HTTPS with a webhook-registered token
        # on the build project, so `aws codebuild start-build` is the only
        # trigger we need.
        # ------------------------------------------------------------------

        # Names of the Lambda functions the app_stack creates (stable, known
        # at platform-stack synth time). buildspec rolls each by name after
        # the Lambda image is pushed, because CloudFormation doesn't detect
        # container-image moves on a tag — TauricResearch incident 2026-05-02.
        lambda_function_names = ",".join([
            "aihedge-data-tools",
            "aihedge-memory-log",
            "aihedge-options-tools",
            "aihedge-get-config",
            "aihedge-aggregate",
            "aihedge-error-handler",
            "aihedge-run-trigger",
            "aihedge-run-status",
        ])

        github_source = codebuild.Source.git_hub(
            owner=repo_owner,
            repo=repo_name,
            branch_or_ref=repo_branch,
            clone_depth=1,
            webhook=False,
        )

        self.codebuild_project = codebuild.Project(
            self,
            "ImageBuild",
            project_name="aihedge-image-build",
            source=github_source,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                privileged=True,
                compute_type=codebuild.ComputeType.MEDIUM,
            ),
            environment_variables={
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(value=self.account),
                "AWS_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
                # AgentCore + Fargate image
                "ECR_REPOSITORY_APP": codebuild.BuildEnvironmentVariable(value=self.image_repo.repository_name),
                "ECR_URI_APP": codebuild.BuildEnvironmentVariable(value=self.image_repo.repository_uri),
                "IMAGE_SSM_APP_URI": codebuild.BuildEnvironmentVariable(value="/aihedge/image/app/uri"),
                "IMAGE_SSM_APP_TAG": codebuild.BuildEnvironmentVariable(value="/aihedge/image/app/tag"),
                "IMAGE_SSM_APP_DIGEST": codebuild.BuildEnvironmentVariable(value="/aihedge/image/app/digest"),
                # Lambda image
                "ECR_REPOSITORY_LAMBDA": codebuild.BuildEnvironmentVariable(value=self.lambda_repo.repository_name),
                "ECR_URI_LAMBDA": codebuild.BuildEnvironmentVariable(value=self.lambda_repo.repository_uri),
                "IMAGE_SSM_LAMBDA_URI": codebuild.BuildEnvironmentVariable(value="/aihedge/image/lambda/uri"),
                "IMAGE_SSM_LAMBDA_TAG": codebuild.BuildEnvironmentVariable(value="/aihedge/image/lambda/tag"),
                "IMAGE_SSM_LAMBDA_DIGEST": codebuild.BuildEnvironmentVariable(value="/aihedge/image/lambda/digest"),
                # Comma-separated list of Lambdas to roll post-push
                "LAMBDA_FUNCTION_NAMES": codebuild.BuildEnvironmentVariable(value=lambda_function_names),
            },
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec.yml"),
            timeout=Duration.minutes(30),
        )
        self.image_repo.grant_pull_push(self.codebuild_project)
        self.lambda_repo.grant_pull_push(self.codebuild_project)
        # buildspec's post_build resolves pushed digests via `ecr describe-images`
        # so it can update SSM params + roll AgentCore Runtime / Lambdas.
        self.codebuild_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecr:DescribeImages"],
                resources=[self.image_repo.repository_arn, self.lambda_repo.repository_arn],
            )
        )
        self.codebuild_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter", "ssm:GetParameter", "ssm:AddTagsToResource"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/aihedge/image/*",
                ],
            )
        )
        # Roll AgentCore Runtime on each push.
        self.codebuild_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore-control:ListAgentRuntimes",
                    "bedrock-agentcore-control:UpdateAgentRuntime",
                    "bedrock-agentcore-control:GetAgentRuntime",
                ],
                resources=["*"],
            )
        )
        # Roll Lambda code on each push.
        self.codebuild_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:UpdateFunctionCode",
                    "lambda:GetFunction",
                ],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:aihedge-*"],
            )
        )

        # ------------------------------------------------------------------
        # SSM parameters — image URI/digest/tag populated by CodeBuild. Seeded
        # with placeholders so app_stack can synth before the first build runs.
        # ------------------------------------------------------------------
        for name, initial in [
            ("/aihedge/image/app/uri", self.image_repo.repository_uri),
            ("/aihedge/image/app/tag", "placeholder"),
            ("/aihedge/image/app/digest", "placeholder"),
            ("/aihedge/image/lambda/uri", self.lambda_repo.repository_uri),
            ("/aihedge/image/lambda/tag", "placeholder"),
            ("/aihedge/image/lambda/digest", "placeholder"),
        ]:
            # Turn "/aihedge/image/app/tag" -> "ParamImageAppTag" for a unique construct ID.
            construct_id = "Param" + "".join(p.title() for p in name.strip("/").split("/")[1:])
            ssm.StringParameter(
                self,
                construct_id,
                parameter_name=name,
                string_value=initial,
                description="Set by CodeBuild post_build phase",
                tier=ssm.ParameterTier.STANDARD,
            )

        # ------------------------------------------------------------------
        # Observability — OpenSearch domain, AMP workspace, OSIS pipeline.
        # Gated on observabilityEnabled context flag (default false) so the
        # cost of the OSIS minimum-OCU doesn't kick in until opt-in.
        # ------------------------------------------------------------------
        self.opensearch_domain: opensearch.Domain | None = None
        self.amp_workspace: aps.CfnWorkspace | None = None
        self.osis_pipeline_arn: str | None = None
        self.osis_ingest_endpoint: str | None = None

        if observability_enabled:
            self.opensearch_master_secret = secretsmanager.Secret(
                self,
                "OpenSearchMasterSecret",
                secret_name="aihedge/opensearch-master",
                description="OpenSearch fine-grained-access master user",
                encryption_key=self.cmk,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template='{"username":"aihedge-admin"}',
                    generate_string_key="password",
                    exclude_characters="/@\"'\\",
                    password_length=24,
                ),
                removal_policy=RemovalPolicy.RETAIN,
            )

            self.opensearch_domain = opensearch.Domain(
                self,
                "ObservabilityDomain",
                version=opensearch.EngineVersion.OPENSEARCH_2_11,
                domain_name="aihedge-observability",
                capacity=opensearch.CapacityConfig(
                    data_nodes=1,
                    data_node_instance_type="t3.small.search",
                    multi_az_with_standby_enabled=False,
                ),
                ebs=opensearch.EbsOptions(
                    enabled=True,
                    volume_size=20,
                    volume_type=ec2.EbsDeviceVolumeType.GP3,
                ),
                encryption_at_rest=opensearch.EncryptionAtRestOptions(
                    enabled=True,
                    kms_key=self.cmk,
                ),
                node_to_node_encryption=True,
                enforce_https=True,
                fine_grained_access_control=opensearch.AdvancedSecurityOptions(
                    master_user_name="aihedge-admin",
                    master_user_password=self.opensearch_master_secret.secret_value_from_json("password"),
                ),
                # AwsSolutions-OS5: require SigV4 / FGAC; no anonymous access.
                # Permits any signed request from this account; FGAC then enforces
                # per-role/user authorization via the OpenSearch security plugin.
                access_policies=[
                    iam.PolicyStatement(
                        actions=["es:ESHttp*"],
                        principals=[iam.AccountRootPrincipal()],
                        resources=["*"],
                    )
                ],
                removal_policy=data_removal,
            )

            self.amp_workspace = aps.CfnWorkspace(
                self,
                "AmpWorkspace",
                alias="aihedge-metrics",
                tags=[cdk.CfnTag(key="UsedBy", value="AIHedge")],
            )

            otel = OtelPipeline(
                self,
                "OtelPipeline",
                domain=self.opensearch_domain,
                amp_workspace_arn=self.amp_workspace.attr_arn,
                amp_remote_write_url=f"https://aps-workspaces.{self.region}.amazonaws.com/workspaces/{self.amp_workspace.attr_workspace_id}/api/v1/remote_write",
                log_retention=log_retention,
            )
            self.osis_pipeline_arn = otel.pipeline.attr_pipeline_arn
            # attr_ingest_endpoint_urls is a list token; take the first entry.
            self.osis_ingest_endpoint = cdk.Fn.select(0, otel.pipeline.attr_ingest_endpoint_urls)

        # ------------------------------------------------------------------
        # Tag drift notifications — SNS topic retained as an email hook for
        # any future drift alerting. The AWS Config rule that previously fed
        # this topic was removed: CfnConfigurationRecorder does not reach
        # CREATE_COMPLETE in CloudFormation until `recording=true`, which in
        # turn can't happen until a DeliveryChannel exists — producing a
        # chicken-and-egg that stalls the stack for hours. Tag enforcement
        # remains two-layered: (1) CDK Tags.of(app).add aspect at synth,
        # (2) IAM permission boundary at request time.
        # ------------------------------------------------------------------
        tag_alarm_topic = sns.Topic(
            self,
            "TagDriftTopic",
            topic_name="aihedge-tag-drift",
            master_key=self.cmk,
        )
        if email_to:
            tag_alarm_topic.add_subscription(sns_subs.EmailSubscription(email_to))

        # ------------------------------------------------------------------
        # Outputs consumed by app_stack.
        # ------------------------------------------------------------------
        CfnOutput(self, "ImageRepoUri", value=self.image_repo.repository_uri, export_name="AIHedgeImageRepoUri")
        CfnOutput(self, "LambdaRepoUri", value=self.lambda_repo.repository_uri, export_name="AIHedgeLambdaRepoUri")
        CfnOutput(self, "ConfigBucketName", value=self.config_bucket.bucket_name, export_name="AIHedgeConfigBucket")
        CfnOutput(self, "VpcIdOut", value=self.vpc.vpc_id, export_name="AIHedgeVpcId")
        CfnOutput(self, "CodeBuildProjectName", value=self.codebuild_project.project_name, export_name="AIHedgeCodeBuildProjectName")
        CfnOutput(self, "PermissionBoundaryArn", value=self.permission_boundary.managed_policy_arn, export_name="AIHedgePermissionBoundaryArn")
        if self.osis_ingest_endpoint:
            CfnOutput(self, "OsisIngestEndpointOut", value=self.osis_ingest_endpoint)
