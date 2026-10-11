from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.settings import ROOT

PATH = ROOT / "data" / "mailings.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    if not PATH.exists():
        return {"drafts": []}
    try:
        data = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {"drafts": []}
    if not isinstance(data, dict):
        return {"drafts": []}
    drafts = data.get("drafts")
    if not isinstance(drafts, list):
        drafts = []
    return {"drafts": drafts}


def save_draft(chat: str, theme: str, text: str) -> None:
    payload = load()
    payload["drafts"].insert(
        0,
        {
            "chat": chat.strip(),
            "theme": theme.strip(),
            "text": text.strip(),
            "at": _now(),
        },
    )
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def delete_draft(index: int) -> None:
    payload = load()
    if 0 <= index < len(payload["drafts"]):
        payload["drafts"].pop(index)
        PATH.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
