#!/usr/bin/env bash
# Local, non-deploying validation. Run before pushing a PR.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

usage() {
  echo "Usage: bash scripts/preflight.sh [--quick|--full]"
  echo "  --quick  Fast checks, syntax, and rendered dashboard units (for CI gate)"
  echo "  --full   (default) All Python tests, Ansible syntax/lint, and unit checks"
}
mode=full
case "${1:-}" in
  "") ;;
  --quick) mode=quick ;;
  --full) mode=full ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing $1. Install dependencies first: python -m pip install -e '.[test]' ansible-core ansible-lint" >&2
    exit 2
  fi
}
require python3
require ansible-playbook
require ansible-lint

step() { printf '\n==> %s\n' "$*"; }
step "Shell syntax"
bash -n scripts/preflight.sh
bash -n deploy.sh

step "Python compilation"
python3 -m compileall -q src

step "Template-rendering regression tests (Ansible trim_blocks)"
python3 -m pytest -q tests/test_dashboard_service_template.py

step "Ansible playbook syntax"
ansible-playbook -i localhost, ansible/playbook.yml --syntax-check
ansible-playbook -i localhost, ansible/dashboard-instances.yml --syntax-check

step "Ansible lint (basic)"
ansible-lint --profile basic ansible/playbook.yml ansible/dashboard-instances.yml

if [ "$mode" = full ]; then
  step "Complete Python test suite"
  python3 -m pytest -ra --durations=10
fi
step "Preflight passed ($mode; no deployment performed)"
