# 010-gcp-bash-and-python-programming

Provision the same Google Cloud stack twice, once with **Bash + gcloud** and
once with **Python + the Google API client**, without an IaC tool such as
Terraform. This is the GCP port of
[009-aws-python-and-bash-programming](https://github.com/waleolayinka/009-aws-python-and-bash-programming).

## What gets deployed

| Layer | Resources |
|---|---|
| Networking | Custom-mode VPC, public + private regional subnets, Cloud Router + Cloud NAT (private subnet only), firewall rules, Private Service Access range + peering, private Cloud DNS zone `db.internal` |
| Compute | Two e2-micro Debian 12 VMs in two zones, no external IP, OS Login + IAP TCP forwarding (no SSH keys), Shielded VM, dedicated VM service account, private GCS transfer bucket |
| Database | AlloyDB for PostgreSQL 17: cluster `<prefix>-alloydb` with a 2 vCPU regional-HA primary instance `<prefix>-alloydb-primary`, private IP only, SSL required, Query Insights, continuous backup (5-day PITR window), 5 retained daily backups, database `lustrous`, superuser `db_admin_user`, credentials in Secret Manager, `A` record `<project>-<env>-postgresql.db.internal` |

| | Bash | Python |
|---|---|---|
| Scripts | `bash-programming/deploy-gcpresources.sh`, `destroy-gcpresources.sh` | `python-programming/deploy-gcpresources.py`, `destroy-gcpresources.py` |
| Default environment | `bash` | `python` |
| VPC CIDRs | `10.1.1.0/24`, `10.1.2.0/24`, PSA `10.1.16.0/20` | `10.2.1.0/24`, `10.2.2.0/24`, PSA `10.2.16.0/20` |

Resource names use `<environment>-<project>`, e.g. `bash-programming-vpc`.

## AWS to GCP mapping

| AWS (009) | GCP (010) |
|---|---|
| VPC, subnets per AZ, IGW, route tables | VPC with regional subnets; the default internet route replaces IGW + public route table |
| NAT gateway + Elastic IP per AZ | One Cloud Router + Cloud NAT (managed, regional) |
| Security groups | Firewall rules targeting network tags. The DB rule is an egress allow (VM tag) + egress deny (everyone else) on 5432 |
| `ADDITIONAL_RDS_SECURITY_GROUP_IDS` | `ADDITIONAL_DB_NETWORK_TAGS` (comma-separated tags in the same VPC) |
| Route 53 private hosted zone | Cloud DNS private zone bound to the VPC |
| EC2 + SSM Session Manager | Compute Engine + IAP TCP forwarding + OS Login |
| IAM role + instance profile + SSM policies | VM service account with `logging.logWriter` and `monitoring.metricWriter` |
| S3 bucket (SSE, public access block) | GCS bucket (default encryption, uniform access, public access prevention) |
| Aurora PostgreSQL cluster, 2 instances | AlloyDB cluster with a `REGIONAL` primary instance (active + standby node in two zones, automatic failover) |
| Aurora master user + `DatabaseName` | AlloyDB built-in user `db_admin_user` with `alloydbsuperuser`; `lustrous` is created with `psql` from vm-1 over IAP SSH, since AlloyDB has no create-database API |
| Aurora PITR + backup retention period | Continuous backup (PITR) + automated backup policy |
| Cluster parameter group | Instance database flags (`alloydb.logical_decoding`, `alloydb.enable_pgaudit`, logging, worker limits) |
| Performance Insights / Enhanced Monitoring | Query Insights / built-in Cloud Monitoring |
| Secrets Manager secret + SSM SecureString | Two Secret Manager secrets (`-database-credentials` JSON, `-database-password`) |
| GitHub OIDC provider + IAM role | Workload Identity Federation pool/provider + deploy service account |
| boto3 | `google-api-python-client` (discovery clients, one per service) |

Notes:

- AlloyDB has no API for creating databases, so the deploy scripts create
  `lustrous` by running `psql` on vm-1 over IAP SSH (the Python script calls
  `gcloud` for this one step). Whoever runs deploy needs
  `roles/iap.tunnelResourceAccessor` and an OS Login role. If that step
  fails, the deploy still completes and prints the commands to run by hand.
- `max_wal_senders` is left at AlloyDB's default: AlloyDB requires at least
  50, which is already above Aurora's 28.
- Cluster and instance creation usually take 15–25 minutes together. AlloyDB
  with regional HA is by far the most expensive part of the stack (two 2 vCPU /
  16 GB nodes plus storage), so run destroy when you're done. Destroy also
  deletes the cluster's automated backups, which otherwise outlive it.

## Prerequisites

- A GCP project with billing enabled.
- Bash: `gcloud` and `jq` (`figlet` optional for banners).
- Python 3.12+: `python -m pip install -r python-programming/requirements.txt`.
  Authenticate with `gcloud auth application-default login`.
- The deploy scripts enable the required APIs if they're missing.

## Run locally

```bash
gcloud auth login
gcloud config set project <gcp-project-id>

# Bash
bash bash-programming/deploy-gcpresources.sh programming bash us-central1
bash bash-programming/destroy-gcpresources.sh programming bash us-central1

# Python
python python-programming/deploy-gcpresources.py programming python us-central1
python python-programming/destroy-gcpresources.py programming python us-central1
```

Arguments: `[project] [environment] [region] [gcp-project-id]`. The GCP project
falls back to `$GCP_PROJECT_ID`, then the active gcloud / ADC project. Destroy
asks you to type the `<environment>-<project>` prefix unless you pass `--yes`.

Connect to a VM (needs `roles/iap.tunnelResourceAccessor` and an OS Login role):

```bash
gcloud compute ssh bash-programming-vm-1 --tunnel-through-iap --zone <zone>
```

## GitHub Actions

One-time setup (requires project owner rights and an authenticated `gh` CLI):

```bash
./github-oidc.sh [github-owner/repo] [service-account-id] [branch] [gcp-project-id]
```

This enables the APIs, creates a Workload Identity pool and provider that only
trust workflows on the chosen branch (default `main`), creates the deploy
service account with the roles the scripts need, and sets these repo secrets:
`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `GCP_PROJECT_ID`.
`ADDITIONAL_DB_NETWORK_TAGS` is an optional secret.

Workflows (manual `workflow_dispatch`, `main` only):

| Workflow | Runs |
|---|---|
| `deploy-bash-pipeline.yml` | `bash-programming/deploy-gcpresources.sh` |
| `destroy-bash-resources.yml` | `bash-programming/destroy-gcpresources.sh --yes` (requires `confirm_destroy`) |
| `deploy-python-pipeline.yml` | `python-programming/deploy-gcpresources.py` |
| `destroy-python-resources.yml` | `python-programming/destroy-gcpresources.py --yes` (requires `confirm_destroy`) |

## Git helpers

`git.sh` / `git.py` commit `bash-programming/` or `python-programming/`, plus
their workflows, to the `bash` / `python` branch and merge the result into `main`.
