import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).parent))

import check_releases


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class MappingOpener:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        parsed = urlsplit(request.full_url)
        key = (parsed.path, tuple(sorted(parse_qsl(parsed.query))))
        method_key = (request.get_method(), *key)
        if method_key in self.responses:
            key = method_key
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return FakeResponse(json.dumps(response).encode())


def api_key(path, **query):
    return (path, tuple(sorted((key, str(value)) for key, value in query.items())))


def method_api_key(method, path, **query):
    return (method, *api_key(path, **query))


def known_pr_search_key(attr):
    return api_key(
        "/search/issues",
        q=f'repo:NixOS/nixpkgs is:pr is:open in:title "{attr}:"',
        per_page=100,
        page=1,
    )


def package_source(version, platform):
    return f"""
      version = "{version}";
      maintainers = with lib.maintainers; [ tyceherrman ];
      platforms = {platform};
    """


def content_payload(source):
    content = base64.b64encode(source.encode()).decode()
    return {
        "type": "file",
        "encoding": "base64",
        "content": content,
        "sha": "b" * 40,
    }


def release_asset(name, *, state="uploaded", size=1024):
    return {
        "url": "https://api.github.com/repos/example/project/releases/assets/1",
        "id": 1,
        "node_id": "RA_test",
        "name": name,
        "label": None,
        "uploader": {
            "login": "github-actions[bot]",
            "id": 41898282,
            "node_id": "MDM6Qm90NDE4OTgyODI=",
            "avatar_url": "https://avatars.githubusercontent.com/in/15368?v=4",
            "gravatar_id": "",
            "url": "https://api.github.com/users/github-actions%5Bbot%5D",
            "html_url": "https://github.com/apps/github-actions",
            "followers_url": "https://api.github.com/users/github-actions%5Bbot%5D/followers",
            "following_url": "https://api.github.com/users/github-actions%5Bbot%5D/following{/other_user}",
            "gists_url": "https://api.github.com/users/github-actions%5Bbot%5D/gists{/gist_id}",
            "starred_url": "https://api.github.com/users/github-actions%5Bbot%5D/starred{/owner}{/repo}",
            "subscriptions_url": "https://api.github.com/users/github-actions%5Bbot%5D/subscriptions",
            "organizations_url": "https://api.github.com/users/github-actions%5Bbot%5D/orgs",
            "repos_url": "https://api.github.com/users/github-actions%5Bbot%5D/repos",
            "events_url": "https://api.github.com/users/github-actions%5Bbot%5D/events{/privacy}",
            "received_events_url": "https://api.github.com/users/github-actions%5Bbot%5D/received_events",
            "type": "Bot",
            "user_view_type": "public",
            "site_admin": False,
        },
        "content_type": "application/octet-stream",
        "state": state,
        "size": size,
        "digest": "sha256:" + "a" * 64,
        "download_count": 0,
        "created_at": "2026-08-29T03:15:03Z",
        "updated_at": "2026-08-29T03:15:03Z",
        "browser_download_url": "https://github.com/example/project/releases/download/v1.0.0/asset",
    }


def release_payload(tag, repository, *, assets=()):
    return {
        "url": f"https://api.github.com/repos/{repository}/releases/1",
        "html_url": f"https://github.com/{repository}/releases/tag/{tag}",
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-08T22:57:10Z",
        "assets": list(assets),
    }


PACKAGES = (
    check_releases.Package(
        attr="harper-desktop",
        package_file="pkgs/by-name/ha/harper-desktop/package.nix",
        upstream="Automattic/harper",
        maintainer="tyceherrman",
        platform_pattern=r"platforms\s*=\s*lib\.platforms\.darwin\s*;",
        required_asset="Harper_{version}_universal.dmg",
    ),
    check_releases.Package(
        attr="whatcable",
        package_file="pkgs/by-name/wh/whatcable/package.nix",
        upstream="darrylmorley/whatcable",
        maintainer="tyceherrman",
        platform_pattern=r'platforms\s*=\s*\[\s*"aarch64-darwin"\s*\]\s*;',
        required_asset="WhatCable.zip",
    ),
)

UPDATER_REPOSITORY = "person/nixpkgs-darwin-updater"


def collection_responses(*, pull_requests=None, search_items=None):
    base_sha = "a" * 40
    branch = "auto-update/whatcable-1.4.0"
    title = "whatcable: 1.2.1 -> 1.4.0"
    return {
        **{
            known_pr_search_key(package.attr): {
                "total_count": 0,
                "incomplete_results": False,
                "items": [],
            }
            for package in PACKAGES
        },
        api_key("/repos/NixOS/nixpkgs/commits/master"): {"sha": base_sha},
        api_key(
            "/repos/NixOS/nixpkgs/contents/pkgs/by-name/ha/harper-desktop/package.nix",
            ref=base_sha,
        ): content_payload(package_source("2.8.0", "lib.platforms.darwin")),
        api_key("/repos/Automattic/harper/releases/latest"): release_payload(
            "v2.8.0", "Automattic/harper"
        ),
        api_key(
            "/repos/NixOS/nixpkgs/contents/pkgs/by-name/wh/whatcable/package.nix",
            ref=base_sha,
        ): content_payload(package_source("1.2.1", '[ "aarch64-darwin" ]')),
        api_key("/repos/darrylmorley/whatcable/releases/latest"): release_payload(
            "v1.4.0",
            "darrylmorley/whatcable",
            assets=[release_asset("WhatCable.zip")],
        ),
        api_key(
            f"/repos/{UPDATER_REPOSITORY}/issues",
            state="all",
            per_page=100,
            page=1,
        ): [],
        api_key(
            "/repos/NixOS/nixpkgs/pulls",
            state="open",
            head=f"person:{branch}",
            per_page=100,
        ): pull_requests or [],
        api_key(
            "/search/issues",
            q=f'repo:NixOS/nixpkgs is:pr is:open in:title "{title}"',
            per_page=100,
        ): {
            "total_count": len(search_items or []),
            "incomplete_results": False,
            "items": search_items or [],
        },
    }


def harper_collection_responses(*, assets, issues=()):
    base_sha = "a" * 40
    branch = "auto-update/harper-desktop-2.9.1"
    title = "harper-desktop: 2.8.0 -> 2.9.1"
    updater_repository = "person/nixpkgs-darwin-updater"
    blocked_issue = {
        "number": 1,
        "title": "harper-desktop 2.9.1: required release asset is unavailable",
        "body": "<!-- nixpkgs-darwin-updater:blocked-release:harper-desktop@2.9.1 -->",
        "state": "open",
        "html_url": f"https://github.com/{updater_repository}/issues/1",
    }
    return {
        known_pr_search_key("harper-desktop"): {
            "total_count": 0,
            "incomplete_results": False,
            "items": [],
        },
        api_key("/repos/NixOS/nixpkgs/commits/master"): {"sha": base_sha},
        api_key(
            "/repos/NixOS/nixpkgs/contents/pkgs/by-name/ha/harper-desktop/package.nix",
            ref=base_sha,
        ): content_payload(package_source("2.8.0", "lib.platforms.darwin")),
        api_key("/repos/Automattic/harper/releases/latest"): release_payload(
            "v2.9.1", "Automattic/harper", assets=assets
        ),
        api_key(
            f"/repos/{updater_repository}/issues",
            state="all",
            per_page=100,
            page=1,
        ): list(issues),
        api_key(
            f"/repos/{updater_repository}/issues",
            state="all",
            per_page=100,
            page=2,
        ): [],
        method_api_key("POST", f"/repos/{updater_repository}/issues"): blocked_issue,
        method_api_key("PATCH", f"/repos/{updater_repository}/issues/1"): blocked_issue,
        method_api_key("POST", f"/repos/{updater_repository}/issues/1/comments"): {
            "id": 2
        },
        api_key(
            "/repos/NixOS/nixpkgs/pulls",
            state="open",
            head=f"person:{branch}",
            per_page=100,
        ): [],
        api_key(
            "/search/issues",
            q=f'repo:NixOS/nixpkgs is:pr is:open in:title "{title}"',
            per_page=100,
        ): {
            "total_count": 0,
            "incomplete_results": False,
            "items": [],
        },
    }


class VersionTests(unittest.TestCase):
    def test_normalizes_optional_v_prefix(self):
        self.assertEqual(check_releases.normalize_version("v1.4.0"), "1.4.0")
        self.assertEqual(check_releases.normalize_version("2.8.0"), "2.8.0")

    def test_compares_numeric_components(self):
        self.assertGreater(check_releases.compare_versions("1.10", "1.9"), 0)
        self.assertLess(check_releases.compare_versions("1.9", "1.10"), 0)
        self.assertEqual(check_releases.compare_versions("1.2", "1.2.0"), 0)

    def test_rejects_non_stable_tag_syntax(self):
        for tag in ("v2.0.0-rc1", "release-2.0", "nightly"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                check_releases.normalize_version(tag)


class RepositoryConfigurationTests(unittest.TestCase):
    def test_derives_nixpkgs_fork_from_updater_repository_owner(self):
        configuration = check_releases.resolve_repositories(
            "person/nixpkgs-darwin-updater", None
        )

        self.assertEqual(
            configuration.updater_repository, "person/nixpkgs-darwin-updater"
        )
        self.assertEqual(configuration.fork_repository, "person/nixpkgs")
        self.assertEqual(configuration.fork_owner, "person")

    def test_accepts_an_explicit_fork_repository_override(self):
        configuration = check_releases.resolve_repositories(
            "person/nixpkgs-darwin-updater", "team/custom-nixpkgs"
        )

        self.assertEqual(configuration.fork_repository, "team/custom-nixpkgs")
        self.assertEqual(configuration.fork_owner, "team")

    def test_rejects_unsafe_repository_names(self):
        for repository in (
            "missing-owner",
            "owner/../repo",
            "/repo",
            "owner/repo/extra",
        ):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                check_releases.resolve_repositories(repository, None)


class PackageSourceTests(unittest.TestCase):
    def test_extracts_exactly_one_literal_version(self):
        source = 'version = "2.8.0";\n'
        self.assertEqual(check_releases.extract_package_version(source), "2.8.0")

    def test_rejects_missing_or_multiple_versions(self):
        with self.assertRaises(ValueError):
            check_releases.extract_package_version('pname = "example";')

        with self.assertRaises(ValueError):
            check_releases.extract_package_version(
                'version = "1.0";\nversion = "2.0";\n'
            )

    def test_validates_maintainer_and_darwin_pattern(self):
        package = check_releases.Package(
            attr="harper-desktop",
            package_file="package.nix",
            upstream="Automattic/harper",
            maintainer="tyceherrman",
            platform_pattern=r"platforms\s*=\s*lib\.platforms\.darwin\s*;",
            required_asset="Harper_{version}_universal.dmg",
        )
        source = """
          version = "2.8.0";
          maintainers = with lib.maintainers; [ tyceherrman ];
          platforms = lib.platforms.darwin;
        """

        check_releases.validate_package_source(package, source)

        with self.assertRaises(ValueError):
            check_releases.validate_package_source(
                package, source.replace("tyceherrman", "someone")
            )

        with self.assertRaises(ValueError):
            check_releases.validate_package_source(
                package, source.replace("lib.platforms.darwin", "lib.platforms.unix")
            )


class CollectionTests(unittest.TestCase):
    def test_records_missing_asset_and_rechecks_the_same_release(self):
        updater_repository = "person/nixpkgs-darwin-updater"
        missing_client = check_releases.GitHubClient(
            "test-token",
            opener=MappingOpener(harper_collection_responses(assets=[])),
        )

        missing_result = check_releases.collect_updates(
            (PACKAGES[0],),
            missing_client,
            fork_owner="person",
            updater_repository=updater_repository,
        )

        self.assertEqual(missing_result.candidates, ())
        self.assertIn(
            "harper-desktop: 2.9.1 blocked by missing asset "
            "Harper_2.9.1_universal.dmg; opened "
            f"https://github.com/{updater_repository}/issues/1",
            missing_result.notes,
        )

        closed_issue = {
            "number": 1,
            "title": "harper-desktop 2.9.1: required release asset is unavailable",
            "body": "<!-- nixpkgs-darwin-updater:blocked-release:harper-desktop@2.9.1 -->",
            "state": "closed",
            "html_url": f"https://github.com/{updater_repository}/issues/1",
        }
        uploaded_client = check_releases.GitHubClient(
            "test-token",
            opener=MappingOpener(
                harper_collection_responses(
                    assets=[release_asset("Harper_2.9.1_universal.dmg")],
                    issues=[closed_issue],
                )
            ),
        )

        uploaded_result = check_releases.collect_updates(
            (PACKAGES[0],),
            uploaded_client,
            fork_owner="person",
            updater_repository=updater_repository,
        )

        self.assertEqual(
            [candidate.attr for candidate in uploaded_result.candidates],
            ["harper-desktop"],
        )
        self.assertEqual(uploaded_result.candidates[0].new_version, "2.9.1")

    def test_rejects_malformed_release_asset_data(self):
        client = check_releases.GitHubClient(
            "test-token",
            opener=MappingOpener(
                harper_collection_responses(
                    assets=[{"name": "Harper_2.9.1_universal.dmg"}]
                )
            ),
        )

        with self.assertRaisesRegex(
            check_releases.GitHubApiError, "invalid release asset"
        ):
            check_releases.collect_updates(
                (PACKAGES[0],),
                client,
                fork_owner="person",
                updater_repository=UPDATER_REPOSITORY,
            )

    def test_collects_only_newer_release(self):
        opener = MappingOpener(collection_responses())
        client = check_releases.GitHubClient("test-token", opener=opener)

        result = check_releases.collect_updates(
            PACKAGES,
            client,
            fork_owner="person",
            updater_repository=UPDATER_REPOSITORY,
        )

        self.assertEqual(result.base_sha, "a" * 40)
        self.assertEqual([item.attr for item in result.candidates], ["whatcable"])
        candidate = result.candidates[0]
        self.assertEqual(candidate.old_version, "1.2.1")
        self.assertEqual(candidate.new_version, "1.4.0")
        self.assertEqual(candidate.branch, "auto-update/whatcable-1.4.0")
        self.assertEqual(candidate.title, "whatcable: 1.2.1 -> 1.4.0")
        self.assertEqual(
            candidate.release_url,
            "https://github.com/darrylmorley/whatcable/releases/tag/v1.4.0",
        )
        self.assertIn(
            "harper-desktop: packaged at 2.8.0; latest published release is 2.8.0; no newer release found",
            result.notes,
        )

    def test_suppresses_existing_head_pull_request(self):
        pull_request = {
            "number": 123,
            "title": "whatcable: 1.2.1 -> 1.4.0",
            "html_url": "https://github.com/NixOS/nixpkgs/pull/123",
        }
        client = check_releases.GitHubClient(
            None,
            opener=MappingOpener(collection_responses(pull_requests=[pull_request])),
        )

        result = check_releases.collect_updates(
            PACKAGES,
            client,
            fork_owner="person",
            updater_repository=UPDATER_REPOSITORY,
        )

        self.assertEqual(result.candidates, ())
        self.assertIn(
            "whatcable: existing pull request https://github.com/NixOS/nixpkgs/pull/123",
            result.notes,
        )

    def test_suppresses_existing_exact_title_pull_request(self):
        pull_request = {
            "number": 456,
            "title": "whatcable: 1.2.1 -> 1.4.0",
            "html_url": "https://github.com/NixOS/nixpkgs/pull/456",
        }
        client = check_releases.GitHubClient(
            None,
            opener=MappingOpener(collection_responses(search_items=[pull_request])),
        )

        result = check_releases.collect_updates(
            PACKAGES,
            client,
            fork_owner="person",
            updater_repository=UPDATER_REPOSITORY,
        )

        self.assertEqual(result.candidates, ())
        self.assertIn(
            "whatcable: existing pull request https://github.com/NixOS/nixpkgs/pull/456",
            result.notes,
        )

    def test_api_failure_is_not_treated_as_no_update(self):
        url = "https://api.github.com/repos/NixOS/nixpkgs/commits/master"
        error = HTTPError(url, 500, "server error", {}, io.BytesIO(b"{}"))
        opener = MappingOpener({api_key("/repos/NixOS/nixpkgs/commits/master"): error})
        client = check_releases.GitHubClient(None, opener=opener)

        with self.assertRaises(check_releases.GitHubApiError):
            check_releases.collect_updates(
                PACKAGES,
                client,
                fork_owner="person",
                updater_repository=UPDATER_REPOSITORY,
            )

    def test_token_is_an_authorization_header_not_part_of_url(self):
        opener = MappingOpener(
            {api_key("/repos/NixOS/nixpkgs/commits/master"): {"sha": "a" * 40}}
        )
        client = check_releases.GitHubClient("test-token", opener=opener)

        client.get_json("/repos/NixOS/nixpkgs/commits/master")

        request = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertNotIn("test-token", request.full_url)


class GitHubClientIssueTests(unittest.TestCase):
    def test_lists_open_and_closed_issues_across_pages(self):
        repository = "person/nixpkgs-darwin-updater"
        first_issue = {
            "number": 1,
            "title": "first",
            "body": "first body",
            "state": "closed",
            "html_url": f"https://github.com/{repository}/issues/1",
        }
        opener = MappingOpener(
            {
                api_key(
                    f"/repos/{repository}/issues",
                    state="all",
                    per_page=100,
                    page=1,
                ): [first_issue],
                api_key(
                    f"/repos/{repository}/issues",
                    state="all",
                    per_page=100,
                    page=2,
                ): [],
            }
        )
        client = check_releases.GitHubClient("test-token", opener=opener)

        issues = client.list_repository_issues(repository)

        self.assertEqual(issues, (first_issue,))
        self.assertEqual(len(opener.requests), 2)

    def test_creates_comments_and_closes_issues_with_json_requests(self):
        repository = "person/nixpkgs-darwin-updater"
        issue = {
            "number": 7,
            "title": "example-app 2.0.0: required release asset is unavailable",
            "body": "marker",
            "state": "open",
            "html_url": f"https://github.com/{repository}/issues/7",
        }
        opener = MappingOpener(
            {
                method_api_key("POST", f"/repos/{repository}/issues"): issue,
                method_api_key("POST", f"/repos/{repository}/issues/7/comments"): {
                    "id": 1,
                    "html_url": f"https://github.com/{repository}/issues/7#issuecomment-1",
                },
                method_api_key("PATCH", f"/repos/{repository}/issues/7"): {
                    **issue,
                    "state": "closed",
                },
            }
        )
        client = check_releases.GitHubClient("test-token", opener=opener)

        created = client.create_issue(repository, title=issue["title"], body="marker")
        client.comment_issue(repository, 7, "Superseded by release 2.1.0")
        client.close_issue(repository, 7)

        self.assertEqual(created, issue)
        self.assertEqual(
            [request.get_method() for request in opener.requests],
            ["POST", "POST", "PATCH"],
        )
        payloads = [json.loads(request.data) for request in opener.requests]
        self.assertEqual(
            payloads,
            [
                {"title": issue["title"], "body": "marker"},
                {"body": "Superseded by release 2.1.0"},
                {"state": "closed"},
            ],
        )


class ManifestTests(unittest.TestCase):
    def write_manifest(self, directory, payload):
        path = Path(directory) / "packages.json"
        path.write_text(json.dumps(payload))
        return path

    def test_loads_valid_manifest_in_attribute_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                directory,
                [
                    {
                        "attr": package.attr,
                        "package_file": package.package_file,
                        "upstream": package.upstream,
                        "maintainer": package.maintainer,
                        "platform_pattern": package.platform_pattern,
                        "required_asset": package.required_asset,
                    }
                    for package in reversed(PACKAGES)
                ],
            )

            packages = check_releases.load_manifest(path)

        self.assertEqual(
            [package.attr for package in packages],
            [
                "harper-desktop",
                "whatcable",
            ],
        )

    def test_rejects_duplicate_or_unsafe_manifest_entries(self):
        base = {
            "attr": "whatcable",
            "package_file": "pkgs/by-name/wh/whatcable/package.nix",
            "upstream": "darrylmorley/whatcable",
            "maintainer": "tyceherrman",
            "platform_pattern": r"platforms\s*=",
            "required_asset": "WhatCable.zip",
        }
        invalid_payloads = (
            [base, base],
            [{**base, "package_file": "../outside.nix"}],
            [{**base, "upstream": "not-a-repository"}],
            [{**base, "verification": "custom-verifier"}],
            [{**base, "extra": "unexpected"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate(invalid_payloads):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    check_releases.load_manifest(
                        self.write_manifest(directory, payload)
                    )

    def test_allows_a_package_without_required_asset_gating(self):
        payload = [
            {
                "attr": "example-app",
                "package_file": "pkgs/by-name/ex/example-app/package.nix",
                "upstream": "example/example-app",
                "maintainer": "example",
                "platform_pattern": r"platforms\s*=",
                "required_asset": None,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            packages = check_releases.load_manifest(
                self.write_manifest(directory, payload)
            )

        self.assertIsNone(packages[0].required_asset)


class ActionsOutputTests(unittest.TestCase):
    def setUp(self):
        self.candidate = check_releases.Candidate(
            attr="whatcable",
            package_file="pkgs/by-name/wh/whatcable/package.nix",
            old_version="1.2.1",
            new_version="1.4.0",
            release_url="https://github.com/darrylmorley/whatcable/releases/tag/v1.4.0",
            branch="auto-update/whatcable-1.4.0",
            title="whatcable: 1.2.1 -> 1.4.0",
        )

    def test_candidate_dict_and_actions_files_are_deterministic(self):
        collection = check_releases.Collection(
            base_sha="a" * 40,
            candidates=(self.candidate,),
            notes=("harper-desktop: current at 2.8.0",),
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            summary_path = Path(directory) / "summary"

            payload = check_releases.write_actions_outputs(
                collection, output_path, summary_path
            )

            output = output_path.read_text()
            summary = summary_path.read_text()

        expected_candidate = {
            "attr": "whatcable",
            "package_file": "pkgs/by-name/wh/whatcable/package.nix",
            "old_version": "1.2.1",
            "new_version": "1.4.0",
            "release_url": "https://github.com/darrylmorley/whatcable/releases/tag/v1.4.0",
            "branch": "auto-update/whatcable-1.4.0",
            "title": "whatcable: 1.2.1 -> 1.4.0",
        }
        matrix = json.dumps({"include": [expected_candidate]}, separators=(",", ":"))
        self.assertEqual(
            check_releases.candidate_to_dict(self.candidate), expected_candidate
        )
        self.assertEqual(
            output,
            f"base_sha={'a' * 40}\nhas_updates=true\nmatrix={matrix}\n",
        )
        self.assertEqual(payload["matrix"], {"include": [expected_candidate]})
        self.assertTrue(payload["has_updates"])
        self.assertIn("harper-desktop: current at 2.8.0", summary)
        self.assertIn("1 update", summary)

    def test_empty_collection_emits_false_and_empty_matrix(self):
        collection = check_releases.Collection("b" * 40, (), ("nothing new",))
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output"
            summary_path = Path(directory) / "summary"

            check_releases.write_actions_outputs(collection, output_path, summary_path)

            output = output_path.read_text()

        self.assertEqual(
            output,
            f'base_sha={"b" * 40}\nhas_updates=false\nmatrix={{"include":[]}}\n',
        )


if __name__ == "__main__":
    unittest.main()
