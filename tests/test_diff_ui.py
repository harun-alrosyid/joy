import unittest
from types import SimpleNamespace
from unittest.mock import patch

from joy.cli import DiffViewer, _show_changes, _tool_status
from prompt_toolkit.mouse_events import MouseEventType
from joy.tools import FileChange


class FakeConsole:
    width = 80

    def __init__(self):
        self.lines = []

    def print(self, value, **kwargs):
        self.lines.append(value)


class DiffSummaryTest(unittest.TestCase):
    def test_failed_tool_status_shows_read_error_reason(self):
        text = _tool_status("read", "failed", "Tool error: Cannot read file: Permission denied")
        self.assertIn("Reading failed", text)
        self.assertIn("Permission denied", text)

    def test_header_counts_and_compact_preview(self):
        viewer = DiffViewer([FileChange("src/main.py", "edited", 2, 1,
                                        "--- a/src/main.py\n+++ b/src/main.py\n"
                                        "@@ -4,2 +4,3 @@\n-old\n+new\n+extra")])
        rendered = viewer.lines(80, 20)
        text = "\n".join(value for _, value in rendered)
        self.assertIn("Edited src/main.py", text)
        self.assertIn("+2  -1", text)
        self.assertIn("   4      -old", text)
        self.assertIn("        4 +new", text)
        self.assertNotIn("+ Show details", text)

    def test_one_control_expands_all_changes(self):
        viewer = DiffViewer([
            FileChange("a.py", "edited", 4, 2,
                       "@@ -1 +1 @@\n-old\n+new\n@@ -4 +4 @@\n-old2\n+new2"),
            FileChange("b.py", "edited", 2, 0, "@@ -0,0 +1,2 @@\n+x\n+y"),
        ])
        collapsed = viewer.lines(80, 20)
        self.assertEqual(sum("Show details" in text for _, text in collapsed), 1)
        viewer.toggle()
        expanded = viewer.lines(80, 20)
        self.assertTrue(viewer.expanded)
        self.assertEqual(sum("Hide details" in text for _, text in expanded), 1)
        self.assertIn("+y", "\n".join(text for _, text in expanded))
        viewer.collapse()
        self.assertFalse(viewer.expanded)

    def test_expanded_lines_are_complete_and_colored(self):
        viewer = DiffViewer([FileChange("a.py", "edited", 1, 1,
                                        "@@ -1 +1 @@\n-old\n+new")])
        viewer.expand()
        rendered = viewer.lines(80, 20)
        self.assertIn(("cyan", "  @@ -1 +1 @@"), rendered)
        self.assertIn(("class:diff-remove", "   1      -old"), rendered)
        self.assertIn(("class:diff-add", "        1 +new"), rendered)

    def test_removals_red_additions_green_and_single_detail_control(self):
        viewer = DiffViewer([
            FileChange("a.py", "edited", 2, 1,
                       "@@ -1 +1 @@\n-old\n+new\n@@ -8 +8 @@\n-old2\n+new2"),
            FileChange("b.py", "created", 1, 0, "@@ -0,0 +1 @@\n+line"),
        ])
        rows = viewer._rows(80, 20)
        controls = [row for row in rows if "Show details" in row[1]]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0][2], -1)
        self.assertIn(("class:diff-remove", "   1      -old", None), rows)
        self.assertIn(("class:diff-add", "        1 +new", None), rows)
        self.assertIn(("summary", "2 files changed  +3  -1", None), rows)

    def test_summary_addition_and_deletion_counts_use_green_and_red(self):
        changes = [
            FileChange("a.py", "edited", 3, 2, "@@ -1 +1 @@\n-old\n+new"),
            FileChange("b.py", "created", 2, 0, "@@ -0,0 +1,2 @@\n+x\n+y"),
        ]
        fragments = _viewer_text({"viewer": DiffViewer(changes)}, 80, 20)
        self.assertTrue(any(style == "class:diff-count-add" and "+5" in text
                            for style, text, *_ in fragments))
        self.assertTrue(any(style == "class:diff-count-remove" and "-2" in text
                            for style, text, *_ in fragments))

    def test_clicking_global_detail_control_expands_all_diffs(self):
        change = FileChange("a.py", "edited", 2, 0,
                            "@@ -0,0 +1,8 @@\n" +
                            "\n".join(f"+line{i}" for i in range(8)))
        viewer = DiffViewer([change])
        fragments = _viewer_text({"viewer": viewer}, 80, 20)
        click = next(fragment[2] for fragment in fragments
                     if len(fragment) == 3 and fragment[1].strip() == "+ Show details")
        with patch("joy.cli.get_app") as get_app:
            click(SimpleNamespace(event_type=MouseEventType.MOUSE_UP))
        self.assertTrue(viewer.expanded)
        get_app.return_value.invalidate.assert_called_once()

    def test_multiple_hunks_and_scroll(self):
        diff = "@@ -1 +1 @@\n-old\n+new\n" + "\n".join(
            f"@@ -{n} +{n} @@\n-old{n}\n+new{n}" for n in range(10, 30)
        )
        viewer = DiffViewer([FileChange("a.py", "edited", 21, 21, diff)])
        viewer.expand()
        first = viewer.lines(80, 5)
        viewer.scroll = 10
        second = viewer.lines(80, 5)
        self.assertNotEqual(first, second)
        self.assertIn("@@ -12 +12 @@", "\n".join(value for _, value in second))

    def test_long_path_narrow_width_and_no_duplicate_rendering(self):
        change = FileChange("src/very/long/path/module.py", "created", 1, 0,
                            "@@ -0,0 +1 @@\n+value")
        viewer = DiffViewer([change])
        text = "\n".join(value for _, value in viewer.lines(30, 20))
        self.assertIn("...", text)
        console = FakeConsole()
        _show_changes(console, [change, change])
        self.assertEqual("\n".join(map(str, console.lines)).count(change.path), 1)

    def test_collapsed_multiple_changes_and_totals(self):
        console = FakeConsole()
        changes = [
            FileChange("src/main.py", "edited", 3, 2, "full diff"),
            FileChange("src/config.py", "overwritten", 8, 1, "full diff"),
            FileChange("src/main.py", "edited", 3, 2, "duplicate"),
        ]
        _show_changes(console, changes)
        output = "\n".join(str(line) for line in console.lines)
        self.assertEqual(output.count("src/main.py"), 1)
        self.assertIn("src/config.py", output)
        self.assertIn("2 files changed", output)
        self.assertIn("+11", output)
        self.assertIn("-3", output)
        self.assertNotIn("full diff", output)

    def test_narrow_width_shortens_path(self):
        console = FakeConsole()
        console.width = 30
        _show_changes(console, [FileChange("very/long/path/example.py", "edited", 1, 0, "diff")])
        self.assertIn("...", str(console.lines[1]))


if __name__ == "__main__":
    unittest.main()
