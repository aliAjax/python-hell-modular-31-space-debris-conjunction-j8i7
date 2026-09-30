import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS satellites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    satellite_id TEXT NOT NULL UNIQUE,
                    organization TEXT NOT NULL,
                    fuel_quota_m_s REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL,
                    satellite_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    level TEXT NOT NULL,
                    tca TEXT NOT NULL,
                    fuel_cost_m_s REAL NOT NULL,
                    basis TEXT NOT NULL,
                    unscheduled_reason TEXT,
                    invalidation_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id),
                    FOREIGN KEY(satellite_id) REFERENCES satellites(satellite_id)
                );
                CREATE TABLE IF NOT EXISTS plan_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    step_type TEXT NOT NULL,
                    command_ref TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    receipt TEXT,
                    dispatched_at TEXT,
                    UNIQUE(plan_id, seq),
                    FOREIGN KEY(plan_id) REFERENCES plans(id)
                );
                CREATE TABLE IF NOT EXISTS fuel_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    satellite_id TEXT NOT NULL,
                    plan_id INTEGER,
                    entry_type TEXT NOT NULL,
                    amount_m_s REAL NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plans_satellite ON plans(satellite_id, state);
                CREATE INDEX IF NOT EXISTS idx_steps_plan ON plan_steps(plan_id, seq);
                CREATE INDEX IF NOT EXISTS idx_ledger_satellite ON fuel_ledger(satellite_id, id);
                """
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------ helpers

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def _log_action(self, conn, item_id, action, actor, role, payload):
        conn.execute(
            "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, action, actor, role, canonical_json(payload), now_iso()),
        )

    # ------------------------------------------------------------------ items

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def lock_item(self, conn, item_id):
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("item_not_found", "业务实体不存在")
        return self._row_to_item(row)

    def update_item(self, conn, item, status=None, payload=None):
        new_status = item["status"] if status is None else status
        new_payload = item["payload"] if payload is None else payload
        version = int(item["version"]) + 1
        conn.execute(
            "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
            (new_status, version, canonical_json(new_payload), now_iso(), item["id"]),
        )
        item["status"] = new_status
        item["payload"] = new_payload
        item["version"] = version

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = self.lock_item(conn, item_id)
            if expected_version is not None and int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            self.update_item(conn, item, new_status, new_payload)
            self._log_action(conn, item_id, action, actor, role, event_payload)
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    # ------------------------------------------------------------- satellites

    def register_satellite(self, satellite_id, organization, quota, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = self.lock_satellite(conn, satellite_id, optional=True)
            if existing is not None:
                raise ConflictError("satellite_exists", "卫星燃料账户已经存在")
            conn.execute(
                "INSERT INTO satellites(satellite_id,organization,fuel_quota_m_s,version,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (satellite_id, organization, float(quota), 1, now_iso(), now_iso()),
            )
            self._ledger(conn, satellite_id, None, "quota_set", float(quota), {"actor": actor})
            self.append_audit(conn, None, "satellite_registered", actor, role,
                              {"satellite_id": satellite_id, "organization": organization, "fuel_quota_m_s": float(quota)})
            conn.execute("COMMIT")
            return self.get_satellite(satellite_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def lock_satellite(self, conn, satellite_id, optional=False):
        row = conn.execute("SELECT * FROM satellites WHERE satellite_id=? ", (satellite_id,)).fetchone()
        if row is None:
            if optional:
                return None
            raise NotFoundError("satellite_not_found", "卫星燃料账户不存在，请先登记卫星")
        return dict(row)

    def bump_satellite(self, conn, sat):
        version = int(sat["version"]) + 1
        conn.execute(
            "UPDATE satellites SET version=?,updated_at=? WHERE satellite_id=?",
            (version, now_iso(), sat["satellite_id"]),
        )
        sat["version"] = version

    def get_satellite(self, satellite_id):
        conn = self.connect()
        try:
            sat = self.lock_satellite(conn, satellite_id)
            return self.satellite_view(conn, sat)
        finally:
            conn.close()

    def list_satellites(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM satellites ORDER BY id").fetchall()
            return [self.satellite_view(conn, dict(row)) for row in rows]
        finally:
            conn.close()

    def satellite_view(self, conn, sat):
        sid = sat["satellite_id"]
        consumed = 0.0
        reserved = 0.0
        for row in conn.execute(
            "SELECT state, fuel_cost_m_s FROM plans WHERE satellite_id=?", (sid,)
        ).fetchall():
            if row["state"] == "completed":
                consumed += float(row["fuel_cost_m_s"])
            elif row["state"] in ("scheduled", "dispatching"):
                reserved += float(row["fuel_cost_m_s"])
        view = dict(sat)
        view["fuel_consumed_m_s"] = round(consumed, 9)
        view["fuel_reserved_m_s"] = round(reserved, 9)
        view["fuel_remaining_m_s"] = round(float(sat["fuel_quota_m_s"]) - consumed - reserved, 9)
        return view

    def _ledger(self, conn, satellite_id, plan_id, entry_type, amount, payload):
        conn.execute(
            "INSERT INTO fuel_ledger(satellite_id,plan_id,entry_type,amount_m_s,payload,created_at) VALUES(?,?,?,?,?,?)",
            (satellite_id, plan_id, entry_type, float(amount), canonical_json(payload), now_iso()),
        )

    def fuel_ledger(self, satellite_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM fuel_ledger WHERE satellite_id=? ORDER BY id", (satellite_id,)
            ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    # ----------------------------------------------------------------- plans

    def list_plans_for_satellite(self, conn, satellite_id):
        rows = conn.execute(
            "SELECT * FROM plans WHERE satellite_id=? ORDER BY id", (satellite_id,)
        ).fetchall()
        result = []
        for row in rows:
            plan = dict(row)
            plan["basis"] = json.loads(plan["basis"])
            result.append(plan)
        return result

    def insert_plan(self, conn, plan_no, item_id, satellite_id, state, level, tca, cost, basis, reason, actor, role):
        conn.execute(
            "INSERT INTO plans(plan_no,item_id,satellite_id,state,level,tca,fuel_cost_m_s,basis,unscheduled_reason,created_by,created_role,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_no, item_id, satellite_id, state, level, tca, float(cost), canonical_json(basis),
             reason, actor, role, now_iso(), now_iso()),
        )
        plan_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return plan_id

    def insert_plan_steps(self, conn, plan_id, steps):
        for seq, step in enumerate(steps):
            conn.execute(
                "INSERT INTO plan_steps(plan_id,seq,step_type,command_ref,payload,status) VALUES(?,?,?,?,?,?)",
                (plan_id, seq, step["step_type"], step["command_ref"], canonical_json(step["payload"]), "pending"),
            )

    def set_plan_state(self, conn, plan_id, state, unscheduled_reason=None, invalidation_reason=None):
        conn.execute(
            "UPDATE plans SET state=?,unscheduled_reason=?,invalidation_reason=?,updated_at=? WHERE id=?",
            (state, unscheduled_reason, invalidation_reason, now_iso(), plan_id),
        )

    def get_plan_by_no(self, plan_no, lock=False):
        conn = self.connect()
        try:
            if lock:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM plans WHERE plan_no=?", (plan_no,)).fetchone()
            if row is None:
                if lock:
                    conn.execute("ROLLBACK")
                raise NotFoundError("plan_not_found", "规避方案不存在")
            plan = dict(row)
            plan["basis"] = json.loads(plan["basis"])
            return conn, plan
        except Exception:
            if lock:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()
            raise

    def get_steps(self, conn, plan_id):
        rows = conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY seq", (plan_id,)
        ).fetchall()
        result = []
        for row in rows:
            step = dict(row)
            step["payload"] = json.loads(step["payload"])
            if step["receipt"] is not None:
                step["receipt"] = json.loads(step["receipt"])
            result.append(step)
        return result

    def plan_view(self, conn, plan):
        view = dict(plan)
        view["steps"] = self.get_steps(conn, plan["id"])
        return view

    def list_plans(self, item_id=None, satellite_id=None):
        conn = self.connect()
        try:
            sql = "SELECT * FROM plans"
            clauses, params = [], []
            if item_id is not None:
                clauses.append("item_id=?")
                params.append(item_id)
            if satellite_id is not None:
                clauses.append("satellite_id=?")
                params.append(satellite_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id"
            rows = conn.execute(sql, tuple(params)).fetchall()
            result = []
            for row in rows:
                plan = dict(row)
                plan["basis"] = json.loads(plan["basis"])
                plan["steps"] = self.get_steps(conn, plan["id"])
                result.append(plan)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            plan_counts = {}
            for row in conn.execute("SELECT state, COUNT(*) AS total FROM plans GROUP BY state").fetchall():
                plan_counts[row["state"]] = row["total"]
            return {
                "counts": counts,
                "plan_counts": plan_counts,
                "items": self.list_items(),
                "satellites": [self.satellite_view(conn, dict(row))
                               for row in conn.execute("SELECT * FROM satellites ORDER BY id").fetchall()],
            }
        finally:
            conn.close()
