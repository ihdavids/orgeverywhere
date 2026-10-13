# Writing OrgEverywhere's documentation

The manual is built from two kinds of source by `tools/builddocs.sh`
(docex from the orgs project, then the orgs html exporter's `docs` theme):

1. **Guides**: the `.org` files at the top of `docs/` (not subdirectories).
   Prose: how to use a feature, with examples.
2. **Reference**: `SDOC` blocks in the python sources, next to the code they
   describe: each command, each setting. Written where the code is, so a
   change to a command is one scroll away from its documentation.

`docs/overview.org` is the introduction and is written first, whole.

Build: `tools/builddocs.sh` (org and html in `build/docs/`, and the html
copied to `docs/docs.html`, which the README links to; commit it), `-org` for
the org file only, `-open` to open the html. It needs an orgs checkout at
`../orgs` or `ORGS_DIR`.
The docex it builds must take `-ext`, `-title`, `-theme` and `-guides`
(and read `#+DOC_ORDER`), which the orgs repository's `cmd/docex` does.

## Guides

```org
#+TITLE: The Agenda
#+DOC_SECTION: Planning
#+DOC_ORDER: 1

* What the agenda shows
  ...
```

- `#+TITLE` is the guide's heading in the manual.
- `#+DOC_SECTION` is the section it sits in (`a::b` nests). The sections are
  listed below; use one of them.
- `#+DOC_ORDER` orders guides inside a section (lowest first; guides
  without one follow alphabetically).
- Only those keyword lines may come before the first text: the first other
  line ends the header.
- Write top level headings with one star. docex pushes them down under the
  guide's title.
- Pictures: link the published copies
  (`https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/NAME`),
  not `file:` links. The html is one file, read from anywhere.

### Sections are guides too

Each top level section has a small guide of its own,
`docs/section-<name>.org`, with `#+TITLE: <Section>`, no `#+DOC_SECTION`
and a `#+DOC_ORDER`. That is what puts *Getting Started* before *Editing*:
docex otherwise sorts sections alphabetically. A new top level section
needs one.

### Writing org that exports well

- **Do not link between guides** (`[[The Agenda]]`): the html exporter
  gives headings generated ids, so such a link goes nowhere. Name the guide
  in bold instead: *The Agenda*. Links out (`https://...`) are fine.
- Verbatim text that contains `=` (a formula like `$4=$2*$3`) needs
  `~...~`; `=...=` ends at the first `=`.
- Inside `#+BEGIN_SRC org` / `#+BEGIN_EXAMPLE` write headings and `#+`
  lines as they are; no comma escaping (a comma would show in the html).
- Anything that starts with `*` or `|` in column one of an example belongs
  in such a block, or it becomes a heading or a table.

## SDOC blocks in python

Python has no `/* */` comments, so a block is a raw string statement. docex
reads lines, not python, and python ignores a string statement:

```python
r"""
/* SDOC: Reference::Commands::Agenda
*** Org: Agenda Query
    Command: =org_agenda_query=

    Asks for an orgs query and shows the headings it finds...
EDOC */
"""
```

- `/* SDOC: Section::Sub` and `EDOC */` each on a line of their own. That
  exact spelling: `/*` on its own line followed by `SDOC:` is not extracted.
- Always `r"""`: a backslash in an example must not become an escape.
- **Heading stars = the depth of the section path.** `Reference::Commands::Agenda`
  is three deep, so the block's headings start with `***` (docex adds the
  one that puts them under the section). Deeper headings inside the block
  add stars from there.
- **The first heading names the block, and must be unique in its section**:
  two blocks with the same first heading in one section overwrite each other.
- Inside a class body the block may be indented; docex re-indents headings.
- Put a block at module level above the class it documents, or inside the
  class. Never inside a function (it would be built every call).
- Text inside `#+BEGIN_...`/`#+END_...` is copied as is, so a `* line` in an
  example stays text.

## Sections

| Section                     | What goes there                                            |
|-----------------------------+------------------------------------------------------------|
| Getting Started             | install, the orgs server, connecting and logging in, keys  |
| Editing                     | headings, folding, navigation, lists, properties, dates    |
| Planning                    | todos, the agenda, capture, refile, clocking, day pages    |
| Links                       | links, ids, backlinks, images                              |
| Tables                      | tables, formulas, plots, column view, dynamic blocks       |
| Source Blocks               | babel, diagrams, tangling                                  |
| Exporting                   | html, latex, pandoc, reveal.js, server exporters           |
| orgs Server                 | what the server adds: query search, habits, reports...     |
| Reference::Commands::<Area> | one heading per command (SDOC)                             |
| Reference::Settings::<Area> | one heading per setting or group of settings (SDOC)        |

Areas for the reference: Connection, Agenda, Capture, Clocking, Day Pages,
Editing, Folding, Navigation, Links, Tables, Source Blocks, Exporting,
Dynamic Blocks, Notifications, orgs Server, Misc.

## A reference entry

```org
*** Org: Clock In
    Command: =org_clock_in=  Keys: =ctrl+alt+o i=

    Starts the clock on the heading at the cursor. With an orgs server the
    clock lives on the server, shared with every other orgs client...

    Settings: =clockShowInStatus=, =orgsClockPollSeconds=.
```

Title by the command palette caption; the command name, its default keys
(from `Default.sublime-keymap` / `OrgEverywhere.sublime-keymap`) and its
arguments, then what it does and what it needs (the orgs server, a
setting). A setting entry gives its name, default and effect.
