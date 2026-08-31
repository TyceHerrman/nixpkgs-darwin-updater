#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    out_paths: tuple[str, ...]
    passthru_test_paths: tuple[str, ...]


def _run_nix_build(command: list[str], *, nixpkgs: Path, runner=subprocess.run):
    result = runner(
        command,
        cwd=nixpkgs,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise VerificationError(detail or f"nix-build exited {result.returncode}")
    return tuple(line for line in result.stdout.splitlines() if line)


def verify_package(
    attr: str,
    *,
    nixpkgs: Path,
    tests_expression: Path,
    runner=subprocess.run,
) -> VerificationResult:
    if re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", attr) is None:
        raise VerificationError(f"invalid package attribute: {attr!r}")
    if not nixpkgs.is_dir():
        raise VerificationError(f"nixpkgs directory does not exist: {nixpkgs}")
    if not tests_expression.is_file():
        raise VerificationError(
            f"passthru test expression does not exist: {tests_expression}"
        )

    package_paths = _run_nix_build(
        ["nix-build", "--no-out-link", "-A", attr],
        nixpkgs=nixpkgs,
        runner=runner,
    )
    if not package_paths:
        raise VerificationError(f"package build returned no output paths for {attr}")
    test_paths = _run_nix_build(
        [
            "nix-build",
            "--no-out-link",
            str(tests_expression),
            "--arg",
            "nixpkgs",
            str(nixpkgs),
            "--argstr",
            "package",
            attr,
        ],
        nixpkgs=nixpkgs,
        runner=runner,
    )
    return VerificationResult(package_paths, test_paths)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a nixpkgs package and its passthru tests"
    )
    parser.add_argument("--attr", required=True)
    parser.add_argument("--nixpkgs", type=Path, required=True)
    parser.add_argument("--tests-expression", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = verify_package(
        args.attr,
        nixpkgs=args.nixpkgs.resolve(),
        tests_expression=args.tests_expression.resolve(),
    )
    print(
        json.dumps(
            {
                "out_paths": list(result.out_paths),
                "passthru_test_paths": list(result.passthru_test_paths),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as error:
        print(f"package verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
