"""Runtime settings and filesystem layout.

Deliberately small. roleradar has no API keys, no provider selection, and no
encrypted store, so there is nothing secret here - only paths and a couple of
knobs. Everything the user actually chooses (fields, terms, skills, schedule)
lives in `data/preferences.json`, not in environment variables.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_root() -> Path:
    """Repo root when running from a checkout, else the user's data dir.

    A cloned repo keeps its database beside the source, which is what a local
    tool should do. An installed package has no repo to write into, so it falls
    back to the platform convention.
    """
    env = os.environ.get("ROLERADAR_HOME")
    if env:
        return Path(env).expanduser()

    # settings.py -> roleradar -> src -> repo root
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "pyproject.toml").exists():
        return repo_root

    return Path.home() / ".roleradar"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROLERADAR_", extra="ignore")

    host: str = "127.0.0.1"
    # Not 8000: that port is contested on any machine with another API on it,
    # and a health check answered by somebody else's server is worse than no
    # health check at all.
    port: int = 8100
    log_level: str = "INFO"

    @property
    def root(self) -> Path:
        return _default_root()

    @property
    def data_dir(self) -> Path:
        """Local runtime data: the listing DB, preferences, and digests."""
        path = self.root / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def config_dir(self) -> Path:
        """Shipped, user-editable YAML: taxonomy, sources, companies."""
        return self.root / "config"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "roleradar.db"

    @property
    def preferences_path(self) -> Path:
        return self.data_dir / "preferences.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
