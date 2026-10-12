# The orgs server integration.
#
# orgs (https://github.com/ihdavids/orgs) is a go based org mode server that
# owns the cross file side of org mode: which files exist, ids, tags,
# backlinks, refile targets, capture, clocking, day pages and the query
# language. OrgEverywhere uses it in place of its own in process database.
#
#   client.py    - pure python REST client (no sublime imports, testable)
#   creds.py     - OS keychain / credential manager / secret-tool storage
#   discovery.py - finding a running server
#   events.py    - the /events server sent event stream
#   localserver.py - downloading, setting up and running a local orgs server
#   paths.py     - mapping server paths to local paths
#   session.py   - the sublime side: settings, login prompts, the singleton
