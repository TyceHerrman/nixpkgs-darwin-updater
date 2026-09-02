import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import review_gate


class QueueRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(
            command,
            response.get("returncode", 0),
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
        )


def completed_json(payload, *, returncode=0):
    return {
        "returncode": returncode,
        "stdout": json.dumps(payload),
        "stderr": "",
    }


def check(name, bucket, state):
    return {
        "bucket": bucket,
        "link": f"https://example.test/{name}",
        "name": name,
        "state": state,
        "workflow": "PR",
    }


class CheckGateTests(unittest.TestCase):
    def test_waits_for_late_checks_to_stabilize(self):
        snapshots = iter(
            (
                [check("lint", "pending", "IN_PROGRESS")],
                [check("lint", "pass", "SUCCESS")],
                [
                    check("lint", "pass", "SUCCESS"),
                    check("eval", "skipping", "SKIPPED"),
                ],
                [
                    check("eval", "skipping", "SKIPPED"),
                    check("lint", "pass", "SUCCESS"),
                ],
            )
        )
        sleeps = []

        result = review_gate.wait_for_passing_checks(
            lambda: next(snapshots),
            interval_seconds=60,
            max_attempts=4,
            stable_observations=2,
            sleep=sleeps.append,
        )

        self.assertEqual(
            result,
            (
                ("eval", "skipping", "SKIPPED", "PR"),
                ("lint", "pass", "SUCCESS", "PR"),
            ),
        )
        self.assertEqual(sleeps, [60, 60, 60])

    def test_rejects_failed_or_cancelled_checks(self):
        for bucket in ("fail", "cancel"):
            with (
                self.subTest(bucket=bucket),
                self.assertRaises(review_gate.CheckFailure),
            ):
                review_gate.wait_for_passing_checks(
                    lambda bucket=bucket: [check("build", bucket, "FAILURE")],
                    interval_seconds=0,
                    max_attempts=1,
                    stable_observations=1,
                    sleep=lambda _seconds: None,
                )

    def test_empty_check_set_times_out_instead_of_dispatching_early(self):
        with self.assertRaises(review_gate.CheckTimeout):
            review_gate.wait_for_passing_checks(
                list,
                interval_seconds=0,
                max_attempts=2,
                stable_observations=1,
                sleep=lambda _seconds: None,
            )


class GitHubCliTests(unittest.TestCase):
    def test_finds_the_single_open_pull_request_for_the_exact_head(self):
        runner = QueueRunner(
            [
                completed_json(
                    [
                        {
                            "number": 557203,
                            "html_url": "https://github.com/NixOS/nixpkgs/pull/557203",
                        }
                    ]
                )
            ]
        )
        cli = review_gate.GitHubCli("read-token", runner=runner)

        pull_request = cli.find_open_pull_request("person:auto-update/whatcable-1.4.0")

        self.assertEqual(pull_request.number, 557203)
        self.assertEqual(
            pull_request.url, "https://github.com/NixOS/nixpkgs/pull/557203"
        )
        command, options = runner.calls[0]
        self.assertEqual(
            command,
            [
                "gh",
                "api",
                "--method",
                "GET",
                "repos/NixOS/nixpkgs/pulls",
                "-f",
                "state=open",
                "-f",
                "head=person:auto-update/whatcable-1.4.0",
                "-f",
                "per_page=100",
            ],
        )
        self.assertEqual(options["env"]["GH_TOKEN"], "read-token")

    def test_reads_check_buckets_while_checks_are_pending(self):
        pending = [check("eval", "pending", "IN_PROGRESS")]
        runner = QueueRunner([completed_json(pending, returncode=8)])
        cli = review_gate.GitHubCli("read-token", runner=runner)

        result = cli.pull_request_checks(557203)

        self.assertEqual(result, pending)
        command, options = runner.calls[0]
        self.assertEqual(
            command,
            [
                "gh",
                "pr",
                "checks",
                "557203",
                "--repo",
                "NixOS/nixpkgs",
                "--json",
                "bucket,link,name,state,workflow",
            ],
        )
        self.assertEqual(options["env"]["GH_TOKEN"], "read-token")

    def test_lists_review_runs_for_deduplication(self):
        runs = [
            {
                "displayTitle": "review #557203",
                "status": "completed",
                "conclusion": "success",
                "url": "https://example.test/run/1",
            }
        ]
        runner = QueueRunner([completed_json(runs)])
        cli = review_gate.GitHubCli(
            "dispatch-token",
            review_repository="person/nixpkgs-review-gha",
            runner=runner,
        )

        result = cli.list_review_runs()

        self.assertEqual(result, runs)
        command, options = runner.calls[0]
        self.assertEqual(
            command,
            [
                "gh",
                "run",
                "list",
                "--repo",
                "person/nixpkgs-review-gha",
                "--workflow",
                "review.yml",
                "--event",
                "workflow_dispatch",
                "--limit",
                "1000",
                "--json",
                "displayTitle,status,conclusion,url",
            ],
        )
        self.assertEqual(options["env"]["GH_TOKEN"], "dispatch-token")

    def test_dispatches_only_the_two_darwin_runners(self):
        runner = QueueRunner([{"returncode": 0, "stdout": "", "stderr": ""}])
        cli = review_gate.GitHubCli(
            "dispatch-token",
            review_repository="person/nixpkgs-review-gha",
            runner=runner,
        )

        cli.dispatch_review(557203)

        command, options = runner.calls[0]
        self.assertEqual(
            command,
            [
                "gh",
                "workflow",
                "run",
                "review.yml",
                "--repo",
                "person/nixpkgs-review-gha",
                "--ref",
                "main",
                "-f",
                "pr=557203",
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
            ],
        )
        self.assertEqual(options["env"]["GH_TOKEN"], "dispatch-token")

    def test_dispatches_the_controller_with_the_exact_darwin_payload(self):
        runner = QueueRunner(
            [
                completed_json(
                    {
                        "workflow_run_id": 91,
                        "run_url": (
                            "https://api.github.com/repos/person/"
                            "nixpkgs-contribution-workflows/actions/runs/91"
                        ),
                        "html_url": (
                            "https://github.com/person/"
                            "nixpkgs-contribution-workflows/actions/runs/91"
                        ),
                    }
                )
            ]
        )
        cli = review_gate.GitHubCli(
            "controller-token",
            review_repository="person/nixpkgs-contribution-workflows",
            runner=runner,
        )

        url = cli.dispatch_controller_review(557203, "whatcable")

        self.assertEqual(
            url,
            "https://github.com/person/nixpkgs-contribution-workflows/actions/runs/91",
        )
        command, options = runner.calls[0]
        self.assertEqual(
            command,
            [
                "gh",
                "api",
                "--method",
                "POST",
                "repos/person/nixpkgs-contribution-workflows/actions/workflows/"
                "review.yml/dispatches",
                "-H",
                "X-GitHub-Api-Version: 2026-03-10",
                "--input",
                "-",
            ],
        )
        self.assertEqual(
            json.loads(options["input"]),
            {
                "ref": "main",
                "inputs": {
                    "pr": "557203",
                    "attribute": "whatcable",
                    "platform-scope": "darwin",
                    "force": False,
                },
            },
        )
        self.assertEqual(options["env"]["GH_TOKEN"], "controller-token")

    def test_rejects_an_ambiguous_controller_response_without_retrying(self):
        runner = QueueRunner([{"returncode": 0, "stdout": "", "stderr": ""}])
        cli = review_gate.GitHubCli(
            "controller-token",
            review_repository="person/nixpkgs-contribution-workflows",
            runner=runner,
        )

        with self.assertRaisesRegex(
            review_gate.GateError,
            "controller dispatch response is uncertain; do not retry automatically",
        ):
            cli.dispatch_controller_review(557203, "whatcable")

        self.assertEqual(len(runner.calls), 1)


class DispatchTests(unittest.TestCase):
    def test_skips_a_pull_request_that_already_has_a_review_run(self):
        class ReviewClient:
            def list_review_runs(self):
                return [
                    {
                        "displayTitle": "review #557203",
                        "status": "completed",
                        "conclusion": "success",
                        "url": "https://example.test/run/1",
                    }
                ]

            def dispatch_review(self, _pull_request):
                raise AssertionError("duplicate review was dispatched")

        result = review_gate.dispatch_review_if_missing(ReviewClient(), 557203)

        self.assertFalse(result.dispatched)
        self.assertEqual(result.existing_url, "https://example.test/run/1")

    def test_dispatches_when_no_prior_review_run_exists(self):
        class ReviewClient:
            def __init__(self):
                self.dispatched = []

            def list_review_runs(self):
                return []

            def dispatch_review(self, pull_request):
                self.dispatched.append(pull_request)

        client = ReviewClient()

        result = review_gate.dispatch_review_if_missing(client, 557203)

        self.assertTrue(result.dispatched)
        self.assertIsNone(result.existing_url)
        self.assertEqual(client.dispatched, [557203])


class ReviewGateTests(unittest.TestCase):
    def test_controller_gate_waits_for_ci_without_runner_deduplication(self):
        class ReadClient:
            def find_open_pull_request(self, _head):
                return review_gate.PullRequest(
                    557203, "https://github.com/NixOS/nixpkgs/pull/557203"
                )

            def pull_request_checks(self, _pull_request):
                return [check("lint", "pass", "SUCCESS")]

        class ControllerClient:
            def __init__(self):
                self.requests = []

            def dispatch_controller_review(self, pull_request, attribute):
                self.requests.append((pull_request, attribute))
                return "https://github.com/person/controller/actions/runs/91"

        controller_client = ControllerClient()

        result = review_gate.run_controller_review_gate(
            ReadClient(),
            controller_client,
            head="person:auto-update/whatcable-1.4.0",
            attribute="whatcable",
            interval_seconds=1,
            max_attempts=1,
            stable_observations=1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(controller_client.requests, [(557203, "whatcable")])
        self.assertTrue(result.dispatch.dispatched)
        self.assertEqual(
            result.dispatch.controller_url,
            "https://github.com/person/controller/actions/runs/91",
        )

    def test_controller_mode_requires_an_attribute(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "read-token",
                "NIXPKGS_REVIEW_DISPATCH_TOKEN": "controller-token",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                review_gate.GateError,
                "--attribute is required for controller dispatch",
            ):
                review_gate.main(
                    [
                        "--head",
                        "person:auto-update/whatcable-1.4.0",
                        "--dispatch-repository",
                        "person/nixpkgs-contribution-workflows",
                    ]
                )

    def test_controller_mode_requires_its_own_token(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "read-token",
                "NIXPKGS_REVIEW_DISPATCH_TOKEN": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                review_gate.GateError,
                "NIXPKGS_REVIEW_DISPATCH_TOKEN is not configured",
            ):
                review_gate.main(
                    [
                        "--head",
                        "person:auto-update/whatcable-1.4.0",
                        "--attribute",
                        "whatcable",
                        "--dispatch-repository",
                        "person/nixpkgs-contribution-workflows",
                    ]
                )

    def test_gates_the_exact_head_then_dispatches_its_pull_request(self):
        class ReadClient:
            def __init__(self):
                self.heads = []

            def find_open_pull_request(self, head):
                self.heads.append(head)
                return review_gate.PullRequest(
                    557203, "https://github.com/NixOS/nixpkgs/pull/557203"
                )

            def pull_request_checks(self, pull_request):
                self.pull_request = pull_request
                return [
                    check("lint", "pass", "SUCCESS"),
                    check("unused", "skipping", "SKIPPED"),
                ]

        class DispatchClient:
            def __init__(self):
                self.dispatched = []

            def list_review_runs(self):
                return []

            def dispatch_review(self, pull_request):
                self.dispatched.append(pull_request)

        read_client = ReadClient()
        dispatch_client = DispatchClient()

        result = review_gate.run_review_gate(
            read_client,
            dispatch_client,
            head="person:auto-update/whatcable-1.4.0",
            interval_seconds=0,
            max_attempts=1,
            stable_observations=1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            read_client.heads,
            ["person:auto-update/whatcable-1.4.0"],
        )
        self.assertEqual(read_client.pull_request, 557203)
        self.assertEqual(result.pull_request.number, 557203)
        self.assertEqual(len(result.checks), 2)
        self.assertTrue(result.dispatch.dispatched)
        self.assertEqual(dispatch_client.dispatched, [557203])

    def test_command_line_entrypoint_runs_the_complete_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            capture_path = directory_path / "dispatch.json"
            fake_gh = directory_path / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["api"]:
    print(json.dumps([{
        "number": 557203,
        "html_url": "https://github.com/NixOS/nixpkgs/pull/557203",
    }]))
elif args[:2] == ["pr", "checks"]:
    print(json.dumps([
        {
            "bucket": "pass",
            "link": "https://example.test/lint",
            "name": "lint",
            "state": "SUCCESS",
            "workflow": "PR",
        },
        {
            "bucket": "skipping",
            "link": "https://example.test/unused",
            "name": "unused",
            "state": "SKIPPED",
            "workflow": "PR",
        },
    ]))
elif args[:2] == ["run", "list"]:
    print("[]")
elif args[:2] == ["workflow", "run"]:
    Path(os.environ["FAKE_GH_CAPTURE"]).write_text(json.dumps(args))
else:
    raise SystemExit(f"unexpected gh command: {args}")
"""
            )
            fake_gh.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "FAKE_GH_CAPTURE": str(capture_path),
                    "GITHUB_TOKEN": "read-token",
                    "NIXPKGS_REVIEW_GHA_TOKEN": "dispatch-token",
                    "NIXPKGS_REVIEW_GHA_REPOSITORY": "person/nixpkgs-review-gha",
                    "PATH": f"{directory}{os.pathsep}{environment['PATH']}",
                }
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("review_gate.py")),
                    "--head",
                    "person:auto-update/whatcable-1.4.0",
                    "--timeout-seconds",
                    "1",
                    "--interval-seconds",
                    "1",
                    "--stable-observations",
                    "1",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2 checks passed or skipped", result.stdout)
            dispatched = json.loads(capture_path.read_text())

        self.assertIn("x86_64-linux=false", dispatched)
        self.assertIn("aarch64-linux=false", dispatched)
        self.assertIn("x86_64-darwin=yes_sandbox_relaxed", dispatched)
        self.assertIn("aarch64-darwin=yes_sandbox_relaxed", dispatched)


if __name__ == "__main__":
    unittest.main()
