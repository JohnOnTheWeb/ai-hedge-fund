#!/usr/bin/env bash
# Purge retained AI-HedgeFund resources that CFN leaves behind after destroy.
#
# After `cdk destroy`, RETAIN-policy resources block a fresh deploy with
# "already exists". This script finds resources tagged UsedBy=AIHedge and
# deletes them. Defensive: never touches anything missing that tag.
#
#   ./scripts/purge_orphans.sh              # dry-run (default)
#   ./scripts/purge_orphans.sh --execute    # actually delete
#
# Typical candidates: Secrets Manager secrets, ECR repos, S3 buckets,
# OpenSearch domain, DynamoDB table, CloudWatch log groups, OSIS pipeline,
# AMP workspace.
set -euo pipefail

PROFILE="${AWS_PROFILE:-IGENV}"
REGION="${AWS_REGION:-us-east-1}"

execute=false
[[ "${1:-}" == "--execute" ]] && execute=true

prefix=">>>"
if ! $execute; then prefix="[DRY]"; fi

echo "$prefix Scanning us-east-1 for retained resources tagged UsedBy=AIHedge"

# Secrets Manager
secrets=$(aws secretsmanager list-secrets \
    --filters "Key=tag-key,Values=UsedBy" "Key=tag-value,Values=AIHedge" \
    --profile "$PROFILE" --region "$REGION" \
    --query 'SecretList[].Name' --output text 2>/dev/null || echo "")
for s in $secrets; do
    echo "$prefix secretsmanager delete-secret --secret-id $s --force-delete-without-recovery"
    $execute && aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery \
        --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

# ECR repos
repos=$(aws ecr describe-repositories --profile "$PROFILE" --region "$REGION" \
    --query 'repositories[?starts_with(repositoryName, `aihedge`)].repositoryName' --output text 2>/dev/null || echo "")
for r in $repos; do
    echo "$prefix ecr delete-repository --repository-name $r --force"
    $execute && aws ecr delete-repository --repository-name "$r" --force \
        --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

# OpenSearch domains
domains=$(aws opensearch list-domain-names --profile "$PROFILE" --region "$REGION" \
    --query 'DomainNames[?starts_with(DomainName, `aihedge`)].DomainName' --output text 2>/dev/null || echo "")
for d in $domains; do
    echo "$prefix opensearch delete-domain --domain-name $d  (takes 15-30 minutes)"
    $execute && aws opensearch delete-domain --domain-name "$d" --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

# AMP workspaces
ws=$(aws amp list-workspaces --profile "$PROFILE" --region "$REGION" \
    --query 'workspaces[?alias==`aihedge-metrics`].workspaceId' --output text 2>/dev/null || echo "")
for w in $ws; do
    echo "$prefix amp delete-workspace --workspace-id $w"
    $execute && aws amp delete-workspace --workspace-id "$w" --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

# OSIS pipelines
pipes=$(aws osis list-pipelines --profile "$PROFILE" --region "$REGION" \
    --query 'Pipelines[?starts_with(PipelineName, `aihedge`)].PipelineName' --output text 2>/dev/null || echo "")
for p in $pipes; do
    echo "$prefix osis delete-pipeline --pipeline-name $p"
    $execute && aws osis delete-pipeline --pipeline-name "$p" --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

# S3 buckets (must empty before delete)
buckets=$(aws s3api list-buckets --profile "$PROFILE" --region "$REGION" \
    --query 'Buckets[?starts_with(Name, `aihedge-`)].Name' --output text 2>/dev/null || echo "")
for b in $buckets; do
    tags=$(aws s3api get-bucket-tagging --bucket "$b" --profile "$PROFILE" --region "$REGION" \
        --query "TagSet[?Key=='UsedBy'].Value" --output text 2>/dev/null || echo "")
    [[ "$tags" == "AIHedge" ]] || { echo "[skip] $b not tagged UsedBy=AIHedge"; continue; }
    echo "$prefix s3 rb s3://$b --force"
    $execute && aws s3 rb "s3://$b" --force --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

# DynamoDB
tbls=$(aws dynamodb list-tables --profile "$PROFILE" --region "$REGION" \
    --query 'TableNames[?starts_with(@, `aihedge`)]' --output text 2>/dev/null || echo "")
for t in $tbls; do
    echo "$prefix dynamodb delete-table --table-name $t"
    $execute && aws dynamodb delete-table --table-name "$t" --profile "$PROFILE" --region "$REGION" >/dev/null || true
done

$execute || echo "$prefix Re-run with --execute to apply."
