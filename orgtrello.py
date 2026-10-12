r"""
/* SDOC: Reference::Commands::Misc
*** Org Trello Sync Board
    Command: =org_trello_sync_board=  Keys: none

    Needs the Trello package (it only exists when that package imports).
    Meant to rebuild the board named by the file's =#+TRELLO: BOARD-ID=
    line, or, without one, to offer your boards and write the chosen one to
    a new =trello_NAME.org= buffer: a TODO/DONE heading per list, a heading
    per card (labels as tags, due date as SCHEDULED, TRELLOID / URL /
    MEMBERS properties, description), checklists as checkbox lists and
    comments as sub headings. Currently broken from an org file: reading
    the =#+TRELLO:= tag fails (the module never imports its settings), so
    run it from a non org buffer to author a new board page.

    Settings: =trello= (a default board id list).
EDOC */
"""


r"""
/* SDOC: Reference::Settings::Misc
*** trello
    Default: not set.

    A board id list used for files with no =#+TRELLO:= line by Org Trello
    Sync Board (which is currently broken from org files, see there).
EDOC */
"""


try:
    import sublime
    import sublime_plugin
    from Trello.trello import TrelloCommand
    import requests
    from Trello.trello import TrelloCommand
    from Trello.lib.trollop.lib import *
    from Trello.trello_cache import TrelloCache
    from Trello.operations import BoardOperation, CardOperation
    import OrgEverywhere.orgutil.util as util
    import OrgEverywhere.orgdb as db
    from OrgEverywhere.orgutil.addmethod import *
    import OrgEverywhere.orgparse.node as node
    import OrgEverywhere.orgparse.date as odate
    import datetime
    import dateutil.parser as dp

    def CreateUniqueViewNamed(name):
        # Return the view if it exists
        win = sublime.active_window()
        for view in win.views():
            if view.name() == name:
                win.focus_view(view)
                return view
        win.run_command('new_file')
        view = win.active_view()
        view.set_name(name)
        view.set_syntax_file("Packages/OrgEverywhere/OrgEverywhere.sublime-syntax")
        return view

    # @add_property_getter(Card, "due")
    # def cardDue(self):
    #     if(not hasattr(self,"_due")):
    #         self._due = DateField("due")
    #     return self._due

    # @add_property_getter(CheckItem, "due")
    # def checkItemDue(self):
    #     if(not hasattr(self,"_due")):
    #         self._due = DateField("due")
    #     return self._due
    
    # @add_property_getter(CheckItem, "state")
    # def checkItemState(self):
    #     if(not hasattr(self,"_state")):
    #         self._state = Field("state")
    #     return self._state

    @add_method(node.OrgBaseNode)
    def trellotag(self, defaultVal = None):
        if(defaultVal == None):
            defaultVal = []
        globalStartup = sets.Get("trello",defaultVal)
        return self.list_comment("TRELLO", globalStartup)

    class OrgTrelloBaseCommand(TrelloCommand):
        def work(self, connection):
            try:
                self.Member = connection.me
                # Grab the boards list. We will use thit to try to match up
                # with current document for a board update OR for authoring a new board
                # page interactively.
                self.Boards = self.Member.boards
                self.Board = None
                node = db.Get().RootInView(self.view)
                if(node != None):
                    trellotag = node.trellotag()
                    if(len(trellotag != 0)):
                        # TODO: Edit check?
                        #       I shouldn't do this if you have edits to the
                        #       current buffer
                        self.BoardId = trellotag[0]
                        self.Board   = self.Member.get_board(self.BoardId)
                self.safe_work(connection)
            except requests.exceptions.HTTPError as e:
                self.show_token_expired_help(e)
                raise e
        def safe_work(self, connection):
            # Override me
            pass



    class OrgTrelloSyncBoardCommand(OrgTrelloBaseCommand):
        def safe_work(self, connection):
            update = False
            if(self.Board != None):
                update = True
                # Clear current view
                self.view.EraseAll()
                # Rebuild the board in this view.
                self.CreateBoardInView(self.view, board)
            if(not update):
                self.AuthorNewBoard()

        def AuthorNewBoard(self):
            self.BoardsList = []
            for b in self.Boards:
                self.BoardsList.append(b.name)
            self.show_quick_panel(self.BoardsList, self.OnCreateBoardPage)

        def OnCreateBoardPage(self, index):
            if(index >= 0):
                board = self.Boards[index]
                v = CreateUniqueViewNamed("trello_" + board.name + ".org")
                self.CreateBoardInView(v, board)

        def CreateBoardInView(self, v, board):
            #print(board.goo())
            v.InsertEnd("#+TITLE: Trello - {}\n".format(board.name))
            v.InsertEnd("#+TRELLO: {}\n".format(board._id))
            v.InsertEnd("\n")
            for l in board.lists:
                v.InsertEnd("* {} {}\n".format("DONE" if l.closed else "TODO",l.name))
                v.InsertEnd("  :PROPERTIES:\n")
                v.InsertEnd("    :TRELLOID: {}\n".format(l._id))
                v.InsertEnd("  :END:\n")
                for c in l.cards:
                    #print(c.goo())
                    if(not hasattr(c,"due")):
                        c.due = c._data['due']
                        #c.due = DateField('due')
                    v.InsertEnd("** {} {}{}\n".format("DONE" if c.closed else "TODO", c.name.ljust(70), (":" + ":".join([str(x['color']) for x in c.labels]) + ":") if c.labels else ""))                    
                    if(c.due != None):
                        v.InsertEnd("   SCHEDULED: {}\n".format(odate.OrgDate.format_date(dp.parse(str(c.due)),True)))
                    v.InsertEnd("   :PROPERTIES:\n")
                    v.InsertEnd("     :TRELLOID: {}\n".format(c._id))
                    v.InsertEnd("     :URL: [[{}][Card]]\n".format(c.short_url))
                    if(len(c.members) > 0):
                        v.InsertEnd("     :MEMBERS: {}\n".format(",".join([x.username for x in c.members])))
                    v.InsertEnd("   :END:\n")
                    if(c.desc):
                        v.InsertEnd("   " + c.desc.replace("\n","\n   ") + "\n")
                    clists = c.checklists
                    for clist in clists:
                        clist.rname = clist._data['name']
                        v.InsertEnd("*** {} [%]\n".format(clist.rname))
                        for it in clist.checkItems:
                            if not hasattr(it,'state'):
                                it.state = it._data['state']
                                #it.state = Field('state')
                            if not hasattr(it,'rname'):
                                it.rname = it._data['name']
                            v.InsertEnd("    - [{}] {}\n".format(' ' if it.state == 'incomplete' else 'x',it.rname))
                    comments = c.comments()
                    for com in comments:
                        v.InsertEnd("*** From {}\n".format(com["username"]))
                        v.InsertEnd("    {}\n".format(com["text"]))
            v.run_command('org_recalc_all_checkbox_summaries')
            v.run_command('org_fold_all')


    class OrgTrelloAddCardCommand(OrgTrelloBaseCommand):
        def safe_work(self, connection):
            pass


except ImportError as err:
    print("Install the Trello Package to add trello support to OrgEverywhere for SublimeText \n  >> " + str(err))


    
