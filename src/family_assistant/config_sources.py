"""Configuration sources and utilities for layered config loading.

This module contains:
- Deep merge utilities for combining dictionaries
- YAML file loading
- DeepMergedYamlSource for pydantic-settings integration

Extracted from config_loader.py to avoid circular imports with config_models.py.
"""

# ast-grep-ignore-block: no-dict-any - Config sources work with dynamic YAML/JSON data

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

import yaml
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

if TYPE_CHECKING:
    import pathlib

    from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__)


def _merge_dicts_inplace(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    """Recursively merge source into target in place.

    This is an internal helper that modifies target directly.
    """
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            _merge_dicts_inplace(target[key], value)
        else:
            target[key] = copy.deepcopy(value) if isinstance(value, dict) else value


def deep_merge_dicts(
    base_dict: dict[str, Any],
    merge_dict: dict[str, Any],
) -> dict[str, Any]:
    """Deeply merges merge_dict into base_dict.

    Args:
        base_dict: The base dictionary to merge into
        merge_dict: The dictionary to merge from (values take precedence)

    Returns:
        A new dictionary with deeply merged values
    """
    result = copy.deepcopy(base_dict)
    _merge_dicts_inplace(result, merge_dict)
    return result


def load_yaml_file(
    file_path: str | pathlib.Path,
) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict if not found.

    Args:
        file_path: Path to the YAML file

    Returns:
        The loaded YAML content as a dictionary, or empty dict if file not found
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = yaml.safe_load(f)
            if isinstance(content, dict):
                return content
            logger.warning(f"{file_path} is not a valid dictionary. Ignoring.")
            return {}
    except FileNotFoundError:
        logger.info(f"{file_path} not found. Using defaults.")
        return {}
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {file_path}: {e}. Using defaults.")
        return {}


class DeepMergedYamlSource(PydanticBaseSettingsSource):
    """Loads multiple YAML files and deep-merges them.

    Files are processed in order; later files override earlier ones at any
    nesting depth.
    """

    def __init__(self, settings_cls: type[BaseSettings], yaml_files: list[str]) -> None:
        super().__init__(settings_cls)
        self.yaml_files = yaml_files

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        """Required by PydanticBaseSettingsSource. Not used since __call__ provides all values."""
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for path in self.yaml_files:
            data = load_yaml_file(path)
            if data:
                merged = deep_merge_dicts(merged, data)
        return merged
