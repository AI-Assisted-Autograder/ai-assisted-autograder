#!/usr/bin/env bash
# Render the architecture diagrams locally.
#
# Requires: java, graphviz (dot). The PlantUML jar is downloaded once
# into ~/.plantuml and reused. Keep PLANTUML_VERSION in sync with
# .github/workflows/render-uml.yml so local and CI output match.
#
#   ./docs/architecture/render.sh          # render SVG + PNG
#   ./docs/architecture/render.sh --check  # validate only, no output

set -euo pipefail

PLANTUML_VERSION='1.2025.4'
JAR="${HOME}/.plantuml/plantuml.jar"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${SRC_DIR}/rendered"

command -v java >/dev/null || { echo 'error: java not found'; exit 1; }
command -v dot  >/dev/null || { echo 'error: graphviz (dot) not found'; exit 1; }

# PlantUML names its output from the "@startuml <name>" directive, NOT
# from the filename. If they drift, a rename silently produces output
# under the old name and the stale file looks legitimate. Catch it.
check_names() {
  local drift=0 f base name
  for f in "${SRC_DIR}"/*.puml; do
    base="$(basename "${f}" .puml)"
    name="$(grep -m1 '^@startuml' "${f}" | awk '{print $2}')"
    if [[ "${name}" != "${base}" ]]; then
      echo "error: ${base}.puml declares '@startuml ${name}'" >&2
      echo "       output would be named '${name}', not '${base}'" >&2
      drift=1
    fi
  done
  return "${drift}"
}

if [[ ! -f "${JAR}" ]]; then
  echo "Downloading PlantUML ${PLANTUML_VERSION}..."
  mkdir -p "$(dirname "${JAR}")"
  curl -fsSL \
    "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar" \
    -o "${JAR}"
fi

# Sequence diagrams cannot use a floating "note as X". Notes there must
# be attached: note over / note left of / note right of. PlantUML
# reports this only as the generic "contains errors", so catch it here
# where the message can say what is actually wrong.
check_sequence_notes() {
  local bad=0 f
  for f in "${SRC_DIR}"/*.puml; do
    grep -q '^participant ' "${f}" || continue
    if grep -n '^[[:space:]]*note as ' "${f}" >/dev/null; then
      echo "error: $(basename "${f}") is a sequence diagram and uses a" >&2
      echo "       floating 'note as'. Attach it: note over <participant>" >&2
      grep -n '^[[:space:]]*note as ' "${f}" | sed 's/^/         /' >&2
      bad=1
    fi
  done
  return "${bad}"
}

# DECISIONS.md carries a generated table of contents. Verify it still
# matches the headings, so a renamed or added section cannot leave a
# stale link behind.
check_toc() {
  python3 - "${SRC_DIR}/DECISIONS.md" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
def anchor(t):
    t = t.lower()
    t = re.sub(r'[^\w\s-]', '', t)
    return re.sub(r'\s+', '-', t.strip())
want = []
for m in re.finditer(r'^(#{2,3}) (.+)$', s, re.M):
    lvl, title = len(m.group(1)), m.group(2)
    if title == 'Contents':
        continue
    want.append(f"{'  '*(lvl-2)}- [{title}](#{anchor(title)})")
block = re.search(r'^## Contents\n\n(.*?)\n\n---', s, re.M | re.S)
if not block:
    print('error: DECISIONS.md has no Contents block', file=sys.stderr)
    sys.exit(1)
have = block.group(1).split('\n')
if have != want:
    print('error: DECISIONS.md table of contents is stale.', file=sys.stderr)
    for line in set(want) - set(have):
        print(f'       missing: {line.strip()}', file=sys.stderr)
    for line in set(have) - set(want):
        print(f'       extra:   {line.strip()}', file=sys.stderr)
    sys.exit(1)
PYEOF
}

echo 'Checking DECISIONS.md table of contents...'
check_toc
echo 'Checking @startuml names match filenames...'
check_names
echo 'Checking sequence-diagram note syntax...'
check_sequence_notes
# One file at a time. Across a glob, PlantUML reports only "Some
# diagram description contains errors" and exits 200 without naming the
# file, which makes a syntax error tedious to locate.
echo 'Checking syntax...'
syntax_status=0
for f in "${SRC_DIR}"/*.puml; do
  if out="$(java -jar "${JAR}" -checkonly -failfast2 "${f}" 2>&1)"; then
    printf 'ok      %s\n' "$(basename "${f}")"
  else
    syntax_status=1
    printf 'FAILED  %s\n' "$(basename "${f}")"
    printf '%s\n' "${out}" | sed 's/^/          /'
  fi
done
if [[ "${syntax_status}" -ne 0 ]]; then
  echo 'One or more diagrams failed to parse.' >&2
  exit 1
fi

if [[ "${1:-}" == '--check' ]]; then
  echo 'OK'
  exit 0
fi

# Remove previously rendered images before regenerating. Without this,
# a renamed or deleted .puml leaves an orphaned .svg/.png behind
# forever, and an orphan is indistinguishable from current output.
# Scoped to the two generated extensions so .gitkeep survives.
mkdir -p "${OUT_DIR}"
echo 'Removing previously rendered images...'
find "${OUT_DIR}" -maxdepth 1 -type f \( -name '*.svg' -o -name '*.png' \) -delete

echo 'Rendering SVG...'
java -Djava.awt.headless=true -jar "${JAR}" -tsvg -failfast2 \
  -o "${OUT_DIR}" "${SRC_DIR}"/*.puml
echo 'Rendering PNG...'
java -Djava.awt.headless=true -jar "${JAR}" -tpng -failfast2 \
  -DPLANTUML_LIMIT_SIZE=16384 \
  -o "${OUT_DIR}" "${SRC_DIR}"/*.puml

echo "Done. Output in ${OUT_DIR}"
ls -1 "${OUT_DIR}"
