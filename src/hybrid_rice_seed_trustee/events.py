"""领域事件定义。

事件是系统唯一的事实来源：所有状态都由事件流重放得到，
服务自身不保存任何可变状态，因此停机恢复不会丢失尚未到期的工作。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

EventType = Literal[
    "field_window_registered",      # 田块适播窗口登记（含隔离条件与适生品种）
    "trustee_rating_published",    # 受托人等级版本发布（公开）
    "variety_authorization_granted",  # 品种制种授权授予（按期间有效）
    "variety_authorization_revoked",  # 品种制种授权撤回
    "parent_batch_admitted",       # 亲本批次登记（数量、适生区域）
    "price_index_published",       # 价格指数版本发布（仅适用于明确的结算期间）
    "priority_rule_published",     # 受托人争用优先规则版本发布（公开）
    "contract_proposed",           # 合同提案（争用裁决前）
    "contract_rejected",           # 合同不成立（记录不成立的原因）
    "contract_confirmed",          # 合同成立并固化计划版本 v1
    "contract_superseded",         # 生效合同被经复核的越级调整替代
    "override_requested",          # 越级调整申请（等待另一名管理者复核）
    "plan_revised",                # 计划版本修订（未播种部分可改，已播种部分锁定）
    "production_fact_appended",   # 播种/花期/去杂/检验/交种事实按来源追加
    "fact_confirmed",             # 重复报送：与原结论一致，原结论保持不变
    "contradiction_frozen",       # 矛盾记录：冻结相关批次，移交独立复核
    "inspection_failed",          # 检验不合格：冻结批次并安排复检
    "loss_allocated",             # 灾害损失面积分摊到合同
    "batch_released",             # 独立复核后解冻批次
    "batch_rejected",             # 独立复核后判废批次
    "disaster_recorded",          # 灾害发生，沿实际依赖重算
    "replant_assigned",           # 重算产生的补种责任
    "delivery_settled",           # 结算（采用结算期间明确适用的指数版本）
    "override_reviewed",          # 越级调整由另一名管理者复核并留痕
    "work_item_scheduled",        # 登记到期工作（农时/复检/结算）
    "work_item_completed",        # 工作完成
]


@dataclass(frozen=True)
class Event:
    """一条不可变的领域事件。

    seq 由存储在追加时分配；idem_key 为业务幂等键，
    同一键的事件只生效一次（重复报送、重放恢复均依赖它）。
    """

    event_type: EventType
    data: dict[str, Any]
    occurred_on: date
    idem_key: str
    actor: str
    seq: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "occurred_on": self.occurred_on.isoformat(),
            "actor": self.actor,
            "idem_key": self.idem_key,
            "data": self.data,
        }

    @staticmethod
    def from_dict(value: dict[str, Any]) -> "Event":
        return Event(
            seq=value["seq"],
            event_type=value["event_type"],
            occurred_on=date.fromisoformat(value["occurred_on"]),
            actor=value["actor"],
            idem_key=value["idem_key"],
            data=value["data"],
        )
