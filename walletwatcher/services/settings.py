"""Shared configuration reference; services always observe current access settings."""
from dataclasses import dataclass

from walletwatcher.config import Config


@dataclass
class Settings:
    config: Config
