#!/bin/sh
set -eu
cd "$(dirname "$0")"

role=""
case "${1:-}" in
  --terminal|--satellite|--watchtower|--control)
    role="${1#--}"
    shift
    ;;
  --*)
    echo "Unknown deploy option: $1" >&2
    exit 2
    ;;
esac
if [ "$#" -ne 0 ]; then
  echo "Usage: ./deploy.sh [--terminal|--satellite|--watchtower|--control]" >&2
  exit 2
fi

root="${OCPP_REPORTS_INSTALL_ROOT:-/opt/ocpp-reports}"
marker="$root/role"
if [ -f "$marker" ]; then
  installed_role=$(cat "$marker")
  case "$installed_role" in terminal|satellite|watchtower|control) ;; *)
    echo "Invalid existing Reports role marker; refusing deployment" >&2; exit 2 ;;
  esac
  if [ -n "$role" ] && [ "$role" != "$installed_role" ]; then
    echo "Reports role is $installed_role; uninstall before selecting $role" >&2
    exit 2
  fi
  role="$installed_role"
elif [ -e "$marker" ]; then
  echo "Invalid Reports role marker; refusing deployment" >&2
  exit 2
else
  # An existing installation without a role marker cannot be adopted.
  if [ -e "$root/current" ] || [ -d "$root/releases" ] || [ -L "${OCPP_REPORTS_CLI_LINK:-/usr/local/bin/ocpp-reports}" ]; then
    echo "Existing Reports installation lacks a role marker; refusing deployment" >&2
    exit 2
  fi
  role="${role:-terminal}"
fi

exec ansible-playbook -i localhost, -c local ansible/playbook.yml -e "reports_role=$role"
