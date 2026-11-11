"""Prepare credential-free build contexts and verify hosted runtime image receipts."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def read(directory, name):
    """Require a nonempty, valid JSON receipt."""
    return json.loads((directory / name).read_text())


def require(condition, message):
    """Fail closed when evidence is missing or describes another image."""
    if not condition:
        raise ValueError(message)


def digest_matches(data, expected):
    """Accept the blob or the single display newline added by imagetools --raw."""
    return any("sha256:" + hashlib.sha256(blob).hexdigest() == expected
               for blob in (data, data.removesuffix(b"\n")))


def build_identity(metadata, inspected):
    """Require a config ID or both matching descriptors for manifest-backed IDs."""
    image_id = inspected["Id"]
    exported = metadata.get("containerimage.digest")
    require(isinstance(exported, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", exported), "Invalid build export digest")
    if "containerimage.config.digest" in metadata:
        require(metadata["containerimage.config.digest"] == image_id, "Build config differs from inspected image")
        return "config"
    descriptor = metadata.get("containerimage.descriptor") or {}
    actual = inspected.get("Descriptor") or {}
    media_type = descriptor.get("mediaType")
    require(media_type in ("application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"), "Missing supported manifest descriptor")
    require(exported == descriptor.get("digest") == actual.get("digest") == image_id, "Build manifest differs from inspected image")
    require(actual.get("mediaType") == media_type and actual.get("size") == descriptor.get("size") and descriptor.get("size", 0) > 0, "Manifest descriptor mismatch")
    require(descriptor.get("platform") == {"architecture": "amd64", "os": "linux"}, "Build manifest platform mismatch")
    return "manifest"


def prepare(directory, context, package, sha):
    """Export only tracked production inputs from the exact checked-out commit."""
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid source SHA")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    require(head == sha, "Checkout differs from requested PR head")
    require(package in ("mcp-server", "profile-server"), "Unexpected package")
    require(not context.exists(), "Context must be new")
    context.mkdir(parents=True)
    prefix = f"packages/{package}/"
    paths = [prefix + item for item in
             ("Dockerfile", ".dockerignore", "package.json", "package-lock.json", "tsconfig.json", "src")]
    files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", sha, "--", *paths], text=True).splitlines()
    hashes = {}
    for path in files:
        # Git blobs, never working-tree files, so ignored/local secrets cannot enter.
        relative = Path(path.removeprefix(prefix))
        require(relative.parts[0] in {"Dockerfile", ".dockerignore", "package.json", "package-lock.json", "tsconfig.json", "src"}, "Unexpected input")
        mode = subprocess.check_output(["git", "ls-tree", sha, "--", path], text=True).split()[0]
        require(mode in ("100644", "100755"), "Symlink/submodule forbidden in context")
        data = subprocess.check_output(["git", "show", f"{sha}:{path}"])
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == "100755" else 0o644)
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    bases = re.findall(r"^FROM\s+(\S+)", (context / "Dockerfile").read_text(), re.M)
    require(bases and len(set(bases)) == 1, "Expected same pinned base in both stages")
    require(re.fullmatch(r".+@sha256:[0-9a-f]{64}", bases[0]), "Base must be digest pinned")
    source = {"sha": sha, "package": package, "files_sha256": hashes, "base_reference": bases[0]}
    (directory / "source.json").write_text(json.dumps(source, indent=2) + "\n")
    with open(os.environ["GITHUB_ENV"], "a") as env:
        env.write(f"BASE_REFERENCE={bases[0]}\n")


def verify(directory, sha, package):
    """Bind source, base manifest, build config, scan and isolated health evidence."""
    source = read(directory, "source.json")
    require((source["sha"], source["package"]) == (sha, package), "Source mismatch")
    base_bytes = (directory / "base-index.json").read_bytes()
    require(digest_matches(base_bytes, source["base_reference"].split("@")[1]), "Base index digest mismatch")
    index = json.loads(base_bytes)
    manifests = [m for m in index["manifests"] if m.get("platform", {}).get("os") == "linux" and m.get("platform", {}).get("architecture") == "amd64"]
    require(len(manifests) == 1, "Missing/ambiguous amd64 base manifest")
    base_manifest = (directory / "base-amd64.json").read_bytes()
    require(digest_matches(base_manifest, manifests[0]["digest"]), "Base platform digest mismatch")
    image = read(directory, "image.json")
    require((image["os"], image["architecture"], image["revision"]) == ("linux", "amd64", sha), "Image platform/revision mismatch")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image["id"]), "Invalid Docker image ID")
    inspected = read(directory, "image-inspect.json")[0]
    require((inspected["Id"], inspected["Os"], inspected["Architecture"], inspected["Config"]["Labels"]["org.opencontainers.image.revision"]) == (image["id"], image["os"], image["architecture"], sha), "Raw/summary inspection mismatch")
    metadata = read(directory, "build-metadata.json")
    identity_kind = build_identity(metadata, inspected)
    scan = read(directory, "scan.json")
    require(scan["ArtifactType"] == "container_image", "Wrong scan artifact type")
    require(scan["Metadata"]["ImageID"] == image["id"], "Scan image differs from built image")
    require(scan["Metadata"]["ImageConfig"]["config"]["Labels"]["org.opencontainers.image.revision"] == sha, "Scan revision mismatch")
    require({"os-pkgs", "lang-pkgs"} <= {r["Class"] for r in scan["Results"]}, "Missing OS/library coverage")
    for kind in ("os-pkgs", "lang-pkgs"):
        require(any(r.get("Packages") for r in scan["Results"] if r["Class"] == kind), "Missing package inventory: " + kind)
    scanner = read(directory, "scanner-version.json")
    require(scanner["Version"] == "0.76.1" and scanner["VulnerabilityDB"]["UpdatedAt"], "Scanner/DB identity missing")
    startup = read(directory, "startup.json")
    container = read(directory, "container.json")
    require(startup == {"path": "/health", "status": 200, "body": {"status": "ok"}}, "Health probe failed")
    require(container["image"] == image["id"] and container["network"] == "none" and container["running"] is True, "Startup image/isolation mismatch")
    findings = [v for result in scan["Results"] for v in result.get("Vulnerabilities", []) or []]
    counts = dict(collections.Counter(v["Severity"] for v in findings))
    receipt = {"source": source, "image": image, "base_amd64_manifest_digest": manifests[0]["digest"],
               "build_export_digest": metadata["containerimage.digest"], "image_identity_kind": identity_kind, "finding_counts": counts,
               "note": "Local Docker image ID kind is checked explicitly; this is not a published registry receipt. Candidate never published. Scan completion is not CVE clearance; parent residual review required."}
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    directory = Path(os.environ["IMAGE_EVIDENCE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == "prepare":
        prepare(directory, Path(os.environ["IMAGE_CONTEXT"]), os.environ["PACKAGE"], os.environ["SOURCE_SHA"])
    else:
        try:
            receipt = verify(directory, os.environ["SOURCE_SHA"], os.environ["PACKAGE"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            (directory / "verification-error.txt").write_text(f"{type(error).__name__}: {error}\n")
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                summary.write("Runtime image evidence INCOMPLETE/INVALID. See raw receipts; no image clearance.\n")
            raise
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("### Runtime image evidence: " + os.environ["PACKAGE"] + "\n\n```json\n" + json.dumps(receipt, indent=2) + "\n```\n\nAll findings retained in scan.json; no CVE clearance. Draft remains for parent residual review.\n")
