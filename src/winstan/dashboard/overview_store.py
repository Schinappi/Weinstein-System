from __future__ import annotations

import json
from pathlib import Path


class OverviewStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, target_date: str) -> dict[str, object] | None:
        path = self._path_for(target_date)
        if not path.exists() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def save(self, target_date: str, payload: dict[str, object]) -> None:
        path = self._path_for(target_date)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_dates(self) -> list[str]:
        return sorted(
            (path.stem for path in self.root.glob("*.json")),
            reverse=True,
        )

    def _path_for(self, target_date: str) -> Path:
        safe_date = str(target_date or "").strip() or "unknown-date"
        return self.root / f"{safe_date}.json"
