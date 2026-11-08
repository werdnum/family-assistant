#!/bin/bash
# Remove idle git worktrees of this repository, and the local-disk deps that
# scripts/link-local-deps.sh made for checkouts that no longer exist.
#
# A worktree is removed only when all of these hold:
#   - it is not the main checkout and is not locked (an active Claude Code
#     session locks its worktree);
#   - it has no uncommitted changes and no untracked, non-ignored files;
#   - git has not touched it for --min-idle-hours (default 12);
#   - its branch's pull request is merged or closed, or, with no pull request,
#     HEAD is already contained in origin/main.
# Before removal HEAD is saved as refs/worktree-backups/<name> whenever it is
# not in origin/main, so committed work stays recoverable after the branch is
# deleted. Dry run by default; pass --apply to act.
set -euo pipefail

apply=false
min_idle_hours=12
while [ $# -gt 0 ]; do
    case "$1" in
    --apply) apply=true ;;
    --min-idle-hours)
        min_idle_hours="$2"
        shift
        ;;
    *)
        echo "usage: $0 [--apply] [--min-idle-hours N]" >&2
        exit 2
        ;;
    esac
    shift
done

main_root="$(dirname "$(cd "$(git rev-parse --git-common-dir)" && pwd)")"
repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
git -C "$main_root" fetch -q origin main
now="$(date +%s)"

# git's recursive delete sometimes fails on the macOS VM's virtiofs share with
# "Directory not empty"; finish the job with rm and drop git's record of it.
remove_worktree() {
    git -C "$main_root" worktree remove --force "$1" && return 0
    rm -rf "$1" && git -C "$main_root" worktree prune
}

run() {
    if [ "$apply" = true ]; then "$@"; else echo "    would run: $*"; fi
}

git -C "$main_root" worktree list --porcelain | awk '
/^worktree /{sub(/^worktree /, ""); path = $0}
BEGIN{branch = "-"; locked = "no"}
/^branch /{branch = $2; sub("refs/heads/", "", branch)}
/^locked/{locked = "yes"}
/^$/{print path "\t" branch "\t" locked; branch = "-"; locked = "no"}' |
    while IFS=$'\t' read -r path branch locked; do
        [ "$path" = "$main_root" ] && continue
        [ "$branch" = "-" ] && branch=""
        name="$(basename "$path")"
        if [ ! -d "$path" ]; then
            echo "gone:  $path"
            continue
        fi
        if [ "$locked" = "yes" ]; then
            echo "keep:  $name (locked)"
            continue
        fi
        if [ -n "$(git -C "$path" --no-optional-locks status --porcelain)" ]; then
            echo "keep:  $name (uncommitted changes)"
            continue
        fi
        # The HEAD reflog records this worktree's last checkout, commit or
        # reset; the index is no signal, since any `git status` rewrites it.
        idle_hours=$(((now - $(git -C "$path" log -g -1 --format=%ct HEAD)) / 3600))
        if [ "$idle_hours" -lt "$min_idle_hours" ]; then
            echo "keep:  $name (active ${idle_hours}h ago)"
            continue
        fi
        head="$(git -C "$path" rev-parse HEAD)"
        in_main=false
        git -C "$path" merge-base --is-ancestor "$head" origin/main && in_main=true
        pr_states=""
        if [ -n "$branch" ]; then
            pr_states="$(gh pr list -R "$repo" --head "$branch" --state all --json state -q '[.[].state] | join(",")')"
        fi
        case "$pr_states" in
        *OPEN*)
            echo "keep:  $name (open PR on $branch)"
            continue
            ;;
        "")
            if [ "$in_main" = false ]; then
                echo "keep:  $name (commits not in main and no PR)"
                continue
            fi
            ;;
        esac
        echo "prune: $name (${branch:-detached}, PR: ${pr_states:-none}, idle ${idle_hours}h)"
        if [ "$in_main" = false ]; then
            run git -C "$main_root" update-ref "refs/worktree-backups/$name" "$head"
        fi
        if ! run remove_worktree "$path"; then
            echo "    failed to remove $path; leaving its branch" >&2
            continue
        fi
        if [ -n "$branch" ]; then
            run git -C "$main_root" branch -q -D "$branch"
        fi
    done

run git -C "$main_root" worktree prune

deps_root="${FA_LOCAL_DEPS_DIR:-$HOME/.cache/fa-worktree-deps}"
if [ -d "$deps_root/checkouts" ]; then
    for dir in "$deps_root/checkouts"/*/; do
        [ -f "$dir/checkout-path" ] || continue
        checkout="$(cat "$dir/checkout-path")"
        # In a dry run the worktrees above still exist, so this lists only deps
        # that are already orphaned.
        if [ ! -d "$checkout" ]; then
            echo "prune deps: $dir ($checkout is gone)"
            run rm -rf "$dir"
        fi
    done
fi
if [ -d "$deps_root/npm-templates" ]; then
    in_use="$(cat "$deps_root"/checkouts/*/node_modules.lock-key 2>/dev/null || true)"
    for template in "$deps_root/npm-templates"/*/; do
        key="$(basename "$template")"
        if ! grep -qx "$key" <<<"$in_use"; then
            echo "prune npm template: $key (no checkout uses it)"
            run rm -rf "$template"
        fi
    done
fi
