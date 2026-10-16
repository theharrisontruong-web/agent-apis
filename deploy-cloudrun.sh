#!/usr/bin/env bash
# Deploy grader + geo agent APIs to Google Cloud Run.
# Usage: PROJECT_ID=<id> REGION=<region> X402_WALLET=0x... ./deploy-cloudrun.sh
# Optional overrides: X402_NETWORK (default Base Sepolia testnet), X402_FACILITATOR_URL,
#   SA_KEY_FILE (service-account JSON; deleted from IAM + shredded after deploy).
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${X402_WALLET:?set X402_WALLET (public receiving address only)}"
REGION="${REGION:-us-east1}"
# Mainnet defaults (verified 2026-09-21): PayAI facilitator settles real USDC on
# Base mainnet, no API key needed. Override with X402_NETWORK / X402_FACILITATOR_URL.
NETWORK="${X402_NETWORK:-eip155:8453}"
FACILITATOR="${X402_FACILITATOR_URL:-https://facilitator.payai.network}"
# Optional: Coinbase CDP (Bazaar rail). When both CDP_API_KEY_ID and
# CDP_API_KEY_SECRET are set in the deploy environment, they are passed to
# both services, which then also offer CDP-settled accepts entries (the
# rail the CDP Bazaar / agentic.market catalog indexes). Keys are free at
# portal.cdp.coinbase.com; the services run PayAI-only without them.
CDP_ENV=""
if [[ -n "${CDP_API_KEY_ID:-}" && -n "${CDP_API_KEY_SECRET:-}" ]]; then
  CDP_ENV=",CDP_API_KEY_ID=${CDP_API_KEY_ID},CDP_API_KEY_SECRET=${CDP_API_KEY_SECRET}"
fi
# Optional: Glama connector claim email (public once served). Unset = claim
# endpoint stays 404 on both services.
GLAMA_ENV=""
if [[ -n "${GLAMA_MAINTAINER_EMAIL:-}" ]]; then
  GLAMA_ENV=",GLAMA_MAINTAINER_EMAIL=${GLAMA_MAINTAINER_EMAIL}"
fi
REPO="agent-api"
AR="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

GCLOUD="${GCLOUD_BIN:-$HOME/workspace/google-cloud-sdk/bin/gcloud}"
"$GCLOUD" config set project "$PROJECT_ID" >/dev/null

echo "== enabling APIs =="
if [[ "${ENABLE_APIS:-0}" == "1" ]]; then
  "$GCLOUD" services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com --project="$PROJECT_ID"
else
  echo "(skipped: APIs already enabled via console)"
fi

echo "== artifact registry repo =="
if "$GCLOUD" artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "(repo exists)"
else
  "$GCLOUD" artifacts repositories create "$REPO" --repository-format=docker \
    --location="$REGION" --project="$PROJECT_ID"
fi

build_and_deploy() {
  local name="$1" dir="$2" price="$3"
  local img="${AR}/${name}:latest"
  echo "== building ${name} via Cloud Build =="
  local bid
  bid=$(cd "$dir" && "$GCLOUD" builds submit --tag "$img" --project="$PROJECT_ID" --async --quiet --format='value(id)' .)
  echo "   build id: ${bid} — polling..."
  local st=""
  while true; do
    st=$("$GCLOUD" builds describe "$bid" --project="$PROJECT_ID" --format='value(status)' 2>/dev/null)
    echo "   build status: ${st}"
    case "$st" in
      SUCCESS) break ;;
      FAILURE|CANCELLED|TIMEOUT|EXPIRED) echo "BUILD FAILED: ${st}"; return 1 ;;
    esac
    sleep 15
  done
  echo "== deploying ${name} to Cloud Run =="
  "$GCLOUD" run deploy "$name" --image "$img" --region "$REGION" --project="$PROJECT_ID" \
    --platform managed --allow-unauthenticated \
    --memory 512Mi --cpu 1 --max-instances 2 --min-instances 0 \
    --timeout 60 \
    --set-env-vars "X402_WALLET=${X402_WALLET},X402_PRICE=${price},X402_NETWORK=${NETWORK},X402_FACILITATOR_URL=${FACILITATOR},X402_DISABLED=0${GLAMA_ENV}${CDP_ENV}" \
    --quiet
  local url
  url=$("$GCLOUD" run services describe "$name" --region "$REGION" --project="$PROJECT_ID" --format='value(status.url)')
  echo "== ${name} live at ${url} =="
  echo "   health: $(curl -s -o /dev/null -w '%{http_code}' "${url}/health")"
  echo "   paywall log:"
  "$GCLOUD" run services logs read "$name" --region "$REGION" --project="$PROJECT_ID" --limit 30 2>/dev/null \
    | grep -E "paywall (ARMED|NOT armed)" | tail -3 || echo "   (check logs manually)"
  echo "$url" > "/tmp/${name}.url"
}

build_and_deploy "grader" "$HOME/workspace/agent-api/grader" '$1.00'
build_and_deploy "geo"    "$HOME/workspace/agent-api/geo"    '$1.50'
build_and_deploy "a11y"   "$HOME/workspace/agent-api/a11y"   '$1.50'
build_and_deploy "feed"   "$HOME/workspace/agent-api/feed"   '$1.00'

# Revoke the one-time deploy key: delete from IAM, shred local copy.
if [[ -n "${SA_KEY_FILE:-}" && -f "$SA_KEY_FILE" ]]; then
  KEY_ID=$(python3 -c "import json;print(json.load(open('$SA_KEY_FILE'))['private_key_id'])")
  SA_EMAIL=$(python3 -c "import json;print(json.load(open('$SA_KEY_FILE'))['client_email'])")
  echo "== revoking one-time deploy key ${KEY_ID} =="
  "$GCLOUD" iam service-accounts keys delete "$KEY_ID" --iam-account="$SA_EMAIL" --project="$PROJECT_ID" --quiet
  shred -u "$SA_KEY_FILE" 2>/dev/null || rm -f "$SA_KEY_FILE"
  echo "key revoked and shredded"
fi

echo "DONE. grader: $(cat /tmp/grader.url)  geo: $(cat /tmp/geo.url)  a11y: $(cat /tmp/a11y.url)  feed: $(cat /tmp/feed.url)"
