"""事件存储：append-only 日志、哈希链、JSON 持久化与重放。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .events import Event, GENESIS, event_hash, verify_chain


class EventStore:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else None
        self._events: list[Event] = []
        self._ids: set[str] = set()
        if self.path and self.path.exists():
            self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._events = [Event.from_dict(d) for d in raw]
        verify_chain(self._events)
        self._ids = {e.event_id for e in self._events}

    def _flush(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([e.to_dict() for e in self._events],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # ---------- 追加 ----------

    def has_id(self, event_id: str) -> bool:
        return event_id in self._ids

    def append(self, event_id: str, type_: str, timestamp: float, data: dict[str, Any],
               actor: str, role: str, accepted_at: float) -> Event:
        """追加一条事件。event_id 重复直接拒绝（幂等键，重复扫码不产生消耗）。"""
        if event_id in self._ids:
            raise KeyError(event_id)
        prev = self._events[-1].hash if self._events else GENESIS
        payload = {
            "event_id": event_id, "type": type_, "timestamp": timestamp,
            "data": data, "actor": actor, "role": role,
            "accepted_at": accepted_at,
        }
        h = event_hash(prev, payload)
        ev = Event(
            seq=len(self._events), event_id=event_id, type=type_,
            timestamp=timestamp, data=data, actor=actor, role=role,
            accepted_at=accepted_at, prev_hash=prev, hash=h,
        )
        self._events.append(ev)
        self._ids.add(event_id)
        self._flush()
        return ev

    # ---------- 读取 ----------

    def events(self) -> list[Event]:
        return list(self._events)

    def verify(self) -> None:
        verify_chain(self._events)
