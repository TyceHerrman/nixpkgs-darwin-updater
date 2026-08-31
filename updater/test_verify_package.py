import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class VerifyPackageTests(unittest.TestCase):
    def test_builds_package_and_all_discovered_passthru_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            capture_path = directory_path / "calls.jsonl"
            fake_nix_build = directory_path / "nix-build"
            fake_nix_build.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

capture = Path(os.environ["FAKE_NIX_CAPTURE"])
with capture.open("a") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
if "-A" in sys.argv:
    print("/nix/store/example-app-2.0.0")
    print("/nix/store/example-app-dev-2.0.0")
else:
    print("/nix/store/example-test-one")
    print("/nix/store/example-test-two")
"""
            )
            fake_nix_build.chmod(0o755)
            nixpkgs = directory_path / "nixpkgs"
            nixpkgs.mkdir()
            tests_expression = directory_path / "passthru-tests.nix"
            tests_expression.write_text("{ nixpkgs, package }: []\n")
            resolved_nixpkgs = str(nixpkgs.resolve())
            resolved_tests_expression = str(tests_expression.resolve())
            environment = os.environ.copy()
            environment.update(
                {
                    "FAKE_NIX_CAPTURE": str(capture_path),
                    "PATH": f"{directory}{os.pathsep}{environment['PATH']}",
                }
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("verify_package.py")),
                    "--attr",
                    "example-app",
                    "--nixpkgs",
                    str(nixpkgs),
                    "--tests-expression",
                    str(tests_expression),
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "out_paths": [
                        "/nix/store/example-app-2.0.0",
                        "/nix/store/example-app-dev-2.0.0",
                    ],
                    "passthru_test_paths": [
                        "/nix/store/example-test-one",
                        "/nix/store/example-test-two",
                    ],
                },
            )
            calls = [json.loads(line) for line in capture_path.read_text().splitlines()]

        self.assertEqual(
            calls,
            [
                {
                    "args": ["--no-out-link", "-A", "example-app"],
                    "cwd": resolved_nixpkgs,
                },
                {
                    "args": [
                        "--no-out-link",
                        resolved_tests_expression,
                        "--arg",
                        "nixpkgs",
                        resolved_nixpkgs,
                        "--argstr",
                        "package",
                        "example-app",
                    ],
                    "cwd": resolved_nixpkgs,
                },
            ],
        )

    def test_fails_when_the_package_build_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            fake_nix_build = directory_path / "nix-build"
            fake_nix_build.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'package build failed' >&2\nexit 1\n"
            )
            fake_nix_build.chmod(0o755)
            nixpkgs = directory_path / "nixpkgs"
            nixpkgs.mkdir()
            tests_expression = directory_path / "passthru-tests.nix"
            tests_expression.write_text("{ nixpkgs, package }: []\n")
            environment = os.environ.copy()
            environment["PATH"] = f"{directory}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("verify_package.py")),
                    "--attr",
                    "example-app",
                    "--nixpkgs",
                    str(nixpkgs),
                    "--tests-expression",
                    str(tests_expression),
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("package build failed", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
