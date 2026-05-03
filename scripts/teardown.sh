#!/usr/bin/env bash
# Push-button teardown.
#   ./scripts/teardown.sh         → destroys AIHedge-App-Stack (keeps platform data)
#   ./scripts/teardown.sh --all   → also destroys AIHedge-Platform-Stack (loses OpenSearch)
set -euo pipefail

PROFILE="${AWS_PROFILE:-IGENV}"
REGION="${AWS_REGION:-us-east-1}"

destroy_all=false
if [[ "${1:-}" == "--all" ]]; then
    destroy_all=true
fi

echo ">>> Destroying AIHedge-App-Stack"
(cd infra && cdk destroy AIHedge-App-Stack --force --profile "$PROFILE")

if [[ "$destroy_all" == "true" ]]; then
    echo ">>> Destroying AIHedge-Platform-Stack (platformDestroy=true retains nothing except Secrets)"
    (cd infra && cdk destroy AIHedge-Platform-Stack \
        -c platformDestroy=true \
        --force --profile "$PROFILE")
    echo ">>> Note: Secrets Manager entries retained with 7-day recovery (intentional)."
else
    echo ">>> Platform stack left in place. Run with --all to destroy it too."
fi
