#!/bin/bash
# Copyright Broadcom, Inc. All Rights Reserved.
# SPDX-License-Identifier: APACHE-2.0

# shellcheck disable=SC1091

set -o errexit
set -o nounset
set -o pipefail
# set -o xtrace # Uncomment this line for debugging purposes

# Load Redis Sentinel environment variables
. /opt/kubengine/scripts/redis-sentinel-env.sh

# Load libraries
. /opt/kubengine/scripts/libredissentinel.sh
. /opt/kubengine/scripts/liblog.sh

if [[ "$*" == *"/opt/kubengine/scripts/redis-sentinel/run.sh"* ]]; then
    info "** Starting Redis sentinel setup **"
    /opt/kubengine/scripts/redis-sentinel/setup.sh
    info "** Redis sentinel setup finished! **"
fi

echo ""
exec "$@"
