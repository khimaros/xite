#!/usr/bin/env python3

import argparse
import os
import re
import sys
import tomllib  # requires python 3.11+
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Union


###############
## CONSTANTS ##
###############


INDENT_SPACES = 4
UNSPECIFIED_SOURCE = "__unspecified__"

STATUS_MAP = {
    " ": "new",
    "@": "active",
    "x": "complete",
    "~": "obsolete",
    "?": "undecided",
    "!": "blocked",
    ">": "deferred",
}
REVERSE_STATUS_MAP = {v: k for k, v in STATUS_MAP.items()}
VALID_STATUS_CHARS = "".join(map(re.escape, STATUS_MAP.keys()))

# regex: captures indent, optional markdown list marker ' ', status char, rest of line
TODO_ITEM_REGEX = re.compile(rf"^( *)(?:- )?(?:\[([{VALID_STATUS_CHARS}])\])\s*(.*)")
# regex: extracts #tags from text
TAG_REGEX = re.compile(r"#(\w+)")

STATUS_ORDER = {
    "active": 0,
    "new": 1,
    "blocked": 2,
    "undecided": 3,
    "deferred": 4,
    "complete": 5,
    "obsolete": 6,
    "unknown": 7,  # ensure unknown statuses sort last
}

DEFAULT_ROADMAP_FILENAMES = ["ROADMAP.md", "ROADMAP.xit"]


#####################
## DATA STRUCTURES ##
#####################


@dataclass
class TodoItem:
    """represents a single todo item, potentially nested."""

    text: str
    status: str
    level: int  # 0-based indentation level
    tags: List[str] = field(default_factory=list)
    children: List["TodoItem"] = field(default_factory=list)
    parent: Optional["TodoItem"] = None
    source_project: Optional[str] = None
    source_file: Optional[str] = None


#############
## PARSING ##
#############


def parse_todo_list(text: str) -> List[TodoItem]:
    """parses multi-line, indented todo list text into a tree of todoitem objects."""
    root_items: List[TodoItem] = []
    parent_stack: List[TodoItem] = []  # track potential parents by indent
    last_item: Optional[TodoItem] = None
    last_item_indent_len: int = -1

    for line_num, line in enumerate(text.strip().splitlines(), 1):
        match = TODO_ITEM_REGEX.match(line)
        if match:
            indentation, status_char, item_text = match.groups()
            item_indent_len = len(indentation)

            # enforce consistent indentation
            if item_indent_len % INDENT_SPACES != 0:
                _print_warning(
                    f"line {line_num}: inconsistent indent ({item_indent_len} spaces), adjusting to multiple of {INDENT_SPACES}"
                )
                # recover by rounding down
                item_indent_len = (item_indent_len // INDENT_SPACES) * INDENT_SPACES

            level = item_indent_len // INDENT_SPACES
            status = STATUS_MAP.get(status_char, "unknown")
            tags = TAG_REGEX.findall(item_text)
            item = TodoItem(
                text=item_text.strip(), status=status, level=level, tags=tags
            )

            while parent_stack and parent_stack[-1].level >= level:
                parent_stack.pop()

            if parent_stack:
                parent = parent_stack[-1]
                item.parent = parent
                parent.children.append(item)
            else:
                root_items.append(item)

            parent_stack.append(item)
            last_item = item
            last_item_indent_len = item_indent_len

        elif last_item and line.strip():
            current_indent_len = len(line) - len(line.lstrip(" "))
            # continuation lines must be indented further than the start of the task text
            if current_indent_len > last_item_indent_len:
                last_item.text += "\n" + line.strip()
                last_item.tags = TAG_REGEX.findall(last_item.text)
            # else: ignore lines not indented enough to be continuations

    return root_items


################
## FORMATTING ##
################


def _format_recursive(item: TodoItem, lines: List[str]):
    """helper to recursively format an item and its children."""
    base_indent = " " * (item.level * INDENT_SPACES)
    status_char = REVERSE_STATUS_MAP.get(
        item.status, " "
    )  # use space ' ' for unknown status
    text_lines = item.text.splitlines()
    first_line_text = text_lines[0] if text_lines else ""

    lines.append(f"{base_indent}[{status_char}] {first_line_text}")

    # continuation lines indented relative to the start of the task text
    continuation_indent = base_indent + " " * INDENT_SPACES
    for continuation_line in text_lines[1:]:
        lines.append(f"{continuation_indent}{continuation_line}")

    for child in item.children:
        _format_recursive(child, lines)


###############
## FILTERING ##
###############


def filter_items(
    items: List[TodoItem],
    filter_tags: Optional[List[str]],
    filter_statuses: Optional[List[str]],
) -> List[TodoItem]:
    """
    recursively filters items by tags and/or statuses.

    keeps an item if:
    1. it directly matches the filters (tags and/or statuses).
       -> includes a deep copy of its *original* children.
    2. it doesn't match directly, but has descendants that *do* match.
       -> includes only the matching descendants (and their ancestors).

    returns a new list of items, preserving necessary hierarchy.
    """
    if not filter_tags and not filter_statuses:
        return deepcopy(items)  # no filters? return a full copy

    filtered_list: List[TodoItem] = []
    tag_filter_set = set(filter_tags) if filter_tags else set()
    status_filter_set = set(filter_statuses) if filter_statuses else set()

    for item in items:
        kept_children = filter_items(item.children, filter_tags, filter_statuses)

        matches_tag = tag_filter_set and not tag_filter_set.isdisjoint(item.tags)
        matches_status = status_filter_set and item.status in status_filter_set

        if tag_filter_set and status_filter_set:
            direct_match = matches_tag and matches_status
        elif tag_filter_set:
            direct_match = matches_tag
        elif status_filter_set:
            direct_match = matches_status
        else:
            # no filters active means no direct match possible here (handled above)
            direct_match = False

        if direct_match or kept_children:
            item_copy = TodoItem(
                text=item.text,
                status=item.status,
                level=item.level,
                tags=list(item.tags),
                parent=None,  # parent link reset by caller if needed
                source_project=item.source_project,
                source_file=item.source_file,
            )

            if direct_match:
                # keep a deep copy of original children if the parent matches directly
                item_copy.children = deepcopy(item.children)
            else:
                # otherwise, attach only the filtered descendants that matched
                item_copy.children = kept_children
            for child in item_copy.children:
                child.parent = item_copy

            filtered_list.append(item_copy)

    return filtered_list


#############
## SORTING ##
#############


def sort_items_by_status(items: List[TodoItem]):
    """recursively sorts items and their children by status order (in-place)."""
    for item in items:
        sort_items_by_status(item.children)  # sort children first (depth-first)
    items.sort(key=lambda x: STATUS_ORDER.get(x.status, float("inf")))


###############
## UTILITIES ##
###############


def _print_warning(message: str):
    """prints a warning message to stderr."""
    print(f"warning: {message}", file=sys.stderr)


def _print_error(message: str):
    """prints an error message to stderr."""
    print(f"error: {message}", file=sys.stderr)


def _resolve_path(path_str: str, base_dir: Optional[Path] = None) -> Optional[Path]:
    """
    resolves a path string, handling tilde expansion, absolute paths,
    and relative paths based on an optional base directory.

    returns the resolved path object or none if resolution fails.
    prints a warning on error.
    """
    try:
        path_obj = Path(path_str)
        if path_str.startswith("~/"):
            resolved = Path.home() / path_str[2:]
        elif path_str.startswith("~"):
            resolved = Path.home()
        elif path_obj.is_absolute():
            resolved = path_obj
        elif base_dir:
            resolved = base_dir / path_obj
        else:
            resolved = path_obj.resolve()

        # always resolve fully at the end to handle symlinks etc.
        return resolved.resolve()
    except Exception as e:
        _print_warning(f"could not resolve path '{path_str}': {e}")
        return None


def _read_file_content(filepath: Union[str, Path]) -> Optional[str]:
    """reads file content safely, returns none on error."""
    try:
        return Path(filepath).read_text(encoding="utf-8")
    except FileNotFoundError:
        _print_warning(f"file not found: {filepath}")
    except Exception as e:
        _print_warning(f"could not read file {filepath}: {e}")
    return None


#######################
## SOURCE MANAGEMENT ##
#######################


def _add_or_update_source(
    source_key: str,
    is_deprecated: bool,
    collected_sources: Dict[str, bool],
):
    """adds a source key or updates its deprecated status."""
    if source_key not in collected_sources:
        collected_sources[source_key] = is_deprecated
    elif is_deprecated:  # update only if the new status is deprecated
        collected_sources[source_key] = True


def _should_skip_source(source_key: str, project_filter: Optional[List[str]]) -> bool:
    """checks if a source should be skipped based on the project filter."""
    return bool(project_filter and source_key not in project_filter)


def _get_ordered_source_keys(
    collected_sources: Dict[str, bool],
    active_projects_from_toml: List[str],
    include_deprecated: bool,
    project_filter: Optional[List[str]],  # Add this parameter
) -> List[str]:
    """
    determines the final sorted order of source keys (projects/files).

    order:
    1. keys listed in active_projects_from_toml (in that order), if they exist in collected_sources
       and meet the deprecation criteria.
    2. remaining keys from collected_sources (in collection/insertion order), if they meet
       the deprecation criteria.

    respects the include_deprecated flag.
    """
    final_ordered_list: List[str] = []
    added_keys: Set[str] = set()

    for key in active_projects_from_toml:
        if key in collected_sources:
            # --- Add this check ---
            if project_filter and key not in project_filter:
                continue  # Skip if filtered out by --project
            # --- End of added check ---

            is_deprecated = collected_sources[key]
            if include_deprecated or not is_deprecated:
                if key not in added_keys:
                    final_ordered_list.append(key)
                    added_keys.add(key)

    for key in collected_sources:
        if key not in added_keys:  # skip already added active keys
            # --- Add this check ---
            if project_filter and key not in project_filter:
                continue  # Skip if filtered out by --project
            # --- End of added check ---

            is_deprecated = collected_sources[key]
            if include_deprecated or not is_deprecated:
                final_ordered_list.append(key)
                added_keys.add(key)  # technically redundant here, but safe

    return final_ordered_list


#############################
## ITEM COLLECTION HELPERS ##
#############################


def _parse_and_update_items(
    content: str,
    source_project: Optional[str],
    source_file: Optional[str],
    all_items: List[TodoItem],
    source_description: str,  # for warnings
):
    """parses content, sets source info, adds root items to list."""
    try:
        root_items = parse_todo_list(content)
        for item in root_items:
            # assign source only if not already set (avoids overwriting)
            if item.source_project is None and item.source_file is None:
                item.source_project = source_project
                item.source_file = source_file
        all_items.extend(root_items)  # add only roots from this source
    except Exception as e:
        _print_warning(f"could not parse content from {source_description}: {e}")


def _add_items_from_path(
    item_path: Path,
    project_name: Optional[str],
    all_items: List[TodoItem],
    processed_paths: Set[Path],
    is_explicit_file: bool = False,  # true if standalone .xit file passed on cli
):
    """reads content from path, parses items, adds to list, avoiding duplicates."""
    # resolve path first to check against processed_paths
    resolved_path = _resolve_path(str(item_path))
    if not resolved_path or resolved_path in processed_paths:
        return  # resolution failed or already processed

    # read content using the original path (might be needed if resolve changes things unexpectedly)

    content = _read_file_content(item_path)  # use original path for reading
    if content is not None:
        processed_paths.add(resolved_path)  # mark as processed *after* successful read
        # set source_file only for explicitly passed .xit files
        source_file = str(item_path) if is_explicit_file else None
        _parse_and_update_items(
            content, project_name, source_file, all_items, str(item_path)
        )


def _process_standalone_file(
    file_path: Path,
    project_filter: Optional[List[str]],  # filter applied during collection
    all_items: List[TodoItem],
    collected_sources: Dict[str, bool],
    processed_paths: Set[Path],
):
    """processes a standalone .xit file passed via cli."""
    # use full path as the unique source key
    source_key = str(file_path)
    # use filename for filtering check
    project_name_for_filter = file_path.name

    _add_or_update_source(source_key, False, collected_sources)

    # skip adding items if filtered out
    if _should_skip_source(project_name_for_filter, project_filter):
        return

    _add_items_from_path(
        file_path,
        None,  # project name is None for standalone files
        all_items,
        processed_paths,
        is_explicit_file=True,  # sets source_file in item
    )


#########################
## DIRECTORY DISCOVERY ##
#########################
# Functions for finding projects/roadmaps in directories


def _find_roadmap_file(directory: Path, roadmap_names: List[str]) -> Optional[Path]:
    """finds the first matching roadmap file in a directory."""
    for name in roadmap_names:
        path = directory / name
        if path.is_file():
            return path


def _discover_projects_in_search_dir(
    search_dir_abs: Path,
    roadmap_names: List[str],
    search_depth: int,
    project_filter: Optional[List[str]],  # filter applied during collection
    all_items: List[TodoItem],
    collected_sources: Dict[str, bool],
    processed_paths: Set[Path],
    source_description_for_warning: Union[str, Path],  # path or description
):
    """
    scans a directory based on search_depth to find and process project roadmap files.

    handles depth=1 (scandir), depth>1 or depth=-1 (walk), and pruning.
    """
    if not search_dir_abs.is_dir():
        _print_warning(
            f"search directory not found: {search_dir_abs} (from {source_description_for_warning})"
        )
        return

    # --- process a potential project directory ---
    def process_project_dir(project_dir: Path) -> bool:
        """finds roadmap, adds source, adds items if not filtered/processed."""
        found_roadmap_file = _find_roadmap_file(project_dir, roadmap_names)
        if not found_roadmap_file:
            return False  # no roadmap found

        project_name = project_dir.name

        # resolve the roadmap path to check if its content has already been processed
        resolved_roadmap_path = _resolve_path(str(found_roadmap_file))
        if not resolved_roadmap_path:
            # resolution failed, cannot process. warning already printed by _resolve_path.
            return False

        # skip adding source/items if this roadmap file's content was already processed
        # (e.g., via a toml repo or include_files directive)
        if resolved_roadmap_path in processed_paths:
            return True

        # skip adding source/items if this project name (dir name) was already added
        # (e.g., via an explicit toml [project] section without repo/include)
        # this prevents discovered projects from overwriting explicit toml definitions.
        if project_name in collected_sources:
            return True

        # if neither content nor name is processed/claimed, add the source
        _add_or_update_source(project_name, False, collected_sources)

        # skip adding items if filtered out by --project
        if _should_skip_source(project_name, project_filter):
            return True

        # _add_items_from_path uses _resolve_path and handles its own errors/warnings
        _add_items_from_path(
            found_roadmap_file, project_name, all_items, processed_paths
        )
        # we return true because a project *was* found, even if items weren't added
        # because the file was already processed. the processed_paths check inside
        # _add_items_from_path prevents duplicate item addition.
        return True

    # --- end of helper ---

    processed_project_dirs = set()  # track found project dirs to prune walk
    start_depth = len(search_dir_abs.parts)

    # use os.walk for all depths (1, >1, -1) for unified logic
    for dirpath_str, dirnames, filenames in os.walk(
        str(search_dir_abs), topdown=True, onerror=_print_warning
    ):
        current_dir = Path(dirpath_str)
        current_depth = len(current_dir.parts)
        relative_depth = current_depth - start_depth

        # prune descent if the *current* directory's depth equals the search_depth.
        # this allows processing directories *at* search_depth, but prevents going deeper.
        # e.g., search_depth=1 processes relative_depth 0 and 1, but prunes descent from depth 1.
        # e.g., search_depth=2 processes relative_depth 0, 1, 2, but prunes descent from depth 2.
        if search_depth != -1 and relative_depth == search_depth:
            dirnames[:] = []

        # skip processing the *content* of directories strictly deeper than search_depth.
        # this check handles the case where walk might yield dirs out of strict depth order,
        # or if pruning logic changes. it ensures we don't process beyond the limit.
        if search_depth != -1 and relative_depth > search_depth:
            continue

        # skip if inside an already processed project dir found during this walk
        # *unless* that processed dir is the starting search_dir_abs itself
        # (allows exploring direct children of a starting dir that contains a project, up to search_depth)
        is_inside_processed_non_start_dir = False
        for processed_dir in processed_project_dirs:
            if current_dir == processed_dir or processed_dir in current_dir.parents:
                # if the container dir that was processed is *not* the starting search dir,
                # then we should skip processing the current_dir.
                if processed_dir != search_dir_abs:
                    is_inside_processed_non_start_dir = True
                    break  # no need to check other processed dirs
                # else: the container was the starting dir, allow processing to continue (don't set flag)
        if is_inside_processed_non_start_dir:
            dirnames[:] = []
            continue

        # process_project_dir now handles the check for already collected sources
        project_found_here = process_project_dir(current_dir)

        if project_found_here:
            # prevent recursion *into* this project's subdirs during the walk
            processed_project_dirs.add(current_dir)
            # prune subdirs *unless* this is the starting directory of the walk
            # this allows finding sub-projects when a directory arg is given
            # that itself contains a roadmap.
            if current_dir != search_dir_abs:
                dirnames[:] = []
            # don't continue, we might need to process siblings


#####################
## TOML PROCESSING ##
#####################


def _process_include_files(
    include_files_data: any,  # The raw value from TOML
    project_name: str,
    toml_path: Path,
    all_items: List[TodoItem],
    processed_paths: Set[Path],
):
    """Processes the 'include_files' list from a TOML project section."""
    if not isinstance(include_files_data, list):
        if include_files_data is not None:  # Only warn if it's present but wrong type
            _print_warning(
                f"invalid include_files entry for {project_name} in {toml_path}. expected list, got {type(include_files_data).__name__}."
            )
        return  # Exit if not a list or None

    toml_dir = toml_path.parent
    for included_path_str in include_files_data:
        if isinstance(included_path_str, str):
            include_path = _resolve_path(included_path_str, base_dir=toml_dir)
            if include_path:
                # project name identifies the source, source_file is None
                _add_items_from_path(
                    include_path, project_name, all_items, processed_paths
                )
        else:
            _print_warning(
                f"non-string path in include_files for {project_name} in {toml_path}: {included_path_str}"
            )


def _process_repo_config(
    repo_config_data: any,  # The raw value from TOML
    project_name: str,
    toml_path: Path,
    roadmap_names: List[str],
    all_items: List[TodoItem],
    processed_paths: Set[Path],
):
    """Processes the 'repo' dictionary from a TOML project section."""
    if not isinstance(repo_config_data, dict):
        if repo_config_data is not None:  # Only warn if present but wrong type
            _print_warning(
                f"invalid repo entry for {project_name} in {toml_path}. expected dictionary, got {type(repo_config_data).__name__}."
            )
        return  # Exit if not a dict or None

    toml_dir = toml_path.parent
    for repo_key in ["public", "private"]:  # Define keys to check
        repo_path_str = repo_config_data.get(repo_key)
        if isinstance(repo_path_str, str):
            repo_path = _resolve_path(repo_path_str, base_dir=toml_dir)
            if repo_path:
                if repo_path.is_dir():
                    roadmap_file = _find_roadmap_file(repo_path, roadmap_names)
                    if roadmap_file:
                        # project name identifies the source, source_file is None
                        _add_items_from_path(
                            roadmap_file, project_name, all_items, processed_paths
                        )
                    # else: no warning if roadmap not found in repo
                else:
                    _print_warning(
                        f"repo path is not a directory for key '{repo_key}' in {project_name}: {repo_path} (from '{repo_path_str}' in {toml_path})"
                    )
            # else: _resolve_path already printed a warning
        elif repo_path_str is not None:
            _print_warning(
                f"invalid path for key '{repo_key}' in {project_name} in {toml_path}. expected string, got {type(repo_path_str).__name__} (value: '{repo_path_str}')."
            )


def _process_toml_project_section(
    project_name: str,
    project_data: Dict,
    toml_path: Path,
    project_filter: Optional[List[str]],  # filter applied during collection
    all_items: List[TodoItem],
    collected_sources: Dict[str, bool],
    active_projects_from_toml: List[str],  # needed for ordering later
    roadmap_names: List[str],
    processed_paths: Set[Path],
):
    """processes a single [project] section from a toml file."""
    is_deprecated = isinstance(project_data, dict) and project_data.get(
        "deprecated", False
    )
    _add_or_update_source(project_name, is_deprecated, collected_sources)

    # skip further processing if filtered out or deprecated (unless include_deprecated)
    # note: deprecation check happens later during ordering/output generation
    if _should_skip_source(project_name, project_filter):
        return

    toml_dir = toml_path.parent

    if isinstance(project_data, dict) and "tasks" in project_data:
        tasks_content = project_data.get("tasks", "")
        if isinstance(tasks_content, str) and tasks_content.strip():
            # inline tasks are associated with the project, not a specific file
            _parse_and_update_items(
                tasks_content,
                project_name,
                None,
                all_items,
                f"inline tasks in {toml_path} for project {project_name}",
            )

    # process include_files using helper
    _process_include_files(
        include_files_data=project_data.get("include_files"),
        project_name=project_name,
        toml_path=toml_path,
        all_items=all_items,
        processed_paths=processed_paths,
    )

    # process repo config using helper
    _process_repo_config(
        repo_config_data=project_data.get("repo"),
        project_name=project_name,
        toml_path=toml_path,
        roadmap_names=roadmap_names,
        all_items=all_items,
        processed_paths=processed_paths,
    )


def _process_toml_file(
    toml_path: Path,
    project_filter: Optional[List[str]],  # filter applied during collection
    all_items: List[TodoItem],
    collected_sources: Dict[str, bool],
    active_projects_from_toml: List[str],  # list to populate
    processed_paths: Set[Path],
):
    """loads and processes a toml configuration file."""
    try:
        resolved_toml_path = toml_path.resolve()
        # note: we don't add resolved_toml_path to processed_paths,
        # as TOML files aren't typically passed as non-TOML args.

        with resolved_toml_path.open("rb") as f:
            toml_data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        _print_error(f"parsing toml file {toml_path}: {e}")
        return
    except FileNotFoundError:
        _print_error(f"file not found: {toml_path}")
        return
    except Exception as e:
        _print_error(f"reading toml file {toml_path}: {e}")
        return

    meta_config = toml_data.get("META", {})
    search_dirs = meta_config.get("search_dirs", [])
    roadmap_names = meta_config.get("roadmap_files", DEFAULT_ROADMAP_FILENAMES)
    active_from_meta = meta_config.get("active", [])
    search_depth = meta_config.get("search_depth", 1)

    if not isinstance(search_depth, int) or search_depth < -1 or search_depth == 0:
        _print_warning(
            f"invalid META.search_depth in {toml_path}: {search_depth}. using default depth 1."
        )
        search_depth = 1

    if isinstance(active_from_meta, list) and all(
        isinstance(p, str) for p in active_from_meta
    ):
        for name in active_from_meta:
            if name not in active_projects_from_toml:  # avoid duplicates
                active_projects_from_toml.append(name)
    elif active_from_meta:
        _print_warning(f"invalid 'active' list in META of {toml_path}")

    if not isinstance(roadmap_names, list) or not all(
        isinstance(f, str) for f in roadmap_names
    ):
        _print_warning(f"invalid META.roadmap_files in {toml_path}, using defaults")
        roadmap_names = DEFAULT_ROADMAP_FILENAMES

    # process explicitly defined [project] sections first
    # (ensures they appear before discovered projects in the ordered list)
    for project_name, project_data in toml_data.items():
        if project_name != "META":
            _process_toml_project_section(
                project_name,
                project_data,
                toml_path,
                project_filter,
                all_items,
                collected_sources,
                active_projects_from_toml,  # pass along for callees if needed (currently not)
                roadmap_names,
                processed_paths,
            )

    if isinstance(search_dirs, list):
        toml_dir = toml_path.parent
        for search_dir_str in search_dirs:
            if isinstance(search_dir_str, str):
                # resolve relative to the toml file's directory
                search_dir_abs = _resolve_path(search_dir_str, base_dir=toml_dir)
                if search_dir_abs:
                    try:  # wrap discovery to catch errors within it
                        _discover_projects_in_search_dir(
                            search_dir_abs=search_dir_abs,
                            roadmap_names=roadmap_names,
                            search_depth=search_depth,
                            project_filter=project_filter,
                            all_items=all_items,
                            collected_sources=collected_sources,
                            processed_paths=processed_paths,
                            source_description_for_warning=toml_path,  # use toml path as source desc
                        )
                    except Exception as e:
                        _print_warning(
                            f"processing search_dir '{search_dir_str}' from {toml_path}: {e}"
                        )
                # else: _resolve_path already printed warning
            else:
                _print_warning(
                    f"non-string path in META.search_dirs in {toml_path}: {search_dir_str}"
                )
    elif search_dirs is not None:
        _print_warning(
            f"invalid search_dirs entry in {toml_path}. expected list, got {type(search_dirs).__name__}."
        )


####################
## MAIN EXECUTION ##
####################


def _collect_all_items(
    args: argparse.Namespace,
    collected_sources: Dict[str, bool],
    active_projects_from_toml: List[str],
    processed_file_paths: Set[Path],  # tracks processed file paths to avoid cycles
) -> List[TodoItem]:
    """phase 1: collect items from all command line sources."""
    raw_items: List[TodoItem] = []
    for path_arg in args.files:
        try:
            resolved_path = _resolve_path(str(path_arg))
            if not resolved_path:
                # _resolve_path already printed warning, or original arg was bad
                _print_error(f"input path could not be processed: {path_arg}")
                continue

            if not resolved_path.exists():
                _print_error(f"input path not found: {resolved_path} (from {path_arg})")
                continue

            if resolved_path.is_dir():
                # depth=2 means scan the directory itself (depth 0) and its immediate children (depth 1)
                _discover_projects_in_search_dir(
                    search_dir_abs=resolved_path,
                    roadmap_names=DEFAULT_ROADMAP_FILENAMES,  # use defaults for direct dir args
                    search_depth=2,
                    project_filter=args.filter_projects,
                    all_items=raw_items,
                    collected_sources=collected_sources,
                    processed_paths=processed_file_paths,
                    source_description_for_warning=path_arg,  # use original arg for warning context
                )
            elif resolved_path.is_file():
                if resolved_path.suffix.lower() == ".toml":
                    _process_toml_file(
                        toml_path=resolved_path,
                        project_filter=args.filter_projects,
                        all_items=raw_items,
                        collected_sources=collected_sources,
                        active_projects_from_toml=active_projects_from_toml,
                        processed_paths=processed_file_paths,
                    )
                else:
                    _process_standalone_file(
                        file_path=resolved_path,
                        project_filter=args.filter_projects,
                        all_items=raw_items,
                        collected_sources=collected_sources,
                        processed_paths=processed_file_paths,
                    )
            else:
                _print_warning(
                    f"input path is not a file or directory: {resolved_path} (from {path_arg})"
                )

        except Exception as e:
            # catch errors during processing of a specific path argument
            _print_error(f"processing path {path_arg}: {e}")
            continue  # process other paths
    return raw_items


def _process_and_format_items(
    args: argparse.Namespace,
    raw_items: List[TodoItem],
    collected_sources: Dict[
        str, bool
    ],  # needed for source info lookup if required later
    active_projects_from_toml: List[str],  # potentially needed if logic changes
    ordered_source_keys: List[str],
) -> List[str]:
    """phases 2-4: filter, group, sort, limit, and format items."""
    filtered_items = filter_items(raw_items, args.filter_tags, args.filter_statuses)

    items_by_source = defaultdict(list)
    # create a set for efficient lookup of keys to include
    keys_to_include = set(ordered_source_keys)
    for item in filtered_items:
        # determine the source key for grouping
        # prefer explicit file path if set (standalone files), then project, then unspecified
        source_key = item.source_file or item.source_project or UNSPECIFIED_SOURCE
        # only group items whose source key is in the final ordered & limited list
        if source_key in keys_to_include:
            items_by_source[source_key].append(item)

    final_output_lines: List[str] = []
    first_header_printed = False
    for source_key in ordered_source_keys:
        # skip sources that ended up with no items after filtering (tags/status)
        if source_key not in items_by_source:
            continue

        source_items = items_by_source[source_key]

        if args.sort_by_status:
            sort_items_by_status(source_items)

        items_to_print = []
        if args.max_tasks is not None and args.max_tasks >= 0:
            top_level_count = 0
            for item in source_items:
                if item.level == 0:
                    if top_level_count < args.max_tasks:
                        items_to_print.append(item)
                        top_level_count += 1
                # children are implicitly included via their parents
        else:
            items_to_print = source_items

        if items_to_print:
            if first_header_printed:
                final_output_lines.append("")
            final_output_lines.append(f"--- {source_key} ---")
            first_header_printed = True
            for item in items_to_print:
                _format_recursive(item, final_output_lines)

    return final_output_lines


def main():
    """cli entry point: parse args, collect, filter, sort, format items."""
    parser = argparse.ArgumentParser(
        description="parse, filter, sort, and format todo list files (.xit, .toml)."
    )
    parser.add_argument(
        "files",
        metavar="FILE",
        type=Path,
        nargs="+",
        help="path(s) to todo list file(s) (.xit or .toml)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="filter_tags",
        help="filter by tag (repeatable, case-sensitive)",
    )
    parser.add_argument(
        "--status",
        action="append",
        dest="filter_statuses",
        choices=list(STATUS_MAP.values()),
        help="filter by status (repeatable)",
    )
    parser.add_argument(
        "--sort-by-status",
        action="store_true",
        help="sort items within each source by status order",
    )
    parser.add_argument(
        "--project",
        action="append",
        dest="filter_projects",
        help="filter by project name (repeatable)",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        metavar="N",
        help="limit output to n top-level tasks per source",
    )
    parser.add_argument(
        "--max-projects",
        type=int,
        default=None,
        metavar="N",
        help="limit output to n projects/sources (by active/alpha order)",
    )
    parser.add_argument(
        "--list-projects",
        action="store_true",
        help="list all discovered/defined project names and exit",
    )
    parser.add_argument(
        "--include-deprecated",
        action="store_true",
        help="include projects marked as deprecated in the output",
    )
    args = parser.parse_args()

    collected_sources: Dict[str, bool] = {}
    active_projects_from_toml: List[str] = []
    processed_file_paths: Set[Path] = (
        set()
    )  # tracks processed file paths to avoid cycles

    # phase 1: collect items
    # let exceptions propagate for tests that mock this function
    raw_items = _collect_all_items(
        args, collected_sources, active_projects_from_toml, processed_file_paths
    )

    # phase 2: determine source order (and apply limits)
    final_ordered_source_keys = _get_ordered_source_keys(
        collected_sources=collected_sources,
        active_projects_from_toml=active_projects_from_toml,
        include_deprecated=args.include_deprecated,
        project_filter=args.filter_projects,  # Pass the filter here
    )

    # apply --max-projects limit *after* determining the full ordered list
    if args.max_projects is not None and args.max_projects >= 0:
        final_ordered_source_keys = final_ordered_source_keys[: args.max_projects]

    # handle --list-projects mode
    if args.list_projects:
        if final_ordered_source_keys:
            print("\n".join(final_ordered_source_keys))
        sys.exit(0)

    # phase 3: process and format items for output
    # let exceptions propagate for tests that mock this function
    final_output_lines = _process_and_format_items(
        args,
        raw_items,
        collected_sources,
        active_projects_from_toml,
        final_ordered_source_keys,
    )

    if final_output_lines:
        print("\n".join(final_output_lines))


if __name__ == "__main__":
    main()
