"""Fill the exact nixpkgs PR template from the inspected upstream commit."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from check_releases import normalize_version

TEMPLATE_PATH = ".github/PULL_REQUEST_TEMPLATE.md"
DARWIN_ITEM = "  - [ ] aarch64-darwin"
PACKAGE_TESTS_ITEM = "  - [ ] [Package tests] at `passthru.tests`."


def read_template(nixpkgs: Path, base_sha: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
        raise ValueError("the template must come from an exact upstream commit SHA")
    result = subprocess.run(
        ["git", "show", f"{base_sha}:{TEMPLATE_PATH}"],
        cwd=nixpkgs,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8")


def read_verification(path: Path) -> dict[str, list[str]]:
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or set(result) != {
        "out_paths",
        "passthru_test_paths",
    }:
        raise ValueError("invalid package verification result")
    for paths in result.values():
        if not isinstance(paths, list) or not all(
            isinstance(path, str) and path.startswith("/nix/store/") for path in paths
        ):
            raise ValueError("invalid verification output paths")
    if not result["out_paths"]:
        raise ValueError("cannot describe a successful build without package outputs")
    return result


def render_body(
    template: str,
    *,
    attr: str,
    old_version: str,
    new_version: str,
    release_url: str,
    base_sha: str,
    tests_built: bool,
) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", attr) is None:
        raise ValueError("invalid package attribute")
    old_version = normalize_version(old_version)
    new_version = normalize_version(new_version)
    release = re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/tag/(v?[0-9.]+)",
        release_url,
    )
    if release is None or normalize_version(release.group(1)) != new_version:
        raise ValueError("invalid upstream release URL or version")

    lines = template.splitlines(keepends=True)
    contents = template.splitlines()
    for required in ("## Things done", DARWIN_ITEM, PACKAGE_TESTS_ITEM):
        if contents.count(required) != 1:
            raise ValueError(
                f"unrecognized nixpkgs PR template: expected one {required!r}"
            )
    checked = {DARWIN_ITEM}
    if tests_built:
        checked.add(PACKAGE_TESTS_ITEM)
    filled_template = "".join(
        line.replace("[ ]", "[x]", 1) if line.rstrip("\r\n") in checked else line
        for line in lines
    )
    tests_note = (
        f"Built all discovered derivation-valued `{attr}.passthru.tests`."
        if tests_built
        else f"No derivation-valued `{attr}.passthru.tests` were present."
    )
    description = "\n".join(
        (
            f"Update `{attr}` from `{old_version}` to `{new_version}`.",
            "",
            f"Upstream release: {release_url}",
            "",
            (
                "Ran the package's declared nixpkgs update script and built the updated "
                "package on `aarch64-darwin`, including its normal check and install-check hooks."
            ),
            tests_note,
            "",
            f"Nixpkgs base: `{base_sha}`.",
            "",
        )
    )
    return description + filled_template


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nixpkgs", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--attr", required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    template = read_template(args.nixpkgs, args.base_sha)
    verification = read_verification(args.verification)
    body = render_body(
        template,
        attr=args.attr,
        old_version=args.old_version,
        new_version=args.new_version,
        release_url=args.release_url,
        base_sha=args.base_sha,
        tests_built=bool(verification["passthru_test_paths"]),
    )
    args.output.write_bytes(body.encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"cannot prepare nixpkgs pull request: {error}", file=sys.stderr)
        raise SystemExit(1) from error
