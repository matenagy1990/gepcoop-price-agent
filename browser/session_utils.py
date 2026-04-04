import json
from datetime import datetime
from pathlib import Path
from typing import Any


def load_session(path: Path) -> dict[str, Any] | None:
    """Load a persisted session wrapper.

    Supported formats:
    - {"saved_at": "...", "state": {...}}  current format
    - {"cookies": [...], "origins": [...]} legacy raw Playwright storage state
    """
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "state" in data:
            return data
        if isinstance(data, dict) and {"cookies", "origins"} <= set(data.keys()):
            return {"saved_at": None, "state": data}
    except Exception:
        return None
    return None


def session_is_fresh(session: dict[str, Any], max_age_hours: int) -> bool:
    saved_at = session.get("saved_at")
    if not saved_at:
        return False
    try:
        age_h = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 3600
        return age_h < max_age_hours
    except Exception:
        return False


def invalidate_session(path: Path) -> None:
    path.unlink(missing_ok=True)


async def save_session(context: Any, path: Path) -> None:
    state = await context.storage_state()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"saved_at": datetime.now().isoformat(timespec="seconds"), "state": state},
            indent=2,
        )
    )
