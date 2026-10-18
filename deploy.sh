#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec ansible-playbook -i localhost, -c local ansible/playbook.yml "$@"
