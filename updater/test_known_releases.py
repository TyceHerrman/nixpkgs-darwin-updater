import json
import re
import unittest
from dataclasses import replace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import check_releases
from test_check_releases import (
    FakeResponse,
    content_payload,
    package_source,
    release_asset,
    release_payload,
)

PACKAGE = check_releases.Package(
    attr="example-app",
    package_file="pkgs/by-name/ex/example-app/package.nix",
    upstream="example/project",
    maintainer="tyceherrman",
    platform_pattern=r"platforms\s*=\s*lib\.platforms\.darwin\s*;",
    required_asset="Example-{version}.dmg",
)
REPOSITORY = "person/nixpkgs-darwin-updater"
PR_URL = "https://github.com/NixOS/nixpkgs/pull/123"
RELEASE_URL = "https://github.com/example/project/releases/tag/v2.0.0"


def ready_release(tag="v2.0.0"):
    return release_payload(
        tag,
        PACKAGE.upstream,
        assets=[release_asset(f"Example-{tag.removeprefix('v')}.dmg")],
    )


class ReleaseApi:
    """Stateful HTTP boundary: exercise the real client and detector across polls."""

    def __init__(self):
        self.latest = ready_release("v1.0.0")
        self.releases = {}
        self.issues = []
        self.comments = []
        self.requests = []
        self.write_error = None
        self.pull = {
            "number": 123,
            "state": "open",
            "html_url": PR_URL,
            "title": "example-app: 1.0.0 -> 2.0.0",
            "body": f"Upstream release: {RELEASE_URL}",
            "base": {"ref": "master", "repo": {"full_name": "NixOS/nixpkgs"}},
            "head": {
                "ref": "auto-update/example-app-2.0.0",
                "repo": {"full_name": "person/nixpkgs"},
            },
        }

    def __call__(self, request):
        self.requests.append(request)
        parsed = urlsplit(request.full_url)
        path, query = parsed.path, parse_qs(parsed.query)
        method = request.get_method()
        if method != "GET" and self.write_error:
            raise self.write_error
        if path == "/repos/NixOS/nixpkgs/commits/master":
            result = {"sha": "a" * 40}
        elif path == f"/repos/NixOS/nixpkgs/contents/{PACKAGE.package_file}":
            result = content_payload(package_source("1.0.0", "lib.platforms.darwin"))
        elif path == f"/repos/{PACKAGE.upstream}/releases/latest":
            result = self.latest
        elif path.startswith(f"/repos/{PACKAGE.upstream}/releases/tags/"):
            result = self.releases.get(path.rsplit("/", 1)[1])
        elif path == "/search/issues":
            items = [self.pull] if self.pull else []
            result = {
                "items": items,
                "total_count": len(items),
                "incomplete_results": False,
            }
        elif path == "/repos/NixOS/nixpkgs/pulls/123":
            result = self.pull
        elif path == "/repos/NixOS/nixpkgs/pulls":
            result = [self.pull] if self.pull else []
        elif path == f"/repos/{REPOSITORY}/issues" and method == "GET":
            result = self.issues if query["page"] == ["1"] else []
        elif path == f"/repos/{REPOSITORY}/issues" and method == "POST":
            number = len(self.issues) + 1
            result = {
                **json.loads(request.data),
                "number": number,
                "state": "open",
                "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
            }
            self.issues.append(result)
        elif re.fullmatch(f"/repos/{REPOSITORY}/issues/[0-9]+/comments", path):
            self.comments.append(json.loads(request.data)["body"])
            result = {"id": len(self.comments)}
        elif (
            re.fullmatch(f"/repos/{REPOSITORY}/issues/[0-9]+", path)
            and method == "PATCH"
        ):
            result = self.issues[int(path.rsplit("/", 1)[1]) - 1]
            result.update(json.loads(request.data))
        else:
            raise AssertionError(f"unexpected request: {method} {request.full_url}")
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return FakeResponse(json.dumps(result).encode())

    def collect(self, package=PACKAGE, **kwargs):
        return check_releases.collect_updates(
            [package],
            check_releases.GitHubClient(None, opener=self),
            fork_owner="person",
            updater_repository=REPOSITORY,
            **kwargs,
        )


class KnownReleaseTests(unittest.TestCase):
    def test_regressed_latest_checks_open_pr_and_notifies_once_even_if_closed(self):
        api = ReleaseApi()
        first = api.collect()
        self.assertEqual(first.candidates, ())
        self.assertEqual(len(api.issues), 1)
        self.assertIn(PR_URL, api.issues[0]["body"])
        self.assertIn(RELEASE_URL, api.issues[0]["body"])
        self.assertIn("Example-2.0.0.dmg", api.issues[0]["body"])
        self.assertIn("unavailable", " ".join(first.notes))
        self.assertNotIn("current at", " ".join(first.notes))
        api.issues[0]["state"] = "closed"
        self.assertEqual(api.collect().candidates, ())
        self.assertEqual(len(api.issues), 1)
        self.assertEqual(api.comments, [])
        self.assertEqual(api.issues[0]["state"], "closed")

    def test_restoration_resolves_once_without_recreating_existing_pr(self):
        api = ReleaseApi()
        api.collect()
        api.releases["v2.0.0"] = ready_release()
        for _ in range(2):
            result = api.collect()
            self.assertEqual(result.candidates, ())
            self.assertIn(PR_URL, " ".join(result.notes))
        self.assertEqual(api.issues[0]["state"], "closed")
        self.assertEqual(len(api.comments), 1)
        self.assertIn("available", api.comments[0])

    def test_asset_readiness_checked_for_known_release_even_when_latest_is_old(self):
        for assets in (
            [],
            [release_asset("Example-2.0.0.dmg", size=0)],
            [release_asset("Example-2.0.0.dmg", state="starter")],
        ):
            with self.subTest(assets=assets):
                api = ReleaseApi()
                api.releases["v2.0.0"] = release_payload(
                    "v2.0.0", PACKAGE.upstream, assets=assets
                )
                self.assertEqual(api.collect().candidates, ())
                self.assertEqual(len(api.issues), 1)
                self.assertIn(PR_URL, api.issues[0]["body"])

    def test_known_blocked_release_recovers_without_latest_or_open_pr(self):
        api = ReleaseApi()
        api.collect()
        api.pull = None
        api.latest = None
        api.releases["v2.0.0"] = ready_release()
        result = api.collect()
        self.assertEqual([c.new_version for c in result.candidates], ["2.0.0"])
        self.assertEqual(api.issues[0]["state"], "closed")

    def test_repeated_withdrawal_reuses_and_reopens_recovered_issue(self):
        api = ReleaseApi()
        api.collect()
        api.releases["v2.0.0"] = ready_release()
        api.collect()
        api.releases.clear()
        api.collect()
        self.assertEqual(len(api.issues), 1)
        self.assertEqual(api.issues[0]["state"], "open")

    def test_api_errors_are_not_release_withdrawals(self):
        for error in [
            HTTPError(RELEASE_URL, code, "failure", {}, None)
            for code in (401, 403, 429, 500, 502)
        ] + [URLError("timeout")]:
            with self.subTest(error=error):
                api = ReleaseApi()
                api.releases["v2.0.0"] = error
                with self.assertRaises(check_releases.GitHubApiError):
                    api.collect()
                self.assertEqual(api.issues, [])

    def test_issue_write_failure_fails_detection(self):
        api = ReleaseApi()
        api.write_error = HTTPError(RELEASE_URL, 403, "Forbidden", {}, None)
        with self.assertRaises(check_releases.GitHubApiError):
            api.collect()

    def test_without_asset_rule_still_requires_a_published_known_release(self):
        api = ReleaseApi()
        self.assertEqual(
            api.collect(replace(PACKAGE, required_asset=None)).candidates, ()
        )
        self.assertEqual(len(api.issues), 1)

    def test_search_result_from_another_fork_is_not_a_known_release(self):
        api = ReleaseApi()
        api.pull["head"]["repo"]["full_name"] = "someone-else/nixpkgs"
        result = api.collect()
        self.assertEqual(result.candidates, ())
        self.assertEqual(api.issues, [])
        self.assertIn("no newer release found", " ".join(result.notes))

    def test_exact_release_must_match_the_known_version(self):
        api = ReleaseApi()
        api.releases["v2.0.0"] = ready_release("v3.0.0")
        with self.assertRaisesRegex(
            check_releases.GitHubApiError, "does not match requested"
        ):
            api.collect()


if __name__ == "__main__":
    unittest.main()
