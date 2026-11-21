"""业务规则测试：把整套阅读评估的口径固化为可执行校验。"""

import unittest
from datetime import date, timedelta

from rotation.data_model import load_dataset
from rotation.planner import (
    audit_all,
    evaluate_renewal,
    single_move_repairs,
)
from rotation.reconstruct import (
    observed_attribution,
    observed_counts,
    reconstruct_observed,
)
from rotation.simulate import (
    Outcome,
    compare_methods,
    sensitivity_report,
    simulate,
)
from rotation.timeline import build_timelines


class EngineTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset("samples")
        cls.bundle = build_timelines(cls.ds)
        cls.views = reconstruct_observed(cls.ds, cls.bundle)


class TestObservedReconstruction(EngineTestBase):
    def test_evaluation_unit_is_the_whole_request(self):
        # 96 份请求；56 整套、40 部分、0 从未服务；部分不等于整套也不等于零
        counts = observed_counts(self.views)
        self.assertEqual(counts["整套完成"], 56)
        self.assertEqual(counts["部分满足"], 40)
        self.assertEqual(counts["未到手"], 0)
        for view in self.views.values():
            if view.outcome is Outcome.PARTIAL:
                self.assertTrue(1 <= view.satisfied_needs < view.total_needs)
                self.assertTrue(view.missing_needs)

    def test_single_volume_is_not_a_complete_set(self):
        # 所有部分满足请求都缺至少一个需要，且已到手册数严格小于 works 数
        for req in self.ds.requests:
            view = self.views[req.id]
            self.assertLessEqual(view.satisfied_needs, req.size)
            if view.outcome is Outcome.PARTIAL:
                self.assertEqual(view.satisfied_needs, 1)
                self.assertEqual(len(view.missing_needs), 1)

    def test_substitution_only_when_explicitly_accepted(self):
        work_of = {c.id: c.work for c in self.ds.copies}
        for req in self.ds.requests:
            for ful in self.bundle.observed_by_request.get(req.id, ()):
                work = work_of[ful.copy_id]
                self.assertTrue(
                    any(need.accepts(work) for need in req.needs)
                )

    def test_renewal_does_not_create_a_fulfillment(self):
        # supply 里的续期/待核登记数量不改变馆方账的到手记录数
        before = len(self.ds.fulfillments)
        reloaded = build_timelines(self.ds)
        self.assertEqual(
            sum(len(v.loans) for v in reloaded.timelines.values()), before
        )


class TestNoDoubleBooking(EngineTestBase):
    def test_same_copy_never_assigned_twice_in_same_window(self):
        for optimistic in (True, False):
            result = simulate(
                self.ds, self.bundle, option=None, optimistic=optimistic
            )
            # 每个 (副本, 日) 最多出现一次分派
            seen: set[tuple[str, date]] = set()
            for rr in result.results.values():
                for a in rr.assignments:
                    key = (a.copy_id, a.day)
                    self.assertNotIn(key, seen)
                    seen.add(key)

    def test_counterfactual_respects_observed_occupancy(self):
        # 反事实分派不得落在某副本被馆方账实际占用的日期
        result = simulate(
            self.ds, self.bundle, option=None, optimistic=False
        )
        for rr in result.results.values():
            for a in rr.assignments:
                if not a.counterfactual:
                    continue
                timeline = self.bundle.get(a.copy_id)
                for loan in timeline.loans:
                    self.assertFalse(
                        loan.pickup_day <= a.day < loan.latest_release
                    )

    def test_observed_completes_are_locked_not_reassigned(self):
        result = simulate(self.ds, self.bundle, option=None, optimistic=True)
        self.assertEqual(result.observed_complete, 56)
        for req in self.ds.requests:
            view = self.views[req.id]
            if view.outcome is Outcome.COMPLETE:
                rr = result.results[req.id]
                self.assertTrue(rr.observed_complete)
                self.assertFalse(
                    any(a.counterfactual for a in rr.assignments)
                )


class TestUncertainReturns(EngineTestBase):
    def test_uncertain_returns_keep_earliest_latest_window(self):
        uncertain = [
            loan
            for timeline in self.bundle.timelines.values()
            for loan in timeline.loans
            if loan.uncertain
        ]
        self.assertTrue(uncertain)
        for loan in uncertain:
            self.assertIsNone(loan.confirmed_return)
            self.assertLessEqual(loan.earliest_release, loan.latest_release)
            self.assertNotEqual(loan.earliest_release, date.max)

    def test_two_modes_never_claim_more_than_available_copies(self):
        # 无论哪种口径，单份请求每个需要最多被一册满足
        for optimistic in (True, False):
            result = simulate(
                self.ds, self.bundle, option=None, optimistic=optimistic
            )
            for rr in result.results.values():
                need_indices = [a.need_index for a in rr.assignments]
                self.assertEqual(len(need_indices), len(set(need_indices)))


class TestMethodComparison(EngineTestBase):
    def test_comparison_reports_ranges_not_point_estimates(self):
        comparison = compare_methods(self.ds, self.bundle)
        for name, mc in comparison.items():
            lo, hi = mc.complete_range()
            self.assertLessEqual(lo, hi)
            self.assertGreaterEqual(lo, mc.optimistic.observed_complete - 0)
            self.assertEqual(mc.optimistic.observed_complete, 56)

    def test_counterfactual_is_ceiling_not_claimed_net_gain(self):
        comparison = compare_methods(self.ds, self.bundle)
        for mc in comparison.values():
            # 反事实上限不得低于已证实事实，且增量被单独计数
            self.assertGreaterEqual(
                mc.optimistic.counterfactual_completes, 0
            )
            total = (
                mc.optimistic.observed_complete
                + mc.optimistic.counterfactual_completes
            )
            self.assertEqual(
                total, mc.optimistic.counts()[Outcome.COMPLETE.value]
            )

    def test_genuinely_infeasible_requests_stay_incomplete(self):
        result = simulate(self.ds, self.bundle, option=None, optimistic=True)
        infeasible = sorted(
            rid
            for rid, rr in result.results.items()
            if rr.outcome is Outcome.PARTIAL
        )
        # 资料中恰有两份请求的取书范围内、所需作品在两个取书日全被占用
        self.assertEqual(infeasible, ["阅031", "阅095"])

    def test_sensitivity_report_marks_robust_convergence(self):
        comparison = compare_methods(self.ds, self.bundle)
        report = sensitivity_report(self.ds, self.bundle, comparison)
        for info in report.values():
            self.assertEqual(info["cf_inside_uncertain_window"], 0)
            self.assertEqual(info["divergent_requests"], [])
            self.assertTrue(info["robust"])
            lo, hi = info["complete_range"]
            self.assertLessEqual(lo, hi)


class TestAttribution(EngineTestBase):
    def test_service_vs_opportunity_attribution_both_present(self):
        attribution = observed_attribution(self.ds, self.views)
        self.assertIn("服务发生馆", attribution)
        self.assertIn("机会所属馆(住户home)", attribution)
        # 两个视角的总册数一致（都是全部到手册），但馆间分布可以不同
        self.assertEqual(
            sum(attribution["服务发生馆"].values()),
            sum(attribution["机会所属馆(住户home)"].values()),
        )

    def test_neighbor_pickups_identified(self):
        neighbor = sum(
            1
            for view in self.views.values()
            for a in view.assignments
            if a.is_neighbor_pickup
        )
        self.assertEqual(neighbor, 74)


class TestPlanner(EngineTestBase):
    def test_options_touching_commitments_are_checked(self):
        audits = audit_all(self.ds, self.bundle)
        # 两个含挪书的组合都涉及已确认承诺副本
        for name in ("集中取齐", "分周轮转"):
            audit = audits[name]
            self.assertTrue(audit.touches_commitment)
            self.assertFalse(audit.auto_confirmable)
            self.assertTrue(
                any(not c.honoured for c in audit.window_checks)
            )

    def test_auto_confirmation_blocked_when_set_coverage_drops(self):
        audits = audit_all(self.ds, self.bundle)
        central = audits["集中取齐"]
        # 即使反事实整套数不变，覆盖下滑仍阻止自动确认
        self.assertTrue(
            any("覆盖" in reason for reason in central.block_reasons)
        )

    def test_single_move_repair_offered_for_floor_breach(self):
        option = next(o for o in self.ds.options if o.name == "集中取齐")
        repairs = single_move_repairs(self.ds, self.bundle, option)
        self.assertTrue(repairs)
        committed = {c.copy_id for c in self.ds.commitments}
        for move in repairs:
            self.assertFalse(self.ds.copy(move.copy_id).restricted)
            self.assertNotIn(move.copy_id, committed)

    def test_renewal_invalidation_and_closed_households(self):
        impact = evaluate_renewal(
            self.ds,
            self.bundle,
            copy_id="副本09",
            start=date(2026, 7, 6),
            until=date(2026, 7, 13),
        )
        self.assertIn("集中取齐", impact.invalidated)
        self.assertIn("分周轮转", impact.invalidated)
        self.assertNotIn("分散留架", impact.invalidated)
        # 已签收轮转的家庭不再进入新一轮取书建议
        self.assertEqual(impact.closed_households, frozenset({"户003"}))


if __name__ == "__main__":
    unittest.main()
