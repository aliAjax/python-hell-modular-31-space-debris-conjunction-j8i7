from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


class ForbiddenError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 403)


class DispatchError(Exception):
    """指令下发失败（非领域校验错误，失败状态按步骤持久化）。"""

    def __init__(self, reason, message=None):
        super().__init__(message or reason)
        self.reason = reason


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def positive_integer(payload, name):
    value = payload.get(name, 0)
    if isinstance(value, bool):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    if value < 0:
        raise DomainError("invalid_integer", "%s 不能为负数" % name)
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    primary = require_text(payload, "primary_object_id")
    secondary = require_text(payload, "secondary_object_id")
    if primary == secondary:
        raise DomainError("same_object", "接近事件的两个物体不能相同")
    tca = parse_timestamp(payload, "tca")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    fuel_budget = number(payload, "fuel_budget_m_s", 0)
    track_age = number(payload, "track_age_hours", 0)
    operators = payload.get("operating_organizations", [])
    if not isinstance(operators, list) or any(not isinstance(item, str) or not item.strip() for item in operators):
        raise DomainError("invalid_operators", "运营方必须是字符串列表")
    stable_key = "%s|%s|%s" % tuple(sorted([primary, secondary]) + [tca])
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "fuel_budget_m_s": fuel_budget,
        "track_age_hours": track_age,
        "operating_organizations": [item.strip() for item in operators],
        "revisions": [],
        "opinions": [],
        "conflict": False,
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "region": region,
        "operator": payload.get("operator"),
    }


def normalize_satellite(payload):
    satellite_id = require_text(payload, "satellite_id")
    organization = require_text(payload, "organization")
    quota = number(payload, "fuel_quota_m_s", 0)
    return {"satellite_id": satellite_id, "organization": organization, "fuel_quota_m_s": quota}


def normalize_quota_change(payload):
    return number(payload, "fuel_quota_m_s", 0)


def normalize_steps(payload):
    steps = payload.get("steps")
    if steps is None:
        # 兼容旧的单个 maneuver 窗口
        window = require_text(payload, "maneuver_window")
        return [{"step_type": "maneuver", "command_ref": "MANEUVER", "payload": {"window": window}}]
    if not isinstance(steps, list) or not steps:
        raise DomainError("invalid_steps", "steps 必须是非空列表")
    if len(steps) > 50:
        raise DomainError("invalid_steps", "单次方案不能超过 50 个步骤")
    normalized = []
    seen = set()
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise DomainError("invalid_steps", "第 %d 个步骤必须是对象" % (index + 1))
        step_type = require_text(raw, "step_type")
        command_ref = str(raw.get("command_ref") or ("STEP-%d" % (index + 1))).strip()
        if command_ref in seen:
            raise DomainError("invalid_steps", "步骤指令号重复: %s" % command_ref)
        seen.add(command_ref)
        extra = raw.get("payload", {})
        if extra is not None and not isinstance(extra, dict):
            raise DomainError("invalid_steps", "步骤 payload 必须是对象")
        normalized.append({"step_type": step_type, "command_ref": command_ref, "payload": extra or {}})
    return normalized
