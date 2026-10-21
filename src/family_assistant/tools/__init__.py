"""Tools the Family Assistant offers the LLM, organised into thematic submodules.

The package itself is deliberately empty: the catalogue of local tools lives in
:mod:`family_assistant.tools.catalog`. Every tool module and every plugin is
imported by that catalogue, so keeping it out of the package ``__init__`` means
importing a leaf such as :mod:`family_assistant.tools.types` does not pull in
the whole application and close an import cycle.
"""
