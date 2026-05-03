#!/usr/bin/env bash
# Two-phase bootstrap for AI-HedgeFund AWS deployment.
#
# Phase 0: seed secrets + cdk bootstrap
# Phase 1: deploy platform + app with agentCoreEnabled=false (provisions ECR, CodePipeline, Gateway shell)
# Phase 2: trigger CodePipeline, wait for image
# Phase 3: cdk deploy AIHedge-App-Stack with agentCoreEnabled=true
# Phase 4 (optional): enable observability
set -euo pipefail

PROFILE="${AWS_PROFILE:-IGENV}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="${AWS_ACCOUNT_ID:-264207762605}"

echo ">>> Using profile=$PROFILE region=$REGION account=$ACCOUNT"

# --- Phase 0a — seed secrets from local env ---
python scripts/seed_secrets.py --profile "$PROFILE" --region "$REGION"

# --- Phase 0b — cdk bootstrap ---
(cd infra && cdk bootstrap "aws://$ACCOUNT/$REGION" --profile "$PROFILE")

# --- Phase 1 — platform + app (no Runtime yet) ---
(cd infra && cdk deploy AIHedge-Platform-Stack AIHedge-App-Stack \
    -c agentCoreEnabled=false \
    --require-approval never \
    --profile "$PROFILE")

# --- Phase 2 — build the image ---
echo ">>> Starting CodePipeline execution"
EXEC_ID=$(aws codepipeline start-pipeline-execution \
    --name aihedge-image-pipeline \
    --profile "$PROFILE" --region "$REGION" \
    --query 'pipelineExecutionId' --output text)
echo ">>> Execution $EXEC_ID — waiting for build..."

while :; do
    STATUS=$(aws codepipeline get-pipeline-execution \
        --pipeline-name aihedge-image-pipeline \
        --pipeline-execution-id "$EXEC_ID" \
        --profile "$PROFILE" --region "$REGION" \
        --query 'pipelineExecution.status' --output text)
    echo "  status=$STATUS"
    case "$STATUS" in
        Succeeded) break ;;
        Failed|Stopped|Cancelled|Superseded)
            echo "Pipeline did not succeed ($STATUS)"; exit 1 ;;
    esac
    sleep 30
done

# --- Phase 3 — redeploy app with Runtime + Gateway targets ---
(cd infra && cdk deploy AIHedge-App-Stack \
    -c agentCoreEnabled=true \
    --require-approval never \
    --profile "$PROFILE")

# --- Phase 4 (optional) — observability ---
if [[ "${AIHEDGE_OBS:-false}" == "true" ]]; then
    (cd infra && cdk deploy AIHedge-Platform-Stack AIHedge-App-Stack \
        -c agentCoreEnabled=true \
        -c observabilityEnabled=true \
        --require-approval never \
        --profile "$PROFILE")
fi

echo ">>> Bootstrap complete"
