#!/usr/bin/env python3
"""Vercel projects in the release kit: kind "vercel", tier full.

Vercel builds the production deployment of a merge itself. A required
Deployment Check (the GitHub commit status `orbit-release/<project>`) keeps the
production domains on the old deployment until the kit posts `success`. So the
kit never builds or uploads anything: it waits for Vercel's build, verifies it
on its own deployment URL (the dev stage), passes the gate, and then lets
Vercel assign the domains to exactly that deployment.

This module holds the Vercel parts that release_light.py uses:

- VercelApi: the Vercel REST API through urllib (projects, deployments,
  aliases, promote, rollback, request logs).
- The commit status of the Deployment Check (GitHub REST API).
- The checks of a deployment: dev verify on the deployment URL, the domains,
  new error log lines and the 5xx share of the request logs.

The Vercel token is team-scoped. In Cloud Build it comes from the environment
(VERCEL_RELEASE_TOKEN, the secretEnv of the Secret Manager secret
vercel-release-token); elsewhere it is read from Secret Manager with the
gcloud identity. It is never printed and never put on a command line.

Part of the release kit (orbit-infrastructure/release-kit). Stdlib only (Python 3.10).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_record as rr  # noqa: E402
import release_signals as rs  # noqa: E402

API = "https://api.vercel.com"
# The request logs: the endpoint the Vercel CLI reads for `vercel logs` (50 rows a page).
LOGS_API = "https://vercel.com"
TOKEN_ENV = "VERCEL_RELEASE_TOKEN"
DEFAULT_TOKEN_SECRET = "vercel-release-token"
# The Secret Manager secret the builds put in GITHUB_TOKEN: a token that can write commit statuses.
DEFAULT_GITHUB_SECRET = "release-kit-github-token"
BYPASS_HEADER = "x-vercel-protection-bypass"
BYPASS_SCOPE = "automation-bypass"
# GitHub cuts a commit status description at 140 characters.
STATUS_LIMIT = 140
STATUS_STATES = ("pending", "success", "failure", "error")
BUILDING = frozenset({"QUEUED", "INITIALIZING", "BUILDING"})
FAILED = frozenset({"ERROR", "CANCELED", "DELETED"})
READY = "READY"
LOG_LEVELS = {"warning": "WARNING", "error": "ERROR", "fatal": "CRITICAL"}
ERROR_LEVELS = ("error", "fatal")
# Pages of request logs one read takes at most (50 rows a page).
LOG_MAX_PAGES = 40
BASELINE_MAX_PAGES = 100
# The dev build exits with this code when Vercel skipped the build (the app is not affected).
EXIT_NOT_AFFECTED = 5
USER_AGENT = "orbit-release-kit"


class VercelError(rr.RecordError):
    """A Vercel or GitHub call failed, or a deployment is not what the release needs."""


Http = Callable[[urllib.request.Request], tuple[int, bytes]]


def http_request(request: urllib.request.Request) -> tuple[int, bytes]:
    """Send a request and return (status, body); HTTP errors are statuses, not exceptions."""
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def clip(text: str, limit: int) -> str:
    """The text on one line, cut to limit characters."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- the token


def secret_token(project: str, secret: str, google_token: Callable[[], str], http: Http = http_request) -> str:
    """The latest version of a Secret Manager secret, read with the gcloud identity's access token."""
    url = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{secret}/versions/latest:access"
    status, body = http(urllib.request.Request(url, headers={"Authorization": f"Bearer {google_token()}"}))
    if status != 200:
        raise VercelError(
            f"cannot read Secret Manager secret {secret} (HTTP {status}); the identity needs "
            "roles/secretmanager.secretAccessor on it"
        )
    try:
        value = base64.b64decode(json.loads(body)["payload"]["data"]).decode("utf-8").strip()
    except (ValueError, KeyError, TypeError) as error:
        raise VercelError(f"Secret Manager secret {secret} has no readable payload: {type(error).__name__}") from None
    if not value:
        raise VercelError(f"Secret Manager secret {secret} is empty")
    return value


def token_source(
    project: str, secret: str, google_token: Callable[[], str], http: Http = http_request
) -> Callable[[], str]:
    """A function that returns the Vercel token: VERCEL_RELEASE_TOKEN, else the Secret Manager secret (read once)."""
    cache: list[str] = []

    def token() -> str:
        """The token, read on first use."""
        if not cache:
            cache.append(os.environ.get(TOKEN_ENV, "").strip() or secret_token(project, secret, google_token, http))
        return cache[0]

    return token


# --------------------------------------------------------------------------- the API


class VercelApi:
    """The Vercel REST API of one team, with a bearer token; 429 and 5xx are tried again."""

    def __init__(
        self,
        team: str,
        token: Callable[[], str],
        *,
        http: Http = http_request,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 4,
    ) -> None:
        """Keep the team id, the token function and the transport."""
        self.team = team
        self.token = token
        self.http = http
        self.sleep = sleep
        self.attempts = attempts

    def call(self, method: str, path: str, query: dict | None = None, body: dict | None = None, base: str = API):
        """Send one request and decode the JSON answer ({} for an empty body); raise VercelError on an error."""
        url = f"{base}{path}?{urllib.parse.urlencode({**(query or {}), 'teamId': self.team})}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.token()}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                status, raw = self.http(request)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == self.attempts:
                    raise VercelError(f"{method} {path} failed: {error}") from None
                self.sleep(2**attempt)
                continue
            if (status == 429 or status >= 500) and attempt < self.attempts:
                self.sleep(2**attempt)
                continue
            if status >= 400:
                raise VercelError(f"{method} {path} returned HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
            text = raw.decode("utf-8", "replace").strip()
            return json.loads(text) if text else {}
        raise AssertionError("unreachable")

    def project(self, name: str) -> dict:
        """The project (name or id): its id, autoAssignCustomDomains, protectionBypass, lastAliasRequest."""
        return self.call("GET", f"/v9/projects/{urllib.parse.quote(name, safe='')}")

    def deployment(self, deployment_id: str) -> dict:
        """One deployment: id, url, readyState, readySubstate, target, meta.githubCommitSha, createdAt."""
        return self.call("GET", f"/v13/deployments/{urllib.parse.quote(deployment_id, safe='')}")

    def production_deployments(
        self, project_id: str, *, commit: str = "", since_ms: int = 0, limit: int = 50
    ) -> list[dict]:
        """Production deployments of the project, newest first (one page), of one commit or created after since_ms."""
        query = {"projectId": project_id, "target": "production", "limit": str(limit)}
        if commit:
            query["meta-githubCommitSha"] = commit
        if since_ms:
            query["since"] = str(since_ms)
        return list(self.call("GET", "/v6/deployments", query).get("deployments") or [])

    def alias_deployment(self, domain: str) -> str:
        """The deployment id a domain (alias) points at now."""
        return str(self.call("GET", f"/v4/aliases/{urllib.parse.quote(domain, safe='')}").get("deploymentId") or "")

    def promote(self, project_id: str, deployment_id: str) -> None:
        """Point the production domains at a deployment without a rebuild (POST /v10/projects/{id}/promote/{dpl})."""
        self.call("POST", f"/v10/projects/{project_id}/promote/{deployment_id}", body={})

    def rollback(self, project_id: str, deployment_id: str) -> None:
        """Point the production domains back at an earlier deployment; Vercel turns auto-assignment off."""
        self.call("POST", f"/v9/projects/{project_id}/rollback/{deployment_id}", body={})

    def request_logs(
        self,
        project_id: str,
        start: datetime,
        end: datetime,
        *,
        deployment: str = "",
        levels: Iterable[str] = (),
        status_code: str = "",
        max_pages: int = LOG_MAX_PAGES,
    ) -> tuple[list[dict], bool]:
        """(request log rows of production in [start, end), complete): complete is False when max_pages ran out."""
        query = {
            "projectId": project_id,
            "ownerId": self.team,
            "environment": "production",
            "startDate": str(int(start.timestamp() * 1000)),
            "endDate": str(int(end.timestamp() * 1000)),
        }
        if deployment:
            query["deploymentId"] = deployment
        if levels:
            query["level"] = ",".join(levels)
        if status_code:
            query["statusCode"] = status_code
        rows: list[dict] = []
        for page in range(max_pages):
            payload = self.call("GET", "/api/logs/request-logs", {**query, "page": str(page)}, base=LOGS_API)
            rows += list(payload.get("rows") or [])
            if not payload.get("hasMoreRows") or not payload.get("rows"):
                return rows, True
        return rows, False


# --------------------------------------------------------------------------- deployments


def deployment_id(deployment: dict) -> str:
    """The id of a deployment (list items have uid, single reads have id)."""
    return str(deployment.get("uid") or deployment.get("id") or "")


def deployment_state(deployment: dict) -> str:
    """READY, BUILDING, ERROR, CANCELED, ..."""
    return str(deployment.get("readyState") or deployment.get("state") or "")


def deployment_commit(deployment: dict) -> str:
    """The Git commit a deployment was built from (empty for a CLI upload)."""
    return str((deployment.get("meta") or {}).get("githubCommitSha") or "")


def deployment_ref(deployment: dict) -> str:
    """The Git branch a deployment was built from."""
    return str((deployment.get("meta") or {}).get("githubCommitRef") or "")


def deployment_created(deployment: dict) -> int:
    """When the deployment was created, in ms since the epoch."""
    return int(deployment.get("createdAt") or deployment.get("created") or 0)


def deployment_url(deployment: dict) -> str:
    """The deployment's own https URL (https://<project>-<hash>-<team>.vercel.app)."""
    url = str(deployment.get("url") or "")
    return url if url.startswith("https://") else f"https://{url}"


def commit_deployment(api: VercelApi, project_id: str, commit: str) -> dict | None:
    """The newest production deployment of the commit that Vercel built from Git, else None."""
    found = [
        item for item in api.production_deployments(project_id, commit=commit) if deployment_commit(item) == commit
    ]
    built = [item for item in found if str(item.get("source") or "git") == "git"] or found
    return max(built, key=deployment_created) if built else None


def wait_for_deployment(
    api: VercelApi,
    project_id: str,
    commit: str,
    timeout_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = 20,
) -> dict | None:
    """Wait until Vercel's production deployment of the commit is READY, ERROR or CANCELED; None when there is none.

    A deployment that still builds when the time runs out is returned as it is.
    """
    deadline = clock() + timeout_seconds
    last = None
    while True:
        last = commit_deployment(api, project_id, commit) or last
        state = deployment_state(last) if last else "none"
        if last and state not in BUILDING:
            return last
        if clock() >= deadline:
            return last
        print(f"Waiting for the Vercel production deployment of {commit[:9]} ({state})", flush=True)
        sleep(poll_seconds)


def newer_deployment(api: VercelApi, project_id: str, deployment: dict, branch: str) -> dict | None:
    """A production deployment of a newer commit on the branch that is not failed or skipped, else None.

    Vercel would give the domains to the newest deployment whose check passed, so an older one must not
    pass. A deployment of the same commit (a redeploy) does not count: its commit status is the same one.
    """
    created = deployment_created(deployment)
    for item in api.production_deployments(project_id, since_ms=created + 1):
        if deployment_id(item) == deployment_id(deployment) or deployment_created(item) <= created:
            continue
        if deployment_ref(item) != branch or deployment_commit(item) in ("", deployment_commit(deployment)):
            continue
        if deployment_state(item) in FAILED:
            continue
        return item
    return None


def skipped_by_ignored_build_step(deployment: dict) -> bool:
    """Whether Vercel cancelled the deployment because its Ignored Build Step said the app is not affected.

    Vercel marks such a cancel with a link to the Ignored Build Step docs (errorLink); a build cancelled
    by a human or by a newer push has no such mark.
    """
    return deployment_state(deployment) == "CANCELED" and "ignored-build-step" in str(deployment.get("errorLink") or "")


def in_rollback_state(project: dict) -> bool:
    """Whether Vercel holds the project after a rollback: auto-assignment of the production domains is off."""
    return project.get("autoAssignCustomDomains") is False


def bypass_secret(project: dict) -> str:
    """The Protection Bypass for Automation secret of the project, or "" when there is none."""
    for secret, info in (project.get("protectionBypass") or {}).items():
        if isinstance(info, dict) and info.get("scope") == BYPASS_SCOPE:
            return str(secret)
    return ""


def aliases(api: VercelApi, domains: Iterable[str]) -> dict[str, str]:
    """{domain: the deployment id it points at}."""
    return {domain: api.alias_deployment(domain) for domain in domains}


def wait_for_aliases(
    api: VercelApi,
    domains: tuple[str, ...],
    target: str,
    timeout_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = 10,
) -> dict[str, str]:
    """Wait until every domain points at the target deployment; return the aliases as they are at the end."""
    deadline = clock() + timeout_seconds
    while True:
        now = aliases(api, domains)
        if all(value == target for value in now.values()) or clock() >= deadline:
            return now
        sleep(poll_seconds)


# --------------------------------------------------------------------------- the Deployment Check


def post_status(
    repo: str,
    commit: str,
    context: str,
    state: str,
    description: str,
    token: str,
    *,
    target_url: str = "",
    http: Http = http_request,
) -> None:
    """Set the commit status that Vercel's Deployment Check reads; raise VercelError when GitHub refuses it."""
    if state not in STATUS_STATES:
        raise VercelError(f"commit status state {state!r} is not one of {STATUS_STATES}")
    body = {"state": state, "context": context, "description": clip(description, STATUS_LIMIT)}
    if target_url:
        body["target_url"] = target_url
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/statuses/{commit}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        status, raw = http(request)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise VercelError(f"GitHub status {context} on {commit[:9]} failed: {error}") from None
    if status not in (200, 201):
        raise VercelError(
            f"GitHub refused status {context}={state} on {commit[:9]} (HTTP {status}: "
            f"{raw[:200].decode('utf-8', 'replace')}); the token needs commit status write on {repo}"
        )
    print(f"Commit status {context}={state} on {commit[:9]}: {body['description']}", flush=True)


def gate_status(index: dict) -> tuple[str, str]:
    """(state, description) of the Deployment Check from a commit's dev index after the dev build.

    Frozen or held: pending (the build stays staged until a human or a fix lets it go).
    Stale, unverified or failed: failure (this deployment must never take the domains).
    Superseded: state "" (leave the status as it is; the newer build owns the release).
    """
    verdict = (index.get("verdict") or {}).get("status")
    promote = index.get("promote") or {}
    if not index:
        return "failure", "dev verify did not run; nothing is promoted"
    if verdict == "superseded":
        return "", "superseded by a newer dev build"
    if verdict != "passed":
        return "failure", f"dev verify {verdict or 'did not finish'}; nothing is promoted"
    status = str(promote.get("status") or "")
    if status == "started":
        return "pending", f"dev verify passed; prod promote build {promote.get('build_id') or ''} started"
    if status in ("frozen", "held"):
        return "pending", f"{status}: {promote.get('reason') or ''}"
    if status:
        return "failure", f"{status}: {promote.get('reason') or ''}"
    return "pending", "dev verify passed; no automatic promote (release_gate.py promote starts one)"


def skip_status(reason: str) -> tuple[str, str]:
    """(state, description) when the prod build skipped at its gate (release_gate.py --skip-reason-file).

    Frozen and held stay pending (a human or a fix lets the build go later); stale fails. No reason
    means single flight superseded the build: state "", the status stays as it is, because a newer
    build (possibly of the same commit) owns the release and may already have posted success.
    """
    reason = reason.strip()
    if not reason:
        return "", "superseded by a newer prod build or deployment"
    return ("pending" if reason.startswith(("frozen:", "held:")) else "failure"), reason


# --------------------------------------------------------------------------- the logs


def row_time(row: dict) -> str:
    """A request log row's time as YYYY-MM-DDTHH:MM:SS (UTC)."""
    value = row.get("timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, rr.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return str(value or "")[:19]


def log_lines(rows: Iterable[dict], levels: Iterable[str] = tuple(LOG_LEVELS)) -> list[tuple[str, str, str, str, str]]:
    """The log lines of request rows at these levels as (time, deployment, source, severity, text)."""
    wanted = set(levels)
    out = []
    for row in rows:
        for item in row.get("logs") or []:
            level = str(item.get("level") or "").lower()
            if level in wanted and str(item.get("message") or "").strip():
                out.append(
                    (row_time(row), str(row.get("deploymentId") or ""), "vercel", LOG_LEVELS[level], item["message"])
                )
    return out


def new_error_problems(
    api: VercelApi,
    project_id: str,
    deployments: Iterable[str],
    scope: rs.Scope,
    now: datetime,
    *,
    min_count: int,
) -> tuple[list[rs.Problem], list[str], bool]:
    """(new error log sites of the release's deployments, notes, judged).

    A site is new when no production deployment wrote it in the baseline (the 24 h before the oldest
    judged release, without spans of rolled-back releases). judged is False when a read stopped at its
    page limit: an incomplete baseline would make old lines look new, so nothing is judged then.
    """
    notes: list[str] = []
    window_rows: list[dict] = []
    for deployment in deployments:
        rows, complete = api.request_logs(
            project_id, scope.window[0], now, deployment=deployment, levels=tuple(LOG_LEVELS)
        )
        window_rows += rows
        if not complete:
            notes.append(f"the window read of {deployment} stopped at {LOG_MAX_PAGES} pages (the newest)")
    window = rs.count_sites(rs.count_patterns(log_lines(window_rows)))
    baseline_rows: list[dict] = []
    for start, end in scope.baseline:
        rows, complete = api.request_logs(
            project_id, start, end, levels=tuple(LOG_LEVELS), max_pages=BASELINE_MAX_PAGES
        )
        baseline_rows += rows
        if not complete:
            notes.append(f"the baseline read stopped at {BASELINE_MAX_PAGES} pages; new lines are not judged")
            return [], notes, False
    known = set(rs.count_sites(rs.count_patterns(log_lines(baseline_rows))))
    recent = (now - timedelta(minutes=rs.LOG_RECENT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")
    problems = []
    for site, item in sorted(window.items(), key=lambda pair: -pair[1].count):
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


@dataclass
class Requests:
    """Request counts of one deployment in one span."""

    deployment: str
    total: int = 0
    errors: int = 0
    # False when the total stopped at the page limit: the share is then an upper bound.
    complete: bool = True
    # False when the 5xx read stopped at the page limit: the 5xx count is then a lower bound.
    errors_complete: bool = True
    paths: dict[str, int] = field(default_factory=dict)

    @property
    def share(self) -> float:
        """The 5xx share of the requests (0 without requests)."""
        return self.errors / self.total if self.total else 0.0


def request_counts(api: VercelApi, project_id: str, deployment: str, start: datetime, end: datetime) -> Requests:
    """All requests and the 5xx ones of a deployment in [start, end); the 5xx are counted in their own read."""
    rows, complete = api.request_logs(project_id, start, end, deployment=deployment)
    counts = Requests(deployment=deployment, total=len(rows), complete=complete)
    errors, errors_complete = api.request_logs(project_id, start, end, deployment=deployment, status_code="5xx")
    counts.errors = len(errors)
    counts.errors_complete = errors_complete
    for row in errors:
        path = str(row.get("requestPath") or "?")
        counts.paths[path] = counts.paths.get(path, 0) + 1
    return counts


@dataclass(frozen=True)
class ShareLimit:
    """When the 5xx share of a release is a regression: above ratio x and absolute over the previous deployment's."""

    ratio: float = 3.0
    absolute: float = 0.02
    min_requests: int = 50
    min_errors: int = 5


def judge_share(new: Requests, old: Requests | None, limit: ShareLimit) -> tuple[str, str]:
    """(status, line): ok, insufficient, no_baseline, inconclusive or exceeded.

    Only "exceeded" can fail a release; the caller applies the runtime-code rule. A read that stopped
    at its page limit never decides alone: a capped new total gives an upper bound (it can only pass),
    a capped 5xx read of the release is inconclusive, and a capped read of the previous deployment is
    insufficient (no share is computed from it; the window marks the check unfinished).
    """
    worst = ", ".join(f"{path} {count}" for path, count in sorted(new.paths.items(), key=lambda pair: -pair[1])[:3])
    line = f"5xx {new.errors}/{new.total}{'+' if not new.complete else ''} ({new.share:.1%}) on {new.deployment}"
    if old is not None:
        line += f"; before {old.errors}/{old.total} ({old.share:.1%}) on {old.deployment}"
    if worst:
        line += f"; paths {worst}"
    if new.errors < limit.min_errors or (new.complete and new.total < limit.min_requests):
        return "insufficient", line
    if not new.errors_complete:
        return "inconclusive", f"{line}; the 5xx read stopped at its page limit"
    if old is not None and not (old.complete and old.errors_complete):
        # A capped total makes no share: the baseline cannot judge the release.
        return "insufficient", f"{line}; the previous deployment's read stopped at its page limit, no share computed"
    if old is None or old.total < limit.min_requests:
        return "no_baseline", line
    bound = max(old.share * limit.ratio, old.share + limit.absolute)
    if new.share <= bound:
        return "ok", line
    return ("exceeded" if new.complete else "inconclusive"), f"{line}; limit {bound:.1%}"
