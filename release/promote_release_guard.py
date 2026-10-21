#!/usr/bin/env python3
"""Guards that let a prod Cloud Build promote the image that the dev build made.

The prod build of a full-tier service (trigger <service>-prod, started by the dev
build of a merge after dev verify passed) builds nothing. Before it deploys it
runs `preflight`, and right before its first change to the cluster it runs
`still-current`:

1. Single flight: a newer build of the prod triggers supersedes this one; an
   older build that still runs is waited for, and so is a running rollback
   build (tag <service>-rollback, release_rollback.py).
2. On the release branch: the commit must be reachable from it on GitHub. The
   GitHub call uses a short-lived read-only token for the repository that the
   `orbit-search` Cloud Build (2nd gen) connection links, so no GitHub secret
   exists.
3. Dev first: the newest dev build of the same commit must succeed.
4. Promote: take the digest that the successful dev build recorded in its
   build step output (`{"image_digest": "sha256:..."}`) and write the image
   references to the workspace for the deploy steps.
5. Still current: no newer build exists and the promoted image did not change.

`turn` is single flight for builds that deploy (a dev build, or the prod build
of a service without a dev stage): an older build that runs is waited for, a
newer one supersedes this build. A prod `turn` also waits for a running
rollback and refuses a commit that is not on the release branch.

Part of the release kit (orbit-infrastructure/release-kit); ported from
deep-search bin/promote_release_guard.py (#2219) and orbit-api #1501.

Exit codes: 0 proceed, 3 superseded (deploy nothing, not a failure), 1 blocked.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402

EXIT_PROCEED = 0
EXIT_BLOCKED = 1
EXIT_SUPERSEDED = 3

ONGOING_STATUSES = frozenset({"PENDING", "QUEUED", "WORKING"})
# GitHub compare statuses for <branch>...<commit> when <commit> is an ancestor of the branch.
ON_BRANCH_COMPARE_STATUSES = frozenset({"identical", "behind"})
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# repository@sha256:... for the release record.
IMAGE_FILE = "release_image"
# repository:<commit>@sha256:... for the Deployments (the tag keeps the commit visible).
K8S_IMAGE_FILE = "release_k8s_image"
# Where the digest came from: "build <id>" (recorded by that dev build) or "tag".
DIGEST_SOURCE_FILE = "release_digest_source"
# Key of the dev build step output that holds the pushed digest.
RECORDED_DIGEST_KEY = "image_digest"
# The step every kit deploy build runs right before its first change to the cluster.
DEPLOY_MARKER_STEP = "release-record-begin"


class GuardError(Exception):
    """A guard failed: the promote must stop before it changes anything."""


class Superseded(Exception):
    """A newer build of the same trigger exists, so this build deploys nothing."""


@dataclass(frozen=True)
class Build:
    id: str
    status: str
    create_time: str
    commit: str
    trigger_name: str
    start_time: str = ""
    finish_time: str = ""
    step_outputs: tuple[str, ...] = ()
    # A rollback build changes nothing unless _DRY_RUN is "false".
    rollback_dry_run: bool = True
    # A dev build promotes to prod unless _AUTO_PROMOTE is "false" (manual dev deploys).
    auto_promote: bool = True

    # Status of each step by id ("release-record-begin" runs right before a deploy's first cluster change).
    step_status: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_api(cls, payload: dict) -> Build:
        substitutions = payload.get("substitutions") or {}
        return cls(
            id=payload["id"],
            status=payload.get("status", "STATUS_UNKNOWN"),
            create_time=payload["createTime"],
            commit=substitutions.get("COMMIT_SHA", ""),
            trigger_name=substitutions.get("TRIGGER_NAME", ""),
            start_time=payload.get("startTime", ""),
            finish_time=payload.get("finishTime", ""),
            rollback_dry_run=substitutions.get("_DRY_RUN", "true") != "false",
            auto_promote=substitutions.get("_AUTO_PROMOTE", "true") != "false",
            step_outputs=tuple(
                _decode_step_output(item) for item in (payload.get("results") or {}).get("buildStepOutputs", [])
            ),
            step_status=tuple(
                (str(step.get("id") or ""), str(step.get("status") or "")) for step in payload.get("steps") or []
            ),
        )

    @property
    def changing_cluster(self) -> bool:
        """Whether this deploy build has reached its first cluster change (or has no marked step, so it may have).

        A deploy build before its "release-record-begin" step only builds, waits for its turn or
        checks the gate, and waits for a running rollback; it changes nothing yet.
        """
        statuses = dict(self.step_status)
        marker = statuses.get(DEPLOY_MARKER_STEP)
        if marker is None:
            return True
        return marker not in ("", "PENDING", "QUEUED", "STATUS_UNKNOWN")

    def recorded_digest(self, key: str = RECORDED_DIGEST_KEY) -> str:
        """Return the digest a build step wrote as {key: digest} to $BUILDER_OUTPUT/output, or ""."""
        digests = set()
        for output in self.step_outputs:
            try:
                value = json.loads(output).get(key, "") if output.startswith("{") else ""
            except (ValueError, AttributeError):
                continue
            if value:
                if not DIGEST_PATTERN.match(str(value)):
                    raise GuardError(f"build {self.id} recorded an invalid {key} {value!r}")
                digests.add(str(value))
        if len(digests) > 1:
            raise GuardError(f"build {self.id} recorded more than one {key}: {sorted(digests)}")
        return digests.pop() if digests else ""

    @property
    def order_key(self) -> tuple[datetime, str]:
        """Builds of one trigger are ordered by creation time, then by id."""
        return (_parse_time(self.create_time), self.id)

    @property
    def ongoing(self) -> bool:
        return self.status in ONGOING_STATUSES

    def describe(self) -> str:
        return f"{self.id} ({self.commit[:9] or 'no commit'}, {self.status}, created {self.create_time})"


def _decode_step_output(value: str) -> str:
    """Build step outputs are base64 in the Cloud Build API; undecodable ones count as empty."""
    try:
        return base64.b64decode(value or "", validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError):
        return ""


def _parse_time(value: str) -> datetime:
    """Parse an RFC 3339 time from the Cloud Build API (nanoseconds are cut to microseconds)."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$", value)
    if not match:
        raise GuardError(f"unexpected Cloud Build time format: {value!r}")
    fraction = (match.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{match.group(1)}.{fraction}+00:00")


def decide_turn(
    own: Build, builds: list[Build], can_supersede: Callable[[Build], bool] | None = None
) -> tuple[str, list[Build]]:
    """Return ("superseded", newer), ("wait", older ongoing) or ("proceed", []).

    can_supersede: which newer builds may replace this one (all when omitted). A newer build
    that may not is ignored here; it waits for this one as an older build that still runs.
    """
    others = {build.id: build for build in builds if build.id != own.id}.values()
    newer = sorted(
        (b for b in others if b.order_key > own.order_key and (can_supersede is None or can_supersede(b))),
        key=lambda b: b.order_key,
    )
    if newer:
        return "superseded", newer
    older_ongoing = sorted((b for b in others if b.order_key < own.order_key and b.ongoing), key=lambda b: b.order_key)
    if older_ongoing:
        return "wait", older_ongoing
    return "proceed", []


def decide_dev(dev_builds: list[Build]) -> tuple[str, Build | None]:
    """Judge the dev builds of the commit: "wait", "ok", "failed" or "overlap".

    Every dev build of a commit pushes the same mutable :<commit> tag. A newest
    build that recorded its digest is "ok" whatever ran beside it. Without a
    recorded digest the tag holds its image only when no other dev build of the
    commit ran its steps after the newest one started ("overlap" otherwise).
    """
    if not dev_builds:
        return "wait", None
    newest = max(dev_builds, key=lambda b: b.order_key)
    if any(build.ongoing for build in dev_builds):
        return "wait", newest
    if newest.status != "SUCCESS":
        return "failed", newest
    if newest.recorded_digest():
        # The prod build promotes the digest this build recorded, so an overlapping rerun cannot
        # change what it deploys; overlap matters only for the :<commit> tag fallback.
        return "ok", newest
    newest_start = _parse_time(newest.start_time)
    for build in dev_builds:
        if build.id == newest.id or not build.start_time:
            continue  # A build that never started pushed nothing.
        if _parse_time(build.finish_time) > newest_start:
            return "overlap", build
    return "ok", newest


def _quote_filter_value(value: str) -> str:
    if '"' in value or "\\" in value:
        raise GuardError(f"refusing to build a Cloud Build filter from {value!r}")
    return f'"{value}"'


def _trigger_names_filter(names: tuple[str, ...]) -> str:
    """(substitutions.TRIGGER_NAME="a" OR ...) for the distinct non-empty names."""
    unique = list(dict.fromkeys(name for name in names if name))
    return "(" + " OR ".join(f"substitutions.TRIGGER_NAME={_quote_filter_value(name)}" for name in unique) + ")"


def _http_json(
    request: urllib.request.Request, *, attempts: int = 4, sleep: Callable[[float], None] = time.sleep
) -> dict:
    """Send a request and decode JSON; retry network errors, 429 and 5xx."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            if not retryable or attempt == attempts:
                body = error.read().decode("utf-8", "replace")[:500]
                raise GuardError(
                    f"{request.get_method()} {request.full_url} returned HTTP {error.code}: {body}"
                ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                raise GuardError(f"{request.get_method()} {request.full_url} failed: {error}") from None
        sleep(2**attempt)
    raise AssertionError("unreachable")


class GoogleApi:
    """Read-only calls to Cloud Build and Artifact Registry with the gcloud identity."""

    def __init__(self, project: str, region: str) -> None:
        self.project = project
        self.region = region
        self._token = ""
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if not self._token or time.monotonic() >= self._token_expires:
            result = subprocess.run(
                ["gcloud", "auth", "print-access-token"], check=True, capture_output=True, text=True, timeout=60
            )
            self._token = result.stdout.strip()
            # Tokens live about an hour; refresh well before that.
            self._token_expires = time.monotonic() + 300
        return self._token

    def access_token(self) -> str:
        """An OAuth access token of the gcloud identity (GCS, Cloud Logging, the GKE API)."""
        return self._access_token()

    def _get(self, url: str) -> dict:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._access_token()}"})
        return _http_json(request)

    def repository_read_token(self, connection: str, repository: str) -> str:
        """Mint a read-only GitHub token for a repository linked to a Cloud Build connection."""
        url = (
            f"https://cloudbuild.googleapis.com/v2/projects/{self.project}/locations/{self.region}"
            f"/connections/{connection}/repositories/{repository}:accessReadToken"
        )
        request = urllib.request.Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"},
        )
        try:
            token = str(_http_json(request).get("token", ""))
        except GuardError as error:
            raise GuardError(
                f"could not get a read token for connection {connection} repository {repository} "
                "(the repository must be linked in that connection and the build identity needs "
                f"roles/cloudbuild.readTokenAccessor on the connection): {error}"
            ) from None
        if not token:
            raise GuardError(f"connection {connection} returned no read token for repository {repository}")
        return token

    def _builds_url(self) -> str:
        return f"https://cloudbuild.googleapis.com/v1/projects/{self.project}/locations/{self.region}/builds"

    def get_build(self, build_id: str) -> Build:
        return Build.from_api(self._get(f"{self._builds_url()}/{urllib.parse.quote(build_id)}"))

    def list_builds(self, build_filter: str, max_pages: int = 5) -> list[Build]:
        builds: list[Build] = []
        page_token = ""
        for _ in range(max_pages):
            query = {"filter": build_filter, "pageSize": "100"}
            if page_token:
                query["pageToken"] = page_token
            payload = self._get(f"{self._builds_url()}?{urllib.parse.urlencode(query)}")
            builds.extend(Build.from_api(item) for item in payload.get("builds", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                return builds
        raise GuardError(f"more than {max_pages} pages of builds match {build_filter!r}")

    def trigger_builds_around(self, own: Build, peers: tuple[str, ...] = ()) -> list[Build]:
        """Builds of the same trigger (and of the peer triggers) that are newer than `own` or still running."""
        names = _trigger_names_filter((own.trigger_name, *peers))
        newer = self.list_builds(f"{names} AND create_time>={_quote_filter_value(own.create_time)}")
        ongoing = self.list_builds(f'{names} AND (status="PENDING" OR status="QUEUED" OR status="WORKING")')
        return newer + ongoing

    def ongoing_tagged_builds(self, tag: str) -> list[Build]:
        """Running builds that carry a build tag (any trigger, or none)."""
        return self.list_builds(
            f'tags={_quote_filter_value(tag)} AND (status="PENDING" OR status="QUEUED" OR status="WORKING")'
        )

    def ongoing_rollbacks(self, tag: str) -> list[Build]:
        """Running rollback builds that change prod images (dry runs change nothing and are left out)."""
        return [build for build in self.ongoing_tagged_builds(tag) if not build.rollback_dry_run]

    def commit_builds(self, trigger_names: tuple[str, ...], commit: str) -> list[Build]:
        return self.list_builds(
            f"{_trigger_names_filter(trigger_names)} AND substitutions.COMMIT_SHA={_quote_filter_value(commit)}"
        )

    def tag_digest(self, location: str, repository: str, package: str, tag: str) -> str:
        url = (
            f"https://artifactregistry.googleapis.com/v1/projects/{self.project}/locations/{location}"
            f"/repositories/{repository}/packages/{urllib.parse.quote(package, safe='')}/tags/{tag}"
        )
        try:
            version = self._get(url).get("version", "")
        except GuardError as error:
            raise GuardError(f"could not read {package}:{tag}; did the dev build push it? {error}") from None
        digest = version.rsplit("/versions/", 1)[-1]
        if not DIGEST_PATTERN.match(digest):
            raise GuardError(f"{package}:{tag} resolves to an invalid digest {version!r}")
        return digest

    def require_version(self, location: str, repository: str, package: str, digest: str) -> None:
        """Fail unless the registry holds this digest of the package."""
        url = (
            f"https://artifactregistry.googleapis.com/v1/projects/{self.project}/locations/{location}"
            f"/repositories/{repository}/packages/{urllib.parse.quote(package, safe='')}/versions/{digest}"
        )
        try:
            self._get(url)
        except GuardError as error:
            raise GuardError(f"{package}@{digest} is not in the registry: {error}") from None


def github_compare_status(repo: str, base: str, head: str, token: str) -> str:
    """Return GitHub's compare status for base...head (identical, behind, ahead or diverged)."""
    url = f"https://api.github.com/repos/{repo}/compare/{urllib.parse.quote(base)}...{urllib.parse.quote(head)}?per_page=1"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return str(_http_json(request).get("status", ""))


@dataclass
class Guard:
    api: GoogleApi
    build_id: str
    commit: str
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_seconds: float = 15.0
    # Other triggers whose builds deploy the same environment (single flight counts them too).
    peer_triggers: tuple[str, ...] = ()
    # The build tag of the service's rollback builds; empty: do not wait for rollbacks (dev).
    rollback_tag: str = ""
    # The merge trigger. A build of it that promotes is replaced only by a newer one that
    # promotes too, so a manual dev deploy (_AUTO_PROMOTE=false) never drops a merged release.
    promoting_trigger: str = ""

    def _can_supersede(self, own: Build) -> Callable[[Build], bool] | None:
        def promotes(build: Build) -> bool:
            return build.trigger_name == self.promoting_trigger and build.auto_promote

        if not self.promoting_trigger or not promotes(own):
            return None
        return promotes

    def own_build(self) -> Build:
        own = self.api.get_build(self.build_id)
        if not own.trigger_name:
            raise GuardError(f"build {self.build_id} has no TRIGGER_NAME; run deploys only from the triggers")
        if own.commit != self.commit:
            raise GuardError(f"build {self.build_id} is for commit {own.commit!r}, not {self.commit!r}")
        return own

    def check_superseded(self, own: Build) -> list[Build]:
        """Raise Superseded when a newer build exists; return builds to wait for.

        Those are older builds of the same trigger that still run, and any
        running rollback build that is not a dry run: a rollback must finish
        before a deploy changes prod again.
        """
        decision, related = decide_turn(
            own, self.api.trigger_builds_around(own, self.peer_triggers), self._can_supersede(own)
        )
        if decision == "superseded":
            raise Superseded(f"newer {related[-1].trigger_name} build {related[-1].describe()} owns the release")
        if not self.rollback_tag:
            return related
        rollbacks = [build for build in self.api.ongoing_rollbacks(self.rollback_tag) if build.id != own.id]
        return related + rollbacks

    def wait_for_turn(self, own: Build, timeout_seconds: float) -> None:
        deadline = self.clock() + timeout_seconds
        announced: set[str] = set()
        while older := self.check_superseded(own):
            for build in older:
                if build.id not in announced:
                    print(f"Waiting for older build {build.describe()}", flush=True)
                    announced.add(build.id)
            if self.clock() >= deadline:
                ids = ", ".join(build.id for build in older)
                raise GuardError(f"older builds still running after {timeout_seconds:.0f} s: {ids}")
            self.sleep(self.poll_seconds)
        print(f"Single flight: no newer {own.trigger_name} build and no older one running", flush=True)

    def check_on_branch(self, repo: str, branch: str, token: str) -> None:
        if not token:
            raise GuardError(f"the GitHub read token is empty; the {branch} check cannot run")
        status = github_compare_status(repo, branch, self.commit, token)
        if status not in ON_BRANCH_COMPARE_STATUSES:
            raise GuardError(
                f"commit {self.commit} is not reachable from {repo}@{branch} (compare status {status!r}); "
                "only merged commits deploy to prod"
            )
        print(f"On {branch}: {self.commit} is reachable from {repo}@{branch} ({status})", flush=True)

    def wait_for_dev(self, own: Build, dev_triggers: tuple[str, ...], timeout_seconds: float, branch: str) -> Build:
        dev_trigger = dev_triggers[0]
        deadline = self.clock() + timeout_seconds
        last_seen = ""
        while True:
            self.check_superseded(own)
            decision, dev_build = decide_dev(self.api.commit_builds(dev_triggers, self.commit))
            if decision == "ok" and dev_build is not None:
                print(f"Dev first: {dev_trigger} build {dev_build.describe()} succeeded", flush=True)
                return dev_build
            if decision == "failed" and dev_build is not None:
                raise GuardError(
                    f"newest {dev_trigger} build of {self.commit[:9]} is {dev_build.describe()}; "
                    f"fix it on {branch} (the next merge deploys dev again) before prod"
                )
            if decision == "overlap" and dev_build is not None:
                raise GuardError(
                    f"{dev_trigger} build {dev_build.describe()} of the same commit ran after the newest one "
                    f"started, so the :{self.commit[:9]} tag may hold its image; run the {dev_trigger} trigger "
                    "again for this commit and let that build finish alone"
                )
            seen = dev_build.describe() if dev_build else "none yet"
            if seen != last_seen:
                print(f"Waiting for the {dev_trigger} build of {self.commit[:9]}: {seen}", flush=True)
                last_seen = seen
            if self.clock() >= deadline:
                raise GuardError(
                    f"no successful {dev_trigger} build of {self.commit[:9]} after {timeout_seconds:.0f} s "
                    f"(latest: {seen}); the {dev_trigger} build of this commit must succeed first"
                )
            self.sleep(self.poll_seconds)


def references(image: rc.Image, commit: str, digest: str) -> dict[str, str]:
    return {
        IMAGE_FILE: f"{image.repository}@{digest}",
        # Kubernetes ignores the tag when a digest is present; the tag keeps the commit visible.
        K8S_IMAGE_FILE: f"{image.repository}:{commit}@{digest}",
    }


def resolve_digest(api: GoogleApi, image: rc.Image, commit: str, dev_build: Build) -> tuple[str, str]:
    """Return (digest, source): the digest the dev build recorded, else the :<commit> tag."""
    digest = dev_build.recorded_digest()
    if digest:
        api.require_version(image.registry_location, image.registry_repository, image.package, digest)
        return digest, f"build {dev_build.id}"
    print(
        f"WARNING: dev build {dev_build.id} recorded no {RECORDED_DIGEST_KEY}; promoting what :{commit[:9]} points at",
        flush=True,
    )
    return api.tag_digest(image.registry_location, image.registry_repository, image.package, commit), "tag"


def write_references(out_dir: Path, values: dict[str, str]) -> None:
    for name, value in values.items():
        (out_dir / name).write_text(f"{value}\n", encoding="utf-8")
        print(f"{name}={value}", flush=True)


def read_references(out_dir: Path) -> dict[str, str]:
    names = (IMAGE_FILE, K8S_IMAGE_FILE, DIGEST_SOURCE_FILE)
    try:
        return {name: (out_dir / name).read_text(encoding="utf-8").strip() for name in names}
    except OSError as error:
        raise GuardError(f"preflight image references are missing: {error}") from None


def _guard(
    args: argparse.Namespace, service: rc.Service, api: GoogleApi, env: rc.Environment, *, rollbacks: bool
) -> Guard:
    return Guard(
        api=api,
        build_id=args.build_id,
        commit=args.commit,
        poll_seconds=args.poll_seconds,
        peer_triggers=env.deploy_triggers,
        rollback_tag=service.rollback_build_tag if rollbacks else "",
        promoting_trigger=env.deploy_trigger if env.name == "dev" else "",
    )


def run_preflight(args: argparse.Namespace, service: rc.Service, api: GoogleApi) -> None:
    if service.dev is None:
        raise GuardError(f"{service.name} has no dev stage; preflight is for full-tier prod builds")
    guard = _guard(args, service, api, service.prod, rollbacks=True)
    own = guard.own_build()
    print(f"Prod promote {own.describe()}", flush=True)
    guard.wait_for_turn(own, args.older_timeout_seconds)
    token = api.repository_read_token(service.connection, service.connected_repository)
    guard.check_on_branch(service.github_repo, service.release_branch, token)
    dev_build = guard.wait_for_dev(own, service.dev.deploy_triggers, args.dev_timeout_seconds, service.release_branch)
    digest, source = resolve_digest(api, service.image, args.commit, dev_build)
    write_references(args.out_dir, {**references(service.image, args.commit, digest), DIGEST_SOURCE_FILE: source})


def run_still_current(args: argparse.Namespace, service: rc.Service, api: GoogleApi) -> None:
    guard = _guard(args, service, api, service.prod, rollbacks=True)
    own = guard.own_build()
    expected = read_references(args.out_dir)
    if older := guard.check_superseded(own):
        raise GuardError(f"an older prod build or a rollback started running after preflight: {older[0].describe()}")
    digest = expected[IMAGE_FILE].rsplit("@", 1)[-1]
    if expected != {**references(service.image, args.commit, digest), DIGEST_SOURCE_FILE: expected[DIGEST_SOURCE_FILE]}:
        raise GuardError(f"preflight image references do not match {service.image.package}@{digest}: {expected}")
    image = service.image
    if expected[DIGEST_SOURCE_FILE] == "tag":
        current = api.tag_digest(image.registry_location, image.registry_repository, image.package, args.commit)
        if current != digest:
            raise GuardError(
                f"the :<commit> tag moved after preflight (a dev build of this commit ran again?): {digest} -> {current}"
            )
    else:
        api.require_version(image.registry_location, image.registry_repository, image.package, digest)
    print(
        f"Still current: {own.id} is the newest {own.trigger_name} build and promotes {image.package}@{digest}",
        flush=True,
    )


def run_turn(args: argparse.Namespace, service: rc.Service, api: GoogleApi) -> None:
    """Single flight of a deploying build: wait for older builds that still run; a newer one supersedes it.

    A prod build (a service without a dev stage) also waits for a running rollback.
    """
    env = service.environments[args.env]
    guard = _guard(args, service, api, env, rollbacks=env.name == "prod")
    own = guard.own_build()
    print(f"{env.name} deploy {own.describe()}", flush=True)
    if env.name == "prod":
        # Only merged commits deploy to prod: a manual run of the prod trigger may name any branch.
        token = api.repository_read_token(service.connection, service.connected_repository)
        guard.check_on_branch(service.github_repo, service.release_branch, token)
    guard.wait_for_turn(own, args.older_timeout_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--build-id", required=True)
        sub.add_argument("--commit", required=True)
        sub.add_argument("--out-dir", type=Path, default=Path("/workspace"))
        sub.add_argument("--poll-seconds", type=float, default=15.0)

    preflight = commands.add_parser("preflight", help="single flight, on-branch and dev-first guards, then digest")
    common(preflight)
    preflight.add_argument("--older-timeout-seconds", type=float, default=3600)
    preflight.add_argument("--dev-timeout-seconds", type=float, default=1200)

    still_current = commands.add_parser("still-current", help="recheck single flight and digest before apply")
    common(still_current)

    turn = commands.add_parser("turn", help="single flight of a deploying build")
    common(turn)
    turn.add_argument("--env", default="dev", choices=["dev", "prod"])
    turn.add_argument("--older-timeout-seconds", type=float, default=3600)

    args = parser.parse_args(argv)
    if not COMMIT_PATTERN.match(args.commit):
        print(f"BLOCKED: --commit must be a full 40-character SHA, got {args.commit!r}", file=sys.stderr)
        return EXIT_BLOCKED
    try:
        service = rc.load_service(args.service)
        api = GoogleApi(service.project, service.region)
        handlers = {"preflight": run_preflight, "still-current": run_still_current, "turn": run_turn}
        handlers[args.command](args, service, api)
    except Superseded as reason:
        print(f"SUPERSEDED: {reason}; this build deploys nothing", flush=True)
        return EXIT_SUPERSEDED
    except (GuardError, rc.ConfigError, subprocess.SubprocessError, KeyError) as reason:
        print(f"BLOCKED: {reason}", file=sys.stderr, flush=True)
        return EXIT_BLOCKED
    return EXIT_PROCEED


if __name__ == "__main__":
    sys.exit(main())
