"""Regression tests for preemption/HPA snapshots, independent failures and follow-up receipts."""

from pathlib import Path
import sys
import base64
import json
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE if (HERE / "release_observation.py").exists() else HERE.parents[1] / "bin"))
import release_observation as ro  # noqa: E402
import promote_release_guard as guard  # noqa: E402


def check(detail=None):
    """A serialized images check: failed with the detail, else passed."""
    return {
        "name": "images",
        "status": "failed" if detail else "passed",
        "details": ["1 workloads checked"] + ([detail] if detail else []),
    }


class ObservationTest(unittest.TestCase):
    def test_hpa_recovery_uses_a_fresh_read(self):
        """HPA recovery uses a fresh read."""
        elapsed = [0]
        pending = iter([check("deployment/backfill: 11/15 replicas available"), check()])

        def read(deadline):
            """Return the next fresh workload sample."""
            self.assertEqual(deadline, 180)
            return next(pending), {"deployment/backfill": {"available_replicas": 11 if elapsed[0] == 0 else 15}}

        with mock.patch.object(ro, "MAX_SAMPLES", 13):
            result, evidence = ro.confirm_images(
                read, clock=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
            )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            [s["workloads"]["deployment/backfill"]["available_replicas"] for s in evidence["samples"]], [11, 15]
        )
        self.assertEqual(elapsed[0], 15)

    def test_persistent_zero_available_never_authorizes_rollback_or_pass(self):
        """Persistent zero available never authorizes rollback or pass."""
        elapsed = [0]
        with mock.patch.object(ro, "MAX_SAMPLES", 13):
            result, evidence = ro.confirm_images(
                lambda _: (check("deployment/smp: 0/2 replicas available"), {}),
                clock=lambda: elapsed[0],
                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
            )
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(evidence["status"], "unconfirmed")
        self.assertEqual(elapsed[0], 180)
        self.assertEqual(len(evidence["samples"]), 13)

    def test_wrong_image_or_crashing_container_remains_a_failure(self):
        """Wrong image or crashing container remains a failure."""
        for detail in ["deployment/app: set to old, release is new", "deployment/app: pod-1: CrashLoopBackOff"]:
            with self.subTest(detail=detail):
                result, evidence = ro.confirm_images(
                    lambda _, problem=detail: (check(problem), {}), sleep=lambda _: self.fail("strong failure slept")
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(evidence["status"], "confirmed")

    def test_unreadable_snapshot_is_inconclusive(self):
        """Unreadable snapshot is inconclusive."""

        def read(_):
            """Return the next fresh workload sample."""
            raise RuntimeError("API unavailable")

        result, evidence = ro.confirm_images(read)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(evidence["status"], "unconfirmed")

    def test_new_deploy_drops_the_observation(self):
        """New deploy drops the observation."""
        states = iter([(None, None), (None, "prod build changed workloads")])
        with mock.patch.object(ro, "MAX_SAMPLES", 13):
            result, evidence = ro.confirm_images(
                lambda _: (check("deployment/app: 0/2 replicas available"), {}),
                state=lambda: next(states),
                sleep=lambda _: None,
            )
        self.assertEqual(evidence["status"], "superseded")
        self.assertIn("prod build", evidence["changed"])
        self.assertEqual(len(evidence["samples"]), 1)

    def test_checkpoint_due_times_are_from_deploy_and_receipts_preserve_owner(self):
        """Checkpoint due times are from deploy and receipts preserve owner."""
        record = {
            "release_id": "r1",
            "times": {"deploy_finished": "2026-10-23T20:00:00Z"},
            "followup": {"owner": {"thread_url": "codex://threads/owner"}},
        }
        plan = ro.checkpoint_plan(record, [15, 60])
        self.assertEqual(plan["15"]["due_at"], "2026-10-23T20:15:00Z")
        self.assertEqual(plan["60"]["due_at"], "2026-10-23T21:00:00Z")
        ro.note_checkpoint(
            record,
            {"at": "2026-10-23T20:16:00Z", "result": "inconclusive", "availability": {"status": "unconfirmed"}},
            "window_15m",
        )
        self.assertEqual(record["followup"]["owner"]["thread_url"], "codex://threads/owner")
        self.assertEqual(record["followup"]["checkpoints"]["window_15m"]["id"], "r1:window_15m")

    def test_rescheduled_watch_preserves_completed_checkpoint(self):
        """Rescheduled watch preserves completed checkpoint."""
        record = {"release_id": "r1", "times": {"deploy_finished": "2026-10-23T20:00:00Z"}}
        ro.merge_plan(record, [15, 60])
        ro.begin_checkpoint(record, "window_15m")
        ro.note_checkpoint(record, {"at": "2026-10-23T20:18:00Z", "result": "passed"}, "window_15m")
        before = dict(record["followup"]["checkpoints"]["window_15m"])
        ro.merge_plan(record, [15, 60])
        self.assertEqual(record["followup"]["checkpoints"]["window_15m"], before)
        self.assertEqual(before["attempts"], 1)
        self.assertEqual(record["followup"]["checkpoints"]["window_60m"]["state"], "scheduled")

    def test_owner_first_claim_idempotency_and_audited_transfer(self):
        """Owner first claim idempotency and audited transfer."""
        record = {}
        ro.claim_owner(record, "codex://threads/one", "https://github.com/o/r/pull/1")
        before = dict(record["followup"]["owner"])
        ro.claim_owner(record, "codex://threads/one", "https://github.com/o/r/pull/1")
        self.assertEqual(record["followup"]["owner"], before)
        with self.assertRaises(ValueError):
            ro.claim_owner(record, "codex://threads/two", "https://github.com/o/r/pull/1")
        ro.claim_owner(
            record,
            "codex://threads/two",
            "https://github.com/o/r/pull/1",
            takeover=True,
            reason="human requested takeover",
        )
        self.assertEqual(record["followup"]["owner_history"][0]["previous"], before)
        self.assertTrue(ro.owned(record))

    def test_supersession_closes_only_unfinished_receipts(self):
        """Supersession closes only unfinished receipts."""
        record = {"release_id": "r1", "times": {"deploy_finished": "2026-10-23T20:00:00Z"}}
        ro.merge_plan(record, [15, 60])
        self.assertFalse(ro.owned(record))
        ro.begin_checkpoint(record, "window_15m")
        ro.supersede(record, "release r2")
        self.assertEqual({receipt["state"] for receipt in record["followup"]["checkpoints"].values()}, {"superseded"})
        self.assertEqual(record["followup"]["checkpoints"]["window_60m"]["superseded_by"], "release r2")

    def test_explicit_retry_preserves_prior_attempt_evidence(self):
        """Explicit retry preserves prior attempt evidence."""
        record = {"release_id": "r1"}
        ro.begin_checkpoint(record, "window_15m")
        ro.note_checkpoint(record, {"at": "first", "result": "inconclusive"}, "window_15m")
        ro.begin_checkpoint(record, "window_15m")
        receipt = record["followup"]["checkpoints"]["window_15m"]
        self.assertEqual(receipt["attempts"], 2)
        self.assertEqual(receipt["attempt_history"][0]["completed_at"], "first")

    def test_manual_repair_retains_scheduled_checkpoint(self):
        """Manual repair retains scheduled checkpoint."""
        self.assertEqual(ro.scheduled_minute({}, 18), 15)
        self.assertEqual(ro.scheduled_minute({}, 63), 60)
        self.assertEqual(ro.scheduled_minute({}, 99, 15), 15)

    def test_early_check_binds_to_no_checkpoint(self):
        """Early check binds to no checkpoint."""
        self.assertIsNone(ro.scheduled_minute({}, 3))
        self.assertIsNone(ro.scheduled_minute({}, 5, 15))
        self.assertEqual(ro.scheduled_minute({}, 15), 15)
        record = {"release_id": "r1", "times": {"deploy_finished": "2026-10-23T20:00:00Z"}}
        ro.merge_plan(record, [15, 60])
        for index in range(ro.EARLY_CHECKS_KEPT + 2):
            ro.note_early_check(record, {"at": f"t{index}", "result": "passed"}, "early_3m")
        self.assertEqual(len(record["followup"]["early_checks"]), ro.EARLY_CHECKS_KEPT)
        self.assertEqual({receipt["state"] for receipt in record["followup"]["checkpoints"].values()}, {"scheduled"})

    def test_later_terminal_verdict_survives_older_checkpoints(self):
        """Later terminal verdict survives older checkpoints."""
        for status in ("passed", "warning", "inconclusive"):
            self.assertTrue(ro.later_terminal({"status": status, "phase": "window_60m"}, "window_15m"))
        self.assertFalse(ro.later_terminal({"status": "pending", "phase": "window_60m"}, "window_15m"))
        self.assertFalse(ro.later_terminal({"status": "passed", "phase": "window_15m"}, "window_60m"))

    def test_manual_repair_restores_adaptive_checkpoint_without_rewriting_earlier_evidence(self):
        """Manual repair restores adaptive checkpoint without rewriting earlier evidence."""
        record = {"release_id": "r1", "times": {"deploy_finished": "2026-10-23T20:00:00Z"}}
        ro.merge_plan(record, [15, 60])
        ro.note_checkpoint(record, {"at": "original", "result": "pending"}, "window_60m")
        ro.extend_plan(record, 75)
        ro.begin_checkpoint(record, "window_75m")
        record["followup"]["checkpoints"]["window_75m"].update(state="error", last_error="read failed")
        restored = json.loads(json.dumps(record))
        minute = ro.scheduled_minute(restored, 80)
        self.assertEqual(minute, 75)
        ro.begin_checkpoint(restored, f"window_{minute}m")
        ro.note_checkpoint(restored, {"at": "repaired", "result": "passed"}, f"window_{minute}m")
        receipts = restored["followup"]["checkpoints"]
        self.assertEqual(receipts["window_60m"], record["followup"]["checkpoints"]["window_60m"])
        self.assertEqual(receipts["window_75m"]["state"], "completed")
        self.assertNotIn("last_error", receipts["window_75m"])
        self.assertEqual(receipts["window_75m"]["attempt_history"][0]["last_error"], "read failed")
        self.assertEqual(ro.scheduled_minute(restored, 80, 60), 60)

    def test_crashed_running_attempt_is_archived_and_stale_error_is_cleared(self):
        """Crashed running attempt is archived and stale error is cleared."""
        record = {"release_id": "r1"}
        ro.begin_checkpoint(record, "window_15m")
        receipt = record["followup"]["checkpoints"]["window_15m"]
        receipt["last_error"] = "old attempt"
        ro.begin_checkpoint(record, "window_15m")
        self.assertEqual(receipt["attempt_history"][0]["state"], "running")
        self.assertNotIn("last_error", receipt)
        ro.note_checkpoint(record, {"at": "now", "result": "passed"}, "window_15m")
        self.assertEqual(receipt["state"], "completed")
        self.assertNotIn("last_error", receipt)

    def test_owner_requires_both_chat_and_pr(self):
        """Ownership requires both the agent chat and the PR."""
        for thread, pr in [(" ", "https://github.com/o/r/pull/1"), ("codex://threads/one", " ")]:
            with self.assertRaises(ValueError):
                ro.claim_owner({}, thread, pr)
        self.assertFalse(ro.owned({"followup": {"owner": {"thread_url": "codex://threads/one"}}}))

    def test_confirmed_noop_requires_an_explicit_decoded_receipt(self):
        """Confirmed noop requires an explicit decoded receipt."""
        payload = {
            "id": "skipped",
            "status": "SUCCESS",
            "createTime": "2026-10-23T20:00:00Z",
            "steps": [
                {"id": "promote-still-current", "status": "SUCCESS"},
                {"id": "release-record-begin", "status": "SUCCESS"},
            ],
        }
        self.assertFalse(guard.Build.from_api(payload).confirmed_noop)
        receipt = base64.b64encode(json.dumps({"release_deploy": "skipped_before_mutation"}).encode()).decode()
        payload["results"] = {"buildStepOutputs": [receipt]}
        skipped = guard.Build.from_api(payload)
        self.assertTrue(skipped.confirmed_noop)
        self.assertFalse(skipped.changing_cluster)
        payload["results"] = {"buildStepOutputs": ["not-base64"]}
        self.assertFalse(guard.Build.from_api(payload).confirmed_noop)

    def test_extension_plan_roundtrips_and_keeps_the_due_time(self):
        """Extension plan roundtrips and keeps the due time."""
        record = {
            "release_id": "r1",
            "times": {"deploy_finished": "2026-10-23T20:00:00Z"},
        }
        ro.extend_plan(record, 75)
        ro.extend_plan(record, 75)
        restored = json.loads(json.dumps(record))
        self.assertEqual(ro.restore_extensions(restored, [15, 60]), [15, 60, 75])
        self.assertEqual(
            restored["followup"]["checkpoints"]["window_75m"]["due_at"],
            "2026-10-23T21:15:00Z",
        )


class WindowDigestTest(unittest.TestCase):
    def test_unresolved_tag_digest_is_uncertain_but_confirmed_failures_still_fail(self):
        """Unresolved tag digest is uncertain but confirmed failures still fail."""
        import release_verify as rv
        from dataclasses import asdict

        digest = "sha256:" + "1" * 64
        images = {role: "registry/app@" + digest for role in ("app", "api", "face")}
        workload = dict(
            kind="deployment",
            role="app",
            digest=None,
            image="registry/app:prod",
            rollout_current=True,
            replicas=2,
            updated_replicas=2,
            available_replicas=0,
            running_digests=[],
            bad_pods=[],
        )
        snapshot = {"deployment/test": workload}
        check = rv.check_images({"images": images}, snapshot, window=True)
        self.assertEqual(check.status, "inconclusive")
        confirmed, evidence = ro.confirm_images(lambda deadline: (asdict(check), snapshot), budget=0)
        self.assertEqual((confirmed["status"], evidence["status"]), ("inconclusive", "unconfirmed"))
        workload["bad_pods"] = ["pod: CrashLoopBackOff"]
        self.assertEqual(rv.check_images({"images": images}, snapshot, window=True).status, "failed")
        workload.update(bad_pods=[], digest="sha256:" + "2" * 64)
        self.assertEqual(rv.check_images({"images": images}, snapshot, window=True).status, "failed")
