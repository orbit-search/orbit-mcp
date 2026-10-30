#!/usr/bin/env python3
"""Release records of one service, kept in GCS.

Every change of the image that an environment runs writes one JSON record: a
deploy or promote by the deploy build, or a rollback (release_rollback.py,
started by a human or by release-verify). A record holds the commit, the image
digest, the image of every Deployment before and after the deploy, the previous
release, the commits since it, the times, and the release-verify results
(release_verify.py).

Layout in the bucket (object versioning keeps every overwrite):

    <store>/<env>/releases/<release id>.json   one record per release
    <store>/<env>/latest.json                  the newest record that touched <env>

The store is the service config's `store` (gs://orbit-search-release-records/<service>).
The "before" workload snapshot is the rollback target: it is what ran before
the deploy, read from the cluster, not what a tag points at now.

Part of the release kit (orbit-infrastructure/release-kit); ported from
deep-search bin/release_record.py (#2232) and orbit-api k8/release (#1525).
Stdlib only; it runs in gcr.io/cloud-builders/kubectl (Python 3.10).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_capacity  # noqa: E402
import release_config as rc  # noqa: E402
from promote_release_guard import ONGOING_STATUSES, GoogleApi, GuardError, _http_json, github_compare_status  # noqa: E402

SCHEMA_VERSION = 1
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT_IN_IMAGE_RE = re.compile(r":([0-9a-f]{40})(?:@|$)")
PR_IN_TITLE_RE = re.compile(r"\(#(\d+)\)\s*$")
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
BAD_WAITING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)
# The role of the service image in record["images"].
IMAGE_ROLE = "app"

Environment = rc.Environment


class RecordError(Exception):
    """A release record or snapshot operation failed."""


# The Cloud Build images run Python 3.10, which has no datetime.UTC.
UTC = timezone.utc  # noqa: UP017


def utc_now() -> str:
    """The time now in UTC as an RFC 3339 string with seconds (2026-10-21T12:00:00Z)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    """Parse an RFC 3339 UTC time (Z or +00:00, any fraction) on Python 3.10."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|\+00:00)$", value)
    if not match:
        raise RecordError(f"unexpected time format: {value!r}")
    fraction = (match.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{match.group(1)}.{fraction}+00:00")


def digest_of(reference: str | None) -> str | None:
    """Return the sha256 digest in an image reference or imageID, else None."""
    if not reference:
        return None
    match = DIGEST_RE.search(reference)
    return match.group(0) if match else None


def commit_of(reference: str | None) -> str | None:
    """Return the 40-character commit tag of an image reference, else None."""
    if not reference:
        return None
    match = COMMIT_IN_IMAGE_RE.search(reference)
    return match.group(1) if match else None


def release_id_for(started_at: str, commit: str) -> str:
    """The release id: the start time (YYYYMMDDTHHMMSSZ) and the first 9 characters of the commit."""
    return f"{parse_utc(started_at).strftime('%Y%m%dT%H%M%SZ')}-{commit[:9]}"


# --------------------------------------------------------------------------- commands


class Runner(Protocol):
    """Runs one CLI command and returns its stdout. Every runner takes the optional timeout in seconds:
    the window check bounds each command by its remaining observation budget (release_verify.read_fresh)."""

    def __call__(self, args: list[str], /, timeout: float = 120) -> str: ...


def run_command(args: list[str], timeout: float = 120) -> str:
    """Run a CLI command and return stdout; raise RecordError with stderr on failure."""
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RecordError(f"{' '.join(args[:4])} failed: {error}") from None
    if result.returncode != 0:
        raise RecordError(f"{' '.join(args[:6])} exited {result.returncode}: {result.stderr.strip()[:800]}")
    return result.stdout


@dataclass
class Clients:
    """kubectl and gcloud calls for one environment (injectable for tests)."""

    env: Environment
    runner: Runner = run_command
    kube_context: str | None = None

    def kubectl_base(self) -> list[str]:
        """The kubectl command, with --context when a kube context is set."""
        return ["kubectl"] + (["--context", self.kube_context] if self.kube_context else [])

    def kubectl_json(self, *args: str, namespace: bool = True) -> dict:
        """Run a kubectl get with JSON output (in the namespace unless namespace=False) and decode it."""
        command = self.kubectl_base()
        if namespace:
            command += ["-n", self.env.namespace]
        command += [*args, "-o", "json", "--request-timeout=30s"]
        return json.loads(self.runner(command))


def scaled_rollout_timeouts(
    clients: Clients, names: list[str], *, minimum: int = 0, factor: int = 1, elapsed: float = 0
) -> dict[str, int]:
    """Rule 3: each Deployment's rollout timeout (release_capacity.rollout_timeout) times factor, less the
    seconds already spent, and at least `minimum`. A Deployment that cannot be read gets the base timeout."""
    out = {}
    for name in names:
        try:
            seconds = release_capacity.rollout_timeout(clients.kubectl_json("get", "deployment", name))
        except (RecordError, ValueError, KeyError, TypeError) as error:
            print(f"WARNING: deployment/{name} unreadable, base rollout timeout: {error}", file=sys.stderr, flush=True)
            seconds = release_capacity.ROLLOUT_BASE_SECONDS
        out[name] = max(int(minimum), round(seconds * factor - elapsed))
    return out


def wait_rollouts(
    clients: Clients,
    names: list[str],
    timeout_seconds: int = 900,
    *,
    timeouts: dict[str, int] | None = None,
    popen: Callable[..., subprocess.Popen] | None = None,
) -> list[str]:
    """Wait for Deployment rollouts in parallel; return one line per rollout that did not complete.

    timeouts: seconds per Deployment (scaled_rollout_timeouts); the others wait timeout_seconds.
    """
    processes = {}
    failures = []
    for name in names:
        command = [*clients.kubectl_base(), "-n", clients.env.namespace, "rollout", "status", f"deployment/{name}"]
        seconds = (timeouts or {}).get(name, timeout_seconds)
        try:
            processes[name] = (popen or subprocess.Popen)(
                [*command, f"--timeout={seconds}s"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except OSError as error:
            failures.append(f"deployment/{name} rollout could not be watched: {error}")
    for name, process in processes.items():
        output, _ = process.communicate()
        last = output.strip().splitlines()[-1] if output.strip() else str(process.returncode)
        print(f"rollout {name}: {last}", flush=True)
        if process.returncode != 0:
            failures.append(f"deployment/{name} rollout did not complete: {last}")
    return failures


# --------------------------------------------------------------------------- snapshots


def _container(spec: dict, name: str) -> dict | None:
    """The container with this name in a pod spec, else None."""
    for container in spec.get("containers") or []:
        if container.get("name") == name:
            return container
    return None


def owned_replica_sets(replica_sets: list[dict], deployment: str) -> list[dict]:
    """The ReplicaSets that the Deployment owns."""
    return [
        rs
        for rs in replica_sets
        if any(
            ref.get("kind") == "Deployment" and ref.get("name") == deployment
            for ref in rs["metadata"].get("ownerReferences") or []
        )
    ]


def owned_pods(pods: list[dict], replica_sets: list[dict], deployment: str) -> list[dict]:
    """Pods that belong to the Deployment through its ReplicaSets."""
    owned_sets = {rs["metadata"]["name"] for rs in owned_replica_sets(replica_sets, deployment)}
    return [
        pod
        for pod in pods
        if any(
            ref.get("kind") == "ReplicaSet" and ref.get("name") in owned_sets
            for ref in pod["metadata"].get("ownerReferences") or []
        )
    ]


def is_live(pod: dict) -> bool:
    """Terminating pods drain for minutes; evicted and finished pods stay listed."""
    return not pod["metadata"].get("deletionTimestamp") and pod.get("status", {}).get("phase") not in (
        "Succeeded",
        "Failed",
    )


def is_ready(pod: dict) -> bool:
    """Whether a pod has the condition Ready=True."""
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions") or []
    )


def _pod_summary(pods: list[dict], container: str) -> dict:
    """Digests that live pods run, ready pods, restarts, and pods in a bad waiting state."""
    digests: set[str] = set()
    restarts = 0
    bad: list[str] = []
    live = 0
    ready = 0
    for pod in pods:
        if not is_live(pod):
            continue
        live += 1
        ready += 1 if is_ready(pod) else 0
        for status in pod.get("status", {}).get("containerStatuses") or []:
            if status.get("name") != container:
                continue
            if digest := digest_of(status.get("imageID")):
                digests.add(digest)
            restarts += int(status.get("restartCount") or 0)
            reason = (status.get("state", {}).get("waiting") or {}).get("reason")
            if reason in BAD_WAITING_REASONS:
                bad.append(f"{pod['metadata']['name']}: {reason}")
    return {
        "pod_observations": [
            {
                "name": pod["metadata"]["name"],
                "created_at": pod["metadata"].get("creationTimestamp"),
                "phase": (pod.get("status") or {}).get("phase"),
                "reason": (pod.get("status") or {}).get("reason"),
                "conditions": [
                    {key: condition.get(key) for key in ("type", "status", "reason", "lastTransitionTime")}
                    for condition in (pod.get("status") or {}).get("conditions") or []
                ],
            }
            for pod in pods[:100]
        ],
        "live_pods": live,
        "ready_pods": ready,
        "running_digests": sorted(digests),
        "restarts": restarts,
        "bad_pods": bad,
    }


def live_pods(clients: Clients, deployment: rc.Deployment) -> list[dict]:
    """The live, running pods of one Deployment."""
    pods = clients.kubectl_json("get", "pods", "-l", deployment.selector)["items"]
    replica_sets = clients.kubectl_json("get", "replicasets", "-l", deployment.selector)["items"]
    return [
        pod
        for pod in owned_pods(pods, replica_sets, deployment.name)
        if is_live(pod) and pod.get("status", {}).get("phase") == "Running"
    ]


def snapshot_workloads(clients: Clients, *, missing_ok: bool = False, resolve_tags: bool = True) -> dict[str, dict]:
    """Read the image of every Deployment that the deploy build moves to the release image.

    Keys: deployment/<name>. "image" is the template reference and "digest"
    the digest that the Deployment is set to (the template digest, or for a
    tag-only template the one digest its live pods run). missing_ok: a
    Deployment that does not exist yet (the first deploy of a new environment)
    is left out instead of failing.
    """
    workloads: dict[str, dict] = {}
    for item in clients.env.deployments:
        try:
            deployment = clients.kubectl_json("get", "deployment", item.name)
        except RecordError as error:
            if missing_ok and "NotFound" in str(error):
                print(f"deployment/{item.name} does not exist yet (first deploy)", flush=True)
                continue
            raise
        pods = clients.kubectl_json("get", "pods", "-l", item.selector)["items"]
        replica_sets = clients.kubectl_json("get", "replicasets", "-l", item.selector)["items"]
        container = _container(deployment["spec"]["template"]["spec"], item.container)
        if container is None:
            raise RecordError(f"deployment/{item.name} has no container {item.container}")
        status = deployment.get("status") or {}
        summary = _pod_summary(owned_pods(pods, replica_sets, item.name), item.container)
        digest = digest_of(container["image"])
        if digest is None and len(summary["running_digests"]) == 1:
            digest = summary["running_digests"][0]
        if digest is None and resolve_tags:
            digest = tag_digest(clients.env, container["image"])
        workloads[f"deployment/{item.name}"] = {
            "kind": "deployment",
            "name": item.name,
            "container": item.container,
            "selector": item.selector,
            "image": container["image"],
            "digest": digest,
            "replicas": int(deployment["spec"].get("replicas") or 0),
            "updated_replicas": int(status.get("updatedReplicas") or 0),
            "available_replicas": int(status.get("availableReplicas") or 0),
            "rollout_current": int(status.get("observedGeneration") or 0)
            >= int(deployment["metadata"].get("generation") or 0),
            **summary,
        }
    return workloads


def image_from_snapshot(env: Environment, workloads: dict[str, dict]) -> str | None:
    """The image (repo@digest) that a snapshot shows the main Deployment running."""
    digest = workloads.get(f"deployment/{env.main_deployment}", {}).get("digest")
    return f"{env.image_repository}@{digest}" if digest else None


# --------------------------------------------------------------------------- stores


class GcsStore:
    """JSON objects under gs://bucket/prefix with generation preconditions."""

    def __init__(self, uri: str, token: Callable[[], str]) -> None:
        """Parse a gs://bucket/prefix URI; raise RecordError when it is invalid."""
        match = re.match(r"^gs://([a-z0-9._-]+)(?:/(.*))?$", uri)
        if not match:
            raise RecordError(f"invalid store {uri!r}")
        self.uri = uri.rstrip("/")
        self.bucket = match.group(1)
        self.prefix = (match.group(2) or "").strip("/")
        self._token = token

    def _object(self, name: str) -> str:
        """The full object name of a store name under the prefix."""
        return f"{self.prefix}/{name}" if self.prefix else name

    def location(self, name: str) -> str:
        """The gs:// URI of a store object."""
        return f"gs://{self.bucket}/{self._object(name)}"

    def read(self, name: str) -> tuple[dict | None, int]:
        """Return (data, generation) of an object; (None, 0) when it does not exist."""
        quoted = urllib.parse.quote(self._object(name), safe="")
        url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quoted}?alt=json"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            meta = _http_json(urllib.request.Request(url, headers=headers))
        except GuardError as error:
            if "HTTP 404" in str(error):
                return None, 0
            raise RecordError(f"read {self.location(name)}: {error}") from None
        generation = int(meta["generation"])
        media = (
            f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quoted}?alt=media&generation={generation}"
        )
        try:
            return _http_json(urllib.request.Request(media, headers=headers)), generation
        except GuardError as error:
            raise RecordError(f"read {self.location(name)}: {error}") from None

    def write(self, name: str, data: dict, if_generation: int) -> None:
        """Write when the object is still at if_generation (0 = must not exist)."""
        query = urllib.parse.urlencode(
            {"uploadType": "media", "name": self._object(name), "ifGenerationMatch": str(if_generation)}
        )
        request = urllib.request.Request(
            f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o?{query}",
            data=json.dumps(data, indent=2, sort_keys=True).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
        )
        try:
            _http_json(request, attempts=3)
        except GuardError as error:
            if "HTTP 412" in str(error):
                raise ConcurrentWrite(name) from None
            raise RecordError(f"write {self.location(name)}: {error}") from None


class ConcurrentWrite(Exception):
    """Another writer changed the object after it was read."""


class LocalStore:
    """A directory store: tests, and dry runs that read a base store and write nowhere else."""

    def __init__(self, root: Path, base: GcsStore | LocalStore | None = None) -> None:
        """A store in the root directory that reads from base what the directory does not hold."""
        self.root = root
        self.base = base
        self.uri = f"file://{root}"

    def location(self, name: str) -> str:
        """The local path of a store object."""
        return str(self.root / name)

    def read(self, name: str) -> tuple[dict | None, int]:
        """Return (data, generation) from the directory, else from the base store with generation 0."""
        path = self.root / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data, int(data.get("_local_generation", 1))
        if self.base is not None:
            data, _ = self.base.read(name)
            return data, 0
        return None, 0

    def write(self, name: str, data: dict, if_generation: int) -> None:
        """Write the object when its local generation is still if_generation; else raise ConcurrentWrite."""
        path = self.root / name
        current = json.loads(path.read_text(encoding="utf-8")).get("_local_generation", 1) if path.exists() else 0
        if current != if_generation:
            raise ConcurrentWrite(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "_local_generation": current + 1}, indent=2, sort_keys=True), "utf-8")


Store = GcsStore | LocalStore


def open_store(uri: str, env: Environment, token: Callable[[], str], dry_run_dir: Path | None = None) -> Store:
    """Open gs://... or a local path; a dry run reads the real store and writes only to dry_run_dir."""
    root = f"{uri.rstrip('/')}/{env.name}"
    store: Store = GcsStore(root, token) if uri.startswith("gs://") else LocalStore(Path(root))
    if dry_run_dir is not None:
        return LocalStore(dry_run_dir, base=store)
    return store


def update_object(store: Store, name: str, change: Callable[[dict | None], dict], attempts: int = 5) -> dict:
    """Read-modify-write one object; retry when another writer won the race."""
    for _ in range(attempts):
        current, generation = store.read(name)
        updated = change(current)
        try:
            store.write(name, updated, generation)
            return updated
        except ConcurrentWrite:
            time.sleep(1)
    raise RecordError(f"{store.location(name)} kept changing; gave up after {attempts} attempts")


# --------------------------------------------------------------------------- the prod lock

# One build at a time changes prod: a deploy build from its gate step to `finish`, and a
# rollback build during its apply. The holder is a build id; a holder build that ended no
# longer holds the lock, so a build that failed half way blocks nobody.
LOCK_OBJECT = "lock.json"
LOCK_POLL_SECONDS = 15


def build_alive(env: Environment) -> Callable[[str], bool]:
    """Whether a build still runs; a build that cannot be read keeps the lock (the safe answer)."""
    api = GoogleApi(env.project, env.region)

    def alive(build_id: str) -> bool:
        """Whether the build is ongoing; True when it cannot be read."""
        try:
            return api.get_build(build_id).status in ONGOING_STATUSES
        except GuardError as error:
            print(
                f"WARNING: cannot read build {build_id}; it keeps the prod lock: {error}", file=sys.stderr, flush=True
            )
            return True

    return alive


def lock_holder(store: Store, alive: Callable[[str], bool]) -> dict | None:
    """The lock while a running build holds it, else None."""
    current, _ = store.read(LOCK_OBJECT)
    holder = (current or {}).get("holder")
    return current if holder and alive(holder) else None


def acquire_lock(
    store: Store,
    holder: str,
    kind: str,
    alive: Callable[[str], bool],
    *,
    wait_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Take the lock for build `holder`; wait while another running build holds it.

    The write is conditional on the object's generation, so of two builds that
    find the lock free only one gets it.
    """
    deadline = clock() + wait_seconds
    announced = ""
    while True:
        current, generation = store.read(LOCK_OBJECT)
        owner = (current or {}).get("holder") or ""
        if not owner or owner == holder or not alive(owner):
            lock = {"holder": holder, "kind": kind, "at": utc_now()}
            if owner and owner != holder:
                lock["took_over_from"] = owner
            try:
                store.write(LOCK_OBJECT, lock, generation)
            except ConcurrentWrite:
                continue
            ended = f"; build {owner} had ended" if lock.get("took_over_from") else ""
            print(f"Prod lock: held by {kind} build {holder}{ended}", flush=True)
            return
        if clock() >= deadline:
            raise RecordError(
                f"the {current.get('kind')} build {owner} still holds the prod lock after {wait_seconds // 60} min "
                f"({store.location(LOCK_OBJECT)})"
            )
        if owner != announced:
            print(f"Waiting for the prod lock: the {current.get('kind')} build {owner} holds it", flush=True)
            announced = owner
        sleep(LOCK_POLL_SECONDS)


def release_lock(store: Store, holder: str | None) -> None:
    """Free the lock when build `holder` holds it; otherwise do nothing."""
    if not holder:
        return
    current, generation = store.read(LOCK_OBJECT)
    if (current or {}).get("holder") != holder:
        return
    try:
        store.write(LOCK_OBJECT, {"holder": "", "released_by": holder, "at": utc_now()}, generation)
    except ConcurrentWrite:
        # Another build took the lock over meanwhile: it is no longer ours to free.
        return
    print(f"Prod lock: freed by build {holder}", flush=True)


def record_name(release_id: str) -> str:
    """The store object name of a release record."""
    return f"releases/{release_id}.json"


def read_record(store: Store, release_id: str) -> dict:
    """Read a release record by id ("latest": the one latest.json names); raise RecordError when it is missing."""
    if release_id == "latest":
        pointer, _ = store.read("latest.json")
        if not pointer:
            raise RecordError(f"no {store.location('latest.json')}; no release was recorded yet")
        release_id = pointer["release_id"]
    record, _ = store.read(record_name(release_id))
    if record is None:
        raise RecordError(f"release record {store.location(record_name(release_id))} does not exist")
    return record


def _with_event(record: dict, event: str, details: dict | None) -> dict:
    """Append a history event to the record and set updated_at."""
    record["history"] = [*(record.get("history") or []), {"at": utc_now(), "event": event, **(details or {})}]
    record["updated_at"] = utc_now()
    record.pop("_local_generation", None)
    return record


def create_record(store: Store, record: dict, event: str, *, replace: bool = False) -> dict:
    """Write a new record; refuse to overwrite one unless replace (backfill re-runs)."""

    def change(current: dict | None) -> dict:
        """The new record with its first history event; raise RecordError when it exists and replace is off."""
        if current is not None and not replace:
            raise RecordError(f"release record {record['release_id']} already exists")
        return _with_event(json.loads(json.dumps(record)), event, None)

    return update_object(store, record_name(record["release_id"]), change)


def update_record(
    store: Store, release_id: str, mutate: Callable[[dict], None], event: str, details: dict | None = None
) -> dict:
    """Apply mutate to the record as it is stored now, and append a history event.

    mutate runs again on every retry against the newest stored version, so a
    concurrent writer's fields (for example a rollback's rolled_back_by) stay.
    """

    def change(current: dict | None) -> dict:
        """A copy of the stored record with mutate applied and the event appended."""
        if current is None:
            raise RecordError(f"release record {release_id} does not exist")
        updated = json.loads(json.dumps(current))
        mutate(updated)
        return _with_event(updated, event, details)

    return update_object(store, record_name(release_id), change)


def newer_latest(store: Store, release_id: str) -> str | None:
    """The release (or rollback) latest.json names when it is newer than release_id, else None.

    Release ids start with their UTC start time (release_id_for), so a later id is a newer release."""
    pointer, _ = store.read("latest.json")
    current = str((pointer or {}).get("release_id") or "")
    return current if current and current != release_id and current > release_id else None


def point_latest_unless_newer(store: Store, record: dict) -> None:
    """Point latest.json at a finished record, unless a newer release or rollback is latest by then.

    A finish that runs late (a recovery from the alert) must never hide a newer release or rollback
    record, or a later promote would miss its hold. The check runs inside the conditional write."""

    def change(current: dict | None) -> dict:
        """The new pointer; refuse when the pointer names a newer release."""
        named = str((current or {}).get("release_id") or "")
        if named and named != record["release_id"] and named > record["release_id"]:
            raise RecordError(
                f"latest.json names the newer release {named}; finishing {record['release_id']} would hide it"
            )
        return {
            "release_id": record["release_id"],
            "commit": record["commit"],
            "kind": record["kind"],
            "object": store.location(record_name(record["release_id"])),
            "updated_at": utc_now(),
        }

    update_object(store, "latest.json", change)


def refuse_newer_latest(store: Store, release_id: str) -> None:
    """Raise RecordError when a newer release or rollback is latest: a finish must not touch the old record."""
    if newer := newer_latest(store, release_id):
        raise RecordError(
            f"latest.json names the newer release {newer}; {release_id} is no longer what runs, so its record "
            "is left as it is (finish it only while it is the newest release)"
        )


def point_latest(store: Store, record: dict) -> None:
    """Point latest.json at the record."""
    update_object(
        store,
        "latest.json",
        lambda _: {
            "release_id": record["release_id"],
            "commit": record["commit"],
            "kind": record["kind"],
            "object": store.location(record_name(record["release_id"])),
            "updated_at": utc_now(),
        },
    )


# --------------------------------------------------------------------------- GitHub


def github_token(env: Environment) -> str:
    """GITHUB_TOKEN when set, else a read-only token from the orbit-search Cloud Build connection."""
    if token := os.environ.get("GITHUB_TOKEN", "").strip():
        return token
    try:
        return GoogleApi(env.project, env.region).repository_read_token(env.connection, env.connected_repository)
    except subprocess.SubprocessError as error:
        raise GuardError(f"no gcloud access token for the GitHub read token: {error}") from None


def github_json(path: str, token: str) -> dict | list:
    """GET a GitHub API path with the token and decode the JSON."""
    request = urllib.request.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return _http_json(request)


def commits_between(repo: str, base: str | None, head: str, token: str) -> tuple[list[dict], str]:
    """Commits in base..head, oldest first, with PR numbers from squash-merge titles."""
    if not base:
        return [], "previous commit unknown"
    if base == head:
        return [], "same commit as the previous release"
    status = github_compare_status(repo, base, head, token)
    payload = github_json(f"repos/{repo}/compare/{base}...{head}?per_page=250", token)
    commits = []
    for item in payload.get("commits") or []:
        title = item["commit"]["message"].splitlines()[0]
        pr = PR_IN_TITLE_RE.search(title)
        commits.append(
            {
                "sha": item["sha"],
                "title": title,
                "pr": int(pr.group(1)) if pr else None,
                "author": (item.get("author") or {}).get("login") or item["commit"]["author"]["name"],
            }
        )
    note = f"compare {status}"
    if int(payload.get("total_commits") or 0) > len(commits):
        note += f"; {payload['total_commits']} commits, list truncated to {len(commits)}"
    return commits, note


def head_pr(repo: str, commit: str, token: str) -> dict | None:
    """The merged PR of the release commit and its author, from GitHub's commit-to-PR index; None when unknown.

    The PR's own author (a commit's author can be someone else), and found also when the release's commit list
    was truncated. Release ownership (release_observation.release_author) reads it."""
    try:
        pulls = github_json(f"repos/{repo}/commits/{commit}/pulls?per_page=10", token)
    except (GuardError, OSError, ValueError) as error:
        print(f"WARNING: could not read the PR of {commit[:9]}: {error}", file=sys.stderr, flush=True)
        return None
    for pull in pulls if isinstance(pulls, list) else []:
        login = (pull.get("user") or {}).get("login") if isinstance(pull, dict) else None
        if login and pull.get("merged_at") and pull.get("number"):
            return {"pr": int(pull["number"]), "author": login}
    return None


# --------------------------------------------------------------------------- Slack


def redact_slack_ids(text: str) -> str:
    """The text for a log: Slack member mentions become <@...>, so ids never reach build or CI logs."""
    return re.sub(r"<@[A-Z0-9]+>", "<@...>", text)


# Every release moment has one status line, in the build log only (post_slack). The reasons (checks, error
# rates, holds, rollback targets, commits) are on the ops board, one page per service at
# <RELEASE_OPS_URL>/<service>, where <service> is the folder of the service's release records
# (gs://orbit-search-release-records/<service>/).
OPS_URL = "https://ops.orbitsearch.com"
STATUS_DOTS = {
    "deploying": ":large_green_circle:",
    "up": ":large_green_circle:",
    "warning": ":large_yellow_circle:",
    "on hold": ":large_yellow_circle:",
    "down": ":red_circle:",
}
# The words a line shows for each state: they name the release event, not the health of the service (a failed
# release is not a service that is down). A caller with a more exact event passes its own words. The ops
# board keeps its own labels for these states.
STATUS_WORDS = {
    "deploying": "deploying",
    "up": "released",
    "warning": "release check inconclusive",
    "on hold": "on hold",
    "down": "release failed",
}
SHORT_SHA = 7
REASON_LIMIT = 200


def slack_text(text: str, limit: int = REASON_LIMIT) -> str:
    """Free text (a human's reason) made safe for a status line: one line, at most limit characters, and
    Slack's control characters escaped (no mention, channel ping or link can come from it)."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[: limit - 3].rstrip() + "..."
    return flat.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_words(event: str, by: str, reason: str = "") -> str:
    """The words of a human's release command: "<event> by <who>", then ": <reason>" when there is one."""
    reason = slack_text(reason)
    return f"{event} by {slack_text(by, 60)}" + (f": {reason}" if reason else "")


def ops_url(service: str, base: str | None = None) -> str:
    """The ops board page of a service: base, else RELEASE_OPS_URL, else OPS_URL; then /<service>."""
    base = (base or os.environ.get("RELEASE_OPS_URL", "")).strip().rstrip("/") or OPS_URL
    return f"{base}/{service}"


def release_label(record: dict | None) -> str:
    """The label of a release record on its status line: "#<pr> <short sha>", the parts it has; "" for none.

    The commit is the record's commit, else the one in its release id (<time>-<commit>). The PR is the
    release commit's merged PR (head_pr), else the PR of that commit in the record's commit list."""
    if not record:
        return ""
    release_id = str(record.get("release_id") or "")
    commit = str(record.get("commit") or (release_id.rsplit("-", 1)[-1] if "-" in release_id else ""))
    pr = (record.get("head_pr") or {}).get("pr")
    if not pr and commit:
        pr = next(
            (
                item.get("pr")
                for item in record.get("commits") or []
                if isinstance(item, dict) and item.get("pr") and str(item.get("sha") or "").startswith(commit)
            ),
            None,
        )
    return " ".join([*([f"#{pr}"] if pr else []), *([commit[:SHORT_SHA]] if commit else [])])


def status_line(
    state: str,
    service: str,
    mentions: list[str] | tuple[str, ...] = (),
    base: str | None = None,
    *,
    release: str = "",
    words: str | None = None,
) -> str:
    """The one Slack line of a release moment: dot, service, release, the event words and the ops board link.

    state: deploying, up, warning, on hold or down (the dot). release: "#<pr> <short sha>" (release_label).
    words: the event, default STATUS_WORDS[state]. mentions: the owner Slack mentions of an alert, at the end
    of the line. base: the ops board (see ops_url). Raise KeyError on an unknown state."""
    url = ops_url(service, base)
    head = " ".join(part for part in (STATUS_DOTS[state], f"*{service}*", release) if part)
    line = f"{head} {words or STATUS_WORDS[state]} · <{url}|{url.split('://', 1)[-1]}>"
    return " ".join([line, *mentions])


def post_status(
    state: str,
    service: str,
    mentions: list[str] | tuple[str, ...] = (),
    detail: str = "",
    *,
    record: dict | None = None,
    words: str | None = None,
) -> bool:
    """Print the detail and the status line for the build log (Slack ids redacted); always True.

    Nothing goes to Slack (see post_slack). record: the release the line is about (its PR and short sha go on
    the line). words: see status_line."""
    if detail:
        print(f"{state}: {redact_slack_ids(detail)}", flush=True)
    return post_slack(status_line(state, service, mentions, release=release_label(record), words=words))


def post_slack(text: str, blocks: list[dict] | None = None) -> bool:
    """Print one release line for the build log (Slack ids redacted) and return True, as if it was sent.

    The release tooling posts nothing to #deployments (Nicholas, 2026-10-29: the lines were not useful, and
    other alerts cover releases). The ops board and the build log have every release moment. Callers that
    note a post (alert_once, the warnings, the environmental marks) settle at once and never wait or retry.
    blocks: accepted and ignored."""
    del blocks
    print(f"Release line (build log only): {redact_slack_ids(text)}", flush=True)
    return True


# --------------------------------------------------------------------------- record lifecycle


def previous_release(store: Store, env: Environment, before: dict[str, dict]) -> dict:
    """Describe what ran before this deploy: the latest record when it matches the snapshot."""
    image = image_from_snapshot(env, before)
    main = before.get(f"deployment/{env.main_deployment}", {})
    pointer, _ = store.read("latest.json")
    previous = {
        "release_id": None,
        "commit": commit_of(main.get("image")),
        "recorded": False,
        "images": {IMAGE_ROLE: image},
        "workloads": before,
    }
    if pointer:
        record, _ = store.read(record_name(pointer["release_id"]))
        if record and digest_of((record.get("images") or {}).get(IMAGE_ROLE)) == digest_of(image):
            previous.update(release_id=record["release_id"], commit=record["commit"], recorded=True)
            previous["deploy_finished"] = (record.get("times") or {}).get("deploy_finished")
        elif record:
            previous["note"] = (
                f"latest record {record['release_id']} has image {record.get('images', {}).get(IMAGE_ROLE)}, "
                "but the environment ran another digest; the snapshot is the source of truth"
            )
    return previous


def describe(env: Environment) -> dict:
    """Where the release runs: the GitHub repository, the deploy target and the domains of the environment.

    Orbit Ops (SMP /ops) finds services by these fields. A value the kit does not know is left out, never null.
    """
    out: dict = {"repo": env.github_repo} if env.github_repo else {}
    target = {key: list(value) if isinstance(value, tuple) else value for key, value in env.deploy_target}
    if target:
        out["deploy_target"] = target
    if env.domains:
        out["domains"] = list(env.domains)
    return out


def new_record(
    env: Environment,
    *,
    kind: str,
    commit: str,
    images: dict[str, str | None],
    build: dict,
    previous: dict,
    started_at: str,
    token: str | None,
) -> dict:
    """A new deploying release record, with the commits since the previous release when a token is given."""
    commits, note = ([], "no GitHub token")
    pull = None
    if token:
        try:
            commits, note = commits_between(env.github_repo, previous.get("commit"), commit, token)
        except GuardError as error:
            note = f"GitHub compare failed: {error}"
        pull = head_pr(env.github_repo, commit, token)
    extra = {"head_pr": pull} if pull else {}
    return {
        **extra,
        **describe(env),
        "schema_version": SCHEMA_VERSION,
        "service": env.service,
        "environment": env.name,
        "release_id": release_id_for(started_at, commit),
        "kind": kind,
        "commit": commit,
        "build": build,
        "status": "deploying",
        "times": {"build_created": build.get("create_time"), "deploy_started": started_at},
        "images": images,
        "previous": previous,
        "commits": commits,
        "commits_note": note,
        "workloads": {},
        "checks": {},
        "verdict": {"status": "pending"},
        "rollback_suggestion": None,
        "rolled_back_by": None,
    }


def build_url(env: Environment, build_id: str) -> str:
    """The Cloud Console link of a build."""
    return f"https://console.cloud.google.com/cloud-build/builds;region={env.region}/{build_id}?project={env.project}"


def build_info(env: Environment, build_id: str | None) -> dict:
    """The id, link, trigger and create time of a build; an error field when the build cannot be read."""
    if not build_id:
        return {"id": None}
    info = {"id": build_id, "url": build_url(env, build_id)}
    try:
        build = GoogleApi(env.project, env.region).get_build(build_id)
        info.update(trigger=build.trigger_name, create_time=build.create_time.replace("+00:00", "Z"))
    except (GuardError, subprocess.SubprocessError) as error:
        info["error"] = str(error)
    return info


def _read_text(path: str | None) -> str | None:
    """The stripped text of a file, or None when there is no path or the file is empty."""
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8").strip()
    return text or None


def cmd_begin(args: argparse.Namespace, env: Environment, store: Store, clients: Clients) -> int:
    """Before the first change to the cluster: snapshot what runs now and write a "deploying" record."""
    started_at = utc_now()
    # The first deploy of a new environment creates its Deployments: before it, nothing runs.
    before = snapshot_workloads(clients, missing_ok=True)
    previous = previous_release(store, env, before)
    image = _read_text(args.image_file)
    if not digest_of(image):
        raise RecordError(f"the release image {image!r} has no digest")
    try:
        token = github_token(env)
    except GuardError as error:
        print(f"WARNING: no GitHub token, the record has no commit list: {error}", file=sys.stderr)
        token = None
    record = new_record(
        env,
        kind="promote",
        commit=args.commit,
        images={IMAGE_ROLE: image},
        build=build_info(env, args.build_id),
        previous=previous,
        started_at=started_at,
        token=token,
    )
    create_record(store, record, "deploy_started")
    Path(args.release_id_file).write_text(record["release_id"] + "\n", encoding="utf-8")
    print(f"Release {record['release_id']}: {store.location(record_name(record['release_id']))}", flush=True)
    print(f"Previous: {previous.get('release_id') or 'unrecorded'} commit {previous.get('commit')}", flush=True)
    for name, workload in sorted(before.items()):
        print(f"  before {name}: {workload.get('digest') or workload.get('image')}", flush=True)
    print(f"Commits since previous ({record['commits_note']}): {len(record['commits'])}", flush=True)
    return 0


def cmd_finish(args: argparse.Namespace, env: Environment, store: Store, clients: Clients) -> int:
    """After the last deploy step: snapshot what runs now, set the status, move latest.json."""
    release_id = _read_text(args.release_id_file) if args.release_id_file else args.release_id
    if not release_id:
        raise RecordError("no release id; release-record-begin did not run")
    refuse_newer_latest(store, release_id)
    after = snapshot_workloads(clients)
    step_file = getattr(args, "failed_step_file", None)
    step_text = getattr(args, "failed_step", None)
    # --failed-step carries the step itself (the recovery command from the alert, after the build's
    # /workspace is gone); "" means the build named its steps and none failed by name.
    steps_named = bool(step_file) or step_text is not None
    failed_step = None
    if args.status == "deploy_failed":
        failed_step = parse_failed_step(step_text) if step_text is not None else read_failed_step(step_file)

    def finish(record: dict) -> None:
        """Write the snapshot, the status, the failed step and the deploy finish time on the record."""
        record["workloads"] = after
        keep_finish_time(record, args.status)
        record["status"] = args.status
        if failed_step:
            record["failed_step"] = failed_step
        if steps_named:
            # This build names every failed deploy step (release_kit.py prod build contract): an unnamed
            # deploy_failed is then never a capacity wait (release_verify.deploy_capacity).
            record["deploy_steps_named"] = True

    saved = update_record(store, release_id, finish, args.status)
    point_latest_unless_newer(store, saved)
    print(f"Release {release_id}: {args.status}; {store.location('latest.json')} points at it", flush=True)
    # The deploy is over: a rollback may start now. A build that ends frees the lock too.
    try:
        release_lock(store, (saved.get("build") or {}).get("id"))
    except (RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not free the prod lock; it ends with this build: {error}", file=sys.stderr, flush=True)
    return 0


def read_failed_step(path: str | None) -> dict | None:
    """The failed deploy step that release_capacity.py rollout-wait wrote ({"id", "deployments"}), else None."""
    return parse_failed_step(_read_text(path) if path and Path(path).is_file() else None)


def parse_failed_step(text: str | None) -> dict | None:
    """A failed step from its file text or a --failed-step value (JSON, or a bare step id), else None."""
    if not text or not text.strip():
        return None
    try:
        step = json.loads(text)
    except ValueError:
        return {"id": text.strip()[:80]}
    return step if isinstance(step, dict) and step.get("id") else None


def keep_finish_time(record: dict, status: str) -> None:
    """Set the deploy finish time; a retried finish of the same status keeps the first one (the window's clock)."""
    times = record.setdefault("times", {})
    if not (record.get("status") == status and times.get("deploy_finished")):
        times["deploy_finished"] = utc_now()


# Waits between the attempts of a prod record finish (release_record.py / release_light.py finish --attempts).
FINISH_RETRY_SECONDS = 15.0
FINISH_ERRORS = (RecordError, GuardError, subprocess.SubprocessError, KeyError, OSError, ValueError)


def finish_recovery(
    tool: str,
    service: str,
    env: str,
    release_id: str,
    status: str,
    *,
    failed_step: dict | None = None,
    steps_named: bool = False,
) -> list[str]:
    """The commands that finish a release record by hand, then run the checks the build skipped.

    A GKE build that names its failed steps (--failed-step-file) passes them on: the recovery runs after
    the build's /workspace is gone, so the command carries the step itself (--failed-step). Without it,
    the hand finish would write an unnamed deploy_failed, which the release check may excuse as capacity."""
    rid = release_id or "<release id>"
    if tool == "light":
        base = f"python3 release/release_light.py --service {service}"
        return [
            f"{base} finish --env {env} --release-id {rid} --status {status}",
            f"{base} check --env {env} --release-id {rid}",
            f'{base} schedule-window --release-id {rid} --source-dir "$(mktemp -d)"',
        ]
    finish = (
        f"python3 release/release_record.py finish --service {service} --env {env} --release-id {rid} --status {status}"
    )
    if steps_named:
        step = json.dumps(failed_step, separators=(",", ":"), sort_keys=True) if failed_step else ""
        finish += f" --failed-step {shlex.quote(step)}"
    return [
        finish,
        f"python3 release/release_verify.py check --service {service} --env {env} --phase deploy --release-id {rid} "
        "--wait-rollouts-seconds 300",
        f"python3 release/release_verify.py schedule-window --service {service} --env {env} --release-id {rid} "
        '--source-dir "$(mktemp -d)"',
    ]


def finish_with_retries(
    finish: Callable[[], int],
    *,
    attempts: int,
    service: str,
    env: str,
    release_id: str,
    recovery: list[str],
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Run a record finish up to `attempts` times; return its exit code, or 1 when every attempt failed.

    The deploy has changed prod by now. A finish that still fails leaves the new image without smoke
    checks and watch window, so the alert (prod) gives the exact recovery commands, in order.
    """
    attempts = max(1, attempts)
    error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return finish()
        except FINISH_ERRORS as failure:
            error = failure
            print(f"release record finish, attempt {attempt}/{attempts}: {failure}", file=sys.stderr, flush=True)
            if attempt < attempts:
                (sleep or time.sleep)(FINISH_RETRY_SECONDS * attempt)
    steps = " ".join(f"{index}) `{command}`" for index, command in enumerate(recovery, 1))
    text = (
        f"{service} {env} release {release_id or '(unknown id)'}: the deploy changed {env}, but "
        f"the release record was not finished after {attempts} attempt(s): {str(error)[:300]}. The new image "
        "runs UNCHECKED (no smoke checks, no +15/+60 watch window, no automatic rollback) and this build ends "
        f"red. From a checkout of this commit with {env} access, run in order: {steps}. The finish refuses "
        "once a newer release or rollback is latest; then this release no longer runs and needs no checks."
    )
    print(text, file=sys.stderr, flush=True)
    if env == "prod":
        post_status("down", service, record={"release_id": release_id}, words="release unchecked")
    return 1


def cmd_unlock(args: argparse.Namespace, env: Environment, store: Store, clients: Clients) -> int:
    """Release only this build's prod lock after a skipped or unrecorded deployment path ends."""
    release_lock(store, args.build_id)
    return 0


def cmd_show(args: argparse.Namespace, env: Environment, store: Store, clients: Clients) -> int:
    """Print a release record as JSON."""
    print(json.dumps(read_record(store, args.release_id), indent=2, sort_keys=True))
    return 0


def _revision(rs: dict) -> int:
    """The deployment revision of a ReplicaSet; 0 when the annotation is missing or not a number."""
    try:
        return int((rs["metadata"].get("annotations") or {}).get(REVISION_ANNOTATION, "0"))
    except ValueError:
        return 0


def digest_before(clients: Clients, deployment: rc.Deployment, current: str | None) -> str | None:
    """Digest of the Deployment's newest older ReplicaSet revision that ran another image.

    The ReplicaSet history (revisionHistoryLimit) keeps what each earlier
    rollout ran, so it names the release before the current one.
    """
    replica_sets = clients.kubectl_json("get", "replicasets", "-l", deployment.selector)["items"]
    # The newest revision is the release that runs now: its tag may have moved to another digest
    # since its pods pulled it, so it is never the "before" image.
    for rs in sorted(owned_replica_sets(replica_sets, deployment.name), key=_revision, reverse=True)[1:]:
        spec = ((rs.get("spec") or {}).get("template") or {}).get("spec") or {}
        image = (_container(spec, deployment.container) or {}).get("image")
        digest = digest_of(image) or tag_digest(clients.env, image)
        if digest and digest != current:
            return digest
    return None


TAG_RE = re.compile(r":([\w][\w.-]{0,127})$")


def tag_digest(env: Environment, reference: str | None) -> str | None:
    """The registry digest of a tag-only image reference of the service's image, else None.

    Deployments deployed before the kit name images by tag (`:<commit>`); the
    registry still knows which digest the tag points at.
    """
    if not reference or digest_of(reference) or not reference.startswith(f"{env.image_repository}:"):
        return None
    match = TAG_RE.search(reference)
    if not match:
        return None
    image = env.image
    try:
        return GoogleApi(env.project, env.region).tag_digest(
            image.registry_location, image.registry_repository, image.package, match.group(1)
        )
    except (GuardError, subprocess.SubprocessError) as error:
        print(f"WARNING: cannot resolve {reference} to a digest: {error}", file=sys.stderr, flush=True)
        return None


def cmd_backfill(args: argparse.Namespace, env: Environment, store: Store, clients: Clients) -> int:
    """Write a record for the release that runs now, from before this tooling existed (by its build id).

    The "after" snapshot is live, so run it while the environment still runs that
    release. The "before" digests come from each Deployment's ReplicaSet history.
    """
    api = GoogleApi(env.project, env.region)
    build = api.get_build(args.build_id)
    if build.status != "SUCCESS":
        raise RecordError(f"build {args.build_id} is {build.status}")
    after = snapshot_workloads(clients)
    main = after[f"deployment/{env.main_deployment}"]
    if commit_of(main["image"]) not in (build.commit, None):
        raise RecordError(f"{env.name} no longer runs {build.commit[:9]}; backfill only the release that runs now")
    if not main.get("digest"):
        raise RecordError(f"deployment/{env.main_deployment} has no single digest; wait for its rollout")
    before = {}
    for item in env.deployments:
        key = f"deployment/{item.name}"
        before[key] = {
            **after[key],
            "image": None,
            "digest": digest_before(clients, item, after[key]["digest"]),
            "source": "the newest older ReplicaSet revision with another image",
        }
    previous_build = api.get_build(args.previous_build_id) if args.previous_build_id else None
    previous = {
        "release_id": None,
        "commit": previous_build.commit if previous_build else None,
        "recorded": False,
        "images": {IMAGE_ROLE: image_from_snapshot(env, before)},
        "workloads": before,
        "deploy_finished": previous_build.finish_time[:19] + "Z" if previous_build else None,
        "note": "backfilled from build history and ReplicaSet revisions",
    }
    try:
        token = github_token(env)
    except GuardError:
        token = None
    record = new_record(
        env,
        kind="promote",
        commit=build.commit,
        images={IMAGE_ROLE: image_from_snapshot(env, after)},
        build=build_info(env, args.build_id),
        previous=previous,
        started_at=build.start_time[:19] + "Z",
        token=token,
    )
    record["workloads"] = after
    record["status"] = "deployed"
    record["times"]["deploy_finished"] = build.finish_time[:19] + "Z"
    record["backfilled"] = True
    saved = create_record(store, record, "backfilled", replace=True)
    point_latest(store, saved)
    print(json.dumps(saved, indent=2, sort_keys=True))
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add the options that every subcommand takes."""
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    parser.add_argument("--env", default="prod", choices=["prod", "dev"])
    parser.add_argument(
        "--store", default=os.environ.get("RELEASE_RECORD_STORE", ""), help="default: the config's store"
    )
    parser.add_argument(
        "--dry-run-dir",
        type=Path,
        help="read the real store, write records only under this local directory",
    )
    parser.add_argument("--kube-context", help="kubectl context (omit in Cloud Build, which sets KUBECONFIG)")


def open_from_args(args: argparse.Namespace) -> tuple[rc.Service, Environment, Store, Clients]:
    """Load the service and open the environment, its record store and its clients from the options."""
    service = rc.load_service(getattr(args, "service", None))
    if args.env not in service.environments:
        raise rc.ConfigError(f"{service.name} has no {args.env} environment (tier {service.tier})")
    env = service.environments[args.env]
    api = GoogleApi(env.project, env.region)
    store = open_store(args.store or service.store, env, api.access_token, args.dry_run_dir)
    return service, env, store, Clients(env=env, kube_context=args.kube_context)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run the command; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    begin = commands.add_parser("begin", help="snapshot the environment and write a deploying record")
    add_common_args(begin)
    begin.add_argument("--commit", required=True)
    begin.add_argument("--build-id")
    begin.add_argument("--image-file", required=True, help="file with the release image repo@sha256:...")
    begin.add_argument("--release-id-file", required=True, help="where to write the new release id")

    finish = commands.add_parser("finish", help="snapshot after the deploy and set the record status")
    add_common_args(finish)
    finish.add_argument("--release-id-file")
    finish.add_argument("--release-id")
    finish.add_argument("--status", required=True, choices=["deployed", "deploy_failed"])
    finish.add_argument("--attempts", type=int, default=1, help="retry a failed finish; the last failure alerts")
    finish.add_argument(
        "--failed-step-file", help="the failed step (release_capacity.py rollout-wait), kept with deploy_failed"
    )
    finish.add_argument(
        "--failed-step",
        help="the failed step itself (JSON or a step id; '' = none named), as the recovery command in the alert "
        "passes it after the build ended",
    )

    unlock = commands.add_parser("unlock", help="free the prod lock only if this build holds it")
    add_common_args(unlock)
    unlock.add_argument("--build-id", required=True)

    show = commands.add_parser("show", help="print a record")
    add_common_args(show)
    show.add_argument("release_id", nargs="?", default="latest")

    backfill = commands.add_parser("backfill", help="record the release that runs now, by its deploy build id")
    add_common_args(backfill)
    backfill.add_argument("--build-id", required=True)
    backfill.add_argument("--previous-build-id")

    args = parser.parse_args(argv)
    handlers = {
        "begin": cmd_begin,
        "finish": cmd_finish,
        "unlock": cmd_unlock,
        "show": cmd_show,
        "backfill": cmd_backfill,
    }
    try:
        service, env, store, clients = open_from_args(args)
        if args.command == "finish":
            release_id = (_read_text(args.release_id_file) if args.release_id_file else args.release_id) or ""
            return finish_with_retries(
                lambda: cmd_finish(args, env, store, clients),
                attempts=args.attempts,
                service=service.name,
                env=args.env,
                release_id=release_id,
                recovery=finish_recovery(
                    "gke",
                    service.name,
                    args.env,
                    release_id,
                    args.status,
                    failed_step=(
                        (
                            parse_failed_step(args.failed_step)
                            if args.failed_step is not None
                            else read_failed_step(args.failed_step_file)
                        )
                        if args.status == "deploy_failed"
                        else None
                    ),
                    steps_named=bool(args.failed_step_file) or args.failed_step is not None,
                ),
            )
        return handlers[args.command](args, env, store, clients)
    except (RecordError, GuardError, rc.ConfigError, subprocess.SubprocessError, KeyError, OSError) as error:
        print(f"release record: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
