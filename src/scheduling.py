"""卫星燃料调度账的纯函数：方案排队、容量占用与未排原因。

方案按等级（high > medium > low）和碰撞时刻 TCA 排队，依次占用卫星
燃料余量；余量不足的方案保持排队态并记录未排原因。
"""
from datetime import datetime

LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}

# 仍占用燃料容量的方案状态
RESERVED_STATES = {"scheduled", "dispatching", "completed"}
# 尚未下发指令、可被轨道修订 / 额度变化失效的方案状态
UNEXECUTED_STATES = {"queued", "scheduled", "unscheduled"}
# 已下发指令，之后只保留原依据
ISSUED_STATES = {"dispatching", "completed"}

EPSILON = 1e-9


def parse_tca(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def plan_sort_key(plan):
    level = (plan.get("level") or "low").lower()
    return (
        LEVEL_ORDER.get(level, 3),
        parse_tca(plan["tca"]),
        plan.get("seq", 0),
        plan.get("id", 0),
    )


def recompute_queue(plans, quota, consumed=0.0):
    """按等级/TCA 重排卫星下的方案，返回 (plan_id -> 结果) 映射。

    - 已完成方案的消耗永久保留；dispatching（已下发）保留占用与原依据；
    - 其余 active 方案按顺序重新占用 remaining = quota - consumed - 已占用；
    - 余量不足记 unscheduled + 原因。返回的映射只包含需要落库的状态变化。
    """
    results = {}
    available = float(quota)
    reserved = float(consumed)
    for plan in plans:
        if plan["state"] in ISSUED_STATES:
            reserved += float(plan["fuel_cost_m_s"])

    ordered = sorted(plans, key=plan_sort_key)
    for plan in ordered:
        state = plan["state"]
        if state in ISSUED_STATES or state == "invalidated":
            continue
        cost = float(plan["fuel_cost_m_s"])
        capacity = available - reserved
        if cost <= capacity + EPSILON:
            new_state, reason = "scheduled", None
            reserved += cost
        elif cost > available + EPSILON:
            new_state, reason = "unscheduled", "fuel_cost_exceeds_quota"
        else:
            new_state, reason = "unscheduled", "fuel_capacity_unavailable"
        if new_state != state or (plan.get("unscheduled_reason") or None) != reason:
            results[plan["id"]] = {"state": new_state, "unscheduled_reason": reason}
    return results


def unscheduled_message(reason):
    return {
        "fuel_capacity_unavailable": "燃料余量被更高优先级方案占用，未排入计划",
        "fuel_cost_exceeds_quota": "方案燃料需求超过卫星总额度，无法排入",
    }.get(reason, "方案未排入")
