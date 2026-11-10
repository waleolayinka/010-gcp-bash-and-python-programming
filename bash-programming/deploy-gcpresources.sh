#!/usr/bin/env bash
#
# Provision Google Cloud resources with Bash and the gcloud CLI instead of Terraform.
#
# Purpose: test Bash + gcloud as an alternative to an IaC tool for building
# GCP infrastructure; see python-programming/ for the same stack in Python.
#
# Resources created:
#   Networking: custom-mode VPC with public/private regional subnets
#               (10.1.1.0/24, 10.1.2.0/24), Cloud Router + Cloud NAT for the
#               private subnet, firewall rules, a Private Service Access range
#               (10.1.16.0/20) peered to Google services, and a private Cloud
#               DNS zone (db.internal).
#   Compute:    two private Compute Engine VMs (spread across two zones) with
#               no external IP; reachable only through IAP TCP forwarding with
#               OS Login: no SSH keys in metadata. A dedicated VM service
#               account and a private GCS transfer bucket.
#   Database:   Cloud SQL for PostgreSQL 17, regional HA (primary + standby in
#               two zones), private IP only; port 5432 egress is allowed only
#               from the VM network tag. Credentials in Secret Manager.

set -Eeuo pipefail

# Windows (Git Bash/MSYS) fix for gcloud/jq: they end lines with CRLF; the
# stray \r ends up inside captured names/IPs, so strip it at the source.
# Do NOT set MSYS_NO_PATHCONV for gcloud: its Git Bash launcher passes a /c/...
# path to python and breaks. No argument in this script starts with "/".
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  gcloud() { command gcloud "$@" | tr -d '\r'; }
  jq() { command jq "$@" | tr -d '\r'; }
fi

usage() {
  cat <<'USAGE'
Usage: deploy-gcpresources.sh [project] [environment] [region] [gcp-project-id]

Defaults: project=programming, environment=bash, region=us-central1.
gcp-project-id defaults to $GCP_PROJECT_ID, then $CLOUDSDK_CORE_PROJECT,
then the active gcloud configuration.

Set ADDITIONAL_DB_NETWORK_TAGS to a comma-separated list of VM network tags
(in the same VPC) that also need database access.
This provisions new resources; it is not safe to rerun against an existing
deployment, and resources created before a failure are not automatically removed.
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

# retry <attempts> <delay-seconds> <command...>
# New service accounts take a few seconds to propagate through IAM.
retry() {
  local attempts="$1" delay="$2" attempt=1
  shift 2
  until "$@"; do
    if ((attempt >= attempts)); then
      return 1
    fi
    log WARN "Command failed (attempt ${attempt}/${attempts}); retrying in ${delay}s: $1 $2 $3"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

project_name="${1:-programming}"
environment_name="${2:-bash}"
region="${3:-us-central1}"
gcp_project="${4:-${GCP_PROJECT_ID:-${CLOUDSDK_CORE_PROJECT:-}}}"
name_prefix="${environment_name}-${project_name}"

# GCP resource names must be lowercase, start with a letter, and not end in a hyphen.
name_pattern='^[a-z][a-z0-9]*(-[a-z0-9]+)*$'
if [[ ! "$project_name" =~ $name_pattern || ! "$environment_name" =~ $name_pattern ]]; then
  die "Project and environment must start with a lowercase letter and contain only lowercase letters, digits, and single hyphens."
fi
# The VM service account ID (<prefix>-vm) must be 6-30 characters.
((${#name_prefix} <= 27)) || die "environment-project (${name_prefix}) must be 27 characters or fewer."

for command_name in gcloud jq; do
  command -v "$command_name" >/dev/null 2>&1 || die "Required command not found: $command_name"
done

umask 077
work_dir="$(mktemp -d)"
# gcloud on Windows cannot read Git Bash /tmp paths; use the C:/... form.
if command -v cygpath >/dev/null 2>&1; then work_dir="$(cygpath -m "$work_dir")"; fi
trap 'rm -rf "$work_dir"' EXIT
trap 'status=$?; if ((BASH_SUBSHELL == 0)); then log ERROR "Deployment failed at line ${BASH_LINENO[0]:-unknown} (exit ${status}). Resources already created in GCP were not rolled back."; fi; exit "$status"' ERR

# Shared names (destroy-gcpresources.sh uses the same ones).
network="${name_prefix}-vpc"
public_subnet="${name_prefix}-public-${region}"
private_subnet="${name_prefix}-private-${region}"
public_cidr="10.1.1.0/24"
private_cidr="10.1.2.0/24"
psa_range="${name_prefix}-psa-range"
psa_address="10.1.16.0"
psa_prefix_length=20
psa_cidr="${psa_address}/${psa_prefix_length}"
router="${name_prefix}-router"
nat="${name_prefix}-nat"
dns_zone="${name_prefix}-db-internal"
vm_tag="${name_prefix}-vm"
vm_service_account_id="${name_prefix}-vm"
labels="deployment=${name_prefix},env=${environment_name},project_name=${project_name}"
required_apis=(
  compute.googleapis.com
  dns.googleapis.com
  sqladmin.googleapis.com
  servicenetworking.googleapis.com
  secretmanager.googleapis.com
  storage.googleapis.com
  iam.googleapis.com
  cloudresourcemanager.googleapis.com
)

ensure_gcloud_login() {
  gcloud auth print-access-token >/dev/null 2>&1 ||
    die "gcloud credentials are unavailable or expired. Run 'gcloud auth login' (or set up Workload Identity Federation), then rerun."
  if [[ -z "$gcp_project" ]]; then
    gcp_project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  [[ -n "$gcp_project" ]] || die "No GCP project ID. Pass it as the 4th argument, set GCP_PROJECT_ID, or run 'gcloud config set project <id>'."
  gcloud projects describe "$gcp_project" --format='value(projectId)' >/dev/null 2>&1 ||
    die "Cannot access GCP project ${gcp_project}."

  export CLOUDSDK_CORE_PROJECT="$gcp_project"
  export CLOUDSDK_CORE_DISABLE_PROMPTS=1
  vm_service_account="${vm_service_account_id}@${gcp_project}.iam.gserviceaccount.com"
  bucket_name="${project_name}-${environment_name}-transfer-${gcp_project}"
  ((${#bucket_name} <= 63)) || die "Bucket name ${bucket_name} exceeds 63 characters; shorten project/environment."
  log INFO "Deploying to project ${gcp_project} in ${region}."
}

ensure_apis() {
  local enabled api
  local -a missing=()
  enabled="$(gcloud services list --enabled --format='value(config.name)')"
  for api in "${required_apis[@]}"; do
    grep -qx "$api" <<<"$enabled" || missing+=("$api")
  done
  if ((${#missing[@]})); then
    log INFO "Enabling APIs: ${missing[*]}"
    gcloud services enable "${missing[@]}" ||
      die "Could not enable required APIs (${missing[*]}). Ask a project owner to enable them."
  fi
  gcloud compute regions describe "$region" --format='value(name)' >/dev/null 2>&1 ||
    die "Unknown or inaccessible region: ${region}."
}

generate_password() {
  # 16 alphanumeric characters with at least one lower, upper, and digit.
  local candidate
  while :; do
    candidate="$(head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-16)"
    if [[ ${#candidate} -eq 16 && "$candidate" =~ [a-z] && "$candidate" =~ [A-Z] && "$candidate" =~ [0-9] ]]; then
      printf '%s' "$candidate"
      return
    fi
  done
}

create_networking_resources() {
  banner "Networking Resources"
  log INFO "Creating VPC, public/private subnets, Cloud Router + NAT, and private DNS."

  gcloud compute networks create "$network" \
    --subnet-mode=custom \
    --bgp-routing-mode=regional \
    --description="VPC for ${name_prefix}" >/dev/null

  # GCP subnets are regional (they span every zone), so one public and one
  # private subnet replace the per-AZ subnet pairs used on AWS. The VPC's
  # default 0.0.0.0/0 route to the internet gateway serves the public subnet.
  gcloud compute networks subnets create "$public_subnet" \
    --network="$network" \
    --region="$region" \
    --range="$public_cidr" >/dev/null
  gcloud compute networks subnets create "$private_subnet" \
    --network="$network" \
    --region="$region" \
    --range="$private_cidr" \
    --enable-private-ip-google-access >/dev/null

  # Cloud NAT is a managed, regional service: no NAT instances or static IPs
  # per zone are needed. Only the private subnet is NATed.
  gcloud compute routers create "$router" \
    --network="$network" \
    --region="$region" >/dev/null
  gcloud compute routers nats create "$nat" \
    --router="$router" \
    --region="$region" \
    --nat-custom-subnet-ip-ranges="$private_subnet" \
    --auto-allocate-nat-external-ips >/dev/null

  gcloud dns managed-zones create "$dns_zone" \
    --dns-name="db.internal." \
    --description="Stable database names for ${name_prefix}" \
    --visibility=private \
    --networks="$network" \
    --labels="$labels" >/dev/null
}

create_compute_resources() {
  banner "Compute Resources"
  log INFO "Creating the VM service account, transfer bucket, firewall rules, and two private VMs."

  gcloud iam service-accounts create "$vm_service_account_id" \
    --display-name="${environment_name}-${project_name} VM service account" >/dev/null

  # Equivalent of the SSM managed policies: let the guest agents write logs
  # and metrics. IAP + OS Login (below) replace SSM Session Manager.
  local role
  for role in roles/logging.logWriter roles/monitoring.metricWriter; do
    retry 6 10 gcloud projects add-iam-policy-binding "$gcp_project" \
      --member="serviceAccount:${vm_service_account}" \
      --role="$role" \
      --condition=None >/dev/null
  done

  # Google Cloud Storage encrypts all data at rest by default (AES-256).
  gcloud storage buckets create "gs://${bucket_name}" \
    --location="$region" \
    --default-storage-class=STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention >/dev/null
  gcloud storage buckets update "gs://${bucket_name}" --update-labels="$labels" >/dev/null
  for role in roles/storage.objectViewer roles/storage.objectCreator; do
    retry 6 10 gcloud storage buckets add-iam-policy-binding "gs://${bucket_name}" \
      --member="serviceAccount:${vm_service_account}" \
      --role="$role" >/dev/null
  done

  gcloud compute firewall-rules create "${name_prefix}-allow-web" \
    --network="$network" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$vm_tag" \
    --description="HTTP/HTTPS to ${name_prefix} VMs" >/dev/null
  # 35.235.240.0/20 is the source range Identity-Aware Proxy uses for TCP forwarding.
  gcloud compute firewall-rules create "${name_prefix}-allow-iap-ssh" \
    --network="$network" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags="$vm_tag" \
    --description="SSH through IAP TCP forwarding only" >/dev/null

  local -a zones=()
  mapfile -t zones < <(gcloud compute regions describe "$region" --format=json |
    jq -r '.zones[] | split("/") | last' | sort)
  ((${#zones[@]} >= 1)) || die "No zones found in ${region}."
  [[ ${#zones[@]} -ge 2 ]] || zones[1]="${zones[0]}"

  cat >"$work_dir/startup-script.sh" <<'BOOTSTRAP'
#!/bin/bash -e
apt-get update -y
apt-get install -y python3 postgresql-client
BOOTSTRAP

  local instance_index instance_name zone
  local -a instance_names=()
  for instance_index in 1 2; do
    instance_name="${name_prefix}-vm-${instance_index}"
    zone="${zones[instance_index - 1]}"
    retry 6 10 gcloud compute instances create "$instance_name" \
      --zone="$zone" \
      --machine-type=e2-micro \
      --subnet="$private_subnet" \
      --no-address \
      --image-family=debian-12 \
      --image-project=debian-cloud \
      --boot-disk-size=10GB \
      --service-account="$vm_service_account" \
      --scopes=cloud-platform \
      --tags="$vm_tag" \
      --labels="${labels},name=${instance_name}" \
      --metadata=enable-oslogin=TRUE \
      --metadata-from-file="startup-script=${work_dir}/startup-script.sh" \
      --shielded-secure-boot \
      --shielded-vtpm \
      --shielded-integrity-monitoring >/dev/null
    instance_names+=("${instance_name} (${zone})")
  done

  log INFO "VMs launched: ${instance_names[*]}"
}

create_database_resources() {
  banner "Database Resources"
  log INFO "Creating Private Service Access, the private Cloud SQL instance, credentials, and internal DNS record."

  # Cloud SQL private IP lives in a Google-managed VPC peered to ours through
  # an allocated range (Private Service Access).
  gcloud compute addresses create "$psa_range" \
    --global \
    --purpose=VPC_PEERING \
    --addresses="$psa_address" \
    --prefix-length="$psa_prefix_length" \
    --network="$network" \
    --description="Private Service Access range for ${name_prefix}" >/dev/null
  gcloud services vpc-peerings connect \
    --service=servicenetworking.googleapis.com \
    --ranges="$psa_range" \
    --network="$network" >/dev/null

  # Firewall equivalent of "database SG only accepts the EC2 SG": deny 5432
  # egress to the database range from everything, then allow it from the VM
  # tag (plus any extra tags) at a higher priority.
  local allowed_tags="$vm_tag" extra_tag
  local -a extra_tags=()
  if [[ -n "${ADDITIONAL_DB_NETWORK_TAGS:-}" ]]; then
    IFS=',' read -r -a extra_tags <<<"$ADDITIONAL_DB_NETWORK_TAGS"
    for extra_tag in "${extra_tags[@]}"; do
      extra_tag="${extra_tag// /}"
      [[ -n "$extra_tag" ]] || continue
      [[ "$extra_tag" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]] ||
        die "Invalid network tag in ADDITIONAL_DB_NETWORK_TAGS: ${extra_tag}"
      allowed_tags+=",${extra_tag}"
    done
  fi
  gcloud compute firewall-rules create "${name_prefix}-allow-db-egress" \
    --network="$network" \
    --direction=EGRESS \
    --action=ALLOW \
    --rules=tcp:5432 \
    --destination-ranges="$psa_cidr" \
    --target-tags="$allowed_tags" \
    --priority=1000 \
    --description="PostgreSQL from tagged VMs only" >/dev/null
  gcloud compute firewall-rules create "${name_prefix}-deny-db-egress" \
    --network="$network" \
    --direction=EGRESS \
    --action=DENY \
    --rules=tcp:5432 \
    --destination-ranges="$psa_cidr" \
    --priority=1100 \
    --description="Block PostgreSQL from untagged VMs" >/dev/null

  local db_password
  db_password="$(generate_password)"
  local password_secret="${name_prefix}-database-password"
  local credentials_secret="${name_prefix}-database-credentials"
  printf '%s' "$db_password" | gcloud secrets create "$password_secret" \
    --replication-policy=automatic \
    --labels="$labels" \
    --data-file=- >/dev/null
  jq -cn --arg username db_admin_user --arg password "$db_password" \
    '{username:$username,password:$password}' |
    gcloud secrets create "$credentials_secret" \
      --replication-policy=automatic \
      --labels="$labels" \
      --data-file=- >/dev/null

  # Cloud SQL names cannot be reused for about a week after deletion, so add a
  # random suffix; destroy finds the instance by its deployment label.
  local suffix
  suffix="$(head -c 3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  local instance_name="${name_prefix}-pg-${suffix}"

  # Database flags replace the Aurora cluster parameter group:
  # rds.logical_replication -> cloudsql.logical_decoding,
  # shared_preload_libraries=pgaudit -> cloudsql.enable_pgaudit
  # (pg_stat_statements is available as an extension by default).
  # REGIONAL availability = primary + synchronous standby in another zone.
  local operation
  operation="$(gcloud beta sql instances create "$instance_name" \
    --database-version=POSTGRES_17 \
    --edition=enterprise \
    --tier=db-custom-2-4096 \
    --region="$region" \
    --availability-type=REGIONAL \
    --network="projects/${gcp_project}/global/networks/${network}" \
    --no-assign-ip \
    --ssl-mode=ENCRYPTED_ONLY \
    --storage-type=SSD \
    --storage-size=10GB \
    --storage-auto-increase \
    --backup-start-time=07:00 \
    --retained-backups-count=5 \
    --retained-transaction-log-days=5 \
    --enable-point-in-time-recovery \
    --maintenance-window-day=SUN \
    --maintenance-window-hour=9 \
    --insights-config-query-insights-enabled \
    --database-flags=log_connections=on,log_disconnections=on,log_statement=all,cloudsql.logical_decoding=on,max_wal_senders=28,max_logical_replication_workers=10,max_worker_processes=28,cloudsql.enable_pgaudit=on \
    --labels="$labels" \
    --no-deletion-protection \
    --async \
    --format='value(name)')"
  log INFO "Waiting for Cloud SQL instance ${instance_name} (this usually takes 10-20 minutes)."
  gcloud sql operations wait "$operation" --timeout=unlimited >/dev/null

  gcloud sql databases create lustrous --instance="$instance_name" >/dev/null
  gcloud sql users create db_admin_user \
    --instance="$instance_name" \
    --password="$db_password" >/dev/null

  local private_ip
  private_ip="$(gcloud sql instances describe "$instance_name" --format=json |
    jq -r '[.ipAddresses[]? | select(.type=="PRIVATE") | .ipAddress][0] // empty')"
  [[ -n "$private_ip" ]] || die "Cloud SQL instance ${instance_name} has no private IP."

  local dns_record="${project_name}-${environment_name}-postgresql.db.internal."
  gcloud dns record-sets create "$dns_record" \
    --zone="$dns_zone" \
    --type=A \
    --ttl=30 \
    --rrdatas="$private_ip" >/dev/null

  printf 'Cloud SQL instance: %s (private IP %s)\n' "$instance_name" "$private_ip"
  printf 'Private database DNS: %s\n' "${dns_record%.}"
  printf 'Credentials: Secret Manager secret %s\n' "$credentials_secret"
  printf 'Password secret: %s\n' "$password_secret"
}

main() {
  ensure_gcloud_login
  ensure_apis
  create_networking_resources
  create_compute_resources
  create_database_resources
  log INFO "Deployment completed. No SSH key or external VM address was created."
  log INFO "Connect with: gcloud compute ssh ${name_prefix}-vm-1 --tunnel-through-iap --project ${gcp_project}"
  banner "Deployment Complete"
}

main "$@"
