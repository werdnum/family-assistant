"""
Family Assistant Scripting System.

Provides scripting engines for executing user-defined scripts
with access to family assistant tools and state.

The package itself re-exports nothing: the engine imports the tool
infrastructure, which imports the script API types from this package, so
pulling the engine in here would close an import cycle. Import from the
submodules directly.
"""
