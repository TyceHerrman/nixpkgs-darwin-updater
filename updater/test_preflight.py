import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import preflight


class MappingClient:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def get_json(self, path, *, query=None):
        self.paths.append(path)
        return self.responses[path]


def fork_payload(*, push=True):
    return {
        "full_name": "person/nixpkgs",
        "fork": True,
        "parent": {"full_name": "NixOS/nixpkgs"},
        "permissions": {"admin": False, "maintain": False, "push": push},
    }


class PreflightTests(unittest.TestCase):
    def test_controller_configuration_takes_precedence_without_a_direct_token(self):
        integration = preflight.resolve_review_integration(
            review_enabled=True,
            updater_repository="person/nixpkgs-darwin-updater",
            dispatch_repository="person/nixpkgs-contribution-workflows",
            dispatch_token="controller-token",
            direct_repository=None,
            direct_token=None,
        )

        self.assertEqual(integration.mode, "controller")
        self.assertEqual(
            integration.repository, "person/nixpkgs-contribution-workflows"
        )
        self.assertEqual(integration.token, "controller-token")

    def test_direct_configuration_uses_the_updater_owner_default(self):
        integration = preflight.resolve_review_integration(
            review_enabled=True,
            updater_repository="person/nixpkgs-darwin-updater",
            dispatch_repository=None,
            dispatch_token=None,
            direct_repository=None,
            direct_token="direct-token",
        )

        self.assertEqual(integration.mode, "direct")
        self.assertEqual(integration.repository, "person/nixpkgs-review-gha")
        self.assertEqual(integration.token, "direct-token")

    def test_controller_configuration_requires_its_own_token(self):
        with self.assertRaisesRegex(
            preflight.PreflightError,
            "NIXPKGS_REVIEW_DISPATCH_TOKEN is not configured",
        ):
            preflight.resolve_review_integration(
                review_enabled=True,
                updater_repository="person/nixpkgs-darwin-updater",
                dispatch_repository="person/nixpkgs-contribution-workflows",
                dispatch_token=None,
                direct_repository="person/nixpkgs-review-gha",
                direct_token="direct-token",
            )

    def test_validates_token_identity_and_nixpkgs_fork_push_access(self):
        client = MappingClient(
            {
                "/user": {"login": "person"},
                "/repos/person/nixpkgs": fork_payload(),
            }
        )

        result = preflight.run_preflight(
            client,
            fork_repository="person/nixpkgs",
            review_enabled=False,
            review_client=None,
            review_repository=None,
        )

        self.assertEqual(result.login, "person")
        self.assertEqual(result.fork_repository, "person/nixpkgs")
        self.assertIsNone(result.review_repository)

    def test_rejects_a_repository_without_push_access(self):
        client = MappingClient(
            {
                "/user": {"login": "person"},
                "/repos/person/nixpkgs": fork_payload(push=False),
            }
        )

        with self.assertRaisesRegex(preflight.PreflightError, "push access"):
            preflight.run_preflight(
                client,
                fork_repository="person/nixpkgs",
                review_enabled=False,
                review_client=None,
                review_repository=None,
            )

    def test_validates_optional_active_review_workflow(self):
        pr_client = MappingClient(
            {
                "/user": {"login": "person"},
                "/repos/person/nixpkgs": fork_payload(),
            }
        )
        review_client = MappingClient(
            {
                "/repos/person/nixpkgs-review-gha/actions/workflows/review.yml": {
                    "id": 123,
                    "name": "Review",
                    "state": "active",
                    "html_url": "https://github.com/person/nixpkgs-review-gha/actions/workflows/review.yml",
                }
            }
        )

        result = preflight.run_preflight(
            pr_client,
            fork_repository="person/nixpkgs",
            review_enabled=True,
            review_client=review_client,
            review_repository="person/nixpkgs-review-gha",
        )

        self.assertEqual(result.review_repository, "person/nixpkgs-review-gha")

    def test_rejects_an_inactive_controller_workflow(self):
        pr_client = MappingClient(
            {
                "/user": {"login": "person"},
                "/repos/person/nixpkgs": fork_payload(),
            }
        )
        controller_client = MappingClient(
            {
                "/repos/person/nixpkgs-contribution-workflows/actions/workflows/review.yml": {
                    "id": 123,
                    "name": "Review",
                    "state": "disabled_manually",
                }
            }
        )

        with self.assertRaisesRegex(preflight.PreflightError, "unavailable or inactive"):
            preflight.run_preflight(
                pr_client,
                fork_repository="person/nixpkgs",
                review_enabled=True,
                review_client=controller_client,
                review_repository="person/nixpkgs-contribution-workflows",
                review_mode="controller",
            )


if __name__ == "__main__":
    unittest.main()
