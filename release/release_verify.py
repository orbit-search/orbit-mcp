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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_capacity as rcap  # noqa: E402
import release_config as rc  # noqa: E402
import release_gate as rg  # noqa: E402
import release_observation as ro  # noqa: E402
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
        """Keep the request headers with both keys and the API base URL of the Datadog site."""
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
    """The values of a scalar query response per group (groups joined by "|", "*" without groups)."""
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
    """The latest version of a Secret Manager secret, or "" when it cannot be read."""
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
    # ok, exceeded, insufficient, within_spread, no_spread, no_runtime_change, environmental, environmental_marked
    status: str
    unit: str = ""

    def line(self) -> str:
        """One readable line: the metric and scope, baseline -> candidate, the samples and the limit."""

        def show(value: float | None) -> str:
            """A value as a percentage for a rate, else in short form; n/a when there is none."""
            if value is None:
                return "n/a"
            return f"{value:.2%}" if self.unit == "rate" else f"{value:,.3g}"

        return (
            f"{self.metric} {self.scope}: {show(self.baseline)} -> {show(self.candidate)} "
            f"(n={self.baseline_samples:,.0f} -> {self.candidate_samples:,.0f}; limit {self.limit})"
        )


def describe_limit(metric: rc.Metric) -> str:
    """The limit of a metric in words (for rates, the absolute part in percentage points)."""
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
    """The scope name of a metric group: the metric scope, and the group when it has one."""
    return metric.scope if group == "*" else f"{metric.scope} {group}"


def judge(metric: rc.Metric, scope: str, base: tuple[float | None, float], cand: tuple[float | None, float]) -> Finding:
    """Compare the window value with the baseline: exceeded, ok, or insufficient with too few samples."""
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


# --------------------------------------------------------------------------- control by version, environmental signals

# Spans with DD_VERSION carry the release's commit in the `version` tag. In deep-search, from 2026-10-21
# 23:00 UTC, the profile store's prod writes failed (a switch, not code), and 3 releases in a row rolled
# back on their HTTP error rate while the rollback target ran as bad or worse in the same windows. An
# exceeded rate of a `versioned` metric is therefore compared with the other versions too.
NO_VERSION = frozenset({"", "*", "N/A", "n/a", "none"})
GROUP_BY_RE = re.compile(r"\bby \{([^}]*)\}")


def versioned_query(query: str) -> str:
    """The query with `version` as its last group tag: `by {a}` -> `by {a,version}`, none -> `by {version}`."""
    match = GROUP_BY_RE.search(query)
    if match:
        return f"{query[: match.end(1)]},version{query[match.end(1) :]}"
    if query.endswith(".as_count()"):
        return query[: -len(".as_count()")] + " by {version}.as_count()"
    return query + " by {version}"


def is_release_version(version: str, commit: str) -> bool:
    """Whether a version tag names the commit (DD_VERSION holds the commit or its short form)."""
    return len(version) >= 7 and commit.startswith(version)


def by_version(
    dd: DatadogClient, metric: rc.Metric, start: datetime, end: datetime
) -> dict[str, dict[str, tuple[float, float]]]:
    """Per scope and version: (numerator, denominator) of a versioned rate metric in [start, end)."""
    numerator = dd.scalar(versioned_query(metric.numerator), "sum", start, end)
    denominator = dd.scalar(versioned_query(metric.denominator), "sum", start, end)
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for key, samples in denominator.items():
        group, _, version = key.rpartition("|")
        if version in NO_VERSION or samples <= 0:
            continue
        out.setdefault(scope_of(metric, group or "*"), {})[version] = (numerator.get(key) or 0.0, samples)
    return out


def control_by_version(
    service: rc.Service,
    dd: DatadogClient,
    record: dict,
    findings: list[Finding],
    end: datetime,
    judged_commits: tuple[str, ...] = (),
) -> None:
    """An exceeded rate that the other versions in prod show too is not the release's: "environmental".

    The window runs from the deploy start to the checkpoint, so it holds the rollout overlap (the
    previous version's pods) and any older pod that still serves, the rollback target among them.
    Environmental only when every other version independently exceeds the limit over the baseline
    and every judged release version stays within its limit over each control. With too few samples the plain
    limit decides. judged_commits: the commits of the earlier unjudged releases judged with this one;
    their versions count as the release, never as controls (the error may have started in them).
    """
    metrics = {metric.name: metric for metric in service.metrics if metric.versioned and metric.kind == "rate"}
    exceeded = [f for f in findings if f.status == "exceeded" and f.metric in metrics]
    if not exceeded:
        return
    start = rr.parse_utc(record["times"]["deploy_started"])
    commits = (record["commit"], *judged_commits)

    def released(version: str) -> bool:
        """Whether a version is one of the releases under judgment."""
        return any(is_release_version(version, commit) for commit in commits)

    rates: dict[str, dict[str, dict[str, tuple[float, float]]]] = {}
    for finding in exceeded:
        metric = metrics[finding.metric]
        if metric.name not in rates:
            rates[metric.name] = by_version(dd, metric, start, end)
        versions = rates[metric.name].get(finding.scope) or {}
        own = {version: value for version, value in versions.items() if released(version)}
        others = {version: value for version, value in versions.items() if not released(version)}
        newest = [value for version, value in own.items() if is_release_version(version, record["commit"])]
        newest_errors, newest_n = sum(errors for errors, _ in newest), sum(n for _, n in newest)
        control_n = sum(n for _, n in others.values())
        if not (newest_n and control_n):
            finding.limit += "; no other version served in the window"
            continue
        control, rate = sum(errors for errors, _ in others.values()) / control_n, newest_errors / newest_n
        names = ", ".join(version[:9] for version in sorted(others, key=lambda version: -others[version][1])[:3])
        same_window = (
            f"in the same window ({start:%H:%M}-{end:%H:%M} UTC) the other versions ({names}) ran "
            f"{control:.2%} (n={control_n:,.0f}), the release {rate:.2%} (n={newest_n:,.0f})"
        )
        # The pooled rate above is diagnostic only. Neither healthy controls nor
        # undersampled versions may be hidden by another version's requests.
        controls_support = all(
            n > 0
            and judge(metric, finding.scope, (finding.baseline, finding.baseline_samples), (errors / n, n)).status
            == "exceeded"
            and all(
                own_n > 0 and judge(metric, finding.scope, (errors / n, n), (own_errors / own_n, own_n)).status == "ok"
                for own_errors, own_n in own.values()
            )
            for errors, n in others.values()
        )
        if controls_support:
            finding.status = "environmental"
            finding.limit += f"; not the release: {same_window}"
        else:
            finding.limit += f"; {same_window}"


# A signal that still fails on the rollback target after an automatic rollback has an environmental
# cause (watch_rollback). It is marked in this object of the store; while it is marked, an exceeded
# limit on it is a quiet warning and never rolls back. The mark ends when the signal is back within
# its limit of the baseline from before the cause.
ENVIRONMENTAL_OBJECT = "environmental_signals.json"
ROLLBACK_WINDOW_MINUTES = 15
ROLLBACK_RECORD_WAIT_SECONDS = 600


def signal_key(metric: str, scope: str) -> str:
    """The key of a signal in the marks: the metric and its scope."""
    return f"{metric} {scope}"


def environmental_signals(store: rr.Store) -> dict[str, dict]:
    """The signals marked environmental now, by signal_key. Unreadable: none (the plain limits decide)."""
    try:
        data, _ = store.read(ENVIRONMENTAL_OBJECT)
    except rr.RecordError as error:
        print(f"WARNING: the environmental signals could not be read: {error}", file=sys.stderr, flush=True)
        return {}
    return {key: entry for key, entry in ((data or {}).get("signals") or {}).items() if entry.get("status") == "active"}


def alert_marks(store: rr.Store) -> None:
    """Post the Slack line of each mark that Slack has not taken yet; marks of one rollback share a line.

    Never raises: the watch posts again at its next checkpoint.
    """
    try:
        data, _ = store.read(ENVIRONMENTAL_OBJECT)
        pending = {
            entry["alert"]
            for entry in ((data or {}).get("signals") or {}).values()
            if entry.get("status") == "active" and not entry.get("alerted") and entry.get("alert")
        }
        posted = {alert for alert in sorted(pending) if rr.post_slack(alert)}
        if not posted:
            return

        def change(current: dict | None) -> dict:
            """Note the marks whose line Slack took."""
            data = current or {"signals": {}}
            for entry in (data.get("signals") or {}).values():
                if entry.get("alert") in posted:
                    entry["alerted"] = True
            return data

        rr.update_object(store, ENVIRONMENTAL_OBJECT, change)
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        print(f"WARNING: the environmental alerts could not be posted: {error}", file=sys.stderr, flush=True)


def apply_environmental(findings: list[Finding], signals: dict[str, dict]) -> None:
    """An exceeded limit on a marked signal is a quiet warning: no automatic rollback until it recovers."""
    for finding in findings:
        entry = signals.get(signal_key(finding.metric, finding.scope))
        if entry and finding.status == "exceeded":
            finding.status = "environmental_marked"
            finding.limit += (
                f"; environmental since {entry['since']} (rollback {entry['rollback_id']} did not help), "
                "so no automatic rollback on it until it recovers"
            )


def recovered_signals(service: rc.Service, findings: list[Finding], signals: dict[str, dict]) -> list[str]:
    """The marked signals that are back within their limit of the baseline from before the cause."""
    metrics = {metric.name: metric for metric in service.metrics}
    recovered = []
    for finding in findings:
        key = signal_key(finding.metric, finding.scope)
        entry = signals.get(key)
        if not entry or finding.metric not in metrics:
            continue
        base = (entry["baseline"], entry["baseline_samples"])
        if (
            judge(metrics[finding.metric], finding.scope, base, (finding.candidate, finding.candidate_samples)).status
            == "ok"
        ):
            recovered.append(key)
    return recovered


def note_recovered(service: rc.Service, store: rr.Store, record: dict, keys: list[str]) -> None:
    """End the marks of recovered signals: automatic rollbacks on them are on again. Never raises."""
    if not keys:
        return
    ended: list[str] = []

    def change(current: dict | None) -> dict:
        """Mark the recovered signals with when and in which release."""
        data = current or {"signals": {}}
        ended.clear()
        for key in keys:
            entry = (data.get("signals") or {}).get(key)
            if entry and entry.get("status") == "active":
                entry.update(status="recovered", recovered_at=rr.utc_now(), recovered_in=record["release_id"])
                ended.append(key)
        return data

    try:
        rr.update_object(store, ENVIRONMENTAL_OBJECT, change)
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        print(f"WARNING: could not end the environmental marks: {error}", file=sys.stderr, flush=True)
        return
    if ended:
        rr.post_slack(
            f":white_check_mark: {service.title} {record['environment']}: {', '.join(ended)} recovered (release "
            f"{record['commit'][:9]}); automatic rollbacks on it are on again."
        )


def signal_value(
    dd: DatadogClient, metric: rc.Metric, scope: str, commit: str, start: datetime, end: datetime
) -> tuple[float | None, float]:
    """(value, samples) of a metric scope in [start, end): the commit's own version when the metric has one."""
    if metric.versioned and metric.kind == "rate":
        versions = by_version(dd, metric, start, end).get(scope) or {}
        own = [value for version, value in versions.items() if is_release_version(version, commit)]
        errors, samples = sum(e for e, _ in own), sum(n for _, n in own)
        return (errors / samples if samples else None), samples
    values = {scope_of(metric, group): value for group, value in metric_values(dd, metric, start, end).items()}
    return values.get(scope, (None, 0.0))


def watch_rollback(
    service: rc.Service,
    store: rr.Store,
    dd: DatadogClient | None,
    record: dict,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(rr.UTC),
    prod_busy: Callable[[], list[str]] | None = None,
) -> str:
    """After an automatic rollback, judge the metrics that caused it on the rollback target.

    ROLLBACK_WINDOW_MINUTES after the rollback deployed, each metric limit of the failed checkpoint is
    judged again against the same baseline, on the rollback target's own version where the metric
    has one. Still exceeded: the cause is environmental, not the release. The signal is marked (no
    automatic rollback on it until it recovers), one Slack line asks a human to look, and when every
    regression of the checkpoint is such a signal, the promote hold of the rollback ends. Metrics
    with the spread rule are not judged again (a rollback back inside the hourly spread proves
    nothing), and nothing is judged while a build changes prod. Returns the outcome; never raises.
    """
    if dd is None:
        return "not checked: no Datadog client"

    def busy() -> list[str]:
        """Builds that change prod now: deploys past their gate (the prod lock) and running rollbacks."""
        holder = rr.lock_holder(store, rr.build_alive(service.prod))
        current = rr.read_record(store, record["release_id"])
        rollback = rr.read_record(store, current["rolled_back_by"])
        running = builds_since_rollback(service, rollback)
        return (
            [f"{holder.get('kind')} build {holder.get('holder')} (holds the prod lock)"] if holder else []
        ) + running

    try:
        return _watch_rollback(service, store, dd, record, sleep, clock, now, prod_busy or busy)
    except Exception as error:  # noqa: BLE001 - the rollback already happened; this check only adds to it
        print(f"WARNING: the check after the rollback could not run: {error}", file=sys.stderr, flush=True)
        return f"not checked: {error}"


def _watch_rollback(
    service: rc.Service,
    store: rr.Store,
    dd: DatadogClient,
    record: dict,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    now: Callable[[], datetime],
    prod_busy: Callable[[], list[str]],
) -> str:
    """watch_rollback without the catch-all."""
    release_id = record["release_id"]
    current = rr.read_record(store, release_id)
    verdict = current.get("verdict") or {}
    phase = verdict.get("phase", "")
    regressions = (ro.phase_result(current, phase, verdict.get("at")) or {}).get("regressions") or []
    items = [item for item in regressions if item.get("kind") == "metric"]
    metrics = {metric.name: metric for metric in service.metrics}
    # Spread metrics need the baseline's hourly values; the check after a rollback does not judge them.
    items = [item for item in items if item.get("metric") in metrics and not metrics[item["metric"]].spread]
    if not items:
        return "no metric without the spread rule regressed"
    deadline = clock() + ROLLBACK_RECORD_WAIT_SECONDS
    while not (by := current.get("rolled_back_by")):
        if clock() > deadline:
            return f"no rollback record for {release_id} after {ROLLBACK_RECORD_WAIT_SECONDS // 60} min"
        sleep(30)
        current = rr.read_record(store, release_id)
    rollback = rr.read_record(store, by)
    kept = (rollback.get("rollback") or {}).get("not_rolled_back") or []
    if kept:
        # The rollback set images (and rollback_env) only; a setting the release changed may still run.
        return f"not judged: rollback {by} left changes in place ({', '.join(kept[:5])})"
    if (rollback.get("rollback") or {}).get("settings_compared") is not True:
        return f"not judged: rollback {by} did not verify which settings stayed in place"
    finished = rr.parse_utc(rollback["times"].get("deploy_finished") or rollback["times"]["deploy_started"])
    wait = (finished + timedelta(minutes=ROLLBACK_WINDOW_MINUTES) - now()).total_seconds()
    if wait > 0:
        print(f"Waiting {wait / 60:.1f} min to judge {len(items)} metric(s) on the rollback target", flush=True)
        sleep(wait)
    pointer, _ = store.read("latest.json")
    if (pointer or {}).get("release_id") != by:
        return f"prod moved on from rollback {by}"
    if changing := prod_busy():
        # A Fixes-release deploy may be past its gate while latest.json still names the rollback.
        return f"not judged: {changing[0]} changes prod"
    end, target = now(), rollback["commit"]
    findings = [
        judge(
            metrics[item["metric"]],
            item["scope"],
            (item["baseline"], item["baseline_samples"]),
            signal_value(dd, metrics[item["metric"]], item["scope"], target, finished, end),
        )
        for item in items
    ]
    pointer, _ = store.read("latest.json")
    if (pointer or {}).get("release_id") != by:
        return f"prod moved on from rollback {by} during the metric reads"
    if changing := prod_busy():
        return f"not judged: {changing[0]} changes prod"
    still = [f for f in findings if f.status == "exceeded"]
    window = f"{finished:%m-%d %H:%M}-{end:%H:%M} UTC on {target[:9]}"

    def note(current: dict) -> None:
        """Keep what the check after the rollback found in the rollback's record."""
        current.setdefault("checks", {})["rollback_window"] = {
            "at": rr.utc_now(),
            "window": window,
            "findings": [asdict(f) for f in findings],
            "environmental": [signal_key(f.metric, f.scope) for f in still],
        }

    rr.update_record(store, by, note, "rollback_window", {"still_failing": len(still)})
    for finding in findings:
        print(f"   after the rollback {finding.status:>11}  {finding.line()}", flush=True)
    if not still:
        if any(f.status == "insufficient" for f in findings):
            return f"not judged: not enough samples on the rollback target ({window})"
        return f"the rollback helped ({window})"
    every = len(still) == len(regressions)
    keys = [signal_key(f.metric, f.scope) for f in still]
    if every:
        why = (
            f"The cause is environmental (a switch, a provider, data), not release {record['commit'][:9]}, "
            f"and the promote hold of {by} ends"
        )
    else:
        why = (
            f"These signals did not come from release {record['commit'][:9]}; its other regressions keep "
            f"the promote hold of {by}"
        )
    alert = (
        f":warning: {service.title} {record['environment']}: rollback {by} did not help. {window}: "
        f"{'; '.join(f.line() for f in still)[:600]}. {why}. Automatic rollbacks on {', '.join(keys)} stop "
        f"until it recovers. Please look. Marks: {store.location(ENVIRONMENTAL_OBJECT)}"
    )

    def mark(current: dict | None) -> dict:
        """Mark each signal that still fails (a mark that is active stays as it is)."""
        data = current or {"signals": {}}
        signals = data.setdefault("signals", {})
        for finding in still:
            key = signal_key(finding.metric, finding.scope)
            if (signals.get(key) or {}).get("status") == "active":
                continue
            signals[key] = {
                "status": "active",
                "metric": finding.metric,
                "scope": finding.scope,
                "baseline": finding.baseline,
                "baseline_samples": finding.baseline_samples,
                "value": finding.candidate,
                "samples": finding.candidate_samples,
                "window": window,
                "since": rr.utc_now(),
                "release_id": release_id,
                "rollback_id": by,
                # One Slack line per rollback; alert_marks posts it and retries until Slack takes it.
                "alert": alert,
                "alerted": False,
            }
        return data

    rr.update_object(store, ENVIRONMENTAL_OBJECT, mark)

    def cause(current: dict) -> None:
        """Note the environmental signals on the failed release; the cause only when every regression was one."""
        verdict = current.setdefault("verdict", {})
        verdict["environmental_signals"] = keys
        if every:
            verdict["cause"] = "environmental"

    rr.update_record(store, release_id, cause, "cause_environmental", {"signals": len(still)})
    if every:
        import release_gate as rg  # noqa: PLC0415 - release_gate imports this module

        stores = rg.Stores(service=service, prod=store, dev=None)
        rg.release_hold(stores, by, f"environmental: {', '.join(keys)} still failed on {window}", AUTO_REQUESTER)
    alert_marks(store)
    return f"environmental: {', '.join(keys)}"


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
    judged_commits: tuple[str, ...] = (),
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
    try:
        control_by_version(service, dd, record, findings, window[1], judged_commits)
    except GuardError as error:
        print(f"WARNING: the per-version control could not be read: {error}", file=sys.stderr, flush=True)
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
    status: str  # passed, failed, skipped, inconclusive, environmental (capacity: not the release's)
    details: list[str] = field(default_factory=list)


# A Deployment fails the check when fewer than this share of its desired
# replicas are available. Rollouts are awaited first; the margin is for HPA
# scale-ups whose new pods wait for nodes.
MIN_AVAILABLE_SHARE = 0.8


def check_images(
    record: dict, snapshot: dict[str, dict], targets: dict[str, str] | None = None, *, window: bool = False
) -> Check:
    """Every Deployment runs its target digest, rollouts are complete and enough pods are available.

    The target is the release image, or, when targets is given (a rollback to
    a per-Deployment snapshot), only the listed Deployments with their own digests.
    """
    expected = rr.digest_of((record.get("images") or {}).get(rr.IMAGE_ROLE))
    problems = []
    uncertain = []
    for name, workload in sorted(snapshot.items()):
        if targets is not None and name not in targets:
            continue
        want = targets[name] if targets is not None else expected
        if window and not workload.get("digest"):
            uncertain.append(f"{name}: image digest unresolved; no running pod proves the image is wrong")
        elif workload.get("digest") != want:
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
    checked_result = Check("images", "failed" if problems else "passed", [f"{checked} Deployments checked", *problems])
    if uncertain:
        if not problems or ro.availability_only(asdict(checked_result)):
            checked_result.status = "inconclusive"
        checked_result.details.extend(uncertain)
    return checked_result


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
        """Read the cluster server and its CA from the kubectl config; keep the token source."""
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
        """GET a path on a pod port through the API server proxy; return (status, body)."""
        url = f"{self.server}/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy{path}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token()}", "User-Agent": USER_AGENT}
        )
        return http_get(request, self.context)


PodGet = Callable[[str, str, int, str], tuple[int, bytes]]


def _expect_ok(status: int, body: bytes, check: dict) -> tuple[bool, str]:
    """Judge a smoke response against the check: the status, the text it contains, and the JSON fields."""
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
    """The Deployment of the environment with this name; raise ConfigError when there is none."""
    for item in env.deployments:
        if item.name == name:
            return item
    raise rc.ConfigError(f"smoke check names deployment {name!r}, which {env.service} {env.name} does not deploy")


# The API server's own answer when its proxy cannot reach a pod (a node or pod that is still
# starting, an unready network path): no answer of the app, so it is tried again.
PROXY_UNREACHABLE = "error trying to reach service"
PROXY_ATTEMPTS = 3
PROXY_RETRY_SECONDS = 10


def _proxy_unreachable(status: int, body: bytes) -> bool:
    """Whether a pod-proxy answer comes from the API server (it could not reach the pod), not from the app."""
    if status != 503:
        return False
    try:
        answer = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return (
        isinstance(answer, dict) and answer.get("kind") == "Status" and PROXY_UNREACHABLE in str(answer.get("message"))
    )


def check_http_pod(
    clients: rr.Clients, check: dict, pod_get: PodGet, sleep: Callable[[float], None] = time.sleep
) -> Check:
    """A GET on every Ready pod of a Deployment (or one pod), through the Kubernetes API pod proxy.

    A pod that is not Ready yet (an autoscaler's new pod) is not called. A Ready pod that the
    API server proxy cannot reach (its own 503 Status answer) is tried again; one that stays
    unreachable is named but fails nothing while another pod answers. A request that keeps
    failing otherwise (a timeout: the pod may hang) fails. At least one pod must answer.
    """
    name = str(check.get("name") or f"http {check['path']}")
    deployment = _deployment(clients.env, check["deployment"])
    live = rr.live_pods(clients, deployment)
    pods = [pod for pod in live if rr.is_ready(pod)]
    if not pods:
        return Check(name, "failed", [f"no Ready {deployment.name} pod ({len(live)} live)"])
    if not check.get("all_pods", True):
        pods = pods[:1]
    details, failed, answered, unreachable = [], False, 0, []
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        for attempt in range(PROXY_ATTEMPTS):
            try:
                status, body = pod_get(clients.env.namespace, pod_name, int(check["port"]), str(check["path"]))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                status, body, problem = 0, b"", f"error: {error}"
            else:
                problem = ""
            if not problem and not _proxy_unreachable(status, body):
                break
            if attempt < PROXY_ATTEMPTS - 1:
                sleep(PROXY_RETRY_SECONDS)
        if problem:
            # A request that keeps failing (a timeout, a connection error) can be a hung pod: it fails.
            failed = True
            details.append(f"{pod_name}: GET {check['path']}: {problem} (after {PROXY_ATTEMPTS} tries)")
            continue
        if _proxy_unreachable(status, body):
            unreachable.append(pod_name)
            details.append(
                f"{pod_name}: not reachable through the API server proxy after {PROXY_ATTEMPTS} tries (not judged)"
            )
            continue
        ok, line = _expect_ok(status, body, check)
        answered += 1
        failed = failed or not ok
        details.append(f"{pod_name}: GET {check['path']}: {line}")
    if not answered:
        failed = True
    waiting = len(live) - len([pod for pod in live if rr.is_ready(pod)])
    head = f"{len(pods)} Ready {deployment.name} pods" + (f" ({waiting} not Ready yet, not called)" if waiting else "")
    return Check(name, "failed" if failed else "passed", [head, *details])


# Right after a rollout the load balancer can take a minute to reach the new pods' nodes, so the
# edge answers with an origin error (Cloudflare 520-526) or a gateway error. Those are tried again
# for PUBLIC_RETRY_SECONDS before the check fails (orbit-mobile-api 20261022: HTTP 521 for a minute).
PUBLIC_RETRY_STATUSES = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 525, 526})
PUBLIC_RETRY_SECONDS = 120
PUBLIC_RETRY_WAIT = 20


def check_http_public(
    check: dict,
    get: HttpGet = http_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Check:
    """A GET on a public URL of the service (through Cloudflare and the load balancer).

    An edge or gateway error, or no connection, is tried again for PUBLIC_RETRY_SECONDS; any
    other answer is judged at once.
    """
    name = str(check.get("name") or f"public {check['url']}")
    headers = {"Accept": "*/*", "User-Agent": USER_AGENT}
    deadline = clock() + PUBLIC_RETRY_SECONDS
    tries = 0
    while True:
        tries += 1
        started = clock()
        retry = False
        try:
            status, body = get(urllib.request.Request(str(check["url"]), headers=headers))
            ok, line = _expect_ok(status, body, check)
            retry = not ok and status in PUBLIC_RETRY_STATUSES
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            ok, line, retry = False, f"error: {error}", True
        elapsed_ms = (clock() - started) * 1000
        if not retry or clock() >= deadline:
            break
        sleep(PUBLIC_RETRY_WAIT)
    after = f" (after {tries} tries)" if tries > 1 else ""
    return Check(name, "passed" if ok else "failed", [f"GET {check['url']}: {line}, {elapsed_ms:.0f} ms{after}"])


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


# --------------------------------------------------------------------------- capacity (Rule 1) and deploy incomplete (Rule 2)

# The capacity re-judge waits at least this long, also when the rollout's budget (Rule 3 timeout x2,
# counted from the deploy start) is spent.
CAPACITY_MIN_WAIT_SECONDS = 120


def never_updated(record: dict, snapshot: dict[str, dict]) -> list[str]:
    """Rule 2: after a failed deploy step, the Deployments that still run the previous release's digest."""
    if record.get("status") != "deploy_failed" and not record.get("failed_step"):
        return []
    expected = rr.digest_of((record.get("images") or {}).get(rr.IMAGE_ROLE))
    before = (record.get("previous") or {}).get("workloads") or {}
    names = []
    for key, workload in sorted(snapshot.items()):
        old = (before.get(key) or {}).get("digest")
        if old and old != expected and workload.get("digest") == old:
            names.append(workload["name"])
    return names


def incomplete_line(record: dict, names: list[str]) -> str:
    """The one Rule 2 finding: "deploy incomplete at step <id>: N workloads not updated (names)"."""
    step = (record.get("failed_step") or {}).get("id") or "unrecorded"
    shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
    return f"deploy incomplete at step {step}: {len(names)} workloads not updated ({shown})"


def without_workloads(check: Check, names: list[str]) -> Check:
    """The images check without the lines of these Deployments; passed when nothing else is wrong."""
    if not names or not check.details:
        return check
    prefixes = tuple(f"deployment/{name}:" for name in names)
    details = [line for line in check.details[1:] if not line.startswith(prefixes)]
    status = "passed" if check.status == "failed" and not details else check.status
    return Check(check.name, status, [check.details[0], *details])


def rollout_names(check: Check) -> list[str]:
    """The Deployments named by a failed rollouts check ("deployment/<name> rollout ...")."""
    return [
        match.group(1) for line in check.details for match in [re.match(r"deployment/(\S+) rollout", line)] if match
    ]


def capacity_candidates(record: dict, snapshot: dict[str, dict], checks: list[Check]) -> list[str]:
    """Deployments on the release digest whose rollout did not finish or whose new pods are not Ready,
    those of a failed rollouts check, and those of the recorded failed step."""
    expected = rr.digest_of((record.get("images") or {}).get(rr.IMAGE_ROLE))
    names = set()
    for workload in snapshot.values():
        if workload.get("digest") != expected:
            continue
        replicas = workload["replicas"]
        if (
            not workload["rollout_current"]
            or workload["updated_replicas"] < replicas
            or workload["bad_pods"]
            or any(digest != expected for digest in workload["running_digests"])
            or (replicas > 0 and workload["available_replicas"] < math.ceil(replicas * MIN_AVAILABLE_SHARE))
            or workload.get("ready_pods", 0) < workload.get("live_pods", 0)
        ):
            names.add(workload["name"])
    for check in checks:
        if check.name == "rollouts" and check.status == "failed":
            names.update(rollout_names(check))
    names.update((record.get("failed_step") or {}).get("deployments") or [])
    known = {f"deployment/{name}" for name in names} & set(snapshot)
    return sorted(key.split("/", 1)[1] for key in known)


def mark_environmental(env: rc.Environment, checks: list[Check], names: list[str]) -> list[str]:
    """Capacity: the failed availability checks of these Deployments become "environmental"; return their names.

    The rollouts and deploy checks, the pods_ready smoke checks of these Deployments, and the images
    check when every problem in it belongs to them. Every other failed check stays failed.
    """
    pods_ready = {
        str(item.get("name") or f"{item['deployment']} ready")
        for item in env.smoke
        if item.get("type") == "pods_ready" and item.get("deployment") in names
    }
    prefixes = tuple(f"deployment/{name}:" for name in names)
    marked = []
    for check in checks:
        if check.status != "failed":
            continue
        if (
            check.name in ("rollouts", "deploy")
            or check.name in pods_ready
            or (check.name == "images" and all(line.startswith(prefixes) for line in check.details[1:]))
        ):
            check.status = "environmental"
            check.details.append("environmental: capacity, not the release (no rollback)")
            marked.append(check.name)
    return marked


def deploy_capacity(
    record: dict,
    clients: rr.Clients,
    checks: list[Check],
    snapshot: dict[str, dict],
    *,
    rebuild: Callable[[dict[str, dict], Check], list[Check]],
    now: datetime,
    judge: Callable[..., dict] = rcap.judge,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[Check], dict | None]:
    """Rule 1 in the deploy phase: judge the Deployments whose rollout did not finish.

    Not capacity: the checks stay as they are (rollback with suspects, as before). Capacity: wait once
    more (Rule 3 timeout x2 from the deploy start, at least CAPACITY_MIN_WAIT_SECONDS), read again and
    re-judge once. Finished: the checks pass. Still incomplete with capacity evidence: the availability
    checks are "environmental" and the result is inconclusive with cause capacity (no rollback).
    """
    if not any(check.status == "failed" for check in checks):
        return checks, None
    names = capacity_candidates(record, snapshot, checks)
    release = rr.digest_of((record.get("images") or {}).get(rr.IMAGE_ROLE))
    step = (record.get("failed_step") or {}).get("id")
    if step:
        # A named failed step failed for a cause other than capacity: rollout-wait names a step only when
        # its verdict was not capacity, and a failed apply or script names its own. Another workload's
        # capacity never excuses what that step left behind (deep-search #2379).
        return checks, {
            "verdict": "not_capacity",
            "status": "failed",
            "blockers": [f"deploy step {step} failed (not a capacity wait)"],
            "indicators": [],
            "deployments": names,
            "summary": f"not capacity: deploy step {step} failed (not a capacity wait)",
        }
    if record.get("status") == "deploy_failed" and record.get("deploy_steps_named"):
        # This build names every failed step, and a capacity wait sets no deploy_failed at all: an unnamed
        # failure (an apply, a script) is the release's, even while another workload lacks capacity.
        return checks, {
            "verdict": "not_capacity",
            "status": "failed",
            "blockers": ["deploy failed outside a rollout wait (the build names its failed steps)"],
            "indicators": [],
            "deployments": names,
            "summary": "not capacity: deploy failed outside a rollout wait",
        }
    if not names and record.get("status") == "deploy_failed":
        # An older build failed a step without naming it: judge every Deployment on the release.
        names = sorted(item["name"] for item in snapshot.values() if item.get("digest") == release)
    if not names:
        return checks, None
    verdict = judge(clients, names, release, now)
    print(f"capacity verdict ({', '.join(names)}): {verdict['summary']}", flush=True)
    if verdict["verdict"] != "capacity":
        return checks, {**verdict, "deployments": names}
    started = rr.parse_utc(record["times"]["deploy_started"])
    elapsed = max(0.0, (now - started).total_seconds())
    timeouts = rr.scaled_rollout_timeouts(
        clients, names, minimum=CAPACITY_MIN_WAIT_SECONDS, factor=rcap.EXTENDED_WAIT_FACTOR, elapsed=elapsed
    )
    print(f"Capacity, not the release: waiting once more ({timeouts})", flush=True)
    begun = clock()
    # Polled, not `kubectl rollout status`: that fails at once after the progress deadline (deep-search #2379).
    again = rcap.poll_rollouts(clients, timeouts)
    fresh = rr.snapshot_workloads(clients)
    rollouts = Check(
        "rollouts", "failed" if again else "passed", [f"{len(names)} Deployments after the capacity wait", *again]
    )
    rechecked = rebuild(fresh, rollouts)
    info = {**verdict, "deployments": names, "extended_wait_seconds": timeouts}
    remaining = capacity_candidates(record, fresh, rechecked)
    if not remaining:
        # Rolled out. A failed deploy step (its rollout timed out) was the capacity; anything else that
        # still fails (a smoke check on Ready pods) is the release's, as before.
        marked = mark_environmental(clients.env, rechecked, names)
        return rechecked, {**info, "finished": not marked, **({"environmental_checks": marked} if marked else {})}
    second = judge(clients, remaining, release, now + timedelta(seconds=clock() - begun))
    print(f"capacity re-judge ({', '.join(remaining)}): {second['summary']}", flush=True)
    result = {**second, "deployments": remaining, "extended_wait_seconds": timeouts, "first": verdict["summary"]}
    if second["verdict"] == "capacity":
        result["environmental_checks"] = mark_environmental(clients.env, rechecked, remaining)
        result["finished"] = False
    return rechecked, result


def deploy_checks(
    record: dict,
    clients: rr.Clients,
    snapshot: dict[str, dict],
    rollouts: Check | None,
    *,
    pod_get: PodGet | None,
    get: HttpGet,
    force_fail: str,
) -> list[Check]:
    """The checks of the deploy phase: images (Rule 2 applied), rollouts, deploy, smoke, forced failure."""
    stale = never_updated(record, snapshot)
    checks = [without_workloads(check_images(record, snapshot), stale)]
    if rollouts is not None:
        checks.append(rollouts)
    if stale:
        checks.append(Check("deploy", "failed", [incomplete_line(record, stale)]))
    elif record.get("status") == "deploy_failed":
        # The old pods can pass every check while the release never fully deployed.
        checks.append(Check("deploy", "failed", ["the deploy step failed (status deploy_failed)"]))
    checks += smoke_checks(clients, snapshot, pod_get=pod_get, get=get)
    if force_fail:
        checks.append(Check("forced_failure", "failed", [f"forced failure (test run): {force_fail}"]))
    return checks


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
    for commit in (record.get("commits") or [])[-rs.MAX_SUSPECT_COMMITS :]:
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
        ranked.append(
            {**commit, "score": score if source or deploy else 0, "reason": reason if files else "files unknown"}
        )
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
    return sorted(
        [item for item in ranked if item.get("traced") or item["score"] > 0],
        key=lambda item: (not item.get("traced"), -item["score"]),
    )


def rollback_suggestion(
    service: rc.Service,
    record: dict,
    regressions: list[dict],
    phase: str,
    token: str | None,
    target: dict | None = None,
    commits: list[dict] | None = None,
) -> dict:
    """What to roll back to and why.

    target: an earlier release than record["previous"] (a new error traced to an older unjudged
    release). commits: the commits of every judged release (default: this release's); the
    suspected PRs rank them, so the hold after the rollback names the PRs that caused the
    failure, also when they came in an earlier release.
    """
    previous = target or record.get("previous") or {}
    if commits is not None:
        record = {**record, "commits": commits}
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
            f"{rollback_cli(service, 'plan', target)}     # dry run: prints every Deployment change",
            f"{rollback_cli(service, 'run', target)} --reason '<why>'     # starts the rollback build",
        ],
        "note": (
            "The watch-window build starts the rollback itself; a human can also start it."
            if service.auto_rollback
            else "Tier check_only: a human starts the rollback."
        ),
    }


def rollback_to_flag(target: dict | str | None) -> str:
    """The --to of a rollback command: empty for the default ("previous"), else the suggested target."""
    if isinstance(target, str):
        return "" if target in ("", "previous") else f" --to {target}"
    to = (target or {}).get("rollback_to") or (target or {}).get("release_id")
    return f" --to {to}" if to else ""


def rollback_cli(service: rc.Service, sub: str, target: dict | str | None = None) -> str:
    """`release_rollback.py <plan|run> [--service S] [--to T]`: the flags after the subcommand, as its parser takes them."""
    return service.kit_command("release_rollback.py", sub) + rollback_to_flag(target)


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
    observation_state: Callable | None = None,
) -> dict:
    """Run the checks of one phase and return the result block for record["checks"].

    With a store, the window phase also runs the new-error checks (release_signals):
    a new error with a clear suspect is a regression, one without is a warning.
    force_fail (test runs) adds a failed check, to prove that a failure stops the promote.
    """
    checks = []
    rollouts = None
    if wait_rollouts_seconds > 0:
        names = [item.name for item in clients.env.deployments]
        started = (record.get("times") or {}).get("deploy_started")
        elapsed = max(0.0, (now - rr.parse_utc(started)).total_seconds()) if started else 0.0
        # Rule 3: each Deployment's own rollout timeout, less the time the deploy already spent.
        timeouts = rr.scaled_rollout_timeouts(clients, names, minimum=wait_rollouts_seconds, elapsed=elapsed)
        begun = time.monotonic()
        failures = rr.wait_rollouts(clients, names, timeouts=timeouts)
        # Later budgets (the capacity wait) count the time this wait took.
        now = now + timedelta(seconds=max(0.0, time.monotonic() - begun))
        rollouts = Check("rollouts", "failed" if failures else "passed", [f"{len(names)} Deployments", *failures])
    snapshot = rr.snapshot_workloads(clients)
    capacity = None
    stale = never_updated(record, snapshot)
    if phase == "deploy":

        def rebuild(fresh: dict[str, dict], rolled: Check | None) -> list[Check]:
            """The deploy checks of one workload snapshot."""
            return deploy_checks(record, clients, fresh, rolled, pod_get=pod_get, get=get, force_fail=force_fail)

        checks = rebuild(snapshot, rollouts)
        checks, capacity = deploy_capacity(record, clients, checks, snapshot, rebuild=rebuild, now=now)
    else:
        checks = [without_workloads(check_images(record, snapshot), stale)]
        if rollouts is not None:
            checks.append(rollouts)
        if stale:
            # Rule 2 in the window: one finding; environmental when the deploy phase judged capacity.
            deploy_cause = ((record.get("checks") or {}).get("deploy") or {}).get("cause")
            checks.append(
                Check(
                    "deploy",
                    "environmental" if deploy_cause == "capacity" else "failed",
                    [incomplete_line(record, stale)],
                )
            )
        if metrics_error and service.metrics:
            checks.append(Check("metrics", "skipped", [metrics_error]))
    findings: list[Finding] = []
    recovered: list[str] = []
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
                    judged_commits=tuple(item["commit"] for item in chain[1:]),
                )
            except GuardError as error:
                checks.append(Check("metrics", "skipped", [f"Datadog query failed: {error}"]))
            if store is not None and findings:
                marks = environmental_signals(store)
                apply_environmental(findings, marks)
                recovered = recovered_signals(service, findings, marks)
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
    availability = None
    if phase == "window":

        def read_fresh(deadline):
            """Use one remaining budget for every CLI in this workload sample."""

            def bounded(command, timeout=120):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("workload observation deadline reached")
                return clients.runner(command, timeout=min(timeout, remaining))

            try:
                fresh = rr.snapshot_workloads(replace(clients, runner=bounded), resolve_tags=False)
            except (rr.RecordError, GuardError) as error:
                raise RuntimeError(str(error)) from error
            return asdict(without_workloads(check_images(record, fresh, window=True), stale)), fresh

        checked, availability = ro.confirm_images(read_fresh, state=observation_state or (lambda: (None, None)))
        checks = [Check(**checked) if check.name == "images" else check for check in checks]
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
    warnings += [
        {
            "kind": "metric",
            "name": f"{item.metric}:{item.scope}",
            "text": f"{item.line()}; the other versions in prod show it too, so it does not roll back",
            "suspects": [],
            "candidates": [],
        }
        for item in findings
        if item.status == "environmental"
    ]
    judged_metrics = {metric.name for metric in service.metrics if metric.judged}
    judged = sum(
        item.candidate_samples for item in findings if item.metric in judged_metrics and item.status != "insufficient"
    )
    parts = (new_errors or {}).get("parts") or {}
    signals_checked = [name for name, part in parts.items() if part.get("status") == "checked"]
    unresolved = bool(capacity and capacity.get("verdict") == "capacity" and not capacity.get("finished"))
    return {
        "capacity": capacity,
        **({"cause": "capacity"} if unresolved else {}),
        "availability": availability,
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
        "recovered_signals": recovered,
        "result": "failed"
        if regressions
        else "inconclusive"
        if unresolved or (availability and availability["status"] != "confirmed")
        else "passed",
    }


def final_verdict(service: rc.Service, result: dict) -> tuple[str, str]:
    """(status, reason) of a passed last checkpoint: "passed" only when enough was judged.

    With judged metrics configured: their samples must reach the service's min_judged
    (at least 1). Without them: the new-error log check must have run.
    """
    if (result.get("availability") or {}).get("status") != "confirmed" and result.get("availability"):
        return "inconclusive", "fresh workload availability is unconfirmed"
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
    early = bool(result.get("early"))
    key = (
        result["phase"]
        if result["phase"] == "deploy"
        else f"early_{result['candidate_minutes']:.0f}m"
        if early
        else f"window_{result.get('checkpoint_minutes', result['candidate_minutes']):.0f}m"
    )
    suggestion = None
    # Dev has nothing to roll back: a failed dev verify only stops the promote.
    if result["result"] == "failed" and record["environment"] == "prod":
        target = None
        new_errors = result.get("new_errors") or {}
        if any(item["kind"] == "new_error" for item in result["regressions"]):
            # A new error traced to an earlier unjudged release rolls back past that release.
            target = rs.rollback_target(record, store, new_errors.get("problems") or [], new_errors.get("scope") or {})
        chain = result.get("chain") or []
        if len(chain) > 1 and any(item["kind"] != "new_error" for item in result["regressions"]):
            # A metric or check judged over several unjudged releases cannot tell which one regressed:
            # roll back to what ran before the oldest of them, also when a new error names a later one.
            oldest = rs._read(store, chain[-1]) or {}
            previous = oldest.get("previous") or {}
            target = {
                "release_id": previous.get("release_id"),
                "commit": previous.get("commit"),
                "images": previous.get("images"),
                "rollback_to": f"previous-of:{chain[-1]}",
            }
        # The commits of the releases this rollback removes: the hold must name only those PRs.
        removed = chain[:1]
        rollback_to = str((target or {}).get("rollback_to") or "")
        if rollback_to.startswith("previous-of:"):
            culprit = rollback_to.split(":", 1)[1]
            removed = chain[: chain.index(culprit) + 1] if culprit in chain else chain
        commits = None
        if len(removed) > 1:
            commits = []
            for release_id in reversed(removed):
                commits += (rs._read(store, release_id) or {}).get("commits") or []
        suggestion = rollback_suggestion(service, record, result["regressions"], key, token, target, commits=commits)
    fresh: list[dict] = []

    def change(current: dict) -> None:
        """Save the result, collect the fresh warnings and set the verdict (a failed verdict stays)."""
        if early:
            # Before any checkpoint is due: kept apart, so the watch still runs +15 and the rest.
            ro.note_early_check(current, result, key)
        else:
            current.setdefault("checks", {})[key] = result
            ro.note_checkpoint(current, result, key)
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
            if early or ro.later_terminal(verdict, key):
                # An early check decides nothing but a failure. An older checkpoint (also with --final)
                # never replaces a later terminal verdict; a new failure above still wins.
                pass
            elif result.get("cause") == "capacity" and result["result"] == "inconclusive":
                # Rule 1: capacity, not the release. No rollback, no suspects, no hold; the +15/+60 checks
                # judge the running release as usual.
                verdict = {
                    "status": "inconclusive",
                    "at": result["at"],
                    "phase": key,
                    "cause": "capacity",
                    "reason": "capacity: "
                    + ", ".join((result.get("capacity") or {}).get("deployments") or [])
                    + " did not finish rolling out after the extended wait; environmental, no rollback",
                }
            elif (result.get("availability") or {}).get("status") in ("unconfirmed", "superseded"):
                verdict = {
                    "status": "inconclusive",
                    "unchecked": True,
                    "at": result["at"],
                    "phase": key,
                    "reason": "fresh workload availability is unconfirmed; investigate capacity before reverting",
                    "cause": "availability_unconfirmed",
                }
            elif not final:
                verdict = {"status": "pending", "at": result["at"], "phase": key}
            else:
                status, reason = final_verdict(service, result)
                verdict = {"status": status, "at": result["at"], "phase": key, "judged": result["judged"]}
                if reason:
                    verdict["reason"] = reason
                parts = ((result.get("new_errors") or {}).get("parts")) or {}
                unread = any(part.get("status") == "inconclusive" for part in parts.values())
                if status == "inconclusive" or unread:
                    # Unchecked: the next release's watch judges this release again (release_signals.release_scope),
                    # also a passed one whose new-error source could not run.
                    verdict["unchecked"] = True
                if current.get("warnings"):
                    verdict["warnings"] = len(current["warnings"])
        current["verdict"] = verdict

    saved = rr.update_record(store, record["release_id"], change, f"verify_{key}", {"result": result["result"]})
    result["fresh_warnings"] = list(fresh)
    return saved


def post_warnings(
    service: rc.Service, store: rr.Store, record: dict, warnings: list[dict], command: str | None = None
) -> None:
    """Post the fresh warnings; mark them posted only when Slack took them. Never raises.

    command: how a human rolls back (default: release_rollback.py run; light services pass their own).
    """
    if not warnings or not rr.post_slack(warning_line(service, record, warnings, store, command)):
        return
    names = {warning["name"] for warning in warnings}

    def mark(current: dict) -> None:
        """Mark the posted warnings as posted."""
        for name, entry in (current.get("warnings") or {}).items():
            if name in names:
                entry["posted"] = True

    try:
        rr.update_record(store, record["release_id"], mark, "warnings_posted", {"count": len(names)})
    except (rr.RecordError, GuardError, OSError, ValueError, KeyError) as error:
        print(f"WARNING: could not mark the warnings posted: {error}", file=sys.stderr, flush=True)


def warning_line(
    service: rc.Service, record: dict, warnings: list[dict], store: rr.Store, command: str | None = None
) -> str:
    """Slack: findings that do not roll back (no clear suspect, no runtime code). A human decides."""
    where = store.location(rr.record_name(record["release_id"]))
    items = " | ".join(_clip(item["text"], 300) for item in warnings[:3])
    how = command or f"{rollback_cli(service, 'run')} --reason '<why>'"
    return (
        f":warning: {service.title} release-verify {record['commit'][:9]}: {len(warnings)} finding(s) that do not "
        f"roll back on their own: {items}. Please look; roll back with "
        f"{how} if the release caused it. Record {where}"
    )


def inconclusive_parts(result: dict) -> str:
    """A sentence that names the new-error checks that did not run; "" when all ran."""
    parts = ((result.get("new_errors") or {}).get("parts")) or {}
    names = [name for name, part in sorted(parts.items()) if part.get("status") == "inconclusive"]
    return f" New-error checks not run: {', '.join(names)}." if names else ""


def slack_line(service: rc.Service, record: dict, result: dict, store: rr.Store) -> str:
    """The Slack line for a check result: failed, passed, inconclusive, or the checks passed so far."""
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
        if verdict.get("cause") == "capacity":
            return (
                f":grey_question: {title} release-verify {short} deploy incomplete for capacity (environmental: no "
                f"rollback, no hold): {str((result.get('capacity') or {}).get('summary'))[:300]}. The +15/+60 checks "
                f"judge the running release. Record {where}"
            )
        return (
            f":grey_question: {title} release-verify {short} inconclusive: {str(verdict.get('reason'))[:160]}."
            f"{inconclusive_parts(result)} Record {where}"
        )
    if result["phase"] == "deploy":
        minutes = " and ".join(f"+{value}" for value in service.checkpoints)
        capacity = result.get("capacity") or {}
        note = (
            f" Capacity (environmental, no rollback): the rollout finished after an extended wait; "
            f"{str(capacity.get('summary'))[:200]}."
            if capacity.get("verdict") == "capacity"
            else ""
        )
        return (
            f":mag: {title} release-verify {short} deploy checks passed;{note} window checks follow at {minutes} min. "
            f"Record {where}"
        )
    return f":mag: {title} release-verify {short} window checks at +{result['candidate_minutes']:.0f} min passed. Record {where}"


def print_result(result: dict) -> None:
    """Print the result: the new errors, the checks and the metric findings."""
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
    """The time to evaluate at: --now, else now."""
    return rr.parse_utc(args.now) if args.now else datetime.now(rr.UTC)


def _datadog(service: rc.Service, clients: rr.Clients) -> tuple[DatadogClient | None, str | None]:
    """A Datadog client when the service has Datadog checks; else (None, the reason when the keys are missing)."""
    signals = service.signals
    if not (service.metrics or signals.monitors or signals.error_tracking or signals.failure_series):
        return None, None
    try:
        return datadog_from_environment(clients), None
    except MetricsUnavailable as error:
        print(f"WARNING: {error}", file=sys.stderr, flush=True)
        return None, str(error)


def _token(env: rc.Environment) -> str | None:
    """The GitHub token of the environment, or None (with a warning) when there is none."""
    try:
        return rr.github_token(env)
    except GuardError as error:
        print(f"WARNING: no GitHub token, suspected PRs carry no file lists: {error}", file=sys.stderr)
        return None


def _pod_get(clients: rr.Clients, env: rc.Environment) -> PodGet | None:
    """The pod proxy GET when the environment has http_pod smoke checks; None when there are none or no client."""
    if not any(check.get("type") == "http_pod" for check in env.smoke):
        return None
    try:
        return KubeApi(clients, GoogleApi(env.project, env.region).access_token).pod_get
    except (rr.RecordError, KeyError, IndexError, ValueError, ssl.SSLError) as error:
        print(f"WARNING: no Kubernetes API client for the pod checks: {error}", file=sys.stderr, flush=True)
        return None


def require_owner(store: rr.Store, record: dict) -> bool:
    """Missing ownership stays red even when the bookkeeping write is unavailable."""
    if ro.owned(record):
        return True
    claimed = [False]

    def missing(current):
        claimed[0] = ro.owned(current)
        if not claimed[0]:
            current.setdefault("followup", {}).update(state="owner_missing")

    try:
        rr.update_record(store, record["release_id"], missing, "followup_owner_missing")
    except (rr.RecordError, GuardError, OSError):
        pass
    if not claimed[0]:
        rr.post_slack(f":warning: release {record['release_id']} has no deploying-agent owner for +15/+60 recovery")
    return claimed[0]


def cmd_check(
    args: argparse.Namespace,
    service: rc.Service,
    env: rc.Environment,
    store: rr.Store,
    clients: rr.Clients,
) -> int:
    """Run one phase of checks, save the result, and alert; return 1 when the verdict is failed."""
    record = rr.read_record(store, args.release_id)
    dd, metrics_error, logs = None, None, None
    token = _token(env)
    if args.phase == "window" and not args.no_metrics:
        dd, metrics_error = _datadog(service, clients)
        logs = rs.CloudLogging(env.project, GoogleApi(env.project, env.region).access_token)
    historical = bool(getattr(args, "now", None))
    checkpoint = None
    if args.phase == "window" and not historical:
        finished = rr.parse_utc(record["times"].get("deploy_finished") or record["times"]["deploy_started"])
        elapsed = (_now(args) - finished).total_seconds() / 60
        checkpoint = ro.scheduled_minute(record, elapsed, getattr(args, "checkpoint_minutes", None))
        if checkpoint is None:
            print(
                f"Early check at +{elapsed:.0f} min: no scheduled checkpoint is due yet. The result is kept "
                "apart and completes no checkpoint; the watch still runs every scheduled one.",
                flush=True,
            )
        else:
            rr.update_record(
                store,
                record["release_id"],
                lambda current: ro.begin_checkpoint(current, f"window_{checkpoint}m"),
                "checkpoint_started",
            )
    try:
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
            observation_state=(lambda: prod_state(service, store, record))
            if args.phase == "window" and env.name == "prod" and not historical
            else None,
        )
        if historical:
            print_result(result)
            print(
                "Historical evaluation only; no release, ownership or recovery state changed",
                flush=True,
            )
            return EXIT_OK if result["result"] == "passed" else EXIT_FAILED
        if checkpoint is not None:
            result["checkpoint_minutes"] = checkpoint
        elif args.phase == "window":
            result["early"] = True
        if args.phase == "window" and env.name == "prod":
            ended, busy = prod_state(service, store, record)
            if ended:
                # Only a replaced release closes its open checkpoints.
                rr.update_record(
                    store,
                    record["release_id"],
                    lambda current: ro.supersede(current, ended),
                    "verify_superseded",
                )
                print(f"Workload observation superseded: {ended}; result not recorded", flush=True)
                return EXIT_ERROR
            if busy:
                # Another build only runs: it may never deploy, so every checkpoint stays open.
                if checkpoint is not None:
                    _record_checkpoint_error(
                        store, record, checkpoint, f"{busy}; result not recorded, checkpoint stays open", final=False
                    )
                print(f"Workload observation interrupted: {busy}; result not recorded", flush=True)
                return EXIT_ERROR
        if args.phase == "window" and env.name == "prod":
            history = builds_since_rollback(service, record)
            if history:
                if checkpoint is not None:
                    _record_checkpoint_error(
                        store, record, checkpoint, f"another build changed prod: {history[0]}", final=True
                    )
                print(f"Another build changed prod: {history[0]}; result not recorded", flush=True)
                return EXIT_FAILED
        print_result(result)
        # The marker comes from the result itself, so a failed record write cannot hide a failed check.
        if args.failed_marker and result["result"] == "failed":
            Path(args.failed_marker).write_text("true\n", encoding="utf-8")
        if result.get("early") and args.final:
            print("--final is ignored: an early check cannot set the final verdict", flush=True)
            args.final = False
        saved = apply_result(service, store, record, result, final=args.final, token=token)
        owner_ok = require_owner(store, saved) if args.phase == "window" and env.name == "prod" else True
        if args.phase == "window":
            note_recovered(service, store, saved, result.get("recovered_signals") or [])
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
            return (
                EXIT_FAILED
                if saved["verdict"]["status"] == "failed" or args.final and saved["verdict"]["status"] == "inconclusive"
                else EXIT_OK
            )
        if args.force_fail and saved["verdict"]["status"] == "failed":
            rr.post_slack(
                f"{TEST_RUN_LINE}. A test of {service.title} release-verify forced a failure for {record['commit'][:9]}; "
                "the window build of a test run never rolls back. No action needed."
            )
        else:
            rr.post_slack(slack_line(service, saved, result, store))
        if args.failed_marker and saved["verdict"]["status"] == "failed":
            Path(args.failed_marker).write_text("true\n", encoding="utf-8")
        if args.final and not owner_ok:
            return EXIT_FAILED
        return (
            EXIT_FAILED
            if saved["verdict"]["status"] == "failed" or args.final and saved["verdict"]["status"] == "inconclusive"
            else EXIT_OK
        )

    except (
        rr.RecordError,
        GuardError,
        subprocess.SubprocessError,
        OSError,
        ValueError,
        KeyError,
    ) as error:
        if checkpoint is not None:
            _record_checkpoint_error(store, record, checkpoint, str(error), final=args.final)
            return EXIT_FAILED
        raise


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
        """Point at this commit unless the known commit is newer or the order is unknown."""
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


def inconclusive_reason(result: dict) -> str:
    """Why a check result is inconclusive, in one line (the unresolved checks)."""
    unresolved = [
        f"{check['name']}: {'; '.join(check.get('details') or [])[:200]}"
        for check in result.get("checks") or []
        if check.get("status") not in ("passed", "failed")
    ]
    return ("; ".join(unresolved) or "the checks could not decide")[:500]


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
    # Only a passed result verifies the commit. An inconclusive one (a rollout that capacity holds back)
    # is not verified: the gate refuses the promote until a dev build passes.
    status = "passed" if result["result"] == "passed" else "inconclusive"
    if failed:
        status = "superseded" if superseded_by else "failed"
    verdict = {
        "status": status,
        "at": result["at"],
        "phase": "dev_verify",
        **({"reasons": [r["text"] for r in result["regressions"]]} if failed else {}),
        **({"superseded_by": superseded_by} if status == "superseded" else {}),
        **({"reason": inconclusive_reason(result)} if status == "inconclusive" else {}),
    }

    def change(current: dict) -> None:
        """Set the dev verify verdict."""
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
            # The light kinds (Cloud Run revisions, a Composer candidate folder): what prod must promote.
            **({"artifacts": saved["artifacts"]} if saved.get("artifacts") else {}),
            "build": saved.get("build"),
            "updated_at": rr.utc_now(),
        },
    )
    if status == "passed":
        _point_verified(store, service.dev or service.prod, saved, token)
    elif status == "superseded":
        print(f"Dev verify superseded, nothing to promote: {superseded_by}", flush=True)
    elif status == "inconclusive":
        link = f" <{build_url}|the dev build>" if build_url else ""
        line = (
            f":grey_question: {service.title} dev verify inconclusive for `{saved['commit'][:9]}`: "
            f"{verdict['reason']}. Not verified, so prod does not promote it; a later dev build that passes does.{link}"
        )
        print(line, flush=True)
        rr.post_slack(line)
    else:
        location = store.location(rr.record_name(saved["release_id"]))
        rr.post_slack(*dev_verify_alert(service, saved, build_url, location, test_run))
    return saved


def _valid_people(people: object) -> bool:
    """{"fallback": [str, ...], "slack_ids": {str: str}}; both keys optional."""
    if not isinstance(people, dict):
        return False
    slack_ids = people.get("slack_ids") or {}
    fallback = people.get("fallback") or []
    return (
        isinstance(slack_ids, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in slack_ids.items())
        and isinstance(fallback, list)
        and all(isinstance(value, str) for value in fallback)
    )


def load_people(path: Path | None = None) -> dict:
    """GitHub login -> Slack id map and fallback mentions.

    The vendored release_people.json (private repositories), else the RELEASE_PEOPLE_JSON
    environment variable (public repositories: the builds read it from Secret Manager). Missing,
    unreadable or wrongly shaped: a warning and an empty map, so the alert still goes out."""
    empty = {"slack_ids": {}, "fallback": []}
    path = path or PEOPLE_FILE
    if path.is_file():
        source = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            print(f"WARNING: cannot read {source}: {error}; the alert mentions nobody", file=sys.stderr, flush=True)
            return empty
    elif os.environ.get("RELEASE_PEOPLE_JSON", "").strip():
        source, text = "RELEASE_PEOPLE_JSON", os.environ["RELEASE_PEOPLE_JSON"]
    else:
        return empty
    try:
        people = json.loads(text)
    except ValueError:
        print(f"WARNING: {source} is not valid JSON; the alert mentions nobody", file=sys.stderr, flush=True)
        return empty
    if not _valid_people(people):
        print(f"WARNING: {source} has the wrong shape; the alert mentions nobody", file=sys.stderr, flush=True)
        return empty
    return {"slack_ids": dict(people.get("slack_ids") or {}), "fallback": list(people.get("fallback") or [])}


def _clip(text: str, limit: int) -> str:
    """The text on one line, cut to limit characters with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _slack_escape(text: str) -> str:
    """Escape &, < and > for a Slack message."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pr_title(item: dict) -> str:
    """The PR title without the (#123) PR number."""
    return rr.PR_IN_TITLE_RE.sub("", str(item.get("title", ""))).strip()


def _mentions(people: dict, items: list[dict]) -> list[str]:
    """Slack mentions of the authors of the items, once each; the fallback people when none is known."""
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
        """One Slack line for a change: the PR or commit link, the title and the author."""
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
    """The suspected PRs worth a human's look: only commits with runtime/deploy or trace evidence."""
    suspects = (record.get("rollback_suggestion") or {}).get("suspected_prs") or []
    return [
        item
        for item in suspects
        if item.get("traced") or item.get("score", 0) > 0 and item.get("reason") != "files unknown"
    ][:3]


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
        "PRs with runtime/deploy or trace evidence (ranking is not causal proof):",
        *(
            []
            if likely_prs(record)
            else ["- No release suspect identified; investigate scheduling, preemption and health."]
        ),
        *[
            f"- #{item.get('pr') or '?'} {_clip(_pr_title(item), 150)} ({_clip(item.get('reason', ''), 80)})"
            for item in likely_prs(record)
        ],
    ]
    steps = [
        f"In {service.github_repo}:",
        f"1. Read {service.docs} and the release record: gcloud storage cat {location}",
        "2. Determine whether this is a release regression or infrastructure/capacity failure; PR ranking is not causal proof. Confirm it with the logs or metrics "
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


def suggested_command(service: rc.Service, record: dict, sub: str = "run") -> str:
    """The rollback command for this release's suggested target (with --to when it is not the default)."""
    target = (record.get("rollback_suggestion") or {}).get("target") or None
    if (
        target
        and (target.get("release_id") == (record.get("previous") or {}).get("release_id"))
        and not target.get("rollback_to")
    ):
        target = None
    return rollback_cli(service, sub, target)


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
        """One Slack line for a PR: the link, the title and the author (a mention when known)."""
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
        manual = command or f"{suggested_command(service, record)} --reason '<why>'"
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
                        if getattr(service, "has_gate", True)
                        else f"Record `{location}` • {service.release_branch} still has the change; the next merge "
                        "deploys as usual (no hold for this service)."
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
    prod build is changing the cluster (it holds the prod lock, or it takes no lock and has passed
    its first cluster change): its result decides whether this release still runs. A build that
    is still building, waiting for its turn or waiting at the gate changes nothing, so the checks go on.
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
    lock = rr.lock_holder(store, rr.build_alive(service.prod))
    if lock and lock.get("holder") != own_build:
        return None, f"the {lock.get('kind')} build {lock.get('holder')} holds the prod lock"
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
    args: argparse.Namespace,
    service: rc.Service,
    env: rc.Environment,
    store: rr.Store,
    clients: rr.Clients,
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
        on_failure(service, store, record, _datadog(service, clients)[0])
        return EXIT_OK if require_owner(store, rr.read_record(store, record["release_id"])) else EXIT_FAILED
    finished = rr.parse_utc(record["times"].get("deploy_finished") or record["times"]["deploy_started"])
    checkpoints = sorted(int(value) for value in args.checkpoints.replace(",", " ").split())
    checkpoints = ro.restore_extensions(record, checkpoints)
    dd, metrics_error = _datadog(service, clients)
    token = _token(env)
    logs = rs.CloudLogging(env.project, GoogleApi(env.project, env.region).access_token)
    index = 0
    while index < len(checkpoints):
        minutes = checkpoints[index]
        last = index == len(checkpoints) - 1
        index += 1
        current = rr.read_record(store, record["release_id"])
        receipt = ((current.get("followup") or {}).get("checkpoints") or {}).get(f"window_{minutes}m") or {}
        if (
            receipt.get("state") == "completed"
            and not getattr(args, "retry_completed", False)
            and not ro.needs_final_decision(current, receipt, last)
        ):
            print(f"Checkpoint +{minutes} min already completed; preserved", flush=True)
            continue
        due = finished + timedelta(minutes=minutes)
        wait = (due - datetime.now(rr.UTC)).total_seconds()
        if wait > 0:
            print(f"Waiting {wait / 60:.1f} min for the +{minutes} min checkpoint", flush=True)
            time.sleep(wait)
        try:
            if reason := wait_until_settled(service, store, record):
                rr.update_record(
                    store,
                    record["release_id"],
                    lambda current, why=reason: ro.supersede(current, why),
                    "verify_superseded",
                    {"by": reason},
                )
                print(f"Superseded by {reason}; the watch stops", flush=True)
                return EXIT_OK
            rr.update_record(
                store,
                record["release_id"],
                lambda current, checkpoint=minutes: ro.begin_checkpoint(current, f"window_{checkpoint}m"),
                "checkpoint_started",
            )
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
                observation_state=lambda: prod_state(service, store, record),
            )
            result["checkpoint_minutes"] = minutes
            print_result(result)
            ended, busy = prod_state(service, store, record)
            busy = busy or ((result.get("availability") or {}).get("changed") if not ended else None)
            if ended:
                rr.update_record(
                    store,
                    record["release_id"],
                    lambda current: ro.supersede(current, ended),
                    "verify_superseded",
                    {"by": ended},
                )
                print(f"Superseded by {ended} during the check; the result is dropped", flush=True)
                return EXIT_OK
            if busy:
                # Prod changed under the check: drop the result and check this checkpoint again.
                print(f"{busy} during the check; the checkpoint runs again", flush=True)
                index -= 1
                continue
            # A mutating build may start AND end between state polls without moving latest.json.
            # Its images are not evidence against the original release. Keep the window unchecked.
            history = builds_since_rollback(service, record)
            if history:
                reason = f"another build changed prod during this release's observation: {history[0]}"
                _record_checkpoint_error(store, record, minutes, reason, final=True)
                rr.post_slack(f":warning: release-verify {record['release_id']} unchecked: {reason}")
                return EXIT_FAILED
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
                rr.update_record(
                    store,
                    record["release_id"],
                    lambda current, extra=minutes + args.extend_minutes: ro.extend_plan(current, extra),
                    "checkpoint_extended",
                )
                checkpoints.append(minutes + args.extend_minutes)
            saved = apply_result(service, store, record, result, final=last and not extend, token=token)
            require_owner(store, saved)
            post_warnings(service, store, saved, result["fresh_warnings"])
            note_recovered(service, store, saved, result.get("recovered_signals") or [])
            alert_marks(store)
            if saved["verdict"]["status"] == "inconclusive" and not last:
                rr.post_slack(slack_line(service, saved, result, store))
        except (
            rr.RecordError,
            GuardError,
            subprocess.SubprocessError,
            rc.ConfigError,
            OSError,
            ValueError,
            KeyError,
        ) as error:
            print(
                f"Checkpoint +{minutes} min could not run: {error}",
                file=sys.stderr,
                flush=True,
            )
            _record_checkpoint_error(store, record, minutes, str(error), final=last)
            rr.post_slack(
                f":warning: {service.title} release-verify {record['commit'][:9]} +{minutes} min check could not run: "
                f"{str(error)[:200]}"
            )
            if last:
                return EXIT_FAILED  # a failed receipt write cannot make a failed checker green
            continue
        if saved["verdict"]["status"] == "failed" or (last and not extend):
            rr.post_slack(slack_line(service, saved, result, store))
            if saved["verdict"]["status"] == "failed":
                on_failure(service, store, saved, dd)
            return (
                EXIT_FAILED
                if saved["verdict"]["status"] == "inconclusive"
                or not ro.owned(rr.read_record(store, record["release_id"]))
                else EXIT_OK
            )
    final_record = rr.read_record(store, record["release_id"])
    return (
        EXIT_FAILED
        if (final_record.get("verdict") or {}).get("status") not in ("passed", "warning", "failed")
        or not ro.owned(final_record)
        else EXIT_OK
    )


def _record_checkpoint_error(store: rr.Store, record: dict, minutes: int, error: str, *, final: bool) -> None:
    """Record that a checkpoint could not run; the final one makes the verdict inconclusive."""

    def change(current: dict) -> None:
        """On the final checkpoint, set the inconclusive verdict unless the record failed or a later one decided."""
        current.setdefault("followup", {}).setdefault("checkpoints", {}).setdefault(f"window_{minutes}m", {}).update(
            state="error", last_error=error[:500], heartbeat_at=rr.utc_now()
        )
        verdict = current.get("verdict") or {}
        if final and verdict.get("status") != "failed" and not ro.later_terminal(verdict, f"window_{minutes}m"):
            current["verdict"] = {
                "status": "inconclusive",
                "unchecked": True,
                "at": rr.utc_now(),
                "phase": f"window_{minutes}m",
                "reason": f"check could not run: {error[:300]}",
            }

    try:
        rr.update_record(store, record["release_id"], change, "verify_checkpoint_error", {"error": error[:500]})
    except (rr.RecordError, GuardError) as write_error:
        print(f"WARNING: could not record the checkpoint error: {write_error}", file=sys.stderr, flush=True)


def on_failure(service: rc.Service, store: rr.Store, record: dict, dd: DatadogClient | None = None) -> None:
    """A failed prod release: start the rollback (tiers with automatic rollback), else ping the author.

    After an automatic rollback, watch_rollback judges the regressed metrics on the rollback target.
    """
    if service.auto_rollback:
        if start_rollback(service, store, record, followed=lambda status: status == "SUCCESS"):
            print(f"After the rollback: {watch_rollback(service, store, dd, record)}", flush=True)
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
# How long to wait for another build that holds the prod lock: it decides its gate, then deploys or skips.
LOCK_HOLDER_WAIT_SECONDS = 2700
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
        """Remove the rollback claim when it is still ours."""
        if (current.get("auto_rollback") or {}).get("claim") == claim_id:
            current.pop("auto_rollback")

    try:
        rr.update_record(store, release_id, release, "auto_rollback_released", {"claim": claim_id})
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not release the rollback claim {claim_id}: {error}", file=sys.stderr, flush=True)


def builds_since_rollback(service: rc.Service, rollback: dict) -> list[str]:
    """Any deploy/rollback that could have changed prod during the observation, including ended builds."""
    api = GoogleApi(service.project, service.region)
    finished = rr.parse_utc(rollback["times"]["deploy_finished"])
    since = finished.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    overlap = f'(finish_time>="{since}" OR status="PENDING" OR status="QUEUED" OR status="WORKING")'
    names = " OR ".join(f'substitutions.TRIGGER_NAME="{name}"' for name in service.prod.deploy_triggers)
    deploys = api.list_builds(f"({names}) AND {overlap}")
    rollbacks = api.list_builds(f'tags="{service.rollback_build_tag}" AND {overlap}')
    own = (rollback.get("build") or {}).get("id")

    def may_have_changed(build) -> bool:
        """An active placeholder is unstarted only without timing; terminal ambiguity stays conservative."""
        marker = dict(build.step_status).get("release-record-begin")
        started = "release-record-begin" in build.started_steps
        if not started and marker in ("PENDING", "QUEUED"):
            return False
        if not started and build.ongoing and marker in ("", "STATUS_UNKNOWN"):
            return False
        return True

    return [
        build.describe()
        for build in deploys
        if build.id != own and not build.confirmed_noop and may_have_changed(build)
    ] + [build.describe() for build in rollbacks if build.id != own and not build.rollback_dry_run]


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
ROLLBACK_WAIT_SECONDS = 4200
TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"})


def follow_rollback(
    service: rc.Service,
    store: rr.Store,
    release_id: str,
    build_id: str,
    claim_id: str,
    *,
    target: str = "previous",
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

    try:
        pointer, _ = store.read("latest.json")
        rollback = rr.read_record(store, pointer["release_id"]) if pointer else {}
        cleanup = ((rollback.get("checks") or {}).get("rollback") or {}).get("cleanup_failures")
        if (
            rollback.get("kind") == "rollback"
            and rollback.get("status") == "deployed"
            and (rollback.get("build") or {}).get("id") == build_id
            and (rollback.get("rollback") or {}).get("rolled_back_release") == release_id
            and (rollback.get("promote_hold") or {}).get("status") == "held"
            and cleanup
        ):
            rr.post_slack(
                f":x: {service.title} rollback build {build_id} restored prod and holds promotes, "
                f"but candidate cleanup is incomplete: {cleanup[0][:300]}. Repair candidate tags; "
                f"do not roll back again ({rr.build_url(service.prod, build_id)})"
            )
            return status
    except (rr.RecordError, GuardError, OSError, KeyError, ValueError) as error:
        print(f"WARNING: could not confirm the rollback result: {error}", file=sys.stderr, flush=True)

    def failed(current: dict) -> None:
        """Mark our rollback claim as failed with the build status."""
        if (current.get("auto_rollback") or {}).get("claim") == claim_id:
            current["auto_rollback"] = {**current["auto_rollback"], "status": "failed", "result": status}

    try:
        rr.update_record(store, release_id, failed, "auto_rollback_failed", {"build_id": build_id, "status": status})
    except (rr.RecordError, GuardError, OSError) as error:
        print(f"WARNING: could not mark the rollback failed: {error}", file=sys.stderr, flush=True)
    rr.post_slack(
        f":x: {service.title} automatic rollback build {build_id} ended {status}; prod may still run the failed "
        f"release {release_id}. Roll back by hand: {rollback_cli(service, 'run', target)} --reason '<why>' "
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
    runner: rr.Runner | None = None,
    builds: Callable[[rc.Service, str | None], tuple[bool, list[str]]] | None = None,
    follow: bool = True,
    followed: Callable[[str], bool] | None = None,
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
    run = runner or rr.run_command
    changing = builds or builds_changing_prod
    if os.environ.get("RELEASE_AUTO_ROLLBACK", "").strip() != "1":
        print("Automatic rollback is off for this run (RELEASE_AUTO_ROLLBACK is not 1)", flush=True)
        return None
    claim_id = uuid.uuid4().hex
    try:
        own_build = (record.get("build") or {}).get("id")
        deadline = clock() + OWN_BUILD_WAIT_SECONDS
        lock_deadline = clock() + LOCK_HOLDER_WAIT_SECONDS
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
            lock = rr.lock_holder(store, rr.build_alive(service.prod))
            if not others and lock and lock.get("holder") != own_build:
                # A deploy build at or past its gate. It may still skip (freeze, hold, a newer commit),
                # so wait for it to free the lock, then look at latest.json again.
                if clock() > lock_deadline:
                    others = [f"{lock.get('kind')} build {lock.get('holder')} (holds the prod lock)"]
                else:
                    print(f"Waiting: the {lock.get('kind')} build {lock.get('holder')} holds the prod lock", flush=True)
                    sleep(30)
                    continue
            if others:
                rr.post_slack(
                    f":warning: {title} release-verify {short} failed, but {others[0]} changes prod now, so no "
                    f"automatic rollback started. Check prod; roll back by hand with "
                    f"{suggested_command(service, current)} --reason '<why>' if needed."
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
            """Claim the automatic rollback; raise RollbackClaimed when another watcher holds it."""
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
            """Note that the rollback build started, with its build id."""
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
            f"Roll back by hand: {suggested_command(service, record)} --reason '<why>'"
        )
        return None
    url = rr.build_url(service.prod, build_id)
    print(f"Automatic rollback build {build_id} started: {url}", flush=True)
    rr.post_slack(*rollback_alert(service, current, url, store.location(rr.record_name(release_id))))
    if follow:
        status = follow_rollback(service, store, release_id, build_id, claim_id, target=target)
        if followed is not None and not followed(status):
            return None
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
        lambda current: (
            current.setdefault("checks", {}).__setitem__("window_build", window_build),
            ro.merge_plan(current, window_build["checkpoints_minutes"]),
        ),
        "window_scheduled",
        {"build_id": build_id},
    )
    print(f"Watch window build {build_id} submitted (checkpoints {checkpoints} min)", flush=True)
    return EXIT_OK


def cmd_own(
    args: argparse.Namespace, service: rc.Service, env: rc.Environment, store: rr.Store, clients: rr.Clients
) -> int:
    """Acknowledge the deploying agent's durable responsibility for the two checkpoints."""
    record = rr.read_record(store, args.release_id)

    def own(current):
        ro.claim_owner(
            current,
            args.thread_url,
            args.pr_url,
            takeover=getattr(args, "takeover", False),
            reason=getattr(args, "reason", ""),
        )

    saved = rr.update_record(store, record["release_id"], own, "followup_owned", {"thread_url": args.thread_url})
    print(
        json.dumps(
            {
                "release_id": saved["release_id"],
                "commit": saved["commit"],
                "images": saved.get("images"),
                "followup": saved["followup"],
                "window_build": (saved.get("checks") or {}).get("window_build"),
                "record": store.location(rr.record_name(saved["release_id"])),
            },
            indent=2,
        )
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run check, watch or schedule-window; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    own = commands.add_parser("own", help="acknowledge agent ownership of +15/+60 checks")
    rr.add_common_args(own)
    own.add_argument("--release-id", required=True)
    own.add_argument("--thread-url", required=True)
    own.add_argument("--pr-url", required=True)
    own.add_argument("--takeover", action="store_true")
    own.add_argument("--reason", default="")

    check = commands.add_parser("check", help="run one phase of checks now")
    rr.add_common_args(check)
    check.add_argument("--release-id", default="latest")
    check.add_argument("--release-id-file", help="read the release id from this file (Cloud Build workspace)")
    check.add_argument("--phase", choices=["deploy", "window"], default="deploy")
    check.add_argument("--checkpoint-minutes", type=int, help="manual repair: scheduled checkpoint identity")
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
    watch.add_argument(
        "--retry-completed", action="store_true", help="rerun completed checkpoints, preserving attempt history"
    )
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
    handlers = {"check": cmd_check, "watch": cmd_watch, "schedule-window": cmd_schedule_window, "own": cmd_own}
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
