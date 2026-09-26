"""制种委托领域服务。

所有写操作都：重放事件流 → 按公开规则裁决 → 仅追加新事件。
服务对象本身不持有可变状态，因此任意进程重启后行为一致，
未到期的农时、复检、结算工作随事件流完整恢复。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from .events import Event
from .ledger import Ledger
from .store import EventStore


class ContractRejected(Exception):
    """合同即时判定不成立（硬性资格问题）。"""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("；".join(reasons))


class RuleError(Exception):
    """操作违反领域规则（如无权复核、价格期间不明确）。"""


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


class SeedCommissionService:
    def __init__(
        self,
        store: EventStore,
        managers: Iterable[str] = (),
        inspectors: Iterable[str] = (),
        reinspection_days: int = 7,
        replant_grace_days: int = 10,
    ) -> None:
        self.store = store
        self.managers = set(managers)
        self.inspectors = set(inspectors)
        self.reinspection_days = reinspection_days
        self.replant_grace_days = replant_grace_days

    def _ledger(self) -> Ledger:
        return Ledger.replay(self.store.read_all())

    def _append(self, event: Event) -> int:
        return self.store.append(event)

    # ===================================================================
    # 基础资料：窗口、等级、授权、亲本、价格指数、优先规则（均有版本）
    # ===================================================================

    def register_field_window(
        self,
        field_id: str,
        county: str,
        area_mu: float,
        isolation_m: int,
        suitable_varieties: list[str],
        sow_start: date,
        sow_end: date,
        on: date,
        actor: str,
    ) -> None:
        if sow_start > sow_end:
            raise RuleError("田块窗口起止日期无效")
        self._append(
            Event(
                "field_window_registered",
                {
                    "field_id": field_id,
                    "county": county,
                    "area_mu": area_mu,
                    "isolation_m": isolation_m,
                    "suitable_varieties": suitable_varieties,
                    "sow_start": _iso(sow_start),
                    "sow_end": _iso(sow_end),
                },
                on,
                f"field:{field_id}",
                actor,
            )
        )

    def publish_trustee_rating(
        self,
        trustee_id: str,
        version: int,
        level: int,
        capacity_mu: float,
        published_on: date,
        actor: str,
    ) -> None:
        """发布受托人等级新版本；历史版本保留，结算与解释可追溯当时版本。"""
        ledger = self._ledger()
        history = ledger.trustees.get(trustee_id, {}).get("history", [])
        if any(item["version"] == version for item in history):
            raise RuleError(f"受托人等级版本已存在：{version}")
        self._append(
            Event(
                "trustee_rating_published",
                {
                    "trustee_id": trustee_id,
                    "version": version,
                    "level": level,
                    "capacity_mu": capacity_mu,
                    "published_on": _iso(published_on),
                },
                published_on,
                f"rating:{trustee_id}:v{version}",
                actor,
            )
        )

    def grant_authorization(
        self,
        variety_id: str,
        enterprise_id: str,
        grant_ref: str,
        valid_from: date,
        valid_to: date | None,
        suitable_counties: list[str],
        required_isolation_m: int,
        on: date,
        actor: str,
    ) -> None:
        ledger = self._ledger()
        if ledger.grant_by_ref(grant_ref) is not None:
            raise RuleError(f"授权文号已存在：{grant_ref}")
        self._append(
            Event(
                "variety_authorization_granted",
                {
                    "variety_id": variety_id,
                    "enterprise_id": enterprise_id,
                    "grant_ref": grant_ref,
                    "granted_on": _iso(on),
                    "valid_from": _iso(valid_from),
                    "valid_to": _iso(valid_to),
                    "suitable_counties": suitable_counties,
                    "required_isolation_m": required_isolation_m,
                },
                on,
                f"grant:{variety_id}:{enterprise_id}:{grant_ref}",
                actor,
            )
        )

    def admit_parent_batch(
        self,
        batch_id: str,
        variety_id: str,
        usable_area_mu: float,
        counties: list[str],
        on: date,
        actor: str,
    ) -> None:
        self._append(
            Event(
                "parent_batch_admitted",
                {
                    "batch_id": batch_id,
                    "variety_id": variety_id,
                    "usable_area_mu": usable_area_mu,
                    "counties": counties,
                    "admitted_on": _iso(on),
                },
                on,
                f"parent:{batch_id}",
                actor,
            )
        )

    def publish_price_index(
        self,
        version: int,
        period_start: date,
        period_end: date,
        index_value: float,
        price_per_kg: float,
        published_on: date,
        actor: str,
    ) -> None:
        """价格指数新版本自带明确适用期间；不向前改写任何既有结算。"""
        ledger = self._ledger()
        if any(item["version"] == version for item in ledger.price_indices):
            raise RuleError(f"价格指数版本已存在：{version}")
        self._append(
            Event(
                "price_index_published",
                {
                    "version": version,
                    "period_start": _iso(period_start),
                    "period_end": _iso(period_end),
                    "index_value": index_value,
                    "price_per_kg": price_per_kg,
                    "published_on": _iso(published_on),
                },
                published_on,
                f"index:v{version}",
                actor,
            )
        )

    def publish_priority_rule(
        self, version: int, criteria: list[str], published_on: date, actor: str
    ) -> None:
        """发布受托人/田块争用的公开优先规则版本。

        criteria 自高到低取值：application_time（申请在先）、
        trustee_rating（受托等级高者优先）、target_area（连片面积大者优先）。
        """
        allowed = {"application_time", "trustee_rating", "target_area"}
        if not criteria or any(c not in allowed for c in criteria):
            raise RuleError("优先规则含未知排序项")
        ledger = self._ledger()
        if any(item["version"] == version for item in ledger.priority_rules):
            raise RuleError(f"优先规则版本已存在：{version}")
        self._append(
            Event(
                "priority_rule_published",
                {"version": version, "criteria": criteria,
                 "published_on": _iso(published_on)},
                published_on,
                f"priority-rule:v{version}",
                actor,
            )
        )

    # ===================================================================
    # 合同提案：硬性资格即时判定；争用类冲突留给公开规则唯一裁决
    # ===================================================================

    def propose_contract(
        self,
        proposal_id: str,
        enterprise_id: str,
        trustee_id: str,
        field_id: str,
        variety_id: str,
        parent_batch_id: str,
        area_mu: float,
        season_key: str,
        sow_start: date,
        sow_end: date,
        required_min_level: int,
        expected_yield_kg_per_mu: float,
        settlement_period_start: date,
        settlement_period_end: date,
        on: date,
        actor: str,
        flowering_expected_on: date | None = None,
    ) -> None:
        ledger = self._ledger()
        reasons = self._hard_qualification_reasons(
            ledger,
            enterprise_id=enterprise_id,
            trustee_id=trustee_id,
            field_id=field_id,
            variety_id=variety_id,
            parent_batch_id=parent_batch_id,
            area_mu=area_mu,
            sow_start=sow_start,
            sow_end=sow_end,
            required_min_level=required_min_level,
            on=on,
        )
        if reasons:
            # 不成立也留痕：记录被拒提案与全部原因，避免企业事后各执一词。
            self._append(
                Event(
                    "contract_rejected",
                    {"proposal_id": proposal_id, "reasons": reasons},
                    on,
                    f"proposal:{proposal_id}:rejected",
                    actor,
                )
            )
            raise ContractRejected(reasons)

        self._append(
            Event(
                "contract_proposed",
                {
                    "proposal_id": proposal_id,
                    "enterprise_id": enterprise_id,
                    "trustee_id": trustee_id,
                    "field_id": field_id,
                    "variety_id": variety_id,
                    "parent_batch_id": parent_batch_id,
                    "area_mu": area_mu,
                    "season_key": season_key,
                    "sow_start": _iso(sow_start),
                    "sow_end": _iso(sow_end),
                    "required_min_level": required_min_level,
                    "expected_yield_kg_per_mu": expected_yield_kg_per_mu,
                    "settlement_period_start": _iso(settlement_period_start),
                    "settlement_period_end": _iso(settlement_period_end),
                    "flowering_expected_on": _iso(flowering_expected_on)
                    if flowering_expected_on
                    else None,
                },
                on,
                f"proposal:{proposal_id}",
                actor,
            )
        )

    def _hard_qualification_reasons(
        self,
        ledger: Ledger,
        *,
        enterprise_id: str,
        trustee_id: str,
        field_id: str,
        variety_id: str,
        parent_batch_id: str,
        area_mu: float,
        sow_start: date,
        sow_end: date,
        required_min_level: int,
        on: date,
    ) -> list[str]:
        reasons: list[str] = []
        field = ledger.fields.get(field_id)
        trustee = ledger.trustees.get(trustee_id)
        grant = ledger.valid_grant(variety_id, enterprise_id, on)
        parent = ledger.parent_batches.get(parent_batch_id)

        if field is None:
            reasons.append("田块窗口未登记")
        if trustee is None or "current" not in trustee:
            reasons.append("受托人等级未发布")
        if grant is None:
            reasons.append("品种制种授权不存在")
        if parent is None:
            reasons.append("亲本批次未登记")
        if reasons:
            return reasons

        if sow_start < field["sow_start"] or sow_end > field["sow_end"]:
            reasons.append("制种农时超出田块适播窗口")
        if area_mu > field["area_mu"]:
            reasons.append("目标面积超过田块面积")
        if variety_id not in field["suitable_varieties"]:
            reasons.append("品种不在田块适宜品种范围")
        if field["county"] not in grant["suitable_counties"]:
            reasons.append("田块所在县不在授权适宜区域")
        if field["isolation_m"] < grant["required_isolation_m"]:
            reasons.append(
                f"隔离距离不足：田块{field['isolation_m']}米<要求{grant['required_isolation_m']}米"
            )
        # 授权存在性、有效期间与撤回状态已由 valid_grant(on) 保证
        if parent["variety_id"] != variety_id:
            reasons.append("亲本批次与品种不符")
        if field["county"] not in parent["counties"]:
            reasons.append("亲本批次不适用于该县")
        current = trustee["current"]
        if current["published_on"] > on:
            reasons.append("受托等级版本发布时间晚于提案")
        if current["level"] > required_min_level:
            reasons.append(
                f"受托人等级不足：{current['level']}级，要求不低于{required_min_level}级"
            )
        if area_mu > current["capacity_mu"]:
            reasons.append("目标面积超过受托人单季受托能力")
        # 亲本余量：扣除已确认合同占用与已损失面积（待裁提案之间的争用在裁决阶段处理）
        committed = sum(
            c["plans"][-1]["target_area_mu"]
            for c in ledger.contracts.values()
            if c["status"] == "active" and c["parent_batch_id"] == parent_batch_id
        )
        remaining_parent = parent["usable_area_mu"] - parent["lost_area_mu"] - committed
        if area_mu > remaining_parent + 1e-9:
            reasons.append("亲本批次可制种面积不足")
        return reasons

    # ===================================================================
    # 争用裁决：同一公开规则版本下得到唯一安排
    # ===================================================================

    def _current_rule(self, ledger: Ledger, on: date) -> dict[str, Any]:
        rules = [r for r in ledger.priority_rules if r["published_on"] <= on]
        if not rules:
            raise RuleError("尚未发布公开的争用优先规则，无法裁决")
        return rules[-1]

    def _sort_key(self, ledger: Ledger, criteria: list[str], proposal: dict[str, Any]):
        key: list[Any] = []
        for criterion in criteria:
            if criterion == "application_time":
                key.append(proposal["proposed_seq"])
            elif criterion == "trustee_rating":
                key.append(ledger.trustees[proposal["trustee_id"]]["current"]["level"])
            elif criterion == "target_area":
                key.append(-proposal["area_mu"])
        key.append(proposal["proposed_seq"])  # 申请时间兜底，保证唯一
        return tuple(key)

    def settle_proposals(self, on: date, actor: str, season_key: str | None = None) -> dict[str, Any]:
        """对所有挂起提案按当前公开规则裁决，每个冲突窗口产出唯一安排。

        争用按“同一块田 + 实际播种窗口重叠”聚类，而非仅按季节标签；
        每个重叠簇生成公开规则下的候选队列，再跨簇依次尝试，
        受托能力/亲本余量不足时由簇内下一候选递补。
        """
        ledger = self._ledger()
        rule = self._current_rule(ledger, on)
        pending = [
            p
            for p in ledger.pending_proposals()
            if season_key is None or p["season_key"] == season_key
        ]
        if not pending:
            return {"confirmed": [], "rejected": [], "rule_version": rule["version"]}

        rejected: list[dict[str, Any]] = []
        admitted: list[dict[str, Any]] = []

        # ---- 按田块收集，再按播种窗口重叠聚成冲突簇 ----
        by_field: dict[str, list[dict[str, Any]]] = {}
        for proposal in pending:
            by_field.setdefault(proposal["field_id"], []).append(proposal)

        queues: list[tuple[tuple, dict[str, Any] | None, list[dict[str, Any]]]] = []
        for field_id, field_proposals in by_field.items():
            ordered_by_window = sorted(
                field_proposals, key=lambda p: (p["sow_start"], p["sow_end"]))
            clusters: list[list[dict[str, Any]]] = []
            cluster_end: date | None = None
            for p in ordered_by_window:
                if clusters and p["sow_start"] <= cluster_end:
                    clusters[-1].append(p)
                    cluster_end = max(cluster_end, p["sow_end"])
                else:
                    clusters.append([p])
                    cluster_end = p["sow_end"]

            for index, proposals in enumerate(clusters):
                live: list[dict[str, Any]] = []
                for p in proposals:
                    stale = self._hard_qualification_reasons(
                        ledger,
                        enterprise_id=p["enterprise_id"],
                        trustee_id=p["trustee_id"],
                        field_id=p["field_id"],
                        variety_id=p["variety_id"],
                        parent_batch_id=p["parent_batch_id"],
                        area_mu=p["area_mu"],
                        sow_start=p["sow_start"],
                        sow_end=p["sow_end"],
                        required_min_level=p["required_min_level"],
                        on=on,
                    )
                    if stale:
                        rejected.append({"proposal_id": p["proposal_id"], "reasons": stale,
                                         "winner_id": None})
                    else:
                        live.append(p)

                window_start = min(p["sow_start"] for p in proposals)
                window_end = max(p["sow_end"] for p in proposals)
                incumbent = next(
                    (
                        c
                        for c in ledger.active_contracts()
                        if c["field_id"] == field_id
                        and c["sow_start"] <= window_end
                        and c["sow_end"] >= window_start
                    ),
                    None,
                )
                ordered = sorted(
                    live,
                    key=lambda p: (0 if p["forced"] else 1,
                                   self._sort_key(ledger, rule["criteria"], p)),
                )
                queues.append(((field_id, index), incumbent, ordered))

        # 跨组尝试顺序：候选队列的优先级（越级优先，其后按公开规则）
        queues.sort(
            key=lambda item: (0 if item[2] and item[2][0]["forced"] else 1,
                              self._sort_key(ledger, rule["criteria"], item[2][0]))
            if item[2] else (2, ())
        )

        # ---- 全局资源账本：计入本季合同，以及与本次待裁窗口重叠的合同 ----
        # 容量按单季计；与待裁提案窗口重叠的在位合同（可能季节标签不同）
        # 也必须计入，因为它们正是稍后可能被替代并释放占用的对象。
        capacity: dict[str, float] = {}
        parent_used: dict[str, float] = {}

        def overlaps_pending(contract: dict[str, Any]) -> bool:
            return any(
                p["field_id"] == contract["field_id"]
                and p["sow_start"] <= contract["sow_end"]
                and p["sow_end"] >= contract["sow_start"]
                for p in pending
            )

        for c in ledger.active_contracts():
            in_season = season_key is None or c["season_key"] == season_key
            if not in_season and not overlaps_pending(c):
                continue
            area = c["plans"][-1]["target_area_mu"]
            capacity[c["trustee_id"]] = capacity.get(c["trustee_id"], 0.0) + area
            parent_used[c["parent_batch_id"]] = (
                parent_used.get(c["parent_batch_id"], 0.0) + area
            )

        for _cluster_key, incumbent, ordered in queues:
            # 已播种的在位合同：任何调度都不得改写已播种部分
            if incumbent is not None and self._sewn_area(ledger, incumbent["contract_id"]) > 1e-9:
                for p in ordered:
                    rejected.append({
                        "proposal_id": p["proposal_id"],
                        "reasons": [
                            "田块已有播种事实，越级调整不得改写已播种部分"
                            if p["forced"]
                            else "田块在该农时已被排他承诺"
                        ],
                        "winner_id": incumbent["contract_id"],
                    })
                continue

            release = {
                "trustee_id": incumbent["trustee_id"],
                "parent_batch_id": incumbent["parent_batch_id"],
                "area_mu": incumbent["plans"][-1]["target_area_mu"],
            } if incumbent is not None else None

            winner: dict[str, Any] | None = None
            for p in ordered:
                if winner is not None:
                    rejected.append({
                        "proposal_id": p["proposal_id"],
                        "reasons": [
                            "田块在重叠农时已被排他承诺：公开优先规则裁决由他人取得"
                        ],
                        "winner_id": winner["proposal_id"],
                    })
                    continue
                if incumbent is not None and not p["forced"]:
                    # 在位合同受保护：只有经另一名管理者复核批准的越级候选才能替代
                    rejected.append({
                        "proposal_id": p["proposal_id"],
                        "reasons": ["田块在重叠农时已被排他承诺"],
                        "winner_id": incumbent["contract_id"],
                    })
                    continue
                # 若替代在位者，其占用先释放再计入新候选
                freed_trustee = (
                    release["area_mu"]
                    if release and release["trustee_id"] == p["trustee_id"]
                    else 0.0
                )
                freed_parent = (
                    release["area_mu"]
                    if release and release["parent_batch_id"] == p["parent_batch_id"]
                    else 0.0
                )
                cap = ledger.trustees[p["trustee_id"]]["current"]["capacity_mu"]
                trustee_used = capacity.get(p["trustee_id"], 0.0) - freed_trustee
                parent = ledger.parent_batches[p["parent_batch_id"]]
                parent_available = (
                    parent["usable_area_mu"]
                    - parent["lost_area_mu"]
                    - parent_used.get(p["parent_batch_id"], 0.0)
                    + freed_parent
                )
                if trustee_used + p["area_mu"] > cap + 1e-9:
                    rejected.append({
                        "proposal_id": p["proposal_id"],
                        "reasons": [
                            f"受托人同季受托能力不足：已占用{trustee_used:g}亩+"
                            f"申请{p['area_mu']:g}亩>{cap:g}亩"
                        ],
                        "winner_id": None,
                    })
                    continue
                if p["area_mu"] > parent_available + 1e-9:
                    rejected.append({
                        "proposal_id": p["proposal_id"],
                        "reasons": ["亲本批次可制种面积在本次裁决中不足"],
                        "winner_id": None,
                    })
                    continue
                winner = p

            if winner is None:
                # 无候选通过资源约束：在位者保留，未给出资源原因的候选记为排他落选
                if incumbent is not None:
                    for item in rejected:
                        if (
                            item["winner_id"] is None
                            and any(p["proposal_id"] == item["proposal_id"] for p in ordered)
                        ):
                            item["winner_id"] = incumbent["contract_id"]
                continue

            if release is not None:
                capacity[release["trustee_id"]] = (
                    capacity.get(release["trustee_id"], 0.0) - release["area_mu"]
                )
                parent_used[release["parent_batch_id"]] = (
                    parent_used.get(release["parent_batch_id"], 0.0) - release["area_mu"]
                )
            capacity[winner["trustee_id"]] = (
                capacity.get(winner["trustee_id"], 0.0) + winner["area_mu"]
            )
            parent_used[winner["parent_batch_id"]] = (
                parent_used.get(winner["parent_batch_id"], 0.0) + winner["area_mu"]
            )
            admitted.append({
                "proposal": winner,
                "supersedes": incumbent["contract_id"] if incumbent is not None else None,
            })
            # 组内排在胜者之前但因资源不足被拒的候选，胜者即为它们的递补安排方
            for item in rejected:
                if (
                    item["winner_id"] is None
                    and any(p["proposal_id"] == item["proposal_id"] for p in ordered)
                ):
                    item["winner_id"] = winner["proposal_id"]

        # ---- 落盘裁决结果 ----
        for item in rejected:
            self._append(
                Event(
                    "contract_rejected",
                    {
                        "proposal_id": item["proposal_id"],
                        "reasons": item["reasons"],
                        "rule_version": rule["version"],
                        "winner_id": item["winner_id"],
                    },
                    on,
                    f"proposal:{item['proposal_id']}:rejected",
                    actor,
                )
            )
        for item in admitted:
            self._confirm(ledger, item["proposal"], item["supersedes"], rule["version"], on, actor)

        return {
            "confirmed": [item["proposal"]["proposal_id"] for item in admitted],
            "rejected": [item["proposal_id"] for item in rejected],
            "rule_version": rule["version"],
        }

    def _confirm(
        self,
        ledger: Ledger,
        proposal: dict[str, Any],
        supersedes: str | None,
        rule_version: int,
        on: date,
        actor: str,
    ) -> None:
        contract_id = proposal["proposal_id"]
        if supersedes:
            self._append(
                Event(
                    "contract_superseded",
                    {"contract_id": supersedes, "replaced_by": contract_id},
                    on,
                    f"contract:{supersedes}:superseded",
                    actor,
                )
            )
        rating = ledger.trustees[proposal["trustee_id"]]["current"]
        grant = ledger.valid_grant(proposal["variety_id"], proposal["enterprise_id"], on)
        if grant is None:  # 裁决资格复核后理论上不会发生
            raise RuleError("品种授权在裁决时已失效")
        period = {
            "start": proposal["settlement_period"]["start"],
            "end": proposal["settlement_period"]["end"],
        }
        index_version = self._index_version_for_period(ledger, period, on)
        plan = {
            "version": 1,
            "as_of": _iso(on),
            "suitable_counties": list(grant["suitable_counties"]),
            "required_isolation_m": grant["required_isolation_m"],
            "target_area_mu": proposal["area_mu"],
            "settlement_period_start": _iso(period["start"]),
            "settlement_period_end": _iso(period["end"]),
            "price_index_version": index_version,
            "parent_batch_id": proposal["parent_batch_id"],
            "rating_version": rating["version"],
            "rating_level": rating["level"],
            "grant_ref": grant["grant_ref"],
            "rule_version": rule_version,
            "expected_yield_kg_per_mu": proposal["expected_yield_kg_per_mu"],
        }
        self._append(
            Event(
                "contract_confirmed",
                {"contract_id": contract_id, "plan": plan},
                on,
                f"contract:{contract_id}:confirmed",
                actor,
            )
        )
        # 农时与结算工作随合同固化一并登记；它们是事件，停机恢复不丢。
        self._schedule(f"sowing:{contract_id}", "sowing", proposal["sow_end"],
                       "contract", contract_id, "适播窗口截止", on, actor)
        flowering_on = proposal.get("flowering_expected_on")
        if flowering_on:
            self._schedule(f"flowering:{contract_id}", "flowering", flowering_on,
                           "contract", contract_id, "花期检查", on, actor)
        self._schedule(f"settlement:{contract_id}", "settlement", period["end"],
                       "contract", contract_id, "结算期间截止", on, actor)

    @staticmethod
    def _index_version_for_period(
        ledger: Ledger, period: dict[str, date], as_of: date
    ) -> int | None:
        """签约时固化价格指数：只认在该结算期间明确适用且已发布的版本。

        优先适用期间与结算期间完全一致的版本；否则取完整覆盖结算期间的版本。
        新版本不能向前改写既有计划，期间不明确覆盖时宁可不固化（返回 None）。
        """
        exact = [
            i
            for i in ledger.price_indices
            if i["published_on"] <= as_of
            and i["period_start"] == period["start"]
            and i["period_end"] == period["end"]
        ]
        if exact:
            return max(exact, key=lambda i: i["version"])["version"]
        covering = [
            i
            for i in ledger.price_indices
            if i["published_on"] <= as_of
            and i["period_start"] <= period["start"]
            and period["end"] <= i["period_end"]
        ]
        return max(covering, key=lambda i: i["version"])["version"] if covering else None

    # ===================================================================
    # 越级调整：申请—另一名管理者复核，全程留痕
    # ===================================================================

    def request_override(
        self, proposal_id: str, requester: str, reason: str, on: date
    ) -> None:
        if requester not in self.managers:
            raise RuleError("只有县域管理人员可以发起越级调整")
        ledger = self._ledger()
        proposal = ledger.proposals.get(proposal_id)
        if proposal is None or proposal["outcome"] != "pending":
            raise RuleError("越级调整只能针对挂起提案")
        self._append(
            Event(
                "override_requested",
                {"proposal_id": proposal_id, "requester": requester, "reason": reason},
                on,
                f"override:{proposal_id}:{requester}:request",
                requester,
            )
        )

    def review_override(
        self, proposal_id: str, reviewer: str, approved: bool, reason: str, on: date
    ) -> None:
        if reviewer not in self.managers:
            raise RuleError("只有县域管理人员可以复核越级调整")
        ledger = self._ledger()
        proposal = ledger.proposals.get(proposal_id)
        if proposal is None:
            raise RuleError("提案不存在")
        pending_request = next(
            (item for item in proposal["overrides"] if item["review"] is None), None
        )
        if pending_request is None:
            raise RuleError("该提案没有待复核的越级调整申请")
        if pending_request["requester"] == reviewer:
            raise RuleError("越级调整必须由另一名管理者复核")
        self._append(
            Event(
                "override_reviewed",
                {
                    "proposal_id": proposal_id,
                    "requester": pending_request["requester"],
                    "reviewer": reviewer,
                    "approved": approved,
                    "reason": reason,
                },
                on,
                f"override:{proposal_id}:{reviewer}:review",
                reviewer,
            )
        )

    # ===================================================================
    # 计划版本修订：已播种部分锁定
    # ===================================================================

    def revise_plan(
        self, contract_id: str, changes: dict[str, Any], on: date, actor: str
    ) -> int:
        ledger = self._ledger()
        contract = ledger.contracts.get(contract_id)
        if contract is None or contract["status"] != "active":
            raise RuleError("合同不存在或未生效")
        plan = ledger.current_plan(contract_id)
        sewn = self._sewn_area(ledger, contract_id)
        clean: dict[str, Any] = {}

        if "target_area_mu" in changes:
            new_area = float(changes["target_area_mu"])
            if new_area < sewn - 1e-9:
                raise RuleError("目标面积不能小于已播种面积：已播种部分锁定")
            if new_area > ledger.fields[contract["field_id"]]["area_mu"]:
                raise RuleError("目标面积超过田块面积")
            clean["target_area_mu"] = new_area
        if "required_isolation_m" in changes or "suitable_counties" in changes:
            if sewn > 1e-9:
                raise RuleError("已播种后不得修改隔离要求与适宜区域")
            if "required_isolation_m" in changes:
                clean["required_isolation_m"] = int(changes["required_isolation_m"])
            if "suitable_counties" in changes:
                clean["suitable_counties"] = list(changes["suitable_counties"])
        if "settlement_period_start" in changes or "settlement_period_end" in changes:
            start = changes.get("settlement_period_start")
            end = changes.get("settlement_period_end")
            if not start or not end:
                raise RuleError("修改结算期间必须同时提供起止日期")
            clean["settlement_period_start"] = _iso(start)
            clean["settlement_period_end"] = _iso(end)
            # 进入新结算期间时只采用对该期间明确适用的指数版本
            index_version = self._index_version_for_period(ledger, {"start": start, "end": end}, on)
            clean["price_index_version"] = index_version
        elif "price_index_version" in changes:
            version = changes["price_index_version"]
            period_start = plan["settlement_period"]["start"]
            period_end = plan["settlement_period"]["end"]
            index = next(
                (i for i in ledger.price_indices if i["version"] == version), None
            )
            if index is None:
                raise RuleError(f"价格指数版本不存在：{version}")
            if not (
                index["period_start"] <= period_start
                and period_end <= index["period_end"]
            ):
                raise RuleError("新价格指数版本的适用期间不明确覆盖本合同结算期间")
            clean["price_index_version"] = version

        if not clean:
            raise RuleError("没有可修订的计划字段")
        version = len(contract["plans"]) + 1
        self._append(
            Event(
                "plan_revised",
                {"contract_id": contract_id, "version": version, "changes": clean},
                on,
                f"contract:{contract_id}:plan:v{version}",
                actor,
            )
        )
        return version

    # ===================================================================
    # 生产事实：按来源追加、重复报送保原结论、矛盾冻结交独立复核
    # ===================================================================

    def append_fact(
        self,
        contract_id: str,
        kind: str,
        source: str,
        report_id: str,
        on: date,
        actor: str,
        conclusion: str | None = None,
        area_mu: float | None = None,
        qty_kg: float | None = None,
        destination: str | None = None,
        evidence_refs: list[str] | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        ledger = self._ledger()
        contract = ledger.contracts.get(contract_id)
        if contract is None:
            raise RuleError("合同不存在")
        if kind not in ("sowing", "flowering", "roguing", "inspection", "delivery"):
            raise RuleError(f"未知生产事实类型：{kind}")
        fact_id = f"{contract_id}:{kind}:{report_id}"
        idem = f"fact:{fact_id}"
        if idem in ledger.idem_keys:
            # 同一来源报送编号重试：不产生任何新事实
            return {"effect": "duplicate_report_ignored", "fact_id": fact_id}

        if kind == "sowing" and batch_id is None:
            # 缺省以合同为一个种子批次；确有多批次时由调用方显式指定
            batch_id = f"{contract_id}:seed"
        if kind == "delivery" and not batch_id:
            raise RuleError("交种事实必须指明种子批次")
        if kind == "delivery":
            grant_ref = ledger.current_plan(contract_id)["grant_ref"]
            grant = ledger.grant_by_ref(grant_ref)
            if grant is not None and grant["revoked_on"] is not None and grant["revoked_on"] <= on:
                raise RuleError("品种授权已撤回，未交接部分不得再交种")

        if batch_id and batch_id in ledger.seed_batches:
            batch = ledger.seed_batches[batch_id]
            if batch["status"] in ("frozen", "rejected") and kind in ("sowing", "delivery"):
                raise RuleError(f"批次处于{batch['status']}状态，不得继续播种或交种")

        # 结论型事实：与既有同来源类型结论比对
        prior = next(
            (
                f
                for f in ledger.facts_of(contract_id, kind)
                if f["conclusion"] is not None
            ),
            None,
        )
        if (
            prior is not None
            and kind in ("flowering", "roguing", "inspection")
            and conclusion is not None
        ):
            if conclusion == prior["conclusion"]:
                self._append(
                    Event(
                        "fact_confirmed",
                        {"original_fact_id": prior["fact_id"], "report_id": report_id,
                         "source": source},
                        on,
                        f"fact-confirm:{fact_id}",
                        actor,
                    )
                )
                return {"effect": "confirmed_original", "fact_id": prior["fact_id"]}
            # 矛盾：双方记录都保留，相关批次冻结，交独立检验人员复核
            self._append(self._fact_event(
                fact_id, contract_id, kind, source, report_id, on, conclusion,
                area_mu, qty_kg, destination, evidence_refs, batch_id,
            ))
            frozen_batch = batch_id or self._latest_batch_id(ledger, contract_id)
            self._append(
                Event(
                    "contradiction_frozen",
                    {
                        "batch_id": frozen_batch,
                        "contract_id": contract_id,
                        "kind": kind,
                        "fact_ids": [prior["fact_id"], fact_id],
                    },
                    on,
                    f"contradiction:{frozen_batch}:{kind}:{report_id}",
                    actor,
                )
            )
            self._auto_close_work(ledger, contract_id, kind, source, on)
            # 检验结论矛盾且新结论为不合格：除交独立复核外，
            # 同样沿依赖重算并安排复检，避免可交付数量被高估。
            if kind == "inspection" and conclusion == "unqualified":
                self._schedule(
                    f"reinspection:{frozen_batch}:{report_id}",
                    "reinspection",
                    on + timedelta(days=self.reinspection_days),
                    "seed_batch", frozen_batch, "矛盾不合格复检", on, actor,
                )
                results = self._recompute(
                    ledger, {"type": "inspection", "fact_id": fact_id},
                    on, [contract_id], actor,
                )
                return {"effect": "contradiction_frozen", "batch_id": frozen_batch,
                        "recomputed": results}
            return {"effect": "contradiction_frozen", "batch_id": frozen_batch}

        self._append(self._fact_event(
            fact_id, contract_id, kind, source, report_id, on, conclusion,
            area_mu, qty_kg, destination, evidence_refs, batch_id,
        ))
        self._auto_close_work(ledger, contract_id, kind, source, on)

        # 检验不合格：冻结该批次并沿实际依赖重算，等待独立复核与复检
        if kind == "inspection" and conclusion == "unqualified":
            frozen_batch = batch_id or self._latest_batch_id(ledger, contract_id)
            if frozen_batch:
                self._append(
                    Event(
                        "inspection_failed",
                        {"batch_id": frozen_batch, "contract_id": contract_id,
                         "fact_id": fact_id},
                        on,
                        f"inspection-failed:{frozen_batch}:{report_id}",
                        actor,
                    )
                )
                self._schedule(
                    f"reinspection:{frozen_batch}:{report_id}",
                    "reinspection",
                    on + timedelta(days=self.reinspection_days),
                    "seed_batch", frozen_batch, "不合格复检", on, actor,
                )
                results = self._recompute(
                    ledger, {"type": "inspection", "fact_id": fact_id},
                    on, [contract_id], actor,
                )
                return {"effect": "inspection_failed", "batch_id": frozen_batch,
                        "recomputed": results}
        return {"effect": "appended", "fact_id": fact_id}

    @staticmethod
    def _fact_event(
        fact_id, contract_id, kind, source, report_id, on, conclusion,
        area_mu, qty_kg, destination, evidence_refs, batch_id
    ) -> Event:
        return Event(
            "production_fact_appended",
            {
                "fact_id": fact_id,
                "contract_id": contract_id,
                "batch_id": batch_id,
                "kind": kind,
                "source": source,
                "report_id": report_id,
                "on": _iso(on),
                "conclusion": conclusion,
                "area_mu": area_mu,
                "qty_kg": qty_kg,
                "destination": destination,
                "evidence_refs": evidence_refs or [],
            },
            on,
            f"fact:{fact_id}",
            source,
        )

    def _auto_close_work(self, ledger: Ledger, contract_id: str, kind: str,
                         actor: str, on: date) -> None:
        mapping = {"sowing": "sowing", "flowering": "flowering"}
        work_kind = mapping.get(kind)
        if work_kind is None:
            return
        for item in ledger.open_work_items():
            if item["kind"] == work_kind and item["ref_id"] == contract_id:
                self._append(
                    Event(
                        "work_item_completed",
                        {"work_id": item["work_id"], "actor": actor},
                        on,
                        f"work:{item['work_id']}:completed:{actor}:{_iso(on)}",
                        actor,
                    )
                )

    def resolve_frozen_batch(
        self,
        batch_id: str,
        reviewer: str,
        decision: str,
        on: date,
        area_mu: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """独立检验人员对冻结批次复核：解冻或判废，随后沿依赖重算。"""
        if reviewer not in self.inspectors:
            raise RuleError("冻结批次必须由独立检验人员复核")
        ledger = self._ledger()
        batch = ledger.seed_batches.get(batch_id)
        if batch is None or batch["status"] != "frozen":
            raise RuleError("该批次当前不在冻结状态")
        involved_sources = {
            f["source"]
            for item in ledger.contradictions
            if item["batch_id"] == batch_id
            for fid in item["fact_ids"]
            for f in [ledger.fact_by_id(fid)]
        }
        if reviewer in involved_sources:
            raise RuleError("复核人不得是矛盾记录的原报送来源")
        if decision not in ("released", "rejected"):
            raise RuleError("复核结论必须是 released 或 rejected")

        event_type = "batch_released" if decision == "released" else "batch_rejected"
        self._append(
            Event(
                event_type,
                {"batch_id": batch_id, "reviewer": reviewer, "area_mu": area_mu,
                 "reason": reason},
                on,
                f"batch:{batch_id}:{decision}:{reviewer}",
                reviewer,
            )
        )
        results = self._recompute(
            ledger,
            {"type": "review", "batch_id": batch_id, "decision": decision},
            on, [batch["contract_id"]], reviewer,
        )
        return {"effect": decision, "batch_id": batch_id, "recomputed": results}

    # ===================================================================
    # 灾害、授权撤回：沿实际依赖重算可交付数量与补种责任
    # ===================================================================

    def record_disaster(
        self,
        disaster_id: str,
        on: date,
        actor: str,
        area_mu: float = 0.0,
        county: str | None = None,
        field_id: str | None = None,
        contract_id: str | None = None,
        seed_batch_id: str | None = None,
        parent_batch_id: str | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        ledger = self._ledger()
        scope = {
            k: v
            for k, v in {
                "county": county,
                "field_id": field_id,
                "contract_id": contract_id,
                "seed_batch_id": seed_batch_id,
                "parent_batch_id": parent_batch_id,
            }.items()
            if v is not None
        }
        affected = self._contracts_in_scope(ledger, scope)
        if not affected:
            raise RuleError("灾害范围未匹配到任何生效合同")
        self._append(
            Event(
                "disaster_recorded",
                {"disaster_id": disaster_id, "scope": scope, "area_mu": area_mu,
                 "on": _iso(on), "detail": detail},
                on,
                f"disaster:{disaster_id}",
                actor,
            )
        )
        # 合同级损失面积分摊（seed_batch 直接损失已在批次上累计，不重复摊）
        if seed_batch_id is None and area_mu > 0:
            if contract_id or len(affected) == 1:
                allocations = {affected[0]: area_mu}
            else:
                total = sum(
                    ledger.current_plan(cid)["target_area_mu"] for cid in affected
                )
                allocations = {
                    cid: area_mu * ledger.current_plan(cid)["target_area_mu"] / total
                    for cid in affected
                }
            for cid, lost in allocations.items():
                self._append(
                    Event(
                        "loss_allocated",
                        {"contract_id": cid, "disaster_id": disaster_id,
                         "area_mu": round(lost, 6)},
                        on,
                        f"loss:{disaster_id}:{cid}",
                        actor,
                    )
                )
        results = self._recompute(
            ledger, {"type": "disaster", "disaster_id": disaster_id},
            on, affected, actor,
        )
        return {"disaster_id": disaster_id, "affected": affected, "recomputed": results}

    @staticmethod
    def _contracts_in_scope(ledger: Ledger, scope: dict[str, str]) -> list[str]:
        contracts = list(ledger.active_contracts())
        if "contract_id" in scope:
            contracts = [c for c in contracts if c["contract_id"] == scope["contract_id"]]
        if "seed_batch_id" in scope:
            batch = ledger.seed_batches.get(scope["seed_batch_id"])
            ids = {batch["contract_id"]} if batch else set()
            contracts = [c for c in contracts if c["contract_id"] in ids]
        if "field_id" in scope:
            contracts = [c for c in contracts if c["field_id"] == scope["field_id"]]
        if "county" in scope:
            county = scope["county"]
            contracts = [
                c for c in contracts
                if ledger.fields[c["field_id"]]["county"] == county
            ]
        if "parent_batch_id" in scope:
            contracts = [
                c for c in contracts if c["parent_batch_id"] == scope["parent_batch_id"]
            ]
        return [c["contract_id"] for c in contracts]

    def revoke_authorization(
        self,
        variety_id: str,
        enterprise_id: str,
        effective_on: date,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        ledger = self._ledger()
        current = ledger.valid_grant(variety_id, enterprise_id, effective_on)
        if current is None:
            raise RuleError("撤回生效日没有有效授权版本")
        self._append(
            Event(
                "variety_authorization_revoked",
                {"variety_id": variety_id, "enterprise_id": enterprise_id,
                 "effective_on": _iso(effective_on), "reason": reason},
                effective_on,
                f"grant:{variety_id}:{enterprise_id}:revoked:{_iso(effective_on)}",
                actor,
            )
        )
        # 沿实际依赖：只影响计划固化了被撤回那一版授权的合同
        affected = [
            c["contract_id"]
            for c in ledger.active_contracts()
            if c["variety_id"] == variety_id
            and c["enterprise_id"] == enterprise_id
            and ledger.current_plan(c["contract_id"])["grant_ref"] == current["grant_ref"]
        ]
        results = []
        if affected:
            results = self._recompute(
                ledger,
                {"type": "authorization_revoked",
                 "variety_id": variety_id, "enterprise_id": enterprise_id},
                effective_on, affected, actor,
            )
        return {"affected_contracts": affected, "recomputed": results}

    def _recompute(
        self,
        stale_ledger: Ledger,
        trigger: dict[str, Any],
        on: date,
        contract_ids: list[str],
        actor: str,
    ) -> list[dict[str, Any]]:
        # 重新读取：本事务此前追加的灾害/冻结/撤回事件必须进入计算
        ledger = self._ledger()
        results: list[dict[str, Any]] = []
        replant_events: list[Event] = []
        work_events: list[Event] = []
        for cid in contract_ids:
            contract = ledger.contracts.get(cid)
            if contract is None or contract["status"] != "active":
                continue
            plan = ledger.current_plan(cid)
            batches = [
                b for b in ledger.seed_batches.values() if b["contract_id"] == cid
            ]
            sewn = sum(b["sewn_area_mu"] for b in batches)
            damaged = sum(b["damaged_area_mu"] for b in batches) + contract.get(
                "disaster_area_mu", 0.0
            )
            failed = sum(b["failed_area_mu"] for b in batches)
            frozen_area = sum(
                b["sewn_area_mu"] - b["damaged_area_mu"] - b["failed_area_mu"]
                for b in batches
                if b["status"] == "frozen"
            )
            target = plan["target_area_mu"]
            lost_area = min(target, damaged + failed + max(0.0, frozen_area))
            deliverable_area = max(0.0, target - lost_area)
            settled_qty = sum(
                s["qty_kg"] for s in ledger.settlements if s["contract_id"] == cid
            )
            # 授权有效性按合同计划固化的那一版授权判定
            grant = ledger.grant_by_ref(plan["grant_ref"])
            authorization_valid = not (
                grant is not None
                and grant["revoked_on"] is not None
                and grant["revoked_on"] <= on
            )
            remaining = max(
                0.0,
                deliverable_area * plan["expected_yield_kg_per_mu"] - settled_qty,
            )
            if not authorization_valid:
                # 未交接部分不得再交付；已完成交接的证据仍然保留
                remaining = 0.0

            # 补种责任：受损面积中尚未指派补种、且仍在补种窗口内的部分。
            # 窗口以田块适播窗口截止日加宽限期为准，而非合同自报的播种截止。
            field = ledger.fields[contract["field_id"]]
            deadline = field["sow_end"] + timedelta(days=self.replant_grace_days)
            already_assigned = sum(
                r["area_mu"] for r in ledger.replants if r["contract_id"] == cid
            )
            replant_area = 0.0
            if authorization_valid and on <= deadline:
                replant_area = max(0.0, damaged + failed - already_assigned)
            replant_id = f"replant:{cid}:{len(ledger.replants) + 1}"
            if replant_area > 1e-9:
                replant_events.append(
                    Event(
                        "replant_assigned",
                        {
                            "replant_id": replant_id,
                            "contract_id": cid,
                            "area_mu": round(replant_area, 6),
                            "responsible": contract["trustee_id"],
                            "due_on": _iso(deadline),
                        },
                        on,
                        f"{replant_id}:{trigger['type']}",
                        actor,
                    )
                )
                work_events.append(
                    self._work_event(
                        f"replant-work:{replant_id}", "replant", deadline,
                        "contract", cid,
                        f"补种责任{round(replant_area, 3)}亩", on, actor,
                    )
                )

            results.append(
                {
                    "contract_id": cid,
                    "variety_id": contract["variety_id"],
                    "trustee_id": contract["trustee_id"],
                    "target_area_mu": round(target, 6),
                    "sewn_area_mu": round(sewn, 6),
                    "damaged_area_mu": round(damaged, 6),
                    "failed_area_mu": round(failed, 6),
                    "frozen_area_mu": round(max(0.0, frozen_area), 6),
                    "deliverable_kg_remaining": round(remaining, 6),
                    "settled_kg": round(settled_qty, 6),
                    "authorization_valid": authorization_valid,
                    "replant_area_mu": round(replant_area, 6),
                    "replant_responsible": contract["trustee_id"]
                    if replant_area > 1e-9
                    else None,
                    "replant_due_on": _iso(deadline) if replant_area > 1e-9 else None,
                }
            )

        self._append(
            Event(
                "deliverable_recomputed",
                {"trigger": trigger, "on": _iso(on), "results": results},
                on,
                f"recompute:{self._trigger_key(trigger)}:{_iso(on)}:{','.join(contract_ids)}",
                actor,
            )
        )
        for event in replant_events:
            self._append(event)
        for event in work_events:
            self._append(event)
        return results

    @staticmethod
    def _trigger_key(trigger: dict[str, Any]) -> str:
        if "disaster_id" in trigger:
            return f"disaster:{trigger['disaster_id']}"
        if "fact_id" in trigger:
            return f"inspection:{trigger['fact_id']}"
        if "batch_id" in trigger:
            return f"review:{trigger['batch_id']}:{trigger['decision']}"
        return f"revoked:{trigger['variety_id']}:{trigger['enterprise_id']}"

    # ===================================================================
    # 交种结算：数量以交接证据为上限，价格只用明确适用期间的指数版本
    # ===================================================================

    def settle_delivery(
        self, contract_id: str, batch_id: str, qty_kg: float, on: date, actor: str
    ) -> dict[str, Any]:
        ledger = self._ledger()
        contract = ledger.contracts.get(contract_id)
        if contract is None or contract["status"] != "active":
            raise RuleError("合同不存在或未生效")
        plan = ledger.current_plan(contract_id)
        if not (plan["settlement_period"]["start"] <= on <= plan["settlement_period"]["end"]):
            raise RuleError("交种日期不在当前计划版本的结算期间内")
        batch = ledger.seed_batches.get(batch_id)
        if batch is None or batch["contract_id"] != contract_id:
            raise RuleError("交种批次不属于该合同")
        if batch["status"] != "active":
            raise RuleError(f"批次处于{batch['status']}状态，不能结算")
        handed_over = batch["delivered_kg"]
        already = sum(
            s["qty_kg"]
            for s in ledger.settlements
            if s["contract_id"] == contract_id and s["batch_id"] == batch_id
        )
        if qty_kg > handed_over - already + 1e-9:
            raise RuleError("结算数量超过已完成交接的数量")

        index = self._index_applicable_on(ledger, plan, on)
        amount = round(qty_kg * index["price_per_kg"], 2)
        self._append(
            Event(
                "delivery_settled",
                {
                    "contract_id": contract_id,
                    "batch_id": batch_id,
                    "index_version": index["version"],
                    "qty_kg": qty_kg,
                    "amount": amount,
                    "period_start": _iso(plan["settlement_period"]["start"]),
                    "period_end": _iso(plan["settlement_period"]["end"]),
                },
                on,
                f"settlement:{contract_id}:{batch_id}:{_iso(on)}",
                actor,
            )
        )
        for item in ledger.open_work_items():
            if item["kind"] == "settlement" and item["ref_id"] == contract_id:
                self._append(
                    Event(
                        "work_item_completed",
                        {"work_id": item["work_id"], "actor": actor},
                        on,
                        f"work:{item['work_id']}:completed:{actor}:{_iso(on)}",
                        actor,
                    )
                )
        return {"index_version": index["version"], "qty_kg": qty_kg, "amount": amount}

    @staticmethod
    def _index_applicable_on(ledger: Ledger, plan: dict[str, Any], on: date) -> dict[str, Any]:
        period = plan["settlement_period"]
        candidates = [
            i
            for i in ledger.price_indices
            if i["published_on"] <= on and i["period_start"] <= on <= i["period_end"]
        ]
        if not candidates:
            raise RuleError("结算日没有已发布且适用期间明确覆盖的价格指数版本")
        # 计划固化版本在其期间仍覆盖该日时继续适用，防止新版本向前串期间
        frozen_version = plan.get("price_index_version")
        frozen = next(
            (i for i in candidates if i["version"] == frozen_version), None
        )
        if frozen is not None and frozen["period_start"] == period["start"]:
            return frozen
        return max(candidates, key=lambda i: i["version"])

    # ===================================================================
    # 工作项（农时、复检、结算、补种）——事件持久化，重启重放即恢复
    # ===================================================================

    @staticmethod
    def _work_event(
        work_id: str, kind: str, due_on: date, ref_type: str, ref_id: str,
        note: str, on: date, actor: str,
    ) -> Event:
        return Event(
            "work_item_scheduled",
            {"work_id": work_id, "kind": kind, "due_on": _iso(due_on),
             "ref_type": ref_type, "ref_id": ref_id, "note": note},
            on,
            f"work:{work_id}",
            actor,
        )

    def _schedule(self, work_id, kind, due_on, ref_type, ref_id, note, on, actor) -> None:
        ledger = self._ledger()
        if f"work:{work_id}" in ledger.idem_keys:
            return
        self._append(
            self._work_event(work_id, kind, due_on, ref_type, ref_id, note, on, actor)
        )

    def schedule_work(
        self, kind: str, due_on: date, ref_type: str, ref_id: str,
        on: date, actor: str, note: str = "",
    ) -> str:
        work_id = f"{kind}:{ref_type}:{ref_id}:{_iso(due_on)}"
        self._schedule(work_id, kind, due_on, ref_type, ref_id, note, on, actor)
        return work_id

    def complete_work(self, work_id: str, actor: str, on: date) -> None:
        ledger = self._ledger()
        item = ledger.work_items.get(work_id)
        if item is None:
            raise RuleError("工作项不存在")
        if item["status"] != "open":
            raise RuleError("工作项已完成")
        self._append(
            Event(
                "work_item_completed",
                {"work_id": work_id, "actor": actor},
                on,
                f"work:{work_id}:completed:{actor}:{_iso(on)}",
                actor,
            )
        )

    def due_work(self, due_by: date) -> list[dict[str, Any]]:
        return self._ledger().open_work_items(due_by)

    # ===================================================================
    # 批次可解释查询：授权、田间过程、受托等级、最终去向
    # ===================================================================

    def explain_batch(self, batch_id: str) -> dict[str, Any]:
        ledger = self._ledger()
        batch = ledger.seed_batches.get(batch_id)
        if batch is None:
            raise RuleError("批次不存在")
        contract = ledger.contracts[batch["contract_id"]]
        plan = ledger.current_plan(contract["contract_id"])
        # 授权：合同当时固化的那一版，以及当前有效版本（可能已续签）
        grant = ledger.grant_by_ref(plan["grant_ref"])
        current_grant = ledger.current_grant(
            contract["variety_id"], contract["enterprise_id"])
        trustee = ledger.trustees[contract["trustee_id"]]
        facts = sorted(
            (
                f
                for f in ledger.facts
                if f.get("batch_id") == batch_id
                or (
                    f["contract_id"] == contract["contract_id"]
                    and f.get("batch_id") is None
                    and f["kind"] in ("flowering", "roguing", "inspection")
                )
            ),
            key=lambda f: (f["on"], f["seq"]),
        )
        contract_facts = sorted(
            ledger.facts_of(contract["contract_id"]), key=lambda f: (f["on"], f["seq"])
        )
        freezes = [
            {
                "kind": item["kind"],
                "fact_ids": item["fact_ids"],
                "on": _iso(item["on"]),
                "resolution": None
                if item["resolution"] is None
                else {**item["resolution"], "on": _iso(item["resolution"]["on"])},
            }
            for item in ledger.contradictions
            if item["batch_id"] == batch_id
        ]
        inspections = [
            f for f in contract_facts if f["kind"] == "inspection"
        ]
        destinations = sorted(
            {h["destination"] for h in batch["handovers"] if h["destination"]}
        )
        if batch["status"] == "rejected":
            final_destination = "rejected"
        elif destinations:
            final_destination = destinations[-1]
        else:
            final_destination = "pending"
        return {
            "batch_id": batch_id,
            "contract_id": contract["contract_id"],
            "variety_id": contract["variety_id"],
            "parent_batch_id": contract["parent_batch_id"],
            "status": batch["status"],
            "sewn_area_mu": batch["sewn_area_mu"],
            "damaged_area_mu": batch["damaged_area_mu"],
            "failed_area_mu": batch["failed_area_mu"],
            "delivered_kg": batch["delivered_kg"],
            "authorization": {
                "grant_ref": grant["grant_ref"] if grant else plan["grant_ref"],
                "valid_from": _iso(grant["valid_from"]) if grant else None,
                "valid_to": _iso(grant["valid_to"]) if grant and grant["valid_to"] else None,
                "revoked_on": _iso(grant["revoked_on"]) if grant and grant["revoked_on"] else None,
                "revoke_reason": grant["revoke_reason"] if grant else None,
                "snapshot_ref_in_plan": plan["grant_ref"],
                "current_grant_ref": current_grant["grant_ref"] if current_grant else None,
                "current_revoked_on": _iso(current_grant["revoked_on"])
                if current_grant and current_grant["revoked_on"] else None,
            },
            "trustee": {
                "trustee_id": contract["trustee_id"],
                "rating_at_contract": {
                    "version": plan["rating_version"],
                    "level": plan["rating_level"],
                },
                "rating_current": {
                    "version": trustee["current"]["version"],
                    "level": trustee["current"]["level"],
                    "published_on": _iso(trustee["current"]["published_on"]),
                },
            },
            "plan_version": plan["version"],
            "field_process": [
                {
                    "fact_id": f["fact_id"],
                    "kind": f["kind"],
                    "on": _iso(f["on"]),
                    "source": f["source"],
                    "conclusion": f["conclusion"],
                    "area_mu": f["area_mu"],
                    "qty_kg": f["qty_kg"],
                    "evidence_refs": f["evidence_refs"],
                    "duplicates": f.get("duplicates", 0),
                }
                for f in facts
            ],
            "inspections": [
                {"fact_id": f["fact_id"], "on": _iso(f["on"]),
                 "source": f["source"], "conclusion": f["conclusion"]}
                for f in inspections
            ],
            "freezes": freezes,
            "handovers": [
                {
                    "fact_id": h["fact_id"],
                    "on": _iso(h["on"]),
                    "qty_kg": h["qty_kg"],
                    "destination": h["destination"],
                    "source": h["source"],
                    "evidence_refs": h["evidence_refs"],
                }
                for h in batch["handovers"]
            ],
            "settlements": [
                {
                    "on": _iso(s["on"]),
                    "index_version": s["index_version"],
                    "qty_kg": s["qty_kg"],
                    "amount": s["amount"],
                }
                for s in ledger.settlements
                if s["batch_id"] == batch_id
            ],
            "final_destination": final_destination,
        }

    # ---- 小工具 ---------------------------------------------------------

    @staticmethod
    def _sewn_area(ledger: Ledger, contract_id: str) -> float:
        return sum(
            b["sewn_area_mu"]
            for b in ledger.seed_batches.values()
            if b["contract_id"] == contract_id
        )

    @staticmethod
    def _latest_batch_id(ledger: Ledger, contract_id: str) -> str | None:
        batches = [
            b for b in ledger.seed_batches.values() if b["contract_id"] == contract_id
        ]
        if not batches:
            return None
        return sorted(batches, key=lambda b: b["created_on"])[-1]["batch_id"]
