#!/bin/sh
# Builds OrgEverywhere's documentation: the SDOC blocks in the python sources
# plus the guides in docs/, into build/docs/OrgEverywhere.org, then that into
# build/docs/OrgEverywhere.html with the orgs html exporter's docs theme.
#
#   tools/builddocs.sh            org and html
#   tools/builddocs.sh -org       only the org file
#   tools/builddocs.sh -open      and open the html when done
#
# Needs the orgs project (https://github.com/ihdavids/orgs) for two tools:
#   docex - pulls the SDOC blocks out of the sources (cmd/docex)
#   orgs  - exports the result to html (its html exporter and templates)
# Found through ORGS_DIR (default: ../orgs beside this repository). DOCEX and
# ORGS name the binaries directly if they live elsewhere; docex is built from
# ORGS_DIR when it is not on the PATH (needs go). DOCS_OUT changes where the
# output goes (default build/docs).
#
# How the documentation is written (SDOC blocks, guides, sections) is in
# docs/WRITING_DOCS.md.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORGS_DIR="${ORGS_DIR:-$ROOT/../orgs}"
OUT="${DOCS_OUT:-$ROOT/build/docs}"
NAME="OrgEverywhere"
HTML=1
OPEN=0
for arg in "$@"; do
    case "$arg" in
        -org) HTML=0 ;;
        -open) OPEN=1 ;;
        -h|-help|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "builddocs: unknown option $arg (try -help)" >&2; exit 2 ;;
    esac
done

mkdir -p "$OUT"

# --- docex ---------------------------------------------------------------
if [ -z "$DOCEX" ]; then
    if command -v docex >/dev/null 2>&1; then
        DOCEX="$(command -v docex)"
    elif [ -d "$ORGS_DIR/cmd/docex" ] && command -v go >/dev/null 2>&1; then
        DOCEX="$OUT/docex"
        echo "builddocs: building docex from $ORGS_DIR"
        (cd "$ORGS_DIR" && go build -o "$DOCEX" ./cmd/docex)
    else
        echo "builddocs: no docex. Put it on the PATH, set DOCEX, or set ORGS_DIR to an orgs checkout (and install go)." >&2
        exit 1
    fi
fi

# docex walks every .py under -src. Give it the package's own sources only:
# not build output, not a stray copy of the package nested inside it.
SRC="$OUT/src"
rm -rf "$SRC"
mkdir -p "$SRC"
(cd "$ROOT" && find . -name '*.py' \
    -not -path './build/*' -not -path './OrgEverywhere/*' -not -path './OrgExtended/*' -not -path '*/__pycache__/*' \
    -not -path './.git/*' -print) | while read -r f; do
    mkdir -p "$SRC/$(dirname "$f")"
    cp "$ROOT/$f" "$SRC/$f"
done

ORGFILE="$OUT/$NAME.org"
"$DOCEX" -src "$SRC" -ext .py -guides "$ROOT/docs" -start "$ROOT/docs/overview.org" \
    -title "$NAME" -theme docs -out "$ORGFILE"
rm -rf "$SRC"
echo "builddocs: wrote $ORGFILE"

[ "$HTML" = 1 ] || exit 0

# --- html ----------------------------------------------------------------
if [ -z "$ORGS" ]; then
    if [ -x "$ORGS_DIR/orgs" ]; then
        ORGS="$ORGS_DIR/orgs"
    elif command -v orgs >/dev/null 2>&1; then
        ORGS="$(command -v orgs)"
    else
        echo "builddocs: no orgs binary for the html (set ORGS or build $ORGS_DIR); the org file is done." >&2
        exit 1
    fi
fi
# The html exporter finds its templates under ./templates of the directory it
# runs in, so it runs from the orgs checkout. A config of our own keeps the
# user's (their org directories, pollers, plugins) out of it; -orgdir runs the
# server inside this one command, so no running server is needed or touched.
TEMPLATES_DIR="${ORGS_TEMPLATES_DIR:-$ORGS_DIR}"
if [ ! -f "$TEMPLATES_DIR/templates/html_docs.tpl" ]; then
    echo "builddocs: no templates/html_docs.tpl under $TEMPLATES_DIR (set ORGS_DIR or ORGS_TEMPLATES_DIR)" >&2
    exit 1
fi
CONF="$OUT/docs-orgs.yaml"
cat > "$CONF" <<EOF
server:
  allowHttps: false
  templatePath: "$TEMPLATES_DIR/templates"
  noInternalFilters: true
  exporters:
    - name: "html"
EOF
HTMLFILE="$OUT/$NAME.html"
LOG="$OUT/export.log"
if (cd "$TEMPLATES_DIR" && "$ORGS" -config "$CONF" -orgdir "$OUT" export -f html -out "$HTMLFILE" "$ORGFILE") >"$LOG" 2>&1; then
    echo "builddocs: wrote $HTMLFILE"
else
    echo "builddocs: the html export failed; see $LOG" >&2
    exit 1
fi

if [ "$OPEN" = 1 ]; then
    case "$(uname)" in
        Darwin) open "$HTMLFILE" ;;
        *) xdg-open "$HTMLFILE" >/dev/null 2>&1 || true ;;
    esac
fi
