import json
from pathlib import Path
import unittest

import review_boundary


class ReviewBoundaryTest(unittest.TestCase):
    def test_action_events_and_incomplete_turn_are_rejected(self):
        finished = json.dumps({"type":"turn.completed"})
        for kind in ("command_execution", "mcp_tool_call", "file_change", "future_tool"):
            item = json.dumps({"type":"item.started", "item":{"type":kind}})
            with self.assertRaises(ValueError):
                review_boundary.verify_events(item + "\n" + finished)
        with self.assertRaises(ValueError):
            review_boundary.verify_events('{"type":"thread.started"}')

    def test_publisher_binds_to_the_current_diff(self):
        patch = "@@ -8,2 +8,2 @@\n-old\n+new\n context"
        item = {"path":"src/a.py","line":8,"body":"Sample finding."}
        candidates = [item, dict(item, path="src/b.py"), dict(item, line=99),
                      dict(item, action="other"), dict(item, line=True)]
        self.assertEqual(review_boundary.public_findings(
            [{"filename":"src/a.py","patch":patch}], candidates), [item])
        public = review_boundary.inert_comment("Example @name\n~~~\n/word")
        self.assertNotIn("@",public)
        self.assertNotIn("\n/word",public)
        self.assertTrue(public.startswith("~~~~text\nFinding: "))

    def test_no_extra_output_fields(self):
        result = {"summary":"Example.","public_comments":[]}
        schema = {"required":list(result)}
        self.assertEqual(review_boundary.validate(result,schema),result)
        with self.assertRaises(ValueError):
            review_boundary.validate(dict(result, other="text"),schema)

    def test_sandbox_never_grants_the_whole_disk(self):
        profile = review_boundary.sandbox_profile(Path("/private/tmp/leapp-review-x"))
        self.assertNotIn('(subpath "/")', profile)
        self.assertIn("(deny process-fork)", profile)
        self.assertIn("(deny file-write*)", profile)


if __name__ == "__main__":
    unittest.main()
