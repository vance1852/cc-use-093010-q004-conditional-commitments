"""条件化承诺管理的领域用例。

承诺按版本冻结提供方、受益方、前置证据、相互依赖、释放顺序、有效期与退出责任；
条件独立满足时部分生效；失败或逾期只释放未兑现部分并按稳定候补转配；
已核验交付只追加。所有占用、生效、交付、释放、回收进入资源台账与哈希链审计。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .contracts import CommitmentSpec, parse_commitment
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, decimal_text, digest, quantize
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "office": {
        "program.write", "milestone.write", "pool.write", "commitment.write",
        "sweep.run", "promotion.run", "report.read", "trace.read", "notification.read",
    },
    "evaluator": {"evidence.evaluate", "report.read", "trace.read"},
    "dispatcher": {
        "delivery.write", "sweep.run", "promotion.run",
        "report.read", "trace.read", "notification.read",
    },
    "auditor": {"audit.read", "report.read", "trace.read", "notification.read"},
}

RESOURCE_TYPE_UNITS = {"fund": "CNY", "expert_time": "HOUR", "platform_quota": "UNIT"}

# 已生效即视为放行完成的资源状态。
_ACTIVE_LIKE = ("active", "fulfilled", "recovered", "reallocated")


class CommitmentControlService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self._promotion_suppressed = False
        initialize(connection)
        # 系统归因主体：里程碑自动达成等无操作者事件使用。
        connection.execute(
            "INSERT OR IGNORE INTO cc_users(user_id,display_name,role,created_at) "
            "VALUES('system','系统','office',?)",
            (utc_text(self.clock.now()),),
        )

    # ── 基础工具 ────────────────────────────────────────────────────────────

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM cc_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM cc_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO cc_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def _notify(self, key: str, scope_type: str, scope_id: str, subject: str, body: Mapping[str, Any]) -> None:
        """相同 notification_key 的通知只入账一次（幂等）。"""
        self.connection.execute(
            "INSERT OR IGNORE INTO notification_outbox(notification_key,scope_type,scope_id,subject,"
            "body_json,created_at) VALUES(?,?,?,?,?,?)",
            (key, scope_type, scope_id, subject, canonical_json(body), self._now()),
        )

    def _idempotent(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM cc_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _save_idempotent(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO cc_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ── 目录：用户、项目、里程碑、资源池 ──────────────────────────────────────

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO cc_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_program(self, actor_id: str, program_id: str, name: str, lead_office: str) -> dict[str, Any]:
        self._require(actor_id, "program.write")
        if not program_id.strip() or not name.strip() or not lead_office.strip():
            raise ValidationFailed("项目编号、名称和牵头办公室不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO programs(program_id,program_name,lead_office,created_at) VALUES(?,?,?,?)",
                    (program_id.strip(), name.strip(), lead_office.strip(), self._now()),
                )
                self._audit("program", program_id, "program.registered", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目已经存在") from exc
        return {"program_id": program_id.strip()}

    def register_milestone(self, actor_id: str, milestone_id: str, program_id: str, title: str) -> dict[str, Any]:
        self._require(actor_id, "milestone.write")
        with transaction(self.connection, immediate=True):
            if self.connection.execute("SELECT 1 FROM programs WHERE program_id=?", (program_id,)).fetchone() is None:
                raise NotFound("项目不存在")
            try:
                self.connection.execute(
                    "INSERT INTO milestones(milestone_id,program_id,title,created_by,created_at) VALUES(?,?,?,?,?)",
                    (milestone_id, program_id, title, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("里程碑已经存在") from exc
            self._audit("milestone", milestone_id, "milestone.registered", actor_id, {"program_id": program_id})
        return {"milestone_id": milestone_id, "state": "open"}

    def create_pool(self, actor_id: str, pool_id: str, resource_type: str, total_quota: object) -> dict[str, Any]:
        self._require(actor_id, "pool.write")
        if resource_type not in RESOURCE_TYPE_UNITS:
            raise ValidationFailed("resource_type 必须是 fund、expert_time 或 platform_quota")
        total = quantize(Decimal(str(total_quota)))
        if total <= 0:
            raise ValidationFailed("资源池总额必须为正数")
        unit = RESOURCE_TYPE_UNITS[resource_type]
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO resource_pools(pool_id,resource_type,unit,total_quota,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (pool_id, resource_type, unit, decimal_text(total), actor_id, self._now()),
                )
                self.connection.execute(
                    "INSERT INTO pool_balances(pool_id,total_quota,occupied_quota,earmarked_quota) "
                    "VALUES(?,?, '0.000', '0.000')",
                    (pool_id, decimal_text(total)),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("资源池已经存在") from exc
            self._audit("pool", pool_id, "pool.created", actor_id,
                        {"resource_type": resource_type, "total_quota": decimal_text(total)})
        return {"pool_id": pool_id, "unit": unit,
                "total_quota": decimal_text(total), "occupied_quota": "0.000"}

    def pool_status(self, pool_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT p.resource_type,p.unit,b.total_quota,b.occupied_quota,b.earmarked_quota "
            "FROM resource_pools p JOIN pool_balances b ON b.pool_id=p.pool_id WHERE p.pool_id=?",
            (pool_id,),
        ).fetchone()
        if row is None:
            raise NotFound("资源池不存在")
        total = Decimal(row["total_quota"])
        occupied = Decimal(row["occupied_quota"])
        earmarked = Decimal(row["earmarked_quota"])
        offers = self.connection.execute(
            "SELECT offer_id,source_resource_version_id,amount,remaining_amount,state,created_at "
            "FROM release_offers WHERE pool_id=? ORDER BY offer_id",
            (pool_id,),
        ).fetchall()
        queue = self.connection.execute(
            "SELECT resource_version_id,committed_amount,standby_priority,gate_condition_id,valid_until "
            "FROM commitment_resources WHERE pool_id=? AND state='waiting' AND standby_priority IS NOT NULL "
            "ORDER BY standby_priority,created_at,resource_version_id",
            (pool_id,),
        ).fetchall()
        return {
            "pool_id": pool_id,
            "resource_type": row["resource_type"],
            "unit": row["unit"],
            "total_quota": decimal_text(total),
            "occupied_quota": decimal_text(occupied),
            "earmarked_quota": decimal_text(earmarked),
            "available_quota": decimal_text(quantize(total - occupied - earmarked)),
            "open_offers": [dict(item) for item in offers],
            "standby_queue": [dict(item) for item in queue],
        }

    # ── 承诺版本冻结 ──────────────────────────────────────────────────────────

    def freeze_commitment(self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        spec = parse_commitment(raw)
        request_digest = digest(raw)
        scope = f"freeze:{spec.commitment_id}"
        existing = self._idempotent(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        now = self._now()
        definition = spec.normalized()
        content_sha = digest(definition)
        with transaction(self.connection, immediate=True):
            program = self.connection.execute(
                "SELECT 1 FROM programs WHERE program_id=?", (spec.program_id,)
            ).fetchone()
            if program is None:
                raise NotFound("项目不存在")
            if self.connection.execute(
                "SELECT 1 FROM commitments WHERE commitment_id=? AND content_sha256=?",
                (spec.commitment_id, content_sha),
            ).fetchone() is not None:
                raise Conflict("内容相同的承诺版本已经存在")
            for resource in spec.resources:
                if self.connection.execute(
                    "SELECT 1 FROM resource_pools WHERE pool_id=?", (resource.pool_id,)
                ).fetchone() is None:
                    raise NotFound(f"资源池不存在: {resource.pool_id}")
                if self.connection.execute(
                    "SELECT 1 FROM commitment_resources WHERE resource_version_id=?",
                    (resource.resource_version_id,),
                ).fetchone() is not None:
                    raise Conflict(f"资源分片编号已被其他版本占用: {resource.resource_version_id}")
                for link in resource.milestones:
                    milestone = self.connection.execute(
                        "SELECT program_id FROM milestones WHERE milestone_id=?", (link["milestone_id"],)
                    ).fetchone()
                    if milestone is None:
                        raise NotFound(f"里程碑不存在: {link['milestone_id']}")
                    if milestone["program_id"] != spec.program_id:
                        raise ValidationFailed(f"里程碑 {link['milestone_id']} 不属于同一项目")

            previous = self.connection.execute(
                "SELECT max(revision) AS revision FROM commitments WHERE commitment_id=?",
                (spec.commitment_id,),
            ).fetchone()
            revision = 1 if previous["revision"] is None else int(previous["revision"]) + 1

            # 条件标识按版本加命名空间，旧版本条件永久保留、不可改写。
            def global_condition(local_id: str) -> str:
                return f"{spec.commitment_id}#r{revision}:{local_id}"

            condition_map = {
                item.condition_id: global_condition(item.condition_id) for item in spec.conditions
            }

            # 旧版本中尚未生效的资源让新版本接管：释放其占用并退出候补；已生效/已交付不动。
            # 换版期间抑制候补抢占，避免旧承诺刚释放就被他人抢走，导致新版本占用失败。
            if revision > 1:
                self._promotion_suppressed = True
                try:
                    self.connection.execute(
                        "UPDATE commitments SET state='superseded',superseded_at=? "
                        "WHERE commitment_id=? AND state='active'",
                        (now, spec.commitment_id),
                    )
                    old_waiting = self.connection.execute(
                        "SELECT * FROM commitment_resources WHERE commitment_id=? AND state='waiting' "
                        "ORDER BY created_at,resource_version_id",
                        (spec.commitment_id,),
                    ).fetchall()
                    for old in old_waiting:
                        self._expire_or_fail_resource(
                            old, "承诺已被新版本取代", now, actor=actor_id, terminal_state="superseded"
                        )
                finally:
                    self._promotion_suppressed = False

            self.connection.execute(
                "INSERT INTO commitments(commitment_id,revision,program_id,title,definition_json,"
                "content_sha256,state,created_by,created_at) VALUES(?,?,?,?,?,?, 'active',?,?)",
                (
                    spec.commitment_id, revision, spec.program_id, spec.title,
                    canonical_json(definition), content_sha, actor_id, now,
                ),
            )
            for condition in spec.conditions:
                self.connection.execute(
                    "INSERT INTO commitment_conditions(condition_id,commitment_id,revision,"
                    "local_condition_id,label,state) VALUES(?,?,?,?,?, 'pending')",
                    (condition_map[condition.condition_id], spec.commitment_id, revision,
                     condition.condition_id, condition.label),
                )
            for condition in spec.conditions:
                for dep in condition.depends_on:
                    self.connection.execute(
                        "INSERT INTO condition_dependencies(condition_id,depends_on_condition_id) VALUES(?,?)",
                        (condition_map[condition.condition_id], condition_map[dep]),
                    )
                for claim in condition.evidence:
                    self.connection.execute(
                        "INSERT INTO evidence_claims(condition_id,evidence_ref,content_sha256,state,"
                        "created_by,created_at) VALUES(?, ?,?, 'pending',?,?)",
                        (condition_map[condition.condition_id], claim["evidence_ref"],
                         claim["content_sha256"], actor_id, now),
                    )

            # 先登记全部资源分片（门控引用映射到全局条件标识）。
            for resource in spec.resources:
                self.connection.execute(
                    "INSERT INTO commitment_resources(resource_version_id,commitment_id,revision,pool_id,"
                    "provider_id,beneficiary_id,committed_amount,gate_condition_id,release_order,valid_until,"
                    "exit_policy,standby_priority,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'waiting',?)",
                    (
                        resource.resource_version_id, spec.commitment_id, revision, resource.pool_id,
                        resource.provider_id, resource.beneficiary_id, decimal_text(resource.amount),
                        None if resource.gate_condition_id is None
                        else condition_map[resource.gate_condition_id],
                        resource.release_order, resource.valid_until,
                        resource.exit_policy, resource.standby_priority, now,
                    ),
                )
                for link in resource.milestones:
                    self.connection.execute(
                        "INSERT INTO milestone_links(milestone_id,resource_version_id,required_state) "
                        "VALUES(?,?,?)",
                        (link["milestone_id"], resource.resource_version_id, link["required_state"]),
                    )

            # 占用资源池：非候补分片在冻结时即占用（无条件的立即生效；带门的等待前置条件）。
            reserves: dict[str, Decimal] = {}
            for resource in spec.resources:
                if resource.standby_priority is None:
                    reserves[resource.pool_id] = reserves.get(resource.pool_id, Decimal(0)) + resource.amount
            for pool_id, need in sorted(reserves.items()):
                balance = self.connection.execute(
                    "SELECT total_quota,occupied_quota,earmarked_quota FROM pool_balances WHERE pool_id=?",
                    (pool_id,),
                ).fetchone()
                available = (
                    Decimal(balance["total_quota"])
                    - Decimal(balance["occupied_quota"])
                    - Decimal(balance["earmarked_quota"])
                )
                if need > available:
                    raise Conflict(
                        f"资源池 {pool_id} 可占用额度不足：需要 {decimal_text(need)}，"
                        f"可用 {decimal_text(quantize(available))}"
                    )
                self.connection.execute(
                    "UPDATE pool_balances SET occupied_quota=? WHERE pool_id=?",
                    (decimal_text(Decimal(balance["occupied_quota"]) + need), pool_id),
                )

            # 无条件分片按释放顺序立即生效；带门分片只入账占用，等待条件满足。
            activated: list[str] = []
            for resource in sorted(
                (item for item in spec.resources if item.standby_priority is None),
                key=lambda item: (item.release_order, item.resource_version_id),
            ):
                row = self.connection.execute(
                    "SELECT * FROM commitment_resources WHERE resource_version_id=?",
                    (resource.resource_version_id,),
                ).fetchone()
                gate = None if resource.gate_condition_id is None else condition_map[resource.gate_condition_id]
                self._insert_event(row, "occupied", resource.amount, actor_id, now, condition_id=gate,
                                   notification_key=f"occupied:{resource.resource_version_id}")
                if gate is None:
                    self._activate_reserved(row, None, actor_id, now)
                    activated.append(resource.resource_version_id)

            # 新版本接管完成后：新直接承诺所在池与候补所在池都尝试一次要约竞争。
            standby_pools = {
                item.pool_id for item in spec.resources if item.standby_priority is not None
            }
            for pool_id in sorted(set(reserves) | standby_pools):
                self._try_promote(pool_id, actor_id, now)

            self._audit("commitment", spec.commitment_id, "commitment.frozen", actor_id,
                        {"revision": revision, "sha256": content_sha, "activated": activated})
            response = {
                "commitment_id": spec.commitment_id,
                "revision": revision,
                "state": "active",
                "content_sha256": content_sha,
                "activated": activated,
            }
            self._save_idempotent(scope, idempotency_key, request_digest, response)
        return response

    def get_commitment(self, commitment_id: str, revision: int | None = None) -> dict[str, Any]:
        if revision is None:
            row = self.connection.execute(
                "SELECT * FROM commitments WHERE commitment_id=? ORDER BY revision DESC LIMIT 1",
                (commitment_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM commitments WHERE commitment_id=? AND revision=?", (commitment_id, revision)
            ).fetchone()
        if row is None:
            raise NotFound("承诺版本不存在")
        result = dict(row)
        result["definition"] = json.loads(row["definition_json"])
        return result

    # ── 前置证据与条件评估 ───────────────────────────────────────────────────

    def evaluate_evidence(
        self, actor_id: str, condition_id: str, evidence_ref: str, outcome: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence.evaluate")
        if outcome not in {"satisfied", "failed"}:
            raise ValidationFailed("outcome 必须是 satisfied 或 failed")
        with transaction(self.connection, immediate=True):
            claim = self.connection.execute(
                "SELECT * FROM evidence_claims WHERE condition_id=? AND evidence_ref=?",
                (condition_id, evidence_ref),
            ).fetchone()
            if claim is None:
                raise NotFound("前置证据不存在")
            condition = self.connection.execute(
                "SELECT * FROM commitment_conditions WHERE condition_id=?", (condition_id,)
            ).fetchone()
            if condition is None:
                raise NotFound("条件不存在")
            commitment = self.connection.execute(
                "SELECT state FROM commitments WHERE commitment_id=? AND revision=?",
                (condition["commitment_id"], condition["revision"]),
            ).fetchone()
            if commitment["state"] != "active":
                raise InvalidState("条件所属承诺版本已冻结归档，不能再登记证据结论")
            if condition["state"] != "pending":
                raise InvalidState("条件已经作出结论，不能再登记证据结论")
            if claim["state"] != "pending":
                raise InvalidState("该证据已经作出结论")
            now = self._now()
            self.connection.execute(
                "UPDATE evidence_claims SET state=?,note=?,decided_by=?,decided_at=? WHERE claim_id=?",
                (outcome, note, actor_id, now, claim["claim_id"]),
            )
            self._audit("condition", condition_id, f"evidence.{outcome}", actor_id,
                        {"evidence_ref": evidence_ref, "note": note})
            if outcome == "failed":
                self._fail_condition(condition_id, f"前置证据 {evidence_ref} 未通过", actor_id, now)
            else:
                self._maybe_satisfy(condition_id, actor_id, now)
        return self.condition_status(condition_id)

    def decide_condition(self, actor_id: str, condition_id: str, outcome: str, note: str = "") -> dict[str, Any]:
        """评估员对无登记证据的条件直接下结论（如现场核查）。"""
        self._require(actor_id, "evidence.evaluate")
        if outcome not in {"satisfied", "failed"}:
            raise ValidationFailed("outcome 必须是 satisfied 或 failed")
        with transaction(self.connection, immediate=True):
            condition = self.connection.execute(
                "SELECT * FROM commitment_conditions WHERE condition_id=?", (condition_id,)
            ).fetchone()
            if condition is None:
                raise NotFound("条件不存在")
            commitment = self.connection.execute(
                "SELECT state FROM commitments WHERE commitment_id=? AND revision=?",
                (condition["commitment_id"], condition["revision"]),
            ).fetchone()
            if commitment["state"] != "active":
                raise InvalidState("条件所属承诺版本已冻结归档，不能再下结论")
            if condition["state"] != "pending":
                raise InvalidState("条件已经作出结论")
            claims = self.connection.execute(
                "SELECT count(*) AS n FROM evidence_claims WHERE condition_id=?", (condition_id,)
            ).fetchone()["n"]
            if claims:
                raise InvalidState("该条件挂有前置证据，必须通过证据核验得出结论")
            now = self._now()
            if outcome == "failed":
                self._fail_condition(condition_id, note or "评估员判定不满足", actor_id, now)
            else:
                self._set_condition_satisfied(condition_id, actor_id, now)
                self._propagate_satisfaction(actor_id, now)
        return self.condition_status(condition_id)

    def condition_status(self, condition_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM commitment_conditions WHERE condition_id=?", (condition_id,)
        ).fetchone()
        if row is None:
            raise NotFound("条件不存在")
        claims = self.connection.execute(
            "SELECT evidence_ref,content_sha256,state,note,decided_by,decided_at FROM evidence_claims "
            "WHERE condition_id=? ORDER BY claim_id",
            (condition_id,),
        ).fetchall()
        deps = self.connection.execute(
            "SELECT depends_on_condition_id FROM condition_dependencies WHERE condition_id=? ORDER BY rowid",
            (condition_id,),
        ).fetchall()
        return {
            "condition": dict(row),
            "dependencies": [item["depends_on_condition_id"] for item in deps],
            "evidence": [dict(item) for item in claims],
        }

    def _condition_ready(self, condition_id: str) -> bool:
        """依赖全部满足且证据全部通过。"""
        deps = self.connection.execute(
            "SELECT c.state FROM condition_dependencies d "
            "JOIN commitment_conditions c ON c.condition_id=d.depends_on_condition_id "
            "WHERE d.condition_id=?",
            (condition_id,),
        ).fetchall()
        if any(item["state"] != "satisfied" for item in deps):
            return False
        claims = self.connection.execute(
            "SELECT state FROM evidence_claims WHERE condition_id=?",
            (condition_id,),
        ).fetchall()
        if not claims:
            return False
        return all(item["state"] == "satisfied" for item in claims)

    def _maybe_satisfy(self, condition_id: str, actor_id: str, now: str) -> None:
        condition = self.connection.execute(
            "SELECT state FROM commitment_conditions WHERE condition_id=?", (condition_id,)
        ).fetchone()
        if condition["state"] == "pending" and self._condition_ready(condition_id):
            self._set_condition_satisfied(condition_id, actor_id, now)
        self._propagate_satisfaction(actor_id, now)

    def _propagate_satisfaction(self, actor_id: str, now: str) -> None:
        """依赖链上的条件在依赖满足后依次满足，并放行对应资源。"""
        changed = True
        while changed:
            changed = False
            pending = self.connection.execute(
                "SELECT condition_id FROM commitment_conditions WHERE state='pending' ORDER BY condition_id"
            ).fetchall()
            for row in pending:
                condition_id = row["condition_id"]
                claims = self.connection.execute(
                    "SELECT state FROM evidence_claims WHERE condition_id=?", (condition_id,)
                ).fetchall()
                if not claims:
                    continue  # 无登记证据的条件等待评估员直接结论
                if self._condition_ready(condition_id):
                    self._set_condition_satisfied(condition_id, actor_id, now)
                    changed = True

    def _set_condition_satisfied(self, condition_id: str, actor_id: str, now: str) -> None:
        self.connection.execute(
            "UPDATE commitment_conditions SET state='satisfied',satisfied_at=? WHERE condition_id=? AND state='pending'",
            (now, condition_id),
        )
        self._audit("condition", condition_id, "condition.satisfied", actor_id, {})
        self._notify(f"condition:satisfied:{condition_id}", "condition", condition_id,
                     "前置条件已满足", {"condition_id": condition_id})
        # 按释放顺序放行被该条件门控、且在冻结时已占用的分片。
        resources = self.connection.execute(
            "SELECT * FROM commitment_resources WHERE gate_condition_id=? AND state='waiting' "
            "AND standby_priority IS NULL ORDER BY release_order,resource_version_id",
            (condition_id,),
        ).fetchall()
        for resource in resources:
            self._activate_reserved(resource, condition_id, actor_id, now)
        # 候补分片可能因该条件满足而获得消费开放要约的资格。
        pools = {
            item["pool_id"]
            for item in self.connection.execute(
                "SELECT DISTINCT pool_id FROM commitment_resources WHERE state='waiting' AND standby_priority IS NOT NULL"
            ).fetchall()
        }
        for pool_id in sorted(pools):
            self._try_promote(pool_id, actor_id, now)
        self._reevaluate_milestones(now)

    def _fail_condition(self, condition_id: str, reason: str, actor_id: str, now: str) -> None:
        """条件失败：级联依赖它的条件；释放仍等待中的资源未兑现部分并转配候补。"""
        failed: list[tuple[str, str]] = [(condition_id, reason)]
        seen = {condition_id}
        frontier = [condition_id]
        while frontier:
            next_frontier: list[str] = []
            for node in frontier:
                rows = self.connection.execute(
                    "SELECT condition_id FROM condition_dependencies WHERE depends_on_condition_id=?",
                    (node,),
                ).fetchall()
                for row in rows:
                    dependent = row["condition_id"]
                    if dependent not in seen:
                        seen.add(dependent)
                        failed.append((dependent, f"依赖条件 {node} 失败"))
                        next_frontier.append(dependent)
            frontier = next_frontier
        for failed_id, failed_reason in failed:
            self.connection.execute(
                "UPDATE commitment_conditions SET state='failed',failed_at=? WHERE condition_id=? AND state='pending'",
                (now, failed_id),
            )
            self._audit("condition", failed_id, "condition.failed", actor_id, {"reason": failed_reason})
            self._notify(f"condition:failed:{failed_id}", "condition", failed_id,
                         "前置条件失败", {"condition_id": failed_id, "reason": failed_reason})
            resources = self.connection.execute(
                "SELECT * FROM commitment_resources WHERE gate_condition_id=? AND state='waiting' "
                "ORDER BY release_order,resource_version_id",
                (failed_id,),
            ).fetchall()
            for resource in resources:
                self._expire_or_fail_resource(resource, failed_reason, now, condition_id=failed_id, actor=actor_id)

    # ── 占用、生效、释放、回收、转配 ──────────────────────────────────────────

    def _insert_event(
        self,
        resource: sqlite3.Row | str,
        event_type: str,
        amount: Decimal,
        actor: str,
        now: str,
        *,
        condition_id: str | None = None,
        reason: str = "",
        linked_event_id: int | None = None,
        notification_key: str | None = None,
    ) -> int:
        rvid = resource if isinstance(resource, str) else resource["resource_version_id"]
        pool_id = resource["pool_id"] if isinstance(resource, sqlite3.Row) else self.connection.execute(
            "SELECT pool_id FROM commitment_resources WHERE resource_version_id=?", (rvid,)
        ).fetchone()["pool_id"]
        cursor = self.connection.execute(
            "INSERT INTO resource_events(resource_version_id,pool_id,event_type,amount,condition_id,reason,"
            "linked_event_id,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rvid, pool_id, event_type, decimal_text(amount), condition_id, reason,
             linked_event_id, actor, now),
        )
        event_id = int(cursor.lastrowid)
        if notification_key is None:
            notification_key = f"{event_type}:{rvid}"
        self._notify(notification_key, "resource", rvid, event_type, {
            "resource_version_id": rvid, "event_type": event_type,
            "amount": decimal_text(amount), "reason": reason,
        })
        return event_id

    def _activate_reserved(
        self, resource: sqlite3.Row, condition_id: str | None, actor: str, now: str
    ) -> None:
        amount = Decimal(resource["committed_amount"])
        self.connection.execute(
            "UPDATE commitment_resources SET state='active',activated_amount=committed_amount,"
            "activated_at=? WHERE resource_version_id=? AND state='waiting'",
            (now, resource["resource_version_id"]),
        )
        self._insert_event(resource, "activated", amount, actor, now, condition_id=condition_id,
                           notification_key=f"activated:{resource['resource_version_id']}")
        self._audit("resource", resource["resource_version_id"], "resource.activated", actor,
                    {"commitment_id": resource["commitment_id"], "revision": resource["revision"],
                     "condition_id": condition_id, "amount": decimal_text(amount)})
        self._reevaluate_milestones(now)

    def _release_unfulfilled(
        self,
        resource: sqlite3.Row,
        reason: str,
        now: str,
        *,
        new_state: str,
        condition_id: str | None,
        actor: str,
    ) -> Decimal:
        """只释放未兑现部分；已核验交付保留。返回回收到池的额度。"""
        committed = Decimal(resource["committed_amount"])
        delivered = Decimal(resource["delivered_amount"])
        freed = quantize(committed - delivered)
        takeover = new_state == "superseded"
        if freed > 0:
            balance = self.connection.execute(
                "SELECT occupied_quota,earmarked_quota FROM pool_balances WHERE pool_id=?",
                (resource["pool_id"],),
            ).fetchone()
            occupied = Decimal(balance["occupied_quota"])
            earmarked = Decimal(balance["earmarked_quota"])
            if not takeover and resource["exit_policy"] == "reallocate_pool":
                # occupied -> earmarked：额度保留在池内专供候补，新的直接承诺不能挪用。
                self.connection.execute(
                    "UPDATE pool_balances SET occupied_quota=?,earmarked_quota=? WHERE pool_id=?",
                    (decimal_text(occupied - freed), decimal_text(earmarked + freed), resource["pool_id"]),
                )
            else:
                # 退回提供方：occupied 直接回落。
                self.connection.execute(
                    "UPDATE pool_balances SET occupied_quota=? WHERE pool_id=?",
                    (decimal_text(occupied - freed), resource["pool_id"]),
                )
            if takeover:
                self.connection.execute(
                    "UPDATE commitment_resources SET state=?,finished_at=? WHERE resource_version_id=?",
                    (new_state, now, resource["resource_version_id"]),
                )
                exit_event_id = self._insert_event(
                    resource, "superseded", freed, actor, now,
                    condition_id=condition_id, reason=reason,
                    notification_key=f"superseded:{resource['resource_version_id']}",
                )
            else:
                new_recovered = quantize(
                    Decimal(self.connection.execute(
                        "SELECT recovered_amount FROM commitment_resources WHERE resource_version_id=?",
                        (resource["resource_version_id"],),
                    ).fetchone()["recovered_amount"]) + freed
                )
                self.connection.execute(
                    "UPDATE commitment_resources SET recovered_amount=?,state=?,finished_at=? "
                    "WHERE resource_version_id=?",
                    (decimal_text(new_recovered), new_state, now, resource["resource_version_id"]),
                )
                exit_event_id = self._insert_event(
                    resource, "recovered", freed, actor, now,
                    condition_id=condition_id, reason=reason,
                    notification_key=f"recovered:{resource['resource_version_id']}",
                )
            self._audit("resource", resource["resource_version_id"],
                        f"resource.{new_state}", actor, {
                "commitment_id": resource["commitment_id"], "revision": resource["revision"],
                "amount": decimal_text(freed), "reason": reason, "exit_policy": resource["exit_policy"],
            })
            if not takeover and resource["exit_policy"] == "reallocate_pool":
                # 释放出的未兑现部分形成可转配要约，是候补资源的唯一合法来源；
                # 要约在来源资源有效期内持续开放供稳定候补竞争。
                self.connection.execute(
                    "INSERT INTO release_offers(pool_id,source_resource_version_id,source_event_id,"
                    "amount,remaining_amount,expires_at,state,created_at) VALUES(?,?,?,?,?,?,'open',?)",
                    (resource["pool_id"], resource["resource_version_id"], exit_event_id,
                     decimal_text(freed), decimal_text(freed), resource["valid_until"], now),
                )
                self._try_promote(resource["pool_id"], actor, now)
        else:
            self.connection.execute(
                "UPDATE commitment_resources SET state=?,finished_at=? WHERE resource_version_id=?",
                (new_state, now, resource["resource_version_id"]),
            )
        self._reevaluate_milestones(now)
        return freed

    def _expire_or_fail_resource(
        self,
        resource: sqlite3.Row,
        reason: str,
        now: str,
        *,
        actor: str,
        condition_id: str | None = None,
        terminal_state: str = "failed",
    ) -> None:
        occupied = self.connection.execute(
            "SELECT 1 FROM resource_events WHERE resource_version_id=? AND event_type='occupied'",
            (resource["resource_version_id"],),
        ).fetchone()
        if occupied is None:
            # 候补或从未占用：直接退出排队，不产生回收。
            self.connection.execute(
                "UPDATE commitment_resources SET state=?,finished_at=? "
                "WHERE resource_version_id=? AND state='waiting'",
                (terminal_state, now, resource["resource_version_id"]),
            )
            self._insert_event(resource, "condition_failed", Decimal(0), actor, now,
                               condition_id=condition_id, reason=reason,
                               notification_key=f"failed:{resource['resource_version_id']}")
            self._audit("resource", resource["resource_version_id"], f"resource.{terminal_state}", actor,
                        {"reason": reason, "condition_id": condition_id})
            return
        self._insert_event(resource, "condition_failed", Decimal(0), actor, now,
                           condition_id=condition_id, reason=reason,
                           notification_key=f"failed:{resource['resource_version_id']}")
        self._release_unfulfilled(
            resource, reason, now, new_state=terminal_state, condition_id=condition_id, actor=actor
        )

    def _try_promote(self, pool_id: str, actor: str, now: str) -> dict[str, Any] | None:
        """消费释放要约，从稳定候补队列中选一个且仅一个胜者。

        候补只能消费 release_offers 中由失败/逾期释放形成的要约，不占用池内普通空闲额度；
        一次竞争至多产生一个胜者，胜者可合并多个要约，消耗逐要约留痕。
        """
        if self._promotion_suppressed:
            return None
        offers = self.connection.execute(
            "SELECT * FROM release_offers WHERE pool_id=? AND state='open' ORDER BY offer_id",
            (pool_id,),
        ).fetchall()
        if not offers:
            return None
        candidates = self.connection.execute(
            "SELECT * FROM commitment_resources WHERE pool_id=? AND state='waiting' "
            "AND standby_priority IS NOT NULL AND valid_until > ? "
            "AND (gate_condition_id IS NULL OR EXISTS("
            "SELECT 1 FROM commitment_conditions c "
            "WHERE c.condition_id=commitment_resources.gate_condition_id AND c.state='satisfied')"
            ") ORDER BY standby_priority,created_at,resource_version_id",
            (pool_id, now),
        ).fetchall()
        if not candidates:
            # 暂无合格候补：要约保留开放，等待后续候补登记或逾期退回（见 sweep_due）。
            return None
        total_open = quantize(sum((Decimal(item["remaining_amount"]) for item in offers), Decimal(0)))
        winner = None
        for candidate in candidates:
            if Decimal(candidate["committed_amount"]) <= total_open:
                winner = candidate
                break
        if winner is None:
            return None  # 候补都不满足（额度不够或门未开），要约保留等待。
        amount = Decimal(winner["committed_amount"])
        need = amount
        links: list[dict[str, Any]] = []
        for offer in offers:
            if need <= 0:
                break
            remaining = Decimal(offer["remaining_amount"])
            take = quantize(min(remaining, need))
            # 每笔要约逐条读取最新余额后做条件移动：earmarked -> occupied，CHECK 约束防止超额。
            balance = self.connection.execute(
                "SELECT occupied_quota,earmarked_quota FROM pool_balances WHERE pool_id=?",
                (pool_id,),
            ).fetchone()
            if Decimal(balance["earmarked_quota"]) < take:
                raise Conflict("候补转配竞争失败：要约额度已被占用")
            self.connection.execute(
                "UPDATE pool_balances SET earmarked_quota=?,occupied_quota=? WHERE pool_id=?",
                (
                    decimal_text(Decimal(balance["earmarked_quota"]) - take),
                    decimal_text(Decimal(balance["occupied_quota"]) + take),
                    pool_id,
                ),
            )
            left = quantize(remaining - take)
            self.connection.execute(
                "UPDATE release_offers SET remaining_amount=?,"
                "state=CASE WHEN ?=0 THEN 'consumed' ELSE 'open' END WHERE offer_id=?",
                (decimal_text(left), 1 if left > 0 else 0, offer["offer_id"]),
            )
            links.append({
                "offer_id": offer["offer_id"], "amount": decimal_text(take),
                "source_resource_version_id": offer["source_resource_version_id"],
                "source_event_id": offer["source_event_id"],
            })
            need = quantize(need - take)
        self.connection.execute(
            "UPDATE commitment_resources SET state='active',activated_amount=committed_amount,activated_at=? "
            "WHERE resource_version_id=? AND state='waiting'",
            (now, winner["resource_version_id"]),
        )
        self._insert_event(
            winner, "occupied", amount, actor, now,
            condition_id=winner["gate_condition_id"], reason="候补转配占用",
            notification_key=f"occupied:standby:{winner['resource_version_id']}",
        )
        in_event = self._insert_event(
            winner, "reallocated_in", amount, actor, now,
            condition_id=winner["gate_condition_id"], reason="稳定候补转配",
            linked_event_id=links[0]["source_event_id"],
            notification_key=f"reallocated:{winner['resource_version_id']}",
        )
        promotion_cursor = self.connection.execute(
            "INSERT INTO standby_promotions(pool_id,winner_resource_version_id,"
            "source_resource_version_id,source_event_id,amount,promoted_by,promoted_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                pool_id, winner["resource_version_id"],
                links[0]["source_resource_version_id"], links[0]["source_event_id"],
                decimal_text(amount), actor, now,
            ),
        )
        promotion_id = int(promotion_cursor.lastrowid)
        for link in links:
            self.connection.execute(
                "INSERT INTO standby_offer_links(promotion_id,offer_id,amount) VALUES(?,?,?)",
                (promotion_id, link["offer_id"], link["amount"]),
            )
        self._audit("resource", winner["resource_version_id"], "resource.promoted", actor, {
            "pool_id": pool_id, "amount": decimal_text(amount),
            "promotion_id": promotion_id, "reallocated_event_id": in_event,
            "sources": links,
        })
        self._reevaluate_milestones(now)
        return {
            "pool_id": pool_id,
            "winner_resource_version_id": winner["resource_version_id"],
            "amount": decimal_text(amount),
            "promotion_id": promotion_id,
            "sources": links,
        }

    # ── 交付核验（只追加） ───────────────────────────────────────────────────

    def record_delivery(
        self,
        actor_id: str,
        resource_version_id: str,
        amount: object,
        evidence_ref: str,
        content_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "delivery.write")
        value = quantize(Decimal(str(amount)))
        if value <= 0:
            raise ValidationFailed("交付数量必须为正数")
        if not evidence_ref.strip() or len(content_sha256) != 64:
            raise ValidationFailed("交付证据引用与 64 位摘要必填")
        request_digest = digest({
            "resource_version_id": resource_version_id,
            "amount": decimal_text(value),
            "evidence_ref": evidence_ref,
            "content_sha256": content_sha256,
        })
        scope = f"delivery:{resource_version_id}"
        existing = self._idempotent(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        with transaction(self.connection, immediate=True):
            resource = self.connection.execute(
                "SELECT * FROM commitment_resources WHERE resource_version_id=?", (resource_version_id,)
            ).fetchone()
            if resource is None:
                raise NotFound("资源分片不存在")
            if resource["state"] != "active":
                raise InvalidState("只有已生效的资源分片可以登记核验交付")
            now = self._now()
            if now > resource["valid_until"]:
                raise InvalidState("资源有效期已过，不能再登记交付")
            delivered = Decimal(resource["delivered_amount"])
            if delivered + value > Decimal(resource["committed_amount"]):
                raise Conflict(
                    f"交付超出承诺：累计 {decimal_text(quantize(delivered + value))}，"
                    f"承诺 {resource['committed_amount']}"
                )
            try:
                cursor = self.connection.execute(
                    "INSERT INTO resource_deliveries(resource_version_id,amount,evidence_ref,content_sha256,"
                    "idempotency_key,verified_by,verified_at) VALUES(?,?,?,?,?,?,?)",
                    (resource_version_id, decimal_text(value), evidence_ref, content_sha256.lower(),
                     idempotency_key, actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("交付幂等键冲突") from exc
            delivery_id = int(cursor.lastrowid)
            new_delivered = quantize(delivered + value)
            fulfilled = new_delivered == Decimal(resource["committed_amount"])
            self.connection.execute(
                "UPDATE commitment_resources SET delivered_amount=?,state=CASE WHEN ? THEN 'fulfilled' ELSE state END,"
                "finished_at=CASE WHEN ? THEN ? ELSE finished_at END WHERE resource_version_id=?",
                (decimal_text(new_delivered), 1 if fulfilled else 0, 1 if fulfilled else 0,
                 now if fulfilled else None, resource_version_id),
            )
            self._insert_event(resource, "delivered", value, actor_id, now,
                               reason=evidence_ref,
                               notification_key=f"delivered:{idempotency_key}")
            self._audit("resource", resource_version_id, "delivery.verified", actor_id, {
                "delivery_id": delivery_id, "amount": decimal_text(value),
                "evidence_ref": evidence_ref, "content_sha256": content_sha256.lower(),
            })
            self._reevaluate_milestones(now)
            response = {
                "delivery_id": delivery_id,
                "resource_version_id": resource_version_id,
                "amount": decimal_text(value),
                "delivered_amount": decimal_text(new_delivered),
                "committed_amount": resource["committed_amount"],
                "state": "fulfilled" if fulfilled else "active",
            }
            self._save_idempotent(scope, idempotency_key, request_digest, response)
        return response

    # ── 逾期扫描 ─────────────────────────────────────────────────────────────

    def sweep_due(self, actor_id: str) -> dict[str, Any]:
        """扫描失败/逾期资源与过期要约，释放未兑现部分并尝试候补转配。幂等可重复执行。"""
        self._require(actor_id, "sweep.run")
        now = self._now()
        promotions: list[dict[str, Any]] = []
        expired: list[str] = []
        returned_offers: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            due = self.connection.execute(
                "SELECT * FROM commitment_resources WHERE valid_until<=? "
                "AND state IN ('waiting','active') ORDER BY created_at,resource_version_id",
                (now,),
            ).fetchall()
            for resource in due:
                expired.append(resource["resource_version_id"])
                if resource["state"] == "waiting":
                    occupied = self.connection.execute(
                        "SELECT 1 FROM resource_events WHERE resource_version_id=? AND event_type='occupied'",
                        (resource["resource_version_id"],),
                    ).fetchone()
                    # 曾占用但未生效：逾期回收未兑现部分；纯候补从未占用：退出队列。
                    terminal = "recovered" if occupied is not None else "failed"
                    self._expire_or_fail_resource(
                        resource, "有效期已逾期", now, actor=actor_id, terminal_state=terminal
                    )
                else:
                    self._insert_event(resource, "condition_failed", Decimal(0), actor_id, now,
                                       reason="有效期已逾期",
                                       notification_key=f"expired:{resource['resource_version_id']}")
                    self._release_unfulfilled(
                        resource, "有效期已逾期", now,
                        new_state="recovered", condition_id=None, actor=actor_id,
                    )
            # 逾期仍未被候补消费的开放要约：预留额度退回提供方/池普通余额。
            stale = self.connection.execute(
                "SELECT * FROM release_offers WHERE state='open' AND expires_at<=? ORDER BY offer_id",
                (now,),
            ).fetchall()
            for offer in stale:
                remaining = Decimal(offer["remaining_amount"])
                balance = self.connection.execute(
                    "SELECT earmarked_quota FROM pool_balances WHERE pool_id=?", (offer["pool_id"],)
                ).fetchone()
                self.connection.execute(
                    "UPDATE pool_balances SET earmarked_quota=? WHERE pool_id=?",
                    (decimal_text(Decimal(balance["earmarked_quota"]) - remaining), offer["pool_id"]),
                )
                self.connection.execute(
                    "UPDATE release_offers SET state='returned' WHERE offer_id=? AND state='open'",
                    (offer["offer_id"],),
                )
                self._audit("pool", offer["pool_id"], "offer.returned", actor_id, {
                    "offer_id": offer["offer_id"], "amount": decimal_text(remaining),
                    "source_resource_version_id": offer["source_resource_version_id"],
                })
                returned_offers.append({"offer_id": offer["offer_id"], "amount": decimal_text(remaining)})
            # 开放要约（含新到期后仍有效的）与候补之间再尝试一次匹配。
            pools = self.connection.execute(
                "SELECT DISTINCT pool_id FROM release_offers WHERE state='open'"
            ).fetchall()
            for row in pools:
                promotion = self._try_promote(row["pool_id"], actor_id, now)
                if promotion is not None:
                    promotions.append(promotion)
            self._audit("system", "sweep", "sweep.completed", actor_id,
                        {"expired": expired, "returned_offers": returned_offers,
                         "promotions": promotions})
        return {"swept_at": now, "expired": expired,
                "returned_offers": returned_offers, "promotions": promotions}

    def promote_standby(self, actor_id: str, pool_id: str) -> dict[str, Any]:
        """手动触发一次候补竞争（至多一个胜者）。"""
        self._require(actor_id, "promotion.run")
        with transaction(self.connection, immediate=True):
            if self.connection.execute("SELECT 1 FROM resource_pools WHERE pool_id=?", (pool_id,)).fetchone() is None:
                raise NotFound("资源池不存在")
            promotion = self._try_promote(pool_id, actor_id, self._now())
        if promotion is None:
            return {"pool_id": pool_id, "winner": None}
        return promotion

    # ── 里程碑阻断说明 ───────────────────────────────────────────────────────

    def _reevaluate_milestones(self, now: str) -> None:
        rows = self.connection.execute(
            "SELECT DISTINCT m.milestone_id FROM milestone_links l "
            "JOIN milestones m ON m.milestone_id=l.milestone_id WHERE m.state='open'"
        ).fetchall()
        for row in rows:
            blockers = self._blocker_rows(row["milestone_id"])
            if not blockers:
                self.connection.execute(
                    "UPDATE milestones SET state='reached',reached_at=? WHERE milestone_id=? AND state='open'",
                    (now, row["milestone_id"]),
                )
                self._notify(f"milestone:reached:{row['milestone_id']}", "milestone", row["milestone_id"],
                             "里程碑已达成", {"milestone_id": row["milestone_id"]})
                self._audit("milestone", row["milestone_id"], "milestone.reached", "system", {})

    def _blocker_rows(self, milestone_id: str) -> list[dict[str, Any]]:
        links = self.connection.execute(
            "SELECT l.*,r.commitment_id,r.revision,r.state AS resource_state,r.gate_condition_id,"
            "r.committed_amount,r.delivered_amount,r.activated_amount,r.recovered_amount,r.valid_until "
            "FROM milestone_links l JOIN commitment_resources r ON r.resource_version_id=l.resource_version_id "
            "WHERE l.milestone_id=? ORDER BY r.commitment_id,r.revision,r.release_order,r.resource_version_id",
            (milestone_id,),
        ).fetchall()
        blockers: list[dict[str, Any]] = []
        for link in links:
            state = link["resource_state"]
            # 被新版本接管的旧分片：若同一承诺的新版本仍挂着本里程碑，则旧分片不再计入阻断。
            if state == "superseded":
                replaced = self.connection.execute(
                    "SELECT 1 FROM milestone_links l2 "
                    "JOIN commitment_resources r2 ON r2.resource_version_id=l2.resource_version_id "
                    "WHERE l2.milestone_id=? AND r2.commitment_id=? AND r2.revision>?",
                    (milestone_id, link["commitment_id"], link["revision"]),
                ).fetchone()
                if replaced is not None:
                    continue
            reason: str | None = None
            if link["required_state"] == "active":
                if state in ("active", "fulfilled"):
                    pass
                elif state in ("failed", "recovered", "reallocated", "superseded"):
                    reason = f"资源已退出（{state}），需要重新承诺"
                else:
                    reason = self._waiting_reason(link)
            else:  # fulfilled
                if state == "fulfilled":
                    pass
                elif state in ("failed", "recovered", "reallocated", "superseded"):
                    reason = f"资源已退出（{state}），已交付部分不能补足全部承诺"
                elif state == "active":
                    reason = (
                        f"已生效但尚未全部核验交付：已交付 {link['delivered_amount']} / "
                        f"承诺 {link['committed_amount']}"
                    )
                else:
                    reason = self._waiting_reason(link)
            if reason is not None:
                blockers.append({
                    "resource_version_id": link["resource_version_id"],
                    "commitment_id": link["commitment_id"],
                    "revision": link["revision"],
                    "required_state": link["required_state"],
                    "resource_state": state,
                    "reason": reason,
                })
        return blockers

    def _waiting_reason(self, link: sqlite3.Row) -> str:
        gate = link["gate_condition_id"]
        if gate is None:
            condition_state = None
        else:
            condition_state = self.connection.execute(
                "SELECT state,label FROM commitment_conditions WHERE condition_id=?", (gate,)
            ).fetchone()
        if gate is None:
            return f"资源仍在候补队列中，有效期至 {link['valid_until']}"
        state = condition_state["state"] if condition_state is not None else "missing"
        if state == "satisfied":
            return f"前置条件已满足，等待释放执行（有效期至 {link['valid_until']}）"
        if state == "failed":
            return f"前置条件 {gate}（{condition_state['label']}）已失败，资源不会释放"
        if state == "pending":
            pending_claims = self.connection.execute(
                "SELECT evidence_ref FROM evidence_claims WHERE condition_id=? AND state='pending' ORDER BY claim_id",
                (gate,),
            ).fetchall()
            failed_claims = self.connection.execute(
                "SELECT evidence_ref FROM evidence_claims WHERE condition_id=? AND state='failed'",
                (gate,),
            ).fetchall()
            if failed_claims:
                return f"前置条件 {gate}（{condition_state['label']}）存在未通过证据：{failed_claims[0]['evidence_ref']}"
            if pending_claims:
                return (
                    f"前置条件 {gate}（{condition_state['label']}）等待证据核验："
                    f"{pending_claims[0]['evidence_ref']}"
                )
            return f"前置条件 {gate}（{condition_state['label']}）等待评估结论"
        return f"前置条件 {gate} 不存在于当前版本"

    def explain_milestone(self, milestone_id: str) -> dict[str, Any]:
        milestone = self.connection.execute(
            "SELECT * FROM milestones WHERE milestone_id=?", (milestone_id,)
        ).fetchone()
        if milestone is None:
            raise NotFound("里程碑不存在")
        blockers = self._blocker_rows(milestone_id)
        return {
            "milestone": dict(milestone),
            "blocked": bool(blockers) and milestone["state"] == "open",
            "blockers": blockers,
        }

    # ── 全过程追踪与通知 ─────────────────────────────────────────────────────

    def resource_trace(self, actor_id: str, resource_version_id: str) -> dict[str, Any]:
        self._require(actor_id, "trace.read")
        resource = self.connection.execute(
            "SELECT * FROM commitment_resources WHERE resource_version_id=?", (resource_version_id,)
        ).fetchone()
        if resource is None:
            raise NotFound("资源分片不存在")
        commitment = self.connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=? AND revision=?",
            (resource["commitment_id"], resource["revision"]),
        ).fetchone()
        gate = None
        if resource["gate_condition_id"] is not None:
            gate = self.condition_status(resource["gate_condition_id"])
        events = self.connection.execute(
            "SELECT * FROM resource_events WHERE resource_version_id=? ORDER BY event_id",
            (resource_version_id,),
        ).fetchall()
        deliveries = self.connection.execute(
            "SELECT delivery_id,amount,evidence_ref,content_sha256,verified_by,verified_at "
            "FROM resource_deliveries WHERE resource_version_id=? ORDER BY delivery_id",
            (resource_version_id,),
        ).fetchall()
        won_rows = self.connection.execute(
            "SELECT * FROM standby_promotions WHERE winner_resource_version_id=?", (resource_version_id,)
        ).fetchall()
        won: list[dict[str, Any]] = []
        for row in won_rows:
            links = self.connection.execute(
                "SELECT l.offer_id,l.amount AS link_amount,o.source_resource_version_id,"
                "o.source_event_id,o.state FROM standby_offer_links l "
                "JOIN release_offers o ON o.offer_id=l.offer_id WHERE l.promotion_id=? ORDER BY l.offer_id",
                (row["promotion_id"],),
            ).fetchall()
            won.append(dict(row) | {"offer_links": [dict(item) for item in links]})
        # 本资源失败/逾期释放形成的要约及其最终去向。
        offers = self.connection.execute(
            "SELECT o.*,p.promotion_id,p.winner_resource_version_id,l.amount AS consumed_amount "
            "FROM release_offers o "
            "LEFT JOIN standby_offer_links l ON l.offer_id=o.offer_id "
            "LEFT JOIN standby_promotions p ON p.promotion_id=l.promotion_id "
            "WHERE o.source_resource_version_id=? ORDER BY o.offer_id,p.promotion_id",
            (resource_version_id,),
        ).fetchall()
        sourced = [dict(item) for item in offers]
        milestones = self.connection.execute(
            "SELECT milestone_id,required_state FROM milestone_links WHERE resource_version_id=?",
            (resource_version_id,),
        ).fetchall()
        return {
            "resource": dict(resource),
            "commitment": {
                "commitment_id": commitment["commitment_id"],
                "revision": commitment["revision"],
                "title": commitment["title"],
                "state": commitment["state"],
                "content_sha256": commitment["content_sha256"],
                "created_at": commitment["created_at"],
                "definition": json.loads(commitment["definition_json"]),
            },
            "gate_condition": gate,
            "provider_id": resource["provider_id"],
            "beneficiary_id": resource["beneficiary_id"],
            "events": [dict(item) for item in events],
            "deliveries": [dict(item) for item in deliveries],
            "promotions_won": won,
            "released_offers": sourced,
            "milestones": [dict(item) for item in milestones],
        }

    def list_notifications(
        self, actor_id: str, scope_type: str | None = None, scope_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self._require(actor_id, "notification.read")
        sql = "SELECT event_id,notification_key,scope_type,scope_id,subject,body_json,created_at " \
              "FROM notification_outbox"
        conditions: list[str] = []
        params: list[Any] = []
        if scope_type is not None:
            conditions.append("scope_type=?")
            params.append(scope_type)
        if scope_id is not None:
            conditions.append("scope_id=?")
            params.append(scope_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY event_id LIMIT ?"
        params.append(min(max(limit, 1), 500))
        rows = self.connection.execute(sql, params).fetchall()
        return [dict(row) | {"body": json.loads(row["body_json"])} for row in rows]

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM cc_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
