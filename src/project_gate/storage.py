"""阶段门控服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS gate_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('coordinator','department','approver','auditor')),
    dept TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 门控策略（政策规则）按版本保存，已签发阶段令永久引用当时版本。
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    name TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(policy_id, version)
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_org TEXT NOT NULL,
    manager_id TEXT NOT NULL REFERENCES gate_users(user_id),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    current_stage TEXT NOT NULL,
    budget_cny TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(policy_id, policy_version) REFERENCES policies(policy_id, version)
);

-- 阶段预算：项目办为每个阶段核定的预算上限。
CREATE TABLE IF NOT EXISTS stage_budgets (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    stage TEXT NOT NULL,
    allocated_cny TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL REFERENCES gate_users(user_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, stage)
);

-- 资金承诺：财政/业主等主体对某阶段的资金承诺，可撤回。
CREATE TABLE IF NOT EXISTS fund_commitments (
    commitment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    stage TEXT NOT NULL,
    source TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'committed' CHECK(state IN ('committed','withdrawn')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, stage, document_ref)
);

CREATE INDEX IF NOT EXISTS idx_commitments_stage
ON fund_commitments(project_id, stage, state);

-- 工作包：可追溯计划的最小执行单元，归属某一阶段并可依赖其他工作包。
CREATE TABLE IF NOT EXISTS work_packages (
    package_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    stage TEXT NOT NULL,
    title TEXT NOT NULL,
    owner_dept TEXT NOT NULL,
    budget_cny TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','ready','executing','done','suspended')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_project_stage
ON work_packages(project_id, stage, state);

-- 有效证据：责任部门为满足门槛提交的许可、批复、承诺函、到货证明等。
CREATE TABLE IF NOT EXISTS evidences (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    gate_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    document_ref TEXT NOT NULL UNIQUE,
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','invalidated')),
    invalidate_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    submitted_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidences_gate
ON evidences(project_id, gate_code, state);

-- 门槛实例：项目按政策版本实例化的每条阶段门槛。
CREATE TABLE IF NOT EXISTS project_gates (
    gate_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    stage TEXT NOT NULL,
    gate_code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    owner_dept TEXT NOT NULL,
    applies TEXT NOT NULL CHECK(applies IN ('REQUIRED','EXEMPTIBLE')),
    evidence_kind TEXT NOT NULL,
    evidence_requirement TEXT NOT NULL,
    valid_days INTEGER,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','satisfied','waived','invalid','expired')),
    evidence_id TEXT REFERENCES evidences(evidence_id),
    satisfied_at TEXT,
    expires_at TEXT,
    waive_note TEXT,
    satisfied_by TEXT REFERENCES gate_users(user_id),
    UNIQUE(project_id, policy_version, stage, gate_code)
);

CREATE INDEX IF NOT EXISTS idx_project_gates_stage
ON project_gates(project_id, policy_version, stage);

-- 紧急例外：明确范围、理由和到期时间，只在有效期内覆盖特定门槛。
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    stage TEXT NOT NULL,
    scope_gates_json TEXT NOT NULL,
    scope_packages_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked','expired','closed')),
    closure_note TEXT,
    issued_by TEXT NOT NULL REFERENCES gate_users(user_id),
    issued_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_exceptions_project
ON exceptions(project_id, stage, state);

-- 阶段令：不可变的历史批准，快照签发时的门槛、证据、资金、预算与例外状态。
CREATE TABLE IF NOT EXISTS stage_orders (
    order_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    policy_sha256 TEXT NOT NULL,
    decision_role TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES gate_users(user_id),
    decided_at TEXT NOT NULL,
    gate_snapshot_json TEXT NOT NULL,
    fund_snapshot_json TEXT NOT NULL,
    exception_ids_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_orders_project
ON stage_orders(project_id, decided_at);

-- 影响记录：条件失效、政策换版、例外到期对未执行工作包的影响与恢复条件。
CREATE TABLE IF NOT EXISTS gate_impacts (
    impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    impact_type TEXT NOT NULL CHECK(impact_type IN ('EVIDENCE_INVALID','POLICY_VERSION','EXCEPTION_EXPIRED')),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    policy_version INTEGER,
    stage TEXT NOT NULL,
    gate_code TEXT,
    package_id TEXT REFERENCES work_packages(package_id),
    detail_json TEXT NOT NULL,
    recovery_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_impacts_open
ON gate_impacts(project_id, status, impact_id);

CREATE TABLE IF NOT EXISTS gate_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS gate_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_gate_audit_entity
ON gate_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
