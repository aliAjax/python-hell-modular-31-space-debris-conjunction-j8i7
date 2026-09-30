import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError, DispatchError
from src import scheduling


def assessed(service, sid="SAT-1", deb="DEB-1", tca="2026-10-10T00:00:00+00:00",
             distance=5, orgs=("Org-A",)):
    item = service.create_item({
        "primary_object_id": sid, "secondary_object_id": deb, "tca": tca,
        "miss_distance_m": distance, "covariance_m": 100,
        "fuel_budget_m_s": 10, "track_age_hours": 1,
        "operating_organizations": list(orgs),
    }, "analyst-1", "analyst")
    return service.act(item["id"], "assess", {"hours_to_tca": 5},
                       "analyst-1", "analyst", item["version"])


class FailNth:
    def __init__(self, fail_seqs):
        self.calls = []
        self.fail_seqs = set(fail_seqs)

    def send(self, satellite_id, step):
        self.calls.append(step["seq"])
        if step["seq"] in self.fail_seqs:
            self.fail_seqs.discard(step["seq"])
            raise DispatchError("transmitter_down")
        return {"command_ref": step["command_ref"], "status": "accepted"}


class SchedulingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.sat = self.service.register_satellite(
            {"satellite_id": "SAT-1", "organization": "Org-A", "fuel_quota_m_s": 10},
            "coordinator-1", "coordinator")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def approve(self, item, cost):
        return self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": cost, "maneuver_window": "w"},
                                "coordinator-1", "coordinator", item["version"],
                                satellite_version=self.service.get_satellite("SAT-1")["version"])

    def plan(self, item):
        return self.service.list_plans(item_id=item["id"])[0]

    def test_level_and_tca_queueing_never_overdraws(self):
        # 同等级按 TCA 占用：先提交的 E1 碰撞时刻最晚，会被更紧急的后到方案挤掉
        e1 = assessed(self.service, deb="E1", tca="2026-10-12T00:00:00+00:00", distance=5)
        e2 = assessed(self.service, deb="E2", tca="2026-10-10T00:00:00+00:00", distance=5)
        e3 = assessed(self.service, deb="E3", tca="2026-10-11T00:00:00+00:00", distance=5)
        e1 = self.approve(e1, 6)   # 先单独排入
        self.assertEqual(self.plan(e1)["state"], "scheduled")
        e2 = self.approve(e2, 3)
        e3 = self.approve(e3, 3)   # 两个更紧急的各占 3，共 6
        self.assertEqual(self.plan(e2)["state"], "scheduled")
        self.assertEqual(self.plan(e3)["state"], "scheduled")
        # E1 成本 6 排不进剩余 4，按 TCA 全量重排后未排；余量绝不为负
        p1 = self.plan(e1)
        self.assertEqual(p1["state"], "unscheduled")
        self.assertEqual(p1["unscheduled_reason"], "fuel_capacity_unavailable")
        view = self.service.get_satellite("SAT-1")
        self.assertEqual(view["fuel_remaining_m_s"], 4.0)
        self.assertEqual(view["fuel_reserved_m_s"], 6.0)

    def test_higher_level_preempts_lower_level(self):
        low = assessed(self.service, deb="L", tca="2026-10-09T00:00:00+00:00", distance=2000)
        low = self.approve(low, 6)
        self.assertEqual(self.plan(low)["state"], "scheduled")
        high = assessed(self.service, deb="H", tca="2026-10-20T00:00:00+00:00", distance=5)
        high = self.approve(high, 7)  # 高等级即使 TCA 更晚也抢占
        self.assertEqual(self.plan(high)["state"], "scheduled")
        self.assertEqual(self.plan(low)["state"], "unscheduled")
        self.assertEqual(self.service.get_satellite("SAT-1")["fuel_remaining_m_s"], 3.0)

    def test_cost_above_quota_has_distinct_reason(self):
        plans = [
            {"id": 1, "state": "queued", "level": "high", "tca": "2026-10-10T00:00:00+00:00",
             "fuel_cost_m_s": 50, "seq": 0},
        ]
        changes = scheduling.recompute_queue(plans, quota=10)
        self.assertEqual(changes[1]["state"], "unscheduled")
        self.assertEqual(changes[1]["unscheduled_reason"], "fuel_cost_exceeds_quota")

    def test_concurrent_approval_only_one_wins(self):
        e1 = assessed(self.service, deb="D1")
        e2 = assessed(self.service, deb="D2", tca="2026-10-11T00:00:00+00:00")
        barrier = threading.Barrier(2)
        results = {}

        def approve(key, event):
            barrier.wait()
            try:
                self.service.act(event["id"], "approve",
                                 {"fuel_cost_m_s": 7, "maneuver_window": "w"},
                                 "c", "coordinator", event["version"], satellite_version=1)
                results[key] = "ok"
            except ConflictError as exc:
                results[key] = exc.code
            except Exception as exc:  # pragma: no cover - 不应发生
                results[key] = type(exc).__name__

        t1 = threading.Thread(target=approve, args=("A", e1))
        t2 = threading.Thread(target=approve, args=("B", e2))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results.values()), ["ok", "satellite_version_conflict"])
        view = self.service.get_satellite("SAT-1")
        self.assertEqual(view["fuel_reserved_m_s"], 7.0)
        self.assertEqual(view["fuel_remaining_m_s"], 3.0)

    def test_same_event_second_approve_is_version_conflict(self):
        item = assessed(self.service)
        v = item["version"]
        self.approve(item, 2)
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "approve",
                             {"fuel_cost_m_s": 2, "maneuver_window": "w2"},
                             "c", "coordinator", v, satellite_version=2)

    def test_revision_invalidates_unexecuted_and_promotes_waiters(self):
        high = assessed(self.service, deb="H", tca="2026-10-11T00:00:00+00:00", distance=5)
        low = assessed(self.service, deb="L", tca="2026-10-09T00:00:00+00:00", distance=2000)
        high = self.approve(high, 6)
        low = self.approve(low, 3)
        med = assessed(self.service, deb="M", tca="2026-10-12T00:00:00+00:00", distance=80)
        med = self.approve(med, 3)  # 抢占 low
        self.assertEqual(self.plan(low)["state"], "unscheduled")
        high = self.service.get_item(high["id"])
        high = self.service.report_revision(high["id"], {
            "observed_at": "2026-09-30T00:00:00+00:00",
            "miss_distance_m": 5000, "covariance_m": 100, "source": "radar",
        }, "analyst-1", "analyst", high["version"])
        self.assertEqual(self.plan(high)["state"], "invalidated")
        self.assertEqual(self.plan(high)["invalidation_reason"], "orbit_revised")
        self.assertEqual(high["status"], "assessed")
        self.assertEqual(self.plan(low)["state"], "scheduled")  # 释放后补位

    def test_issued_plan_keeps_basis_after_revision(self):
        item = assessed(self.service)
        item = self.approve(item, 3)
        plan_no = self.plan(item)["plan_no"]
        result = self.service.dispatch_plan(plan_no, "op", "operator", organization="Org-A")
        self.assertEqual(result["state"], "completed")
        basis_before = result["basis"]
        item = self.service.get_item(item["id"])
        self.service.report_revision(item["id"], {
            "observed_at": "2026-09-30T00:00:00+00:00",
            "miss_distance_m": 9, "covariance_m": 100, "source": "radar",
        }, "analyst-1", "analyst", item["version"])
        plan_after = self.service.get_plan(plan_no)
        self.assertEqual(plan_after["state"], "completed")
        self.assertEqual(plan_after["basis"], basis_before)

    def test_quota_change_invalidates_unexecuted(self):
        item = assessed(self.service)
        self.approve(item, 4)
        self.assertEqual(self.plan(item)["state"], "scheduled")
        version = self.service.get_satellite("SAT-1")["version"]
        self.service.adjust_fuel_quota("SAT-1", {"fuel_quota_m_s": 50},
                                       "coordinator-1", "coordinator", expected_version=version)
        self.assertEqual(self.plan(item)["state"], "invalidated")
        with self.assertRaises(ConflictError):
            self.service.adjust_fuel_quota("SAT-1", {"fuel_quota_m_s": 60},
                                           "coordinator-1", "coordinator", expected_version=version)

    def test_dispatch_retry_preserves_completed_steps_across_restart(self):
        item = assessed(self.service, sid="SAT-9", deb="D9")
        self.service.register_satellite(
            {"satellite_id": "SAT-9", "organization": "Org-A", "fuel_quota_m_s": 10},
            "coordinator-1", "coordinator")
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "steps": [
            {"step_type": "arm", "command_ref": "C0"},
            {"step_type": "burn", "command_ref": "C1"},
            {"step_type": "verify", "command_ref": "C2"},
        ]}, "coordinator-1", "coordinator", item["version"], satellite_version=1)
        plan_no = self.service.list_plans(item_id=item["id"])[0]["plan_no"]
        first = FailNth({1})
        self.service.transmitter = first
        r = self.service.dispatch_plan(plan_no, "op", "operator", organization="Org-A")
        self.assertEqual([s["status"] for s in r["steps"]], ["succeeded", "failed", "pending"])
        self.assertEqual(first.calls, [0, 1])
        # 重启：新 Service 实例（状态只来自 SQLite）
        restarted = Service(self.repo)
        second = FailNth(set())
        restarted.transmitter = second
        r = restarted.dispatch_plan(plan_no, "op", "operator", organization="Org-A")
        self.assertEqual([s["status"] for s in r["steps"]], ["succeeded", "succeeded", "succeeded"])
        self.assertEqual(second.calls, [1, 2])  # 已完成步骤不重发
        self.assertEqual(r["state"], "completed")

    def test_operator_scoped_to_own_organization(self):
        item = assessed(self.service, orgs=("Org-B",))
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "record_opinion",
                             {"operator": "Org-A", "opinion": "approve"},
                             "op", "operator", item["version"], organization="Org-A")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(DomainError) as ctx:
            self.service.dispatch_plan("PLAN-NA", "op", "operator", organization="Org-A")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
