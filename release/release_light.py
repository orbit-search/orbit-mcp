#!/usr/bin/env python3
"""Releases of services that are not GKE Deployments: Cloud Run services and a Composer DAG folder.

The GKE scripts (release_record, release_verify, release_rollback) move image
digests on Deployments. Two Orbit services deploy another way:

- kind "cloud_run": one build deploys one or more Cloud Run services. A release is
  the serving revision of each.
- kind "composer": one build syncs dags/ into a Cloud Composer environment. A
  release is the commit.

Tier check_only (records, the window check, an alert with a printed rollback
command for a human):

- cloud_run: the build deploys new revisions that take all traffic. Deploy
  check: the new revision is ready and serves all traffic, plus the config's
  public URLs. Rollback: send the traffic back to the previous revisions.
- composer: the build syncs dags/. Deploy check: no new DAG import error.
  Rollback: run the sync trigger at the previous commit.

Tier full (the GKE full tier: dev stage, dev verify, the same artifact to prod,
release_gate.py, automatic rollback, the hold):

- cloud_run: the dev build of a merge deploys a new revision of each prod
  service with no traffic and the tag `candidate`. Dev verify: the candidate
  revisions run the images this build pushed, and the smoke checks answer on the
  tag URLs (https://candidate---<host>). The prod build builds nothing: it moves
  100% of the traffic to the revisions that dev verify checked. Rollback: the
  traffic goes back to the revisions of the target release. The dev stage is a
  revision of the prod service: it shares the prod config, secrets and data.
- composer: the dev build copies the commit's dags/ to
  data/release-candidate/<commit>/dags/ in the environment's bucket (the DAG
  processor does not read data/). Dev verify: the build parses those files with
  the environment's Airflow version and fails on an import error that the DAGs
  in prod do not have (Airflow 3.1 has no `dags list-import-errors --subdir`).
  The prod build syncs that folder to dags/. Rollback: the target commit's
  folder goes to dags/.

Both tiers get the watch window of the GKE services: new warning and error log
lines (the config's Cloud Logging filter) compared with the 24 h before the
release, each traced to a suspected commit; composer also gets DAGs whose runs
fail after the release and not in the baseline. A failed check_only release
posts one Slack alert with a fix prompt and the rollback command; a failed full
release starts the rollback trigger (_EXPECT_LATEST) and pings the author.

    python3 release/release_light.py turn [--env dev] --commit <sha> --build-id <id>   # exit 3: superseded
    python3 release/release_light.py begin [--env dev] --commit <sha> --build-id <id> --release-id-file <file>
    python3 release/release_light.py finish [--env dev] --release-id-file <file> --status deployed
    python3 release/release_light.py check [--env dev] --release-id-file <file>
    python3 release/release_light.py schedule-window --release-id-file <file>
    python3 release/release_light.py rollback-command [--release-id <id>]
    # tier full only
    python3 release/release_light.py stage --commit <sha>                       # composer: the candidate folder
    python3 release/release_light.py fetch-dags --commit <sha> --out-dir <dir>  # composer: files for the parse
    python3 release/release_light.py parse-dags --candidate <dir> --baseline <dir> --out <file>  # Airflow image
    python3 release/release_light.py preflight --commit <sha> --build-id <id>   # prod: turn, branch, dev, artifacts
    python3 release/release_light.py still-current --commit <sha> --build-id <id>
    python3 release/release_light.py apply --artifacts-file <file>              # prod: the promote itself

`release_gate.py` and `release_rollback.py` take these services too.

Part of the release kit (orbit-infrastructure/release-kit). Stdlib only (Python 3.10).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import promote_release_guard as guard  # noqa: E402
import release_config as rc  # noqa: E402
import release_gate as rg  # noqa: E402
import release_record as rr  # noqa: E402
import release_rollback as rb  # noqa: E402
import release_signals as rs  # noqa: E402
import release_verify as rv  # noqa: E402
from promote_release_guard import GoogleApi, GuardError, _http_json  # noqa: E402

KINDS = ("cloud_run", "composer")
LIGHT_TIERS = ("check_only", "full")
# The file the prod build's preflight writes: what it promotes (release_gate.py check-prod --artifacts-file).
ARTIFACTS_FILE = "release_artifacts.json"
KIT_DIR = Path(__file__).resolve().parent
REPO_ROOT = KIT_DIR.parent


@dataclass(frozen=True)
class LightService:
    name: str
    title: str
    kind: str
    github_repo: str
    connection: str
    connected_repository: str
    release_branch: str
    project: str
    region: str
    store: str
    docs: str
    code: rc.Code
    checkpoints: tuple[int, ...]
    smoke: tuple[dict, ...]
    log_filter: str
    log_min_spread: int
    # The triggers whose builds deploy prod (single flight counts them all; tier full: triggers.prod).
    deploy_triggers: tuple[str, ...] = ()
    # cloud_run: the services one build deploys.
    services: tuple[str, ...] = ()
    # composer: the environment and the trigger that syncs dags/.
    environment: str = ""
    sync_trigger: str = ""
    dags_prefix: str = "dags/"
    parse_wait_seconds: int = 300
    path: Path | None = None
    tier: str = "check_only"
    # Tier full: the dev triggers (the merge trigger first) and the rollback trigger.
    dev_triggers: tuple[str, ...] = ()
    rollback_trigger_name: str = ""
    # Tier full, cloud_run: (service, base URL) of each service; the tag of the dev revision.
    service_urls: tuple[tuple[str, str], ...] = ()
    candidate_tag: str = "candidate"
    # Tier full, composer: the environment's bucket when it cannot be read; the candidate folder.
    bucket: str = ""
    candidate_prefix: str = "data/release-candidate/"

    metrics: tuple = ()
    signals = rc.Signals()

    @property
    def auto_rollback(self) -> bool:
        """Whether a failed release rolls back by itself (tier full)."""
        return self.tier == "full"

    @property
    def has_gate(self) -> bool:
        """Whether deploys pass release_gate.py (the freeze, the hold, dev verify): tier full."""
        return self.tier == "full"

    @property
    def has_dev(self) -> bool:
        """Whether the service has a dev stage (tier full)."""
        return self.tier == "full"

    @property
    def window_config(self) -> str:
        """The repository path of the release verify build config of this service."""
        return f"release/services/{self.name}.release-verify.yaml"

    @property
    def rollback_config(self) -> str:
        """The repository path of the rollback build config (tier full)."""
        return f"release/services/{self.name}.rollback.yaml"

    @property
    def rollback_trigger(self) -> str:
        """The manual trigger that runs the rollback build (tier full)."""
        return self.rollback_trigger_name or f"{self.name}-rollback"

    @property
    def rollback_build_tag(self) -> str:
        """Builds with this tag change prod (the rollback config); a promote waits for them."""
        return f"{self.name}-rollback"

    @property
    def verify_build_tag(self) -> str:
        """The tag of the release verify builds of this service."""
        return f"{self.name}-release-verify"

    def kit_command(self, script: str, sub: str = "") -> str:
        """The command line of a kit script for this service (see release_config.kit_command)."""
        return rc.kit_command(self.name, script, sub)

    def environment_named(self, name: str) -> rc.Environment:
        """The fields of an Environment that the shared record, gate and GitHub helpers read."""
        return rc.Environment(
            name=name,
            service=self.name,
            title=self.title,
            tier=self.tier,
            project=self.project,
            region=self.region,
            cluster="",
            namespace="",
            image=rc.Image(repository="", registry_location="", registry_repository="", package=""),
            deployments=(),
            smoke=self.smoke if name == "prod" else tuple(dev_smoke(self)),
            github_repo=self.github_repo,
            connection=self.connection,
            connected_repository=self.connected_repository,
            release_branch=self.release_branch,
            deploy_triggers=self.deploy_triggers if name == "prod" else self.dev_triggers,
        )

    @property
    def prod(self) -> rc.Environment:
        """The prod environment."""
        return self.environment_named("prod")

    @property
    def dev(self) -> rc.Environment | None:
        """The dev stage (tier full), else None."""
        return self.environment_named("dev") if self.has_dev else None

    @property
    def env(self) -> rc.Environment:
        """The prod environment (the name the check_only code reads)."""
        return self.prod

    @property
    def environments(self) -> dict[str, rc.Environment]:
        """The environments by name: prod, and dev for tier full."""
        return {"prod": self.prod, **({"dev": self.dev} if self.has_dev else {})}


def parse_light(data: dict, path: Path | None = None) -> LightService:
    """Parse a cloud_run or composer service config; raise ConfigError when it is invalid."""
    where = str(path or "service config")
    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        raise rc.ConfigError(f"{where}: kind {kind!r} is not one of {KINDS}")
    tier = str(data.get("tier") or "")
    if tier not in LIGHT_TIERS:
        raise rc.ConfigError(f"{where}: kind {kind} supports tiers {LIGHT_TIERS}, not {tier!r}")
    name = str(rc._require(data, "service", where))
    smoke = tuple(data.get("smoke") or ())
    for check in smoke:
        if check.get("type") != "http_public":
            raise rc.ConfigError(f"{where}: kind {kind} supports http_public smoke checks only")
        rc.validate_smoke(check, set(), f"{where} smoke")
    verify = dict(data.get("verify") or {})
    code_data = dict(rc._require(data, "code", where))  # type: ignore[arg-type]
    defaults = rc.Code(source_prefixes=(), extensions=())
    code = rc.Code(
        source_prefixes=rc._names(code_data.get("source_prefixes"), f"{where} code.source_prefixes"),
        extensions=rc._names(code_data.get("extensions"), f"{where} code.extensions"),
        test_markers=tuple(code_data.get("test_markers") or defaults.test_markers),
        non_runtime_prefixes=tuple(code_data.get("non_runtime_prefixes") or defaults.non_runtime_prefixes),
        deploy_prefixes=tuple(code_data.get("deploy_prefixes") or defaults.deploy_prefixes),
    )
    common = dict(
        name=name,
        title=str(data.get("title") or name),
        kind=kind,
        github_repo=str(rc._require(data, "github_repo", where)),
        connection=str(data.get("connection") or "orbit-search"),
        connected_repository=str(rc._require(data, "connected_repository", where)),
        release_branch=str(data.get("release_branch") or "main"),
        project=str(data.get("project") or "orbit-search"),
        region=str(data.get("region") or "us-central1"),
        store=str(data.get("store") or f"gs://orbit-search-release-records/{name}"),
        docs=str(data.get("docs") or "docs/RELEASE_PIPELINE.md"),
        code=code,
        checkpoints=tuple(int(value) for value in verify.get("checkpoints") or (15, 60)),
        smoke=smoke,
        log_filter=str(rc._require(verify, "log_filter", f"{where} verify")),
        log_min_spread=int(verify.get("log_min_spread") or 1),
        path=path,
        tier=tier,
    )
    if tier == "full":
        return parse_full(data, where, common)
    if kind == "cloud_run":
        services = rc._names(rc._require(data, "cloud_run_services", where), f"{where} cloud_run_services")
        triggers = rc._names(rc._require(data, "deploy_triggers", where), f"{where} deploy_triggers")
        return LightService(**common, services=services, deploy_triggers=triggers)
    composer = dict(rc._require(data, "composer", where))  # type: ignore[arg-type]
    sync_trigger = str(rc._require(composer, "sync_trigger", f"{where} composer"))
    return LightService(
        **common,
        deploy_triggers=tuple(data.get("deploy_triggers") or (sync_trigger,)),
        environment=str(rc._require(composer, "environment", f"{where} composer")),
        sync_trigger=sync_trigger,
        dags_prefix=str(composer.get("dags_prefix") or "dags/"),
        parse_wait_seconds=int(composer.get("parse_wait_seconds") or 300),
    )


def parse_full(data: dict, where: str, common: dict) -> LightService:
    """The tier full parts of a cloud_run or composer config: triggers, URLs, the candidate folder."""
    if "deploy_triggers" in data:
        raise rc.ConfigError(f"{where}: tier full names its triggers in triggers.dev, triggers.prod, triggers.rollback")
    triggers = dict(rc._require(data, "triggers", where))  # type: ignore[arg-type]
    full = dict(
        deploy_triggers=rc._names(triggers.get("prod"), f"{where} triggers.prod"),
        dev_triggers=rc._names(triggers.get("dev"), f"{where} triggers.dev"),
        rollback_trigger_name=str(triggers.get("rollback") or f"{common['name']}-rollback"),
    )
    if common["kind"] == "cloud_run":
        services = rc._names(rc._require(data, "cloud_run_services", where), f"{where} cloud_run_services")
        urls = dict(rc._require(data, "cloud_run_urls", where))  # type: ignore[arg-type]
        for name in services:
            url = str(urls.get(name) or "")
            if not re.match(r"^https://[a-z0-9.-]+\.run\.app$", url):
                raise rc.ConfigError(f"{where}: cloud_run_urls.{name} must be the service's https://<host>.run.app URL")
        if set(urls) - set(services):
            raise rc.ConfigError(f"{where}: cloud_run_urls names services that cloud_run_services does not")
        tagged = set()
        for check in common["smoke"]:
            owner = [name for name in services if str(check["url"]).startswith(urls[name] + "/")]
            if not owner:
                raise rc.ConfigError(
                    f"{where}: smoke url {check['url']} is not under a cloud_run_urls base URL, so dev verify cannot "
                    "call it on the candidate tag"
                )
            tagged.add(owner[0])
        if missing := sorted(set(services) - tagged):
            raise rc.ConfigError(f"{where}: tier full needs a smoke check of every service; none for {missing}")
        return LightService(
            **common,
            **full,
            services=services,
            service_urls=tuple((name, str(urls[name])) for name in services),
            candidate_tag=str(data.get("candidate_tag") or "candidate"),
        )
    composer = dict(rc._require(data, "composer", where))  # type: ignore[arg-type]
    bucket = str(composer.get("bucket") or "").rstrip("/")
    if bucket and not bucket.startswith("gs://"):
        raise rc.ConfigError(f"{where}: composer.bucket must be gs://<bucket>")
    prefix = str(composer.get("candidate_prefix") or "data/release-candidate/").strip("/") + "/"
    if not prefix.startswith("data/"):
        raise rc.ConfigError(f"{where}: composer.candidate_prefix must be under data/ (the DAG processor reads dags/)")
    return LightService(
        **common,
        **full,
        environment=str(rc._require(composer, "environment", f"{where} composer")),
        dags_prefix=str(composer.get("dags_prefix") or "dags/"),
        parse_wait_seconds=int(composer.get("parse_wait_seconds") or 300),
        bucket=bucket,
        candidate_prefix=prefix,
    )


def load_light(name: str | None = None) -> LightService:
    """Load the named service config, or the only one; the file name must be the service name."""
    name = name or os.environ.get("RELEASE_SERVICE", "").strip() or None
    files = rc.service_files()
    if name:
        path = rc.SERVICES_DIR / f"{name}.json"
    elif len(files) == 1:
        path = files[0]
    else:
        raise rc.ConfigError(f"{len(files)} service configs; name one with --service")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise rc.ConfigError(f"{path}: {error}") from None
    service = parse_light(data, path)
    if service.name != path.stem:
        raise rc.ConfigError(f"{path}: the file name must be the service name {service.name!r}")
    return service


# --------------------------------------------------------------------------- snapshots

Runner = Callable[[list[str]], str]


def gcloud_json(runner: Runner, *args: str) -> dict:
    """Run a gcloud command with JSON output and decode it."""
    return json.loads(runner(["gcloud", *args, "--format=json"]))


def snapshot_cloud_run(service: LightService, runner: Runner) -> dict[str, dict]:
    """Per Cloud Run service: the newest created and ready revisions, the traffic split and the image."""
    out = {}
    for name in service.services:
        data = gcloud_json(
            runner, "run", "services", "describe", name, f"--region={service.region}", f"--project={service.project}"
        )
        status = data.get("status") or {}
        traffic: dict[str, int] = {}
        tags: dict[str, str] = {}
        for item in status.get("traffic") or []:
            revision = item.get("revisionName") or (
                status.get("latestReadyRevisionName") if item.get("latestRevision") else ""
            )
            if revision:
                traffic[revision] = traffic.get(revision, 0) + int(item.get("percent") or 0)
                if item.get("tag"):
                    tags[str(item["tag"])] = revision
        containers = (((data.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [{}]
        out[f"cloud_run/{name}"] = {
            "kind": "cloud_run",
            "name": name,
            "latest_created": status.get("latestCreatedRevisionName"),
            "latest_ready": status.get("latestReadyRevisionName"),
            "traffic": traffic,
            "tags": tags,
            "image": containers[0].get("image"),
            "url": status.get("url"),
        }
    return out


def serving_split(workload: dict) -> dict[str, int]:
    """The revisions that serve traffic in a Cloud Run snapshot, with their percent (0% tags left out)."""
    split = {revision: int(percent) for revision, percent in (workload.get("traffic") or {}).items() if percent}
    if not split and workload.get("latest_ready"):
        split = {str(workload["latest_ready"]): 100}
    return split


def split_text(split: dict[str, int]) -> str:
    """A traffic split as gcloud's --to-revisions value: rev-a=90,rev-b=10."""
    return ",".join(f"{revision}={percent}" for revision, percent in sorted(split.items()))


def candidate_url(service: LightService, name: str, url: str | None = None) -> str:
    """The URL of the candidate tag of a Cloud Run service: https://<tag>---<host>[path]."""
    base = dict(service.service_urls)[name]
    host = base.split("://", 1)[1]
    return f"https://{service.candidate_tag}---{host}" + (url[len(base) :] if url else "")


def dev_smoke(service: LightService) -> list[dict]:
    """The smoke checks of dev verify: cloud_run calls the prod checks on the candidate tag URLs."""
    if service.kind != "cloud_run" or not service.service_urls:
        return []
    out = []
    for check in service.smoke:
        for name, base in service.service_urls:
            if str(check["url"]).startswith(base + "/"):
                out.append(
                    {
                        **check,
                        "name": f"{check.get('name') or name} (candidate)",
                        "url": candidate_url(service, name, check["url"]),
                    }
                )
                break
    return out


def describe_revision(service: LightService, runner: Runner, revision: str) -> dict:
    """A Cloud Run revision: its name, whether it is Ready, and the image digest it runs."""
    data = gcloud_json(
        runner, "run", "revisions", "describe", revision, f"--region={service.region}", f"--project={service.project}"
    )
    status = data.get("status") or {}
    ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions") or [])
    containers = (data.get("spec") or {}).get("containers") or [{}]
    image = status.get("imageDigest") or containers[0].get("image") or ""
    return {"name": revision, "ready": ready, "image": image, "digest": rr.digest_of(image)}


def revision_artifacts(service: LightService, runner: Runner, revisions: dict[str, str]) -> dict[str, str]:
    """{service: revision@digest} of Ready revisions; raise RecordError when one is missing, not Ready or has no digest."""
    out = {}
    for name in service.services:
        revision = revisions.get(name)
        if not revision:
            raise rr.RecordError(f"no {service.candidate_tag} revision of {name}")
        info = describe_revision(service, runner, revision)
        if not info["ready"]:
            raise rr.RecordError(f"revision {revision} of {name} is not Ready")
        if not info["digest"]:
            raise rr.RecordError(f"revision {revision} of {name} has no image digest ({info['image']!r})")
        out[name] = f"{revision}@{info['digest']}"
    return out


def candidate_revisions(service: LightService, snapshot: dict[str, dict]) -> dict[str, str]:
    """{service: the revision the candidate tag names} in a Cloud Run snapshot."""
    return {
        name: rev
        for name in service.services
        if (rev := ((snapshot.get(f"cloud_run/{name}") or {}).get("tags") or {}).get(service.candidate_tag))
    }


def remove_candidate_tags(service: LightService, revisions: dict[str, str]) -> list[str]:
    """Remove only these revisions' candidate tags, with an etag precondition against concurrent dev builds.

    The Cloud Run v2 traffic-only PATCH preserves percentages and other tags. A missing etag,
    conflict or incomplete operation fails cleanup rather than removing a newer build's tag.
    """
    failures = []
    api = GoogleApi(service.project, service.region)
    for name, revision in revisions.items():
        try:
            if (
                name not in service.services
                or not isinstance(revision, str)
                or not re.fullmatch(re.escape(name) + r"-[a-z0-9-]+", revision)
            ):
                raise rr.RecordError("cleanup needs an exact revision of a configured service")
            resource = f"projects/{service.project}/locations/{service.region}/services/{name}"
            url = f"https://run.googleapis.com/v2/{resource}"
            headers = {"Authorization": f"Bearer {api.access_token()}", "Content-Type": "application/json"}
            current = _http_json(urllib.request.Request(url, headers=headers))
            traffic = current.get("traffic") or []
            matched = [
                item
                for item in traffic
                if item.get("tag") == service.candidate_tag
                and str(item.get("revision") or "").rsplit("/", 1)[-1] == revision
            ]
            if not matched:
                continue
            if not current.get("etag"):
                raise rr.RecordError("Cloud Run returned no etag; refusing an unguarded tag removal")
            updated = [
                {key: value for key, value in item.items() if key != "tag"} if item in matched else dict(item)
                for item in traffic
            ]
            operation = _http_json(
                urllib.request.Request(
                    url + "?updateMask=traffic",
                    data=json.dumps({"name": resource, "etag": current["etag"], "traffic": updated}).encode(),
                    method="PATCH",
                    headers=headers,
                )
            )
            deadline = time.monotonic() + 120
            while not operation.get("done"):
                operation_name = str(operation.get("name") or "")
                if not operation_name.startswith(f"projects/{service.project}/locations/{service.region}/operations/"):
                    raise rr.RecordError("Cloud Run returned no valid cleanup operation")
                if time.monotonic() >= deadline:
                    raise rr.RecordError("Cloud Run tag cleanup did not finish within 120 seconds")
                time.sleep(2)
                operation = _http_json(
                    urllib.request.Request(
                        f"https://run.googleapis.com/v2/{operation_name}",
                        headers=headers,
                    )
                )
            if operation.get("error"):
                raise rr.RecordError(f"Cloud Run tag cleanup failed: {operation['error']}")
            after = _http_json(urllib.request.Request(url, headers=headers))
            if any(
                item.get("tag") == service.candidate_tag
                and str(item.get("revision") or "").rsplit("/", 1)[-1] == revision
                for item in [*(after.get("traffic") or []), *(after.get("trafficStatuses") or [])]
            ):
                raise rr.RecordError("the failed revision still has its candidate tag after cleanup")
            print(f"{name}: removed {service.candidate_tag} tag from {revision}", flush=True)
        except (rr.RecordError, GuardError, OSError) as error:
            failures.append(f"{name}: {str(error)[:300]}")
    return failures


def cmd_cleanup_candidates(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Failure cleanup of explicitly named Cloud Run candidate revisions; never infer ownership from a live tag."""
    _full(service)
    if service.kind != "cloud_run":
        raise rc.ConfigError("cleanup-candidates is only for Cloud Run")
    revisions = _json_file(args.revisions_file)
    if not revisions:
        raise rr.RecordError("candidate cleanup needs the revisions planned by this dev build")
    failures = remove_candidate_tags(service, revisions)
    for failure in failures:
        print(f"CLEANUP FAILED: {failure}", file=sys.stderr)
    return rv.EXIT_ERROR if failures else rv.EXIT_OK


def dags_uri(service: LightService, runner: Runner) -> str:
    """The environment's DAG folder (gs://<bucket>/dags); the config's bucket when it cannot be read."""
    try:
        uri = runner(
            [
                "gcloud",
                "composer",
                "environments",
                "describe",
                service.environment,
                f"--location={service.region}",
                f"--project={service.project}",
                "--format=value(config.dagGcsPrefix)",
            ]
        ).strip()
    except rr.RecordError as error:
        uri = ""
        print(f"WARNING: cannot read the DAG folder of {service.environment}: {str(error)[:200]}", file=sys.stderr)
    if uri.startswith("gs://"):
        return uri.rstrip("/")
    if not service.bucket:
        raise rr.RecordError(f"no DAG folder for {service.environment} and no composer.bucket in the config")
    print(f"Using the config's bucket {service.bucket}", flush=True)
    return f"{service.bucket}/dags"


def candidate_uri(service: LightService, dags: str, commit: str) -> str:
    """The candidate folder of a commit: gs://<bucket>/data/release-candidate/<commit>/dags."""
    bucket = dags.rsplit("/", 1)[0]
    return f"{bucket}/{service.candidate_prefix}{commit}/{service.dags_prefix.strip('/')}"


GCS_LIST_PAGES = 50


def _crc32c_table() -> tuple[int, ...]:
    """Build the Castagnoli lookup table used by GCS object integrity checks."""
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
        table.append(value)
    return tuple(table)


CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> str:
    """Return the base64 CRC32C representation used in GCS object metadata."""
    value = 0xFFFFFFFF
    for byte in data:
        value = CRC32C_TABLE[(value ^ byte) & 255] ^ (value >> 8)
    return base64.b64encode(((value ^ 0xFFFFFFFF) & 0xFFFFFFFF).to_bytes(4, "big")).decode("ascii")


def local_dag_manifest(folder: Path) -> str:
    """Compute the expected GCS manifest from the exact commit's downloaded files, before upload."""
    lines = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            lines.append(f"{path.relative_to(folder).as_posix()} {crc32c(content)} {len(content)}")
    if not lines:
        raise rr.RecordError("the target commit has no DAG files")
    return "sha256:" + hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def folder_manifest(uri: str, token: Callable[[], str]) -> tuple[str, int]:
    """(sha256 over every object's relative name, crc32c and size; object count) of a GCS folder. ("", 0) when empty.

    Two folders with the same manifest hold the same bytes, so the hash names the artifact.
    """
    bucket, _, prefix = uri.removeprefix("gs://").partition("/")
    prefix = prefix.rstrip("/") + "/"
    lines: list[str] = []
    page = ""
    for _ in range(GCS_LIST_PAGES):
        query = {"prefix": prefix, "fields": "items(name,crc32c,size),nextPageToken"}
        if page:
            query["pageToken"] = page
        url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?{urllib.parse.urlencode(query)}"
        payload = _http_json(urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"}))
        lines += [
            f"{item['name'][len(prefix) :]} {item.get('crc32c')} {item.get('size')}"
            for item in payload.get("items") or []
            if not item["name"].endswith("/")
        ]
        page = payload.get("nextPageToken") or ""
        if not page:
            break
    else:
        raise rr.RecordError(f"{uri} has more than {GCS_LIST_PAGES} pages of objects")
    if not lines:
        return "", 0
    return "sha256:" + hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest(), len(lines)


def composer_artifacts(service: LightService, runner: Runner, token: Callable[[], str], commit: str) -> dict[str, str]:
    """{"dags": "<candidate folder> <manifest hash>"}; raise RecordError when the folder is empty."""
    uri = candidate_uri(service, dags_uri(service, runner), commit)
    digest, count = folder_manifest(uri, token)
    if not count:
        raise rr.RecordError(f"the candidate folder {uri} is empty")
    return {"dags": f"{uri} {digest}"}


def live_artifacts(
    service: LightService, runner: Runner, token: Callable[[], str], commit: str, recorded: dict[str, str]
) -> dict[str, str]:
    """What prod would promote now, read live: the recorded revisions (cloud_run) or the candidate folder (composer)."""
    if service.kind == "cloud_run":
        return revision_artifacts(service, runner, {name: value.split("@", 1)[0] for name, value in recorded.items()})
    return composer_artifacts(service, runner, token, commit)


# --------------------------------------------------------------------------- the DAG parse (composer dev verify)

# Errors that need the environment, not the code: Airflow Variables and Connections exist only in Composer.
ENVIRONMENT_ONLY_ERRORS = re.compile(
    r"VARIABLE_NOT_FOUND|CONNECTION_NOT_FOUND|Variable \S+ (?:not found|does not exist)|conn_id .{0,40} isn't defined",
    re.IGNORECASE,
)


def parse_dag_folder(folder: Path) -> dict:
    """Parse every DAG file of a folder the way the DAG processor does (one file at a time); runs in the Airflow image.

    Returns {"airflow": version, "files": n, "dags": n, "errors": {relative path: last line of the error}}.
    """
    sys.path.insert(0, str(folder))
    os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    import airflow  # noqa: PLC0415 - only the Airflow image has it
    from airflow.models.dagbag import DagBag  # noqa: PLC0415

    try:
        from airflow.utils.file import list_py_file_paths  # noqa: PLC0415

        files = list_py_file_paths(str(folder), safe_mode=True)
    except ImportError:
        files = sorted(str(path) for path in folder.rglob("*.py"))
    errors: dict[str, str] = {}
    dags = 0
    for path in sorted(files):
        bag = DagBag(dag_folder=path, include_examples=False)
        dags += len(bag.dags)
        for name, trace in bag.import_errors.items():
            lines = [line.strip() for line in str(trace).splitlines() if line.strip()]
            relative = os.path.relpath(name, folder)
            errors[relative] = (lines[-1] if lines else "").replace(str(folder), "<dags>")[:500]
    return {"airflow": getattr(airflow, "__version__", "?"), "files": len(files), "dags": dags, "errors": errors}


def compare_parse(result: dict) -> tuple[dict[str, str], dict[str, str]]:
    """(new errors, new errors that need the environment): candidate errors that prod's DAGs do not have."""
    baseline = (result.get("baseline") or {}).get("errors") or {}
    new = {
        name: error
        for name, error in ((result.get("candidate") or {}).get("errors") or {}).items()
        if baseline.get(name) != error
    }
    environment = {name: error for name, error in new.items() if ENVIRONMENT_ONLY_ERRORS.search(error)}
    return {name: error for name, error in new.items() if name not in environment}, environment


def check_parse(service: LightService, path: str) -> rv.Check:
    """The candidate DAGs import as well as prod's: no new import error in the parse result."""
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        return rv.Check("dag_parse", "failed", [f"the DAG parse did not run or wrote no result: {error}"])
    if not isinstance(result, dict):
        return rv.Check("dag_parse", "failed", ["the DAG parse result must be an object"])
    if result.get("error"):
        return rv.Check("dag_parse", "failed", [f"the DAG parse failed: {str(result['error'])[:300]}"])
    for role in ("candidate", "baseline"):
        parsed = result.get(role)
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("airflow"), str)
            or not parsed["airflow"].strip()
            or any(type(parsed.get(count)) is not int or parsed[count] < 0 for count in ("files", "dags"))
            or not isinstance(parsed.get("errors"), dict)
            or any(not isinstance(error, str) for error in parsed["errors"].values())
        ):
            return rv.Check("dag_parse", "failed", [f"the DAG parse result has incomplete or invalid {role} metadata"])
    new, environment = compare_parse(result)
    candidate = result.get("candidate") or {}
    details = [
        f"Airflow {candidate.get('airflow')}: {candidate.get('files')} files, {candidate.get('dags')} DAGs, "
        f"{len(candidate.get('errors') or {})} import errors ({len((result.get('baseline') or {}).get('errors') or {})} "
        "in prod's DAGs)"
    ]
    details += [f"{service.dags_prefix}{name}: {error[:200]}" for name, error in sorted(new.items())]
    details += [
        f"not judged (needs a Composer Variable or Connection): {service.dags_prefix}{name}: {error[:160]}"
        for name, error in sorted(environment.items())
    ]
    return rv.Check("dag_parse", "failed" if new else "passed", details)


# Airflow list pages: a day of failures of the five-minute DAGs fits in a few pages.
AIRFLOW_PAGE_SIZE = 100
AIRFLOW_MAX_PAGES = 50


class Airflow:
    """Read-only Airflow REST API of a Composer environment (the build identity's token)."""

    def __init__(self, service: LightService, runner: Runner, token: Callable[[], str]) -> None:
        """Find the Airflow URI of the Composer environment; raise RecordError when there is none."""
        uri = runner(
            [
                "gcloud",
                "composer",
                "environments",
                "describe",
                service.environment,
                f"--location={service.region}",
                f"--project={service.project}",
                "--format=value(config.airflowUri)",
            ]
        ).strip()
        if not uri.startswith("https://"):
            raise rr.RecordError(f"no Airflow URI for {service.environment}: {uri!r}")
        self.uri = uri.rstrip("/")
        self.token = token

    def get(self, path: str) -> dict:
        """GET an Airflow API path with the bearer token and decode the JSON."""
        request = urllib.request.Request(f"{self.uri}{path}", headers={"Authorization": f"Bearer {self.token()}"})
        return _http_json(request)

    def all_items(self, path: str, key: str) -> list[dict]:
        """Every item of a paged Airflow list; an answer that does not reach total_entries raises.

        A partial list would make an old failure look new, so it never passes as complete.
        """
        items: list[dict] = []
        separator = "&" if "?" in path else "?"
        for _ in range(AIRFLOW_MAX_PAGES):
            payload = self.get(f"{path}{separator}limit={AIRFLOW_PAGE_SIZE}&offset={len(items)}")
            page = payload.get(key) or []
            items.extend(page)
            total = int(payload.get("total_entries") or 0)
            if len(items) >= total or not page:
                if len(items) < total:
                    raise rr.RecordError(f"Airflow {path} returned {len(items)} of {total} entries")
                return items
        raise rr.RecordError(f"Airflow {path} has more than {AIRFLOW_MAX_PAGES * AIRFLOW_PAGE_SIZE} entries")

    def import_errors(self) -> dict[str, str]:
        """{filename: first line of the error} of every DAG file that does not import now."""
        return {
            str(item.get("filename")): str(item.get("stack_trace") or "").strip().splitlines()[-1:][0]
            if item.get("stack_trace")
            else ""
            for item in self.all_items("/api/v2/importErrors", "import_errors")
        }

    def failed_runs(self, start: datetime, end: datetime) -> dict[str, int]:
        """{dag_id: failed runs} that ended in [start, end)."""
        query = (
            f"/api/v2/dags/~/dagRuns?state=failed"
            f"&end_date_gte={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&end_date_lte={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        out: dict[str, int] = {}
        for run in self.all_items(query, "dag_runs"):
            out[str(run.get("dag_id"))] = out.get(str(run.get("dag_id")), 0) + 1
        return out


def snapshot(service: LightService, runner: Runner, token: Callable[[], str]) -> dict[str, dict]:
    """What runs now: the Cloud Run revisions, or the Composer DAG import errors."""
    if service.kind == "cloud_run":
        return snapshot_cloud_run(service, runner)
    try:
        errors: dict[str, str] | None = Airflow(service, runner, token).import_errors()
        problem = ""
    except (GuardError, rr.RecordError) as error:
        # The build identity needs roles/composer.user to read the Airflow API; the log checks still run.
        errors, problem = None, f"the Airflow API cannot be read: {str(error)[:300]}"
    return {"composer/import_errors": {"kind": "composer", "errors": errors, "error": problem}}


# --------------------------------------------------------------------------- checks


def repo_path(service: LightService, filename: str) -> str:
    """A Composer DAG file path (/home/airflow/gcs/dags/x.py) as its repository path (dags/x.py)."""
    marker = "/dags/"
    return service.dags_prefix + filename.split(marker, 1)[1] if marker in filename else filename


def check_cloud_run(before: dict[str, dict], after: dict[str, dict]) -> rv.Check:
    """Each service's newest revision is ready and serves all traffic."""
    problems = []
    for key, now in sorted(after.items()):
        if now.get("latest_created") != now.get("latest_ready"):
            problems.append(
                f"{key}: revision {now.get('latest_created')} is not ready (ready: {now.get('latest_ready')})"
            )
            continue
        serving = (now.get("traffic") or {}).get(now.get("latest_ready") or "", 0)
        if serving != 100:
            problems.append(f"{key}: {now.get('latest_ready')} serves {serving}% of traffic, not 100%")
        previous = before.get(key) or {}
        if previous.get("latest_ready") == now.get("latest_ready") and previous.get("image") != now.get("image"):
            # A new image must make a new revision; the same image may keep the old one.
            problems.append(f"{key}: no new revision for {now.get('image')} (still {now.get('latest_ready')})")
    return rv.Check("revisions", "failed" if problems else "passed", [f"{len(after)} services checked", *problems])


def check_promoted_revisions(service: LightService, record: dict, after: dict[str, dict]) -> rv.Check:
    """Tier full: every service sends 100% of its traffic to the revision this release promoted."""
    artifacts = record.get("artifacts") or {}
    problems = []
    for name in service.services:
        revision = str(artifacts.get(name) or "").split("@", 1)[0]
        serving = serving_split(after.get(f"cloud_run/{name}") or {})
        if not revision:
            problems.append(f"cloud_run/{name}: the release record names no revision")
        elif serving != {revision: 100}:
            problems.append(f"cloud_run/{name}: traffic is {split_text(serving) or 'unknown'}, not {revision}=100")
    return rv.Check(
        "revisions", "failed" if problems else "passed", [f"{len(service.services)} services checked", *problems]
    )


def check_import_errors(service: LightService, record: dict, after: dict[str, dict]) -> rv.Check:
    """No DAG file fails to import that imported before the sync (or fails with another error now)."""
    before_snapshot = (record.get("previous_snapshot") or {}).get("composer/import_errors") or {}
    after_snapshot = after.get("composer/import_errors") or {}
    if before_snapshot.get("errors") is None or after_snapshot.get("errors") is None:
        reason = after_snapshot.get("error") or before_snapshot.get("error") or "no import error snapshot"
        return rv.Check("dag_import_errors", "skipped", [reason])
    before, now = before_snapshot["errors"], after_snapshot["errors"]
    new = {name: error for name, error in now.items() if before.get(name) != error}
    details = [f"{len(now)} import errors now, {len(before)} before the sync"]
    details += [f"{repo_path(service, name)}: {error[:200]}" for name, error in sorted(new.items())]
    return rv.Check("dag_import_errors", "failed" if new else "passed", details)


def deploy_checks(
    service: LightService, record: dict, runner: Runner, token: Callable[[], str], get: rv.HttpGet = rv.http_get
) -> tuple[list[rv.Check], dict[str, dict]]:
    """Run the deploy checks and the public smoke checks; return them with the snapshot after the deploy."""
    after = snapshot(service, runner, token)
    if service.kind == "cloud_run" and service.has_gate:
        checks = [check_promoted_revisions(service, record, after)]
    elif service.kind == "cloud_run":
        checks = [check_cloud_run(record.get("previous_snapshot") or {}, after)]
    else:
        checks = [check_import_errors(service, record, after)]
    checks += [rv.check_http_public(check, get) for check in service.smoke]
    return checks, after


def dag_failure_problems(
    service: LightService, airflow: Airflow, scope: rs.Scope, now: datetime, tree: rs.SourceTree
) -> list[rs.Problem]:
    """DAGs with failed runs in the window and none in the baseline; the literal is the dag_id the code names."""
    baseline: dict[str, int] = {}
    for start, end in scope.baseline:
        for dag, count in airflow.failed_runs(start, end).items():
            baseline[dag] = baseline.get(dag, 0) + count
    problems = []
    for dag, count in sorted(airflow.failed_runs(scope.window[0], now).items()):
        if baseline.get(dag):
            continue
        files = sorted({path for quote in ('"', "'") for path, _ in tree.find_text(f"{quote}{dag}{quote}")})
        problems.append(
            rs.Problem(
                kind="dag_failure",
                key=f"dag:{dag}",
                text=f"DAG {dag}: {count} failed run(s) after the release, none in the 24 h before"
                + (f" (defined in {', '.join(files[:2])})" if files else ""),
                count=count,
                identifiers=[dag],
                fields={"dag": dag},
            )
        )
    return problems


# --------------------------------------------------------------------------- record lifecycle


def open_store(service: LightService, dry_run_dir: Path | None, *, uri: str = "", env: str = "prod") -> rr.Store:
    """Open the record store of one environment of the service (dry_run_dir: writes go only there)."""
    token = GoogleApi(service.project, service.region).access_token
    return rr.open_store(uri or service.store, service.environment_named(env), token, dry_run_dir)


def rollback_command(service: LightService, record: dict, target: dict | None = None) -> str:
    """The command a human runs to undo this release (it prints; nothing runs it automatically).

    Tier full: release_rollback.py run (the rollback trigger, as for GKE); target: an older one than "previous".
    """
    if service.has_gate:
        return f"{rv.rollback_cli(service, 'run', target)} --reason '<why>'"
    previous = record.get("previous") or {}
    if service.kind == "composer":
        commit = previous.get("commit") or "<previous commit>"
        return (
            f"gcloud builds triggers run {service.sync_trigger} --region={service.region} "
            f"--project={service.project} --sha={commit}"
        )
    snapshot_before = record.get("previous_snapshot") or {}
    commands = []
    for name in service.services:
        before = snapshot_before.get(f"cloud_run/{name}") or {}
        # The revisions that served before the release: after a manual traffic move the newest ready
        # revision may be one that serves nothing.
        traffic = {revision: percent for revision, percent in (before.get("traffic") or {}).items() if percent}
        split = ",".join(f"{revision}={percent}" for revision, percent in sorted(traffic.items()))
        split = split or f"{before.get('latest_ready') or '<previous revision>'}=100"
        commands.append(
            f"gcloud run services update-traffic {name} --region={service.region} --project={service.project} "
            f"--to-revisions={split}"
        )
    return " && ".join(commands)


def _json_file(path: str | None) -> dict:
    """The JSON object in a file; {} when no path is given."""
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise rr.RecordError(f"{path} does not hold a JSON object")
    return data


def cmd_begin(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Take a snapshot and write a deploying record before the deploy; write its id to --release-id-file.

    Tier full: on dev the record holds the images this build pushed (--images-file); on prod it holds
    the artifacts it promotes (--artifacts-file), and "previous" keeps the snapshot of what served
    before (the rollback target) with the previous release's artifacts.
    """
    started_at = rr.utc_now()
    token = GoogleApi(service.project, service.region).access_token
    dev = args.env == "dev"
    if dev:
        before = snapshot_cloud_run(service, runner) if service.kind == "cloud_run" else {}
    else:
        before = snapshot(service, runner, token)
    artifacts = _json_file(args.artifacts_file)
    if service.has_gate and not dev and not artifacts:
        raise rr.RecordError("tier full: the prod record needs --artifacts-file (what the build promotes)")
    pointer, _ = store.read("latest.json")
    previous: dict = {"release_id": None, "commit": None}
    if pointer:
        record, _ = store.read(rr.record_name(pointer["release_id"]))
        if record:
            previous = {
                "release_id": record["release_id"],
                "commit": record["commit"],
                "deploy_finished": (record.get("times") or {}).get("deploy_finished"),
            }
            if service.has_gate and record.get("artifacts"):
                previous["artifacts"] = record["artifacts"]
    if not previous["commit"] and args.previous_commit:
        previous["commit"] = args.previous_commit
    if service.has_gate and not dev:
        # What served before this release: release_rollback.py's "previous" target.
        previous["workloads"] = before
    try:
        github = rr.github_token(service.env)
    except GuardError as error:
        print(f"WARNING: no GitHub token, the record has no commit list: {error}", file=sys.stderr)
        github = None
    record = rr.new_record(
        service.environment_named(args.env),
        kind="promote",
        commit=args.commit,
        images={str(key): str(value) for key, value in _json_file(args.images_file).items()},
        build=rr.build_info(service.env, args.build_id),
        previous=previous,
        started_at=started_at,
        token=github,
    )
    record["previous_snapshot"] = before
    if artifacts:
        record["artifacts"] = artifacts
    rr.create_record(store, record, "deploy_started")
    Path(args.release_id_file).write_text(record["release_id"] + "\n", encoding="utf-8")
    print(f"Release {record['release_id']}: {store.location(rr.record_name(record['release_id']))}", flush=True)
    print(f"Previous: {previous.get('release_id') or 'unrecorded'} commit {previous.get('commit')}", flush=True)
    return 0


def dev_artifacts(service: LightService, record: dict, after: dict, runner: Runner) -> dict[str, str]:
    """What the dev stage holds now: the candidate revisions (cloud_run) or the candidate folder (composer)."""
    if service.kind == "cloud_run":
        return revision_artifacts(service, runner, candidate_revisions(service, after))
    return composer_artifacts(
        service, runner, GoogleApi(service.project, service.region).access_token, record["commit"]
    )


def cmd_finish(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Take a snapshot after the deploy, set the record status and point latest.json at it.

    Dev (tier full): also record the artifacts the dev stage holds; prod (tier full): free the prod lock.
    """
    release_id = (
        Path(args.release_id_file).read_text(encoding="utf-8").strip() if args.release_id_file else args.release_id
    )
    artifacts: dict[str, str] = {}
    problem = ""
    if args.env == "dev":
        after = snapshot_cloud_run(service, runner) if service.kind == "cloud_run" else {}
        try:
            artifacts = dev_artifacts(service, rr.read_record(store, release_id), after, runner)
        except (rr.RecordError, GuardError) as error:
            problem = str(error)[:300]
            print(f"WARNING: no artifacts for dev verify: {problem}", file=sys.stderr, flush=True)
    else:
        after = snapshot(service, runner, GoogleApi(service.project, service.region).access_token)

    def finish(record: dict) -> None:
        """Write the snapshot, the status and the deploy finish time on the record."""
        record["workloads"] = after
        record["status"] = args.status
        record.setdefault("times", {})["deploy_finished"] = rr.utc_now()
        if args.env == "dev":
            record["artifacts"] = artifacts
            if problem:
                record["artifacts_error"] = problem

    saved = rr.update_record(store, release_id, finish, args.status)
    rr.point_latest(store, saved)
    print(f"Release {release_id}: {args.status}", flush=True)
    if service.has_gate and args.env == "prod":
        # The promote is over: a rollback may start now. A build that ends frees the lock too.
        try:
            rr.release_lock(store, (saved.get("build") or {}).get("id"))
        except (rr.RecordError, GuardError, OSError) as error:
            print(f"WARNING: could not free the prod lock; it ends with this build: {error}", file=sys.stderr)
    return 0


def _fail(
    service: LightService,
    store: rr.Store,
    record: dict,
    phase: str,
    regressions: list[dict],
    new_errors: dict | None = None,
) -> dict:
    """Set the failed verdict and the suggestion (the rollback command for a human).

    A window that judges several releases together and blames an older one suggests
    what ran before that older release, from its own record.
    """
    base = record
    target = None
    if new_errors:
        target = rs.rollback_target(record, store, new_errors.get("problems") or [], new_errors.get("scope") or {})
        if target:
            base = rr.read_record(store, target["rollback_to"].split(":", 1)[1])
    previous = base.get("previous") or {}
    suggestion = {
        "created_at": rr.utc_now(),
        "phase": phase,
        "summary": f"{len(regressions)} problem(s): " + "; ".join(item["text"] for item in regressions[:3]),
        "regressions": regressions,
        "suspected_prs": rv.suspected_prs(service, record, regressions, rr_token(service)),
        "target": {
            "release_id": previous.get("release_id"),
            "commit": previous.get("commit"),
            **({"rollback_to": target["rollback_to"]} if target else {}),
        },
        "how": [rollback_command(service, base, target)],
        "note": (
            "Tier full: the watch-window build starts the rollback itself; a human can also start it."
            if service.auto_rollback
            else "Tier check_only: a human rolls back."
        ),
    }

    def change(current: dict) -> None:
        """Set the failed verdict and the suggestion, unless the record has failed already."""
        if (current.get("verdict") or {}).get("status") != "failed":
            current["verdict"] = {
                "status": "failed",
                "at": rr.utc_now(),
                "phase": phase,
                "reasons": [item["text"] for item in regressions],
            }
            current["rollback_suggestion"] = suggestion

    return rr.update_record(store, record["release_id"], change, f"verify_{phase}", {"result": "failed"})


def rr_token(service: LightService) -> str | None:
    """The GitHub token of the service, or None when there is none."""
    try:
        return rr.github_token(service.env)
    except GuardError:
        return None


def on_failed(service: LightService, store: rr.Store, record: dict) -> None:
    """A failed prod release: tier full starts the rollback trigger (release_verify.start_rollback), else the alert."""
    if service.auto_rollback:
        rv.start_rollback(service, store, record)
    else:
        alert(service, store, record)


def suggested(service: LightService, record: dict) -> str:
    """The saved suggestion's rollback command, else the one that undoes this release."""
    how = (record.get("rollback_suggestion") or {}).get("how") or []
    return how[0] if how else rollback_command(service, record)


def alert(service: LightService, store: rr.Store, record: dict) -> None:
    """The failed-release Slack alert with the author ping, a fix prompt and the rollback command.

    Sent once per release: the deploy check and the watch both reach a failed verdict,
    and only a post that Slack did not take is sent again.
    """
    if os.environ.get("RELEASE_SLACK_DISABLED", "").strip() == "1":
        print("Test run: no alert", flush=True)
        return
    if (record.get("alert") or {}).get("posted"):
        print("The failed-release alert was sent already", flush=True)
        return
    location = store.location(rr.record_name(record["release_id"]))
    text, blocks = rv.rollback_alert(service, record, None, location, command=suggested(service, record))
    if not rr.post_slack(text, blocks):
        return
    try:
        rr.update_record(
            store,
            record["release_id"],
            lambda current: current.__setitem__("alert", {"posted": True, "at": rr.utc_now()}),
            "alert_posted",
        )
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not mark the alert sent: {error}", file=sys.stderr, flush=True)


def cmd_check(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Deploy checks right after the deploy (composer: after the DAG processor parsed the new files)."""
    if args.env == "dev":
        return cmd_dev_verify(args, service, store, runner)
    record = rr.read_record(store, args.release_id)
    if service.kind == "composer" and args.wait:
        print(f"Waiting {service.parse_wait_seconds} s for the DAG processor", flush=True)
        time.sleep(service.parse_wait_seconds)
    checks, after = deploy_checks(service, record, runner, GoogleApi(service.project, service.region).access_token)
    if record.get("status") == "deploy_failed":
        checks.insert(0, rv.Check("deploy", "failed", ["the deploy step failed (status deploy_failed)"]))
    for check in checks:
        print(f"   check {check.name}: {check.status}")
        for line in check.details[:12]:
            print(f"      {line}")
    failed = [check for check in checks if check.status == "failed"]
    if failed and args.failed_marker:
        # First: a record or Slack write that fails below must not turn a known failure green.
        Path(args.failed_marker).write_text("true\n", encoding="utf-8")
    result = {"phase": "deploy", "at": rr.utc_now(), "checks": [asdict(check) for check in checks]}
    saved = rr.update_record(
        store,
        record["release_id"],
        lambda current: current.setdefault("checks", {}).__setitem__("deploy", result),
        "verify_deploy",
        {"result": "failed" if failed else "passed"},
    )
    where = store.location(rr.record_name(record["release_id"]))
    if failed:
        regressions = [
            {
                "kind": "check",
                "name": check.name,
                "text": f"{check.name}: {'; '.join(check.details[1:] or check.details)}",
            }
            for check in failed
        ]
        saved = _fail(service, store, saved, "deploy", regressions)
        if args.force_fail:
            rr.post_slack(
                f"{rv.TEST_RUN_LINE}. A test of {service.title} release-verify failed on purpose; no action needed."
            )
        elif service.auto_rollback:
            # The watch-window build sees the failed verdict at once and starts the rollback trigger.
            rr.post_slack(
                f":rotating_light: {service.title} release-verify {record['commit'][:9]} FAILED (deploy): "
                f"{saved['rollback_suggestion']['summary'][:300]} An automatic rollback follows. Record {where}"
            )
        else:
            alert(service, store, saved)
        return rv.EXIT_FAILED
    minutes = " and ".join(f"+{value}" for value in service.checkpoints)
    rr.post_slack(
        f":mag: {service.title} release-verify {record['commit'][:9]} deploy checks passed; window checks follow "
        f"at {minutes} min. Record {where}"
    )
    return rv.EXIT_OK


def dev_checks(service: LightService, record: dict, args: argparse.Namespace, runner: Runner) -> list[rv.Check]:
    """Dev verify of tier full: the dev stage holds what this build made, and it works."""
    checks = []
    if record.get("status") == "deploy_failed":
        checks.append(rv.Check("deploy", "failed", ["the dev deploy step failed (status deploy_failed)"]))
    artifacts = record.get("artifacts") or {}
    if not artifacts:
        checks.append(rv.Check("artifacts", "failed", [record.get("artifacts_error") or "the dev record has none"]))
    elif service.kind == "cloud_run":
        problems = []
        now = candidate_revisions(service, snapshot_cloud_run(service, runner))
        for name in service.services:
            revision, _, digest = str(artifacts.get(name) or "").partition("@")
            pushed = rr.digest_of((record.get("images") or {}).get(name))
            if not revision:
                problems.append(f"{name}: no candidate revision recorded")
                continue
            if not pushed or digest != pushed:
                problems.append(f"{name}: {revision} runs {digest or 'no digest'}, this build pushed {pushed}")
            if now.get(name) != revision:
                problems.append(
                    f"{name}: the {service.candidate_tag} tag moved to {now.get(name)} (a newer dev build?)"
                )
        checks.append(
            rv.Check(
                "candidate_revisions",
                "failed" if problems else "passed",
                [f"{len(service.services)} services: {', '.join(sorted(artifacts.values()))}", *problems],
            )
        )
        checks += [rv.check_http_public(check) for check in dev_smoke(service)]
    else:
        token = GoogleApi(service.project, service.region).access_token
        try:
            live = composer_artifacts(service, runner, token, record["commit"])
            same = live == artifacts
            detail = live["dags"] if same else f"the candidate folder changed: {live['dags']} (recorded {artifacts})"
        except (rr.RecordError, GuardError) as error:
            same, detail = False, f"the candidate folder cannot be read: {str(error)[:300]}"
        checks.append(rv.Check("candidate_folder", "passed" if same else "failed", [detail]))
        checks.append(check_parse(service, args.parse_result))
    if args.force_fail:
        checks.append(rv.Check("forced_failure", "failed", [f"forced failure (test run): {args.force_fail}"]))
    return checks


def cmd_dev_verify(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Dev verify (tier full): the dev checks, the verdict on the dev record and its commit index, the alert."""
    if not service.has_dev:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}: it has no dev stage")
    record = rr.read_record(store, args.release_id)
    checks = dev_checks(service, record, args, runner)
    for check in checks:
        print(f"   check {check.name}: {check.status}")
        for line in check.details[:12]:
            print(f"      {line}")
    failed = [check for check in checks if check.status == "failed"]
    if failed and args.failed_marker:
        # First: a record or Slack write that fails below must not turn a known failure green.
        Path(args.failed_marker).write_text("true\n", encoding="utf-8")
    result = {
        "phase": "deploy",
        "at": rr.utc_now(),
        "checks": [asdict(check) for check in checks],
        "regressions": [
            {"kind": "check", "name": c.name, "text": f"{c.name}: {'; '.join(c.details[1:] or c.details)}"}
            for c in failed
        ],
        "result": "failed" if failed else "passed",
    }
    saved = rr.update_record(
        store,
        record["release_id"],
        lambda current: current.setdefault("checks", {}).__setitem__("deploy", result),
        "verify_deploy",
        {"result": result["result"]},
    )
    superseded = rv.newer_dev_build(service.dev, args.build_id) if failed else None
    saved = rv.finish_dev_verify(
        service,
        store,
        saved,
        result,
        build_url=args.build_url,
        test_run=bool(args.force_fail),
        token=rr_token(service),
        superseded_by=superseded,
    )
    if args.failed_marker and saved["verdict"]["status"] != "failed":
        Path(args.failed_marker).unlink(missing_ok=True)  # superseded: nothing to promote, not red
    return rv.EXIT_FAILED if saved["verdict"]["status"] == "failed" else rv.EXIT_OK


def evaluate_window(service: LightService, store: rr.Store, record: dict, runner: Runner, now: datetime) -> dict:
    """New error log lines (and composer: newly failing DAGs), traced to suspects."""
    token = GoogleApi(service.project, service.region).access_token
    scope = rs.release_scope(store, record, now)
    logs = rs.CloudLogging(service.project, token)
    tree = rs.SourceTree(REPO_ROOT, service.code)
    parts: dict[str, dict] = {}
    problems: list[rs.Problem] = []
    try:
        found, notes = rs.log_problems(
            logs,
            scope,
            now,
            env=service.env,
            containers=(),
            min_spread=service.log_min_spread,
            base_filter=service.log_filter,
        )
        parts["logs"] = {"status": "checked", "found": len(found), "detail": "; ".join(notes)}
        problems += found
    except (GuardError, rs.NoAccess, TimeoutError, OSError, ValueError, KeyError) as error:
        parts["logs"] = {"status": "inconclusive", "detail": str(error)[:300]}
    if service.kind == "composer":
        try:
            found = dag_failure_problems(service, Airflow(service, runner, token), scope, now, tree)
            parts["dag_failures"] = {"status": "checked", "found": len(found), "detail": ""}
            problems += found
        except (GuardError, rr.RecordError, OSError, ValueError, KeyError) as error:
            parts["dag_failures"] = {"status": "inconclusive", "detail": str(error)[:300]}
    rs.trace_suspects(problems, scope, token=rr_token(service), repo=service.github_repo, tree=tree, code=service.code)
    return {"scope": scope.summary(), "parts": parts, "problems": [problem.summary() for problem in problems]}


# How long a checkpoint waits for another deploy build of the service.
DEPLOY_WAIT_SECONDS = 2700
# How many times a checkpoint runs again when a deploy build starts during its checks.
CHECKPOINT_ATTEMPTS = 3


def running_deploys(service: LightService, own_build: str | None) -> list[str]:
    """Other deploy builds of the service that run now."""
    names = " OR ".join(f'substitutions.TRIGGER_NAME="{name}"' for name in service.deploy_triggers)
    running = f'({names}) AND (status="PENDING" OR status="QUEUED" OR status="WORKING")'
    api = GoogleApi(service.project, service.region)
    builds = api.list_builds(running)
    if service.has_gate:
        # A full-tier prod build changes nothing before its release-record-begin step (it waits for the dev
        # build, its turn and the gate); a rollback build changes prod.
        builds = [build for build in builds if build.changing_cluster]
        builds += api.ongoing_rollbacks(service.rollback_build_tag)
    return [build.describe() for build in builds if build.id != own_build]


def replaced_by(service: LightService, store: rr.Store, record: dict) -> str | None:
    """The newer release, or the deploy build that runs now, that replaces this release; None if neither."""
    pointer, _ = store.read("latest.json")
    if pointer and pointer.get("release_id") != record["release_id"]:
        return f"release {pointer['release_id']}"
    running = running_deploys(service, (record.get("build") or {}).get("id"))
    return f"deploy build {running[0]}" if running else None


def wait_for_deploys(
    service: LightService,
    store: rr.Store,
    record: dict,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Wait while another deploy build runs; return the newer release that replaced this one, else None."""
    deadline = clock() + DEPLOY_WAIT_SECONDS
    while True:
        pointer, _ = store.read("latest.json")
        if pointer and pointer.get("release_id") != record["release_id"]:
            return f"release {pointer['release_id']}"
        running = running_deploys(service, (record.get("build") or {}).get("id"))
        if not running:
            return None
        if clock() >= deadline:
            raise rr.RecordError(f"deploy build {running[0]} still runs after {DEPLOY_WAIT_SECONDS // 60} min")
        print(f"Waiting: deploy build {running[0]} runs", flush=True)
        sleep(60)


def finish_inconclusive(service: LightService, store: rr.Store, record: dict, key: str, why: str) -> None:
    """The last checkpoint could not run: the verdict is inconclusive (never left pending) and a human hears of it."""

    def done(current: dict) -> None:
        """Set the inconclusive verdict, unless the record has failed already."""
        if (current.get("verdict") or {}).get("status") != "failed":
            current["verdict"] = {
                "status": "inconclusive",
                "unchecked": True,
                "at": rr.utc_now(),
                "phase": key,
                "reasons": [why[:300]],
            }

    try:
        rr.update_record(store, record["release_id"], done, "verify_final", {"status": "inconclusive"})
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not save the inconclusive verdict: {error}", file=sys.stderr, flush=True)
    rr.post_slack(
        f":grey_question: {service.title} release-verify {record['commit'][:9]} inconclusive: the last checkpoint "
        f"could not run ({why[:200]}). Check the release by hand. "
        f"Record {store.location(rr.record_name(record['release_id']))}"
    )


def cmd_watch(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Run the window checks at each checkpoint; fail and alert on a clear new error, else pass at the end."""
    record = rr.read_record(store, args.release_id)
    if (record.get("verdict") or {}).get("status") == "failed":
        on_failed(service, store, record)
        return rv.EXIT_OK
    finished = rr.parse_utc(record["times"].get("deploy_finished") or record["times"]["deploy_started"])
    checkpoints = sorted(int(value) for value in (args.checkpoints or " ".join(map(str, service.checkpoints))).split())
    for index, minutes in enumerate(checkpoints):
        last = index == len(checkpoints) - 1
        wait = (finished + timedelta(minutes=minutes) - datetime.now(rr.UTC)).total_seconds()
        if wait > 0:
            print(f"Waiting {wait / 60:.1f} min for the +{minutes} min checkpoint", flush=True)
            time.sleep(wait)
        try:
            new_errors = None
            for _ in range(CHECKPOINT_ATTEMPTS):
                if replaced := wait_for_deploys(service, store, record):
                    print(f"Superseded by {replaced}; the watch stops", flush=True)
                    return rv.EXIT_OK
                new_errors = evaluate_window(service, store, record, runner, datetime.now(rr.UTC))
                # A deploy build that started during the checks may have changed what they read. Only a
                # newer release ends the watch; a build that only runs makes the checkpoint run again.
                if replaced := replaced_by(service, store, record):
                    if replaced.startswith("release "):
                        print(f"Superseded by {replaced} during the checks; the watch stops", flush=True)
                        return rv.EXIT_OK
                    print(f"{replaced} started during the checks; the checkpoint runs again", flush=True)
                    new_errors = None
                    continue
                break
            if new_errors is None:
                raise rr.RecordError(f"deploy builds kept starting during {CHECKPOINT_ATTEMPTS} attempts")
        except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
            print(f"Checkpoint +{minutes} min could not run: {error}", file=sys.stderr, flush=True)
            if last:
                finish_inconclusive(service, store, record, f"window_{minutes}m", str(error))
            continue
        key = f"window_{minutes}m"
        regressions = [rv.new_error_regression(problem) for problem in new_errors["problems"] if problem["clear"]]
        warnings = [
            rv.new_error_regression(problem)
            for problem in new_errors["problems"]
            if not problem["clear"] and problem.get("suspects") and problem["suspects"][0].get("linked")
        ]
        result = {"phase": "window", "at": rr.utc_now(), "new_errors": new_errors, "warnings": warnings}
        fresh: list[dict] = []

        def store_result(current: dict, key: str = key, result: dict = result) -> None:
            """Save the checkpoint result and collect the warnings not posted yet into fresh."""
            current.setdefault("checks", {})[key] = result
            known = current.setdefault("warnings", {})
            fresh.clear()
            for warning in result["warnings"]:
                entry = known.setdefault(
                    warning["name"], {"at": result["at"], "text": warning["text"], "posted": False}
                )
                if not entry.get("posted"):
                    fresh.append(warning)

        saved = rr.update_record(
            store, record["release_id"], store_result, f"verify_{key}", {"problems": len(new_errors["problems"])}
        )
        for problem in new_errors["problems"]:
            print(f"   {'FAILS' if problem['clear'] else 'warns'}  {problem['text'][:300]}")
        if regressions:
            saved = _fail(service, store, saved, key, regressions, new_errors)
            on_failed(service, store, saved)
            return rv.EXIT_OK
        if fresh:
            rv.post_warnings(service, store, saved, fresh, command=rollback_command(service, saved))
        if last:
            checked = new_errors["parts"].get("logs", {}).get("status") == "checked"
            status = "passed" if checked else "inconclusive"

            def done(current: dict, status: str = status) -> None:
                """Set the final verdict, unless the record has failed already."""
                if (current.get("verdict") or {}).get("status") != "failed":
                    current["verdict"] = {"status": status, "at": rr.utc_now(), "phase": key}

            rr.update_record(store, record["release_id"], done, "verify_final", {"status": status})
            icon = ":white_check_mark:" if checked else ":grey_question:"
            rr.post_slack(
                f"{icon} {service.title} release-verify {record['commit'][:9]} {status}. "
                f"Record {store.location(rr.record_name(record['release_id']))}"
            )
    return rv.EXIT_OK


def window_sources(service: LightService) -> list[Path]:
    """The files the watch window build needs: the kit files and the runtime source files."""
    sources = [path for path in KIT_DIR.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    for prefix in service.code.source_prefixes:
        base = REPO_ROOT / prefix.rstrip("/")
        paths = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for path in paths:
            if (
                path.is_file()
                and "node_modules" not in path.parts
                and service.code.is_source(path.relative_to(REPO_ROOT).as_posix())
            ):
                sources.append(path)
    return sources


def cmd_schedule_window(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Copy the sources and submit the watch window build; note its id on the record."""
    source = Path(args.source_dir)
    for path in window_sources(service):
        target = source / path.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    checkpoints = " ".join(str(value) for value in service.checkpoints)
    substitutions = (
        f"_SERVICE={service.name},_RELEASE_ID={args.release_id},_CHECKPOINTS={checkpoints},"
        f"_SLACK_DISABLED={'1' if args.no_slack else ''}"
    )
    if service.auto_rollback:
        # Real releases only: a test run (--no-slack) never rolls back.
        substitutions += f",_AUTO_ROLLBACK={'' if args.no_slack else '1'}"
    build_id = rr.run_command(
        [
            "gcloud",
            "builds",
            "submit",
            str(source),
            f"--config={source / service.window_config}",
            f"--region={service.region}",
            f"--project={service.project}",
            f"--substitutions={substitutions}",
            "--async",
            "--format=value(id)",
        ],
        timeout=600,
    ).strip()
    rr.update_record(
        store,
        args.release_id,
        lambda current: current.setdefault("checks", {}).__setitem__("window_build", {"id": build_id}),
        "window_scheduled",
        {"build_id": build_id},
    )
    print(f"Watch window build {build_id} submitted", flush=True)
    return rv.EXIT_OK


def cmd_turn(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Single flight of the deploy build: an older one that runs is waited for, a newer one supersedes this one.

    Prod (tier check_only): the commit must be on the release branch. Dev (tier full): the dev
    triggers' builds; a manual dev run of a branch is allowed (it never promotes). Exit 3:
    superseded (deploy nothing, not a failure).
    """
    api = GoogleApi(service.project, service.region)
    dev = args.env == "dev"
    if dev and not service.has_dev:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}: it has no dev stage")
    turn = guard.Guard(
        api=api,
        build_id=args.build_id,
        commit=args.commit,
        peer_triggers=service.dev_triggers if dev else service.deploy_triggers,
        promoting_trigger=service.dev_triggers[0] if dev else "",
    )
    try:
        own = turn.own_build()
        if not dev:
            token = api.repository_read_token(service.connection, service.connected_repository)
            turn.check_on_branch(service.github_repo, service.release_branch, token)
        turn.wait_for_turn(own, args.older_timeout_seconds)
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    return rv.EXIT_OK


# --------------------------------------------------------------------------- tier full: candidates and the promote


def _full(service: LightService) -> None:
    """Raise ConfigError unless the service is tier full."""
    if not service.has_gate:
        raise rc.ConfigError(f"{service.name} is tier {service.tier}; this command is for tier full")


def gsutil_rsync(runner: Runner, source: str, target: str, *, delete: bool = True) -> None:
    """gsutil -m rsync -r [-d] source target (the deploy steps of the composer kind)."""
    runner(["gsutil", "-m", "rsync", "-r", *(["-d"] if delete else []), source, target])


def cmd_stage(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Composer dev stage: copy the commit's DAG files to its candidate folder (the DAG processor does not read it)."""
    _full(service)
    if service.kind != "composer":
        raise rc.ConfigError("stage is for kind composer; cloud_run deploys a no-traffic revision in its dev build")
    target = candidate_uri(service, dags_uri(service, runner), args.commit)
    source = Path(args.source or (REPO_ROOT / service.dags_prefix))
    if not source.is_dir():
        raise rr.RecordError(f"no DAG folder {source}")
    gsutil_rsync(runner, str(source), target)
    print(f"Staged {source} at {target}", flush=True)
    return rv.EXIT_OK


def cmd_fetch_dags(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Copy the candidate DAGs and prod's DAGs into --out-dir (candidate/, baseline/) for parse-dags."""
    _full(service)
    dags = dags_uri(service, runner)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Invalidate a previous attempt before downloading either folder. A partial
    # rsync must not authorize a parse of the leftover/empty local directories.
    (out / "fetch-complete").unlink(missing_ok=True)
    (out / "result.json").unlink(missing_ok=True)
    for name, source in (("candidate", candidate_uri(service, dags, args.commit)), ("baseline", dags)):
        (out / name).mkdir(parents=True, exist_ok=True)
        gsutil_rsync(runner, source, str(out / name))
    # The Airflow image runs as the user airflow; it writes the result here.
    for root, folders, files in os.walk(out):
        for name in [*folders, *files]:
            os.chmod(os.path.join(root, name), 0o777 if name in folders else 0o666)
    os.chmod(out, 0o777)
    (out / "fetch-complete").write_text(args.commit + "\n", encoding="utf-8")
    print(f"Candidate and prod DAGs in {out}", flush=True)
    return rv.EXIT_OK


def cmd_parse_dags(args: argparse.Namespace) -> int:
    """Parse the candidate and the prod DAGs, each in its own process (module names would collide), into --out.

    Runs in the Airflow image of the environment's version; it needs no service config and no gcloud.
    """
    if args.one:
        Path(args.out).write_text(json.dumps(parse_dag_folder(Path(args.one)), sort_keys=True), encoding="utf-8")
        return rv.EXIT_OK
    result: dict = {}
    for role, folder in (("candidate", args.candidate), ("baseline", args.baseline)):
        with tempfile.TemporaryDirectory() as home:
            out = Path(home) / "parse.json"
            env = {
                **os.environ,
                "AIRFLOW_HOME": home,
                "AIRFLOW__CORE__DAGS_FOLDER": str(Path(folder).resolve()),
                "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
            }
            command = [sys.executable, str(Path(__file__).resolve()), "parse-dags", "--one", folder, "--out", str(out)]
            try:
                done = subprocess.run(
                    command, env=env, capture_output=True, text=True, timeout=args.timeout, check=False
                )
                result[role] = (
                    json.loads(out.read_text(encoding="utf-8"))
                    if done.returncode == 0
                    else {"error": (done.stderr or done.stdout).strip()[-1500:]}
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                result[role] = {"error": str(error)}
        if result[role].get("error"):
            result["error"] = f"{role}: {result[role]['error'][-500:]}"
        print(f"{role}: {json.dumps({k: v for k, v in result[role].items() if k != 'errors'})}", flush=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return rv.EXIT_OK


def _prod_guard(args: argparse.Namespace, service: LightService, api: GoogleApi) -> guard.Guard:
    """Single flight of the prod builds of tier full: a newer one supersedes, an older one or a rollback is waited for."""
    return guard.Guard(
        api=api,
        build_id=args.build_id,
        commit=args.commit,
        poll_seconds=args.poll_seconds,
        peer_triggers=service.deploy_triggers,
        rollback_tag=service.rollback_build_tag,
    )


def _verified_artifacts(service: LightService, commit: str, dry_run_dir: Path | None) -> dict[str, str]:
    """The artifacts that dev verify recorded for the commit; raise GuardError when there are none."""
    index, _ = open_store(service, dry_run_dir, env="dev").read(rg.dev_commit_index(commit))
    recorded = (index or {}).get("artifacts") or {}
    if not recorded:
        raise GuardError(f"dev verify recorded no artifacts for {commit[:9]}; the dev build of this commit must pass")
    return recorded


def cmd_preflight(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Prod build of tier full, first step: single flight, on the release branch, the dev build of the commit
    succeeded; then read what dev verify recorded and write the artifacts as they are now (ARTIFACTS_FILE)."""
    _full(service)
    api = GoogleApi(service.project, service.region)
    turn = _prod_guard(args, service, api)
    try:
        own = turn.own_build()
        print(f"Prod promote {own.describe()}", flush=True)
        turn.wait_for_turn(own, args.older_timeout_seconds)
        token = api.repository_read_token(service.connection, service.connected_repository)
        turn.check_on_branch(service.github_repo, service.release_branch, token)
        turn.wait_for_dev(
            own, service.dev_triggers, args.dev_timeout_seconds, service.release_branch, allow_overlap=True
        )
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    recorded = _verified_artifacts(service, args.commit, args.dry_run_dir)
    live = live_artifacts(service, runner, api.access_token, args.commit, recorded)
    (Path(args.out_dir) / ARTIFACTS_FILE).write_text(
        json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for name, value in sorted(live.items()):
        print(f"promote {name}: {value}", flush=True)
    return rv.EXIT_OK


def cmd_still_current(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Right before the promote: no newer prod build, no older one or rollback started, the artifacts did not change."""
    _full(service)
    api = GoogleApi(service.project, service.region)
    turn = _prod_guard(args, service, api)
    try:
        own = turn.own_build()
        if older := turn.check_superseded(own):
            raise GuardError(
                f"an older prod build or a rollback started running after preflight: {older[0].describe()}"
            )
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    expected = _json_file(str(Path(args.out_dir) / ARTIFACTS_FILE))
    live = live_artifacts(service, runner, api.access_token, args.commit, expected)
    if live != expected:
        raise GuardError(f"the artifacts changed after preflight: {live} (preflight: {expected})")
    print(f"Still current: {own.id} is the newest {own.trigger_name} build and promotes {sorted(live.values())}")
    return rv.EXIT_OK


def sync_verified_dags(service: LightService, artifact: str, destination: str, runner: Runner) -> None:
    """Check the expected candidate checksum before copying and the live checksum afterward."""
    source, _, expected = artifact.partition(" ")
    if not source.startswith("gs://") or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise rr.RecordError(f"no verified DAG artifact: {artifact!r}")
    token = GoogleApi(service.project, service.region).access_token
    if folder_manifest(source, token)[0] != expected:
        raise rr.RecordError("the candidate DAG manifest changed after verification; nothing copied")
    gsutil_rsync(runner, source, destination)
    if folder_manifest(destination, token)[0] != expected:
        raise rr.RecordError("the live DAG manifest differs from the verified artifact after the copy")


def promote_artifacts(service: LightService, artifacts: dict[str, str], runner: Runner) -> list[str]:
    """Send prod to the artifacts: 100% traffic to each revision, or the candidate folder to dags/. Returns failures."""
    failures = []
    if service.kind == "cloud_run":
        for name in service.services:
            revision = str(artifacts.get(name) or "").split("@", 1)[0]
            try:
                if not revision:
                    raise rr.RecordError("no revision")
                runner(
                    [
                        "gcloud",
                        "run",
                        "services",
                        "update-traffic",
                        name,
                        f"--region={service.region}",
                        f"--project={service.project}",
                        f"--to-revisions={revision}=100",
                    ]
                )
                print(f"{name}: 100% of the traffic to {revision}", flush=True)
            except rr.RecordError as error:
                failures.append(f"cloud_run/{name}: {str(error)[:300]}")
        return failures
    try:
        target = dags_uri(service, runner)
        sync_verified_dags(service, str(artifacts.get("dags") or ""), target, runner)
        print(f"Synced the verified DAG artifact to {target}", flush=True)
    except (rr.RecordError, GuardError) as error:
        failures.append(f"composer/dags: {str(error)[:300]}")
    return failures


def cmd_apply(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """The promote of the prod build (tier full): exit 1 when a change failed (the build records deploy_failed)."""
    _full(service)
    failures = promote_artifacts(service, _json_file(args.artifacts_file), runner)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr, flush=True)
    return rv.EXIT_ERROR if failures else rv.EXIT_OK


# --------------------------------------------------------------------------- tier full: rollback


def rollback_plan(service: LightService, latest: dict, target: dict, runner: Runner) -> rb.Plan:
    """One step per Cloud Run service (traffic split) or one for the DAG folder (commit); problems block the apply."""
    steps: list[rb.Step] = []
    problems: list[str] = []
    notes: list[str] = []
    if service.kind == "cloud_run":
        live = snapshot_cloud_run(service, runner)
        for name in service.services:
            key = f"cloud_run/{name}"
            want = serving_split((target.get("workloads") or {}).get(key) or {})
            have = split_text(serving_split(live.get(key) or {}))
            if not want:
                steps.append(rb.Step(key, name, "traffic", have, None, "skip"))
                problems.append(f"{key}: the target release recorded no serving revisions")
                continue
            steps.append(
                rb.Step(key, name, "traffic", have, split_text(want), "none" if have == split_text(want) else "set")
            )
            for revision in want:
                try:
                    if not describe_revision(service, runner, revision)["ready"]:
                        problems.append(f"{key}: revision {revision} is not Ready")
                except (rr.RecordError, ValueError) as error:
                    problems.append(f"{key}: revision {revision} cannot be read: {str(error)[:200]}")
    else:
        commit = target.get("commit")
        steps.append(
            rb.Step("composer/dags", "dags", "commit", latest.get("commit"), commit, "set" if commit else "skip")
        )
        if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
            problems.append("composer/dags: the target commit is unknown")
        notes.append("the target commit's candidate folder goes to dags/; a missing folder is staged from GitHub first")
    notes.append(
        f"{service.release_branch} still has {str(latest.get('commit'))[:9]}. Automatic prod deploys hold after the "
        f"rollback until a revert or a commit with the trailer 'Fixes-release: {latest.get('release_id')}' merges."
    )
    return rb.Plan(
        from_release=latest.get("release_id"),
        from_commit=latest.get("commit"),
        target_release=target.get("release_id"),
        target_commit=target.get("commit"),
        target_images=target.get("images") or {},
        steps=steps,
        not_rolled_back=[],
        notes=notes,
        problems=problems,
        target_artifacts=dict(target.get("artifacts") or {}),
    )


def resolve_target(store: rr.Store, target: str) -> tuple[dict, dict]:
    """release_rollback.resolve_target; a "previous" of a check_only record takes its before-snapshot.

    Tier full records keep what served before in previous.workloads; records of tier check_only
    (before the move to tier full) keep it in previous_snapshot.
    """
    latest, resolved = rb.resolve_target(store, target)
    if not resolved.get("workloads") and (target == "previous" or target.startswith(rb.PREVIOUS_OF)):
        holder = latest if target == "previous" else rr.read_record(store, target[len(rb.PREVIOUS_OF) :])
        resolved["workloads"] = holder.get("previous_snapshot") or {}
    return latest, resolved


def print_rollback_plan(service: LightService, plan: rb.Plan) -> None:
    """Print the plan: the target, each step, the notes and the problems."""
    print(f"Rollback of {service.name} prod ({service.kind}) from {plan.from_release} ({str(plan.from_commit)[:9]})")
    print(f"  to {plan.target_release or 'the unrecorded release before it'} ({str(plan.target_commit)[:9]})")
    print(f"Changes ({len(plan.changes())} of {len(plan.steps)}):")
    for step in plan.steps:
        print(step.line())
    for note in plan.notes:
        print(f"Note: {note}")
    for problem in plan.problems:
        print(f"BLOCKED: {problem}")


def cmd_rollback_plan(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner | None) -> int:
    """release_rollback.py plan of a light service: print what a rollback would change; 1 when it is blocked."""
    latest, target = resolve_target(store, args.to)
    plan = rollback_plan(service, latest, target, runner or rr.run_command)
    print_rollback_plan(service, plan)
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(plan), indent=2, sort_keys=True), encoding="utf-8")
    if plan.problems:
        print("\nDry run: nothing changed. This rollback is blocked; pick another target with --to.")
        return 1
    print(
        f"\nDry run: nothing changed. Start the rollback with: {service.kit_command('release_rollback.py', 'run')} --reason '<why>'"
    )
    return 0


def stage_from_github(service: LightService, commit: str, target: str, runner: Runner) -> str:
    """Stage exact commit files and return their locally computed manifest, independent of mutable GCS contents."""
    token = rr.github_token(service.prod)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{service.github_repo}/tarball/{commit}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    prefix = service.dags_prefix.strip("/") + "/"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.tar.gz"
        with urllib.request.urlopen(request, timeout=120) as response:
            archive.write_bytes(response.read())
        root = Path(tmp) / "src"
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                # <owner>-<repo>-<sha>/dags/...: only regular files under the DAG folder, no links, no "..".
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not member.isfile() or not parts[1].startswith(prefix):
                    continue
                relative = Path(parts[1])
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is not None:
                    path.write_bytes(source.read())
        folder = root / prefix.rstrip("/")
        if not folder.is_dir():
            raise rr.RecordError(f"commit {commit[:9]} has no {prefix} folder")
        expected = local_dag_manifest(folder)
        gsutil_rsync(runner, str(folder), target)
    print(f"Staged {commit[:9]} from GitHub at {target}", flush=True)
    return expected


def apply_rollback(service: LightService, plan: rb.Plan, runner: Runner) -> list[str]:
    """Set every planned traffic split, or sync the target commit's DAGs to dags/; return the failures."""
    failures = []
    for step in plan.changes():
        try:
            if service.kind == "cloud_run":
                runner(
                    [
                        "gcloud",
                        "run",
                        "services",
                        "update-traffic",
                        step.name,
                        f"--region={service.region}",
                        f"--project={service.project}",
                        f"--to-revisions={step.target}",
                    ]
                )
                print(f"{step.workload}: traffic {step.current} -> {step.target}", flush=True)
                continue
            dags = dags_uri(service, runner)
            artifact = plan.target_artifacts.get("dags") or ""
            source = artifact.partition(" ")[0] if artifact else candidate_uri(service, dags, str(step.target))
            expected = artifact.partition(" ")[2]
            if not source.startswith("gs://") or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
                source, expected = candidate_uri(service, dags, str(step.target)), ""
            token = GoogleApi(service.project, service.region).access_token
            if not expected or folder_manifest(source, token)[0] != expected:
                staged = stage_from_github(service, str(step.target), source, runner)
                if expected and staged != expected:
                    raise rr.RecordError("the exact target commit does not match the recorded DAG artifact")
                artifact = f"{source} {staged}"
                plan.target_artifacts["dags"] = artifact
            sync_verified_dags(service, artifact, dags, runner)
            print(f"composer/dags: synced {source} to {dags}", flush=True)
        except (rr.RecordError, GuardError, OSError, tarfile.TarError) as error:
            failures.append(f"{step.workload}: {str(error)[:300]}")
    return failures


def check_rollback(service: LightService, plan: rb.Plan, after: dict) -> rv.Check:
    """Cloud Run: each service serves its target split; Composer: the sync ran (the next window checks the DAGs)."""
    problems = []
    if service.kind == "cloud_run":
        for step in plan.steps:
            have = split_text(serving_split(after.get(step.workload) or {}))
            if step.target and have != step.target:
                problems.append(f"{step.workload}: traffic is {have}, not {step.target}")
    else:
        expected = (plan.target_artifacts.get("dags") or "").partition(" ")[2]
        have = (after.get("composer/dags") or {}).get("manifest")
        if not expected or have != expected:
            problems.append(f"composer/dags: live manifest {have!r} differs from target {expected!r}")
    return rv.Check("rollback", "failed" if problems else "passed", [f"{len(plan.steps)} changes checked", *problems])


def cmd_rollback_apply(
    args: argparse.Namespace,
    service: LightService,
    store: rr.Store,
    runner: Runner | None,
    *,
    alive: Callable[[str], bool] | None = None,
) -> int:
    """release_rollback.py apply of a light service (inside the rollback build), under the prod lock."""
    run = runner or long_runner
    return rb.locked_apply(args, service, store, lambda: _rollback_apply(args, service, store, run), alive=alive)


def _rollback_apply(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Guard, record, change, check and record: the same record and hold as a GKE rollback."""
    rb.assert_alone(service, args.build_id)
    latest, target = resolve_target(store, args.target)
    rb.check_expect_latest(args, latest)
    plan = rollback_plan(service, latest, target, runner)
    print_rollback_plan(service, plan)
    if plan.problems:
        raise rr.RecordError(f"rollback blocked before any change: {plan.problems[0]}")
    if args.dry_run:
        print("\nDry run (_DRY_RUN=true): nothing changed.")
        return 0
    previous = (latest.get("workloads") or {}, latest.get("images") or {})
    record = rb.create_rollback_record(service, store, args, latest, target, previous, plan)
    failures = apply_rollback(service, plan, runner)
    if service.kind == "cloud_run":
        failed_candidates = {
            name: str(artifact).partition("@")[0]
            for name, artifact in (latest.get("artifacts") or {}).items()
            if name in service.services
        }
        failures += remove_candidate_tags(service, failed_candidates)
        after = snapshot_cloud_run(service, runner)
    else:
        try:
            manifest, count = folder_manifest(
                dags_uri(service, runner), GoogleApi(service.project, service.region).access_token
            )
        except (rr.RecordError, GuardError) as error:
            manifest, count = "", 0
            failures.append(f"composer/dags: could not verify restored files: {error}")
        after = {
            "composer/dags": {
                "kind": "composer",
                "commit": target.get("commit"),
                "manifest": manifest,
                "objects": count,
            }
        }
    if plan.target_artifacts:
        target["artifacts"] = dict(plan.target_artifacts)
    check = check_rollback(service, plan, after)
    if check.status == "failed":
        failures += check.details[1:]
    return rb.finish_rollback(service, store, args, record, latest, target, after, [check], failures)


def cmd_rollback_command(args: argparse.Namespace, service: LightService, store: rr.Store, runner: Runner) -> int:
    """Print the command that undoes a release."""
    record = rr.read_record(store, args.release_id)
    print(rollback_command(service, record))
    return rv.EXIT_OK


def long_runner(command: list[str]) -> str:
    """rr.run_command with the time a deploy step needs (a gsutil rsync, a traffic move)."""
    return rr.run_command(command, timeout=1800)


# Commands that change or copy something: their gcloud and gsutil calls may take minutes.
LONG_COMMANDS = ("stage", "fetch-dags", "apply")


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, load the service and run the command; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    parser.add_argument("--dry-run-dir", type=Path, help="read the real store, write records only under this directory")
    commands = parser.add_subparsers(dest="command", required=True)

    def release_id_args(sub: argparse.ArgumentParser) -> None:
        """Add the --release-id and --release-id-file options to a subcommand."""
        sub.add_argument("--release-id", default="latest")
        sub.add_argument("--release-id-file")

    def env_arg(sub: argparse.ArgumentParser) -> None:
        """Add --env: prod, or dev (the dev stage of tier full)."""
        sub.add_argument("--env", default="prod", choices=["prod", "dev"])

    def prod_build_args(sub: argparse.ArgumentParser) -> None:
        """Add the options of the prod build's guard steps."""
        sub.add_argument("--build-id", required=True)
        sub.add_argument("--commit", required=True)
        sub.add_argument("--out-dir", default="/workspace")
        sub.add_argument("--poll-seconds", type=float, default=15.0)

    begin = commands.add_parser("begin", help="snapshot and write a deploying record (before the deploy)")
    env_arg(begin)
    begin.add_argument("--commit", required=True)
    begin.add_argument("--build-id")
    begin.add_argument("--release-id-file", required=True)
    begin.add_argument("--previous-commit", default="", help="the commit that ran before, when no record exists yet")
    begin.add_argument("--images-file", help="tier full, dev: JSON {service: image@digest} this build pushed")
    begin.add_argument("--artifacts-file", help="tier full, prod: the artifacts preflight wrote")
    finish = commands.add_parser("finish", help="snapshot after the deploy and set the status")
    env_arg(finish)
    release_id_args(finish)
    finish.add_argument("--status", required=True, choices=["deployed", "deploy_failed"])
    check = commands.add_parser("check", help="deploy checks (prod) or dev verify (--env dev)")
    env_arg(check)
    release_id_args(check)
    check.add_argument("--wait", action="store_true", help="composer: wait for the DAG processor first")
    check.add_argument("--failed-marker")
    check.add_argument("--force-fail", default="", help="test runs: the alert says TEST RUN")
    check.add_argument("--parse-result", default="", help="composer dev verify: the parse-dags result")
    check.add_argument("--build-id", default="", help="dev verify: the dev build, to tell a superseded verify")
    check.add_argument("--build-url", default="", help="dev verify: the dev build's link for Slack")
    watch = commands.add_parser("watch", help="the window checks at each checkpoint")
    release_id_args(watch)
    watch.add_argument("--checkpoints", default="")
    schedule = commands.add_parser("schedule-window", help="submit the watch-window build")
    release_id_args(schedule)
    schedule.add_argument("--source-dir", default="/workspace/release-verify-src")
    schedule.add_argument("--no-slack", action="store_true")
    turn = commands.add_parser("turn", help="single flight of the deploy build (exit 3: superseded)")
    env_arg(turn)
    turn.add_argument("--build-id", required=True)
    turn.add_argument("--commit", required=True)
    turn.add_argument("--older-timeout-seconds", type=float, default=1800)
    command = commands.add_parser("rollback-command", help="print the command that undoes a release")
    release_id_args(command)
    stage = commands.add_parser("stage", help="tier full, composer: copy dags/ to the commit's candidate folder")
    stage.add_argument("--commit", required=True)
    stage.add_argument("--source", default="", help="default: the repository's DAG folder")
    fetch = commands.add_parser("fetch-dags", help="tier full, composer: candidate and prod DAGs for parse-dags")
    fetch.add_argument("--commit", required=True)
    fetch.add_argument("--out-dir", required=True)
    cleanup = commands.add_parser("cleanup-candidates", help="remove this build's failed Cloud Run candidate tags")
    cleanup.add_argument("--revisions-file", required=True)
    parse = commands.add_parser("parse-dags", help="tier full, composer: parse DAG folders (in the Airflow image)")
    parse.add_argument("--candidate", default="")
    parse.add_argument("--baseline", default="")
    parse.add_argument("--one", default="", help=argparse.SUPPRESS)
    parse.add_argument("--out", required=True)
    parse.add_argument("--timeout", type=float, default=1200)
    preflight = commands.add_parser("preflight", help="tier full, prod: single flight, branch, dev, artifacts")
    prod_build_args(preflight)
    preflight.add_argument("--older-timeout-seconds", type=float, default=3600)
    preflight.add_argument("--dev-timeout-seconds", type=float, default=1200)
    still = commands.add_parser("still-current", help="tier full, prod: recheck single flight and the artifacts")
    prod_build_args(still)
    apply = commands.add_parser("apply", help="tier full, prod: promote the artifacts")
    apply.add_argument("--artifacts-file", required=True)

    args = parser.parse_args(argv)
    if args.command == "parse-dags":
        if not args.one and not (args.candidate and args.baseline):
            parser.error("parse-dags needs --candidate and --baseline")
        return cmd_parse_dags(args)
    if getattr(args, "release_id_file", None) and args.command != "begin":
        args.release_id = Path(args.release_id_file).read_text(encoding="utf-8").strip()
    handlers = {
        "begin": cmd_begin,
        "finish": cmd_finish,
        "check": cmd_check,
        "watch": cmd_watch,
        "schedule-window": cmd_schedule_window,
        "turn": cmd_turn,
        "rollback-command": cmd_rollback_command,
        "stage": cmd_stage,
        "fetch-dags": cmd_fetch_dags,
        "cleanup-candidates": cmd_cleanup_candidates,
        "preflight": cmd_preflight,
        "still-current": cmd_still_current,
        "apply": cmd_apply,
    }
    try:
        service = load_light(args.service)
        env = getattr(args, "env", "prod")
        if env == "dev" and not service.has_dev:
            raise rc.ConfigError(f"{service.name} is tier {service.tier}: it has no dev stage")
        store = open_store(service, args.dry_run_dir, env=env)
        runner = long_runner if args.command in LONG_COMMANDS else rr.run_command
        return handlers[args.command](args, service, store, runner)
    except guard.Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return guard.EXIT_SUPERSEDED
    except (
        rr.RecordError,
        GuardError,
        rc.ConfigError,
        subprocess.SubprocessError,
        KeyError,
        OSError,
        ValueError,
    ) as error:
        print(f"release light: {error}", file=sys.stderr, flush=True)
        return rv.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
