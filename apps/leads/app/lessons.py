from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.settings import ROOT

LESSONS_PATH = ROOT / "data" / "reject-lessons.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_lessons(limit: int = 30) -> list[str]:
    if not LESSONS_PATH.exists():
        return []
    try:
        data = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = data.get("lessons") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    reasons: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            reasons.append(item.strip())
        elif isinstance(item, dict) and item.get("reason"):
            reasons.append(str(item["reason"]).strip())
    # последние важнее
    uniq: list[str] = []
    for reason in reversed(reasons):
        if reason not in uniq:
            uniq.append(reason)
    return uniq[:limit]


def add_lesson(reason: str, lead_id: int | None = None) -> None:
    reason = (reason or "").strip()
    if not reason:
        return
    LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"lessons": []}
    if LESSONS_PATH.exists():
        try:
            loaded = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("lessons"), list):
                payload = loaded
        except json.JSONDecodeError:
            pass
    payload["lessons"].append(
        {
            "reason": reason,
            "lead_id": lead_id,
            "at": _now(),
        }
    )
    LESSONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
