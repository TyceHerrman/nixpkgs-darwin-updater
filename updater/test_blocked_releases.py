import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import check_releases


class InMemoryIssueClient:
    def __init__(self, issues=()):
        self.issues = [dict(issue) for issue in issues]
        self.comments = []

    def list_repository_issues(self, repository):
        return tuple(dict(issue) for issue in self.issues)

    def create_issue(self, repository, *, title, body):
        issue = {
            "number": len(self.issues) + 1,
            "title": title,
            "body": body,
            "state": "open",
            "html_url": f"https://github.com/{repository}/issues/{len(self.issues) + 1}",
        }
        self.issues.append(issue)
        return dict(issue)

    def comment_issue(self, repository, number, body):
        self.comments.append((repository, number, body))

    def close_issue(self, repository, number):
        for issue in self.issues:
            if issue["number"] == number:
                issue["state"] = "closed"
                return
        raise AssertionError(f"missing issue {number}")


def package():
    return check_releases.Package(
        attr="example-app",
        package_file="pkgs/by-name/ex/example-app/package.nix",
        upstream="example/example-app",
        maintainer="example",
        platform_pattern=r"platforms\s*=\s*lib\.platforms\.darwin\s*;",
        required_asset="Example-{version}.dmg",
    )


def asset(name, *, state="uploaded", size=1024):
    return check_releases.ReleaseAsset(name=name, state=state, size=size)


class BlockedReleaseTests(unittest.TestCase):
    repository = "person/nixpkgs-darwin-updater"
    release_url = "https://github.com/example/example-app/releases/tag/v2.0.0"

    def test_bad_release_opens_one_issue_and_is_not_actionable(self):
        client = InMemoryIssueClient()

        result = check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.0.0",
            release_url=self.release_url,
            assets=(),
        )

        self.assertFalse(result.actionable)
        self.assertEqual(len(client.issues), 1)
        issue = client.issues[0]
        self.assertEqual(issue["state"], "open")
        self.assertIn("example-app 2.0.0", issue["title"])
        self.assertIn("Example-2.0.0.dmg", issue["body"])
        self.assertIn(self.release_url, issue["body"])
        self.assertIn(
            "nixpkgs-darwin-updater:blocked-release:example-app@2.0.0",
            issue["body"],
        )

    def test_same_release_stays_suppressed_after_manual_issue_closure(self):
        client = InMemoryIssueClient()
        first = check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.0.0",
            release_url=self.release_url,
            assets=(),
        )
        self.assertFalse(first.actionable)
        client.issues[0]["state"] = "closed"

        second = check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.0.0",
            release_url=self.release_url,
            assets=(asset("Example-2.0.0.dmg"),),
        )

        self.assertFalse(second.actionable)
        self.assertEqual(len(client.issues), 1)

    def test_newer_valid_release_closes_old_issue_and_becomes_actionable(self):
        client = InMemoryIssueClient()
        check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.0.0",
            release_url=self.release_url,
            assets=(),
        )

        result = check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.1.0",
            release_url="https://github.com/example/example-app/releases/tag/v2.1.0",
            assets=(asset("Example-2.1.0.dmg"),),
        )

        self.assertTrue(result.actionable)
        self.assertEqual(client.issues[0]["state"], "closed")
        self.assertEqual(
            client.comments,
            [
                (
                    self.repository,
                    1,
                    (
                        "Superseded by release 2.1.0: "
                        "https://github.com/example/example-app/releases/tag/v2.1.0"
                    ),
                )
            ],
        )

    def test_newer_bad_release_closes_old_issue_and_opens_replacement(self):
        client = InMemoryIssueClient()
        check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.0.0",
            release_url=self.release_url,
            assets=(),
        )

        result = check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=package(),
            version="2.1.0",
            release_url="https://github.com/example/example-app/releases/tag/v2.1.0",
            assets=(),
        )

        self.assertFalse(result.actionable)
        self.assertEqual(
            [issue["state"] for issue in client.issues], ["closed", "open"]
        )
        self.assertIn("example-app@2.1.0", client.issues[1]["body"])

    def test_package_without_required_asset_is_actionable(self):
        client = InMemoryIssueClient()
        ungated_package = check_releases.Package(
            attr="example-app",
            package_file="pkgs/by-name/ex/example-app/package.nix",
            upstream="example/example-app",
            maintainer="example",
            platform_pattern=r"platforms\s*=\s*lib\.platforms\.darwin\s*;",
            required_asset=None,
        )

        result = check_releases.gate_release_asset(
            client,
            repository=self.repository,
            package=ungated_package,
            version="2.0.0",
            release_url=self.release_url,
            assets=(),
        )

        self.assertTrue(result.actionable)
        self.assertEqual(client.issues, [])

    def test_issue_write_failure_is_not_treated_as_a_skip(self):
        class FailingIssueClient(InMemoryIssueClient):
            def create_issue(self, repository, *, title, body):
                raise check_releases.GitHubApiError("issue write failed")

        with self.assertRaisesRegex(
            check_releases.GitHubApiError, "issue write failed"
        ):
            check_releases.gate_release_asset(
                FailingIssueClient(),
                repository=self.repository,
                package=package(),
                version="2.0.0",
                release_url=self.release_url,
                assets=(),
            )


if __name__ == "__main__":
    unittest.main()
