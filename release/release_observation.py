"""Fresh workload observations and durable post-deploy checkpoint receipts (stdlib only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import time


def utc_now() -> str:
    """Return a portable UTC receipt timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def availability_only(check: dict) -> bool:
    """Replica counts alone cannot establish that reverting the release will help."""
    details = check.get("details", [])[1:]
    return (
        check.get("status") == "failed"
        and bool(details)
        and all(
            re.search(r": (\d+/\d+ replicas available|rollout not complete \(\d+/\d+ updated\))$", line)
            for line in details
        )
    )


MAX_SAMPLES = 13


def confirm_images(read, *, state=lambda: (None, None), budget=180, poll=15, sleep=time.sleep, clock=time.monotonic):
    """Read AFTER slow metrics; retry count-only shortfalls within a shared CLI deadline.

    read(deadline) returns (serialized Check, workload snapshot) and must enforce
    the deadline on each external command. An unreadable/uncertain observation
    never becomes a successful check or authorizes an image rollback.
    """
    deadline = clock() + budget
    samples = []
    while True:
        ended, busy = state()
        if ended or busy:
            return {"name": "images", "status": "inconclusive", "details": ["prod changed during observation"]}, {
                "status": "superseded",
                "changed": ended or busy,
                "samples": samples,
            }
        try:
            check, workloads = read(deadline)
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            check = {"name": "images", "status": "inconclusive", "details": [f"fresh workload read failed: {error}"]}
            workloads = {}
        samples.append({"at": utc_now(), "check": check, "workloads": workloads})
        if check["status"] == "passed" or (check["status"] == "failed" and not availability_only(check)):
            return check, {"status": "confirmed", "samples": samples}
        remaining = deadline - clock()
        if check["status"] == "inconclusive" or remaining <= 0 or len(samples) >= MAX_SAMPLES:
            details = check.get("details") or ["workload availability could not be confirmed"]
            return {"name": "images", "status": "inconclusive", "details": details}, {
                "status": "unconfirmed",
                "reason": "availability is uncertain; image rollback has no causal evidence",
                "samples": samples,
            }
        sleep(min(poll, remaining))


def checkpoint_plan(record: dict, minutes: list[int]) -> dict:
    """Stable per-release checkpoint identities, due times and ownership state."""
    finished = record["times"].get("deploy_finished") or record["times"]["deploy_started"]
    deployed = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    return {
        str(value): {
            "id": f"{record['release_id']}:window_{value}m",
            "state": "scheduled",
            "due_at": (deployed + timedelta(minutes=value)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for value in minutes
    }


def note_checkpoint(record: dict, result: dict, key: str) -> None:
    """Update a receipt without replacing the deploying agent's ownership acknowledgement."""
    receipt = record.setdefault("followup", {}).setdefault("checkpoints", {}).setdefault(key, {})
    receipt.update(
        id=f"{record['release_id']}:{key}",
        state="completed",
        completed_at=result["at"],
        result=result["result"],
        observation=(result.get("availability") or {}).get("status"),
    )


def begin_checkpoint(record: dict, key: str) -> None:
    """Record an attempt before querying providers, so a killed checker remains detectable."""
    receipt = record.setdefault("followup", {}).setdefault("checkpoints", {}).setdefault(key, {})
    if receipt.get("attempts", 0) or receipt.get("state") in ("completed", "error"):
        previous = {name: value for name, value in receipt.items() if name != "attempt_history"}
        if (record.get("checks") or {}).get(key):
            previous["check"] = record["checks"][key]
        receipt.setdefault("attempt_history", []).append(previous)
    for terminal in ("last_error", "completed_at", "result", "observation", "at", "superseded_by"):
        receipt.pop(terminal, None)
    receipt.update(
        id=f"{record['release_id']}:{key}",
        state="running",
        heartbeat_at=utc_now(),
        attempts=receipt.get("attempts", 0) + 1,
    )


def merge_plan(record: dict, minutes: list[int]) -> None:
    """A resumed watch keeps completed receipts and attempt history."""
    followup = record.setdefault("followup", {})
    followup.setdefault("owner", {"state": "unclaimed"})
    checkpoints = followup.setdefault("checkpoints", {})
    for minute, planned in checkpoint_plan(record, minutes).items():
        checkpoints.setdefault(f"window_{minute}m", planned)


def claim_owner(record: dict, thread_url: str, pr_url: str, *, takeover: bool = False, reason: str = "") -> None:
    """First claim is atomic; a different owner requires an explicit, audited transfer."""
    thread_url, pr_url = thread_url.strip(), pr_url.strip()
    if not thread_url or not pr_url:
        raise ValueError("ownership requires both the agent chat and the PR URL")
    followup = record.setdefault("followup", {})
    existing = followup.get("owner") or {}
    same = existing.get("thread_url") == thread_url and existing.get("pr_url") == pr_url
    if existing.get("thread_url") and not same:
        if not takeover or not reason.strip():
            raise ValueError("release already has an owner; use --takeover --reason to record a transfer")
        followup.setdefault("owner_history", []).append(
            {"at": utc_now(), "previous": existing, "thread_url": thread_url, "pr_url": pr_url, "reason": reason}
        )
    followup["state"] = "owned"
    if not same:
        followup["owner"] = {
            "state": "claimed",
            "thread_url": thread_url,
            "pr_url": pr_url,
            "acknowledged_at": utc_now(),
            "checkpoints_minutes": [15, 60],
        }


def supersede(record: dict, reason: str) -> None:
    """A newer deploy closes every remaining obligation explicitly, preserving completed evidence."""
    for receipt in record.setdefault("followup", {}).setdefault("checkpoints", {}).values():
        if receipt.get("state") not in ("completed", "superseded"):
            receipt.update(state="superseded", at=utc_now(), superseded_by=reason)


def owned(record: dict) -> bool:
    """An actual agent chat must acknowledge ownership; an unclaimed placeholder is insufficient."""
    owner = record.get("followup", {}).get("owner") or {}
    return bool(str(owner.get("thread_url") or "").strip() and str(owner.get("pr_url") or "").strip())


def author_owned(record: dict) -> bool:
    """No agent claimed the release, and its PR author was recorded as the owner (release_verify.require_owner)."""
    return ((record.get("followup") or {}).get("owner") or {}).get("state") == "author"


def release_author(record: dict) -> tuple[str | None, int | None]:
    """The GitHub login and PR number of the change the release deploys: its commit's PR, else the newest PR.

    A commit without a PR (a direct or automated push) never wins over a PR; its author is used only when the
    release holds no PR at all."""
    commits = [item for item in record.get("commits") or [] if isinstance(item, dict) and item.get("author")]
    with_pr = [item for item in commits if item.get("pr")]
    head = [item for item in with_pr if item.get("sha") == record.get("commit")]
    newest = list(reversed(with_pr))
    direct = [item for item in commits if item.get("sha") == record.get("commit")] + list(reversed(commits))
    chosen = (head or newest or direct or [{}])[0]
    return chosen.get("author"), chosen.get("pr")


def author_owner(record: dict) -> dict:
    """The owner of a release that no agent claimed: the author of its PR."""
    login, pr = release_author(record)
    return {"state": "author", "login": login, "pr": pr, "assigned_at": utc_now()}


def scheduled_minute(record: dict, elapsed: float, requested: int | None = None) -> int | None:
    """The scheduled checkpoint a manual check completes: the latest one that is due, else None.

    A late manual repair binds to the original scheduled checkpoint. A check before a checkpoint is due
    (an early check, or --checkpoint-minutes ahead of its due time) completes none: it returns None, and
    the caller records it apart with note_early_check, so the watch still runs the real checkpoint.
    """
    if requested is not None:
        return requested if requested <= elapsed else None
    planned = (record.get("checks", {}).get("window_build") or {}).get("checkpoints_minutes") or [15, 60]
    planned = restore_extensions(record, planned)
    return max((minute for minute in planned if minute <= elapsed), default=None)


EARLY_CHECKS_KEPT = 20


def note_early_check(record: dict, result: dict, key: str) -> None:
    """Keep an early manual observation apart from the scheduled checkpoint receipts and checks."""
    early = record.setdefault("followup", {}).setdefault("early_checks", [])
    early.append({"key": key, "at": result["at"], "result": result["result"], "check": result})
    del early[:-EARLY_CHECKS_KEPT]


def phase_result(record: dict, phase: str, at: str | None = None) -> dict | None:
    """The saved check result of a verdict phase: record["checks"][phase], or an early check of that key.

    Early keys round to whole minutes, so two early checks can share one. With at (the verdict's time),
    the early check of that exact time is the one; without a match, the first failed one of the key (a
    failed verdict stays, so it is the one that set it), else the last one."""
    saved = (record.get("checks") or {}).get(phase)
    if saved is not None:
        return saved
    early = [item for item in (record.get("followup") or {}).get("early_checks") or [] if item.get("key") == phase]
    exact = [item for item in early if at and item.get("at") == at]
    failed = [item for item in early if item.get("result") == "failed"]
    chosen = (exact or failed[:1] or early[-1:] or [None])[0]
    return chosen.get("check") if chosen else None


def later_terminal(verdict: dict, checkpoint: str) -> bool:
    """A late repair of an earlier checkpoint cannot erase a later terminal verdict.

    Only a failure replaces it (the callers set a failed verdict before they ask), also with --final.
    """
    previous = re.fullmatch(r"window_(\d+)m", verdict.get("phase") or "")
    current = re.fullmatch(r"window_(\d+)m", checkpoint)
    return bool(
        previous
        and current
        and int(previous[1]) > int(current[1])
        and verdict.get("status") in ("passed", "warning", "inconclusive")
    )


def extend_plan(record: dict, minutes: int) -> None:
    """Persist an adaptive checkpoint before the previous result can become completed."""
    extensions = record.setdefault("followup", {}).setdefault("extensions_minutes", [])
    if minutes not in extensions:
        extensions.append(minutes)
        extensions.sort()
    merge_plan(record, [minutes])


def restore_extensions(record: dict, requested: list[int]) -> list[int]:
    """A restarted watch restores adaptive checkpoints as well as the configured window."""
    return sorted(set(requested) | set(record.get("followup", {}).get("extensions_minutes") or []))


def needs_final_decision(record: dict, receipt: dict, last: bool) -> bool:
    """A completed manual attempt without a terminal decision cannot be skipped as a finished watch."""
    return bool(
        last
        and receipt.get("state") == "completed"
        and (record.get("verdict") or {}).get("status") not in ("passed", "warning", "failed", "inconclusive")
    )
