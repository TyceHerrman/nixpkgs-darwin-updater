#!/usr/bin/env python3

"""Gate nixpkgs-review-gha dispatches on upstream pull-request checks."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


@dataclass(frozen=True)
class DispatchResult:
    dispatched: bool
    existing_url: str | None
    controller_url: str | None = None


@dataclass(frozen=True)
class GateOutcome:
    pull_request: PullRequest
    checks: tuple[tuple[str, str, str, str], ...]
    dispatch: DispatchResult


class CheckFailure(RuntimeError):
    pass


class CheckTimeout(RuntimeError):
    pass


class GateError(RuntimeError):
    pass


class GitHubCli:
    def __init__(self, token, *, review_repository=None, runner=subprocess.run):
        self.token = token
        self.review_repository = review_repository
        self.runner = runner

    def _require_review_repository(self):
        if not self.review_repository:
            raise GateError("review repository is not configured")
        return self.review_repository

    def _run_json(self, command, *, allowed_returncodes=(0,), input=None):
        environment = os.environ.copy()
        environment["GH_TOKEN"] = self.token
        result = self.runner(
            command,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            input=input,
        )
        if result.returncode not in allowed_returncodes:
            raise GateError(
                f"command failed with exit code {result.returncode}: "
                f"{result.stderr.strip()}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GateError("GitHub CLI returned invalid JSON") from error

    def _run(self, command):
        environment = os.environ.copy()
        environment["GH_TOKEN"] = self.token
        result = self.runner(
            command,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise GateError(
                f"command failed with exit code {result.returncode}: "
                f"{result.stderr.strip()}"
            )

    def find_open_pull_request(self, head):
        payload = self._run_json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "repos/NixOS/nixpkgs/pulls",
                "-f",
                "state=open",
                "-f",
                f"head={head}",
                "-f",
                "per_page=100",
            ]
        )
        if not isinstance(payload, list) or len(payload) != 1:
            count = len(payload) if isinstance(payload, list) else "invalid"
            raise GateError(f"expected one open pull request for {head}, found {count}")
        item = payload[0]
        number = item.get("number") if isinstance(item, dict) else None
        url = item.get("html_url") if isinstance(item, dict) else None
        if not isinstance(number, int) or number <= 0 or not isinstance(url, str):
            raise GateError("GitHub returned an invalid pull request")
        return PullRequest(number, url)

    def pull_request_checks(self, pull_request):
        payload = self._run_json(
            [
                "gh",
                "pr",
                "checks",
                str(pull_request),
                "--repo",
                "NixOS/nixpkgs",
                "--json",
                "bucket,link,name,state,workflow",
            ],
            allowed_returncodes=(0, 1, 8),
        )
        if not isinstance(payload, list):
            raise GateError("GitHub returned an invalid check list")
        return payload

    def list_review_runs(self):
        review_repository = self._require_review_repository()
        payload = self._run_json(
            [
                "gh",
                "run",
                "list",
                "--repo",
                review_repository,
                "--workflow",
                "review.yml",
                "--event",
                "workflow_dispatch",
                "--limit",
                "1000",
                "--json",
                "displayTitle,status,conclusion,url",
            ]
        )
        if not isinstance(payload, list):
            raise GateError("GitHub returned an invalid workflow run list")
        return payload

    def dispatch_review(self, pull_request):
        review_repository = self._require_review_repository()
        self._run(
            [
                "gh",
                "workflow",
                "run",
                "review.yml",
                "--repo",
                review_repository,
                "--ref",
                "main",
                "-f",
                f"pr={pull_request}",
                "-f",
                "x86_64-linux=false",
                "-f",
                "aarch64-linux=false",
                "-f",
                "x86_64-darwin=yes_sandbox_relaxed",
                "-f",
                "aarch64-darwin=yes_sandbox_relaxed",
                "-f",
                "riscv64-linux=false",
            ]
        )

    def dispatch_controller_review(self, pull_request, attribute):
        review_repository = self._require_review_repository()
        payload = {
            "ref": "main",
            "inputs": {
                "pr": str(pull_request),
                "attribute": attribute,
                "platform-scope": "darwin",
                "force": False,
            },
        }
        try:
            reply = self._run_json(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{review_repository}/actions/workflows/review.yml/dispatches",
                    "-H",
                    "X-GitHub-Api-Version: 2026-03-10",
                    "--input",
                    "-",
                ],
                input=json.dumps(payload, separators=(",", ":")),
            )
        except GateError as error:
            raise GateError(
                "controller dispatch response is uncertain; do not retry automatically"
            ) from error

        run_id = reply.get("workflow_run_id") if isinstance(reply, dict) else None
        run_url = reply.get("run_url") if isinstance(reply, dict) else None
        html_url = reply.get("html_url") if isinstance(reply, dict) else None
        if (
            type(run_id) is not int
            or run_id <= 0
            or run_url
            != f"https://api.github.com/repos/{review_repository}/actions/runs/{run_id}"
            or html_url
            != f"https://github.com/{review_repository}/actions/runs/{run_id}"
        ):
            raise GateError(
                "controller dispatch response is uncertain; do not retry automatically"
            )
        return html_url


def dispatch_review_if_missing(client, pull_request):
    title = f"review #{pull_request}"
    runs = client.list_review_runs()
    if not isinstance(runs, list):
        raise GateError("GitHub returned an invalid workflow run list")
    for run in runs:
        if not isinstance(run, dict):
            raise GateError("GitHub returned an invalid workflow run")
        display_title = run.get("displayTitle")
        if display_title == title or (
            isinstance(display_title, str)
            and re.fullmatch(rf"{re.escape(title)} \(.+\)", display_title)
        ):
            url = run.get("url")
            if not isinstance(url, str):
                raise GateError("existing review run has no URL")
            return DispatchResult(False, url)

    client.dispatch_review(pull_request)
    return DispatchResult(True, None)


def run_review_gate(
    read_client,
    dispatch_client,
    *,
    head,
    interval_seconds,
    max_attempts,
    stable_observations,
    sleep,
):
    pull_request = read_client.find_open_pull_request(head)
    checks = wait_for_passing_checks(
        lambda: read_client.pull_request_checks(pull_request.number),
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        stable_observations=stable_observations,
        sleep=sleep,
    )
    dispatch = dispatch_review_if_missing(dispatch_client, pull_request.number)
    return GateOutcome(pull_request, checks, dispatch)


def run_controller_review_gate(
    read_client,
    controller_client,
    *,
    head,
    attribute,
    interval_seconds,
    max_attempts,
    stable_observations,
    sleep,
):
    pull_request = read_client.find_open_pull_request(head)
    checks = wait_for_passing_checks(
        lambda: read_client.pull_request_checks(pull_request.number),
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        stable_observations=stable_observations,
        sleep=sleep,
    )
    controller_url = controller_client.dispatch_controller_review(
        pull_request.number, attribute
    )
    return GateOutcome(
        pull_request,
        checks,
        DispatchResult(True, None, controller_url),
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "wait for upstream nixpkgs checks and dispatch a Darwin-only "
            "nixpkgs-review-gha run"
        )
    )
    parser.add_argument("--head", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=10_800)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--stable-observations", type=int, default=3)
    parser.add_argument("--github-summary", type=Path)
    parser.add_argument("--attribute")
    parser.add_argument(
        "--review-repository",
        default=os.environ.get("NIXPKGS_REVIEW_GHA_REPOSITORY"),
    )
    parser.add_argument(
        "--dispatch-repository",
        default=os.environ.get("NIXPKGS_REVIEW_DISPATCH_REPOSITORY"),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if (
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*:auto-update/"
            r"[a-z0-9][a-z0-9+._-]*-[0-9]+(?:\.[0-9]+)*",
            args.head,
        )
        is None
    ):
        raise GateError(f"refusing unexpected pull-request head: {args.head!r}")
    if args.timeout_seconds <= 0 or args.interval_seconds <= 0:
        raise GateError("timeout and interval must be positive")
    if args.stable_observations <= 0:
        raise GateError("stable observations must be positive")
    read_token = os.environ.get("GITHUB_TOKEN")
    if not read_token:
        raise GateError("GITHUB_TOKEN is not configured")

    max_attempts = max(1, args.timeout_seconds // args.interval_seconds)
    if args.dispatch_repository:
        if not args.attribute:
            raise GateError("--attribute is required for controller dispatch")
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                args.dispatch_repository,
            )
            is None
        ):
            raise GateError("dispatch repository is invalid")
        dispatch_token = os.environ.get("NIXPKGS_REVIEW_DISPATCH_TOKEN")
        if not dispatch_token:
            raise GateError("NIXPKGS_REVIEW_DISPATCH_TOKEN is not configured")
        outcome = run_controller_review_gate(
            GitHubCli(read_token),
            GitHubCli(dispatch_token, review_repository=args.dispatch_repository),
            head=args.head,
            attribute=args.attribute,
            interval_seconds=args.interval_seconds,
            max_attempts=max_attempts,
            stable_observations=args.stable_observations,
            sleep=time.sleep,
        )
        result = f"requested shared review controller: {outcome.dispatch.controller_url}"
    else:
        if (
            not args.review_repository
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                args.review_repository,
            )
            is None
        ):
            raise GateError("review repository is not configured or invalid")
        dispatch_token = os.environ.get("NIXPKGS_REVIEW_GHA_TOKEN")
        if not dispatch_token:
            raise GateError("NIXPKGS_REVIEW_GHA_TOKEN is not configured")
        outcome = run_review_gate(
            GitHubCli(read_token),
            GitHubCli(dispatch_token, review_repository=args.review_repository),
            head=args.head,
            interval_seconds=args.interval_seconds,
            max_attempts=max_attempts,
            stable_observations=args.stable_observations,
            sleep=time.sleep,
        )
        if outcome.dispatch.dispatched:
            result = "dispatched a Darwin-only nixpkgs-review-gha run"
        else:
            result = f"review already exists: {outcome.dispatch.existing_url}"
    message = (
        f"{outcome.pull_request.url}: {len(outcome.checks)} checks passed or "
        f"skipped; {result}"
    )
    print(message)
    if args.github_summary is not None:
        with args.github_summary.open("a", encoding="utf-8") as summary:
            summary.write("## Darwin nixpkgs-review gate\n\n")
            summary.write(f"- Pull request: {outcome.pull_request.url}\n")
            summary.write(f"- Checks: {len(outcome.checks)} passed or skipped\n")
            if args.dispatch_repository:
                summary.write(
                    "- Controller request: "
                    f"[review.yml run]({outcome.dispatch.controller_url})\n"
                )
                summary.write("- Downstream build: pending controller dispatch\n")
            else:
                summary.write(f"- Review runner: {result}\n")
    return 0


def wait_for_passing_checks(
    fetch_checks,
    *,
    interval_seconds,
    max_attempts,
    stable_observations,
    sleep,
):
    previous = None
    stable_count = 0

    for attempt in range(max_attempts):
        checks = fetch_checks()
        snapshot = tuple(
            sorted(
                (
                    item["name"],
                    item["bucket"],
                    item["state"],
                    item["workflow"],
                )
                for item in checks
            )
        )
        buckets = {item[1] for item in snapshot}

        if buckets & {"fail", "cancel"}:
            failed = ", ".join(
                f"{name} ({bucket}/{state})"
                for name, bucket, state, _workflow in snapshot
                if bucket in {"fail", "cancel"}
            )
            raise CheckFailure(f"pull-request checks did not pass: {failed}")

        if snapshot and buckets <= {"pass", "skipping"}:
            if snapshot == previous:
                stable_count += 1
            else:
                previous = snapshot
                stable_count = 1
            if stable_count >= stable_observations:
                return snapshot
        else:
            previous = None
            stable_count = 0

        if attempt + 1 < max_attempts:
            sleep(interval_seconds)

    raise CheckTimeout("pull-request checks did not pass before the timeout")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckFailure, CheckTimeout, GateError) as error:
        print(f"review gate failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
