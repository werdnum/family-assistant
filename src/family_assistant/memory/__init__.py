"""Conversation memory: the curated notes layer and its store invariants.

See docs/design/conversation-memory.md. Memory is a set of notes carrying the
``memory`` visibility label; the shape of that set (one always-loaded core
note, per-note caps, a trusted-pole provenance floor, a household store
revision) is enforced at the notes repository, so every writer -- curator,
foreground tool, web notes API -- is held to it.

Deliberately re-exports nothing: ``memory.limits`` is imported by
``storage.database``, and ``memory.invariants`` imports the notes table, so a
package-level re-export of either would close an import cycle. Import from the
submodules.
"""
