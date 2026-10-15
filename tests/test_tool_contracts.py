import json
import tempfile
import unittest
from pathlib import Path
from threading import Event

from joy.tools import MAX_FILE_SIZE, create_tools


class ToolContractTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.tools = create_tools(self.root, Event())

    def tearDown(self):
        self.tempdir.cleanup()

    def call(self, name, **arguments):
        return self.tools.execute({
            "function": {"name": name, "arguments": json.dumps(arguments)},
        })[0]

    def test_read_ranges_empty_and_invalid(self):
        (self.root / "main.py").write_text("a\nb\nc\n")
        self.assertIn("  2 | b", self.call("read", path="main.py", start_line=2, end_line=2))
        (self.root / "empty").write_text("")
        self.assertIn("File: empty", self.call("read", path="empty"))
        with self.assertRaises(ValueError):
            self.call("read", path="main.py", start_line=4, end_line=5)
        with self.assertRaises(ValueError):
            self.call("read", path="missing")
        with self.assertRaises(ValueError):
            self.call("read", path="main.py", limit=-1)
        with self.assertRaises(ValueError):
            self.call("read", path="main.py", limit=2001)

    def test_read_oversized_file_fails_with_specific_reason(self):
        (self.root / "large.txt").write_bytes(b"x" * (MAX_FILE_SIZE + 1))
        with self.assertRaisesRegex(ValueError, "2 MiB limit"):
            self.call("read", path="large.txt")

    def test_write_metadata_and_limit(self):
        result = self.call("write", path="nested/file.py", content="one\ntwo\n")
        self.assertIn("+one", result)
        self.assertIn("+two", result)
        self.assertEqual((self.root / "nested/file.py").read_text(), "one\ntwo\n")
        with self.assertRaises(ValueError):
            self.call("write", path="too-big", content="x" * (MAX_FILE_SIZE + 1))

    def test_write_approval_precedes_commit_and_record(self):
        seen = []
        tools = create_tools(self.root, Event(), seen.append, approve_change=lambda change: False)
        with self.assertRaises(InterruptedError):
            tools.execute({"function": {"name": "write", "arguments": json.dumps({
                "path": "rejected.txt", "content": "proposed",
            })}})
        self.assertFalse((self.root / "rejected.txt").exists())
        self.assertEqual(seen, [])

        tools = create_tools(self.root, Event(), seen.append, approve_change=lambda change: True)
        tools.execute({"function": {"name": "write", "arguments": json.dumps({
            "path": "accepted.txt", "content": "approved",
        })}})
        self.assertEqual((self.root / "accepted.txt").read_text(), "approved")
        self.assertEqual([change.path for change in seen], ["accepted.txt"])

    def test_edit_rejection_preserves_original(self):
        path = self.root / "existing.txt"
        path.write_text("before\n")
        seen = []
        tools = create_tools(self.root, Event(), seen.append, approve_change=lambda change: False)
        with self.assertRaises(InterruptedError):
            tools.execute({"function": {"name": "edit", "arguments": json.dumps({
                "path": "existing.txt", "old_text": "before", "new_text": "after",
            })}})
        self.assertEqual(path.read_text(), "before\n")
        self.assertEqual(seen, [])

    def test_edit_matching_and_bom_newline_preservation(self):
        path = self.root / "file.py"
        path.write_bytes(b"\xef\xbb\xbfold\r\n")
        result = self.call("edit", path="file.py", old_text="old", new_text="new")
        self.assertIn("+\ufeffnew", result)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfnew\r\n")
        with self.assertRaises(ValueError):
            self.call("edit", path="file.py", old_text="", new_text="x")

    def test_bash_separates_streams_and_reports_failure(self):
        result = self.call("bash", command="printf out; printf err >&2; exit 3")
        self.assertIn("Failed", result)
        self.assertIn("exit code 3", result)
        self.assertIn("stdout:\n", result)
        self.assertIn("stderr:\n", result)


if __name__ == "__main__":
    unittest.main()
