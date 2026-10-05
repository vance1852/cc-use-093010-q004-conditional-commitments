"""条件化承诺的输入契约：校验并规范化承诺版本内容。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .jsonio import quantize


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
RESOURCE_TYPES = {
    "fund": "CNY",
    "expert_time": "HOUR",
    "platform_quota": "UNIT",
}
EXIT_POLICIES = {"return_provider", "reallocate_pool"}
CONDITION_OUTCOMES = {"satisfied", "failed"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def sha256_text(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256")
    return result.lower()


def amount_value(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite() or result <= 0:
        raise ValidationFailed(f"{field} 必须是正数")
    return quantize(result)


def timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    try:
        return parse_utc(value, field).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class ConditionSpec:
    condition_id: str
    label: str
    depends_on: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    resource_version_id: str
    pool_id: str
    provider_id: str
    beneficiary_id: str
    amount: Decimal
    gate_condition_id: str | None
    release_order: int
    valid_until: str
    exit_policy: str
    standby_priority: int | None
    milestones: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CommitmentSpec:
    commitment_id: str
    program_id: str
    title: str
    conditions: tuple[ConditionSpec, ...]
    resources: tuple[ResourceSpec, ...]

    def normalized(self) -> dict[str, Any]:
        """版本冻结用的规范结构：只含业务字段，顺序固定。"""
        return {
            "commitment_id": self.commitment_id,
            "program_id": self.program_id,
            "title": self.title,
            "conditions": [
                {
                    "condition_id": item.condition_id,
                    "label": item.label,
                    "depends_on": list(item.depends_on),
                    "evidence": [dict(entry) for entry in item.evidence],
                }
                for item in self.conditions
            ],
            "resources": [
                {
                    "resource_version_id": item.resource_version_id,
                    "pool_id": item.pool_id,
                    "provider_id": item.provider_id,
                    "beneficiary_id": item.beneficiary_id,
                    "amount": format(item.amount, "f"),
                    "gate_condition_id": item.gate_condition_id,
                    "release_order": item.release_order,
                    "valid_until": item.valid_until,
                    "exit_policy": item.exit_policy,
                    "standby_priority": item.standby_priority,
                    "milestones": [dict(entry) for entry in item.milestones],
                }
                for item in self.resources
            ],
        }


def _parse_condition(raw: Mapping[str, Any]) -> ConditionSpec:
    condition_id = identifier(raw.get("condition_id"), "condition_id")
    label = required_text(raw.get("label"), "label")
    depends_raw = raw.get("depends_on", [])
    if not isinstance(depends_raw, list):
        raise ValidationFailed("depends_on 必须是数组")
    depends_on = tuple(identifier(item, "depends_on[]") for item in depends_raw)
    evidence_raw = raw.get("evidence", [])
    if not isinstance(evidence_raw, list):
        raise ValidationFailed("evidence 必须是数组")
    evidence: list[dict[str, Any]] = []
    for entry in evidence_raw:
        if not isinstance(entry, Mapping):
            raise ValidationFailed("evidence 条目必须是对象")
        evidence.append({
            "evidence_ref": identifier(entry.get("evidence_ref"), "evidence_ref"),
            "content_sha256": sha256_text(entry.get("content_sha256"), "content_sha256"),
        })
    return ConditionSpec(condition_id, label, depends_on, tuple(evidence))


def _parse_resource(raw: Mapping[str, Any]) -> ResourceSpec:
    gate = raw.get("gate_condition_id")
    gate_id = None if gate is None else identifier(gate, "gate_condition_id")
    order = raw.get("release_order", 0)
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise ValidationFailed("release_order 必须是非负整数")
    priority = raw.get("standby_priority")
    if priority is not None:
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("standby_priority 必须是 1 到 999 的整数")
    policy = required_text(raw.get("exit_policy"), "exit_policy", 32)
    if policy not in EXIT_POLICIES:
        raise ValidationFailed("exit_policy 必须是 return_provider 或 reallocate_pool")
    milestones_raw = raw.get("milestones", [])
    if not isinstance(milestones_raw, list):
        raise ValidationFailed("milestones 必须是数组")
    milestones: list[dict[str, Any]] = []
    for entry in milestones_raw:
        if not isinstance(entry, Mapping):
            raise ValidationFailed("milestones 条目必须是对象")
        required_state = entry.get("required_state", "fulfilled")
        if required_state not in {"active", "fulfilled"}:
            raise ValidationFailed("milestones.required_state 必须是 active 或 fulfilled")
        milestones.append({
            "milestone_id": identifier(entry.get("milestone_id"), "milestone_id"),
            "required_state": required_state,
        })
    return ResourceSpec(
        resource_version_id=identifier(raw.get("resource_version_id"), "resource_version_id"),
        pool_id=identifier(raw.get("pool_id"), "pool_id"),
        provider_id=identifier(raw.get("provider_id"), "provider_id"),
        beneficiary_id=identifier(raw.get("beneficiary_id"), "beneficiary_id"),
        amount=amount_value(raw.get("amount"), "amount"),
        gate_condition_id=gate_id,
        release_order=order,
        valid_until=timestamp(raw.get("valid_until"), "valid_until"),
        exit_policy=policy,
        standby_priority=priority,
        milestones=tuple(milestones),
    )


def parse_commitment(raw: Mapping[str, Any]) -> CommitmentSpec:
    commitment_id = identifier(raw.get("commitment_id"), "commitment_id")
    program_id = identifier(raw.get("program_id"), "program_id")
    title = required_text(raw.get("title"), "title")
    for field in ("conditions", "resources"):
        if not isinstance(raw.get(field), list) or not raw[field]:
            raise ValidationFailed(f"{field} 必须是非空数组")
    conditions = [_parse_condition(item) for item in raw["conditions"]]
    resources = [_parse_resource(item) for item in raw["resources"]]

    condition_ids = [item.condition_id for item in conditions]
    if len(set(condition_ids)) != len(condition_ids):
        raise ValidationFailed("condition_id 不能重复")
    condition_set = set(condition_ids)
    for item in conditions:
        if item.condition_id in item.depends_on:
            raise ValidationFailed("条件不能依赖自身")
        unknown = [dep for dep in item.depends_on if dep not in condition_set]
        if unknown:
            raise ValidationFailed(f"依赖的条件不存在: {unknown[0]}")
    # 依赖图必须无环，且可以按稳定顺序求值。
    graph = {item.condition_id: set(item.depends_on) for item in conditions}
    resolved: set[str] = set()
    while len(resolved) < len(graph):
        progress = False
        for node, deps in graph.items():
            if node not in resolved and deps <= resolved:
                resolved.add(node)
                progress = True
        if not progress:
            raise ValidationFailed("条件依赖存在循环")

    resource_ids = [item.resource_version_id for item in resources]
    if len(set(resource_ids)) != len(resource_ids):
        raise ValidationFailed("resource_version_id 不能重复")
    for item in resources:
        if item.gate_condition_id is not None and item.gate_condition_id not in condition_set:
            raise ValidationFailed(f"资源 {item.resource_version_id} 引用了不存在的前置条件")

    return CommitmentSpec(
        commitment_id=commitment_id,
        program_id=program_id,
        title=title,
        conditions=tuple(conditions),
        resources=tuple(resources),
    )
