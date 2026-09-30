"""Bespoke integrations packaged as plugins.

A plugin declares everything an integration contributes in one place: its
config model, its tools, and what each configured instance supplies at runtime
(context providers, event sources). See
``docs/design/plugin-architecture.md``.

Plugin tool modules import ``family_assistant.tools.types``, and the tool
catalogue in ``family_assistant.tools`` includes every plugin's tools, so the
two packages import each other. Importing ``family_assistant.tools`` here first
makes that cycle resolve the same way whichever package a caller imports first.
"""

import family_assistant.tools  # noqa: F401  # pyright: ignore[reportUnusedImport]
