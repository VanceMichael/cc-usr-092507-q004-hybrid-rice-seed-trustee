"""领域值对象与不可变事实结构。

设计约定
========

* 所有业务状态变化都先表达为一条 ``Fact``，追加进只增日志后才生效；
* ``PlanSnapshot`` 在合同成立当时固化适宜区域、隔离、目标面积与结算口径，
  事后任何规则新版本都不能改写其中已经播种的部分；
* 时间一律使用 ISO 8601 字符串（``YYYY-MM-DD`` 或 ``YYYY-MM-DDTHH:MM:SS``），
  便于持久化与按字典序比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FactType(str, Enum):
    """日志中允许出现的事实类型。"""

    # —— 公开版本资料（带期间生效）——
    VARIETY_AUTHORIZATION = "variety_authorization"   # 品种授权版本
    TRUSTEE_GRADE = "trustee_grade"                   # 受托人等级
    PRICE_INDEX = "price_index"                       # 价格指数
    RULEBOOK_PUBLISHED = "rulebook_published"         # 争用优先规则簿
    AUTH_WITHDRAWN = "auth_withdrawn"                 # 授权撤回

    # —— 基础资源 ——
    FIELD_REGISTERED = "field_registered"             # 田块登记
    PARENT_BATCH_REGISTERED = "parent_batch_registered"  # 亲本批次登记

    # —— 合同与计划 ——
    PLAN_PROPOSED = "plan_proposed"                   # 合同计划提案
    PLAN_CONFIRMED = "plan_confirmed"                 # 合同成立（快照固化）
    PLAN_REJECTED = "plan_rejected"                   # 合同不能成立
    BATCH_OPENED = "batch_opened"                     # 生产批次开启
    PLAN_RESCHEDULED = "plan_rescheduled"             # 调度改写（仅未播余量）
    PLAN_OVERRIDE = "plan_override"                   # 越级调整（需复核生效）
    OVERRIDE_APPROVED = "override_approved"           # 另一管理者复核通过
    OVERRIDE_REJECTED = "override_rejected"           # 复核驳回（提案不生效）

    # —— 田间与交付事实 ——
    SOWING_REPORTED = "sowing_reported"               # 播种
    FLOWERING_REPORTED = "flowering_reported"         # 花期
    ROGUING_REPORTED = "roguing_reported"             # 去杂
    INSPECTION_REPORTED = "inspection_reported"       # 检验
    SEED_DELIVERED = "seed_delivered"                 # 交种（完成交接）

    # —— 异常与处置 ——
    DISCREPANCY_FROZEN = "discrepancy_frozen"         # 矛盾记录→冻结
    DISASTER_REPORTED = "disaster_reported"           # 灾害
    REVIEW_RESOLVED = "review_resolved"               # 独立复核结论
    REPLANT_ASSIGNED = "replant_assigned"             # 补种责任落实
    RECOMPUTATION = "recomputation"                   # 沿依赖重算结果
    TASK_SCHEDULED = "task_scheduled"                 # 到期工作登记（农时/复检/结算）
    TASK_COMPLETED = "task_completed"                 # 到期工作完成

    # —— 结算 ——
    SETTLEMENT_FINALIZED = "settlement_finalized"     # 结算线（不可变）


class BatchStatus(str, Enum):
    """生产批次的生命周期状态。"""

    ACTIVE = "active"        # 正常生产中
    FROZEN = "frozen"        # 冻结，等待独立复核
    REJECTED = "rejected"    # 复核/检验最终否决
    DELIVERED = "delivered"  # 已完成交种交接


@dataclass(frozen=True)
class Fact:
    """一条不可变事实。seq 在写入日志时分配。"""

    type: FactType
    data: dict[str, Any]
    source: str
    recorded_at: str
    seq: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type.value,
            "data": self.data,
            "source": self.source,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Fact":
        return cls(
            type=FactType(raw["type"]),
            data=dict(raw["data"]),
            source=raw["source"],
            recorded_at=raw["recorded_at"],
            seq=int(raw["seq"]),
        )


@dataclass(frozen=True)
class ValidityWindow:
    """版本资料的生效期间（含端点）。"""

    effective_from: str
    effective_to: str | None = None  # None 表示至今有效

    def covers(self, day: str) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


@dataclass(frozen=True)
class PlanSnapshot:
    """合同成立当时固化的计划版本。

    结算口径引用价格指数的版本号与适用结算期间；之后发布的指数新版本
    不会回溯修改本快照。
    """

    plan_id: str
    plan_version: int
    company_id: str
    trustee_id: str
    variety_id: str
    suitable_regions: tuple[str, ...]
    isolation_m: float
    target_area_mu: float
    settlement: dict[str, Any]
    confirmed_at: str
    # 播种锁定以事件为准，快照仅记录成立时的排程依据：
    field_windows: tuple[dict[str, Any], ...]
    parent_batch_id: str
    rulebook_version: int


@dataclass(frozen=True)
class DueTask:
    """到期前不得丢失的工作项：农时、复检、结算。"""

    task_id: str
    kind: str                 # farming_window（农时）/ reinspection（复检）/ settlement（结算）
    due_date: str
    ref_id: str               # 关联计划或批次
    detail: str
    created_at: str
    done: bool = False
    done_at: str | None = None

    def mark(self, done_at: str) -> "DueTask":
        return DueTask(
            task_id=self.task_id, kind=self.kind, due_date=self.due_date,
            ref_id=self.ref_id, detail=self.detail, created_at=self.created_at,
            done=True, done_at=done_at,
        )
