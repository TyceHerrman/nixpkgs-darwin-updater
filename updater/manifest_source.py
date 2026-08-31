#!/usr/bin/env python3

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from check_releases import GitHubClient, Package, load_manifest, validate_repository_name


MANIFEST_PATH = "updater/packages.json"


@dataclass(frozen=True)
class ResolvedManifest:
    path: Path
    source_ref: str | None
    commit_sha: str | None
    packages: tuple[Package, ...]


def validate_manifest_ref(manifest_ref: str) -> str:
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", manifest_ref) is None
        or ".." in manifest_ref
        or "//" in manifest_ref
        or "@{" in manifest_ref
        or manifest_ref.endswith(("/", ".", ".lock"))
    ):
        raise ValueError(f"invalid manifest ref: {manifest_ref!r}")
    return manifest_ref


def _validate_commit_sha(commit_sha: str | None) -> str | None:
    if commit_sha is not None and re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
        raise ValueError(f"invalid manifest commit: {commit_sha!r}")
    return commit_sha


def _require_packages(packages: tuple[Package, ...]) -> tuple[Package, ...]:
    if not packages:
        raise ValueError("package manifest must contain at least one package")
    return packages


def resolve_manifest(
    *,
    client,
    repository: str,
    local_manifest: Path,
    manifest_ref: str | None,
    output_manifest: Path,
    local_sha: str | None,
) -> ResolvedManifest:
    validate_repository_name(repository)
    output_manifest = output_manifest.resolve()

    if not manifest_ref:
        packages = _require_packages(load_manifest(local_manifest))
        output_manifest.write_text(local_manifest.read_text(encoding="utf-8"), encoding="utf-8")
        return ResolvedManifest(
            output_manifest,
            None,
            _validate_commit_sha(local_sha),
            packages,
        )

    manifest_ref = validate_manifest_ref(manifest_ref)
    if client is None:
        raise ValueError("a GitHub client is required for a manifest ref")
    commits = client.get_json(
        f"/repos/{repository}/commits",
        query={"sha": manifest_ref, "per_page": 1},
    )
    if not isinstance(commits, list) or len(commits) != 1:
        raise ValueError(f"cannot resolve manifest ref: {manifest_ref!r}")
    commit = commits[0]
    commit_sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(commit_sha, str):
        raise ValueError(f"cannot resolve manifest ref: {manifest_ref!r}")
    _validate_commit_sha(commit_sha)

    content = client.get_text_content(
        repository,
        MANIFEST_PATH,
        ref=commit_sha,
    )
    output_manifest.write_text(content, encoding="utf-8")
    packages = _require_packages(load_manifest(output_manifest))
    return ResolvedManifest(
        output_manifest,
        manifest_ref,
        commit_sha,
        packages,
    )


def write_summary(resolved: ResolvedManifest, summary_path: Path) -> None:
    if resolved.source_ref is None:
        source_line = "- Source: checked-out `updater/packages.json`"
    else:
        source_line = f"- Source ref: `{resolved.source_ref}`"
    commit_line = (
        f"- Resolved commit: `{resolved.commit_sha}`"
        if resolved.commit_sha is not None
        else "- Resolved commit: unavailable"
    )
    package_list = ", ".join(f"`{package.attr}`" for package in resolved.packages)
    lines = [
        "## Package manifest",
        "",
        source_line,
        commit_line,
        f"- Packages: {package_list}",
    ]
    with summary_path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve and validate the updater package manifest"
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
    )
    parser.add_argument(
        "--local-manifest",
        type=Path,
        default=Path(__file__).with_name("packages.json"),
    )
    parser.add_argument("--manifest-ref", default=os.environ.get("UPDATER_MANIFEST_REF"))
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--local-sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.repository:
        raise ValueError("GITHUB_REPOSITORY or --repository is required")
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_manifest(
        client=(
            GitHubClient(os.environ.get("GITHUB_TOKEN"))
            if args.manifest_ref
            else None
        ),
        repository=args.repository,
        local_manifest=args.local_manifest,
        manifest_ref=args.manifest_ref,
        output_manifest=args.output_manifest,
        local_sha=args.local_sha,
    )
    if args.github_output is not None:
        values = {
            "manifest_path": resolved.path,
            "manifest_ref": resolved.source_ref or "",
            "manifest_sha": resolved.commit_sha or "",
        }
        with args.github_output.open("a", encoding="utf-8") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
    if args.github_summary is not None:
        write_summary(resolved, args.github_summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"manifest resolution failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
