"""条件化承诺管理的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path


# 进程内事务锁：ThreadingHTTPServer 复用同一连接时，串行化 BEGIN/COMMIT，
# 避免跨线程交错事务；跨进程并发由 WAL 与 busy_timeout 保证。事务都很短。
_TRANSACTION_LOCK = threading.RLock()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cc_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('office','evaluator','dispatcher','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS programs (
    program_id TEXT PRIMARY KEY,
    program_name TEXT NOT NULL,
    lead_office TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL REFERENCES programs(program_id),
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','reached')),
    created_by TEXT NOT NULL REFERENCES cc_users(user_id),
    created_at TEXT NOT NULL,
    reached_at TEXT
);

-- 可竞争的资源池：资金(CNY)、专家时段(HOUR)、平台额度(UNIT) 等同质资源共享一个池。
CREATE TABLE IF NOT EXISTS resource_pools (
    pool_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('fund','expert_time','platform_quota')),
    unit TEXT NOT NULL CHECK(unit IN ('CNY','HOUR','UNIT')),
    total_quota TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES cc_users(user_id),
    created_at TEXT NOT NULL
);

-- 池余额是竞争释放唯一胜者的权威依据：直接占用与候补要约分开记账。
CREATE TABLE IF NOT EXISTS pool_balances (
    pool_id TEXT PRIMARY KEY REFERENCES resource_pools(pool_id),
    total_quota TEXT NOT NULL,
    occupied_quota TEXT NOT NULL DEFAULT '0',
    earmarked_quota TEXT NOT NULL DEFAULT '0',
    CHECK(CAST(occupied_quota AS REAL) >= 0),
    CHECK(CAST(earmarked_quota AS REAL) >= 0),
    CHECK(CAST(occupied_quota AS REAL) + CAST(earmarked_quota AS REAL) <= CAST(total_quota AS REAL))
);

-- 承诺本身按版本冻结，旧版本永不改写，只迁移状态。
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    program_id TEXT NOT NULL REFERENCES programs(program_id),
    title TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    state TEXT NOT NULL CHECK(state IN ('active','superseded','locked','closed')),
    created_by TEXT NOT NULL REFERENCES cc_users(user_id),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    PRIMARY KEY(commitment_id, revision)
);

CREATE TABLE IF NOT EXISTS commitment_conditions (
    condition_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    local_condition_id TEXT NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','satisfied','failed')),
    satisfied_at TEXT,
    failed_at TEXT,
    FOREIGN KEY(commitment_id, revision) REFERENCES commitments(commitment_id, revision),
    UNIQUE(commitment_id, revision, local_condition_id)
);

-- 条件相互依赖：被依赖条件未满足时本条件不能满足；被依赖条件失败则级联失败。
CREATE TABLE IF NOT EXISTS condition_dependencies (
    condition_id TEXT NOT NULL REFERENCES commitment_conditions(condition_id),
    depends_on_condition_id TEXT NOT NULL REFERENCES commitment_conditions(condition_id),
    PRIMARY KEY(condition_id, depends_on_condition_id),
    CHECK(condition_id <> depends_on_condition_id)
);

-- 每项资源分片：提供方、受益方、前置条件、释放顺序、有效期、退出责任冻结在所属版本里。
CREATE TABLE IF NOT EXISTS commitment_resources (
    resource_version_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    provider_id TEXT NOT NULL,
    beneficiary_id TEXT NOT NULL,
    committed_amount TEXT NOT NULL CHECK(CAST(committed_amount AS REAL) > 0),
    activated_amount TEXT NOT NULL DEFAULT '0',
    delivered_amount TEXT NOT NULL DEFAULT '0',
    recovered_amount TEXT NOT NULL DEFAULT '0',
    gate_condition_id TEXT REFERENCES commitment_conditions(condition_id),
    release_order INTEGER NOT NULL CHECK(release_order >= 0),
    valid_until TEXT NOT NULL,
    exit_policy TEXT NOT NULL CHECK(exit_policy IN ('return_provider','reallocate_pool')),
    standby_priority INTEGER CHECK(standby_priority IS NULL OR (standby_priority >= 1 AND standby_priority <= 999)),
    state TEXT NOT NULL CHECK(state IN (
        'waiting','active','fulfilled','recovered','reallocated','failed','superseded'
    )),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(commitment_id, revision) REFERENCES commitments(commitment_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_cr_commitment
ON commitment_resources(commitment_id, revision, release_order);

CREATE UNIQUE INDEX IF NOT EXISTS unique_commitment_content
ON commitments(commitment_id, content_sha256);

CREATE INDEX IF NOT EXISTS idx_cr_pool_state
ON commitment_resources(pool_id, state, standby_priority, created_at);

-- 前置证据登记与核验结论；证据引用外部证据评估能力的版本标识与摘要。
CREATE TABLE IF NOT EXISTS evidence_claims (
    claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL REFERENCES commitment_conditions(condition_id),
    evidence_ref TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','satisfied','failed')),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT REFERENCES cc_users(user_id),
    decided_at TEXT,
    created_by TEXT NOT NULL REFERENCES cc_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(condition_id, evidence_ref)
);

-- 已核验通过的交付只追加、不可改删（后续规则不能重排）。
CREATE TABLE IF NOT EXISTS resource_deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_version_id TEXT NOT NULL REFERENCES commitment_resources(resource_version_id),
    amount TEXT NOT NULL CHECK(CAST(amount AS REAL) > 0),
    evidence_ref TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    idempotency_key TEXT NOT NULL UNIQUE,
    verified_by TEXT NOT NULL REFERENCES cc_users(user_id),
    verified_at TEXT NOT NULL
);

-- 资源全流程台账：占用、生效、交付、失败、回收、候补转配全部在此留痕。
CREATE TABLE IF NOT EXISTS resource_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_version_id TEXT NOT NULL REFERENCES commitment_resources(resource_version_id),
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'occupied','activated','delivered','condition_failed','superseded',
        'recovered','reallocated_out','reallocated_in'
    )),
    amount TEXT NOT NULL DEFAULT '0',
    condition_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    linked_event_id INTEGER REFERENCES resource_events(event_id),
    actor_id TEXT NOT NULL REFERENCES cc_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_re_trace
ON resource_events(resource_version_id, event_id);

-- 同一分片的占用、生效、失败、退出事件各自只能入账一次。
CREATE UNIQUE INDEX IF NOT EXISTS unique_occupied_event
ON resource_events(resource_version_id) WHERE event_type='occupied';

CREATE UNIQUE INDEX IF NOT EXISTS unique_activated_event
ON resource_events(resource_version_id) WHERE event_type='activated';

CREATE UNIQUE INDEX IF NOT EXISTS unique_failed_event
ON resource_events(resource_version_id) WHERE event_type='condition_failed';

CREATE UNIQUE INDEX IF NOT EXISTS unique_recovered_event
ON resource_events(resource_version_id) WHERE event_type='recovered';

-- 失败或逾期释放（退出责任为 reallocate_pool）形成的可转配要约，是候补资源的唯一来源。
CREATE TABLE IF NOT EXISTS release_offers (
    offer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    source_resource_version_id TEXT NOT NULL REFERENCES commitment_resources(resource_version_id),
    source_event_id INTEGER NOT NULL REFERENCES resource_events(event_id),
    amount TEXT NOT NULL,
    remaining_amount TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','consumed','returned')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_offers_pool
ON release_offers(pool_id, state, offer_id);

-- 候补转配记录：每个胜者分片唯一；一个胜者可消费多个释放要约。
CREATE TABLE IF NOT EXISTS standby_promotions (
    promotion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    winner_resource_version_id TEXT NOT NULL UNIQUE REFERENCES commitment_resources(resource_version_id),
    source_resource_version_id TEXT REFERENCES commitment_resources(resource_version_id),
    source_event_id INTEGER REFERENCES resource_events(event_id),
    amount TEXT NOT NULL,
    promoted_by TEXT NOT NULL REFERENCES cc_users(user_id),
    promoted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standby_offer_links (
    promotion_id INTEGER NOT NULL REFERENCES standby_promotions(promotion_id),
    offer_id INTEGER NOT NULL REFERENCES release_offers(offer_id),
    amount TEXT NOT NULL,
    PRIMARY KEY(promotion_id, offer_id)
);

CREATE TABLE IF NOT EXISTS milestone_links (
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    resource_version_id TEXT NOT NULL REFERENCES commitment_resources(resource_version_id),
    -- active：资源生效放行即解除阻断；fulfilled：必须交付核验通过。
    required_state TEXT NOT NULL CHECK(required_state IN ('active','fulfilled')),
    PRIMARY KEY(milestone_id, resource_version_id)
);

-- 通知箱：相同通知以 notification_key 幂等，重试只保留一条。
CREATE TABLE IF NOT EXISTS notification_outbox (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_key TEXT NOT NULL UNIQUE,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_scope
ON notification_outbox(scope_type, scope_id, event_id);

CREATE TABLE IF NOT EXISTS cc_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS cc_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cc_audit_entity
ON cc_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用同一连接；用连接级锁串行化事务，
    # 配合 busy_timeout 与 WAL，跨线程/多进程写入都是安全的。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    with _TRANSACTION_LOCK:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
