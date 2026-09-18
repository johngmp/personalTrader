"""A file on disk that blocks all new orders while it exists (`trader halt` / `trader resume`)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class KillSwitch:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def active(self) -> bool:
        return self.path.exists()

    @property
    def reason(self) -> str:
        return self.path.read_text().strip() if self.active else ""

    def engage(self, reason: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.write_text(f"{stamp} {reason}\n")

    def release(self) -> None:
        self.path.unlink(missing_ok=True)
