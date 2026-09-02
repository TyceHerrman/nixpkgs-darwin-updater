import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_pr_body import ROOT, workflow_scripts

VALIDATE_STEP = "Validate the update commit and version"
RECHECK_STEP = "Recheck the commit before publication"
SUBJECT = "example-app: 1.0.0 -> 2.0.0"


class CommitMessageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-update-commit-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.nixpkgs = self.workspace / "nixpkgs"
        self.nixpkgs.mkdir()
        (self.workspace / "automation").symlink_to(ROOT, target_is_directory=True)
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GITHUB_WORKSPACE": str(self.workspace),
            "ATTR": "example-app",
            "OLD_VERSION": "1.0.0",
            "NEW_VERSION": "2.0.0",
            "PACKAGE_FILE": "pkgs/by-name/ex/example-app/package.nix",
            "RELEASE_URL": "https://github.com/example/project/releases/tag/v2.0.0",
        }
        self.package = self.nixpkgs / self.environment["PACKAGE_FILE"]
        self.package.parent.mkdir(parents=True)
        self.package.write_text('{\n  version = "1.0.0";\n}\n')
        self.git("init", "--quiet")
        self.git("config", "user.name", "Test Updater")
        self.git("config", "user.email", "updater@example.invalid")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "base")
        self.environment["BASE_SHA"] = self.git("rev-parse", "HEAD").strip()
        self.package.write_text('{\n  version = "2.0.0";\n}\n')
        self.git("commit", "--quiet", "-am", SUBJECT)

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.nixpkgs,
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def run_step(self, name=VALIDATE_STEP):
        scripts = workflow_scripts()
        self.assertIn(
            name, scripts, "commit validation must be wired into the workflow"
        )
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", scripts[name]],
            cwd=self.nixpkgs,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def message(self):
        return self.git("log", "-1", "--format=%B")

    def metadata(self):
        headers = self.git("cat-file", "commit", "HEAD").split("\n\n", 1)[0]
        return [
            line
            for line in headers.splitlines()
            if line.startswith(("tree ", "parent ", "author "))
        ]

    def amend(self, message):
        self.git("commit", "--quiet", "--amend", "-m", message)

    def assert_rejected_without_amending(self):
        before = self.git("rev-parse", "HEAD")
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_workflow_adds_release_reference_without_claiming_ai_assistance(self):
        before = self.metadata()
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata(), before)
        message = self.message()
        self.assertEqual(message.splitlines()[0], SUBJECT)
        self.assertIn("Changelog: " + self.environment["RELEASE_URL"], message)
        self.assertNotIn("Assisted-by:", message)
        self.assertNotIn("OpenAI", message)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-list", "--count", "HEAD^..HEAD").strip(), "1")

    def test_amending_preserves_explanation_and_existing_trailers(self):
        self.amend(
            SUBJECT + "\n\nKeep the original explanation.\n\n"
            "Signed-off-by: Maintainer <maintainer@example.invalid>\n"
            "Assisted-by: Example AI (model-version)\n"
        )
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Keep the original explanation.\n\n", self.message())
        trailers = subprocess.run(
            ["git", "interpret-trailers", "--parse"],
            input=self.message(),
            capture_output=True,
            text=True,
            check=True,
            env=self.environment,
        ).stdout.splitlines()
        self.assertIn(
            "Signed-off-by: Maintainer <maintainer@example.invalid>", trailers
        )
        self.assertIn("Assisted-by: Example AI (model-version)", trailers)
        self.assertEqual(
            [line for line in trailers if line.startswith("Assisted-by:")],
            ["Assisted-by: Example AI (model-version)"],
        )

    def test_normalization_is_idempotent_and_final_recheck_is_read_only(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.git("rev-parse", "HEAD")
        for step in (VALIDATE_STEP, RECHECK_STEP):
            result = self.run_step(step)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.message().count("Changelog: "), 1)

    def test_incorrect_commit_subject_is_rejected(self):
        for subject in (
            "another-app: 1.0.0 -> 2.0.0",
            "example-app: 1.0.0 -> 3.0.0",
            SUBJECT + ".",
            "Update example-app",
        ):
            with self.subTest(subject=subject):
                self.amend(subject)
                self.assert_rejected_without_amending()

    def test_message_without_blank_line_after_subject_is_rejected(self):
        self.amend(SUBJECT + "\nMissing separator")
        self.assert_rejected_without_amending()

    def test_dirty_tree_extra_files_and_extra_commits_are_rejected(self):
        extra = self.nixpkgs / "unexpected.txt"
        extra.write_text("unexpected\n")
        self.assert_rejected_without_amending()
        self.git("add", "unexpected.txt")
        self.assert_rejected_without_amending()
        self.git("commit", "--quiet", "--amend", "--no-edit")
        self.assert_rejected_without_amending()
        self.git("commit", "--quiet", "--allow-empty", "-m", SUBJECT)
        self.assert_rejected_without_amending()

    def test_both_detected_versions_must_match_committed_files(self):
        self.environment["OLD_VERSION"] = "0.1.0"
        self.assert_rejected_without_amending()
        self.environment["OLD_VERSION"] = "1.0.0"
        self.package.write_text('{\n  version = "3.0.0";\n}\n')
        self.git("commit", "--quiet", "--amend", "-a", "--no-edit")
        self.assert_rejected_without_amending()

    def test_incorrect_release_url_is_rejected(self):
        for url in (
            "https://github.com/example/project/releases/tag/v3.0.0",
            "https://example.invalid/v2.0.0",
            self.environment["RELEASE_URL"] + "\nAssisted-by: fake",
        ):
            with self.subTest(url=url):
                self.environment["RELEASE_URL"] = url
                self.assert_rejected_without_amending()

    def test_non_exact_base_and_unsafe_path_are_rejected(self):
        self.environment["BASE_SHA"] = "HEAD^"
        self.assert_rejected_without_amending()
        self.environment["BASE_SHA"] = self.git("rev-parse", "HEAD^").strip()
        self.environment["PACKAGE_FILE"] = "../package.nix"
        self.assert_rejected_without_amending()

    def test_recheck_rejects_missing_changelog_without_repairing_commit(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        complete = self.message()
        missing = "Changelog: " + self.environment["RELEASE_URL"]
        self.amend(complete.replace(missing + "\n", ""))
        before = self.git("rev-parse", "HEAD")
        result = self.run_step(RECHECK_STEP)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)


if __name__ == "__main__":
    unittest.main()
