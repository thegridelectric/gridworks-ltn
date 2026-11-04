#!/usr/bin/env bash
# Regenerate the LTN's vendored Sema snapshot (src/gwltn/sema) from the
# canonical sema repo. The vendored tree is GENERATED: never hand-edit it;
# edit the seed (src/gwltn/sema_seed_request.yaml) or the sema definitions
# and re-run. The sema CLI refuses a dirty sema checkout and writes only
# under <sema>/output, so the snapshot is reproducible from the sema commit
# the checkout sits on.
#
#   scripts/regen_sema_snapshot.sh                 # sibling ../sema checkout
#   SEMA_REPO=/path/to/sema scripts/regen_sema_snapshot.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEMA_REPO="${SEMA_REPO:-$(cd "${REPO_ROOT}/../sema" 2>/dev/null && pwd || true)}"
PACKAGE_NAME="gwltn"
SEED="${REPO_ROOT}/src/gwltn/sema_seed_request.yaml"
VENDOR_DIR="${REPO_ROOT}/src/gwltn/sema"

if [[ -z "${SEMA_REPO}" || ! -d "${SEMA_REPO}" ]]; then
  echo "error: sema repo not found; set SEMA_REPO=/path/to/sema" >&2
  exit 1
fi

echo "==> sema repo: ${SEMA_REPO} @ $(git -C "${SEMA_REPO}" rev-parse --short HEAD)"
echo "==> seed:      ${SEED}"
cd "${SEMA_REPO}"
echo "==> sema snapshot prepare --allow-staged"
uv run sema snapshot prepare --allow-staged "${SEED}"
echo "==> sema snapshot build --package-name ${PACKAGE_NAME}"
uv run sema snapshot build --package-name "${PACKAGE_NAME}"
echo "==> mirror ${SEMA_REPO}/output/sema -> ${VENDOR_DIR}"
mkdir -p "${VENDOR_DIR}"
rsync -a --delete --exclude='__pycache__' "${SEMA_REPO}/output/sema/" "${VENDOR_DIR}/"
echo "==> done. review the diff (git status) and run ./ci.sh."
