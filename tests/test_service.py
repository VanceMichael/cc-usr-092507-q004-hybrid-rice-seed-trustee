"""制种委托服务的领域规则测试。

每个场景对应需求中的一条约束：合同成立判断、唯一排程、计划版本
固化、事实去重与矛盾冻结、依赖重算、结算期间、越级复核、批次追溯
与停机恢复。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_rice_seed_trustee.journal import Journal
from hybrid_rice_seed_trustee.service import DomainError, SeedProductionService


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.svc = SeedProductionService(
            self.dir / "journal.log", clock=lambda: "2026-04-01T08:00:00")
        self._bootstrap()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # —— 标准世界 ——

    def _bootstrap(self) -> None:
        s = self.svc
        s.publish_rulebook(1, [
            {"key": "grade", "order": "desc"},
            {"key": "proposed_seq", "order": "asc"},
        ], effective_from="2026-01-01")
        s.register_authorization(
            "C1", "V1", 1, ["县A", "县B"], min_grade=3,
            effective_from="2026-01-01", isolation_required_m=100)
        s.register_authorization(
            "C2", "V1", 1, ["县A"], min_grade=2,
            effective_from="2026-01-01", isolation_required_m=100)
        s.register_trustee_grade("T1", grade=4, capacity_mu=200,
                                 effective_from="2026-01-01")
        s.register_trustee_grade("T2", grade=2, capacity_mu=500,
                                 effective_from="2026-01-01")
        s.publish_price_index("IDX", 1, "2026-autumn", {"V1": 12.0},
                              "2026-08-01", "2026-12-31")
        s.register_field("F1", "县A", 300, 150,
                         [("2026-04-10", "2026-05-10")])
        s.register_field("F2", "县A", 300, 150,
                         [("2026-04-10", "2026-05-10")])
        s.register_field("F3", "县C", 100, 60,
                         [("2026-04-10", "2026-05-10")])
        s.register_parent_batch("P1", "V1", 500)

    def window(self, field="F1", start="2026-04-10", end="2026-05-05",
               area=80) -> dict:
        return {"field_id": field, "start": start, "end": end, "area_mu": area}

    def propose(self, plan="PL-1", company="C1", trustee="T1", variety="V1",
                allocations=None, parent="P1", period="2026-autumn",
                index="IDX"):
        return self.svc.propose_contract(
            plan, company, trustee, variety,
            allocations or [self.window()], parent, period, index)

    # ================================================================
    # 合同成立判断
    # ================================================================

    def test_happy_path_contract_confirms_with_snapshot(self):
        self.propose()
        decision = self.svc.confirm_contract("PL-1", "2026-04-02")
        self.assertEqual(decision["result"], "confirmed")
        plan = self.svc.explain_contract("PL-1")
        snap = plan["snapshot"]
        self.assertEqual(snap["suitable_regions"], ["县A", "县B"])
        self.assertEqual(snap["isolation_m"], 100)
        self.assertEqual(snap["target_area_mu"], 80)
        # 结算口径固化为当时的指数版本与期间
        self.assertEqual(snap["settlement"],
                         {"index_id": "IDX", "period": "2026-autumn", "version": 1})
        self.assertEqual(snap["rulebook_version"], 1)

    def test_missing_authorization_rejects_contract(self):
        self.propose(plan="PL-X", company="C9")
        decision = self.svc.confirm_contract("PL-X", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertIn("授权", "".join(decision["reasons"]))

    def test_withdrawn_authorization_rejects_pending_contract(self):
        self.prove_only = None
        self.propose(plan="PL-W")
        self.svc.withdraw_authorization("C1", "V1", 1, day="2026-04-01")
        decision = self.svc.confirm_contract("PL-W", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertIn("撤回", "".join(decision["reasons"]))

    def test_trustee_grade_below_minimum_rejected(self):
        self.propose(plan="PL-G", trustee="T2", company="C1")
        decision = self.svc.confirm_contract("PL-G", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertTrue(any("等级" in r for r in decision["reasons"]))

    def test_field_outside_authorized_region_rejected(self):
        self.propose(plan="PL-R",
                     allocations=[self.window(field="F3")])
        decision = self.svc.confirm_contract("PL-R", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertTrue(any("适宜区域" in r for r in decision["reasons"]))

    def test_insufficient_isolation_rejected(self):
        # F3 隔离仅 60m；给它一个在县A的授权以排除区域因素
        self.svc.register_field("F4", "县A", 100, 80,
                                [("2026-04-10", "2026-05-10")])
        self.propose(plan="PL-I",
                     allocations=[self.window(field="F4")])
        decision = self.svc.confirm_contract("PL-I", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertTrue(any("隔离" in r for r in decision["reasons"]))

    def test_window_outside_field_schedule_rejected(self):
        self.propose(plan="PL-T", allocations=[
            self.window(start="2026-03-01", end="2026-03-20")])
        decision = self.svc.confirm_contract("PL-T", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertTrue(any("农时窗口" in r for r in decision["reasons"]))

    def test_parent_batch_wrong_variety_rejected(self):
        self.svc.register_parent_batch("P2", "V9", 500)
        self.propose(plan="PL-P", parent="P2")
        decision = self.svc.confirm_contract("PL-P", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertTrue(any("亲本" in r for r in decision["reasons"]))

    def test_missing_price_index_for_period_rejected(self):
        self.propose(plan="PL-X2", period="2027-spring")
        decision = self.svc.confirm_contract("PL-X2", "2026-04-02")
        self.assertEqual(decision["result"], "rejected")
        self.assertTrue(any("价格指数" in r for r in decision["reasons"]))

    def test_infeasible_contract_writes_rejection_fact(self):
        self.propose(plan="PL-J", company="C9")
        self.svc.confirm_contract("PL-J", "2026-04-02")
        plan = self.svc.explain_contract("PL-J")
        self.assertEqual(plan["status"], "rejected")
        self.assertTrue(plan["rejected_reason"])

    # ================================================================
    # 田块排他排程与唯一安排
    # ================================================================

    def test_two_companies_same_window_single_winner(self):
        self.propose(plan="PL-A", company="C1", trustee="T1")
        self.propose(plan="PL-B", company="C2", trustee="T2")
        decisions = self.svc.arbitrate("2026-04-02")
        results = {d["plan_id"]: d["result"] for d in decisions}
        self.assertEqual(results["PL-A"], "confirmed")
        self.assertEqual(results["PL-B"], "rejected")
        self.assertEqual(
            self.svc.explain_contract("PL-B")["rejected_reason"][:2],
            "争用")

    def test_priority_rules_decide_winner_and_new_version_inverts(self):
        # 同一争用：等级高的 T1 后提案，等级低的 T2 先提案。
        def build(log: str):
            svc = SeedProductionService(
                self.dir / log, clock=lambda: "2026-04-01T08:00:00")
            svc.register_authorization(
                "C1", "V1", 1, ["县A"], min_grade=2,
                effective_from="2026-01-01")
            svc.register_authorization(
                "C2", "V1", 1, ["县A"], min_grade=2,
                effective_from="2026-01-01")
            svc.register_trustee_grade("T1", grade=4, capacity_mu=200,
                                       effective_from="2026-01-01")
            svc.register_trustee_grade("T2", grade=2, capacity_mu=200,
                                       effective_from="2026-01-01")
            svc.publish_price_index("IDX", 1, "2026-autumn", {"V1": 12.0},
                                    "2026-08-01", "2026-12-31")
            svc.register_field("F1", "县A", 100, 150,
                               [("2026-04-10", "2026-05-10")])
            svc.register_parent_batch("P1", "V1", 500)
            return svc

        w = self.window()
        # 规则簿 v1：等级优先 → 后提案但等级高的 T1 方胜出
        s1 = build("j1.log")
        s1.publish_rulebook(1, [{"key": "grade", "order": "desc"},
                                {"key": "plan_id", "order": "asc"}],
                            effective_from="2026-01-01")
        s1.propose_contract("PL-LOW", "C2", "T2", "V1", [w], "P1",
                            "2026-autumn", "IDX")
        s1.propose_contract("PL-HIGH", "C1", "T1", "V1", [w], "P1",
                            "2026-autumn", "IDX")
        r1 = {d["plan_id"]: d["result"]
              for d in s1.arbitrate("2026-04-02")}
        self.assertEqual(r1["PL-HIGH"], "confirmed")
        self.assertEqual(r1["PL-LOW"], "rejected")

        # 规则簿 v2：改为先提案优先 → 先提案的低等级方胜出
        s2 = build("j2.log")
        s2.publish_rulebook(2, [{"key": "proposed_seq", "order": "asc"}],
                            effective_from="2026-04-02")
        s2.propose_contract("PL-LOW", "C2", "T2", "V1", [w], "P1",
                            "2026-autumn", "IDX")
        s2.propose_contract("PL-HIGH", "C1", "T1", "V1", [w], "P1",
                            "2026-autumn", "IDX")
        r2 = {d["plan_id"]: d["result"]
              for d in s2.arbitrate("2026-04-02")}
        self.assertEqual(r2["PL-LOW"], "confirmed")
        self.assertEqual(r2["PL-HIGH"], "rejected")

    def test_non_overlapping_windows_on_same_field_can_coexist(self):
        self.propose(plan="PL-A",
                     allocations=[self.window(start="2026-04-10",
                                              end="2026-04-20", area=40)])
        self.propose(plan="PL-B", company="C2", trustee="T2",
                     allocations=[self.window(start="2026-04-21",
                                              end="2026-05-05", area=40)])
        decisions = self.svc.arbitrate("2026-04-02")
        results = {d["plan_id"]: d["result"] for d in decisions}
        self.assertEqual(results["PL-A"], "confirmed")
        self.assertEqual(results["PL-B"], "confirmed")

    def test_trustee_capacity_blocks_second_plan_when_exceeded(self):
        # T1 能力 200 亩：两个不重叠但合计 220 亩的计划不能同时成立
        self.propose(plan="PL-A", trustee="T1",
                     allocations=[self.window(start="2026-04-10",
                                              end="2026-04-20", area=120)])
        self.propose(plan="PL-B", company="C2", trustee="T1",
                     allocations=[self.window(field="F2",
                                              start="2026-04-21",
                                              end="2026-05-05", area=100)])
        decisions = self.svc.arbitrate("2026-04-02")
        results = {d["plan_id"]: d["result"] for d in decisions}
        self.assertEqual(results["PL-A"], "confirmed")
        self.assertEqual(results["PL-B"], "rejected")

    def test_confirmed_plan_blocks_field_for_later_proposal(self):
        self.propose(plan="PL-A")
        self.svc.confirm_contract("PL-A", "2026-04-02")
        self.propose(plan="PL-B", company="C2", trustee="T2")
        decision = self.svc.confirm_contract("PL-B", "2026-04-03")
        self.assertEqual(decision["result"], "rejected")
        self.assertIn("排他占用", "".join(decision["reasons"]))

    def test_arbitration_without_rulebook_is_domain_error(self):
        fresh = SeedProductionService(
            self.dir / "empty.log", clock=lambda: "2026-04-01T08:00:00")
        fresh.register_field("F1", "县A", 100, 150,
                             [("2026-04-10", "2026-05-10")])
        with self.assertRaises(DomainError):
            fresh.arbitrate("2026-04-02")

    # ================================================================
    # 计划版本固化与播种锁定
    # ================================================================

    def _confirmed(self, plan="PL-1", **kw):
        self.propose(plan=plan, **kw)
        d = self.svc.confirm_contract(plan, "2026-04-02")
        return d["batch_id"]

    def test_reschedule_keeps_snapshot_history(self):
        self._confirmed()
        self.svc.reschedule("PL-1", [
            self.window(field="F1", area=60),
            self.window(field="F2", area=40)], day="2026-04-05")
        plan = self.svc.explain_contract("PL-1")
        self.assertEqual(len(plan["versions"]), 2)
        self.assertEqual(plan["versions"][0]["target_area_mu"], 80)
        self.assertEqual(plan["versions"][1]["target_area_mu"], 100)
        # 结算口径不随后续调度改变
        self.assertEqual(plan["versions"][1]["settlement"]["version"], 1)

    def test_sown_area_cannot_be_rewritten_by_dispatch(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        with self.assertRaisesRegex(DomainError, "已播种"):
            self.svc.reschedule("PL-1", [
                self.window(field="F2", area=80)], day="2026-04-20")

    def test_unsown_remainder_can_be_redistributed(self):
        bid = self._confirmed(allocations=[
            self.window(field="F1", area=40),
            self.window(field="F2", area=40)])
        self.svc.report_sowing(bid, "F1", 40, source="app",
                               report_id="S1", day="2026-04-12")
        # F2 未播种：可调走到 F1 扩面（亲本充足），F1 已播窗口保留
        out = self.svc.reschedule("PL-1", [
            self.window(field="F1", area=70),
            self.window(field="F2", area=10)], day="2026-04-20")
        self.assertEqual(out["target_area_mu"], 80)

    # ================================================================
    # 生产事件：追加、去重、矛盾冻结
    # ================================================================

    def test_duplicate_report_keeps_original_conclusion(self):
        bid = self._confirmed()
        first = self.svc.report_sowing(bid, "F1", 80, source="app",
                                       report_id="S1", day="2026-04-12")
        second = self.svc.report_sowing(bid, "F1", 80, source="app",
                                        report_id="S1", day="2026-04-13")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["original_seq"], first["seq"])
        events = self.svc.trace_batch(bid)["field_events"]
        self.assertEqual(len([e for e in events if e["stage"] == "sowing"]), 1)

    def test_contradictory_reports_freeze_batch_for_review(self):
        bid = self._confirmed()
        self.svc.report_inspection(bid, source="lab-a", report_id="I1",
                                   qualified=True, day="2026-07-10")
        out = self.svc.report_inspection(bid, source="lab-b", report_id="I2",
                                         qualified=False, day="2026-07-11")
        self.assertTrue(out["frozen"])
        trace = self.svc.trace_batch(bid)
        self.assertEqual(trace["status"], "frozen")
        self.assertIn("contradictory", trace["freeze"]["reason"])
        # 冻结期间停止接收新报送
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.svc.report_roguing(bid, source="app", report_id="R2",
                                    day="2026-07-12")

    def test_independent_reviewer_unfreezes_or_rejects(self):
        bid = self._confirmed()
        self.svc.report_inspection(bid, source="lab-a", report_id="I1",
                                   qualified=False, day="2026-07-10")
        self.assertEqual(self.svc.trace_batch(bid)["status"], "frozen")
        out = self.svc.resolve_review("indep-01", bid, True,
                                      "复检合格", day="2026-07-15")
        self.assertEqual(out["decision"], "qualified")
        self.assertEqual(self.svc.trace_batch(bid)["status"], "active")
        self.assertEqual(self.svc.trace_batch(bid)["review"]["reviewer"],
                         "indep-01")

    def test_unqualified_inspection_schedules_retest(self):
        bid = self._confirmed()
        self.svc.report_inspection(bid, source="lab", report_id="I1",
                                   qualified=False, retest_due="2026-07-20",
                                   day="2026-07-10")
        due = self.svc.due_tasks("2026-07-20")
        self.assertTrue(any(t["kind"] == "reinspection" and t["ref_id"] == bid
                            for t in due))

    # ================================================================
    # 灾害与依赖重算
    # ================================================================

    def test_disaster_recomputes_deliverable_and_assigns_replant(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        out = self.svc.report_disaster(
            "PL-1", [{"field_id": "F1", "lost_area_mu": 30}],
            source="bureau", report_id="D1", day="2026-05-01")
        rec = out["recomputation"]
        self.assertEqual(rec["deliverable_mu"], 50)
        self.assertEqual(rec["lost_sown_mu"], 30)
        self.assertEqual(rec["replant_due_mu"], 30)
        self.assertEqual(rec["replant"]["responsible"], "T1")

    def test_duplicate_disaster_report_does_not_double_count(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        args = ("PL-1", [{"field_id": "F1", "lost_area_mu": 30}],
                "bureau", "D1")
        self.svc.report_disaster(*args, day="2026-05-01")
        dup = self.svc.report_disaster(*args, day="2026-05-02")
        self.assertTrue(dup["deduplicated"])
        plan = self.svc.explain_contract("PL-1")
        self.assertEqual(plan["deliverable_mu"], 50)

    def test_delivered_evidence_preserved_after_later_disaster(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.deliver_seed(bid, 60, to="wh", evidence=["交接单"],
                              source="app", report_id="DL1", day="2026-09-01")
        out = self.svc.report_disaster(
            "PL-1", [{"field_id": "F1", "lost_area_mu": 80}],
            source="bureau", report_id="D2", day="2026-09-10")
        rec = out["recomputation"]
        # 已交接的 60 亩证据与可交付量保留
        self.assertEqual(rec["deliverable_mu"], 60)
        self.assertEqual(rec["preserved_deliveries"][0]["quantity_mu"], 60)
        self.assertEqual(rec["preserved_deliveries"][0]["evidence"], ["交接单"])

    def test_authorization_withdrawal_recomputes_affected_contracts(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.deliver_seed(bid, 40, to="wh", evidence=["单"],
                              source="app", report_id="DL1", day="2026-09-01")
        out = self.svc.withdraw_authorization("C1", "V1", 1, day="2026-09-20")
        self.assertEqual(out["affected_plans"][0]["deliverable_mu"], 40)
        plan = self.svc.explain_contract("PL-1")
        self.assertEqual(plan["deliverable_mu"], 40)
        self.assertEqual(plan["delivered_mu"], 40)

    def test_disaster_on_unsown_area_does_not_assign_replant(self):
        # 灾害只落在尚未播种的排程上：可交付扣减，但不产生补种责任
        bid = self._confirmed()
        out = self.svc.report_disaster(
            "PL-1", [{"field_id": "F1", "lost_area_mu": 30}],
            source="bureau", report_id="D0", day="2026-04-05")
        rec = out["recomputation"]
        self.assertEqual(rec["deliverable_mu"], 50)
        self.assertEqual(rec["lost_unsown_mu"], 30)
        self.assertNotIn("replant", rec)

    def test_qualified_inspection_does_not_freeze_or_schedule_retest(self):
        bid = self._confirmed()
        out = self.svc.report_inspection(bid, source="lab", report_id="I1",
                                         qualified=True, day="2026-07-10")
        self.assertNotIn("frozen", out)
        self.assertEqual(self.svc.trace_batch(bid)["status"], "active")
        self.assertFalse(any(t["kind"] == "reinspection"
                             for t in self.svc.due_tasks("2026-12-31")))

    def test_published_versions_cannot_be_overwritten(self):
        with self.assertRaises(DomainError):
            self.svc.register_authorization(
                "C1", "V1", 1, ["县A"], min_grade=3,
                effective_from="2026-01-01")
        with self.assertRaises(DomainError):
            self.svc.publish_price_index(
                "IDX", 1, "2026-autumn", {"V1": 1.0},
                "2026-08-01", "2026-12-31")
        with self.assertRaises(DomainError):
            self.svc.publish_rulebook(1, [], effective_from="2026-01-01")

    def test_replant_shortage_propagates_along_parent_batch_dependency(self):        # 两个合同共用同一亲本批次，余量不足以支撑补种时，
        # 沿亲本依赖把另一个合同列为受影响合同，并标记材料缺口。
        self.svc.register_field("F9", "县A", 300, 150,
                                [("2026-04-10", "2026-05-10")])
        self.propose(plan="PL-1", allocations=[self.window(field="F1", area=200)])
        self.svc.confirm_contract("PL-1", "2026-04-02")
        bid1 = "B-PL-1"
        self.svc.report_sowing(bid1, "F1", 200, source="app",
                               report_id="S1", day="2026-04-12")
        # 另一个合同由 T2 承接（C2 授权、县A、不与 PL-1 争田）
        self.propose(plan="PL-2", company="C2", trustee="T2",
                     allocations=[self.window(field="F9", area=250)])
        d2 = self.svc.confirm_contract("PL-2", "2026-04-03")
        self.assertEqual(d2["result"], "confirmed")
        # 亲本 P1 共 500 亩：200+250=450 已占用，余 50；灾害损失 100 亩在田
        out = self.svc.report_disaster(
            "PL-1", [{"field_id": "F1", "lost_area_mu": 100}],
            source="bureau", report_id="D1", day="2026-05-01")
        rec = out["recomputation"]
        self.assertEqual(rec["replant_due_mu"], 100)
        self.assertFalse(rec["replant"]["parent_sufficient"])
        self.assertIn("PL-2", rec["affected_plans"])

    # ================================================================
    # 结算：版本与期间
    # ================================================================

    def test_price_index_new_version_stays_out_of_old_period(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.deliver_seed(bid, 80, to="wh", evidence=["单"],
                              source="app", report_id="DL1", day="2026-09-01")
        # 发布标注另一个结算期间的新版本
        self.svc.publish_price_index("IDX", 2, "2027-spring", {"V1": 99.0},
                                     "2027-01-01", "2027-06-30")
        settle = self.svc.finalize_settlement("PL-1", day="2026-10-01")
        self.assertEqual(settle["lines"][0]["index_version"], 1)
        self.assertEqual(settle["lines"][0]["unit_price"], 12.0)
        self.assertEqual(settle["total_amount"], 960.0)

    def test_settlement_line_is_immutable(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.deliver_seed(bid, 80, to="wh", evidence=["单"],
                              source="app", report_id="DL1", day="2026-09-01")
        self.svc.finalize_settlement("PL-1", day="2026-10-01")
        with self.assertRaisesRegex(DomainError, "不可变"):
            self.svc.finalize_settlement("PL-1", day="2026-10-02")

    def test_explicit_period_version_is_used_when_applicable(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.deliver_seed(bid, 80, to="wh", evidence=["单"],
                              source="app", report_id="DL1", day="2026-09-01")
        # 同期间的新版本也不能回溯进入已固化快照
        self.svc.publish_price_index("IDX", 3, "2026-autumn", {"V1": 15.0},
                                     "2026-08-01", "2026-12-31")
        settle = self.svc.finalize_settlement("PL-1", day="2026-10-01")
        self.assertEqual(settle["lines"][0]["index_version"], 1)

    def test_delivery_blocked_after_withdrawal_but_prior_evidence_kept(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.deliver_seed(bid, 40, to="wh", evidence=["单1"],
                              source="app", report_id="DL1", day="2026-09-01")
        self.svc.withdraw_authorization("C1", "V1", 1, day="2026-09-20")
        with self.assertRaisesRegex(DomainError, "撤回"):
            self.svc.deliver_seed(bid, 40, to="wh", evidence=["单2"],
                                  source="app", report_id="DL2",
                                  day="2026-09-21")
        # 已交接的 40 亩证据仍然可追溯
        self.assertEqual(self.svc.trace_batch(bid)["delivery"]["quantity_mu"], 40)

    # ================================================================
    # 越级调整：另一名管理者复核并留痕
    # ================================================================

    def test_override_requires_second_manager_and_leaves_trace(self):
        bid = self._confirmed()
        proposal = self.svc.propose_override(
            "manager-1", "PL-1",
            [self.window(field="F1", area=90)], "应急扩面",
            day="2026-04-05")
        # 同一管理者不能复核自己的提案
        with self.assertRaisesRegex(DomainError, "另一名管理者"):
            self.svc.review_override("manager-1", proposal["proposal_id"],
                                     True, day="2026-04-06")
        out = self.svc.review_override("manager-2", proposal["proposal_id"],
                                       True, day="2026-04-06")
        self.assertEqual(out["result"], "approved")
        plan = self.svc.explain_contract("PL-1")
        recorded = plan["override_proposals"][0]
        self.assertEqual(recorded["manager_id"], "manager-1")
        self.assertEqual(recorded["review"]["reviewer"], "manager-2")
        self.assertEqual(plan["versions"][-1]["target_area_mu"], 90)

    def test_rejected_override_does_not_change_plan(self):
        self._confirmed()
        proposal = self.svc.propose_override(
            "manager-1", "PL-1",
            [self.window(field="F1", area=95)], "扩面",
            day="2026-04-05")
        self.svc.review_override("manager-2", proposal["proposal_id"],
                                 False, day="2026-04-06", reason="依据不足")
        plan = self.svc.explain_contract("PL-1")
        self.assertEqual(len(plan["versions"]), 1)
        self.assertEqual(plan["override_proposals"][0]["review"]["result"],
                         "rejected")

    # ================================================================
    # 批次追溯
    # ================================================================

    def test_batch_trace_explains_full_lineage(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.report_flowering(bid, source="inspector", report_id="F1",
                                  day="2026-06-01",
                                  conclusion={"status": "normal"})
        self.svc.deliver_seed(bid, 80, to="wh", evidence=["交接单", "合格证"],
                              source="app", report_id="DL1", day="2026-09-01")
        trace = self.svc.trace_batch(bid)
        stages = [e["stage"] for e in trace["field_events"]]
        self.assertEqual(stages, ["sowing", "flowering"])
        self.assertEqual(trace["authorization"][0]["version"], 1)
        self.assertEqual(trace["trustee_grade_at_confirmation"]["grade"], 4)
        self.assertEqual(trace["delivery"]["to"], "wh")
        self.assertEqual(trace["delivery"]["evidence"], ["交接单", "合格证"])

    # ================================================================
    # 到期任务与停机恢复
    # ================================================================

    def test_due_farming_tasks_survive_restart(self):
        self._confirmed()
        due_before = self.svc.due_tasks("2026-05-10")
        self.assertTrue(any(t["kind"] == "farming_window"
                            and t["ref_id"] == "PL-1" for t in due_before))
        # 停机恢复：用同一日志重新构造服务
        recovered = SeedProductionService(
            self.dir / "journal.log", clock=lambda: "2026-05-11T00:00:00")
        due_after = recovered.due_tasks("2026-05-10")
        self.assertEqual(
            {(t["kind"], t["ref_id"]) for t in due_before},
            {(t["kind"], t["ref_id"]) for t in due_after})

    def test_retest_and_settlement_tasks_survive_restart(self):
        bid = self._confirmed()
        self.svc.report_inspection(bid, source="lab", report_id="I1",
                                   qualified=False, retest_due="2026-07-20",
                                   day="2026-07-10")
        recovered = SeedProductionService(
            self.dir / "journal.log", clock=lambda: "2026-07-21T00:00:00")
        due = recovered.due_tasks("2026-07-21")
        self.assertTrue(any(t["kind"] == "reinspection" for t in due))

    def test_state_identical_after_replay(self):
        bid = self._confirmed()
        self.svc.report_sowing(bid, "F1", 80, source="app",
                               report_id="S1", day="2026-04-12")
        self.svc.report_disaster("PL-1",
                                 [{"field_id": "F1", "lost_area_mu": 10}],
                                 source="bureau", report_id="D1",
                                 day="2026-05-01")
        recovered = SeedProductionService(
            self.dir / "journal.log", clock=lambda: "2026-05-02T00:00:00")
        self.assertEqual(
            recovered.explain_contract("PL-1")["deliverable_mu"],
            self.svc.explain_contract("PL-1")["deliverable_mu"])
        self.assertEqual(
            recovered.trace_batch(bid)["field_events"],
            self.svc.trace_batch(bid)["field_events"])

    def test_journal_is_append_only(self):
        self._confirmed()
        lines = (self.dir / "journal.log").read_text(encoding="utf-8").splitlines()
        seq = [__import__("json").loads(line)["seq"] for line in lines]
        self.assertEqual(seq, list(range(1, len(seq) + 1)))


if __name__ == "__main__":
    unittest.main()
