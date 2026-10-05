from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from commitment_control.clock import FrozenClock
from commitment_control.errors import Conflict, Forbidden, InvalidState, NotFound
from commitment_control.service import CommitmentControlService


def base_commitment(
    resources: list[dict],
    *,
    commitment_id: str = "c-1",
    conditions: list[dict] | None = None,
) -> dict:
    return {
        "commitment_id": commitment_id,
        "program_id": "p-1",
        "title": "联合承诺",
        "conditions": conditions if conditions is not None else [
            {"condition_id": "g1", "label": "培训", "depends_on": [],
             "evidence": [{"evidence_ref": "e-train", "content_sha256": "a" * 64}]},
            {"condition_id": "g2", "label": "合规", "depends_on": ["g1"],
             "evidence": [{"evidence_ref": "e-comply", "content_sha256": "b" * 64}]},
        ],
        "resources": resources,
    }


def unconditional_resource(rvid: str, amount: str, **extra: object) -> dict:
    data = {
        "resource_version_id": rvid,
        "pool_id": "pool-q",
        "provider_id": "agency",
        "beneficiary_id": "partner",
        "amount": amount,
        "release_order": 0,
        "valid_until": "2026-12-31T23:59:59Z",
        "exit_policy": "reallocate_pool",
    }
    data.update(extra)
    return data


class CommitmentControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        self.service = CommitmentControlService(self.connection, self.clock)
        for user_id, role in (
            ("office", "office"),
            ("evaluator", "evaluator"),
            ("dispatcher", "dispatcher"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_program("office", "p-1", "联合项目", "项目办公室")
        self.service.register_milestone("office", "m-1", "p-1", "试点上线")
        self.service.create_pool("office", "pool-q", "platform_quota", "10000")

    def tearDown(self) -> None:
        self.connection.close()

    def cid(self, local: str, revision: int = 1, commitment_id: str = "c-1") -> str:
        return f"{commitment_id}#r{revision}:{local}"

    # ── 版本冻结与幂等 ────────────────────────────────────────────────────────

    def test_freeze_is_idempotent_and_content_conflicts(self) -> None:
        spec = base_commitment([unconditional_resource("r1", "100")])
        first = self.service.freeze_commitment("office", spec, "k-1")
        second = self.service.freeze_commitment("office", spec, "k-1")
        self.assertEqual(first, second)
        changed = base_commitment([unconditional_resource("r1", "101")])
        with self.assertRaises(Conflict):
            self.service.freeze_commitment("office", changed, "k-2")
        with self.assertRaises(Conflict):
            self.service.freeze_commitment("office", spec, "k-other")

    def test_pool_capacity_blocks_overcommit(self) -> None:
        spec = base_commitment([unconditional_resource("r1", "20000")])
        with self.assertRaises(Conflict):
            self.service.freeze_commitment("office", spec, "k-1")
        self.assertEqual(self.service.pool_status("pool-q")["occupied_quota"], "0.000")

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.freeze_commitment("dispatcher", base_commitment([]), "k")
        with self.assertRaises(Forbidden):
            self.service.sweep_due("auditor")

    # ── 独立条件部分生效 ──────────────────────────────────────────────────────

    def _freeze_gated(self) -> dict:
        spec = base_commitment([
            unconditional_resource("r-free", "100", release_order=0),
            unconditional_resource("r-train", "200", gate_condition_id="g1", release_order=1),
            unconditional_resource("r-comply", "300", gate_condition_id="g2", release_order=2),
        ])
        return self.service.freeze_commitment("office", spec, "k-1")

    def test_independent_conditions_release_partially(self) -> None:
        self._freeze_gated()
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        trace_free = self.service.resource_trace("auditor", "r-free")
        trace_train = self.service.resource_trace("auditor", "r-train")
        trace_comply = self.service.resource_trace("auditor", "r-comply")
        self.assertEqual(trace_free["resource"]["state"], "active")
        self.assertEqual(trace_train["resource"]["state"], "active")
        self.assertEqual(trace_comply["resource"]["state"], "waiting")
        # 占用在冻结时已全部登记；生效不改变池占用总额。
        status = self.service.pool_status("pool-q")
        self.assertEqual(status["occupied_quota"], "600.000")
        self.assertEqual(status["available_quota"], "9400.000")
        event_types = [e["event_type"] for e in trace_train["events"]]
        self.assertEqual(event_types, ["occupied", "activated"])

    def test_dependency_blocks_condition(self) -> None:
        self._freeze_gated()
        # g2 依赖 g1：g1 未满足时，即使 g2 的证据核验通过，条件也保持 pending。
        status = self.service.evaluate_evidence(
            "evaluator", self.cid("g2"), "e-comply", "satisfied", "证据先行"
        )
        self.assertEqual(status["condition"]["state"], "pending")
        # g1 满足后传播：g2 的证据此前已全部通过，条件随之满足。
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        self.assertEqual(
            self.service.condition_status(self.cid("g2"))["condition"]["state"], "satisfied"
        )

    def test_failed_evidence_cascades_and_releases_unfulfilled_only(self) -> None:
        self._freeze_gated()
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        self.service.evaluate_evidence("evaluator", self.cid("g2"), "e-comply", "failed", "合规未过")
        # g2 失败 → r-comply 释放 300 形成要约；r-train/r-free 已生效不受影响。
        comply = self.service.resource_trace("auditor", "r-comply")
        self.assertEqual(comply["resource"]["state"], "failed")
        self.assertEqual(comply["released_offers"][0]["amount"], "300.000")
        status = self.service.pool_status("pool-q")
        self.assertEqual(status["occupied_quota"], "300.000")
        self.assertEqual(status["earmarked_quota"], "300.000")

    def test_verified_delivery_survives_later_failure(self) -> None:
        spec = base_commitment([unconditional_resource("r1", "100")])
        self.service.freeze_commitment("office", spec, "k-1")
        first = self.service.record_delivery("dispatcher", "r1", "40", "ev-d1", "c" * 64, "d-1")
        self.assertEqual(first["state"], "active")
        # 交付登记幂等。
        self.assertEqual(
            self.service.record_delivery("dispatcher", "r1", "40", "ev-d1", "c" * 64, "d-1"),
            first,
        )
        # 超额交付被拒绝。
        with self.assertRaises(Conflict):
            self.service.record_delivery("dispatcher", "r1", "70", "ev-d2", "d" * 64, "d-2")
        # 逾期后只释放未兑现的 60，已核验的 40 保留且不可重排。
        self.clock.advance(days=90)
        self.service.sweep_due("dispatcher")
        trace = self.service.resource_trace("auditor", "r1")
        self.assertEqual(trace["resource"]["delivered_amount"], "40.000")
        self.assertEqual(trace["resource"]["recovered_amount"], "60.000")
        self.assertEqual(trace["resource"]["state"], "recovered")
        self.assertEqual(len(trace["deliveries"]), 1)
        self.assertEqual(trace["deliveries"][0]["evidence_ref"], "ev-d1")

    # ── 候补竞争：稳定排序、唯一胜者、去向可追 ─────────────────────────────────

    def _standby_commitment(self) -> None:
        spec = base_commitment([
            unconditional_resource("sb-low", "300", standby_priority=20),
            unconditional_resource("sb-high", "300", standby_priority=10),
            unconditional_resource("sb-big", "1000", standby_priority=5),
        ], commitment_id="c-b", conditions=[
            {"condition_id": "n", "label": "无", "depends_on": [], "evidence": []},
        ])
        self.service.freeze_commitment("office", spec, "k-b")

    def test_standby_competition_single_winner_stable_order(self) -> None:
        self._freeze_gated()
        self._standby_commitment()
        # 候补在没有释放要约前不得占用池的普通空闲额度。
        self.assertEqual(self.service.pool_status("pool-q")["occupied_quota"], "600.000")
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        self.service.evaluate_evidence("evaluator", self.cid("g2"), "e-comply", "failed", "未过")
        high = self.service.resource_trace("auditor", "sb-high")
        low = self.service.resource_trace("auditor", "sb-low")
        big = self.service.resource_trace("auditor", "sb-big")
        self.assertEqual(high["resource"]["state"], "active")  # 优先级 10 且额度 300 匹配
        self.assertEqual(low["resource"]["state"], "waiting")
        self.assertEqual(big["resource"]["state"], "waiting")  # 要约只有 300，装不下
        source = self.service.resource_trace("auditor", "r-comply")
        self.assertEqual(source["released_offers"][0]["winner_resource_version_id"], "sb-high")
        # 重复扫描不产生第二个胜者或重复通知。
        sweep = self.service.sweep_due("dispatcher")
        self.assertEqual(sweep["promotions"], [])
        notifications = self.service.list_notifications("auditor")
        keys = [n["notification_key"] for n in notifications]
        self.assertEqual(len(keys), len(set(keys)))

    def test_unused_offer_returns_to_pool(self) -> None:
        self._freeze_gated()
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        self.service.evaluate_evidence("evaluator", self.cid("g2"), "e-comply", "failed", "未过")
        # 没有任何候补：要约在来源有效期内持续保留。
        result = self.service.sweep_due("dispatcher")
        self.assertEqual(result["promotions"], [])
        status = self.service.pool_status("pool-q")
        self.assertEqual(status["earmarked_quota"], "300.000")
        self.assertEqual(status["open_offers"][0]["state"], "open")
        # 逾期后要约退回，池额度重新可用。
        self.clock.advance(days=90)
        result = self.service.sweep_due("dispatcher")
        returned = {item["offer_id"]: item["amount"] for item in result["returned_offers"]}
        self.assertIn(1, returned)
        self.assertEqual(returned[1], "300.000")
        status = self.service.pool_status("pool-q")
        self.assertEqual(status["earmarked_quota"], "0.000")
        self.assertEqual(status["occupied_quota"], "0.000")
        self.assertTrue(all(item["state"] == "returned" for item in status["open_offers"]))

    # ── 版本替换：已交付不可重排，未生效被接管 ─────────────────────────────────

    def test_revision_supersede_preserves_fulfilled(self) -> None:
        spec = base_commitment([
            unconditional_resource("r1", "100", milestones=[{"milestone_id": "m-1", "required_state": "fulfilled"}]),
            unconditional_resource("r2", "200", gate_condition_id="g1",
                                   milestones=[{"milestone_id": "m-1", "required_state": "active"}]),
        ])
        self.service.freeze_commitment("office", spec, "k-1")
        self.service.record_delivery("dispatcher", "r1", "100", "ev", "c" * 64, "d-1")
        # 新版本：r1 保持，r2 换成 r3。
        rev2 = base_commitment([
            unconditional_resource("r1-v2", "100"),
            unconditional_resource("r3", "200", gate_condition_id="g1"),
        ])
        frozen2 = self.service.freeze_commitment("office", rev2, "k-2")
        self.assertEqual(frozen2["revision"], 2)
        old_r2 = self.service.resource_trace("auditor", "r2")
        self.assertEqual(old_r2["resource"]["state"], "superseded")
        old_r1 = self.service.resource_trace("auditor", "r1")
        self.assertEqual(old_r1["resource"]["state"], "fulfilled")
        self.assertEqual(len(old_r1["deliveries"]), 1)
        self.assertEqual(self.service.get_commitment("c-1", 1)["state"], "superseded")
        # 旧条件冻结保留，仍可读取。
        self.assertEqual(
            self.service.condition_status(self.cid("g1", revision=1))["condition"]["state"],
            "pending",
        )

    # ── 里程碑阻断说明 ────────────────────────────────────────────────────────

    def test_milestone_explanation_names_blocker(self) -> None:
        spec = base_commitment([
            unconditional_resource("r1", "100", milestones=[{"milestone_id": "m-1", "required_state": "fulfilled"}]),
            unconditional_resource("r2", "200", gate_condition_id="g1",
                                   milestones=[{"milestone_id": "m-1", "required_state": "active"}]),
        ])
        self.service.freeze_commitment("office", spec, "k-1")
        explanation = self.service.explain_milestone("m-1")
        self.assertTrue(explanation["blocked"])
        joined = " ".join(b["reason"] for b in explanation["blockers"])
        self.assertIn("等待证据核验", joined)
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        explanation = self.service.explain_milestone("m-1")
        joined = " ".join(b["reason"] for b in explanation["blockers"])
        self.assertIn("尚未全部核验交付", joined)
        self.service.record_delivery("dispatcher", "r1", "100", "ev", "c" * 64, "d-1")
        explanation = self.service.explain_milestone("m-1")
        self.assertFalse(explanation["blocked"])
        self.assertEqual(explanation["milestone"]["state"], "reached")

    # ── 追踪与审计 ────────────────────────────────────────────────────────────

    def test_trace_covers_commitment_evidence_occupancy_release(self) -> None:
        self._freeze_gated()
        self.service.evaluate_evidence("evaluator", self.cid("g1"), "e-train", "satisfied", "达标")
        trace = self.service.resource_trace("auditor", "r-train")
        self.assertEqual(trace["provider_id"], "agency")
        self.assertEqual(trace["beneficiary_id"], "partner")
        self.assertEqual(trace["commitment"]["revision"], 1)
        self.assertEqual(trace["gate_condition"]["condition"]["state"], "satisfied")
        self.assertEqual(
            [e["event_type"] for e in trace["events"]],
            ["occupied", "activated"],
        )

    def test_trace_and_audit_require_permission_chain_detects_tampering(self) -> None:
        # dispatcher 无审计链读取权限。
        with self.assertRaises(Forbidden):
            self.service.audit_chain("dispatcher")
        self.assertTrue(self.service.audit_chain("auditor")["valid"])
        self.connection.execute("UPDATE cc_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.resource_trace("auditor", "nope")
        with self.assertRaises(NotFound):
            self.service.explain_milestone("nope")
        with self.assertRaises(NotFound):
            self.service.pool_status("nope")


if __name__ == "__main__":
    unittest.main()
