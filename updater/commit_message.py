"""Enforce nixpkgs package-update commit conventions before publication."""

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from check_releases import compare_versions, normalize_version


def git(nixpkgs: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=nixpkgs,
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def validate_inputs(args):
    if re.fullmatch(r"[0-9a-f]{40}", args.base_sha) is None:
        raise ValueError("an exact upstream commit SHA is required")
    if re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", args.attr) is None:
        raise ValueError("invalid package attribute")
    path = PurePosixPath(args.package_file)
    if (
        not path.parts
        or path.parts[0] != "pkgs"
        or ".." in path.parts
        or path.suffix != ".nix"
        or str(path) != args.package_file
        or any(ord(char) < 32 for char in args.package_file)
    ):
        raise ValueError("invalid package file")
    for version in (args.old_version, args.new_version):
        if normalize_version(version) != version:
            raise ValueError("expected a numeric package version without a tag prefix")
    if compare_versions(args.new_version, args.old_version) <= 0:
        raise ValueError("the detected version must be newer than the base version")
    release = re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/tag/(v?[0-9.]+)",
        args.release_url,
    )
    if release is None or normalize_version(release.group(1)) != args.new_version:
        raise ValueError("invalid upstream release URL or version")


def validate_update(args) -> tuple[list[str], str]:
    """Return the protected commit headers and original message without modifying Git."""
    nixpkgs = args.nixpkgs
    if git(nixpkgs, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("the update must leave a clean worktree and index")
    headers, _, message = git(nixpkgs, "cat-file", "commit", "HEAD").partition("\n\n")
    if [line for line in headers.splitlines() if line.startswith("parent ")] != [
        f"parent {args.base_sha}"
    ]:
        raise ValueError(
            "expected exactly one update commit directly on the inspected base"
        )
    changed = git(
        nixpkgs,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        args.base_sha,
        "HEAD",
        "--",
    )
    if changed != args.package_file + "\0":
        raise ValueError("only the configured package file may change")
    for revision, expected in (
        (args.base_sha, args.old_version),
        ("HEAD", args.new_version),
    ):
        source = git(nixpkgs, "show", f"{revision}:{args.package_file}")
        versions = re.findall(r'^\s*version\s*=\s*"([^"]+)"\s*;', source, re.MULTILINE)
        if versions != [expected]:
            raise ValueError(
                f"expected version {expected!r} at {revision}, got {versions!r}"
            )
    lines = message.splitlines()
    subject = f"{args.attr}: {args.old_version} -> {args.new_version}"
    if not lines or lines[0] != subject:
        raise ValueError(f"expected commit subject {subject!r}")
    if len(lines) > 1 and lines[1] != "":
        raise ValueError(
            "the commit subject and body must be separated by a blank line"
        )
    protected = [
        line
        for line in headers.splitlines()
        if line.startswith(("tree ", "parent ", "author "))
    ]
    return protected, message


def prepare_commit(args):
    validate_inputs(args)
    protected, message = validate_update(args)
    # Git understands existing trailer blocks; appending free-form paragraphs
    # after them would accidentally turn sign-offs/disclosures into ordinary text.
    prepared = git(
        args.nixpkgs,
        "-c",
        "trailer.separators=:",
        "interpret-trailers",
        "--no-divider",
        "--if-exists",
        "addIfDifferent",
        "--if-missing",
        "add",
        "--trailer",
        f"Changelog: {args.release_url}",
        input=message,
    )
    if prepared != message:
        if args.check:
            raise ValueError("commit is missing the required changelog trailer")
        git(
            args.nixpkgs,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--amend",
            "--only",
            "--no-gpg-sign",
            "--cleanup=verbatim",
            "--file=-",
            input=prepared,
        )
    after, actual_message = validate_update(args)
    if after != protected or actual_message != prepared:
        raise ValueError(
            "commit preparation changed content, ancestry, author, or message unexpectedly"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nixpkgs", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--attr", required=True)
    parser.add_argument("--package-file", required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument(
        "--check", action="store_true", help="validate without amending"
    )
    args = parser.parse_args(argv)
    prepare_commit(args)
    print(f"Validated nixpkgs update commit for {args.attr} {args.new_version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"cannot publish nixpkgs update commit: {error}", file=sys.stderr)
        raise SystemExit(1) from error
