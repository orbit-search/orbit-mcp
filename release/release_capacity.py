#!/usr/bin/env python3
"""Capacity verdict of a rollout that did not finish, and rollout waits scaled by replica count.

Rule 1 (deploy phase): a workload whose rollout timed out, or whose new pods restart while they
start, is "capacity" (environmental: no rollback, no suspected PRs, no hold, no author ping) only
when the new version has the right image, shows no application failure, is healthy where it runs,
and the cluster shows a capacity indicator. Anything else keeps the release check's usual path.

Rule 3: one rollout timeout per Deployment, from its replicas, strategy and probes. The prod
builds' rollout waits (`rollout-wait` below) and release_verify.py --wait-rollouts use it.

The classifier is a set of pure functions over kubectl JSON (pods, events, HPAs, nodes,
ReplicaSets), so recorded cases can feed it. gather() reads that JSON, read only.

    python3 release/release_capacity.py --service S timeout                    # Rule 3 per Deployment
    python3 release/release_capacity.py --service S classify --image IMAGE     # read-only verdict
    python3 release/release_capacity.py --service S rollout-wait --image-file F --step-id deploy-prod \\
        [--deployment NAME ...] [--failed-step-file /workspace/deploy_failed_step]

rollout-wait exits 0 when every rollout finished, or when one did not and the verdict is capacity
(it waits once more, then the build goes on with its later steps); 1 otherwise (the build writes
deploy_failed). Stdlib only; it runs in gcr.io/cloud-builders/kubectl (Python 3.10).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------- Rule 3: rollout timeouts

ROLLOUT_BASE_SECONDS = 900
# Scheduling, a node scale-up and the image pull of the first wave.
ROLLOUT_STARTUP_SECONDS = 300
# The readiness budget of one wave is at least this long.
ROLLOUT_MIN_READINESS_SECONDS = 120
ROLLOUT_CAP_SECONDS = 10800
# A capacity verdict waits once more; the rollout's whole budget is this many timeouts.
EXTENDED_WAIT_FACTOR = 2
# Kubernetes defaults.
DEFAULT_SURGE = "25%"
DEFAULT_UNAVAILABLE = "25%"
DEFAULT_GRACE_SECONDS = 30
DEFAULT_PROBE_PERIOD = 10
DEFAULT_PROBE_FAILURES = 3


def resolve_count(value: object, total: int, *, round_up: bool) -> int:
    """An int-or-percent strategy value resolved like Kubernetes: surge rounds up, unavailable rounds down."""
    if isinstance(value, str) and value.endswith("%"):
        share = total * float(value[:-1]) / 100
        return math.ceil(share) if round_up else math.floor(share)
    return int(value or 0)


def probe_budget(probe: dict | None) -> int:
    """initialDelay + period * failureThreshold of a probe (Kubernetes defaults), 0 without one."""
    if not probe:
        return 0
    return int(probe.get("initialDelaySeconds") or 0) + int(probe.get("periodSeconds") or DEFAULT_PROBE_PERIOD) * int(
        probe.get("failureThreshold") or DEFAULT_PROBE_FAILURES
    )


def rollout_plan(deployment: dict) -> dict:
    """The numbers behind rollout_timeout: replicas, pods per wave, waves, seconds per wave."""
    spec = deployment.get("spec") or {}
    replicas = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
    strategy = spec.get("strategy") or {}
    rolling = strategy.get("rollingUpdate") or {}
    if strategy.get("type") == "Recreate":
        surge, unavailable = 0, replicas
    else:
        surge = resolve_count(rolling.get("maxSurge", DEFAULT_SURGE), replicas, round_up=True)
        unavailable = resolve_count(rolling.get("maxUnavailable", DEFAULT_UNAVAILABLE), replicas, round_up=False)
    per_wave_pods = max(1, surge + unavailable)
    waves = max(1, math.ceil(replicas / per_wave_pods))
    pod = (spec.get("template") or {}).get("spec") or {}
    grace = int(pod.get("terminationGracePeriodSeconds") or DEFAULT_GRACE_SECONDS)
    readiness = 0
    for container in pod.get("containers") or []:
        readiness = max(readiness, probe_budget(container.get("startupProbe") or container.get("readinessProbe")))
    per_wave = grace + max(ROLLOUT_MIN_READINESS_SECONDS, readiness)
    return {"replicas": replicas, "per_wave_pods": per_wave_pods, "waves": waves, "per_wave_seconds": per_wave}


def rollout_timeout(deployment: dict, base: int = ROLLOUT_BASE_SECONDS) -> int:
    """Seconds to wait for one Deployment's rollout: max(base, waves * per_wave + startup), capped."""
    plan = rollout_plan(deployment)
    return min(ROLLOUT_CAP_SECONDS, max(base, plan["waves"] * plan["per_wave_seconds"] + ROLLOUT_STARTUP_SECONDS))


# --------------------------------------------------------------------------- Rule 1: the classifier

# Thresholds of the capacity indicators (spec 10-26, the same in deep-search and orbit-api).
NODE_CPU_MEDIAN_SHARE = 0.90
NODE_CPU_HOT_SHARE = 0.95
NODE_CPU_HOT_NODES = 3
KILL_LOOKBACK_SECONDS = 7200
OTHER_NAMESPACES_WITH_KILLS = 2
READY_SHARE = 0.5
LOG_TAIL_LINES = 50
# The logs of at most this many crashed new pods are read; an unread or unreadable one fails the verdict.
MAX_LOG_READS = 10
# A probe kill ends the container with SIGKILL (137), or SIGTERM (143) when it exits on the signal.
PROBE_KILL_EXIT_CODES = frozenset({137, 143})
# A container that handles SIGTERM itself ends a probe kill with its own code: the AI worker's graceful
# shutdown exits 0, and it has crashed with 139 while it closed its connections (orbit-api #1573). They
# count only when the container ended within its grace period after the kubelet's kill.
SIGTERM_HANDLER_EXIT_CODES = frozenset({0, 139})
# A killed run that lasted no longer than its probe budget plus this was killed while starting.
STARTUP_KILL_SLACK_SECONDS = 120
STARTUP_KILL_DEFAULT_SECONDS = 300
IMAGE_PULL_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"})
CONFIG_REASONS = frozenset({"CreateContainerConfigError", "CreateContainerError", "RunContainerError"})
PROBE_FAILED = re.compile(r"^(Liveness|Startup) probe failed")
PROBE_KILLING = re.compile(r"failed (liveness|startup) probe", re.IGNORECASE)
# A Python traceback, a Node stack, or an "Error:" line in the last log lines of a crashed container.
APP_ERROR = re.compile(r"Traceback \(most recent call last\)|^\s+at \S+ \(|(^|\s)\w*Error: ", re.MULTILINE)
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = "deployment.kubernetes.io/revision"

UTC = timezone.utc  # noqa: UP017 - Python 3.10 in Cloud Build


def _digest(reference: str | None) -> str | None:
    """The sha256 digest in an image reference or imageID, else None."""
    match = DIGEST_RE.search(reference or "")
    return match.group(0) if match else None


def _time(value: str | None) -> datetime | None:
    """An RFC 3339 time (Z, any fraction) as a UTC datetime; None when missing or malformed."""
    if not value:
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", value)
    return datetime.fromisoformat(match.group(1) + "+00:00") if match else None


def event_time(event: dict) -> datetime | None:
    """When an event last happened."""
    series = event.get("series") or {}
    return (
        _time(series.get("lastObservedTime"))
        or _time(event.get("lastTimestamp"))
        or _time(event.get("eventTime"))
        or _time((event.get("metadata") or {}).get("creationTimestamp"))
    )


def _cpu(value: object) -> float:
    """A CPU quantity (2, 500m, 1500000n) in cores."""
    text = str(value or "0")
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    if text.endswith("n"):
        return float(text[:-1]) / 1e9
    return float(text)


def _owned_by(item: dict, kind: str, names: set[str]) -> bool:
    """Whether an object has an owner reference of this kind with one of these names."""
    return any(
        ref.get("kind") == kind and ref.get("name") in names
        for ref in (item.get("metadata") or {}).get("ownerReferences") or []
    )


def _revision(item: dict) -> int:
    """The deployment revision annotation of a Deployment or ReplicaSet; 0 when missing."""
    try:
        return int(((item.get("metadata") or {}).get("annotations") or {}).get(REVISION, "0"))
    except ValueError:
        return 0


def split_replica_sets(deployment: dict, replica_sets: list[dict]) -> tuple[dict | None, list[dict]]:
    """(the new ReplicaSet: the Deployment's revision, else the newest; the older ones)."""
    name = deployment["metadata"]["name"]
    owned = [rs for rs in replica_sets if _owned_by(rs, "Deployment", {name})]
    if not owned:
        return None, []
    wanted = _revision(deployment)
    new = next((rs for rs in owned if wanted and _revision(rs) == wanted), None) or max(owned, key=_revision)
    return new, [rs for rs in owned if rs is not new]


def _container(spec: dict, name: str) -> dict:
    """The container with this name in a pod spec (the first one when the name is not found)."""
    containers = spec.get("containers") or []
    return next((item for item in containers if item.get("name") == name), containers[0] if containers else {})


def _status(pod: dict, container: str) -> dict:
    """The container status of one container of a pod; {} before the kubelet reports it."""
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return next((item for item in statuses if item.get("name") == container), {})


def _ready(pod: dict) -> bool:
    """Whether a pod has the condition Ready=True."""
    return any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in (pod.get("status") or {}).get("conditions") or []
    )


def _pod_events(events: list[dict], pod: str) -> list[dict]:
    """The events of one pod."""
    return [
        event
        for event in events
        if (event.get("involvedObject") or {}).get("kind") == "Pod"
        and (event.get("involvedObject") or {}).get("name") == pod
    ]


def _probe_kill(event: dict) -> bool:
    """A liveness or startup probe failure, or the kubelet's kill after one."""
    message = str(event.get("message") or "")
    return bool(
        (event.get("reason") == "Unhealthy" and PROBE_FAILED.search(message))
        or (event.get("reason") == "Killing" and PROBE_KILLING.search(message))
    )


def displaced(pod: dict) -> str:
    """Why a pod left its node without an application fault ("" when it did not).

    A Deployment's pod (restartPolicy Always) ends in phase Failed only when the node removes it:
    kubelet preemption for a critical pod, an eviction, a node shutdown. A terminating pod is going away.
    """
    status = pod.get("status") or {}
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return "terminating"
    if status.get("phase") != "Failed":
        return ""
    target = next((item for item in status.get("conditions") or [] if item.get("type") == "DisruptionTarget"), {})
    return str(target.get("message") or status.get("reason") or "failed on its node")[:120]


def _starting_limit(spec: dict) -> int:
    """How long a run may last and still count as a start: the startup or liveness budget + slack."""
    budget = max(probe_budget(spec.get("startupProbe")), probe_budget(spec.get("livenessProbe")))
    return budget + STARTUP_KILL_SLACK_SECONDS if budget else STARTUP_KILL_DEFAULT_SECONDS


def killed_by_probe(kills: list[dict], terminated: dict, grace: int) -> bool:
    """Whether a kubelet probe kill ended this run (orbit-api #1573).

    137 / 143: a probe event falls inside the run (or the run's times are unknown). 0 / 139, a container
    that handles SIGTERM itself (the AI worker exits 0, or 139 while it closes its connections): the run
    ended within its grace period after the kubelet's Killing event for the failed probe (a failed probe
    alone sends no signal; deep-search #2383). Any other code is the container's own exit.
    """
    code = terminated.get("exitCode")
    code = None if code is None else int(code)
    if not kills or (code is not None and code not in PROBE_KILL_EXIT_CODES | SIGTERM_HANDLER_EXIT_CODES):
        return False
    started, finished = _time(terminated.get("startedAt")), _time(terminated.get("finishedAt"))
    if started is None or finished is None:
        return code is None or code in PROBE_KILL_EXIT_CODES
    for kill in kills:
        first, last = _time(kill.get("firstTimestamp")) or event_time(kill), event_time(kill)
        if first is None or last is None:
            continue
        if code in SIGTERM_HANDLER_EXIT_CODES:
            # Only the kubelet's kill sends SIGTERM: a failed probe alone (Unhealthy, below the failure
            # threshold) does not excuse an exit 0 or a segfault (deep-search #2383).
            if kill.get("reason") != "Killing":
                continue
            # The kill came first, and the container ended within its grace period (+30 s event skew).
            if (
                started - timedelta(seconds=5) <= last <= finished + timedelta(seconds=30)
                and (finished - last).total_seconds() <= grace + 30
            ):
                return True
        elif first <= finished + timedelta(seconds=90) and last >= started - timedelta(seconds=5):
            return True
    return False


def _attempts(container_status: dict) -> list[dict]:
    """Every run the kubelet still reports, the current one first (deep-search #2379): a later crash is
    never hidden behind an earlier probe kill."""
    state = (container_status.get("state") or {}).get("terminated") or {}
    last = (container_status.get("lastState") or {}).get("terminated") or {}
    return [item for item in (state, last) if item]


def pod_facts(pod: dict, container: str, events: list[dict], logs: dict[str, str | None]) -> dict:
    """What one pod shows: Ready, Pending unschedulable, killed while starting or running, an application failure.

    A pod that a probe killed and that is Ready again is Ready (orbit-api #1573). A pod that a probe
    killed after its start (a run longer than the probe budget) and that is not Ready is not explained.
    """
    name = pod["metadata"]["name"]
    status = pod.get("status") or {}
    gone = displaced(pod)
    facts = {
        "name": name,
        "ready": False if gone else _ready(pod),
        "phase": status.get("phase"),
        "displaced": gone,
        "pending_unschedulable": False,
        "killed_while_starting": False,
        "killed_while_running": False,
        "ever_running": False,
        "restarts": 0,
        "image_digest": _digest(_status(pod, container).get("imageID")),
        "waiting": None,
        "failure": "",
    }
    if gone:
        return facts
    own = _pod_events(events, name)
    container_status = _status(pod, container)
    waiting = ((container_status.get("state") or {}).get("waiting") or {}).get("reason")
    attempts = _attempts(container_status)
    restarts = int(container_status.get("restartCount") or 0)
    scheduled = next((item for item in status.get("conditions") or [] if item.get("type") == "PodScheduled"), {})
    kills = [event for event in own if _probe_kill(event)]
    spec = _container(pod.get("spec") or {}, container)
    grace = int((pod.get("spec") or {}).get("terminationGracePeriodSeconds") or DEFAULT_GRACE_SECONDS)
    failure = ""
    if any(item.get("reason") == "OOMKilled" for item in attempts):
        failure = "OOMKilled"
    elif waiting in IMAGE_PULL_REASONS or waiting in CONFIG_REASONS:
        failure = waiting
    else:
        bad = next((item for item in attempts if not killed_by_probe(kills, item, grace)), None)
        if bad:
            failure = f"exit {bad.get('exitCode')} ({bad.get('reason') or 'no reason'})" + (
                "" if kills else ", not killed by a probe"
            )
    # A crash shows as a terminated run, or only as CrashLoopBackOff / restarts when the kubelet kept no
    # termination details: its logs are checked either way (Greptile #500).
    crashed = bool(attempts) or waiting == "CrashLoopBackOff" or restarts > 0
    if not failure and crashed and name in logs:
        text = logs[name]
        if text is None:
            failure = "crashed and its logs could not be read"
        elif APP_ERROR.search(text):
            line = next((row.strip() for row in reversed(text.splitlines()) if APP_ERROR.search(row)), "")
            failure = f"logs show an application error before the kill: {line[:160]}"
    if not failure and attempts and not facts["ready"]:
        limit = _starting_limit(spec)
        long_run = any(
            (_time(item.get("finishedAt")) - _time(item.get("startedAt"))).total_seconds() > limit
            for item in attempts
            if _time(item.get("startedAt")) and _time(item.get("finishedAt"))
        )
        facts["killed_while_running" if long_run else "killed_while_starting"] = True
    facts.update(
        pending_unschedulable=status.get("phase") == "Pending"
        and (
            scheduled.get("reason") == "Unschedulable"
            or any(event.get("reason") == "FailedScheduling" for event in own)
        ),
        ever_running=status.get("phase") == "Running"
        or restarts > 0
        or bool((container_status.get("state") or {}).get("running"))
        or bool(attempts),
        restarts=restarts,
        waiting=waiting,
        failure=failure,
    )
    return facts


def hpas_at_max(hpas: list[dict]) -> list[str]:
    """HPAs at their maximum (currentReplicas >= maxReplicas); one pinned at min == max is not load."""
    out = []
    for hpa in hpas:
        spec, status = hpa.get("spec") or {}, hpa.get("status") or {}
        most, least = int(spec.get("maxReplicas") or 0), int(spec.get("minReplicas") or 1)
        current = int(status.get("currentReplicas") or 0)
        if most and least < most and current >= most:
            meta = hpa.get("metadata") or {}
            out.append(f"{meta.get('namespace')}/{meta.get('name')} {current}/{most}")
    return out


def node_cpu_shares(nodes: list[dict], pods: list[dict]) -> list[float]:
    """CPU requested / allocatable per node, from `kubectl get nodes` and every non-terminated pod."""
    allocatable = {
        node["metadata"]["name"]: _cpu(((node.get("status") or {}).get("allocatable") or {}).get("cpu"))
        for node in nodes
    }
    requested = dict.fromkeys(allocatable, 0.0)
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if node not in requested or (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for container in (pod.get("spec") or {}).get("containers") or []:
            requested[node] += _cpu(((container.get("resources") or {}).get("requests") or {}).get("cpu"))
    return sorted(requested[name] / cores for name, cores in allocatable.items() if cores > 0)


def recent_probe_kills(events: list[dict], now: datetime) -> list[dict]:
    """Probe kills (liveness or startup) of pods in the last KILL_LOOKBACK_SECONDS."""
    since = now - timedelta(seconds=KILL_LOOKBACK_SECONDS)
    return [event for event in events if _probe_kill(event) and (event_time(event) or since) >= since]


def classify(evidence: dict) -> dict:
    """The capacity verdict of the failing workloads in `evidence` (see gather for its shape).

    "capacity" only when all hold: 1. every new pod uses the release digest; 2. no new pod shows an
    application failure; 3. of the new pods that ever ran at least half are Ready, or every failing
    new pod is Pending unschedulable or was killed while starting; 4. a capacity indicator.
    """
    now = _time(evidence.get("now")) or datetime.now(UTC)
    release = evidence.get("release_digest")
    namespace = evidence.get("namespace")
    events = evidence.get("events") or []
    logs = evidence.get("logs") or {}
    blockers: list[str] = []
    indicators: list[str] = []
    workloads: dict[str, dict] = {}
    old_kills = 0
    for item in evidence.get("workloads") or []:
        deployment, name = item.get("deployment") or {}, item.get("name")
        if not deployment or item.get("unreadable"):
            blockers.append(f"{name}: its Deployment, ReplicaSets or pods could not be read")
            continue
        new, old = split_replica_sets(deployment, item.get("replica_sets") or [])
        template = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
        image = _digest(_container(template, item.get("container", "")).get("image"))
        if new is None:
            blockers.append(f"{name}: no ReplicaSet of the Deployment")
            continue
        new_image = _digest(
            _container(
                ((new.get("spec") or {}).get("template") or {}).get("spec") or {}, item.get("container", "")
            ).get("image")
        )
        pods = item.get("pods") or []
        new_pods = [pod for pod in pods if _owned_by(pod, "ReplicaSet", {new["metadata"]["name"]})]
        old_names = {rs["metadata"]["name"] for rs in old}
        old_pods = [pod for pod in pods if _owned_by(pod, "ReplicaSet", old_names)]
        every = [pod_facts(pod, item.get("container", ""), events, logs) for pod in new_pods]
        # Preempted, evicted or terminating pods are not the version's health; preemption is node pressure.
        facts = [fact for fact in every if not fact["displaced"]]
        if not every:
            # No pod at all (quota, admission, a ReplicaSet that cannot create pods): nothing shows a slow start.
            blockers.append(f"{name}: the new ReplicaSet {new['metadata']['name']} has no pods")
        removed = [fact for fact in every if fact["displaced"] and fact["displaced"] != "terminating"]
        # 1. image right
        if not release:
            blockers.append(f"{name}: the release digest is unknown")
        elif image != release or new_image != release:
            blockers.append(
                f"{name}: the new ReplicaSet runs {new_image or image or 'an image without a digest'}, release is {release}"
            )
        wrong = [fact["name"] for fact in every if fact["image_digest"] and fact["image_digest"] != release]
        if release and wrong:
            blockers.append(f"{name}: new pods run another image ({', '.join(wrong[:3])})")
        # 2. no application failure on the new version
        blockers += [f"{name}: {fact['name']} {fact['failure']}" for fact in facts if fact["failure"]][:5]
        # 3. healthy where it runs
        running = [fact for fact in facts if fact["ever_running"]]
        failing = [fact for fact in facts if not fact["ready"]]
        ready = sum(1 for fact in running if fact["ready"])
        explained = all(fact["pending_unschedulable"] or fact["killed_while_starting"] for fact in failing)
        healthy = (running and ready >= math.ceil(len(running) * READY_SHARE)) or (bool(failing) and explained)
        if not healthy and (running or failing):
            blockers.append(
                f"{name}: {ready}/{len(running)} new pods that ran are Ready, and not every failing new pod "
                "waits for a node or was killed by a probe while starting"
            )
        # 4. indicators of this workload
        pending = [fact["name"] for fact in facts if fact["pending_unschedulable"]]
        if pending:
            indicators.append(
                f"{name}: {len(pending)} new pods Pending with FailedScheduling ({', '.join(pending[:3])})"
            )
        if removed:
            indicators.append(f"{name}: {len(removed)} new pods preempted or evicted ({removed[0]['displaced']})")
        old_kills += sum(
            1 for pod in old_pods for event in recent_probe_kills(_pod_events(events, pod["metadata"]["name"]), now)
        )
        workloads[name] = {
            "new_replica_set": new["metadata"]["name"],
            "new_pods": len(facts),
            "ready": sum(1 for fact in facts if fact["ready"]),
            "pending_unschedulable": len(pending),
            "killed_while_starting": sum(1 for fact in facts if fact["killed_while_starting"]),
            "killed_while_running": sum(1 for fact in facts if fact["killed_while_running"]),
            "displaced": len(removed),
            "old_pods": len(old_pods),
        }
    if not workloads and not blockers:
        blockers.append("no failing workload to judge")
    if old_kills:
        indicators.append(f"{old_kills} probe kills on the old version in the last {KILL_LOOKBACK_SECONDS // 3600} h")
    at_max = hpas_at_max(evidence.get("hpas") or [])
    if at_max:
        indicators.append(f"HPAs at max: {', '.join(at_max[:6])}")
    shares = evidence.get("node_cpu_shares") or []
    if shares:
        median = statistics.median(shares)
        hot = sum(1 for share in shares if share >= NODE_CPU_HOT_SHARE)
        if median >= NODE_CPU_MEDIAN_SHARE or hot >= NODE_CPU_HOT_NODES:
            indicators.append(
                f"node CPU requested: median {median:.0%}, {hot} of {len(shares)} nodes at >= {NODE_CPU_HOT_SHARE:.0%}"
            )
    others = sorted(
        {
            (event.get("involvedObject") or {}).get("namespace") or (event.get("metadata") or {}).get("namespace")
            for event in recent_probe_kills(evidence.get("cluster_events") or [], now)
        }
        - {namespace, None}
    )
    if len(others) >= OTHER_NAMESPACES_WITH_KILLS:
        indicators.append(f"probe kills in {len(others)} other namespaces in the last 2 h ({', '.join(others[:5])})")
    unread = evidence.get("errors") or []
    capacity = not blockers and bool(indicators)
    return {
        "verdict": "capacity" if capacity else "not_capacity",
        "status": "environmental" if capacity else "failed",
        "reason": "capacity" if capacity else "",
        "blockers": blockers,
        "indicators": indicators,
        "workloads": workloads,
        "unread": unread,
        "summary": summary(capacity, blockers, indicators, workloads),
    }


def summary(capacity: bool, blockers: list[str], indicators: list[str], workloads: dict[str, dict]) -> str:
    """One line for logs, the record and Slack."""
    shown = "; ".join(
        f"{name} {item['ready']}/{item['new_pods']} new pods Ready"
        + (f", {item['pending_unschedulable']} waiting for a node" if item["pending_unschedulable"] else "")
        + (f", {item['killed_while_starting']} killed while starting" if item["killed_while_starting"] else "")
        for name, item in sorted(workloads.items())
    )
    if capacity:
        return f"capacity ({shown}): {'; '.join(indicators)[:400]}"
    if blockers:
        return f"not capacity: {'; '.join(blockers)[:400]}"
    return f"not capacity: no capacity indicator ({shown})"


# --------------------------------------------------------------------------- gather (read only)


def gather(clients, names: list[str], release_digest: str | None, now: datetime | None = None) -> dict:
    """Read the evidence of the failing workloads and the cluster (read only); unreadable parts are listed.

    clients is a release_record.Clients (kubectl_json, kubectl_base, runner, env).
    """
    env = clients.env
    evidence: dict = {
        "now": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "namespace": env.namespace,
        "release_digest": release_digest,
        "workloads": [],
        "logs": {},
        "errors": [],
    }

    def read(label: str, *args: str, namespace: bool = True) -> list[dict]:
        """The items of one kubectl get; [] and an error line when it fails."""
        try:
            data = clients.kubectl_json(*args, namespace=namespace)
        except Exception as error:  # noqa: BLE001 - every read failure only removes evidence
            evidence["errors"].append(f"{label}: {str(error)[:200]}")
            return []
        return data.get("items") if "items" in data else [data]

    by_name = {item.name: item for item in env.deployments}
    for name in names:
        item = by_name.get(name)
        if item is None:
            continue
        before = len(evidence["errors"])
        deployment = read(f"deployment/{name}", "get", "deployment", name)
        pods = read(f"pods {item.selector}", "get", "pods", "-l", item.selector)
        replica_sets = read(f"replicasets {item.selector}", "get", "replicasets", "-l", item.selector)
        evidence["workloads"].append(
            {
                "name": name,
                "container": item.container,
                "deployment": deployment[0] if deployment else {},
                "replica_sets": replica_sets,
                "pods": pods,
                # An empty pod list from a failed read is not "no pods": the classifier refuses to judge it.
                "unreadable": len(evidence["errors"]) > before,
            }
        )
    evidence["events"] = read("events", "get", "events")
    evidence["cluster_events"] = read(
        "events -A", "get", "events", "-A", "--field-selector", "reason=Killing", namespace=False
    )
    evidence["hpas"] = read("hpa -A", "get", "hpa", "-A", namespace=False)
    nodes = read("nodes", "get", "nodes", namespace=False)
    every = read(
        "pods -A",
        "get",
        "pods",
        "-A",
        "--field-selector",
        "status.phase!=Succeeded,status.phase!=Failed",
        namespace=False,
    )
    evidence["node_cpu_shares"] = node_cpu_shares(nodes, every) if nodes and every else []
    reads = 0
    for workload in evidence["workloads"]:
        new, _ = (
            split_replica_sets(workload["deployment"], workload["replica_sets"])
            if workload["deployment"]
            else (None, [])
        )
        for pod in workload["pods"]:
            status = _status(pod, workload["container"])
            current = (status.get("state") or {}).get("terminated")
            crashed = (int(status.get("restartCount") or 0) > 0 or bool(current)) and not displaced(pod)
            if not new or not crashed or not _owned_by(pod, "ReplicaSet", {new["metadata"]["name"]}):
                continue
            pod_name = pod["metadata"]["name"]
            if reads >= MAX_LOG_READS:
                # Past the sample: a crash whose logs nobody read is never excused (deep-search #2379).
                evidence["logs"][pod_name] = None
                continue
            reads += 1
            # The run that ended last: the current container when it has terminated, else the previous one.
            which = [] if current else ["--previous"]
            command = [
                *clients.kubectl_base(),
                "-n",
                env.namespace,
                "logs",
                pod_name,
                "-c",
                workload["container"],
                *which,
                f"--tail={LOG_TAIL_LINES}",
            ]
            try:
                evidence["logs"][pod_name] = clients.runner(command)
            except Exception as error:  # noqa: BLE001 - an unread log keeps the pod's crash unexplained
                evidence["logs"][pod_name] = None
                evidence["errors"].append(f"logs {pod_name}: {str(error)[:200]}")
    return evidence


# --------------------------------------------------------------------------- the wait after a capacity verdict

POLL_SECONDS = 30
READ_SECONDS = 30
# Injectable for tests (looked up at call time).
_sleep = time.sleep
_clock = time.monotonic


def rollout_complete(deployment: dict) -> bool:
    """The test `kubectl rollout status` makes: the newest generation observed, every replica updated and
    available, and no old replica left."""
    meta, spec, status = deployment.get("metadata") or {}, deployment.get("spec") or {}, deployment.get("status") or {}
    replicas = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
    updated = int(status.get("updatedReplicas") or 0)
    return (
        int(status.get("observedGeneration") or 0) >= int(meta.get("generation") or 0)
        and updated >= replicas
        and int(status.get("replicas") if status.get("replicas") is not None else updated) <= updated
        and int(status.get("availableReplicas") or 0) >= updated
    )


def poll_rollouts(clients, timeouts: dict[str, int]) -> list[str]:
    """Read each Deployment until its rollout completes or its own deadline passes; one line per Deployment
    that did not complete ("deployment/<name> rollout did not complete: ...").

    Not `kubectl rollout status`: once a Deployment passed its progressDeadlineSeconds, rollout status
    fails at once whatever its --timeout, so it cannot give a capacity-blocked rollout more time
    (deep-search #2379). Each read is bounded by the time the Deployment has left (at most 30 s).
    """
    sleep, clock = _sleep, _clock
    start = clock()
    pending = dict(timeouts)
    last: dict[str, str] = {}
    while pending:
        for name in list(pending):
            remaining = pending[name] - (clock() - start)
            if remaining <= 0:
                continue
            bound = max(1, int(min(READ_SECONDS, remaining)))
            command = [
                *clients.kubectl_base(),
                "-n",
                clients.env.namespace,
                "get",
                "deployment",
                name,
                "-o",
                "json",
                f"--request-timeout={bound}s",
            ]
            try:
                deployment = json.loads(clients.runner(command, timeout=bound + 5))
            except Exception as error:  # noqa: BLE001 - a failed read is tried again until the deadline
                last[name] = f"unreadable: {str(error)[:200]}"
                continue
            status = deployment.get("status") or {}
            last[name] = (
                f"{status.get('updatedReplicas', 0)}/{(deployment.get('spec') or {}).get('replicas', 0)} updated, "
                f"{status.get('availableReplicas', 0)} available"
            )
            if rollout_complete(deployment):
                print(f"rollout {name}: complete after the extended wait", flush=True)
                pending.pop(name)
                last.pop(name)
        elapsed = clock() - start
        for name in [name for name, seconds in pending.items() if elapsed >= seconds]:
            # A Deployment whose deadline passed before any read is a failure too (Greptile #500).
            last.setdefault(name, "not read before its deadline")
            print(f"rollout {name}: still incomplete after {pending[name]}s ({last[name]})", flush=True)
            pending.pop(name)
        if pending:
            sleep(min(POLL_SECONDS, max(1.0, min(pending.values()) - elapsed)))
    return [f"deployment/{name} rollout did not complete: {state}" for name, state in sorted(last.items())]


def judge(clients, names: list[str], release_digest: str | None, now: datetime | None = None) -> dict:
    """gather + classify; the verdict carries the evidence's read errors."""
    return classify(gather(clients, names, release_digest, now))


# --------------------------------------------------------------------------- command line


def _read_image(args: argparse.Namespace) -> str:
    """The release image from --image or --image-file."""
    if args.image:
        return args.image
    if args.image_file:
        return Path(args.image_file).read_text(encoding="utf-8").strip()
    return ""


def cmd_timeout(args, clients, rr) -> int:
    """Print the rollout timeout of each Deployment (Rule 3)."""
    for name in args.deployment or [item.name for item in clients.env.deployments]:
        deployment = clients.kubectl_json("get", "deployment", name)
        plan = rollout_plan(deployment)
        print(
            f"deployment/{name}: {rollout_timeout(deployment, args.base_seconds)} s "
            f"({plan['replicas']} replicas, {plan['waves']} waves of {plan['per_wave_pods']}, "
            f"{plan['per_wave_seconds']} s per wave)",
            flush=True,
        )
    return 0


def cmd_classify(args, clients, rr) -> int:
    """Print the capacity verdict of the Deployments (read only)."""
    names = args.deployment or [item.name for item in clients.env.deployments]
    verdict = judge(clients, names, _digest(_read_image(args)))
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0


def cmd_rollout_wait(args, clients, rr, *, popen=None, now: Callable[[], datetime] | None = None) -> int:
    """Wait for the rollouts (Rule 3 timeouts); on a timeout, capacity waits once more, else exit 1."""
    names = args.deployment or [item.name for item in clients.env.deployments]
    timeouts = rr.scaled_rollout_timeouts(clients, names, minimum=args.base_seconds)
    failures = rr.wait_rollouts(clients, names, timeouts=timeouts, popen=popen)
    if not failures:
        return 0
    failed = [name for name in names if any(line.startswith(f"deployment/{name} ") for line in failures)] or names
    release = _digest(_read_image(args))
    clock = now or (lambda: datetime.now(UTC))
    verdict = judge(clients, failed, release, clock())
    print(f"rollout capacity verdict: {verdict['summary']}", flush=True)
    if verdict["verdict"] == "capacity":
        print("Capacity, not the release: no deploy_failed. Waiting once more, then the later steps run.", flush=True)
        again = poll_rollouts(clients, {name: timeouts[name] for name in failed})
        if not again:
            print("rollouts finished after the extended wait", flush=True)
            return 0
        still = [name for name in failed if any(line.startswith(f"deployment/{name} ") for line in again)] or failed
        # Judged once more, like deep-search and orbit-api: what is no longer capacity fails the step.
        verdict = judge(clients, still, release, clock())
        print(f"rollout capacity verdict after the second wait: {verdict['summary']}", flush=True)
        if verdict["verdict"] == "capacity":
            print("still incomplete after the extended wait (capacity); release_verify judges it", flush=True)
            return 0
        failed, failures = still, again
    if args.failed_step_file:
        Path(args.failed_step_file).write_text(
            json.dumps({"id": args.step_id, "deployments": failed, "verdict": verdict["summary"][:500]}) + "\n",
            encoding="utf-8",
        )
    print(f"rollout failed at step {args.step_id}: {'; '.join(failures)[:600]}", file=sys.stderr, flush=True)
    return 1


def main(argv: list[str] | None = None, *, clients=None, popen=None, now=None) -> int:
    """Parse the command line and run it; tests pass clients, popen and now."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import release_config as rc  # noqa: PLC0415 - the pure functions above need no config
    import release_record as rr  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--service", help="service config name (release/services/<name>.json)")
    parser.add_argument("--env", default="prod", choices=["prod", "dev"])
    parser.add_argument("--kube-context", help="kubectl context (omit in Cloud Build, which sets KUBECONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("timeout", "print each Deployment's rollout timeout"),
        ("classify", "print the capacity verdict (read only)"),
        ("rollout-wait", "wait for the rollouts; capacity waits once more and exits 0"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--deployment", action="append", help="a Deployment of the service (default: all)")
        sub.add_argument("--base-seconds", type=int, default=ROLLOUT_BASE_SECONDS)
        if name != "timeout":
            sub.add_argument("--image", default="", help="the release image repo@sha256:...")
            sub.add_argument("--image-file", default="", help="file with the release image")
        if name == "rollout-wait":
            sub.add_argument("--step-id", required=True, help="the build step id, for the deploy-incomplete finding")
            sub.add_argument("--failed-step-file", default="", help="written when the step fails (not capacity)")
    args = parser.parse_args(argv)
    try:
        if clients is None:
            service = rc.load_service(args.service)
            if args.env not in service.environments:
                raise rc.ConfigError(f"{service.name} has no {args.env} environment")
            clients = rr.Clients(env=service.environments[args.env], kube_context=args.kube_context)
        if args.command == "timeout":
            return cmd_timeout(args, clients, rr)
        if args.command == "classify":
            return cmd_classify(args, clients, rr)
        return cmd_rollout_wait(args, clients, rr, popen=popen, now=now)
    except (rr.RecordError, rc.ConfigError, OSError, ValueError, KeyError) as error:
        print(f"release capacity: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
