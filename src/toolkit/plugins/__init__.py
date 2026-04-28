"""Plugin system for Toolkit.

Each plugin lives in its own module inside ``toolkit.plugins`` and exposes a
subclass of :class:`BasePlugin` named ``Plugin``. The GUI discovers and loads
these dynamically, so new tools can be added without modifying the core
application.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol


class PluginError(Exception):
  """Base exception for plugin-related errors."""


class PluginUIFactory(Protocol):
  """Callable that attaches this plugin's UI to a parent widget."""

  def __call__(self, parent) -> None:  # pragma: no cover - Qt runtime hook
    ...


@dataclass
class PluginMeta:
  """Metadata describing a plugin."""

  name: str
  description: str
  category: str


class BasePlugin:
  """Base class for all Toolkit plugins.

  Plugins should inherit from this class and implement
  :meth:`create_ui` to attach their widgets to the provided parent.
  """

  meta: PluginMeta

  def __init__(self, meta: PluginMeta) -> None:
    self.meta = meta

  def create_ui(self, parent) -> None:  # pragma: no cover - Qt runtime hook
    """Create and attach the plugin's UI to ``parent``.

    Parameters
    ----------
    parent:
      A Qt widget that the plugin should populate.
    """

    raise NotImplementedError


def discover_plugins() -> list[BasePlugin]:
  """Discover and instantiate all plugins in the ``plugins`` package."""

  plugins: list[BasePlugin] = []
  package = __name__
  package_path = Path(__file__).resolve().parent

  for path in sorted(package_path.glob("*.py")):
    if path.name in {"__init__.py"}:
      continue
    module_name = f"{package}.{path.stem}"
    try:
      module = import_module(module_name)
    except Exception:  # pragma: no cover - defensive
      import logging

      logging.getLogger(__name__).exception("Failed to import plugin %s", module_name)
      continue

    plugin_cls = getattr(module, "Plugin", None)
    if plugin_cls is None:
      continue

    try:
      plugin: BasePlugin = plugin_cls()
    except Exception:  # pragma: no cover - defensive
      import logging

      logging.getLogger(__name__).exception("Failed to instantiate plugin %s", module_name)
      continue

    plugins.append(plugin)

  return plugins
