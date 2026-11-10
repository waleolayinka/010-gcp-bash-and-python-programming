#!/usr/bin/env bash
set -Eeuo pipefail

# Windows (Git Bash/MSYS) fix for gcloud/jq: they end lines with CRLF; the
# stray \r ends up inside captured names, so strip it at the source.
# Do NOT set MSYS_NO_PATHCONV for gcloud: its Git Bash launcher breaks.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  gcloud() { command gcloud "$@" | tr -d '\r'; }
  jq() { command jq "$@" | tr -d '\r'; }
fi

usage() {
  cat <<'USAGE'
Usage: destroy-gcpresources.sh [project] [environment] [region] [gcp-project-id] [--yes]

Defaults match deploy-gcpresources.sh: project=programming, environment=bash,
region=us-central1. gcp-project-id defaults to $GCP_PROJECT_ID, then
$CLOUDSDK_CORE_PROJECT, then the active gcloud configuration.
The script removes resources with the deployment's names, including data in
its transfer bucket, the AlloyDB cluster and its backups, and database
credentials.
USAGE
}

log() {
  printf '[%s] %s\n' "$1" "$2" >&2
}

die() {
  log ERROR "$1"
  exit 1
}

figlet_or_echo() {
  if command -v figlet >/dev/null 2>&1; then
    figlet -f slant "$1"
  elif command -v python >/dev/null 2>&1 && python -c "import pyfiglet" >/dev/null 2>&1; then
    python -m pyfiglet -f slant "$1"
  else
    printf '\n========== %s ==========\n' "$1"
  fi
}

banner() {
  printf '\n'
  figlet_or_echo "$1"
  printf '\n'
}

project_name="programming"
environment_name="bash"
region="us-central1"
gcp_project="${GCP_PROJECT_ID:-${CLOUDSDK_CORE_PROJECT:-}}"
assume_yes=false
positional=()

for argument in "$@"; do
  case "$argument" in
    -h|--help)
      usage
      exit 0
      ;;
    --yes)
      assume_yes=true
      ;;
    -*)
      die "Unknown option: $argument"
      ;;
    *)
      positional+=("$argument")
      ;;
  esac
done

if ((${#positional[@]} > 4)); then
  usage >&2
  exit 2
fi
project_name="${positional[0]:-$project_name}"
environment_name="${positional[1]:-$environment_name}"
region="${positional[2]:-$region}"
gcp_project="${positional[3]:-$gcp_project}"
name_prefix="${environment_name}-${project_name}"

name_pattern='^[a-z][a-z0-9]*(-[a-z0-9]+)*$'
if [[ ! "$project_name" =~ $name_pattern || ! "$environment_name" =~ $name_pattern ]]; then
  die "Project and environment must start with a lowercase letter and contain only lowercase letters, digits, and single hyphens."
fi

for command_name in gcloud jq; do
  command -v "$command_name" >/dev/null 2>&1 || die "Required command not found: $command_name"
done

gcloud auth print-access-token >/dev/null 2>&1 ||
  die "gcloud credentials are unavailable or expired. Run 'gcloud auth login', then rerun."
if [[ -z "$gcp_project" ]]; then
  gcp_project="$(gcloud config get-value project 2>/dev/null || true)"
fi
[[ -n "$gcp_project" ]] || die "No GCP project ID. Pass it as the 4th argument, set GCP_PROJECT_ID, or run 'gcloud config set project <id>'."
gcloud projects describe "$gcp_project" --format='value(projectId)' >/dev/null 2>&1 ||
  die "Cannot access GCP project ${gcp_project}."
export CLOUDSDK_CORE_PROJECT="$gcp_project"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

# Names shared with deploy-gcpresources.sh.
network="${name_prefix}-vpc"
public_subnet="${name_prefix}-public-${region}"
private_subnet="${name_prefix}-private-${region}"
psa_range="${name_prefix}-psa-range"
router="${name_prefix}-router"
dns_zone="${name_prefix}-db-internal"
alloydb_cluster="${name_prefix}-alloydb"
vm_service_account="${name_prefix}-vm@${gcp_project}.iam.gserviceaccount.com"
bucket_name="${project_name}-${environment_name}-transfer-${gcp_project}"
peering_name="servicenetworking-googleapis-com"

if [[ "$assume_yes" != true ]]; then
  [[ -t 0 ]] || die "A confirmation prompt needs a terminal. Rerun interactively or pass --yes."
  printf 'This will permanently delete the %s-%s deployment in %s (GCP project %s), including database data and bucket contents.\n' \
    "$project_name" "$environment_name" "$region" "$gcp_project" >&2
  printf 'Type %s to continue: ' "$name_prefix" >&2
  IFS= read -r confirmation
  [[ "$confirmation" == "$name_prefix" ]] || die "Confirmation did not match; nothing was deleted."
fi

log INFO "Tearing down ${name_prefix} in project ${gcp_project}, region ${region}."

delete_compute_instances() {
  banner "Compute Resources"
  local instances name zone
  instances="$(gcloud compute instances list \
    --filter="name=(${name_prefix}-vm-1 ${name_prefix}-vm-2)" \
    --format='value(name,zone.basename())')"
  [[ -n "$instances" ]] || return 0

  while read -r name zone; do
    [[ -n "$name" ]] || continue
    log INFO "Deleting VM ${name} in ${zone}."
    gcloud compute instances delete "$name" --zone="$zone" --quiet >/dev/null
  done <<<"$instances"
}

delete_database_resources() {
  banner "Database Resources"
  if gcloud alloydb clusters describe "$alloydb_cluster" --region="$region" >/dev/null 2>&1; then
    # --force also deletes the cluster's instances (and continuous backup).
    log INFO "Deleting AlloyDB cluster ${alloydb_cluster} and its instances."
    gcloud alloydb clusters delete "$alloydb_cluster" --region="$region" --force --quiet >/dev/null
  fi

  # Automated backups outlive their cluster until they expire; remove them.
  local backups backup
  backups="$(gcloud alloydb backups list --region="$region" \
    --filter="clusterName ~ /clusters/${alloydb_cluster}\$" \
    --format='value(name.basename())')"
  while IFS= read -r backup; do
    [[ -n "$backup" ]] || continue
    log INFO "Deleting AlloyDB backup ${backup}."
    gcloud alloydb backups delete "$backup" --region="$region" --quiet >/dev/null
  done <<<"$backups"
}

delete_database_credentials() {
  banner "Database Credentials"
  local secret
  for secret in "${name_prefix}-database-password" "${name_prefix}-database-credentials"; do
    if gcloud secrets describe "$secret" >/dev/null 2>&1; then
      log INFO "Deleting Secret Manager secret ${secret}."
      gcloud secrets delete "$secret" --quiet
    fi
  done
}

delete_transfer_bucket() {
  banner "Transfer Bucket"
  if ! gcloud storage buckets describe "gs://${bucket_name}" >/dev/null 2>&1; then
    return 0
  fi
  log INFO "Removing objects and bucket gs://${bucket_name}."
  gcloud storage rm --recursive --all-versions "gs://${bucket_name}" >/dev/null
}

delete_iam_resources() {
  banner "IAM Resources"
  if ! gcloud iam service-accounts describe "$vm_service_account" >/dev/null 2>&1; then
    return 0
  fi

  # Remove project bindings first; deleting the account would leave
  # "deleted:serviceAccount:..." members behind in the project policy.
  local member="serviceAccount:${vm_service_account}" role
  local -a roles=()
  mapfile -t roles < <(gcloud projects get-iam-policy "$gcp_project" --format=json |
    jq -r --arg member "$member" '[.bindings[]? | select(.members | index($member)) | .role] | unique | .[]')
  for role in "${roles[@]}"; do
    [[ -n "$role" ]] || continue
    log INFO "Removing ${role} from ${vm_service_account}."
    gcloud projects remove-iam-policy-binding "$gcp_project" \
      --member="$member" --role="$role" --all >/dev/null
  done

  log INFO "Deleting service account ${vm_service_account}."
  gcloud iam service-accounts delete "$vm_service_account" --quiet
}

delete_network_resources() {
  banner "Networking Resources"
  local dns_record="${project_name}-${environment_name}-postgresql.db.internal."
  if gcloud dns managed-zones describe "$dns_zone" >/dev/null 2>&1; then
    if [[ -n "$(gcloud dns record-sets list --zone="$dns_zone" --name="$dns_record" --type=A --format='value(name)')" ]]; then
      log INFO "Removing Cloud DNS record ${dns_record}."
      gcloud dns record-sets delete "$dns_record" --zone="$dns_zone" --type=A >/dev/null
    fi
    log INFO "Deleting private DNS zone ${dns_zone}."
    gcloud dns managed-zones delete "$dns_zone" >/dev/null
  fi

  local rule
  for rule in allow-web allow-iap-ssh allow-db-egress deny-db-egress; do
    if gcloud compute firewall-rules describe "${name_prefix}-${rule}" >/dev/null 2>&1; then
      log INFO "Deleting firewall rule ${name_prefix}-${rule}."
      gcloud compute firewall-rules delete "${name_prefix}-${rule}" --quiet >/dev/null
    fi
  done

  # Deleting the router also deletes its Cloud NAT gateway and NAT IPs.
  if gcloud compute routers describe "$router" --region="$region" >/dev/null 2>&1; then
    log INFO "Deleting Cloud Router ${router} and its NAT."
    gcloud compute routers delete "$router" --region="$region" --quiet >/dev/null
  fi

  if gcloud compute networks describe "$network" --format=json |
      jq -e --arg peering "$peering_name" '.peerings[]? | select(.name == $peering)' >/dev/null; then
    log INFO "Removing Private Service Access peering from ${network}."
    # Service Networking can refuse for a while after AlloyDB deletion
    # ("producer services still using this connection"); removing the
    # peering from our side releases the network either way.
    if ! gcloud services vpc-peerings delete \
        --service=servicenetworking.googleapis.com \
        --network="$network" >/dev/null 2>&1; then
      log WARN "Service Networking refused; deleting the VPC peering directly."
      gcloud compute networks peerings delete "$peering_name" --network="$network" >/dev/null
    fi
  fi

  if gcloud compute addresses describe "$psa_range" --global >/dev/null 2>&1; then
    log INFO "Releasing Private Service Access range ${psa_range}."
    gcloud compute addresses delete "$psa_range" --global --quiet >/dev/null
  fi

  local subnet
  for subnet in "$public_subnet" "$private_subnet"; do
    if gcloud compute networks subnets describe "$subnet" --region="$region" >/dev/null 2>&1; then
      log INFO "Deleting subnet ${subnet}."
      gcloud compute networks subnets delete "$subnet" --region="$region" --quiet >/dev/null
    fi
  done

  log INFO "Deleting VPC ${network}."
  gcloud compute networks delete "$network" --quiet >/dev/null
}

main() {
  delete_compute_instances
  delete_database_resources
  delete_database_credentials
  delete_iam_resources
  delete_transfer_bucket

  if gcloud compute networks describe "$network" >/dev/null 2>&1; then
    delete_network_resources
  else
    log INFO "No matching VPC was found; skipping networking cleanup."
  fi

  log INFO "Teardown completed."
  banner "Teardown Complete"
}

main
