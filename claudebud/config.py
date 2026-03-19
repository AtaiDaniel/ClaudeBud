"""
config.py — load/save ~/.claudebud/config.json
"""
import json
from pathlib import Path

DEFAULTS = {
    "port": 3131,
    "vapid_private_key": "",
    "vapid_public_key":  "",
    "push_subscription": {},
    "prompt_patterns": [
        r"\(Y/n\)",
        r"\(y/N\)",
        r"\(yes/no\)",
        r"Allow",
        r"Approve",
        r"Do you want to",
        r"Press Enter",
        r"Continue\?",
    ],
    "completion_patterns": [
        r"✓ Completed",
        r"Task complete",
        r"Done\.",
        r"Finished",
        r"All done",
    ],
    "max_scrollback_lines": 2000,
}


def get_config_path() -> Path:
    return Path.home() / ".claudebud" / "config.json"


def load_config() -> dict:
    """Load config from disk, creating defaults if the file doesn't exist."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        save_config(DEFAULTS)
        return dict(DEFAULTS)

    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Fill in any keys that are missing (e.g. after an upgrade)
    changed = False
    for key, value in DEFAULTS.items():
        if key not in cfg:
            cfg[key] = value
            changed = True

    # Remove keys that no longer exist (e.g. ntfy_topic/ntfy_server after migration)
    _removed = {"ntfy_topic", "ntfy_server"}
    for key in _removed:
        if key in cfg:
            del cfg[key]
            changed = True

    if changed:
        save_config(cfg)

    return cfg


def save_config(cfg: dict) -> None:
    """Write config to disk."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
