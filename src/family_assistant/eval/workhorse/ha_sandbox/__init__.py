"""A Home Assistant that looks like the household's, for the workhorse sandbox.

``custom_components/eval_home`` is a Home Assistant integration, not part of
the application: the launcher in :mod:`family_assistant.eval.workhorse.ha_server`
copies it into a sandbox ``hass`` configuration directory, where it serves the
household's entities with their production ids, names, states and attributes
from a seed file written beside it.
"""
