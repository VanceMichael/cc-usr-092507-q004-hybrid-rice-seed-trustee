"""事件流重放得到的台账状态。

台账本身只追加事件、重放派生，不做任何业务裁决；裁决规则在 service.py。
进程重启后用同一份事件流重放即可完整恢复，包括未到期的工作项。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .events import Event


class Ledger:
    def __init__(self) -> None:
        self.idem_keys: set[str] = set()
        self.fields: dict[str, dict[str, Any]] = {}
        self.trustees: dict[str, dict[str, Any]] = {}
        # 授权按 grant_ref 保留全部历史版本；合同计划只固化当时的 grant_ref。
        self.grants: list[dict[str, Any]] = []
        self.parent_batches: dict[str, dict[str, Any]] = {}
        self.price_indices: list[dict[str, Any]] = []
        self.priority_rules: list[dict[str, Any]] = []
        self.proposals: dict[str, dict[str, Any]] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.facts: list[dict[str, Any]] = []
        self.seed_batches: dict[str, dict[str, Any]] = {}
        self.contradictions: list[dict[str, Any]] = []
        self.disasters: list[dict[str, Any]] = []
        self.recomputations: list[dict[str, Any]] = []
        self.replants: list[dict[str, Any]] = []
        self.settlements: list[dict[str, Any]] = []
        self.overrides: list[dict[str, Any]] = []
        self.work_items: dict[str, dict[str, Any]] = {}

    # ---- 重放入口 -------------------------------------------------------

    @classmethod
    def replay(cls, events: list[Event]) -> "Ledger":
        ledger = cls()
        for event in events:
            ledger.apply(event)
        return ledger

    def apply(self, event: Event) -> None:
        self.idem_keys.add(event.idem_key)
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)

    # ---- 基础资料（均带版本，按时间取当前生效版） -----------------------

    def _on_field_window_registered(self, e: Event) -> None:
        d = e.data
        self.fields[d["field_id"]] = {
            "field_id": d["field_id"],
            "county": d["county"],
            "isolation_m": d["isolation_m"],
            "area_mu": d["area_mu"],
            "suitable_varieties": list(d["suitable_varieties"]),
            "sow_start": date.fromisoformat(d["sow_start"]),
            "sow_end": date.fromisoformat(d["sow_end"]),
            "registered_on": e.occurred_on,
        }

    def _on_trustee_rating_published(self, e: Event) -> None:
        d = e.data
        record = self.trustees.setdefault(
            d["trustee_id"], {"trustee_id": d["trustee_id"], "history": []}
        )
        rating = {
            "version": d["version"],
            "level": d["level"],
            "capacity_mu": d["capacity_mu"],
            "published_on": date.fromisoformat(d["published_on"]),
        }
        record["history"].append(rating)
        record["history"].sort(key=lambda r: r["published_on"])
        record["current"] = record["history"][-1]

    def _on_variety_authorization_granted(self, e: Event) -> None:
        d = e.data
        if any(g["grant_ref"] == d["grant_ref"] for g in self.grants):
            raise ValueError(f"授权文号重复：{d['grant_ref']}")
        self.grants.append({
            "variety_id": d["variety_id"],
            "enterprise_id": d["enterprise_id"],
            "grant_ref": d["grant_ref"],
            "granted_on": date.fromisoformat(d["granted_on"]),
            "valid_from": date.fromisoformat(d["valid_from"]),
            "valid_to": date.fromisoformat(d["valid_to"]) if d.get("valid_to") else None,
            "suitable_counties": list(d["suitable_counties"]),
            "required_isolation_m": d["required_isolation_m"],
            "revoked_on": None,
            "revoke_reason": None,
        })

    def _on_variety_authorization_revoked(self, e: Event) -> None:
        d = e.data
        effective = date.fromisoformat(d["effective_on"])
        matches = [
            g
            for g in self.grants
            if g["variety_id"] == d["variety_id"]
            and g["enterprise_id"] == d["enterprise_id"]
            and g["revoked_on"] is None
            and g["valid_from"] <= effective
            and (g["valid_to"] is None or effective <= g["valid_to"])
        ]
        if not matches:
            raise ValueError("没有可撤回的有效授权版本")
        # 撤回作用于生效日当日有效的最新一版；历史版本保持原样可追溯。
        grant = max(matches, key=lambda g: g["granted_on"])
        grant["revoked_on"] = date.fromisoformat(d["effective_on"])
        grant["revoke_reason"] = d.get("reason", "")

    def grant_by_ref(self, grant_ref: str) -> dict[str, Any] | None:
        return next((g for g in self.grants if g["grant_ref"] == grant_ref), None)

    def current_grant(self, variety_id: str, enterprise_id: str) -> dict[str, Any] | None:
        """最新授予的授权版本（是否已撤回看 revoked_on，是否在有效期看 valid_*）。"""
        matches = [
            g
            for g in self.grants
            if g["variety_id"] == variety_id and g["enterprise_id"] == enterprise_id
        ]
        return max(matches, key=lambda g: g["granted_on"]) if matches else None

    def valid_grant(
        self, variety_id: str, enterprise_id: str, on: date
    ) -> dict[str, Any] | None:
        """on 当日有效（在期间内且未撤回）的最新授权版本。"""
        matches = [
            g
            for g in self.grants
            if g["variety_id"] == variety_id
            and g["enterprise_id"] == enterprise_id
            and g["valid_from"] <= on
            and (g["valid_to"] is None or on <= g["valid_to"])
            and (g["revoked_on"] is None or g["revoked_on"] > on)
        ]
        return max(matches, key=lambda g: g["granted_on"]) if matches else None

    def _on_parent_batch_admitted(self, e: Event) -> None:
        d = e.data
        self.parent_batches[d["batch_id"]] = {
            "batch_id": d["batch_id"],
            "variety_id": d["variety_id"],
            "usable_area_mu": d["usable_area_mu"],
            "counties": list(d["counties"]),
            "admitted_on": date.fromisoformat(d["admitted_on"]),
            "lost_area_mu": 0.0,
        }

    def _on_price_index_published(self, e: Event) -> None:
        d = e.data
        index = {
            "version": d["version"],
            "period_start": date.fromisoformat(d["period_start"]),
            "period_end": date.fromisoformat(d["period_end"]),
            "published_on": date.fromisoformat(d["published_on"]),
            "index_value": d["index_value"],
            "price_per_kg": d["price_per_kg"],
        }
        self.price_indices.append(index)
        self.price_indices.sort(key=lambda i: (i["period_start"], i["version"]))

    def _on_priority_rule_published(self, e: Event) -> None:
        d = e.data
        rule = {
            "version": d["version"],
            "published_on": date.fromisoformat(d["published_on"]),
            "criteria": list(d["criteria"]),
        }
        self.priority_rules.append(rule)
        self.priority_rules.sort(key=lambda r: r["version"])

    # ---- 提案与合同 -----------------------------------------------------

    def _on_contract_proposed(self, e: Event) -> None:
        d = e.data
        self.proposals[d["proposal_id"]] = {
            "proposal_id": d["proposal_id"],
            "enterprise_id": d["enterprise_id"],
            "trustee_id": d["trustee_id"],
            "field_id": d["field_id"],
            "variety_id": d["variety_id"],
            "parent_batch_id": d["parent_batch_id"],
            "area_mu": d["area_mu"],
            "season_key": d["season_key"],
            "sow_start": date.fromisoformat(d["sow_start"]),
            "sow_end": date.fromisoformat(d["sow_end"]),
            "required_min_level": d["required_min_level"],
            "expected_yield_kg_per_mu": d["expected_yield_kg_per_mu"],
            "settlement_period": {
                "start": date.fromisoformat(d["settlement_period_start"]),
                "end": date.fromisoformat(d["settlement_period_end"]),
            },
            "flowering_expected_on": date.fromisoformat(d["flowering_expected_on"])
            if d.get("flowering_expected_on") else None,
            "proposed_on": e.occurred_on,
            "proposed_seq": e.seq,
            "outcome": "pending",
            "forced": False,
            "overrides": [],
        }

    def _on_contract_rejected(self, e: Event) -> None:
        d = e.data
        # 硬资格不成立的提案没有单独的提案事件，这里补一条最小记录。
        proposal = self.proposals.setdefault(
            d["proposal_id"], {"proposal_id": d["proposal_id"], "outcome": "rejected"}
        )
        proposal["outcome"] = "rejected"
        proposal["rejection"] = {
            "reasons": list(d["reasons"]),
            "rule_version": d.get("rule_version"),
            "winner_id": d.get("winner_id"),
            "on": e.occurred_on,
        }

    def _on_contract_confirmed(self, e: Event) -> None:
        d = e.data
        proposal = self.proposals[d["contract_id"]]
        proposal["outcome"] = "confirmed"
        snapshot = d["plan"]
        plan = {
            "version": snapshot["version"],
            "as_of": date.fromisoformat(snapshot["as_of"]),
            "suitable_counties": list(snapshot["suitable_counties"]),
            "required_isolation_m": snapshot["required_isolation_m"],
            "target_area_mu": snapshot["target_area_mu"],
            "settlement_period": {
                "start": date.fromisoformat(snapshot["settlement_period_start"]),
                "end": date.fromisoformat(snapshot["settlement_period_end"]),
            },
            "price_index_version": snapshot.get("price_index_version"),
            "parent_batch_id": snapshot["parent_batch_id"],
            "rating_version": snapshot["rating_version"],
            "rating_level": snapshot["rating_level"],
            "grant_ref": snapshot["grant_ref"],
            "rule_version": snapshot["rule_version"],
            "expected_yield_kg_per_mu": snapshot["expected_yield_kg_per_mu"],
        }
        self.contracts[d["contract_id"]] = {
            "contract_id": d["contract_id"],
            "enterprise_id": proposal["enterprise_id"],
            "trustee_id": proposal["trustee_id"],
            "field_id": proposal["field_id"],
            "variety_id": proposal["variety_id"],
            "parent_batch_id": proposal["parent_batch_id"],
            "season_key": proposal["season_key"],
            "sow_start": proposal["sow_start"],
            "sow_end": proposal["sow_end"],
            "confirmed_on": e.occurred_on,
            "status": "active",
            "plans": [plan],
            "superseded_by": None,
            "disaster_area_mu": 0.0,
        }

    def _on_contract_superseded(self, e: Event) -> None:
        d = e.data
        contract = self.contracts[d["contract_id"]]
        contract["status"] = "superseded"
        contract["superseded_by"] = d["replaced_by"]

    def _on_plan_revised(self, e: Event) -> None:
        d = e.data
        contract = self.contracts[d["contract_id"]]
        previous = contract["plans"][-1]
        plan = dict(previous)
        changes = d["changes"]
        plan["version"] = d["version"]
        plan["as_of"] = e.occurred_on
        if "target_area_mu" in changes:
            plan["target_area_mu"] = changes["target_area_mu"]
        if "suitable_counties" in changes:
            plan["suitable_counties"] = list(changes["suitable_counties"])
        if "required_isolation_m" in changes:
            plan["required_isolation_m"] = changes["required_isolation_m"]
        if "settlement_period_start" in changes:
            plan["settlement_period"] = {
                "start": date.fromisoformat(changes["settlement_period_start"]),
                "end": date.fromisoformat(changes["settlement_period_end"]),
            }
        if "price_index_version" in changes:
            plan["price_index_version"] = changes["price_index_version"]
        contract["plans"].append(plan)

    # ---- 田间事实与批次 -------------------------------------------------

    def _on_production_fact_appended(self, e: Event) -> None:
        d = e.data
        fact = {
            "fact_id": d["fact_id"],
            "contract_id": d["contract_id"],
            "batch_id": d.get("batch_id"),
            "kind": d["kind"],
            "source": d["source"],
            "report_id": d["report_id"],
            "on": date.fromisoformat(d["on"]),
            "conclusion": d.get("conclusion"),
            "area_mu": d.get("area_mu"),
            "qty_kg": d.get("qty_kg"),
            "destination": d.get("destination"),
            "evidence_refs": list(d.get("evidence_refs", [])),
            "seq": e.seq,
        }
        self.facts.append(fact)
        if d["kind"] == "sowing" and d.get("batch_id"):
            self.seed_batches.setdefault(
                d["batch_id"],
                {
                    "batch_id": d["batch_id"],
                    "contract_id": d["contract_id"],
                    "variety_id": self.contracts[d["contract_id"]]["variety_id"],
                    "created_on": date.fromisoformat(d["on"]),
                    "status": "active",
                    "sewn_area_mu": 0.0,
                    "failed_area_mu": 0.0,
                    "damaged_area_mu": 0.0,
                    "frozen_reason": None,
                    "delivered_kg": 0.0,
                    "handovers": [],
                },
            )
            batch = self.seed_batches[d["batch_id"]]
            if batch["status"] == "active" and d.get("area_mu"):
                batch["sewn_area_mu"] += d["area_mu"]
        if d["kind"] == "delivery" and d.get("batch_id"):
            batch = self.seed_batches.setdefault(
                d["batch_id"],
                {
                    "batch_id": d["batch_id"],
                    "contract_id": d["contract_id"],
                    "variety_id": self.contracts[d["contract_id"]]["variety_id"],
                    "created_on": date.fromisoformat(d["on"]),
                    "status": "active",
                    "sewn_area_mu": 0.0,
                    "failed_area_mu": 0.0,
                    "damaged_area_mu": 0.0,
                    "frozen_reason": None,
                    "delivered_kg": 0.0,
                    "handovers": [],
                },
            )
            # 已完成交接的证据独立保留：后续冻结/判废/重算都不删除它。
            batch["handovers"].append(
                {
                    "fact_id": d["fact_id"],
                    "on": date.fromisoformat(d["on"]),
                    "qty_kg": d.get("qty_kg", 0.0),
                    "destination": d.get("destination"),
                    "source": d["source"],
                    "evidence_refs": list(d.get("evidence_refs", [])),
                }
            )
            batch["delivered_kg"] += d.get("qty_kg", 0.0)

    def _on_fact_confirmed(self, e: Event) -> None:
        # 原结论保持不变，仅登记一次重复报送，没有任何状态被改写。
        d = e.data
        original = self.fact_by_id(d["original_fact_id"])
        original["duplicates"] = original.get("duplicates", 0) + 1

    def _on_contradiction_frozen(self, e: Event) -> None:
        d = e.data
        batch = self.seed_batches.get(d["batch_id"])
        record = {
            "batch_id": d["batch_id"],
            "contract_id": d["contract_id"],
            "kind": d["kind"],
            "fact_ids": list(d["fact_ids"]),
            "on": e.occurred_on,
            "resolution": None,
        }
        self.contradictions.append(record)
        if batch is not None:
            batch["status"] = "frozen"
            batch["frozen_reason"] = f"contradiction:{d['kind']}"

    def _on_batch_released(self, e: Event) -> None:
        d = e.data
        batch = self.seed_batches[d["batch_id"]]
        batch["status"] = "active"
        batch["frozen_reason"] = None
        for item in self.contradictions:
            if item["batch_id"] == d["batch_id"] and item["resolution"] is None:
                item["resolution"] = {"decision": "released", "reviewer": d["reviewer"], "on": e.occurred_on}

    def _on_batch_rejected(self, e: Event) -> None:
        d = e.data
        batch = self.seed_batches[d["batch_id"]]
        batch["status"] = "rejected"
        if d.get("area_mu") is not None:
            batch["failed_area_mu"] = d["area_mu"]
        else:
            # 未明示判废面积时，已播种且未受灾的面积全部计损
            batch["failed_area_mu"] = max(
                0.0, batch["sewn_area_mu"] - batch["damaged_area_mu"]
            )
        for item in self.contradictions:
            if item["batch_id"] == d["batch_id"] and item["resolution"] is None:
                item["resolution"] = {"decision": "rejected", "reviewer": d["reviewer"], "on": e.occurred_on}

    def _on_inspection_failed(self, e: Event) -> None:
        d = e.data
        batch = self.seed_batches.get(d["batch_id"])
        if batch is not None and batch["status"] == "active":
            batch["status"] = "frozen"
            batch["frozen_reason"] = "inspection:unqualified"

    # ---- 灾害与重算 -----------------------------------------------------

    def _on_disaster_recorded(self, e: Event) -> None:
        d = e.data
        disaster = {
            "disaster_id": d["disaster_id"],
            "scope": dict(d["scope"]),
            "area_mu": d.get("area_mu", 0.0),
            "on": date.fromisoformat(d["on"]),
            "detail": d.get("detail", ""),
        }
        self.disasters.append(disaster)
        parent_batch_id = d["scope"].get("parent_batch_id")
        if parent_batch_id and parent_batch_id in self.parent_batches:
            self.parent_batches[parent_batch_id]["lost_area_mu"] += d.get("area_mu", 0.0)
        seed_batch_id = d["scope"].get("seed_batch_id")
        if seed_batch_id and seed_batch_id in self.seed_batches:
            batch = self.seed_batches[seed_batch_id]
            if batch["status"] == "active":
                batch["damaged_area_mu"] += d.get("area_mu", 0.0)

    def _on_deliverable_recomputed(self, e: Event) -> None:
        self.recomputations.append(
            {"trigger": dict(e.data["trigger"]), "on": e.occurred_on, "results": e.data["results"]}
        )

    def _on_loss_allocated(self, e: Event) -> None:
        d = e.data
        contract = self.contracts[d["contract_id"]]
        contract["disaster_area_mu"] += d["area_mu"]

    def _on_replant_assigned(self, e: Event) -> None:
        d = e.data
        self.replants.append(
            {
                "replant_id": d["replant_id"],
                "contract_id": d["contract_id"],
                "area_mu": d["area_mu"],
                "responsible": d["responsible"],
                "due_on": date.fromisoformat(d["due_on"]),
                "on": e.occurred_on,
                "status": "assigned",
            }
        )

    # ---- 交接与结算 -----------------------------------------------------

    def _on_delivery_settled(self, e: Event) -> None:
        d = e.data
        self.settlements.append(
            {
                "contract_id": d["contract_id"],
                "batch_id": d["batch_id"],
                "index_version": d["index_version"],
                "qty_kg": d["qty_kg"],
                "amount": d["amount"],
                "period": {
                    "start": date.fromisoformat(d["period_start"]),
                    "end": date.fromisoformat(d["period_end"]),
                },
                "on": e.occurred_on,
            }
        )

    # ---- 越级调整与工作项 ----------------------------------------------

    def _on_override_requested(self, e: Event) -> None:
        d = e.data
        proposal = self.proposals[d["proposal_id"]]
        proposal["overrides"].append(
            {"requester": d["requester"], "reason": d.get("reason", ""), "on": e.occurred_on,
             "review": None}
        )

    def _on_override_reviewed(self, e: Event) -> None:
        d = e.data
        proposal = self.proposals[d["proposal_id"]]
        pending = next(
            (item for item in proposal["overrides"] if item["review"] is None), None
        )
        record = {
            "requester": pending["requester"] if pending else d["requester"],
            "reviewer": d["reviewer"],
            "approved": d["approved"],
            "reason": d.get("reason", ""),
            "on": e.occurred_on,
        }
        self.overrides.append({"proposal_id": d["proposal_id"], **record})
        if pending is not None:
            pending["review"] = record
        if d["approved"]:
            # 经另一名管理者复核批准：该提案可突破公开优先规则。
            proposal["forced"] = True

    def _on_work_item_scheduled(self, e: Event) -> None:
        d = e.data
        self.work_items[d["work_id"]] = {
            "work_id": d["work_id"],
            "kind": d["kind"],
            "due_on": date.fromisoformat(d["due_on"]),
            "ref_type": d["ref_type"],
            "ref_id": d["ref_id"],
            "note": d.get("note", ""),
            "status": "open",
        }

    def _on_work_item_completed(self, e: Event) -> None:
        d = e.data
        item = self.work_items[d["work_id"]]
        item["status"] = "completed"
        item["completed_on"] = e.occurred_on
        item["completed_by"] = d["actor"]

    # ---- 查询辅助 -------------------------------------------------------

    def fact_by_id(self, fact_id: str) -> dict[str, Any]:
        for fact in self.facts:
            if fact["fact_id"] == fact_id:
                return fact
        raise KeyError(fact_id)

    def facts_of(self, contract_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            fact
            for fact in self.facts
            if fact["contract_id"] == contract_id and (kind is None or fact["kind"] == kind)
        ]

    def current_plan(self, contract_id: str) -> dict[str, Any]:
        return self.contracts[contract_id]["plans"][-1]

    def active_contracts(self) -> list[dict[str, Any]]:
        return [c for c in self.contracts.values() if c["status"] == "active"]

    def pending_proposals(self) -> list[dict[str, Any]]:
        return [p for p in self.proposals.values() if p["outcome"] == "pending"]

    def open_work_items(self, due_by: date | None = None) -> list[dict[str, Any]]:
        items = [w for w in self.work_items.values() if w["status"] == "open"]
        if due_by is not None:
            items = [w for w in items if w["due_on"] <= due_by]
        return sorted(items, key=lambda w: (w["due_on"], w["work_id"]))
