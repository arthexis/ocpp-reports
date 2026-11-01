#!/bin/sh
set -eu
cd "$(dirname "$0")"

# Serialize all canonical owner deployments and retention operations.
# Keep the descriptor open while Ansible (or prune) runs; do not unlink the lock.
state_dir="$HOME/.local/state/ocpp-reports"
if [ -L "$HOME/.local/state" ] || [ -L "$state_dir" ]; then
  echo "Unsafe deployment state directory" >&2; exit 2
fi
(umask 077; mkdir -p "$state_dir")
if [ -L "$state_dir/deploy.lock" ]; then
  echo "Unsafe deployment lock" >&2; exit 2
fi
(umask 077; : >> "$state_dir/deploy.lock")
exec 9<>"$state_dir/deploy.lock"
if ! flock -n 9; then
  echo "Another ocpp-reports deployment/prune holds the owner lock" >&2; exit 3
fi

# Read-only owner release inventory; no Ansible, root privileges or service changes.
if [ "${1:-}" = "--prune" ]; then
  shift
  if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--dry-run" ]; }; then
    echo "Usage: ./deploy.sh --prune [--dry-run]" >&2
    exit 2
  fi
  exec python3 scripts/release_prune.py --owner reports \
    --root "${OCPP_REPORTS_INSTALL_ROOT:-/opt/ocpp-reports}" \
    --cli "${OCPP_REPORTS_CLI_LINK:-/usr/local/bin/ocpp-reports}" --dry-run
fi

role=""
explicit_role=false
case "${1:-}" in
  --terminal|--satellite|--watchtower|--control)
    role="${1#--}"
    explicit_role=true
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

# Always reconcile the requested role. A linked release and an exit-zero CLI
# do not prove that post-activation Ansible tasks completed successfully.
# Ansible is idempotent; failed deployments must be retryable.
exec ansible-playbook -i localhost, -c local ansible/playbook.yml -e "reports_role=$role"
