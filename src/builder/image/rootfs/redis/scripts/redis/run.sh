#!/bin/bash
# Copyright Broadcom, Inc. All Rights Reserved.
# SPDX-License-Identifier: APACHE-2.0

# shellcheck disable=SC1091

set -o errexit
set -o nounset
set -o pipefail
# set -o xtrace # Uncomment this line for debugging purposes

# Load Redis environment variables
. /opt/kubengine/scripts/redis-env.sh

# Load libraries
. /opt/kubengine/scripts/libos.sh
. /opt/kubengine/scripts/libredis.sh

# Parse CLI flags to pass to the 'redis-server' call
args=("${REDIS_BASE_DIR}/etc/redis.conf" "--daemonize" "no")
# Add flags specified via the 'REDIS_EXTRA_FLAGS' environment variable
read -r -a extra_flags <<< "$REDIS_EXTRA_FLAGS"
[[ "${#extra_flags[@]}" -gt 0 ]] && args+=("${extra_flags[@]}")
# Add flags passed to this script
args+=("$@")

info "** Starting Redis **"
if am_i_root; then
    exec_as_user "$REDIS_DAEMON_USER" redis-server "${args[@]}"
else
    exec redis-server "${args[@]}"
fi
