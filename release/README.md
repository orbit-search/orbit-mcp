# Release kit

Release safety for one Orbit service: merge deploys, dev verify, digest promotion,
release records, the release check, automatic rollback, the freeze and the hold.
It is the deep-search and orbit-api release pipeline with everything
service-specific moved into one config file.

This directory is a copy of `orbit-infrastructure/release-kit/release/`.
`KIT.json` pins every file; `python3 release/release_kit.py check` fails when a
copy changes. Change the kit in orbit-infrastructure and sync it to every repo.
The repo owns only `release/services/<service>.json` and the build files that
`release_kit.py render` writes from it.

## Tiers

| Tier | Deploy | Check | Failed release |
|---|---|---|---|
| `full` | A merge deploys dev; dev verify gates; the dev build starts the prod build, which promotes the dev digest (Cloud Run: the dev revisions; Composer: the dev candidate folder). | Smoke checks after the deploy, watch window at +15 and +60 min. | Rolls back on its own; promotes hold. |
| `check_rollback` | The prod build of a merge deploys (no dev stage), after single flight and the freeze and hold gate. | Same. | Rolls back on its own; deploys hold. |
| `check_only` | Same as `check_rollback`. | Same. | Slack alert with the author ping and a fix prompt; a human rolls back. |

## Files

| File | What it does |
|---|---|
| `release_config.py` | Loads and validates `services/<service>.json`. |
| `promote_release_guard.py` | Single flight (`turn`), and for the full tier `preflight` (on the release branch, dev build passed, the dev digest) and `still-current`. |
| `release_record.py` | Records in `gs://orbit-search-release-records/<service>/<env>/`: `begin` before the first cluster change, `finish` after the deploy, `show`, `backfill`. |
| `release_verify.py` | `check --phase deploy` (smoke checks), `schedule-window`, `watch` (metrics and new errors at each checkpoint), the automatic rollback and the Slack alerts. |
| `release_signals.py` | New monitor alerts, new Error Tracking issues, new error log lines (Cloud Logging) and new failure series, each traced to a suspected commit. |
| `release_gate.py` | `request-promote`, `check-prod`, `check-direct`, `status`, `freeze`, `unfreeze`, `resume`, `promote`. |
| `release_rollback.py` | `plan`, `run` (starts the `<service>-rollback` trigger), `apply` (inside the rollback build). |
| `release_light.py` | Services that are not GKE Deployments: kind `cloud_run` and kind `composer`, tier `check_only` or `full`. See [Cloud Run and Composer](#cloud-run-and-composer). |
| `release_kit.py` | `check` and `render`. |
| `release_people.json` | GitHub login to Slack id, for the author ping. |

## The service config

```json
{
  "service": "slipi-management-portal",
  "title": "SMP",
  "tier": "full",
  "github_repo": "orbit-search/slipi-management-portal",
  "connected_repository": "orbit-search-slipi-management-portal",
  "release_branch": "main",
  "docs": "docs/RELEASE_PIPELINE.md",
  "image": {"repository": "us-central1-docker.pkg.dev/orbit-search/slipi-management-portal/slipi-management-portal",
            "registry_location": "us-central1", "registry_repository": "slipi-management-portal",
            "package": "slipi-management-portal"},
  "triggers": {"dev": ["slipi-management-portal-dev"], "prod": ["slipi-management-portal-prod"],
               "rollback": "slipi-management-portal-rollback"},
  "environments": {
    "prod": {"cluster": "prod", "namespace": "smp",
             "deployments": [{"name": "prod-smp-deployment", "container": "smp", "selector": "app=prod-smp-deployment"}],
             "smoke": [{"type": "http_pod", "name": "readiness", "deployment": "prod-smp-deployment",
                        "port": 3000, "path": "/api/readyz"}]},
    "dev": {"cluster": "dev", "namespace": "smp", "deployments": [...], "smoke": [...]}
  },
  "verify": {"checkpoints": [15, 60], "metrics": [], "signals": {"log_containers": ["smp"]}},
  "code": {"source_prefixes": ["app/", "lib/", "components/"], "extensions": [".ts", ".tsx", ".js"]}
}
```

- `triggers`: the first name of each list is the trigger; later names are retired
  triggers whose builds still count for single flight.
- `deployments[].rollback_env`: env values a rollback sets together with the image,
  in one patch. For a value that the deploy turns on only after its rollout
  (orbit-chatgpt-app: `OAUTH_SESSION_ISSUANCE_ENABLED`), so a rollback after a
  deploy that failed before that step does not leave it off.
- Smoke check types: `http_pod` (a GET on every live pod through the Kubernetes API
  pod proxy: `port`, `path`, `expect_status`, `contains`, `expect_json`, `all_pods`),
  `http_public` (`url`, same expectations), `pods_ready` (`deployment`, `min_share`).
- `verify.metrics`: Datadog metrics compared with the baseline. `kind` `rate`
  (`numerator`, `denominator`) or `value` (`value`, `samples`, `value_ms`); a query
  `by {tag}` is judged per group. `ratio` and `absolute` must both be exceeded, with
  `min_samples` in both windows; `spread` also needs the value above 1.5x the highest
  baseline hour; `judged` metrics decide "passed" against `verify.min_judged`.
  `versioned` (rate metrics only): the queries can also be grouped by the Datadog
  `version` tag (the service sets DD_VERSION to the commit), so an exceeded rate is
  compared with the other versions that served in the same window.
- `verify.signals`: `log_containers` (Cloud Logging new error lines), `monitors` (a
  Datadog event query), `error_tracking` (APM services), `datadog_env`,
  `failure_series` (`failures` and `all` count queries grouped by `fields`, `min_new`).
- `code`: which paths are runtime source (`source_prefixes`, `extensions`), tests,
  non-runtime and deploy files. A release that changes no runtime code never rolls
  back on a metric; suspects rank by the source they change.

## Rules carried over from deep-search

- A metric limit fails a release only when the release changed runtime code and the
  value is above the baseline's hourly spread (deep-search #2315).
- Control by version (deep-search #2334): an exceeded `versioned` rate is not the
  release's ("environmental", a warning) when the other versions that served in the
  same window (the rollout overlap, the rollback target) also exceed the limit over
  the baseline and the release does not exceed the limit over them.
- After an automatic rollback, the watch judges the regressed metrics without the
  spread rule again on the rollback target, 15 min after it deployed, when no build
  changes prod. Still exceeded: the cause is
  environmental. The signal is marked in `<store>/<service>/prod/environmental_signals.json`
  (an exceeded limit on it is a quiet warning until it is back within its old limit),
  Slack gets one line, and when every regression was such a signal the rollback's
  hold ends.
- A new error fails a release only with a clear suspect: a score of 4 or more and 2
  above the next PR. A commit that adds the log line itself is never clear.
- The watch window judges releases that a newer release replaced before their own
  window ended together with the newer one.
- The automatic rollback claims the record with a generation-conditional write and
  passes `_EXPECT_LATEST`, so it refuses when prod moved on.
- One build at a time changes prod. The prod lock (`<store>/<service>/prod/lock.json`,
  a generation-conditional write) belongs to a build id: a deploy build takes it in
  its gate step (`check-prod` or `check-direct` with `--build-id "$BUILD_ID"`), decides
  the gate while it holds it, and frees it in `release_record.py finish`; a rollback
  build holds it for its apply. A build that ended frees it too. A deploy lets a
  rollback that runs go first, so the rollback's hold decides whether it deploys.
- After a rollback, deploys hold until a revert of the held PRs, a commit with the
  trailer `Fixes-release: <release id>`, or `release_gate.py resume`.
- Test runs say `TEST RUN, nothing is wrong` first and never roll back.

## Cloud Run and Composer

`release_light.py` serves the services that are not GKE Deployments. Their
config has `"kind": "cloud_run"` or `"kind": "composer"`. `release_gate.py` and
`release_rollback.py` take these services too: the same freeze, hold, prod lock,
records, `_EXPECT_LATEST` and `Fixes-release:` trailers.

### Tier check_only

One build deploys prod. The kit records the release, checks it, and runs the
watch window. A failed release posts an alert with the command that undoes it.
A human runs that command. Config: `cloud_run_services` and `deploy_triggers`
(cloud_run), or `composer.environment` and `composer.sync_trigger` (composer).

### Tier full

The release goes through a dev stage, dev verify and a prod promote of the same
artifact. No kind has a dev environment of its own, so the dev stage lives in
prod's own resources and takes no traffic.

| | Cloud Run | Composer |
|---|---|---|
| Dev stage | The dev build builds each image once and deploys a new revision of each prod service: `gcloud run deploy ... --no-traffic --tag candidate`. | The dev build copies the commit's `dags/` to `data/release-candidate/<commit>/dags/` in the environment's bucket (`release_light.py stage`). The DAG processor reads only `dags/`. |
| Artifact | `{service: revision@digest}` of the candidate revisions. | `{"dags": "<candidate folder> sha256:<manifest>"}`: a hash over the name, CRC32C and size of every object. |
| Dev verify | Each candidate revision is Ready and runs the digest this build pushed, the `candidate` tag still names it, and the smoke checks answer on the tag URLs (`https://candidate---<host>`). | The candidate folder is unchanged, and a parse of its files with the environment's Airflow version has no import error that prod's DAGs do not have. |
| Promote (prod build) | 100% of the traffic to the recorded revisions (`gcloud run services update-traffic --to-revisions`). | `gsutil rsync -r -d` of the candidate folder to `dags/`. |
| Deploy check | Each service sends 100% to its promoted revision; the prod smoke checks. | No new DAG import error after the DAG processor parsed the files (Airflow API). |
| Rollback | Traffic back to the revisions that served before the release (or those of the release named with `--to`). | The target commit's candidate folder to `dags/`. A commit from before the candidate folders is staged from GitHub first. |

What differs from GKE:

- Cloud Run: the dev stage is a revision of the prod service. It reads the
  prod config, secrets and data, and dev verify calls write to prod data like
  any prod request. Service-level settings of the deploy command (IAM such as
  `--allow-unauthenticated`, ingress, the service template) change at the dev
  deploy, before dev verify. Only the traffic waits for the promote.
- Composer: Airflow 3.1 has no `dags list-import-errors --subdir`; the CLI
  takes only `--bundle-name` and `--local`, and both read `dags/`. So dev verify
  parses the files in the build (`parse-dags`, image `apache/airflow:<version>`).
  The parse has the repository's `pypi-requirements.txt`, not the environment's
  exact packages, and no Composer Variables, Connections or environment
  variables. An error that prod's DAGs also have does not count, and an error
  that needs a Variable or a Connection is reported but not judged. The deploy
  check after the promote still reads the import errors of the environment.

Config of tier full (cloud_run):

```json
{
  "service": "orbit-mcp", "kind": "cloud_run", "tier": "full",
  "cloud_run_services": ["orbit-mcp", "orbit-profile-mcp"],
  "cloud_run_urls": {"orbit-mcp": "https://orbit-mcp-k7iyipqilq-uc.a.run.app",
                     "orbit-profile-mcp": "https://orbit-profile-mcp-k7iyipqilq-uc.a.run.app"},
  "triggers": {"dev": ["orbit-mcp-deploy-dev"], "prod": ["orbit-mcp-prod", "orbit-mcp-deploy"],
               "rollback": "orbit-mcp-rollback"},
  "smoke": [{"type": "http_public", "url": "https://orbit-mcp-k7iyipqilq-uc.a.run.app/health"}, ...]
}
```

Every smoke URL must be under a `cloud_run_urls` base URL, and every service
needs one, so dev verify can call it on the tag URL. Composer takes
`composer.environment`, `composer.bucket` (used when the build cannot read the
environment) and `triggers`. Later names in `triggers.prod` are retired
triggers whose builds still count for single flight.

The dev build (`<service>-deploy-dev`, on a merge): build, `turn --env dev`,
`begin --env dev`, the candidate deploy, `finish --env dev`, `check --env dev`
(dev verify), `release_gate.py request-promote`. The prod build
(`<service>-prod`, manual, started by the dev build): `preflight` (single
flight, on the release branch, the dev build passed, the artifacts as they are
now), `release_gate.py check-prod --artifacts-file`, `still-current`, `begin`,
`apply`, `finish`, `check`, `schedule-window`. `release_kit.py render` writes
the window build and the rollback build (`<service>-rollback`).

### Fresh availability evidence and deploying-agent ownership

Window checks refresh workload state after metric/log queries. Replica-count
shortfalls alone are retried for up to three minutes; persistent uncertainty is
recorded as inconclusive and unchecked, alerts at +15, continues to +60, and ends
red without automatically reverting an unproven code cause. Independent metric
or traced-error regressions can still roll back. Every sample records its time,
digests, desired/available replicas and pod creation/scheduling conditions.

`release_verify.py own --env prod --release-id <id> --thread-url <chat URL> --pr-url <PR URL>`
records the agent's acknowledgement in `followup.owner`. Use the script's local
path and service flag. `followup.checkpoints` carries stable release/checkpoint
IDs, due times, attempt counts, running/error/completion receipts and observed
results. These receipts support recovery and auditing; they do not themselves
wake an agent. The deploying agent must set a native follow-up in its original
chat, read +15/+60 results and retain ownership through recovery or escalation.
The existing watch build performs server-side checks even while that agent is
asleep. No generic automatic cross-provider chat delivery is claimed.

Cloud Run/Composer use the same ownership fields through
`python3 release/release_light.py --service <service> own --env prod --release-id <id> --thread-url <chat URL> --pr-url <PR URL>`.
