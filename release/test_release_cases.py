"""Tests of the release cases: every shared and repository case replays as expected, and the runner's rules.

Standard library only: `python3 -m unittest discover -s release -p 'test_*.py'`. The replay
runs in its own Python process, so module state that other kit tests change (for example the
sample limit of release_observation) never reaches the kit's real decision code.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import release_cases as cases  # noqa: E402
import release_kit as kit  # noqa: E402

# Case files are frozen evidence; they must never carry a credential or a person's address.
SECRET_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"  # an e-mail address
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"  # GitHub, Slack, Google keys
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r'|"(?:author|email|password|secret|token|api_key|authorization)"\s*:',
    re.IGNORECASE,
)


def rule_case(**overrides) -> dict:
    """A rule case on this test file (it always holds the line 'frozen evidence')."""
    case = {
        "schema_version": 1,
        "id": "rule",
        "title": "a rule",
        "kind": "rule",
        "inputs": {"path": f"{HERE.name}/test_release_cases.py", "pattern": r"frozen\s+evidence"},
        "expected": {"verdict": "passed"},
        "at_the_time": {"verdict": "failed"},
    }
    case.update(overrides)
    return case


class ReplayTest(unittest.TestCase):
    def test_every_case_replays_as_expected(self) -> None:
        """The shared kit cases and the repository's own cases (release/cases/) all replay as expected."""
        done = subprocess.run(
            [sys.executable, str(HERE / "release_cases.py"), "replay"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"(?m)^score \d+/\d+")
        for path in sorted(cases.KIT_CASES_DIR.glob("*.json")):
            self.assertIn(f"kit_cases/{path.stem}:", done.stdout)


class CaseFileTest(unittest.TestCase):
    def test_kit_cases_ship_with_the_kit(self) -> None:
        """Every shared case is a pinned kit file, and every pinned case exists."""
        shipped = {f"kit_cases/{path.name}" for path in cases.KIT_CASES_DIR.glob("*.json")}
        pinned = {name for name in kit.KIT_FILES if name.startswith("kit_cases/")}
        self.assertTrue(shipped)
        self.assertEqual(shipped, pinned)

    def test_case_files_are_valid_and_carry_no_secrets(self) -> None:
        """Each case has a valid shape, its id is its file name, and it holds no address or key."""
        for path in cases.case_paths([cases.KIT_CASES_DIR, cases.REPO_CASES_DIR]):
            text = path.read_text(encoding="utf-8")
            with self.subTest(case=path.name):
                self.assertEqual(cases.case_problems(json.loads(text), path), [])
                self.assertIsNone(SECRET_RE.search(text), SECRET_RE.search(text))


class OutcomeTest(unittest.TestCase):
    def setUp(self) -> None:
        """Rule paths resolve from the kit's parent folder, as in a repository."""
        patcher = mock.patch.object(cases, "REPO_ROOT", HERE.parent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rule_case(self) -> None:
        """A rule case passes while its line exists and fails without it."""
        self.assertEqual(cases.outcome(rule_case())[0], "ok")
        missing = rule_case(inputs={"path": "release/no-such-file.md", "pattern": "x"})
        status, problems, _ = cases.outcome(missing)
        self.assertEqual(status, "FAIL")
        self.assertIn("verdict failed, expected passed", problems)

    def test_known_open_is_reported_then_fixed_fails(self) -> None:
        """A known-open case that is still wrong is "open"; once the code gets it right, it fails as FIXED."""
        still_wrong = rule_case(
            known_open="the rule is not written yet", inputs={"path": "release/no-such-file.md", "pattern": "x"}
        )
        status, problems, _ = cases.outcome(still_wrong)
        self.assertEqual(status, "open")
        self.assertIn("known open: the rule is not written yet", problems)
        status, problems, _ = cases.outcome(rule_case(known_open="the rule is not written yet"))
        self.assertEqual(status, "FIXED")

    def test_replay_scores_known_open_cases_apart(self) -> None:
        """replay passes with a known-open case and names it in the score; a FIXED case fails it."""
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            open_case = rule_case(id="open", known_open="why", inputs={"path": "release/none.md", "pattern": "x"})
            (folder / "open.json").write_text(json.dumps(open_case), encoding="utf-8")
            (folder / "rule.json").write_text(json.dumps(rule_case()), encoding="utf-8")
            with mock.patch("sys.stdout") as out:
                self.assertEqual(cases.replay(folders=[folder]), 0)
            printed = "".join(call.args[0] for call in out.write.call_args_list)
            self.assertIn("score 1/2 (1 known open)", printed)
            (folder / "fixed.json").write_text(json.dumps(rule_case(id="fixed", known_open="why")), encoding="utf-8")
            with mock.patch("sys.stdout"):
                self.assertEqual(cases.replay(folders=[folder]), 1)

    def test_a_bad_case_file_fails(self) -> None:
        """A case whose id is not its file name, or of an unknown kind, fails without running."""
        status, problems, _ = cases.outcome(rule_case(kind="mystery"), Path("rule.json"))
        self.assertEqual(status, "FAIL")
        self.assertTrue(any("kind 'mystery'" in problem for problem in problems))

    def test_light_kinds_are_refused(self) -> None:
        """A checkpoint case of a Cloud Run, Composer or Vercel service fails clearly until it has a replay."""
        case = {"service": {"kind": "cloud_run"}, "decision": {"phase": "window"}, "record": {}}
        with self.assertRaises(cases.CaseError):
            cases.checkpoint_decision(case)


class AnswersTest(unittest.TestCase):
    def test_recorded_value_error_and_missing_read(self) -> None:
        """A recorded answer is returned, a recorded error raised again, a missing read fails the case."""
        key = cases.read_key("github.json", "repos/o/r/commits/abc")
        answers = cases.Answers({key: {"value": {"files": []}}, "boom": {"error": "HTTP 500", "type": "TimeoutError"}})
        self.assertEqual(answers.get(key), {"files": []})
        with self.assertRaises(TimeoutError):
            answers.get("boom")
        with self.assertRaises(cases.MissingRead):
            answers.get("other")
        self.assertEqual(answers.used, {key, "boom", "other"})

    def test_a_missing_read_passes_the_checks_own_handlers(self) -> None:
        """confirm_images handles RuntimeError on a fresh read; a missing read still fails the case."""
        observation = getattr(cases.rv, "ro", None)
        if observation is None:
            self.skipTest("this kit has no fresh workload observation")

        def read(deadline: float) -> tuple[dict, dict]:
            raise cases.MissingRead("missing read: workloads")

        with self.assertRaises(cases.MissingRead):
            observation.confirm_images(read, budget=0)

    def test_store_reads_objects_and_refuses_writes(self) -> None:
        """The case store reads its objects, answers (None, 0) for others and refuses every write."""
        store = cases.CaseStore({"svc/prod/latest.json": {"release_id": "r1"}})
        self.assertEqual(store.read("svc/prod/latest.json"), ({"release_id": "r1"}, 1))
        self.assertEqual(store.read("svc/prod/lock.json"), (None, 0))
        with self.assertRaises(cases.MissingRead):
            store.write("svc/prod/lock.json", {}, 0)

    def test_http_timeline_and_clock(self) -> None:
        """An HTTP answer is the newest at or before the replay's time; the clock moves only on sleep."""
        clock = cases.FakeClock(cases.rr.parse_utc("2026-10-22T05:39:13Z"))
        timeline = {"u": [{"after_seconds": 0, "status": 521}, {"after_seconds": 60, "status": 200, "body": "ok"}]}
        self.assertEqual(cases._http_answer(timeline, "u", clock), (521, b""))
        clock.sleep(59)
        self.assertEqual(cases._http_answer(timeline, "u", clock)[0], 521)
        clock.sleep(1)
        self.assertEqual(cases._http_answer(timeline, "u", clock), (200, b"ok"))
        self.assertEqual(f"{clock.now():%H:%M:%S}", "05:40:13")
        with self.assertRaises(cases.MissingRead):
            cases._http_answer(timeline, "v", clock)

    def test_an_answer_before_its_time_is_a_missing_read(self) -> None:
        """A first answer at +60 s does not answer a request at +0 s: the case must hold what came first."""
        clock = cases.FakeClock(cases.rr.parse_utc("2026-10-22T05:39:13Z"))
        later = {"u": [{"after_seconds": 60, "status": 200}]}
        with self.assertRaises(cases.MissingRead):
            cases._http_answer(later, "u", clock)
        clock.sleep(60)
        self.assertEqual(cases._http_answer(later, "u", clock), (200, b""))


if __name__ == "__main__":
    unittest.main()
