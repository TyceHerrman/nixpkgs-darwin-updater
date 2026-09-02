#!/usr/bin/env python3

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from check_releases import GitHubClient, resolve_repositories, validate_repository_name


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    login: str
    fork_repository: str
    review_repository: str | None
    review_mode: str


@dataclass(frozen=True)
class ReviewIntegration:
    mode: str
    repository: str | None
    token: str | None


def resolve_review_integration(
    *,
    review_enabled: bool,
    updater_repository: str,
    dispatch_repository: str | None,
    dispatch_token: str | None,
    direct_repository: str | None,
    direct_token: str | None,
) -> ReviewIntegration:
    if not review_enabled:
        return ReviewIntegration("disabled", None, None)
    if dispatch_repository:
        if not dispatch_token:
            raise PreflightError("NIXPKGS_REVIEW_DISPATCH_TOKEN is not configured")
        return ReviewIntegration("controller", dispatch_repository, dispatch_token)
    if not direct_repository:
        direct_repository = (
            f"{updater_repository.split('/', 1)[0]}/nixpkgs-review-gha"
        )
    if not direct_token:
        raise PreflightError("NIXPKGS_REVIEW_GHA_TOKEN is not configured")
    return ReviewIntegration("direct", direct_repository, direct_token)


def run_preflight(
    pr_client,
    *,
    fork_repository: str,
    review_enabled: bool,
    review_client,
    review_repository: str | None,
    review_mode: str = "direct",
) -> PreflightResult:
    validate_repository_name(fork_repository)
    user = pr_client.get_json("/user")
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise PreflightError("PR token did not return a GitHub user identity")

    repository = pr_client.get_json(f"/repos/{fork_repository}")
    if not isinstance(repository, dict):
        raise PreflightError("nixpkgs fork response is invalid")
    permissions = repository.get("permissions")
    parent = repository.get("parent")
    if repository.get("full_name") != fork_repository:
        raise PreflightError("nixpkgs fork response names a different repository")
    if repository.get("fork") is not True or not isinstance(parent, dict):
        raise PreflightError("configured repository is not a fork")
    if parent.get("full_name") != "NixOS/nixpkgs":
        raise PreflightError("configured repository is not a NixOS/nixpkgs fork")
    if not isinstance(permissions, dict) or permissions.get("push") is not True:
        raise PreflightError("PR token does not have push access to the nixpkgs fork")

    resolved_review = None
    if review_enabled:
        if review_client is None or not review_repository:
            raise PreflightError("review integration is enabled but not configured")
        validate_repository_name(review_repository)
        workflow = review_client.get_json(
            f"/repos/{review_repository}/actions/workflows/review.yml"
        )
        if not isinstance(workflow, dict) or workflow.get("state") != "active":
            raise PreflightError("review.yml is unavailable or inactive")
        resolved_review = review_repository

    return PreflightResult(login, fork_repository, resolved_review, review_mode)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate updater repository access without making changes"
    )
    parser.add_argument("--github-summary", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    updater_repository = os.environ.get("GITHUB_REPOSITORY")
    if not updater_repository:
        raise PreflightError("GITHUB_REPOSITORY is not configured")
    try:
        repositories = resolve_repositories(
            updater_repository, os.environ.get("NIXPKGS_FORK_REPOSITORY")
        )
    except ValueError as error:
        raise PreflightError(str(error)) from error

    pr_token = os.environ.get("NIXPKGS_PR_TOKEN")
    if not pr_token:
        raise PreflightError("NIXPKGS_PR_TOKEN is not configured")
    review_setting = os.environ.get("NIXPKGS_REVIEW_GHA_ENABLED", "false").lower()
    if review_setting not in {"true", "false"}:
        raise PreflightError("NIXPKGS_REVIEW_GHA_ENABLED must be true or false")
    review_enabled = review_setting == "true"
    integration = resolve_review_integration(
        review_enabled=review_enabled,
        updater_repository=repositories.updater_repository,
        dispatch_repository=os.environ.get("NIXPKGS_REVIEW_DISPATCH_REPOSITORY"),
        dispatch_token=os.environ.get("NIXPKGS_REVIEW_DISPATCH_TOKEN"),
        direct_repository=os.environ.get("NIXPKGS_REVIEW_GHA_REPOSITORY"),
        direct_token=os.environ.get("NIXPKGS_REVIEW_GHA_TOKEN"),
    )

    result = run_preflight(
        GitHubClient(pr_token),
        fork_repository=repositories.fork_repository,
        review_enabled=review_enabled,
        review_client=(
            GitHubClient(integration.token)
            if integration.mode != "disabled"
            else None
        ),
        review_repository=integration.repository,
        review_mode=integration.mode,
    )
    payload = {
        "login": result.login,
        "fork_repository": result.fork_repository,
        "review_repository": result.review_repository,
    }
    print(json.dumps(payload, separators=(",", ":")))
    if args.github_summary is not None:
        lines = [
            "## Darwin updater preflight",
            "",
            f"- PR identity: `{result.login}`",
            f"- Nixpkgs fork: `{result.fork_repository}`",
            "- Fork relationship and push access: verified",
        ]
        if result.review_repository is not None:
            if result.review_mode == "controller":
                lines.append(
                    f"- Review controller workflow: `{result.review_repository}/review.yml`"
                )
            else:
                lines.append(
                    f"- Direct review runner workflow: `{result.review_repository}/review.yml`"
                )
        else:
            lines.append("- Review integration: disabled")
        with args.github_summary.open("a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PreflightError, ValueError) as error:
        print(f"preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
