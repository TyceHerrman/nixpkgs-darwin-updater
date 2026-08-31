import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import self_update


PACKAGE = {
    "attr": "example-app",
    "package_file": "pkgs/by-name/ex/example-app/package.nix",
    "upstream": "example/example-app",
    "maintainer": "example",
    "platform_pattern": r"platforms\s*=",
    "required_asset": None,
}


def git(*args, cwd=None):
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def configure_repository(path):
    git("config", "user.name", "Updater Test", cwd=path)
    git("config", "user.email", "updater@example.com", cwd=path)


class RepositoryFixture:
    def __init__(self, root, *, conflict=False):
        self.root = Path(root)
        self.source = self.root / "source"
        self.upstream = self.root / "upstream.git"
        self.fork_seed = self.root / "fork-seed"
        self.fork = self.root / "fork.git"
        self.runner = self.root / "runner"

        git("init", "-b", "main", str(self.source))
        configure_repository(self.source)
        (self.source / "updater").mkdir()
        (self.source / "updater" / "packages.json").write_text("[]\n")
        (self.source / "code.txt").write_text("initial\n")
        git("add", ".", cwd=self.source)
        git("commit", "-m", "initial", cwd=self.source)
        git("clone", "--bare", str(self.source), str(self.upstream))
        git("remote", "add", "origin", str(self.upstream), cwd=self.source)

        git("clone", str(self.upstream), str(self.fork_seed))
        configure_repository(self.fork_seed)
        self.manifest_text = json.dumps([PACKAGE], indent=2) + "\n"
        (self.fork_seed / "updater" / "packages.json").write_text(self.manifest_text)
        if conflict:
            (self.fork_seed / "code.txt").write_text("fork change\n")
        git("add", ".", cwd=self.fork_seed)
        git("commit", "-m", "configure fork", cwd=self.fork_seed)
        git("clone", "--bare", str(self.fork_seed), str(self.fork))
        git("clone", str(self.fork), str(self.runner))
        configure_repository(self.runner)
        self.original_fork_sha = git("rev-parse", "HEAD", cwd=self.runner)

        if conflict:
            (self.source / "code.txt").write_text("upstream change\n")
        else:
            (self.source / "upstream.txt").write_text("new upstream code\n")
        git("add", ".", cwd=self.source)
        git("commit", "-m", "upstream update", cwd=self.source)
        git("push", "origin", "main", cwd=self.source)


class MappingClient:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, path, *, query=None):
        expected_path = f"/repos/{self.payload['full_name']}"
        if path != expected_path or query is not None:
            raise AssertionError((path, query))
        return self.payload


class ParentDiscoveryTests(unittest.TestCase):
    def test_discovers_exact_parent_clone_url_and_default_branch(self):
        result = self_update.discover_parent(
            MappingClient(
                {
                    "full_name": "person/nixpkgs-darwin-updater",
                    "fork": True,
                    "default_branch": "main",
                    "parent": {
                        "full_name": "TyceHerrman/nixpkgs-darwin-updater",
                        "clone_url": "https://github.com/TyceHerrman/nixpkgs-darwin-updater.git",
                        "default_branch": "main",
                    },
                }
            ),
            "person/nixpkgs-darwin-updater",
        )

        self.assertEqual(result.full_name, "TyceHerrman/nixpkgs-darwin-updater")
        self.assertEqual(
            result.clone_url,
            "https://github.com/TyceHerrman/nixpkgs-darwin-updater.git",
        )
        self.assertEqual(result.default_branch, "main")

    def test_canonical_repository_is_rejected_as_not_a_fork(self):
        with self.assertRaisesRegex(self_update.SelfUpdateError, "not a fork"):
            self_update.discover_parent(
                MappingClient(
                    {
                        "full_name": "TyceHerrman/nixpkgs-darwin-updater",
                        "fork": False,
                        "default_branch": "main",
                    }
                ),
                "TyceHerrman/nixpkgs-darwin-updater",
            )


class GitSynchronizationTests(unittest.TestCase):
    def test_prepare_rebases_and_checks_without_pushing(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(directory)

            result = self_update.prepare_update(
                repository_dir=fixture.runner,
                upstream_url=str(fixture.upstream),
                upstream_branch="main",
                fork_branch="main",
                test_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
            )

            self.assertTrue(result.changed)
            self.assertTrue((fixture.runner / "upstream.txt").is_file())
            self.assertEqual(
                git("--git-dir", str(fixture.fork), "rev-parse", "main"),
                fixture.original_fork_sha,
            )

    def test_rebased_checks_do_not_receive_github_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(directory)
            observed = Path(directory) / "observed-environment.json"
            command = (
                sys.executable,
                "-c",
                "import json, os, pathlib; "
                f"pathlib.Path({str(observed)!r}).write_text(json.dumps("
                "{name: os.environ.get(name) for name in "
                "('GH_SELF_UPDATE_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN')}))",
            )

            with patch.dict(
                "os.environ",
                {
                    "GH_SELF_UPDATE_TOKEN": "long-lived-secret",
                    "GITHUB_TOKEN": "job-token",
                    "GH_TOKEN": "cli-token",
                },
            ):
                self_update.synchronize(
                    repository_dir=fixture.runner,
                    upstream_url=str(fixture.upstream),
                    upstream_branch="main",
                    fork_branch="main",
                    test_commands=(command,),
                    push_token=None,
                )

            self.assertEqual(
                json.loads(observed.read_text()),
                {
                    "GH_SELF_UPDATE_TOKEN": None,
                    "GITHUB_TOKEN": None,
                    "GH_TOKEN": None,
                },
            )

    def test_rebase_preserves_fork_manifest_and_pushes_upstream_code(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(directory)

            result = self_update.synchronize(
                repository_dir=fixture.runner,
                upstream_url=str(fixture.upstream),
                upstream_branch="main",
                fork_branch="main",
                test_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
                push_token=None,
            )

            inspect = Path(directory) / "inspect"
            git("clone", str(fixture.fork), str(inspect))
            self.assertTrue(result.changed)
            self.assertEqual(
                (inspect / "updater" / "packages.json").read_text(),
                fixture.manifest_text,
            )
            self.assertEqual((inspect / "upstream.txt").read_text(), "new upstream code\n")

    def test_conflict_leaves_remote_fork_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(directory, conflict=True)

            with self.assertRaisesRegex(self_update.SelfUpdateError, "git rebase"):
                self_update.synchronize(
                    repository_dir=fixture.runner,
                    upstream_url=str(fixture.upstream),
                    upstream_branch="main",
                    fork_branch="main",
                    test_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
                    push_token=None,
                )

            self.assertEqual(
                git("--git-dir", str(fixture.fork), "rev-parse", "main"),
                fixture.original_fork_sha,
            )

    def test_failed_check_leaves_remote_fork_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(directory)

            with self.assertRaisesRegex(self_update.SelfUpdateError, "check failed"):
                self_update.synchronize(
                    repository_dir=fixture.runner,
                    upstream_url=str(fixture.upstream),
                    upstream_branch="main",
                    fork_branch="main",
                    test_commands=((sys.executable, "-c", "raise SystemExit(7)"),),
                    push_token=None,
                )

            self.assertEqual(
                git("--git-dir", str(fixture.fork), "rev-parse", "main"),
                fixture.original_fork_sha,
            )

    def test_force_with_lease_refuses_to_overwrite_a_racing_push(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(directory)
            rebase = self_update.rebase_onto_parent(
                repository_dir=fixture.runner,
                upstream_url=str(fixture.upstream),
                upstream_branch="main",
                fork_branch="main",
            )
            racing = Path(directory) / "racing"
            git("clone", str(fixture.fork), str(racing))
            configure_repository(racing)
            (racing / "race.txt").write_text("concurrent update\n")
            git("add", "race.txt", cwd=racing)
            git("commit", "-m", "racing update", cwd=racing)
            git("push", "origin", "main", cwd=racing)
            racing_sha = git("rev-parse", "HEAD", cwd=racing)

            with self.assertRaisesRegex(self_update.SelfUpdateError, "git push"):
                self_update.push_with_lease(
                    repository_dir=fixture.runner,
                    fork_branch="main",
                    expected_remote_sha=rebase.original_sha,
                    push_token=None,
                )

            self.assertEqual(
                git("--git-dir", str(fixture.fork), "rev-parse", "main"),
                racing_sha,
            )


if __name__ == "__main__":
    unittest.main()
