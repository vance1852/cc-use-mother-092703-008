"""重大项目阶段门控服务的 SQLite 模式和事务辅助。"""

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
    role TEXT NOT NULL CHECK(role IN ('planner','director','risk','finance','auditor','department')),
    dept TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_org TEXT NOT NULL,
    director_id TEXT NOT NULL REFERENCES gate_users(user_id),
    tags_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_gates (
    gate_id TEXT PRIMARY KEY,
    gate_order INTEGER NOT NULL,
    name TEXT NOT NULL,
    decision_role TEXT NOT NULL,
    stage_budget_cny TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'published' CHECK(state IN ('draft','published','retired')),
    supersedes_gate_id TEXT REFERENCES policy_gates(gate_id),
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS gate_conditions (
    condition_uid INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_key TEXT NOT NULL,
    gate_id TEXT NOT NULL REFERENCES policy_gates(gate_id),
    version INTEGER NOT NULL DEFAULT 1,
    condition_type TEXT NOT NULL CHECK(condition_type IN ('evidence','budget')),
    title TEXT NOT NULL,
    responsible_dept TEXT NOT NULL,
    authority TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    threshold_cny TEXT,
    evidence_spec_json TEXT,
    applicability_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(gate_id, condition_key)
);

CREATE INDEX IF NOT EXISTS idx_conditions_gate ON gate_conditions(gate_id, active);

CREATE INDEX IF NOT EXISTS idx_conditions_key ON gate_conditions(condition_key, active);

CREATE TABLE IF NOT EXISTS work_packages (
    package_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    gate_order INTEGER NOT NULL,
    requires_json TEXT NOT NULL DEFAULT '[]',
    responsible_dept TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned'
        CHECK(state IN ('planned','ready','blocked','in_progress','completed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_project ON work_packages(project_id, gate_order);

CREATE TABLE IF NOT EXISTS evidence_documents (
    evidence_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    condition_id TEXT NOT NULL,
    doc_number TEXT NOT NULL,
    doc_kind TEXT NOT NULL,
    source TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    submitted_by_dept TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted' CHECK(state IN ('submitted','accepted','rejected','revoked')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_condition ON evidence_documents(project_id, condition_id, state);

CREATE TABLE IF NOT EXISTS budget_commitments (
    commitment_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    gate_order INTEGER NOT NULL,
    fund_source TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    doc_number TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'committed'
        CHECK(state IN ('committed','disbursed','frozen','withdrawn')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, commitment_id)
);

CREATE INDEX IF NOT EXISTS idx_commitments_gate ON budget_commitments(project_id, gate_order, state);

CREATE TABLE IF NOT EXISTS emergency_overrides (
    override_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    scope TEXT NOT NULL CHECK(scope IN ('condition','package')),
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    boundary_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed','revoked')),
    granted_by TEXT NOT NULL REFERENCES gate_users(user_id),
    granted_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY(project_id, override_id)
);

CREATE INDEX IF NOT EXISTS idx_overrides_target ON emergency_overrides(project_id, scope, target_id, state);

CREATE TABLE IF NOT EXISTS stage_orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    gate_id TEXT NOT NULL REFERENCES policy_gates(gate_id),
    gate_version INTEGER NOT NULL,
    gate_order INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('issued','rejected')),
    snapshot_json TEXT NOT NULL,
    blocker_summary_json TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL,
    issued_by TEXT NOT NULL REFERENCES gate_users(user_id),
    issued_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_project ON stage_orders(project_id, gate_order, order_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gate_order_published
ON policy_gates(gate_order) WHERE state='published';

CREATE UNIQUE INDEX IF NOT EXISTS idx_order_once_per_gate
ON stage_orders(project_id, gate_order);

CREATE TABLE IF NOT EXISTS package_progress (
    progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    package_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    order_id INTEGER REFERENCES stage_orders(order_id),
    note TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_package_progress ON package_progress(project_id, package_id, progress_id);

CREATE TABLE IF NOT EXISTS rule_change_impacts (
    impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    change_kind TEXT NOT NULL CHECK(change_kind IN ('condition_expired','rule_revised','condition_deactivated')),
    condition_ids_json TEXT NOT NULL,
    from_gate_order INTEGER NOT NULL,
    impacted_packages_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    recognized_at TEXT,
    created_by TEXT NOT NULL REFERENCES gate_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_impacts_project ON rule_change_impacts(project_id, impact_id);

CREATE TABLE IF NOT EXISTS gate_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    project_id TEXT,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_audit_entity ON gate_audit_events(project_id, entity_type, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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
