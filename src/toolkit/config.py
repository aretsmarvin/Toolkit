"""Application configuration for Toolkit GUI."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List
import json


CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class AppBranding:
  """Branding information loaded from config.json."""

  app_name: str = "Toolkit"
  window_title: str = "Toolkit - Digital Forensics Toolkit"
  logo_path: str | None = None
  icon_path: str | None = None


@dataclass
class ExternalToolUpdate:
  """Configuration for how an external tool is updated."""

  type: str | None = None  # "url" or "command"
  url: str | None = None
  command: List[str] | None = None


@dataclass
class ExternalToolConfig:
  """Configuration for an external (non-Python) tool."""

  name: str
  description: str
  category: str = "External"
  command: List[str] | None = None
  update: ExternalToolUpdate | None = None


@dataclass
class AppConfig:
  """Top-level application configuration."""

  branding: AppBranding
  log_dir: Path
  external_tools: list[ExternalToolConfig]


def _load_raw_config() -> dict:
  if not CONFIG_FILE.exists():
    return {}
  with CONFIG_FILE.open("r", encoding="utf-8") as f:
    return json.load(f)


def load_config() -> AppConfig:
  """Load configuration from config.json, falling back to sane defaults.

  The config file allows changing the GUI name, logo, icon, and external
  tools without touching the Python sources.
  """

  raw = _load_raw_config()

  default_branding = AppBranding()
  branding_raw = raw.get("branding", {}) if isinstance(raw, dict) else {}
  branding = AppBranding(
    app_name=branding_raw.get("app_name", default_branding.app_name),
    window_title=branding_raw.get("window_title", default_branding.window_title),
    logo_path=branding_raw.get("logo_path", default_branding.logo_path),
    icon_path=branding_raw.get("icon_path", default_branding.icon_path),
  )

  log_dir = Path(home_dir := raw.get("logging", {}).get("log_dir", "~/.toolkit/logs"))
  log_dir = log_dir.expanduser().resolve()

  external_tools: list[ExternalToolConfig] = []
  for item in raw.get("external_tools", []):
    if not isinstance(item, dict):
      continue
    update_raw = item.get("update") or {}
    update = None
    if isinstance(update_raw, dict):
      update = ExternalToolUpdate(
        type=update_raw.get("type"),
        url=update_raw.get("url"),
        command=update_raw.get("command"),
      )
    external_tools.append(
      ExternalToolConfig(
        name=item.get("name", "External tool"),
        description=item.get("description", ""),
        category=item.get("category", "External"),
        command=item.get("command"),
        update=update,
      )
    )

  log_dir.mkdir(parents=True, exist_ok=True)
  return AppConfig(branding=branding, log_dir=log_dir, external_tools=external_tools)
