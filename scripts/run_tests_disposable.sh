#!/usr/bin/env bash
# Run the frappe_wms2 test suite on a DISPOSABLE site.
#
# The suite creates a test Company, warehouses, items and SUBMITTED stock
# documents. It must never run against a production site: doing so once left
# a test Company, real stock ledger entries and a test WIP pot in the global
# WMS Settings behind on a live site.
#
# This script creates a throwaway site, marks it disposable (the suite
# refuses to run without that flag), runs the tests and drops the site again
# — pass or fail.
#
# Usage, from the bench directory:
#   bash apps/frappe_wms2/scripts/run_tests_disposable.sh
#   bash apps/frappe_wms2/scripts/run_tests_disposable.sh --keep   # debug
#   MODULE=frappe_wms2.tests.test_picking_phase3a bash .../run_tests_disposable.sh
#
# Environment:
#   DB_ROOT_PASSWORD  (default: root)
#   ADMIN_PASSWORD    (default: admin)
#   MODULE            run one module instead of the whole app

set -euo pipefail

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-root}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
SITE="wms2-test-$(date +%Y%m%d-%H%M%S)-$RANDOM"

if [[ ! -d sites ]]; then
  echo "Run this from the bench directory (the one containing ./sites)." >&2
  exit 1
fi

cleanup() {
  if [[ "$KEEP" == "1" ]]; then
    echo ""
    echo "Site kept for inspection: $SITE"
    echo "Drop it with: bench drop-site $SITE --db-root-password '***' --force"
    return
  fi
  echo ""
  echo "--- dropping disposable site $SITE ---"
  bench drop-site "$SITE" --db-root-password "$DB_ROOT_PASSWORD" --force \
    --no-backup >/dev/null 2>&1 || true
  echo "dropped."
}
trap cleanup EXIT

echo "--- creating disposable site $SITE ---"
bench new-site "$SITE" \
  --db-root-password "$DB_ROOT_PASSWORD" \
  --admin-password "$ADMIN_PASSWORD" >/dev/null

echo "--- installing apps ---"
bench --site "$SITE" install-app erpnext >/dev/null
bench --site "$SITE" install-app frappe_wms2 >/dev/null

echo "--- marking site disposable + enabling tests ---"
bench --site "$SITE" set-config wms2_disposable_test_site 1 >/dev/null
bench --site "$SITE" set-config allow_tests true >/dev/null

echo "--- running tests ---"
if [[ -n "${MODULE:-}" ]]; then
  bench --site "$SITE" run-tests --app frappe_wms2 --module "$MODULE"
else
  bench --site "$SITE" run-tests --app frappe_wms2
fi
