"""事实日志的状态投影。

从日志首条事实顺序回放得到当前读模型；停机恢复时重新回放即可，
到期任务（农时、复检、结算）同样存在于日志中，因此不会丢失。

投影只负责"发生了什么 → 当前状态"，"能否发生"的判断在 ``service`` 中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import BatchStatus, DueTask, Fact, FactType, PlanSnapshot, ValidityWindow


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start <= b_end and b_start <= a_end


@dataclass
class AuthorizationVersion:
    company_id: str
    variety_id: str
    version: int
    regions: tuple[str, ...]
    min_grade: int               # 受托人最低等级（数字越大等级越高）
    isolation_required_m: float
    window: ValidityWindow
    withdrawn: bool = False
    withdrawn_at: str | None = None
    source: str = ""

    def key(self) -> tuple[str, str]:
        return (self.company_id, self.variety_id)


@dataclass
class TrusteeGradeVersion:
    trustee_id: str
    grade: int
    capacity_mu: float           # 当季可承接制种面积上限（亩）
    window: ValidityWindow


@dataclass
class PriceIndexVersion:
    index_id: str
    version: int
    period: str                  # 明确适用的结算期间，如 "2026-autumn"
    values: dict[str, float]     # 品种 → 单价
    period_start: str
    period_end: str


@dataclass
class Rulebook:
    version: int
    priorities: tuple[dict[str, str], ...]  # [{"key": ..., "order": "asc|desc"}]
    effective_from: str
    source: str


@dataclass
class FieldRec:
    field_id: str
    region: str
    area_mu: float
    buffer_m: float              # 该田块可保证的空间隔离距离（米）
    windows: tuple[tuple[str, str], ...]  # 可排程农时窗口


@dataclass
class ParentBatchRec:
    parent_batch_id: str
    variety_id: str
    quantity_mu: float           # 可支撑制种面积（亩）
    remaining_mu: float
    source: str


@dataclass
class EventRecord:
    stage: str
    source: str
    report_id: str
    recorded_at: str
    seq: int
    field_id: str | None
    conclusion: dict[str, Any]


@dataclass
class ProductionBatchRec:
    batch_id: str
    plan_id: str
    parent_batch_id: str
    variety_id: str
    field_ids: list[str]
    status: BatchStatus = BatchStatus.ACTIVE
    events: list[EventRecord] = field(default_factory=list)
    sown_by_field: dict[str, float] = field(default_factory=dict)
    freeze_reason: str = ""
    frozen_at: str | None = None
    review: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None


@dataclass
class PlanRec:
    plan_id: str
    company_id: str
    trustee_id: str
    variety_id: str
    status: str = "proposed"     # proposed / confirmed / rejected
    proposal: dict[str, Any] = field(default_factory=dict)
    versions: list[PlanSnapshot] = field(default_factory=list)
    batches: list[str] = field(default_factory=list)
    sown_by_field: dict[str, float] = field(default_factory=dict)
    sown_total_mu: float = 0.0
    delivered_mu: float = 0.0
    reschedules: list[dict[str, Any]] = field(default_factory=list)
    recomputations: list[dict[str, Any]] = field(default_factory=list)
    disaster_loss_by_field: dict[str, float] = field(default_factory=dict)
    override_proposals: list[dict[str, Any]] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    rejected_reason: str = ""
    proposed_at: str = ""
    proposed_seq: int = -1

    @property
    def snapshot(self) -> PlanSnapshot | None:
        return self.versions[-1] if self.versions else None

    @property
    def target_area_mu(self) -> float:
        return self.snapshot.target_area_mu if self.snapshot else 0.0

    @property
    def deliverable_mu(self) -> float:
        """最近一次沿依赖重算确认的可交付面积。"""
        if self.recomputations:
            return round(float(self.recomputations[-1]["deliverable_mu"]), 4)
        return round(self.target_area_mu, 4)


# (field_id, start, end, plan_id, area_mu)
OccupancyEntry = tuple[str, str, str, str, float]


class State:
    """回放全部事实后的投影；service 在其基础上做准入判断。"""

    def __init__(self) -> None:
        self.fields: dict[str, FieldRec] = {}
        self.authorizations: dict[tuple[str, str], list[AuthorizationVersion]] = {}
        self.trustee_grades: dict[str, list[TrusteeGradeVersion]] = {}
        self.price_indices: dict[str, list[PriceIndexVersion]] = {}
        self.rulebooks: dict[int, Rulebook] = {}
        self.parent_batches: dict[str, ParentBatchRec] = {}
        self.plans: dict[str, PlanRec] = {}
        self.batches: dict[str, ProductionBatchRec] = {}
        self.occupancy: dict[str, list[OccupancyEntry]] = {}
        # (来源, 报送编号) → 首次结论（重复报送返回它）
        self.dedupe: dict[tuple[str, str], dict[str, Any]] = {}
        self.tasks: dict[str, DueTask] = {}
        self.settlements: list[dict[str, Any]] = []
        self.seq = 0

    # —— 版本资料查询 ——

    def authorization(self, company_id: str, variety_id: str,
                      day: str) -> AuthorizationVersion | None:
        """返回某日有效的最新授权版本；撤回或不在期间内则无授权。"""
        for auth in reversed(self.authorizations.get((company_id, variety_id), [])):
            if auth.window.covers(day) and not auth.withdrawn:
                return auth
        return None

    def trustee_grade(self, trustee_id: str, day: str) -> TrusteeGradeVersion | None:
        for grade in reversed(self.trustee_grades.get(trustee_id, [])):
            if grade.window.covers(day):
                return grade
        return None

    def trustee_load(self, trustee_id: str, day: str,
                     ignore_plan: str | None = None) -> float:
        """受托人在某日所在农时窗口上已承诺的面积。"""
        used = 0.0
        for plan_id, plan in self.plans.items():
            if plan.status != "confirmed" or plan.trustee_id != trustee_id:
                continue
            if plan_id == ignore_plan:
                continue
            entries = self._all_entries(plan_id)
            if any(start <= day <= end for _, start, end, *_ in entries):
                used += plan.target_area_mu
        return round(used, 4)
    def _all_entries(self, plan_id: str) -> list[OccupancyEntry]:
        return [e for entries in self.occupancy.values() for e in entries
                if e[3] == plan_id]

    def price_index(self, index_id: str, version: int):
        for idx in self.price_indices.get(index_id, []):
            if idx.version == version:
                return idx
        return None

    def index_for_period(self, index_id: str, period: str,
                         day: str) -> PriceIndexVersion | None:
        """结算期间在交种日应适用的指数版本：必须明确标注该期间。

        之后发布的指数新版本若标注的是另一个结算期间，不会进入本次结算。
        """
        for idx in reversed(self.price_indices.get(index_id, [])):
            if idx.period == period and idx.period_start <= day <= idx.period_end:
                return idx
        return None

    def rulebook(self, day: str) -> Rulebook | None:
        active = [r for r in self.rulebooks.values() if r.effective_from <= day]
        return max(active, key=lambda r: r.version, default=None)

    # —— 排程查询 ——

    def conflicts_on(self, allocations: list[dict[str, Any]],
                     ignore_plan: str | None) -> list[OccupancyEntry]:
        """返回与拟排程在同一田块重叠农时的有效占用。"""
        found: list[OccupancyEntry] = []
        for alloc in allocations:
            for entry in self.occupancy.get(alloc["field_id"], []):
                _, start, end, plan_id, _ = entry
                if plan_id == ignore_plan:
                    continue
                if _overlaps(start, end, alloc["start"], alloc["end"]):
                    found.append(entry)
        return found

    def field_window_covered(self, field_id: str, start: str, end: str) -> bool:
        field = self.fields[field_id]
        return any(ws <= start and we >= end for ws, we in field.windows)

    def due_tasks(self, today: str) -> list[DueTask]:
        return sorted(
            (t for t in self.tasks.values() if not t.done and t.due_date <= today),
            key=lambda t: (t.due_date, t.task_id),
        )

    def batch_trace(self, batch_id: str) -> dict[str, Any]:
        """批次完整解释：授权、田间过程、受托等级与最终去向。"""
        batch = self.batches[batch_id]
        plan = self.plans[batch.plan_id]
        snap = plan.snapshot
        auth_versions = self.authorizations.get((plan.company_id, batch.variety_id), [])
        grade_versions = self.trustee_grades.get(plan.trustee_id, [])
        return {
            "batch_id": batch_id,
            "status": batch.status.value,
            "plan_id": batch.plan_id,
            "company_id": plan.company_id,
            "variety_id": batch.variety_id,
            "trustee_id": plan.trustee_id,
            "parent_batch_id": batch.parent_batch_id,
            "authorization": [
                {
                    "version": a.version,
                    "regions": list(a.regions),
                    "min_grade": a.min_grade,
                    "isolation_required_m": a.isolation_required_m,
                    "effective_from": a.window.effective_from,
                    "effective_to": a.window.effective_to,
                    "withdrawn": a.withdrawn,
                    "withdrawn_at": a.withdrawn_at,
                }
                for a in auth_versions
            ],
            "trustee_grade_at_confirmation": (
                self._grade_on(grade_versions, snap.confirmed_at) if snap else None
            ),
            "plan_snapshot": self._snapshot_dict(snap),
            "sown_total_mu": plan.sown_total_mu,
            "delivered_mu": plan.delivered_mu,
            "deliverable_mu": plan.deliverable_mu,
            "field_events": [
                {
                    "stage": e.stage,
                    "source": e.source,
                    "report_id": e.report_id,
                    "recorded_at": e.recorded_at,
                    "seq": e.seq,
                    "field_id": e.field_id,
                    "conclusion": e.conclusion,
                }
                for e in batch.events
            ],
            "freeze": None if batch.status != BatchStatus.FROZEN else {
                "reason": batch.freeze_reason,
                "frozen_at": batch.frozen_at,
            },
            "review": batch.review,
            "delivery": batch.delivery,
            "deliveries": [dict(d) for d in plan.deliveries
                           if d.get("batch_id") == batch_id],
            "recomputations": list(plan.recomputations),
        }

    @staticmethod
    def _grade_on(versions: list[TrusteeGradeVersion], day: str) -> dict[str, Any] | None:
        for grade in reversed(versions):
            if grade.window.covers(day):
                return {"grade": grade.grade, "capacity_mu": grade.capacity_mu,
                        "effective_from": grade.window.effective_from}
        return None

    @staticmethod
    def _snapshot_dict(snap: PlanSnapshot | None) -> dict[str, Any] | None:
        if snap is None:
            return None
        return {
            "plan_version": snap.plan_version,
            "suitable_regions": list(snap.suitable_regions),
            "isolation_m": snap.isolation_m,
            "target_area_mu": snap.target_area_mu,
            "settlement": snap.settlement,
            "confirmed_at": snap.confirmed_at,
            "field_windows": list(snap.field_windows),
            "parent_batch_id": snap.parent_batch_id,
            "rulebook_version": snap.rulebook_version,
        }

    # —— 回放 ——

    def apply(self, fact: Fact) -> None:
        self.seq = fact.seq
        d, kind = fact.data, fact.type
        if kind is FactType.VARIETY_AUTHORIZATION:
            self.authorizations.setdefault((d["company_id"], d["variety_id"]), []).append(
                AuthorizationVersion(
                    company_id=d["company_id"], variety_id=d["variety_id"],
                    version=int(d["version"]), regions=tuple(d["regions"]),
                    min_grade=int(d["min_grade"]),
                    isolation_required_m=float(d.get("isolation_required_m", 100.0)),
                    window=ValidityWindow(d["effective_from"], d.get("effective_to")),
                    source=fact.source,
                ))
        elif kind is FactType.TRUSTEE_GRADE:
            self.trustee_grades.setdefault(d["trustee_id"], []).append(
                TrusteeGradeVersion(
                    trustee_id=d["trustee_id"], grade=int(d["grade"]),
                    capacity_mu=float(d["capacity_mu"]),
                    window=ValidityWindow(d["effective_from"], d.get("effective_to")),
                ))
        elif kind is FactType.PRICE_INDEX:
            self.price_indices.setdefault(d["index_id"], []).append(PriceIndexVersion(
                index_id=d["index_id"], version=int(d["version"]),
                period=d["period"], values=dict(d["values"]),
                period_start=d["period_start"], period_end=d["period_end"],
            ))
        elif kind is FactType.RULEBOOK_PUBLISHED:
            self.rulebooks[int(d["version"])] = Rulebook(
                version=int(d["version"]),
                priorities=tuple(d["priorities"]),
                effective_from=d["effective_from"], source=fact.source,
            )
        elif kind is FactType.AUTH_WITHDRAWN:
            for auth in self.authorizations.get((d["company_id"], d["variety_id"]), []):
                if auth.version == d["version"] and not auth.withdrawn:
                    object.__setattr__(auth, "withdrawn", True)
                    object.__setattr__(auth, "withdrawn_at", d["withdrawn_at"])
        elif kind is FactType.FIELD_REGISTERED:
            self.fields[d["field_id"]] = FieldRec(
                field_id=d["field_id"], region=d["region"],
                area_mu=float(d["area_mu"]), buffer_m=float(d["buffer_m"]),
                windows=tuple((w["start"], w["end"]) for w in d["windows"]),
            )
        elif kind is FactType.PARENT_BATCH_REGISTERED:
            self.parent_batches[d["parent_batch_id"]] = ParentBatchRec(
                parent_batch_id=d["parent_batch_id"], variety_id=d["variety_id"],
                quantity_mu=float(d["quantity_mu"]),
                remaining_mu=float(d["quantity_mu"]), source=fact.source,
            )
        elif kind is FactType.PLAN_PROPOSED:
            self.plans[d["plan_id"]] = PlanRec(
                plan_id=d["plan_id"], company_id=d["company_id"],
                trustee_id=d["trustee_id"], variety_id=d["variety_id"],
                proposal=dict(d),
                proposed_at=fact.recorded_at, proposed_seq=fact.seq)
        elif kind is FactType.PLAN_CONFIRMED:
            self._confirm_plan(d)
        elif kind is FactType.PLAN_REJECTED:
            plan = self.plans[d["plan_id"]]
            plan.status = "rejected"
            plan.rejected_reason = d["reason"]
        elif kind is FactType.BATCH_OPENED:
            self.batches[d["batch_id"]] = ProductionBatchRec(
                batch_id=d["batch_id"], plan_id=d["plan_id"],
                parent_batch_id=d["parent_batch_id"], variety_id=d["variety_id"],
                field_ids=list(d["field_ids"]))
            self.plans[d["plan_id"]].batches.append(d["batch_id"])
        elif kind is FactType.PLAN_RESCHEDULED:
            self._reschedule(d)
        elif kind is FactType.PLAN_OVERRIDE:
            self.plans[d["plan_id"]].override_proposals.append(d)
        elif kind is FactType.OVERRIDE_APPROVED:
            self._approve_override(d)
        elif kind is FactType.OVERRIDE_REJECTED:
            proposal = next(
                p for p in self.plans[d["plan_id"]].override_proposals
                if p["proposal_id"] == d["proposal_id"])
            proposal["review"] = {"result": "rejected", "reviewer": d["reviewer"],
                                  "reviewed_at": d["reviewed_at"],
                                  "reason": d.get("reason", "")}
        elif kind is FactType.TASK_SCHEDULED:
            task = DueTask(
                task_id=d["task_id"], kind=d["kind"], due_date=d["due_date"],
                ref_id=d["ref_id"], detail=d.get("detail", ""),
                created_at=fact.recorded_at)
            self.tasks[task.task_id] = task
        elif kind is FactType.TASK_COMPLETED:
            self.tasks[d["task_id"]] = self.tasks[d["task_id"]].mark(fact.recorded_at)
        elif kind in (FactType.SOWING_REPORTED, FactType.FLOWERING_REPORTED,
                      FactType.ROGUING_REPORTED, FactType.INSPECTION_REPORTED):
            self._append_field_event(fact)
        elif kind is FactType.SEED_DELIVERED:
            self._deliver(fact)
        elif kind is FactType.DISCREPANCY_FROZEN:
            batch = self.batches[d["batch_id"]]
            batch.status = BatchStatus.FROZEN
            batch.freeze_reason = d["reason"]
            batch.frozen_at = fact.recorded_at
        elif kind is FactType.DISASTER_REPORTED:
            # 累积灾害损失（去重键已保证同一报送不重复计数）。
            plan = self.plans[d["plan_id"]]
            for loss in d.get("losses", []):
                fid = loss["field_id"]
                plan.disaster_loss_by_field[fid] = round(
                    plan.disaster_loss_by_field.get(fid, 0.0)
                    + float(loss["lost_area_mu"]), 4)
            self.dedupe[(fact.source, d["report_id"])] = {
                "seq": fact.seq, "stage": "disaster", "batch_id": None,
                "field_id": None,
                "conclusion": {"losses": d.get("losses", [])},
            }
        elif kind is FactType.REVIEW_RESOLVED:
            self._resolve_review(d)
        elif kind is FactType.REPLANT_ASSIGNED:
            latest = self.plans[d["plan_id"]].recomputations[-1]
            latest["replant"] = {
                "responsible": d["responsible"], "area_mu": float(d["area_mu"]),
                "parent_batch_id": d.get("parent_batch_id"),
                "due_date": d["due_date"], "assigned_at": fact.recorded_at,
                "parent_sufficient": d.get("parent_sufficient"),
            }
            # 补种动用亲本材料，余量随之扣减（不足时为负即材料缺口信号）。
            parent = self.parent_batches.get(d.get("parent_batch_id", ""))
            if parent is not None:
                parent.remaining_mu = round(
                    parent.remaining_mu - float(d["area_mu"]), 4)
        elif kind is FactType.RECOMPUTATION:
            rec = dict(d)
            self.plans[d["plan_id"]].recomputations.append(rec)
            if d.get("field_windows") is not None:
                self._set_occupancy(d["plan_id"], d["field_windows"])
        elif kind is FactType.SETTLEMENT_FINALIZED:
            self.settlements.append(d)
        else:  # pragma: no cover - 新类型必须显式处理
            raise ValueError(f"未知事实类型：{kind}")

    # —— 回放内部方法 ——

    def _confirm_plan(self, d: dict[str, Any]) -> None:
        snap = PlanSnapshot(
            plan_id=d["plan_id"], plan_version=int(d["plan_version"]),
            company_id=d["company_id"], trustee_id=d["trustee_id"],
            variety_id=d["variety_id"],
            suitable_regions=tuple(d["suitable_regions"]),
            isolation_m=float(d["isolation_m"]),
            target_area_mu=float(d["target_area_mu"]),
            settlement=dict(d["settlement"]),
            confirmed_at=d["confirmed_at"],
            field_windows=tuple(d["field_windows"]),
            parent_batch_id=d["parent_batch_id"],
            rulebook_version=int(d["rulebook_version"]),
        )
        plan = self.plans[d["plan_id"]]
        plan.status = "confirmed"
        plan.versions.append(snap)
        self._set_occupancy(plan.plan_id, d["field_windows"])
        parent = self.parent_batches.get(snap.parent_batch_id)
        if parent is not None:
            parent.remaining_mu = round(
                parent.remaining_mu - snap.target_area_mu, 4)

    def _new_version(self, plan: PlanRec, **changes: Any) -> PlanSnapshot:
        snap = plan.snapshot
        kwargs = {
            "plan_id": snap.plan_id, "plan_version": snap.plan_version,
            "company_id": snap.company_id, "trustee_id": snap.trustee_id,
            "variety_id": snap.variety_id,
            "suitable_regions": snap.suitable_regions,
            "isolation_m": snap.isolation_m,
            "target_area_mu": snap.target_area_mu,
            "settlement": snap.settlement, "confirmed_at": snap.confirmed_at,
            "field_windows": snap.field_windows,
            "parent_batch_id": snap.parent_batch_id,
            "rulebook_version": snap.rulebook_version,
        }
        kwargs.update(changes)
        return PlanSnapshot(**kwargs)

    def _reschedule(self, d: dict[str, Any]) -> None:
        plan = self.plans[d["plan_id"]]
        old_target = plan.target_area_mu
        plan.reschedules.append(d)
        plan.versions.append(self._new_version(
            plan, plan_version=int(d["plan_version"]),
            field_windows=tuple(d["field_windows"]),
            target_area_mu=float(d["target_area_mu"])))
        self._adjust_parent(plan, float(d["target_area_mu"]) - old_target)
        self._set_occupancy(plan.plan_id, d["field_windows"])
        self._sync_batch_fields(plan, d["field_windows"])

    def _approve_override(self, d: dict[str, Any]) -> None:
        plan = self.plans[d["plan_id"]]
        proposal = next(p for p in plan.override_proposals
                        if p["proposal_id"] == d["proposal_id"])
        proposal["review"] = {"result": "approved", "reviewer": d["reviewer"],
                              "reviewed_at": d["reviewed_at"]}
        old_target = plan.target_area_mu
        # 版本号以实际生效时为准：复核期间若有普通调度，按当前顺序递增。
        actual_version = len(plan.versions) + 1
        plan.reschedules.append({"kind": "override", **proposal})
        plan.versions.append(self._new_version(
            plan, plan_version=actual_version,
            field_windows=tuple(proposal["field_windows"]),
            target_area_mu=float(proposal["target_area_mu"])))
        proposal["effective_plan_version"] = actual_version
        self._adjust_parent(plan, float(proposal["target_area_mu"]) - old_target)
        self._set_occupancy(plan.plan_id, proposal["field_windows"])
        self._sync_batch_fields(plan, proposal["field_windows"])

    def _sync_batch_fields(self, plan: PlanRec,
                           windows: list[dict[str, Any]]) -> None:
        """扩面到新田块时，把田块加入既有批次的可报范围；
        已播种的田块即使被调出未播余量也不从清单中移除。"""
        current = {w["field_id"] for w in windows}
        sown = set(plan.sown_by_field)
        wanted = current | sown
        for bid in plan.batches:
            batch = self.batches[bid]
            for fid in sorted(wanted):
                if fid not in batch.field_ids:
                    batch.field_ids.append(fid)

    def _adjust_parent(self, plan: PlanRec, delta_mu: float) -> None:
        """调度改面时同步亲本批次余量（已播种部分不可被调减）。"""
        if abs(delta_mu) < 1e-9:
            return
        parent = self.parent_batches.get(plan.snapshot.parent_batch_id)
        if parent is not None:
            parent.remaining_mu = round(parent.remaining_mu - delta_mu, 4)

    def _set_occupancy(self, plan_id: str,
                       windows: list[dict[str, Any]]) -> None:
        """整体重写某计划的占用；服务层负责保证已播种窗口原样保留。"""
        for field_id in list(self.occupancy):
            self.occupancy[field_id] = [
                e for e in self.occupancy[field_id] if e[3] != plan_id]
        for w in windows:
            self.occupancy.setdefault(w["field_id"], []).append(
                (w["field_id"], w["start"], w["end"], plan_id,
                 float(w["area_mu"])))

    def _append_field_event(self, fact: Fact) -> None:
        d, kind = fact.data, fact.type
        stage = kind.value.replace("_reported", "")
        batch = self.batches[d["batch_id"]]
        record = EventRecord(
            stage=stage, source=fact.source, report_id=d.get("report_id", ""),
            recorded_at=fact.recorded_at, seq=fact.seq,
            field_id=d.get("field_id"), conclusion=dict(d.get("conclusion", {})))
        batch.events.append(record)
        # 去重索引随回放恢复：停机后同源同号重复报送仍返回原结论。
        self.dedupe[(fact.source, d.get("report_id", ""))] = {
            "seq": fact.seq, "stage": stage, "batch_id": batch.batch_id,
            "field_id": record.field_id, "conclusion": record.conclusion,
        }
        if stage == "sowing":
            area = float(d["conclusion"]["area_mu"])
            fid = d["field_id"]
            batch.sown_by_field[fid] = round(
                batch.sown_by_field.get(fid, 0.0) + area, 4)
            plan = self.plans[batch.plan_id]
            plan.sown_by_field[fid] = round(
                plan.sown_by_field.get(fid, 0.0) + area, 4)
            plan.sown_total_mu = round(plan.sown_total_mu + area, 4)

    def _resolve_review(self, d: dict[str, Any]) -> None:
        batch = self.batches[d["batch_id"]]
        batch.review = d
        if d["decision"] == "qualified":
            batch.status = BatchStatus.ACTIVE
            batch.freeze_reason = ""
            batch.frozen_at = None
        else:
            batch.status = BatchStatus.REJECTED

    def _deliver(self, fact: Fact) -> None:
        d = fact.data
        batch = self.batches[d["batch_id"]]
        batch.delivery = {
            "batch_id": batch.batch_id,
            "quantity_mu": float(d["quantity_mu"]),
            "to": d["to"], "delivered_at": d["delivered_at"],
            "source": fact.source, "seq": fact.seq,
            "evidence": list(d.get("evidence", [])),
        }
        plan = self.plans[batch.plan_id]
        plan.delivered_mu = round(
            plan.delivered_mu + float(d["quantity_mu"]), 4)
        plan.deliveries.append(batch.delivery)
        # 全部面积完成交接后批次才整体置为已交付；部分交接不影响继续生产。
        if plan.delivered_mu >= plan.sown_total_mu - 1e-9:
            batch.status = BatchStatus.DELIVERED
        self.dedupe[(fact.source, d.get("report_id", ""))] = {
            "seq": fact.seq, "stage": "delivery", "batch_id": batch.batch_id,
            "field_id": None, "conclusion": {"quantity_mu": d["quantity_mu"]},
        }
