# Hosted runtime image evidence

`../workflows/runtime-image.yml` adds two PR-only, read-only-token jobs without
changing the application tests, policy, release kit or release pipeline. Each
checks out the exact PR head (including drafts), exports only tracked Dockerfile,
.dockerignore, manifests, tsconfig and src blobs to a fresh RUNNER_TEMP context,
and builds the final linux/amd64 image. No local environment, dependencies,
credentials or repository Git metadata enter that context. Receipts are outside
both the checkout and the context.

The pinned base index and amd64 manifest, source file SHA256s, Docker/buildx
versions, build logs/metadata and final image config ID are retained. Verification
binds the base manifest hashes, source revision, build config digest, inspected
image and Trivy ImageID/revision. The image config ID is not a registry manifest
digest. These ephemeral candidates are never pushed or deployed.

Trivy 0.76.1 is digest pinned; the scan explicitly includes OS and library
packages, every severity, unfixed findings and the complete package inventory.
It reads no repository Trivy config/ignore file, uses no suppression controls,
and retains raw JSON/logs plus scanner/database version metadata. Scan exit zero
means collection completed; HIGH/CRITICAL or other findings still require the
parent's residual review. Summaries show severity counts and point to the full
scan artifact. Do not treat this job as a vulnerability clearance gate.

The unmodified default Node HTTP entrypoint is started with `--network none`,
no published port, capabilities dropped and bounded resources. Its only config
is synthetic PORT=8080 and a loopback discard API URL. `docker exec` probes only
`http://127.0.0.1:8080/health` and requires HTTP200 with exactly `{"status":"ok"}`.
This constant route is defined in both HTTP apps and documented in
`docs/RELEASE_PIPELINE.md`; it does not construct MCP clients or call tools.
No provider, database or production request is authorized. Startup is bounded
by a 65-second exec timeout and a 30-minute job timeout.

Raw receipts/logs upload even when verification fails; missing/invalid artifacts
fail the job. The summary also records incomplete verification. The final image
archive is deliberately not uploaded. PR41 stays draft until parent review of
actual hosted receipts, current checks and Greptile findings.

Lightweight local verification (no Docker or dependencies):

```
python3 -m unittest discover -s .github/image-evidence -p 'test_*.py'
/tmp/orbit-cve-remediation-20261110/workflow-tools/actionlint .github/workflows/runtime-image.yml
```
