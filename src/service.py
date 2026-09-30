import json

from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def _identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def _check_role(self, role, allowed):
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)

    def create_item(self, payload, actor, role, region=None):
        self._identity(actor, role)
        self._check_role(role, rules.CREATE_ROLES)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        item = self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )
        # 卫星燃料账户按颗建立：同卫星的多个事件共享同一本燃料账
        self.repository.ensure_fuel_account(normalized["primary_object_id"], normalized["fuel_budget_m_s"])
        return self.get_item(item["id"])

    def add_source(self, item_id, payload, actor, role, region=None):
        self._identity(actor, role)
        self._check_role(role, rules.SOURCE_ROLES)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        return self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None, org=None, expected_fuel_version=None):
        self._identity(actor, role)
        self._check_role(role, rules.ACTION_ROLES.get(action, set()))
        item = self.repository.get_item(item_id)
        rules.check_org(item, role, org)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        if action == "approve":
            fuel = domain.number(payload, "fuel_cost_m_s", 0)
            window = domain.require_text(payload, "maneuver_window")
            _, reason = self.repository.book_plan(
                item_id, fuel, window, expected_version, expected_fuel_version, actor, role
            )
            if reason is not None:
                raise DomainError("plan_unscheduled", "规避方案未能排入调度：%s" % self._reason_text(reason), 409)
            return self.get_item(item_id)

        if action == "execute":
            command_ref = domain.require_text(payload, "command_ref")
            fail_step = payload.get("fail_step")
            if fail_step is not None:
                fail_step = int(fail_step)
            self.repository.execute_dispatch(
                item_id, command_ref, fail_step, actor, role, expected_version, expected_fuel_version
            )
            return self.get_item(item_id)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        # 轨道修订后，未执行方案立即失效并释放占用；已下发指令保留原依据
        if action == "report_revision":
            satellite_id = item["payload"].get("primary_object_id")
            self.repository.invalidate_queued_plans(satellite_id, "orbit_revision", actor, role)
        return self.get_item(item_id)

    def retry_dispatch(self, plan_no, actor, role, org=None):
        self._identity(actor, role)
        self._check_role(role, rules.DISPATCH_RETRY_ROLES)
        plan = self.repository.get_plan(plan_no)
        item = self.repository.get_item(plan["item_id"])
        rules.check_org(item, role, org)
        self.repository.retry_dispatch(plan_no, actor, role)
        return self.get_item(plan["item_id"])

    def set_fuel_quota(self, satellite_id, payload, actor, role):
        self._identity(actor, role)
        self._check_role(role, rules.FUEL_QUOTA_ROLES)
        quota = domain.number(payload, "fuel_quota_m_s", 0)
        return self.repository.set_fuel_quota(satellite_id, quota, actor, role)

    def get_fuel_account(self, satellite_id):
        return self.repository.get_fuel_account(satellite_id)

    def get_plan(self, plan_no):
        return self.repository.get_plan(plan_no)

    def list_plans(self, satellite_id):
        return self.repository.list_plans(satellite_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        satellite_id = item["payload"].get("primary_object_id")
        if satellite_id:
            item["fuel"] = self.repository.get_fuel_account(satellite_id)
        plan = self._active_plan(item_id)
        if plan is not None:
            item["plan"] = plan
            item["dispatch"] = plan.get("dispatch")
        return item

    def _active_plan(self, item_id):
        conn = self.repository.connect()
        try:
            row = conn.execute(
                "SELECT * FROM plans WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)
            ).fetchone()
            if row is None:
                return None
            plan = dict(row)
            disp = conn.execute("SELECT * FROM dispatches WHERE plan_no=?", (plan["plan_no"],)).fetchone()
            if disp is not None:
                disp = dict(disp)
                disp["steps"] = json.loads(disp["steps"])
            plan["dispatch"] = disp
            return plan
        finally:
            conn.close()

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    @staticmethod
    def _reason_text(reason):
        return {
            rules.REASON_FUEL_CAPACITY: "卫星燃料容量不足，方案按等级与碰撞时刻排队后无余量",
            rules.REASON_EVENT_BUDGET: "超过单次事件燃料预算",
        }.get(reason, reason)
