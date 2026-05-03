"""Push-button app stack.

Everything provisioned here is `RemovalPolicy.DESTROY`. `cdk destroy` on this
stack wipes the Runtime, Gateway, Fargate cluster, Step Fn, Lambdas, APIGW,
EventBridge, DynamoDB memory log, and SNS topic without touching the platform
stack or the Secrets Manager entries.
"""
from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as apigw_authz
from aws_cdk import aws_apigatewayv2_integrations as apigw_int
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks
from constructs import Construct

from aihedge_constructs.agent_runtime import AgentRuntimeBundle, LambdaTargetSpec
from stacks.platform_stack import PlatformStack

_STATE_MACHINE_DEF = Path(__file__).resolve().parent.parent.parent / "statemachine" / "aihedge_run.asl.json"


class AppStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id_: str,
        *,
        platform: PlatformStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, id_, **kwargs)

        log_retention_days = int(self.node.try_get_context("aihedge:logRetentionDays") or 7)
        log_retention = logs.RetentionDays.ONE_WEEK if log_retention_days == 7 else logs.RetentionDays.TWO_WEEKS

        agent_core_enabled = str(self.node.try_get_context("agentCoreEnabled")).lower() == "true"
        observability_enabled = str(self.node.try_get_context("observabilityEnabled")).lower() == "true"

        # First-pass bootstrap: only the platform stack exists, no image in ECR
        # yet, and the /aihedge/image/digest SSM parameter is still the
        # placeholder string. Every container-image Lambda + Fargate task in
        # this stack would reject that at synth. We short-circuit instead, so
        # `cdk deploy AIHedge-App-Stack -c agentCoreEnabled=false` produces an
        # empty stack. After CodeBuild pushes the first real image, redeploy
        # with `-c agentCoreEnabled=true` to fill in Runtime/Lambdas/Step Fn.
        if not agent_core_enabled:
            self._emit_placeholder_output()
            return

        email_to = self.node.try_get_context("aihedge:emailTo")
        md_store_prefix = self.node.try_get_context("aihedge:mdStorePrefix") or "AIHedge"
        md_store_url = self.node.try_get_context("aihedge:mdStoreUrl") or ""
        models = self.node.try_get_context("aihedge:models") or {}
        assignments = self.node.try_get_context("aihedge:modelAssignments") or {}

        # Build the 3 inference-profile ARN patterns Bedrock Converse uses.
        def _inference_profile_arn(model_id: str) -> str:
            return f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/us.{model_id}"

        allowed_bedrock_model_arns = [
            f"arn:aws:bedrock:{self.region}::foundation-model/{m}"
            for m in models.values()
        ] + [_inference_profile_arn(m) for m in models.values()]

        # ------------------------------------------------------------------
        # DynamoDB memory log — Portfolio Manager past-context.
        # ------------------------------------------------------------------
        self.memory_table = dynamodb.Table(
            self,
            "MemoryLogTable",
            table_name="aihedge-memory-log",
            partition_key=dynamodb.Attribute(name="ticker", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="trade_date_run", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # SNS — run summary emails.
        # ------------------------------------------------------------------
        self.summary_topic = sns.Topic(
            self,
            "RunSummaryTopic",
            topic_name="aihedge-run-summary",
            master_key=platform.cmk,
        )
        if email_to:
            self.summary_topic.add_subscription(sns_subs.EmailSubscription(email_to))

        # ------------------------------------------------------------------
        # IAM roles — one per execution context.
        # ------------------------------------------------------------------
        runtime_role = iam.Role(
            self,
            "RuntimeExecRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
            description="AgentCore Runtime execution role",
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                    "bedrock:GetInferenceProfile",
                ],
                resources=allowed_bedrock_model_arns,
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"],
            )
        )
        # Avoid cross-stack grants (they create platform→app resource-policy
        # references that cycle back). Add policies to the role directly; the
        # resource side doesn't need to know about us.
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[platform.md_store_secret.secret_arn],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:GetAuthorizationToken",
                ],
                resources=[platform.image_repo.repository_arn, "*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"],
            )
        )

        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
            description="AgentCore Gateway role — invokes target Lambdas",
        )

        lambda_data_role = iam.Role(
            self,
            "LambdaDataRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        lambda_data_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[platform.financial_datasets_secret.secret_arn],
            )
        )

        lambda_memory_role = iam.Role(
            self,
            "LambdaMemoryRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        self.memory_table.grant_read_write_data(lambda_memory_role)

        # ------------------------------------------------------------------
        # Runtime + Gateway + Targets (gated on agentCoreEnabled).
        # ------------------------------------------------------------------
        # Read image coordinates from SSM at deploy time (CFN tokens, not
        # synth-time lookups). CodeBuild populates these in its post_build.
        # Synth succeeds even before the first build; the Lambda container
        # images just won't resolve until deploy time.
        # AgentCore + Fargate image (slim python base).
        image_tag = ssm.StringParameter.value_for_string_parameter(self, "/aihedge/image/app/tag")
        image_digest = ssm.StringParameter.value_for_string_parameter(self, "/aihedge/image/app/digest")
        # Lambda image (public.ecr.aws/lambda/python base — required by Lambda runtime).
        lambda_tag = ssm.StringParameter.value_for_string_parameter(self, "/aihedge/image/lambda/tag")
        lambda_digest = ssm.StringParameter.value_for_string_parameter(self, "/aihedge/image/lambda/digest")
        osis_endpoint_param = platform.osis_ingest_endpoint if observability_enabled and platform.osis_ingest_endpoint else ""

        runtime_env = {
            "AIHEDGE_IN_CLUSTER": "1",
            "AIHEDGE_MD_STORE_SECRET_ID": platform.md_store_secret.secret_name,
            "AIHEDGE_MD_STORE_PREFIX": md_store_prefix,
            "AIHEDGE_MD_STORE_URL": md_store_url,
            "AIHEDGE_MEMORY_TABLE": self.memory_table.table_name,
            "AIHEDGE_MODEL_MAP_JSON": json.dumps({k: models[v] for k, v in assignments.items() if v in models}),
            # AIHEDGE_GATEWAY_URL / _REGION are injected by AgentRuntimeBundle
            # after the Gateway is created — don't set them here.
            "AWS_DEFAULT_REGION": self.region,
        }
        if osis_endpoint_param:
            runtime_env.update(
                {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": osis_endpoint_param,
                    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                    "OTEL_SERVICE_NAME": "aihedge-runtime",
                    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=prod,service.namespace=aihedge",
                    "AIH_OTEL_SIGV4": "1",
                }
            )

        data_tools_spec = LambdaTargetSpec(
            target_name="data-tools",
            function_name="aihedge-data-tools",
            handler_cmd=["deploy.app.lambdas.data_tools.handler.handler"],
            tool_schemas=_data_tool_schemas(),
        )
        memory_log_spec = LambdaTargetSpec(
            target_name="memory-log",
            function_name="aihedge-memory-log",
            handler_cmd=["deploy.app.lambdas.memory_log.handler.handler"],
            tool_schemas=_memory_tool_schemas(),
        )

        self.bundle: AgentRuntimeBundle | None = None
        if agent_core_enabled:
            self.bundle = AgentRuntimeBundle(
                self,
                "AgentCore",
                # AgentCore Runtime image (slim python, Dockerfile.agentcore)
                image_repo=platform.image_repo,
                image_tag=image_tag,  # Runtime uses tag; post-build update-agent-runtime forces re-pull
                image_digest=image_digest,
                # Lambda target image (public.ecr.aws/lambda/python, Dockerfile.lambda)
                lambda_repo=platform.lambda_repo,
                lambda_digest=lambda_digest,
                runtime_role=runtime_role,
                gateway_role=gateway_role,
                lambda_targets=[
                    (data_tools_spec, lambda_data_role),
                    (memory_log_spec, lambda_memory_role),
                ],
                runtime_env=runtime_env,
                log_retention=log_retention,
            )
            self.memory_table.grant_read_write_data(self.bundle.target_functions["memory-log"])
            # role-side policy already granted via lambda_data_role above; nothing extra needed

        # ------------------------------------------------------------------
        # Fargate cluster + task def — analyst-driver per ticker.
        # ------------------------------------------------------------------
        cluster = ecs.Cluster(
            self,
            "FargateCluster",
            vpc=platform.vpc,
            cluster_name="aihedge",
            container_insights=False,
        )

        task_role = iam.Role(
            self,
            "FargateTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"],
                resources=[platform.config_bucket.bucket_arn, f"{platform.config_bucket.bucket_arn}/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:Converse",
                ],
                resources=allowed_bedrock_model_arns,
            )
        )

        task_def = ecs.FargateTaskDefinition(
            self,
            "AnalystDriverTaskDef",
            cpu=1024,
            memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_role=task_role,
        )
        task_def.add_container(
            "driver",
            image=ecs.ContainerImage.from_ecr_repository(platform.image_repo, image_digest),
            command=["python", "-m", "deploy.app.task_runner"],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="analyst-driver",
                log_retention=log_retention,
            ),
            environment={
                "AIHEDGE_IN_CLUSTER": "1",
                "AIHEDGE_CONFIG_BUCKET": platform.config_bucket.bucket_name,
                "AWS_DEFAULT_REGION": self.region,
                **({"OTEL_EXPORTER_OTLP_ENDPOINT": osis_endpoint_param, "OTEL_SERVICE_NAME": "aihedge-driver"} if osis_endpoint_param else {}),
            },
        )

        # ------------------------------------------------------------------
        # Glue Lambdas — get-config, aggregate, run-trigger, run-status, error.
        # All pull from the dedicated Lambda image repo (public.ecr.aws/lambda
        # base); each differs only by CMD (the dotted handler path). Explicit
        # function_name keeps the buildspec post-build `update-function-code`
        # roll stable across redeploys.
        # ------------------------------------------------------------------
        def _lambda(
            construct_id: str,
            function_name: str,
            handler_path: str,
            role: iam.IRole,
            env: dict | None = None,
        ) -> lambda_.Function:
            return lambda_.Function(
                self,
                construct_id,
                function_name=function_name,
                runtime=lambda_.Runtime.FROM_IMAGE,
                code=lambda_.Code.from_ecr_image(
                    repository=platform.lambda_repo,
                    tag_or_digest=lambda_digest,
                    cmd=[handler_path],
                ),
                handler=lambda_.Handler.FROM_IMAGE,
                architecture=lambda_.Architecture.ARM_64,
                memory_size=512,
                timeout=Duration.seconds(60),
                role=role,
                log_retention=log_retention,
                environment={"AIHEDGE_IN_CLUSTER": "1", **(env or {})},
            )

        glue_role = iam.Role(
            self,
            "GlueLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        glue_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"],
                resources=[platform.config_bucket.bucket_arn, f"{platform.config_bucket.bucket_arn}/*"],
            )
        )
        self.summary_topic.grant_publish(glue_role)

        get_config_fn = _lambda(
            "GetConfig",
            "aihedge-get-config",
            "deploy.app.lambdas.get_config.handler.handler",
            glue_role,
            {"AIHEDGE_CONFIG_BUCKET": platform.config_bucket.bucket_name},
        )
        aggregate_fn = _lambda(
            "Aggregate",
            "aihedge-aggregate",
            "deploy.app.lambdas.aggregate.handler.handler",
            glue_role,
            {
                "AIHEDGE_CONFIG_BUCKET": platform.config_bucket.bucket_name,
                "AIHEDGE_SUMMARY_TOPIC_ARN": self.summary_topic.topic_arn,
            },
        )
        error_fn = _lambda(
            "ErrorHandler",
            "aihedge-error-handler",
            "deploy.app.lambdas.error_handler.handler.handler",
            glue_role,
            {"AIHEDGE_SUMMARY_TOPIC_ARN": self.summary_topic.topic_arn},
        )

        # run-trigger / run-status need Step Fn permissions — separate role.
        sfn_caller_role = iam.Role(
            self,
            "SfnCallerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        run_trigger_fn = _lambda(
            "RunTrigger",
            "aihedge-run-trigger",
            "deploy.app.lambdas.run_trigger.handler.handler",
            sfn_caller_role,
        )
        run_status_fn = _lambda(
            "RunStatus",
            "aihedge-run-status",
            "deploy.app.lambdas.run_status.handler.handler",
            sfn_caller_role,
        )

        # ------------------------------------------------------------------
        # Step Functions state machine.
        # ------------------------------------------------------------------
        state_machine_role = iam.Role(
            self,
            "StateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
        )

        sfn_log_group = logs.LogGroup(
            self,
            "StateMachineLogs",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )

        definition_substitutions = {
            "GetConfigArn": get_config_fn.function_arn,
            "AggregateArn": aggregate_fn.function_arn,
            "ErrorHandlerArn": error_fn.function_arn,
            "ClusterArn": cluster.cluster_arn,
            "TaskDefArn": task_def.task_definition_arn,
            "SubnetList": ",".join(s.subnet_id for s in platform.vpc.private_subnets),
            "TaskRoleArn": task_role.role_arn,
            "ExecutionRoleArn": task_def.execution_role.role_arn if task_def.execution_role else "",
            "ContainerName": "driver",
            "RuntimeArn": self.bundle.runtime.get_att("AgentRuntimeArn").to_string() if self.bundle else "arn:aws:bedrock-agentcore:pending",
            "SummaryTopicArn": self.summary_topic.topic_arn,
        }

        state_machine_body = _STATE_MACHINE_DEF.read_text()

        state_machine = sfn.CfnStateMachine(
            self,
            "StateMachine",
            state_machine_name="AIHedge-Run",
            role_arn=state_machine_role.role_arn,
            definition_string=state_machine_body,
            definition_substitutions=definition_substitutions,
            state_machine_type="STANDARD",
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                            log_group_arn=sfn_log_group.log_group_arn
                        )
                    )
                ],
                include_execution_data=True,
                level="ERROR",
            ),
            tracing_configuration=sfn.CfnStateMachine.TracingConfigurationProperty(enabled=True),
            tags=[sfn.CfnStateMachine.TagsEntryProperty(key="UsedBy", value="AIHedge")],
        )

        # State Machine perms
        get_config_fn.grant_invoke(state_machine_role)
        aggregate_fn.grant_invoke(state_machine_role)
        error_fn.grant_invoke(state_machine_role)
        state_machine_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask", "ecs:DescribeTasks", "ecs:StopTask"],
                resources=[task_def.task_definition_arn],
                conditions={"ArnEquals": {"ecs:cluster": cluster.cluster_arn}},
            )
        )
        state_machine_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[task_role.role_arn, task_def.execution_role.role_arn if task_def.execution_role else "*"],
            )
        )
        state_machine_role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
                resources=[f"arn:aws:events:{self.region}:{self.account}:rule/StepFunctionsGetEventsForECSTaskRule"],
            )
        )
        state_machine_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[self.summary_topic.topic_arn],
            )
        )

        state_machine_arn = f"arn:{self.partition}:states:{self.region}:{self.account}:stateMachine:AIHedge-Run"
        run_trigger_fn.add_environment("AIHEDGE_STATE_MACHINE_ARN", state_machine_arn)
        run_status_fn.add_environment("AIHEDGE_STATE_MACHINE_ARN", state_machine_arn)
        sfn_caller_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution", "states:DescribeExecution", "states:ListExecutions"],
                resources=[state_machine_arn, f"arn:aws:states:{self.region}:{self.account}:execution:AIHedge-Run:*"],
            )
        )

        # ------------------------------------------------------------------
        # API Gateway HTTP API — IAM auth.
        # ------------------------------------------------------------------
        http_api = apigw.HttpApi(
            self,
            "WebApi",
            api_name="aihedge-web",
            description="IAM-auth entry points for AI-HedgeFund runs",
        )
        # HttpApi's CfnApi L1 doesn't inherit app-level tags; set explicitly.
        cdk.Tags.of(http_api).add("UsedBy", "AIHedge")
        iam_authorizer = apigw_authz.HttpIamAuthorizer()
        http_api.add_routes(
            path="/runs",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_int.HttpLambdaIntegration("RunTriggerInt", run_trigger_fn),
            authorizer=iam_authorizer,
        )
        http_api.add_routes(
            path="/runs/{runId}",
            methods=[apigw.HttpMethod.GET],
            integration=apigw_int.HttpLambdaIntegration("RunStatusInt", run_status_fn),
            authorizer=iam_authorizer,
        )

        # ------------------------------------------------------------------
        # EventBridge daily cron (21:30 UTC Mon–Fri).
        # ------------------------------------------------------------------
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            permissions_boundary=platform.permission_boundary,
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[state_machine_arn],
            )
        )

        scheduler.CfnSchedule(
            self,
            "DailyRun",
            name="aihedge-daily",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            schedule_expression="cron(30 21 ? * MON-FRI *)",
            schedule_expression_timezone="UTC",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=state_machine_arn,
                role_arn=scheduler_role.role_arn,
                input=json.dumps(
                    {
                        "trigger": "scheduled",
                        "tickers_key": "watchlist.json",
                    }
                ),
            ),
        )

        CfnOutput(self, "StateMachineArnOut", value=state_machine_arn)
        CfnOutput(self, "HttpApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "MemoryTableName", value=self.memory_table.table_name)

    def _emit_placeholder_output(self) -> None:
        """Produce a stand-in CFN output so the stack isn't empty on first pass."""
        CfnOutput(
            self,
            "BootstrapStatus",
            value="agentCoreEnabled=false — redeploy with -c agentCoreEnabled=true after CodeBuild produces the first image",
        )


def _data_tool_schemas() -> list[dict]:
    """MCP tool schemas served by the data-tools Lambda target."""
    ticker_prop = {"type": "string", "description": "Stock ticker (e.g. AAPL)"}
    end_date_prop = {"type": "string", "description": "ISO date (YYYY-MM-DD)"}

    return [
        {
            "name": "get_prices",
            "description": "Return OHLCV prices for the ticker between start_date and end_date.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": ticker_prop,
                    "start_date": end_date_prop,
                    "end_date": end_date_prop,
                },
                "required": ["ticker", "start_date", "end_date"],
            },
        },
        {
            "name": "get_financial_metrics",
            "description": "Return financial metrics (ratios, margins, growth) for the ticker.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": ticker_prop,
                    "end_date": end_date_prop,
                    "period": {"type": "string", "enum": ["ttm", "annual", "quarterly"], "default": "ttm"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["ticker", "end_date"],
            },
        },
        {
            "name": "search_line_items",
            "description": "Return specific line items from the financial statements.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": ticker_prop,
                    "line_items": {"type": "array", "items": {"type": "string"}},
                    "end_date": end_date_prop,
                    "period": {"type": "string", "default": "ttm"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["ticker", "line_items", "end_date"],
            },
        },
        {
            "name": "get_market_cap",
            "description": "Return the market cap for the ticker as of end_date.",
            "inputSchema": {
                "type": "object",
                "properties": {"ticker": ticker_prop, "end_date": end_date_prop},
                "required": ["ticker", "end_date"],
            },
        },
        {
            "name": "get_company_news",
            "description": "Return news headlines for the ticker ending on end_date.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": ticker_prop,
                    "end_date": end_date_prop,
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["ticker", "end_date"],
            },
        },
        {
            "name": "get_insider_trades",
            "description": "Return insider trading records for the ticker.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": ticker_prop,
                    "end_date": end_date_prop,
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["ticker", "end_date"],
            },
        },
    ]


def _memory_tool_schemas() -> list[dict]:
    """MCP tool schemas served by the memory-log Lambda target."""
    return [
        {
            "name": "get_past_context",
            "description": "Return recent same-ticker decisions plus cross-ticker reflections.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "same_ticker_limit": {"type": "integer", "default": 5},
                    "cross_ticker_limit": {"type": "integer", "default": 10},
                },
                "required": ["ticker"],
            },
        },
        {
            "name": "store_decision",
            "description": "Persist a portfolio manager decision.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "trade_date": {"type": "string"},
                    "run_id": {"type": "string"},
                    "decision": {"type": "object"},
                    "analyst_signals": {"type": "object"},
                    "cost_usd": {"type": "number"},
                    "tokens": {"type": "object"},
                },
                "required": ["ticker", "trade_date", "run_id", "decision"],
            },
        },
        {
            "name": "get_pending_entries",
            "description": "Return decisions with pending=true older than N days.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "older_than_days": {"type": "integer", "default": 1},
                },
                "required": ["ticker"],
            },
        },
        {
            "name": "update_realized_returns",
            "description": "Fill in realized_return_raw / realized_return_vs_spy for a prior decision.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "trade_date_run": {"type": "string"},
                    "realized_return_raw": {"type": "number"},
                    "realized_return_vs_spy": {"type": "number"},
                },
                "required": ["ticker", "trade_date_run", "realized_return_raw", "realized_return_vs_spy"],
            },
        },
    ]
