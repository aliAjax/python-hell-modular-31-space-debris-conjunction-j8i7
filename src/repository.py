import json
import sqlite3
from datetime import datetime, timezone

from . import rules
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
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
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
                CREATE TABLE IF NOT EXISTS fuel_accounts (
                    satellite_id TEXT PRIMARY KEY,
                    fuel_quota_m_s REAL NOT NULL,
                    fuel_consumed_m_s REAL NOT NULL DEFAULT 0,
                    fuel_occupied_m_s REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL,
                    satellite_id TEXT NOT NULL,
                    fuel_cost_m_s REAL NOT NULL,
                    maneuver_window TEXT NOT NULL,
                    level TEXT NOT NULL,
                    tca TEXT NOT NULL,
                    status TEXT NOT NULL,
                    unscheduled_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    total_steps INTEGER NOT NULL,
                    steps TEXT NOT NULL,
                    fail_step INTEGER,
                    command_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

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
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
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

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items(), "fuel_accounts": self.list_fuel_accounts()}
        finally:
            conn.close()

    # ---------------- 调度账：燃料账户 / 方案 / 指令下发 ----------------

    def ensure_fuel_account(self, satellite_id, quota):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO fuel_accounts(satellite_id,fuel_quota_m_s,fuel_consumed_m_s,fuel_occupied_m_s,version,updated_at) "
                "VALUES(?,?,0,0,1,?)",
                (satellite_id, quota, now_iso()),
            )
            conn.execute("COMMIT")
            return self.get_fuel_account(satellite_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_fuel_account(self, satellite_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM fuel_accounts WHERE satellite_id=?", (satellite_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_fuel_accounts(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM fuel_accounts ORDER BY satellite_id").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def list_plans(self, satellite_id):
        # 方案按等级(high>medium>low)、碰撞时刻(早在前)排队
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM plans WHERE satellite_id=? "
                "ORDER BY CASE level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, tca ASC, id ASC",
                (satellite_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_plan(self, plan_no):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM plans WHERE plan_no=?", (plan_no,)).fetchone()
            if row is None:
                raise NotFoundError("plan_not_found", "方案不存在")
            plan = dict(row)
            disp = conn.execute("SELECT * FROM dispatches WHERE plan_no=?", (plan_no,)).fetchone()
            if disp is not None:
                disp = dict(disp)
                disp["steps"] = json.loads(disp["steps"])
            plan["dispatch"] = disp
            return plan
        finally:
            conn.close()

    def book_plan(self, item_id, fuel_cost, window, expected_version, expected_fuel_version, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            if item["status"] != "assessed":
                raise DomainError("invalid_state", "当前状态 %s 不允许批准方案" % item["status"])
            payload = item["payload"]
            if payload.get("conflict"):
                raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
            event_budget = float(payload.get("fuel_budget_m_s", 0))
            satellite_id = payload.get("primary_object_id")
            fuel = conn.execute("SELECT * FROM fuel_accounts WHERE satellite_id=?", (satellite_id,)).fetchone()
            if fuel is None:
                conn.execute(
                    "INSERT INTO fuel_accounts(satellite_id,fuel_quota_m_s,fuel_consumed_m_s,fuel_occupied_m_s,version,updated_at) "
                    "VALUES(?,?,0,0,1,?)",
                    (satellite_id, event_budget, now_iso()),
                )
                fuel = conn.execute("SELECT * FROM fuel_accounts WHERE satellite_id=?", (satellite_id,)).fetchone()
            if expected_fuel_version is not None and int(expected_fuel_version) != int(fuel["version"]):
                raise ConflictError("version_conflict", "燃料账户已被其他操作更新，请重新读取")
            available = float(fuel["fuel_quota_m_s"]) - float(fuel["fuel_consumed_m_s"]) - float(fuel["fuel_occupied_m_s"])
            reason = rules.unscheduled_reason(fuel_cost, event_budget, available)
            level = rules.plan_level(item)
            tca = rules.plan_tca(item)
            cur = conn.execute(
                "INSERT INTO plans(plan_no,item_id,satellite_id,fuel_cost_m_s,maneuver_window,level,tca,status,unscheduled_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "PENDING",
                    item_id,
                    satellite_id,
                    fuel_cost,
                    window,
                    level,
                    tca,
                    rules.PLAN_QUEUED if reason is None else rules.PLAN_UNSCHEDULED,
                    reason,
                    now_iso(),
                    now_iso(),
                ),
            )
            plan_id = cur.lastrowid
            plan_no = "PLAN-%06d" % plan_id
            conn.execute("UPDATE plans SET plan_no=? WHERE id=?", (plan_no, plan_id))
            if reason is None:
                conn.execute(
                    "UPDATE fuel_accounts SET fuel_occupied_m_s=fuel_occupied_m_s+?, version=version+1, updated_at=? WHERE satellite_id=?",
                    (fuel_cost, now_iso(), satellite_id),
                )
            new_payload = dict(payload)
            new_payload["approved_maneuver"] = {"fuel_cost_m_s": fuel_cost, "maneuver_window": window}
            new_payload["plan"] = {
                "plan_no": plan_no,
                "status": rules.PLAN_QUEUED if reason is None else rules.PLAN_UNSCHEDULED,
                "unscheduled_reason": reason,
                "level": level,
                "tca": tca,
                "fuel_cost_m_s": fuel_cost,
                "maneuver_window": window,
            }
            new_status = "coordinating" if reason is None else "assessed"
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "approve", actor, role, canonical_json({"plan_no": plan_no, "unscheduled_reason": reason}), now_iso()),
            )
            self.append_audit(
                conn,
                item_id,
                "approve",
                actor,
                role,
                {"plan_no": plan_no, "unscheduled_reason": reason, "fuel_cost_m_s": fuel_cost},
            )
            conn.execute("COMMIT")
            return self.get_item(item_id), reason
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def execute_dispatch(self, item_id, command_ref, fail_step, actor, role, expected_version, expected_fuel_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            if item["status"] != "coordinating":
                raise DomainError("invalid_state", "当前状态 %s 不允许下发指令" % item["status"])
            plan = conn.execute("SELECT * FROM plans WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
            if plan is None or plan["status"] != rules.PLAN_QUEUED:
                raise DomainError("no_queued_plan", "没有处于排队占用状态的规避方案")
            plan = dict(plan)
            fuel = conn.execute("SELECT * FROM fuel_accounts WHERE satellite_id=?", (plan["satellite_id"],)).fetchone()
            if fuel is None:
                raise DomainError("fuel_account_missing", "燃料账户不存在")
            if expected_fuel_version is not None and int(expected_fuel_version) != int(fuel["version"]):
                raise ConflictError("version_conflict", "燃料账户已被其他操作更新，请重新读取")
            steps = [{"name": name, "status": "pending"} for name in rules.DISPATCH_STEPS]
            conn.execute(
                "INSERT INTO dispatches(plan_no,item_id,status,current_step,total_steps,steps,fail_step,command_ref,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    plan["plan_no"],
                    item_id,
                    rules.DISPATCH_IN_PROGRESS,
                    0,
                    len(steps),
                    canonical_json(steps),
                    fail_step,
                    command_ref,
                    now_iso(),
                    now_iso(),
                ),
            )
            failed_step = self._run_dispatch_steps(
                conn, item_id, plan, steps, 0, fail_step, command_ref, actor, role
            )
            conn.execute("COMMIT")
            if failed_step is not None:
                raise DomainError("dispatch_failed", "指令下发在第 %d 步(%s)失败，可按方案号重试" % (failed_step[0], failed_step[1]), 500)
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def retry_dispatch(self, plan_no, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            disp = conn.execute("SELECT * FROM dispatches WHERE plan_no=?", (plan_no,)).fetchone()
            if disp is None:
                raise NotFoundError("dispatch_not_found", "方案 %s 没有下发记录" % plan_no)
            disp = dict(disp)
            if disp["status"] == rules.DISPATCH_COMPLETED:
                conn.execute("COMMIT")
                return self.get_item(disp["item_id"])
            plan = conn.execute("SELECT * FROM plans WHERE plan_no=?", (plan_no,)).fetchone()
            if plan is None:
                raise NotFoundError("plan_not_found", "方案不存在")
            plan = dict(plan)
            if plan["status"] == rules.PLAN_INVALIDATED:
                raise DomainError("plan_invalidated", "方案已因轨道修订或额度变化失效，不能重试", 409)
            item_id = disp["item_id"]
            steps = json.loads(disp["steps"])
            start = int(disp["current_step"])
            # 重试时不再重放首次的瞬时失败，从未完成步骤继续
            failed_step = self._run_dispatch_steps(
                conn, item_id, plan, steps, start, None, disp["command_ref"], actor, role
            )
            conn.execute("COMMIT")
            if failed_step is not None:
                raise DomainError("dispatch_failed", "指令下发在第 %d 步(%s)失败，可按方案号重试" % (failed_step[0], failed_step[1]), 500)
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _run_dispatch_steps(self, conn, item_id, plan, steps, start_step, fail_step, command_ref, actor, role):
        # 返回 (step_index, step_name) 表示失败步骤；None 表示全部完成
        for i in range(start_step, len(steps)):
            if fail_step is not None and i == int(fail_step):
                conn.execute(
                    "UPDATE dispatches SET status=?, current_step=?, updated_at=? WHERE id=?",
                    (rules.DISPATCH_FAILED, i, now_iso(), self._dispatch_id(conn, plan["plan_no"])),
                )
                conn.execute(
                    "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, "dispatch_failed", actor, role, canonical_json({"plan_no": plan["plan_no"], "failed_step": steps[i]["name"]}), now_iso()),
                )
                self.append_audit(
                    conn, item_id, "dispatch_failed", actor, role,
                    {"plan_no": plan["plan_no"], "failed_step": steps[i]["name"]},
                )
                return (i, steps[i]["name"])
            if steps[i]["name"] == "transmit_command":
                # 下发指令：方案 queued -> executed，燃料占用 -> 消耗，不可回退
                conn.execute(
                    "UPDATE plans SET status=?, updated_at=? WHERE id=? AND status=?",
                    (rules.PLAN_EXECUTED, now_iso(), plan["id"], rules.PLAN_QUEUED),
                )
                conn.execute(
                    "UPDATE fuel_accounts SET fuel_occupied_m_s=fuel_occupied_m_s-?, fuel_consumed_m_s=fuel_consumed_m_s+?, "
                    "version=version+1, updated_at=? WHERE satellite_id=?",
                    (plan["fuel_cost_m_s"], plan["fuel_cost_m_s"], now_iso(), plan["satellite_id"]),
                )
                item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                item = self._row_to_item(item_row)
                new_payload = dict(item["payload"])
                new_payload["command_ref"] = command_ref
                if "plan" in new_payload:
                    new_payload["plan"]["status"] = rules.PLAN_EXECUTED
                conn.execute(
                    "UPDATE items SET status=?,payload=?,updated_at=? WHERE id=?",
                    ("executing", canonical_json(new_payload), now_iso(), item_id),
                )
            steps[i]["status"] = "done"
            conn.execute(
                "UPDATE dispatches SET steps=?, current_step=?, updated_at=? WHERE id=?",
                (canonical_json(steps), i + 1, now_iso(), self._dispatch_id(conn, plan["plan_no"])),
            )
        conn.execute(
            "UPDATE dispatches SET status=?, updated_at=? WHERE id=?",
            (rules.DISPATCH_COMPLETED, now_iso(), self._dispatch_id(conn, plan["plan_no"])),
        )
        conn.execute(
            "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, "execute", actor, role, canonical_json({"plan_no": plan["plan_no"], "command_ref": command_ref}), now_iso()),
        )
        self.append_audit(conn, item_id, "execute", actor, role, {"plan_no": plan["plan_no"], "command_ref": command_ref})
        return None

    def _dispatch_id(self, conn, plan_no):
        row = conn.execute("SELECT id FROM dispatches WHERE plan_no=?", (plan_no,)).fetchone()
        return row["id"]

    def invalidate_queued_plans(self, satellite_id, reason, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            total = self._invalidate_queued(conn, satellite_id, reason, actor, role)
            conn.execute("COMMIT")
            return total
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _invalidate_queued(self, conn, satellite_id, reason, actor, role):
        rows = conn.execute(
            "SELECT * FROM plans WHERE satellite_id=? AND status=?", (satellite_id, rules.PLAN_QUEUED)
        ).fetchall()
        for row in rows:
            plan = dict(row)
            conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE id=? AND status=?",
                (rules.PLAN_INVALIDATED, now_iso(), plan["id"], rules.PLAN_QUEUED),
            )
            conn.execute(
                "UPDATE fuel_accounts SET fuel_occupied_m_s=fuel_occupied_m_s-?, version=version+1, updated_at=? WHERE satellite_id=?",
                (plan["fuel_cost_m_s"], now_iso(), satellite_id),
            )
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (plan["item_id"],)).fetchone()
            item = self._row_to_item(item_row)
            if item["status"] == "coordinating":
                new_payload = dict(item["payload"])
                if "plan" in new_payload:
                    new_payload["plan"]["status"] = rules.PLAN_INVALIDATED
                conn.execute(
                    "UPDATE items SET status=?,payload=?,updated_at=? WHERE id=?",
                    ("assessed", canonical_json(new_payload), now_iso(), plan["item_id"]),
                )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (plan["item_id"], "plan_invalidated", actor, role, canonical_json({"plan_no": plan["plan_no"], "reason": reason}), now_iso()),
            )
            self.append_audit(
                conn, plan["item_id"], "plan_invalidated", actor, role,
                {"plan_no": plan["plan_no"], "reason": reason},
            )
        return len(rows)

    def set_fuel_quota(self, satellite_id, new_quota, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            fuel = conn.execute("SELECT * FROM fuel_accounts WHERE satellite_id=?", (satellite_id,)).fetchone()
            if fuel is None:
                conn.execute(
                    "INSERT INTO fuel_accounts(satellite_id,fuel_quota_m_s,fuel_consumed_m_s,fuel_occupied_m_s,version,updated_at) "
                    "VALUES(?,?,0,0,1,?)",
                    (satellite_id, new_quota, now_iso()),
                )
            else:
                conn.execute(
                    "UPDATE fuel_accounts SET fuel_quota_m_s=?, version=version+1, updated_at=? WHERE satellite_id=?",
                    (new_quota, now_iso(), satellite_id),
                )
            self._invalidate_queued(conn, satellite_id, "fuel_quota_change", actor, role)
            conn.execute("COMMIT")
            return self.get_fuel_account(satellite_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
