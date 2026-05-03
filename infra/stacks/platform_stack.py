"""Long-lived platform stack.

Creates the resources that outlive a single feature iteration: VPC, endpoints,
ECR, CodePipeline + CodeBuild, OpenSearch + AMP + OSIS, Secrets, KMS, AgentCore
Gateway shell (runtime + targets are built in app_stack once the image exists),
Config rule, and the permission boundary policy document.

OpenSearch / AMP / OSIS default to `RemovalPolicy.RETAIN` so historical traces
survive app-stack teardown. Pass `-c platformDestroy=true` on `cdk destroy` to
wipe them too.
"""
from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_aps as aps
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as codepipeline_actions
from aws_cdk import aws_codestarconnections as codestarconnections
from aws_cdk import aws_config as config_
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

from constructs.otel_pipeline import OtelPipeline

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
        # ECR — single shared image repo (Runtime + Fargate + 7 Lambdas).
        # ------------------------------------------------------------------
        self.image_repo = ecr.Repository(
            self,
            "AppRepo",
            repository_name="aihedge-app",
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

        # ------------------------------------------------------------------
        # S3 buckets — config (watchlist + per-run JSONs), CodePipeline artifacts.
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

        artifact_bucket = s3.Bucket(
            self,
            "ArtifactBucket",
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
        # CodePipeline — GitHub source → single CodeBuild → ECR push.
        # ------------------------------------------------------------------
        self.codestar_connection = codestarconnections.CfnConnection(
            self,
            "GitHubConnection",
            connection_name="aihedge-github",
            provider_type="GitHub",
            tags=[cdk.CfnTag(key="UsedBy", value="AIHedge")],
        )

        self.codebuild_project = codebuild.PipelineProject(
            self,
            "ImageBuild",
            project_name="aihedge-image-build",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                privileged=True,
                compute_type=codebuild.ComputeType.MEDIUM,
            ),
            environment_variables={
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(value=self.account),
                "AWS_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
                "ECR_REPOSITORY": codebuild.BuildEnvironmentVariable(value=self.image_repo.repository_name),
                "ECR_URI": codebuild.BuildEnvironmentVariable(value=self.image_repo.repository_uri),
                "IMAGE_SSM_URI": codebuild.BuildEnvironmentVariable(value="/aihedge/image/uri"),
                "IMAGE_SSM_DIGEST": codebuild.BuildEnvironmentVariable(value="/aihedge/image/digest"),
                "IMAGE_SSM_TAG": codebuild.BuildEnvironmentVariable(value="/aihedge/image/tag"),
            },
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec.yml"),
            timeout=Duration.minutes(30),
        )
        self.image_repo.grant_pull_push(self.codebuild_project)
        self.codebuild_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter", "ssm:GetParameter", "ssm:AddTagsToResource"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/aihedge/image/*",
                ],
            )
        )
        # Needed for the post_build step that rolls the AgentCore Runtime
        # after every new image push (see buildspec.yml).
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

        source_artifact = codepipeline.Artifact("Source")
        build_artifact = codepipeline.Artifact("Build")

        pipeline = codepipeline.Pipeline(
            self,
            "ImagePipeline",
            pipeline_name="aihedge-image-pipeline",
            artifact_bucket=artifact_bucket,
            stages=[
                codepipeline.StageProps(
                    stage_name="Source",
                    actions=[
                        codepipeline_actions.CodeStarConnectionsSourceAction(
                            action_name="GitHub",
                            owner=repo_owner,
                            repo=repo_name,
                            branch=repo_branch,
                            connection_arn=self.codestar_connection.attr_connection_arn,
                            output=source_artifact,
                            trigger_on_push=True,
                        )
                    ],
                ),
                codepipeline.StageProps(
                    stage_name="Build",
                    actions=[
                        codepipeline_actions.CodeBuildAction(
                            action_name="DockerBuild",
                            project=self.codebuild_project,
                            input=source_artifact,
                            outputs=[build_artifact],
                        )
                    ],
                ),
            ],
        )

        # ------------------------------------------------------------------
        # SSM parameters — image URI/digest/tag populated by CodeBuild. Seeded
        # with placeholders so app_stack can synth before the first build runs.
        # ------------------------------------------------------------------
        for name, initial in [
            ("/aihedge/image/uri", self.image_repo.repository_uri),
            ("/aihedge/image/tag", "placeholder"),
            ("/aihedge/image/digest", "placeholder"),
        ]:
            ssm.StringParameter(
                self,
                f"Param{name.split('/')[-1].title()}",
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
            self.osis_ingest_endpoint = otel.pipeline.attr_ingest_endpoint_urls

        # ------------------------------------------------------------------
        # Config rule — continuous tag drift detection.
        # Assumes an account-level Config recorder is already running.
        # ------------------------------------------------------------------
        tag_alarm_topic = sns.Topic(
            self,
            "TagDriftTopic",
            topic_name="aihedge-tag-drift",
            master_key=self.cmk,
        )
        if email_to:
            tag_alarm_topic.add_subscription(sns_subs.EmailSubscription(email_to))

        config_.ManagedRule(
            self,
            "RequiredTagsRule",
            identifier=config_.ManagedRuleIdentifiers.REQUIRED_TAGS,
            config_rule_name="aihedge-required-tags",
            input_parameters={
                "tag1Key": "UsedBy",
                "tag1Value": "AIHedge",
            },
            description="Every AI-HedgeFund resource must carry UsedBy=AIHedge",
        )

        # ------------------------------------------------------------------
        # Outputs consumed by app_stack.
        # ------------------------------------------------------------------
        CfnOutput(self, "ImageRepoUri", value=self.image_repo.repository_uri, export_name="AIHedgeImageRepoUri")
        CfnOutput(self, "ConfigBucketName", value=self.config_bucket.bucket_name, export_name="AIHedgeConfigBucket")
        CfnOutput(self, "VpcIdOut", value=self.vpc.vpc_id, export_name="AIHedgeVpcId")
        CfnOutput(self, "CodePipelineName", value=pipeline.pipeline_name, export_name="AIHedgeCodePipelineName")
        CfnOutput(self, "PermissionBoundaryArn", value=self.permission_boundary.managed_policy_arn, export_name="AIHedgePermissionBoundaryArn")
        if self.osis_ingest_endpoint:
            CfnOutput(self, "OsisIngestEndpointOut", value=self.osis_ingest_endpoint)
