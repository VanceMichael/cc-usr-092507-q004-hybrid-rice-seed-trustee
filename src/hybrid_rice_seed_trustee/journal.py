"""只增事实日志（event store）。

每条事实占一行 JSON；写入时分配单调递增的 ``seq``，先 append 再
``flush + fsync``，进程崩溃（停机）后已确认的事实不会丢失。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .models import Fact, FactType


class Journal:
    """文件支持的只增日志，线程安全。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        if self.path.exists():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    self._seq = max(self._seq, int(json.loads(raw)["seq"]))

    # —— 读 ——

    def read_all(self) -> list[Fact]:
        if not self.path.exists():
            return []
        facts: list[Fact] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                facts.append(Fact.from_dict(json.loads(raw)))
        return facts

    @property
    def next_seq(self) -> int:
        return self._seq + 1

    # —— 写 ——

    def append(self, fact_type: FactType, data: dict, source: str,
               recorded_at: str) -> Fact:
        """串行化追加并落盘，返回带 seq 的事实。"""
        with self._lock:
            self._seq += 1
            fact = Fact(type=fact_type, data=data, source=source,
                        recorded_at=recorded_at, seq=self._seq)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(fact.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return fact
