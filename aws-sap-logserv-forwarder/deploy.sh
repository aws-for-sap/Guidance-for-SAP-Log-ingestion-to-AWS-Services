#!/bin/bash
# =============================================================================
# AWS SAP LogServ Log Forwarder - Deployment Script
# =============================================================================
# Handles the two-step deployment required for fresh (first-time) deployments
# to new AWS accounts. On subsequent updates, a single deploy is sufficient.
#
# Why two steps?
# CloudFormation validates that the Lambda role can access the cross-account
# SQS queue BEFORE creating resources. On a brand-new stack the role doesn't
# exist yet, causing validation failure. Step 1 creates the role + Lambda,
# Step 2 adds the SQS trigger once the role exists.
#
# Usage:
#   ./deploy.sh --region <region> --profile <aws-profile> [options]
#
# Required:
#   --region              AWS region (e.g. us-east-1)
#   --profile             AWS CLI profile name
#
# Optional:
#   --config-file         SAM config file (default: samconfig.toml)
#   --stack-name          Stack name override
#   --first-deploy        Force two-step deployment (auto-detected if omitted)
#   --help                Show this help message
# =============================================================================

set -euo pipefail

# --- Defaults ---
CONFIG_FILE="samconfig.toml"
STACK_NAME=""
REGION=""
PROFILE=""
FIRST_DEPLOY=""
BUILD_DIR=""

# --- Parse Arguments ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --config-file) CONFIG_FILE="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --first-deploy) FIRST_DEPLOY="true"; shift ;;
    --help)
      head -30 "$0" | tail -25
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# --- Validate Required Args ---
if [[ -z "$REGION" || -z "$PROFILE" ]]; then
  echo "ERROR: --region and --profile are required."
  echo "Usage: ./deploy.sh --region us-east-1 --profile my-aws-profile"
  exit 1
fi

# --- Auto-detect First Deploy ---
if [[ -z "$FIRST_DEPLOY" ]]; then
  if [[ -n "$STACK_NAME" ]]; then
    CHECK_STACK="$STACK_NAME"
  else
    # Read stack name from samconfig
    CHECK_STACK=$(grep -E '^\s*stack_name' "$CONFIG_FILE" 2>/dev/null | head -1 | sed 's/.*= *"\?\([^"]*\)"\?/\1/' || echo "")
  fi

  if [[ -n "$CHECK_STACK" ]]; then
    STACK_STATUS=$(aws cloudformation describe-stacks \
      --stack-name "$CHECK_STACK" \
      --profile "$PROFILE" \
      --region "$REGION" \
      --query "Stacks[0].StackStatus" \
      --output text 2>/dev/null || echo "DOES_NOT_EXIST")

    if [[ "$STACK_STATUS" == "DOES_NOT_EXIST" || "$STACK_STATUS" == "None" ]]; then
      FIRST_DEPLOY="true"
      echo "Stack '$CHECK_STACK' does not exist — running two-step first deployment."
    else
      FIRST_DEPLOY="false"
      echo "Stack '$CHECK_STACK' exists (status: $STACK_STATUS) — running single update."
    fi
  else
    FIRST_DEPLOY="true"
    echo "Could not determine stack name — assuming first deployment."
  fi
fi

# --- Build ---
# Use a temp build dir to avoid OneDrive file lock issues on Windows
BUILD_DIR=$(mktemp -d 2>/dev/null || echo "/tmp/sam-build-logserv-$$")
echo "Building... (build dir: $BUILD_DIR)"
sam build --build-dir "$BUILD_DIR"

# --- Common Deploy Args ---
DEPLOY_ARGS=(
  --template-file "$BUILD_DIR/template.yaml"
  --profile "$PROFILE"
  --region "$REGION"
  --no-confirm-changeset
  --no-fail-on-empty-changeset
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM
  --resolve-s3
)

if [[ -n "$STACK_NAME" ]]; then
  DEPLOY_ARGS+=(--stack-name "$STACK_NAME")
fi

if [[ -f "$CONFIG_FILE" ]]; then
  DEPLOY_ARGS+=(--config-file "$CONFIG_FILE")
fi

# --- Deploy ---
if [[ "$FIRST_DEPLOY" == "true" ]]; then
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  STEP 1/2: Creating infrastructure (no bucket, no SQS trigger)"
  echo "═══════════════════════════════════════════════════════════════"
  sam deploy "${DEPLOY_ARGS[@]}" --parameter-overrides "EnableSQSTrigger=false" "CreateDestBucket=false"

  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  STEP 2/2: Adding destination bucket and SQS trigger"
  echo "═══════════════════════════════════════════════════════════════"
  sam deploy "${DEPLOY_ARGS[@]}" --parameter-overrides "EnableSQSTrigger=true" "CreateDestBucket=true"

  echo ""
  echo "✅ First deployment complete! Log processing is now active."
else
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  Updating stack..."
  echo "═══════════════════════════════════════════════════════════════"
  sam deploy "${DEPLOY_ARGS[@]}"

  echo ""
  echo "✅ Update complete!"
fi

# --- Cleanup ---
if [[ -d "$BUILD_DIR" ]]; then
  rm -rf "$BUILD_DIR" 2>/dev/null || true
fi
