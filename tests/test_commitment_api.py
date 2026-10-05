from __future__ import annotations

import json
import sqlite3
import unittest

from commitment_control.api import JsonApplication
from commitment_control.service import CommitmentControlService


HEADERS = {"X-Actor-Id": "office"}
EVALUATOR = {"X-Actor-Id": "evaluator"}
AUDITOR = {"X-Actor-Id": "auditor"}


class CommitmentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(CommitmentControlService(self.connection))
        bootstrap = {"X-Actor-Id": "bootstrap"}
        self.app.handle("POST", "/users", bootstrap, json.dumps({
            "user_id": "office", "display_name": "办公室", "role": "office"}).encode())
        self.app.handle("POST", "/users", bootstrap, json.dumps({
            "user_id": "evaluator", "display_name": "评估员", "role": "evaluator"}).encode())
        self.app.handle("POST", "/users", bootstrap, json.dumps({
            "user_id": "auditor", "display_name": "审计", "role": "auditor"}).encode())

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, headers: dict | None = None) -> object:
        response = self.app.handle(
            "POST", path, headers or HEADERS, json.dumps(payload).encode("utf-8")
        )
        return response

    def test_health_and_missing_actor(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/pools/status/x")
        self.assertEqual(response.status, 422)

    def test_full_conditional_release_flow_over_http(self) -> None:
        self.post("/programs", {"program_id": "p1", "name": "项目", "lead_office": "办公室"})
        self.post("/milestones", {"milestone_id": "m1", "program_id": "p1", "title": "上线"})
        self.post("/pools", {"pool_id": "q1", "resource_type": "platform_quota", "total_quota": "1000"})

        commitment = {
            "commitment_id": "c1",
            "program_id": "p1",
            "title": "联合承诺",
            "idempotency_key": "freeze-1",
            "conditions": [
                {"condition_id": "g", "label": "合规", "depends_on": [],
                 "evidence": [{"evidence_ref": "ev-1", "content_sha256": "a" * 64}]},
            ],
            "resources": [
                {"resource_version_id": "r1", "pool_id": "q1", "provider_id": "A", "beneficiary_id": "B",
                 "amount": "100", "gate_condition_id": "g", "release_order": 0,
                 "valid_until": "2026-12-31T23:59:59Z", "exit_policy": "reallocate_pool",
                 "milestones": [{"milestone_id": "m1", "required_state": "active"}]},
            ],
        }
        frozen = self.post("/commitments", commitment)
        self.assertEqual(frozen.status, 201, frozen.body)
        self.assertEqual(frozen.body["activated"], [])

        # 幂等重放。
        replay = self.post("/commitments", commitment)
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.body["revision"], 1)

        # 里程碑仍被阻断，能给出条件原因。
        explain = self.app.handle("GET", "/milestones/explain/m1", HEADERS)
        self.assertTrue(explain.body["blocked"])

        evaluate = self.post("/evidence/evaluate", {
            "condition_id": "c1#r1:g", "evidence_ref": "ev-1",
            "outcome": "satisfied", "note": "通过",
        }, EVALUATOR)
        self.assertEqual(evaluate.status, 200, evaluate.body)
        self.assertEqual(evaluate.body["condition"]["state"], "satisfied")

        trace = self.app.handle("GET", "/resources/trace/r1", AUDITOR)
        self.assertEqual(trace.body["resource"]["state"], "active")

        explain = self.app.handle("GET", "/milestones/explain/m1", HEADERS)
        self.assertFalse(explain.body["blocked"])

        notifications = self.app.handle("GET", "/notifications", AUDITOR)
        self.assertEqual(notifications.status, 200)
        keys = [item["notification_key"] for item in notifications.body]
        self.assertEqual(len(keys), len(set(keys)))

        chain = self.app.handle("GET", "/audit/chain", AUDITOR)
        self.assertTrue(chain.body["valid"])

    def test_unknown_route_and_bad_json(self) -> None:
        self.assertEqual(self.app.handle("GET", "/nope", HEADERS).status, 404)
        response = self.app.handle("POST", "/pools", HEADERS, b"{bad")
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
