"""Application configuration."""

from dataclasses import dataclass


@dataclass
class Config:
    debug: bool = False
    max_upload_mb: int = 25
    cache_ttl_seconds: int = 300


def default_config() -> Config:
    return Config()
