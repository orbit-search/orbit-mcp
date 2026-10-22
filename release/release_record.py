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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


Runner = Callable[[list[str]], str]


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


def wait_rollouts(clients: Clients, names: list[str], timeout_seconds: int = 900) -> list[str]:
    """Wait for Deployment rollouts in parallel; return one line per rollout that did not complete."""
    processes = {}
    failures = []
    for name in names:
        command = [*clients.kubectl_base(), "-n", clients.env.namespace, "rollout", "status", f"deployment/{name}"]
        try:
            processes[name] = subprocess.Popen(
                [*command, f"--timeout={timeout_seconds}s"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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


def snapshot_workloads(clients: Clients) -> dict[str, dict]:
    """Read the image of every Deployment that the deploy build moves to the release image.

    Keys: deployment/<name>. "image" is the template reference and "digest"
    the digest that the Deployment is set to (the template digest, or for a
    tag-only template the one digest its live pods run).
    """
    workloads: dict[str, dict] = {}
    for item in clients.env.deployments:
        deployment = clients.kubectl_json("get", "deployment", item.name)
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
        if digest is None:
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


# --------------------------------------------------------------------------- Slack


def post_slack(text: str, blocks: list[dict] | None = None) -> bool:
    """Post one line (and optional Block Kit blocks) to the Orbit workspace webhook when WEBHOOK_ORBIT is set.

    Never fails the caller; returns whether Slack took the post. With blocks, text is
    the notification and fallback text. RELEASE_SLACK_DISABLED=1 (test runs) prints only.
    """
    webhook = os.environ.get("WEBHOOK_ORBIT", "").strip()
    if os.environ.get("RELEASE_SLACK_DISABLED", "").strip() == "1":
        webhook = ""
    if not webhook:
        print(f"Slack (not sent, no WEBHOOK_ORBIT): {text}", flush=True)
        return False
    request = urllib.request.Request(
        webhook,
        data=json.dumps({"text": text, **({"blocks": blocks} if blocks else {})}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"WARNING: Slack post failed: {error}", file=sys.stderr, flush=True)
        return False


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
    if token:
        try:
            commits, note = commits_between(env.github_repo, previous.get("commit"), commit, token)
        except GuardError as error:
            note = f"GitHub compare failed: {error}"
    return {
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
    before = snapshot_workloads(clients)
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
    after = snapshot_workloads(clients)

    def finish(record: dict) -> None:
        """Write the snapshot, the status and the deploy finish time on the record."""
        record["workloads"] = after
        record["status"] = args.status
        record.setdefault("times", {})["deploy_finished"] = utc_now()

    saved = update_record(store, release_id, finish, args.status)
    point_latest(store, saved)
    print(f"Release {release_id}: {args.status}; {store.location('latest.json')} points at it", flush=True)
    # The deploy is over: a rollback may start now. A build that ends frees the lock too.
    try:
        release_lock(store, (saved.get("build") or {}).get("id"))
    except (RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not free the prod lock; it ends with this build: {error}", file=sys.stderr, flush=True)
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

    show = commands.add_parser("show", help="print a record")
    add_common_args(show)
    show.add_argument("release_id", nargs="?", default="latest")

    backfill = commands.add_parser("backfill", help="record the release that runs now, by its deploy build id")
    add_common_args(backfill)
    backfill.add_argument("--build-id", required=True)
    backfill.add_argument("--previous-build-id")

    args = parser.parse_args(argv)
    handlers = {"begin": cmd_begin, "finish": cmd_finish, "show": cmd_show, "backfill": cmd_backfill}
    try:
        _, env, store, clients = open_from_args(args)
        return handlers[args.command](args, env, store, clients)
    except (RecordError, GuardError, rc.ConfigError, subprocess.SubprocessError, KeyError, OSError) as error:
        print(f"release record: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
