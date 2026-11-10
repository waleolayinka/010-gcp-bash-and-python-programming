#!/usr/bin/env bash
# Sets up GitHub Actions -> Google Cloud keyless authentication for this repo
# (Workload Identity Federation, the GCP equivalent of an AWS OIDC role):
#   1. Enables the APIs the deploy/destroy scripts use
#   2. Creates a Workload Identity Pool + GitHub OIDC provider (if missing) whose
#      attribute condition only accepts tokens from ONE branch of this repo
#      (default: main)
#   3. Creates/updates a deploy service account that only that branch can
#      impersonate, and grants it the predefined roles the scripts need.
#      Project IAM changes are limited by an IAM condition to the two roles the
#      scripts grant the VM service account.
#   4. Stores the provider, service account, and project ID in repo secrets
#      GCP_WORKLOAD_IDENTITY_PROVIDER, GCP_SERVICE_ACCOUNT, GCP_PROJECT_ID
#
# Safe to re-run: existing resources are reused/updated, not duplicated. Re-running
# also removes roles/owner and roles/editor if they were granted to the deploy account.
#
# Usage: ./github-oidc.sh [github-owner/repo] [service-account-id] [branch] [gcp-project-id]

set -euo pipefail

for cmd in gcloud gh jq; do
  command -v "$cmd" >/dev/null || { echo "ERROR: '$cmd' is not installed." >&2; exit 1; }
done

# Git Bash: strip CRLF from gcloud.cmd output. Do NOT set MSYS_NO_PATHCONV for
# gcloud; its Git Bash launcher breaks with it.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  gcloud() { command gcloud "$@" | tr -d '\r'; }
fi

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
SA_ID="${2:-github-actions-010-deploy}"
BRANCH="${3:-main}"
GCP_PROJECT="${4:-${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}}"
POOL_ID="github-actions"
PROVIDER_ID="github"
OIDC_ISSUER="https://token.actions.githubusercontent.com"

[[ -n "$GCP_PROJECT" ]] || { echo "ERROR: pass a GCP project ID or run 'gcloud config set project <id>'." >&2; exit 1; }
export CLOUDSDK_CORE_PROJECT="$GCP_PROJECT"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

# The token's "sub" claim prefix differs per repo: newer repos use the immutable
# form (repo:owner@<id>/name@<id>), older ones repo:owner/name. Ask GitHub which.
SUB_PREFIX="$(gh api "repos/${REPO}/actions/oidc/customization/sub" --jq '.sub_claim_prefix // empty' 2>/dev/null || true)"
SUB_PREFIX="${SUB_PREFIX:-repo:${REPO}}"
SUBJECT="${SUB_PREFIX}:ref:refs/heads/${BRANCH}"

PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')"
SA_EMAIL="${SA_ID}@${GCP_PROJECT}.iam.gserviceaccount.com"
POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"
PROVIDER_NAME="${POOL_NAME}/providers/${PROVIDER_ID}"
PRINCIPAL="principal://iam.googleapis.com/${POOL_NAME}/subject/${SUBJECT}"

echo "Project : $GCP_PROJECT ($PROJECT_NUMBER)"
echo "Repo    : $REPO"
echo "Subject : $SUBJECT"
echo "Account : $SA_EMAIL"
echo

work_dir="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1; then work_dir="$(cygpath -m "$work_dir")"; fi
trap 'rm -rf "$work_dir"' EXIT

# 1. APIs used by this script and by the deploy/destroy scripts.
echo "Enabling APIs..."
gcloud services enable \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
  compute.googleapis.com dns.googleapis.com sqladmin.googleapis.com \
  servicenetworking.googleapis.com secretmanager.googleapis.com storage.googleapis.com

# 2. Workload Identity Pool + provider that only accepts tokens from $BRANCH.
if gcloud iam workload-identity-pools describe "$POOL_ID" --location=global >/dev/null 2>&1; then
  echo "Workload Identity Pool already exists: $POOL_NAME"
else
  echo "Creating Workload Identity Pool..."
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --location=global \
    --display-name="GitHub Actions" >/dev/null
fi

ATTRIBUTE_MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"
ATTRIBUTE_CONDITION="assertion.sub == '${SUBJECT}'"
if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
    --location=global --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
  echo "Provider exists; updating attribute condition..."
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER_ID" \
    --location=global \
    --workload-identity-pool="$POOL_ID" \
    --attribute-mapping="$ATTRIBUTE_MAPPING" \
    --attribute-condition="$ATTRIBUTE_CONDITION" >/dev/null
else
  echo "Creating GitHub OIDC provider..."
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --location=global \
    --workload-identity-pool="$POOL_ID" \
    --display-name="GitHub" \
    --issuer-uri="$OIDC_ISSUER" \
    --attribute-mapping="$ATTRIBUTE_MAPPING" \
    --attribute-condition="$ATTRIBUTE_CONDITION" >/dev/null
fi

# 3. Deploy service account, impersonable only by the workflow subject above.
if gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  echo "Service account already exists: $SA_EMAIL"
else
  echo "Creating service account..."
  gcloud iam service-accounts create "$SA_ID" \
    --display-name="GitHub Actions deploy for ${REPO} (${BRANCH})" >/dev/null
  sleep 10 # let the new account propagate before binding roles to it
fi

gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --role=roles/iam.workloadIdentityUser \
  --member="$PRINCIPAL" >/dev/null
echo "Allowed $SUBJECT to impersonate $SA_EMAIL"

# Predefined roles covering exactly the services the scripts call.
DEPLOY_ROLES=(
  roles/compute.networkAdmin            # VPC, subnets, router/NAT, PSA range, peering
  roles/compute.securityAdmin           # firewall rules
  roles/compute.instanceAdmin.v1        # VMs
  roles/iam.serviceAccountAdmin         # create/delete the VM service account
  roles/iam.serviceAccountUser          # attach the VM service account to VMs
  roles/dns.admin                       # private zone + record
  roles/cloudsql.admin                  # Cloud SQL instance, database, user
  roles/servicenetworking.networksAdmin # Private Service Access connection
  roles/secretmanager.admin             # database credentials
  roles/storage.admin                   # transfer bucket
  roles/serviceusage.serviceUsageConsumer
)
for role in "${DEPLOY_ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role" \
    --condition=None >/dev/null
  echo "Granted $role"
done

# Project IAM is the privilege-escalation path, so the deploy account may only
# add/remove the two roles the scripts give the VM service account.
cat >"$work_dir/iam-condition.json" <<'EOF'
{
  "title": "vm-agent-roles-only",
  "description": "Only grant/revoke logging.logWriter and monitoring.metricWriter",
  "expression": "api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly(['roles/logging.logWriter', 'roles/monitoring.metricWriter'])"
}
EOF
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role=roles/resourcemanager.projectIamAdmin \
  --condition-from-file="$work_dir/iam-condition.json" >/dev/null
echo "Granted roles/resourcemanager.projectIamAdmin (conditional)"

# Remove broad roles an earlier manual setup may have granted.
for broad_role in roles/owner roles/editor; do
  if gcloud projects get-iam-policy "$GCP_PROJECT" --format=json |
      jq -e --arg role "$broad_role" --arg member "serviceAccount:${SA_EMAIL}" \
        '.bindings[]? | select(.role == $role and (.members | index($member)))' >/dev/null; then
    gcloud projects remove-iam-policy-binding "$GCP_PROJECT" \
      --member="serviceAccount:${SA_EMAIL}" --role="$broad_role" --all >/dev/null
    echo "Removed $broad_role"
  fi
done

# 4. Repo secrets used by the workflows
gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$REPO" --body "$PROVIDER_NAME"
gh secret set GCP_SERVICE_ACCOUNT --repo "$REPO" --body "$SA_EMAIL"
gh secret set GCP_PROJECT_ID --repo "$REPO" --body "$GCP_PROJECT"
echo "Set secrets GCP_WORKLOAD_IDENTITY_PROVIDER, GCP_SERVICE_ACCOUNT, GCP_PROJECT_ID on $REPO"

echo
echo "Done. Provider: $PROVIDER_NAME"
echo "Only workflows run from '${BRANCH}' can impersonate ${SA_EMAIL}, e.g.:"
echo "  gh workflow run deploy-bash-pipeline.yml --repo $REPO --ref $BRANCH"
