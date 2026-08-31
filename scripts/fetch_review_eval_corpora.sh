#!/usr/bin/env bash

# Fetch pinned public corpora into the private review-eval tree.
#
# This deliberately fetches only the files needed by the adapters. The output
# path is fixed below the current worktree's .review-eval-local directory, so
# an invocation cannot accidentally place raw corpus content in a tracked or
# caller-selected directory.

set -Eeuo pipefail

die() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || die "git is required"
git lfs version >/dev/null 2>&1 || die "git-lfs is required"
command -v shasum >/dev/null 2>&1 || die "shasum is required"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
LOCAL_ROOT="$REPO_ROOT/.review-eval-local"
CORPUS_ROOT="$LOCAL_ROOT/upstream"
MANIFEST="$CORPUS_ROOT/manifest.txt"

mkdir -p "$LOCAL_ROOT"

resolve_main_revision() {
    local remote="$1"
    local revision
    revision="$(git ls-remote "$remote" refs/heads/main | awk 'NR == 1 {print $1}')"
    [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "could not resolve main revision for $remote"
    printf '%s\n' "$revision"
}

DEEPSET_REMOTE="https://huggingface.co/datasets/deepset/prompt-injections.git"
INJECAGENT_REMOTE="https://github.com/uiuc-kang-lab/InjecAgent.git"
DEEPSET_REV="$(resolve_main_revision "$DEEPSET_REMOTE")"
INJECAGENT_REV="$(resolve_main_revision "$INJECAGENT_REMOTE")"

[[ ! -e "$CORPUS_ROOT" && ! -L "$CORPUS_ROOT" ]] || die "refusing to overwrite existing $CORPUS_ROOT"

STAGING_ROOT="$(mktemp -d "$LOCAL_ROOT/.upstream-staging.XXXXXX")"
cleanup() {
    if [[ -n "${STAGING_ROOT:-}" && -e "$STAGING_ROOT" ]]; then
        rm -rf -- "$STAGING_ROOT"
    fi
}
trap cleanup EXIT

DEEPSET_STAGE="$STAGING_ROOT/deepset"
INJECAGENT_STAGE="$STAGING_ROOT/InjecAgent"
STAGING_MANIFEST="$STAGING_ROOT/manifest.txt"

GIT_LFS_SKIP_SMUDGE=1 git clone \
    --filter=blob:none \
    --no-checkout \
    --quiet \
    "$DEEPSET_REMOTE" \
    "$DEEPSET_STAGE"
git -C "$DEEPSET_STAGE" sparse-checkout init --no-cone
git -C "$DEEPSET_STAGE" sparse-checkout set \
    '/data/*.parquet' \
    '/README.md'
git -C "$DEEPSET_STAGE" fetch --depth=1 --quiet origin "$DEEPSET_REV"
git -C "$DEEPSET_STAGE" checkout --quiet --detach "$DEEPSET_REV"
git -C "$DEEPSET_STAGE" lfs pull --include='data/*.parquet'

git clone \
    --filter=blob:none \
    --no-checkout \
    --quiet \
    "$INJECAGENT_REMOTE" \
    "$INJECAGENT_STAGE"
git -C "$INJECAGENT_STAGE" sparse-checkout init --no-cone
git -C "$INJECAGENT_STAGE" sparse-checkout set \
    /data/test_cases_dh_base.json \
    /data/test_cases_ds_base.json \
    /data/test_cases_dh_enhanced.json \
    /data/test_cases_ds_enhanced.json \
    /README.md \
    /LICENCE
git -C "$INJECAGENT_STAGE" checkout --quiet --detach "$INJECAGENT_REV"

sha256_for() {
    local file="$1"
    local relative="$2"
    printf '%s  ' "$relative"
    shasum -a 256 "$file" | awk '{print $1}'
}

{
    printf '# Review-eval corpus fetch manifest\n'
    printf 'deepset.revision=%s\n' "$DEEPSET_REV"
    printf 'deepset.upstream=%s\n' "https://huggingface.co/datasets/deepset/prompt-injections"
    for file in "$DEEPSET_STAGE"/data/*.parquet; do
        [[ -f "$file" ]] || die "deepset Parquet download produced no data files"
        sha256_for "$file" "deepset/data/${file##*/}"
    done
    [[ -f "$DEEPSET_STAGE/README.md" ]] || die "deepset README download failed"
    sha256_for "$DEEPSET_STAGE/README.md" "deepset/README.md"
    if [[ -f "$DEEPSET_STAGE/LICENSE" ]]; then
        sha256_for "$DEEPSET_STAGE/LICENSE" "deepset/LICENSE"
    else
        printf 'deepset.license_file=absent-at-revision\n'
    fi
    printf 'injecagent.revision=%s\n' "$INJECAGENT_REV"
    printf 'injecagent.upstream=%s\n' "$INJECAGENT_REMOTE"
    for file in \
        test_cases_dh_base.json \
        test_cases_dh_enhanced.json \
        test_cases_ds_base.json \
        test_cases_ds_enhanced.json; do
        [[ -f "$INJECAGENT_STAGE/data/$file" ]] || die "InjecAgent $file download failed"
        sha256_for "$INJECAGENT_STAGE/data/$file" "InjecAgent/data/$file"
    done
    [[ -f "$INJECAGENT_STAGE/README.md" ]] || die "InjecAgent README download failed"
    [[ -f "$INJECAGENT_STAGE/LICENCE" ]] || die "InjecAgent LICENCE download failed"
    sha256_for "$INJECAGENT_STAGE/README.md" "InjecAgent/README.md"
    sha256_for "$INJECAGENT_STAGE/LICENCE" "InjecAgent/LICENCE"
} >"$STAGING_MANIFEST"

mv "$STAGING_ROOT" "$CORPUS_ROOT"
STAGING_ROOT=""

printf 'Fetched pinned review-eval corpora under %s\n' "$CORPUS_ROOT"
printf 'Recorded revisions and SHA-256 checksums in %s\n' "$MANIFEST"
