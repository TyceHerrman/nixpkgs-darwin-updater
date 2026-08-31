import unittest
from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "self-update.yml"


class SelfUpdateWorkflowTests(unittest.TestCase):
    def test_rebased_code_and_push_token_are_isolated_in_separate_jobs(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("prepare:\n", workflow)
        self.assertIn("push:\n", workflow)
        self.assertNotIn("credentials:\n", workflow)
        self.assertEqual(workflow.count("GH_SELF_UPDATE_TOKEN: ${{ secrets.GH_SELF_UPDATE_TOKEN }}"), 1)
        prepare = workflow.split("  prepare:\n", 1)[1].split("  push:\n", 1)[0]
        self.assertNotIn("GH_SELF_UPDATE_TOKEN", prepare)
        self.assertIn("actions/upload-artifact@", prepare)
        push = workflow.split("  push:\n", 1)[1]
        self.assertIn("actions/download-artifact@", push)
        self.assertNotIn("actions/checkout@", push)
        self.assertIn(
            "if: needs.prepare.result == 'success' && needs.prepare.outputs.changed == 'true'",
            push,
        )
        self.assertIn(
            "- name: Validate credentials and push the tested commit with an exact lease\n"
            "        if: always()",
            push,
        )

    def test_canonical_repository_is_skipped_by_each_job(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertEqual(workflow.count("github.event.repository.fork == true"), 2)


if __name__ == "__main__":
    unittest.main()
