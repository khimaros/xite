# XITE

a more evolved plain-text file format for todos and checklists

based heavily on [xit](https://github.com/jotaen/xit) but is not spec compliant

(a todo list for exciteable people, or... [x]it! evolved)

## example

tasks will be pulled from almost any file, as long as the
lines start with `[ ]` or `- [ ]`

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
xite --status=open ./examples/bare.xit

xite --status=open --tag=fnord ./examples/bare.xit

xite --max-tasks=3 ./examples/bare.xit
```

## flag parsing precedence

command-line flags are processed in the following order:

1. top level FILES from positional args are parsed
1. `--project` filters are applied before other files are loaded
1. files referenced explicitly by TOML or implicitly by `search_dirs` are loaded
1. `--tag` and `--status` filters are applied
1. sorting (`--sort-by-status`) is applied
1. output limits (`--max-tasks`) are applied (per project/source)

## acknowledgments

this project was almost 100% vibe coded with the help of aider and Gemini 2.5 Pro.

inspired heavily by [xit](https://github.com/jotaen/xit)
