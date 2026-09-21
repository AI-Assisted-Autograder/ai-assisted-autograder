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

echo 'Checking @startuml names match filenames...'
check_names
echo 'Checking syntax...'
java -jar "${JAR}" -checkonly -failfast2 "${SRC_DIR}"/*.puml

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
