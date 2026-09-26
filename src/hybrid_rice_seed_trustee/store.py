"""仅追加事件存储。

事件以 JSON Lines 持久化：每条事件一行，追加后立即落盘。
进程在写入中途被杀死时，最后一行可能只写了一半——重放时
截断容忍该尾部残行（尚未形成事实），此前已落盘的事件不丢。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .events import Event


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: Event) -> int:
        """追加一条事件并返回其序号。

        相同幂等键已存在时，不重复写入，直接返回原事件序号——
        调用方在崩溃后重试同一动作是安全的。
        """
        existing = self._idem_index().get(event.idem_key)
        if existing is not None:
            return existing
        seq = self.count()
        event = Event(
            event_type=event.event_type,
            data=event.data,
            occurred_on=event.occurred_on,
            idem_key=event.idem_key,
            actor=event.actor,
            seq=seq,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return seq

    def read_all(self) -> list[Event]:
        """按序号读取全部事件，容忍尾部残缺行。"""
        if not self.path.exists():
            return []
        events: list[Event] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                # 停机可能发生在一次 write() 中间：最后一行不完整，
                # 它本就未随 fsync 形成可确认事实，停止读取即可。
                break
        return events

    def count(self) -> int:
        return len(self.read_all())

    def _idem_index(self) -> dict[str, int]:
        return {event.idem_key: event.seq for event in self.read_all()}
