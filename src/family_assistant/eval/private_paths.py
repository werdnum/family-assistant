"""Containment rule for household-derived evaluation material.

Everything the review-eval tooling derives from this household — task templates
extracted from message history, and the full run records whose per-trial reasons
quote whatever was reviewed — belongs inside the repository's one gitignored
tree. The rule has a single definition here so the extraction script and the
harness CLI cannot drift from each other, and it lives in the eval package
rather than in the application's configuration models because nothing the
running application does consults it.
"""

from __future__ import annotations

import os
from pathlib import Path

from family_assistant.paths import PROJECT_ROOT

__all__ = [
    "PRIVATE_EVAL_DIR_NAME",
    "PrivateEvalPathError",
    "anchor_private_eval_path",
    "resolve_private_eval_path",
]

PRIVATE_EVAL_DIR_NAME = ".review-eval-local"
"""The one directory ``.gitignore`` ignores, as a repository-root-anchored path."""


class PrivateEvalPathError(ValueError):
    """A path for household-derived eval material lands outside the private tree."""


def anchor_private_eval_path(path: str | Path) -> Path:
    """Return ``path`` as an absolute, symlink-resolved repository-anchored path.

    Relative paths anchor at the repository root, never at the process working
    directory: a script started from elsewhere would otherwise pass a
    containment check and then write household-derived content somewhere the
    check never looked.

    Both steps are load-bearing. ``..`` is collapsed lexically *first*, so a
    traversal cannot walk out of the private tree by way of a symlinked parent's
    target. The result is then resolved, because a lexical check alone treats
    ``.review-eval-local/templates`` as contained even when it is a symlink into
    a tracked directory and the write follows the link. Resolution is
    non-strict: what exists is resolved and the rest stays lexical, so the
    destination still need not exist yet.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return Path(os.path.normpath(candidate)).resolve()


def _private_eval_root() -> Path:
    """Return the resolved private tree root, rejecting a symlinked marker.

    A symlinked *parent* of the checkout stays supported — resolving it is what
    lets a repository reached through a linked parent accept its own private
    tree — so only the marker directory's own link status is examined.
    """
    marker = PROJECT_ROOT / PRIVATE_EVAL_DIR_NAME
    if marker.is_symlink():
        raise PrivateEvalPathError(
            f"cannot be contained: the private eval root {marker} is a symlink "
            f"to {marker.readlink()}, so containment cannot tell whether a write "
            "lands in tracked space. Replace it with a real directory; a "
            "destination outside the tree has to be named explicitly rather than "
            "reached through a link"
        )
    return anchor_private_eval_path(PRIVATE_EVAL_DIR_NAME)


def resolve_private_eval_path(path: str | Path) -> Path:
    """Anchor ``path`` and require it to land inside the private eval tree.

    Every writer of household-derived eval material — history-derived templates,
    run records, anything added later — resolves its destination through here,
    so the containment rule has one definition instead of one per writer. What
    is checked is containment in ``<repo root>/.review-eval-local/``, not the
    presence of the marker name: ``.gitignore`` ignores only the root-anchored
    path, so ``nested/.review-eval-local/templates`` carries the name yet sits
    in a tracked directory, and ``.review-eval-local/../out`` names it while
    resolving outside. Both raise, as does a path whose existing components
    symlink out of the tree. The private root is resolved the same way as the
    candidate, so a repository checked out under a symlinked parent still
    accepts its own private tree.

    The marker directory itself must be a real one — :func:`_private_eval_root`
    refuses a symlinked marker before containment is evaluated, because
    candidate and root would otherwise resolve under the same target and every
    write would pass on its way into a commit-visible location.
    """
    resolved = anchor_private_eval_path(path)
    private_root = _private_eval_root()
    if not resolved.is_relative_to(private_root):
        raise PrivateEvalPathError(
            f"must resolve to a location inside {private_root} (the gitignored "
            f"tree that holds raw household content); got {str(path)!r}, which "
            f"resolves to {resolved}"
        )
    return resolved
