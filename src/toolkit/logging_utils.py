"""Logging utilities for Toolkit GUI."""
from __future__ import annotations

from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

from .config import AppConfig


def configure_logging(config: AppConfig) -> None:
  """Configure application-wide logging.

  Logs are written to the directory specified in the configuration and
  rotated to keep file sizes manageable.
  """

  log_dir: Path = config.log_dir
  log_dir.mkdir(parents=True, exist_ok=True)
  log_file = log_dir / "toolkit.log"

  root_logger = logging.getLogger()
  root_logger.setLevel(logging.DEBUG)

  console_handler = logging.StreamHandler()
  console_handler.setLevel(logging.INFO)
  console_formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
  console_handler.setFormatter(console_formatter)

  file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)
  file_handler.setLevel(logging.DEBUG)
  file_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s",
  )
  file_handler.setFormatter(file_formatter)

  root_logger.handlers.clear()
  root_logger.addHandler(console_handler)
  root_logger.addHandler(file_handler)
