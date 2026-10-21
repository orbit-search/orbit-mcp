#!/usr/bin/env python3
"""release-verify: deterministic checks of a release of one service, on dev and on prod.

Dev verify (`check --env dev --phase deploy`, full tier, the last steps of the
dev build of every merge) runs the smoke checks against dev and sets the dev
record's verdict. Only a commit whose dev verify passed is promoted to prod
(release_gate.py). A failure posts the author mention and a fix prompt.

On prod it runs twice per release and writes every result into the release
record (release_record.py):

1. `check --phase deploy`, the last steps of the prod build: smoke checks.
   - every Deployment runs the release digest, rollouts are complete, enough
     replicas are available, no pod is in CrashLoopBackOff or an image pull error;
   - the service's own smoke checks from its config: HTTP probes of every pod
     through the Kubernetes API (`http_pod`), public URLs (`http_public`), and
     pods Ready (`pods_ready`).
2. `watch`, a separate Cloud Build that the prod build submits
   (release/services/<service>.release-verify.yaml): at each checkpoint
   (default 15 and 60 minutes after the deploy) it compares the configured
   Datadog metrics of the release window with the baseline (per group: per
   route, per task type), and runs the new-error checks (release_signals.py):
   new monitor alerts, new Error Tracking issues, new error log lines and new
   failure series, each traced to a suspected commit.

A metric limit counts as exceeded only when both its ratio and its absolute
rise are exceeded and both windows hold enough samples; a metric with `spread`
counts only above 1.5x the highest baseline hour of the same group; and a
release whose commits change no runtime code never rolls back on a metric (a
warning instead). A new error with a clear suspect is a regression; one without
is a warning that goes to Slack once.

When a smoke check fails or a regression is found, the record gets verdict
"failed" and a rollback suggestion. Tiers full and check_rollback then start
the rollback build (start_rollback) when the failed release still runs in prod
and nothing else changes prod, and Slack pings the suspected PR's author with a
fix prompt. Tier check_only posts the same alert and asks a human to roll back.

Part of the release kit (orbit-infrastructure/release-kit); ported from
deep-search bin/release_verify.py (#2232, #2290, #2308, #2315) and orbit-api
k8/release (#1525). Stdlib only; it runs in gcr.io/cloud-builders/kubectl (Python 3.10).
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402
import release_gate as rg  # noqa: E402
import release_record as rr  # noqa: E402
import release_signals as rs  # noqa: E402
from promote_release_guard import (  # noqa: E402
    GoogleApi,
    Guard,
    GuardError,
    Superseded,
    _http_json,
    github_compare_status,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAILED = 2

BASELINE_MAX = timedelta(hours=24)
BASELINE_MIN = timedelta(hours=6)
DATADOG_API_KEY_SECRET = "release-verify-datadog-api-key"
DATADOG_APP_KEY_SECRET = "release-verify-datadog-app-key"
USER_AGENT = "orbit-release-verify/1"
KIT_DIR = Path(__file__).resolve().parent
REPO_ROOT = KIT_DIR.parent
PEOPLE_FILE = KIT_DIR / "release_people.json"


# --------------------------------------------------------------------------- Datadog


class DatadogClient:
    """Scalar metric queries (POST /api/v2/query/scalar) and other reads of the Datadog API."""

    def __init__(self, api_key: str, app_key: str, site: str = "datadoghq.com") -> None:
        self._headers = {
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Content-Type": "application/json",
        }
        self._base = f"https://api.{site}"

    def scalar(self, query: str, aggregator: str, start: datetime, end: datetime) -> dict[str, float]:
        """Return {group: value}; the group is the tag values joined by "|" ("*" when the query has no group)."""
        body = {
            "data": {
                "type": "scalar_request",
                "attributes": {
                    "from": int(start.timestamp() * 1000),
                    "to": int(end.timestamp() * 1000),
                    "queries": [{"data_source": "metrics", "name": "q", "query": query, "aggregator": aggregator}],
                },
            }
        }
        request = urllib.request.Request(
            f"{self._base}/api/v2/query/scalar",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=self._headers,
        )
        return parse_scalar(_http_json(request))

    def api(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> dict:
        """Any read of the Datadog API (release_signals: events, Error Tracking)."""
        url = self._base + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        return _http_json(urllib.request.Request(url, data=data, method=method, headers=self._headers))


def parse_scalar(payload: dict) -> dict[str, float]:
    columns = ((payload.get("data") or {}).get("attributes") or {}).get("columns") or []
    groups = [column for column in columns if column.get("type") == "group"]
    numbers = [column for column in columns if column.get("type") == "number"]
    if not numbers:
        return {}
    values = numbers[0].get("values") or []
    keys = []
    for index in range(len(values)):
        parts = []
        for column in groups:
            group_values = column.get("values") or []
            value = group_values[index] if index < len(group_values) else []
            parts.append(",".join(value) if isinstance(value, list) else str(value))
        keys.append("|".join(parts) or "*")
    return {key: float(value) for key, value in zip(keys, values) if value is not None}


class MetricsUnavailable(Exception):
    """No Datadog key that can read metrics; the metric checks cannot run."""


def _secret_manager(clients: rr.Clients, secret: str) -> str:
    try:
        return clients.runner(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={secret}",
                f"--project={clients.env.project}",
            ]
        ).strip()
    except rr.RecordError:
        return ""


def datadog_from_environment(clients: rr.Clients) -> DatadogClient:
    """Keys: DD_API_KEY/DD_APP_KEY, else Secret Manager; the API key also from the agent's cluster secret.

    The application key must hold the timeseries_query, events_read and
    error_tracking_read scopes. The agent's own application key does not.
    """
    api_key = os.environ.get("DD_API_KEY", "").strip() or _secret_manager(clients, DATADOG_API_KEY_SECRET)
    app_key = os.environ.get("DD_APP_KEY", "").strip() or _secret_manager(clients, DATADOG_APP_KEY_SECRET)
    if not api_key:
        try:
            secret = clients.kubectl_json("-n", "datadog", "get", "secret", "datadog", namespace=False)
            api_key = base64.b64decode((secret.get("data") or {}).get("api-key", "")).decode("utf-8").strip()
        except rr.RecordError:
            api_key = ""
    if not (api_key and app_key):
        raise MetricsUnavailable(
            f"no Datadog key: set DD_API_KEY/DD_APP_KEY or create the Secret Manager secret {DATADOG_APP_KEY_SECRET} "
            "(an application key with the timeseries_query scope)"
        )
    return DatadogClient(api_key, app_key, os.environ.get("DD_SITE", "datadoghq.com"))


# --------------------------------------------------------------------------- metrics


@dataclass
class Finding:
    metric: str
    scope: str
    baseline: float | None
    candidate: float | None
    baseline_samples: float
    candidate_samples: float
    limit: str
    status: str  # ok, exceeded, insufficient, within_spread, no_runtime_change
    unit: str = ""

    def line(self) -> str:
        def show(value: float | None) -> str:
            if value is None:
                return "n/a"
            return f"{value:.2%}" if self.unit == "rate" else f"{value:,.3g}"

        return (
            f"{self.metric} {self.scope}: {show(self.baseline)} -> {show(self.candidate)} "
            f"(n={self.baseline_samples:,.0f} -> {self.candidate_samples:,.0f}; limit {self.limit})"
        )


def describe_limit(metric: rc.Metric) -> str:
    if metric.unit == "rate":
        return f"> {metric.ratio:g}x baseline and > baseline + {metric.absolute * 100:g} pp"
    return f"> {metric.ratio:g}x baseline and > baseline + {metric.absolute:g} {metric.unit}"


def metric_values(
    dd: DatadogClient, metric: rc.Metric, start: datetime, end: datetime
) -> dict[str, tuple[float | None, float]]:
    """Per group: (value, samples). Rate: numerator / denominator; value: the value with its samples."""
    if metric.kind == "rate":
        numerator = dd.scalar(metric.numerator, "sum", start, end)
        denominator = dd.scalar(metric.denominator, "sum", start, end)
        return {
            group: ((numerator.get(group) or 0.0) / samples if samples > 0 else None, samples)
            for group, samples in denominator.items()
        }
    values = dd.scalar(metric.value, metric.value_aggregator, start, end)
    samples = dd.scalar(metric.samples, "sum", start, end)
    scale = 1000.0 if metric.value_ms else 1.0
    return {group: (values[group] / scale if group in values else None, count) for group, count in samples.items()}


def scope_of(metric: rc.Metric, group: str) -> str:
    return metric.scope if group == "*" else f"{metric.scope} {group}"


def judge(metric: rc.Metric, scope: str, base: tuple[float | None, float], cand: tuple[float | None, float]) -> Finding:
    (b_value, b_n), (c_value, c_n) = base, cand
    status = "insufficient"
    if b_value is not None and c_value is not None and b_n >= metric.min_samples and c_n >= metric.min_samples:
        exceeded = c_value > b_value * metric.ratio and c_value - b_value > metric.absolute
        status = "exceeded" if exceeded else "ok"
    return Finding(metric.name, scope, b_value, c_value, b_n, c_n, describe_limit(metric), status, metric.unit)


def compare(
    metrics: tuple[rc.Metric, ...],
    baseline: dict[str, dict[str, tuple[float | None, float]]],
    candidate: dict[str, dict[str, tuple[float | None, float]]],
) -> list[Finding]:
    """One finding per metric and group seen in the release window."""
    findings = []
    for metric in metrics:
        base, cand = baseline.get(metric.name) or {}, candidate.get(metric.name) or {}
        for group in sorted(cand):
            findings.append(judge(metric, scope_of(metric, group), base.get(group, (None, 0.0)), cand[group]))
    return findings


# A value swings with load and volume, release or not. On 2026-10-21 the hourly p95 of one
# deep-search step ran 6.0 to 28.9 s, and a release with no runtime code was rolled back at
# 1.19x the highest hour. An exceeded metric with `spread` counts only when it is also above
# SPREAD_MARGIN x the highest hourly value of the same group in the baseline.
SPREAD_MARGIN = 1.5
SPREAD_MIN_HOURS = 4
SPREAD_MAX_HOURS = 24


def baseline_hours(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The whole clock hours inside [start, end), newest first, at most SPREAD_MAX_HOURS."""
    hour = end.replace(minute=0, second=0, microsecond=0)
    out = []
    while hour - timedelta(hours=1) >= start and len(out) < SPREAD_MAX_HOURS:
        out.append((hour - timedelta(hours=1), hour))
        hour -= timedelta(hours=1)
    return out


def hourly_values(
    dd: DatadogClient, metrics: list[rc.Metric], start: datetime, end: datetime
) -> dict[str, dict[str, list[float]]]:
    """Per metric and scope: the hourly values of the baseline hours with enough samples.

    The same query as the window value, one clock hour at a time, so both use one aggregation.
    An hour needs a tenth of the metric's min_samples (at least 10).
    """
    out: dict[str, dict[str, list[float]]] = {metric.name: {} for metric in metrics}
    for hour_start, hour_end in baseline_hours(start, end):
        for metric in metrics:
            floor = max(10, metric.min_samples // 10)
            for group, (value, samples) in metric_values(dd, metric, hour_start, hour_end).items():
                if value is not None and samples >= floor:
                    out[metric.name].setdefault(scope_of(metric, group), []).append(value)
    return out


def apply_spread(findings: list[Finding], hourly: dict[str, dict[str, list[float]]]) -> None:
    """An exceeded value of a spread metric counts only above SPREAD_MARGIN x its scope's highest baseline hour.

    Within that: "within_spread". With fewer than SPREAD_MIN_HOURS usable baseline hours the
    comparison cannot be made: "no_spread", a warning, never a rollback.
    """
    for finding in findings:
        if finding.status != "exceeded" or finding.candidate is None or finding.metric not in hourly:
            continue
        hours = hourly[finding.metric].get(finding.scope) or []
        if len(hours) < SPREAD_MIN_HOURS:
            finding.status = "no_spread"
            finding.limit += f"; not judged: {len(hours)} usable baseline hours (need {SPREAD_MIN_HOURS})"
        elif finding.candidate <= SPREAD_MARGIN * max(hours):
            finding.status = "within_spread"
            finding.limit += (
                f"; not a regression: the baseline's hourly value ran {min(hours):.3g} to {max(hours):.3g} "
                f"over {len(hours)} h (limit {SPREAD_MARGIN:g}x the highest)"
            )


# Lines of a manifest that set only the Datadog version.
VERSION_LINE_RE = re.compile(
    r'DD_VERSION|tags\.datadoghq\.com/version|^\s*value:\s*"(unreleased|1\.0\.0)"\s*$|^\s*(#.*)?$'
)
# Lines of a deploy build file that are release tooling only (the kit's steps and comments).
RELEASE_BUILD_LINE_RE = re.compile(r"^\s*(#.*)?$|release/[\w./-]+|^\s*- \"?(RELEASE_|WEBHOOK_)[A-Z_]*=")
VERSION_PART_RE = re.compile(r"[|,]?DD_VERSION=[^\"|,\s]*")


def _changed_lines(patch: str) -> list[str]:
    """Added and removed lines, without the pairs that differ only by a DD_VERSION entry."""
    added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
    unpaired = [VERSION_PART_RE.sub("", line) for line in added]
    used = [False] * len(added)
    kept = []
    for line in removed:
        match = next((i for i, other in enumerate(unpaired) if not used[i] and other == line), None)
        if match is None:
            kept.append(line)
        else:
            used[match] = True
    return [line for line, paired in zip(added, used) if not paired] + kept


def runtime_files(code: rc.Code, files: list[dict]) -> list[str]:
    """The changed files (GitHub compare entries with patches) that can change how the service runs.

    Not runtime: the release kit (release/), tests, Markdown, CI and agent files,
    manifests whose changed lines only set the Datadog version, and deploy build
    lines of the kit's own steps. Everything else counts (unknown counts too).
    """
    out = []
    for item in files:
        path = item.get("filename", "")
        if path.startswith("release/") or path.startswith(code.non_runtime_prefixes) or path.endswith(".md"):
            continue
        if code.is_test(path):
            continue
        changed = _changed_lines(str(item.get("patch") or ""))
        if item.get("patch") and path.startswith("k8/") and all(VERSION_LINE_RE.search(line) for line in changed):
            continue
        if (
            item.get("patch")
            and path.startswith("cloudbuild")
            and all(RELEASE_BUILD_LINE_RE.search(line) for line in changed)
        ):
            continue
        out.append(path)
    return out


def runtime_change(service: rc.Service, record: dict, token: str | None, base: str | None = None) -> tuple[bool, str]:
    """(the commits since `base` change runtime code, why). Unknown counts as a runtime change.

    base: the commit before the oldest release judged together (default: this release's previous).
    """
    base, head = base or (record.get("previous") or {}).get("commit"), record.get("commit")
    if not token or not base or not head:
        return True, "the changed files are unknown"
    try:
        payload = rr.github_json(f"repos/{service.github_repo}/compare/{base}...{head}", token)
    except GuardError as error:
        return True, f"the changed files are unknown ({str(error)[:120]})"
    files = payload.get("files") or []
    if len(files) >= 300 or not files:
        return True, f"{len(files)} files listed"
    runtime = runtime_files(service.code, files)
    if runtime:
        return True, "it changes " + ", ".join(runtime[:5])
    return False, f"its {len(files)} changed files are release tooling, tests, docs or Datadog version lines"


def judge_metrics(
    service: rc.Service,
    dd: DatadogClient,
    record: dict,
    baseline: tuple[datetime, datetime],
    window: tuple[datetime, datetime],
    token: str | None,
    base_commit: str | None = None,
) -> list[Finding]:
    """compare() of the two windows, then the spread rule, then no rollback for releases without runtime code.

    base_commit: the commit before the oldest release judged together; the runtime-code rule
    covers every commit since it, so a docs-only release after an unjudged one is no excuse.
    """
    metrics = service.metrics
    findings = compare(
        metrics,
        {metric.name: metric_values(dd, metric, *baseline) for metric in metrics},
        {metric.name: metric_values(dd, metric, *window) for metric in metrics},
    )
    spread = [
        metric
        for metric in metrics
        if metric.spread and any(f.metric == metric.name and f.status == "exceeded" for f in findings)
    ]
    if spread:
        try:
            hourly = hourly_values(dd, spread, *baseline)
        except GuardError as error:
            print(f"WARNING: the hourly spread could not be read: {error}", file=sys.stderr, flush=True)
            hourly = {metric.name: {} for metric in spread}  # every exceeded spread finding becomes no_spread
        apply_spread(findings, hourly)
    left = [f for f in findings if f.status == "exceeded"]
    if left:
        runtime, why = runtime_change(service, record, token, base_commit)
        if not runtime:
            for finding in left:
                # A release without runtime code cannot change these values: a warning, never a rollback.
                finding.status = "no_runtime_change"
                finding.limit += f"; not judged: {why}"
    return findings


# --------------------------------------------------------------------------- smoke checks


@dataclass
class Check:
    name: str
    status: str  # passed, failed, skipped
    details: list[str] = field(default_factory=list)


# A Deployment fails the check when fewer than this share of its desired
# replicas are available. Rollouts are awaited first; the margin is for HPA
# scale-ups whose new pods wait for nodes.
MIN_AVAILABLE_SHARE = 0.8


def check_images(record: dict, snapshot: dict[str, dict], targets: dict[str, str] | None = None) -> Check:
    """Every Deployment runs its target digest, rollouts are complete and enough pods are available.

    The target is the release image, or, when targets is given (a rollback to
    a per-Deployment snapshot), only the listed Deployments with their own digests.
    """
    expected = rr.digest_of((record.get("images") or {}).get(rr.IMAGE_ROLE))
    problems = []
    for name, workload in sorted(snapshot.items()):
        if targets is not None and name not in targets:
            continue
        want = targets[name] if targets is not None else expected
        if workload.get("digest") != want:
            problems.append(f"{name}: set to {workload.get('digest') or workload.get('image')}, release is {want}")
            continue
        if not workload["rollout_current"] or workload["updated_replicas"] < workload["replicas"]:
            problems.append(
                f"{name}: rollout not complete ({workload['updated_replicas']}/{workload['replicas']} updated)"
            )
        stale = [digest for digest in workload["running_digests"] if digest != want]
        if stale:
            problems.append(f"{name}: live pods still run {', '.join(stale)}")
        if workload["bad_pods"]:
            problems.append(f"{name}: {'; '.join(workload['bad_pods'][:5])}")
        replicas = workload["replicas"]
        if replicas > 0 and (
            not workload["running_digests"]
            or workload["available_replicas"] < math.ceil(replicas * MIN_AVAILABLE_SHARE)
        ):
            problems.append(f"{name}: {workload['available_replicas']}/{replicas} replicas available")
    checked = len(snapshot) if targets is None else len([name for name in snapshot if name in targets])
    return Check("images", "failed" if problems else "passed", [f"{checked} Deployments checked", *problems])


HttpGet = Callable[[urllib.request.Request], tuple[int, bytes]]


# A page's <title> can sit past the first 4 KiB of a server-rendered page (orbitsearch.com/search: byte 4,461).
HTTP_READ_BYTES = 65536


def http_get(request: urllib.request.Request, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
    """Send a GET and return (status, first bytes of the body); HTTP errors are statuses, not exceptions."""
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            return response.status, response.read(HTTP_READ_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, error.read(HTTP_READ_BYTES)


class KubeApi:
    """GETs through the Kubernetes API server's pod proxy, with the gcloud identity.

    The proxy reaches any pod port with no tool inside the image (Go and
    distroless images have no shell), and returns the pod's real HTTP status.
    The build identity needs container.pods.proxy (roles/container.developer has it).
    """

    def __init__(self, clients: rr.Clients, token: Callable[[], str]) -> None:
        config = json.loads(
            clients.runner([*clients.kubectl_base(), "config", "view", "--raw", "--minify", "-o", "json"])
        )
        cluster = config["clusters"][0]["cluster"]
        self.server = cluster["server"].rstrip("/")
        authority = cluster.get("certificate-authority-data")
        self.context = (
            ssl.create_default_context(cadata=base64.b64decode(authority).decode("ascii"))
            if authority
            else ssl.create_default_context()
        )
        self.token = token

    def pod_get(self, namespace: str, pod: str, port: int, path: str) -> tuple[int, bytes]:
        url = f"{self.server}/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy{path}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token()}", "User-Agent": USER_AGENT}
        )
        return http_get(request, self.context)


PodGet = Callable[[str, str, int, str], tuple[int, bytes]]


def _expect_ok(status: int, body: bytes, check: dict) -> tuple[bool, str]:
    expected = check.get("expect_status") or [200, 204]
    expected = expected if isinstance(expected, list) else [expected]
    text = body.decode("utf-8", "replace")
    if status not in expected:
        return False, f"HTTP {status} (expected {', '.join(str(code) for code in expected)}): {text[:120]!r}"
    if contains := check.get("contains"):
        if contains not in text:
            return False, f"HTTP {status}, the body has no {contains!r}: {text[:120]!r}"
    if expect_json := check.get("expect_json"):
        try:
            payload = json.loads(text)
        except ValueError:
            return False, f"HTTP {status}, the body is not JSON: {text[:120]!r}"
        wrong = {key: payload.get(key) for key, value in expect_json.items() if payload.get(key) != value}
        if wrong:
            return False, f"HTTP {status}, unexpected fields {wrong}"
    return True, f"HTTP {status} {text[:60]!r}"


def _deployment(env: rc.Environment, name: str) -> rc.Deployment:
    for item in env.deployments:
        if item.name == name:
            return item
    raise rc.ConfigError(f"smoke check names deployment {name!r}, which {env.service} {env.name} does not deploy")


def check_http_pod(clients: rr.Clients, check: dict, pod_get: PodGet) -> Check:
    """A GET on every live pod of a Deployment (or one pod), through the Kubernetes API pod proxy."""
    name = str(check.get("name") or f"http {check['path']}")
    deployment = _deployment(clients.env, check["deployment"])
    pods = rr.live_pods(clients, deployment)
    if not pods:
        return Check(name, "failed", [f"no running {deployment.name} pod"])
    if not check.get("all_pods", True):
        pods = pods[:1]
    details, failed = [], False
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        try:
            status, body = pod_get(clients.env.namespace, pod_name, int(check["port"]), str(check["path"]))
            ok, line = _expect_ok(status, body, check)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            ok, line = False, f"error: {error}"
        failed = failed or not ok
        details.append(f"{pod_name}: GET {check['path']}: {line}")
    return Check(name, "failed" if failed else "passed", [f"{len(pods)} {deployment.name} pods", *details])


def check_http_public(check: dict, get: HttpGet = http_get) -> Check:
    """A GET on a public URL of the service (through Cloudflare and the load balancer)."""
    name = str(check.get("name") or f"public {check['url']}")
    headers = {"Accept": "*/*", "User-Agent": USER_AGENT}
    started = time.monotonic()
    try:
        status, body = get(urllib.request.Request(str(check["url"]), headers=headers))
        ok, line = _expect_ok(status, body, check)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        ok, line = False, f"error: {error}"
    elapsed_ms = (time.monotonic() - started) * 1000
    return Check(name, "passed" if ok else "failed", [f"GET {check['url']}: {line}, {elapsed_ms:.0f} ms"])


def check_pods_ready(clients: rr.Clients, check: dict, snapshot: dict[str, dict]) -> Check:
    """Every live pod of a Deployment is Ready, and at least min_share of the desired replicas are."""
    deployment = _deployment(clients.env, check["deployment"])
    name = str(check.get("name") or f"{deployment.name} ready")
    workload = snapshot.get(f"deployment/{deployment.name}")
    if workload is None:
        return Check(name, "failed", [f"deployment/{deployment.name} is missing"])
    share = float(check.get("min_share") or MIN_AVAILABLE_SHARE)
    replicas, live, ready = workload["replicas"], workload["live_pods"], workload["ready_pods"]
    failed = ready < live or (replicas > 0 and ready < math.ceil(replicas * share))
    return Check(name, "failed" if failed else "passed", [f"{ready}/{live} live pods Ready, {replicas} desired"])


def smoke_checks(
    clients: rr.Clients, snapshot: dict[str, dict], *, pod_get: PodGet | None, get: HttpGet = http_get
) -> list[Check]:
    """The service's own smoke checks of this environment (service config `smoke`)."""
    out = []
    for check in clients.env.smoke:
        kind = check.get("type")
        if kind == "http_pod":
            if pod_get is None:
                out.append(Check(str(check.get("name") or "http_pod"), "failed", ["no Kubernetes API client"]))
            else:
                out.append(check_http_pod(clients, check, pod_get))
        elif kind == "http_public":
            out.append(check_http_public(check, get))
        elif kind == "pods_ready":
            out.append(check_pods_ready(clients, check, snapshot))
        else:
            raise rc.ConfigError(f"unknown smoke check type {kind!r}")
    return out


# --------------------------------------------------------------------------- windows and verdict


def windows(record: dict, now: datetime) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime], str]:
    """Baseline = before the deploy, back to the previous release (6 h to 24 h); candidate = after it."""
    times = record.get("times") or {}
    started = rr.parse_utc(times["deploy_started"])
    finished = rr.parse_utc(times.get("deploy_finished") or times["deploy_started"])
    baseline_start = started - BASELINE_MAX
    note = "baseline is the 24 h before the deploy"
    previous_finished = (record.get("previous") or {}).get("deploy_finished")
    if previous_finished:
        prev = rr.parse_utc(previous_finished)
        if prev > baseline_start:
            baseline_start, note = prev, "baseline is the previous release, from its deploy to this deploy"
    if started - baseline_start < BASELINE_MIN:
        baseline_start = started - BASELINE_MIN
        note = (
            "the previous release ran less than 6 h; the baseline is the 6 h before the deploy (more than one release)"
        )
    return (baseline_start, started), (finished, now), note


def suspected_prs(service: rc.Service, record: dict, regressions: list[dict], token: str | None) -> list[dict]:
    """Rank the release's commits.

    A new error traced to a commit (release_signals.trace_suspects) puts that
    commit first; it can belong to an earlier unjudged release. The rest rank by
    source changes, more so when paths name a regressed scope.
    """
    traced: dict[str, dict] = {}
    for item in regressions:
        top = (item.get("suspects") or [None])[0]
        if top and (top["sha"] not in traced or top["score"] > traced[top["sha"]]["score"]):
            traced[top["sha"]] = top
    words = set()
    for item in regressions:
        scope = str(item.get("scope", ""))
        words.update(part.lower() for part in re.split(r"[:_,|\s/.-]+", scope) if len(part) > 3)
    ranked = []
    for commit in (record.get("commits") or [])[:40]:
        files: list[str] = []
        if token:
            try:
                payload = rr.github_json(f"repos/{service.github_repo}/commits/{commit['sha']}", token)
                files = [item["filename"] for item in payload.get("files") or []]
            except GuardError:
                files = []
        source = [path for path in files if service.code.is_source(path)]
        deploy = [path for path in files if service.code.is_deploy(path)]
        matched = sorted({word for word in words for path in source + [commit["title"]] if word in path.lower()})
        score = (2 if source else 0) + min(len(source), 10) / 10 + (1 if deploy else 0) + len(matched)
        reason = f"{len(source)} source files" + (f", {len(deploy)} deploy files" if deploy else "")
        if matched:
            reason += f"; matches {', '.join(matched)}"
        ranked.append({**commit, "score": score if files else 1, "reason": reason if files else "files unknown"})
    known = {item["sha"] for item in ranked}
    ranked += [
        {key: top.get(key) for key in ("sha", "title", "pr", "author")}
        for sha, top in traced.items()
        if sha not in known
    ]
    for item in ranked:
        if item["sha"] in traced:
            top = traced[item["sha"]]
            item.update(score=top["score"], reason=top["reason"], traced=True, pr=item.get("pr") or top.get("pr"))
    return sorted(ranked, key=lambda item: (not item.get("traced"), -item["score"]))


def rollback_suggestion(
    service: rc.Service,
    record: dict,
    regressions: list[dict],
    phase: str,
    token: str | None,
    target: dict | None = None,
) -> dict:
    """target: an earlier release than record["previous"] (a new error traced to an older unjudged release)."""
    previous = target or record.get("previous") or {}
    command = service.kit_command("release_rollback.py") + rollback_to_flag(target)
    return {
        "created_at": rr.utc_now(),
        "phase": phase,
        "summary": f"{len(regressions)} problem(s): " + "; ".join(item["text"] for item in regressions[:3]),
        "regressions": regressions,
        "suspected_prs": suspected_prs(service, record, regressions, token)[:10],
        "target": {
            "release_id": previous.get("release_id"),
            "commit": previous.get("commit"),
            "images": previous.get("images"),
            **({"rollback_to": target["rollback_to"]} if target and target.get("rollback_to") else {}),
        },
        "how": [
            f"{command} plan     # dry run: prints every Deployment change",
            f"{command} run --reason '<why>'     # starts the rollback build",
        ],
        "note": (
            "The watch-window build starts the rollback itself; a human can also start it."
            if service.auto_rollback
            else "Tier check_only: a human starts the rollback."
        ),
    }


def rollback_to_flag(target: dict | None) -> str:
    """The --to of a rollback command: empty for the default ("previous"), else the suggested target."""
    to = (target or {}).get("rollback_to") or (target or {}).get("release_id")
    return f" --to {to}" if to else ""


def new_error_regression(problem: dict) -> dict:
    """A release_signals problem as a regression (clear suspect) or a warning (none)."""
    where = f"; written by {problem['origins'][0]}" if problem.get("origins") else ""
    suspects = problem.get("suspects") or []
    if problem["clear"]:
        top = suspects[0]
        name = f"#{top['pr']}" if top.get("pr") else top["sha"][:9]
        blame = f"; suspect {name} {rr.PR_IN_TITLE_RE.sub('', top['title']).strip()[:80]} ({top['reason'][:160]})"
    else:
        names = [f"#{item['pr']}" if item.get("pr") else item["sha"][:9] for item in suspects[:3]]
        blame = "; no clear suspect" + (f" (top commits {', '.join(names)})" if names else "")
    return {
        "kind": "new_error",
        "name": problem["key"],
        "text": f"{problem['text']}{where}{blame}",
        "link": problem.get("link"),
        "suspects": suspects[:3] if problem["clear"] else [],
        "candidates": suspects[:3],
    }


def evaluate(
    service: rc.Service,
    record: dict,
    *,
    phase: str,
    clients: rr.Clients,
    dd: DatadogClient | None,
    now: datetime,
    metrics_error: str | None = None,
    wait_rollouts_seconds: int = 0,
    store: rr.Store | None = None,
    token: str | None = None,
    logs: rs.CloudLogging | None = None,
    source_root: Path | None = None,
    pod_get: PodGet | None = None,
    get: HttpGet = http_get,
    force_fail: str = "",
) -> dict:
    """Run the checks of one phase and return the result block for record["checks"].

    With a store, the window phase also runs the new-error checks (release_signals):
    a new error with a clear suspect is a regression, one without is a warning.
    force_fail (test runs) adds a failed check, to prove that a failure stops the promote.
    """
    checks = []
    if wait_rollouts_seconds > 0:
        names = [item.name for item in clients.env.deployments]
        failures = rr.wait_rollouts(clients, names, wait_rollouts_seconds)
        checks.append(Check("rollouts", "failed" if failures else "passed", [f"{len(names)} Deployments", *failures]))
    snapshot = rr.snapshot_workloads(clients)
    checks.insert(0, check_images(record, snapshot))
    if phase == "deploy":
        checks += smoke_checks(clients, snapshot, pod_get=pod_get, get=get)
        if force_fail:
            checks.append(Check("forced_failure", "failed", [f"forced failure (test run): {force_fail}"]))
    elif metrics_error and service.metrics:
        checks.append(Check("metrics", "skipped", [metrics_error]))
    findings: list[Finding] = []
    new_errors: dict | None = None
    window_note = ""
    candidate_minutes = 0.0
    # Metrics start at the end of the deploy: during a rollout old and new pods serve together.
    chain = [record]
    if phase == "window":
        if store is not None:
            # Releases that a newer one replaced before any checkpoint judged them are judged with it:
            # the baseline is before the oldest, the window is what runs now.
            chain = rs.release_scope(store, record, now).releases
        oldest = chain[-1]
        (b_start, b_end), _, window_note = windows(oldest, now)
        _, (c_start, c_end), _ = windows(record, now)
        if len(chain) > 1:
            window_note += f"; judged with {len(chain) - 1} earlier unjudged release(s)"
        candidate_minutes = max(0.0, (c_end - c_start).total_seconds() / 60)
        if dd is not None and service.metrics and candidate_minutes >= 1:
            try:
                findings = judge_metrics(
                    service,
                    dd,
                    record,
                    (b_start, b_end),
                    (c_start, c_end),
                    token,
                    base_commit=(oldest.get("previous") or {}).get("commit"),
                )
            except GuardError as error:
                checks.append(Check("metrics", "skipped", [f"Datadog query failed: {error}"]))
        if store is not None and candidate_minutes >= 1:
            try:
                new_errors = rs.check_new_errors(
                    record,
                    service=service,
                    store=store,
                    dd=dd,
                    logs=logs,
                    now=now,
                    token=token,
                    source_root=source_root or REPO_ROOT,
                )
            except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
                checks.append(Check("new_errors", "skipped", [f"new-error checks could not run: {error}"]))
        window_note += (
            f"; baseline {b_start:%m-%d %H:%M}-{b_end:%m-%d %H:%M} UTC, "
            f"release {c_start:%m-%d %H:%M}-{c_end:%H:%M} UTC ({candidate_minutes:.0f} min)"
        )
    exceeded = [item for item in findings if item.status == "exceeded"]
    regressions = [
        {"kind": "check", "name": c.name, "text": f"{c.name}: {'; '.join(c.details[1:] or c.details)}"}
        for c in checks
        if c.status == "failed"
    ]
    regressions += [{"kind": "metric", **asdict(item), "text": item.line()} for item in exceeded]
    problems = (new_errors or {}).get("problems") or []
    regressions += [new_error_regression(problem) for problem in problems if problem["clear"]]
    # Without a clear suspect, only an error that some commit's code touches asks a human. One that no
    # code links to (a provider outage, new data) stays in the record; its monitors page on their own.
    warnings = [
        new_error_regression(problem)
        for problem in problems
        if not problem["clear"] and problem.get("suspects") and problem["suspects"][0].get("linked")
    ]
    warnings += [
        {
            "kind": "metric",
            "name": f"{item.metric}:{item.scope}",
            "text": f"{item.line()}; the release changes no runtime code, so it does not roll back",
            "suspects": [],
            "candidates": [],
        }
        for item in findings
        if item.status == "no_runtime_change"
    ]
    warnings += [
        {
            "kind": "metric",
            "name": f"{item.metric}:{item.scope}",
            "text": f"{item.line()}; the baseline's hourly spread could not be read, so it does not roll back",
            "suspects": [],
            "candidates": [],
        }
        for item in findings
        if item.status == "no_spread"
    ]
    judged_metrics = {metric.name for metric in service.metrics if metric.judged}
    judged = sum(
        item.candidate_samples for item in findings if item.metric in judged_metrics and item.status != "insufficient"
    )
    parts = (new_errors or {}).get("parts") or {}
    signals_checked = [name for name, part in parts.items() if part.get("status") == "checked"]
    return {
        "phase": phase,
        "at": rr.utc_now(),
        "evaluated_until": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": window_note,
        "dashboard": service.dashboard,
        "checks": [asdict(c) for c in checks],
        "findings": [asdict(item) for item in findings if item.status != "insufficient"],
        "insufficient": sorted({f"{item.metric} {item.scope}" for item in findings if item.status == "insufficient"}),
        "judged": judged,
        "signals_checked": signals_checked,
        "candidate_minutes": round(candidate_minutes, 1),
        "regressions": regressions,
        "new_errors": new_errors,
        "warnings": warnings,
        "chain": [item["release_id"] for item in chain],
        "result": "failed" if regressions else "passed",
    }


def final_verdict(service: rc.Service, result: dict) -> tuple[str, str]:
    """(status, reason) of a passed last checkpoint: "passed" only when enough was judged.

    With judged metrics configured: their samples must reach the service's min_judged
    (at least 1). Without them: the new-error log check must have run.
    """
    if any(metric.judged for metric in service.metrics):
        if result["judged"] >= max(1, service.min_judged):
            return "passed", ""
        skipped = [c for c in result.get("checks", []) if c["name"] == "metrics" and c["status"] == "skipped"]
        return "inconclusive", skipped[0]["details"][0] if skipped else "not enough traffic to judge"
    if "logs" in result.get("signals_checked", []):
        return "passed", ""
    return "inconclusive", "the new-error log check did not run"


def apply_result(
    service: rc.Service, store: rr.Store, record: dict, result: dict, *, final: bool, token: str | None
) -> dict:
    """Store a check result; a failure sets the verdict and the rollback suggestion.

    The change applies to the record as stored at write time, so fields that
    another writer set meanwhile (a rollback's status) stay.
    """
    key = result["phase"] if result["phase"] == "deploy" else f"window_{result['candidate_minutes']:.0f}m"
    suggestion = None
    # Dev has nothing to roll back: a failed dev verify only stops the promote.
    if result["result"] == "failed" and record["environment"] == "prod":
        target = None
        new_errors = result.get("new_errors") or {}
        if any(item["kind"] == "new_error" for item in result["regressions"]):
            # A new error traced to an earlier unjudged release rolls back past that release.
            target = rs.rollback_target(record, store, new_errors.get("problems") or [], new_errors.get("scope") or {})
        chain = result.get("chain") or []
        if target is None and len(chain) > 1 and any(item["kind"] != "new_error" for item in result["regressions"]):
            # A metric judged over several unjudged releases cannot tell which one regressed:
            # roll back to what ran before the oldest of them.
            oldest = rs._read(store, chain[-1]) or {}
            previous = oldest.get("previous") or {}
            target = {
                "release_id": previous.get("release_id"),
                "commit": previous.get("commit"),
                "images": previous.get("images"),
                "rollback_to": f"previous-of:{chain[-1]}",
            }
        suggestion = rollback_suggestion(service, record, result["regressions"], key, token, target)
    fresh: list[dict] = []

    def change(current: dict) -> None:
        current.setdefault("checks", {})[key] = result
        # Each warning goes to Slack once per release; one whose post failed is sent again at the next checkpoint.
        known = current.setdefault("warnings", {})
        fresh.clear()
        for warning in result.get("warnings") or []:
            entry = known.setdefault(
                warning["name"], {"at": result["at"], "phase": key, "text": warning["text"], "posted": False}
            )
            if not entry.get("posted"):
                fresh.append(warning)
        verdict = current.get("verdict") or {"status": "pending"}
        if result["result"] == "failed" and verdict.get("status") != "failed":
            verdict = {
                "status": "failed",
                "at": result["at"],
                "phase": key,
                "reasons": [r["text"] for r in result["regressions"]],
            }
            current["rollback_suggestion"] = suggestion
        elif verdict.get("status") != "failed":
            if not final:
                verdict = {"status": "pending", "at": result["at"], "phase": key}
            else:
                status, reason = final_verdict(service, result)
                verdict = {"status": status, "at": result["at"], "phase": key, "judged": result["judged"]}
                if reason:
                    verdict["reason"] = reason
                if current.get("warnings"):
                    verdict["warnings"] = len(current["warnings"])
        current["verdict"] = verdict

    saved = rr.update_record(store, record["release_id"], change, f"verify_{key}", {"result": result["result"]})
    result["fresh_warnings"] = list(fresh)
    return saved


def post_warnings(service: rc.Service, store: rr.Store, record: dict, warnings: list[dict]) -> None:
    """Post the fresh warnings; mark them posted only when Slack took them. Never raises."""
    if not warnings or not rr.post_slack(warning_line(service, record, warnings, store)):
        return
    names = {warning["name"] for warning in warnings}

    def mark(current: dict) -> None:
        for name, entry in (current.get("warnings") or {}).items():
            if name in names:
                entry["posted"] = True

    try:
        rr.update_record(store, record["release_id"], mark, "warnings_posted", {"count": len(names)})
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        print(f"WARNING: could not mark the warnings posted: {error}", file=sys.stderr, flush=True)


def warning_line(service: rc.Service, record: dict, warnings: list[dict], store: rr.Store) -> str:
    """Slack: findings that do not roll back (no clear suspect, no runtime code). A human decides."""
    where = store.location(rr.record_name(record["release_id"]))
    items = " | ".join(_clip(item["text"], 300) for item in warnings[:3])
    return (
        f":warning: {service.title} release-verify {record['commit'][:9]}: {len(warnings)} finding(s) that do not "
        f"roll back on their own: {items}. Please look; roll back with "
        f"{service.kit_command('release_rollback.py')} run --reason '<why>' if the release caused it. Record {where}"
    )


def inconclusive_parts(result: dict) -> str:
    parts = ((result.get("new_errors") or {}).get("parts")) or {}
    names = [name for name, part in sorted(parts.items()) if part.get("status") == "inconclusive"]
    return f" New-error checks not run: {', '.join(names)}." if names else ""


def slack_line(service: rc.Service, record: dict, result: dict, store: rr.Store) -> str:
    title = service.title
    where = store.location(rr.record_name(record["release_id"]))
    short = record["commit"][:9]
    verdict = record.get("verdict") or {}
    status = verdict.get("status")
    if status == "failed":
        suggestion = record.get("rollback_suggestion") or {}
        first = suggestion.get("summary", "")
        target = (suggestion.get("target") or {}).get("commit") or (record.get("previous") or {}).get("commit")
        follow = "an automatic rollback follows" if service.auto_rollback else "roll back by hand (tier check_only)"
        return (
            f":rotating_light: {title} release-verify {short} FAILED ({result['phase']}): {first[:300]} "
            f"Rollback target {(target or 'unknown')[:9]}; {follow}.{inconclusive_parts(result)} Record {where}"
        )
    if status == "passed":
        judged = f" ({result['judged']:,.0f} samples judged)" if result.get("judged") else ""
        return f":white_check_mark: {title} release-verify {short} passed{judged}.{inconclusive_parts(result)} Record {where}"
    if status == "inconclusive":
        return (
            f":grey_question: {title} release-verify {short} inconclusive: {str(verdict.get('reason'))[:160]}."
            f"{inconclusive_parts(result)} Record {where}"
        )
    if result["phase"] == "deploy":
        minutes = " and ".join(f"+{value}" for value in service.checkpoints)
        return f":mag: {title} release-verify {short} deploy checks passed; window checks follow at {minutes} min. Record {where}"
    return f":mag: {title} release-verify {short} window checks at +{result['candidate_minutes']:.0f} min passed. Record {where}"


def print_result(result: dict) -> None:
    print(f"== release-verify {result['phase']}: {result['result']}", flush=True)
    new_errors = result.get("new_errors") or {}
    if new_errors:
        scope = new_errors.get("scope") or {}
        print(f"   new errors: releases {', '.join(scope.get('releases') or [])}; window {scope.get('window')}")
        for note in scope.get("notes") or []:
            print(f"      {note}")
        for name, part in sorted((new_errors.get("parts") or {}).items()):
            print(f"   new errors {name}: {part.get('status')} {part.get('found', '')} {part.get('detail', '')[:200]}")
        for problem in new_errors.get("problems") or []:
            print(f"   {'FAILS' if problem['clear'] else 'warns'}  {problem['text'][:300]}")
            for suspect in problem.get("suspects")[:3]:
                print(
                    f"      {suspect['score']:>3} #{suspect.get('pr')} {suspect['sha'][:9]} {suspect['reason'][:160]}"
                )
    if result.get("window"):
        print(f"   window: {result['window']}")
    for check in result["checks"]:
        print(f"   check {check['name']}: {check['status']}")
        for line in check["details"][:12]:
            print(f"      {line}")
    for finding in result["findings"]:
        print(f"   {finding['status']:>8}  {Finding(**finding).line()}")
    if result["insufficient"]:
        print(f"   not judged (too few samples): {len(result['insufficient'])} scopes")


# --------------------------------------------------------------------------- commands


def _now(args: argparse.Namespace) -> datetime:
    return rr.parse_utc(args.now) if args.now else datetime.now(rr.UTC)


def _datadog(service: rc.Service, clients: rr.Clients) -> tuple[DatadogClient | None, str | None]:
    signals = service.signals
    if not (service.metrics or signals.monitors or signals.error_tracking or signals.failure_series):
        return None, None
    try:
        return datadog_from_environment(clients), None
    except MetricsUnavailable as error:
        print(f"WARNING: {error}", file=sys.stderr, flush=True)
        return None, str(error)


def _token(env: rc.Environment) -> str | None:
    try:
        return rr.github_token(env)
    except GuardError as error:
        print(f"WARNING: no GitHub token, suspected PRs carry no file lists: {error}", file=sys.stderr)
        return None


def _pod_get(clients: rr.Clients, env: rc.Environment) -> PodGet | None:
    if not any(check.get("type") == "http_pod" for check in env.smoke):
        return None
    try:
        return KubeApi(clients, GoogleApi(env.project, env.region).access_token).pod_get
    except (rr.RecordError, KeyError, IndexError, ValueError, ssl.SSLError) as error:
        print(f"WARNING: no Kubernetes API client for the pod checks: {error}", file=sys.stderr, flush=True)
        return None


def cmd_check(
    args: argparse.Namespace, service: rc.Service, env: rc.Environment, store: rr.Store, clients: rr.Clients
) -> int:
    record = rr.read_record(store, args.release_id)
    dd, metrics_error, logs = None, None, None
    token = _token(env)
    if args.phase == "window" and not args.no_metrics:
        dd, metrics_error = _datadog(service, clients)
        logs = rs.CloudLogging(env.project, GoogleApi(env.project, env.region).access_token)
    result = evaluate(
        service,
        record,
        phase=args.phase,
        clients=clients,
        dd=dd,
        now=_now(args),
        metrics_error=metrics_error,
        wait_rollouts_seconds=args.wait_rollouts_seconds,
        store=store if args.phase == "window" else None,
        token=token,
        logs=logs,
        pod_get=_pod_get(clients, env) if args.phase == "deploy" else None,
        force_fail=args.force_fail,
    )
    print_result(result)
    # The marker comes from the result itself, so a failed record write cannot hide a failed check.
    if args.failed_marker and result["result"] == "failed":
        Path(args.failed_marker).write_text("true\n", encoding="utf-8")
    saved = apply_result(service, store, record, result, final=args.final, token=token)
    if env.name == "dev":
        superseded = newer_dev_build(env, args.build_id) if result["result"] == "failed" else None
        saved = finish_dev_verify(
            service,
            store,
            saved,
            result,
            build_url=args.build_url,
            test_run=bool(args.force_fail),
            token=token,
            superseded_by=superseded,
        )
        if args.failed_marker and saved["verdict"]["status"] != "failed":
            Path(args.failed_marker).unlink(missing_ok=True)  # superseded: nothing to promote, not red
        return EXIT_FAILED if saved["verdict"]["status"] == "failed" else EXIT_OK
    if args.force_fail and saved["verdict"]["status"] == "failed":
        rr.post_slack(
            f"{TEST_RUN_LINE}. A test of {service.title} release-verify forced a failure for {record['commit'][:9]}; "
            "the window build of a test run never rolls back. No action needed."
        )
    else:
        rr.post_slack(slack_line(service, saved, result, store))
    if args.failed_marker and saved["verdict"]["status"] == "failed":
        Path(args.failed_marker).write_text("true\n", encoding="utf-8")
    return EXIT_FAILED if saved["verdict"]["status"] == "failed" else EXIT_OK


# --------------------------------------------------------------------------- dev verify

SLACK_SECTION_LIMIT = 3000
TEST_RUN_LINE = ":test_tube: TEST RUN, nothing is wrong"


def _point_verified(store: rr.Store, env: rc.Environment, record: dict, token: str | None) -> None:
    """Move the newest-passed pointer to this commit when it is on the release branch and newer.

    Only release-branch commits can be promoted, so a test run of a branch commit never moves it.
    A known commit that is not an ancestor of this one (a branch commit) is replaced; an older
    commit never replaces a newer one. When GitHub cannot answer, the pointer stays.
    """
    commit = record["commit"]
    try:
        if not token:
            raise GuardError("no GitHub token")
        if github_compare_status(env.github_repo, env.release_branch, commit, token) not in ("identical", "behind"):
            print(f"Newest verified commit unchanged: {commit[:9]} is not on {env.release_branch}", flush=True)
            return
    except GuardError as error:
        print(f"WARNING: newest verified commit unchanged: cannot check {commit[:9]} ({error})", file=sys.stderr)
        return

    def change(current: dict | None) -> dict:
        known = (current or {}).get("commit")
        if known and known != commit:
            # A re-run of an older commit must never move the pointer back.
            try:
                status = github_compare_status(env.github_repo, known, commit, token)
            except GuardError as error:
                print(
                    f"WARNING: newest verified commit stays {known[:9]}: cannot order {commit[:9]} ({error})",
                    file=sys.stderr,
                    flush=True,
                )
                return current
            if status not in ("ahead", "diverged"):
                return current  # an older commit of the release branch
        return {
            "commit": commit,
            "release_id": record["release_id"],
            "images": record.get("images"),
            "updated_at": rr.utc_now(),
        }

    rr.update_object(store, rg.DEV_VERIFIED_POINTER, change)


def newer_dev_build(env: rc.Environment, build_id: str) -> str | None:
    """The newer dev build that replaced this one during its verify (it may change dev under the
    checks), or None. Uses the dev single-flight rule (promote_release_guard.Guard)."""
    if not build_id:
        return None
    guard = Guard(
        api=GoogleApi(env.project, env.region),
        build_id=build_id,
        commit="",
        peer_triggers=env.deploy_triggers,
        promoting_trigger=env.deploy_trigger,
    )
    try:
        guard.check_superseded(guard.api.get_build(build_id))
    except Superseded as reason:
        return str(reason)
    except (GuardError, OSError) as error:
        print(f"WARNING: could not check for a newer dev build: {error}", file=sys.stderr, flush=True)
    return None


def finish_dev_verify(
    service: rc.Service,
    store: rr.Store,
    record: dict,
    result: dict,
    *,
    build_url: str,
    test_run: bool = False,
    token: str | None = None,
    superseded_by: str | None = None,
) -> dict:
    """Dev verify is the deploy phase only: set the final verdict, index it by commit, alert on a failure.

    The index keeps the image digest that dev verify checked; the prod build must deploy exactly
    that one. A pass posts nothing here; the promote step posts one line with what happened next.
    A failure while a newer dev build replaced this one (superseded_by) is "superseded": no
    alert, the newer commit promotes instead. A test run alerts with a TEST RUN header, no mentions.
    """
    failed = result["result"] == "failed"
    status = "passed"
    if failed:
        status = "superseded" if superseded_by else "failed"
    verdict = {
        "status": status,
        "at": result["at"],
        "phase": "dev_verify",
        **({"reasons": [r["text"] for r in result["regressions"]]} if failed else {}),
        **({"superseded_by": superseded_by} if status == "superseded" else {}),
    }

    def change(current: dict) -> None:
        current["verdict"] = verdict

    saved = rr.update_record(store, record["release_id"], change, "dev_verify", {"result": verdict["status"]})
    rr.update_object(
        store,
        rg.dev_commit_index(saved["commit"]),
        lambda _: {
            "commit": saved["commit"],
            "release_id": saved["release_id"],
            "verdict": verdict,
            "images": saved.get("images"),
            "build": saved.get("build"),
            "updated_at": rr.utc_now(),
        },
    )
    if status == "passed":
        _point_verified(store, service.dev or service.prod, saved, token)
    elif status == "superseded":
        print(f"Dev verify superseded, nothing to promote: {superseded_by}", flush=True)
    else:
        location = store.location(rr.record_name(saved["release_id"]))
        rr.post_slack(*dev_verify_alert(service, saved, build_url, location, test_run))
    return saved


def load_people(path: Path | None = None) -> dict:
    """GitHub login -> Slack id map and fallback mentions; empty when the file is missing or broken."""
    try:
        people = json.loads((path or PEOPLE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"slack_ids": {}, "fallback": []}
    return {"slack_ids": dict(people.get("slack_ids") or {}), "fallback": list(people.get("fallback") or [])}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _slack_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pr_title(item: dict) -> str:
    return rr.PR_IN_TITLE_RE.sub("", str(item.get("title", ""))).strip()


def _mentions(people: dict, items: list[dict]) -> list[str]:
    ids: list[str] = []
    for item in items:
        slack_id = people["slack_ids"].get(item.get("author") or "")
        if slack_id and slack_id not in ids:
            ids.append(slack_id)
    return [f"<@{slack_id}>" for slack_id in (ids or people["fallback"])]


def dev_suspects(record: dict) -> list[dict]:
    """The PRs that dev verify tested for the first time: the commits since the previous dev release."""
    commits = [item for item in record.get("commits") or [] if item.get("sha")]
    return commits[-3:] or [{"sha": record["commit"], "title": "", "pr": None, "author": None}]


def dev_fix_prompt(service: rc.Service, record: dict, location: str) -> tuple[str, str]:
    """A self-contained prompt for the author: (facts, steps). Only the facts may be shortened."""
    verdict = record.get("verdict") or {}
    branch = service.release_branch
    facts = [
        f"{service.title} dev verify failed for commit {record['commit'][:9]} (merged to {branch} and deployed to "
        "dev). The automatic prod promote of this commit did not start; prod still runs the last verified "
        "release. Every later merge deploys to dev and promotes only when its dev verify passes.",
        "",
        "What failed:",
        *[f"- {_clip(reason, 300)}" for reason in (verdict.get("reasons") or ["see the dev release record"])[:5]],
        "",
        "Changes first tested by this dev verify:",
        *[f"- #{item.get('pr') or '?'} {_clip(_pr_title(item), 150)}" for item in dev_suspects(record)],
    ]
    steps = [
        f"In {service.github_repo}:",
        f"1. Read {service.docs} and the dev release record: gcloud storage cat {location}",
        "2. Read the dev build log (link in Slack) and the dev pods' logs; find which change breaks dev.",
        f"3. Fix it on {branch} with a test, or revert the PR when a fix is not quick. Open the PR as me and "
        "follow the PR review loop in AGENTS.md.",
        "4. After it merges it deploys by itself: dev, dev verify, then prod. Watch Slack #deployments.",
    ]
    return "\n".join(facts), "\n".join(steps)


def _fit(facts: str, steps: str) -> str:
    """The code block of a prompt in one Slack section: shorten the facts, never the steps."""
    room = SLACK_SECTION_LIMIT - len("``````") - len("\n\n") - len(steps)
    if len(facts) > room:
        facts = facts[: max(room - 1, 0)] + "…"
    return f"{facts}\n\n{steps}"


def dev_verify_alert(
    service: rc.Service, record: dict, build_url: str, location: str, test_run: bool = False
) -> tuple[str, list[dict]]:
    """The Slack message for a failed dev verify: who should fix it, and a prompt to copy.

    A test run (forced failure) says so first and mentions nobody.
    """
    people = load_people()
    suspects = dev_suspects(record)
    who = "(test run: no mentions)" if test_run else " ".join(_mentions(people, suspects))
    verdict = record.get("verdict") or {}
    short = record["commit"][:9]
    repo_url = f"https://github.com/{service.github_repo}"
    reasons = "\n".join(f"• {_slack_escape(_clip(reason, 300))}" for reason in (verdict.get("reasons") or [])[:5])

    def change_line(item: dict) -> str:
        number = item.get("pr")
        link = f"<{repo_url}/pull/{number}|#{number}>" if number else f"`{str(item.get('sha', ''))[:9]}`"
        return f"• {link} {_slack_escape(_clip(_pr_title(item), 150))} ({item.get('author') or 'unknown'})"

    facts, steps = (_slack_escape(part) for part in dev_fix_prompt(service, record, location))
    header = (
        f"{TEST_RUN_LINE}: forced dev verify failure" if test_run else f":no_entry: {service.title} dev verify failed"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Commit <{repo_url}/commit/{record['commit']}|{short}> failed dev verify "
                    f"(<{build_url}|dev build>). The prod promote did not start."
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*What failed*\n{reasons or '• see the record'}"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Changes first tested*\n" + "\n".join(map(change_line, suspects))},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{who}: please fix or revert this. Paste this prompt into Claude Code or Codex:",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{_fit(facts, steps)}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Record `{location}`"}]},
    ]
    first = suspects[0].get("pr") if suspects else None
    if test_run:
        # Only what happened: no "please fix", no fix prompt, so nobody acts on a forced failure.
        text = (
            f"{TEST_RUN_LINE}. A test of {service.title} dev verify forced a failure for {short} to prove that a "
            "failure stops the prod promote. No rollback, no action needed."
        )
        return text, [
            blocks[0],
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*What the test checked*\n{reasons or '• see the record'}"},
            },
            blocks[-1],
        ]
    text = (
        f":no_entry: {service.title} dev verify failed for {short}; the prod promote did not start. {who}: please fix "
        "or revert" + (f" #{first}" if first else "") + "."
    )
    return text, blocks


# --------------------------------------------------------------------------- rollback alert


def likely_prs(record: dict) -> list[dict]:
    """The suspected PRs worth a human's look: the ones that change source, else the top one."""
    suspects = (record.get("rollback_suggestion") or {}).get("suspected_prs") or []
    changed = [item for item in suspects if item.get("score", 0) >= 2]
    return (changed or suspects[:1])[:3]


def fix_prompt(service: rc.Service, record: dict, location: str, *, automatic: bool) -> tuple[str, str]:
    """A self-contained prompt the PR author pastes into Claude Code or Codex: (facts, steps)."""
    verdict = record.get("verdict") or {}
    target = ((record.get("rollback_suggestion") or {}).get("target") or {}).get("commit") or (
        (record.get("previous") or {}).get("commit") or "unknown"
    )
    branch = service.release_branch
    started = (
        f"an automatic rollback to commit {target[:9]} started. Check Slack #deployments for its result."
        if automatic
        else f"prod still runs it (tier check_only: no automatic rollback; the target is commit {target[:9]})."
    )
    facts = [
        f"{service.title} prod release {record['release_id']} (commit {record['commit'][:9]}) failed release-verify "
        f"({verdict.get('phase', 'unknown phase')}), and {started} A rollback changes only images: {branch} still "
        "has the change, so the next prod release ships it again.",
        "",
        "What regressed:",
        *[f"- {_clip(reason, 300)}" for reason in (verdict.get("reasons") or ["see the release record"])[:5]],
        "",
        "Suspected PRs (ranked by release-verify):",
        *[
            f"- #{item.get('pr') or '?'} {_clip(_pr_title(item), 150)} ({_clip(item.get('reason', ''), 80)})"
            for item in likely_prs(record)
        ],
    ]
    steps = [
        f"In {service.github_repo}:",
        f"1. Read {service.docs} and the release record: gcloud storage cat {location}",
        "2. Find which change in the suspected PRs causes the regression. Confirm it with the logs or metrics "
        "named above.",
        f"3. Fix it on {branch} with a test, or revert the PR when a fix is not quick. Open the PR as me and "
        "follow the PR review loop in AGENTS.md.",
        (
            "4. Automatic prod deploys are on hold after a rollback. Put the trailer "
            f"'Fixes-release: {record['release_id']}' in the fix or revert commit message (a revert of the PR also "
            "works). After it merges it deploys by itself. Watch Slack #deployments."
        )
        if getattr(service, "has_gate", True)
        else "4. After it merges it deploys by itself. Watch Slack #deployments.",
    ]
    return "\n".join(facts), "\n".join(steps)


def suggested_command(service: rc.Service, record: dict) -> str:
    """The rollback command for this release's suggested target (with --to when it is not the default)."""
    target = (record.get("rollback_suggestion") or {}).get("target") or None
    if (
        target
        and (target.get("release_id") == (record.get("previous") or {}).get("release_id"))
        and not target.get("rollback_to")
    ):
        target = None
    return service.kit_command("release_rollback.py") + rollback_to_flag(target)


def rollback_alert(
    service: rc.Service, record: dict, build_url: str | None, location: str, command: str | None = None
) -> tuple[str, list[dict]]:
    """The Slack message for a failed release: who should fix it, and a prompt to copy.

    build_url: the automatic rollback build; None when no rollback started (tier check_only).
    """
    people = load_people()
    suspects = likely_prs(record)
    who = " ".join(_mentions(people, suspects)) or "Someone"
    verdict = record.get("verdict") or {}
    short = record["commit"][:9]
    target = ((record.get("rollback_suggestion") or {}).get("target") or {}).get("commit") or "unknown"
    repo_url = f"https://github.com/{service.github_repo}"
    automatic = build_url is not None

    def pr_line(item: dict) -> str:
        number = item.get("pr")
        link = f"<{repo_url}/pull/{number}|#{number}>" if number else f"`{str(item.get('sha', ''))[:9]}`"
        author = item.get("author") or "unknown"
        slack_id = people["slack_ids"].get(author)
        return (
            f"• {link} {_slack_escape(_clip(_pr_title(item), 150))} ({'<@' + slack_id + '>' if slack_id else author})"
        )

    reasons = "\n".join(f"• {_slack_escape(_clip(reason, 300))}" for reason in (verdict.get("reasons") or [])[:5])
    facts, steps = (_slack_escape(part) for part in fix_prompt(service, record, location, automatic=automatic))
    if automatic:
        header = f":rewind: {service.title} prod rollback started"
        what = (
            f"A rollback to `{target[:9]}` started (<{build_url}|rollback build>); its result follows in this channel."
        )
    else:
        manual = command or f"{suggested_command(service, record)} run --reason '<why>'"
        header = f":rotating_light: {service.title} prod release failed release-verify"
        what = f"Tier check_only: no automatic rollback. Roll back by hand to `{target[:9]}`: `{manual}`."
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Release `{record['release_id']}` (<{repo_url}/commit/{record['commit']}|{short}>) failed "
                    f"release-verify at *{_slack_escape(verdict.get('phase', '?'))}*. {what}"
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*What regressed*\n{reasons or '• see the record'}"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Suspected PRs*\n" + ("\n".join(map(pr_line, suspects)) or "• none")},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{who}: please fix or revert this before the next prod release. Paste this prompt into Claude Code or Codex:",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{_fit(facts, steps)}```"}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Record `{location}` • {service.release_branch} still has the change; automatic prod deploys "
                        f"hold after a rollback until a revert or a commit with `Fixes-release: {record['release_id']}` merges."
                    ),
                }
            ],
        },
    ]
    first = suspects[0].get("pr") if suspects else None
    lead = "prod rollback started" if automatic else "prod release failed release-verify"
    text = (
        f"{header.split(' ', 1)[0]} {service.title} {lead}: release {short}. {who}: please fix or revert"
        + (f" #{first}" if first else "")
        + " before the next release."
    )
    return text, blocks


# --------------------------------------------------------------------------- watch window


def prod_state(service: rc.Service, store: rr.Store, record: dict) -> tuple[str | None, str | None]:
    """(why this release's window ends, why the checkpoint must wait), each None when it does not.

    It ends when a newer record is the latest or a rollback build runs. It waits while another
    prod build is changing the cluster: its result decides whether this release still runs. A
    build that has not reached its first cluster change (building, waiting for its turn, held by
    the gate) changes nothing, so the checks go on.
    """
    pointer, _ = store.read("latest.json")
    if pointer and pointer.get("release_id") != record["release_id"]:
        return f"release {pointer['release_id']}", None
    api = GoogleApi(service.project, service.region)
    rollbacks = api.ongoing_rollbacks(service.rollback_build_tag)
    if rollbacks:
        return f"rollback build {rollbacks[0].describe()}", None
    own_build = (record.get("build") or {}).get("id")
    # This release's own prod build still runs its last steps after deploy_finished.
    busy = [
        build
        for build in api.list_builds(service.prod.running_deploys_filter())
        if build.id != own_build and build.changing_cluster
    ]
    if busy:
        return None, f"build {busy[0].describe()} is changing prod"
    return None, None


# How long a checkpoint waits for another prod build that is changing the cluster.
BUSY_WAIT_SECONDS = 2700
BUSY_POLL_SECONDS = 60


def wait_until_settled(
    service: rc.Service,
    store: rr.Store,
    record: dict,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Wait while another prod build changes the cluster; return why the window ends, or None to check now."""
    deadline = clock() + BUSY_WAIT_SECONDS
    announced = ""
    while True:
        ended, busy = prod_state(service, store, record)
        if ended or not busy:
            return ended
        if busy != announced:
            print(f"Waiting: {busy}", flush=True)
            announced = busy
        if clock() >= deadline:
            raise rr.RecordError(f"{busy} for more than {BUSY_WAIT_SECONDS // 60} min")
        sleep(BUSY_POLL_SECONDS)


def cmd_watch(
    args: argparse.Namespace, service: rc.Service, env: rc.Environment, store: rr.Store, clients: rr.Clients
) -> int:
    """Sleep to each checkpoint after the deploy and check.

    The watch stops on a failed verdict, and when another release or a running
    prod or rollback build replaces this one (its image is no longer the one
    under test). A checkpoint that cannot run is recorded and the watch goes
    on to the next one.
    """
    record = rr.read_record(store, args.release_id)
    if (record.get("verdict") or {}).get("status") == "failed":
        # The deploy checks failed in the prod build: act now, not at the first checkpoint.
        on_failure(service, store, record)
        return EXIT_OK
    finished = rr.parse_utc(record["times"].get("deploy_finished") or record["times"]["deploy_started"])
    checkpoints = sorted(int(value) for value in args.checkpoints.replace(",", " ").split())
    dd, metrics_error = _datadog(service, clients)
    token = _token(env)
    logs = rs.CloudLogging(env.project, GoogleApi(env.project, env.region).access_token)
    index = 0
    while index < len(checkpoints):
        minutes = checkpoints[index]
        last = index == len(checkpoints) - 1
        index += 1
        due = finished + timedelta(minutes=minutes)
        wait = (due - datetime.now(rr.UTC)).total_seconds()
        if wait > 0:
            print(f"Waiting {wait / 60:.1f} min for the +{minutes} min checkpoint", flush=True)
            time.sleep(wait)
        try:
            if reason := wait_until_settled(service, store, record):
                rr.update_record(store, record["release_id"], lambda _: None, "verify_superseded", {"by": reason})
                print(f"Superseded by {reason}; the watch stops", flush=True)
                return EXIT_OK
            result = evaluate(
                service,
                record,
                phase="window",
                clients=clients,
                dd=dd,
                now=datetime.now(rr.UTC),
                metrics_error=metrics_error,
                store=store,
                token=token,
                logs=logs,
            )
            print_result(result)
            ended, busy = prod_state(service, store, record)
            if ended:
                rr.update_record(store, record["release_id"], lambda _: None, "verify_superseded", {"by": ended})
                print(f"Superseded by {ended} during the check; the result is dropped", flush=True)
                return EXIT_OK
            if busy:
                # Prod changed under the check: drop the result and check this checkpoint again.
                print(f"{busy} during the check; the checkpoint runs again", flush=True)
                index -= 1
                continue
            extend = (
                dd is not None
                and last
                and result["result"] == "passed"
                and any(metric.judged for metric in service.metrics)
                and result["judged"] < service.min_judged
                and minutes + args.extend_minutes <= args.max_window_minutes
            )
            if extend:
                print(f"Only {result['judged']:.0f} samples judged (< {service.min_judged}); extending", flush=True)
                checkpoints.append(minutes + args.extend_minutes)
            saved = apply_result(service, store, record, result, final=last and not extend, token=token)
            post_warnings(service, store, saved, result["fresh_warnings"])
        except (rr.RecordError, GuardError, rc.ConfigError, OSError, ValueError, KeyError) as error:
            print(f"Checkpoint +{minutes} min could not run: {error}", file=sys.stderr, flush=True)
            _record_checkpoint_error(store, record, minutes, str(error), final=last)
            rr.post_slack(
                f":warning: {service.title} release-verify {record['commit'][:9]} +{minutes} min check could not run: "
                f"{str(error)[:200]}"
            )
            continue
        if saved["verdict"]["status"] == "failed" or (last and not extend):
            rr.post_slack(slack_line(service, saved, result, store))
            if saved["verdict"]["status"] == "failed":
                on_failure(service, store, saved)
            return EXIT_OK
    return EXIT_OK


def _record_checkpoint_error(store: rr.Store, record: dict, minutes: int, error: str, *, final: bool) -> None:
    def change(current: dict) -> None:
        if final and (current.get("verdict") or {}).get("status") != "failed":
            current["verdict"] = {
                "status": "inconclusive",
                "at": rr.utc_now(),
                "phase": f"window_{minutes}m",
                "reason": f"check could not run: {error[:300]}",
            }

    try:
        rr.update_record(store, record["release_id"], change, "verify_checkpoint_error", {"error": error[:500]})
    except (rr.RecordError, GuardError) as write_error:
        print(f"WARNING: could not record the checkpoint error: {write_error}", file=sys.stderr, flush=True)


def on_failure(service: rc.Service, store: rr.Store, record: dict) -> None:
    """A failed prod release: start the rollback (tiers with automatic rollback), else ping the author."""
    if service.auto_rollback:
        start_rollback(service, store, record)
        return
    if os.environ.get("RELEASE_AUTO_ROLLBACK", "").strip() != "1":
        print("Test run: no alert for the failed release (RELEASE_AUTO_ROLLBACK is not 1)", flush=True)
        return
    current = rr.read_record(store, record["release_id"])
    rr.post_slack(*rollback_alert(service, current, None, store.location(rr.record_name(record["release_id"]))))


# --------------------------------------------------------------------------- automatic rollback

AUTO_REQUESTER = "release-verify (automatic)"
# How long to wait for the release's own prod build to end; the rollback build refuses while it runs.
OWN_BUILD_WAIT_SECONDS = 1200
# A claim still "starting" after this long belongs to a watcher that died before the trigger ran.
STALE_CLAIM_SECONDS = 600


def claim_blocks(current: dict, claim_id: str, now: datetime) -> bool:
    """Whether the record's rollback state stops this watcher from starting one.

    A rollback that started, or a release already rolled back, blocks. Our own
    claim does not (a claim write whose reply was lost). A claim still
    "starting" blocks for STALE_CLAIM_SECONDS; after that its watcher died
    before the trigger ran, and the caller has checked that no rollback build
    runs, so another watcher takes it over.
    """
    if current.get("rolled_back_by"):
        return True
    existing = current.get("auto_rollback")
    if not existing or existing.get("claim") == claim_id:
        return False
    if existing.get("status") == "failed":
        return False  # the rollback build failed: another attempt may start
    if existing.get("status") != "starting":
        return True
    try:
        age = (now - rr.parse_utc(existing["at"])).total_seconds()
    except (KeyError, TypeError, ValueError, rr.RecordError):
        return True
    return age < STALE_CLAIM_SECONDS


class RollbackClaimed(Exception):
    """Another watcher already claimed the automatic rollback of this release."""


def _release_claim(store: rr.Store, release_id: str, claim_id: str) -> None:
    """Drop our claim after the trigger failed, so a later attempt (or a rerun) can start one."""

    def release(current: dict) -> None:
        if (current.get("auto_rollback") or {}).get("claim") == claim_id:
            current.pop("auto_rollback")

    try:
        rr.update_record(store, release_id, release, "auto_rollback_released", {"claim": claim_id})
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not release the rollback claim {claim_id}: {error}", file=sys.stderr, flush=True)


def builds_changing_prod(service: rc.Service, own_build: str | None) -> tuple[bool, list[str]]:
    """(the release's own prod build still runs, other builds that change prod now).

    A prod build that has not reached its first cluster change waits for a rollback, so it does
    not stop one.
    """
    api = GoogleApi(service.project, service.region)
    deploys = api.list_builds(service.prod.running_deploys_filter())
    running = [build for build in deploys if build.id == own_build or build.changing_cluster]
    running += api.ongoing_rollbacks(service.rollback_build_tag)
    others = [build.describe() for build in running if build.id != own_build]
    return any(build.id == own_build for build in running), others


# How long the watch follows the rollback build it started.
ROLLBACK_WAIT_SECONDS = 3000
TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"})


def follow_rollback(
    service: rc.Service,
    store: rr.Store,
    release_id: str,
    build_id: str,
    claim_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    get_build: Callable[[str], object] | None = None,
) -> str:
    """Wait for the rollback build; on a failure free the claim (status "failed") and ask a human. Returns its status."""
    fetch = get_build or GoogleApi(service.project, service.region).get_build
    deadline = clock() + ROLLBACK_WAIT_SECONDS
    status = "STATUS_UNKNOWN"
    while clock() < deadline:
        try:
            status = str(getattr(fetch(build_id), "status", "STATUS_UNKNOWN"))
        except (GuardError, subprocess.SubprocessError, OSError) as error:
            print(f"WARNING: cannot read rollback build {build_id}: {error}", file=sys.stderr, flush=True)
        if status in TERMINAL_STATUSES:
            break
        sleep(30)
    if status == "SUCCESS":
        return status
    if status not in TERMINAL_STATUSES:
        # Still running, or unreadable: the claim stays, a human looks.
        rr.post_slack(
            f":warning: {service.title} automatic rollback build {build_id} has no result after "
            f"{ROLLBACK_WAIT_SECONDS // 60} min ({status}); check it: {rr.build_url(service.prod, build_id)}"
        )
        return status

    def failed(current: dict) -> None:
        if (current.get("auto_rollback") or {}).get("claim") == claim_id:
            current["auto_rollback"] = {**current["auto_rollback"], "status": "failed", "result": status}

    try:
        rr.update_record(store, release_id, failed, "auto_rollback_failed", {"build_id": build_id, "status": status})
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not mark the rollback failed: {error}", file=sys.stderr, flush=True)
    rr.post_slack(
        f":x: {service.title} automatic rollback build {build_id} ended {status}; prod may still run the failed "
        f"release {release_id}. Roll back by hand: {service.kit_command('release_rollback.py')} run --reason '<why>' "
        f"({rr.build_url(service.prod, build_id)})"
    )
    return status


def start_rollback(
    service: rc.Service,
    store: rr.Store,
    record: dict,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    runner: Callable[[list[str]], str] | None = None,
    builds: Callable[[rc.Service, str | None], tuple[bool, list[str]]] | None = None,
    follow: bool = True,
) -> str | None:
    """Start the rollback build for a failed release that still runs in prod; return the build id.

    Only the watch-window build of a real release calls it (RELEASE_AUTO_ROLLBACK=1;
    test runs leave it unset). It starts nothing when a rollback already started
    for the release, when a newer record exists, or when another prod or rollback
    build runs. The rollback build checks again with _EXPECT_LATEST and refuses
    if prod moved on. Never raises: a failure posts a Slack line that asks a
    human to roll back.
    """
    release_id = record["release_id"]
    short = record["commit"][:9]
    title = service.title
    command = service.kit_command("release_rollback.py")
    run = runner or rr.run_command
    changing = builds or builds_changing_prod
    if os.environ.get("RELEASE_AUTO_ROLLBACK", "").strip() != "1":
        print("Automatic rollback is off for this run (RELEASE_AUTO_ROLLBACK is not 1)", flush=True)
        return None
    claim_id = uuid.uuid4().hex
    try:
        own_build = (record.get("build") or {}).get("id")
        deadline = clock() + OWN_BUILD_WAIT_SECONDS
        while True:
            current = rr.read_record(store, release_id)
            if claim_blocks(current, claim_id, datetime.now(rr.UTC)):
                print(f"A rollback already started for {release_id}; nothing to do", flush=True)
                return None
            pointer, _ = store.read("latest.json")
            if (pointer or {}).get("release_id") != release_id:
                print(f"No automatic rollback: {(pointer or {}).get('release_id')} replaced {release_id}", flush=True)
                return None
            own_running, others = changing(service, own_build)
            if others:
                rr.post_slack(
                    f":warning: {title} release-verify {short} failed, but {others[0]} changes prod now, so no "
                    f"automatic rollback started. Check prod; roll back by hand with {command} run if needed."
                )
                return None
            if not own_running:
                break
            if clock() > deadline:
                raise rr.RecordError(
                    f"its own prod build {own_build} still runs after {OWN_BUILD_WAIT_SECONDS // 60} min"
                )
            sleep(30)
        summary = (current.get("rollback_suggestion") or {}).get("summary") or "release-verify failed"
        # A regression traced to an earlier unjudged release names an older target than "previous".
        suggested = ((current.get("rollback_suggestion") or {}).get("target")) or {}
        target = suggested.get("rollback_to") or suggested.get("release_id")
        if not target or target == (current.get("previous") or {}).get("release_id"):
            target = "previous"
        phase = (current.get("verdict") or {}).get("phase", "")
        # Substitution values use "|" as the delimiter; keep the reason on one line without it.
        reason = " ".join(f"release-verify failed ({phase}): {summary}".replace("|", "/").split())[:300]
        substitutions = (
            f"^|^_TARGET={target}|_REASON={reason}|_REQUESTED_BY={AUTO_REQUESTER}|_DRY_RUN=false"
            f"|_EXPECT_LATEST={release_id}"
        )

        # Claim the rollback in the record before the trigger runs. The write is
        # conditional on the record's generation, so of two watchers only one wins.
        def claim(current: dict) -> None:
            if claim_blocks(current, claim_id, datetime.now(rr.UTC)):
                raise RollbackClaimed(release_id)
            current["auto_rollback"] = {"status": "starting", "claim": claim_id, "at": rr.utc_now()}

        try:
            rr.update_record(store, release_id, claim, "auto_rollback_claimed", {"claim": claim_id})
        except RollbackClaimed:
            print(f"Another watcher claimed the rollback of {release_id}; nothing to do", flush=True)
            return None
        try:
            build_id = run(
                [
                    "gcloud",
                    "builds",
                    "triggers",
                    "run",
                    service.rollback_trigger,
                    f"--region={service.region}",
                    f"--project={service.project}",
                    f"--branch={service.release_branch}",
                    f"--substitutions={substitutions}",
                    "--format=value(metadata.build.id)",
                ]
            ).strip()
        except Exception:
            _release_claim(store, release_id, claim_id)
            raise

        def started(current: dict) -> None:
            current["auto_rollback"] = {
                "status": "started",
                "claim": claim_id,
                "build_id": build_id,
                "at": rr.utc_now(),
            }

        rr.update_record(store, release_id, started, "auto_rollback_started", {"build_id": build_id})
    except Exception as error:  # noqa: BLE001 - a failure here must reach a human, not end the watch with a trace
        print(f"WARNING: the automatic rollback did not start: {error}", file=sys.stderr, flush=True)
        rr.post_slack(
            f":x: {title} release-verify {short} failed and the automatic rollback did not start: {str(error)[:200]}. "
            f"Roll back by hand: {suggested_command(service, record)} run --reason '<why>'"
        )
        return None
    url = rr.build_url(service.prod, build_id)
    print(f"Automatic rollback build {build_id} started: {url}", flush=True)
    rr.post_slack(*rollback_alert(service, current, url, store.location(rr.record_name(release_id))))
    if follow:
        follow_rollback(service, store, release_id, build_id, claim_id)
    return build_id


def window_sources(service: rc.Service) -> list[Path]:
    """What the watch-window build needs: the kit with the service configs, and the release's source tree
    (to trace a new error to the commit that changed it)."""
    sources = [path for path in KIT_DIR.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    for prefix in service.code.source_prefixes:
        base = REPO_ROOT / prefix.rstrip("/")
        paths = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for path in paths:
            if not path.is_file() or "node_modules" in path.parts:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            if service.code.is_source(relative):
                sources.append(path)
    return sources


def cmd_schedule_window(
    args: argparse.Namespace, service: rc.Service, env: rc.Environment, store: rr.Store, clients: rr.Clients
) -> int:
    """Submit the watch-window build with only the kit and the source tree as its source.

    It is a separate build, so the prod build ends at once and the next
    deploy does not wait for the window (single flight counts only builds of
    the prod triggers and rollback builds).
    """
    source = Path(args.source_dir)
    for path in window_sources(service):
        target = source / path.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    checkpoints = args.checkpoints or ",".join(str(value) for value in service.checkpoints)
    substitutions = (
        f"_SERVICE={service.name},_STORE={args.store or service.store},_RELEASE_ID={args.release_id},"
        f"_CHECKPOINTS={checkpoints.replace(',', ' ')},_SLACK_DISABLED={'1' if args.no_slack else ''},"
        f"_AUTO_ROLLBACK={'' if args.no_slack else '1'}"
    )
    command = [
        "gcloud",
        "builds",
        "submit",
        str(source),
        f"--config={source / service.window_config}",
        f"--region={env.region}",
        f"--project={env.project}",
        f"--substitutions={substitutions}",
        "--async",
        "--format=value(id)",
    ]
    build_id = rr.run_command(command, timeout=600).strip()
    window_build = {
        "id": build_id,
        "url": rr.build_url(env, build_id),
        "checkpoints_minutes": [int(value) for value in checkpoints.replace(",", " ").split()],
    }
    rr.update_record(
        store,
        rr.read_record(store, args.release_id)["release_id"],
        lambda current: current.setdefault("checks", {}).__setitem__("window_build", window_build),
        "window_scheduled",
        {"build_id": build_id},
    )
    print(f"Watch window build {build_id} submitted (checkpoints {checkpoints} min)", flush=True)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="run one phase of checks now")
    rr.add_common_args(check)
    check.add_argument("--release-id", default="latest")
    check.add_argument("--release-id-file", help="read the release id from this file (Cloud Build workspace)")
    check.add_argument("--phase", choices=["deploy", "window"], default="deploy")
    check.add_argument("--final", action="store_true", help="this check sets the final verdict")
    check.add_argument("--no-metrics", action="store_true", help="smoke checks only")
    check.add_argument("--now", help="evaluate as of this UTC time (dry runs of a past window)")
    check.add_argument("--failed-marker", help="write this file when the verdict is failed")
    check.add_argument("--force-fail", default="", help="test runs: add a failed check with this text")
    check.add_argument("--build-url", default="", help="the build that runs this check (Slack link)")
    check.add_argument("--build-id", default="", help="dev verify: the dev build, to tell a superseded verify")
    check.add_argument(
        "--wait-rollouts-seconds", type=int, default=0, help="first wait this long for every Deployment rollout"
    )

    watch = commands.add_parser("watch", help="checks at each checkpoint of the watch window")
    rr.add_common_args(watch)
    watch.add_argument("--release-id", required=True)
    watch.add_argument("--checkpoints", default="", help="minutes after the deploy (default: the config's)")
    watch.add_argument("--extend-minutes", type=int, default=15)
    watch.add_argument("--max-window-minutes", type=int, default=180)

    schedule = commands.add_parser("schedule-window", help="submit the watch-window build for a release")
    rr.add_common_args(schedule)
    schedule.add_argument("--release-id", default="latest")
    schedule.add_argument("--release-id-file")
    schedule.add_argument("--checkpoints", default="")
    schedule.add_argument("--source-dir", default="/workspace/release-verify-src")
    schedule.add_argument(
        "--no-slack", action="store_true", help="test runs: the window build posts nothing and never rolls back"
    )

    args = parser.parse_args(argv)
    if getattr(args, "release_id_file", None):
        args.release_id = Path(args.release_id_file).read_text(encoding="utf-8").strip()
    handlers = {"check": cmd_check, "watch": cmd_watch, "schedule-window": cmd_schedule_window}
    try:
        service, env, store, clients = rr.open_from_args(args)
        if args.command == "watch" and not args.checkpoints:
            args.checkpoints = ",".join(str(value) for value in service.checkpoints)
        return handlers[args.command](args, service, env, store, clients)
    except (
        rr.RecordError,
        GuardError,
        rc.ConfigError,
        subprocess.SubprocessError,
        KeyError,
        OSError,
        ValueError,
    ) as error:
        print(f"release-verify: {error}", file=sys.stderr, flush=True)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
