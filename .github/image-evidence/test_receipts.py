"""Disposable evidence fixtures exercise identity mismatches and missing receipts."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from receipts import prepare, verify

SHA = "a" * 40
CONFIG = "sha256:" + "b" * 64


class ReceiptTests(unittest.TestCase):
    """Evidence can pass with findings, but cannot pass with missing/wrong identities."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        manifest = b'{"config":{"digest":"sha256:fixture"}}'
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({"manifests": [{"digest": digest, "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
        (self.directory / "base-index.json").write_bytes(index)
        (self.directory / "base-amd64.json").write_bytes(manifest)
        self.docs = {
            "source.json": {"sha": SHA, "package": "mcp-server", "files_sha256": {}, "base_reference": "node@sha256:" + hashlib.sha256(index).hexdigest()},
            "image.json": {"id": CONFIG, "os": "linux", "architecture": "amd64", "revision": SHA},
            "image-inspect.json": [{"Id": CONFIG, "Os": "linux", "Architecture": "amd64", "Config": {"Labels": {"org.opencontainers.image.revision": SHA}}}],
            "build-metadata.json": {"containerimage.config.digest": CONFIG, "containerimage.digest": "sha256:" + "c" * 64},
            "scan.json": {"ArtifactType": "container_image", "Metadata": {"ImageID": CONFIG, "ImageConfig": {"config": {"Labels": {"org.opencontainers.image.revision": SHA}}}}, "Results": [{"Class": "os-pkgs", "Packages": [{"Name": "libc6"}], "Vulnerabilities": [{"Severity": "HIGH", "VulnerabilityID": "fixture-unfixed"}]}, {"Class": "lang-pkgs", "Packages": [{"Name": "zod"}]}]},
            "scanner-version.json": {"Version": "0.76.1", "VulnerabilityDB": {"UpdatedAt": "2026-11-10T19:01:23Z"}},
            "startup.json": {"path": "/health", "status": 200, "body": {"status": "ok"}},
            "container.json": {"image": CONFIG, "network": "none", "running": True},
        }
        self.write(self.docs)

    def write(self, docs):
        for name, value in docs.items():
            (self.directory / name).write_text(json.dumps(value))

    def test_findings_preserved_and_config_is_not_manifest(self):
        receipt = verify(self.directory, SHA, "mcp-server")
        self.assertEqual(receipt["finding_counts"], {"HIGH": 1})
        self.assertNotEqual(receipt["image"]["id"], receipt["build_export_digest"])
        self.assertIn("fixture-unfixed", (self.directory / "scan.json").read_text())

    def test_manifest_backed_image_requires_matching_raw_descriptors(self):
        """Cover actual Docker/OCI exports and reject missing or contradictory proof."""
        for media_type in ("application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"):
            docs = copy.deepcopy(self.docs)
            descriptor = {"mediaType": media_type, "digest": CONFIG, "size": 2270,
                          "platform": {"architecture": "amd64", "os": "linux"}}
            docs["build-metadata.json"] = {"containerimage.digest": CONFIG, "containerimage.descriptor": descriptor}
            docs["image-inspect.json"][0]["Descriptor"] = {k: v for k, v in descriptor.items() if k != "platform"}
            self.write(docs)
            self.assertEqual(verify(self.directory, SHA, "mcp-server")["image_identity_kind"], "manifest")
            for name, keys, value in [
                ("image-inspect.json", (0, "Descriptor", "digest"), "sha256:" + "e" * 64),
                ("image-inspect.json", (0, "Descriptor", "size"), 1),
                ("image-inspect.json", (0, "Descriptor"), {}),
                ("build-metadata.json", ("containerimage.descriptor", "mediaType"), "unsupported"),
                ("build-metadata.json", ("containerimage.descriptor", "platform", "architecture"), "arm64"),
                ("build-metadata.json", ("containerimage.descriptor",), {}),
            ]:
                with self.subTest(media_type=media_type, name=name, keys=keys):
                    damaged = copy.deepcopy(docs)
                    target = damaged[name]
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    self.write(damaged)
                    with self.assertRaises(ValueError):
                        verify(self.directory, SHA, "mcp-server")
        self.write(self.docs)

    def test_every_required_receipt_missing_fails(self):
        for path in list(self.directory.glob("*.json")):
            with self.subTest(path=path.name):
                data = path.read_bytes()
                path.unlink()
                with self.assertRaises((OSError, ValueError, KeyError)):
                    verify(self.directory, SHA, "mcp-server")
                path.write_bytes(data)

    def test_identity_and_isolation_mismatches_fail(self):
        cases = [
            ("source.json", ("sha",), "d" * 40),
            ("source.json", ("package",), "profile-server"),
            ("image.json", ("architecture",), "arm64"),
            ("image.json", ("revision",), "d" * 40),
            ("image-inspect.json", (0, "Id"), "sha256:" + "e" * 64),
            ("scan.json", ("Results", 0, "Packages"), []),
            ("scan.json", ("Results", 1, "Packages"), []),
            ("scanner-version.json", ("VulnerabilityDB", "UpdatedAt"), ""),
            ("build-metadata.json", ("containerimage.config.digest",), "sha256:" + "c" * 64),
            ("scan.json", ("Metadata", "ImageID"), "sha256:" + "c" * 64),
            ("scan.json", ("Metadata", "ImageConfig", "config", "Labels", "org.opencontainers.image.revision"), "d" * 40),
            ("scan.json", ("Results",), [{"Class": "os-pkgs"}]),
            ("container.json", ("image",), "sha256:" + "c" * 64),
            ("container.json", ("network",), "bridge"),
            ("container.json", ("running",), False),
            ("startup.json", ("body",), {"status": "failed"}),
        ]
        for name, keys, value in cases:
            with self.subTest(name=name, keys=keys):
                docs = copy.deepcopy(self.docs)
                target = docs[name]
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.write(docs)
                with self.assertRaises(ValueError):
                    verify(self.directory, SHA, "mcp-server")
                self.write(self.docs)

    def test_base_tampering_fails(self):
        for name in ("base-index.json", "base-amd64.json"):
            path = self.directory / name
            data = path.read_bytes()
            path.write_bytes(data + b" ")
            with self.assertRaises(ValueError):
                verify(self.directory, SHA, "mcp-server")
            path.write_bytes(data)

    def test_prepare_exports_commit_blobs_and_excludes_local_env(self):
        repo = self.directory / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL, text=True).strip()
        git("init")
        package = repo / "packages/mcp-server"
        (package / "src").mkdir(parents=True)
        (package / "src/http.ts").write_text("tracked fixture")
        (package / "Dockerfile").write_text("FROM node@sha256:" + "e" * 64 + "\n")
        for name in ("package.json", "package-lock.json", "tsconfig.json", ".dockerignore"):
            (package / name).write_text("{}")
        (package / ".env").write_text("SYNTHETIC_SECRET=excluded")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        sha = git("rev-parse", "HEAD")
        (package / "src/http.ts").write_text("uncommitted mutation")
        context = self.directory / "context"
        with patch.dict("os.environ", {"GITHUB_ENV": str(self.directory / "env")}):
            # subprocess inherits the process cwd, so use an isolated cwd here.
            import os
            previous = os.getcwd()
            try:
                os.chdir(repo)
                prepare(self.directory, context, "mcp-server", sha)
                with self.assertRaises(ValueError):
                    prepare(self.directory, self.directory / "wrong-head", "mcp-server", "f" * 40)
            finally:
                os.chdir(previous)
        self.assertFalse((context / ".env").exists())
        self.assertEqual((context / "src/http.ts").read_text(), "tracked fixture")


if __name__ == "__main__":
    unittest.main()
