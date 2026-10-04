# requirements

product requirements for xite. never regress these without explicit approval.

## reading tasks (existing)

- parse `.xit` and markdown-ish task lines (`[ ]` and `- [ ]`) from files.
- support statuses: new, active, complete, obsolete, undecided, blocked, deferred.
- support nesting, priorities, target dates, tags, line continuations.
- parse TOML trackers with inline, included, and discovered task sources.
- filter by tag, status, text pattern (`--match`, case-insensitive
  substring), and project; sort by status; limit output size.
- repeated `--tag`/`--status`/`--match` filters are or'ed within a kind
  and and'ed across kinds; the same selectors apply to editing.
- `--status` accepts a comma separated list of statuses (e.g.
  `--status=new,active`), and empty or unknown entries are rejected with
  a non-zero exit before any file is read or written.
- lossless round tripping: reading and re-writing preserves formatting.
- `--format` prints tasks grouped by status (active, new, blocked,
  undecided, deferred, complete, obsolete) with no section headers and one
  empty line between sections, and wraps output lines to 80 columns using
  continuation-line indentation so wrapped output re-parses as the same task.
- grouping and wrapping are one flag: `--format` always does both, applies per
  source, keeps children attached to their parents, and preserves relative
  order within a status.
- print a stable, content-derived id with each task (`--show-ids`);
  ids are deterministic across runs, preserved through filtering, and
  distinct for duplicate task texts within a source.
- select tasks by stable id (`--id`, full id or prefix) for listings
  and for `--set-status`; repeated `--id` selectors are or'ed and
  combined by and with the other selectors; status edits leave ids
  unchanged.

## editing tasks (new)

- add a new top-level task to a file from the command line (`--add`).
- edit the status of existing tasks from the command line (`--set-status`).
- select tasks to edit by text pattern (`--match`, case-insensitive substring),
  by `--tag`, and/or by current `--status`; all provided criteria must hold.
- edits are applied in place and preserve all other file content,
  indentation, markdown list markers, and spacing after the status char.
- mutation commands (`--add`, `--set-status`) require exactly one file argument.
- TOML trackers and directories cannot be edited directly; only plain
  task files can.
- `--add` may create a new file; `--set-status` requires the file to exist.
- if no task matches a `--set-status` edit, exit non-zero and leave the file
  unchanged.
- mutation commands print the added or modified task line(s) to stdout.
