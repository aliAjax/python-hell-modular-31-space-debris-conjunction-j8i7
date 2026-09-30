import json

from . import domain, rules, scheduling
from .domain import DomainError, ForbiddenError, DispatchError, ConflictError, NotFoundError
from .repository import now_iso

# 方案编号前缀
PLAN_PREFIX = "PLAN"


class DefaultTransmitter:
    """指令下发器的演示实现：成功并生成回执。

    测试可注入伪下发器（按 command_ref 抛 DispatchError）来验证重试。
    """

    def send(self, satellite_id, step):
        return {
            "command_ref": step["command_ref"],
            "accepted_at": now_iso(),
            "status": "accepted",
        }


class Service:
    def __init__(self, repository, transmitter=None):
        self.repository = repository
        self.transmitter = transmitter or DefaultTransmitter()

    # ------------------------------------------------------------- identity

    def _identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def _operator_org(self, role, organization):
        if role == "operator" and not organization:
            raise DomainError("organization_required", "运营方请求需要 X-Organization 标明所属机构", 403)

    def _check_event_org(self, item, organization):
        """运营方只能处理本机构的接近事件。"""
        if not organization:
            raise ForbiddenError("forbidden", "无法确认运营方所属机构")
        orgs = item["payload"].get("operating_organizations", [])
        if orgs and organization not in orgs:
            raise ForbiddenError("forbidden", "该接近事件不属于本机构，禁止越权操作")

    # --------------------------------------------------------------- items

    def create_item(self, payload, actor, role, region=None):
        self._identity(actor, role)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None, organization=None):
        self._identity(actor, role)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        if role == "operator":
            self._operator_org(role, organization)
            self._check_event_org(item, organization)
        normalized = domain.normalize_source(payload)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    # ------------------------------------------------------------- satellites

    def register_satellite(self, payload, actor, role, organization=None):
        self._identity(actor, role)
        normalized = domain.normalize_satellite(payload)
        if role not in {"operator", "coordinator", "analyst"}:
            raise ForbiddenError("forbidden", "当前角色不能登记卫星燃料账户")
        if role == "operator":
            self._operator_org(role, organization)
            if organization != normalized["organization"]:
                raise ForbiddenError("forbidden", "只能为本机构登记卫星")
        return self.repository.register_satellite(
            normalized["satellite_id"], normalized["organization"], normalized["fuel_quota_m_s"], actor, role
        )

    def get_satellite(self, satellite_id):
        return self.repository.get_satellite(satellite_id)

    def list_satellites(self):
        return self.repository.list_satellites()

    def fuel_ledger(self, satellite_id):
        return self.repository.fuel_ledger(satellite_id)

    def adjust_fuel_quota(self, satellite_id, payload, actor, role, expected_version=None, organization=None):
        """燃料额度变化：未执行方案立即失效并释放占用，随后重新排队。"""
        self._identity(actor, role)
        if role not in {"coordinator", "operator"}:
            raise ForbiddenError("forbidden", "当前角色不能调整燃料额度")
        if expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_quota = domain.normalize_quota_change(payload)
        conn = self.repository.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            sat = self.repository.lock_satellite(conn, satellite_id)
            if role == "operator":
                self._operator_org(role, organization)
                if organization != sat["organization"]:
                    raise ForbiddenError("forbidden", "只能调整本机构卫星的燃料额度")
            if int(expected_version) != int(sat["version"]):
                raise ConflictError("satellite_version_conflict", "卫星燃料账户版本已变化，请重新读取")
            old_quota = float(sat["fuel_quota_m_s"])
            self._invalidate_unexecuted(conn, satellite_id, "fuel_quota_changed", actor, role, {
                "old_quota_m_s": old_quota, "new_quota_m_s": new_quota,
            })
            conn.execute(
                "UPDATE satellites SET fuel_quota_m_s=?,version=version+1,updated_at=? WHERE satellite_id=?",
                (new_quota, now_iso(), satellite_id),
            )
            sat["fuel_quota_m_s"] = new_quota
            sat["version"] = int(sat["version"]) + 1
            self.repository._ledger(conn, satellite_id, None, "quota_change", new_quota - old_quota,
                                    {"old": old_quota, "new": new_quota, "actor": actor})
            self.repository.append_audit(conn, None, "fuel_quota_changed", actor, role, {
                "satellite_id": satellite_id, "old_quota_m_s": old_quota, "new_quota_m_s": new_quota,
            })
            conn.execute("COMMIT")
            return self.repository.get_satellite(satellite_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------- plans core

    def _reschedule(self, conn, satellite_id):
        """在已锁定卫星账户行的事务内重新排队并落库状态变化。"""
        sat = self.repository.lock_satellite(conn, satellite_id)
        plans = self.repository.list_plans_for_satellite(conn, satellite_id)
        consumed = sum(p["fuel_cost_m_s"] for p in plans if p["state"] == "completed")
        changes = scheduling.recompute_queue(plans, sat["fuel_quota_m_s"], consumed)
        for plan_id, change in changes.items():
            self.repository.set_plan_state(
                conn, plan_id, change["state"], unscheduled_reason=change["unscheduled_reason"]
            )
            plan = next(p for p in plans if p["id"] == plan_id)
            self.repository.append_audit(conn, plan["item_id"], "plan_rescheduled", None, None, {
                "plan_id": plan_id, "plan_no": plan["plan_no"], "state": change["state"],
                "unscheduled_reason": change["unscheduled_reason"],
            })
        if changes:
            self.repository.bump_satellite(conn, sat)
        return changes

    def _invalidate_unexecuted(self, conn, satellite_id, reason, actor, role, detail=None, item_id=None):
        plans = self.repository.list_plans_for_satellite(conn, satellite_id)
        affected_items = set()
        for plan in plans:
            if item_id is not None and plan["item_id"] != item_id:
                continue  # 轨道修订只失效该事件的方案，同卫星其他事件不受影响
            if plan["state"] in scheduling.UNEXECUTED_STATES:
                self.repository.set_plan_state(
                    conn, plan["id"], "invalidated", invalidation_reason=reason
                )
                self.repository._ledger(conn, satellite_id, plan["id"], "reservation_released",
                                        -float(plan["fuel_cost_m_s"]), {"reason": reason})
                self.repository.append_audit(conn, plan["item_id"], "plan_invalidated", actor, role, {
                    "plan_id": plan["id"], "plan_no": plan["plan_no"], "reason": reason, "detail": detail or {},
                })
                affected_items.add(plan["item_id"])
            # 已下发（dispatching/completed）保留原依据与占用，不动
        for affected_id in affected_items:
            item = self.repository.lock_item(conn, affected_id)
            if item["status"] == "coordinating":
                # 方案未执行即失效，事件回到已评估，等待按新依据重新批准
                self.repository.update_item(conn, item, "assessed")
        return affected_items

    def submit_plan(self, item_id, payload, actor, role, expected_version=None,
                    satellite_version=None, organization=None):
        """批准接近事件 = 提交规避方案：按等级/TCA 排队占用卫星燃料。

        并发控制：expected_version 锁定事件版本，satellite_version 锁定卫星
        燃料账户版本；两个人同时批准同一卫星时后提交者得到 409 版本冲突。
        """
        self._identity(actor, role)
        if role not in rules.ACTION_ROLES["approve"]:
            raise ForbiddenError("forbidden", "当前角色不能批准规避方案")
        if expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        cost = float(payload["fuel_cost_m_s"]) if payload.get("fuel_cost_m_s") is not None else None
        if cost is None:
            raise DomainError("field_required", "fuel_cost_m_s 不能为空")
        if cost < 0:
            raise DomainError("invalid_number", "fuel_cost_m_s 不能小于 0")
        steps = domain.normalize_steps(payload)
        window = payload.get("maneuver_window") or steps[0]["payload"].get("window") or steps[0]["command_ref"]

        conn = self.repository.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = self.repository.lock_item(conn, item_id)
            if int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            # 先做事件本身的规则校验（状态、冲突意见、燃料字段）
            new_status, new_payload, event_payload = rules.apply_action(
                item, "approve", dict(payload, maneuver_window=window), actor, role
            )
            satellite_id = new_payload.get("primary_object_id")
            # 每个事件同时只允许一个未完成方案
            existing = [p for p in self.repository.list_plans_for_satellite(conn, satellite_id)
                        if p["item_id"] == item_id
                        and p["state"] in scheduling.UNEXECUTED_STATES | scheduling.ISSUED_STATES]
            if existing:
                raise DomainError("plan_exists", "该事件已有未完成的规避方案", 409)
            # 再锁定卫星燃料账户行：两个人并发时只有一个事务拿到锁，
            # 另一个在 satellite_version 上按版本冲突重读余量。
            sat = self.repository.lock_satellite(conn, satellite_id)
            if satellite_version is None:
                raise DomainError("satellite_version_required",
                                  "批准方案需要 satellite_version，请先读取卫星燃料账户", 400)
            if int(satellite_version) != int(sat["version"]):
                raise ConflictError("satellite_version_conflict",
                                    "卫星燃料账户已被其他批准更新，请重新读取燃料余量后重试")

            level = (new_payload.get("assessment") or {}).get("level", "low")
            tca = new_payload["tca"]
            basis = {
                "item_id": item_id,
                "miss_distance_m": new_payload.get("miss_distance_m"),
                "covariance_m": new_payload.get("covariance_m"),
                "assessment": new_payload.get("assessment"),
                "fuel_budget_m_s": new_payload.get("fuel_budget_m_s"),
                "revisions": list(new_payload.get("revisions", [])),
                "approved_by": actor,
                "approved_at": now_iso(),
            }
            # 方案号按该事件历史方案数递增，轨道修订后重新批准也不撞号
            prior = [p for p in self.repository.list_plans_for_satellite(conn, satellite_id)
                     if p["item_id"] == item_id]
            plan_no = "%s-%d-%d" % (PLAN_PREFIX, item_id, len(prior) + 1)
            # 先以 queued 插入，再统一重排决定 scheduled / unscheduled
            plan_id = self.repository.insert_plan(
                conn, plan_no, item_id, satellite_id, "queued", level, tca, cost, basis, None, actor, role
            )
            self.repository.insert_plan_steps(conn, plan_id, steps)
            self.repository.update_item(conn, item, new_status, new_payload)
            self.repository._log_action(conn, item_id, "approve", actor, role, event_payload)
            self.repository.append_audit(conn, item_id, "approve", actor, role, event_payload)
            self.repository.append_audit(conn, item_id, "plan_submitted", actor, role, {
                "plan_id": plan_id, "plan_no": plan_no, "satellite_id": satellite_id,
                "fuel_cost_m_s": cost, "level": level,
            })
            changes = self._reschedule(conn, satellite_id)
            outcome = changes.get(plan_id, {"state": "scheduled", "unscheduled_reason": None})
            self.repository.append_audit(conn, item_id, "plan_scheduling_result", actor, role, {
                "plan_no": plan_no, "state": outcome["state"],
                "unscheduled_reason": outcome["unscheduled_reason"],
            })
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return self.get_item(item_id)

    def report_revision(self, item_id, payload, actor, role, expected_version=None):
        """轨道修订：本事件未执行方案立即失效并释放占用；已下发指令保留原依据。"""
        self._identity(actor, role)
        if role not in rules.ACTION_ROLES["report_revision"]:
            raise ForbiddenError("forbidden", "当前角色不能上报轨道修订")
        if expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        conn = self.repository.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = self.repository.lock_item(conn, item_id)
            if int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            _, new_payload, event_payload = rules.apply_action(
                item, "report_revision", payload, actor, role
            )
            satellite_id = new_payload.get("primary_object_id")
            sat = self.repository.lock_satellite(conn, satellite_id, optional=True)
            self.repository.update_item(conn, item, payload=new_payload)
            self.repository._log_action(conn, item_id, "report_revision", actor, role, event_payload)
            self.repository.append_audit(conn, item_id, "report_revision", actor, role, event_payload)
            if sat is not None:
                # 只失效本事件的方案；释放后让同卫星其他事件重新排队
                self._invalidate_unexecuted(
                    conn, satellite_id, "orbit_revised", actor, role,
                    {"revision": event_payload.get("revision")}, item_id=item_id
                )
                self._reschedule(conn, satellite_id)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return self.get_item(item_id)

    def list_plans(self, item_id=None, satellite_id=None):
        return self.repository.list_plans(item_id=item_id, satellite_id=satellite_id)

    def get_plan(self, plan_no):
        conn = self.repository.connect()
        try:
            row = conn.execute("SELECT * FROM plans WHERE plan_no=?", (plan_no,)).fetchone()
            if row is None:
                raise NotFoundError("plan_not_found", "规避方案不存在")
            plan = dict(row)
            plan["basis"] = json.loads(plan["basis"])
            return self.repository.plan_view(conn, plan)
        finally:
            conn.close()

    # ------------------------------------------------------------- dispatch

    def dispatch_plan(self, plan_no, actor, role, organization=None):
        """按方案号重试未完成项：已完成步骤保留，只发 pending/failed 的步骤。"""
        self._identity(actor, role)
        if role not in {"operator"}:
            raise ForbiddenError("forbidden", "只有运营方可以下发规避指令")
        self._operator_org(role, organization)

        conn = self.repository.connect()
        failure = None
        dispatched = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM plans WHERE plan_no=?", (plan_no,)).fetchone()
            if row is None:
                raise NotFoundError("plan_not_found", "规避方案不存在")
            plan = dict(row)
            plan["basis"] = json.loads(plan["basis"])
            if plan["state"] not in ("scheduled", "dispatching"):
                raise DomainError("plan_not_dispatchable",
                                 "方案状态 %s 不可下发（未排入或已失效）" % plan["state"], 409)
            sat = self.repository.lock_satellite(conn, plan["satellite_id"])
            item = self.repository.lock_item(conn, plan["item_id"])
            self._check_event_org(item, organization)

            steps = self.repository.get_steps(conn, plan["id"])
            for step in steps:
                if step["status"] == "succeeded":
                    continue  # 已完成步骤保留，不重发
                try:
                    conn.execute(
                        "UPDATE plan_steps SET attempts=attempts+1,status='sending',last_error=NULL WHERE id=?",
                        (step["id"],),
                    )
                    receipt = self.transmitter.send(plan["satellite_id"], {**step, "plan_no": plan_no})
                    conn.execute(
                        "UPDATE plan_steps SET status='succeeded',receipt=?,dispatched_at=? WHERE id=?",
                        (json.dumps(receipt, ensure_ascii=False), now_iso(), step["id"]),
                    )
                    dispatched.append({"seq": step["seq"], "command_ref": step["command_ref"], "receipt": receipt})
                except DispatchError as exc:
                    conn.execute(
                        "UPDATE plan_steps SET status='failed',last_error=? WHERE id=?",
                        (exc.reason, step["id"]),
                    )
                    failure = {"seq": step["seq"], "command_ref": step["command_ref"], "reason": exc.reason}
                    break
                except Exception as exc:  # 下发通道异常同样落 failed，可重试
                    conn.execute(
                        "UPDATE plan_steps SET status='failed',last_error=? WHERE id=?",
                        (str(exc)[:200], step["id"]),
                    )
                    failure = {"seq": step["seq"], "command_ref": step["command_ref"], "reason": str(exc)[:200]}
                    break

            final_steps = self.repository.get_steps(conn, plan["id"])
            if plan["state"] == "scheduled" and any(s["status"] == "succeeded" for s in final_steps):
                self.repository.set_plan_state(conn, plan["id"], "dispatching")
            if failure:
                self.repository.append_audit(conn, plan["item_id"], "plan_dispatch_failed", actor, role, {
                    "plan_no": plan_no, "failure": failure,
                    "completed": [s["seq"] for s in final_steps if s["status"] == "succeeded"],
                })
            elif all(s["status"] == "succeeded" for s in final_steps):
                self.repository.set_plan_state(conn, plan["id"], "completed")
                # 占用转为已消耗
                self.repository._ledger(conn, plan["satellite_id"], plan["id"], "reservation_consumed",
                                        -float(plan["fuel_cost_m_s"]), {"plan_no": plan_no})
                self.repository._ledger(conn, plan["satellite_id"], plan["id"], "fuel_consumed",
                                        float(plan["fuel_cost_m_s"]), {"plan_no": plan_no})
                self.repository.update_item(conn, item, "executing")
                self.repository.append_audit(conn, plan["item_id"], "plan_completed", actor, role, {
                    "plan_no": plan_no, "command_refs": [s["command_ref"] for s in final_steps],
                })
            else:
                self.repository.append_audit(conn, plan["item_id"], "plan_dispatched", actor, role, {
                    "plan_no": plan_no, "dispatched": dispatched,
                })
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        result = self.get_plan(plan_no)
        result["dispatch_result"] = {
            "succeeded": [s["seq"] for s in result["steps"] if s["status"] == "succeeded"],
            "failed": [s["seq"] for s in result["steps"] if s["status"] == "failed"],
            "pending": [s["seq"] for s in result["steps"] if s["status"] in ("pending", "sending")],
        }
        if failure:
            result["dispatch_result"]["failure"] = failure
        return result

    # ------------------------------------------------------- legacy actions

    def _legacy_execute(self, item, payload, actor, role, expected_version, organization):
        """兼容旧 execute 动作：在同一事务内确认指令并把方案占用结转为消耗。"""
        item_id = item["id"]
        conn = self.repository.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = self.repository.lock_item(conn, item_id)
            if int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            new_status, new_payload, event_payload = rules.apply_action(
                item, "execute", payload, actor, role
            )
            satellite_id = item["payload"].get("primary_object_id")
            plans = [p for p in self.repository.list_plans_for_satellite(conn, satellite_id)
                     if p["item_id"] == item_id
                     and p["state"] in ("scheduled", "dispatching")]
            if not plans:
                raise DomainError("plan_not_dispatchable",
                                 "没有已排入的规避方案可供执行", 409)
            plan = sorted(plans, key=lambda p: p["id"])[-1]
            steps = self.repository.get_steps(conn, plan["id"])
            for step in steps:
                if step["status"] != "succeeded":
                    conn.execute(
                        "UPDATE plan_steps SET attempts=attempts+1,status='succeeded',"
                        "receipt=?,dispatched_at=?,last_error=NULL WHERE id=?",
                        (json.dumps({"command_ref": step["command_ref"], "legacy_ref": payload.get("command_ref"),
                                     "status": "accepted"}, ensure_ascii=False), now_iso(), step["id"]),
                    )
            if plan["state"] == "scheduled":
                self.repository._ledger(conn, satellite_id, plan["id"], "reservation_consumed",
                                        -float(plan["fuel_cost_m_s"]), {"plan_no": plan["plan_no"]})
                self.repository._ledger(conn, satellite_id, plan["id"], "fuel_consumed",
                                        float(plan["fuel_cost_m_s"]), {"plan_no": plan["plan_no"]})
            self.repository.set_plan_state(conn, plan["id"], "completed")
            self.repository.update_item(conn, item, new_status, new_payload)
            self.repository._log_action(conn, item_id, "execute", actor, role, event_payload)
            self.repository.append_audit(conn, item_id, "execute", actor, role, event_payload)
            self.repository.append_audit(conn, item_id, "plan_completed", actor, role, {
                "plan_no": plan["plan_no"], "command_refs": [s["command_ref"] for s in steps],
                "legacy": True,
            })
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return self.get_item(item_id)

    def act(self, item_id, action, payload, actor, role, expected_version=None,
            region=None, organization=None, satellite_version=None):
        self._identity(actor, role)
        if action == "approve":
            return self.submit_plan(
                item_id, payload, actor, role, expected_version, satellite_version, organization
            )
        if action == "report_revision":
            return self.report_revision(item_id, payload, actor, role, expected_version)

        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if role == "operator" and action in {"execute", "record_opinion"}:
            self._operator_org(role, organization)
            self._check_event_org(item, organization)
            if action == "record_opinion":
                claimed = str(payload.get("operator", "")).strip()
                if claimed and claimed != organization:
                    raise ForbiddenError("forbidden", "意见中的机构必须与所属机构一致")
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action == "execute":
            return self._legacy_execute(item, payload, actor, role, expected_version, organization)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["plans"] = self.repository.list_plans(item_id=item_id)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
