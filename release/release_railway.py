#!/usr/bin/env python3
"""Railway services in the release kit: kind "railway", tiers full and check_rollback.

Railway builds and runs the service. Its GitHub auto deploy is off (a human
setting), so the release builds are the only thing that deploys:

- Tier full: the dev build of a merge deploys the commit to the Railway
  environment that is the dev stage (staging) through the public GraphQL API
  (serviceInstanceDeployV2), and verifies it there. The prod build deploys the
  same commit to production. Railway builds again for production: the same
  source, not the same bytes (the record keeps both image digests).
- Tier check_rollback: no dev stage (a service whose staging environment does
  not exist yet). The prod build of a merge deploys the commit to production
  after the freeze and hold gate.

The checks read Railway itself: the deployment status, the deployment logs
(error lines) and the HTTP logs (the 5xx share) of the release's deployment,
plus the smoke checks on the environment's URL. A failed release redeploys the
deployment that served before (deploymentRedeploy with the previous image, no
rebuild) and holds. A release that adds a database migration never rolls back
by itself (the old binary does not start against a newer schema): it holds and
pings, and a forward fix with `Fixes-release: <release id>` ends the hold.

This module holds the Railway parts that release_light.py uses: the GraphQL
API, the project tokens, the log reads, the migration rule and the rendered
build files. Each environment has its own project token (scoped to that one
environment) in Secret Manager. In Cloud Build it comes from the environment
(RAILWAY_TOKEN_DEV, RAILWAY_TOKEN_PROD); elsewhere it is read from Secret
Manager with the gcloud identity. It is never printed and never on a command
line, and every mutation first checks that the token belongs to the
environment it is about to change.

Part of the release kit (orbit-infrastructure/release-kit). Stdlib only (Python 3.10).
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_record as rr  # noqa: E402
import release_signals as rs  # noqa: E402
import release_vercel as rvc  # noqa: E402

API = "https://backboard.railway.com/graphql/v2"
# A project token is scoped to one environment of one project; it goes in this header.
TOKEN_HEADER = "Project-Access-Token"
TOKEN_ENVS = {"dev": "RAILWAY_TOKEN_DEV", "prod": "RAILWAY_TOKEN_PROD"}
USER_AGENT = "orbit-release-kit"
BUILDING = frozenset({"QUEUED", "WAITING", "INITIALIZING", "BUILDING", "DEPLOYING"})
SERVING = frozenset({"SUCCESS", "SLEEPING"})
SUCCESS = "SUCCESS"
DEPLOYMENT_FIELDS = "id status environmentId createdAt statusUpdatedAt canRedeploy canRollback staticUrl url meta"
# deploymentLogs returns the newest `limit` lines before endDate; one read takes LOG_PAGE lines, at most
# LOG_MAX_PAGES pages back. httpLogs pages forward from an anchor time (afterLimit rows a page).
LOG_PAGE = 2000
LOG_MAX_PAGES = 10
HTTP_PAGE = 5000
HTTP_MAX_PAGES = 12
# Deployments one list read returns; the baseline looks for the ones that served in the 24 h before.
DEPLOYMENT_LIST = 30
DEPLOYMENT_PAGES = 10


class RailwayError(rr.RecordError):
    """A Railway call failed, or a deployment is not what the release needs."""


class RailwayUncertain(RailwayError):
    """A mutation got no answer (a timeout, a dropped connection, a 5xx): Railway may have run it."""


# After a deploy or redeploy whose outcome is unknown, the kit looks this many times (RECONCILE_SECONDS apart) for
# the deployment it may have started, instead of sending the mutation again.
RECONCILE_TRIES = 4
RECONCILE_SECONDS = 10


Http = Callable[[urllib.request.Request], tuple[int, bytes]]


def iso(when: datetime) -> str:
    """A UTC time as Railway takes it: 2026-10-23T22:10:00.000Z."""
    return when.astimezone(rr.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def parse_time(value: str) -> datetime:
    """A Railway time (nanoseconds, Z) as an aware datetime (microseconds)."""
    text = str(value).strip().replace("Z", "+00:00")
    head, _, rest = text.partition(".")
    if rest:
        digits = rest[: len(rest) - len(rest.lstrip("0123456789"))]
        zone = rest[len(digits) :]
        text = f"{head}.{(digits + '000000')[:6]}{zone}"
    return datetime.fromisoformat(text).astimezone(rr.UTC)


# --------------------------------------------------------------------------- the tokens


def token_source(
    env: str, project: str, secret: str, google_token: Callable[[], str], http: Http = rvc.http_request
) -> Callable[[], str]:
    """A function that returns an environment's project token: RAILWAY_TOKEN_<ENV>, else the Secret Manager secret."""
    cache: list[str] = []

    def token() -> str:
        """The token, read on first use."""
        if not cache:
            cache.append(
                os.environ.get(TOKEN_ENVS[env], "").strip() or rvc.secret_token(project, secret, google_token, http)
            )
        return cache[0]

    return token


# --------------------------------------------------------------------------- the API


class RailwayApi:
    """Railway's public GraphQL API with one environment's project token; 429 and 5xx are tried again."""

    def __init__(
        self,
        token: Callable[[], str],
        *,
        http: Http = rvc.http_request,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 4,
    ) -> None:
        """Keep the token function and the transport."""
        self.token = token
        self.http = http
        self.sleep = sleep
        self.attempts = attempts

    def call(self, query: str, variables: dict | None = None, *, resend: bool = False) -> dict:
        """Run one query or mutation and return its data; raise RailwayError on an HTTP or GraphQL error."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        name = query.split("(", 1)[0].split("{", 1)[0].strip() or "query"
        # A mutation is sent once: after a timeout or a 5xx Railway may have run it, and a second send can start a
        # second deployment. Only a 429 (refused before it ran) is sent again. Reads are tried again on all three.
        # resend: a mutation that is safe to send twice (a cancel) keeps the read retries.
        mutation = query.lstrip().startswith("mutation") and not resend
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                API,
                data=body,
                method="POST",
                headers={TOKEN_HEADER: self.token(), "Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                status, raw = self.http(request)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
                # HTTPException: a reply cut off mid-body (IncompleteRead) after Railway may have run it.
                if mutation:
                    raise RailwayUncertain(f"Railway {name} got no answer ({error}); it may have run") from None
                if attempt == self.attempts:
                    raise RailwayError(f"Railway {name} failed: {error}") from None
                self.sleep(2**attempt)
                continue
            if mutation and status >= 500:
                raise RailwayUncertain(f"Railway {name} returned HTTP {status}; it may have run")
            if (status == 429 or status >= 500) and attempt < self.attempts:
                self.sleep(2**attempt)
                continue
            if status >= 400:
                raise RailwayError(f"Railway {name} returned HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
            try:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            except ValueError:
                raise RailwayError(f"Railway {name} returned no JSON: {raw[:200]!r}") from None
            if payload.get("errors"):
                messages = "; ".join(str(item.get("message") or item)[:200] for item in payload["errors"][:3])
                raise RailwayError(f"Railway {name}: {messages}")
            return payload.get("data") or {}
        raise AssertionError("unreachable")

    def token_scope(self) -> dict:
        """The project and environment the token belongs to: {projectId, environmentId}."""
        return dict(
            self.call("query projectToken { projectToken { projectId environmentId } }").get("projectToken") or {}
        )

    def known_ids(self, service_id: str, environment_id: str) -> set[str]:
        """The ids of the service's deployments Railway lists in the environment now (newest page)."""
        listed, _ = self.deployments(service_id, environment_id)
        return {str(item.get("id")) for item in listed}

    def reconcile(
        self,
        service_id: str,
        environment_id: str,
        before: set[str],
        match: Callable[[dict], bool],
        what: str,
        error: RailwayError,
    ) -> str:
        """After a mutation with an unknown outcome: the id of the one new deployment that matches, else raise."""
        unread = ""
        for attempt in range(RECONCILE_TRIES):
            try:
                listed, _ = self.deployments(service_id, environment_id)
            except RailwayError as read_error:
                # A failed read uses up one attempt, not the reconcile: the next read may answer.
                unread = f"; the last read failed: {str(read_error)[:160]}"
                if attempt < RECONCILE_TRIES - 1:
                    self.sleep(RECONCILE_SECONDS)
                continue
            unread = ""
            new = [d for d in listed if str(d.get("id")) not in before and match(d)]
            if len(new) == 1:
                print(f"Railway {what}: {error}; it did start deployment {new[0].get('id')}", flush=True)
                return str(new[0].get("id"))
            if len(new) > 1:
                ids = ", ".join(str(d.get("id")) for d in new)
                raise RailwayError(f"Railway {what}: {error}; several new deployments ({ids}); a human decides")
            if attempt < RECONCILE_TRIES - 1:
                self.sleep(RECONCILE_SECONDS)
        raise RailwayError(f"Railway {what}: {error}; Railway lists no new deployment{unread}; nothing was sent again")

    def deploy(self, service_id: str, environment_id: str, commit: str) -> str:
        """Build and deploy one commit of the service's GitHub repository to an environment; the deployment id.

        Sent once. When the answer is lost, the new deployment of the commit that Railway lists is the result.
        """
        before = self.known_ids(service_id, environment_id)
        try:
            data = self.call(
                "mutation deploy($s: String!, $e: String!, $c: String!) "
                "{ serviceInstanceDeployV2(serviceId: $s, environmentId: $e, commitSha: $c) }",
                {"s": service_id, "e": environment_id, "c": commit},
            )
        except RailwayUncertain as error:
            return self.reconcile(
                service_id, environment_id, before, lambda d: commit_of(d) == commit, f"deploy of {commit[:9]}", error
            )
        found = str(data.get("serviceInstanceDeployV2") or "")
        if not found:
            raise RailwayError(f"Railway started no deployment of {commit[:9]}")
        return found

    def deployment(self, deployment_id: str) -> dict:
        """One deployment: id, status, createdAt, canRedeploy, meta (commitHash, imageDigest, reason)."""
        data = self.call(
            f"query deployment($id: String!) {{ deployment(id: $id) {{ {DEPLOYMENT_FIELDS} }} }}", {"id": deployment_id}
        )
        found = data.get("deployment")
        if not found:
            raise RailwayError(f"Railway has no deployment {deployment_id}")
        return dict(found)

    def deployments(
        self,
        service_id: str,
        environment_id: str,
        *,
        since: datetime | None = None,
        first: int = DEPLOYMENT_LIST,
        max_pages: int = DEPLOYMENT_PAGES,
    ) -> tuple[list[dict], bool]:
        """(the deployments of the service in the environment, newest first, complete).

        With since: pages on until a deployment created before it is listed (it may have served then);
        complete is False when the pages ran out first. Without since: one page, complete.
        """
        nodes: list[dict] = []
        after = None
        for _ in range(max_pages):
            data = self.call(
                "query deployments($s: String!, $e: String!, $n: Int!, $a: String) { deployments(first: $n, "
                "after: $a, input: {serviceId: $s, environmentId: $e}) "
                f"{{ edges {{ node {{ {DEPLOYMENT_FIELDS} }} }} pageInfo {{ hasNextPage endCursor }} }} }}",
                {"s": service_id, "e": environment_id, "n": first, "a": after},
            )
            connection = data.get("deployments") or {}
            nodes += [dict(edge.get("node") or {}) for edge in connection.get("edges") or []]
            info = connection.get("pageInfo") or {}
            older = since is not None and any((created_of(item) or since) < since for item in nodes)
            if since is None or older or not info.get("hasNextPage") or not info.get("endCursor"):
                return sorted(nodes, key=lambda item: str(item.get("createdAt") or ""), reverse=True), True
            after = info["endCursor"]
        return sorted(nodes, key=lambda item: str(item.get("createdAt") or ""), reverse=True), False

    def building(self, service_id: str, environment_id: str) -> list[dict]:
        """The deployments of the service in the environment that still build or deploy (newest page)."""
        listed, _ = self.deployments(service_id, environment_id)
        return [item for item in listed if str(item.get("status")) in BUILDING]

    def cancel(self, deployment_id: str) -> None:
        """Cancel a deployment that still builds or deploys (the kit's own, after its wait ran out)."""
        # Sent again after a lost answer: cancelling twice is harmless, and a lost cancel must not leave it running.
        self.call("mutation cancel($id: String!) { deploymentCancel(id: $id) }", {"id": deployment_id}, resend=True)

    def active(self, service_id: str, environment_id: str) -> list[dict]:
        """The deployments that serve the environment now (Railway's activeDeployments)."""
        data = self.call(
            "query active($s: String!, $e: String!) { serviceInstance(serviceId: $s, environmentId: $e) "
            f"{{ activeDeployments {{ {DEPLOYMENT_FIELDS} }} }} }}",
            {"s": service_id, "e": environment_id},
        )
        return [dict(item) for item in (data.get("serviceInstance") or {}).get("activeDeployments") or []]

    def auto_deploy(self, project_id: str, service_id: str, environment_id: str) -> dict:
        """Whether Railway deploys pushes to the connected branch by itself: {enabled, canEnable, reason}."""
        data = self.call(
            "query auto($p: String!, $s: String!, $e: String!) { serviceInstanceAutoDeployStatus("
            "projectId: $p, serviceId: $s, environmentId: $e) { enabled canEnable reason } }",
            {"p": project_id, "s": service_id, "e": environment_id},
        )
        return dict(data.get("serviceInstanceAutoDeployStatus") or {})

    def redeploy(self, deployment_id: str, service_id: str = "", environment_id: str = "") -> dict:
        """A new deployment from an earlier one with its image (no rebuild) and the current variables.

        Sent once. With the service and environment, a lost answer is reconciled: the one new deployment that runs
        the earlier one's image is the result (a rebuild of the same commit has another image and is not adopted).
        A target without a known image is never reconciled: nothing else proves which deployment is the rollback.
        """
        before = self.known_ids(service_id, environment_id) if service_id and environment_id else None
        # Read before the mutation: after a lost answer the reconcile must not depend on another read of it.
        image = image_of(self.deployment(deployment_id)) if before is not None else ""
        try:
            data = self.call(
                "mutation redeploy($id: String!) { deploymentRedeploy(id: $id, usePreviousImageTag: true) "
                f"{{ {DEPLOYMENT_FIELDS} }} }}",
                {"id": deployment_id},
            )
        except RailwayUncertain as error:
            if before is None or not image:
                raise RailwayError(f"Railway redeploy of {deployment_id}: {error}; nothing was sent again") from None
            found = self.reconcile(
                service_id,
                environment_id,
                before,
                lambda d: image_of(d) == image,
                f"redeploy of {deployment_id}",
                error,
            )
            try:
                return self.deployment(found)
            except RailwayError:
                # The id is enough: the caller waits for it, or cancels it.
                return {"id": found}
        found = data.get("deploymentRedeploy")
        if not found:
            raise RailwayError(f"Railway did not redeploy {deployment_id}")
        return dict(found)

    def logs(
        self,
        deployment_id: str,
        start: datetime,
        end: datetime,
        *,
        filter_text: str = "",
        max_pages: int = LOG_MAX_PAGES,
    ) -> tuple[list[dict], bool]:
        """(log lines of a deployment in [start, end), oldest first, complete); complete is False at max_pages."""
        rows: dict[tuple[str, str, str], dict] = {}
        # The next page ends at the oldest row's own timestamp (full precision, inclusive), so no row between
        # two pages is skipped; rows seen twice at that boundary count once.
        before, before_text = end, iso(end)
        for _ in range(max_pages):
            data = self.call(
                "query logs($d: String!, $s: DateTime!, $e: DateTime!, $f: String, $n: Int!) { deploymentLogs("
                "deploymentId: $d, startDate: $s, endDate: $e, filter: $f, limit: $n) "
                "{ timestamp severity message } }",
                {"d": deployment_id, "s": iso(start), "e": before_text, "f": filter_text or None, "n": LOG_PAGE},
            )
            answer = list(data.get("deploymentLogs") or [])
            page = [row for row in answer if start <= parse_time(row["timestamp"]) <= before]
            fresh = 0
            for row in page:
                key = (str(row["timestamp"]), str(row.get("severity")), str(row.get("message")))
                if key not in rows and parse_time(row["timestamp"]) < end:
                    rows[key] = row
                    fresh += 1
            ordered = sorted(rows.values(), key=lambda row: parse_time(row["timestamp"]))
            if len(answer) < LOG_PAGE:
                return ordered, True
            if not page or not fresh:
                return ordered, False  # a full page of rows at one instant: more may hide behind it
            oldest = min(page, key=lambda row: parse_time(row["timestamp"]))
            before, before_text = parse_time(oldest["timestamp"]), str(oldest["timestamp"])
        return sorted(rows.values(), key=lambda row: parse_time(row["timestamp"])), False

    def http_logs(
        self, deployment_id: str, start: datetime, end: datetime, *, max_pages: int = HTTP_MAX_PAGES
    ) -> tuple[list[dict], bool]:
        """(HTTP log rows of a deployment in [start, end), complete); complete is False when max_pages ran out."""
        rows: dict[str, dict] = {}
        anchor = start
        for _ in range(max_pages):
            data = self.call(
                "query http($d: String!, $a: String!, $n: Int!) { httpLogs(deploymentId: $d, anchorDate: $a, "
                "afterLimit: $n) { requestId timestamp httpStatus path method totalDuration upstreamErrors } }",
                {"d": deployment_id, "a": iso(anchor), "n": HTTP_PAGE},
            )
            page = list(data.get("httpLogs") or [])
            newest = anchor
            for row in page:
                when = parse_time(row["timestamp"])
                newest = max(newest, when)
                if start <= when < end:
                    rows[str(row.get("requestId") or f"{row['timestamp']}{row.get('path')}")] = row
            if len(page) < HTTP_PAGE or newest >= end:
                return list(rows.values()), True
            if newest <= anchor:
                return list(rows.values()), False
            anchor = newest
        return list(rows.values()), False


# --------------------------------------------------------------------------- deployments


def commit_of(deployment: dict) -> str:
    """The Git commit a deployment was built from (empty for a CLI upload)."""
    return str((deployment.get("meta") or {}).get("commitHash") or "")


def image_of(deployment: dict) -> str:
    """The image digest Railway built for a deployment."""
    return str((deployment.get("meta") or {}).get("imageDigest") or "")


def created_of(deployment: dict) -> datetime | None:
    """When the deployment was created."""
    value = deployment.get("createdAt")
    return parse_time(value) if value else None


def describe(deployment: dict) -> str:
    """One line: id, status, commit and image."""
    image = image_of(deployment)
    return f"{deployment.get('id')} {deployment.get('status')} commit {commit_of(deployment)[:9] or 'none'}" + (
        f" image {image[:19]}" if image else ""
    )


def wait_for_deployment(
    api: RailwayApi,
    deployment_id: str,
    timeout_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = 15,
) -> dict:
    """Wait until the deployment leaves the build and deploy states; return it as it is at the end."""
    deadline = clock() + timeout_seconds
    while True:
        deployment = api.deployment(deployment_id)
        status = str(deployment.get("status") or "")
        if status not in BUILDING or clock() >= deadline:
            return deployment
        print(f"Waiting for Railway deployment {deployment_id} ({status})", flush=True)
        sleep(poll_seconds)


def serving(api: RailwayApi, service_id: str, environment_id: str) -> dict | None:
    """The deployment that serves the environment now (the newest active one), else None."""
    active = [item for item in api.active(service_id, environment_id) if str(item.get("status")) in SERVING]
    if not active:
        return None
    return max(active, key=lambda item: str(item.get("createdAt") or ""))


def check_token_scope(api: RailwayApi, project_id: str, environment_id: str, label: str) -> None:
    """Refuse a token that belongs to another project or environment (a prod token in the dev slot, or the reverse)."""
    scope = api.token_scope()
    if scope.get("projectId") != project_id or scope.get("environmentId") != environment_id:
        raise RailwayError(
            f"the Railway token for {label} belongs to project {scope.get('projectId')} environment "
            f"{scope.get('environmentId')}, not {project_id} {environment_id}; nothing changed"
        )


def baseline_deployments(
    deployments: Iterable[dict], start: datetime, end: datetime, exclude: Iterable[str]
) -> list[str]:
    """The deployments that may have served in [start, end): created before end, removed (or not yet) after start."""
    skip = set(exclude)
    out = []
    for item in deployments:
        if str(item.get("id")) in skip or str(item.get("status")) in BUILDING:
            continue
        created = created_of(item)
        if created is None or created >= end:
            continue
        status = str(item.get("status") or "")
        ended = parse_time(item["statusUpdatedAt"]) if item.get("statusUpdatedAt") and status not in SERVING else None
        if ended is not None and ended < start:
            continue
        out.append(str(item["id"]))
    return out


# --------------------------------------------------------------------------- logs and the checks


def log_lines(rows: Iterable[dict], deployment: str) -> list[tuple[str, str, str, str, str]]:
    """Railway log lines as (time, deployment, source, severity, text) for release_signals."""
    levels = {"error": "ERROR", "err": "ERROR", "fatal": "CRITICAL", "warn": "WARNING", "warning": "WARNING"}
    out = []
    for row in rows:
        level = levels.get(str(row.get("severity") or "").lower())
        message = str(row.get("message") or "").strip()
        if level and message:
            stamp = parse_time(row["timestamp"]).strftime("%Y-%m-%dT%H:%M:%S")
            out.append((stamp, deployment, "railway", level, message))
    return out


def new_error_problems(
    api: RailwayApi,
    window: Iterable[str],
    baseline: list[tuple[list[str], datetime, datetime]],
    scope: rs.Scope,
    now: datetime,
    *,
    min_count: int,
) -> tuple[list[rs.Problem], list[str], bool]:
    """(new error log sites of the release's deployments, notes, judged).

    window: the release deployments read since the window start. baseline: (deployments, start, end) spans
    of the 24 h before. A site is new when no deployment wrote it in the baseline. judged is False when a
    baseline read stopped at its page limit (old lines would look new).
    """
    notes: list[str] = []
    window_lines = []
    for deployment in window:
        rows, complete = api.logs(deployment, scope.window[0], now, filter_text="@level:error")
        window_lines += log_lines(rows, deployment)
        if not complete:
            notes.append(f"the window read of {deployment} stopped at {LOG_MAX_PAGES} pages (the newest)")
    counted = rs.count_sites(rs.count_patterns(window_lines))
    known_lines = []
    for deployments, start, end in baseline:
        for deployment in deployments:
            rows, complete = api.logs(deployment, start, end, filter_text="@level:error")
            known_lines += log_lines(rows, deployment)
            if not complete:
                notes.append(f"the baseline read of {deployment} stopped at {LOG_MAX_PAGES} pages; not judged")
                return [], notes, False
    known = set(rs.count_sites(rs.count_patterns(known_lines)))
    recent = (now - timedelta(minutes=rs.LOG_RECENT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")
    problems = []
    for site, item in sorted(counted.items(), key=lambda pair: -pair[1].count):
        if site in known or item.count < min_count or item.last < recent or not rs.is_error(item.pattern):
            continue
        text = site.split(":", 2)[2]
        problems.append(
            rs.Problem(
                kind="log",
                key=f"log:{site}",
                text=f"new log line, {item.count} in the window, 0 in the baseline: {item.example[:220]}",
                count=item.count,
                literals=[text, *[part for part in rs.literal_fragments(item.pattern) if part != text]],
                identifiers=sorted(item.identifiers),
            )
        )
    return problems, notes, True


def request_counts(api: RailwayApi, deployment: str, start: datetime, end: datetime) -> rvc.Requests:
    """All requests and the 5xx ones of a deployment in [start, end), from one read of its HTTP logs."""
    rows, complete = api.http_logs(deployment, start, end)
    counts = rvc.Requests(deployment=deployment, total=len(rows), complete=complete, errors_complete=complete)
    for row in rows:
        if int(row.get("httpStatus") or 0) >= 500:
            counts.errors += 1
            path = str(row.get("path") or "?")
            counts.paths[path] = counts.paths.get(path, 0) + 1
    return counts


# --------------------------------------------------------------------------- database migrations


def migration_files(files: Iterable[dict], prefix: str | tuple[str, ...], suffix: str) -> list[str]:
    """The changed files (GitHub compare entries) that add or change a database migration or a store format.

    prefix: one path prefix or several (a folder such as backend/migrations/, or a file such as src/store.js).
    """
    out = []
    for item in files:
        path = str(item.get("filename") or "")
        if str(item.get("status") or "") == "removed":
            continue
        if path.startswith(prefix) and path.endswith(suffix):
            out.append(path)
    return sorted(out)


def migrations_between(
    repo: str, base: str | None, head: str | None, prefix: str | tuple[str, ...], suffix: str, token: str | None
) -> dict:
    """{"migrations": [...]} of base..head, or {"unknown": why} when the changed files cannot be read.

    An unknown range counts as one with a migration: a release that may have changed the schema never
    rolls back on its own.
    """
    if not base or not head:
        return {"unknown": "the previous commit is unknown"}
    if base == head:
        return {"migrations": []}
    if not token:
        return {"unknown": "no GitHub token"}
    try:
        payload = rr.github_json(f"repos/{repo}/compare/{base}...{head}?per_page=300", token)
    except Exception as error:  # noqa: BLE001 - GuardError, HTTP and network errors all mean unknown
        return {"unknown": f"the compare {base[:9]}...{head[:9]} failed: {str(error)[:160]}"}
    files = payload.get("files") or []
    if len(files) >= 300:
        return {"unknown": f"{len(files)} changed files (GitHub lists at most 300)"}
    return {"migrations": migration_files(files, prefix, suffix)}


def forward_only(state: dict | None) -> bool:
    """Whether a release's migration state forbids an automatic rollback: a migration, or an unknown range."""
    return bool(state) and (bool(state.get("migrations")) or "unknown" in state)


# --------------------------------------------------------------------------- rendered builds

SECRETS_HEAD = """\
availableSecrets:
    secretManager:
        - versionName: projects/$PROJECT_ID/secrets/slack-webhook-workspace-orbit/versions/latest
          env: "WEBHOOK_ORBIT"
"""
SECRET_LINE = """\
        # Railway project token of one environment; release_railway.py reads it from the environment, never prints it.
        - versionName: projects/$PROJECT_ID/secrets/{secret}/versions/latest
          env: "{env}"
"""

DEV_TEMPLATE = """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Dev stage of {service} (kind railway, tier full), the first half of a merge
# deploy ({docs}). The push trigger {dev_trigger} runs it for every merge to
# {branch}. Railway's own GitHub auto deploy is off: this build deploys the
# commit to the Railway environment {dev_environment} (serviceInstanceDeployV2),
# verifies it there (dev verify: the deployment is SUCCESS and serves, the smoke
# checks on {dev_url}, no error log line and no 5xx while they run), and starts
# the prod build {prod_trigger} for the same commit when the gate allows
# (release/release_gate.py request-promote). It changes nothing in production.
substitutions:
    # "false": verify only, start no prod build (manual runs).
    _AUTO_PROMOTE: "true"
    # "true": print the prod trigger command instead of running it.
    _PROMOTE_DRY_RUN: "false"
    # Test runs: a non-empty text makes dev verify fail with it, to prove that a failure stops the promote.
    _DEV_VERIFY_FORCE_FAIL: ""

steps:
    # Single flight of the dev builds: a newer one supersedes this one (exit 3); an older one is waited for.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "dev-turn"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              python3 release/release_light.py --service {service} turn --env dev --build-id "$BUILD_ID" --commit "$COMMIT_SHA"
              TURN_STATUS=$$?
              if [ "$${{TURN_STATUS}}" -eq 3 ]; then touch /workspace/superseded; exit 0; fi
              if [ "$${{TURN_STATUS}}" -ne 0 ]; then touch /workspace/deploy_failed; fi
              exit 0
      timeout: "2400s"

    # Deploy the commit to {dev_environment}; wait until Railway's deployment is SUCCESS, FAILED or CRASHED.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "railway-deploy-dev"
      entrypoint: "bash"
      secretEnv: ["RAILWAY_TOKEN_DEV"]
      args:
          - "-c"
          - |
              if [ -f /workspace/superseded ] || [ -f /workspace/deploy_failed ]; then exit 0; fi
              python3 release/release_light.py --service {service} railway-deploy --env dev --commit "$COMMIT_SHA" \\
                --out-dir /workspace || touch /workspace/deploy_failed
              exit 0
      timeout: "{wait_seconds}s"

    # Dev release record (gs://orbit-search-release-records/{service}/dev/) with the staging deployment.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-record-begin"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              if [ -f /workspace/superseded ] || [ -f /workspace/deploy_failed ]; then exit 0; fi
              python3 release/release_light.py --service {service} begin --env dev --commit "$COMMIT_SHA" \\
                --build-id "$BUILD_ID" --images-file /workspace/railway_deployment.json \\
                --release-id-file /workspace/release_id \\
                || {{ echo "ERROR: dev release record not written; no verification or promotion" >&2; touch /workspace/release_record_failed /workspace/release_verify_failed; rm -f /workspace/release_id; }}

    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-record-finish"
      entrypoint: "bash"
      secretEnv: ["RAILWAY_TOKEN_DEV"]
      args:
          - "-c"
          - |
              if [ -f /workspace/superseded ] || [ ! -s /workspace/release_id ]; then exit 0; fi
              python3 release/release_light.py --service {service} finish --env dev \\
                --release-id-file /workspace/release_id --status deployed \\
                || {{ echo "ERROR: dev release record not finished; promotion stopped" >&2; touch /workspace/release_record_failed /workspace/release_verify_failed; }}

    # Dev verify (release_light.py check --env dev). The verdict goes into the dev record and its
    # commit index; a failure posts the author mention and a fix prompt.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "dev-verify"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT", "RAILWAY_TOKEN_DEV"]
      args:
          - "-c"
          - |
              if [ -f /workspace/superseded ] || [ -f /workspace/deploy_failed ]; then exit 0; fi
              if [ -f /workspace/release_record_failed ]; then exit 0; fi
              python3 release/release_light.py --service {service} check --env dev \\
                --release-id-file /workspace/release_id \\
                --force-fail "$${{DEV_VERIFY_FORCE_FAIL}}" \\
                --build-url "https://console.cloud.google.com/cloud-build/builds;region={region}/$BUILD_ID?project=$PROJECT_ID" \\
                --build-id "$BUILD_ID" \\
                --failed-marker /workspace/release_verify_failed
              VERIFY_STATUS=$$?
              if [ "$${{VERIFY_STATUS}}" -ne 0 ] && [ "$${{VERIFY_STATUS}}" -ne 2 ]; then touch /workspace/release_verify_failed; fi
              exit 0
      env:
          # Free text goes through the environment, never into the script text.
          - "DEV_VERIFY_FORCE_FAIL=${{_DEV_VERIFY_FORCE_FAIL}}"
      timeout: "1200s"

    # When dev verify passed: start the prod build for this commit unless the gate says freeze, hold
    # after a rollback, or a newer verified commit. It never fails this build.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "promote-request"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT"]
      args:
          - "-c"
          - |
              if [ -f /workspace/superseded ] || [ -f /workspace/deploy_failed ] || [ -f /workspace/release_verify_failed ]; then exit 0; fi
              DRY_RUN_FLAG=""
              if [ "${{_PROMOTE_DRY_RUN}}" = "true" ]; then DRY_RUN_FLAG="--dry-run"; fi
              python3 release/release_gate.py --service {service} request-promote \\
                --commit "$COMMIT_SHA" --build-id "$BUILD_ID" --trigger-name "$TRIGGER_NAME" \\
                --auto-promote "${{_AUTO_PROMOTE}}" $${{DRY_RUN_FLAG}}
              exit 0

    - name: "gcr.io/cloud-builders/gcloud"
      id: "dev-result"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT"]
      args:
          - "-c"
          - |
              TEXT=""
              if [ -f /workspace/superseded ]; then echo "superseded by a newer dev build"; exit 0; fi
              if [ -f /workspace/deploy_failed ]; then
                TEXT=":x: {title} Railway {dev_environment} deploy of $SHORT_SHA failed; nothing is released (see the railway-deploy-dev step)"
              elif [ -f /workspace/release_record_failed ]; then
                TEXT=":x: {title} dev release record failed for $SHORT_SHA; verification and promotion stopped"
              fi
              if [ -n "$${{TEXT}}" ]; then
                # Slack gets the status line; the reason stays in this build log.
                curl -s -X POST -H 'Content-type: application/json' --data '{{"text":"{line_on_hold}"}}' "$$WEBHOOK_ORBIT" || true
                echo "$${{TEXT}}" >&2
                exit 1
              fi
              if [ -f /workspace/release_verify_failed ]; then
                echo "dev verify failed; see the dev-verify step and the dev release record" >&2
                exit 1
              fi

# Up to 40 minutes for an older dev build, {wait_minutes} for Railway's build and deploy, then dev verify.
timeout: "{build_seconds}s"
options:
    logging: CLOUD_LOGGING_ONLY

"""

# The deploy steps of the prod builds (tier full: after preflight and check-prod; tier check_rollback:
# after turn and check-direct). Free of {{ }} placeholders that differ between the two.
PROD_DEPLOY_STEPS = """\
    # Release record (gs://orbit-search-release-records/{service}/prod/): the deployment that serves now
    # (the rollback target) and whether the commits since the last release add a database migration.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-record-begin"
      entrypoint: "bash"
      secretEnv: ["RAILWAY_TOKEN_PROD"]
      args:
          - "-c"
          - |
              # Every path that stops before the deploy leaves the no-change receipt, so this build never
              # counts as a prod change (release_verify.builds_since_rollback).
              release_no_change() {{
                if [ -n "$${{BUILDER_OUTPUT:-}}" ]; then
                  printf '%s\\n' '{{"release_deploy":"skipped_before_mutation"}}' > "$${{BUILDER_OUTPUT}}/output"
                fi
              }}
              if [ -f /workspace/deploy_failed ] || [ -f /workspace/deploy_superseded ] || [ -f /workspace/promote_blocked ]; then
                release_no_change
                exit 0
              fi
              python3 release/release_light.py --service {service} begin --env prod --commit "$COMMIT_SHA" \\
                --build-id "$BUILD_ID" --artifacts-file /workspace/release_artifacts.json \\
                --release-id-file /workspace/release_id \\
                || {{ echo "release record not written; deploy stopped" >&2; release_no_change; exit 1; }}

    # The deploy: Railway builds the commit for {prod_environment} (same source as dev verify, not the
    # same bytes) and the kit waits until the deployment is SUCCESS.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "deploy"
      entrypoint: "bash"
      allowExitCodes: [1]
      secretEnv: ["RAILWAY_TOKEN_PROD"]
      args:
          - "-c"
          - |
              if [ -f /workspace/deploy_failed ] || [ -f /workspace/deploy_superseded ]; then exit 0; fi
              python3 release/release_light.py --service {service} apply --artifacts-file /workspace/release_artifacts.json \\
                || {{ touch /workspace/deploy_failed; exit 1; }}
      timeout: "{wait_seconds}s"

    # Release record: snapshot again, set deployed or deploy_failed, point latest.json at it, free the prod lock.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-record-finish"
      entrypoint: "bash"
      secretEnv: ["RAILWAY_TOKEN_PROD", "WEBHOOK_ORBIT"]
      args:
          - "-c"
          - |
              if [ -f /workspace/deploy_superseded ] || [ ! -s /workspace/release_id ]; then exit 0; fi
              STATUS=deployed
              if [ -f /workspace/deploy_failed ]; then STATUS=deploy_failed; fi
              # The deploy changed prod: retry the finish. If it still fails, the kit posts the recovery
              # commands and the release stays unchecked until they run.
              python3 release/release_light.py --service {service} finish --env prod --attempts 3 \\
                --release-id-file /workspace/release_id --status "$${{STATUS}}" \\
                || {{ echo "release record not finished after 3 attempts; the release is unchecked until the recovery commands in the alert run" | tee -a /workspace/release_verify_unchecked >&2; touch /workspace/release_record_failed; }}

    # Deploy check: the new deployment serves {prod_environment}, of this commit, and the smoke checks pass.
    # Then the watch window (+15 and +60 min): the deployment status, new error log lines and the 5xx
    # share of its HTTP logs, and the smoke checks again. A failed release redeploys the deployment that
    # served before (trigger {rollback_trigger}) and holds; a release with a database migration holds and pings.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-verify-deploy"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT", "RAILWAY_TOKEN_PROD"]
      args:
          - "-c"
          - |
              if [ -f /workspace/release_record_failed ] || [ -f /workspace/deploy_superseded ]; then exit 0; fi
              if [ ! -s /workspace/release_id ]; then
                echo "release record missing; release is unchecked" | tee -a /workspace/release_verify_unchecked >&2
                exit 0
              fi
              python3 release/release_light.py --service {service} check --env prod \\
                --release-id-file /workspace/release_id --failed-marker /workspace/release_verify_failed
              VERIFY_STATUS=$$?
              if [ "$${{VERIFY_STATUS}}" -ne 0 ] && [ "$${{VERIFY_STATUS}}" -ne 2 ]; then
                echo "release-verify could not run (exit $${{VERIFY_STATUS}}); release is unchecked" | tee -a /workspace/release_verify_unchecked >&2
              fi
              python3 release/release_light.py --service {service} schedule-window --release-id-file /workspace/release_id \\
                || echo "schedule-window failed; the +15/+60 checks may be unchecked. Reuse a running watch (tag {service}-release-verify) or submit one: python3 release/release_light.py --service {service} schedule-window --release-id $$(cat /workspace/release_id) --source-dir /tmp/release-verify-src" | tee -a /workspace/release_verify_unchecked >&2
              exit 0

    - name: "gcr.io/cloud-builders/gcloud"
      id: "notify-result"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT"]
      args:
          - "-c"
          - |
              if [ -f /workspace/deploy_superseded ]; then
                REASON="a newer prod build owns the release"
                if [ -s /workspace/promote_skip_reason ]; then REASON="$$(head -c 600 /workspace/promote_skip_reason)"; fi
                TEXT=":fast_forward: {title} prod skipped commit \\`$SHORT_SHA\\`: $${{REASON}}"
              elif [ -f /workspace/deploy_failed ]; then
                TEXT=":x: {title} prod deploy failed for commit \\`$SHORT_SHA\\`; Railway keeps the old deployment when the new one fails its health check. See the deploy step"
              elif [ -f /workspace/release_verify_unchecked ]; then
                TEXT=":x: {title} prod is UNCHECKED for commit \\`$SHORT_SHA\\`; see the release-verify steps"
              elif [ -f /workspace/release_verify_failed ]; then
                TEXT=":x: {title} release check FAILED for commit \\`$SHORT_SHA\\`; see the release record"
              else
                TEXT=":white_check_mark: {title} prod deploy completed for commit \\`$SHORT_SHA\\`"
              fi
              echo "$${{TEXT}}"
              # Slack gets a status line only when no other step posts this moment: a failed deploy or an
              # unchecked prod. A failed check posted its down line; the promote said deploying; the watch says up.
              if [ -f /workspace/deploy_failed ] || [ -f /workspace/release_verify_unchecked ]; then
                curl -s -X POST -H 'Content-type: application/json' --data '{{"text":"{line_warning}"}}' "$$WEBHOOK_ORBIT"
              fi
              if [ -f /workspace/deploy_failed ]; then exit 1; fi

    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-verify-result"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              if [ -f /workspace/release_verify_unchecked ]; then
                echo "release UNCHECKED: $$(cat /workspace/release_verify_unchecked)" >&2
                exit 1
              fi
              if [ ! -f /workspace/release_verify_failed ]; then exit 0; fi
              echo "release-verify failed; see the release record and the release-verify-deploy step" >&2
              exit 1
"""

PROMOTE_TEMPLATE = (
    """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Prod deploy of {service} (kind railway, tier full), the second half of a merge
# deploy ({docs}). The manual trigger {prod_trigger} runs it; the dev build of a
# merge starts it for the same commit after dev verify passed in
# {dev_environment}. It deploys that commit to {prod_environment}. Railway builds
# it again: the same source as dev verify checked, not the same bytes.
#
# Guards before the deploy:
#   1. single flight: a newer prod build supersedes this one; an older one or a
#      rollback that still runs is waited for;
#   2. the commit is on {branch}, and the newest dev build of the commit succeeded;
#   3. the gate: dev verify of this commit passed, no freeze, no hold after a
#      rollback or a failed forward-only release, no newer verified commit; the
#      build holds the prod lock from here to release-record-finish.
# Then the release record, the deploy, the deploy check and the watch window
# (+15 and +60 min), which rolls back a failed release on its own.
substitutions:
    # Set by the dev build that started this promote (release_gate.py); empty for manual runs.
    _DEV_BUILD_ID: ""

steps:
    - name: "gcr.io/cloud-builders/gcloud"
      id: "promote-preflight"
      entrypoint: "bash"
      allowExitCodes: [1]
      args:
          - "-c"
          - |
              echo "Started by dev build: $${{PROMOTE_DEV_BUILD_ID:-none (manual run)}}"
              python3 release/release_light.py --service {service} preflight --build-id "$BUILD_ID" \\
                --commit "$COMMIT_SHA" --out-dir /workspace
              GUARD_STATUS=$$?
              if [ "$${{GUARD_STATUS}}" -eq 3 ]; then touch /workspace/deploy_superseded; exit 0; fi
              if [ "$${{GUARD_STATUS}}" -ne 0 ]; then touch /workspace/promote_blocked; exit 1; fi
      env:
          - "PROMOTE_DEV_BUILD_ID=${{_DEV_BUILD_ID}}"
      timeout: "5400s"

    - name: "gcr.io/cloud-builders/gcloud"
      id: "promote-gate"
      entrypoint: "bash"
      allowExitCodes: [1]
      args:
          - "-c"
          - |
              if [ -f /workspace/promote_blocked ] || [ -f /workspace/deploy_superseded ]; then exit 0; fi
              python3 release/release_gate.py --service {service} check-prod \\
                --commit "$COMMIT_SHA" \\
                --build-id "$BUILD_ID" \\
                --artifacts-file /workspace/release_artifacts.json \\
                --skip-reason-file /workspace/promote_skip_reason
              GATE_STATUS=$$?
              if [ "$${{GATE_STATUS}}" -eq 4 ]; then touch /workspace/deploy_superseded; exit 0; fi
              if [ "$${{GATE_STATUS}}" -ne 0 ]; then touch /workspace/promote_blocked; exit 1; fi

    - name: "gcr.io/cloud-builders/curl"
      id: "notify-promote-blocked"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT"]
      args:
          - "-c"
          - |
              if [ ! -f /workspace/promote_blocked ]; then exit 0; fi
              # Blocked before any change: the no-change receipt (release_verify.builds_since_rollback).
              if [ -n "$${{BUILDER_OUTPUT:-}}" ]; then
                printf '%s\\n' '{{"release_deploy":"skipped_before_mutation"}}' > "$${{BUILDER_OUTPUT}}/output"
              fi
              echo '{title} prod deploy blocked for commit `$SHORT_SHA` (not on {branch}, dev build missing or failed, or dev verify not passed); production is unchanged. See <https://console.cloud.google.com/cloud-build/builds;region={region}/$BUILD_ID?project=$PROJECT_ID|the prod build>'
              curl -s -X POST -H 'Content-type: application/json' --data '{{"text":"{line_on_hold}"}}' "$$WEBHOOK_ORBIT"
              exit 1

    # Still current: right before the deploy, no newer prod build exists and no older build or rollback started.
    - name: "gcr.io/cloud-builders/gcloud"
      id: "promote-still-current"
      entrypoint: "bash"
      allowExitCodes: [1]
      args:
          - "-c"
          - |
              if [ -f /workspace/deploy_superseded ]; then exit 0; fi
              python3 release/release_light.py --service {service} still-current --build-id "$BUILD_ID" \\
                --commit "$COMMIT_SHA" --out-dir /workspace
              GUARD_STATUS=$$?
              if [ "$${{GUARD_STATUS}}" -eq 3 ]; then touch /workspace/deploy_superseded; exit 0; fi
              if [ "$${{GUARD_STATUS}}" -ne 0 ]; then touch /workspace/deploy_failed; exit 1; fi

"""
    + PROD_DEPLOY_STEPS
    + """
# Up to 60 minutes waiting for an older prod build and 20 for the dev build of this commit.
timeout: "7200s"
options:
    logging: CLOUD_LOGGING_ONLY

"""
)

DIRECT_TEMPLATE = (
    """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Prod deploy of {service} (kind railway, tier check_rollback) on every merge to
# {branch} ({docs}); the push trigger {prod_trigger} runs it. This service has no
# dev stage yet (no Railway environment that is not production), so a merge
# goes to {prod_environment} without dev verify. Railway's own GitHub auto
# deploy is off: this build is the only thing that deploys. Guards: single
# flight (a newer build supersedes this one), the commit is on {branch}, and the
# gate (no freeze, no hold after a rollback or a failed forward-only release;
# the build holds the prod lock to release-record-finish). Then the release
# record, the deploy, the deploy check and the watch window (+15 and +60 min),
# which rolls back a failed release on its own. When the service gets a dev
# environment, set tier full in the config and render again.
steps:
    - name: "gcr.io/cloud-builders/gcloud"
      id: "deploy-turn"
      entrypoint: "bash"
      allowExitCodes: [1]
      args:
          - "-c"
          - |
              python3 release/release_light.py --service {service} turn --build-id "$BUILD_ID" --commit "$COMMIT_SHA"
              GUARD_STATUS=$$?
              if [ "$${{GUARD_STATUS}}" -eq 3 ]; then touch /workspace/deploy_superseded; exit 0; fi
              if [ "$${{GUARD_STATUS}}" -ne 0 ]; then touch /workspace/promote_blocked; exit 1; fi
              # What this build deploys: the commit (Railway builds it).
              python3 -c 'import json,sys; print(json.dumps({{sys.argv[1]: sys.argv[2]}}))' "{service}" "$COMMIT_SHA" \\
                > /workspace/release_artifacts.json
      timeout: "2400s"

    - name: "gcr.io/cloud-builders/gcloud"
      id: "deploy-gate"
      entrypoint: "bash"
      allowExitCodes: [1]
      args:
          - "-c"
          - |
              if [ -f /workspace/promote_blocked ] || [ -f /workspace/deploy_superseded ]; then exit 0; fi
              python3 release/release_gate.py --service {service} check-direct \\
                --commit "$COMMIT_SHA" --build-id "$BUILD_ID" --skip-reason-file /workspace/promote_skip_reason
              GATE_STATUS=$$?
              if [ "$${{GATE_STATUS}}" -eq 4 ]; then touch /workspace/deploy_superseded; exit 0; fi
              if [ "$${{GATE_STATUS}}" -ne 0 ]; then touch /workspace/promote_blocked; exit 1; fi

    # Blocked (not on {branch}, or the gate could not decide): production is unchanged; a human decides.
    - name: "gcr.io/cloud-builders/curl"
      id: "notify-promote-blocked"
      entrypoint: "bash"
      secretEnv: ["WEBHOOK_ORBIT"]
      args:
          - "-c"
          - |
              if [ ! -f /workspace/promote_blocked ]; then exit 0; fi
              # Blocked before any change: the no-change receipt (release_verify.builds_since_rollback).
              if [ -n "$${{BUILDER_OUTPUT:-}}" ]; then
                printf '%s\\n' '{{"release_deploy":"skipped_before_mutation"}}' > "$${{BUILDER_OUTPUT}}/output"
              fi
              echo '{title} prod deploy blocked for commit `$SHORT_SHA` (not on {branch}, or the single-flight or gate step failed); production is unchanged. See <https://console.cloud.google.com/cloud-build/builds;region={region}/$BUILD_ID?project=$PROJECT_ID|the deploy build>'
              curl -s -X POST -H 'Content-type: application/json' --data '{{"text":"{line_on_hold}"}}' "$$WEBHOOK_ORBIT"
              exit 1

"""
    + PROD_DEPLOY_STEPS
    + """
# Up to 40 minutes for an older deploy build, then the deploy.
timeout: "5400s"
options:
    logging: CLOUD_LOGGING_ONLY

"""
)

WINDOW_TEMPLATE = """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Watch window of one {service} prod release (kind railway): release/release_light.py
# watch. The prod build submits it with the kit and the source tree as its
# source (release_light.py schedule-window). It reads Railway (the deployment
# status, its error log lines, the 5xx share of its HTTP logs against the
# previous deployment), the smoke checks and GitHub, and writes the release
# record and Slack lines. When the release fails it starts the
# {rollback_trigger} trigger with _EXPECT_LATEST and pings the suspected PR's
# author; a release that adds a database migration holds and pings instead.
# Test runs (_AUTO_ROLLBACK empty) never roll back.
steps:
    - name: "gcr.io/cloud-builders/gcloud"
      id: "release-verify-window"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              set -euo pipefail
              python3 release/release_light.py --service "${{_SERVICE}}" watch \\
                --release-id "${{_RELEASE_ID}}" \\
                --checkpoints "${{_CHECKPOINTS}}"
      env:
          - "RELEASE_SLACK_DISABLED=${{_SLACK_DISABLED}}"
          - "RELEASE_AUTO_ROLLBACK=${{_AUTO_ROLLBACK}}"
      secretEnv: ["WEBHOOK_ORBIT", "RAILWAY_TOKEN_PROD"]
      timeout: "13800s"

substitutions:
    _SERVICE: {service}
    _RELEASE_ID: latest
    _CHECKPOINTS: "{checkpoints}"
    # "1" for test runs: print the Slack lines instead of posting them.
    _SLACK_DISABLED: ""
    # "1" (set by schedule-window for real releases): a failed release rolls back.
    _AUTO_ROLLBACK: ""

tags: ["{service}-release-verify"]
serviceAccount: "projects/{project}/serviceAccounts/{build_service_account}"
timeout: "14400s"
options:
    logging: CLOUD_LOGGING_ONLY

"""

ROLLBACK_TEMPLATE = """\
# GENERATED by release/release_kit.py render from release/services/{service}.json. Do not edit.
# Rollback of {service} prod (kind railway): release/release_rollback.py apply.
# Started through the manual trigger {rollback_trigger} by `release_rollback.py
# run`, or by the watch-window build of a release that failed release-verify
# (with _EXPECT_LATEST set). It builds nothing: Railway redeploys the deployment
# that served before the target release with its image (no rebuild) and the
# current variables. It refuses when production moved to a deployment the kit
# did not make, and when the rolled-back commits add a database migration
# (forward-only). It holds the prod lock, writes a release record, and holds
# automatic prod deploys until a revert or a Fixes-release commit merges.
# _DRY_RUN defaults to true, so a run without substitutions changes nothing.
steps:
    - name: "gcr.io/cloud-builders/gcloud"
      id: "rollback"
      entrypoint: "bash"
      args:
          - "-c"
          - |
              set -euo pipefail
              DRY_RUN_FLAG=""
              if [ "$${{ROLLBACK_DRY_RUN}}" != "false" ]; then DRY_RUN_FLAG="--dry-run"; fi
              python3 release/release_rollback.py apply \\
                --service {service} \\
                --target "$${{ROLLBACK_TARGET}}" \\
                --reason "$${{ROLLBACK_REASON}}" \\
                --requested-by "$${{ROLLBACK_REQUESTED_BY}}" \\
                --build-id "$BUILD_ID" \\
                --expect-latest "$${{ROLLBACK_EXPECT_LATEST}}" \\
                $${{DRY_RUN_FLAG}}
      env:
          # Free text goes through the environment, never into the script text.
          - "ROLLBACK_TARGET=${{_TARGET}}"
          - "ROLLBACK_REASON=${{_REASON}}"
          - "ROLLBACK_REQUESTED_BY=${{_REQUESTED_BY}}"
          - "ROLLBACK_DRY_RUN=${{_DRY_RUN}}"
          - "ROLLBACK_EXPECT_LATEST=${{_EXPECT_LATEST}}"
      secretEnv: ["WEBHOOK_ORBIT", "RAILWAY_TOKEN_PROD"]
      # Up to 30 min for the prod lock (release_rollback.py LOCK_WAIT_SECONDS), then the redeploy.
      timeout: "3600s"

substitutions:
    _TARGET: previous
    _REASON: ""
    _REQUESTED_BY: ""
    _DRY_RUN: "true"
    # A release id: refuse unless it is still the newest record (automatic rollbacks).
    _EXPECT_LATEST: ""

# The prod builds and release_light.py find running rollbacks by this tag.
tags: ["{service}-rollback"]
serviceAccount: "projects/{project}/serviceAccounts/{build_service_account}"
timeout: "3900s"
options:
    logging: CLOUD_LOGGING_ONLY

"""


def secrets(service: object, envs: Iterable[str]) -> str:
    """The availableSecrets block: the Slack webhook and the project token of each environment named."""
    tokens = dict(service.railway_tokens)  # type: ignore[attr-defined]
    return SECRETS_HEAD + "".join(SECRET_LINE.format(secret=tokens[env], env=TOKEN_ENVS[env]) for env in envs)


def rendered(service: object) -> dict[str, str]:
    """The generated builds of a kind railway service: the merge build, the prod build (tier full), window, rollback."""
    environments = dict(service.railway_environments)  # type: ignore[attr-defined]
    urls = dict(service.railway_urls)  # type: ignore[attr-defined]
    wait = service.deployment_wait_minutes  # type: ignore[attr-defined]
    full = service.tier == "full"  # type: ignore[attr-defined]
    values = {
        "service": service.name,  # type: ignore[attr-defined]
        "title": service.title,  # type: ignore[attr-defined]
        "project": service.project,  # type: ignore[attr-defined]
        "build_service_account": service.build_service_account,  # type: ignore[attr-defined]
        "region": service.region,  # type: ignore[attr-defined]
        "docs": service.docs,  # type: ignore[attr-defined]
        "branch": service.release_branch,  # type: ignore[attr-defined]
        "prod_trigger": service.deploy_triggers[0],  # type: ignore[attr-defined]
        "dev_trigger": service.dev_triggers[0] if full else "",  # type: ignore[attr-defined]
        "rollback_trigger": service.rollback_trigger,  # type: ignore[attr-defined]
        "checkpoints": " ".join(str(value) for value in service.checkpoints),  # type: ignore[attr-defined]
        "prod_environment": environments["prod"],
        "dev_environment": environments.get("dev", ""),
        "dev_url": urls.get("dev", ""),
        "wait_minutes": wait,
        "wait_seconds": (wait + 5) * 60,
        "build_seconds": (wait + 70) * 60,
        # The #deployments lines the build steps post with curl (no Python in the curl image): set at render.
        "line_on_hold": rr.status_line("on hold", service.name),  # type: ignore[attr-defined]
        "line_warning": rr.status_line("warning", service.name),  # type: ignore[attr-defined]
    }
    prod = secrets(service, ["prod"])
    files = {
        service.window_config: WINDOW_TEMPLATE.format(**values) + prod,  # type: ignore[attr-defined]
        service.rollback_config: ROLLBACK_TEMPLATE.format(**values) + prod,  # type: ignore[attr-defined]
    }
    if full:
        files[service.release_build_config] = DEV_TEMPLATE.format(**values) + secrets(service, ["dev"])  # type: ignore[attr-defined]
        files[service.promote_build_config] = PROMOTE_TEMPLATE.format(**values) + prod  # type: ignore[attr-defined]
    else:
        files[service.release_build_config] = DIRECT_TEMPLATE.format(**values) + prod  # type: ignore[attr-defined]
    return files
