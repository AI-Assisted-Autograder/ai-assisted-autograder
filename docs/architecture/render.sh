#!/usr/bin/env bash
# Render the architecture diagrams locally.
#
# Requires: java, graphviz (dot). The PlantUML jar is downloaded once
# into ~/.plantuml and reused. Keep PLANTUML_VERSION in sync with
# .github/workflows/render-uml.yml so local and CI output match.
#
#   ./docs/architecture/render.sh          # render SVG + PNG
#   ./docs/architecture/render.sh --check  # syntax check only

set -euo pipefail

PLANTUML_VERSION='1.2025.4'
JAR="${HOME}/.plantuml/plantuml.jar"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${SRC_DIR}/rendered"

command -v java >/dev/null || { echo 'error: java not found'; exit 1; }
command -v dot  >/dev/null || { echo 'error: graphviz (dot) not found'; exit 1; }

if [[ ! -f "${JAR}" ]]; then
  echo "Downloading PlantUML ${PLANTUML_VERSION}..."
  mkdir -p "$(dirname "${JAR}")"
  curl -fsSL \
    "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar" \
    -o "${JAR}"
fi

if [[ "${1:-}" == '--check' ]]; then
  echo 'Checking syntax...'
  java -jar "${JAR}" -checkonly -failfast2 "${SRC_DIR}"/*.puml
  echo 'OK'
  exit 0
fi

mkdir -p "${OUT_DIR}"
echo 'Rendering SVG...'
java -Djava.awt.headless=true -jar "${JAR}" -tsvg -failfast2 \
  -o "${OUT_DIR}" "${SRC_DIR}"/*.puml
echo 'Rendering PNG...'
java -Djava.awt.headless=true -jar "${JAR}" -tpng -failfast2 \
  -DPLANTUML_LIMIT_SIZE=16384 \
  -o "${OUT_DIR}" "${SRC_DIR}"/*.puml

echo "Done. Output in ${OUT_DIR}"
ls -1 "${OUT_DIR}"
