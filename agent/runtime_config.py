import logging
import os
from pathlib import Path

from dotenv import dotenv_values

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).parent.parent / ".env"


def get_runtime_env(key: str, default: str = "") -> str:
    """Read a config value from the current .env file, falling back to process env."""
    if ENV_FILE.exists():
        try:
            value = dotenv_values(ENV_FILE).get(key)
            if value is not None:
                return str(value)
        except Exception as exc:
            log.warning("%s nem olvasható a .env fájlból: %s", key, exc)
    return os.environ.get(key, default)
