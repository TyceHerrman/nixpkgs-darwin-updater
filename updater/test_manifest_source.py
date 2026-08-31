import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import manifest_source


PACKAGE = {
    "attr": "example-app",
    "package_file": "pkgs/by-name/ex/example-app/package.nix",
    "upstream": "example/example-app",
    "maintainer": "example",
    "platform_pattern": r"platforms\s*=",
    "required_asset": "Example_{version}.dmg",
}


class RecordingClient:
    def __init__(self, *, commits=None, content=None):
        self.commits = commits
        self.content = content
        self.calls = []

    def get_json(self, path, *, query=None):
        self.calls.append(("get_json", path, query))
        return self.commits

    def get_text_content(self, repository, path, *, ref):
        self.calls.append(("get_text_content", repository, path, ref))
        return self.content


class ManifestSourceTests(unittest.TestCase):
    def write_manifest(self, directory, payload):
        path = Path(directory) / "packages.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_unset_ref_uses_checked_out_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            local = self.write_manifest(directory, [PACKAGE])
            output = Path(directory) / "resolved.json"

            resolved = manifest_source.resolve_manifest(
                client=None,
                repository="person/nixpkgs-darwin-updater",
                local_manifest=local,
                manifest_ref=None,
                output_manifest=output,
                local_sha="1" * 40,
            )

            self.assertEqual(json.loads(output.read_text()), [PACKAGE])
            self.assertEqual(resolved.path, output.resolve())
            self.assertIsNone(resolved.source_ref)
            self.assertEqual(resolved.commit_sha, "1" * 40)
            self.assertEqual([package.attr for package in resolved.packages], ["example-app"])

    def test_ref_is_resolved_to_a_commit_before_fetching_only_the_manifest(self):
        commit_sha = "a" * 40
        client = RecordingClient(
            commits=[
                {
                    "sha": commit_sha,
                    "commit": {"message": "update manifest"},
                    "html_url": f"https://github.com/example/repo/commit/{commit_sha}",
                }
            ],
            content=json.dumps([PACKAGE]),
        )
        with tempfile.TemporaryDirectory() as directory:
            local = self.write_manifest(directory, [])
            output = Path(directory) / "resolved.json"

            resolved = manifest_source.resolve_manifest(
                client=client,
                repository="person/nixpkgs-darwin-updater",
                local_manifest=local,
                manifest_ref="instance/tyce",
                output_manifest=output,
                local_sha="2" * 40,
            )

        self.assertEqual(
            client.calls,
            [
                (
                    "get_json",
                    "/repos/person/nixpkgs-darwin-updater/commits",
                    {"sha": "instance/tyce", "per_page": 1},
                ),
                (
                    "get_text_content",
                    "person/nixpkgs-darwin-updater",
                    "updater/packages.json",
                    commit_sha,
                ),
            ],
        )
        self.assertEqual(resolved.source_ref, "instance/tyce")
        self.assertEqual(resolved.commit_sha, commit_sha)
        self.assertEqual([package.attr for package in resolved.packages], ["example-app"])

    def test_empty_manifest_is_rejected_before_work_is_started(self):
        with tempfile.TemporaryDirectory() as directory:
            local = self.write_manifest(directory, [])
            with self.assertRaisesRegex(ValueError, "at least one package"):
                manifest_source.resolve_manifest(
                    client=None,
                    repository="person/nixpkgs-darwin-updater",
                    local_manifest=local,
                    manifest_ref=None,
                    output_manifest=Path(directory) / "resolved.json",
                    local_sha="3" * 40,
                )

    def test_external_url_is_not_accepted_as_a_manifest_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            local = self.write_manifest(directory, [PACKAGE])
            with self.assertRaisesRegex(ValueError, "invalid manifest ref"):
                manifest_source.resolve_manifest(
                    client=RecordingClient(),
                    repository="person/nixpkgs-darwin-updater",
                    local_manifest=local,
                    manifest_ref="https://example.com/packages.json",
                    output_manifest=Path(directory) / "resolved.json",
                    local_sha="4" * 40,
                )

    def test_pull_request_namespace_is_not_accepted_as_a_manifest_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            local = self.write_manifest(directory, [PACKAGE])
            for manifest_ref in ("refs/pull/123/head", "pull/123/head"):
                with self.subTest(manifest_ref=manifest_ref):
                    with self.assertRaisesRegex(ValueError, "invalid manifest ref"):
                        manifest_source.resolve_manifest(
                            client=RecordingClient(),
                            repository="person/nixpkgs-darwin-updater",
                            local_manifest=local,
                            manifest_ref=manifest_ref,
                            output_manifest=Path(directory) / "resolved.json",
                            local_sha="4" * 40,
                        )

    def test_summary_identifies_exact_source_and_packages(self):
        commit_sha = "b" * 40
        client = RecordingClient(
            commits=[
                {
                    "sha": commit_sha,
                    "commit": {"message": "update manifest"},
                    "html_url": f"https://github.com/example/repo/commit/{commit_sha}",
                }
            ],
            content=json.dumps([PACKAGE]),
        )
        with tempfile.TemporaryDirectory() as directory:
            resolved = manifest_source.resolve_manifest(
                client=client,
                repository="person/nixpkgs-darwin-updater",
                local_manifest=self.write_manifest(directory, []),
                manifest_ref="instance/tyce",
                output_manifest=Path(directory) / "resolved.json",
                local_sha="5" * 40,
            )
            summary = Path(directory) / "summary.md"

            manifest_source.write_summary(resolved, summary)

            self.assertEqual(
                summary.read_text(),
                "## Package manifest\n\n"
                "- Source ref: `instance/tyce`\n"
                f"- Resolved commit: `{commit_sha}`\n"
                "- Packages: `example-app`\n",
            )

    def test_cli_writes_manifest_path_and_summary_for_later_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            local = self.write_manifest(directory, [PACKAGE])
            output = Path(directory) / "resolved.json"
            actions_output = Path(directory) / "actions-output"
            summary = Path(directory) / "summary.md"

            result = manifest_source.main(
                [
                    "--repository",
                    "person/nixpkgs-darwin-updater",
                    "--local-manifest",
                    str(local),
                    "--output-manifest",
                    str(output),
                    "--local-sha",
                    "c" * 40,
                    "--github-output",
                    str(actions_output),
                    "--github-summary",
                    str(summary),
                ]
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                actions_output.read_text(),
                f"manifest_path={output.resolve()}\n"
                "manifest_ref=\n"
                f"manifest_sha={'c' * 40}\n",
            )
            self.assertEqual(
                summary.read_text(),
                "## Package manifest\n\n"
                "- Source: checked-out `updater/packages.json`\n"
                f"- Resolved commit: `{'c' * 40}`\n"
                "- Packages: `example-app`\n",
            )


if __name__ == "__main__":
    unittest.main()
