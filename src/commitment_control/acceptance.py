"""条件化承诺管理的离线验收：数贸非洲日联合项目执行阶段全流程。

场景：资金、专家时段、平台额度由不同机构承诺；培训达标证据已过、属地数据合规待确认；
合规条件失败后只释放未兑现部分并按稳定候补转配；已核验交付不被重排；
通知幂等、竞争释放唯一胜者、里程碑阻断可解释、资源全过程可追溯。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CommitmentControlService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
    service = CommitmentControlService(connection, clock)
    for user_id, role in (
        ("office", "office"),
        ("evaluator", "evaluator"),
        ("dispatcher", "dispatcher"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.register_program("office", "africa-day", "数贸非洲日联合项目", "项目办公室")
    service.register_milestone("office", "m-pilot", "africa-day", "属地试点上线")

    # 三个资源池：资金（人民币）、专家时段（小时）、平台额度（单位）。
    service.create_pool("office", "fund-pool", "fund", "1000000")
    service.create_pool("office", "expert-pool", "expert_time", "2000")
    service.create_pool("office", "quota-pool", "platform_quota", "50000")

    commitment = {
        "commitment_id": "caf-joint",
        "program_id": "africa-day",
        "title": "数贸非洲日联合资源承诺",
        "conditions": [
            {
                "condition_id": "training",
                "label": "培训达标",
                "depends_on": [],
                "evidence": [
                    {"evidence_ref": "evidence_revision-training-1.0", "content_sha256": "a" * 64},
                ],
            },
            {
                "condition_id": "compliance",
                "label": "属地数据合规确认",
                "depends_on": ["training"],
                "evidence": [
                    {"evidence_ref": "evidence_revision-compliance-1.0", "content_sha256": "b" * 64},
                ],
            },
        ],
        "resources": [
            {
                # 无争议资金：无条件，冻结即生效，要求交付后里程碑才放行。
                "resource_version_id": "r-fund-base",
                "pool_id": "fund-pool",
                "provider_id": "agency-A",
                "beneficiary_id": "office-africa",
                "amount": "200000",
                "release_order": 0,
                "valid_until": "2026-12-31T23:59:59Z",
                "exit_policy": "return_provider",
                "milestones": [{"milestone_id": "m-pilot", "required_state": "fulfilled"}],
            },
            {
                # 专家时段：培训达标即释放。
                "resource_version_id": "r-expert-training",
                "pool_id": "expert-pool",
                "provider_id": "agency-B",
                "beneficiary_id": "local-partner",
                "amount": "300",
                "gate_condition_id": "training",
                "release_order": 1,
                "valid_until": "2026-11-30T23:59:59Z",
                "exit_policy": "reallocate_pool",
                "milestones": [{"milestone_id": "m-pilot", "required_state": "active"}],
            },
            {
                # 平台额度：培训与合规均通过才释放（依赖链）。
                "resource_version_id": "r-quota-compliance",
                "pool_id": "quota-pool",
                "provider_id": "agency-C",
                "beneficiary_id": "local-partner",
                "amount": "8000",
                "gate_condition_id": "compliance",
                "release_order": 2,
                "valid_until": "2026-11-30T23:59:59Z",
                "exit_policy": "reallocate_pool",
                "milestones": [{"milestone_id": "m-pilot", "required_state": "active"}],
            },
        ],
        "idempotency_key": "freeze-1",
    }
    frozen = service.freeze_commitment("office", commitment, "freeze-1")
    assert frozen["activated"] == ["r-fund-base"], frozen

    # 另立承诺：稳定候补 1/2 等待平台额度。
    standby = {
        "commitment_id": "caf-backup",
        "program_id": "africa-day",
        "title": "候补用途承诺",
        "conditions": [
            {"condition_id": "none", "label": "无条件", "depends_on": [], "evidence": []},
        ],
        "resources": [
            {
                "resource_version_id": "r-standby-2",
                "pool_id": "quota-pool",
                "provider_id": "agency-D",
                "beneficiary_id": "backup-2",
                "amount": "8000",
                "release_order": 0,
                "valid_until": "2026-12-15T23:59:59Z",
                "exit_policy": "return_provider",
                "standby_priority": 20,
            },
            {
                "resource_version_id": "r-standby-1",
                "pool_id": "quota-pool",
                "provider_id": "agency-D",
                "beneficiary_id": "backup-1",
                "amount": "8000",
                "release_order": 0,
                "valid_until": "2026-12-15T23:59:59Z",
                "exit_policy": "return_provider",
                "standby_priority": 10,
            },
        ],
    }
    service.freeze_commitment("office", standby, "freeze-2")

    # 里程碑此时被三条资源阻断。
    blocked_before = service.explain_milestone("m-pilot")
    assert blocked_before["blocked"] and len(blocked_before["blockers"]) == 3, blocked_before

    # 无争议资金部分交付并核验通过。
    delivery = service.record_delivery(
        "dispatcher", "r-fund-base", "200000", "evidence_revision-delivery-1", "c" * 64, "delivery-1"
    )
    assert delivery["state"] == "fulfilled", delivery

    # 培训证据通过：专家时段独立部分生效；合规仍 pending，平台额度继续保留。
    service.evaluate_evidence(
        "evaluator", "caf-joint#r1:training", "evidence_revision-training-1.0", "satisfied", "培训考核达标"
    )
    expert = service.resource_trace("auditor", "r-expert-training")
    assert expert["resource"]["state"] == "active", expert["resource"]["state"]
    quota = service.resource_trace("auditor", "r-quota-compliance")
    assert quota["resource"]["state"] == "waiting", quota["resource"]["state"]

    # 里程碑仍因平台额度被阻断，且能说明原因。
    blocked_mid = service.explain_milestone("m-pilot")
    reasons = [b["reason"] for b in blocked_mid["blockers"]]
    assert any("等待证据核验" in text for text in reasons), reasons

    # 合规失败：平台额度的未兑现部分释放，稳定候补 #1（优先级 10）唯一获胜。
    service.evaluate_evidence(
        "evaluator", "caf-joint#r1:compliance", "evidence_revision-compliance-1.0", "failed", "属地数据驻留未通过"
    )
    failed_quota = service.resource_trace("auditor", "r-quota-compliance")
    assert failed_quota["resource"]["state"] == "failed", failed_quota["resource"]["state"]
    # 释放出的未兑现部分形成要约，并能追到最终被谁消费。
    offers = failed_quota["released_offers"]
    assert len(offers) == 1 and offers[0]["winner_resource_version_id"] == "r-standby-1", offers
    winner = service.resource_trace("auditor", "r-standby-1")
    assert winner["resource"]["state"] == "active", winner["resource"]["state"]
    loser = service.resource_trace("auditor", "r-standby-2")
    assert loser["resource"]["state"] == "waiting", loser["resource"]["state"]
    assert len(winner["promotions_won"]) == 1
    assert winner["promotions_won"][0]["offer_links"][0]["source_resource_version_id"] == "r-quota-compliance"

    # 再次扫描不产生重复转配、不产生重复通知。
    sweep = service.sweep_due("dispatcher")
    assert sweep["promotions"] == [], sweep
    notifications = service.list_notifications("auditor")
    keys = [item["notification_key"] for item in notifications]
    assert len(keys) == len(set(keys)), "通知必须幂等"

    # 已核验交付不能被任何后续规则重排：资金分片仍 fulfilled，交付记录原样保留。
    funded = service.resource_trace("auditor", "r-fund-base")
    assert funded["resource"]["state"] == "fulfilled"
    assert funded["deliveries"][0]["evidence_ref"] == "evidence_revision-delivery-1"

    # 逾期扫描：专家时段有效期过后释放未兑现部分。
    clock.advance(days=58)
    expired_sweep = service.sweep_due("dispatcher")
    assert "r-expert-training" in expired_sweep["expired"], expired_sweep
    expert_after = service.resource_trace("auditor", "r-expert-training")
    assert expert_after["resource"]["state"] == "recovered", expert_after["resource"]["state"]

    # 池余额守恒：占用 = 仍在占用的生效/已交付分片之和。
    quota_status = service.pool_status("quota-pool")
    assert quota_status["occupied_quota"] == "8000.000", quota_status

    # 里程碑现状：专家时段已退出需要重新承诺，平台额度给了候补（未挂里程碑），仍阻断。
    explanation = service.explain_milestone("m-pilot")
    assert explanation["blocked"], explanation

    audit = service.audit_chain("auditor")
    assert audit["valid"], audit

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "frozen_revision": frozen["revision"],
        "milestone_blocked_reasons": [b["reason"] for b in explanation["blockers"]],
        "standby_winner": winner["promotions_won"][0],
        "quota_pool": quota_status,
        "notification_count": len(notifications),
        "audit": audit,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行条件化承诺管理离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
