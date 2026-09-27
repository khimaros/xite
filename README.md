# XITE

a more evolved plain-text file format for todos and checklists

based heavily on [xit](https://github.com/jotaen/xit) but is not spec compliant

(a todo list for exciteable people, or... [x]it! evolved)

## example

tasks will be pulled from almost any file, as long as the
lines start with `[ ]` or `- [ ]`

tasks can be filtered by `--tag`, by `--status`, and by `--match` (a
case-insensitive substring of the task text, including continuation
lines). repeated filters of the same kind are or'ed together, different
kinds are and'ed. `--status` also takes a comma separated list, so
`--status=new,active` matches both. the filter options work for listings
as well as for the editing commands.

this is an example of a bare todo list:

```
[ ] new
[@] active
[x] complete
[~] obsolete
[?] undecided
[!] blocked
[>] deferred

[@] active parent item
    [@] active child item
        [ ] open second level child
        [x] completed second level child
    [ ] open child item
    [x] completed child item

[ ] item with #project or #category tags
    [ ] and some children of a task with tags
    [ ] this one has a #fnord tag
```

```bash
xite --status=new ./examples/bare.xit

xite --status=new,active ./examples/bare.xit

xite --status=new --tag=fnord ./examples/bare.xit

xite --max-tasks=3 ./examples/bare.xit

xite --match="child item" ./examples/bare.xit
```

## editing

tasks can be added and edited from the command line. a mutation command
(`--add` or `--set-status`) takes exactly one file (not a toml tracker or
a directory), rewrites it in place, and prints the added or modified
task line(s) instead of listing tasks.

```bash
# append a new top-level task (defaults to status new)
xite --add "write docs" ./examples/bare.xit

# new tasks can start with a status and tags
xite --add "ship v1" --set-status=active --tag=v1 ./examples/bare.xit

# set the status of tasks matching a text pattern (case-insensitive)
xite --set-status=complete --match="write docs" ./examples/bare.xit

# selectors can be combined, and all of them must match
xite --set-status=deferred --tag=v1 ./examples/bare.xit
xite --set-status=active --status=blocked ./examples/bare.xit

# a status selector takes a comma separated list
xite --set-status=active --status=blocked,deferred ./examples/bare.xit
```

`--set-status` selects tasks with `--match`, `--tag`, and `--status`
(the current status). if no task matches, the file is left unchanged and
the exit status is non-zero. indentation, markdown list markers, and all
other file content are preserved.

## stable ids

every task can carry a short stable identifier with `--show-ids`. the id
is derived from the task's source and its text, so it is deterministic
across runs and machines, and survives filtering, sorting, and limiting
unchanged. editing a task's text (or moving it between sources) changes
its id.

```bash
xite --show-ids ./examples/bare.xit

# ids stay the same when filtering
xite --show-ids --status=new ./examples/bare.xit
```

ids double as selectors: `--id` matches a task by its full id or any id
prefix, and works with listings and with `--set-status` (the ids of
tasks are unaffected by status edits). to change a task's text, first
find its id, then match the old text with `--match`, since the id of
the new text does not exist yet.

```bash
# list, then edit a single task by id
xite --show-ids ./examples/bare.xit
xite --set-status=active --id=6395425c ./examples/bare.xit

# id prefixes work like the other selectors, and can be combined
xite --id=02a7 ./examples/bare.xit
xite --set-status=deferred --id=02a7 --status=active ./examples/bare.xit
```

## flag parsing precedence

command-line flags are processed in the following order:

1. mutation commands (`--add`, `--set-status`) short-circuit and edit the single file argument
1. top level FILES from positional args are parsed
1. `--project` filters are applied before other files are loaded
1. files referenced explicitly by TOML or implicitly by `search_dirs` are loaded
1. stable ids are assigned to all collected tasks
1. `--tag`, `--status`, `--match`, and `--id` filters are applied
   (ids are shown with `--show-ids`)
1. sorting (`--sort-by-status`) is applied
1. output limits (`--max-tasks`) are applied (per project/source)

## acknowledgments

this project was almost 100% vibe coded with the help of aider and Gemini 2.5 Pro.

inspired heavily by [xit](https://github.com/jotaen/xit)
