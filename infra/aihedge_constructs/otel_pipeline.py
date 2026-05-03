"""OSIS pipeline + OpenSearch dashboards importer.

Consumes OTLP/HTTP traffic (SigV4) from Fargate + AgentCore, fans:
  trace  → OpenSearch (ss4o_traces-aih-*)
  log    → OpenSearch (ss4o_logs-aih-*)
  metric → Amazon Managed Prometheus (remote_write)
"""
from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_osis as osis
from constructs import Construct

_DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "dashboards"


class OtelPipeline(Construct):
    def __init__(
        self,
        scope: Construct,
        id_: str,
        *,
        domain: opensearch.Domain,
        amp_workspace_arn: str,
        amp_remote_write_url: str,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id_)

        stack = Stack.of(self)

        pipeline_role = iam.Role(
            self,
            "OsisRole",
            assumed_by=iam.ServicePrincipal("osis-pipelines.amazonaws.com"),
            description="OSIS pipeline role for AI-HedgeFund observability",
        )
        pipeline_role.add_to_policy(
            iam.PolicyStatement(
                actions=["es:DescribeDomain", "es:ESHttp*"],
                resources=[domain.domain_arn, f"{domain.domain_arn}/*"],
            )
        )
        pipeline_role.add_to_policy(
            iam.PolicyStatement(
                actions=["aps:RemoteWrite"],
                resources=[amp_workspace_arn],
            )
        )

        log_group = logs.LogGroup(
            self,
            "OsisLogs",
            log_group_name="/aws/vendedlogs/OpenSearchService/pipelines/aihedge-otel",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )

        pipeline_body = self._render_pipeline_body(
            domain_endpoint=f"https://{domain.domain_endpoint}",
            sink_role_arn=pipeline_role.role_arn,
            region=stack.region,
            amp_remote_write_url=amp_remote_write_url,
        )

        self.pipeline = osis.CfnPipeline(
            self,
            "Pipeline",
            pipeline_name="aihedge-otel",
            min_units=1,
            max_units=4,
            pipeline_configuration_body=pipeline_body,
            log_publishing_options=osis.CfnPipeline.LogPublishingOptionsProperty(
                is_logging_enabled=True,
                cloud_watch_log_destination=osis.CfnPipeline.CloudWatchLogDestinationProperty(
                    log_group=log_group.log_group_name,
                ),
            ),
            tags=[{"key": "UsedBy", "value": "AIHedge"}],
        )

        import aws_cdk as cdk  # local import avoids circularity at module load

        CfnOutput(
            self,
            "OsisIngestEndpoint",
            value=cdk.Fn.select(0, self.pipeline.attr_ingest_endpoint_urls),
        )
        CfnOutput(self, "OpenSearchDomainEndpoint", value=domain.domain_endpoint)

    @staticmethod
    def _render_pipeline_body(
        *,
        domain_endpoint: str,
        sink_role_arn: str,
        region: str,
        amp_remote_write_url: str,
    ) -> str:
        """Return the YAML pipeline config inline.

        Three sub-pipelines: traces, logs, metrics. OTLP/HTTP source with SigV4.
        """
        return f"""\
version: "2"

otel-trace-pipeline:
  source:
    otel_trace_source:
      path: /v1/traces
  processor:
    - otel_traces:
    - trace_peer_forwarder:
  sink:
    - opensearch:
        hosts: ["{domain_endpoint}"]
        aws:
          sts_role_arn: "{sink_role_arn}"
          region: "{region}"
        index_type: trace-analytics-raw
        index: ss4o_traces-aih-%{{yyyy.MM.dd}}

otel-service-map-pipeline:
  source:
    pipeline:
      name: otel-trace-pipeline
  processor:
    - service_map:
  sink:
    - opensearch:
        hosts: ["{domain_endpoint}"]
        aws:
          sts_role_arn: "{sink_role_arn}"
          region: "{region}"
        index_type: trace-analytics-service-map

otel-log-pipeline:
  source:
    otel_logs_source:
      path: /v1/logs
  processor:
    - parse_json:
  sink:
    - opensearch:
        hosts: ["{domain_endpoint}"]
        aws:
          sts_role_arn: "{sink_role_arn}"
          region: "{region}"
        index: ss4o_logs-aih-%{{yyyy.MM.dd}}

otel-metrics-pipeline:
  source:
    otel_metrics_source:
      path: /v1/metrics
  processor:
    - otel_metrics:
  sink:
    - prometheus:
        endpoint: "{amp_remote_write_url}"
        aws_sigv4: true
        aws_region: "{region}"
        aws_sts_role_arn: "{sink_role_arn}"
"""
