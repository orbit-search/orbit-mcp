#!/usr/bin/env python3
"""New recurring errors after a prod release, and the commit that likely caused them.

release_verify.py calls check_new_errors() at each watch-window checkpoint. It
compares the release window with the baseline: the 24 h before the release,
without the time that a rolled-back release ran. The sources come from the
service config (verify.signals); a source that is not configured is skipped:

1. Datadog monitors (`monitors`, an event query such as `source:alert
   service:twtwapi`): a monitor group that went to Alert or Warn after the
   deploy, had no event in the baseline and still alerts at the checkpoint.
2. Datadog Error Tracking issues of the APM services (`error_tracking`), first
   seen after the deploy or first seen on the release version (DD_VERSION).
3. Warning and error log lines in Cloud Logging of the service's containers
   (`log_containers`), grouped into sites: 0 lines in the baseline,
   LOG_MIN_COUNT or more in the window, from 2 or more pods or runs.
4. Failure series (`failure_series`, Datadog count metrics grouped by tags):
   0 in the baseline and `min_new` or more in the window, or a failure rate over
   FAILURE_RATE_LIMIT per group (per route, per task type, per step).

A release that a newer release replaced before its watch judged it is judged
with the newer one: the window starts at the oldest unjudged release, and the
suspects include the commits of all of them.

trace_suspects() ranks the commits for each problem: a commit that changes the
function that writes the error (found by its text or stack frame in the
release's source), a function that code calls, or the error's own text in its
diff. A problem with a clear suspect fails the release; one without a clear
suspect is a warning that asks a human.

Part of the release kit (orbit-infrastructure/release-kit); ported from
deep-search bin/release_signals.py (#2308), with a language-neutral source tree
(TypeScript, JavaScript, Go and Python) in place of the Python AST.
Stdlib only; it runs in gcr.io/cloud-builders/kubectl (Python 3.10).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_config as rc  # noqa: E402
import release_record as rr  # noqa: E402
from promote_release_guard import GuardError, _http_json  # noqa: E402

# --------------------------------------------------------------------------- limits

# The baseline is the 24 h before the oldest unjudged release (rolled-back releases excluded).
BASELINE = timedelta(hours=24)
# At most this many unjudged releases are judged together.
MAX_CHAIN = 8

# Log sites: lines in the window, and runs (or pods, for lines without a run id) that wrote them.
LOG_MIN_COUNT = 10
LOG_MIN_SPREAD = 2
# The window read is its newest LOG_READ_MINUTES (a recurring error recurs there too); the
# reference read is the LOG_REFERENCE_MINUTES before the release. A site that is new against
# the reference is then searched for in the whole baseline (at most LOG_MAX_PROBES sites).
LOG_READ_MINUTES = 60
LOG_REFERENCE_MINUTES = 120
# Recurring: the site still writes lines in the newest LOG_RECENT_MINUTES (a burst right after
# the rollout that has stopped is not).
LOG_RECENT_MINUTES = 15
LOG_MAX_PROBES = 10
LOG_MAX_LINES = 250_000
LOG_PROBE_SECONDS = 150


@dataclass(frozen=True)
class RateLimit:
    """Exceeded when rate > baseline * ratio AND rate - baseline > absolute, with min_samples in both windows."""

    ratio: float
    absolute: float
    min_samples: int


FAILURE_MIN_NEW = 10
FAILURE_RATE_LIMIT = RateLimit(ratio=3.0, absolute=0.2, min_samples=50)
# Failure types that a rollout itself causes (work cancelled on stopped pods).
ROLLOUT_FAILURE_TYPES = frozenset({"cancelled", "canceled"})

# Error Tracking: occurrences in the window of a new issue.
ET_MIN_COUNT = 5

# A suspect is clear when its score is at least CLEAR_MIN and CLEAR_MARGIN above the next PR.
CLEAR_MIN = 4
CLEAR_MARGIN = 2

DATADOG_APP = "https://app.datadoghq.com"
# The scope each Datadog read needs; a 403 makes that part inconclusive.
SCOPES = {
    "monitors": "events_read",
    "error_tracking": "error_tracking_read",
    "failure_series": "timeseries_query",
}


class NoAccess(Exception):
    """The credentials cannot read this source (HTTP 401 or 403)."""


def _iso(value: datetime) -> str:
    """A time as an RFC 3339 UTC string with seconds."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- scope


Span = tuple[datetime, datetime]


@dataclass
class Scope:
    """What one checkpoint judges: the unjudged releases (newest first), their commits and windows."""

    releases: list[dict]
    commits: list[dict]
    baseline: list[Span]
    window: Span
    release_start: datetime
    notes: list[str] = field(default_factory=list)

    @property
    def commit_release(self) -> dict[str, dict]:
        """The release of each commit in the scope: sha -> release record."""
        return {commit["sha"]: release for release in self.releases for commit in release.get("commits") or []}

    def summary(self) -> dict:
        """The scope for the record: release ids, commit count, baseline and window times, notes."""
        return {
            "releases": [release["release_id"] for release in self.releases],
            "commits": len(self.commits),
            "baseline": [[_iso(start), _iso(end)] for start, end in self.baseline],
            "window": [_iso(self.window[0]), _iso(self.window[1])],
            "notes": self.notes,
        }


def _times(record: dict) -> tuple[datetime, datetime]:
    """(deploy started, deploy finished) of a record; finished is the start when the deploy has no finish time."""
    times = record.get("times") or {}
    started = rr.parse_utc(times["deploy_started"])
    return started, rr.parse_utc(times.get("deploy_finished") or times["deploy_started"])


def _read(store: rr.Store, release_id: str | None) -> dict | None:
    """The record of a release id, or None when there is no id or it cannot be read."""
    if not release_id:
        return None
    try:
        record, _ = store.read(rr.record_name(release_id))
    except (rr.RecordError, GuardError, OSError, ValueError):
        return None
    return record


def subtract(span: Span, holes: Iterable[Span]) -> list[Span]:
    """span minus the holes, as sorted spans."""
    pieces = [span]
    for hole_start, hole_end in sorted(holes):
        rest = []
        for start, end in pieces:
            if hole_end <= start or hole_start >= end:
                rest.append((start, end))
                continue
            if start < hole_start:
                rest.append((start, hole_start))
            if hole_end < end:
                rest.append((hole_end, end))
        pieces = rest
    return [piece for piece in pieces if piece[1] > piece[0]]


def release_scope(store: rr.Store, record: dict, now: datetime) -> Scope:
    """The releases to judge together, the 24 h baseline before the oldest, and the window since it."""
    chain = [record]
    newest_start, _ = _times(record)
    while len(chain) < MAX_CHAIN:
        previous = _read(store, (chain[-1].get("previous") or {}).get("release_id"))
        if not previous or previous.get("kind") != "promote" or previous.get("status") != "deployed":
            break
        verdict = previous.get("verdict") or {}
        # A release whose last checkpoint could not run is inconclusive but unchecked: the next
        # watch judges it again, so its commits stay suspects.
        if verdict.get("status") != "pending" and not verdict.get("unchecked"):
            break  # judged: its verdict stands
        if newest_start - _times(previous)[0] > BASELINE:
            break
        chain.append(previous)
    oldest = chain[-1]
    release_start, window_start = _times(oldest)
    baseline_span = (release_start - BASELINE, release_start)
    holes, notes = rolled_back_spans(store, oldest, baseline_span[0])
    commits: list[dict] = []
    seen = set()
    for release in reversed(chain):
        for commit in release.get("commits") or []:
            if commit.get("sha") and commit["sha"] not in seen:
                seen.add(commit["sha"])
                commits.append(commit)
    if len(chain) > 1:
        notes.append(
            f"judged with {len(chain) - 1} earlier release(s) that no checkpoint judged: "
            + ", ".join(release["release_id"] for release in chain[1:])
        )
    return Scope(chain, commits, subtract(baseline_span, holes), (window_start, now), release_start, notes)


def rolled_back_spans(store: rr.Store, oldest: dict, since: datetime) -> tuple[list[Span], list[str]]:
    """When reverted releases ran after `since`: their errors must not count as known ones.

    A rollback undoes every release after its target (the newest earlier record
    with the target commit), not only the one it names in `previous`.
    """
    walk: list[dict] = []
    current = oldest
    for _ in range(40):
        current = _read(store, (current.get("previous") or {}).get("release_id"))
        if not current or not (current.get("times") or {}).get("deploy_started"):
            break
        walk.append(current)
        if _times(current)[0] < since - BASELINE:
            break  # far enough back to find the target of a rollback inside the baseline
    holes, notes = [], []
    for index, record in enumerate(walk):  # newest first
        if record.get("kind") != "rollback" or record.get("status") != "deployed":
            continue
        target = next(
            (j for j in range(index + 1, len(walk)) if walk[j].get("commit") == record.get("commit")), len(walk)
        )
        reverted = walk[index + 1 : target]
        if not reverted:
            continue
        span = (_times(reverted[-1])[0], _times(record)[1])
        if span[1] > since:
            holes.append(span)
            notes.append(
                f"baseline leaves out {', '.join(r['release_id'] for r in reverted)} (rolled back by {record['release_id']})"
            )
    return holes, notes


# --------------------------------------------------------------------------- Datadog


class DatadogReader:
    """The Datadog reads of the new-error checks, over release_verify.DatadogClient's keys."""

    def __init__(self, dd) -> None:  # noqa: ANN001 - release_verify.DatadogClient (no import cycle)
        """Wrap a release_verify.DatadogClient."""
        self.dd = dd

    def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        """Call the Datadog API; raise NoAccess on HTTP 401 or 403."""
        try:
            return self.dd.api(method, path, body=body, params=params)
        except GuardError as error:
            if re.search(r"HTTP (401|403)\b", str(error)):
                raise NoAccess(str(error)[:300]) from None
            raise

    def scalar(self, query: str, start: datetime, end: datetime) -> dict[str, float]:
        """The sum of a metric query per group over [start, end); raise NoAccess on HTTP 401 or 403."""
        try:
            return self.dd.scalar(query, "sum", start, end)
        except GuardError as error:
            if re.search(r"HTTP (401|403)\b", str(error)):
                raise NoAccess(str(error)[:300]) from None
            raise

    def events(self, query: str, start: datetime, end: datetime, max_pages: int = 30) -> list[dict]:
        """The Datadog events that match the query in [start, end), oldest first, over at most max_pages pages."""
        out: list[dict] = []
        cursor = ""
        for _ in range(max_pages):
            params = {
                "filter[query]": query,
                "filter[from]": _iso(start),
                "filter[to]": _iso(end),
                "page[limit]": "1000",
                "sort": "timestamp",
            }
            if cursor:
                params["page[cursor]"] = cursor
            payload = self._call("GET", "/api/v2/events", params=params)
            out += payload.get("data") or []
            cursor = (((payload.get("meta") or {}).get("page")) or {}).get("after") or ""
            if not cursor or not payload.get("data"):
                break
        return out

    def issues(self, query: str, start: datetime, end: datetime) -> list[dict]:
        """Error Tracking issues with events in the window: {total_count, **issue attributes}."""
        out = []
        for track in ("trace", "logs"):
            body = {
                "data": {
                    "type": "search_request",
                    "attributes": {
                        "query": query,
                        "from": int(start.timestamp() * 1000),
                        "to": int(end.timestamp() * 1000),
                        "track": track,
                    },
                }
            }
            payload = self._call("POST", "/api/v2/error-tracking/issues/search", body=body, params={"include": "issue"})
            issues = {item["id"]: item.get("attributes") or {} for item in payload.get("included") or []}
            for item in payload.get("data") or []:
                attributes = issues.get(item["id"]) or {}
                out.append({"id": item["id"], **attributes, **(item.get("attributes") or {})})
        return out


# --------------------------------------------------------------------------- problems


@dataclass
class Problem:
    """One new recurring error; trace_suspects() fills suspects."""

    kind: str  # monitor, error_tracking, log, failure_series
    key: str
    text: str
    count: float = 0
    link: str | None = None
    literals: list[str] = field(default_factory=list)  # text the code writes
    # snake_case or camelCase values a log line names in [brackets]: code names them verbatim.
    identifiers: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    frames: list[tuple[str, str]] = field(default_factory=list)  # (path, function) from a stack trace
    suspects: list[dict] = field(default_factory=list)
    clear: bool = False
    origins: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        """The problem for the record: its fields, with at most 5 origins and 5 suspects."""
        return {
            "kind": self.kind,
            "key": self.key,
            "text": self.text,
            "count": self.count,
            "link": self.link,
            "origins": self.origins[:5],
            "suspects": self.suspects[:5],
            "clear": self.clear,
        }


TITLE_RE = re.compile(r"^\[(?P<kind>[A-Za-z -]+?)(?: on \{(?P<group>[^}]*)\})?\]\s*(?P<name>.*)$")
FRESH = {"Triggered": "alert", "Warn": "warn"}
STATES = {"Triggered": "alert", "Re-Triggered": "alert", "Warn": "warn", "Re-Warn": "warn", "Recovered": "ok"}


def monitor_problems(reader: DatadogReader, scope: Scope, now: datetime, query: str) -> list[Problem]:
    """Monitor groups that went to Alert or Warn after the release, were silent in the baseline, and still alert."""
    start = scope.baseline[0][0] if scope.baseline else scope.release_start
    timelines: dict[tuple[str, str], list[tuple[datetime, str, str, str]]] = {}
    for event in reader.events(query, start, now):
        attributes = event.get("attributes") or {}
        inner = attributes.get("attributes") or {}
        monitor = inner.get("monitor") or {}
        tags = set(attributes.get("tags") or inner.get("tags") or [])
        if "env:dev" in tags and not ({"env:prod", "env:production"} & tags):
            continue
        if any(tag.startswith("type:") and tag.endswith("sample-size-gate") for tag in tags):
            continue  # traffic volume gates of composite monitors, not errors
        match = TITLE_RE.match(str(inner.get("title") or attributes.get("title") or ""))
        if not match or not monitor.get("id"):
            continue
        group = ",".join(sorted(monitor.get("groups") or [])) or "*"
        stamp = rr.parse_utc(str(attributes.get("timestamp")).replace(".000Z", "Z"))
        timelines.setdefault((str(monitor["id"]), group), []).append(
            (stamp, match.group("kind").strip(), match.group("name").strip(), group)
        )
    alerting: dict[str, list[tuple[datetime, str, str, str]]] = {}
    for (monitor_id, group), events in sorted(timelines.items()):
        events.sort()
        if any(start <= stamp < end for stamp, *_ in events for start, end in scope.baseline):
            continue  # it moved in the baseline: flapping, or not OK the whole time
        after = [item for item in events if scope.release_start <= item[0] <= now]
        if not after or after[0][1] not in FRESH:
            continue  # a cycle that began before the release (re-triggers and recoveries only)
        state = STATES.get(after[-1][1])
        if state not in ("alert", "warn"):
            continue
        alerting.setdefault(group if group != "*" else f"monitor:{monitor_id}", []).append(
            (after[0][0], monitor_id, after[0][2], state)
        )
    problems = []
    for key, monitors in sorted(alerting.items()):
        monitors.sort()
        stamp, monitor_id, name, state = monitors[0]
        group = "" if key.startswith("monitor:") else key
        names = ", ".join(repr(item[2]) for item in monitors[:2]) + (
            f" and {len(monitors) - 2} more" if len(monitors) > 2 else ""
        )
        problems.append(
            Problem(
                kind="monitor",
                key=f"monitor:{key}",
                text=(
                    f"monitor{'s' if len(monitors) > 1 else ''} {names}"
                    + (f" ({group})" if group else "")
                    + f" went to {state} from {stamp:%H:%M} UTC after the release and still alert; "
                    "no event in the 24 h before"
                ),
                count=len(monitors),
                link=f"{DATADOG_APP}/monitors/{monitor_id}" + (f"?group={urllib.parse.quote(group)}" if group else ""),
                fields=dict(part.split(":", 1) for part in group.split(",") if ":" in part),
            )
        )
    return problems


def _source_path(path: str, code: rc.Code) -> str:
    """A stack-frame path cut to the repository path (it starts at a source prefix)."""
    for prefix in code.source_prefixes:
        index = path.find(prefix)
        if index >= 0 and (index == 0 or path[index - 1] == "/"):
            return path[index:]
    return path


def issue_problems(
    reader: DatadogReader, scope: Scope, now: datetime, signals: rc.Signals, code: rc.Code
) -> list[Problem]:
    """Error Tracking issues of the service first seen in the window, or first seen on a release version."""
    services = " OR ".join(signals.error_tracking)
    versions = {release["commit"] for release in scope.releases}
    window_ms = scope.window[0].timestamp() * 1000
    problems = {}
    for issue in reader.issues(f"service:({services}) env:{signals.datadog_env}", scope.window[0], now):
        new = float(issue.get("first_seen") or 0) >= window_ms or issue.get("first_seen_version") in versions
        if not new or float(issue.get("total_count") or 0) < ET_MIN_COUNT or issue["id"] in problems:
            continue
        message = str(issue.get("error_message") or "").splitlines()[0][:200] if issue.get("error_message") else ""
        frame = (_source_path(str(issue.get("file_path") or ""), code), str(issue.get("function_name") or ""))
        problems[issue["id"]] = Problem(
            kind="error_tracking",
            key=f"issue:{issue['id']}",
            text=(
                f"new Error Tracking issue {issue.get('error_type')}: {message} "
                f"({float(issue.get('total_count') or 0):.0f} in the window, at {frame[0]}:{frame[1]})"
            ),
            count=float(issue.get("total_count") or 0),
            link=f"{DATADOG_APP}/error-tracking/issue/{issue['id']}",
            literals=literal_fragments(message),
            frames=[frame] if code.is_source(frame[0]) else [],
        )
    return list(problems.values())


def _sum_spans(reader: DatadogReader, query: str, spans: list[Span]) -> dict[str, float]:
    """The sum of a metric query per group over all the spans."""
    total: dict[str, float] = {}
    for start, end in spans:
        for key, value in reader.scalar(query, start, end).items():
            total[key] = total.get(key, 0.0) + value
    return total


def failure_problems(reader: DatadogReader, scope: Scope, series: dict) -> list[Problem]:
    """Compare the failure series of the window with the baseline (see judge_failures)."""
    base_fail = _sum_spans(reader, series["failures"], scope.baseline)
    cand_fail = _sum_spans(reader, series["failures"], [scope.window])
    base_all = _sum_spans(reader, series["all"], scope.baseline) if series.get("all") else {}
    cand_all = _sum_spans(reader, series["all"], [scope.window]) if series.get("all") else {}
    return judge_failures(
        base_fail,
        cand_fail,
        base_all,
        cand_all,
        fields=list(series.get("fields") or []),
        min_new=int(series.get("min_new") or FAILURE_MIN_NEW),
    )


def judge_failures(
    base_fail: dict[str, float],
    cand_fail: dict[str, float],
    base_all: dict[str, float],
    cand_all: dict[str, float],
    *,
    fields: list[str],
    min_new: int = FAILURE_MIN_NEW,
) -> list[Problem]:
    """New failure series (group of the failures query) and failure-rate rises (group of the all query).

    The failures query groups by the fields of the all query plus at most one more (a failure type):
    `fields` names them in order. A failure-type value in ROLLOUT_FAILURE_TYPES never counts.
    """
    problems = []
    rate_width = len(fields) - 1 if len(fields) > 1 else len(fields)

    def named(key: str, width: int) -> dict[str, str]:
        """The first width fields of a group key, by field name."""
        parts = (key.split("|") + [""] * width)[:width]
        return {name: value for name, value in zip(fields[:width], parts)}

    def rollout(key: str) -> bool:
        """Whether the failure type of a group key is a rollout failure, which never counts."""
        return len(fields) > 1 and key.split("|")[-1] in ROLLOUT_FAILURE_TYPES

    for key, count in sorted(cand_fail.items()):
        if rollout(key) or base_fail.get(key, 0.0) > 0 or count < min_new:
            continue
        values = named(key, len(fields))
        label = " ".join(f"{name}:{value}" for name, value in values.items()) or key
        problems.append(
            Problem(
                kind="failure_series",
                key=f"failure_series:{key}",
                text=f"new failure series {label}: 0 in the baseline, {count:.0f} in the window",
                count=count,
                fields=values,
            )
        )
    new_groups = {"|".join(problem.key.split(":", 1)[1].split("|")[:rate_width]) for problem in problems}
    for key in sorted(cand_all):
        if key in new_groups:
            continue

        def failures_of(values: dict[str, float]) -> float:
            """The failures of this group in a window, without the rollout failures."""
            return sum(v for k, v in values.items() if "|".join(k.split("|")[:rate_width]) == key and not rollout(k))

        base_n, cand_n = base_all.get(key, 0.0), cand_all.get(key, 0.0)
        limit = FAILURE_RATE_LIMIT
        if base_n < limit.min_samples or cand_n < limit.min_samples:
            continue
        base_rate, cand_rate = failures_of(base_fail) / base_n, failures_of(cand_fail) / cand_n
        if cand_rate > base_rate * limit.ratio and cand_rate - base_rate > limit.absolute:
            values = named(key, rate_width)
            label = " ".join(f"{name}:{value}" for name, value in values.items()) or key
            problems.append(
                Problem(
                    kind="failure_series",
                    key=f"failure_rate:{key}",
                    text=(
                        f"failure rate of {label} {base_rate:.0%} -> {cand_rate:.0%} "
                        f"(n={base_n:,.0f} -> {cand_n:,.0f}; limit > {limit.ratio:g}x and > +{limit.absolute * 100:g} pp)"
                    ),
                    count=failures_of(cand_fail),
                    fields=values,
                )
            )
    return problems


# --------------------------------------------------------------------------- Cloud Logging


def log_filter(env: rc.Environment, containers: tuple[str, ...]) -> str:
    """Warning and error lines of the service's containers in its prod namespace."""
    names = " OR ".join(f'"{name}"' for name in containers)
    return (
        f'resource.type="k8s_container" AND resource.labels.cluster_name="{env.cluster}" '
        f'AND resource.labels.namespace_name="{env.namespace}" '
        f"AND resource.labels.container_name=({names}) AND severity>=WARNING"
    )


# The fields that hold the message of a JSON log line, in order. entry_text() reads them and the
# baseline search (message_match) searches every one, so a known line is found wherever it sits.
MESSAGE_FIELDS = ("message", "msg", "error", "err")


def entry_text(entry: dict) -> str:
    """Keep a console prefix before the message, with structured error context after it."""
    if entry.get("textPayload"):
        return str(entry["textPayload"])
    payload = entry.get("jsonPayload") or {}
    message = ""
    for key in MESSAGE_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            message = value
            break
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            message = value["message"]
            break
    metadata = payload.get("metadata")
    args = metadata.get("consoleArgs") if isinstance(metadata, dict) else None
    parts = [arg for arg in args if isinstance(arg, str) and arg.strip()] if isinstance(args, list) else []
    # A console prefix that cannot identify a site (for example "Error:") must
    # not hide the searchable message on a later line.
    if parts:
        pattern = log_pattern(parts[0])
        if not pattern or not site_fragment(pattern):
            parts.insert(0, message)
    parts.append(message)
    if not any(part.strip() for part in parts):
        return ""  # Stack and request fields are context, never an unsearchable site of their own.
    for key in ("error", "err"):
        error = payload.get(key)
        if isinstance(error, dict) and isinstance(error.get("stack"), str):
            parts.append(error["stack"])
    request = payload.get("request")
    if isinstance(request, dict):
        route = " ".join(request[key] for key in ("method", "path") if isinstance(request.get(key), str))
        if route:
            parts.append(route)
    return "\n".join(dict.fromkeys(part for part in parts if part.strip()))


def message_match(text: str) -> str:
    """A Cloud Logging filter that finds `text` in any field entry_text() reads."""
    fields = ["textPayload"] + [f"jsonPayload.{key}" for key in MESSAGE_FIELDS]
    fields += [f"jsonPayload.{key}.message" for key in MESSAGE_FIELDS]
    fields.append("jsonPayload.metadata.consoleArgs")
    return "(" + " OR ".join(f'{field}:"{text}"' for field in fields) + ")"


class CloudLogging:
    """entries:list reads with the build identity."""

    URL = "https://logging.googleapis.com/v2/entries:list"

    def __init__(self, project: str, token: Callable[[], str], clock: Callable[[], float] = time.monotonic) -> None:
        """Keep the project, the token source and the clock."""
        self.project = project
        self.token = token
        self.clock = clock

    def _page(self, body: dict) -> dict:
        """Read one page of log entries; raise NoAccess on HTTP 401 or 403."""
        request = urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
        )
        try:
            return _http_json(request, attempts=6)
        except GuardError as error:
            if re.search(r"HTTP (401|403)\b", str(error)):
                raise NoAccess(str(error)[:300]) from None
            raise

    def lines(
        self, extra: str, spans: list[Span], *, limit: int, newest_first: bool = False, seconds: float | None = None
    ) -> Iterator[tuple[str, str, str, str, str]]:
        """(timestamp, pod, container, severity, text) of the matching lines; stops at limit or after `seconds`."""
        if not spans:
            return
        times = " OR ".join(f'(timestamp>="{_iso(start)}" AND timestamp<"{_iso(end)}")' for start, end in spans)
        body = {
            "resourceNames": [f"projects/{self.project}"],
            "filter": f"{extra} AND ({times})",
            "orderBy": "timestamp desc" if newest_first else "timestamp asc",
            "pageSize": min(1000, limit),
        }
        deadline = None if seconds is None else self.clock() + seconds
        count = 0
        while True:
            payload = self._page(body)
            for entry in payload.get("entries") or []:
                labels = (entry.get("resource") or {}).get("labels") or {}
                entry_labels = entry.get("labels") or {}
                yield (
                    entry.get("timestamp", ""),
                    # Who wrote it: a pod, a Cloud Run instance, a Composer worker.
                    labels.get("pod_name")
                    or entry_labels.get("instanceId")
                    or entry_labels.get("worker_id")
                    or entry_labels.get("dag_processor_manager_id")
                    or "",
                    # What wrote it: a container, a Cloud Run service, a DAG.
                    labels.get("container_name") or labels.get("service_name") or entry_labels.get("workflow") or "",
                    str(entry.get("severity") or "DEFAULT"),
                    entry_text(entry),
                )
                count += 1
                if count >= limit:
                    return
            body["pageToken"] = payload.get("nextPageToken") or ""
            if not body["pageToken"]:
                return
            if deadline is not None and self.clock() > deadline:
                raise TimeoutError("the search ran out of time")


LEVELS = {"WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL", "ALERT": "CRITICAL", "EMERGENCY": "CRITICAL"}
# A Python log header in the text (LEVEL:logger:message) sets the level and logger itself.
HEADER_RE = re.compile(r"^(WARNING|ERROR|CRITICAL):([\w.]*):(.*)$")
PLACEHOLDERS = (
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<id>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b"), "<hex>"),
    # Greedy: quoted values hold names with apostrophes (O'Brien) and quotes in quotes.
    (re.compile(r"'.*'"), "'*'"),
    (re.compile(r'".*"'), "'*'"),
    # Person names: 2 to 5 capitalized words or initials in a row.
    (re.compile(r"\b(?:[A-Z][a-z'\-]+|[A-Z]\.?)(?:\s+(?:[A-Z][a-z'\-]+|[A-Z]\.?)){1,4}\b"), "<name>"),
)
BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
IDENTIFIER_LIST_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:, ?[A-Za-z][A-Za-z0-9_]*)*")
# Numbers, but not digits that are part of a name (v3, urllib3).
NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?")
PLACEHOLDER_RE = re.compile(r"<id>|<url>|<email>|<hex>|<name>|\[\*\]|'\*'|#")
RUN_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Continuation lines of a stack trace or of a multi-line object, not messages.
CONTINUATION_RE = re.compile(r"^(\s|at\s|Traceback \(|During handling|The above exception|[{}\[\]),]*$)")


def log_pattern(text: str, severity: str = "ERROR", container: str = "") -> str | None:
    """'LEVEL:source:message' with ids, numbers, quoted text and free-form [values] replaced; None for other lines."""
    first = text.split("\n", 1)[0]
    if not first.strip() or CONTINUATION_RE.match(first):
        return None
    match = HEADER_RE.match(first)
    if match:
        level, source, message = match.group(1), match.group(2), match.group(3)
    else:
        level = LEVELS.get(severity.upper())
        if level is None:
            return None
        source, message = container, first
    for pattern, placeholder in PLACEHOLDERS:
        message = pattern.sub(placeholder, message)
    # Keep [identifier] values (result keys, steps): they name what failed. Others are data.
    message = BRACKET_RE.sub(
        lambda m: m.group(0) if IDENTIFIER_LIST_RE.fullmatch(m.group(1)) or m.group(1).startswith("<") else "[*]",
        message,
    )
    message = NUMBER_RE.sub("#", message)
    message = " ".join(message.split())[:160]
    return f"{level}:{source}:{message}" if message else None


def literal_fragments(message: str, minimum: int = 12) -> list[str]:
    """The literal parts of a pattern or message, longest first: the text the code wrote."""
    body = message.split(":", 2)[2] if HEADER_RE.match(message) or _is_pattern(message) else message
    parts = [part.strip(" []():,;") for part in PLACEHOLDER_RE.split(body)]
    return sorted({part for part in parts if len(part) >= minimum}, key=len, reverse=True)


def _is_pattern(text: str) -> bool:
    """Whether the text is a log pattern: SEVERITY:logger: at the start."""
    return bool(re.match(r"^(WARNING|ERROR|CRITICAL):[\w.-]*:", text))


def bracket_identifiers(message: str) -> list[str]:
    """snake_case and camelCase values in [brackets]: result keys and steps, which code names verbatim."""
    out = []
    for match in BRACKET_RE.finditer(message):
        if IDENTIFIER_LIST_RE.fullmatch(match.group(1)):
            out += [part.strip() for part in match.group(1).split(",") if "_" in part or re.search(r"[a-z][A-Z]", part)]
    return sorted(set(out))


@dataclass
class PatternCount:
    count: int = 0
    pods: set[str] = field(default_factory=set)
    runs: set[str] = field(default_factory=set)
    example: str = ""
    pattern: str = ""
    identifiers: set[str] = field(default_factory=set)
    last: str = ""  # newest timestamp

    @property
    def spread(self) -> int:
        """Runs when the lines name one (a run retried on many pods is one run), else pods."""
        return len(self.runs) if self.runs else len(self.pods)


def count_patterns(lines: Iterable[tuple[str, str, str, str, str]]) -> dict[str, PatternCount]:
    """Count the log lines per pattern, with the pods, runs, newest time and an example line."""
    counts: dict[str, PatternCount] = {}
    for stamp, pod, container, severity, text in lines:
        pattern = log_pattern(text, severity, container)
        if pattern is None:
            continue
        item = counts.setdefault(pattern, PatternCount(example=text.split("\n", 1)[0][:300]))
        item.count += 1
        item.last = max(item.last, stamp[:19])
        item.pods.add(pod)
        run = RUN_ID_RE.search(text)
        if run:
            item.runs.add(run.group(0))
    return counts


FAILURE_RE = re.compile(
    r"\b(fail\w*|error\w*|exception\w*|traceback|timed? ?out|timeout\w*|unable|cannot|can't|could ?not|refused|"
    r"denied|invalid|missing|lost|crash\w*|abort\w*|unavailable|exhausted|rejected|corrupt\w*|not found|panic\w*)\b",
    re.IGNORECASE,
)


def is_error(pattern: str) -> bool:
    """ERROR and CRITICAL lines, and WARNING lines that report a failure; other warnings are informational."""
    return not pattern.startswith("WARNING:") or bool(FAILURE_RE.search(pattern.split(":", 2)[2]))


def site_fragment(pattern: str) -> str | None:
    """'LEVEL:source:<leading text>': the text before the first value, else the longest literal part."""
    level, source, body = pattern.split(":", 2)
    first = PLACEHOLDER_RE.split(body)[0].strip(" []():,;")
    if len(first) < 15:
        fragments = literal_fragments(pattern)
        first = fragments[0] if fragments else ""
    return f"{level}:{source}:{first}" if first else None


def count_sites(patterns: dict[str, PatternCount]) -> dict[str, PatternCount]:
    """Patterns grouped by site; the example and pattern are the site's most frequent pattern."""
    sites: dict[str, PatternCount] = {}
    for pattern, item in sorted(patterns.items(), key=lambda pair: -pair[1].count):
        site = site_fragment(pattern)
        if site is None:
            continue
        total = sites.setdefault(site, PatternCount(example=item.example, pattern=pattern))
        total.count += item.count
        total.pods |= item.pods
        total.runs |= item.runs
        total.identifiers |= set(bracket_identifiers(pattern))
        total.last = max(total.last, item.last)
    return sites


def _clip(span: Span, bounds: Span) -> Span | None:
    """The part of the span inside the bounds, or None when they do not overlap."""
    start, end = max(span[0], bounds[0]), min(span[1], bounds[1])
    return (start, end) if end > start else None


def log_problems(
    logs: CloudLogging,
    scope: Scope,
    now: datetime,
    *,
    env: rc.Environment,
    containers: tuple[str, ...],
    min_spread: int = LOG_MIN_SPREAD,
    base_filter: str = "",
) -> tuple[list[Problem], list[str]]:
    """New log sites, and notes on what was not judged.

    A site is the leading text of a line (site_fragment), so a known line with a
    new value (another error code, another id) is not new; a line that the code
    never wrote before is. base_filter replaces the GKE container filter (Cloud Run,
    Composer); it must select warning and error lines only.
    """
    base = base_filter or log_filter(env, containers)
    read_start = max(scope.window[0], now - timedelta(minutes=LOG_READ_MINUTES))
    notes = []
    # Newest first: a cap keeps the lines that decide "still recurring" and "just before the release".
    window_lines = list(logs.lines(base, [(read_start, now)], limit=LOG_MAX_LINES, newest_first=True))
    window = count_sites(count_patterns(window_lines))
    reference_span = (scope.release_start - timedelta(minutes=LOG_REFERENCE_MINUTES), scope.release_start)
    reference_spans = [span for baseline in scope.baseline for span in [_clip(baseline, reference_span)] if span]
    reference_lines = list(logs.lines(base, reference_spans, limit=LOG_MAX_LINES, newest_first=True))
    known = set(count_sites(count_patterns(reference_lines)))
    for name, read in (("window", window_lines), ("reference", reference_lines)):
        if len(read) >= LOG_MAX_LINES:
            notes.append(f"the {name} read stopped at {LOG_MAX_LINES:,} lines (the newest)")
    recent = _iso(now - timedelta(minutes=LOG_RECENT_MINUTES))[:19]
    candidates = sorted(
        (
            (site, item)
            for site, item in window.items()
            if site not in known and item.count >= LOG_MIN_COUNT and item.spread >= min_spread and item.last >= recent
        ),
        key=lambda pair: -pair[1].count,
    )
    informational = [site for site, item in candidates if not is_error(item.pattern)]
    if informational:
        notes.append(f"{len(informational)} new informational log lines not judged: {informational[0][:100]}")
    candidates = [(site, item) for site, item in candidates if is_error(item.pattern)]
    if len(candidates) > LOG_MAX_PROBES:
        notes.append(f"{len(candidates) - LOG_MAX_PROBES} new-looking log lines not searched in the baseline")
    problems = []
    for site, item in candidates[:LOG_MAX_PROBES]:
        text = site.split(":", 2)[2]
        if '"' in text or "\\" in text:
            notes.append(f"cannot search for: {site[:120]}")
            continue
        probe = f"{base} AND {message_match(text)}"
        try:
            found = next(logs.lines(probe, scope.baseline, limit=1, newest_first=True, seconds=LOG_PROBE_SECONDS), None)
        except TimeoutError:
            notes.append(f"baseline search ran out of time: {site[:120]}")
            continue
        if found is not None:
            continue
        problems.append(
            Problem(
                kind="log",
                key=f"log:{site}",
                text=(
                    f"new log line, {item.count} in the window from {len(item.pods)} pods "
                    f"({len(item.runs)} runs), 0 in the baseline: {item.example[:220]}"
                ),
                count=item.count,
                literals=[text, *[part for part in literal_fragments(item.pattern) if part != text]],
                identifiers=sorted(item.identifiers),
            )
        )
    return problems, notes


# --------------------------------------------------------------------------- source tree

KEYWORDS = frozenset(
    "if for while switch catch return function func def class new await async typeof sizeof else elif try "
    "with yield import export from super this self print len make append panic range select case go defer "
    "require console log error warn info debug assert throw raise".split()
)
# An arrow function whose parameters continue on the next lines; it counts only when "=>" follows
# the parameters (`const data = (` that opens a parenthesized expression is no function).
MULTILINE_ARROW_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?\([^)]*$"
)
FUNCTION_PATTERNS = {
    ".go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]")],
    ".py": [re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")],
    "js": [
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]"),
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
            r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"
        ),
        re.compile(
            r"^\s+(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
            r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^={;]+)?\{\s*$"
        ),
        # A method whose parameters continue on the next lines (at least one modifier, so a call
        # statement that spans lines is no method): `private static async startWithFunding(`.
        re.compile(
            r"^\s+(?:(?:public|private|protected|static|async|readonly|override)\s+)+"
            r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*$"
        ),
        MULTILINE_ARROW_RE,
    ],
}
# An Express or koa-router route registration with an inline handler:
# `api.patch("/v2/x/:id", auth, async (ctx) => {`. Its name is the Datadog resource of the route
# (`patch_/v2/x/:id`, lowercase), so a failing route or a monitor grouped by resource_name finds
# the handler that serves it.
ROUTE_RE = re.compile(r"""^\s*[A-Za-z_$][\w$]*\.(get|post|put|patch|delete|del|all)\(\s*(["'`])(/[^"'`]*)\2""")


def route_name(method: str, path: str) -> str:
    """The Datadog resource name of a route: `<method>_<path>`, lowercase."""
    return f"{'delete' if method == 'del' else method.lower()}_{path.lower()}"


# A route registration with an inline handler can be long; it ends at its closing parenthesis.
ROUTE_LINES = 3000
IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")
# A JavaScript regex literal where an expression starts (after `=`, `(`, `,`, `:` ...).
# A JavaScript regex literal where an expression starts: after a punctuator, `=>`, or a keyword such
# as `return` (`values.filter((v) => /\(/.test(v))`, `return /\(/.test(q);`).
REGEX_LITERAL_RE = re.compile(
    r"(^|[=(,:\[!&|?{};]|=>|(?<![.\w$])(?:return|typeof|case|void|throw|yield|await|in|of|delete)\b)"
    r"(\s*)/(?![/*])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+/[a-z]*"
)


def _without_regex(code: str) -> str:
    """The line with its regex literals replaced by `/./` (their parentheses are no code)."""
    return REGEX_LITERAL_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}/./", code)


def _route_end(lines: list[str], start: int) -> tuple[int, bool]:
    """(the last line (1-based) of a route registration, whether it holds an inline handler).

    The registration ends where the parenthesis of `x.get(` closes; a named handler
    (`api.get("/a", handlers.a);`) ends on its own line and takes in no later route.
    """
    nesting, inline = 0, False
    for index in range(start - 1, min(len(lines), start - 1 + ROUTE_LINES)):
        # Regex literals (`/\(/`) hold parentheses that are no code.
        code = _without_regex(_code_only(lines[index]))
        inline = inline or "=>" in code or re.search(r"\bfunction\b", code) is not None
        for char in code:
            if char in "([":
                nesting += 1
            elif char in ")]":
                nesting -= 1
                if nesting == 0:
                    return index + 1, inline
    return start, inline


def _arrow_follows(lines: list[str], start: int) -> bool:
    """Whether "=>" follows the parameters that open on line `start` (a multi-line arrow function).

    Right after the closing parenthesis comes "=>", or a return type (":") that may span
    lines and ends in "=>"; anything else (`).body;`, `).map(`) is an expression.
    """
    nesting = 0
    last = min(len(lines), start - 1 + SIGNATURE_LINES)
    for index in range(start - 1, last):
        code = _code_only(lines[index])
        for column, char in enumerate(code):
            if char == "(":
                nesting += 1
            elif char == ")":
                nesting -= 1
                if nesting == 0:
                    rest = " ".join([code[column + 1 :], *(_code_only(line) for line in lines[index + 1 : last])])
                    rest = rest.strip()
                    if rest.startswith("=>"):
                        return True
                    return rest.startswith(":") and _arrow_after_type(rest)
    return False


def _arrow_after_type(text: str) -> bool:
    """Whether a return type (`: Promise<{ a: string; b: number }>`) ends in "=>" before the statement ends.

    Semicolons inside the type's brackets belong to the type; only one outside them ends the statement.
    """
    depth, position = 0, 0
    while position < len(text):
        if text.startswith("=>", position):
            if depth == 0:
                return True
            position += 2
            continue
        char = text[position]
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            return False
        position += 1
    return False


CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


@dataclass
class Function:
    path: str
    name: str
    start: int
    end: int
    calls: frozenset[str] = frozenset()


def _patterns_for(path: str) -> list[re.Pattern[str]]:
    """The function definition patterns for the language of a path (Go, Python, else JavaScript)."""
    for extension in (".go", ".py"):
        if path.endswith(extension):
            return FUNCTION_PATTERNS[extension]
    return FUNCTION_PATTERNS["js"]


STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
LINE_COMMENT_RE = re.compile(r"//.*$")
# A block comment on one line, or the start of one that continues on the next lines.
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/|/\*.*$")
# How many lines a function signature may span before its body opens.
SIGNATURE_LINES = 15


def _code_only(line: str) -> str:
    """A line without string literals, regex literals and comments, for brace counting."""
    return BLOCK_COMMENT_RE.sub(" ", LINE_COMMENT_RE.sub("", _without_regex(STRING_RE.sub('""', line))))


# Words after which a "/" starts a regex literal, not a division.
REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "case", "void", "throw", "yield", "await", "in", "of", "delete", "instanceof", "new", "else"}
)


CONDITION_KEYWORDS = frozenset({"if", "while", "for", "with"})


def _regex_allowed(last: str, word: str) -> bool:
    """Whether a "/" after the last token starts a regex literal (an expression can start there)."""
    if last == "word":
        return word in REGEX_KEYWORDS
    return last not in (")", "]", "literal", "}")


def blank_comments(lines: list[str], go: bool = False) -> list[str]:
    """The lines with comments blanked and string and regex contents filled, in one pass over the file.

    The pass knows whether it is in code, a // or /* */ comment, a string (also a template or Go raw
    string over several lines) or a regex literal, so quotes inside comments, `src/*` inside a
    string and `/[/*]/` are read as what they are. The contents of a template's `${...}` stay code,
    so their calls count. Line numbers stay; the parse of function bodies then never sees a brace in a
    comment or a string. Go has no regex literals and no escapes in its raw strings.
    """
    text = "\n".join(lines)
    out = list(text)
    size = len(text)
    templates: list[int] = []  # open "{" count inside each open `${` expression, innermost last
    conditions: list[bool] = []  # for each open "(": whether it holds the condition of if, while, for or with

    def fill(begin: int, stop: int, char: str) -> None:
        """Replace text[begin:stop] with char, keeping line breaks."""
        for position in range(begin, min(stop, size)):
            if out[position] != "\n":
                out[position] = char

    def template_text(begin: int) -> tuple[int, str]:
        """Fill template text from begin; (where code resumes, the last token: a literal or `${`)."""
        stop = begin
        while stop < size:
            if text[stop] == "\\":
                stop += 2
                continue
            if text[stop] == "`":
                fill(begin, stop, "_")
                out[stop] = "_"  # a literal stays: "`` / 2" divides
                return stop + 1, "literal"
            if text.startswith("${", stop):
                fill(begin, stop, "_")
                fill(stop, stop + 2, " ")  # blank delimiters: `VALUES ${rows}` is no one-line body
                templates.append(0)
                return stop + 2, "{"
            stop += 1
        fill(begin, size, "_")
        return size, "literal"

    # `last` is the last token: "word", "literal", or a punctuation character; `word` is the last word.
    position, last, word = 0, "", ""
    while position < size:
        char = text[position]
        if char in " \t\r\n":
            position += 1
            continue
        pair = text[position : position + 2]
        if pair == "//":
            stop = text.find("\n", position)
            stop = size if stop == -1 else stop
            fill(position, stop, " ")
            position = stop
            continue
        if pair == "/*":
            stop = text.find("*/", position + 2)
            stop = size if stop == -1 else stop + 2
            fill(position, stop, " ")
            position = stop
            continue
        if char == "`" and not go:
            # Template backticks become a space and a "_": a later per-line string pass must not
            # pair the backticks of a template and of one nested in its `${...}`.
            out[position] = " "
            position, last = template_text(position + 1)
            continue
        if char in "\"'`":
            stop = position + 1
            while stop < size and text[stop] != char:
                if text[stop] == "\\" and char != "`":
                    stop += 2
                    continue
                if char != "`" and text[stop] == "\n":
                    break  # an unterminated one-line string ends at the line
                stop += 1
            fill(position + 1, stop, "_")
            position, last = min(stop + 1, size), "literal"
            continue
        if templates and char in "{}":
            if char == "{":
                templates[-1] += 1
            elif templates[-1] == 0:
                templates.pop()  # the "}" of `${`: the template text goes on
                out[position] = " "
                position, last = template_text(position + 1)
                continue
            else:
                templates[-1] -= 1
        if not go and char == "/" and _regex_allowed(last, word):
            stop, in_class = position + 1, False
            while stop < size and text[stop] != "\n":
                if text[stop] == "\\":
                    stop += 2
                    continue
                if text[stop] == "[":
                    in_class = True
                elif text[stop] == "]":
                    in_class = False
                elif text[stop] == "/" and not in_class:
                    break
                stop += 1
            if stop < size and text[stop] == "/":
                fill(position + 1, stop, "_")
                stop += 1
                while stop < size and text[stop].isalpha():
                    stop += 1
                position, last = stop, "literal"
                continue
        if pair in ("++", "--"):
            # Postfix (`index++`) ends an expression, so a "/" after it divides; prefix (`++index`) does not.
            postfix = last in (")", "]", "literal") or (last == "word" and word not in REGEX_KEYWORDS)
            position, last = position + 2, "literal" if postfix else char
            continue
        if char.isalnum() or char in "_$":
            stop = position
            while stop < size and (text[stop].isalnum() or text[stop] in "_$"):
                stop += 1
            # A name after a dot (`units.in`) is a property, never a keyword.
            word = "" if last == "." else text[position:stop]
            position, last = stop, "word"
            continue
        if char == "(":
            # `if (x) /re/.test(y)`: after the condition of if, while, for or with a statement starts.
            conditions.append(last == "word" and word in CONDITION_KEYWORDS)
        elif char == ")" and conditions and conditions.pop():
            last, position = ";", position + 1
            continue
        last = char
        position += 1
    return "".join(out).split("\n")


def _brace_end(lines: list[str], start: int) -> int:
    """The last line (1-based) of a brace-delimited body whose definition is on line `start`.

    Braces inside the signature (a default `options = {}`, a `Promise<{ ok: boolean }>`
    return type, a Go `map[string]struct{}` parameter) are not the body. The signature
    ends on the first line, at most SIGNATURE_LINES on, where every "(" and "[" has
    closed; the body opens at the last "{" of that line when the line ends with it,
    and closes where the braces balance. A signature line that holds a whole body
    (`func f() int { return 1 }`) or ends a statement (`const f = (x) => x;`) ends there.
    """
    nesting = 0
    index = start - 1
    while index < min(len(lines), start - 1 + SIGNATURE_LINES):
        code = _code_only(lines[index]).rstrip()
        nesting += sum(code.count(char) for char in "([") - sum(code.count(char) for char in ")]")
        if nesting > 0:
            index += 1
            continue
        if TYPE_OPEN_RE.search(code):
            # `): {` opens an object return type, not the body: the body follows that type.
            line, column = _close_at(lines, index, code.rindex("{"))
            rest = _code_only(lines[line])[column + 1 :].rstrip()
            if rest.endswith("{"):
                return _close_of(lines, line, column + 1 + rest.rindex("{"))
            if rest.endswith(";"):
                return line + 1
            index = line + 1
            continue
        if code.endswith("{"):
            return _close_of(lines, index, code.rindex("{"))
        if code.endswith(";") or (code.endswith("}") and code.count("{") and code.count("{") == code.count("}")):
            return index + 1
        index += 1
    return start


# The end of a signature line that opens an object return type: `): {`, `): Promise<{`.
TYPE_OPEN_RE = re.compile(r"\)\s*:\s*(?:[\w.]+<)?\{$")


def _close_at(lines: list[str], index: int, column: int) -> tuple[int, int]:
    """(line index, column) of the "}" that closes the "{" at lines[index][column] (code only)."""
    depth = 0
    for number in range(index, len(lines)):
        code = _code_only(lines[number])
        for position in range(column if number == index else 0, len(code)):
            if code[position] == "{":
                depth += 1
            elif code[position] == "}":
                depth -= 1
                if depth == 0:
                    return number, position
    return len(lines) - 1, 0


def _close_of(lines: list[str], index: int, column: int) -> int:
    """The line (1-based) of the "}" that closes the "{" at lines[index][column] (code only)."""
    depth = 0
    for number in range(index, len(lines)):
        code = _code_only(lines[number]).rstrip()
        for char in code[column:] if number == index else code:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return number + 1
    return len(lines)


def _indent_end(lines: list[str], start: int) -> int:
    """The last line (1-based) of a Python block: before the next line indented at or left of the def."""
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    end = start
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        end = index + 1
    return end


def functions_of(path: str, text: str) -> list[Function]:
    """The functions of a file by their definition lines and their real bodies.

    Go, TypeScript and JavaScript bodies end at the brace that closes them; Python
    bodies end where the indentation returns to the def's level. A declaration
    between two functions belongs to neither.
    """
    python = path.endswith(".py")
    raw = text.splitlines()
    # Ranges and calls come from the blanked lines (same length); route paths from the original text.
    lines = raw if python else blank_comments(raw, go=path.endswith(".go"))
    patterns = _patterns_for(path)
    out = []
    for index, line in enumerate(lines, start=1):
        route = None if python or path.endswith(".go") else ROUTE_RE.match(raw[index - 1])
        # A registration inside a comment or a string is blanked: it is no route.
        if route and line[: route.start(2)] == raw[index - 1][: route.start(2)]:
            end, inline = _route_end(lines, index)
            body = "\n".join(lines[index - 1 : end])
            names = set(CALL_RE.findall(body))
            if not inline:
                # A named handler: the names it passes (`handlers.getWatcher`) are what it calls.
                names |= set(IDENTIFIER_RE.findall(_code_only(body[route.end() :])))
            calls = frozenset(name for name in names if name not in KEYWORDS and len(name) >= 4)
            out.append(Function(path, route_name(route.group(1), route.group(3)), index, end, calls))
            continue
        for pattern in patterns:
            match = pattern.match(line)
            if match and pattern is MULTILINE_ARROW_RE and not _arrow_follows(lines, index):
                continue
            if match and match.group(1) not in KEYWORDS:
                end = _indent_end(lines, index) if python else _brace_end(lines, index)
                body = "\n".join(lines[index - 1 : end])
                calls = frozenset(name for name in CALL_RE.findall(body) if name not in KEYWORDS and len(name) >= 4)
                out.append(Function(path, match.group(1), index, end, calls))
                break
    return out


def enclosing(functions: list[Function], line: int) -> Function | None:
    """The innermost function that contains the line, or None."""
    inside = [f for f in functions if f.start <= line <= f.end]
    return min(inside, key=lambda f: f.end - f.start) if inside else None


class SourceTree:
    """The release's source tree (the watch build gets a copy): functions, what they call, literal search."""

    def __init__(self, root: Path, code: rc.Code) -> None:
        """A source tree under root; it reads the files only when they are first needed."""
        self.root = root
        self.code = code
        self._texts: dict[str, str] | None = None
        self._functions: dict[str, list[Function]] = {}
        self._callers: dict[str, int] | None = None
        self._by_name: dict[str, list[Function]] | None = None

    @property
    def available(self) -> bool:
        """Whether any source prefix exists under the root."""
        return any((self.root / prefix.rstrip("/")).exists() for prefix in self.code.source_prefixes)

    def texts(self) -> dict[str, str]:
        """The text of every source file: repository path -> text (read once)."""
        if self._texts is None:
            self._texts = {}
            for prefix in self.code.source_prefixes:
                base = self.root / prefix.rstrip("/")
                paths = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
                for path in paths:
                    if not path.is_file():
                        continue
                    relative = path.relative_to(self.root).as_posix()
                    if "node_modules/" in relative or not self.code.is_source(relative):
                        continue
                    try:
                        self._texts[relative] = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
        return self._texts

    def functions(self, path: str) -> list[Function]:
        """The functions in a source file (parsed once)."""
        if path not in self._functions:
            text = self.texts().get(path)
            self._functions[path] = functions_of(path, text) if text is not None else []
        return self._functions[path]

    def _index(self) -> None:
        """Index the functions by name and count the calls of each name (once)."""
        if self._callers is not None:
            return
        self._callers, self._by_name = {}, {}
        for path in self.texts():
            for function in self.functions(path):
                self._by_name.setdefault(function.name, []).append(function)
                for name in function.calls:
                    self._callers[name] = self._callers.get(name, 0) + 1

    def named(self, name: str) -> list[Function]:
        """Every function with this name in the source tree (route handlers by their resource name)."""
        return [function for path in sorted(self.texts()) for function in self.functions(path) if function.name == name]

    def enclosing(self, path: str, line: int) -> Function | None:
        """The innermost function of a file that contains the line, or None."""
        return enclosing(self.functions(path), line)

    def find_text(self, needle: str) -> list[tuple[str, int]]:
        """Where the text occurs in the source files: (path, line), at most 50."""
        out = []
        for path, text in self.texts().items():
            index = text.find(needle)
            while index >= 0 and len(out) < 50:
                out.append((path, text.count("\n", 0, index) + 1))
                index = text.find(needle, index + 1)
        return out

    def callees(self, function: Function) -> list[Function]:
        """The functions this one calls, by name; names that most of the code calls (helpers) are left out."""
        self._index()
        out = []
        for name in function.calls:
            if name == function.name or (self._callers or {}).get(name, 0) > 40:
                continue
            candidates = (self._by_name or {}).get(name, [])
            if 0 < len(candidates) <= 3:  # a name defined in many places tells nothing
                out += candidates
        return out


def origins(problem: Problem, tree: SourceTree) -> list[Function]:
    """The functions that write this error."""
    found: dict[tuple[str, str, int], Function] = {}

    def add(function: Function | None) -> None:
        """Add a function to the origins found, once per path, name and start line."""
        if function is not None:
            found[(function.path, function.name, function.start)] = function

    for fragment in [part for part in problem.literals[:2] if len(part) >= 15]:
        for path, line in tree.find_text(fragment)[:10]:
            add(tree.enclosing(path, line))
    for identifier in problem.identifiers:
        for quote in ('"', "'", "`"):
            for path, line in tree.find_text(f"{quote}{identifier}{quote}")[:10]:
                function = tree.enclosing(path, line)
                if function is None and problem.kind == "dag_failure":
                    # A DAG is defined at module level: its file is the unit that writes it.
                    function = Function(path, MODULE, 1, len(tree.texts().get(path, "").splitlines()))
                add(function)
    for path, name in problem.frames:
        for function in tree.functions(path):
            if function.name == name:
                add(function)
    # A failing route, or a monitor grouped by route: the handler that serves it.
    resource = str((problem.fields or {}).get("resource_name") or "").lower()
    if resource:
        for function in tree.named(resource):
            add(function)
    return list(found.values())


# --------------------------------------------------------------------------- suspects

ORIGIN, LITERAL, CALLEE, SAME_FILE, WORD = 6, 4, 4, 2, 1
# The name of a whole-file origin (a DAG file): a commit that changes the file changes it.
MODULE = "<file>"
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class Change:
    """What one commit changed: files, functions (path, name) and the changed lines."""

    sha: str
    files: list[str]
    functions: set[tuple[str, str]]
    lines: list[str]
    added: list[str] = field(default_factory=list)


def parse_change(sha: str, files: list[dict], sources: dict[str, str]) -> Change:
    """What a commit changed. A function counts as changed only when a changed line lies in it, in the
    source at that commit (sources: path -> text); a patch heading names a neighbor, not a change."""
    functions: set[tuple[str, str]] = set()
    lines: list[str] = []
    added: list[str] = []
    for item in files:
        path = item.get("filename", "")
        at_commit = functions_of(path, sources[path]) if path in sources else []
        new_line = 0
        for raw in str(item.get("patch") or "").splitlines():
            hunk = HUNK_RE.match(raw)
            if hunk:
                new_line = int(hunk.group(1))
                continue
            if raw.startswith(("+", "-")):
                lines.append(raw[1:])
                if raw.startswith("+"):
                    added.append(raw[1:])
                    for pattern in _patterns_for(path):
                        defined = pattern.match(raw[1:])
                        if defined and defined.group(1) not in KEYWORDS:
                            functions.add((path, defined.group(1)))
                            break
                # An added line sits at new_line; a removed one where it was, just before new_line.
                function = enclosing(at_commit, max(new_line, 1))
                if function is not None:
                    functions.add((path, function.name))
            if not raw.startswith("-"):
                new_line += 1
    return Change(sha, [item.get("filename", "") for item in files], functions, lines, added)


def github_text(path: str, token: str) -> str | None:
    """A file at a commit (GitHub contents API, raw, any size); None when GitHub cannot serve it."""
    request = urllib.request.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        headers={
            "Accept": "application/vnd.github.raw",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                return None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2**attempt)
    return None


def _words(problem: Problem) -> set[str]:
    """The words of 5 or more characters in the fields and identifiers of a problem, lowercase."""
    text = " ".join([*problem.fields.values(), *problem.identifiers])
    return {word for word in re.split(r"[^a-z0-9]+", text.lower()) if len(word) >= 5}


def score_commit(
    problem: Problem, commit: dict, change: Change | None, found: list[Function], tree: SourceTree | None
) -> tuple[int, list[str], bool, bool]:
    """(score, reasons, linked, adds_line).

    linked: the code evidence (not the word matches) scores 2 or more.
    adds_line: the commit adds the log statement itself (new logging, maybe of an old failure).
    """
    if change is None:
        return 0, ["files unknown"], False, False
    score, reasons = 0, []
    adds_line = (
        problem.kind == "log" and bool(problem.literals) and any(problem.literals[0] in line for line in change.added)
    )
    if adds_line:
        reasons.append("adds this log line")
    origin_keys = {(f.path, f.name) for f in found}
    hit = sorted(origin_keys & change.functions) + sorted(
        (f.path, f.name) for f in found if f.name == MODULE and f.path in change.files
    )
    if hit:
        score += ORIGIN
        reasons.append(f"changes {hit[0][0]}:{hit[0][1]}, which writes the error")
    texts = [part for part in problem.literals[:2] if len(part) >= 15] + [
        i for i in problem.identifiers if len(i) >= 10
    ]
    literal = next((text for text in texts if any(text in line for line in change.lines)), None)
    if literal:
        score += LITERAL
        reasons.append(f"its diff has {literal[:60]!r}")
    if tree is not None and found:
        callees = {(f.path, f.name) for origin in found for f in tree.callees(origin)} - origin_keys
        direct = sorted(callees & change.functions)
        if direct:
            score += CALLEE
            reasons.append(f"changes {direct[0][0]}:{direct[0][1]}, which that code calls")
    if not hit and {f.path for f in found} & set(change.files):
        score += SAME_FILE
        reasons.append("changes the file that writes the error")
    words = sorted(
        w
        for w in _words(problem)
        if any(w in path.lower() for path in change.files) or w in commit.get("title", "").lower()
    )
    linked = score >= SAME_FILE
    if words:
        score += min(len(words), 2) * WORD
        reasons.append(f"names {', '.join(words[:2])}")
    return score, reasons or ["no link to the error"], linked, adds_line


# How many commits (the newest) the suspect search reads.
MAX_SUSPECT_COMMITS = 40


def commit_changes(commits: list[dict], token: str | None, repo: str, code: rc.Code) -> dict[str, Change | None]:
    """What each of the newest MAX_SUSPECT_COMMITS commits changes; None when the commit cannot be read."""
    out: dict[str, Change | None] = {}
    for commit in commits[-MAX_SUSPECT_COMMITS:]:
        if not token:
            out[commit["sha"]] = None
            continue
        try:
            payload = rr.github_json(f"repos/{repo}/commits/{commit['sha']}", token)
        except GuardError:
            out[commit["sha"]] = None
            continue
        files = payload.get("files") or []
        sources = {}
        for item in files:
            path = item.get("filename", "")
            if code.is_source(path) and item.get("status") != "removed":
                text = github_text(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={commit['sha']}", token)
                if text is not None:
                    sources[path] = text
        out[commit["sha"]] = parse_change(commit["sha"], files, sources)
    return out


def pull_request_of(commit: dict, token: str | None, repo: str) -> int | None:
    """The PR of a commit: the "(#123)" of a squash merge, else GitHub's commit-to-PR link."""
    if commit.get("pr"):
        return int(commit["pr"])
    if not token:
        return None
    try:
        pulls = rr.github_json(f"repos/{repo}/commits/{commit['sha']}/pulls", token)
    except GuardError:
        return None
    merged = (
        [item for item in pulls if isinstance(item, dict) and item.get("merged_at")] if isinstance(pulls, list) else []
    )
    return int(merged[0]["number"]) if merged else None


def trace_suspects(
    problems: list[Problem],
    scope: Scope,
    *,
    token: str | None,
    repo: str,
    tree: SourceTree | None,
    code: rc.Code,
) -> None:
    """Rank the scope's commits for each problem, grouped by PR, and decide whether the top one is clear."""
    if not problems:
        return
    tree = tree if tree is not None and tree.available else None
    # The newest commits (scope.commits is oldest first); the ones left out are named in the problem.
    considered = scope.commits[-MAX_SUSPECT_COMMITS:]
    left_out = len(scope.commits) - len(considered)
    changes = commit_changes(considered, token, repo, code)
    prs = {commit["sha"]: pull_request_of(commit, token, repo) for commit in considered}
    releases = scope.commit_release
    for problem in problems:
        found = origins(problem, tree) if tree is not None else []
        problem.origins = [f"{f.path}:{f.name}" for f in found]
        groups: dict[str, dict] = {}
        for commit in considered:
            score, reasons, linked, adds_line = score_commit(problem, commit, changes.get(commit["sha"]), found, tree)
            pr = prs.get(commit["sha"])
            key = f"pr:{pr}" if pr else f"sha:{commit['sha']}"
            current = groups.get(key)
            if current is None or score > current["score"]:
                groups[key] = {
                    "sha": commit["sha"],
                    "title": commit.get("title", ""),
                    "pr": pr,
                    "author": commit.get("author"),
                    "score": score,
                    "linked": linked,
                    "adds_line": adds_line,
                    "reason": "; ".join(reasons),
                    "release_id": (releases.get(commit["sha"]) or {}).get("release_id"),
                }
        ranked = sorted(groups.values(), key=lambda item: -item["score"])
        problem.suspects = ranked
        top = ranked[0]["score"] if ranked else 0
        second = ranked[1]["score"] if len(ranked) > 1 else 0
        problem.clear = top >= CLEAR_MIN and top - second >= CLEAR_MARGIN
        if left_out:
            problem.text += f"; {left_out} older commit(s) of the judged releases were not searched"
        if problem.clear and ranked[0]["adds_line"]:
            # New logging cannot tell a new failure from an old one that was silent: ask a human.
            problem.clear = False
            problem.text += "; the top suspect adds this log line, so the failure may be older"


# --------------------------------------------------------------------------- entry point


def check_new_errors(
    record: dict,
    *,
    service: rc.Service,
    store: rr.Store,
    dd,  # noqa: ANN001 - release_verify.DatadogClient or None
    logs: CloudLogging | None,
    now: datetime,
    token: str | None,
    source_root: Path,
) -> dict:
    """Run the configured sources and trace each problem; a source that cannot run is "inconclusive", never "failed"."""
    signals = service.signals
    scope = release_scope(store, record, now)
    parts: dict[str, dict] = {}
    problems: list[Problem] = []
    reader = DatadogReader(dd) if dd is not None else None

    def run(name: str, check: Callable[[], list[Problem]], needs: str | None, configured: bool) -> None:
        """Run one check and save its status in parts; a check that cannot run is inconclusive."""
        if not configured:
            parts[name] = {"status": "not_configured", "detail": ""}
            return
        if needs == "datadog" and reader is None:
            parts[name] = {"status": "inconclusive", "detail": "no Datadog key"}
            return
        if needs == "logging" and logs is None:
            parts[name] = {"status": "inconclusive", "detail": "no Cloud Logging client"}
            return
        try:
            found = check()
        except NoAccess as error:
            scope_name = SCOPES.get(name)
            detail = f"no access: {error}"
            if scope_name:
                detail = f"the Datadog application key lacks the {scope_name} scope ({error})"
            parts[name] = {"status": "inconclusive", "detail": detail[:400]}
            return
        except (GuardError, TimeoutError, OSError, ValueError, KeyError, rr.RecordError) as error:
            parts[name] = {"status": "inconclusive", "detail": f"could not run: {str(error)[:300]}"}
            return
        parts.setdefault(name, {"status": "checked", "detail": ""})
        parts[name]["found"] = len(found)
        problems.extend(found)

    def logs_check() -> list[Problem]:
        """Find the new log errors and note the log check details."""
        found, notes = log_problems(
            logs, scope, now, env=service.prod, containers=signals.log_containers, min_spread=signals.log_min_spread
        )
        parts["logs"] = {"status": "checked", "detail": "; ".join(notes)}
        return found

    series = signals.failure_series
    run(
        "failure_series",
        lambda: failure_problems(reader, scope, series),
        "datadog",
        bool(series.get("failures")),
    )
    run("logs", logs_check, "logging", bool(signals.log_containers))
    run(
        "error_tracking",
        lambda: issue_problems(reader, scope, now, signals, service.code),
        "datadog",
        bool(signals.error_tracking),
    )
    run("monitors", lambda: monitor_problems(reader, scope, now, signals.monitors), "datadog", bool(signals.monitors))
    trace_suspects(
        problems,
        scope,
        token=token,
        repo=service.github_repo,
        tree=SourceTree(source_root, service.code),
        code=service.code,
    )
    return {"scope": scope.summary(), "parts": parts, "problems": [problem.summary() for problem in problems]}


def rollback_target(record: dict, store: rr.Store, problems: list[dict], scope: dict) -> dict | None:
    """The release before the oldest release that holds a clear suspect; None when that is the newest release."""
    releases = scope.get("releases") or []
    culprit = [p["suspects"][0].get("release_id") for p in problems if p.get("clear") and p.get("suspects")]
    culprit = [release_id for release_id in culprit if release_id in releases]
    if not culprit:
        return None
    oldest = max(culprit, key=releases.index)
    if oldest == record["release_id"]:
        return None
    holder = _read(store, oldest)
    previous = (holder or {}).get("previous") or {}
    usable = previous.get("workloads") or previous.get("release_id") or previous.get("commit")
    if not usable and not (holder or {}).get("previous_snapshot"):
        return None
    # "previous-of:<id>" is what ran before that release, from its own "before" snapshot: it works also
    # when that release is the first one the kit recorded (no recorded predecessor). A light service
    # (Composer, Cloud Run) keeps only the commit, which is all its rollback command needs.
    return {
        "release_id": previous.get("release_id"),
        "commit": previous.get("commit"),
        "images": previous.get("images"),
        "rollback_to": f"previous-of:{oldest}",
    }
