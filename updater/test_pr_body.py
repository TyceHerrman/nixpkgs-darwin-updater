import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Snapshot from NixOS/nixpkgs at 88ad3228bd74d58f821b0d6d148972de1069ebd3.
TEMPLATE = (ROOT / "updater/fixtures/nixpkgs-pr-template.md").read_text()
PREPARE_STEP = "Prepare the upstream pull request body"
CREATE_STEP = "Open an upstream draft pull request"


def workflow_scripts():
    """Extract literal run blocks so tests execute the actual publication steps."""
    scripts = {}
    name = None
    for line in (ROOT / ".github/workflows/update.yml").read_text().splitlines():
        if line.startswith("      - name: "):
            name = line.removeprefix("      - name: ")
        elif line == "        run: |":
            scripts[name] = []
        elif line.startswith("          ") and name in scripts:
            scripts[name].append(line[10:])
    return {name: "\n".join(lines) for name, lines in scripts.items()}


class PullRequestBodyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-pr-body-", dir="/private/tmp"
        )
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.nixpkgs = self.workspace / "nixpkgs"
        self.nixpkgs.mkdir()
        (self.workspace / "automation").symlink_to(ROOT, target_is_directory=True)
        self.template = self.nixpkgs / ".github/PULL_REQUEST_TEMPLATE.md"
        self.template.parent.mkdir()
        self.template.write_text(TEMPLATE)
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TEMP": str(self.workspace),
            "ATTR": "example-app",
            "NEW_VERSION": "2.0.0",
            "OLD_VERSION": "1.0.0",
            "RELEASE_URL": "https://github.com/example/project/releases/tag/v2.0.0",
            "BRANCH": "auto-update/example-app-2.0.0",
            "FORK_OWNER": "person",
            "TITLE": "example-app: 1.0.0 -> 2.0.0",
            "GH_TOKEN": "not-a-real-token",
            "GITHUB_TOKEN": "not-a-real-token",
        }
        self.git("init", "--quiet")
        self.commit_template()
        self.verification = self.workspace / "package-verification.json"
        self.verification.write_text(
            json.dumps(
                {
                    "out_paths": ["/nix/store/example-app"],
                    "passthru_test_paths": [],
                }
            )
        )
        self.capture = self.workspace / "created-pr.json"
        self.environment["PR_CAPTURE"] = str(self.capture)
        fake_bin = self.workspace / "bin"
        fake_bin.mkdir()
        gh = fake_bin / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:2] == ["pr", "create"], args
if "--body-file" in args:
    body = Path(args[args.index("--body-file") + 1]).read_text()
else:
    body = args[args.index("--body") + 1]
Path(os.environ["PR_CAPTURE"]).write_text(json.dumps({"body": body, "args": args}))
"""
        )
        gh.chmod(0o755)
        self.environment["PATH"] = f"{fake_bin}:{os.environ['PATH']}"

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.nixpkgs,
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit_template(self):
        self.git("add", ".github/PULL_REQUEST_TEMPLATE.md")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "template",
        )
        self.environment["BASE_SHA"] = self.git("rev-parse", "HEAD")

    def run_publication_steps(self):
        scripts = workflow_scripts()
        for name in (PREPARE_STEP, CREATE_STEP):
            if name not in scripts:
                continue  # The old workflow has no preparation step.
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", scripts[name]],
                cwd=self.nixpkgs,
                env=self.environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                return result
        return result

    def assert_template_preserved(self, body, *, tests_built=False, template=TEMPLATE):
        self.assertIn("## Things done", body)
        self.assertIn("Update `example-app` from `1.0.0` to `2.0.0`", body)
        self.assertIn(self.environment["RELEASE_URL"], body)
        actual_template = body[body.index("\n<!--") :]
        restored = actual_template.replace(
            "  - [x] aarch64-darwin", "  - [ ] aarch64-darwin"
        )
        if tests_built:
            restored = restored.replace(
                "  - [x] [Package tests] at `passthru.tests`.",
                "  - [ ] [Package tests] at `passthru.tests`.",
            )
        self.assertEqual(restored, template)
        self.assertIn("  - [x] aarch64-darwin", actual_template)
        self.assertEqual(actual_template.count("[x]"), 2 if tests_built else 1)

    def test_published_body_preserves_exact_upstream_template(self):
        result = self.run_publication_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        created = json.loads(self.capture.read_text())
        self.assert_template_preserved(created["body"])
        self.assertIn("--draft", created["args"])

    def test_package_tests_checked_only_when_derivations_were_built(self):
        self.verification.write_text(
            json.dumps(
                {
                    "out_paths": ["/nix/store/example-app"],
                    "passthru_test_paths": ["/nix/store/example-test"],
                }
            )
        )
        result = self.run_publication_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_template_preserved(
            json.loads(self.capture.read_text())["body"], tests_built=True
        )

    def test_template_is_read_from_exact_base_not_current_worktree(self):
        self.template.write_text("untrusted replacement\n")
        result = self.run_publication_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_template_preserved(json.loads(self.capture.read_text())["body"])

    def test_new_upstream_sections_are_preserved_without_copying_a_template(self):
        template = (
            TEMPLATE + "\n## New upstream checklist\n\n- [ ] Human confirmation\n"
        )
        self.template.write_text(template)
        self.commit_template()
        result = self.run_publication_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_template_preserved(
            json.loads(self.capture.read_text())["body"], template=template
        )

    def test_missing_verification_stops_before_pr_creation(self):
        self.verification.unlink()
        result = self.run_publication_steps()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_invalid_verification_cannot_claim_a_successful_build(self):
        for payload in (
            {},
            {"out_paths": [], "passthru_test_paths": []},
            {"out_paths": ["not a store path"], "passthru_test_paths": []},
        ):
            with self.subTest(payload=payload):
                self.verification.write_text(json.dumps(payload))
                result = self.run_publication_steps()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_missing_template_at_base_stops_before_pr_creation(self):
        self.git("rm", ".github/PULL_REQUEST_TEMPLATE.md")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "remove template",
        )
        self.environment["BASE_SHA"] = self.git("rev-parse", "HEAD")
        result = self.run_publication_steps()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_unknown_template_shape_stops_before_pr_creation(self):
        self.template.write_text("## Different template\n")
        self.commit_template()
        result = self.run_publication_steps()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
