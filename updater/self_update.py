#!/usr/bin/env python3

import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from check_releases import GitHubClient, validate_repository_name


class SelfUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParentRepository:
    full_name: str
    clone_url: str
    default_branch: str


@dataclass(frozen=True)
class RebaseResult:
    original_sha: str
    upstream_sha: str
    rebased_sha: str

    @property
    def changed(self) -> bool:
        return self.original_sha != self.rebased_sha


def validate_branch_name(branch: str) -> str:
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", branch) is None
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch.endswith(("/", ".", ".lock"))
    ):
        raise SelfUpdateError(f"invalid default branch: {branch!r}")
    return branch


def discover_parent(client, repository: str) -> ParentRepository:
    validate_repository_name(repository)
    payload = client.get_json(f"/repos/{repository}")
    if not isinstance(payload, dict) or payload.get("full_name") != repository:
        raise SelfUpdateError("updater repository response is invalid")
    parent = payload.get("parent")
    if payload.get("fork") is not True or not isinstance(parent, dict):
        raise SelfUpdateError("updater repository is not a fork")

    full_name = parent.get("full_name")
    clone_url = parent.get("clone_url")
    default_branch = parent.get("default_branch")
    if not all(isinstance(value, str) for value in (full_name, clone_url, default_branch)):
        raise SelfUpdateError("fork parent response is invalid")
    validate_repository_name(full_name)
    expected_url = f"https://github.com/{full_name}.git"
    if clone_url != expected_url:
        raise SelfUpdateError("fork parent clone URL is invalid")
    validate_branch_name(default_branch)
    return ParentRepository(full_name, clone_url, default_branch)


def _run_git(args, *, cwd: Path, env=None) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise SelfUpdateError(f"git {args[0]} failed: {detail}")
    return result.stdout.strip()


def _ensure_clean_checkout(repository_dir: Path, fork_branch: str) -> str:
    current_branch = _run_git(("branch", "--show-current"), cwd=repository_dir)
    if current_branch != fork_branch:
        raise SelfUpdateError(
            f"expected checked-out branch {fork_branch!r}, got {current_branch!r}"
        )
    if _run_git(("status", "--porcelain"), cwd=repository_dir):
        raise SelfUpdateError("self-update checkout is not clean")
    original_sha = _run_git(("rev-parse", "HEAD"), cwd=repository_dir)
    remote_line = _run_git(
        ("ls-remote", "--exit-code", "origin", f"refs/heads/{fork_branch}"),
        cwd=repository_dir,
    )
    remote_sha = remote_line.split(maxsplit=1)[0] if remote_line else ""
    if remote_sha != original_sha:
        raise SelfUpdateError("checked-out branch does not match the remote fork")
    return original_sha


def rebase_onto_parent(
    *,
    repository_dir: Path,
    upstream_url: str,
    upstream_branch: str,
    fork_branch: str,
) -> RebaseResult:
    repository_dir = repository_dir.resolve()
    validate_branch_name(upstream_branch)
    validate_branch_name(fork_branch)
    original_sha = _ensure_clean_checkout(repository_dir, fork_branch)

    remotes = set(_run_git(("remote",), cwd=repository_dir).splitlines())
    if "upstream" in remotes:
        _run_git(("remote", "set-url", "upstream", upstream_url), cwd=repository_dir)
    else:
        _run_git(("remote", "add", "upstream", upstream_url), cwd=repository_dir)
    upstream_ref = f"refs/remotes/upstream/{upstream_branch}"
    _run_git(
        (
            "fetch",
            "--no-tags",
            "upstream",
            f"+refs/heads/{upstream_branch}:{upstream_ref}",
        ),
        cwd=repository_dir,
    )
    upstream_sha = _run_git(("rev-parse", upstream_ref), cwd=repository_dir)
    _run_git(("rebase", upstream_ref), cwd=repository_dir)
    rebased_sha = _run_git(("rev-parse", "HEAD"), cwd=repository_dir)
    return RebaseResult(original_sha, upstream_sha, rebased_sha)


def _run_checks(repository_dir: Path, test_commands) -> None:
    check_environment = os.environ.copy()
    for name in ("GH_SELF_UPDATE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        check_environment.pop(name, None)
    for command in test_commands:
        result = subprocess.run(
            command,
            cwd=repository_dir,
            env=check_environment,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise SelfUpdateError(f"check failed ({command[0]}): {detail}")


def push_with_lease(
    *,
    repository_dir: Path,
    fork_branch: str,
    expected_remote_sha: str,
    push_token: str | None,
) -> None:
    validate_branch_name(fork_branch)
    if re.fullmatch(r"[0-9a-f]{40}", expected_remote_sha) is None:
        raise SelfUpdateError("invalid expected fork commit")
    env = os.environ.copy()
    if push_token:
        authorization = base64.b64encode(
            f"x-access-token:{push_token}".encode()
        ).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
            }
        )
    _run_git(
        (
            "push",
            f"--force-with-lease=refs/heads/{fork_branch}:{expected_remote_sha}",
            "origin",
            f"HEAD:refs/heads/{fork_branch}",
        ),
        cwd=repository_dir,
        env=env,
    )


def synchronize(
    *,
    repository_dir: Path,
    upstream_url: str,
    upstream_branch: str,
    fork_branch: str,
    test_commands,
    push_token: str | None,
) -> RebaseResult:
    result = prepare_update(
        repository_dir=repository_dir,
        upstream_url=upstream_url,
        upstream_branch=upstream_branch,
        fork_branch=fork_branch,
        test_commands=test_commands,
    )
    if result.changed:
        push_with_lease(
            repository_dir=repository_dir,
            fork_branch=fork_branch,
            expected_remote_sha=result.original_sha,
            push_token=push_token,
        )
    return result


def prepare_update(
    *,
    repository_dir: Path,
    upstream_url: str,
    upstream_branch: str,
    fork_branch: str,
    test_commands,
) -> RebaseResult:
    result = rebase_onto_parent(
        repository_dir=repository_dir,
        upstream_url=upstream_url,
        upstream_branch=upstream_branch,
        fork_branch=fork_branch,
    )
    _run_checks(repository_dir.resolve(), test_commands)
    return result


def write_summary(
    *,
    summary_path: Path,
    parent: ParentRepository,
    result: RebaseResult,
) -> None:
    status = "updated" if result.changed else "already current"
    lines = [
        "## Self-update",
        "",
        f"- Parent: `{parent.full_name}`",
        f"- Upstream commit: `{result.upstream_sha}`",
        f"- Result: {status}",
    ]
    with summary_path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Rebase a configured updater fork onto its canonical parent"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="Resolve the canonical parent")
    discover.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
    )
    discover.add_argument("--github-output", type=Path, required=True)

    prepare = commands.add_parser("prepare", help="Rebase and test without pushing")
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--parent-name", required=True)
    prepare.add_argument("--upstream-url", required=True)
    prepare.add_argument("--upstream-branch", required=True)
    prepare.add_argument(
        "--repository-dir",
        type=Path,
        default=Path.cwd(),
    )
    prepare.add_argument("--fork-branch", required=True)
    prepare.add_argument("--github-output", type=Path, required=True)
    prepare.add_argument("--github-summary", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "discover":
        if not args.repository:
            raise SelfUpdateError("GITHUB_REPOSITORY or --repository is required")
        parent = discover_parent(
            GitHubClient(os.environ.get("GITHUB_TOKEN")),
            args.repository,
        )
        values = {
            "parent_name": parent.full_name,
            "upstream_url": parent.clone_url,
            "upstream_branch": parent.default_branch,
        }
        with args.github_output.open("a", encoding="utf-8") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
        return 0

    validate_repository_name(args.repository)
    validate_repository_name(args.parent_name)
    parent = ParentRepository(
        args.parent_name,
        args.upstream_url,
        validate_branch_name(args.upstream_branch),
    )
    with tempfile.TemporaryDirectory(prefix="nixpkgs-darwin-updater-") as directory:
        resolved_manifest = Path(directory) / "packages.json"
        test_commands = (
            (
                sys.executable,
                "updater/manifest_source.py",
                "--repository",
                args.repository,
                "--local-manifest",
                "updater/packages.json",
                "--output-manifest",
                str(resolved_manifest),
            ),
            (sys.executable, "-m", "compileall", "-q", "updater"),
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "updater",
                "-p",
                "test_*.py",
                "-v",
            ),
        )
        result = prepare_update(
            repository_dir=args.repository_dir,
            upstream_url=args.upstream_url,
            upstream_branch=args.upstream_branch,
            fork_branch=args.fork_branch,
            test_commands=test_commands,
        )
    values = {
        "changed": str(result.changed).lower(),
        "original_sha": result.original_sha,
        "upstream_sha": result.upstream_sha,
        "rebased_sha": result.rebased_sha,
        "fork_branch": args.fork_branch,
    }
    with args.github_output.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")
    if args.github_summary is not None:
        write_summary(summary_path=args.github_summary, parent=parent, result=result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SelfUpdateError, ValueError) as error:
        print(f"self-update failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
