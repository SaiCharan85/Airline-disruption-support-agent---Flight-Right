"""Procedural playbooks per disruption type (FR-10.4), looked up by key."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from flightright.config import ROOT


class Playbook(BaseModel):
    disruption_type: str
    text: str
    version: str  # content hash, recorded with each draft


class Playbooks:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or ROOT / "config" / "playbooks"
        self._cache: dict[str, Playbook] = {}

    def get(self, disruption_type: str) -> Playbook:
        if disruption_type not in self._cache:
            path = self.directory / f"{disruption_type}.md"
            if not path.is_file():
                raise KeyError(f"no playbook for {disruption_type!r}")
            text = path.read_text(encoding="utf-8")
            self._cache[disruption_type] = Playbook(
                disruption_type=disruption_type,
                text=text,
                version=hashlib.sha256(text.encode()).hexdigest()[:12],
            )
        return self._cache[disruption_type]
