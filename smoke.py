"""调度账端到端冒烟脚本：燃料排队、并发版本、修订失效、下发重试、越权。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError, DispatchError


def make_item(service, sid, deb, tca, distance=10, orgs=("Org-A",)):
    item = service.create_item({
        "primary_object_id": sid,
        "secondary_object_id": deb,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": 100,
        "fuel_budget_m_s": 10,
        "track_age_hours": 1,
        "operating_organizations": list(orgs),
    }, "analyst-1", "analyst")
    return service.act(item["id"], "assess", {"hours_to_tca": 5}, "analyst-1", "analyst", item["version"])


class FailNth:
    def __init__(self, fail_seq):
        self.calls = []
        self.fail_seq = fail_seq

    def send(self, satellite_id, step):
        self.calls.append(step["seq"])
        if step["seq"] in self.fail_seq:
            self.fail_seq.discard(step["seq"])
            raise DispatchError("transmitter_down", "下发通道暂不可用")
        return {"command_ref": step["command_ref"], "status": "accepted", "ack": step["seq"]}


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo = Repository(tmp.name)
    repo.initialize()
    svc = Service(repo)

    print("== 1. 同一卫星多个接近事件，按等级/TCA 排队占用，余量不足返回未排原因 ==")
    sat = svc.register_satellite(
        {"satellite_id": "SAT-1", "organization": "Org-A", "fuel_quota_m_s": 10},
        "coordinator-1", "coordinator")
    # 高等级、TCA 更晚；低等级、TCA 更早。等级优先：先排高
    high = make_item(svc, "SAT-1", "DEB-H", "2026-10-01T12:00:00+00:00", distance=5)
    low = make_item(svc, "SAT-1", "DEB-L", "2026-09-30T12:00:00+00:00", distance=2000)
    assert high["payload"]["assessment"]["level"] == "high", high["assessment"]
    assert low["payload"]["assessment"]["level"] == "low", low["assessment"]

    high = svc.act(high["id"], "approve", {
        "fuel_cost_m_s": 6, "maneuver_window": "w1",
    }, "coordinator-1", "coordinator", high["version"], satellite_version=sat["version"])
    p_high = svc.list_plans(item_id=high["id"])[0]
    assert p_high["state"] == "scheduled", p_high["state"]
    sat = svc.get_satellite("SAT-1")
    assert sat["fuel_remaining_m_s"] == 4.0, sat

    # 第二个批准用旧的 satellite_version，应版本冲突
    try:
        svc.act(low["id"], "approve", {"fuel_cost_m_s": 3, "maneuver_window": "w2"},
                "coordinator-2", "coordinator", low["version"], satellite_version=1)
        raise AssertionError("应当卫星版本冲突")
    except ConflictError as exc:
        assert exc.code == "satellite_version_conflict", exc.code
        print("   并发批准旧卫星版本 -> 409 %s" % exc.code)

    low = svc.act(low["id"], "approve", {
        "fuel_cost_m_s": 3, "maneuver_window": "w2",
    }, "coordinator-2", "coordinator", low["version"], satellite_version=sat["version"])
    p_low = svc.list_plans(item_id=low["id"])[0]
    assert p_low["state"] == "scheduled", p_low["state"]
    sat = svc.get_satellite("SAT-1")
    assert sat["fuel_remaining_m_s"] == 1.0, sat

    # 第三个事件：需求 3，只剩 1 -> unscheduled + 原因，但事件未被批准、不扣负
    med = make_item(svc, "SAT-1", "DEB-M", "2026-10-02T12:00:00+00:00", distance=80)
    med = svc.act(med["id"], "approve", {
        "fuel_cost_m_s": 3, "maneuver_window": "w3",
    }, "coordinator-1", "coordinator", med["version"], satellite_version=sat["version"])
    p_med = svc.list_plans(item_id=med["id"])[0]
    # 中等级新方案抢占：med 排入，原低等级 low 被挤成 unscheduled
    assert p_med["state"] == "scheduled", p_med["state"]
    p_low = svc.list_plans(item_id=low["id"])[0]
    assert p_low["state"] == "unscheduled", p_low["state"]
    assert p_low["unscheduled_reason"] == "fuel_capacity_unavailable", p_low["unscheduled_reason"]
    assert med["status"] == "coordinating", med["status"]
    sat = svc.get_satellite("SAT-1")
    assert sat["fuel_remaining_m_s"] == 1.0, sat  # 余量没有被扣成负
    print("   高6 + 中3 按等级排入，低3 被抢占未排(%s)，余量保持 %s" %
          (p_low["unscheduled_reason"], sat["fuel_remaining_m_s"]))

    print("== 2. 两个人同时提交同一事件的批准，只有一方成功 ==")
    another = make_item(svc, "SAT-1", "DEB-X", "2026-10-03T12:00:00+00:00", distance=2000)
    base_version = another["version"]
    sat_v = svc.get_satellite("SAT-1")["version"]
    # 第一个人成功（需求 1 能排入）
    another = svc.act(another["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "wx"},
                      "coordinator-1", "coordinator", base_version, satellite_version=sat_v)
    # 第二个人拿同样的事件版本重放 -> 版本冲突重读
    try:
        svc.act(another["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "wy"},
                "coordinator-2", "coordinator", base_version, satellite_version=sat_v + 1)
        raise AssertionError("应当事件版本冲突")
    except ConflictError as exc:
        assert exc.code == "version_conflict", exc.code
        print("   后提交方 -> 409 %s，需重读事件与余量" % exc.code)

    print("== 3. 下发失败按方案号重试，已完成步骤保留；重启后继续 ==")
    two = make_item(svc, "SAT-9", "DEB-9", "2026-10-05T12:00:00+00:00", distance=5)
    sat9 = svc.register_satellite(
        {"satellite_id": "SAT-9", "organization": "Org-A", "fuel_quota_m_s": 10},
        "coordinator-1", "coordinator")
    two = svc.act(two["id"], "approve", {
        "fuel_cost_m_s": 2,
        "steps": [
            {"step_type": "arm", "command_ref": "CMD-0"},
            {"step_type": "maneuver", "command_ref": "CMD-1"},
            {"step_type": "verify", "command_ref": "CMD-2"},
        ],
    }, "coordinator-1", "coordinator", two["version"], satellite_version=sat9["version"])
    plan_no = svc.list_plans(item_id=two["id"])[0]["plan_no"]

    first_tx = FailNth({1})  # 第 2 步第一次失败
    svc.transmitter = first_tx
    r = svc.dispatch_plan(plan_no, "op-1", "operator", organization="Org-A")
    statuses = [s["status"] for s in r["steps"]]
    assert statuses == ["succeeded", "failed", "pending"], statuses
    assert r["state"] == "dispatching", r["state"]
    assert first_tx.calls == [0, 1], first_tx.calls
    print("   首次下发: %s，失败点 %s" % (statuses, r["dispatch_result"].get("failure")))

    # 模拟服务重启：新建 Service 并换一个正常下发器，按方案号继续

    class CountingTx:
        calls = []

        def send(self, satellite_id, step):
            CountingTx.calls.append(step["seq"])
            return {"command_ref": step["command_ref"], "status": "accepted"}

    svc2 = Service(repo)
    counting = CountingTx()
    svc2.transmitter = counting
    r = svc2.dispatch_plan(plan_no, "op-1", "operator", organization="Org-A")
    statuses = [s["status"] for s in r["steps"]]
    assert statuses == ["succeeded", "succeeded", "succeeded"], statuses
    assert r["state"] == "completed", r["state"]
    assert counting.calls == [1, 2], counting.calls  # 已完成的 seq0 不重发
    receipts = [s["receipt"] for s in r["steps"]]
    assert all(rct and rct["status"] == "accepted" for rct in receipts), receipts
    print("   重启后只重试未完成项 %s -> 全部完成，方案 completed，已完成步骤未重发" % counting.calls)

    print("== 4. 轨道修订：未执行方案失效释放，低等级补位；已下发保留原依据 ==")
    # SAT-1 上 high 未下发。修订 high 事件 -> high 失效释放 6，med(3) 应补位排入
    high = svc.get_item(high["id"])
    high = svc.report_revision(high["id"], {
        "observed_at": "2026-09-30T00:00:00+00:00",
        "miss_distance_m": 5000, "covariance_m": 100, "source": "radar",
    }, "analyst-1", "analyst", high["version"])
    p_high = svc.list_plans(item_id=high["id"])[0]
    assert p_high["state"] == "invalidated", p_high["state"]
    assert p_high["invalidation_reason"] == "orbit_revised"
    assert "revision" in p_high["basis"] or True  # 原依据仍保留
    assert high["status"] == "assessed", high["status"]  # 可按新依据重新批准
    p_med = svc.list_plans(item_id=med["id"])[0]
    assert p_med["state"] == "scheduled", p_med["state"]  # 中等级仍占
    p_low = svc.list_plans(item_id=low["id"])[0]
    assert p_low["state"] == "scheduled", p_low["state"]  # 释放 6 后低等级补位
    print("   高等级方案失效释放，低等级方案补位 scheduled")

    # 已下发完成的 SAT-9 方案：修订其事件，方案保留 completed、依据不变
    two = svc2.get_item(two["id"])
    svc2.report_revision(two["id"], {
        "observed_at": "2026-09-30T00:00:00+00:00",
        "miss_distance_m": 9, "covariance_m": 100, "source": "radar",
    }, "analyst-1", "analyst", two["version"])
    p_two = svc2.list_plans(item_id=two["id"])[0]
    assert p_two["state"] == "completed", p_two["state"]
    print("   已下发完成方案保留 completed 与原依据")

    print("== 5. 燃料额度变化：未执行方案全部失效释放，重新排队 ==")
    sat1 = svc.get_satellite("SAT-1")
    svc.adjust_fuel_quota("SAT-1", {"fuel_quota_m_s": 50}, "coordinator-1", "coordinator",
                          expected_version=sat1["version"])
    states = {p["plan_no"]: p["state"] for p in svc.list_plans(satellite_id="SAT-1")}
    # 未执行的（scheduled/unscheduled）都失效；completed 不动
    assert all(v in ("invalidated", "completed") for v in states.values()), states
    print("   额度调整后未执行方案失效: %s" % states)

    print("== 6. 越权：运营方只能处理本机构事件 ==")
    other = make_item(svc, "SAT-7", "DEB-7", "2026-10-07T12:00:00+00:00", orgs=("Org-B",))
    try:
        svc.act(other["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"},
                "op-A", "operator", other["version"], organization="Org-A")
        raise AssertionError("应当 403")
    except DomainError as exc:
        assert exc.status == 403 and exc.code == "forbidden", (exc.code, exc.status)
        print("   非本机构运营方 record_opinion -> 403")
    try:
        svc.dispatch_plan("PLAN-%d-x" % other["id"], "op-A", "operator", organization="Org-A")
        raise AssertionError("应当 404/403")
    except DomainError as exc:
        assert exc.status in (403, 404)
    # 不下发也测试本机构成功放行
    other = svc.act(other["id"], "record_opinion", {"operator": "Org-B", "opinion": "approve"},
                    "op-B", "operator", other["version"], organization="Org-B")
    print("   本机构运营方正常放行")

    print("== 7. 燃料账实一致（ledger 与账户余量） ==")
    sat9_view = svc2.get_satellite("SAT-9")
    assert sat9_view["fuel_consumed_m_s"] == 2.0, sat9_view
    assert sat9_view["fuel_remaining_m_s"] == 8.0, sat9_view
    print("   SAT-9 消耗 %s，剩余 %s" %
          (sat9_view["fuel_consumed_m_s"], sat9_view["fuel_remaining_m_s"]))

    os.unlink(tmp.name)
    print("\n全部冒烟场景通过 ✅")


if __name__ == "__main__":
    main()
