import unittest
import os
import re
import tempfile
import shutil
import io
import contextlib
from pathlib import Path
from unittest.mock import patch

from xite import (
    main,
    parse_todo_list,
    # format_todo_list, # removed in refactor
    filter_items,
    sort_items_by_status,
    TodoItem,
    STATUS_ORDER,
    REVERSE_STATUS_MAP,
    UNSPECIFIED_SOURCE,
    _collect_all_items,
    _process_and_format_items,
    _get_ordered_source_keys,
    DEFAULT_ROADMAP_FILENAMES,
    _format_recursive,  # import the internal helper
)
from copy import deepcopy
import tomllib  # for testing toml errors
import argparse  # for creating mock args
import sys  # for checking python version
from typing import List  # Add this import

# test data (from examples/bare.xit)
BARE_XIT_CONTENT = """
[ ] new item
[@] active
[x] complete
[~] obsolete
[?] in question
[!] blocked
[>] deferred

[@] parent item
    [x] completed child item
    [ ] new child item #tag1
    [@] active child item
        [ ] new second level child #tag2
        [x] completed second level child

[ ] item with #project or #category tags
"""


class TestTodoItem(unittest.TestCase):

    def test_defaults(self):
        item = TodoItem(text="test", status="new", level=0)
        self.assertEqual(item.tags, [])
        self.assertEqual(item.children, [])
        self.assertIsNone(item.parent)
        self.assertIsNone(item.source_project)
        self.assertIsNone(item.source_file)
        self.assertIsNone(item.priority)
        self.assertIsNone(item.target_date)
        self.assertEqual(item.space_after_status, " ")

    def test_initialization(self):
        child = TodoItem(text="child", status="new", level=1)
        parent = TodoItem(
            text="parent",
            status="active",
            level=0,
            priority=1,
            target_date="2025-01-01",
            space_after_status="   ",
            tags=["proj"],
            children=[child],
            source_project="testproj",
            source_file="test.xit",
        )
        child.parent = parent

        self.assertEqual(parent.text, "parent")
        self.assertEqual(parent.status, "active")
        self.assertEqual(parent.level, 0)
        self.assertEqual(parent.priority, 1)
        self.assertEqual(parent.target_date, "2025-01-01")
        self.assertEqual(parent.space_after_status, "   ")
        self.assertEqual(parent.tags, ["proj"])
        self.assertEqual(parent.children, [child])
        self.assertIsNone(parent.parent)
        self.assertEqual(parent.source_project, "testproj")
        self.assertEqual(parent.source_file, "test.xit")

        self.assertEqual(child.text, "child")
        self.assertEqual(child.status, "new")
        self.assertEqual(child.level, 1)
        self.assertEqual(child.tags, [])
        self.assertEqual(child.children, [])
        self.assertEqual(child.parent, parent)
        # child doesn't inherit source attributes unless explicitly set
        self.assertIsNone(child.source_project)
        self.assertIsNone(child.source_file)


class TestXiteParsing(unittest.TestCase):

    def setUp(self):
        self.parsed_items = parse_todo_list(BARE_XIT_CONTENT)

    def test_parse_counts(self):
        self.assertEqual(len(self.parsed_items), 9, "should have 9 top-level items")
        parent_item = next(
            (item for item in self.parsed_items if item.text == "parent item"), None
        )
        self.assertIsNotNone(parent_item)
        self.assertEqual(
            len(parent_item.children), 3, "parent item should have 3 children"
        )
        active_child = next(
            (child for child in parent_item.children if child.status == "active"), None
        )
        self.assertIsNotNone(active_child)
        self.assertEqual(
            len(active_child.children), 2, "active child item should have 2 children"
        )

    def test_parse_content(self):
        new_item = self.parsed_items[0]
        self.assertEqual(new_item.status, "new")
        self.assertEqual(new_item.text, "new item")
        self.assertEqual(new_item.level, 0)
        self.assertEqual(new_item.tags, [])

        parent_item = self.parsed_items[7]
        new_child = parent_item.children[1]
        self.assertEqual(new_child.status, "new")
        self.assertEqual(new_child.text, "new child item #tag1")
        self.assertEqual(new_child.level, 1)
        self.assertEqual(new_child.tags, ["tag1"])

        active_child = parent_item.children[2]
        new_second_level = active_child.children[0]
        self.assertEqual(new_second_level.status, "new")
        self.assertEqual(new_second_level.text, "new second level child #tag2")
        self.assertEqual(new_second_level.level, 2)
        self.assertEqual(new_second_level.tags, ["tag2"])

        multi_tag_item = self.parsed_items[8]
        self.assertEqual(multi_tag_item.status, "new")
        self.assertEqual(multi_tag_item.text, "item with #project or #category tags")
        self.assertEqual(multi_tag_item.level, 0)
        self.assertEqual(multi_tag_item.tags, ["project", "category"])

    def test_parse_empty_input(self):
        self.assertEqual(parse_todo_list(""), [])

    def test_parse_whitespace_and_comments(self):
        content = """
        # this is a comment
          \t
        another non-item line.
        """
        self.assertEqual(parse_todo_list(content), [])

    def test_parse_invalid_status_char(self):
        content = "[z] invalid status"
        items = parse_todo_list(content)
        self.assertEqual(
            len(items), 0, "line with invalid status char should be skipped"
        )

    def test_parse_inconsistent_indentation(self):
        content = """
[ ] level 0
    [ ] level 1 (4 spaces)
      [ ] level 1.5? (6 spaces -> level 1)
        [ ] level 2 (8 spaces)
"""
        items = parse_todo_list(content)
        self.assertEqual(len(items), 1, "should be one top-level item")
        self.assertEqual(items[0].level, 0, "top level is 0")
        self.assertEqual(
            len(items[0].children), 2, "level 0 should have 2 children (4sp and 6sp)"
        )

        level1_4sp = items[0].children[0]
        level1_6sp = items[0].children[1]

        self.assertEqual(level1_4sp.level, 1, "first child level is 1 (4 spaces)")
        self.assertEqual(
            len(level1_4sp.children),
            0,
            "first child (4 spaces) should have no children",
        )

        self.assertEqual(level1_6sp.level, 1, "second child level is 1 (6 spaces // 4)")
        self.assertEqual(
            len(level1_6sp.children), 1, "second child (6 spaces) should have 1 child"
        )

        level2_8sp = level1_6sp.children[0]
        self.assertEqual(level2_8sp.level, 2, "grandchild level is 2 (8 spaces)")
        self.assertEqual(
            len(level2_8sp.children), 0, "grandchild (8 spaces) should have no children"
        )

        # check for inconsistent indent warning
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            parse_todo_list(content)
        # line numbers refer to the raw content, which starts with a blank line
        self.assertIn(
            "warning: line 4: inconsistent indent (6 spaces)", stderr_capture.getvalue()
        )

    def test_parse_parent_links(self):
        parent_item = self.parsed_items[7]
        new_child = parent_item.children[1]
        active_child = parent_item.children[2]
        new_second_level = active_child.children[0]

        self.assertIsNone(parent_item.parent)
        self.assertEqual(new_child.parent, parent_item)
        self.assertEqual(active_child.parent, parent_item)
        self.assertEqual(new_second_level.parent, active_child)

    def test_parse_line_continuation(self):
        content = """
[ ] task 1
    this is the first continuation line.
    and this is the second. #cont_tag
[ ] task 2
    [@] subtask 2.1
        continuation for subtask.
    [ ] subtask 2.2
        another continuation.
            even more indented continuation.
[ ] task 3 #tag3
    continuation for task 3.
"""
        items = parse_todo_list(content)

        self.assertEqual(len(items), 3)
        task1 = items[0]
        self.assertEqual(task1.status, "new")
        self.assertEqual(task1.level, 0)
        expected_text1 = "task 1\nthis is the first continuation line.\nand this is the second. #cont_tag"
        self.assertEqual(task1.text, expected_text1)
        self.assertEqual(task1.tags, ["cont_tag"])  # tag from continuation line
        self.assertEqual(len(task1.children), 0)

        task2 = items[1]
        self.assertEqual(task2.status, "new")
        self.assertEqual(task2.level, 0)
        self.assertEqual(task2.text, "task 2")
        self.assertEqual(task2.tags, [])
        self.assertEqual(len(task2.children), 2)

        subtask2_1 = task2.children[0]
        self.assertEqual(subtask2_1.status, "active")
        self.assertEqual(subtask2_1.level, 1)
        expected_text2_1 = "subtask 2.1\ncontinuation for subtask."
        self.assertEqual(subtask2_1.text, expected_text2_1)
        self.assertEqual(subtask2_1.tags, [])
        self.assertEqual(len(subtask2_1.children), 0)

        subtask2_2 = task2.children[1]
        self.assertEqual(subtask2_2.status, "new")
        self.assertEqual(subtask2_2.level, 1)
        expected_text2_2 = (
            "subtask 2.2\nanother continuation.\neven more indented continuation."
        )
        self.assertEqual(subtask2_2.text, expected_text2_2)
        self.assertEqual(subtask2_2.tags, [])
        self.assertEqual(len(subtask2_2.children), 0)

        task3 = items[2]
        self.assertEqual(task3.status, "new")
        self.assertEqual(task3.level, 0)
        expected_text3 = "task 3 #tag3\ncontinuation for task 3."
        self.assertEqual(task3.text, expected_text3)
        self.assertEqual(
            task3.tags, ["tag3"]
        )  # tag from first line (re-parsed with continuation)
        self.assertEqual(len(task3.children), 0)

    def test_parse_line_continuation_edge_cases(self):
        content = """
[ ] task 1
    continuation 1
  not a continuation (less indented)
    [ ] subtask (not a continuation)
    # just a comment line, indented (same indent as subtask)
    another continuation (same indent as subtask)
        deeper continuation for subtask #subtag
"""
        items = parse_todo_list(content)

        self.assertEqual(len(items), 1)
        task1 = items[0]
        # line with indent 2 ("not a continuation...") is appended because it's not a valid task line
        expected_task1_text = (
            "task 1\ncontinuation 1\nnot a continuation (less indented)"
        )
        self.assertEqual(task1.text, expected_task1_text)
        self.assertEqual(len(task1.children), 1)

        subtask = task1.children[0]
        expected_subtask_text = (
            "subtask (not a continuation)\ndeeper continuation for subtask #subtag"
        )
        self.assertEqual(subtask.text, expected_subtask_text)
        self.assertEqual(subtask.status, "new")
        self.assertEqual(subtask.level, 1)
        self.assertEqual(subtask.tags, ["subtag"])  # tag from continuation

    def test_parse_priorities(self):
        content = """
[ ] ! high priority
[ ] !! very high priority
[ ] . low priority
[ ] .. very low priority
[ ] no priority
[ ] !. mixed is not a priority
[ ] .! also not a priority
[ ] !not a priority (no space)
[ ] ! a ! b priority is 1
"""
        items = parse_todo_list(content)
        self.assertEqual(len(items), 9)
        self.assertEqual(items[0].priority, 1)
        self.assertEqual(items[0].text, "high priority")
        self.assertEqual(items[1].priority, 2)
        self.assertEqual(items[1].text, "very high priority")
        self.assertEqual(items[2].priority, -1)
        self.assertEqual(items[2].text, "low priority")
        self.assertEqual(items[3].priority, -2)
        self.assertEqual(items[3].text, "very low priority")
        self.assertIsNone(items[4].priority)
        self.assertEqual(items[4].text, "no priority")
        self.assertIsNone(items[5].priority)
        self.assertEqual(items[5].text, "!. mixed is not a priority")
        self.assertIsNone(items[6].priority)
        self.assertEqual(items[6].text, ".! also not a priority")
        self.assertIsNone(items[7].priority)
        self.assertEqual(items[7].text, "!not a priority (no space)")
        self.assertEqual(items[8].priority, 1)
        self.assertEqual(items[8].text, "a ! b priority is 1")

    def test_parse_target_dates(self):
        content = """
[ ] simple date -> 2025-07-31
[ ] date with text -> 2025-08-01 do something
[ ] text with date do something -> 2025-08-15
[ ] partial date year-month -> 2025-09
[ ] partial date year -> 2025
[ ] date inside text (first one wins) -> 2025-10-10 and also -> 2025-11-11
[ ] no date
[ ] invalid arrow ->2025-01-01
[ ] invalid arrow - > 2025-01-01
"""
        items = parse_todo_list(content)
        self.assertEqual(len(items), 9)
        self.assertEqual(items[0].target_date, "2025-07-31")
        self.assertEqual(items[0].text, "simple date")
        self.assertEqual(items[1].target_date, "2025-08-01")
        self.assertEqual(items[1].text, "date with text do something")
        self.assertEqual(items[2].target_date, "2025-08-15")
        self.assertEqual(items[2].text, "text with date do something")
        self.assertEqual(items[3].target_date, "2025-09")
        self.assertEqual(items[3].text, "partial date year-month")
        self.assertEqual(items[4].target_date, "2025")
        self.assertEqual(items[4].text, "partial date year")
        self.assertEqual(items[5].target_date, "2025-10-10")
        self.assertEqual(
            items[5].text, "date inside text (first one wins) and also -> 2025-11-11"
        )
        self.assertIsNone(items[6].target_date)
        self.assertEqual(items[6].text, "no date")
        self.assertIsNone(items[7].target_date)
        self.assertEqual(items[7].text, "invalid arrow ->2025-01-01")
        self.assertIsNone(items[8].target_date)
        self.assertEqual(items[8].text, "invalid arrow - > 2025-01-01")

    def test_parse_whitespace_after_status(self):
        content = """
[ ] one space
[ ]  two spaces
[ ]   three spaces
[ ]	one tab
[ ]notask
"""
        items = parse_todo_list(content)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0].space_after_status, " ")
        self.assertEqual(items[0].text, "one space")
        self.assertEqual(items[1].space_after_status, "  ")
        self.assertEqual(items[1].text, "two spaces")
        self.assertEqual(items[2].space_after_status, "   ")
        self.assertEqual(items[2].text, "three spaces")
        self.assertEqual(items[3].space_after_status, "\t")
        self.assertEqual(items[3].text, "one tab")


# helper to mimic removed format_todo_list using the internal _format_recursive
def _test_format_todo_list(items: List[TodoItem]) -> str:
    """formats a list of todoitem objects back into an indented string."""
    all_lines: List[str] = []
    for item in items:
        _format_recursive(item, all_lines)
    return "\n".join(all_lines)


class TestXiteFormatting(unittest.TestCase):

    def test_format_empty_list(self):
        self.assertEqual(_test_format_todo_list([]), "")

    def test_format_simple(self):
        items = [
            TodoItem(text="task 1", status="new", level=0),
            TodoItem(text="task 2", status="active", level=0),
        ]
        expected = "[ ] task 1\n[@] task 2"
        self.assertEqual(_test_format_todo_list(items), expected)

    def test_format_nested(self):
        items = [
            TodoItem(
                text="parent",
                status="active",
                level=0,
                children=[
                    TodoItem(text="child 1", status="complete", level=1),
                    TodoItem(
                        text="child 2",
                        status="new",
                        level=1,
                        children=[
                            TodoItem(text="grandchild", status="blocked", level=2)
                        ],
                    ),
                ],
            )
        ]
        expected = """[@] parent
    [x] child 1
    [ ] child 2
        [!] grandchild"""
        self.assertEqual(_test_format_todo_list(items), expected)

    def test_format_unknown_status(self):
        items = [TodoItem(text="unknown", status="weird", level=0)]
        expected = "[ ] unknown"
        self.assertEqual(_test_format_todo_list(items), expected)

    def test_format_reversibility(self):
        # test format(parse(text)) == text (ignoring empty lines/comments)
        parsed_items = parse_todo_list(BARE_XIT_CONTENT)
        formatted_output = _test_format_todo_list(parsed_items)
        expected_lines = [
            line.strip()
            for line in BARE_XIT_CONTENT.strip().splitlines()
            if line.strip()
        ]
        actual_lines = [
            line.strip()
            for line in formatted_output.strip().splitlines()
            if line.strip()
        ]

        self.assertEqual(
            actual_lines,
            expected_lines,
            "formatted output should match normalized original input",
        )

    def test_format_multiline_text(self):
        items = [
            TodoItem(
                text="parent task\nfirst continuation.\nsecond continuation.",
                status="active",
                level=0,
                children=[
                    TodoItem(
                        text="child task\nchild continuation.", status="new", level=1
                    )
                ],
            ),
            TodoItem(text="single line task", status="complete", level=0),
        ]
        items[0].children[0].parent = items[0]

        expected_output = """
[@] parent task
    first continuation.
    second continuation.
    [ ] child task
        child continuation.
[x] single line task
""".strip()

        formatted_output = _test_format_todo_list(items)
        self.assertEqual(formatted_output, expected_output)

    def test_format_preserves_whitespace_after_status(self):
        items = [
            TodoItem(text="one", status="new", level=0, space_after_status=" "),
            TodoItem(text="two", status="new", level=0, space_after_status="  "),
            TodoItem(text="tab", status="new", level=0, space_after_status="\t"),
        ]
        expected_output = """
[ ] one
[ ]  two
[ ]	tab
""".strip()
        formatted_output = _test_format_todo_list(items)
        self.assertEqual(formatted_output, expected_output)

    def test_format_with_priority_and_date(self):
        items = [
            TodoItem(text="high prio", status="new", level=0, priority=2),
            TodoItem(text="low prio", status="new", level=0, priority=-2),
            TodoItem(text="with date", status="new", level=0, target_date="2025-12-25"),
            TodoItem(
                text="prio and date",
                status="new",
                level=0,
                priority=1,
                target_date="2025-01-01",
            ),
            TodoItem(
                text="prio and date with child",
                status="active",
                level=0,
                priority=-1,
                target_date="2025-02-01",
                children=[
                    TodoItem(
                        text="child",
                        status="new",
                        level=1,
                        priority=3,
                        target_date="2025-03-01",
                    )
                ],
            ),
        ]
        expected_output = """
[ ] !! high prio
[ ] .. low prio
[ ] with date -> 2025-12-25
[ ] ! prio and date -> 2025-01-01
[@] . prio and date with child -> 2025-02-01
    [ ] !!! child -> 2025-03-01
""".strip()
        formatted_output = _test_format_todo_list(items)
        self.assertEqual(formatted_output, expected_output)

    def test_format_roundtrip_whitespace_exact(self):
        """tests that parsing and formatting preserves whitespace exactly for valid todo items."""
        # content from examples/whitespace.xit
        content = (
            "[ ] one space\n"
            "[ ]  two spaces\n"
            "[ ]   three spaces\n"
            "[ ]\tone tab\n"
            "[ ]not a task"
        )
        parsed_items = parse_todo_list(content)
        formatted_output = _test_format_todo_list(parsed_items)

        # The line "[ ]not a task" is invalid and ignored by the parser.
        # Expected output contains only valid lines, with exact whitespace.
        expected_output = (
            "[ ] one space\n" "[ ]  two spaces\n" "[ ]   three spaces\n" "[ ]\tone tab"
        )
        self.assertEqual(formatted_output, expected_output)


class TestXiteFiltering(unittest.TestCase):

    def setUp(self):
        self.items = [
            TodoItem(text="top 1 #projA", status="new", level=0, tags=["projA"]),
            TodoItem(
                text="top 2 #projB",
                status="active",
                level=0,
                tags=["projB"],
                children=[
                    TodoItem(
                        text="child 2.1 #projA",
                        status="complete",
                        level=1,
                        tags=["projA"],
                    ),
                    TodoItem(
                        text="child 2.2",
                        status="new",
                        level=1,
                        children=[
                            TodoItem(
                                text="grandchild 2.2.1 #projC",
                                status="blocked",
                                level=2,
                                tags=["projC"],
                            )
                        ],
                    ),
                ],
            ),
            TodoItem(text="top 3", status="complete", level=0),
        ]
        # set parent links manually after deepcopy for testing filter logic
        self.original_items = deepcopy(self.items)
        self.original_items[1].children[0].parent = self.original_items[1]
        self.original_items[1].children[1].parent = self.original_items[1]
        self.original_items[1].children[1].children[0].parent = self.original_items[
            1
        ].children[1]

    def test_filter_no_filters(self):
        # Add priority and date to an item to check for copying
        self.original_items[0].priority = 1
        self.original_items[0].target_date = "2025-01-01"
        self.original_items[0].space_after_status = "  "

        filtered = filter_items(
            self.original_items, filter_tags=None, filter_statuses=None
        )
        self.assertIsNot(filtered, self.original_items)  # ensure it's a copy
        self.assertEqual(
            _test_format_todo_list(filtered),
            _test_format_todo_list(self.original_items),
            "formatted output of filtered list should match original when no filters applied",
        )
        # check all attributes are copied
        self.assertEqual(
            filtered[0].source_project, self.original_items[0].source_project
        )
        self.assertEqual(filtered[0].source_file, self.original_items[0].source_file)
        self.assertEqual(filtered[0].priority, self.original_items[0].priority)
        self.assertEqual(filtered[0].target_date, self.original_items[0].target_date)
        self.assertEqual(
            filtered[0].space_after_status, self.original_items[0].space_after_status
        )

    def test_filter_empty_list(self):
        filtered = filter_items([], filter_tags=["projA"], filter_statuses=["new"])
        self.assertEqual(filtered, [])

    def test_filter_no_matches(self):
        filtered = filter_items(
            self.original_items, filter_tags=["nonexistent"], filter_statuses=None
        )
        self.assertEqual(filtered, [])
        filtered = filter_items(
            self.original_items, filter_tags=None, filter_statuses=["nonexistent"]
        )
        self.assertEqual(filtered, [])
        filtered = filter_items(
            self.original_items, filter_tags=["projA"], filter_statuses=["blocked"]
        )
        self.assertEqual(filtered, [])

    def test_filter_by_tag(self):
        # filter by tag #proja
        filtered = filter_items(
            self.original_items, filter_tags=["projA"], filter_statuses=None
        )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0].text, "top 1 #projA")
        self.assertEqual(len(filtered[0].children), 0)
        self.assertEqual(filtered[1].text, "top 2 #projB")  # ancestor kept
        self.assertEqual(len(filtered[1].children), 1)
        self.assertEqual(filtered[1].children[0].text, "child 2.1 #projA")
        self.assertEqual(len(filtered[1].children[0].children), 0)

    def test_filter_by_tag_parent_match(self):
        # filter by tag #projb (matches parent)
        filtered = filter_items(
            self.original_items, filter_tags=["projB"], filter_statuses=None
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].text, "top 2 #projB")
        self.assertEqual(len(filtered[0].children), 2)  # keep all original children
        self.assertEqual(filtered[0].children[0].text, "child 2.1 #projA")
        self.assertEqual(filtered[0].children[1].text, "child 2.2")
        self.assertEqual(
            len(filtered[0].children[1].children), 1
        )  # keep original grandchild
        self.assertEqual(
            filtered[0].children[1].children[0].text, "grandchild 2.2.1 #projC"
        )

    def test_filter_by_multiple_tags(self):
        # filter by tags #proja or #projc
        filtered = filter_items(
            self.original_items, filter_tags=["projA", "projC"], filter_statuses=None
        )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0].text, "top 1 #projA")
        self.assertEqual(len(filtered[0].children), 0)
        self.assertEqual(filtered[1].text, "top 2 #projB")  # ancestor kept
        self.assertEqual(
            len(filtered[1].children), 2
        )  # keep both children because descendants matched
        self.assertEqual(filtered[1].children[0].text, "child 2.1 #projA")
        self.assertEqual(filtered[1].children[1].text, "child 2.2")
        self.assertEqual(len(filtered[1].children[1].children), 1)
        self.assertEqual(
            filtered[1].children[1].children[0].text, "grandchild 2.2.1 #projC"
        )

    def test_filter_by_status(self):
        # filter by status 'new'
        filtered = filter_items(
            self.original_items, filter_tags=None, filter_statuses=["new"]
        )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0].text, "top 1 #projA")
        self.assertEqual(
            len(filtered[0].children),
            0,
            "parent matched ('new'), keep original children (none)",
        )
        self.assertEqual(filtered[1].text, "top 2 #projB")  # ancestor kept
        self.assertEqual(len(filtered[1].children), 1)
        self.assertEqual(
            filtered[1].children[0].text, "child 2.2"
        )  # child matched ('new')
        self.assertEqual(
            len(filtered[1].children[0].children),
            1,
            "child matched ('new'), keep original grandchild",
        )
        self.assertEqual(
            filtered[1].children[0].children[0].text, "grandchild 2.2.1 #projC"
        )

    def test_filter_by_multiple_statuses(self):
        # filter by status 'active' or 'blocked'
        filtered = filter_items(
            self.original_items, filter_tags=None, filter_statuses=["active", "blocked"]
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].text, "top 2 #projB")  # parent matched ('active')
        self.assertEqual(filtered[0].status, "active")
        self.assertEqual(len(filtered[0].children), 2)  # keep all original children
        self.assertEqual(filtered[0].children[0].text, "child 2.1 #projA")
        self.assertEqual(filtered[0].children[1].text, "child 2.2")
        self.assertEqual(
            len(filtered[0].children[1].children), 1
        )  # keep original grandchild
        self.assertEqual(
            filtered[0].children[1].children[0].text, "grandchild 2.2.1 #projC"
        )

    def test_filter_combined_tag_status(self):
        # filter by tag #proja and status 'complete'
        filtered = filter_items(
            self.original_items, filter_tags=["projA"], filter_statuses=["complete"]
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].text, "top 2 #projB")  # ancestor kept
        self.assertEqual(len(filtered[0].children), 1)
        self.assertEqual(
            filtered[0].children[0].text, "child 2.1 #projA"
        )  # child matched
        self.assertEqual(filtered[0].children[0].status, "complete")
        self.assertEqual(len(filtered[0].children[0].children), 0)

    def test_filter_only_grandchild_matches(self):
        filtered = filter_items(
            self.original_items, filter_tags=["projC"], filter_statuses=["blocked"]
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].text, "top 2 #projB")  # ancestor kept
        self.assertEqual(len(filtered[0].children), 1)
        self.assertEqual(filtered[0].children[0].text, "child 2.2")  # ancestor kept
        self.assertEqual(len(filtered[0].children[0].children), 1)
        self.assertEqual(
            filtered[0].children[0].children[0].text, "grandchild 2.2.1 #projC"
        )  # grandchild matched
        self.assertEqual(filtered[0].children[0].children[0].status, "blocked")


class TestXiteSorting(unittest.TestCase):

    def test_sort_empty_list(self):
        items = []
        sort_items_by_status(items)
        self.assertEqual(items, [])

    def test_sort_by_status(self):
        items = [
            TodoItem(text="c", status="complete", level=0),
            TodoItem(text="a", status="active", level=0),
            TodoItem(text="n", status="new", level=0),
            TodoItem(text="u", status="undecided", level=0),
            TodoItem(text="d", status="deferred", level=0),
            TodoItem(text="s", status="obsolete", level=0),
            TodoItem(text="?", status="unknown", level=0),
        ]
        expected_order = sorted(STATUS_ORDER.keys(), key=lambda k: STATUS_ORDER[k])
        present_statuses = {item.status for item in items}
        expected_order_filtered = [s for s in expected_order if s in present_statuses]

        sort_items_by_status(items)
        statuses = [item.status for item in items]

        self.assertEqual(statuses, expected_order_filtered)

    def test_sort_nested(self):
        items = [
            TodoItem(
                text="parent",
                status="new",
                level=0,
                children=[
                    TodoItem(text="child complete", status="complete", level=1),
                    TodoItem(text="child active", status="active", level=1),
                    TodoItem(text="child new", status="new", level=1),
                ],
            ),
            TodoItem(text="another parent", status="active", level=0),
        ]
        sort_items_by_status(items)

        self.assertEqual(items[0].status, "active")
        self.assertEqual(items[1].status, "new")

        new_parent_children = items[1].children
        child_statuses = [child.status for child in new_parent_children]
        expected_child_order = sorted(
            ["complete", "active", "new"], key=lambda s: STATUS_ORDER.get(s, 99)
        )
        self.assertEqual(child_statuses, expected_child_order)


# --- integration tests ---


def run_main_and_capture(args_list):
    """helper: runs xite.main with specified args and returns captured stdout.

    stderr is left alone so tests can capture warnings/errors themselves.
    """
    stdout_capture = io.StringIO()
    with patch("sys.argv", ["xite.py"] + args_list):
        with contextlib.redirect_stdout(stdout_capture):
            try:
                main()
            except SystemExit as e:
                pass
    return stdout_capture.getvalue()


def run_main_and_capture_all(args_list):
    """helper: runs xite.main and returns (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch("sys.argv", ["xite.py"] + args_list):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as e:
                code = e.code if e.code else 0
    return out.getvalue(), err.getvalue(), code


class TestXiteIntegration(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.test_dir)

        self.xit_file_path = os.path.join(self.test_dir, "simple.xit")
        with open(self.xit_file_path, "w") as f:
            f.write("[ ] task from simple.xit\n")
            f.write("    [x] subtask from simple.xit\n")

        self.toml_file_path = os.path.join(self.test_dir, "tracker.toml")
        self.included_file_path = os.path.join(self.test_dir, "included.xit")
        self.project_a_dir = os.path.join(self.test_dir, "project_a")
        self.project_a_roadmap = os.path.join(self.project_a_dir, "ROADMAP.md")
        self.project_b_dir = os.path.join(self.test_dir, "project_b")
        self.project_b_roadmap = os.path.join(self.project_b_dir, "ROADMAP.xit")
        self.other_dir = os.path.join(self.test_dir, "other_stuff")
        self.other_roadmap = os.path.join(self.other_dir, "OTHER_TASKS.md")

        os.makedirs(self.project_a_dir)
        os.makedirs(self.project_b_dir)
        os.makedirs(self.other_dir)

        with open(self.included_file_path, "w") as f:
            f.write("[@] included task for proj1\n")

        with open(self.project_a_roadmap, "w") as f:
            f.write("[ ] task from project a roadmap\n")
            f.write("[ ] another task from project a\n")

        with open(self.project_b_roadmap, "w") as f:
            f.write("[!] task from project b roadmap\n")

        with open(self.other_roadmap, "w") as f:
            f.write("[?] task from other stuff\n")

        with open(self.toml_file_path, "w") as f:
            f.write("""
[META]
search_dirs = ["./project_a", "project_b"] # search relative to toml

[Proj1]
tasks = '''
[ ] task 1 for proj1 # keep toml comment
    [ ] subtask 1.1
[@] task 2 for proj1 #proj1tag
'''
include_files = ["included.xit"]

[Proj2]
tasks = '''
[x] task x for proj2
'''

# project using meta search_dirs, custom roadmap_files
[Proj3_OtherRoadmap]
search_dirs = ["../other_stuff"] # relative path up
roadmap_files = ["OTHER_TASKS.md"] # custom roadmap name

[Proj4_MaxTasks]
tasks = '''
[ ] task a
[ ] task b
[ ] task c
'''
            """)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.test_dir)

    def test_xit_file_processing(self):
        output = run_main_and_capture([self.xit_file_path])
        self.assertIn(f"--- {self.xit_file_path} ---", output)
        self.assertIn("[ ] task from simple.xit", output)
        self.assertIn("[x] subtask from simple.xit", output)
        # ensure header isn't immediately followed by another header
        output_lines = output.splitlines()
        if len(output_lines) > 1:
            self.assertNotIn("---", output_lines[1])

    def test_toml_inline_tasks(self):
        output = run_main_and_capture(
            [self.toml_file_path, "--project", "Proj1", "--project", "Proj2"]
        )
        self.assertIn("--- Proj1 ---", output)
        self.assertIn("[ ] task 1 for proj1 # keep toml comment", output)
        self.assertIn("[@] task 2 for proj1 #proj1tag", output)
        self.assertIn("--- Proj2 ---", output)
        self.assertIn("[x] task x for proj2", output)
        self.assertIn("[@] included task for proj1", output)
        self.assertNotIn("--- project_a ---", output)
        self.assertNotIn("--- project_b ---", output)

    def test_toml_include_files(self):
        output = run_main_and_capture([self.toml_file_path, "--project", "Proj1"])
        self.assertIn("--- Proj1 ---", output)
        self.assertIn("[@] included task for proj1", output)
        lines = output.splitlines()
        proj1_header_index = lines.index("--- Proj1 ---")
        included_task_index = lines.index("[@] included task for proj1")
        self.assertGreater(
            included_task_index,
            proj1_header_index,
            "included task should be under proj1 header",
        )

    def test_toml_project_discovery(self):
        output = run_main_and_capture([self.toml_file_path])
        self.assertIn("--- project_a ---", output)
        self.assertIn("[ ] task from project a roadmap", output)
        self.assertIn("--- project_b ---", output)
        self.assertIn("[!] task from project b roadmap", output)
        self.assertIn("--- Proj1 ---", output)
        self.assertIn("--- Proj2 ---", output)
        self.assertNotIn("--- other_stuff ---", output)
        self.assertNotIn("[?] task from other stuff", output)

    def test_toml_custom_roadmap_files(self):
        custom_toml_path = os.path.join(self.test_dir, "custom_meta.toml")
        with open(custom_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["./other_stuff"]
roadmap_files = ["OTHER_TASKS.md", "nonexistent.file"] # custom name + nonexistent

[SomeProject]
tasks = "[ ] dummy task"
            """)
        output = run_main_and_capture([custom_toml_path])
        self.assertIn("--- other_stuff ---", output)
        self.assertIn("[?] task from other stuff", output)
        self.assertIn("--- SomeProject ---", output)

    def test_project_filtering(self):
        output = run_main_and_capture(
            [self.toml_file_path, "--project", "project_a", "--project", "Proj1"]
        )
        self.assertIn("--- project_a ---", output)
        self.assertIn("[ ] task from project a roadmap", output)
        self.assertIn("--- Proj1 ---", output)
        self.assertIn("[ ] task 1 for proj1 # keep toml comment", output)
        self.assertIn("[@] included task for proj1", output)
        self.assertNotIn("--- project_b ---", output)
        self.assertNotIn("--- Proj2 ---", output)
        self.assertNotIn("--- Proj4_MaxTasks ---", output)

    def test_max_tasks_limit(self):
        output = run_main_and_capture(
            [self.toml_file_path, self.xit_file_path, "--max-tasks", "1"]
        )

        self.assertIn(f"--- {self.xit_file_path} ---", output)
        self.assertIn("[ ] task from simple.xit", output)
        self.assertIn(
            "[x] subtask from simple.xit", output
        )  # subtask included with parent

        self.assertIn("--- Proj1 ---", output)
        self.assertIn("[ ] task 1 for proj1 # keep toml comment", output)
        self.assertNotIn("[@] task 2 for proj1", output)
        self.assertNotIn(
            "[@] included task for proj1", output
        )  # included task excluded by max_tasks

        self.assertIn("--- Proj2 ---", output)
        self.assertIn("[x] task x for proj2", output)

        self.assertIn("--- project_a ---", output)
        self.assertIn("[ ] task from project a roadmap", output)
        self.assertNotIn("[ ] another task from project a", output)
        self.assertIn("--- project_b ---", output)
        self.assertIn("[!] task from project b roadmap", output)

        self.assertIn("--- Proj4_MaxTasks ---", output)
        self.assertIn("[ ] task a", output)
        self.assertNotIn("[ ] task b", output)
        self.assertNotIn("[ ] task c", output)

    def test_output_headers_order(self):
        output = run_main_and_capture([self.toml_file_path, self.xit_file_path])
        lines = output.splitlines()

        try:
            xit_header_idx = lines.index(f"--- {self.xit_file_path} ---")
            xit_item_idx = lines.index("[ ] task from simple.xit")
            self.assertLess(xit_header_idx, xit_item_idx)

            proj1_header_idx = lines.index("--- Proj1 ---")
            proj1_item_idx = lines.index("[ ] task 1 for proj1 # keep toml comment")
            self.assertLess(proj1_header_idx, proj1_item_idx)

            proja_header_idx = lines.index("--- project_a ---")
            proja_item_idx = lines.index("[ ] task from project a roadmap")
            self.assertLess(proja_header_idx, proja_item_idx)

        except ValueError as e:
            self.fail(
                f"expected header/item not found or out of order: {e}\noutput:\n{output}"
            )

    def test_missing_files_warnings(self):
        bad_toml_path = os.path.join(self.test_dir, "bad_refs.toml")
        with open(bad_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["./nonexistent_dir"]

[BadInclude]
include_files = ["./nonexistent_file.xit"]

[GoodProject]
tasks = "[ ] a good task"
            """)

        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", bad_toml_path]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(stderr_capture):
                    try:
                        main()
                    except SystemExit:
                        pass

        stderr_output = stderr_capture.getvalue()
        self.assertIn("warning: search directory not found:", stderr_output)
        self.assertIn("nonexistent_dir", stderr_output)
        self.assertIn("warning: file not found:", stderr_output)
        self.assertIn("nonexistent_file.xit", stderr_output)

    def test_toml_project_repo_config(self):
        repo_dir = os.path.join(self.test_dir, "my_repo")
        repo_roadmap = os.path.join(repo_dir, "ROADMAP.md")
        os.makedirs(repo_dir)
        with open(repo_roadmap, "w") as f:
            f.write("[ ] task from repo roadmap\n")

        repo_toml_path = os.path.join(self.test_dir, "repo_test.toml")
        with open(repo_toml_path, "w") as f:
            f.write("""
[MyRepoProject]
repo = { public = "./my_repo" } # relative path to repo dir from toml
tasks = "[ ] inline task for repo project"
            """)

        output = run_main_and_capture([repo_toml_path])
        self.assertIn("--- MyRepoProject ---", output)
        self.assertIn("[ ] inline task for repo project", output)
        self.assertIn("[ ] task from repo roadmap", output)

    def test_toml_search_depth_gt_1(self):
        level1_dir = os.path.join(self.test_dir, "level1")
        level2_dir = os.path.join(level1_dir, "level2")
        level3_dir = os.path.join(level2_dir, "level3")
        os.makedirs(level3_dir)
        with open(os.path.join(level1_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task level 1\n")
        with open(os.path.join(level2_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task level 2\n")
        with open(os.path.join(level3_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task level 3\n")

        depth_toml_path = os.path.join(self.test_dir, "depth_test.toml")
        with open(depth_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["."]
search_depth = 2 # find level1 and level2, but not level3 (due to pruning)
            """)

        output = run_main_and_capture([depth_toml_path])
        # level1 is discovered because '.' is searched, and level1 is a subdir
        self.assertIn("--- level1 ---", output)
        self.assertIn("[ ] task level 1", output)
        # level2 should *not* be discovered because level1 was found and walk pruned at depth 1
        self.assertNotIn("--- level2 ---", output)
        self.assertNotIn("[ ] task level 2", output)
        # level3 should also not be discovered
        self.assertNotIn("--- level3 ---", output)
        self.assertNotIn("[ ] task level 3", output)

    def test_toml_search_depth_infinite(self):
        level1_dir = os.path.join(self.test_dir, "level1")
        level2_dir = os.path.join(level1_dir, "level2")
        level3_dir = os.path.join(level2_dir, "level3")
        os.makedirs(level3_dir, exist_ok=True)
        with open(os.path.join(level1_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task level 1\n")
        with open(os.path.join(level2_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task level 2\n")
        with open(os.path.join(level3_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task level 3\n")

        depth_toml_path = os.path.join(self.test_dir, "depth_test_inf.toml")
        with open(depth_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["."]
search_depth = -1 # infinite depth (but should still prune)
            """)
        output = run_main_and_capture([depth_toml_path])
        self.assertIn("--- level1 ---", output)
        self.assertIn("[ ] task level 1", output)
        # level2 should *not* be discovered because level1 was found and walk pruned
        self.assertNotIn("--- level2 ---", output)
        self.assertNotIn("[ ] task level 2", output)
        # level3 should also not be discovered due to pruning at level1
        self.assertNotIn("--- level3 ---", output)
        self.assertNotIn("[ ] task level 3", output)

    def test_toml_search_depth_pruning(self):
        level1_dir = os.path.join(self.test_dir, "prune_l1")
        level2_dir = os.path.join(level1_dir, "prune_l2")
        os.makedirs(level2_dir)
        with open(os.path.join(level1_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task prune level 1\n")  # roadmap in parent
        with open(os.path.join(level2_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task prune level 2\n")  # roadmap in child

        prune_toml_path = os.path.join(self.test_dir, "prune_test.toml")
        with open(prune_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["."]
search_depth = -1 # infinite depth, but should prune at first find
            """)
        output = run_main_and_capture([prune_toml_path])
        self.assertIn("--- prune_l1 ---", output)
        self.assertIn("[ ] task prune level 1", output)
        # level2 should *not* be discovered because l1 was found and walk pruned
        self.assertNotIn("--- prune_l2 ---", output)
        self.assertNotIn("[ ] task prune level 2", output)

    def test_toml_meta_active_order(self):
        active_toml_path = os.path.join(self.test_dir, "active_test.toml")
        with open(active_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["./project_a", "./project_b"]
active = ["Proj2", "project_a"] # specify desired order

[Proj1]
tasks = "[ ] proj1 task"

[Proj2]
tasks = "[ ] proj2 task"
            """)
        output = run_main_and_capture([active_toml_path])
        lines = [line.strip() for line in output.splitlines() if line.strip()]

        # find indices of headers to check order
        try:
            idx_proj2 = lines.index("--- Proj2 ---")
            idx_proja = lines.index("--- project_a ---")
            idx_proj1 = lines.index("--- Proj1 ---")
            idx_projb = lines.index("--- project_b ---")
        except ValueError as e:
            self.fail(f"expected header not found in output: {e}\noutput:\n{output}")

        # assert order based on 'active' list ["Proj2", "project_a"],
        # then remaining projects in collection order ["Proj1", "project_b"].
        # expected final order: Proj2, project_a, Proj1, project_b
        self.assertLess(idx_proj2, idx_proja, "Proj2 should be before project_a")
        self.assertLess(
            idx_proja, idx_proj1, "project_a should be before Proj1 (remaining order)"
        )
        self.assertLess(
            idx_proj1, idx_projb, "Proj1 should be before project_b (remaining order)"
        )

    def test_toml_invalid_values_warnings(self):
        invalid_toml_path = os.path.join(self.test_dir, "invalid_values.toml")
        with open(invalid_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["valid_dir", 123] # invalid entry (non-string)
roadmap_files = "not_a_list" # invalid type (not list)
active = ["valid_proj", {}] # invalid entry (non-string)
search_depth = "abc" # invalid type (non-int)

[BadProject]
include_files = 555 # invalid type (not list)
repo = "not_a_dict" # invalid type (not dict)

[GoodProject]
tasks = "[ ] good task"
            """)

        stderr_capture = io.StringIO()
        stdout_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", invalid_toml_path]):
            with contextlib.redirect_stdout(stdout_capture):
                with contextlib.redirect_stderr(stderr_capture):
                    try:
                        main()
                    except SystemExit:
                        pass  # expected exit for cli apps

        stderr_output = stderr_capture.getvalue()
        stdout_output = (
            stdout_capture.getvalue()
        )  # check good project still loads despite errors

        # check meta warnings
        self.assertIn("warning: non-string path in META.search_dirs", stderr_output)
        self.assertIn("warning: invalid META.roadmap_files", stderr_output)
        self.assertIn("warning: invalid 'active' list in META", stderr_output)
        self.assertIn("warning: invalid META.search_depth", stderr_output)

        # check project-specific warnings
        self.assertIn(
            f"warning: invalid include_files entry for BadProject in {invalid_toml_path}. expected list, got int.",
            stderr_output,
        )
        self.assertIn(
            f"warning: invalid repo entry for BadProject in {invalid_toml_path}. expected dictionary, got str.",
            stderr_output,
        )

        # check that the good project was still processed
        self.assertIn("--- GoodProject ---", stdout_output)
        self.assertIn("[ ] good task", stdout_output)

        # check specific warning details
        self.assertIn("warning: non-string path in META.search_dirs in", stderr_output)
        self.assertIn(": 123", stderr_output)
        self.assertIn("warning: invalid META.roadmap_files in", stderr_output)
        self.assertIn(", using defaults", stderr_output)
        self.assertIn("warning: invalid 'active' list in META of", stderr_output)
        # check meta warning details again (redundant but ok)
        self.assertIn("warning: non-string path in META.search_dirs", stderr_output)
        self.assertIn(": 123", stderr_output)
        self.assertIn("warning: invalid META.roadmap_files", stderr_output)
        self.assertIn(", using defaults", stderr_output)
        self.assertIn("warning: invalid 'active' list in META", stderr_output)
        self.assertIn("warning: invalid META.search_depth", stderr_output)
        self.assertIn(": abc", stderr_output)

        # check project warning details again (redundant but ok)
        # covers line 428
        self.assertIn(
            f"warning: invalid include_files entry for BadProject in {invalid_toml_path}. expected list, got int.",
            stderr_output,
        )
        # covers line 487
        self.assertIn(
            f"warning: invalid repo entry for BadProject in {invalid_toml_path}. expected dictionary, got str.",
            stderr_output,
        )

        # check good project processed again (redundant but ok)
        self.assertIn("--- GoodProject ---", stdout_output)
        self.assertIn("[ ] good task", stdout_output)

    def test_max_projects_limit(self):
        """test the --max-projects argument."""
        # uses setup projects defined in self.toml_file_path
        # collection order (explicit toml first, then discovered):
        # Proj1, Proj2, Proj3_OtherRoadmap, Proj4_MaxTasks, project_a, project_b
        # --max-projects 2 should limit to the first two: Proj1, Proj2
        output = run_main_and_capture([self.toml_file_path, "--max-projects", "2"])

        # expect the first two projects based on collection order
        self.assertIn("--- Proj1 ---", output)
        self.assertIn("--- Proj2 ---", output)
        self.assertNotIn("--- Proj3_OtherRoadmap ---", output)
        self.assertNotIn("--- Proj4_MaxTasks ---", output)
        self.assertNotIn("--- project_a ---", output)
        self.assertNotIn("--- project_b ---", output)

    def test_list_projects_no_projects(self):
        empty_toml_file = os.path.join(self.test_dir, "no_projects_here.toml")
        with open(empty_toml_file, "w") as f:  # create empty file
            f.write("# empty\n")

        output = run_main_and_capture(["--list-projects", empty_toml_file])
        self.assertEqual(
            output.strip(), "", "output should be empty if no projects found"
        )

    def test_direct_directory_argument(self):
        output = run_main_and_capture([self.project_a_dir])
        # check project_a itself is found
        self.assertIn(f"--- {os.path.basename(self.project_a_dir)} ---", output)
        self.assertIn("[ ] task from project a roadmap", output)
        self.assertIn("[ ] another task from project a", output)
        # ensure other projects from general setup are not included
        self.assertNotIn("--- project_b ---", output)  # project_b is not passed
        self.assertNotIn("--- Proj1 ---", output)
        # check that subdirs are *not* scanned when passing a dir directly (depth=1)
        sub_project_dir = os.path.join(self.project_a_dir, "sub_project")
        os.makedirs(sub_project_dir)
        with open(os.path.join(sub_project_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task from sub project\n")
        # re-run with the sub-project existing
        output_with_sub = run_main_and_capture([self.project_a_dir])
        # check that subdirs *are* scanned when passing a dir directly (depth=1)
        self.assertIn("--- sub_project ---", output_with_sub)
        self.assertIn("[ ] task from sub project", output_with_sub)

    def test_list_projects_max_projects_limit(self):
        """test --list-projects respects --max-projects."""
        # uses setup projects defined in self.toml_file_path
        # collection order: Proj1, Proj2, Proj3_OtherRoadmap, Proj4_MaxTasks, project_a, project_b
        output = run_main_and_capture(
            [self.toml_file_path, "--list-projects", "--max-projects", "2"]
        )
        listed_projects = output.strip().splitlines()
        # expect the first two projects based on collection order
        self.assertEqual(listed_projects, ["Proj1", "Proj2"])

    def test_deprecated_project_handling(self):
        deprecated_toml_path = os.path.join(self.test_dir, "deprecated.toml")
        with open(deprecated_toml_path, "w") as f:
            f.write("""
[NormalProject]
tasks = "[ ] normal task"

[DeprecatedProject]
deprecated = true
tasks = '''
[ ] deprecated task 1
[x] deprecated task 2
'''

[AnotherNormal]
tasks = "[ ] another normal task"
            """)

        # test default behavior (deprecated project skipped)
        output_default = run_main_and_capture([deprecated_toml_path])
        self.assertIn("--- NormalProject ---", output_default)
        self.assertIn("[ ] normal task", output_default)
        self.assertIn("--- AnotherNormal ---", output_default)
        self.assertIn("[ ] another normal task", output_default)
        self.assertNotIn("--- DeprecatedProject ---", output_default)
        self.assertNotIn("[ ] deprecated task 1", output_default)

        # test with --include-deprecated flag
        output_included = run_main_and_capture(
            [deprecated_toml_path, "--include-deprecated"]
        )
        self.assertIn("--- NormalProject ---", output_included)
        self.assertIn("[ ] normal task", output_included)
        self.assertIn("--- DeprecatedProject ---", output_included)
        self.assertIn("[ ] deprecated task 1", output_included)
        self.assertIn("[x] deprecated task 2", output_included)
        self.assertIn("--- AnotherNormal ---", output_included)
        self.assertIn("[ ] another normal task", output_included)

        # test --list-projects default (deprecated skipped, toml insertion order)
        output_list_default = run_main_and_capture(
            [deprecated_toml_path, "--list-projects"]
        )
        project_list_default = output_list_default.strip().splitlines()
        # order based on definition in toml file
        expected_list_default = ["NormalProject", "AnotherNormal"]
        self.assertEqual(project_list_default, expected_list_default)

        # test --list-projects with --include-deprecated (deprecated included, toml insertion order)
        output_list_included = run_main_and_capture(
            [deprecated_toml_path, "--list-projects", "--include-deprecated"]
        )
        project_list_included = output_list_included.strip().splitlines()
        # order based on definition in toml file
        expected_list_included = ["NormalProject", "DeprecatedProject", "AnotherNormal"]
        self.assertEqual(project_list_included, expected_list_included)

    def test_list_projects_order_with_meta_active(self):
        ordered_toml_path = os.path.join(self.test_dir, "ordered_list.toml")
        with open(ordered_toml_path, "w") as f:
            f.write("""
[META]
active = ["ProjC", "ProjA"] # define order

[ProjA]
tasks = "[ ] a"

[ProjB]
tasks = "[ ] b"
deprecated = true

[ProjC]
tasks = "[ ] c"

[ProjD]
tasks = "[ ] d"
            """)

        # test default (--list-projects, no --include-deprecated)
        # expected order: ProjC, ProjA (from active), then ProjD (remaining, insertion order)
        output_default = run_main_and_capture([ordered_toml_path, "--list-projects"])
        list_default = output_default.strip().splitlines()
        expected_default = [
            "ProjC",
            "ProjA",
            "ProjD",
        ]  # ProjB is deprecated and skipped
        self.assertEqual(list_default, expected_default)

        # test with --include-deprecated
        # expected order: ProjC, ProjA (from active), then ProjB, ProjD (remaining, insertion order)
        output_included = run_main_and_capture(
            [ordered_toml_path, "--list-projects", "--include-deprecated"]
        )
        list_included = output_included.strip().splitlines()
        expected_included = [
            "ProjC",
            "ProjA",
            "ProjB",
            "ProjD",
        ]  # includes deprecated ProjB in its insertion order
        self.assertEqual(list_included, expected_included)

    def test_no_output_scenario(self):
        # test a scenario that produces no output (e.g., filter matches nothing)
        output = run_main_and_capture([self.toml_file_path, "--tag", "nonexistenttag"])
        self.assertEqual(output.strip(), "")

    def test_unspecified_source_handling(self):
        # test items without source info get grouped under UNSPECIFIED_SOURCE
        item_no_source = TodoItem(text="task without source", status="new", level=0)

        # mock args needed by _process_and_format_items
        mock_args = argparse.Namespace(
            filter_tags=None,
            filter_statuses=None,
            filter_matches=None,
            include_deprecated=False,
            max_projects=None,
            sort_by_status=False,
            max_tasks=None,
        )

        # call the processing function directly
        output_lines = _process_and_format_items(
            args=mock_args,
            raw_items=[item_no_source],
            collected_sources={
                UNSPECIFIED_SOURCE: False
            },  # source exists, not deprecated
            active_projects_from_toml=[],
            ordered_source_keys=[
                UNSPECIFIED_SOURCE
            ],  # provide the expected ordered keys
        )
        output = "\n".join(output_lines)

        self.assertIn(f"--- {UNSPECIFIED_SOURCE} ---", output)
        self.assertIn("[ ] task without source", output)

    def test_toml_search_dir_is_file_warning(self):
        file_as_dir_toml_path = os.path.join(self.test_dir, "file_as_dir.toml")
        existing_file = (
            self.included_file_path
        )  # use an existing file path as search_dir
        with open(file_as_dir_toml_path, "w") as f:
            f.write(f"""
[META]
search_dirs = ["{existing_file}"] # point search_dirs to a file
search_depth = 1
            """)

        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", file_as_dir_toml_path]):
            with contextlib.redirect_stdout(io.StringIO()):  # ignore stdout
                with contextlib.redirect_stderr(stderr_capture):
                    try:
                        main()
                    except SystemExit:
                        pass

        stderr_output = stderr_capture.getvalue()
        self.assertIn("warning: search directory not found:", stderr_output)
        self.assertIn(
            str(existing_file), stderr_output
        )  # check the file path is mentioned

    def test_toml_invalid_repo_paths(self):
        invalid_repo_toml_path = os.path.join(self.test_dir, "invalid_repo.toml")
        with open(invalid_repo_toml_path, "w") as f:
            f.write("""
[ProjectWithBadRepos]
repo = { public = 123, private = ["a", "list"] }
            """)

        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", invalid_repo_toml_path]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(stderr_capture):
                    try:
                        main()
                    except SystemExit:
                        pass

        stderr_output = stderr_capture.getvalue()
        # check for warnings triggered by non-string values in repo dict
        # covers line 498
        self.assertIn(
            "warning: invalid path for key 'public' in ProjectWithBadRepos",
            stderr_output,
        )
        self.assertIn("expected string, got int (value: '123').", stderr_output)
        # covers line 498 again
        self.assertIn(
            "warning: invalid path for key 'private' in ProjectWithBadRepos",
            stderr_output,
        )
        self.assertIn(
            "expected string, got list (value: '['a', 'list']').", stderr_output
        )

    def test_toml_invalid_repo_path_not_none_not_string(self):
        invalid_repo_toml_path = os.path.join(self.test_dir, "invalid_repo_type.toml")
        with open(invalid_repo_toml_path, "w") as f:
            f.write("""
[ProjectWithBadRepoType]
repo = { public = 123 } # invalid type (int), but not None
            """)

        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", invalid_repo_toml_path]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(stderr_capture):
                    try:
                        main()
                    except SystemExit:
                        pass

        stderr_output = stderr_capture.getvalue()
        # this covers the `elif repo_relpath is not None:` case in _process_toml_project_section
        self.assertIn(
            "warning: invalid path for key 'public' in ProjectWithBadRepoType",
            stderr_output,
        )
        self.assertIn("expected string, got int (value: '123').", stderr_output)

    def test_toml_repo_path_is_file(self):
        repo_file_toml_path = os.path.join(self.test_dir, "repo_is_file.toml")
        # create a file where a repo directory is expected
        repo_file_path = os.path.join(self.test_dir, "my_repo_file.txt")
        with open(repo_file_path, "w") as f:
            f.write("this is not a directory")

        with open(repo_file_toml_path, "w") as f:
            f.write(f"""
[RepoFileProject]
repo = {{ public = "{os.path.basename(repo_file_path)}" }}
            """)

        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", repo_file_toml_path]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(stderr_capture):
                    try:
                        main()
                    except SystemExit:
                        pass

        stderr_output = stderr_capture.getvalue()
        # check the warning for when the repo path exists but is not a directory
        self.assertIn(
            "warning: repo path is not a directory for key 'public' in RepoFileProject",
            stderr_output,
        )
        self.assertIn(
            f"{repo_file_path}", stderr_output
        )  # check resolved path is mentioned
        self.assertIn(
            f"from '{os.path.basename(repo_file_path)}'", stderr_output
        )  # check original path is mentioned

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    def test_toml_include_file_processing_error(self):
        include_err_toml_path = os.path.join(self.test_dir, "include_err.toml")
        bad_include_path = os.path.join(self.test_dir, "bad_include.xit")
        # don't create the file, let resolve fail later when processing includes

        with open(include_err_toml_path, "w") as f:
            f.write(f"""
[IncludeErrorProject]
include_files = ["{os.path.basename(bad_include_path)}"]
            """)

        stderr_capture = io.StringIO()
        # mock Path.resolve globally to raise error during processing
        with patch("sys.argv", ["xite.py", include_err_toml_path]):
            with patch(
                "pathlib.Path.resolve",
                side_effect=OSError("mock resolve error for include"),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(stderr_capture):
                        try:
                            main()
                        except SystemExit:
                            pass

        stderr_output = stderr_capture.getvalue()
        # the global patch of resolve causes the error when trying to resolve the toml path itself
        self.assertIn(
            f"warning: could not resolve path '{include_err_toml_path}': mock resolve error for include",
            stderr_output,
        )
        self.assertIn(
            f"error: input path could not be processed: {include_err_toml_path}",
            stderr_output,
        )


# --- tests with mocks for error conditions ---


def create_toml_str(**kwargs):
    """helper: create minimal toml content string."""
    lines = []
    for key, value in kwargs.items():
        if isinstance(value, dict):  # assume META or project section
            lines.append(f"[{key}]")
            for sub_key, sub_value in value.items():
                lines.append(f"{sub_key} = {repr(sub_value)}")
        else:  # direct key-value under root (not expected for valid structure)
            lines.append(f"{key} = {repr(value)}")
    return "\n".join(lines)


class TestXiteErrorHandling(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.test_dir)
        self.mock_file_path = Path(self.test_dir) / "mock_file.xit"
        with open(self.mock_file_path, "w") as f:  # dummy file targeted by mocks
            f.write("[ ] real task\n")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.test_dir)

    @patch("pathlib.Path.read_text", side_effect=PermissionError("permission denied"))
    def test_read_file_permission_error(self, mock_read_text):
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(self.mock_file_path)])

        stderr_output = stderr_capture.getvalue()
        # check warning from _read_file_content
        self.assertIn(
            f"warning: could not read file {self.mock_file_path}: permission denied",
            stderr_output,
        )
        # note: the outer loop's error is not triggered because _read_file_content
        # returns none after the warning, preventing further processing errors.
        self.assertEqual(output.strip(), "")

    @patch("xite.parse_todo_list", side_effect=Exception("generic parse failure"))
    def test_parse_error_during_load(self, mock_parse):
        """covers lines 304-305"""
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(self.mock_file_path)])

        stderr_output = stderr_capture.getvalue()
        # check warning from _parse_and_update_items
        self.assertIn(
            f"warning: could not parse content from {self.mock_file_path}: generic parse failure",
            stderr_output,
        )
        # note: the outer loop's error is not triggered because the inner function
        # catches the exception and prints a warning without re-raising.
        self.assertEqual(output.strip(), "")

    @patch("pathlib.Path.resolve", side_effect=OSError("mock resolve error"))
    def test_path_resolve_error(self, mock_resolve):
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(self.mock_file_path)])

        stderr_output = stderr_capture.getvalue()
        # check warning from _resolve_path and error from _collect_all_items
        self.assertIn(
            f"warning: could not resolve path '{self.mock_file_path}': mock resolve error",
            stderr_output,
        )
        self.assertIn(
            f"error: input path could not be processed: {self.mock_file_path}",
            stderr_output,
        )
        self.assertEqual(output.strip(), "")

    @patch("os.scandir", side_effect=OSError("mock scandir error"))
    def test_scandir_error_warning(self, mock_scandir):
        scan_dir = os.path.join(self.test_dir, "scan_me")
        os.makedirs(scan_dir)
        scan_toml_path = os.path.join(self.test_dir, "scan_error.toml")
        with open(scan_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["./scan_me"]
search_depth = 1
            """)

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([scan_toml_path])

        stderr_output = stderr_capture.getvalue()
        # check warning from os.walk's onerror callback (_print_warning)
        self.assertIn("warning: mock scandir error", stderr_output)
        # ensure the specific directory path is not part of the default onerror message
        self.assertNotIn(f"scanning directory {scan_dir}", stderr_output)

    @patch("pathlib.Path.read_text", side_effect=Exception("generic read error"))
    def test_read_file_generic_exception(self, mock_read_text):
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(self.mock_file_path)])

        stderr_output = stderr_capture.getvalue()
        # check warning from _read_file_content
        self.assertIn(
            f"warning: could not read file {self.mock_file_path}: generic read error",
            stderr_output,
        )
        # note: the outer loop's error is not triggered because _read_file_content
        # returns none after the warning, preventing further processing errors.
        self.assertEqual(output.strip(), "")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("tomllib.load", side_effect=tomllib.TOMLDecodeError("bad toml format"))
    def test_toml_decode_error(self, mock_toml_load):
        bad_toml_path = Path(self.test_dir) / "bad_format.toml"
        bad_toml_path.touch()  # create file so path.resolve works before open fails

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(bad_toml_path)])

        stderr_output = stderr_capture.getvalue()
        self.assertIn("error: parsing toml file", stderr_output)
        self.assertIn("bad toml format", stderr_output)
        self.assertNotIn("---", output)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch(
        "pathlib.Path.open", side_effect=PermissionError("toml open permission denied")
    )
    def test_toml_open_permission_error(self, mock_open):
        perm_toml_path = Path(self.test_dir) / "perm_error.toml"
        perm_toml_path.touch()  # create file so path.resolve works

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(perm_toml_path)])

        stderr_output = stderr_capture.getvalue()
        self.assertIn("error: reading toml file", stderr_output)
        self.assertIn("toml open permission denied", stderr_output)
        self.assertNotIn("---", output)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("pathlib.Path.resolve", side_effect=OSError("toml search_dir resolve error"))
    def test_toml_search_dir_resolve_error(self, mock_resolve):
        # mock resolve globally; error should occur when resolving the toml path itself
        resolve_err_toml_path = Path(self.test_dir) / "resolve_err.toml"
        with open(resolve_err_toml_path, "w") as f:
            f.write("""
[META]
search_dirs = ["./will_fail_resolve"]
            """)

        stderr_capture = io.StringIO()
        # run main with the global resolve patch active
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(resolve_err_toml_path)])

        stderr_output = stderr_capture.getvalue()
        # the mocked resolve error is caught when resolving the toml path itself
        self.assertIn(
            f"warning: could not resolve path '{resolve_err_toml_path}': toml search_dir resolve error",
            stderr_output,
        )
        self.assertIn(
            f"error: input path could not be processed: {resolve_err_toml_path}",
            stderr_output,
        )

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("os.walk", side_effect=OSError("mock walk error"))
    def test_os_walk_error_warning(self, mock_walk):
        walk_err_dir = Path(self.test_dir) / "walk_err_dir"
        walk_err_dir.mkdir()
        walk_err_toml_path = Path(self.test_dir) / "walk_err.toml"
        with open(walk_err_toml_path, "w") as f:
            f.write(f"""
[META]
search_dirs = ["{walk_err_dir.name}"]
search_depth = -1 # trigger os.walk
            """)

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(walk_err_toml_path)])

        stderr_output = stderr_capture.getvalue()
        # this covers the `except exception as e:` block in _process_toml_file's search_dir loop
        # note the change from 'in' to 'from' in the warning message format
        self.assertIn(
            f"warning: processing search_dir '{walk_err_dir.name}' from {walk_err_toml_path}: mock walk error",
            stderr_output,
        )

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("os.walk")
    def test_os_walk_onerror_callback(self, mock_walk):
        walk_onerror_dir = Path(self.test_dir) / "walk_onerror_dir"
        subdir = walk_onerror_dir / "subdir"
        subdir.mkdir(parents=True)
        walk_onerror_toml_path = Path(self.test_dir) / "walk_onerror.toml"
        with open(walk_onerror_toml_path, "w") as f:
            f.write(f"""
[META]
search_dirs = ["{walk_onerror_dir.name}"]
search_depth = -1 # trigger os.walk
            """)

        # configure mock_walk to simulate calling the onerror handler
        mock_exception = OSError("mock walk permission error")

        # simulate walk yielding the top dir, then raising error for the next level
        def walk_generator_with_error(*args, **kwargs):
            yield (str(walk_onerror_dir), ["subdir"], [])
            raise mock_exception

        mock_walk.side_effect = walk_generator_with_error

        # run main; the error during walk should be caught by the loop in _process_toml_file
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(walk_onerror_toml_path)])

        stderr_output = stderr_capture.getvalue()
        # assert that the warning from the `except exception as e:` block is printed
        # note the change from 'in' to 'from' in the warning message format
        self.assertIn(
            f"warning: processing search_dir '{walk_onerror_dir.name}' from {walk_onerror_toml_path}: {mock_exception}",
            stderr_output,
        )
        # ensure no output was generated as the walk failed
        self.assertEqual(output.strip(), "")

    def test_collect_items_non_existent_file(self):
        non_existent_path = Path(self.test_dir) / "non_existent.xit"
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(non_existent_path)])

        stderr_output = stderr_capture.getvalue()
        self.assertIn(
            f"error: input path not found: {non_existent_path}", stderr_output
        )
        self.assertEqual(output.strip(), "")

    @patch("pathlib.Path.is_file", return_value=False)
    @patch("pathlib.Path.is_dir", return_value=False)
    def test_collect_items_not_file_or_dir(self, mock_is_dir, mock_is_file):
        # use an existing file path, but mock checks to return false
        weird_path = self.mock_file_path
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(weird_path)])

        stderr_output = stderr_capture.getvalue()
        self.assertIn(
            f"warning: input path is not a file or directory: {weird_path}",
            stderr_output,
        )
        self.assertEqual(output.strip(), "")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("xite._process_toml_file", side_effect=Exception("mock processing error"))
    def test_collect_items_processing_exception(self, mock_process_toml):
        # create a dummy toml file to trigger the mocked function
        process_err_toml = Path(self.test_dir) / "process_err.toml"
        process_err_toml.touch()

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(process_err_toml)])

        stderr_output = stderr_capture.getvalue()
        self.assertIn(
            f"error: processing path {process_err_toml}: mock processing error",
            stderr_output,
        )
        self.assertEqual(output.strip(), "")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("xite._collect_all_items", side_effect=Exception("mock collection error"))
    def test_main_collect_exception(self, mock_collect):
        # use any valid file path; the mock will prevent processing
        dummy_path = self.mock_file_path
        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", str(dummy_path)]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(stderr_capture):
                    # expect the original mocked exception to propagate from main
                    with self.assertRaisesRegex(Exception, "mock collection error"):
                        main()

        # stderr should be empty as the exception is caught by assertraisesregex
        stderr_output = stderr_capture.getvalue()
        self.assertEqual(stderr_output, "")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch(
        "xite._process_and_format_items",
        side_effect=Exception("mock processing format error"),
    )
    def test_main_process_format_exception(self, mock_process):
        # use any valid file path
        dummy_path = self.mock_file_path
        stderr_capture = io.StringIO()
        with patch("sys.argv", ["xite.py", str(dummy_path)]):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(stderr_capture):
                    # expect the original mocked exception to propagate from main
                    with self.assertRaisesRegex(
                        Exception, "mock processing format error"
                    ):
                        main()

        # stderr should be empty as the exception is caught by assertraisesregex
        stderr_output = stderr_capture.getvalue()
        self.assertEqual(stderr_output, "")

    # --- tests for specific uncovered lines / edge cases ---

    # <<< Test methods below were confirmed moved to TestXiteIntegration and deleted from here >>>

    # test_list_projects_with_project_filter was moved to TestXiteIntegration

    # test_list_projects_filter_interactions is defined below, the duplicate above was removed.

    def test_list_projects_filter_interactions(self):
        """test --list-projects with --project, --include-deprecated, --max-projects."""
        # Setup a specific toml for this test
        interaction_toml_path = os.path.join(self.test_dir, "interaction.toml")
        with open(interaction_toml_path, "w") as f:
            f.write("""
[META]
active = ["ProjC", "ProjA"] # A is deprecated

[ProjA]
deprecated = true
tasks = "[ ] a"

[ProjB]
tasks = "[ ] b"

[ProjC]
tasks = "[ ] c"

[ProjD] # Deprecated, not in active
deprecated = true
tasks = "[ ] d"
            """)

        # Case 1: Filter includes deprecated, but --include-deprecated is OFF
        output1 = run_main_and_capture(
            [
                interaction_toml_path,
                "--list-projects",
                "--project",
                "ProjA",
                "--project",
                "ProjC",
            ]
        )
        # ProjA is filtered by --project but skipped due to deprecation
        self.assertEqual(output1.strip().splitlines(), ["ProjC"])

        # Case 2: Filter includes deprecated, and --include-deprecated is ON
        output2 = run_main_and_capture(
            [
                interaction_toml_path,
                "--list-projects",
                "--project",
                "ProjA",
                "--project",
                "ProjC",
                "--include-deprecated",
            ]
        )
        # ProjA is now included. Order based on META.active: ProjC, ProjA
        self.assertEqual(output2.strip().splitlines(), ["ProjC", "ProjA"])

        # Case 3: Filter, include deprecated, and max-projects limit
        output3 = run_main_and_capture(
            [
                interaction_toml_path,
                "--list-projects",
                "--project",
                "ProjA",
                "--project",
                "ProjC",
                "--project",
                "ProjB",
                "--include-deprecated",
                "--max-projects",
                "2",
            ]
        )
        # Filter selects A, B, C. Include deprecated keeps A.
        # Order based on META.active + collected: ProjC, ProjA, ProjB
        # Max-projects=2 limits to the first two: ProjC, ProjA
        self.assertEqual(output3.strip().splitlines(), ["ProjC", "ProjA"])

        # Case 4: Filter selects only deprecated, include deprecated is ON
        output4 = run_main_and_capture(
            [
                interaction_toml_path,
                "--list-projects",
                "--project",
                "ProjA",
                "--project",
                "ProjD",
                "--include-deprecated",
            ]
        )
        # Order based on META.active + collected: ProjA, ProjD
        self.assertEqual(output4.strip().splitlines(), ["ProjA", "ProjD"])

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    def test_toml_file_not_found_error_in_collect(self):
        non_existent_toml = Path(self.test_dir) / "non_existent.toml"
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(non_existent_toml)])

        stderr_output = stderr_capture.getvalue()
        # error comes from the initial check in _collect_all_items
        self.assertIn(
            f"error: input path not found: {non_existent_toml}", stderr_output
        )
        # ensure no task output was generated
        self.assertNotIn("---", output)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    def test_toml_invalid_meta_values_specific_warnings(self):
        # test invalid search_depth values
        for invalid_depth in [0, -2, "abc", 1.5]:
            toml_content = create_toml_str(META={"search_depth": invalid_depth})
            toml_path = Path(self.test_dir) / f"invalid_depth_{invalid_depth}.toml"
            toml_path.write_text(toml_content)
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):
                run_main_and_capture([str(toml_path)])
            self.assertIn(
                f"warning: invalid META.search_depth in {toml_path}: {invalid_depth}",
                stderr_capture.getvalue(),
            )

        # test invalid active (not list)
        toml_content = create_toml_str(META={"active": "not_a_list"})
        toml_path = Path(self.test_dir) / "invalid_active.toml"
        toml_path.write_text(toml_content)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            run_main_and_capture([str(toml_path)])
        self.assertIn(
            f"warning: invalid 'active' list in META of {toml_path}",  # covers line 540
            stderr_capture.getvalue(),
        )

        # test invalid roadmap_files (not list, contains non-string)
        for invalid_roadmap in ["not_a_list", ["valid.md", 123]]:
            toml_content = create_toml_str(META={"roadmap_files": invalid_roadmap})
            toml_path = (
                Path(self.test_dir)
                / f"invalid_roadmap_{type(invalid_roadmap).__name__}.toml"
            )
            toml_path.write_text(toml_content)
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):
                run_main_and_capture([str(toml_path)])
            self.assertIn(
                f"warning: invalid META.roadmap_files in {toml_path}, using defaults",
                stderr_capture.getvalue(),
            )

        # test invalid search_dirs (not list)
        toml_content = create_toml_str(META={"search_dirs": "not_a_list"})
        toml_path = Path(self.test_dir) / "invalid_searchdirs.toml"
        toml_path.write_text(toml_content)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            run_main_and_capture([str(toml_path)])
        # this case now correctly produces a warning
        # covers lines 551-552
        self.assertIn(
            f"warning: invalid search_dirs entry in {toml_path}. expected list, got str.",
            stderr_capture.getvalue(),
        )

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    def test_toml_invalid_project_values_specific_warnings(self):
        # test invalid include_files values
        for invalid_include in [123, ["valid.xit", 456]]:
            toml_content = create_toml_str(MyProject={"include_files": invalid_include})
            toml_path = (
                Path(self.test_dir)
                / f"invalid_include_{type(invalid_include).__name__}.toml"
            )
            toml_path.write_text(toml_content)
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):
                run_main_and_capture([str(toml_path)])
            if isinstance(invalid_include, list):
                self.assertIn(
                    f"warning: non-string path in include_files for MyProject in {toml_path}: 456",
                    stderr_capture.getvalue(),
                )
            else:
                self.assertIn(
                    f"warning: invalid include_files entry for MyProject in {toml_path}. expected list, got int.",
                    stderr_capture.getvalue(),
                )

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch("pathlib.Path.resolve", side_effect=OSError("mock repo resolve error"))
    def test_toml_repo_resolve_error(self, mock_resolve):
        toml_content = create_toml_str(MyProject={"repo": {"public": "./repo_dir"}})
        toml_path = Path(self.test_dir) / "repo_resolve_err.toml"
        toml_path.write_text(toml_content)

        stderr_capture = io.StringIO()
        # need to ensure resolve fails *after* the toml is loaded
        # patch resolve globally; it will be hit during repo processing
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(toml_path)])

        stderr_output = stderr_capture.getvalue()
        # the global patch causes the initial resolve of the toml path itself in _collect_all_items to fail first
        self.assertIn(
            f"warning: could not resolve path '{toml_path}': mock repo resolve error",
            stderr_output,
        )
        self.assertIn(
            f"error: input path could not be processed: {toml_path}", stderr_output
        )
        self.assertEqual(output.strip(), "")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
    @patch(
        "xite._discover_projects_in_search_dir",
        side_effect=Exception("mock discover error"),  # Use generic Exception
    )
    def test_toml_search_dir_processing_exception_warning(self, mock_discover):
        """covers lines 555-556"""
        toml_content = create_toml_str(META={"search_dirs": ["./some_dir"]})
        toml_path = Path(self.test_dir) / "search_dir_proc_err.toml"
        toml_path.write_text(toml_content)
        (Path(self.test_dir) / "some_dir").mkdir()  # create dir so initial check passes

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            output = run_main_and_capture([str(toml_path)])

        stderr_output = stderr_capture.getvalue()
        # the error is caught inside the loop in _process_toml_file and reported as a warning
        self.assertIn(
            f"warning: processing search_dir './some_dir' from {toml_path}: mock discover error",
            stderr_output,
        )
        self.assertEqual(output.strip(), "")  # no output expected as discovery failed

    def test_list_projects_deduplication(self):
        """test --list-projects doesn't show duplicates from discovery and toml repo."""
        # create a scenario where a project is defined via repo and also discoverable
        repo_dir = os.path.join(self.test_dir, "dedup_repo")
        os.makedirs(repo_dir)
        with open(os.path.join(repo_dir, "ROADMAP.md"), "w") as f:
            f.write("[ ] task from dedup_repo\n")

        dedup_toml_path = os.path.join(self.test_dir, "dedup.toml")
        with open(dedup_toml_path, "w") as f:
            f.write(f"""
[META]
search_dirs = ["."] # discover dedup_repo
search_depth = 1

[DedupProject]
repo = {{ public = "./dedup_repo" }} # also define via repo
            """)

        output = run_main_and_capture([dedup_toml_path, "--list-projects"])
        listed_projects = output.strip().splitlines()

        # expected order: DedupProject (from toml), then dedup_repo (discovered)
        # the refactor should ensure 'dedup_repo' is listed only once,
        # likely under the name 'dedup_repo' as discovery happens after toml processing.
        # let's verify the exact output based on implementation details.
        # current implementation: toml projects first, then discovered.
        # 'DedupProject' processes its repo, adding 'dedup_repo' to processed_paths.
        # discovery then finds 'dedup_repo' dir, but its roadmap is already processed.
        # final order should only contain DedupProject.
        self.assertEqual(listed_projects, ["DedupProject"])

        # check task output for comparison (should only show DedupProject)
        output_tasks = run_main_and_capture([dedup_toml_path])
        self.assertIn("--- DedupProject ---", output_tasks)
        self.assertIn("[ ] task from dedup_repo", output_tasks)
        self.assertNotIn(
            "--- dedup_repo ---", output_tasks
        )  # should not appear as separate source

    @patch.dict(
        os.environ, {"HOME": str(Path.home())}, clear=True
    )  # ensure home is set for expanduser
    def test_tilde_expansion_cli_args(self):
        """test that ~ is expanded correctly for cli file/dir arguments."""
        # create a structure inside a subdir named like the user's home dir basename
        # to simulate a path like ~/test_dir/file.xit
        home_dir_name = Path.home().name
        simulated_home_sub = Path(self.test_dir) / home_dir_name
        simulated_home_sub.mkdir()

        # create a test file and dir inside the simulated home subdir
        tilde_file_path = simulated_home_sub / "tilde_test.xit"
        tilde_dir_path = simulated_home_sub / "tilde_dir"
        tilde_dir_roadmap_path = tilde_dir_path / "ROADMAP.md"
        tilde_dir_path.mkdir()

        with open(tilde_file_path, "w") as f:
            f.write("[ ] task from tilde file\n")
        with open(tilde_dir_roadmap_path, "w") as f:
            f.write("[ ] task from tilde dir roadmap\n")

        # construct cli args using the *resolved* paths, simulating shell expansion
        cli_arg_file = str(tilde_file_path.resolve())
        cli_arg_dir = str(tilde_dir_path.resolve())

        # test running with both resolved paths
        output = run_main_and_capture([cli_arg_file, cli_arg_dir])

        # check output contains tasks from both expanded paths
        self.assertIn(f"--- {tilde_file_path} ---", output)  # header uses resolved path
        self.assertIn("[ ] task from tilde file", output)
        self.assertIn(f"--- {tilde_dir_path.name} ---", output)  # header uses dir name
        self.assertIn("[ ] task from tilde dir roadmap", output)

    @patch(
        "pathlib.Path.home", return_value=Path(".")
    )  # Mock home to be current (test) dir
    def test_tilde_expansion_toml_repo_path(self, mock_home):
        """test that ~ is expanded correctly for repo paths in toml."""
        # create a repo dir directly in the test dir (which is mocked as home)
        repo_in_home = Path(self.test_dir) / "my_repo_in_home"
        repo_in_home.mkdir(parents=True)
        with open(repo_in_home / "ROADMAP.md", "w") as f:
            f.write("[ ] task from repo in home\n")

        # create a toml file referencing the repo using ~/
        toml_path = Path(self.test_dir) / "tilde_repo.toml"
        # use ~ directly, it should expand to the mocked home (test_dir)
        repo_path_str = "~/my_repo_in_home"
        with open(toml_path, "w") as f:
            f.write(f"""
[MyHomeRepoProject]
repo = {{ public = "{repo_path_str}" }}
tasks = "[ ] inline task"
            """)

        # run xite with the toml file
        output = run_main_and_capture([str(toml_path)])

        # check that tasks from both inline and the tilde-expanded repo are present
        self.assertIn("--- MyHomeRepoProject ---", output)
        self.assertIn("[ ] inline task", output)
        self.assertIn("[ ] task from repo in home", output)

    @patch(
        "pathlib.Path.home", return_value=Path(".")
    )  # Mock home to be current (test) dir
    def test_tilde_expansion_toml_search_dirs(self, mock_home):
        """test that ~ and / are expanded correctly for search_dirs in toml."""
        # create simulated dirs directly in test dir (mocked home) and an absolute path dir
        search_dir_in_home = Path(self.test_dir) / "search_dir_home"
        search_dir_in_home.mkdir(parents=True)
        with open(search_dir_in_home / "ROADMAP.md", "w") as f:
            f.write("[ ] task from search_dir in home\n")

        # absolute path dir remains the same logic
        abs_search_dir = Path(self.test_dir) / "abs_search_dir"
        abs_search_dir.mkdir()
        with open(abs_search_dir / "ROADMAP.md", "w") as f:
            f.write("[ ] task from absolute search_dir\n")

        rel_search_dir = Path(self.test_dir) / "rel_search_dir"
        rel_search_dir.mkdir()
        with open(rel_search_dir / "ROADMAP.md", "w") as f:
            f.write("[ ] task from relative search_dir\n")

        # create a toml file referencing these dirs
        toml_path = Path(self.test_dir) / "tilde_search.toml"
        search_path_home_str = (
            "~/search_dir_home"  # path using tilde relative to mocked home
        )
        search_path_abs_str = str(abs_search_dir.resolve())  # absolute path
        search_path_rel_str = "./rel_search_dir"  # relative path

        with open(toml_path, "w") as f:
            f.write(f"""
[META]
search_dirs = [
    "{search_path_home_str}",
    "{search_path_abs_str}",
    "{search_path_rel_str}"
]
search_depth = 1 # only look inside the specified dirs
            """)

        # run xite with the toml file
        output = run_main_and_capture([str(toml_path)])

        # check that tasks from all search_dirs are found
        self.assertIn("--- search_dir_home ---", output)
        self.assertIn("[ ] task from search_dir in home", output)
        self.assertIn("--- abs_search_dir ---", output)
        self.assertIn("[ ] task from absolute search_dir", output)
        self.assertIn("--- rel_search_dir ---", output)
        self.assertIn("[ ] task from relative search_dir", output)

    # <<< The duplicate test_list_projects_filter_interactions method was here and has been removed >>>
    # The correct version exists in TestXiteIntegration.

    def test_list_projects_filter_active_project(self):
        """test --list-projects filters projects listed in META.active (covers line 265)."""
        filter_active_toml = Path(self.test_dir) / "filter_active.toml"
        with open(filter_active_toml, "w") as f:
            f.write("""
[META]
active = ["ProjA", "ProjB"]

[ProjA]
tasks = "[ ] a"

[ProjB]
tasks = "[ ] b"
            """)
        # Filter *out* ProjA, keep ProjB
        output = run_main_and_capture(
            [str(filter_active_toml), "--list-projects", "--project", "ProjB"]
        )
        self.assertEqual(output.strip().splitlines(), ["ProjB"])

    def test_list_projects_filter_non_active_project(self):
        """test --list-projects filters projects *not* listed in META.active (covers line 271)."""
        filter_nonactive_toml = Path(self.test_dir) / "filter_nonactive.toml"
        with open(filter_nonactive_toml, "w") as f:
            f.write("""
# No META.active
[ProjC]
tasks = "[ ] c"

[ProjD]
tasks = "[ ] d"
            """)
        # Filter *out* ProjD, keep ProjC
        output = run_main_and_capture(
            [str(filter_nonactive_toml), "--list-projects", "--project", "ProjC"]
        )
        self.assertEqual(output.strip().splitlines(), ["ProjC"])

    def test_discover_nested_project_pruning(self):
        """test project discovery pruning for nested projects (covers line 397)."""
        base_dir = Path(self.test_dir) / "basedir"
        outer_proj = base_dir / "outer_proj"
        inner_proj = outer_proj / "inner_proj"
        inner_proj.mkdir(parents=True)

        with open(outer_proj / "ROADMAP.md", "w") as f:
            f.write("[ ] task outer\n")
        with open(inner_proj / "ROADMAP.md", "w") as f:
            f.write("[ ] task inner\n")

        prune_toml = Path(self.test_dir) / "prune_nested.toml"
        with open(prune_toml, "w") as f:
            f.write("""
[META]
search_dirs = ["./basedir"]
search_depth = -1 # infinite depth needed to reach inner_proj potential
            """)

        output = run_main_and_capture([str(prune_toml)])
        # Expect only outer_proj, as inner_proj should be pruned because outer_proj
        # was found during the walk and is *not* the starting search_dir (basedir).
        self.assertIn("--- outer_proj ---", output)
        self.assertIn("[ ] task outer", output)
        self.assertNotIn("--- inner_proj ---", output)
        self.assertNotIn("[ ] task inner", output)

    # --- End of TestXiteIntegration ---

    # --- Start of TestXiteErrorHandling ---
    # (Ensure test_list_projects_with_project_filter is NOT defined here)

    # The duplicate test_list_projects_with_project_filter method definition
    # that was previously here has been removed.

    # Note: The test 'test_list_projects_filter_interactions' was moved from
    # TestXiteErrorHandling to TestXiteIntegration as it tests core functionality,
    # not just error handling.


class TestXiteEditing(unittest.TestCase):
    """end-to-end tests for command-line task editing (--add, --set-status)."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.test_dir)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.test_dir)

    def run_xite(self, args_list):
        return run_main_and_capture_all(args_list)

    def write_file(self, name, content):
        path = os.path.join(self.test_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def read_file(self, name):
        with open(os.path.join(self.test_dir, name)) as f:
            return f.read()

    # --- --add ---

    def test_add_appends_after_last_task(self):
        self.write_file("todos.xit", "[ ] first task\n[x] second task\n")
        out, _, code = self.run_xite(["--add", "new task", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertIn("[ ] new task", out)
        self.assertEqual(
            self.read_file("todos.xit"),
            "[ ] first task\n[x] second task\n[ ] new task\n",
        )

    def test_add_with_status_and_tags(self):
        self.write_file("todos.xit", "[ ] first task\n")
        out, _, code = self.run_xite(
            ["--add", "ship it", "--set-status", "blocked", "--tag", "v1", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("[!] ship it #v1", out)
        self.assertIn("[!] ship it #v1\n", self.read_file("todos.xit"))

    def test_add_creates_missing_file(self):
        _, _, code = self.run_xite(["--add", "fresh idea", "brand.xit"])
        self.assertEqual(code, 0)
        self.assertEqual(self.read_file("brand.xit"), "[ ] fresh idea\n")

    def test_add_to_file_without_tasks(self):
        self.write_file("notes.md", "# notes\n\nnothing here yet\n")
        _, _, code = self.run_xite(["--add", "do it", "notes.md"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("notes.md"),
            "# notes\n\nnothing here yet\n[ ] do it\n",
        )

    def test_add_inserts_after_continuation_lines(self):
        self.write_file(
            "todos.xit",
            "[ ] first task\n    continued thought\n[x] done task\n        more detail\n",
        )
        _, _, code = self.run_xite(["--add", "another", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("todos.xit"),
            "[ ] first task\n    continued thought\n"
            "[x] done task\n        more detail\n[ ] another\n",
        )

    def test_add_into_fenced_block_keeps_fence_closed(self):
        self.write_file(
            "README-like.md", "intro\n\n```\n[ ] inside fence\n```\n\nfooter\n"
        )
        _, _, code = self.run_xite(["--add", "more inside", "README-like.md"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("README-like.md"),
            "intro\n\n```\n[ ] inside fence\n[ ] more inside\n```\n\nfooter\n",
        )

    def test_add_rejects_multiple_files(self):
        self.write_file("a.xit", "[ ] a\n")
        self.write_file("b.xit", "[ ] b\n")
        _, err, code = self.run_xite(["--add", "x", "a.xit", "b.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("a.xit"), "[ ] a\n")

    def test_add_rejects_toml_file(self):
        self.write_file("tracker.toml", "[META]\n")
        _, err, code = self.run_xite(["--add", "x", "tracker.toml"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("tracker.toml"), "[META]\n")

    def test_add_rejects_match_and_status_filter(self):
        self.write_file("todos.xit", "[ ] a\n")
        _, err, code = self.run_xite(
            ["--add", "x", "--match", "a", "--status", "new", "todos.xit"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("todos.xit"), "[ ] a\n")

    # --- --set-status ---

    def test_set_status_by_match_preserves_formatting(self):
        self.write_file(
            "todos.xit",
            "[ ] fix bug #triage\n- [ ] markdown task\n    [@] nested active\n",
        )
        out, _, code = self.run_xite(
            ["--set-status", "active", "--match", "fix bug", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("[@] fix bug #triage", out)
        self.assertEqual(
            self.read_file("todos.xit"),
            "[@] fix bug #triage\n- [ ] markdown task\n    [@] nested active\n",
        )

    def test_set_status_match_is_case_insensitive_substring(self):
        self.write_file("todos.xit", "[ ] Fix the FLUb flask\n[ ] other\n")
        _, _, code = self.run_xite(["--set-status", "complete", "--match", "flub", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("todos.xit"), "[x] Fix the FLUb flask\n[ ] other\n"
        )

    def test_set_status_preserves_markdown_marker_and_spacing(self):
        self.write_file("todos.xit", "  - [ ] indented dash task\n")
        _, _, code = self.run_xite(
            ["--set-status", "complete", "--match", "dash", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.read_file("todos.xit"), "  - [x] indented dash task\n")

    def test_set_status_by_tag(self):
        self.write_file(
            "todos.xit", "[ ] one #v1\n[x] two #v1\n[ ] three #v2\n"
        )
        _, _, code = self.run_xite(
            ["--set-status", "deferred", "--tag", "v1", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("todos.xit"),
            "[>] one #v1\n[>] two #v1\n[ ] three #v2\n",
        )

    def test_set_status_by_current_status(self):
        self.write_file("todos.xit", "[ ] one\n[ ] two\n[x] three\n")
        out, _, code = self.run_xite(
            ["--set-status", "active", "--status", "new", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.count("[@]"), 2)
        self.assertEqual(
            self.read_file("todos.xit"), "[@] one\n[@] two\n[x] three\n"
        )

    def test_set_status_requires_a_selector(self):
        self.write_file("todos.xit", "[ ] a\n")
        _, err, code = self.run_xite(["--set-status", "complete", "todos.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("todos.xit"), "[ ] a\n")

    def test_set_status_no_match_leaves_file_unchanged(self):
        self.write_file("todos.xit", "[ ] a\n")
        _, err, code = self.run_xite(["--set-status", "complete", "--match", "zzz", "todos.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("todos.xit"), "[ ] a\n")

    def test_set_status_requires_existing_file(self):
        _, err, code = self.run_xite(["--set-status", "complete", "--match", "x", "nope.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)

    def test_set_status_rejects_multiple_files(self):
        self.write_file("a.xit", "[ ] a\n")
        self.write_file("b.xit", "[ ] a\n")
        _, err, code = self.run_xite(
            ["--set-status", "complete", "--match", "a", "a.xit", "b.xit"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("a.xit"), "[ ] a\n")
        self.assertEqual(self.read_file("b.xit"), "[ ] a\n")

    def test_set_status_rejects_toml_file(self):
        self.write_file("tracker.toml", '[p]\ntasks = """\n[ ] x\n"""\n')
        _, err, code = self.run_xite(
            ["--set-status", "complete", "--match", "x", "tracker.toml"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)

    # --- --match as a read-mode filter ---

    def test_match_filters_listed_tasks(self):
        self.write_file(
            "todos.xit",
            "[ ] write the docs\n[x] ship v1\n    [x] polish docs formatting\n[ ] unrelated\n",
        )
        out = run_main_and_capture(["--match", "docs", "todos.xit"])
        self.assertIn("write the docs", out)
        self.assertIn("polish docs formatting", out)
        # "ship v1" is kept only as context for its matching child; "unrelated" is gone
        self.assertNotIn("unrelated", out)

    def test_match_is_case_insensitive(self):
        self.write_file("todos.xit", "[ ] Write The DOCS\n[ ] other\n")
        out = run_main_and_capture(["--match", "the docs", "todos.xit"])
        self.assertIn("Write The DOCS", out)
        self.assertNotIn("other", out)

    def test_multiple_matches_are_ored(self):
        self.write_file("todos.xit", "[ ] alpha\n[ ] beta\n[ ] gamma\n")
        out = run_main_and_capture(["--match", "alpha", "--match", "beta", "todos.xit"])
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("gamma", out)

    def test_match_combined_with_status_is_anded(self):
        self.write_file("todos.xit", "[ ] docs one\n[x] docs two\n[ ] other\n")
        out = run_main_and_capture(["--match", "docs", "--status", "new", "todos.xit"])
        self.assertIn("docs one", out)
        self.assertNotIn("docs two", out)
        self.assertNotIn("other", out)

    def test_match_matches_continuation_lines(self):
        self.write_file("todos.xit", "[ ] parent task\n    extended with flurb\n[ ] nope\n")
        out = run_main_and_capture(["--match", "flurb", "todos.xit"])
        self.assertIn("parent task", out)
        self.assertNotIn("nope", out)

    def test_match_with_no_results_prints_nothing(self):
        self.write_file("todos.xit", "[ ] a\n")
        out = run_main_and_capture(["--match", "dfkljdflkjd", "todos.xit"])
        self.assertEqual(out.strip(), "")

    def test_added_task_is_readable_by_filters(self):
        self.write_file("todos.xit", "[ ] a\n")
        self.run_xite(["--add", "shiny new", "--set-status", "blocked", "todos.xit"])
        out = run_main_and_capture(["--status", "blocked", "todos.xit"])
        self.assertIn("[!] shiny new", out)


class TestXiteStableIds(unittest.TestCase):
    """end-to-end tests for --show-ids stable task identifiers."""

    ID_LINE_RE = re.compile(r"( *)([0-9a-f]{8}) (\[.\] .*)")

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.test_dir)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.test_dir)

    def write_file(self, name, content):
        path = os.path.join(self.test_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def parse_id_lines(self, output):
        """extract (indent, id, rest) tuples from id-prefixed output lines."""
        entries = []
        for line in output.splitlines():
            m = self.ID_LINE_RE.search(line)
            if m:
                entries.append((m.group(1), m.group(2), m.group(3)))
        return entries

    def test_show_ids_prints_hex_id_per_task(self):
        self.write_file("todos.xit", "[ ] alpha task\n[x] beta task\n")
        out = run_main_and_capture(["--show-ids", "todos.xit"])
        entries = self.parse_id_lines(out)
        self.assertEqual(len(entries), 2)
        for _, task_id, _ in entries:
            self.assertEqual(len(task_id), 8)
        self.assertIn("alpha task", entries[0][2])
        self.assertIn("beta task", entries[1][2])

    def test_ids_omitted_without_flag(self):
        self.write_file("todos.xit", "[ ] alpha task\n")
        out = run_main_and_capture(["todos.xit"])
        self.assertEqual(self.parse_id_lines(out), [])
        self.assertIn("[ ] alpha task", out)

    def test_ids_are_stable_across_runs(self):
        self.write_file("todos.xit", "[ ] alpha task\n[x] beta task\n")
        first = run_main_and_capture(["--show-ids", "todos.xit"])
        second = run_main_and_capture(["--show-ids", "todos.xit"])
        self.assertEqual(first, second)
        entries = self.parse_id_lines(first)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries, self.parse_id_lines(second))

    def test_ids_match_between_full_and_filtered_output(self):
        self.write_file("todos.xit", "[ ] alpha task\n[x] beta task\n")
        full = run_main_and_capture(["--show-ids", "todos.xit"])
        filtered = run_main_and_capture(
            ["--show-ids", "--status", "complete", "todos.xit"]
        )
        full_entries = dict(
            (rest, task_id) for _, task_id, rest in self.parse_id_lines(full)
        )
        filtered_entries = dict(
            (rest, task_id) for _, task_id, rest in self.parse_id_lines(filtered)
        )
        self.assertEqual(len(filtered_entries), 1)
        self.assertEqual(filtered_entries, {k: v for k, v in full_entries.items() if "beta" in k})

    def test_editing_task_text_changes_only_that_id(self):
        self.write_file("todos.xit", "[ ] alpha task\n[x] beta task\n")
        before = dict(
            (rest, task_id)
            for _, task_id, rest in self.parse_id_lines(
                run_main_and_capture(["--show-ids", "todos.xit"])
            )
        )
        self.write_file("todos.xit", "[ ] alpha task edited\n[x] beta task\n")
        after = dict(
            (rest, task_id)
            for _, task_id, rest in self.parse_id_lines(
                run_main_and_capture(["--show-ids", "todos.xit"])
            )
        )
        alpha_before = next(tid for rest, tid in before.items() if "alpha" in rest)
        alpha_after = next(tid for rest, tid in after.items() if "alpha" in rest)
        beta_before = next(tid for rest, tid in before.items() if "beta" in rest)
        beta_after = next(tid for rest, tid in after.items() if "beta" in rest)
        self.assertNotEqual(alpha_before, alpha_after)
        self.assertEqual(beta_before, beta_after)

    def test_duplicate_tasks_get_distinct_ids(self):
        self.write_file("todos.xit", "[ ] same text\n[ ] same text\n")
        entries = self.parse_id_lines(run_main_and_capture(["--show-ids", "todos.xit"]))
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0][1], entries[1][1])

    def test_children_get_ids_at_their_indent(self):
        self.write_file(
            "todos.xit", "[ ] parent task\n    [ ] child task\n        [ ] grandchild\n"
        )
        entries = self.parse_id_lines(run_main_and_capture(["--show-ids", "todos.xit"]))
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0][0], "")
        self.assertEqual(entries[1][0], "    ")
        self.assertEqual(entries[2][0], "        ")

    def test_ids_differ_between_projects(self):
        os.makedirs(os.path.join(self.test_dir, "proj_a"))
        os.makedirs(os.path.join(self.test_dir, "proj_b"))
        self.write_file("proj_a/ROADMAP.md", "[ ] shared text\n")
        self.write_file("proj_b/ROADMAP.md", "[ ] shared text\n")
        entries = self.parse_id_lines(
            run_main_and_capture(["--show-ids", self.test_dir])
        )
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0][1], entries[1][1])


class TestXiteIdSelectors(unittest.TestCase):
    """end-to-end tests for selecting and editing tasks by stable id."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.test_dir)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.test_dir)

    def run_xite(self, args_list):
        return run_main_and_capture_all(args_list)

    def write_file(self, name, content):
        path = os.path.join(self.test_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def read_file(self, name):
        with open(os.path.join(self.test_dir, name)) as f:
            return f.read()

    def id_of(self, filename, text):
        """returns the id printed for the first task containing text."""
        out = run_main_and_capture(["--show-ids", filename])
        for line in out.splitlines():
            m = re.search(r"([0-9a-f]{8}) (\[.\] .*)", line)
            if m and text in m.group(2):
                return m.group(1)
        self.fail(f"no id found for {text!r} in {out!r}")

    def test_match_by_full_id_lists_only_that_task(self):
        self.write_file("todos.xit", "[ ] alpha task\n[x] beta task\n")
        target = self.id_of("todos.xit", "alpha")
        out = run_main_and_capture(["--id", target, "todos.xit"])
        self.assertIn("alpha task", out)
        self.assertNotIn("beta task", out)

    def test_match_by_id_prefix(self):
        self.write_file("todos.xit", "[ ] alpha task\n[x] beta task\n")
        target = self.id_of("todos.xit", "alpha")
        out = run_main_and_capture(["--id", target[:4], "todos.xit"])
        self.assertIn("alpha task", out)
        self.assertNotIn("beta task", out)

    def test_unknown_id_lists_nothing(self):
        self.write_file("todos.xit", "[ ] alpha task\n")
        out = run_main_and_capture(["--id", "ffffffff", "todos.xit"])
        self.assertEqual(out.strip(), "")

    def test_id_filter_anded_with_status(self):
        self.write_file("todos.xit", "[ ] alpha task\n")
        target = self.id_of("todos.xit", "alpha")
        out = run_main_and_capture(["--id", target, "--status", "complete", "todos.xit"])
        self.assertEqual(out.strip(), "")
        out = run_main_and_capture(["--id", target, "--status", "new", "todos.xit"])
        self.assertIn("alpha task", out)

    def test_children_get_distinct_ids_across_projects(self):
        os.makedirs(os.path.join(self.test_dir, "proj_a"))
        os.makedirs(os.path.join(self.test_dir, "proj_b"))
        self.write_file("proj_a/ROADMAP.md", "[ ] parent\n    [ ] shared child\n")
        self.write_file("proj_b/ROADMAP.md", "[ ] parent\n    [ ] shared child\n")
        out = run_main_and_capture(["--show-ids", self.test_dir])
        child_ids = [
            m.group(1)
            for line in out.splitlines()
            if (m := re.search(r"([0-9a-f]{8}) \[.\] shared child", line))
        ]
        self.assertEqual(len(child_ids), 2)
        self.assertNotEqual(child_ids[0], child_ids[1])

    def test_set_status_by_id_edits_only_that_task(self):
        self.write_file("todos.xit", "[ ] alpha task\n[ ] beta task\n")
        target = self.id_of("todos.xit", "alpha")
        out, _, code = self.run_xite(["--set-status", "complete", "--id", target, "todos.xit"])
        self.assertEqual(code, 0)
        self.assertIn("[x] alpha task", out)
        self.assertEqual(self.read_file("todos.xit"), "[x] alpha task\n[ ] beta task\n")

    def test_set_status_by_id_prefix_edits_all_matches(self):
        self.write_file(
            "todos.xit", "[ ] alpha task\n[ ] alpha two\n[ ] beta task\n[x] delta\n"
        )
        ids = {
            name: self.id_of("todos.xit", name)
            for name in ("alpha task", "alpha two", "beta task", "delta")
        }
        first, second = ids["alpha task"], ids["alpha two"]
        prefix = os.path.commonprefix([first, second]) or first[:4]
        expected = {name for name, tid in ids.items() if tid.startswith(prefix)}
        self.assertIn("alpha task", expected)
        _, _, code = self.run_xite(["--set-status", "blocked", "--id", prefix, "todos.xit"])
        self.assertEqual(code, 0)
        content = self.read_file("todos.xit")
        original = {"alpha task": " ", "alpha two": " ", "beta task": " ", "delta": "x"}
        for name in ids:
            marker = "!" if name in expected else original[name]
            for line in content.splitlines():
                if name in line:
                    self.assertTrue(line.startswith(f"[{marker}]"), f"{name}: {line}")
                    break
            else:
                self.fail(f"{name} missing from {content!r}")

    def test_set_status_by_unknown_id_fails_unchanged(self):
        self.write_file("todos.xit", "[ ] alpha task\n")
        _, err, code = self.run_xite(
            ["--set-status", "complete", "--id", "ffffffff", "todos.xit"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("todos.xit"), "[ ] alpha task\n")

    def test_set_status_by_id_with_child_task(self):
        self.write_file("todos.xit", "[ ] parent task\n    [ ] child task\n")
        target = self.id_of("todos.xit", "child task")
        _, _, code = self.run_xite(
            ["--set-status", "active", "--id", target, "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("todos.xit"), "[ ] parent task\n    [@] child task\n"
        )

    def test_id_survives_status_edit_through_id(self):
        self.write_file("todos.xit", "[ ] alpha task\n")
        target = self.id_of("todos.xit", "alpha")
        _, _, code = self.run_xite(
            ["--set-status", "complete", "--id", target, "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.id_of("todos.xit", "alpha"), target)

    def test_add_rejects_id_selector(self):
        self.write_file("todos.xit", "[ ] alpha task\n")
        _, err, code = self.run_xite(["--add", "new", "--id", "abcd1234", "todos.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)
        self.assertEqual(self.read_file("todos.xit"), "[ ] alpha task\n")


class TestXiteStatusLists(unittest.TestCase):
    """end-to-end tests for comma separated --status selections."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.test_dir)
        self.write_file(
            "todos.xit",
            "[ ] alpha task #work\n"
            "[@] beta task #work\n"
            "[x] gamma task #work\n"
            "[!] delta task #home\n",
        )

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.test_dir)

    def write_file(self, name, content):
        with open(os.path.join(self.test_dir, name), "w") as f:
            f.write(content)

    def read_file(self, name):
        with open(os.path.join(self.test_dir, name)) as f:
            return f.read()

    def run_xite(self, args_list):
        return run_main_and_capture_all(args_list)

    def test_comma_separated_status_matches_both(self):
        out, _, code = self.run_xite(["--status=new,active", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertIn("alpha task", out)
        self.assertIn("beta task", out)
        self.assertNotIn("gamma task", out)
        self.assertNotIn("delta task", out)

    def test_comma_separated_status_as_separate_argument(self):
        out, _, code = self.run_xite(["--status", "new,complete", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertIn("alpha task", out)
        self.assertIn("gamma task", out)
        self.assertNotIn("beta task", out)

    def test_comma_separated_status_tolerates_whitespace(self):
        out, _, code = self.run_xite(["--status=new , blocked", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertIn("alpha task", out)
        self.assertIn("delta task", out)
        self.assertNotIn("beta task", out)

    def test_comma_separated_status_combines_with_repeated_flag(self):
        out, _, code = self.run_xite(
            ["--status=new,complete", "--status=deferred", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("alpha task", out)
        self.assertIn("gamma task", out)
        self.assertNotIn("beta task", out)
        self.assertNotIn("delta task", out)

    def test_single_status_still_works(self):
        out, _, code = self.run_xite(["--status=active", "todos.xit"])
        self.assertEqual(code, 0)
        self.assertIn("beta task", out)
        self.assertNotIn("alpha task", out)

    def test_comma_separated_status_ands_with_tag(self):
        out, _, code = self.run_xite(
            ["--status=new,active", "--tag=home", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_set_status_with_status_list_edits_only_matches(self):
        _, _, code = self.run_xite(
            ["--set-status", "complete", "--status=new,blocked", "todos.xit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.read_file("todos.xit"),
            "[x] alpha task #work\n"
            "[@] beta task #work\n"
            "[x] gamma task #work\n"
            "[x] delta task #home\n",
        )

    def test_invalid_status_in_list_is_rejected(self):
        _, err, code = self.run_xite(["--status=new,fnord", "todos.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("fnord", err)
        self.assertIn("new", err)

    def test_invalid_status_in_list_blocks_edit(self):
        _, err, code = self.run_xite(
            ["--set-status", "complete", "--status=fnord,new", "todos.xit"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("fnord", err)
        self.assertEqual(
            self.read_file("todos.xit"),
            "[ ] alpha task #work\n"
            "[@] beta task #work\n"
            "[x] gamma task #work\n"
            "[!] delta task #home\n",
        )

    def test_empty_status_entry_is_rejected(self):
        _, err, code = self.run_xite(["--status=new,", "todos.xit"])
        self.assertNotEqual(code, 0)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
