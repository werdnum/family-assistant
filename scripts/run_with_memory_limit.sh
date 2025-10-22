#!/bin/bash
#
# A wrapper script to run a command with a memory limit derived from the
# pod's available cgroup memory, supporting both cgroups v1 and v2.

set -e

# --- Configuration ---
# Safety margin in Megabytes (MB). This much memory will be left free.
# Override by setting the SAFETY_MARGIN_MB environment variable.
: "${SAFETY_MARGIN_MB:=32}"

# Check if a command was provided
if [ "$#" -eq 0 ]; then
    echo "Error: No command specified." >&2
    echo "Usage: $0 <command> [args...]" >&2
    exit 1
fi

# --- cgroup Detection and Path Setup ---
MEM_LIMIT_FILE=""
MEM_USAGE_FILE=""

# Detect cgroup version and set paths to memory files
if [ -f "/sys/fs/cgroup/memory.max" ]; then
    # cgroup v2
    echo "INFO: Detected cgroup v2." >&2
    MEM_LIMIT_FILE="/sys/fs/cgroup/memory.max"
    MEM_USAGE_FILE="/sys/fs/cgroup/memory.current"
elif [ -f "/sys/fs/cgroup/memory/memory.limit_in_bytes" ]; then
    # cgroup v1
    echo "INFO: Detected cgroup v1." >&2
    MEM_LIMIT_FILE="/sys/fs/cgroup/memory/memory.limit_in_bytes"
    MEM_USAGE_FILE="/sys/fs/cgroup/memory/memory.usage_in_bytes"
else
    echo "WARNING: Could not find cgroup memory files. Running command without a new memory limit." >&2
    exec "$@"
fi

# --- Read Memory Values ---
pod_limit_bytes=$(cat "${MEM_LIMIT_FILE}")
pod_usage_bytes=$(cat "${MEM_USAGE_FILE}")

# In cgroup v2, "max" means unlimited. In v1, it's a huge integer.
# We'll treat any value > 2^60 as unlimited.
if [ "$pod_limit_bytes" = "max" ] || [ "$pod_limit_bytes" -gt 1152921504606846976 ]; then
    echo "WARNING: Pod has no memory limit set. Running command without a new memory limit." >&2
    exec "$@"
fi

# --- Calculation ---
safety_margin_bytes=$((SAFETY_MARGIN_MB * 1024 * 1024))
child_limit_bytes=$((pod_limit_bytes - pod_usage_bytes - safety_margin_bytes))

# If calculated limit is not positive, there isn't enough memory.
if [ "$child_limit_bytes" -le 0 ]; then
    echo "ERROR: Not enough memory to run command with a ${SAFETY_MARGIN_MB}MB safety margin." >&2
    echo "  Pod Limit:   $((pod_limit_bytes / 1024 / 1024)) MB" >&2
    echo "  Pod Usage:   $((pod_usage_bytes / 1024 / 1024)) MB" >&2
    echo "  Available:   $(((pod_limit_bytes - pod_usage_bytes) / 1024 / 1024)) MB" >&2
    exit 1
fi

# Convert bytes to kilobytes for `ulimit`
child_limit_kb=$((child_limit_bytes / 1024))

# --- Execution ---
echo "INFO: Setting memory limit for command to ${child_limit_kb} KB." >&2

# Export the limit as an environment variable for applications that can use it.
export DYNAMIC_MEM_LIMIT_BYTES=$child_limit_bytes
export DYNAMIC_MEM_LIMIT_KB=$child_limit_kb

# Apply the virtual memory limit (in KB). This is a hard limit enforced by the kernel.
ulimit -v "$child_limit_kb"

# Replace this script with the user's command.
exec "$@"
