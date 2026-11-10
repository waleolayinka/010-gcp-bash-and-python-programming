#!/usr/bin/env python3
"""Provision Google Cloud resources with Python instead of Terraform.

Purpose: test Python (Google API client libraries) as an alternative to an IaC
tool for building GCP infrastructure; see bash-programming/ for the same stack
in Bash + gcloud.

Resources created:
  Networking: custom-mode VPC with public/private regional subnets
              (10.2.1.0/24, 10.2.2.0/24), Cloud Router + Cloud NAT for the
              private subnet, firewall rules, a Private Service Access range
              (10.2.16.0/20) peered to Google services, and a private Cloud
              DNS zone (db.internal).
  Compute:    two private Compute Engine VMs (spread across two zones) with
              no external IP; reachable only through IAP TCP forwarding with
              OS Login: no SSH keys in metadata. A dedicated VM service
              account and a private GCS transfer bucket.
  Database:   Cloud SQL for PostgreSQL 17, regional HA (primary + standby in
              two zones), private IP only; port 5432 egress is allowed only
              from the VM network tag. Credentials in Secret Manager.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import logging
import os
import re
import secrets
import string
import sys
import time
from typing import Any, Callable

try:
    import google.auth
    from google.auth.exceptions import GoogleAuthError
    from googleapiclient import discovery
    from googleapiclient.errors import HttpError
except ImportError as exc:
    raise SystemExit(
        "Install the Google API client before running this script: "
        "python -m pip install -r python-programming/requirements.txt"
    ) from exc


LOG = logging.getLogger("deploy-gcpresources")
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE_URL = "https://www.googleapis.com/compute/v1"
REQUIRED_APIS = (
    "compute.googleapis.com",
    "dns.googleapis.com",
    "sqladmin.googleapis.com",
    "servicenetworking.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
)
# Equivalent of the SSM managed policies: let guest agents write logs/metrics.
VM_PROJECT_ROLES = ("roles/logging.logWriter", "roles/monitoring.metricWriter")
VM_BUCKET_ROLES = ("roles/storage.objectViewer", "roles/storage.objectCreator")
PUBLIC_CIDR = "10.2.1.0/24"
PRIVATE_CIDR = "10.2.2.0/24"
PSA_ADDRESS = "10.2.16.0"
PSA_PREFIX_LENGTH = 20
IAP_SOURCE_RANGE = "35.235.240.0/20"
NAME_PATTERN = r"[a-z][a-z0-9]*(-[a-z0-9]+)*"
TAG_PATTERN = r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?"
ADDITIONAL_DB_NETWORK_TAGS = os.environ.get("ADDITIONAL_DB_NETWORK_TAGS", "")
STARTUP_SCRIPT = (
    "#!/bin/bash -e\n"
    "apt-get update -y\n"
    "apt-get install -y python3 postgresql-client\n"
)
# Replace the Aurora cluster parameter group: rds.logical_replication ->
# cloudsql.logical_decoding, shared_preload_libraries=pgaudit ->
# cloudsql.enable_pgaudit (pg_stat_statements is available by default).
DATABASE_FLAGS = (
    ("log_connections", "on"),
    ("log_disconnections", "on"),
    ("log_statement", "all"),
    ("cloudsql.logical_decoding", "on"),
    ("max_wal_senders", "28"),
    ("max_logical_replication_workers", "10"),
    ("max_worker_processes", "28"),
    ("cloudsql.enable_pgaudit", "on"),
)


class Gcp:
    """Per-service API clients, like boto3.Session().client(...)."""

    def __init__(self, project: str, region: str, credentials: Any) -> None:
        self.project = project
        self.region = region
        self._credentials = credentials
        self._clients: dict[str, Any] = {}

    def client(self, name: str, version: str) -> Any:
        key = f"{name}/{version}"
        if key not in self._clients:
            self._clients[key] = discovery.build(
                name, version, credentials=self._credentials, cache_discovery=False
            )
        return self._clients[key]

    @property
    def compute(self) -> Any:
        return self.client("compute", "v1")


def banner(title: str) -> None:
    """Print a large section heading, falling back to plain text."""
    print()
    try:
        pyfiglet = importlib.import_module("pyfiglet")
        print(pyfiglet.figlet_format(title, font="slant"), end="")
    except ImportError:
        print(f"========== {title} ==========")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy the networking, compute, and database GCP resources."
    )
    parser.add_argument("project", nargs="?", default="programming")
    parser.add_argument("environment", nargs="?", default="python")
    parser.add_argument("region", nargs="?", default="us-central1")
    parser.add_argument(
        "gcp_project",
        nargs="?",
        default=None,
        help="GCP project ID (default: $GCP_PROJECT_ID, $GOOGLE_CLOUD_PROJECT, "
        "or the Application Default Credentials project)",
    )
    return parser.parse_args()


def validate_name(value: str, label: str) -> None:
    if not re.fullmatch(NAME_PATTERN, value):
        raise ValueError(
            f"{label} must start with a lowercase letter and contain only "
            "lowercase letters, digits, and single hyphens."
        )


def run(request: Any) -> Any:
    """Execute an API request, retrying transient 429/5xx errors."""
    return request.execute(num_retries=5)


def basename(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def with_retries(
    action: Callable[[], Any],
    description: str,
    attempts: int = 6,
    delay: int = 10,
) -> Any:
    """Retry while a new service account propagates through IAM."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except HttpError as exc:
            if attempt == attempts or exc.resp.status not in (400, 404, 409, 412):
                raise
            LOG.warning(
                "%s failed (attempt %d/%d, HTTP %s); retrying in %ds.",
                description,
                attempt,
                attempts,
                exc.resp.status,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def wait_compute(gcp: Gcp, operation: dict[str, Any]) -> dict[str, Any]:
    compute = gcp.compute
    while True:
        if operation.get("zone"):
            request = compute.zoneOperations().wait(
                project=gcp.project,
                zone=basename(operation["zone"]),
                operation=operation["name"],
            )
        elif operation.get("region"):
            request = compute.regionOperations().wait(
                project=gcp.project,
                region=basename(operation["region"]),
                operation=operation["name"],
            )
        else:
            request = compute.globalOperations().wait(
                project=gcp.project, operation=operation["name"]
            )
        operation = run(request)
        if operation["status"] == "DONE":
            errors = operation.get("error", {}).get("errors")
            if errors:
                raise RuntimeError(f"Compute operation failed: {errors}")
            return operation


def wait_long_running(
    operations: Any, operation: dict[str, Any], description: str
) -> dict[str, Any]:
    """Poll a google.longrunning operation (Service Networking, Service Usage)."""
    while not operation.get("done"):
        time.sleep(5)
        operation = run(operations.get(name=operation["name"]))
    if operation.get("error"):
        raise RuntimeError(f"{description} failed: {operation['error']}")
    return operation


def wait_sql(gcp: Gcp, operation: dict[str, Any], description: str) -> None:
    sql = gcp.client("sqladmin", "v1")
    while operation["status"] != "DONE":
        time.sleep(15)
        operation = run(
            sql.operations().get(project=gcp.project, operation=operation["name"])
        )
    errors = operation.get("error", {}).get("errors")
    if errors:
        raise RuntimeError(f"{description} failed: {errors}")


def generate_password(length: int = 16) -> str:
    """Alphanumeric password with at least one lower, upper, and digit."""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


def ensure_apis(gcp: Gcp) -> None:
    usage = gcp.client("serviceusage", "v1")
    parent = f"projects/{gcp.project}"
    enabled: set[str] = set()
    request = usage.services().list(parent=parent, filter="state:ENABLED", pageSize=200)
    while request is not None:
        response = run(request)
        enabled.update(
            service["config"]["name"] for service in response.get("services", [])
        )
        request = usage.services().list_next(request, response)
    missing = [api for api in REQUIRED_APIS if api not in enabled]
    if not missing:
        return
    LOG.info("Enabling APIs: %s", ", ".join(missing))
    try:
        operation = run(
            usage.services().batchEnable(parent=parent, body={"serviceIds": missing})
        )
        wait_long_running(usage.operations(), operation, "Enabling APIs")
    except HttpError as exc:
        raise RuntimeError(
            f"Could not enable required APIs ({', '.join(missing)}). "
            "Ask a project owner to enable them."
        ) from exc


def add_project_bindings(gcp: Gcp, member: str, roles: tuple[str, ...]) -> None:
    crm = gcp.client("cloudresourcemanager", "v1")

    def update() -> None:
        policy = run(
            crm.projects().getIamPolicy(
                resource=gcp.project,
                body={"options": {"requestedPolicyVersion": 3}},
            )
        )
        bindings = policy.setdefault("bindings", [])
        for role in roles:
            binding = next(
                (b for b in bindings if b["role"] == role and "condition" not in b),
                None,
            )
            if binding is None:
                bindings.append({"role": role, "members": [member]})
            elif member not in binding["members"]:
                binding["members"].append(member)
        policy["version"] = 3
        run(crm.projects().setIamPolicy(resource=gcp.project, body={"policy": policy}))

    with_retries(update, f"Granting {', '.join(roles)} to {member}")


def add_bucket_bindings(
    gcp: Gcp, bucket: str, member: str, roles: tuple[str, ...]
) -> None:
    storage = gcp.client("storage", "v1")

    def update() -> None:
        policy = run(
            storage.buckets().getIamPolicy(bucket=bucket, optionsRequestedPolicyVersion=3)
        )
        bindings = policy.setdefault("bindings", [])
        for role in roles:
            binding = next((b for b in bindings if b["role"] == role), None)
            if binding is None:
                bindings.append({"role": role, "members": [member]})
            elif member not in binding["members"]:
                binding["members"].append(member)
        run(storage.buckets().setIamPolicy(bucket=bucket, body=policy))

    with_retries(update, f"Granting bucket access to {member}")


def create_networking(gcp: Gcp, prefix: str, labels: dict[str, str]) -> dict[str, Any]:
    compute = gcp.compute
    project, region = gcp.project, gcp.region
    banner("Networking Resources")
    LOG.info("Creating VPC, subnets, Cloud Router + NAT, and private DNS.")

    network = f"{prefix}-vpc"
    network_url = f"{COMPUTE_URL}/projects/{project}/global/networks/{network}"
    wait_compute(
        gcp,
        run(
            compute.networks().insert(
                project=project,
                body={
                    "name": network,
                    "description": f"VPC for {prefix}",
                    "autoCreateSubnetworks": False,
                    "routingConfig": {"routingMode": "REGIONAL"},
                },
            )
        ),
    )

    # GCP subnets are regional (they span every zone), so one public and one
    # private subnet replace the per-AZ subnet pairs used on AWS. The VPC's
    # default 0.0.0.0/0 route to the internet gateway serves the public subnet.
    public_subnet = f"{prefix}-public-{region}"
    private_subnet = f"{prefix}-private-{region}"
    for name, cidr, private in (
        (public_subnet, PUBLIC_CIDR, False),
        (private_subnet, PRIVATE_CIDR, True),
    ):
        wait_compute(
            gcp,
            run(
                compute.subnetworks().insert(
                    project=project,
                    region=region,
                    body={
                        "name": name,
                        "network": network_url,
                        "ipCidrRange": cidr,
                        "privateIpGoogleAccess": private,
                    },
                )
            ),
        )
    private_subnet_url = (
        f"{COMPUTE_URL}/projects/{project}/regions/{region}/subnetworks/{private_subnet}"
    )

    # Cloud NAT is a managed, regional service: no NAT instances or static
    # IPs per zone are needed. Only the private subnet is NATed.
    wait_compute(
        gcp,
        run(
            compute.routers().insert(
                project=project,
                region=region,
                body={
                    "name": f"{prefix}-router",
                    "network": network_url,
                    "nats": [
                        {
                            "name": f"{prefix}-nat",
                            "natIpAllocateOption": "AUTO_ONLY",
                            "sourceSubnetworkIpRangesToNat": "LIST_OF_SUBNETWORKS",
                            "subnetworks": [
                                {
                                    "name": private_subnet_url,
                                    "sourceIpRangesToNat": ["ALL_IP_RANGES"],
                                }
                            ],
                        }
                    ],
                },
            )
        ),
    )

    dns_zone = f"{prefix}-db-internal"
    run(
        gcp.client("dns", "v1").managedZones().create(
            project=project,
            body={
                "name": dns_zone,
                "dnsName": "db.internal.",
                "description": f"Stable database names for {prefix}",
                "visibility": "private",
                "privateVisibilityConfig": {"networks": [{"networkUrl": network_url}]},
                "labels": labels,
            },
        )
    )

    return {
        "network": network,
        "network_url": network_url,
        "private_subnet_url": private_subnet_url,
        "dns_zone": dns_zone,
    }


def create_compute(
    gcp: Gcp,
    network: dict[str, Any],
    project_name: str,
    environment: str,
    labels: dict[str, str],
) -> dict[str, Any]:
    prefix = f"{environment}-{project_name}"
    compute = gcp.compute
    project, region = gcp.project, gcp.region
    banner("Compute Resources")
    LOG.info(
        "Creating the VM service account, transfer bucket, firewall rules, and two private VMs."
    )

    iam = gcp.client("iam", "v1")
    account = run(
        iam.projects().serviceAccounts().create(
            name=f"projects/{project}",
            body={
                "accountId": f"{prefix}-vm",
                "serviceAccount": {
                    "displayName": f"{environment}-{project_name} VM service account"
                },
            },
        )
    )
    service_account = account["email"]
    member = f"serviceAccount:{service_account}"
    add_project_bindings(gcp, member, VM_PROJECT_ROLES)

    # Google Cloud Storage encrypts all data at rest by default (AES-256).
    bucket_name = f"{project_name}-{environment}-transfer-{project}"
    run(
        gcp.client("storage", "v1").buckets().insert(
            project=project,
            body={
                "name": bucket_name,
                "location": region,
                "storageClass": "STANDARD",
                "labels": labels,
                "iamConfiguration": {
                    "uniformBucketLevelAccess": {"enabled": True},
                    "publicAccessPrevention": "enforced",
                },
            },
        )
    )
    add_bucket_bindings(gcp, bucket_name, member, VM_BUCKET_ROLES)

    vm_tag = f"{prefix}-vm"
    for name, ports, source, description in (
        (f"{prefix}-allow-web", ["80", "443"], "0.0.0.0/0", f"HTTP/HTTPS to {prefix} VMs"),
        # Identity-Aware Proxy TCP forwarding source range.
        (f"{prefix}-allow-iap-ssh", ["22"], IAP_SOURCE_RANGE, "SSH through IAP TCP forwarding only"),
    ):
        wait_compute(
            gcp,
            run(
                compute.firewalls().insert(
                    project=project,
                    body={
                        "name": name,
                        "network": network["network_url"],
                        "direction": "INGRESS",
                        "allowed": [{"IPProtocol": "tcp", "ports": ports}],
                        "sourceRanges": [source],
                        "targetTags": [vm_tag],
                        "description": description,
                    },
                )
            ),
        )

    zones = sorted(
        basename(url)
        for url in run(compute.regions().get(project=project, region=region))["zones"]
    )
    if not zones:
        raise RuntimeError(f"No zones found in {region}.")
    zones = (zones + zones)[:2]

    instance_names: list[str] = []
    for index, zone in enumerate(zones, start=1):
        instance_name = f"{prefix}-vm-{index}"
        body = {
            "name": instance_name,
            "machineType": f"zones/{zone}/machineTypes/e2-micro",
            "labels": {**labels, "name": instance_name},
            "tags": {"items": [vm_tag]},
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "initializeParams": {
                        "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                        "diskSizeGb": "10",
                    },
                }
            ],
            # No accessConfigs -> no external IP address.
            "networkInterfaces": [{"subnetwork": network["private_subnet_url"]}],
            "serviceAccounts": [
                {"email": service_account, "scopes": [CLOUD_PLATFORM_SCOPE]}
            ],
            "metadata": {
                "items": [
                    {"key": "enable-oslogin", "value": "TRUE"},
                    {"key": "startup-script", "value": STARTUP_SCRIPT},
                ]
            },
            "shieldedInstanceConfig": {
                "enableSecureBoot": True,
                "enableVtpm": True,
                "enableIntegrityMonitoring": True,
            },
        }
        with_retries(
            lambda zone=zone, body=body: wait_compute(
                gcp,
                run(compute.instances().insert(project=project, zone=zone, body=body)),
            ),
            f"Creating VM {instance_name}",
        )
        instance_names.append(f"{instance_name} ({zone})")
    LOG.info("VMs launched: %s", ", ".join(instance_names))
    return {"vm_tag": vm_tag, "service_account": service_account}


def create_database(
    gcp: Gcp,
    network: dict[str, Any],
    compute_result: dict[str, Any],
    project_name: str,
    environment: str,
    labels: dict[str, str],
    extra_tags: list[str],
) -> dict[str, str]:
    prefix = f"{environment}-{project_name}"
    compute = gcp.compute
    project, region = gcp.project, gcp.region
    banner("Database Resources")
    LOG.info(
        "Creating Private Service Access, private Cloud SQL, credentials, and internal DNS."
    )

    # Cloud SQL private IP lives in a Google-managed VPC peered to ours
    # through an allocated range (Private Service Access).
    psa_range = f"{prefix}-psa-range"
    psa_cidr = f"{PSA_ADDRESS}/{PSA_PREFIX_LENGTH}"
    wait_compute(
        gcp,
        run(
            compute.globalAddresses().insert(
                project=project,
                body={
                    "name": psa_range,
                    "description": f"Private Service Access range for {prefix}",
                    "purpose": "VPC_PEERING",
                    "addressType": "INTERNAL",
                    "address": PSA_ADDRESS,
                    "prefixLength": PSA_PREFIX_LENGTH,
                    "network": network["network_url"],
                },
            )
        ),
    )
    project_number = run(
        gcp.client("cloudresourcemanager", "v1").projects().get(projectId=project)
    )["projectNumber"]
    networking = gcp.client("servicenetworking", "v1")
    wait_long_running(
        networking.operations(),
        run(
            networking.services().connections().create(
                parent="services/servicenetworking.googleapis.com",
                body={
                    "network": f"projects/{project_number}/global/networks/{network['network']}",
                    "reservedPeeringRanges": [psa_range],
                },
            )
        ),
        "Private Service Access peering",
    )

    # Firewall equivalent of "database SG only accepts the EC2 SG": deny 5432
    # egress to the database range from everything, then allow it from the
    # VM tag (plus any extra tags) at a higher priority.
    for name, action, priority, target_tags, description in (
        (
            f"{prefix}-allow-db-egress",
            "allowed",
            1000,
            [compute_result["vm_tag"], *extra_tags],
            "PostgreSQL from tagged VMs only",
        ),
        (f"{prefix}-deny-db-egress", "denied", 1100, [], "Block PostgreSQL from untagged VMs"),
    ):
        body: dict[str, Any] = {
            "name": name,
            "network": network["network_url"],
            "direction": "EGRESS",
            action: [{"IPProtocol": "tcp", "ports": ["5432"]}],
            "destinationRanges": [psa_cidr],
            "priority": priority,
            "description": description,
        }
        if target_tags:
            body["targetTags"] = target_tags
        wait_compute(gcp, run(compute.firewalls().insert(project=project, body=body)))

    password = generate_password()
    secret_manager = gcp.client("secretmanager", "v1")
    password_secret = f"{prefix}-database-password"
    credentials_secret = f"{prefix}-database-credentials"
    for secret_id, value in (
        (password_secret, password),
        (credentials_secret, json.dumps({"username": "db_admin_user", "password": password})),
    ):
        run(
            secret_manager.projects().secrets().create(
                parent=f"projects/{project}",
                secretId=secret_id,
                body={"replication": {"automatic": {}}, "labels": labels},
            )
        )
        run(
            secret_manager.projects().secrets().addVersion(
                parent=f"projects/{project}/secrets/{secret_id}",
                body={"payload": {"data": base64.b64encode(value.encode()).decode()}},
            )
        )

    # Cloud SQL names cannot be reused for about a week after deletion, so add
    # a random suffix; destroy finds the instance by its deployment label.
    instance_name = f"{prefix}-pg-{secrets.token_hex(3)}"
    sql = gcp.client("sqladmin", "v1")
    operation = run(
        sql.instances().insert(
            project=project,
            body={
                "name": instance_name,
                "databaseVersion": "POSTGRES_17",
                "region": region,
                "settings": {
                    "edition": "ENTERPRISE",
                    "tier": "db-custom-2-4096",
                    # Primary + synchronous standby in another zone.
                    "availabilityType": "REGIONAL",
                    "dataDiskType": "PD_SSD",
                    "dataDiskSizeGb": "10",
                    "storageAutoResize": True,
                    "ipConfiguration": {
                        "ipv4Enabled": False,
                        "privateNetwork": f"projects/{project}/global/networks/{network['network']}",
                        "allocatedIpRange": psa_range,
                        "sslMode": "ENCRYPTED_ONLY",
                    },
                    "backupConfiguration": {
                        "enabled": True,
                        "startTime": "07:00",
                        "pointInTimeRecoveryEnabled": True,
                        "backupRetentionSettings": {
                            "retentionUnit": "COUNT",
                            "retainedBackups": 5,
                        },
                    },
                    # day: 1=Monday ... 7=Sunday
                    "maintenanceWindow": {"day": 7, "hour": 9},
                    "insightsConfig": {"queryInsightsEnabled": True},
                    "databaseFlags": [
                        {"name": name, "value": value} for name, value in DATABASE_FLAGS
                    ],
                    "userLabels": labels,
                    "deletionProtectionEnabled": False,
                },
            },
        )
    )
    LOG.info(
        "Waiting for Cloud SQL instance %s (this usually takes 10-20 minutes).",
        instance_name,
    )
    wait_sql(gcp, operation, f"Creating Cloud SQL instance {instance_name}")
    wait_sql(
        gcp,
        run(
            sql.databases().insert(
                project=project, instance=instance_name, body={"name": "lustrous"}
            )
        ),
        "Creating database lustrous",
    )
    wait_sql(
        gcp,
        run(
            sql.users().insert(
                project=project,
                instance=instance_name,
                body={"name": "db_admin_user", "password": password},
            )
        ),
        "Creating database user",
    )

    instance = run(sql.instances().get(project=project, instance=instance_name))
    private_ip = next(
        (
            address["ipAddress"]
            for address in instance.get("ipAddresses", [])
            if address.get("type") == "PRIVATE"
        ),
        None,
    )
    if not private_ip:
        raise RuntimeError(f"Cloud SQL instance {instance_name} has no private IP.")

    dns_name = f"{project_name}-{environment}-postgresql.db.internal."
    run(
        gcp.client("dns", "v1").resourceRecordSets().create(
            project=project,
            managedZone=network["dns_zone"],
            body={"name": dns_name, "type": "A", "ttl": 30, "rrdatas": [private_ip]},
        )
    )
    return {
        "instance_name": instance_name,
        "private_ip": private_ip,
        "dns_name": dns_name.rstrip("."),
        "credentials_secret": credentials_secret,
        "password_secret": password_secret,
    }


def resolve_project(explicit: str | None, default_project: str | None) -> str:
    project = (
        explicit
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or default_project
    )
    if not project:
        raise ValueError(
            "No GCP project ID. Pass it as the 4th argument, set GCP_PROJECT_ID, "
            "or run 'gcloud auth application-default set-quota-project <id>'."
        )
    return project


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    args = parse_args()
    try:
        validate_name(args.project, "Project")
        validate_name(args.environment, "Environment")
        prefix = f"{args.environment}-{args.project}"
        if len(prefix) > 27:
            raise ValueError(
                f"environment-project ({prefix}) must be 27 characters or fewer."
            )
        extra_tags = [
            tag.strip() for tag in ADDITIONAL_DB_NETWORK_TAGS.split(",") if tag.strip()
        ]
        for tag in extra_tags:
            if not re.fullmatch(TAG_PATTERN, tag):
                raise ValueError(
                    f"Invalid network tag in ADDITIONAL_DB_NETWORK_TAGS: {tag}"
                )

        credentials, default_project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        project = resolve_project(args.gcp_project, default_project)
        bucket_name = f"{args.project}-{args.environment}-transfer-{project}"
        if len(bucket_name) > 63:
            raise ValueError(
                f"Bucket name {bucket_name} exceeds 63 characters; "
                "shorten project/environment."
            )
        gcp = Gcp(project, args.region, credentials)
        LOG.info("Deploying to project %s in %s.", project, args.region)
        labels = {
            "deployment": prefix,
            "env": args.environment,
            "project_name": args.project,
        }

        ensure_apis(gcp)
        try:
            run(gcp.compute.regions().get(project=project, region=args.region))
        except HttpError as exc:
            raise ValueError(f"Unknown or inaccessible region: {args.region}.") from exc
        network = create_networking(gcp, prefix, labels)
        compute_result = create_compute(
            gcp, network, args.project, args.environment, labels
        )
        database = create_database(
            gcp,
            network,
            compute_result,
            args.project,
            args.environment,
            labels,
            extra_tags,
        )
        print(
            f"Cloud SQL instance: {database['instance_name']} "
            f"(private IP {database['private_ip']})"
        )
        print(f"Private database DNS: {database['dns_name']}")
        print(f"Credentials: Secret Manager secret {database['credentials_secret']}")
        print(f"Password secret: {database['password_secret']}")
        LOG.info("Deployment completed. No SSH key or external VM address was created.")
        LOG.info(
            "Connect with: gcloud compute ssh %s-vm-1 --tunnel-through-iap --project %s",
            prefix,
            project,
        )
        banner("Deployment Complete")
        return 0
    except (ValueError, RuntimeError, HttpError, GoogleAuthError) as exc:
        LOG.error("Deployment failed: %s", exc)
        LOG.error("Resources already created in GCP were not automatically rolled back.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
