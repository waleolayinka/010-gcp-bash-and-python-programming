#!/usr/bin/env python3
"""Delete the GCP resources created by deploy-gcpresources.py."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import re
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


LOG = logging.getLogger("destroy-gcpresources")
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
NAME_PATTERN = r"[a-z][a-z0-9]*(-[a-z0-9]+)*"
PEERING_NAME = "servicenetworking-googleapis-com"
FIREWALL_RULES = ("allow-web", "allow-iap-ssh", "allow-db-egress", "deny-db-egress")


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
        description="Tear down resources created by deploy-gcpresources.py."
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
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation prompt",
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


def is_not_found(exc: HttpError) -> bool:
    return exc.resp.status in (404, 410)


def get_or_none(request: Any) -> Any:
    try:
        return run(request)
    except HttpError as exc:
        if is_not_found(exc):
            return None
        raise


def basename(url: str) -> str:
    return url.rsplit("/", 1)[-1]


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
    while not operation.get("done"):
        time.sleep(5)
        operation = run(operations.get(name=operation["name"]))
    if operation.get("error"):
        raise RuntimeError(f"{description} failed: {operation['error']}")
    return operation


def delete_compute_if_exists(
    gcp: Gcp, getter: Callable[[], Any], deleter: Callable[[], Any], description: str
) -> None:
    if get_or_none(getter()) is None:
        return
    LOG.info("Deleting %s.", description)
    try:
        wait_compute(gcp, run(deleter()))
    except HttpError as exc:
        if not is_not_found(exc):
            raise


def delete_compute(gcp: Gcp, prefix: str) -> None:
    banner("Compute Resources")
    compute = gcp.compute
    operations: list[dict[str, Any]] = []
    request = compute.instances().aggregatedList(
        project=gcp.project, filter=f"name eq {prefix}-vm-[12]"
    )
    while request is not None:
        response = run(request)
        for scope in response.get("items", {}).values():
            for instance in scope.get("instances", []):
                zone = basename(instance["zone"])
                LOG.info("Deleting VM %s in %s.", instance["name"], zone)
                operations.append(
                    run(
                        compute.instances().delete(
                            project=gcp.project, zone=zone, instance=instance["name"]
                        )
                    )
                )
        request = compute.instances().aggregatedList_next(request, response)
    for operation in operations:
        wait_compute(gcp, operation)


def delete_database(gcp: Gcp, prefix: str) -> None:
    banner("Database Resources")
    alloydb = gcp.client("alloydb", "v1")
    locations = alloydb.projects().locations()
    parent = f"projects/{gcp.project}/locations/{gcp.region}"
    cluster_name = f"{parent}/clusters/{prefix}-alloydb"
    if get_or_none(locations.clusters().get(name=cluster_name)) is not None:
        # force=True also deletes the cluster's instances (and continuous backup).
        LOG.info("Deleting AlloyDB cluster %s-alloydb and its instances.", prefix)
        wait_long_running(
            locations.operations(),
            run(locations.clusters().delete(name=cluster_name, force=True)),
            "Deleting AlloyDB cluster",
        )

    # Automated backups outlive their cluster until they expire; remove them.
    backups: list[str] = []
    request = locations.backups().list(parent=parent)
    while request is not None:
        response = run(request)
        backups.extend(
            backup["name"]
            for backup in response.get("backups", [])
            if backup.get("clusterName", "").endswith(f"/clusters/{prefix}-alloydb")
        )
        request = locations.backups().list_next(request, response)
    for name in backups:
        LOG.info("Deleting AlloyDB backup %s.", basename(name))
        wait_long_running(
            locations.operations(),
            run(locations.backups().delete(name=name)),
            f"Deleting AlloyDB backup {basename(name)}",
        )


def delete_database_credentials(gcp: Gcp, prefix: str) -> None:
    banner("Database Credentials")
    secret_manager = gcp.client("secretmanager", "v1")
    for secret_id in (f"{prefix}-database-password", f"{prefix}-database-credentials"):
        name = f"projects/{gcp.project}/secrets/{secret_id}"
        try:
            run(secret_manager.projects().secrets().delete(name=name))
            LOG.info("Deleted Secret Manager secret %s.", secret_id)
        except HttpError as exc:
            if not is_not_found(exc):
                raise


def delete_transfer_bucket(gcp: Gcp, bucket_name: str) -> None:
    banner("Transfer Bucket")
    storage = gcp.client("storage", "v1")
    if get_or_none(storage.buckets().get(bucket=bucket_name)) is None:
        return

    LOG.info("Removing objects and versions from bucket %s.", bucket_name)
    request = storage.objects().list(bucket=bucket_name, versions=True)
    while request is not None:
        response = run(request)
        for item in response.get("items", []):
            try:
                run(
                    storage.objects().delete(
                        bucket=bucket_name,
                        object=item["name"],
                        generation=item["generation"],
                    )
                )
            except HttpError as exc:
                if not is_not_found(exc):
                    raise
        request = storage.objects().list_next(request, response)
    run(storage.buckets().delete(bucket=bucket_name))
    LOG.info("Deleted bucket %s.", bucket_name)


def delete_iam(gcp: Gcp, prefix: str) -> None:
    banner("IAM Resources")
    email = f"{prefix}-vm@{gcp.project}.iam.gserviceaccount.com"
    member = f"serviceAccount:{email}"
    iam = gcp.client("iam", "v1")
    account_name = f"projects/{gcp.project}/serviceAccounts/{email}"
    if get_or_none(iam.projects().serviceAccounts().get(name=account_name)) is None:
        return

    # Remove project bindings first; deleting the account would leave
    # "deleted:serviceAccount:..." members behind in the project policy.
    crm = gcp.client("cloudresourcemanager", "v1")
    for attempt in range(1, 6):
        policy = run(
            crm.projects().getIamPolicy(
                resource=gcp.project,
                body={"options": {"requestedPolicyVersion": 3}},
            )
        )
        changed = False
        bindings = []
        for binding in policy.get("bindings", []):
            if member in binding.get("members", []):
                binding["members"].remove(member)
                changed = True
                LOG.info("Removing %s from %s.", binding["role"], email)
            if binding.get("members"):
                bindings.append(binding)
        if not changed:
            break
        policy["bindings"] = bindings
        policy["version"] = 3
        try:
            run(
                crm.projects().setIamPolicy(
                    resource=gcp.project, body={"policy": policy}
                )
            )
            break
        except HttpError as exc:
            # 409: concurrent policy change (etag mismatch); reread and retry.
            if exc.resp.status != 409 or attempt == 5:
                raise
            time.sleep(3)

    LOG.info("Deleting service account %s.", email)
    try:
        run(iam.projects().serviceAccounts().delete(name=account_name))
    except HttpError as exc:
        if not is_not_found(exc):
            raise


def delete_networking(
    gcp: Gcp, prefix: str, project_name: str, environment: str
) -> None:
    banner("Networking Resources")
    compute = gcp.compute
    project, region = gcp.project, gcp.region
    network = f"{prefix}-vpc"

    dns = gcp.client("dns", "v1")
    dns_zone = f"{prefix}-db-internal"
    if get_or_none(dns.managedZones().get(project=project, managedZone=dns_zone)):
        dns_record = f"{project_name}-{environment}-postgresql.db.internal."
        records = run(
            dns.resourceRecordSets().list(
                project=project, managedZone=dns_zone, name=dns_record, type="A"
            )
        ).get("rrsets", [])
        for record in records:
            LOG.info("Removing Cloud DNS record %s.", record["name"])
            run(
                dns.resourceRecordSets().delete(
                    project=project,
                    managedZone=dns_zone,
                    name=record["name"],
                    type=record["type"],
                )
            )
        LOG.info("Deleting private DNS zone %s.", dns_zone)
        run(dns.managedZones().delete(project=project, managedZone=dns_zone))

    for rule in FIREWALL_RULES:
        name = f"{prefix}-{rule}"
        delete_compute_if_exists(
            gcp,
            lambda name=name: compute.firewalls().get(project=project, firewall=name),
            lambda name=name: compute.firewalls().delete(project=project, firewall=name),
            f"firewall rule {name}",
        )

    # Deleting the router also deletes its Cloud NAT gateway and NAT IPs.
    router = f"{prefix}-router"
    delete_compute_if_exists(
        gcp,
        lambda: compute.routers().get(project=project, region=region, router=router),
        lambda: compute.routers().delete(project=project, region=region, router=router),
        f"Cloud Router {router} and its NAT",
    )

    network_info = run(compute.networks().get(project=project, network=network))
    if any(p.get("name") == PEERING_NAME for p in network_info.get("peerings", [])):
        LOG.info("Removing Private Service Access peering from %s.", network)
        project_number = run(
            gcp.client("cloudresourcemanager", "v1").projects().get(projectId=project)
        )["projectNumber"]
        networking = gcp.client("servicenetworking", "v1")
        try:
            wait_long_running(
                networking.operations(),
                run(
                    networking.services().connections().deleteConnection(
                        name=f"services/servicenetworking.googleapis.com/connections/{PEERING_NAME}",
                        body={
                            "consumerNetwork": f"projects/{project_number}/global/networks/{network}"
                        },
                    )
                ),
                "Deleting Private Service Access connection",
            )
        except (HttpError, RuntimeError) as exc:
            # Service Networking can refuse for a while after AlloyDB
            # deletion; removing the peering from our side frees the network.
            LOG.warning("Service Networking refused (%s); deleting the VPC peering directly.", exc)
            wait_compute(
                gcp,
                run(
                    compute.networks().removePeering(
                        project=project, network=network, body={"name": PEERING_NAME}
                    )
                ),
            )

    psa_range = f"{prefix}-psa-range"
    delete_compute_if_exists(
        gcp,
        lambda: compute.globalAddresses().get(project=project, address=psa_range),
        lambda: compute.globalAddresses().delete(project=project, address=psa_range),
        f"Private Service Access range {psa_range}",
    )

    for subnet in (f"{prefix}-public-{region}", f"{prefix}-private-{region}"):
        delete_compute_if_exists(
            gcp,
            lambda subnet=subnet: compute.subnetworks().get(
                project=project, region=region, subnetwork=subnet
            ),
            lambda subnet=subnet: compute.subnetworks().delete(
                project=project, region=region, subnetwork=subnet
            ),
            f"subnet {subnet}",
        )

    LOG.info("Deleting VPC %s.", network)
    wait_compute(gcp, run(compute.networks().delete(project=project, network=network)))


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
        credentials, default_project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        project = resolve_project(args.gcp_project, default_project)
        prefix = f"{args.environment}-{args.project}"
        if not args.yes:
            if not sys.stdin.isatty():
                LOG.error(
                    "A confirmation prompt needs a terminal. "
                    "Rerun interactively or pass --yes."
                )
                return 2
            print(
                f"This permanently deletes the {args.project}-{args.environment} "
                f"deployment in {args.region} (GCP project {project}), "
                "including database data and bucket contents."
            )
            if input(f"Type {prefix} to continue: ") != prefix:
                LOG.error("Confirmation did not match; nothing was deleted.")
                return 2
        LOG.info(
            "Tearing down %s in project %s, region %s.",
            prefix,
            project,
            args.region,
        )
        gcp = Gcp(project, args.region, credentials)

        network_exists = (
            get_or_none(gcp.compute.networks().get(project=project, network=f"{prefix}-vpc"))
            is not None
        )
        delete_compute(gcp, prefix)
        delete_database(gcp, prefix)
        delete_database_credentials(gcp, prefix)
        delete_iam(gcp, prefix)
        delete_transfer_bucket(
            gcp, f"{args.project}-{args.environment}-transfer-{project}"
        )
        if network_exists:
            delete_networking(gcp, prefix, args.project, args.environment)
        else:
            LOG.info("No matching VPC found; skipping networking cleanup.")
        LOG.info("Teardown completed.")
        banner("Teardown Complete")
        return 0
    except (ValueError, RuntimeError, HttpError, GoogleAuthError) as exc:
        LOG.error("Teardown failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
