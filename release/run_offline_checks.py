#!/usr/bin/env python3
"""Run a release check command offline: no cloud credentials, no live CLIs, no outbound HTTP.

    python3 release/run_offline_checks.py -- python -m unittest discover -s release -p 'test_*.py'

Every repository's release-kit workflow runs the kit tests through it, so a test that misses a mock
can never reach a cluster, a cloud project or Slack with the runner's or a developer's access:
KUBECONFIG=/dev/null, an empty temporary CLOUDSDK_CONFIG, GOOGLE_APPLICATION_CREDENTIALS=/dev/null,
stubs that refuse kubectl, gcloud, gsutil, bq, curl, gh and orbit-agent-gh (exit 98), Slack posts
disabled, and HTTP(S) proxies that point at a closed local port. Only a small set of variables
(PATH, HOME, locale, temp dir, Python paths) passes through from the caller's environment.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BLOCKED_CLIS = ("kubectl", "gcloud", "gsutil", "bq", "curl", "gh", "orbit-agent-gh")
KEPT_VARIABLES = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH")
BLOCKED_EXIT = 98


def offline_env(folder: Path, base: dict[str, str]) -> dict[str, str]:
    """The environment of an offline check: blocked CLIs first on PATH, null credentials, no network."""
    bin_dir, config = folder / "bin", folder / "gcloud-config"
    bin_dir.mkdir()
    config.mkdir()
    for name in BLOCKED_CLIS:
        path = bin_dir / name
        path.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "{name}: blocked in offline release checks" >&2\nexit {BLOCKED_EXIT}\n',
            encoding="utf-8",
        )
        path.chmod(0o700)
    env = {key: value for key, value in base.items() if key in KEPT_VARIABLES}
    env.update(
        PATH=str(bin_dir) + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        KUBECONFIG="/dev/null",
        CLOUDSDK_CONFIG=str(config),
        GOOGLE_APPLICATION_CREDENTIALS="/dev/null",
        RELEASE_SLACK_DISABLED="1",
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        http_proxy="http://127.0.0.1:9",
        https_proxy="http://127.0.0.1:9",
        NO_PROXY="",
        no_proxy="",
    )
    return env


def main(argv: list[str] | None = None) -> int:
    """Run the command after -- in the offline environment; return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--cwd", default=".", help="directory to run the command in")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the check command after --")
    with tempfile.TemporaryDirectory(prefix="release-offline-check-") as tmp:
        return subprocess.run(
            command, cwd=args.cwd, env=offline_env(Path(tmp), dict(os.environ)), check=False
        ).returncode


if __name__ == "__main__":
    sys.exit(main())
