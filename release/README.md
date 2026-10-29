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
| `check_only` | Same as `check_rollback`. | Same. | A red "release failed" line in Slack with the author ping (the fix prompt is in the build log); a human rolls back. |

## Slack

Every post to #deployments is one line: a status dot, the service, its state and a link to the
service's page on the ops board, `https://ops.orbitsearch.com/<service>` (`<service>` is the folder of
the service's records in `gs://orbit-search-release-records/`). The rendered build files always link
that board; `RELEASE_OPS_URL` changes only the lines the Python steps post, so a local value never
makes `release_kit.py check` reject the committed build files.
The reasons (failed checks, error rates, suspects, rollback targets, holds, commits) are on the
ops board and in the build log, never in the post.

```text
:red_circle: *deep-search* #2430 8a6cd79 release failed · rolling back to 1a2b3c4 · <https://ops.orbitsearch.com/deep-search|ops.orbitsearch.com/deep-search>
```

#deployments gets a line only when a human should look: a release failed or a rollback failed (red), a
promote hold or a freeze (yellow, `on hold`), a release check that could not decide (yellow). Routine
progress posts nothing: `deploying` and `up` go to the build log only (`release_record.QUIET_STATES`), and
the ops board shows them on its Changes page and on each service page. A sync round that merges in many
repositories at once therefore adds no lines when its releases pass.

The line names the release event, not the health of the service: a release that failed its check is
"release failed", and the service may be healthy. After the service come the release's PR and short
commit when its record has them (`release_record.release_label`). The rendered Vercel and Railway builds
put `$SHORT_SHA` there. The state keys and the dots stay; the words are these, unless the moment has
more exact words (in parentheses). The ops board keeps its own labels.

| State | Dot | Words | Posted | When |
|---|---|---|---|---|
| `deploying` | green | deploying | no | The prod promote started; for a service without a dev stage, the deploy checks passed. |
| `up` | green | released | no | The release passed (also: "signals recovered", "unfrozen", "deploys resumed", "test run"). |
| `warning` | yellow | release check inconclusive | yes | Not verified: an inconclusive check, a checkpoint that could not run (also: "release check warning" for warnings that do not roll back, "dev check inconclusive", "promote did not start", "release failed · not published" for a static site that never went live). |
| `on hold` | yellow | on hold | yes | Prod promotes hold: after a rollback, a forward-only release, a freeze, a failed dev verify. |
| `down` | red | release failed | yes | A release failed, once per release; a failed promote or deploy is one line. A service with an automatic rollback adds "· rolling back to <short sha>" (`release_verify.failed_words`); others say only "release failed". Also: "rollback failed", "rollback has no result", "release failed · rollback did not start", "release failed · no automatic rollback", "rollback did not help", "release unchecked" (the record finish failed). |

A failed release posts only its failure line: its warnings that do not roll back stay unposted, in the
record (the ops board) and in the build log.

An alert about a release no agent claimed ends with the mentions of its owner (and of the suspects'
authors for a failed release). `release_record.status_line` builds the line; `post_status` prints the
detail to the build log (Slack ids redacted) and posts the line (a routine state: the build log only).

The deploy check, the watch and the rollback can reach the same failed release; `release_verify.alert_once`
lets one of them post its line. A caller that finds another caller's claim waits until that caller notes
the post. When that post fails the claim is freed and the waiting caller posts; a claim older than two
minutes (its caller died) is taken over.

## Files

| File | What it does |
|---|---|
| `release_config.py` | Loads and validates `services/<service>.json`. |
| `promote_release_guard.py` | Single flight (`turn`), and for the full tier `preflight` (on the release branch, dev build passed, the dev digest) and `still-current`. |
| `release_record.py` | Records in `gs://orbit-search-release-records/<service>/<env>/`: `begin` before the first cluster change, `finish` after the deploy, `show`, `backfill`. |
| `release_verify.py` | `check --phase deploy` (smoke checks), `schedule-window`, `watch` (metrics and new errors at each checkpoint), the automatic rollback and the Slack alerts. |
| `release_capacity.py` | The capacity verdict of a rollout that did not finish (Rule 1), rollout timeouts scaled by replicas (Rule 3), and `rollout-wait` for the prod build's deploy step. See [Capacity is environmental](#capacity-is-environmental). |
| `release_signals.py` | New monitor alerts, new Error Tracking issues, new error log lines (Cloud Logging) and new failure series, each traced to a suspected commit. |
| `release_gate.py` | `request-promote`, `check-prod`, `check-direct`, `status`, `freeze`, `unfreeze`, `resume`, `promote`. |
| `release_rollback.py` | `plan`, `run` (starts the `<service>-rollback` trigger), `apply` (inside the rollback build). |
| `release_light.py` | Services that are not GKE Deployments: kind `cloud_run` and kind `composer`, tier `check_only` or `full`. See [Cloud Run and Composer](#cloud-run-and-composer). |
| `release_vercel.py` | Kind `vercel` (tier `full`): the Vercel REST API, the Deployment Check's commit status, and the checks of a Vercel deployment. See [Vercel](#vercel). |
| `release_railway.py` | Kind `railway` (tiers `full` and `check_rollback`): Railway's GraphQL API with one project token per environment, the log and HTTP log reads, the migration rule, the rendered builds. See [Railway](#railway). |
| `release_static.py` | Kind `static_site` (tier `check_only`): the publisher's check run, the read-only page and link checks, the revert command, the rendered builds. See [Static sites](#static-sites). |
| `release_kit.py` | `check` (also the prod build contract below) and `render`. |
| `run_offline_checks.py` | Runs the kit tests offline: `KUBECONFIG=/dev/null`, an empty `CLOUDSDK_CONFIG`, `GOOGLE_APPLICATION_CREDENTIALS=/dev/null`, blocked live CLIs, Slack off. Every repository's release-kit workflow runs `python release/run_offline_checks.py -- python -m unittest discover -s release -p 'test_*.py'`. |
| `release_people.json` | GitHub login to Slack id, for the author ping. Private repositories only: `sync.py` leaves it out of a public repository (and deletes it there), `release_kit.py check` refuses it when the GitHub event says the repository is public, and a public repository's rendered builds read the map from the Secret Manager secret `release-people-json` as `RELEASE_PEOPLE_JSON`. A missing or malformed map mentions nobody; the alert still goes out. Logs show `<@...>`, never a Slack id. |
| `release_cases.py`, `kit_cases/` | The release cases: past release incidents, replayed offline against the kit's decision code. See [Release cases](#release-cases). |

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
- `environments.<env>.domains` (GKE, optional): the host names the environment
  serves, for the release record. The kit adds the hosts of the `http_public` smoke
  checks itself; name a host here only when no public smoke check calls it.

## Orbit Ops

Every release record names where the release runs, so Orbit Ops (SMP `/ops`) finds
the service without a hand-written list. The kit takes each value from what the run
already knows: the build's GitHub repository, the platform and target of the deploy
step, and the hosts the service config names. A value the kit does not know is left
out, never `null`. Records written before kit 2026-10-28 have none of the three.

```json
"repo": "orbit-search/orbit-search-web",
"deploy_target": {"platform": "gke", "project": "orbit-search", "region": "us-central1", "cluster": "prod",
                  "namespace": "orbit-search-web", "workload": "prod-orbit-search-web-deployment"},
"domains": ["orbitsearch.com"]
```

| Kind | `deploy_target` | `domains` |
|---|---|---|
| `gke` | `project`, `region`, `cluster`, `namespace`, `workload` (the first Deployment; `workloads` when there are more) | `environments.<env>.domains`, then the `http_public` smoke hosts |
| `cloud_run` | `project`, `region`, `workload` (the first service; `workloads` when there are more). The dev stage names the same services. | prod: the `http_public` smoke hosts; dev: none |
| `composer` | `project`, `region`, `workload` (the environment) | none |
| `vercel` | `vercel_project` | prod: `vercel.domains`; dev: none |
| `railway` | `railway_project`, `railway_service` | the environment's `url` host |
| `static_site` | platform `mintlify` only | the `static_site.url` host |

**The rule for agents.** A service that ships with the kit appears on Orbit Ops
after its first release. Add something by hand only for call lines, a display label,
or a platform that discovery does not cover yet. Put it in SMP's
`lib/ops-overrides.ts`, and check it on `/ops`.

## Release cases

A release case freezes the inputs of one release-check decision that a past
release needed, and the decision it should make. `release_cases.py replay`
runs the kit's real decision code (`release_verify.evaluate`) on those inputs.
Fakes replace kubectl, HTTP, Datadog, Cloud Logging, GitHub, the record store
and the clock, so a replay is offline and takes no real time. A case fails when
its decision differs from `expected`.

```bash
python3 release/run_offline_checks.py -- python3 release/release_cases.py replay [--case <id>] [-v]
```

Two folders are replayed:

- `release/kit_cases/`: the shared cases. They ship with the kit (`KIT.json`
  pins them), so every repository replays the incidents of every kit service.
  Add a shared case in orbit-infrastructure, then sync.
- `release/cases/`: the repository's own cases. Sync never touches them.

`test_release_cases.py` runs the replay, so the release-kit workflow of every
repository (`unittest discover -s release`) fails on a case that today's code
gets wrong. No workflow change is needed.

A case (`<id>.json`, the id is the file name):

| Field | What it holds |
|---|---|
| `kind` | `checkpoint` (one deploy or window check of a GKE kit service) or `rule` (a rule file must keep a line that matches `inputs.pattern`). |
| `service` | The service config as it was at the incident. |
| `decision` | `phase` (`deploy` or `window`), `now` (the time of the check), `checkpoint_minutes` (a window check's checkpoint, for the reader). |
| `record` | The release record as the check read it (status `deployed`, no verdict). Keep no author names or e-mail addresses. |
| `inputs` | What the check observed. `workloads`: snapshots in time (a read gets the newest one at or before the replay's time; replay never invents a later one). `pods` and `http`: the pods and the HTTP answers of the smoke checks, in time (`after_seconds`). `rollout_failures`, `prod_changes` (builds that changed prod during the observation), `store` (other release records), `answers` (Datadog, Cloud Logging and GitHub reads by key). A read that the case does not hold fails it with `missing read: <key>`: add that key with the answer the source gave. |
| `expected` | `verdict`, `rolls_back`, the status of named `checks`, `regressions_include`, `regressions_exclude`, `warnings_include`. |
| `at_the_time` | The decision of the code at the time. |
| `fixed_by` | The PR that fixed the decision. |
| `known_open` | Optional: why today's code still gets the case wrong. Replay reports it and passes; once the code gets it right, replay fails until the fixing PR drops the field. |

Every case must fail on the kit before its fix and pass after it. Show this in
the PR that adds the case: copy `release_cases.py` and the cases into an
export of the older kit (`git archive <commit> release-kit/release`) and run
the replay there. A case of a Cloud Run, Composer or Vercel service fails
with "no checkpoint replay yet": add that replay with the first such case.

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

## Capacity is environmental

A deploy that a full cluster slows down is not the release's fault (Nicholas, 10-26). Two releases rolled
back on 10-25 for capacity: orbit-api at 4/8 (new pods killed by the liveness probe while they loaded
their models) and deep-search at 63/64 (one pod waited for a node; the workloads of the later steps were
then reported as 11 image regressions). The same rules run in deep-search and orbit-api.

**Rule 1, the capacity verdict** (`release_capacity.classify`, a pure function over kubectl JSON). The
deploy phase of `release_verify.py` judges each Deployment whose rollout did not finish, or whose new pods
are not Ready. The verdict is `capacity` only when all four hold:

1. Every pod of the new ReplicaSet, and its template, uses the release digest.
2. No new pod shows an application failure: OOMKilled, an image pull or container config error, a run
   that a probe did not end, or a crash whose last 50 log lines show a Python traceback, a Node stack or an
   `Error:` line. Every run the kubelet reports counts, the current one first. A probe ended a run when it
   exited 137 or 143 with a probe event inside the run, or exited 0 or 139 (a container that handles SIGTERM
   itself, like the AI worker) within its grace period after the kubelet's Killing event for the failed
   probe (a failed probe alone sends no signal). The logs of up to 10 crashed
   pods are read; a crash whose logs were not read, or could not be read, fails this condition.
3. Of the new pods that ran, at least half are Ready, or every new pod that is not Ready waits for a node
   or was killed by a probe while starting. A pod that a probe killed and that is Ready again is Ready.
   "While starting": every killed run lasted no longer than its startup or liveness budget plus 120 s; a
   longer run that a probe killed, on a pod that is not Ready, is not explained.
4. A capacity indicator: a new pod Pending with FailedScheduling; a new pod preempted or evicted; an HPA at
   its maximum anywhere in the cluster (not one pinned at min = max); median node CPU requested >= 90%, or
   3 nodes at >= 95%; probe kills on the old ReplicaSet, or in 2 other namespaces, in the last 2 hours.

A failed step that the record names (`failed_step`) is never capacity: `rollout-wait` names a step only
after a verdict that was not capacity, and a failed apply or script names its own step. A build that passes
`--failed-step-file` to `finish` names every failed step (the record says `deploy_steps_named`), so its
`deploy_failed` without a step is never capacity either. Only an older build that wrote `deploy_failed`
without a step, and without that flag, has every Deployment on the release judged.

Every failure in a deploy step writes `/workspace/deploy_failed_step` before `deploy_failed` (keep the file
that `rollout-wait` wrote):

```bash
deploy_step_failed() {
  [ -s /workspace/deploy_failed_step ] || printf '{"id":"%s"}\n' "$1" > /workspace/deploy_failed_step
  touch /workspace/deploy_failed
}
kubectl apply -f /workspace/k8-prod-rendered/ || { deploy_step_failed deploy-prod; exit 1; }
```

Dev verify passes only on a passed result. A dev rollout that capacity still holds back after the extended
wait is `inconclusive`: the commit is not verified, prod does not promote it, and Slack gets one line.

Capacity: the availability checks of those Deployments (images, rollouts, deploy, their `pods_ready`
checks) get status `environmental`. No rollback, no suspected PRs, no hold, no author ping; one line goes to
the deploy channel. The check then waits once more (Rule 3 timeout x2, counted from the deploy start, at
least 2 min) and judges again. This wait polls the Deployment (`poll_rollouts`, each read bounded by the
time left), because `kubectl rollout status` fails at once after the progress deadline. Finished: the deploy phase passes. Still incomplete with capacity evidence:
the verdict is `inconclusive` with cause `capacity`, and the +15/+60 checks judge the running release as
usual. Every other failure (an http check on Ready pods, a wrong image, a traceback) keeps the rollback.

On 10-26 the cluster met indicator 4 all the time (node CPU requested: median 91-94%, 13-17 nodes at
>= 95%; two HPAs at max). Conditions 1 to 3 are what separate a slow start from a broken release.

**Rule 2, one finding.** When a deploy step failed (record status `deploy_failed`, or a recorded
`failed_step`), the Deployments that still run the previous release are one finding, `deploy incomplete at
step <id>: N workloads not updated (names)`, not one image regression each. It follows Rule 1.

**Rule 3, rollout waits scale with replicas.** `timeout = min(3 h, max(900 s, waves x per_wave + 300 s))`,
with `waves = ceil(replicas / max(1, maxSurge + maxUnavailable))` (percentages resolved like Kubernetes:
surge rounds up, unavailable down) and `per_wave = terminationGracePeriodSeconds + max(120 s, startupProbe
or readinessProbe initialDelay + period x failureThreshold)`. `release_verify.py --wait-rollouts-seconds`
uses it as a floor per Deployment (less the time the deploy already spent), and so does `rollout-wait`:

| Deployment | Replicas | Strategy and probes | Waves | Timeout |
|---|---|---|---|---|
| prod-orbit-api | 8 | surge 25%, unavailable 0; grace 60 s; readiness 20 + 5 x 1 | 4 of 2 | 1,020 s |
| entity-backfill worker | 64 | surge 0, unavailable 25%; grace 600 s; no probes | 4 of 16 | 3,180 s |
| prod-ai-worker | 128 | surge 25%, unavailable 0; grace 60 s; startup 5 x 36 | 4 of 32 | 1,260 s |
| one pod per wave (surge 1) | 8 / 64 / 128 | grace 30 s; no probes | 8 / 64 / 128 | 1,500 / 9,900 / 10,800 s (cap) |

**The prod build** (GKE): the deploy step waits with `rollout-wait` instead of a fixed
`kubectl rollout status --timeout`. On a timeout it runs the classifier: capacity polls once more for the scaled
timeout and judges again; still capacity (or finished) exits 0, so the step writes no `deploy_failed` and
the later steps deploy the rest of the release. Anything else writes the failed step and exits 1. Pass the failed step to `finish`, so Rule 2 names it:

```bash
kubectl apply -f /workspace/k8-prod-rendered/ || { touch /workspace/deploy_failed; exit 1; }
python3 release/release_capacity.py --env prod rollout-wait --step-id deploy-prod \
  --image-file /workspace/release_k8s_image --failed-step-file /workspace/deploy_failed_step \
  || { touch /workspace/deploy_failed; exit 1; }
# release-record-finish:
python3 release/release_record.py finish --env prod --release-id-file /workspace/release_id \
  --status "$${STATUS}" --failed-step-file /workspace/deploy_failed_step --attempts 3
```

`python3 release/release_capacity.py --service S timeout` prints each Deployment's timeout;
`classify --image <repo@digest>` prints the verdict of what runs now (read only).

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
needs one, so dev verify can call it on the tag URL. A Cloud Run or Composer
service with its own build identity names it in `build_service_account`
(`<name>@<project>.iam.gserviceaccount.com`). The rendered window and rollback
builds then run as that identity, not as `cloud-build@`. The field changes only
the rendered build files, never a trigger: an operator creates the service's dev,
prod and rollback triggers with that same identity (the human setup script does),
and the kit starts rollbacks through that existing trigger. Composer takes
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

A release that no agent claimed (a person merged it) is owned by its PR author:
the watch records `followup.owner` `{"state": "author", "login", "pr"}` and posts
nothing about it. The author is the author of the PR that GitHub links to the
release commit (`head_pr`, read when the record is written), else the newest PR in
the release's commits. The checks and the automatic rollback run as usual, and the
watch ends green on a pass. Only an alert that already goes out for a failed or
inconclusive +15/+60 check (or a check that could not run), and the failed-release
alerts (rollback, Docs, Railway forward hold), mention the author,
resolved through the people map (`release_people.json`, or `RELEASE_PEOPLE_JSON`
in public repositories); an author missing from the map gets the map's
`fallback` people, also when a suspected PR's author is in the map (the
failed-release alerts mention the mapped suspects and the owner). A capacity
verdict mentions nobody. A later agent claim
replaces the author.

A manual `check --phase window` before a checkpoint is due (an early check, or
`--checkpoint-minutes` ahead of its due time) completes no checkpoint. Its result
goes to `followup.early_checks` and the watch still runs every scheduled
checkpoint. Only a failure from it sets the verdict; `--final` does not apply.
A late repair of an older checkpoint, also with `--final`, never replaces a later
terminal verdict unless it finds a failure. Only a replaced release (a newer
record or a rollback) supersedes open checkpoints; while another build only runs,
the check records nothing and its checkpoint stays open.
A manual check only records and alerts. When it completes a checkpoint with a
failed verdict while the watch runs, the watch starts the rollback itself (or
pings the author, by tier): at the next checkpoint it skips, and after its own
checks of the checkpoint it is on (they passed, but the saved verdict is failed).

### Prod build contract

`release_kit.py check` reads each prod build file (`cloudbuild*.yaml` with a prod
`release-record-begin` step, and the rendered Vercel promote) and requires:

- `release-record-begin` defines `release_no_change`, which writes
  `{"release_deploy":"skipped_before_mutation"}` to `$BUILDER_OUTPUT/output`, and
  calls it on the skip path (`deploy_failed`, `deploy_superseded`,
  `promote_blocked`: a failed preflight or render, a gate skip, a blocked
  promote) and when `begin` fails. `notify-promote-blocked` writes the receipt
  before it ends the build. A build without the receipt counts as a prod change
  and stops the window watch of the release before it (`builds_since_rollback`).
- `release-record-finish` runs `finish --env prod --attempts 3` and has
  `WEBHOOK_ORBIT`. After the deploy changed prod, a finish that still fails posts
  the recovery commands in order (finish the record, `check --phase deploy`,
  `schedule-window`) and the build ends red. Nothing below the finish runs on an
  unfinished record.
  The finish refuses (and changes nothing) once a newer release or rollback is
  latest, so a late recovery never hides it.
  When the build passed `--failed-step-file`, the recovery finish command carries
  the step itself (`--failed-step '<json>'`, or `''` when no step was named), so a
  hand finish after the build ended still records `failed_step` and
  `deploy_steps_named`.

Cloud Run/Composer use the same ownership fields through
`python3 release/release_light.py --service <service> own --env prod --release-id <id> --thread-url <chat URL> --pr-url <PR URL>`.

## Vercel

Kind `vercel` serves a Vercel project (one kit service per project; a
monorepo with several projects has one config per project). It is tier `full`
only. `release_light.py` drives it with `release_vercel.py`; the gate, the
records, the hold, the freeze, the rollback build and the author ping are the
same code as for the other kinds.

Vercel builds the production deployment of a merge itself. A required
Deployment Check (Settings > Build and Deployment > Deployment Checks) on the
GitHub commit status `orbit-release/<service>` keeps the production domains on
the old deployment until the kit posts `success`. So the kit builds nothing:

1. The dev build (push trigger, `release/services/<service>.release.yaml`)
   posts the status `pending`, waits for Vercel's production deployment of the
   commit (`vercel-wait`), and writes a dev record with the deployment id.
   A build that Vercel skipped (Ignored Build Step: the app is not affected)
   ends here with the status `success` "not affected".
2. Dev verify on the deployment's own URL (`https://<deployment>.vercel.app`)
   with the Protection Bypass for Automation header: the deployment is READY,
   of this commit, and a production deployment; each smoke check passes on the
   same path; and its request logs show no error line and no 5xx during the
   checks. The URL serves prod env vars and prod data, so smoke checks must be
   read-only GETs.
3. `release_gate.py request-promote` starts the prod build
   (`<service>.promote.yaml`) or records why not. `vercel-status
   --from-dev-index` sets the status: `pending` while the prod build runs or a
   freeze or hold waits, `failure` for a failed dev verify or a stale commit.
   A re-run for the commit that prod already serves keeps `success`.
4. The prod build: single flight, on the release branch, the dev build passed;
   Vercel does not hold the project after a rollback the kit did not record;
   the deployment is still READY and no newer production deployment of
   another commit on the branch exists (Vercel gives the domains to the
   newest); `check-prod` under the prod lock (freeze, hold, newest verified
   commit, the same deployment as dev verify).
5. `apply` posts `success`; Vercel assigns the domains to exactly that
   deployment (no rebuild). When Vercel holds the project after a rollback
   (auto-assignment off), or the domains did not move within
   `assign_wait_seconds`, the kit promotes the deployment by API. A refused
   status stops the promote: the kit never promotes around the check.
6. The deploy check: every configured domain points at the deployment; the
   smoke checks pass on the domains. Then the watch window (+15, +60).

Window checks read Vercel's request logs by deployment id, so requests that
Skew Protection still sends to an older deployment never count against the
release (control by version):

- New error log lines: sites (`release_signals.py` patterns) of the release's
  deployment that no production deployment wrote in the 24 h baseline. A
  baseline read that hits its page limit judges nothing. A new line fails a
  release only with a clear suspect, as for every kind.
- The 5xx share against the previous deployment over the same length of time
  before the release: above `max_5xx_ratio` times and `max_5xx_absolute` over
  it, with `min_requests` and `min_5xx`. It fails a release only when the
  release changed runtime code; without a baseline it only warns.
- The smoke checks again, only after every domain is confirmed to serve the
  release. A domain that serves another deployment (a human rollback in the
  dashboard) makes the checkpoint inconclusive: nothing fails and nothing
  rolls back. The automatic rollback build checks the domains again and
  refuses when they moved.
- A 5xx read that failed or stopped at its page limit, or a moved domain,
  leaves the final verdict inconclusive with `unchecked: true`; the next
  release's watch judges it again. A capped baseline computes no share.
- A metric or smoke failure judged over several unchecked releases rolls back
  to what ran before the oldest of them (`previous-of:<oldest>`), as for GKE.

A failed release rolls back on its own: the rollback build points the domains
back at the deployment that served before the release
(`POST /v9/projects/<id>/rollback/<dpl>`, an alias switch in seconds), turns
the status of every commit it removes to `failure` (a redeploy of one must not go
live), and writes the rollback record with the hold. Vercel then turns
auto-assignment off: its own hold. The next promote that the kit's hold lets
through (a revert, `Fixes-release:`, or `release_gate.py resume`) promotes by
API, which turns auto-assignment on again.

Config:

```json
{
  "service": "neighborlookup", "kind": "vercel", "tier": "full",
  "github_repo": "orbit-search/nebula", "connected_repository": "orbit-search-nebula",
  "vercel": {"team": "team_...", "project": "neighborlookup",
             "domains": ["nebula-neighborlookup.vercel.app"]},
  "triggers": {"dev": ["neighborlookup-release"], "prod": ["neighborlookup-promote"],
               "rollback": "neighborlookup-rollback"},
  "smoke": [{"type": "http_public", "name": "home", "url": "https://nebula-neighborlookup.vercel.app/"}],
  "verify": {"checkpoints": [15, 60], "min_requests": 50, "min_5xx": 5,
             "max_5xx_ratio": 3, "max_5xx_absolute": 0.02, "log_min_count": 3},
  "code": {"source_prefixes": ["apps/neighborlookup/"], "extensions": [".ts", ".tsx"]}
}
```

`vercel` also takes `check_context` (default `orbit-release/<service>`),
`token_secret` (default `vercel-release-token`), `github_token_secret`
(default `connection`), `deployment_wait_minutes`
(30), `assign_wait_seconds` (180) and `log_settle_seconds` (30). Every smoke
URL must be `https://<a configured domain>/...`. `release_kit.py render`
writes all four builds: `<service>.release.yaml` (dev, push trigger),
`<service>.promote.yaml` (prod, manual trigger), `<service>.release-verify.yaml`
(window) and `<service>.rollback.yaml`.

Credentials: the builds read two Secret Manager secrets:
`slack-webhook-workspace-orbit` and the team-scoped Vercel token
(`vercel-release-token`, env `VERCEL_RELEASE_TOKEN`). GitHub needs no
secret: reads use the read token of the Cloud Build connection (as for every
kind), and each commit status uses a read-write token of the connection's
GitHub App (Google Cloud Build: commit statuses write), minted for that status
and never stored. So the repository must be linked in the connection
(`connected_repository`), and the build identity needs
`roles/cloudbuild.tokenAccessor` on the connection. A `github_token_secret`
other than `connection` names a Secret Manager secret that the builds put in
`GITHUB_TOKEN` instead (a token that reads the repository and writes commit
statuses). On a laptop `release_vercel.py` reads the Vercel token from Secret
Manager with the gcloud identity, and `GITHUB_TOKEN`, when set, is used for
the status. No token is printed or put on a command line.

What differs from GKE:

- The build is Vercel's, made on push; Cloud Build only waits, verifies,
  gates and lets it go live. Vercel may build several commits at once; single
  flight is the prod lock plus the newest-deployment rule.
- The dev stage is the production deployment on its own URL: prod env vars,
  prod data and prod backends. Cookie-domain auth flows do not work there.
- Same artifact: exact. Vercel assigns the domains to the deployment that dev
  verify checked; nothing rebuilds it. A redeploy or deploy hook of an already
  passed commit builds again and goes live through the passed status.
- Rollback is an alias switch in seconds, with the target's old env vars.
  Vercel keeps production deployments for 30 days (the last 10 longer); an
  expired target blocks the rollback plan.
- Signals: Vercel request logs and smoke checks only (no Datadog or Cloud
  Logging). Low traffic makes the 5xx rule weak, so smoke checks and new error
  lines carry most of the check.

## Railway

Kind `railway` serves one Railway service. Railway builds and runs it; its
GitHub auto deploy is off (Service > Settings > Source, a human setting), so the
kit's builds are the only thing that deploys. The service stays connected to
its GitHub repository: `serviceInstanceDeployV2` deploys one commit of it.

| | Tier `full` | Tier `check_rollback` |
|---|---|---|
| Merge build (`<service>.release.yaml`, push trigger) | Deploys the commit to the dev environment (staging), dev verify, `release_gate.py request-promote`. | Single flight, `check-direct` (freeze, hold), deploys the commit to production. |
| Prod build (`<service>.promote.yaml`, manual) | `preflight`, `check-prod`, `still-current`, deploys the commit to production. | none |
| Dev verify | The staging deployment is SUCCESS, of the commit, and serves staging; Railway auto deploy is off there; the smoke checks pass on the staging URL; the version field names the commit; no error log line and no 5xx while the checks run. | none |

Then, for both: the release record, the deploy check (production serves a new
SUCCESS deployment of the commit, the smoke checks, the version field) and the
watch window (+15, +60): the release deployment's status (CRASHED or FAILED
fails), new error log lines (`@level:error`) against the deployments that
served in the 24 h baseline, the 5xx share of its HTTP logs against the
previous deployment over the same length of time, and the smoke checks. The
reads filter by deployment id (control by version). When production serves a
deployment the kit did not make (a `railway up`, a dashboard redeploy),
nothing is judged.

A failed release redeploys the deployment that served before
(`deploymentRedeploy` with `usePreviousImageTag`: its image, no rebuild, the
current variables; a target whose image is gone is rebuilt from its commit)
and holds. The rollback refuses when production serves a deployment the kit
did not record.

Forward-only: with `railway.migrations` (`prefix`: a folder, a file or a list
of them; `suffix`: optional, such as `.up.sql`), `begin` records whether the
commits since the last release add or change such a file (an unknown range
counts as one). Such a release never rolls back by itself: an older build does not
start against a newer schema. It holds instead: its own record gets
`promote_hold` with `forward_only`, `release_gate.py` holds every later deploy,
and the author gets an `on hold` line with a ping (the prompt is in the build log). Only a commit with the trailer
`Fixes-release: <release id>` or `release_gate.py resume` ends that hold; a
revert does not (the database keeps the migration). A manual rollback across
such a range is blocked too.

```json
{
  "service": "deepfind-api", "kind": "railway", "tier": "full",
  "github_repo": "orbit-search/deepfind", "connected_repository": "orbit-search-deepfind",
  "railway": {
    "project_id": "<uuid>", "service_id": "<uuid>",
    "environments": {
      "dev": {"name": "staging", "environment_id": "<uuid>", "token_secret": "railway-token-deepfind-staging",
              "url": "https://deepfind-api-staging.up.railway.app"},
      "prod": {"name": "production", "environment_id": "<uuid>", "token_secret": "railway-token-deepfind-production",
               "url": "https://api.deepfindpeople.com"}
    },
    "migrations": {"prefix": "backend/migrations/", "suffix": ".up.sql"},
    "version": {"path": "/health", "field": "version"}
  },
  "triggers": {"dev": ["deepfind-api-release"], "prod": ["deepfind-api-promote"], "rollback": "deepfind-api-rollback"},
  "smoke": [{"type": "http_public", "name": "health", "url": "https://api.deepfindpeople.com/health"}],
  "verify": {"checkpoints": [15, 60], "min_requests": 50, "min_5xx": 5},
  "code": {"source_prefixes": ["backend/"], "extensions": [".go", ".sql"]}
}
```

Tier `check_rollback` names only `environments.prod` and `triggers.prod`.
`railway` also takes `deploy_wait_minutes` (20) and `log_settle_seconds` (30).
Every smoke URL must be under the prod URL (dev verify calls the same path on
the dev URL).

A deploy or redeploy is sent once. When its answer is lost (a timeout or a 5xx),
the kit looks for the new deployment Railway lists and adopts it, or stops;
it never sends the mutation again. A rollback adopts only a deployment of the
target's image (a rebuild of the same commit is not the rollback); a cancel is
sent again after a lost answer. Dev verify fails when Railway's auto deploy
setting cannot be read.

Credentials: one Railway project token per environment (scoped to that one
environment) in Secret Manager, read into `RAILWAY_TOKEN_DEV` and
`RAILWAY_TOKEN_PROD`; a build reads only the environments it touches. Before
every deploy or redeploy the kit asks the token which project and environment
it belongs to and refuses a mismatch. Tokens are never printed.

What differs from GKE:

- Same source, not the same bytes: Railway builds the commit again for
  production. The records keep both image digests.
- The dev stage is a Railway environment with its own variables and database;
  it is only as close to production as its variables are.
- Each deploy and rollback is Railway's own (its health check keeps the old
  deployment when the new one fails). Signals are Railway's logs and HTTP logs,
  not Datadog or Cloud Logging.
- A release with a database migration holds instead of rolling back.

## Static sites

Kind `static_site` (tier `check_only`) serves a site that a hosted publisher
builds and publishes on merge (Mintlify for `orbit-search/docs`). There is no
staged build and no rollback API, so the kit watches and pings:

1. The merge build (`<service>.release.yaml`, push trigger) waits for the
   publisher's GitHub check run of the commit (`static_site.publisher_check`).
   Skipped or neutral: nothing was published, nothing to check. Failure or no
   run in `publisher_wait_minutes`: nothing went live; the author is pinged.
2. The release record in the kit's store layout.
3. Page checks at +0, +15 and +60 min (read-only GETs with a cache-busting
   query): the sitemap answers and lists no fewer pages than the previous
   release minus the pages this release removed; every sitemap page and every
   `required_paths` entry answers 200; each page the release changed answers
   and carries its frontmatter title (a warning at +0, a failure later); the
   internal links on the changed pages resolve. External links are not checked.
4. A failed check pings the PR author in Slack (a red "release failed" line); the build log has the exact revert
   command (`git revert` of the release's commits into a PR) and a fix prompt.
   Nothing reverts by itself: a bot must not author commits.

```json
{
  "service": "docs", "kind": "static_site", "tier": "check_only",
  "github_repo": "orbit-search/docs", "connected_repository": "orbit-search-docs",
  "deploy_triggers": ["docs-release"],
  "static_site": {"url": "https://docs.orbitsearch.com", "publisher_check": "Mintlify Deployment",
                  "required_paths": ["/llms.txt"]},
  "smoke": [{"type": "http_public", "name": "home", "url": "https://docs.orbitsearch.com/"}],
  "code": {"source_prefixes": ["api/", "concepts/", "guides/"], "extensions": [".mdx"]}
}
```

The builds read GitHub (contents, commits, the publisher's check runs) with a
read token of the service's Cloud Build connection link (`connected_repository`),
like the GKE kinds; the build identity needs `roles/cloudbuild.readTokenAccessor`
on the connection. No GitHub secret is needed (`github_token_secret` defaults to
`connection`, as for kind `vercel`). Any other value names a secret that the builds
put in `GITHUB_TOKEN`.
