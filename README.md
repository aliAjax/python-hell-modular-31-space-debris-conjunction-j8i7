# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role`、`X-Org-Id` 请求头。

## 调度账

卫星燃料、接近事件、规避方案与指令回执在同一本调度账里联动：

- **燃料账户**：按卫星（`primary_object_id`）建立，同一颗卫星的多个事件共享同一额度。`GET /api/state` 返回各卫星燃料账户，`GET /api/satellites/<id>/fuel` 可查单颗。
- **方案排队与占用**：批准（`approve`）时按等级（high/medium/low）与碰撞时刻（TCA）排队并占用燃料容量；占用量 = 额度 − 已消耗 − 已占用。余量不足时方案落账为 `unscheduled` 并返回 `plan_unscheduled` 与未排原因（`fuel_capacity_exceeded` 或 `fuel_budget_exceeded`），绝不把余量冲成负数。
- **版本冲突重读**：燃料账户带乐观版本。批准需传 `expected_fuel_version`；两人同时提交同一卫星的批准时，先提交者成功，后提交者收到 `version_conflict`，重读后以新版本重试。
- **失效与释放**：轨道修订（`report_revision`）或燃料额度变化（`POST /api/satellites/<id>/fuel`）后，未执行方案立即失效并释放占用；已下发指令的方案保留原依据（燃料记为消耗，不回退）。
- **下发重试与续跑**：下发指令（`execute`）按 `reserve_fuel` → `transmit_command` → `await_receipt` 步骤持久化。失败后按方案号 `POST /api/plans/<plan_no>/retry` 重试，已完成步骤保留、从未完成步骤继续；状态落 SQLite，重启后可继续。
- **机构归属**：运营方（`X-Org-Id`）只能处理本机构（`operating_organizations`）的事件，越权返回 403。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、跨机构 403、燃料共享与未排原因、方案排队、轨道修订/额度变化失效释放、燃料版本冲突重读、下发失败重试与已完成步骤保留、已下发指令保留依据，以及重启后续跑。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
