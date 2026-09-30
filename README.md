# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

卫星燃料、接近事件、规避方案和指令回执接成一本**调度账**：方案按风险等级
（high > medium > low）和碰撞时刻 TCA 排队占用卫星燃料余量，余量不足返回未排
原因；轨道修订或燃料额度变化后，未执行方案立即失效并释放占用，已下发指令保留
原依据；同一事件/卫星的并发批准通过乐观版本保证只有一方成功；指令下发按方案号
重试未完成步骤，已完成步骤与回执保留，进程重启后可继续。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/scheduling.py`：方案等级/TCA 排队、容量占用与未排原因（纯函数）。
- `src/repository.py`：SQLite、事务、乐观版本、方案/步骤/燃料账和审计链。
- `src/service.py`：身份、机构权限、用例编排和指令下发重试。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

身份使用 `X-User-Id`、`X-Role` 请求头；运营方（operator）还必须带
`X-Organization`，且只能处理本机构的接近事件/卫星/方案，越权返回 403。

## 接口

- `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`
- `POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`
- `POST /api/satellites`：登记卫星燃料账户（`satellite_id`、`organization`、`fuel_quota_m_s`）
- `GET /api/satellites`、`GET /api/satellites/<id>`、`GET /api/satellites/<id>/ledger`
- `POST /api/satellites/<id>/quota`：调整燃料额度（需 `expected_version`），未执行方案失效后重排
- `GET /api/plans/<plan_no>`、`GET /api/items/<id>/plans`
- `POST /api/plans/<plan_no>/dispatch`：按方案号下发/重试未完成步骤

批准（`action=approve`）即提交规避方案，除事件 `expected_version` 外还需
`satellite_version`（先 `GET /api/satellites/<id>` 读余量）。方案支持单窗口
（`maneuver_window`）或多步骤 `steps: [{step_type, command_ref, payload}]`。
并发的另一方收到 409 `version_conflict` / `satellite_version_conflict` 后重读再提交。

方案状态：`scheduled`（已排入并占用）、`unscheduled`（余量不足，带
`unscheduled_reason`）、`dispatching`（已下发部分步骤）、`completed`、
`invalidated`（轨道修订/额度变化导致，带 `invalidation_reason`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 smoke.py   # 端到端冒烟：排队、并发、修订、额度、重试、越权
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见
冲突，以及燃料排队占用、抢占与未排原因、并发批准互斥、轨道修订/额度变化失效、
指令失败重试与重启续跑、机构越权。数据使用 SQLite 持久化；规则是可运行的演示
模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
