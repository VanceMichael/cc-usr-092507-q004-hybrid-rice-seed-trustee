"""制种委托服务测试。

覆盖：合同成立判定、公开规则唯一裁决、计划版本固化与播种锁定、
生产事实追加/重复/矛盾冻结、灾害与撤回重算、价格指数期间适用、
越级双人复核、批次可解释查询，以及停机恢复。
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_rice_seed_trustee.events import Event
from hybrid_rice_seed_trustee.ledger import Ledger
from hybrid_rice_seed_trustee.service import (
    ContractRejected,
    RuleError,
    SeedCommissionService,
)
from hybrid_rice_seed_trustee.store import EventStore

D = date.fromisoformat
MANAGERS = ["m-zhang", "m-li"]
INSPECTORS = ["insp-a", "insp-b"]


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.service = self._service()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _service(self, **kwargs) -> SeedCommissionService:
        return SeedCommissionService(
            EventStore(self.path),
            managers=kwargs.pop("managers", MANAGERS),
            inspectors=kwargs.pop("inspectors", INSPECTORS),
            **kwargs,
        )

    # ---- 基础资料装配 ---------------------------------------------------

    def seed_basics(
        self,
        *,
        field_id="F1",
        county="建宁县",
        varieties=("V1",),
        enterprises=("E1", "E2"),
        trustees=(("T1", 1, 100.0), ("T2", 1, 100.0), ("T-low", 1, 100.0)),
        isolation=200,
        field_isolation=300,
        parent_area=300.0,
        index_published_on=D("2026-02-01"),
    ) -> None:
        s = self.service
        s.register_field_window(
            field_id, county, 120.0, field_isolation, list(varieties),
            D("2026-04-01"), D("2026-05-10"), D("2026-03-01"), "registry",
        )
        for trustee, version, cap in trustees:
            s.publish_trustee_rating(
                trustee, version, 1 if trustee != "T-low" else 2,
                cap, D("2026-03-01"), "registry",
            )
        for variety in varieties:
            for enterprise in enterprises:
                s.grant_authorization(
                    variety, enterprise, f"G-{variety}-{enterprise}",
                    D("2026-01-01"), None, [county], isolation,
                    D("2026-03-01"), "registry",
                )
            s.admit_parent_batch(
                f"P-{variety}", variety, parent_area, [county],
                D("2026-03-01"), "registry",
            )
        s.publish_price_index(
            1, D("2026-09-01"), D("2026-10-31"), 1.0, 12.0,
            index_published_on, "registry",
        )

    def propose(self, cid, *, enterprise="E1", trustee="T1", field="F1",
                variety="V1", area=40.0, day=10, min_level=1, yield_kg=200.0,
                parent="P-V1"):
        proposed_on = D(f"2026-03-{day:02d}") if isinstance(day, int) else day
        self.service.propose_contract(
            cid, enterprise, trustee, field, variety, parent, area, "2026S",
            D("2026-04-05"), D("2026-04-20"), min_level, yield_kg,
            D("2026-09-01"), D("2026-10-31"), proposed_on, enterprise,
            flowering_expected_on=D("2026-07-05"),
        )

    def settle(self, on=D("2026-03-20")):
        return self.service.settle_proposals(on, "registry")

    # ===================================================================
    # 一、合同成立判定
    # ===================================================================

    def test_happy_path_confirms_contract_with_frozen_plan(self):
        self.seed_basics()
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.propose("C1")
        result = self.settle()
        self.assertEqual(result["confirmed"], ["C1"])
        ledger = self.service._ledger()
        plan = ledger.current_plan("C1")
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["target_area_mu"], 40.0)
        self.assertEqual(plan["required_isolation_m"], 200)
        self.assertEqual(plan["suitable_counties"], ["建宁县"])
        self.assertEqual(plan["price_index_version"], 1)
        self.assertEqual(plan["rating_version"], 1)
        self.assertEqual(plan["grant_ref"], "G-V1-E1")

    def test_proposal_outside_sow_window_is_rejected(self):
        self.seed_basics()
        with self.assertRaises(ContractRejected) as ctx:
            self.service.propose_contract(
                "CX", "E1", "T1", "F1", "V1", "P-V1", 40.0, "2026S",
                D("2026-03-20"), D("2026-04-20"), 1, 200.0,
                D("2026-09-01"), D("2026-10-31"), D("2026-03-10"), "E1",
            )
        self.assertIn("适播窗口", "；".join(ctx.exception.reasons))

    def test_isolation_shortage_is_rejected(self):
        self.seed_basics(isolation=500, field_isolation=300)
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        with self.assertRaises(ContractRejected) as ctx:
            self.propose("CX")
        self.assertTrue(any("隔离" in r for r in ctx.exception.reasons))

    def test_trustee_level_and_capacity_checked(self):
        self.seed_basics()
        with self.assertRaises(ContractRejected) as ctx:
            self.propose("CX", trustee="T-low", min_level=1)
        self.assertTrue(any("等级不足" in r for r in ctx.exception.reasons))
        with self.assertRaises(ContractRejected):
            self.propose("CY", trustee="T1", area=999.0)

    def test_parent_batch_and_authorization_checked(self):
        self.seed_basics()
        with self.assertRaises(ContractRejected) as ctx:
            self.propose("CX", parent="P-UNKNOWN")
        self.assertIn("亲本批次未登记", ctx.exception.reasons)
        with self.assertRaises(ContractRejected) as ctx:
            self.propose("CY", enterprise="E-NO-GRANT")
        self.assertIn("品种制种授权不存在", ctx.exception.reasons)

    def test_rejected_proposal_leaves_evidence(self):
        self.seed_basics()
        with self.assertRaises(ContractRejected):
            self.propose("CX", area=999.0)
        ledger = self.service._ledger()
        self.assertEqual(ledger.proposals["CX"]["outcome"], "rejected")
        self.assertTrue(ledger.proposals["CX"]["rejection"]["reasons"])

    # ===================================================================
    # 二、公开规则唯一裁决
    # ===================================================================

    def test_overlapping_commitment_single_winner_by_application_time(self):
        self.seed_basics()
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.propose("C1", enterprise="E1", trustee="T1", day=10)
        self.propose("C2", enterprise="E2", trustee="T2", day=11)
        result = self.settle()
        self.assertEqual(result["confirmed"], ["C1"])
        self.assertEqual(result["rejected"], ["C2"])
        ledger = self.service._ledger()
        self.assertIn("已被排他承诺",
                      ledger.proposals["C2"]["rejection"]["reasons"][0])
        self.assertEqual(
            ledger.proposals["C2"]["rejection"]["winner_id"], "C1")

    def test_rating_rule_picks_higher_level_deterministically(self):
        self.seed_basics()
        # 规则：先比受托等级，再比申请时间；同分仍唯一
        self.service.publish_priority_rule(
            1, ["trustee_rating", "application_time"], D("2026-03-01"), "registry")
        # 品种门槛为不低于 2 级，两名受托人都过资格；
        # 先发的低等级(2级) vs 后发的高等级(1级)，按规则高等级胜
        self.propose("C-low", enterprise="E1", trustee="T-low", day=9,
                     min_level=2)
        self.propose("C-high", enterprise="E2", trustee="T2", day=12,
                     min_level=2)
        result = self.settle()
        self.assertEqual(result["confirmed"], ["C-high"])
        self.assertEqual(result["rejected"], ["C-low"])

    def test_settling_requires_public_rule(self):
        self.seed_basics()
        self.propose("C1")
        with self.assertRaisesRegex(RuleError, "公开的争用优先规则"):
            self.settle()

    def test_capacity_shortage_falls_back_to_next_candidate(self):
        # T1 容量只够自己的存量；新申请落到 T1 时容量不足，同田块 T2 递补
        self.seed_basics(trustees=(("T1", 1, 40.0), ("T2", 1, 100.0)))
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        # C0 已占用 T1 的 40 亩容量（另一块田）
        self.service.register_field_window(
            "F0", "建宁县", 60.0, 300, ["V1"],
            D("2026-04-01"), D("2026-05-10"), D("2026-03-01"), "registry")
        self.propose("C0", trustee="T1", field="F0", day=8)
        self.propose("C1", enterprise="E1", trustee="T1", day=9)
        self.propose("C2", enterprise="E2", trustee="T2", day=10)
        result = self.settle()
        self.assertIn("C0", result["confirmed"])
        self.assertIn("C2", result["confirmed"])
        self.assertIn("C1", result["rejected"])

    def test_same_field_non_overlapping_windows_can_coexist(self):
        # 同一块田、不同季节标签但实际窗口不重叠：两份合同都应成立
        self.seed_basics(parent_area=600.0)
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.service.propose_contract(
            "C-early", "E1", "T1", "F1", "V1", "P-V1", 30.0, "2026-early",
            D("2026-04-02"), D("2026-04-12"), 1, 200.0,
            D("2026-09-01"), D("2026-10-31"), D("2026-03-05"), "E1")
        self.service.propose_contract(
            "C-late", "E2", "T2", "F1", "V1", "P-V1", 30.0, "2026-late",
            D("2026-04-25"), D("2026-05-05"), 1, 200.0,
            D("2026-09-01"), D("2026-10-31"), D("2026-03-06"), "E2")
        result = self.settle()
        self.assertEqual(sorted(result["confirmed"]), ["C-early", "C-late"])

    def test_same_field_overlapping_windows_conflict_even_with_different_season(self):
        # 季节标签不同但实际播种窗口重叠，仍属同一争用，只能有一个安排
        self.seed_basics()
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.service.propose_contract(
            "C-a", "E1", "T1", "F1", "V1", "P-V1", 30.0, "tag-a",
            D("2026-04-05"), D("2026-04-25"), 1, 200.0,
            D("2026-09-01"), D("2026-10-31"), D("2026-03-05"), "E1")
        self.service.propose_contract(
            "C-b", "E2", "T2", "F1", "V1", "P-V1", 30.0, "tag-b",
            D("2026-04-20"), D("2026-05-05"), 1, 200.0,
            D("2026-09-01"), D("2026-10-31"), D("2026-03-06"), "E2")
        result = self.settle()
        self.assertEqual(result["confirmed"], ["C-a"])
        self.assertEqual(result["rejected"], ["C-b"])

    # ===================================================================
    # 三、计划版本固化与已播种锁定
    # ===================================================================

    def _confirmed(self, cid="C1", **kwargs):
        self.seed_basics(**kwargs)
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.propose(cid)
        self.settle()

    def test_revision_keeps_history_and_locks_sown_area(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=25.0, batch_id="B1")
        # 未播种部分可以调减，但不能低于已播种面积
        with self.assertRaisesRegex(RuleError, "已播种面积"):
            self.service.revise_plan(
                "C1", {"target_area_mu": 20.0}, D("2026-04-12"), "m-zhang")
        version = self.service.revise_plan(
            "C1", {"target_area_mu": 30.0}, D("2026-04-12"), "m-zhang")
        self.assertEqual(version, 2)
        ledger = self.service._ledger()
        self.assertEqual(len(ledger.contracts["C1"]["plans"]), 2)
        self.assertEqual(ledger.current_plan("C1")["target_area_mu"], 30.0)

    def test_sown_contract_rejects_isolation_and_region_changes(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=10.0, batch_id="B1")
        with self.assertRaisesRegex(RuleError, "已播种"):
            self.service.revise_plan(
                "C1", {"required_isolation_m": 50}, D("2026-04-12"), "m-zhang")
        with self.assertRaisesRegex(RuleError, "已播种"):
            self.service.revise_plan(
                "C1", {"suitable_counties": ["泰宁县"]}, D("2026-04-12"), "m-zhang")

    def test_new_price_index_only_enters_explicit_period(self):
        self._confirmed()
        # 新版本覆盖另一个结算期间，不得进入当前计划
        self.service.publish_price_index(
            2, D("2026-11-01"), D("2026-12-31"), 1.2, 14.0,
            D("2026-08-01"), "registry")
        with self.assertRaisesRegex(RuleError, "适用期间不明确覆盖"):
            self.service.revise_plan(
                "C1", {"price_index_version": 2}, D("2026-05-01"), "m-zhang")
        # 同期间新版本可以经计划修订显式采用
        self.service.publish_price_index(
            3, D("2026-09-01"), D("2026-10-31"), 1.1, 13.0,
            D("2026-08-15"), "registry")
        self.service.revise_plan(
            "C1", {"price_index_version": 3}, D("2026-08-20"), "m-zhang")
        self.assertEqual(
            self.service._ledger().current_plan("C1")["price_index_version"], 3)

    # ===================================================================
    # 四、生产事实：追加、重复、矛盾冻结
    # ===================================================================

    def test_duplicate_conclusion_keeps_original(self):
        self._confirmed()
        first = self.service.append_fact(
            "C1", "flowering", "T1", "f-1", D("2026-07-05"), "T1",
            conclusion="normal")
        again = self.service.append_fact(
            "C1", "flowering", "T1", "f-2", D("2026-07-06"), "T1",
            conclusion="normal")
        self.assertEqual(first["effect"], "appended")
        self.assertEqual(again["effect"], "confirmed_original")
        ledger = self.service._ledger()
        facts = ledger.facts_of("C1", "flowering")
        self.assertEqual(len(facts), 1)  # 原结论不产生第二条事实
        self.assertEqual(facts[0].get("duplicates"), 1)

    def test_same_report_id_is_idempotent(self):
        self._confirmed()
        kwargs = dict(contract_id="C1", kind="sowing", source="T1",
                      report_id="sow-1", on=D("2026-04-10"), actor="T1",
                      area_mu=12.0, batch_id="B1")
        r1 = self.service.append_fact(**kwargs)
        r2 = self.service.append_fact(**kwargs)
        self.assertEqual(r1["effect"], "appended")
        self.assertEqual(r2["effect"], "duplicate_report_ignored")
        self.assertEqual(
            self.service._ledger().seed_batches["B1"]["sewn_area_mu"], 12.0)

    def test_contradiction_freezes_batch_for_independent_review(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=20.0, batch_id="B1")
        self.service.append_fact(
            "C1", "roguing", "T1", "rg-1", D("2026-07-05"), "T1",
            conclusion="normal")
        result = self.service.append_fact(
            "C1", "roguing", "insp-b", "rg-2", D("2026-07-08"), "insp-b",
            conclusion="abnormal")
        self.assertEqual(result["effect"], "contradiction_frozen")
        self.assertEqual(result["batch_id"], "B1")
        ledger = self.service._ledger()
        self.assertEqual(ledger.seed_batches["B1"]["status"], "frozen")
        # 冻结后不得继续播种/交种
        with self.assertRaisesRegex(RuleError, "frozen"):
            self.service.append_fact(
                "C1", "sowing", "T1", "sow-2", D("2026-04-15"), "T1",
                area_mu=5.0, batch_id="B1")

    def test_reviewer_must_be_independent_and_qualified(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=20.0, batch_id="B1")
        self.service.append_fact(
            "C1", "inspection", "insp-a", "i-1", D("2026-08-01"), "insp-a",
            conclusion="qualified", batch_id="B1")
        self.service.append_fact(
            "C1", "inspection", "insp-b", "i-2", D("2026-08-02"), "insp-b",
            conclusion="unqualified", batch_id="B1")
        # 非检验人员不能复核
        with self.assertRaisesRegex(RuleError, "独立检验人员"):
            self.service.resolve_frozen_batch(
                "B1", "m-zhang", "released", D("2026-08-03"))
        # 原报送来源必须回避
        with self.assertRaisesRegex(RuleError, "原报送来源"):
            self.service.resolve_frozen_batch(
                "B1", "insp-a", "released", D("2026-08-03"))
        # 两名检验人员都是矛盾记录的来源，新增一名独立人员完成复核
        self.service.inspectors.add("insp-c")
        outcome = self.service.resolve_frozen_batch(
            "B1", "insp-c", "released", D("2026-08-03"))
        self.assertEqual(outcome["effect"], "released")
        self.assertEqual(
            self.service._ledger().seed_batches["B1"]["status"], "active")

    def test_rejected_batch_drives_recomputation(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=30.0, batch_id="B1")
        self.service.append_fact(
            "C1", "roguing", "T1", "rg-1", D("2026-07-05"), "T1",
            conclusion="normal")
        self.service.append_fact(
            "C1", "roguing", "insp-b", "rg-2", D("2026-07-08"), "insp-b",
            conclusion="severe-offtype")
        self.service.inspectors.add("insp-c")
        outcome = self.service.resolve_frozen_batch(
            "B1", "insp-c", "rejected", D("2026-07-10"), area_mu=30.0)
        recomputed = outcome["recomputed"][0]
        self.assertEqual(recomputed["failed_area_mu"], 30.0)
        self.assertEqual(
            self.service._ledger().seed_batches["B1"]["status"], "rejected")

    def test_contradictory_unqualified_after_qualified_also_recomputes(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        self.service.append_fact(
            "C1", "inspection", "lab-1", "i-1", D("2026-08-01"), "lab-1",
            conclusion="qualified", batch_id="B1")
        result = self.service.append_fact(
            "C1", "inspection", "lab-2", "i-2", D("2026-08-02"), "lab-2",
            conclusion="unqualified", batch_id="B1")
        self.assertEqual(result["effect"], "contradiction_frozen")
        self.assertEqual(result["recomputed"][0]["deliverable_kg_remaining"], 0.0)
        recheck = [
            w for w in self.service.due_work(D("2026-12-31"))
            if w["kind"] == "reinspection" and w["ref_id"] == "B1"
        ]
        self.assertEqual(len(recheck), 1)

    def test_grant_renewal_keeps_history_and_scopes_revocation(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        # 旧授权 G-V1-E1 撤回前，先续签新授权 G-V1-E1-R2
        self.service.grant_authorization(
            "V1", "E1", "G-V1-E1-R2", D("2026-09-01"), None,
            ["建宁县"], 200, D("2026-08-20"), "registry")
        # 撤回当前版本（续签后当前版本是 R2，旧合同计划固化的是旧文号）
        result = self.service.revoke_authorization(
            "V1", "E1", D("2026-09-05"), "registry")
        # 旧合同依赖旧文号 G-V1-E1，撤回的是 R2，故旧合同不受影响
        self.assertEqual(result["affected_contracts"], [])
        explanation = self.service.explain_batch("B1")
        self.assertEqual(
            explanation["authorization"]["snapshot_ref_in_plan"], "G-V1-E1")
        self.assertEqual(
            explanation["authorization"]["current_grant_ref"], "G-V1-E1-R2")

    def test_revocation_of_old_grant_blocks_only_contracts_on_that_version(self):
        # 撤回旧版本（无续签）时，依赖旧版本的合同立即不可再交付
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        result = self.service.revoke_authorization(
            "V1", "E1", D("2026-06-01"), "registry")
        self.assertEqual(result["affected_contracts"], ["C1"])
        self.assertFalse(result["recomputed"][0]["authorization_valid"])

    # ===================================================================
    # 五、检验不合格、灾害、授权撤回的重算
    # ===================================================================

    def test_unqualified_inspection_freezes_schedules_recheck_and_recomputes(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        result = self.service.append_fact(
            "C1", "inspection", "lab-1", "i-1", D("2026-08-01"), "lab-1",
            conclusion="unqualified", batch_id="B1")
        self.assertEqual(result["effect"], "inspection_failed")
        self.assertEqual(result["recomputed"][0]["deliverable_kg_remaining"], 0.0)
        recheck = [
            w for w in self.service.due_work(D("2026-12-31"))
            if w["kind"] == "reinspection" and w["ref_id"] == "B1"
        ]
        self.assertEqual(len(recheck), 1)
        self.assertEqual(recheck[0]["due_on"], D("2026-08-08"))

    def test_disaster_recomputes_deliverable_and_assigns_replant(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        result = self.service.record_disaster(
            "D-1", D("2026-05-01"), "registry", area_mu=8.0,
            seed_batch_id="B1")
        row = result["recomputed"][0]
        self.assertEqual(row["damaged_area_mu"], 8.0)
        # 可交付面积 = (40-8) 亩 × 200kg
        self.assertEqual(row["deliverable_kg_remaining"], 6400.0)
        # 仍在播种窗口+宽限期内，8 亩补种责任落到受托人
        self.assertEqual(row["replant_area_mu"], 8.0)
        self.assertEqual(row["replant_responsible"], "T1")
        replant_work = [
            w for w in self.service.due_work(D("2026-12-31"))
            if w["kind"] == "replant"
        ]
        self.assertEqual(len(replant_work), 1)

    def test_replant_not_assigned_after_window(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        # 播种窗口 05-10 + 宽限 10 天 = 05-20，之后受灾不指派补种
        result = self.service.record_disaster(
            "D-late", D("2026-06-01"), "registry", area_mu=8.0,
            seed_batch_id="B1")
        row = result["recomputed"][0]
        self.assertEqual(row["replant_area_mu"], 0.0)
        self.assertIsNone(row["replant_responsible"])

    def test_revocation_recomputes_but_keeps_completed_handovers(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        self.service.append_fact(
            "C1", "delivery", "T1", "d-1", D("2026-09-10"), "T1",
            qty_kg=3000.0, destination="E1-中心库",
            evidence_refs=["handover-001"], batch_id="B1")
        self.service.settle_delivery("C1", "B1", 3000.0, D("2026-09-12"), "E1")
        result = self.service.revoke_authorization(
            "V1", "E1", D("2026-09-15"), "registry", reason="权利终止")
        row = result["recomputed"][0]
        self.assertFalse(row["authorization_valid"])
        self.assertEqual(row["deliverable_kg_remaining"], 0.0)
        self.assertEqual(row["settled_kg"], 3000.0)
        # 已完成交接的证据保留
        explanation = self.service.explain_batch("B1")
        self.assertEqual(explanation["handovers"][0]["evidence_refs"],
                         ["handover-001"])
        self.assertEqual(explanation["settlements"][0]["amount"], 36000.0)
        # 撤回后不得再交种
        with self.assertRaisesRegex(RuleError, "授权已撤回"):
            self.service.append_fact(
                "C1", "delivery", "T1", "d-2", D("2026-09-16"), "T1",
                qty_kg=100.0, destination="E1-中心库", batch_id="B1")

    def test_county_scope_disaster_allocates_proportionally(self):
        # 同县两块田、两份合同，县域灾害按目标面积分摊
        self.seed_basics()
        self.service.register_field_window(
            "F2", "建宁县", 120.0, 300, ["V1"],
            D("2026-04-01"), D("2026-05-10"), D("2026-03-01"), "registry")
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.propose("C1", field="F1", area=30.0, day=8)
        self.propose("C2", enterprise="E2", trustee="T2", field="F2",
                     area=60.0, day=9)
        self.settle()
        result = self.service.record_disaster(
            "D-county", D("2026-05-02"), "registry",
            area_mu=30.0, county="建宁县")
        rows = {r["contract_id"]: r for r in result["recomputed"]}
        self.assertAlmostEqual(rows["C1"]["damaged_area_mu"], 10.0, places=3)
        self.assertAlmostEqual(rows["C2"]["damaged_area_mu"], 20.0, places=3)

    # ===================================================================
    # 六、结算口径
    # ===================================================================

    def test_settlement_blocked_outside_period_and_over_handover(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        self.service.append_fact(
            "C1", "delivery", "T1", "d-1", D("2026-09-10"), "T1",
            qty_kg=1000.0, destination="E1-库", batch_id="B1")
        with self.assertRaisesRegex(RuleError, "结算期间"):
            self.service.settle_delivery("C1", "B1", 100.0, D("2026-08-31"), "E1")
        with self.assertRaisesRegex(RuleError, "超过已完成交接"):
            self.service.settle_delivery("C1", "B1", 1001.0, D("2026-09-12"), "E1")
        result = self.service.settle_delivery(
            "C1", "B1", 1000.0, D("2026-09-12"), "E1")
        self.assertEqual(result["amount"], 12000.0)
        self.assertEqual(result["index_version"], 1)

    def test_new_index_version_does_not_rewrite_prior_period_settlement(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        self.service.append_fact(
            "C1", "delivery", "T1", "d-1", D("2026-09-10"), "T1",
            qty_kg=500.0, destination="E1-库", batch_id="B1")
        self.service.settle_delivery("C1", "B1", 500.0, D("2026-09-12"), "E1")
        # 期间之后发布的新版本对已完成结算无影响
        self.service.publish_price_index(
            9, D("2026-09-01"), D("2026-10-31"), 2.0, 99.0,
            D("2026-09-20"), "registry")
        self.service.append_fact(
            "C1", "delivery", "T1", "d-2", D("2026-09-22"), "T1",
            qty_kg=500.0, destination="E1-库", batch_id="B1")
        result = self.service.settle_delivery(
            "C1", "B1", 500.0, D("2026-09-25"), "E1")
        # 计划固化的 v1 仍覆盖该期间，继续用 v1
        self.assertEqual(result["index_version"], 1)
        self.assertEqual(result["amount"], 6000.0)

    # ===================================================================
    # 七、越级调整双人复核
    # ===================================================================

    def _two_rounds(self):
        self.seed_basics()
        self.service.publish_priority_rule(
            1, ["application_time"], D("2026-03-01"), "registry")
        self.propose("C1", day=10)
        self.settle()
        self.propose("C2", enterprise="E2", trustee="T2", day=15)

    def test_override_requires_manager_and_distinct_reviewer(self):
        self._two_rounds()
        with self.assertRaisesRegex(RuleError, "县域管理人员"):
            self.service.request_override("C2", "T1", "x", D("2026-03-16"))
        self.service.request_override("C2", "m-zhang", "救灾安排", D("2026-03-16"))
        with self.assertRaisesRegex(RuleError, "另一名管理者"):
            self.service.review_override(
                "C2", "m-zhang", True, "同意", D("2026-03-16"))
        # 未批准时裁决仍按公开规则，在位者受保护
        result = self.service.settle_proposals(D("2026-03-17"), "registry")
        self.assertEqual(result["confirmed"], [])
        self.assertEqual(result["rejected"], ["C2"])

    def test_approved_override_replaces_unsown_incumbent_with_audit_trail(self):
        self._two_rounds()
        self.service.request_override("C2", "m-zhang", "救灾安排", D("2026-03-16"))
        self.service.review_override(
            "C2", "m-li", True, "情况属实", D("2026-03-16"))
        result = self.service.settle_proposals(D("2026-03-17"), "registry")
        self.assertEqual(result["confirmed"], ["C2"])
        ledger = self.service._ledger()
        self.assertEqual(ledger.contracts["C1"]["status"], "superseded")
        self.assertEqual(ledger.contracts["C1"]["superseded_by"], "C2")
        trail = ledger.overrides[-1]
        self.assertEqual(trail["requester"], "m-zhang")
        self.assertEqual(trail["reviewer"], "m-li")
        self.assertTrue(trail["approved"])

    def test_override_cannot_rewrite_sown_part(self):
        self._two_rounds()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=10.0, batch_id="B1")
        self.service.request_override("C2", "m-zhang", "救灾安排", D("2026-04-11"))
        self.service.review_override(
            "C2", "m-li", True, "同意", D("2026-04-11"))
        result = self.service.settle_proposals(D("2026-04-12"), "registry")
        self.assertEqual(result["rejected"], ["C2"])
        ledger = self.service._ledger()
        self.assertEqual(ledger.contracts["C1"]["status"], "active")

    # ===================================================================
    # 八、批次解释与停机恢复
    # ===================================================================

    def test_explain_batch_covers_authorization_process_rating_destination(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        self.service.append_fact(
            "C1", "flowering", "T1", "f-1", D("2026-07-05"), "T1",
            conclusion="normal")
        self.service.append_fact(
            "C1", "delivery", "T1", "d-1", D("2026-09-10"), "T1",
            qty_kg=2000.0, destination="E1-中心库",
            evidence_refs=["ev-1"], batch_id="B1")
        # 受托等级发布新版本：解释中同时给当时版本与当前版本
        self.service.publish_trustee_rating(
            "T1", 2, 1, 100.0, D("2026-08-01"), "registry")
        explanation = self.service.explain_batch("B1")
        self.assertEqual(explanation["authorization"]["grant_ref"], "G-V1-E1")
        self.assertEqual(explanation["trustee"]["rating_at_contract"]["version"], 1)
        self.assertEqual(explanation["trustee"]["rating_current"]["version"], 2)
        kinds = [f["kind"] for f in explanation["field_process"]]
        self.assertEqual(kinds, ["sowing", "flowering", "delivery"])
        self.assertEqual(explanation["final_destination"], "E1-中心库")
        self.assertEqual(explanation["handovers"][0]["qty_kg"], 2000.0)

    def test_restart_recovers_open_work_items(self):
        self._confirmed()
        self.service.append_fact(
            "C1", "sowing", "T1", "sow-1", D("2026-04-10"), "T1",
            area_mu=40.0, batch_id="B1")
        self.service.append_fact(
            "C1", "inspection", "lab-1", "i-1", D("2026-08-01"), "lab-1",
            conclusion="unqualified", batch_id="B1")
        # 全新进程、同一事件文件
        restarted = self._service()
        work = restarted.due_work(D("2026-12-31"))
        kinds = {w["kind"] for w in work}
        self.assertIn("reinspection", kinds)
        self.assertIn("settlement", kinds)
        # 历史事实与裁决同样完整恢复
        explanation = restarted.explain_batch("B1")
        self.assertEqual(explanation["status"], "frozen")
        self.assertEqual(restarted._ledger().current_plan("C1")["version"], 1)

    def test_torn_tail_line_is_tolerated_on_recovery(self):
        self._confirmed()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 999, "event_type": "broken", "occurred_on":')
        service = self._service()
        # 残行被忽略，此前事件完整可用
        self.assertIn("C1", service._ledger().contracts)

    def test_event_store_rejects_corrupt_json_line(self):
        store = EventStore(self.path)
        store.append(Event(
            "field_window_registered", {}, D("2026-03-01"), "k1", "a"))
        raw = self.path.read_text(encoding="utf-8")
        self.path.write_text(raw + "{bad json\n", encoding="utf-8")
        events = store.read_all()
        self.assertEqual(len(events), 1)

    # ===================================================================
    # 九、工作项手工管理
    # ===================================================================

    def test_manual_work_item_lifecycle(self):
        self._confirmed()
        before = len(self.service.due_work(D("2026-07-10")))
        work_id = self.service.schedule_work(
            "roguing", D("2026-07-10"), "contract", "C1",
            D("2026-04-10"), "registry", note="首次去杂")
        self.assertEqual(len(self.service.due_work(D("2026-07-10"))), before + 1)
        self.service.complete_work(work_id, "T1", D("2026-07-09"))
        with self.assertRaisesRegex(RuleError, "已完成"):
            self.service.complete_work(work_id, "T1", D("2026-07-09"))
        self.assertNotIn(
            work_id, {w["work_id"] for w in self.service.due_work(D("2026-12-31"))})


if __name__ == "__main__":
    unittest.main()
