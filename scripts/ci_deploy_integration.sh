#!/usr/bin/env bash
# Real deployment integration on an isolated GitHub Actions runner.
# Usage: bash scripts/ci_deploy_integration.sh <terminal|satellite|watchtower|control>
set -Eeuo pipefail
role="${1:?role required}"
case "$role" in terminal|satellite|watchtower|control) ;; *) echo "Invalid role: $role" >&2; exit 2;; esac
# Always capture service evidence at the first failing command, even
# if the failure occurs before the final service assertions.
report_deploy_failure() {
  local rc=$?
  trap - ERR
  echo "::error::Deployment integration failed (role: ${role}, exit: $rc)"
  if [ -f /etc/systemd/system/ocpp-reports-dashboard.service ]; then
    echo "==> Rendered dashboard systemd unit"
    systemctl cat ocpp-reports-dashboard.service || true
    echo "==> Dashboard service state and recent journal"
    systemctl status ocpp-reports-dashboard.service --no-pager -l || true
    journalctl -u ocpp-reports-dashboard.service -n 80 --no-pager || true
  fi
  exit "$rc"
}
trap report_deploy_failure ERR
# Isolated runners cannot request public ACME certificates or provision credentials.
# Exercise the explicit HTTPS opt-out while keeping production default enabled.
export OCPP_REPORTS_NGINX_ENABLED=false
export OCPP_REPORTS_INSTALL_ROOT="/opt/ocpp-reports-ci"
export OCPP_REPORTS_CLI_LINK="/usr/local/bin/ocpp-reports-ci"
./deploy.sh --${role}
test -f "$OCPP_REPORTS_INSTALL_ROOT/role"
test "$(cat "$OCPP_REPORTS_INSTALL_ROOT/role")" = "${role}"
test -x "$OCPP_REPORTS_CLI_LINK"
test -L "$OCPP_REPORTS_INSTALL_ROOT/current"
test "$(readlink "$OCPP_REPORTS_CLI_LINK")" = "$OCPP_REPORTS_INSTALL_ROOT/current/venv/bin/ocpp-reports"
test "$(readlink "$OCPP_REPORTS_INSTALL_ROOT/current")" = "$OCPP_REPORTS_INSTALL_ROOT/releases/$(git rev-parse HEAD)-pkg"
# Fresh collector/dashboard installs have valid configuration but no
# collected warehouse yet. Validate JSON rather than assuming that
# the CLI's exit code represents installation success.
verify_installed_status() {
  local result code=0
  result="$(sudo -u ocpp-reports "$OCPP_REPORTS_CLI_LINK" status --json)" || code=$?
  printf '%s\n' "$result"
  printf 'Reports CLI status exit code: %s (role: %s)\n' "$code" "${role}"
  if [ "${role}" = watchtower ] || [ "${role}" = control ]; then
    # Both zero and 'unconfigured' (3) are legitimate status exit
    # codes across supported installations, but the JSON must prove
    # this is *only* a missing, not corrupt, collector warehouse.
    if [ "$code" -ne 0 ] && [ "$code" -ne 3 ]; then
      echo "::error::Unexpected Reports status exit code $code" >&2
      return 1
    fi
    printf '%s\n' "$result" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["state"] == "unconfigured"; assert d["config"]["state"] == "valid"; assert d["warehouse"]["state"] == "missing"'
  else
    test "$code" -eq 0
  fi
}
verify_installed_status
test -x "$OCPP_REPORTS_INSTALL_ROOT/releases/$(git rev-parse HEAD)-pkg/venv/bin/ocpp-reports"
bash deploy.sh
# Explicit role must always re-enter Ansible, even for an installed SHA.
./deploy.sh --${role} > /tmp/ocpp-reports-explicit-redeploy.log
! grep -q 'skipped Ansible' /tmp/ocpp-reports-explicit-redeploy.log
grep -q 'PLAY RECAP' /tmp/ocpp-reports-explicit-redeploy.log
test -L "$OCPP_REPORTS_INSTALL_ROOT/current"
test ! -e "$OCPP_REPORTS_INSTALL_ROOT/previous"
verify_installed_status
test "$(cat "$OCPP_REPORTS_INSTALL_ROOT/role")" = "${role}"
case "${role}" in
  terminal) ;;
  satellite)
    test -f /etc/systemd/system/ocpp-reports-forwarder.service
    ! systemctl is-active --quiet ocpp-reports-forwarder.service
    ;;
  watchtower)
    test -f /etc/systemd/system/ocpp-reports-collector.service
    test -f /etc/systemd/system/ocpp-reports-dashboard.service
    ! systemctl is-active --quiet ocpp-reports-collector.service
    if ! systemctl is-active --quiet ocpp-reports-dashboard.service; then
      echo "::error::Dashboard service failed to remain active after deploy"
      systemctl status ocpp-reports-dashboard.service --no-pager -l || true
      journalctl -u ocpp-reports-dashboard.service -n 80 --no-pager || true
      exit 1
    fi
    ;;
  control)
    test -f /etc/systemd/system/ocpp-reports-forwarder.service
    test -f /etc/systemd/system/ocpp-reports-collector.service
    test -f /etc/systemd/system/ocpp-reports-dashboard.service
    ! systemctl is-active --quiet ocpp-reports-forwarder.service
    ! systemctl is-active --quiet ocpp-reports-collector.service
    if ! systemctl is-active --quiet ocpp-reports-dashboard.service; then
      echo "::error::Dashboard service failed to remain active after deploy"
      systemctl status ocpp-reports-dashboard.service --no-pager -l || true
      journalctl -u ocpp-reports-dashboard.service -n 80 --no-pager || true
      exit 1
    fi
    ;;
esac
