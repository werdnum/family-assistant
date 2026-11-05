#!/bin/bash
# The fallback test lock for hosts without flock (macOS). Sourced by
# run-tests.sh; a test sources it directly.
#
# flock releases itself when its holder dies. This lock is a directory, which
# outlives a holder killed with SIGKILL -- as an agent harness does to a test
# run it times out -- because no trap runs. So the holder records its pid and
# start time, and a later run reclaims the lock once that process is gone. The
# start time stops a recycled pid from passing for the holder.

# Sets LOCK_DIR_ACQUIRED=1 when the lock is taken; the caller's EXIT trap
# removes the directory on any exit a trap can see.
LOCK_DIR_ACQUIRED=0

# Whitespace-normalised: ps pads the field, and the pid file is read back with
# `read`, which trims. Compared raw, a live holder would never match itself.
process_start_time() {
    ps -o lstart= -p "$1" 2>/dev/null | awk '{$1 = $1; print}'
}

# Whether the holder recorded in a lock directory's pid file is still running.
lock_holder_alive() {
    local pid_file="$1/pid" holder="" started=""
    if [ ! -f "$pid_file" ]; then
        # mkdir and the pid write are two steps, so a fresh lock briefly has no
        # pid. Only one that has stayed empty for a minute is abandoned.
        [ -z "$(find "$1" -maxdepth 0 -mmin +1 2>/dev/null)" ]
        return
    fi
    { read -r holder; read -r started; } < "$pid_file"
    [ -n "$holder" ] || return 1
    if [ -n "$started" ]; then
        [ "$(process_start_time "$holder")" = "$started" ]
    else
        kill -0 "$holder" 2>/dev/null
    fi
}

# Removes the lock directory when its holder is gone. Renaming first means only
# one waiter can claim a given stale lock; if a live holder took the lock
# between the check and the rename, it is put back.
reclaim_stale_lock_dir() {
    local lock_dir="$1" claimed="$1.stale.$$"
    lock_holder_alive "$lock_dir" && return 1
    mv "$lock_dir" "$claimed" 2>/dev/null || return 1
    if lock_holder_alive "$claimed"; then
        mv "$claimed" "$lock_dir" 2>/dev/null || rm -rf "$claimed"
        return 1
    fi
    local holder=""
    [ -f "$claimed/pid" ] && read -r holder < "$claimed/pid"
    rm -rf "$claimed"
    echo "Reclaimed a test lock left by PID ${holder:-unknown}, which is no longer running."
}

# One attempt at the lock, reclaiming it from a holder that has gone.
try_acquire_lock_dir() {
    local lock_dir="$1"
    if ! mkdir "$lock_dir" 2>/dev/null; then
        reclaim_stale_lock_dir "$lock_dir" || return 1
        mkdir "$lock_dir" 2>/dev/null || return 1
    fi
    printf '%s\n%s\n' "$$" "$(process_start_time "$$")" > "$lock_dir/pid"
    # shellcheck disable=SC2034  # read by the sourcing script's EXIT trap
    LOCK_DIR_ACQUIRED=1
}
