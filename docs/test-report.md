# 测试报告

> 生成方式：`pytest -q --cov=app --cov-report=term-missing`
> 运行环境：Windows / Python 3.12.14 / SQLite（内存库，StaticPool）/ pytest 8.3.0
> 测试代码：`tests/unit`（纯函数、状态机、加密、算法边界）+ `tests/integration`（接口、数据范围、六条流程端到端）+ `tests/integration/test_web_smoke.py`（页面冒烟）

---

## 1. 总体结果（本轮最终运行）

| 指标 | 数值 |
|---|---|
| 测试用例数 | 340 |
| 通过 | 340 |
| 失败 / 错误 | 0 / 0 |
| 跳过 | 0 |
| 总耗时 | 约 256 秒 |
| 语句覆盖率 | **89%**（7401 条语句，841 条未覆盖） |

> 覆盖率明细见运行输出；`app/models/*` 与 `app/core/enums.py` 为 100%，`app/services/billing_service.py`、`stats_service.py` 均在 90% 左右，`archive_service.py` 89%、`appointment_service.py` 88%。

覆盖率较低的模块与原因（可接受）：
- `app/tasks/scheduler.py`（0%）：APScheduler 调度器只在进程启动时装配，单测不启动后台线程；任务函数本身（`alert_timeout` / `backup_daily` / `billing_monthly`）已单独覆盖 85% / 88% / 56%。
- `app/web/routes.py`（73%→）：页面路由以页面冒烟测试 + 手工页面验证覆盖，未逐行构造异常分支。

---

## 2. 按模块统计

| 模块 | 测试文件 | 用例数 | 覆盖重点 |
|---|---|---|---|
| 基座（安全/状态机/审计） | `unit/test_foundation.py` | 10 | JWT、AES-GCM、脱敏、哈希链篡改检测、状态机非法跳转 |
| 认证与系统管理 | `unit/test_auth_admin.py` / `integration/test_auth_api.py` / `integration/test_admin_api.py` | 18 + 9 + 12 | 登录/失败审计、Cookie、RBAC、组织层级、备份恢复核对 |
| 备份 | `unit/test_backup.py` | 7 | 在线备份、快照行数、30 版保留、恢复回滚、损坏备份拒绝 |
| 老人档案 | `unit/test_archive.py` / `integration/test_elders_api.py` | 9 + 9 | 密文落库、摘要检索、脱敏、越权 403、二次授权过期、变更日志 |
| 服务目录 | `unit/test_catalog.py` / `integration/test_catalog_api.py` | 8 + 6 | 价格版本化、调价不影响历史工单 |
| 预约 | `unit/test_appointment.py` / `integration/test_appointment_api.py` | 15 + 13 | 三重校验、119/121 分钟取消计费、状态机 |
| 派单 | `unit/test_dispatch.py` / `integration/test_dispatch_api.py` | 9 + 14 | 四因子、Top3、权重覆盖、技能/半径/负荷/冲突过滤、30 分钟顺延 |
| 执行与质量 | `unit/test_order.py` / `unit/test_quality.py` / `integration/test_orders_api.py` / `integration/test_quality_api.py` | 22 + 17 + 16 + 11 | 199/201 米签到、第 4 张照片、29/31 分钟时长、≤3 星 48h 回访、投诉 3 次降权 |
| 健康与预警 | `unit/test_health_service.py` / `unit/test_alert_rules.py` / `integration/test_health_api.py` / `integration/test_alerts_api.py` | 9 + 21 + 9 + 20 | 指标入库、趋势聚合、179/180 边界、连续 3 次、47.9/48.1 小时静默、冷却、升级 |
| 结算与统计 | `unit/test_billing.py` / `unit/test_stats_service.py` / `integration/test_billing_api.py` / `integration/test_stats_api.py` | 23 + 7 + 11 + 5 | 补贴上限、差异率 0.9/1.0/1.1%、红冲、收入占比、趋势同比、分布占比 |
| 端到端 | `integration/test_flows_e2e.py` | 7 | 六条业务流程 + 权限/审计/备份 |
| 页面冒烟 | `integration/test_web_smoke.py` | 23 | 登录/未登录跳转、角色菜单、13 个业务页面渲染、静态资源、/health 不被遮挡 |

---

## 3. 赛题关键边界覆盖清单

| 赛题要求 | 边界用例 | 结果 |
|---|---|---|
| 取消 <2 小时计费 | 119 分钟 → 计费；121 分钟 → 免费 | ✅ |
| 签到 200 米阈值 | 199 米通过；201 米无说明 400；201 米有说明通过 | ✅ |
| 照片 ≤3 张 | 第 4 张 400；压缩后落盘且尺寸受限 | ✅ |
| 时长偏差 30 分钟 | 29 分钟不标记；31 分钟标记异常 | ✅ |
| 血压越界阈值 | 179 不触发 / 180 触发红；91 不触发 / 89、90 触发红 | ✅ |
| 连续异常 | 第 2 次不触发 / 第 3 次触发黄色 | ✅ |
| 静默 48 小时 | 47.9 小时不触发 / 48.1 小时触发蓝 | ✅ |
| 红色预警升级 | 31 分钟后 scan → escalated_at + 升级动作留痕 | ✅ |
| 补贴月度上限 | 跨明细累计截断（第 6 笔补贴 0），跨账单累计 | ✅ |
| 差异率 1% | 0.9% 不复核 / 1.0% 不复核（严格 >）/ 1.1% needs_review 且 confirm 拦截 | ✅ |
| 时长 3 倍拦截 | 恰好 180 分钟不拦截 / 181 分钟拦截 | ✅ |
| 红冲 | 原单金额与状态不变，新单负数 | ✅ |
| 派单 Top3 与权重 | 权重改序后排序变化，四因子明细齐全 | ✅ |
| 越权数据范围 | grid2 查 grid1 老人 403 / 列表为空 | ✅ |
| 审计不可篡改 | 篡改任一记录 → verify_chain 返回断点 | ✅ |
| 备份恢复 | 恢复后记录数回到备份点，输出 before/after 核对报告 | ✅ |

---

## 4. 静态检查

```
ruff check app tests  →  All checks passed!
```

---

## 5. 手动验证（真实服务冒烟）

启动 `uvicorn app.main:app`（真实 SQLite 种子库，37 张表 / 1535 行演示数据）后验证：

| 检查项 | 结果 |
|---|---|
| `GET /health` | 200，JSON `{"status":"healthy"}` |
| `GET /login` | 200，登录页 2.2KB |
| `GET /docs` | 200，OpenAPI 文档可访问 |
| `POST /api/v1/auth/login`（grid1） | 200，role=grid，16 项权限 |
| `GET /api/v1/elders`（grid1） | 200，total=3（本网格可见） |
| `GET /monitoring`（带 Cookie） | 200，页面渲染正常 |

---

## 6. 复现命令

```powershell
Set-Location 'D:\Program Files (x86)\巅峰\python\eldercare-platform'
$env:TEMP=(Join-Path $PWD 'data\tmp'); $env:TMP=$env:TEMP; $env:PYTHONIOENCODING='utf-8'
& '.\.venv\Scripts\python.exe' -m pytest tests -q --cov=app --cov-report=term-missing
```
