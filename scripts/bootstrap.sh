#!/usr/bin/env bash
# Two-phase bootstrap for AI-HedgeFund AWS deployment.
#
# Phase 0: seed secrets + cdk bootstrap
# Phase 1: deploy platform with agentCoreEnabled=false (provisions ECR, CodeBuild, Gateway shell)
# Phase 2: trigger CodeBuild directly, wait for image
# Phase 3: cdk deploy AIHedge-App-Stack with agentCoreEnabled=true
# Phase 4 (optional): enable observability
set -euo pipefail

PROFILE="${AWS_PROFILE:-IGENV}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="${AWS_ACCOUNT_ID:-590183796434}"
BUILD_PROJECT="aihedge-image-build"

echo ">>> Using profile=$PROFILE region=$REGION account=$ACCOUNT"

# --- Phase 0a — seed secrets from local env ---
python scripts/seed_secrets.py --profile "$PROFILE" --region "$REGION"

# --- Phase 0b — cdk bootstrap ---
(cd infra && cdk bootstrap "aws://$ACCOUNT/$REGION" --profile "$PROFILE")

# --- Phase 1 — platform only (creates ECR, CodeBuild, Gateway shell) ---
(cd infra && cdk deploy AIHedge-Platform-Stack \
    --require-approval never \
    --profile "$PROFILE")

echo ""
echo ">>> MANUAL STEP REQUIRED: request Bedrock model access"
echo "    1. Open https://console.aws.amazon.com/bedrock/home?region=$REGION#/modelaccess"
echo "    2. Request access to:"
echo "         anthropic.claude-opus-4-7-20251015-v1:0"
echo "         anthropic.claude-sonnet-4-5-20250929-v1:0"
echo "         anthropic.claude-haiku-4-5-20251001"
echo "    Press ENTER when all three show Access granted..."
read -r _

# --- Phase 2 — build the image ---
echo ">>> Starting CodeBuild project $BUILD_PROJECT"
BUILD_ID=$(aws codebuild start-build \
    --project-name "$BUILD_PROJECT" \
    --profile "$PROFILE" --region "$REGION" \
    --query 'build.id' --output text)
echo ">>> Build $BUILD_ID — waiting..."

while :; do
    STATUS=$(aws codebuild batch-get-builds \
        --ids "$BUILD_ID" \
        --profile "$PROFILE" --region "$REGION" \
        --query 'builds[0].buildStatus' --output text)
    echo "  status=$STATUS"
    case "$STATUS" in
        SUCCEEDED) break ;;
        FAILED|FAULT|STOPPED|TIMED_OUT)
            echo "Build did not succeed ($STATUS)"; exit 1 ;;
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
