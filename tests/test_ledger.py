import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _item(self, satellite, secondary, tca, budget, distance=120):
        return self.service.create_item({
            "primary_object_id": satellite,
            "secondary_object_id": secondary,
            "tca": tca,
            "miss_distance_m": distance,
            "covariance_m": 100,
            "fuel_budget_m_s": budget,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A"],
        }, "analyst-1", "analyst")

    def _assess(self, item, hours=18):
        return self.service.act(item["id"], "assess", {"hours_to_tca": hours},
                                "analyst-1", "analyst", item["version"])

    def _approve(self, item, cost, fuel_version=None, actor="coord-1"):
        return self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": cost,
            "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z",
        }, actor, "coordinator", item["version"], None, None, fuel_version)

    def _fuel_version(self, satellite):
        return self.service.get_fuel_account(satellite)["version"]

    def test_fuel_shared_across_events_and_unscheduled_reason(self):
        i1 = self._assess(self._item("SAT-1", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        i2 = self._assess(self._item("SAT-1", "DEB-2", "2026-09-29T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-1")

        self._approve(i1, 6, v)
        v = self._fuel_version("SAT-1")
        fuel = self.service.get_fuel_account("SAT-1")
        self.assertEqual(fuel["fuel_occupied_m_s"], 6)
        self.assertEqual(fuel["fuel_quota_m_s"] - fuel["fuel_consumed_m_s"] - fuel["fuel_occupied_m_s"], 4)

        with self.assertRaises(DomainError) as ctx:
            self._approve(i2, 5, v)
        self.assertEqual(ctx.exception.code, "plan_unscheduled")

        # 余量不能被冲成负数：第二笔未排，占用仍是 6
        fuel = self.service.get_fuel_account("SAT-1")
        self.assertEqual(fuel["fuel_occupied_m_s"], 6)
        self.assertEqual(fuel["fuel_consumed_m_s"], 0)

        # 未排方案落账，状态与原因可查
        i2 = self.service.get_item(i2["id"])
        self.assertEqual(i2["plan"]["status"], "unscheduled")
        self.assertEqual(i2["plan"]["unscheduled_reason"], "fuel_capacity_exceeded")
        self.assertEqual(i2["status"], "assessed")

    def test_plans_queue_by_level_and_tca(self):
        low = self._assess(self._item("SAT-2", "DEB-1", "2026-09-29T12:00:00+00:00", 10, distance=2000), hours=40)
        high_late = self._assess(self._item("SAT-2", "DEB-2", "2026-09-30T12:00:00+00:00", 10, distance=10), hours=6)
        high_early = self._assess(self._item("SAT-2", "DEB-3", "2026-09-28T12:00:00+00:00", 10, distance=10), hours=6)
        v = self._fuel_version("SAT-2")
        self._approve(low, 2, v)
        v = self._fuel_version("SAT-2")
        self._approve(high_late, 2, v)
        v = self._fuel_version("SAT-2")
        self._approve(high_early, 2, v)

        queue = self.service.list_plans("SAT-2")
        order = [(p["level"], p["tca"]) for p in queue]
        self.assertEqual(order[0][0], "high")
        self.assertEqual(order[1][0], "high")
        self.assertLess(order[0][1], order[1][1])  # high 中碰撞时刻早的在前
        self.assertEqual(order[2][0], "low")

    def test_revision_invalidates_unexecuted_and_releases_fuel(self):
        i1 = self._assess(self._item("SAT-3", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        i2 = self._assess(self._item("SAT-3", "DEB-2", "2026-09-29T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-3")
        self._approve(i1, 6, v)
        v = self._fuel_version("SAT-3")
        self._approve(i2, 4, v)

        # 下发 i1 的指令（占用转消耗，不可回退）
        i1 = self.service.get_item(i1["id"])
        self.service.act(i1["id"], "execute", {"command_ref": "CMD-1"},
                         "op-1", "operator", i1["version"], None, None, self._fuel_version("SAT-3"))
        fuel = self.service.get_fuel_account("SAT-3")
        self.assertEqual(fuel["fuel_consumed_m_s"], 6)
        self.assertEqual(fuel["fuel_occupied_m_s"], 4)

        # 轨道修订：未执行的 i2 方案失效并释放占用；已下发的 i1 保留原依据
        i1 = self.service.get_item(i1["id"])
        self.service.act(i1["id"], "report_revision", {
            "observed_at": "2026-09-27T00:00:00+00:00",
            "miss_distance_m": 50,
            "covariance_m": 100,
            "source": "radar",
        }, "analyst-1", "analyst", i1["version"])

        fuel = self.service.get_fuel_account("SAT-3")
        self.assertEqual(fuel["fuel_occupied_m_s"], 0)
        self.assertEqual(fuel["fuel_consumed_m_s"], 6)  # 已下发指令依据保留

        i2 = self.service.get_item(i2["id"])
        self.assertEqual(i2["plan"]["status"], "invalidated")
        self.assertEqual(i2["status"], "assessed")
        i1 = self.service.get_item(i1["id"])
        self.assertEqual(i1["plan"]["status"], "executed")
        self.assertEqual(i1["status"], "executing")

    def test_fuel_quota_change_invalidates_and_releases(self):
        i1 = self._assess(self._item("SAT-4", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-4")
        self._approve(i1, 6, v)
        fuel = self.service.get_fuel_account("SAT-4")
        self.assertEqual(fuel["fuel_occupied_m_s"], 6)

        self.service.set_fuel_quota("SAT-4", {"fuel_quota_m_s": 3}, "analyst-1", "analyst")
        fuel = self.service.get_fuel_account("SAT-4")
        self.assertEqual(fuel["fuel_quota_m_s"], 3)
        self.assertEqual(fuel["fuel_occupied_m_s"], 0)
        i1 = self.service.get_item(i1["id"])
        self.assertEqual(i1["plan"]["status"], "invalidated")

        # 额度调小后重新批准，按新额度占用
        i1 = self.service.get_item(i1["id"])
        i1 = self.service.act(i1["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", i1["version"])
        self._approve(i1, 2, self._fuel_version("SAT-4"))
        fuel = self.service.get_fuel_account("SAT-4")
        self.assertEqual(fuel["fuel_occupied_m_s"], 2)

    def test_fuel_version_conflict_reread_succeeds(self):
        i1 = self._assess(self._item("SAT-5", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        i2 = self._assess(self._item("SAT-5", "DEB-2", "2026-09-29T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-5")

        # 两人同时提交同一卫星的批准：先提交的成功
        self._approve(i1, 6, v)
        # 后提交的按燃料账户版本冲突重读
        with self.assertRaises(ConflictError) as ctx:
            self._approve(i2, 4, v)
        self.assertEqual(ctx.exception.code, "version_conflict")

        # 重读后以新版本提交成功
        i2 = self.service.get_item(i2["id"])
        self._approve(i2, 4, self._fuel_version("SAT-5"))
        fuel = self.service.get_fuel_account("SAT-5")
        self.assertEqual(fuel["fuel_occupied_m_s"], 10)

    def test_dispatch_failure_retry_keeps_completed_steps(self):
        i1 = self._assess(self._item("SAT-6", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-6")
        self._approve(i1, 6, v)
        i1 = self.service.get_item(i1["id"])

        # 首次下发在第 1 步(transmit_command)失败：第 0 步已完成，方案仍占用
        with self.assertRaises(DomainError) as ctx:
            self.service.act(i1["id"], "execute", {"command_ref": "CMD-1", "fail_step": 1},
                             "op-1", "operator", i1["version"], None, None, self._fuel_version("SAT-6"))
        self.assertEqual(ctx.exception.code, "dispatch_failed")
        i1 = self.service.get_item(i1["id"])
        self.assertEqual(i1["status"], "coordinating")
        self.assertEqual(i1["plan"]["status"], "queued")
        disp = i1["dispatch"]
        self.assertEqual(disp["status"], "failed")
        self.assertEqual(disp["current_step"], 1)
        self.assertEqual(disp["steps"][0]["status"], "done")
        self.assertEqual(disp["steps"][1]["status"], "pending")

        # 按方案号重试：从未完成步骤继续，已完成步骤保留
        plan_no = i1["plan"]["plan_no"]
        i1 = self.service.retry_dispatch(plan_no, "op-1", "operator")
        self.assertEqual(i1["status"], "executing")
        self.assertEqual(i1["plan"]["status"], "executed")
        disp = i1["dispatch"]
        self.assertEqual(disp["status"], "completed")
        self.assertTrue(all(s["status"] == "done" for s in disp["steps"]))
        fuel = self.service.get_fuel_account("SAT-6")
        self.assertEqual(fuel["fuel_consumed_m_s"], 6)
        self.assertEqual(fuel["fuel_occupied_m_s"], 0)

    def test_dispatch_failure_after_issued_command_retains_basis(self):
        i1 = self._assess(self._item("SAT-7", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-7")
        self._approve(i1, 6, v)
        i1 = self.service.get_item(i1["id"])

        # 下发指令已发出(第1步完成)，第2步(await_receipt)失败
        with self.assertRaises(DomainError):
            self.service.act(i1["id"], "execute", {"command_ref": "CMD-2", "fail_step": 2},
                             "op-1", "operator", i1["version"], None, None, self._fuel_version("SAT-7"))
        i1 = self.service.get_item(i1["id"])
        self.assertEqual(i1["status"], "executing")
        self.assertEqual(i1["plan"]["status"], "executed")  # 已下发指令保留原依据
        disp = i1["dispatch"]
        self.assertEqual(disp["status"], "failed")
        self.assertEqual(disp["current_step"], 2)

        # 重试仅补做未完成的回执步骤
        plan_no = i1["plan"]["plan_no"]
        i1 = self.service.retry_dispatch(plan_no, "op-1", "operator")
        self.assertEqual(i1["dispatch"]["status"], "completed")

    def test_operator_cross_org_forbidden(self):
        i1 = self._assess(self._item("SAT-8", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        # 本机构意见可以提交
        self.service.act(i1["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"},
                         "op-a", "operator", i1["version"], None, "Org-A")
        # 越权机构返回 403
        with self.assertRaises(DomainError) as ctx:
            self.service.act(i1["id"], "record_opinion", {"operator": "Org-B", "opinion": "approve"},
                             "op-b", "operator", i1["version"], None, "Org-B")
        self.assertEqual(ctx.exception.status, 403)

    def test_dispatch_resumes_after_restart(self):
        i1 = self._assess(self._item("SAT-9", "DEB-1", "2026-09-28T12:00:00+00:00", 10))
        v = self._fuel_version("SAT-9")
        self._approve(i1, 6, v)
        i1 = self.service.get_item(i1["id"])
        with self.assertRaises(DomainError):
            self.service.act(i1["id"], "execute", {"command_ref": "CMD-3", "fail_step": 1},
                             "op-1", "operator", i1["version"], None, None, self._fuel_version("SAT-9"))
        plan_no = self.service.get_item(i1["id"])["plan"]["plan_no"]

        # 模拟重启：新建 Service/Repository 指向同一数据库文件，下发账仍在
        repo2 = Repository(self.tmp.name)
        service2 = Service(repo2)
        plan = service2.get_plan(plan_no)
        self.assertEqual(plan["dispatch"]["status"], "failed")
        self.assertEqual(plan["dispatch"]["current_step"], 1)
        i1 = service2.retry_dispatch(plan_no, "op-1", "operator")
        self.assertEqual(i1["dispatch"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
