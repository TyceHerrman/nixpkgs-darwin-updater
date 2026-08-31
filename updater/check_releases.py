#!/usr/bin/env python3

import argparse
import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
MANIFEST_FIELDS = {
    "attr",
    "package_file",
    "upstream",
    "maintainer",
    "platform_pattern",
    "required_asset",
}


class GitHubApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryConfiguration:
    updater_repository: str
    fork_repository: str
    fork_owner: str


@dataclass(frozen=True)
class Package:
    attr: str
    package_file: str
    upstream: str
    maintainer: str
    platform_pattern: str
    required_asset: str | None


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    state: str
    size: int


@dataclass(frozen=True)
class BlockedReleaseIssue:
    number: int
    version: str
    state: str
    url: str


@dataclass(frozen=True)
class AssetGate:
    actionable: bool
    note: str | None


@dataclass(frozen=True)
class Candidate:
    attr: str
    package_file: str
    old_version: str
    new_version: str
    release_url: str
    branch: str
    title: str


@dataclass(frozen=True)
class Collection:
    base_sha: str
    candidates: tuple[Candidate, ...]
    notes: tuple[str, ...]


class GitHubClient:
    def __init__(self, token: str | None, *, opener=urlopen):
        self.token = token
        self.opener = opener

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ):
        if not path.startswith("/"):
            raise ValueError("GitHub API path must begin with a slash")

        url = f"{API_ROOT}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "nixpkgs-darwin-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode()
        request = Request(url, data=data, headers=headers, method=method)

        try:
            with self.opener(request) as response:
                return json.load(response)
        except (
            HTTPError,
            URLError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise GitHubApiError(
                f"GitHub API request failed for {path}: {error}"
            ) from error

    def get_json(self, path: str, *, query: dict[str, object] | None = None):
        return self._request_json("GET", path, query=query)

    def post_json(self, path: str, payload: dict[str, object]):
        return self._request_json("POST", path, payload=payload)

    def patch_json(self, path: str, payload: dict[str, object]):
        return self._request_json("PATCH", path, payload=payload)

    def list_repository_issues(self, repository: str):
        issues: list[dict[str, object]] = []
        page = 1
        while True:
            payload = self.get_json(
                f"/repos/{repository}/issues",
                query={"state": "all", "per_page": 100, "page": page},
            )
            if not isinstance(payload, list):
                raise GitHubApiError("invalid updater issue list response")
            if not payload:
                break
            for item in payload:
                if not isinstance(item, dict):
                    raise GitHubApiError("invalid updater issue list response")
                if "pull_request" not in item:
                    issues.append(item)
            page += 1
        return tuple(issues)

    def create_issue(self, repository: str, *, title: str, body: str):
        return self.post_json(
            f"/repos/{repository}/issues", {"title": title, "body": body}
        )

    def comment_issue(self, repository: str, number: int, body: str):
        self.post_json(f"/repos/{repository}/issues/{number}/comments", {"body": body})

    def close_issue(self, repository: str, number: int):
        self.patch_json(f"/repos/{repository}/issues/{number}", {"state": "closed"})

    def get_text_content(self, repository: str, path: str, *, ref: str) -> str:
        payload = self.get_json(
            f"/repos/{repository}/contents/{path}", query={"ref": ref}
        )
        try:
            if not isinstance(payload, dict):
                raise TypeError("contents response is not an object")
            if payload.get("type") != "file" or payload.get("encoding") != "base64":
                raise ValueError("contents response is not a base64 file")
            content = payload["content"]
            if not isinstance(content, str):
                raise TypeError("contents payload is not text")
            return base64.b64decode(content).decode()
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
            raise GitHubApiError(
                f"invalid GitHub contents response for {repository}/{path}: {error}"
            ) from error


def validate_repository_name(repository: str) -> str:
    component = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
    if re.fullmatch(rf"{component}/{component}", repository) is None:
        raise ValueError(f"invalid GitHub repository: {repository!r}")
    return repository


def resolve_repositories(
    updater_repository: str, fork_repository: str | None
) -> RepositoryConfiguration:
    validate_repository_name(updater_repository)
    updater_owner = updater_repository.split("/", 1)[0]
    resolved_fork = fork_repository or f"{updater_owner}/nixpkgs"
    validate_repository_name(resolved_fork)
    return RepositoryConfiguration(
        updater_repository=updater_repository,
        fork_repository=resolved_fork,
        fork_owner=resolved_fork.split("/", 1)[0],
    )


def load_manifest(path: Path) -> tuple[Package, ...]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read package manifest {path}: {error}") from error
    if not isinstance(payload, list):
        raise TypeError("package manifest must be a JSON array")

    packages: list[Package] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or set(item) != MANIFEST_FIELDS:
            raise ValueError(f"manifest entry {index} has unexpected fields")
        string_fields = MANIFEST_FIELDS - {"required_asset"}
        if not all(isinstance(item[field], str) for field in string_fields):
            raise ValueError(f"manifest entry {index} fields must be strings")
        if item["required_asset"] is not None and not isinstance(
            item["required_asset"], str
        ):
            raise ValueError(
                f"manifest entry {index} required_asset must be a string or null"
            )

        package = Package(**item)
        if re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", package.attr) is None:
            raise ValueError(f"invalid package attribute: {package.attr!r}")
        if package.attr in seen:
            raise ValueError(f"duplicate package attribute: {package.attr}")
        seen.add(package.attr)

        package_path = PurePosixPath(package.package_file)
        if (
            package_path.is_absolute()
            or not package_path.parts
            or package_path.parts[0] != "pkgs"
            or ".." in package_path.parts
            or package_path.suffix != ".nix"
        ):
            raise ValueError(f"unsafe package path: {package.package_file!r}")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", package.upstream) is None:
            raise ValueError(f"invalid upstream repository: {package.upstream!r}")
        if re.fullmatch(r"[A-Za-z0-9_]+", package.maintainer) is None:
            raise ValueError(f"invalid maintainer name: {package.maintainer!r}")
        try:
            re.compile(package.platform_pattern)
        except re.error as error:
            raise ValueError(
                f"invalid platform pattern for {package.attr}: {error}"
            ) from error
        if package.required_asset is not None:
            render_required_asset(package.required_asset, "0.0.0")
        packages.append(package)

    return tuple(sorted(packages, key=lambda package: package.attr))


def normalize_version(tag: str) -> str:
    match = re.fullmatch(r"v?([0-9]+(?:\.[0-9]+)*)", tag)
    if match is None:
        raise ValueError(f"unsupported stable release tag: {tag!r}")
    return match.group(1)


def compare_versions(left: str, right: str) -> int:
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    length = max(len(left_parts), len(right_parts))
    left_parts += (0,) * (length - len(left_parts))
    right_parts += (0,) * (length - len(right_parts))
    return (left_parts > right_parts) - (left_parts < right_parts)


def extract_package_version(source: str) -> str:
    versions = re.findall(r'^\s*version\s*=\s*"([^"]+)"\s*;', source, re.MULTILINE)
    if len(versions) != 1:
        raise ValueError(
            f"expected one literal version assignment, found {len(versions)}"
        )
    return normalize_version(versions[0])


def render_required_asset(template: str, version: str) -> str:
    if template.count("{version}") > 1:
        raise ValueError(f"invalid required asset template: {template!r}")
    asset_name = template.replace("{version}", version)
    if (
        not asset_name
        or "{" in asset_name
        or "}" in asset_name
        or re.fullmatch(r"[A-Za-z0-9+._-]+", asset_name) is None
    ):
        raise ValueError(f"invalid required asset template: {template!r}")
    return asset_name


def blocked_release_marker(attr: str, version: str) -> str:
    return f"<!-- nixpkgs-darwin-updater:blocked-release:{attr}@{version} -->"


def _blocked_release_issues(client, repository: str, attr: str):
    prefix = f"nixpkgs-darwin-updater:blocked-release:{attr}@"
    issues: list[BlockedReleaseIssue] = []
    for item in client.list_repository_issues(repository):
        if not isinstance(item, dict):
            raise GitHubApiError("invalid updater issue response")
        body = item.get("body")
        if not isinstance(body, str) or prefix not in body:
            continue
        match = re.search(
            rf"<!--\s*{re.escape(prefix)}([0-9]+(?:\.[0-9]+)*)\s*-->",
            body,
        )
        if match is None:
            continue
        number = item.get("number")
        state = item.get("state")
        url = item.get("html_url")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or state not in {"open", "closed"}
            or not isinstance(url, str)
            or not url
        ):
            raise GitHubApiError("invalid blocked-release issue response")
        issues.append(BlockedReleaseIssue(number, match.group(1), state, url))
    return tuple(issues)


def gate_release_asset(
    client,
    *,
    repository: str,
    package: Package,
    version: str,
    release_url: str,
    assets: tuple[ReleaseAsset, ...],
) -> AssetGate:
    issues = _blocked_release_issues(client, repository, package.attr)
    for issue in issues:
        if issue.state == "open" and compare_versions(version, issue.version) > 0:
            client.comment_issue(
                repository,
                issue.number,
                f"Superseded by release {version}: {release_url}",
            )
            client.close_issue(repository, issue.number)

    matching_issue = next(
        (issue for issue in issues if compare_versions(version, issue.version) == 0),
        None,
    )
    if matching_issue is not None:
        return AssetGate(
            False,
            f"{package.attr}: {version} suppressed by blocked-release issue "
            f"{matching_issue.url}",
        )

    if package.required_asset is None:
        return AssetGate(True, None)

    required_asset = render_required_asset(package.required_asset, version)
    asset_is_ready = any(
        asset.name == required_asset and asset.state == "uploaded" and asset.size > 0
        for asset in assets
    )
    if asset_is_ready:
        return AssetGate(True, None)

    title = f"{package.attr} {version}: required release asset is unavailable"
    body = "\n".join(
        (
            blocked_release_marker(package.attr, version),
            "",
            f"The upstream release does not contain a ready `{required_asset}` asset.",
            "",
            f"- Upstream release: {release_url}",
            f"- Expected asset: `{required_asset}`",
            "",
            "The updater will ignore this version and evaluate the next release.",
        )
    )
    issue = client.create_issue(repository, title=title, body=body)
    issue_url = issue.get("html_url") if isinstance(issue, dict) else None
    if not isinstance(issue_url, str) or not issue_url:
        raise GitHubApiError("created blocked-release issue has no URL")
    return AssetGate(
        False,
        f"{package.attr}: {version} blocked by missing asset {required_asset}; "
        f"opened {issue_url}",
    )


def validate_package_source(package: Package, source: str) -> None:
    if re.search(rf"\b{re.escape(package.maintainer)}\b", source) is None:
        raise ValueError(f"{package.attr}: expected maintainer is absent")
    if re.search(package.platform_pattern, source, re.MULTILINE) is None:
        raise ValueError(
            f"{package.attr}: expected Darwin platform declaration is absent"
        )


def _require_base_sha(payload) -> str:
    if not isinstance(payload, dict):
        raise GitHubApiError("invalid upstream commit response")
    sha = payload.get("sha")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise GitHubApiError("invalid upstream commit SHA")
    return sha


def _require_latest_release(
    payload, upstream: str
) -> tuple[str, str, tuple[ReleaseAsset, ...]]:
    if not isinstance(payload, dict):
        raise GitHubApiError(f"invalid latest release response for {upstream}")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise GitHubApiError(f"latest release for {upstream} is not stable")
    tag = payload.get("tag_name")
    release_url = payload.get("html_url")
    if not isinstance(tag, str) or not tag:
        raise GitHubApiError(f"latest release for {upstream} has no tag")
    if not isinstance(release_url, str) or not release_url:
        raise GitHubApiError(f"latest release for {upstream} has no URL")
    assets_payload = payload.get("assets")
    if not isinstance(assets_payload, list):
        raise GitHubApiError(f"latest release for {upstream} has invalid assets")
    assets: list[ReleaseAsset] = []
    for index, item in enumerate(assets_payload):
        if not isinstance(item, dict):
            raise GitHubApiError(
                f"invalid release asset {index} for {upstream}: expected an object"
            )
        name = item.get("name")
        state = item.get("state")
        size = item.get("size")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(state, str)
            or not state
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise GitHubApiError(f"invalid release asset {index} for {upstream}")
        assets.append(ReleaseAsset(name=name, state=state, size=size))
    return normalize_version(tag), release_url, tuple(assets)


def _existing_pr_url(
    client: GitHubClient, *, fork_owner: str, branch: str, title: str
) -> str | None:
    pulls = client.get_json(
        "/repos/NixOS/nixpkgs/pulls",
        query={
            "state": "open",
            "head": f"{fork_owner}:{branch}",
            "per_page": 100,
        },
    )
    if not isinstance(pulls, list):
        raise GitHubApiError("invalid pull request list response")
    if pulls:
        url = pulls[0].get("html_url") if isinstance(pulls[0], dict) else None
        if not isinstance(url, str) or not url:
            raise GitHubApiError("existing pull request has no URL")
        return url

    search = client.get_json(
        "/search/issues",
        query={
            "q": f'repo:NixOS/nixpkgs is:pr is:open in:title "{title}"',
            "per_page": 100,
        },
    )
    if not isinstance(search, dict) or not isinstance(search.get("items"), list):
        raise GitHubApiError("invalid pull request search response")
    for item in search["items"]:
        if isinstance(item, dict) and item.get("title") == title:
            url = item.get("html_url")
            if not isinstance(url, str) or not url:
                raise GitHubApiError("matching pull request has no URL")
            return url
    return None


def collect_updates(
    packages: tuple[Package, ...] | list[Package],
    client: GitHubClient,
    *,
    fork_owner: str,
    updater_repository: str,
) -> Collection:
    base_sha = _require_base_sha(client.get_json("/repos/NixOS/nixpkgs/commits/master"))
    candidates: list[Candidate] = []
    notes: list[str] = []

    for package in sorted(packages, key=lambda item: item.attr):
        source = client.get_text_content(
            "NixOS/nixpkgs", package.package_file, ref=base_sha
        )
        validate_package_source(package, source)
        old_version = extract_package_version(source)
        new_version, release_url, release_assets = _require_latest_release(
            client.get_json(f"/repos/{package.upstream}/releases/latest"),
            package.upstream,
        )
        if compare_versions(new_version, old_version) <= 0:
            notes.append(f"{package.attr}: current at {old_version}")
            continue

        asset_gate = gate_release_asset(
            client,
            repository=updater_repository,
            package=package,
            version=new_version,
            release_url=release_url,
            assets=release_assets,
        )
        if not asset_gate.actionable:
            if asset_gate.note is not None:
                notes.append(asset_gate.note)
            continue

        branch = f"auto-update/{package.attr}-{new_version}"
        title = f"{package.attr}: {old_version} -> {new_version}"
        existing_url = _existing_pr_url(
            client,
            fork_owner=fork_owner,
            branch=branch,
            title=title,
        )
        if existing_url is not None:
            notes.append(f"{package.attr}: existing pull request {existing_url}")
            continue

        candidates.append(
            Candidate(
                attr=package.attr,
                package_file=package.package_file,
                old_version=old_version,
                new_version=new_version,
                release_url=release_url,
                branch=branch,
                title=title,
            )
        )

    return Collection(base_sha, tuple(candidates), tuple(notes))


def candidate_to_dict(candidate: Candidate) -> dict[str, str]:
    return {
        "attr": candidate.attr,
        "package_file": candidate.package_file,
        "old_version": candidate.old_version,
        "new_version": candidate.new_version,
        "release_url": candidate.release_url,
        "branch": candidate.branch,
        "title": candidate.title,
    }


def collection_payload(collection: Collection) -> dict[str, object]:
    return {
        "base_sha": collection.base_sha,
        "has_updates": bool(collection.candidates),
        "matrix": {
            "include": [
                candidate_to_dict(candidate) for candidate in collection.candidates
            ]
        },
        "notes": list(collection.notes),
    }


def write_actions_outputs(
    collection: Collection, output_path: Path, summary_path: Path
) -> dict[str, object]:
    payload = collection_payload(collection)
    matrix_json = json.dumps(payload["matrix"], separators=(",", ":"))
    has_updates = "true" if payload["has_updates"] else "false"
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"base_sha={collection.base_sha}\n")
        output.write(f"has_updates={has_updates}\n")
        output.write(f"matrix={matrix_json}\n")

    count = len(collection.candidates)
    noun = "update" if count == 1 else "updates"
    summary_lines = [
        "## Darwin maintainer release check",
        "",
        f"- Upstream nixpkgs base: `{collection.base_sha}`",
        f"- Result: {count} {noun}",
    ]
    if collection.notes:
        summary_lines.extend(["", "### Notes", ""])
        summary_lines.extend(f"- {note}" for note in collection.notes)
    with summary_path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(summary_lines) + "\n")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect GitHub releases for Darwin-only nixpkgs packages"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("packages.json"),
    )
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    parser.add_argument(
        "--updater-repository", default=os.environ.get("GITHUB_REPOSITORY")
    )
    parser.add_argument(
        "--fork-repository", default=os.environ.get("NIXPKGS_FORK_REPOSITORY")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if (args.github_output is None) != (args.github_summary is None):
        raise ValueError(
            "--github-output and --github-summary must be supplied together"
        )
    if not args.updater_repository:
        raise ValueError("GITHUB_REPOSITORY or --updater-repository is required")
    repositories = resolve_repositories(args.updater_repository, args.fork_repository)
    packages = load_manifest(args.manifest)
    client = GitHubClient(os.environ.get("GITHUB_TOKEN"))
    collection = collect_updates(
        packages,
        client,
        fork_owner=repositories.fork_owner,
        updater_repository=repositories.updater_repository,
    )
    if args.github_output is not None:
        payload = write_actions_outputs(
            collection, args.github_output, args.github_summary
        )
    else:
        payload = collection_payload(collection)
    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
