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
. /opt/kubengine/scripts/libredis.sh


# We add the copy from default config in the entrypoint to not break users 
# bypassing the setup.sh logic. If the file already exists do not overwrite (in
# case someone mounts a configuration file in /opt/kubengine/redis/etc)
debug "Copying files from $REDIS_DEFAULT_CONF_DIR to $REDIS_CONF_DIR"
cp -nr "$REDIS_DEFAULT_CONF_DIR"/. "$REDIS_CONF_DIR"

if [[ "$*" = *"/opt/kubengine/scripts/redis/run.sh"* || "$*" = *"/run.sh"* ]]; then
    info "** Starting Redis setup **"
    /opt/kubengine/scripts/redis/setup.sh
    info "** Redis setup finished! **"
fi

echo ""
exec "$@"
