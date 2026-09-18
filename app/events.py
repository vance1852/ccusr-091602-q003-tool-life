"""事件层：append-only 事件日志、哈希链、幂等键与进程重启重放。

每个事件携带：

* ``seq``        日志内全局严格递增序号
* ``event_id``   内容 UUID，重复提交可被识别
* ``dedup_key``  业务幂等键（扫码/加工回执/迟到回执/调拨单据……）
* ``actor`` / ``role``  谁以什么角色操作
* ``occurred_at`` 事件实际发生时间（UTC ISO）
* ``prev_hash`` / ``hash`` 与前一事件构成哈希链，任何篡改在重放时暴露

事件只追加、不修改、不删除；所有状态都是重放产物，进程重启不产生分叉。
多进程共用同一日志时通过旁路锁文件加 ``flock`` 串行化写入。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

try:
    import fcntl  # POSIX 进程间文件锁
except ImportError:  # pragma: no cover - 非 POSIX 平台降级为仅线程锁
    fcntl = None

from .domain import TamperDetected

GENESIS_HASH = "0" * 64
HASH_ALGO = "sha256"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    dedup_key: Optional[str]
    etype: str
    payload: dict
    actor: str
    role: str
    occurred_at: str
    recorded_at: str
    prev_hash: str
    hash: str

    def as_line(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "event_id": self.event_id,
                "dedup_key": self.dedup_key,
                "etype": self.etype,
                "payload": self.payload,
                "actor": self.actor,
                "role": self.role,
                "occurred_at": self.occurred_at,
                "recorded_at": self.recorded_at,
                "prev_hash": self.prev_hash,
                "hash": self.hash,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def from_line(line: str) -> "Event":
        d = json.loads(line)
        return Event(
            seq=d["seq"],
            event_id=d["event_id"],
            dedup_key=d.get("dedup_key"),
            etype=d["etype"],
            payload=d["payload"],
            actor=d["actor"],
            role=d["role"],
            occurred_at=d["occurred_at"],
            recorded_at=d["recorded_at"],
            prev_hash=d["prev_hash"],
            hash=d["hash"],
        )

    def body_for_hash(self) -> dict:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "dedup_key": self.dedup_key,
            "etype": self.etype,
            "payload": self.payload,
            "actor": self.actor,
            "role": self.role,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "prev_hash": self.prev_hash,
        }


def compute_hash(ev: Event) -> str:
    return payload_hash(ev.body_for_hash())


class EventStore:
    """JSONL 追加日志；进程内加锁，跨进程用 flock；重放校验哈希链。"""

    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._dedup: dict[str, int] = {}   # dedup_key -> seq
        self._event_ids: set[str] = set()
        self._file_handle = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            self._load()
            if fcntl is not None:
                # 旁路锁文件：不干扰对正式日志的顺序追加与重放读取
                self._file_handle = open(f"{path}.lock", "a+", encoding="utf-8")

    # ---- 持久化 -------------------------------------------------------

    @contextmanager
    def _file_lock(self):
        if self._file_handle is not None:
            fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
        else:
            yield

    def _load(self) -> None:
        assert self.path is not None
        if not os.path.exists(self.path):
            return
        prev = GENESIS_HASH
        with open(self.path, "r", encoding="utf-8") as fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                ev = Event.from_line(line)
                if ev.seq != ln:
                    raise TamperDetected(f"事件序号断裂：第 {ln} 行 seq={ev.seq}")
                if ev.prev_hash != prev:
                    raise TamperDetected(f"哈希链断裂于 seq={ev.seq}")
                if compute_hash(ev) != ev.hash:
                    raise TamperDetected(f"事件内容被篡改：seq={ev.seq}")
                prev = ev.hash
                self._events.append(ev)
                self._event_ids.add(ev.event_id)
                if ev.dedup_key:
                    self._dedup[ev.dedup_key] = ev.seq

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS_HASH

    @property
    def head_seq(self) -> int:
        return len(self._events)

    # ---- 写入 ---------------------------------------------------------

    def _key_exists_on_disk(self, key: str) -> bool:
        """跨进程兜底：本进程启动后可能有别的进程写入了同一幂等键。"""
        assert self.path is not None
        if not os.path.exists(self.path):
            return False
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    if json.loads(line).get("dedup_key") == key:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def append(
        self,
        etype: str,
        payload: dict,
        *,
        actor: str,
        role: str,
        dedup_key: Optional[str] = None,
        event_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
        validator: Optional[Callable[[Event], None]] = None,
    ) -> Event:
        """追加事件。

        ``dedup_key`` 已存在时不写入、不增加任何消耗，直接返回原事件
        （重复扫码/重复回执走这里）。``validator`` 在哈希封存入盘前
        执行业务校验；抛异常则事件不写入。
        """
        with self._lock, self._file_lock():
            if dedup_key is not None:
                if dedup_key not in self._dedup and self.path is not None:
                    if self._key_exists_on_disk(dedup_key):
                        # 其他进程先写入：重载以保持本进程投影一致
                        self._reload_locked()
                if dedup_key in self._dedup:
                    return self._events[self._dedup[dedup_key] - 1]

            eid = event_id or str(uuid.uuid4())
            if eid in self._event_ids:
                # 同 event_id 重提（一般由 dedup_key 先拦住，双保险）
                for ev in self._events:
                    if ev.event_id == eid:
                        return ev

            ev = Event(
                seq=len(self._events) + 1,
                event_id=eid,
                dedup_key=dedup_key,
                etype=etype,
                payload=payload,
                actor=actor,
                role=role,
                occurred_at=occurred_at or utcnow_iso(),
                recorded_at=utcnow_iso(),
                prev_hash=self.head_hash,
                hash="",
            )
            # 先校验、再封哈希、再落盘：校验失败绝不留痕
            if validator is not None:
                validator(ev)
            object.__setattr__(ev, "hash", compute_hash(ev))

            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(ev.as_line() + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._events.append(ev)
            self._event_ids.add(ev.event_id)
            if dedup_key:
                self._dedup[dedup_key] = ev.seq
            return ev

    def _reload_locked(self) -> None:
        self._events.clear()
        self._dedup.clear()
        self._event_ids.clear()
        if self.path:
            self._load()

    def close(self) -> None:
        if self._file_handle is not None:
            self._file_handle.close()
            self._file_handle = None

    def find_dedup(self, key: str) -> Optional[Event]:
        with self._lock:
            seq = self._dedup.get(key)
            return self._events[seq - 1] if seq else None

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def replay(self, handler: Callable[[Event], None],
               upto_seq: Optional[int] = None) -> None:
        with self._lock:
            snapshot = list(self._events)
        for ev in snapshot:
            if upto_seq is not None and ev.seq > upto_seq:
                break
            handler(ev)
