# Org Everywhere

  Emacs [Org mode](https://orgmode.org) for Sublime Text, backed by the
  [orgs](https://github.com/ihdavids/orgs) server.

  > **This is a fork.** Org Everywhere is a fork of
  > [OrgExtended](https://github.com/ihdavids/orgextended). Editing an org file
  > works the way it always did, but everything that spans more than one file
  > (the agenda, clocking, capture, refile and archive, ids and backlinks,
  > search, exporting) is now done by the orgs server. OrgExtended and Org
  > Everywhere cannot both be enabled: if Org Everywhere finds OrgExtended at
  > startup it offers to disable it.

# Documentation

  The manual is [docs/docs.html](docs/docs.html): one html file with every
  guide and the full command and settings reference
  ([read it in the browser](https://raw.githack.com/ihdavids/orgeverywhere/main/docs/docs.html)).
  It is built from `docs/` and the sources by `tools/builddocs.sh`.

# How it works

  orgs is a small server that watches a directory of org files and keeps a
  database of them: which files are org files, every heading, its tags, ids,
  dates, clocks and links. Org Everywhere talks to it, and so do the other
  orgs clients:

```
   Sublime Text          VSCode             orgs command line      worg (web)
  (Org Everywhere)  (vscode-org-everywhere)   orgs agenda / tui    any browser
         \                   |                      |                 /
          \------------------+----------+-----------+----------------/
                                        |
                                   orgs server
                                        |
                              your folder of org files
```

  - **In Sublime**: folding, todo cycling, tables, lists, links, source blocks
    and the rest of editing a buffer happen locally, in the plugin.
  - **On the server**: what lands on each day of the agenda, the running
    clock, capture templates, refile targets, id and link resolution,
    backlinks, the query language, habits, exporting, running code, tangling
    and more.
  - **Shared**: every client sees the same notes and the same clock. Clock in
    from Sublime and the clock is running in worg and at the command line; tick
    a task off on your phone in worg and the Sublime agenda shows it done.

  Without a server, editing still works; features that need the server say it
  is not connected rather than showing nothing.

## Getting started

  1. Install Org Everywhere with Package Control, let it install its
     dependencies, and restart Sublime. It needs Sublime Text build 4205 or
     later (it runs on the Python 3.14 plugin host).
  2. Run **Org: Set Up a Local orgs Server**. It downloads orgs, runs it on
     this machine reading a folder of org files you pick, and logs you in to
     it. Updates, its config, its log and its users are under
     **Org: Local orgs Server**.
  3. Or run a server yourself (`orgs serve` beside its `orgs.yaml`) and point
     Org Everywhere at it with **Org: Set orgs server url**. It looks at
     `http://localhost:8010` and `https://localhost:443` by default.
  4. **Org: orgs connection status** shows what is going on, and the status bar
     of an org file shows the connection.

## worg: the same notes in a browser

  `orgs serve` hands out worg, a web front end, at `/` - any browser that can
  reach the server, a phone included, has it. **Org: orgs Open Web UI** opens
  it from Sublime.

  The agenda: habits at the top, the day as a planner, and the tasks still to
  place down the side. Drag a task onto an hour to schedule it.

  ![worg's agenda: habits, the day as a planner, and the tasks still to place](https://raw.githubusercontent.com/ihdavids/orgs/main/docs/images/worg-agenda.png)

  Kanban boards built from a saved query, one column per keyword. Dragging a
  card to another column changes the heading in its file.

  ![worg's kanban board](https://raw.githubusercontent.com/ihdavids/orgs/main/docs/images/worg-kanban.png)

  A saved query as a gantt chart: `EFFORT` sizes the bars, `AFTER` and
  `ORDERED` chain them.

  ![worg's gantt chart](https://raw.githubusercontent.com/ihdavids/orgs/main/docs/images/worg-gantt.png)

  More in [worg, in pictures](https://github.com/ihdavids/orgs/blob/main/docs/worg.org).

## The orgs command line

  The `orgs` command talks to the same server, so what you change at a prompt
  is in Sublime a second later, and the other way round. With a local server
  set up from Sublime, **Org: Use the Local orgs Server from a Terminal**
  gives you a shell alias for its `orgs`, which needs no login. Every command
  also takes `-json`, so a script can read what is drawn here for a person.

  `orgs agenda`: three months of calendar, today's entries and the week.

  ![orgs agenda in a terminal](https://raw.githubusercontent.com/ihdavids/orgs/main/docs/images/cli-agenda.png)

  `orgs tui`: a query's results in a list, with keys to filter, change the
  query, change a keyword or open the heading in your editor.

  ![orgs tui in a terminal](https://raw.githubusercontent.com/ihdavids/orgs/main/docs/images/cli-tui.png)

  `orgs kanban`: worg's boards, editable from the terminal.

  ![orgs kanban in a terminal](https://raw.githubusercontent.com/ihdavids/orgs/main/docs/images/cli-kanban.png)

  More in [the command line, in pictures](https://github.com/ihdavids/orgs/blob/main/docs/cli.org).

# Org mode

  ![Org](https://orgmode.org/resources/img/org-mode-unicorn.svg)

  OrgMode is a lifestyle. While I like sublime, living without Org Mode has been something 
  that has driven me back to Emacs over and over again. I finally decided that I might take a stab
  at building a usable orgmode plugin for sublime.

  ![Start](https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/orgstart.gif)

  When people show off org, they often just show off some folding
  and the ability to tab cycle a headings tree. This often leads people
  to compare org with Markdown. Org IS a markup and document interchange
  format like markdown, but it is also much more.

  ![Folding](https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/orgstartfolding.gif)

  Fundamentally org mode is *2* very /different/ things and fairly hard to describe:

  1. Org Files - A text based document markup language similar to markdown.
  2. Org Mode  - A wide variety of tools for operating on and working with collections of these files.
  3. Org Extensions - I know I said 2 but Org Mode has a wide variety of tools built around the mode and api's extending it in many creative ways. Although not org mode itself most people refer to these tools when they think of org mode.

  Org Mode can and has been used for a large variety of things:

  + A Personal Wiki
  + Professional Notes
  + Time Tracking and Billing
  + Documentation
  + Research Papers - Dynamic Documents
  + Literate Programming
  + Personal Agenda
  + Project Planning
  + As a blogging tool
  + Website content management 
  + A means of planning and managing a budget with ledger mode
  + As a process and memory aid for IT professionals
  + As a self documenting configuration file for emacs itself (and other things).
  + Many many more things

  I use org to manage my agenda:

  ![Agenda](https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/agenda_day.gif)

  Or to help me manipulate tables of data:

  ![Tables](https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/table_hacking.gif) 

  Or build document diagrams:

  ![Diagrams](https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/creating_diagrams.gif) 


  Or build presentations with revealjs, html or pdfs with latex:

  ![Presentations](https://raw.githubusercontent.com/ihdavids/orgextended_docs/master/images/revealjs.gif) 


  What makes Org Mode so versatile is the deceptive simplicity of the format
  the flexibility of the simple markup and the depth of the tools and API's provided around that format.

  At times I have wondered why I shouldn't just use markdown? It is a much more widely popular markup system.
  There is no reason markdown could not be extended to become exactly what Org Mode
  has become, but markdown is not quite as flexible with its meta data and has not had the same degree of *cult*
  following so has yet to evolve into something like Org.

  I hope to go beyond simply the file format and bring some of the actual Mode to Sublime.
  I simply cannot build the entire thing, but I hope we can build something unique and amazing
  in sublime, in its own right.

# This Plugin

  - [Starter Tutorial](https://github.com/ihdavids/orgextended_docs/blob/master/learning_todos.org) - Learning about todos
  - [Basic Structure](https://github.com/ihdavids/orgextended_docs/blob/master/orgeverywhere.org) - Basic file structure
  - [Setup](https://github.com/ihdavids/orgextended_docs/blob/master/setup.org) - Sublime Setup
  - [Editing](https://github.com/ihdavids/orgextended_docs/blob/master/editing.org) - Editing your org file
  - [Lists](https://github.com/ihdavids/orgextended_docs/blob/master/lists.org) - Supported lists in an org file
  - [Folding](https://github.com/ihdavids/orgextended_docs/blob/master/folding.org) - Jumping from the full view to a folded representation.
  - [Links](https://github.com/ihdavids/orgextended_docs/blob/master/links.org) - Jumping within and without.
  - [Navigation](https://github.com/ihdavids/orgextended_docs/blob/master/navigation.org) - Powerful ways of navigating your org files.
  - [Capture, Refile, Archive](https://github.com/ihdavids/orgextended_docs/blob/master/capture.org) - Save ideas fast.
  - [Exporting](https://github.com/ihdavids/orgextended_docs/blob/master/exporting.org) - Converting from org to other things.
  - [Properties and Drawers](https://github.com/ihdavids/orgextended_docs/blob/master/properties.org) - Metadata within a heading
  - [Scheduling](https://github.com/ihdavids/orgextended_docs/blob/master/dates.org) - Dates and times
  - [Time Tracking](https://github.com/ihdavids/orgextended_docs/blob/master/clocking.org) - Clocking and clock reports
  - [Dynamic Blocks](https://github.com/ihdavids/orgextended_docs/blob/master/dynamicblocks.org) - Run arbitrary code
  - [Living Documents](https://github.com/ihdavids/orgextended_docs/blob/master/babel.org) - Babel - Literate Programming, Reproducible Research and Living Documents
  - [Tables](https://github.com/ihdavids/orgextended_docs/blob/master/tables.org) - Spreadsheets in your documents.
  - [Diagrams](https://github.com/ihdavids/orgextended_docs/blob/master/diagrams.org) - Adding diagrams to your documents.
  - [Agenda](https://github.com/ihdavids/orgextended_docs/blob/master/agenda.org) - Keeping track of your day.
  - [Key Bindings](https://github.com/ihdavids/orgextended_docs/blob/master/keybindings.org) - A quick reference of some of the default keybindings.
  
  The full documentation for this plugin is [docs/docs.html](docs/docs.html). The older OrgExtended
  documentation is at: [Docs](https://github.com/ihdavids/orgextended_docs)

# Thank You
  I have shamelessly built this plugin on the backs of some excellent plugins and libraries.

  - [OrgExtended](https://github.com/ihdavids/orgextended) - Org Everywhere is a fork of it, and still owes it most of its editing.
  - The original orgmode plugin - This work was exceptional, OrgEverywhere is a tribute to that work and consumes some of it.
  - sublime_ZK - The inline image preview is entirely due to the Markdown ZK plugin. 
    [ZK](https://github.com/renerocksai/sublime_zk) Thank you renerocksai for pointing out that phantoms can be used for image preview.
  - Table Edit - For now this excellent plugin is a dependency and the basis by which we offer Org style table editing.
  	In a future version we may look to consume the package and own table editing to facilitate expressions and some of the more
  	advanced org table tools, but for now, thank you for making this excellent plugin!
  - pymitter - a great little event library for python.
  - highlightjs - a wonderful highlighting library for html.
  - pandoc - a great command line tool for converting formats.


# References
  To help get you started on your Org journey
  Here are some useful external links:

- [Org Mode Site](https://orgmode.org) - Org
- [The Org Manual](https://orgmode.org/manual/) - The official source of truth for all things orgmode
- [Org Mode 4 Beginners](https://orgmode.org/worg/org-tutorials/org4beginners.html) - An introduction
- [David O'Tools Introduction](https://orgmode.org/worg/org-tutorials/orgtutorial_dto.html) - Yet another intro
- [The Many Uses of Org Mode](https://thoughtbot.com/blog/the-many-uses-of-org-mode)
- [More Uses](https://kitchingroup.cheme.cmu.edu/blog/2014/08/08/What-we-are-using-org-mode-for/)
- [Your Life In Plain Text](http://doc.norang.ca/org-mode.html) - A wonderful reference on one mans journey to use orgmode to improve his life
