"""制种委托领域服务。

所有命令都先做准入判断，再把结论以事实形式追加进日志；读模型由
``state.py`` 回放得到。服务重启即重放日志，农时、复检与结算任务
不会丢失。

关键规则
========

* **合同成立**：田块窗口与隔离、受托人等级与能力、亲本批次余量、
  品种授权与适宜区域、结算价格指数，任一不满足则合同不成立
  （记录 ``PLAN_REJECTED``）。
* **唯一安排**：重叠农时争用田块或受托人时，按当时公开的规则簿
  版本排序，一个争用组只产生一个赢家。
* **版本固化**：成立当时把适宜区域、隔离、目标面积、结算口径
  （价格指数与结算期间）写入计划快照；调度只能改写未播种部分，
  已播种窗口被锁定。
* **事实追加**：生产事件按来源追加；同源同号报送幂等返回原结论；
  异源结论矛盾则冻结批次，交独立人员复核。
* **依赖重算**：灾害、授权撤回、检验不合格沿
  亲本批次→计划→批次的实际依赖重算可交付量、补种责任与受影响
  合同，已交接证据原样保留。
* **结算**：价格指数新版本只进入明确标注该结算期间的期间；
  结算线一经固化不可变。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .journal import Journal
from .models import BatchStatus, FactType
from .state import State, _overlaps

NumericTolerance = 1e-6


class DomainError(ValueError):
    """命令本身不合法（区别于合同评审未通过的正常决策）。"""


def _day_of(value: str) -> str:
    return value[:10]


def _add_days(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _same_number(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=NumericTolerance, abs_tol=NumericTolerance)


class SeedProductionService:
    """制种委托的全部命令与查询入口。"""

    def __init__(self, journal: str | Path | Journal,
                 clock: Callable[[], str] | None = None):
        self.journal = journal if isinstance(journal, Journal) else Journal(journal)
        self.clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.state = State()
        # 停机恢复：重放全部已落盘事实（含到期任务）。
        for fact in self.journal.read_all():
            self.state.apply(fact)

    def _now(self, day: str | None) -> str:
        return day or self.clock()

    # ================================================================
    # 公开版本资料
    # ================================================================

    def register_authorization(self, company_id: str, variety_id: str, version: int,
                               regions: list[str], min_grade: int,
                               effective_from: str, effective_to: str | None = None,
                               isolation_required_m: float = 100.0,
                               source: str = "authority") -> dict[str, Any]:
        existing = [a for a in self.state.authorizations.get(
            (company_id, variety_id), []) if a.version == version]
        if existing:
            raise DomainError("授权版本已存在，不得覆盖；请发布新版本")
        fact = self._append(FactType.VARIETY_AUTHORIZATION, {
            "company_id": company_id, "variety_id": variety_id,
            "version": version, "regions": list(regions),
            "min_grade": min_grade,
            "isolation_required_m": isolation_required_m,
            "effective_from": effective_from, "effective_to": effective_to,
        }, source)
        return {"seq": fact.seq}

    def withdraw_authorization(self, company_id: str, variety_id: str,
                               version: int, day: str | None = None,
                               source: str = "authority") -> dict[str, Any]:
        at = self._now(day)
        target = next((a for a in self.state.authorizations.get(
            (company_id, variety_id), []) if a.version == version), None)
        if target is None:
            raise DomainError("撤回的授权版本不存在")
        if target.withdrawn:
            raise DomainError("授权版本已撤回，不得重复撤回")
        self._append(FactType.AUTH_WITHDRAWN, {
            "company_id": company_id, "variety_id": variety_id,
            "version": version, "withdrawn_at": _day_of(at),
        }, source)
        # 沿实际依赖重算所有受影响合同。
        decisions = []
        for plan_id, plan in self.state.plans.items():
            snap = plan.snapshot
            if (plan.status == "confirmed" and snap is not None
                    and plan.company_id == company_id
                    and plan.variety_id == variety_id):
                decisions.append(self._recompute(
                    plan_id, trigger="authorization_withdrawn",
                    at=_day_of(at), detail={"auth_version": version}))
        return {"withdrawn_at": _day_of(at), "affected_plans": decisions}

    def register_trustee_grade(self, trustee_id: str, grade: int, capacity_mu: float,
                               effective_from: str, effective_to: str | None = None,
                               source: str = "authority") -> dict[str, Any]:
        fact = self._append(FactType.TRUSTEE_GRADE, {
            "trustee_id": trustee_id, "grade": grade,
            "capacity_mu": capacity_mu,
            "effective_from": effective_from, "effective_to": effective_to,
        }, source)
        return {"seq": fact.seq}

    def publish_price_index(self, index_id: str, version: int, period: str,
                            values: dict[str, float], period_start: str,
                            period_end: str, source: str = "authority") -> dict[str, Any]:
        if any(idx.version == version for idx in self.state.price_indices.get(index_id, [])):
            raise DomainError("价格指数版本已存在；新版本必须显式标注结算期间")
        fact = self._append(FactType.PRICE_INDEX, {
            "index_id": index_id, "version": version, "period": period,
            "values": dict(values), "period_start": period_start,
            "period_end": period_end,
        }, source)
        return {"seq": fact.seq}

    def publish_rulebook(self, version: int, priorities: list[dict[str, str]],
                         effective_from: str, source: str = "authority") -> dict[str, Any]:
        if version in self.state.rulebooks:
            raise DomainError("规则簿版本已存在；请发布新版本")
        orders = {p["key"]: p.get("order", "asc") for p in priorities}
        invalid = {v for v in orders.values() if v not in ("asc", "desc")}
        if invalid:
            raise DomainError("优先规则方向只能是 asc 或 desc")
        fact = self._append(FactType.RULEBOOK_PUBLISHED, {
            "version": version, "priorities": list(priorities),
            "effective_from": effective_from,
        }, source)
        return {"seq": fact.seq}

    def register_field(self, field_id: str, region: str, area_mu: float,
                       buffer_m: float, windows: list[tuple[str, str]],
                       source: str = "registry") -> dict[str, Any]:
        if field_id in self.state.fields:
            raise DomainError("田块已登记")
        fact = self._append(FactType.FIELD_REGISTERED, {
            "field_id": field_id, "region": region, "area_mu": area_mu,
            "buffer_m": buffer_m,
            "windows": [{"start": s, "end": e} for s, e in windows],
        }, source)
        return {"seq": fact.seq}

    def register_parent_batch(self, parent_batch_id: str, variety_id: str,
                              quantity_mu: float, source: str = "warehouse") -> dict[str, Any]:
        if parent_batch_id in self.state.parent_batches:
            raise DomainError("亲本批次已登记")
        fact = self._append(FactType.PARENT_BATCH_REGISTERED, {
            "parent_batch_id": parent_batch_id, "variety_id": variety_id,
            "quantity_mu": quantity_mu,
        }, source)
        return {"seq": fact.seq}

    # ================================================================
    # 合同提案、成立判断与争用仲裁
    # ================================================================

    def propose_contract(self, plan_id: str, company_id: str, trustee_id: str,
                         variety_id: str, allocations: list[dict[str, Any]],
                         parent_batch_id: str, settlement_period: str,
                         index_id: str, source: str = "company") -> dict[str, Any]:
        if plan_id in self.state.plans:
            raise DomainError("计划已存在")
        windows = self._normalize_allocations(allocations)
        self._append(FactType.PLAN_PROPOSED, {
            "plan_id": plan_id, "company_id": company_id,
            "trustee_id": trustee_id, "variety_id": variety_id,
            "field_windows": windows,
            "target_area_mu": round(sum(w["area_mu"] for w in windows), 4),
            "parent_batch_id": parent_batch_id,
            "settlement": {"index_id": index_id, "period": settlement_period},
            "source": source,
        }, source)
        return {"plan_id": plan_id, "status": "proposed",
                "target_area_mu": round(sum(w["area_mu"] for w in windows), 4)}

    def confirm_contract(self, plan_id: str, day: str | None = None) -> dict[str, Any]:
        """提案 + 仲裁的便捷入口；返回该计划的唯一结论。"""
        if self.state.plans[plan_id].status != "proposed":
            raise DomainError("计划不是待决提案")
        return self._arbitrate(_day_of(self._now(day)), only=plan_id)[0]

    def arbitrate(self, day: str | None = None) -> list[dict[str, Any]]:
        """对全部待决提案按公开规则簿给出唯一安排。"""
        return self._arbitrate(_day_of(self._now(day)))

    def _arbitrate(self, day: str, only: str | None = None) -> list[dict[str, Any]]:
        rulebook = self.state.rulebook(day)
        if rulebook is None:
            raise DomainError("仲裁日没有已发布的公开争用规则簿")
        pending = [p for p in self.state.plans.values() if p.status == "proposed"]
        if only is not None:
            pending = [p for p in pending if p.plan_id == only]
        if not pending:
            return []

        assessed: list[tuple[Any, dict[str, Any]]] = [
            (plan, self._assess_proposal(plan, day)) for plan in pending]
        info_of = {p.plan_id: info for p, info in assessed}

        # 不可行提案（授权/等级/隔离/窗口/指数等硬条件不满足）直接否决，
        # 不参与资源争用。
        infeasible = [p for p, _ in assessed if not info_of[p.plan_id]["feasible"]]
        candidates = sorted(
            (p for p, _ in assessed if info_of[p.plan_id]["feasible"]),
            key=lambda p: self._priority_tuple(p, day, rulebook.priorities))

        # 唯一安排：按公开优先级从高到低贪心裁定。
        #   * 田块：与已成立提案在同一田块重叠农时 → 争用，后者出局；
        #   * 受托人：当季累计承诺面积超过等级能力上限 → 争用，后者出局；
        #   * 亲本：批次余量不足以再支撑本计划 → 争用，后者出局。
        # 互不冲突的计划可以共存；plan_id 兜底键保证任何情况下结论唯一。
        confirmed: list[Any] = []
        parent_hold: dict[str, float] = {}
        trustee_hold: dict[str, float] = {}
        winners_by_field: list[Any] = []

        def conflicts_with_won(cand: Any) -> tuple[bool, str, str | None]:
            # 田块重叠农时
            for won in confirmed:
                for x in cand.proposal["field_windows"]:
                    for y in won.proposal["field_windows"]:
                        if x["field_id"] == y["field_id"] and _overlaps(
                                x["start"], x["end"], y["start"], y["end"]):
                            return True, "field_overlap", won.plan_id
            # 受托人当季能力
            grade = self.state.trustee_grade(cand.trustee_id, day)
            total = (self.state.trustee_load(cand.trustee_id, day)
                     + trustee_hold.get(cand.trustee_id, 0.0)
                     + cand.proposal["target_area_mu"])
            if grade is None or total > grade.capacity_mu + 1e-9:
                return True, "trustee_capacity", None
            # 亲本批次余量
            pid = cand.proposal["parent_batch_id"]
            parent = self.state.parent_batches[pid]
            if parent.remaining_mu - parent_hold.get(pid, 0.0) + 1e-9 \
                    < cand.proposal["target_area_mu"]:
                return True, "parent_batch", None
            return False, "", None

        decisions: list[dict[str, Any]] = []
        for cand in candidates:
            conflict, code, winner_id = conflicts_with_won(cand)
            if conflict:
                detail = (f"争用唯一安排归 {winner_id}（规则簿 v{rulebook.version}）"
                          if winner_id else
                          "资源在公开优先规则下已被更优先合同占用")
                decisions.append(self._reject(cand, "lost_arbitration", [detail]))
                continue
            pid = cand.proposal["parent_batch_id"]
            parent_hold[pid] = round(
                parent_hold.get(pid, 0.0) + cand.proposal["target_area_mu"], 4)
            trustee_hold[cand.trustee_id] = round(
                trustee_hold.get(cand.trustee_id, 0.0)
                + cand.proposal["target_area_mu"], 4)
            confirmed.append(cand)
            winners_by_field.append(cand)
            decisions.append(
                self._confirm(cand, day, info_of[cand.plan_id]["resolutions"]))

        for plan in infeasible:
            decisions.append(
                self._reject(plan, "infeasible", info_of[plan.plan_id]["reasons"]))
        return sorted(decisions, key=lambda d: d["plan_id"])

    def _priority_tuple(self, plan: Any, day: str,
                        priorities: tuple[dict[str, str], ...]) -> tuple:
        grade = self.state.trustee_grade(plan.trustee_id, day)
        values = {
            "proposed_seq": plan.proposed_seq,
            "grade": grade.grade if grade else -1,
            "capacity_mu": grade.capacity_mu if grade else -1.0,
            "target_area_mu": plan.proposal["target_area_mu"],
            "company_id": plan.company_id,
            "trustee_id": plan.trustee_id,
            "plan_id": plan.plan_id,
        }
        out: list[Any] = []
        for rule in priorities:
            key, order = rule["key"], rule.get("order", "asc")
            value = values.get(key)
            if value is None:
                raise DomainError(f"规则簿引用了未知优先键：{key}")
            if isinstance(value, str):
                # 字符串方向通过倒序表实现；数值直接取反。
                out.append(value if order == "asc" else _ReverseText(value))
            else:
                out.append(value if order == "asc" else -value)
        # 系统级确定性兜底：任何情况下结论唯一。
        if not any(r["key"] == "plan_id" for r in priorities):
            out.append(plan.plan_id)
        return tuple(out)

    def _assess_proposal(self, plan: Any, day: str) -> dict[str, Any]:
        proposal = plan.proposal
        windows = proposal["field_windows"]
        reasons: list[str] = []
        resolutions: dict[str, Any] = {}

        auth = self.state.authorization(plan.company_id, plan.variety_id, day)
        if auth is None:
            reasons.append("品种授权在评审日无效或已撤回")
        else:
            resolutions["auth_version"] = auth.version

        grade = self.state.trustee_grade(plan.trustee_id, day)
        if grade is None:
            reasons.append("受托人在评审日没有有效等级版本")
        else:
            resolutions["grade"] = {"version_day": day, "grade": grade.grade,
                                    "capacity_mu": grade.capacity_mu}

        if auth and grade and grade.grade < auth.min_grade:
            reasons.append(
                f"受托人等级 {grade.grade} 低于授权要求 {auth.min_grade}")

        total = proposal["target_area_mu"]
        area_by_field: dict[str, float] = {}
        parent = self.state.parent_batches.get(proposal["parent_batch_id"])
        if parent is None:
            reasons.append("亲本批次不存在")
        elif parent.variety_id != plan.variety_id:
            reasons.append("亲本批次与生产品种不一致")
        # 待决提案之间的亲本余量争用由仲裁按优先顺序统一分配。

        field_area: dict[str, float] = {}
        for w in windows:
            field = self.state.fields.get(w["field_id"])
            if field is None:
                reasons.append(f"田块 {w['field_id']} 未登记")
                continue
            field_area[field.field_id] = field_area.get(field.field_id, 0.0) + w["area_mu"]
            if not self.state.field_window_covered(field.field_id, w["start"], w["end"]):
                reasons.append(f"田块 {field.field_id} 在 {w['start']}~{w['end']} 无可用农时窗口")
            if auth and field.region not in auth.regions:
                reasons.append(f"田块 {field.field_id} 不在授权适宜区域")
            if auth and field.buffer_m < auth.isolation_required_m - 1e-9:
                reasons.append(
                    f"田块 {field.field_id} 隔离 {field.buffer_m}m 不足要求 "
                    f"{auth.isolation_required_m}m")
        for fid, area in field_area.items():
            field = self.state.fields[fid]
            if area > field.area_mu + 1e-9:
                reasons.append(f"田块 {fid} 排程面积 {area} 超过地块面积 {field.area_mu}")

        for entry in self.state.conflicts_on(windows, ignore_plan=plan.plan_id):
            other = self.state.plans[entry[3]]
            if other.status == "confirmed":
                reasons.append(
                    f"田块 {entry[0]} 在 {entry[1]}~{entry[2]} 已被合同 "
                    f"{entry[3]} 排他占用")
        if grade is not None:
            load = self.state.trustee_load(plan.trustee_id, day,
                                           ignore_plan=plan.plan_id)
            # 同组待决提案之间的能力争用交给仲裁，这里只核对已成立合同。
            if load + total > grade.capacity_mu + 1e-9:
                reasons.append(
                    f"受托人能力不足：已承诺 {load} 亩，本计划 {total} 亩，"
                    f"上限 {grade.capacity_mu} 亩")

        settlement = proposal["settlement"]
        # 合同成立时固化结算口径：必须已有明确标注该结算期间的指数版本，
        # 但不要求评审日落在期间内（春订秋结）。
        index = self._index_version_for_period(
            settlement["index_id"], settlement["period"])
        if index is None:
            reasons.append(
                f"结算期间 {settlement['period']} 在评审日没有明确适用的价格指数版本")
        elif plan.variety_id not in index.values:
            reasons.append("价格指数缺少该品种单价")
        else:
            resolutions["settlement"] = {
                "index_id": settlement["index_id"],
                "period": settlement["period"],
                "version": index.version,
            }
        return {"feasible": not reasons, "reasons": reasons,
                "resolutions": resolutions}

    def _confirm(self, plan: Any, day: str, resolutions: dict[str, Any]) -> dict[str, Any]:
        proposal = plan.proposal
        windows = proposal["field_windows"]
        batch_id = f"B-{plan.plan_id}"
        data = {
            "plan_id": plan.plan_id, "plan_version": 1,
            "company_id": plan.company_id, "trustee_id": plan.trustee_id,
            "variety_id": plan.variety_id,
            "suitable_regions": list(self.state.authorization(
                plan.company_id, plan.variety_id, day).regions),
            "isolation_m": self.state.authorization(
                plan.company_id, plan.variety_id, day).isolation_required_m,
            "target_area_mu": proposal["target_area_mu"],
            "settlement": resolutions["settlement"],
            "confirmed_at": day,
            "field_windows": windows,
            "parent_batch_id": proposal["parent_batch_id"],
            "rulebook_version": self.state.rulebook(day).version,
            "auth_version": resolutions["auth_version"],
            "trustee_grade": resolutions["grade"],
        }
        self._append(FactType.PLAN_CONFIRMED, data, plan.proposal.get("source", "company"))
        self._append(FactType.BATCH_OPENED, {
            "batch_id": batch_id, "plan_id": plan.plan_id,
            "parent_batch_id": proposal["parent_batch_id"],
            "variety_id": plan.variety_id,
            "field_ids": sorted({w["field_id"] for w in windows}),
        }, "platform")
        # 把每个排程窗口登记为到期前不得丢失的农时任务。
        self._register_window_tasks(plan.plan_id, windows, day)
        return {"plan_id": plan.plan_id, "result": "confirmed",
                "plan_version": 1, "batch_id": batch_id,
                "resolutions": resolutions}

    def _reject(self, plan: Any, code: str, reasons: list[str]) -> dict[str, Any]:
        self._append(FactType.PLAN_REJECTED, {
            "plan_id": plan.plan_id, "reason": "；".join(reasons),
            "code": code,
        }, "platform")
        return {"plan_id": plan.plan_id, "result": "rejected",
                "code": code, "reasons": reasons}

    # ================================================================
    # 调度改写与越级调整
    # ================================================================

    def reschedule(self, plan_id: str, allocations: list[dict[str, Any]],
                   day: str | None = None, source: str = "dispatcher") -> dict[str, Any]:
        plan = self.state.plans.get(plan_id)
        if plan is None or plan.status != "confirmed":
            raise DomainError("只能对已成立合同做调度")
        day = _day_of(self._now(day))
        windows = self._normalize_allocations(allocations)
        target = round(sum(w["area_mu"] for w in windows), 4)
        self._validate_reschedule(plan, windows, target, day)
        new_version = len(plan.versions) + 1
        self._append(FactType.PLAN_RESCHEDULED, {
            "plan_id": plan_id, "plan_version": new_version,
            "field_windows": windows, "target_area_mu": target,
            "reason": "regular_dispatch", "changed_by": source,
        }, source)
        self._register_window_tasks(plan_id, windows, day)
        return {"plan_id": plan_id, "result": "rescheduled",
                "plan_version": new_version, "target_area_mu": target}

    def _register_window_tasks(self, plan_id: str,
                               windows: list[dict[str, Any]], day: str) -> None:
        """为新增的排程窗口登记到期农时任务（已登记的不重复）。"""
        for w in windows:
            self._schedule_task(
                f"T-farm-{plan_id}-{w['field_id']}-{w['end']}",
                "farming_window", w["end"], plan_id,
                f"田块 {w['field_id']} 农时窗口 {w['start']}~{w['end']} 到期",
                created=day)

    def propose_override(self, manager_id: str, plan_id: str,
                         allocations: list[dict[str, Any]], reason: str,
                         day: str | None = None) -> dict[str, Any]:
        plan = self.state.plans.get(plan_id)
        if plan is None or plan.status != "confirmed":
            raise DomainError("只能对已成立合同提出越级调整")
        windows = self._normalize_allocations(allocations)
        target = round(sum(w["area_mu"] for w in windows), 4)
        proposal_id = f"OV-{plan_id}-{len(plan.override_proposals) + 1}"
        self._append(FactType.PLAN_OVERRIDE, {
            "proposal_id": proposal_id, "plan_id": plan_id,
            "plan_version": len(plan.versions) + 1,
            "manager_id": manager_id, "reason": reason,
            "field_windows": windows, "target_area_mu": target,
        }, manager_id)
        return {"proposal_id": proposal_id, "status": "pending_second_manager_review"}

    def review_override(self, reviewer_id: str, proposal_id: str,
                        approved: bool, day: str | None = None,
                        reason: str = "") -> dict[str, Any]:
        proposal = next((p for pl in self.state.plans.values()
                         for p in pl.override_proposals
                         if p["proposal_id"] == proposal_id), None)
        if proposal is None:
            raise DomainError("越级调整提案不存在")
        if proposal.get("review") is not None:
            raise DomainError("提案已经复核，结论不可改变")
        if reviewer_id == proposal["manager_id"]:
            raise DomainError("越级调整必须由另一名管理者复核")
        day = _day_of(self._now(day))
        plan = self.state.plans[proposal["plan_id"]]
        if approved:
            windows = proposal["field_windows"]
            self._validate_reschedule(
                plan, windows, float(proposal["target_area_mu"]), day)
            self._append(FactType.OVERRIDE_APPROVED, {
                "proposal_id": proposal_id, "plan_id": plan.plan_id,
                "reviewer": reviewer_id, "reviewed_at": day,
            }, reviewer_id)
            self._register_window_tasks(
                plan.plan_id, proposal["field_windows"], day)
            result = "approved"
        else:
            self._append(FactType.OVERRIDE_REJECTED, {
                "proposal_id": proposal_id, "plan_id": plan.plan_id,
                "reviewer": reviewer_id, "reviewed_at": day,
                "reason": reason,
            }, reviewer_id)
            result = "rejected"
        return {"proposal_id": proposal_id, "result": result,
                "reviewer": reviewer_id}

    def _validate_reschedule(self, plan: Any, windows: list[dict[str, Any]],
                             target: float, day: str) -> None:
        if target < plan.sown_total_mu - 1e-9:
            raise DomainError("调度后面积小于已播种面积，已播种部分不得改写")
        snap = plan.snapshot

        # 已播种部分锁定：每个已播种田块在新排程中必须有窗口覆盖原窗口，
        # 且该田块排程总面积不得低于已播种面积。未播种余量可以自由改派。
        new_area_by_field: dict[str, float] = {}
        new_windows_by_field: dict[str, list[dict[str, Any]]] = {}
        for w in windows:
            new_area_by_field[w["field_id"]] = round(
                new_area_by_field.get(w["field_id"], 0.0) + w["area_mu"], 4)
            new_windows_by_field.setdefault(w["field_id"], []).append(w)
        for old_w in snap.field_windows:
            fid = old_w["field_id"]
            sown_here = plan.sown_by_field.get(fid, 0.0)
            if sown_here <= 0:
                continue
            if new_area_by_field.get(fid, 0.0) < sown_here - 1e-9:
                raise DomainError(
                    f"田块 {fid} 已播种 {sown_here} 亩，调度不得改写到该面积以下")
            covered = any(
                nw["start"] <= old_w["start"] and nw["end"] >= old_w["end"]
                for nw in new_windows_by_field.get(fid, []))
            if not covered:
                raise DomainError(
                    f"田块 {fid} 已播种窗口 {old_w['start']}~{old_w['end']} "
                    "被锁定，不能随调度改写")

        field_area: dict[str, float] = {}
        for w in windows:
            field = self.state.fields.get(w["field_id"])
            if field is None:
                raise DomainError(f"田块 {w['field_id']} 未登记")
            field_area[field.field_id] = field_area.get(field.field_id, 0.0) + w["area_mu"]
            if not self.state.field_window_covered(field.field_id, w["start"], w["end"]):
                raise DomainError(f"田块 {field.field_id} 无可用农时窗口")
            auth = self.state.authorization(plan.company_id, plan.variety_id, day)
            if auth is None:
                raise DomainError("品种授权已失效，不能扩大排程")
            if field.region not in auth.regions:
                raise DomainError(f"田块 {field.field_id} 不在适宜区域")
            if field.buffer_m < snap.isolation_m - 1e-9:
                raise DomainError(f"田块 {field.field_id} 隔离不满足固化要求")
        for fid, area in field_area.items():
            if area > self.state.fields[fid].area_mu + 1e-9:
                raise DomainError(f"田块 {fid} 面积超限")
        for entry in self.state.conflicts_on(windows, ignore_plan=plan.plan_id):
            raise DomainError(
                f"田块 {entry[0]} 在 {entry[1]}~{entry[2]} 与合同 {entry[3]} 冲突")
        grade = self.state.trustee_grade(plan.trustee_id, day)
        if grade is None:
            raise DomainError("受托人没有有效等级版本")
        load = self.state.trustee_load(plan.trustee_id, day, ignore_plan=plan.plan_id)
        if load + target > grade.capacity_mu + 1e-9:
            raise DomainError("调度后超出受托人能力上限")
        parent = self.state.parent_batches[snap.parent_batch_id]
        extra = target - plan.target_area_mu
        if extra > 0 and parent.remaining_mu + 1e-9 < extra:
            raise DomainError("亲本批次余量不足以支持扩面")

    # ================================================================
    # 田间与交种事实
    # ================================================================

    def report_sowing(self, batch_id: str, field_id: str, area_mu: float,
                      source: str, report_id: str, day: str | None = None) -> dict[str, Any]:
        return self._report_event(
            FactType.SOWING_REPORTED, batch_id, source, report_id,
            self._now(day), field_id, {"area_mu": round(float(area_mu), 4)})

    def report_flowering(self, batch_id: str, source: str, report_id: str,
                         day: str | None = None, field_id: str | None = None,
                         conclusion: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._report_event(
            FactType.FLOWERING_REPORTED, batch_id, source, report_id,
            self._now(day), field_id, dict(conclusion or {}))

    def report_roguing(self, batch_id: str, source: str, report_id: str,
                       day: str | None = None, field_id: str | None = None,
                       conclusion: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._report_event(
            FactType.ROGUING_REPORTED, batch_id, source, report_id,
            self._now(day), field_id, dict(conclusion or {}))

    def report_inspection(self, batch_id: str, source: str, report_id: str,
                          qualified: bool, day: str | None = None,
                          field_id: str | None = None,
                          retest_due: str | None = None,
                          metrics: dict[str, Any] | None = None) -> dict[str, Any]:
        day = _day_of(self._now(day))
        conclusion = {"result": "qualified" if qualified else "unqualified"}
        conclusion.update(metrics or {})
        out = self._report_event(
            FactType.INSPECTION_REPORTED, batch_id, source, report_id,
            day, field_id, conclusion)
        if out.get("deduplicated") or out.get("frozen"):
            return out
        if not qualified:
            self._freeze_batch(batch_id, "inspection_unqualified", day,
                               source, report_id)
            due = retest_due or _add_days(day, 7)
            self._schedule_task(
                f"T-retest-{batch_id}-{due}", "reinspection", due, batch_id,
                f"批次 {batch_id} 检验不合格，到期前必须完成复检", created=day)
            rec = self._recompute(
                self.state.batches[batch_id].plan_id,
                trigger="inspection_unqualified", at=day,
                detail={"batch_id": batch_id})
            out["recomputation"] = rec
        return out

    def _report_event(self, fact_type: FactType, batch_id: str, source: str,
                      report_id: str, recorded_at: str, field_id: str | None,
                      conclusion: dict[str, Any]) -> dict[str, Any]:
        if not report_id:
            raise DomainError("报送必须带来源方唯一编号，用于去重")
        key = (source, report_id)
        dup = self.state.dedupe.get(key)
        if dup is not None:
            # 重复报送：保持原结论，不产生新事实。
            return {"deduplicated": True, "original_seq": dup["seq"],
                    "conclusion": dup["conclusion"], "batch_id": dup["batch_id"]}
        batch = self.state.batches.get(batch_id)
        if batch is None:
            raise DomainError("生产批次不存在")
        if batch.status == BatchStatus.FROZEN:
            raise DomainError("批次已冻结，等待独立复核，暂停接收新报送")
        if batch.status == BatchStatus.REJECTED:
            raise DomainError("批次已被否决")
        if batch.status == BatchStatus.DELIVERED:
            raise DomainError("批次已完成交接，不再接收田间报送")
        if field_id is not None and field_id not in batch.field_ids:
            raise DomainError("该田块不属于此批次")
        stage = fact_type.value.replace("_reported", "")
        if stage == "sowing":
            plan = self.state.plans[batch.plan_id]
            allocated = self._allocated_area(plan, field_id)
            cumulative = plan.sown_by_field.get(field_id, 0.0) + conclusion["area_mu"]
            if cumulative > allocated + 1e-9:
                raise DomainError(
                    f"田块 {field_id} 累计播种 {cumulative} 超过排程 {allocated} 亩")
        # 播种是累加事实，不同来源的播种面积不构成结论矛盾。
        contradiction = (None if stage == "sowing"
                         else self._find_contradiction(batch, stage, field_id, conclusion))
        fact = self._append(fact_type, {
            "batch_id": batch_id, "field_id": field_id,
            "report_id": report_id, "conclusion": conclusion,
        }, source, recorded_at=recorded_at)
        out = {"seq": fact.seq, "stage": stage, "conclusion": conclusion,
               "batch_id": batch_id}
        if contradiction is not None:
            reason = ("contradictory_reports:"
                      f"{stage}:{field_id or '-'}")
            self._freeze_batch(batch_id, reason, _day_of(recorded_at), source,
                               report_id, extra={"contradicts_seq": contradiction.seq,
                                                 "contradicts_source": contradiction.source})
            due = _add_days(_day_of(recorded_at), 3)
            self._schedule_task(
                f"T-review-{batch_id}-{due}", "independent_review", due, batch_id,
                f"批次 {batch_id} 出现来源矛盾，须独立人员复核", created=_day_of(recorded_at))
            out["frozen"] = True
            out["contradicts"] = {"seq": contradiction.seq,
                                  "source": contradiction.source}
            out["recomputation"] = self._recompute(
                batch.plan_id, trigger="contradictory_reports",
                at=_day_of(recorded_at),
                detail={"batch_id": batch_id,
                        "contradicts_seq": contradiction.seq})
        elif stage == "sowing":
            plan = self.state.plans[batch.plan_id]
            if plan.sown_total_mu >= plan.target_area_mu - 1e-9:
                for task in list(self.state.tasks.values()):
                    if (task.kind == "farming_window" and not task.done
                            and task.ref_id == plan.plan_id):
                        self._complete_task(task.task_id, _day_of(recorded_at))
        return out

    @staticmethod
    def _allocated_area(plan: Any, field_id: str) -> float:
        return round(sum(w["area_mu"] for w in plan.snapshot.field_windows
                         if w["field_id"] == field_id), 4)

    def _find_contradiction(self, batch: Any, stage: str, field_id: str | None,
                            conclusion: dict[str, Any]) -> Any:
        """与既有结论比对；同源同号报送由去重索引提前拦截，
        能走到这里的不同结论一律视为矛盾，交独立人员复核。"""
        for prev in batch.events:
            if prev.stage == stage and prev.field_id == field_id:
                if self._conclusions_conflict(prev.conclusion, conclusion):
                    return prev
        return None

    @staticmethod
    def _conclusions_conflict(a: dict[str, Any], b: dict[str, Any]) -> bool:
        enum_keys = {"result", "status", "qualification"}
        for key in enum_keys & a.keys() & b.keys():
            if a[key] != b[key]:
                return True
        numeric_conflict = False
        for key in (a.keys() & b.keys()) - enum_keys:
            x, y = a[key], b[key]
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                numeric_conflict = numeric_conflict or not _same_number(
                    float(x), float(y))
        return numeric_conflict

    def _freeze_batch(self, batch_id: str, reason: str, day: str,
                      source: str, report_id: str,
                      extra: dict[str, Any] | None = None) -> None:
        data = {"batch_id": batch_id, "reason": reason,
                "trigger_source": source, "trigger_report_id": report_id}
        if extra:
            data.update(extra)
        self._append(FactType.DISCREPANCY_FROZEN, data, "platform")

    def resolve_review(self, reviewer_id: str, batch_id: str,
                       qualified: bool, note: str, day: str | None = None) -> dict[str, Any]:
        batch = self.state.batches.get(batch_id)
        if batch is None:
            raise DomainError("批次不存在")
        if batch.status != BatchStatus.FROZEN:
            raise DomainError("只有冻结批次需要独立复核")
        day = _day_of(self._now(day))
        decision = "qualified" if qualified else "rejected"
        self._append(FactType.REVIEW_RESOLVED, {
            "batch_id": batch_id, "decision": decision,
            "reviewer": reviewer_id, "note": note, "resolved_at": day,
        }, reviewer_id)
        for task in list(self.state.tasks.values()):
            if not task.done and task.ref_id == batch_id and task.kind in (
                    "reinspection", "independent_review"):
                self._complete_task(task.task_id, day)
        rec = self._recompute(
            batch.plan_id,
            trigger="review_unqualified" if not qualified else "review_qualified",
            at=day, detail={"batch_id": batch_id, "reviewer": reviewer_id})
        # 补种责任由重算按增量统一落实，这里不再重复派责。
        return {"batch_id": batch_id, "decision": decision, "recomputation": rec}

    def deliver_seed(self, batch_id: str, quantity_mu: float, to: str,
                     evidence: list[str], source: str, report_id: str,
                     day: str | None = None, settlement_due: str | None = None) -> dict[str, Any]:
        key = (source, report_id)
        dup = self.state.dedupe.get(key)
        if dup is not None:
            return {"deduplicated": True, "original_seq": dup["seq"],
                    "conclusion": dup["conclusion"], "batch_id": dup["batch_id"]}
        batch = self.state.batches.get(batch_id)
        if batch is None:
            raise DomainError("批次不存在")
        if batch.status in (BatchStatus.FROZEN, BatchStatus.REJECTED):
            raise DomainError("冻结或已否决批次不能交种")
        plan = self.state.plans[batch.plan_id]
        quantity_mu = round(float(quantity_mu), 4)
        if quantity_mu <= 0:
            raise DomainError("交种数量必须为正")
        # 授权撤回后未交付部分不再具备合法交付依据；已完成交接的证据保留。
        day = _day_of(self._now(day))
        if self.state.authorization(plan.company_id, plan.variety_id, day) is None:
            raise DomainError("品种授权已撤回或失效，未交付部分不能交种")
        if plan.delivered_mu + quantity_mu > plan.sown_total_mu + 1e-9:
            raise DomainError("累计交种超过已播种面积")
        fact = self._append(FactType.SEED_DELIVERED, {
            "batch_id": batch_id, "quantity_mu": quantity_mu, "to": to,
            "delivered_at": day, "evidence": list(evidence),
            "report_id": report_id,
        }, source)
        due = settlement_due or _add_days(day, 30)
        self._schedule_task(
            f"T-settle-{plan.plan_id}-{fact.seq}", "settlement", due, plan.plan_id,
            f"合同 {plan.plan_id} 第 {fact.seq} 批交种到期结算", created=day)
        return {"seq": fact.seq, "delivered_at": day, "quantity_mu": quantity_mu}

    # ================================================================
    # 灾害与依赖重算
    # ================================================================

    def report_disaster(self, plan_id: str, losses: list[dict[str, Any]],
                        source: str, report_id: str,
                        day: str | None = None) -> dict[str, Any]:
        if not report_id:
            raise DomainError("灾害报送必须带来源方唯一编号")
        dup = self.state.dedupe.get((source, report_id))
        if dup is not None:
            return {"deduplicated": True, "original_seq": dup["original_seq"]
                    if "original_seq" in dup else dup["seq"]}
        plan = self.state.plans.get(plan_id)
        if plan is None or plan.status != "confirmed":
            raise DomainError("合同不存在或未成立")
        day = _day_of(self._now(day))
        plan_fields = {w["field_id"] for w in plan.snapshot.field_windows}
        normalized = []
        for x in losses:
            if x["field_id"] not in plan_fields and \
                    x["field_id"] not in plan.sown_by_field:
                raise DomainError(
                    f"田块 {x['field_id']} 不属于合同 {plan_id}")
            normalized.append({"field_id": x["field_id"],
                               "lost_area_mu": round(float(x["lost_area_mu"]), 4)})
        fact = self._append(FactType.DISASTER_REPORTED, {
            "plan_id": plan_id, "losses": normalized,
            "report_id": report_id,
        }, source, recorded_at=day + "T00:00:00")
        # 去重键与累计损失由投影在回放时维护。
        rec = self._recompute(plan_id, trigger="disaster", at=day,
                              detail={"losses": normalized, "report_seq": fact.seq})
        # 补种责任由重算按增量统一落实。
        return {"seq": fact.seq, "recomputation": rec}

    def _recompute(self, plan_id: str, trigger: str, at: str,
                   detail: dict[str, Any]) -> dict[str, Any]:
        """沿实际依赖（亲本批次→计划→批次）从事实重新计算。

        每次重算都从当前全部事实直接推导，不依赖上一次重算的差额，
        因此同一触发重复执行结论一致，已完成交接的证据始终保留。
        """
        plan = self.state.plans[plan_id]
        snap = plan.snapshot

        # 1) 冻结/否决批次占用的已播种面积（检验不合格待复检、复核否决）。
        blocked_by_field: dict[str, float] = {}
        rejected_sown = 0.0
        for bid in plan.batches:
            batch = self.state.batches[bid]
            if batch.status == BatchStatus.REJECTED:
                rejected_sown += self._batch_sown_area(bid)
            if batch.status in (BatchStatus.FROZEN, BatchStatus.REJECTED):
                for fid, area in batch.sown_by_field.items():
                    blocked_by_field[fid] = round(
                        blocked_by_field.get(fid, 0.0) + area, 4)
        blocked_total = round(sum(blocked_by_field.values()), 4)

        # 2) 灾害损失按田归集：先冲减在田、且未被冻结的播种面积，
        #    超出部分冲减该田尚未播种的排程面积。
        allocated_by_field: dict[str, float] = {}
        for w in snap.field_windows:
            allocated_by_field[w["field_id"]] = round(
                allocated_by_field.get(w["field_id"], 0.0) + w["area_mu"], 4)
        lost_sown = lost_unsown = 0.0
        for fid, lost in plan.disaster_loss_by_field.items():
            sown_active = max(
                plan.sown_by_field.get(fid, 0.0) - blocked_by_field.get(fid, 0.0), 0.0)
            take_sown = min(lost, sown_active)
            take_unsown = min(max(lost - take_sown, 0.0),
                              max(allocated_by_field.get(fid, 0.0)
                                  - plan.sown_by_field.get(fid, 0.0), 0.0))
            lost_sown += take_sown
            lost_unsown += take_unsown

        # 3) 授权撤回：已交接部分保留，未交付部分不再具备合法交付依据。
        withdrawn = trigger == "authorization_withdrawn"

        physical = round(plan.target_area_mu - blocked_total - lost_sown - lost_unsown, 4)
        # 已完成交接的数量必须保留：任何在田损失都不能追溯到交接之后。
        physical = max(physical, plan.delivered_mu)
        if withdrawn:
            deliverable = plan.delivered_mu
        else:
            deliverable = physical

        replant_due = round(lost_sown + rejected_sown, 4)
        affected = self._affected_plans(plan, replant_due, withdrawn)
        preserved = [dict(d) for d in plan.deliveries]
        record = {
            "plan_id": plan_id, "trigger": trigger, "at": at,
            "detail": detail,
            "deliverable_mu": max(round(deliverable, 4), 0.0),
            "lost_sown_mu": round(lost_sown, 4),
            "lost_unsown_mu": round(lost_unsown, 4),
            "blocked_mu": blocked_total,
            "replant_due_mu": replant_due,
            "affected_plans": affected,
            "preserved_deliveries": preserved,
            "field_windows": None,  # 既有播种与占用证据保持不变
        }
        self._append(FactType.RECOMPUTATION, record, "platform")

        # 补种责任按"较上次已落实"的增量落实，避免重复派责。
        already = round(sum(
            r.get("replant", {}).get("area_mu", 0.0)
            for r in plan.recomputations), 4)
        incremental = round(replant_due - already, 4)
        if incremental > 0:
            self._assign_replant(plan_id, plan.trustee_id, incremental,
                                 _add_days(at, 14), at,
                                 parent_batch_id=snap.parent_batch_id,
                                 cause=trigger)
        # 返回投影中保存的同一份记录（含补种责任与已保留交接）。
        return plan.recomputations[-1]

    def _affected_plans(self, plan: Any, replant_area: float,
                        withdrawn: bool) -> list[str]:
        """沿亲本批次/品种授权依赖找出受影响的其他已成立合同。"""
        affected: set[str] = set()
        if replant_area > 0:
            parent = self.state.parent_batches.get(plan.snapshot.parent_batch_id)
            if parent is not None and replant_area > parent.remaining_mu + 1e-9:
                for other_id, other in self.state.plans.items():
                    if (other_id != plan.plan_id and other.status == "confirmed"
                            and other.snapshot is not None
                            and other.snapshot.parent_batch_id == parent.parent_batch_id):
                        affected.add(other_id)
        if withdrawn:
            for other_id, other in self.state.plans.items():
                if (other_id != plan.plan_id and other.status == "confirmed"
                        and other.company_id == plan.company_id
                        and other.variety_id == plan.variety_id):
                    affected.add(other_id)
        return sorted(affected)

    def _assign_replant(self, plan_id: str, responsible: str, area_mu: float,
                        due_date: str, day: str, parent_batch_id: str | None,
                        cause: str) -> None:
        parent = self.state.parent_batches.get(parent_batch_id) if parent_batch_id else None
        sufficient = None if parent is None else parent.remaining_mu + 1e-9 >= area_mu
        self._append(FactType.REPLANT_ASSIGNED, {
            "plan_id": plan_id, "responsible": responsible,
            "area_mu": round(area_mu, 4), "due_date": due_date,
            "parent_batch_id": parent_batch_id, "cause": cause,
            "parent_sufficient": sufficient,
        }, "platform")

    def _batch_sown_area(self, batch_id: str) -> float:
        return round(sum(self.state.batches[batch_id].sown_by_field.values()), 4)

    # ================================================================
    # 结算
    # ================================================================

    def finalize_settlement(self, plan_id: str, day: str | None = None) -> dict[str, Any]:
        plan = self.state.plans.get(plan_id)
        if plan is None or plan.status != "confirmed":
            raise DomainError("合同不存在或未成立")
        if any(s["plan_id"] == plan_id for s in self.state.settlements):
            raise DomainError("结算线已固化且不可变，不能重复结算")
        if not plan.deliveries:
            raise DomainError("尚无完成交接的交种事实，不能结算")
        snap = plan.snapshot
        index_id, period = snap.settlement["index_id"], snap.settlement["period"]
        frozen_version = int(snap.settlement["version"])
        index = self.state.price_index(index_id, frozen_version)
        if index is None or index.period != period:
            raise DomainError(
                f"固化的指数版本 v{frozen_version} 与结算期间 {period} 不一致")
        lines = []
        for delivery in plan.deliveries:
            if not (index.period_start <= delivery["delivered_at"] <= index.period_end):
                raise DomainError(
                    f"交种日 {delivery['delivered_at']} 不在固化指数 v{index.version} "
                    f"明确适用的结算期间 {period}（{index.period_start}~"
                    f"{index.period_end}）内；指数新版本只进入其明确适用的期间")
            price = index.values[plan.variety_id]
            lines.append({
                "delivery_seq": delivery["seq"],
                "batch_id": delivery.get("batch_id", ""),
                "delivered_at": delivery["delivered_at"],
                "quantity_mu": delivery["quantity_mu"],
                "index_id": index_id, "index_version": index.version,
                "period": period, "unit_price": price,
                "amount": round(delivery["quantity_mu"] * price, 2),
            })
        record = {
            "plan_id": plan_id, "period": period,
            "finalized_at": _day_of(self._now(day)),
            "plan_version_at_confirm": snap.plan_version,
            "index_version_at_confirm": snap.settlement.get("version"),
            "lines": lines,
            "total_amount": round(sum(x["amount"] for x in lines), 2),
        }
        self._append(FactType.SETTLEMENT_FINALIZED, record, "platform")
        for task in list(self.state.tasks.values()):
            if not task.done and task.kind == "settlement" and task.ref_id == plan_id:
                self._complete_task(task.task_id, record["finalized_at"])
        return record

    # ================================================================
    # 到期任务与查询
    # ================================================================

    def due_tasks(self, today: str | None = None) -> list[dict[str, Any]]:
        today = _day_of(self._now(today))
        return [
            {"task_id": t.task_id, "kind": t.kind, "due_date": t.due_date,
             "ref_id": t.ref_id, "detail": t.detail}
            for t in self.state.due_tasks(today)
        ]

    def complete_task(self, task_id: str, day: str | None = None) -> dict[str, Any]:
        if task_id not in self.state.tasks:
            raise DomainError("任务不存在")
        return self._complete_task(task_id, _day_of(self._now(day)))

    def _schedule_task(self, task_id: str, kind: str, due_date: str,
                       ref_id: str, detail: str, created: str) -> None:
        if task_id in self.state.tasks:
            return
        self._append(FactType.TASK_SCHEDULED, {
            "task_id": task_id, "kind": kind, "due_date": due_date,
            "ref_id": ref_id, "detail": detail,
        }, "platform", recorded_at=created + "T00:00:00")

    def _complete_task(self, task_id: str, day: str) -> dict[str, Any]:
        self._append(FactType.TASK_COMPLETED, {"task_id": task_id},
                     "platform", recorded_at=day + "T00:00:00")
        return {"task_id": task_id, "done": True}

    def trace_batch(self, batch_id: str) -> dict[str, Any]:
        if batch_id not in self.state.batches:
            raise DomainError("批次不存在")
        return self.state.batch_trace(batch_id)

    def explain_contract(self, plan_id: str) -> dict[str, Any]:
        plan = self.state.plans.get(plan_id)
        if plan is None:
            raise DomainError("合同不存在")
        return {
            "plan_id": plan_id, "status": plan.status,
            "company_id": plan.company_id, "trustee_id": plan.trustee_id,
            "variety_id": plan.variety_id,
            "sown_total_mu": plan.sown_total_mu,
            "delivered_mu": plan.delivered_mu,
            "deliverable_mu": plan.deliverable_mu,
            "snapshot": self.state._snapshot_dict(plan.snapshot),
            "versions": [self.state._snapshot_dict(v) for v in plan.versions],
            "reschedules": list(plan.reschedules),
            "recomputations": list(plan.recomputations),
            "rejected_reason": plan.rejected_reason,
            "override_proposals": list(plan.override_proposals),
        }

    # ================================================================
    # 辅助
    # ================================================================

    def _index_version_for_period(self, index_id: str, period: str):
        """明确标注该结算期间的、当时已发布的最新指数版本。"""
        versions = self.state.price_indices.get(index_id, [])
        matched = [idx for idx in versions if idx.period == period]
        return max(matched, key=lambda idx: idx.version, default=None)

    @staticmethod
    def _normalize_allocations(allocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        windows = []
        for x in allocations:
            if x["start"] > x["end"]:
                raise DomainError("农时窗口起止日期无效")
            if float(x["area_mu"]) <= 0:
                raise DomainError("排程面积必须为正")
            windows.append({
                "field_id": x["field_id"], "start": x["start"],
                "end": x["end"], "area_mu": round(float(x["area_mu"]), 4),
            })
        return windows

    def _append(self, fact_type: FactType, data: dict[str, Any], source: str,
                recorded_at: str | None = None):
        fact = self.journal.append(fact_type, data, source,
                                   recorded_at or self.clock())
        self.state.apply(fact)
        return fact


class _ReverseText:
    """字符串降序键。"""

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value

    def __lt__(self, other: "_ReverseText") -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ReverseText) and self.value == other.value
