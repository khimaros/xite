# ROADMAP

```
[ ] more terse and intuitive query/edit ux
[ ] reimplement the entire project in rust

[>] backward compatibility with [x]it specification
    [>] support grouping tasks
    [x] support target dates
    [x] support prioritization
    [x] support line continuations on task names
    [x] support standard statuses {open,ongoing,checked,obsolete,in question}
    [x] parse basic `.xit` file format
    [x] support for #tags

[x] xite --format to linewrap (80 cols) and group the roadmap items by status:
    active, new, blocked, undecided, deferred, complete, obsolete;
    each section should have one empty line between them
[x] allow selecting multiple (OR) statuses with --status=new,active
[x] enable matching (and editing) by task id
[x] add a flag to output a stable id with each task
[x] lossless round tripping back to original text format
    [x] preserve all white space
    [x] preserve all non-whitespace formatting
[x] handle child tasks (unlimited levels of nesting)
[x] parse TOML files with nested project definitions
[x] extract inline tasks defined within TOML projects
[x] load tasks from external files specified in TOML
[x] discover and load project tasks automatically by searching directories
    specified in `[META].search_dirs`.
[x] look for specific roadmap filenames (`ROADMAP.md`, `ROADMAP.xit` by
    default, configurable via `[META].roadmap_files`) within discovered
    project directories.
[x] filter tasks by one or more tags (`--tag`).
[x] filter tasks by one or more statuses (`--status`).
[x] filter tasks by project name (`--project`)
[x] option to include all children when applying a status filter (`--include-children`).
[x] sort tasks recursively by status (`--sort-by-status`)
[x] limit the number of top-level tasks displayed per source (project or file) (`--max-tasks`).
[x] list all discovered and defined project names (`--list-projects`).
```
