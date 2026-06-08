#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${REPO_ROOT}/.codex/skills/pgc-production-batch"
CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
TARGET_DIR="${CODEX_HOME_DIR}/skills"
TARGET="${TARGET_DIR}/pgc-production-batch"
MODE="${1:---symlink}"

if [[ ! -f "${SOURCE}/SKILL.md" ]]; then
  echo "missing repo skill: ${SOURCE}/SKILL.md" >&2
  exit 1
fi

case "${MODE}" in
  --symlink|--copy)
    ;;
  *)
    echo "usage: $0 [--symlink|--copy]" >&2
    exit 2
    ;;
esac

mkdir -p "${TARGET_DIR}"

if [[ -L "${TARGET}" ]]; then
  rm "${TARGET}"
elif [[ -e "${TARGET}" ]]; then
  BACKUP="${TARGET}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  mv "${TARGET}" "${BACKUP}"
  echo "backed up existing skill to ${BACKUP}"
fi

if [[ "${MODE}" == "--copy" ]]; then
  cp -R "${SOURCE}" "${TARGET}"
else
  ln -s "${SOURCE}" "${TARGET}"
fi

echo "installed pgc-production-batch skill at ${TARGET}"
