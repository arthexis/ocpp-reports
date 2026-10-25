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
  # Explicit role permits resuming a partial release owned by this installer.
  # Never adopt a legacy current pointer or an unrecognized CLI link.
  cli="${OCPP_REPORTS_CLI_LINK:-/usr/local/bin/ocpp-reports}"
  if [ -e "$root/current" ] || [ -L "$root/current" ]; then
    # Activation precedes role persistence. Resume only a recognizable, linked,
    # immutable release, and only when the operator explicitly selects a role.
    current_target=$(readlink "$root/current" 2>/dev/null || true)
    case "$current_target" in
      "$root"/releases/*-pkg)
        if [ -z "$role" ] || [ ! -d "$current_target" ] || [ ! -x "$current_target/venv/bin/ocpp-reports" ]; then
          echo "Unmarked active Reports release requires an explicit role and valid release" >&2; exit 2
        fi
        ;;
      *)
        echo "Unrecognized Reports current pointer without role marker; refusing deployment" >&2; exit 2
        ;;
    esac
  fi
  if [ -e "$root/releases" ] || [ -L "$root/releases" ] || [ -L "$cli" ] || [ -e "$cli" ]; then
    if [ -z "$role" ] || [ ! -d "$root/releases" ] || [ -L "$root/releases" ]; then
      echo "Partial Reports installation requires an explicit role and owned releases directory" >&2; exit 2
    fi
    if [ -e "$cli" ] || [ -L "$cli" ]; then
      target=$(readlink "$cli" 2>/dev/null || true)
      case "$target" in "$root"/releases/*/venv/bin/ocpp-reports|"$root"/current/venv/bin/ocpp-reports) ;; *)
        echo "Unrecognized Reports CLI link; refusing deployment" >&2; exit 2;; esac
    fi
  fi
  role="${role:-terminal}"
fi

# Default deployments may skip convergence only for a verified active release.
# Explicit role selection and environment overrides continue through Ansible.
if [ "$#" -eq 0 ] &&
   [ -z "${OCPP_REPORTS_LOCAL_INGEST_ENABLED+x}" ] &&
   [ -z "${OCPP_REPORTS_NGINX_ENABLED+x}" ]; then
  revision=$(git rev-parse --verify HEAD)
  release="$root/releases/$revision-pkg"
  cli="${OCPP_REPORTS_CLI_LINK:-/usr/local/bin/ocpp-reports}"
  if [ "$(readlink "$root/current" 2>/dev/null || true)" = "$release" ] &&
     { [ "$(readlink "$cli" 2>/dev/null || true)" = "$root/current/venv/bin/ocpp-reports" ] || [ "$(readlink "$cli" 2>/dev/null || true)" = "$release/venv/bin/ocpp-reports" ]; } &&
     [ -x "$release/venv/bin/ocpp-reports" ] &&
     "$release/venv/bin/ocpp-reports" status --json >/dev/null 2>&1; then
    printf 'Reports revision %s already installed and healthy; skipped Ansible.\n' "$revision"
    exit 0
  fi
fi

exec ansible-playbook -i localhost, -c local ansible/playbook.yml -e "reports_role=$role"
