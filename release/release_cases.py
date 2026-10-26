#!/usr/bin/env python3
"""Release cases: past release incidents, replayed offline against the kit's decision code.

A case freezes the inputs of one release-check decision that a past release
needed (the record, the workload observations, the pods and HTTP answers of the
smoke checks, the rollouts, the metric, signal, log and GitHub answers, the
other release records, the prod builds that ran) together with the decision it
should make ("expected") and the decision it made at the time ("at_the_time").
`replay` runs the kit's real decision code on those inputs with fakes in place
of kubectl, HTTP, Datadog, Cloud Logging, GitHub, the record store and the
clock, and fails when a decision differs from "expected".

Two folders are replayed:

  release/kit_cases/   The shared cases of every kit service. They ship with the kit
                       (KIT.json pins them), so every repository replays them.
  release/cases/       The repository's own cases. The repository owns them; sync
                       never touches them.

Case kinds:

  checkpoint  One check of a release: release_verify.evaluate() for phase "deploy"
              (smoke checks after the deploy) or "window" (a watch-window checkpoint),
              for a GKE service of the kit. Decision: the verdict, whether it rolls
              back, the status of each check, the regressions and warnings.
  rule        A lesson that lives in a rule file of the repository: the file must
              still hold a line that matches the case's pattern.

A case may carry "known_open": the reason today's code still gets it wrong.
Replay reports it and does not fail. Once the code gets it right, replay fails
until the PR that fixes the code drops "known_open".

Inputs of a checkpoint case ("inputs"; every key is optional):

  workloads        [{"at": time, "workloads": {...}}]: workload snapshots as
                   release_record.snapshot_workloads returns them. A read at time t
                   gets the newest snapshot at or before t (the first one before
                   them all). Default: the record's own workloads at "now". Replay
                   never invents a later snapshot: with one snapshot, every fresh
                   read sees that snapshot again.
  pods             {deployment: [{"name": pod, "ready": true}]}: the live pods that
                   the http_pod smoke checks call.
  http             {"pod <pod>:<port><path>" or URL: [{"after_seconds": s, "status": n,
                   "body": text} or {"after_seconds": s, "error": text}]}: the answer
                   at s seconds after the check started (the newest at or before; none
                   before the first answer: that read is missing).
  rollout_failures The lines release_record.wait_rollouts returned (deploy phase).
  prod_changes     [{"after_seconds": s, "ended": text, "busy": text}]: what the
                   observation of the window check learns about prod builds.
  store            {object name: data}: the release records and other store objects
                   that the window check reads (new errors, the release chain).
                   Without it, the window check runs no new-error checks.
  answers          {read key: {"value": v} or {"error": text, "type": name}}: the
                   Datadog, Cloud Logging and GitHub reads. A read that the case does
                   not hold fails the case with "missing read: <key>"; add the key with
                   the answer that the source gave at the time.

Commands:

  python3 release/release_cases.py replay [--case <id>] [-v]      # offline: no keys, no network
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import functools
import inspect
import io
import json
import re
import sys
import tempfile
import urllib.error
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT_DIR))

import release_config as rc  # noqa: E402
import release_record as rr  # noqa: E402
import release_signals as rs  # noqa: E402
import release_verify as rv  # noqa: E402
from promote_release_guard import GuardError  # noqa: E402

REPO_ROOT = KIT_DIR.parent
KIT_CASES_DIR = KIT_DIR / "kit_cases"
REPO_CASES_DIR = KIT_DIR / "cases"
SCHEMA_VERSION = 1
KINDS = ("checkpoint", "rule")
VERDICTS = ("passed", "failed", "inconclusive", "warning")
# How long a deploy check waits for the rollouts (the rendered builds pass this).
ROLLOUT_WAIT_SECONDS = 900


class MissingRead(BaseException):  # noqa: N818 - a replay stop, like KeyboardInterrupt
    """The code made a read that the case does not hold.

    A BaseException, so the decision code's own handlers (OSError, RuntimeError, GuardError, even
    Exception) cannot turn a missing input into a verdict: it always fails the case.
    """


class CaseError(RuntimeError):
    """A case that cannot run (an unknown kind, a service kind without a replay)."""


def _iso(value: datetime) -> str:
    """A time as the read keys write it."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def read_key(kind: str, *parts: object) -> str:
    """The key of one outside read in a case's "answers"."""
    return kind + " " + json.dumps(parts, sort_keys=True, default=str)


def _error_classes() -> dict[str, type[BaseException]]:
    """Read failures that the checks handle; a case can hold them so a replay fails the same way."""
    classes: dict[str, type[BaseException]] = {
        "TimeoutError": TimeoutError,
        "RecordError": rr.RecordError,
        "GuardError": GuardError,
    }
    if hasattr(rs, "NoAccess"):
        classes["NoAccess"] = rs.NoAccess
    return classes


class Answers:
    """The case's recorded answers to outside reads: key -> {"value": ...} or {"error": "...", "type": "..."}."""

    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        """Keep the entries; `used` collects the keys the replay read."""
        self.entries = entries or {}
        self.used: set[str] = set()

    def get(self, key: str) -> object:
        """The recorded answer to `key`; a recorded error is raised again; a missing key raises MissingRead."""
        self.used.add(key)
        if key not in self.entries:
            raise MissingRead(f"missing read: {key[:400]}")
        entry = self.entries[key]
        if "error" in entry:
            raise _error_classes().get(str(entry.get("type")), GuardError)(entry["error"])
        return entry["value"]


class CaseDatadog:
    """release_verify.DatadogClient answered from the case."""

    def __init__(self, answers: Answers) -> None:
        """Keep the answers."""
        self.answers = answers

    def scalar(self, query: str, aggregator: str, start: datetime, end: datetime) -> dict[str, float]:
        """A metric query over a time span."""
        return self.answers.get(read_key("datadog.scalar", query, aggregator, _iso(start), _iso(end)))  # type: ignore[return-value]

    def api(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> dict:
        """A Datadog API call (monitor events, Error Tracking)."""
        return self.answers.get(read_key("datadog.api", method, path, body, params))  # type: ignore[return-value]


class CaseLogging:
    """release_signals.CloudLogging answered from the case."""

    def __init__(self, answers: Answers) -> None:
        """Keep the answers."""
        self.answers = answers

    def lines(
        self, extra: str, spans: list, *, limit: int, newest_first: bool = False, seconds: float | None = None
    ) -> Iterator[tuple]:
        """The recorded log lines of one query."""
        key = read_key("logging.lines", extra, [[_iso(a), _iso(b)] for a, b in spans], limit, newest_first)
        for line in self.answers.get(key):  # type: ignore[union-attr]
            yield tuple(line)


class CaseStore:
    """A read-only release-record store holding the case's objects; a missing object does not exist."""

    def __init__(self, objects: dict[str, dict]) -> None:
        """Keep the objects; `reads` collects the names the replay read."""
        self.objects = objects
        self.reads: list[str] = []

    def location(self, name: str) -> str:
        """Where an object lives."""
        return f"case://{name}"

    def read(self, name: str) -> tuple[dict | None, int]:
        """(data, generation 1), or (None, 0) when the case has no such object."""
        self.reads.append(name)
        if name not in self.objects:
            return None, 0
        return copy.deepcopy(self.objects[name]), 1

    def write(self, name: str, data: dict, if_generation: int) -> None:
        """A replayed check never writes."""
        raise MissingRead(f"a replayed check wrote to the store: {name}")


class FakeClock:
    """The time of a replay: it starts at the case's "now" and moves only when the code sleeps."""

    def __init__(self, start: datetime) -> None:
        """Start at `start` with no time elapsed."""
        self.start = start
        self.elapsed = 0.0

    def monotonic(self) -> float:
        """Seconds since the replay began (time.monotonic for the code)."""
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        """Let `seconds` pass."""
        self.elapsed += max(0.0, float(seconds))

    def now(self) -> datetime:
        """The wall-clock time of the replay."""
        return self.start + timedelta(seconds=self.elapsed)


def _accepts(function: Callable, name: str) -> bool:
    """Whether `function` takes a parameter `name` (older kit code takes fewer)."""
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _with(function: Callable, **values: object) -> Callable:
    """`function` with the keyword arguments that it takes; the others are left out."""
    accepted = {name: value for name, value in values.items() if _accepts(function, name)}
    return functools.partial(function, **accepted) if accepted else function


def _timeline(entries: list[dict], elapsed: float) -> dict | None:
    """The newest entry with after_seconds at or before `elapsed`; None before the first."""
    current = None
    for entry in sorted(entries, key=lambda item: float(item.get("after_seconds", 0))):
        if float(entry.get("after_seconds", 0)) <= elapsed:
            current = entry
    return current


def _http_answer(answers: dict[str, list[dict]], key: str, clock: FakeClock) -> tuple[int, bytes]:
    """The recorded HTTP answer to `key` at the replay's time; a recorded error raises URLError."""
    if key not in answers:
        raise MissingRead(f"missing read: http {key}")
    entry = _timeline(answers[key], clock.elapsed)
    if entry is None:
        # The first recorded answer comes later: nothing was observed this early, so replay invents nothing.
        raise MissingRead(f"missing read: http {key} at +{clock.elapsed:.0f} s (the first answer is later)")
    if "error" in entry:
        raise urllib.error.URLError(str(entry["error"]))
    return int(entry["status"]), str(entry.get("body", "")).encode("utf-8")


def _pod(name: str, ready: bool) -> dict:
    """A running pod as kubectl returns it, with only the fields the checks read."""
    status = "True" if ready else "False"
    return {
        "metadata": {"name": name},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": status}]},
    }


@contextlib.contextmanager
def patched(targets: dict[tuple[object, str], object]) -> Iterator[None]:
    """Set module attributes for the replay and restore them after it."""
    real = {key: getattr(*key) for key in targets}
    for (module, name), value in targets.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for (module, name), value in real.items():
            setattr(module, name, value)


def checkpoint_decision(case: dict, *, source_root: Path | None = None) -> dict:
    """Run the kit's check on the case's inputs; return its decision."""
    data = case["service"]
    if data.get("kind", "gke") != "gke":
        raise CaseError(f"service kind {data['kind']!r} has no checkpoint replay yet; add it with its first case")
    service = rc.parse_service(data)
    decision = case["decision"]
    phase = decision["phase"]
    record = copy.deepcopy(case["record"])
    env = service.environments[record.get("environment", "prod")]
    inputs = case.get("inputs") or {}
    clock = FakeClock(rr.parse_utc(decision["now"]))
    answers = Answers(inputs.get("answers"))
    snapshots = sorted(
        inputs.get("workloads") or [{"at": decision["now"], "workloads": record.get("workloads") or {}}],
        key=lambda item: rr.parse_utc(item["at"]),
    )
    pods = inputs.get("pods") or {}
    http = inputs.get("http") or {}
    observed: list[str] = []

    def snapshot_workloads(*args: object, **kwargs: object) -> dict[str, dict]:
        now = clock.now()
        current = snapshots[0]
        for item in snapshots:
            if rr.parse_utc(item["at"]) <= now:
                current = item
        observed.append(f"{now:%H:%M:%S} {current['at']}")
        return copy.deepcopy(current["workloads"])

    def live_pods(clients: rr.Clients, deployment: rc.Deployment) -> list[dict]:
        if deployment.name not in pods:
            raise MissingRead(f"missing read: pods of {deployment.name}")
        return [_pod(item["name"], bool(item.get("ready", True))) for item in pods[deployment.name]]

    def pod_get(namespace: str, pod: str, port: int, path: str) -> tuple[int, bytes]:
        return _http_answer(http, f"pod {pod}:{port}{path}", clock)

    def public_get(request: object, *args: object, **kwargs: object) -> tuple[int, bytes]:
        return _http_answer(http, str(request.full_url), clock)  # type: ignore[attr-defined]

    def observation_state() -> tuple[str | None, str | None]:
        entry = _timeline(inputs.get("prod_changes") or [], clock.elapsed) or {}
        return entry.get("ended"), entry.get("busy")

    def github_json(path: str, token: str) -> object:
        return answers.get(read_key("github.json", path))

    def github_text(path: str, token: str) -> object:
        return answers.get(read_key("github.text", path))

    def refuse(command: list[str], timeout: float = 120) -> str:
        raise MissingRead(f"a replayed check ran a command: {' '.join(command[:6])}")

    targets: dict[tuple[object, str], object] = {
        (rr, "snapshot_workloads"): snapshot_workloads,
        (rr, "live_pods"): live_pods,
        (rr, "wait_rollouts"): lambda *args, **kwargs: list(inputs.get("rollout_failures") or []),
        # Rule 3 reads each Deployment's strategy for its timeout; a replay waits for nothing, so the minimum.
        (rr, "scaled_rollout_timeouts"): lambda clients, names, *, minimum=0, **kwargs: {
            n: int(minimum) for n in names
        },
        (rr, "github_json"): github_json,
        (rs, "github_text"): github_text,
        (rv, "check_http_pod"): _with(rv.check_http_pod, sleep=clock.sleep),
        (rv, "check_http_public"): _with(rv.check_http_public, sleep=clock.sleep, clock=clock.monotonic),
    }
    observation = getattr(rv, "ro", None)
    if observation is not None and hasattr(observation, "confirm_images"):
        targets[(observation, "confirm_images")] = _with(
            observation.confirm_images, sleep=clock.sleep, clock=clock.monotonic
        )
    store = CaseStore(inputs["store"]) if "store" in inputs else None
    arguments = {
        "phase": phase,
        "clients": rr.Clients(env=env, runner=refuse),
        "dd": CaseDatadog(answers),
        "now": clock.now(),
        "wait_rollouts_seconds": ROLLOUT_WAIT_SECONDS if phase == "deploy" else 0,
        "store": store,
        "token": "case",
        "logs": CaseLogging(answers) if store is not None else None,
        "pod_get": pod_get,
        "get": public_get,
        "observation_state": observation_state,
    }
    with contextlib.ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patched(targets))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        # A shared case is about another repository: its release's source is not here.
        arguments["source_root"] = source_root or scratch
        evaluate = rv.evaluate
        result = evaluate(service, record, **{k: v for k, v in arguments.items() if _accepts(evaluate, k)})
    regressions = sorted(item["text"] for item in result["regressions"])
    return {
        "verdict": result["result"],
        "rolls_back": result["result"] == "failed" and env.name == "prod" and service.auto_rollback,
        "checks": {check["name"]: check["status"] for check in result["checks"]},
        "regressions": regressions,
        "warnings": sorted(item["text"] for item in result.get("warnings") or []),
        "availability": (result.get("availability") or {}).get("status"),
        "observations": observed,
        "seconds": round(clock.elapsed, 1),
        "skipped": [f"{c['name']}: {'; '.join(c['details'])}" for c in result["checks"] if c["status"] == "skipped"],
    }


def rule_decision(case: dict) -> dict:
    """passed when the rule file of the repository still holds a line that matches the case's pattern."""
    inputs = case["inputs"]
    path = REPO_ROOT / inputs["path"]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    found = re.search(inputs["pattern"], text, re.IGNORECASE | re.MULTILINE)
    return {
        "verdict": "passed" if found else "failed",
        "regressions": [] if found else [f"{inputs['path']} has no line that matches {inputs['pattern']!r}"],
    }


def decide(case: dict, *, source_root: Path | None = None) -> dict:
    """The decision of today's code on one case."""
    if case["kind"] == "checkpoint":
        return checkpoint_decision(case, source_root=source_root)
    if case["kind"] == "rule":
        return rule_decision(case)
    raise CaseError(f"unknown case kind {case['kind']!r}")


def mismatches(expected: dict, decision: dict) -> list[str]:
    """How the decision differs from what the case expects (empty when it matches)."""
    out = []
    if decision["verdict"] != expected["verdict"]:
        out.append(f"verdict {decision['verdict']}, expected {expected['verdict']}")
    if "rolls_back" in expected and decision.get("rolls_back") != expected["rolls_back"]:
        out.append(f"rolls back: {decision.get('rolls_back')}, expected {expected['rolls_back']}")
    for name, status in (expected.get("checks") or {}).items():
        got = (decision.get("checks") or {}).get(name)
        if got != status:
            out.append(f"check {name!r} {got}, expected {status}")
    if decision.get("skipped") and not expected.get("allow_skipped"):
        out.append(f"a check was skipped, so the case judged less than it should: {decision['skipped'][0][:200]}")
    texts = decision.get("regressions", [])
    for needle in expected.get("regressions_include") or []:
        if not any(needle in text for text in texts):
            out.append(f"no regression mentions {needle!r}")
    for needle in expected.get("regressions_exclude") or []:
        hits = [text for text in texts if needle in text]
        if hits:
            out.append(f"a regression mentions {needle!r}: {hits[0][:200]}")
    for needle in expected.get("warnings_include") or []:
        if not any(needle in text for text in decision.get("warnings", [])):
            out.append(f"no warning mentions {needle!r}")
    return out


# --------------------------------------------------------------------------- case files


def case_problems(case: dict, path: Path) -> list[str]:
    """What is wrong with a case file's shape (empty when it can be replayed)."""
    problems = []
    if case.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version {case.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    if case.get("id") != path.stem:
        problems.append(f"id {case.get('id')!r} differs from the file name {path.stem!r}")
    if case.get("kind") not in KINDS:
        problems.append(f"kind {case.get('kind')!r} is not one of {KINDS}")
    for key in ("title", "expected", "at_the_time"):
        if not case.get(key):
            problems.append(f"no {key}")
    if (case.get("expected") or {}).get("verdict") not in VERDICTS:
        problems.append(f"expected verdict must be one of {VERDICTS}")
    if case.get("kind") == "checkpoint":
        for key in ("service", "record", "decision"):
            if not case.get(key):
                problems.append(f"a checkpoint case needs {key}")
        if (case.get("decision") or {}).get("phase") not in ("deploy", "window"):
            problems.append("decision.phase must be deploy or window")
    return problems


def case_paths(folders: list[Path], case_id: str | None = None) -> list[Path]:
    """The case files of the folders, sorted, optionally only the one with this id."""
    paths = [path for folder in folders if folder.is_dir() for path in sorted(folder.glob("*.json"))]
    return [path for path in paths if case_id is None or path.stem == case_id]


def outcome(case: dict, path: Path | None = None) -> tuple[str, list[str], dict | None]:
    """(status, problems, decision): status ok, FAIL, open (a known_open case still wrong) or FIXED."""
    if path is not None and (problems := case_problems(case, path)):
        return "FAIL", problems, None
    own = path is not None and path.parent.resolve() == REPO_CASES_DIR.resolve()
    try:
        decision = decide(case, source_root=REPO_ROOT if own else None)
        problems = mismatches(case["expected"], decision)
    except (MissingRead, CaseError, rc.ConfigError) as error:
        return "FAIL", [str(error)], None
    except Exception as error:  # noqa: BLE001 - one broken case must not hide the result of the others
        return "FAIL", [f"the replay raised {type(error).__name__}: {error}"], None
    if case.get("known_open"):
        if problems:
            return "open", [f"known open: {case['known_open']}", *problems], decision
        return "FIXED", ["today's code gets this case right: drop known_open in the PR that fixed it"], decision
    return ("ok" if not problems else "FAIL"), problems, decision


def replay(case_id: str | None = None, verbose: bool = False, folders: list[Path] | None = None) -> int:
    """Replay every case; print a score; 1 when any case fails (or a known_open case is fixed)."""
    folders = folders or [KIT_CASES_DIR, REPO_CASES_DIR]
    paths = case_paths(folders, case_id)
    if not paths:
        print(f"no cases in {', '.join(str(folder) for folder in folders)}" + (f" named {case_id}" if case_id else ""))
        return 1 if case_id else 0
    failed = opened = 0
    for path in paths:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            status, problems, decision, case = "FAIL", [f"cannot read: {error}"], None, {"id": path.stem, "title": ""}
        else:
            status, problems, decision = outcome(case, path)
        failed += status in ("FAIL", "FIXED")
        opened += status == "open"
        print(f"{status:5} {path.parent.name}/{case.get('id')}: {case.get('title', '')}")
        for problem in problems:
            print(f"      - {problem}")
        if verbose and decision:
            print("      " + json.dumps(decision, indent=1).replace("\n", "\n      "))
    print(f"score {len(paths) - failed - opened}/{len(paths)}" + (f" ({opened} known open)" if opened else ""))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and replay the cases."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    play = sub.add_parser("replay", help="replay the shared and the repository's cases offline")
    play.add_argument("--case", help="only this case id")
    play.add_argument("-v", "--verbose", action="store_true", help="print each decision")
    play.add_argument("--cases-dir", type=Path, action="append", help="replay this folder instead (repeatable)")
    args = parser.parse_args(argv)
    return replay(args.case, args.verbose, args.cases_dir)


if __name__ == "__main__":
    sys.exit(main())
