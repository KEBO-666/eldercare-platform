# 开发契约（所有并行开发者必读，冻结后不得擅自修改）

> 本文件是「社区智慧养老服务与健康监测平台」并行开发的唯一协作协议。
> 任何需要改动本契约（模型、路由表、共享文件）的诉求，**先给 lead 发消息**，不要直接改。

---

## 0. 环境事实（先读，避免踩坑）

| 项 | 值 |
|---|---|
| 项目根目录 | `D:\Program Files (x86)\巅峰\python\eldercare-platform` |
| **唯一可用解释器** | `.venv\Scripts\python.exe`（Python 3.12.14 独立虚拟环境，已装全部依赖） |
| ❌ 禁止 | 系统 `python` / `py` / `python3`（系统 3.10 的 `pathlib` 被篡改，pytest 会崩）、任何 `pip install` |
| ❌ 禁止 | DSH 自带 `pwsh` 工具（本机 DLL 初始化失败），**一律用 `mcp__windows-mcp__PowerShell`** |
| 命令模板 | 先 `Set-Location 'D:\Program Files (x86)\巅峰\python\eldercare-platform'`，再 `& '.\.venv\Scripts\python.exe' ...` |
| 测试 | `& '.\.venv\Scripts\python.exe' -m pytest tests -q` |
| 静态检查 | `& '.\.venv\Scripts\python.exe' -m ruff check app tests` |
| 编码 | 全部 UTF-8；写文件用 write/edit 工具，不要用 PowerShell 写源码 |
| 常驻服务 | **不要**启动 uvicorn 常驻进程（端口冲突）；接口验证一律用 `TestClient` |

---

## 1. 文件所有权（禁止越界写入）

### Lead 冻结文件（任何人不许改）
```
app/core/config.py  app/core/enums.py  app/core/exceptions.py  app/core/response.py
app/core/security.py  app/core/state_machine.py  app/core/permissions.py
app/core/deps.py  app/core/audit.py
app/db/base.py  app/db/session.py
app/models/*.py（全部模型）
app/main.py  app/api/v1/router.py  app/tasks/scheduler.py
tests/conftest.py  pytest.ini  pyproject.toml  requirements.txt
docs/CONTRACTS.md
```

### 各模块负责人可写范围

| 负责人 | 可写文件 |
|---|---|
| A1 | `app/api/v1/auth.py`、`app/api/v1/admin.py`、`app/services/auth_service.py`、`app/services/admin_service.py`、`app/tasks/backup_daily.py`、`tests/unit/test_auth_admin.py`、`tests/unit/test_backup.py`、`tests/integration/test_auth_api.py`、`tests/integration/test_admin_api.py` |
| A2 | `app/api/v1/elders.py`、`app/services/archive_service.py`、`app/schemas/elder.py`、`tests/unit/test_archive.py`、`tests/integration/test_elders_api.py` |
| B | `app/api/v1/catalog.py`、`app/api/v1/appointments.py`、`app/api/v1/dispatch.py`、`app/services/catalog_service.py`、`app/services/appointment_service.py`、`app/services/dispatch_service.py`、`app/algorithms/dispatch.py`、`tests/unit/test_catalog.py`、`tests/unit/test_appointment.py`、`tests/unit/test_dispatch.py`、`tests/integration/test_catalog_api.py`、`tests/integration/test_appointment_api.py`、`tests/integration/test_dispatch_api.py` |
| C | `app/api/v1/orders.py`、`app/api/v1/quality.py`、`app/services/order_service.py`、`app/services/quality_service.py`、`tests/unit/test_order.py`、`tests/unit/test_quality.py`、`tests/integration/test_orders_api.py`、`tests/integration/test_quality_api.py` |
| D | `app/api/v1/health.py`、`app/api/v1/alerts.py`、`app/services/health_service.py`、`app/services/alert_service.py`、`app/algorithms/alert_rules.py`、`app/tasks/alert_timeout.py`、`tests/unit/test_health_alert.py`、`tests/integration/test_health_api.py`、`tests/integration/test_alerts_api.py` |
| E | `app/api/v1/billing.py`、`app/api/v1/stats.py`、`app/services/billing_service.py`、`app/services/stats_service.py`、`app/algorithms/subsidy.py`、`app/tasks/billing_monthly.py`、`tests/unit/test_billing.py`、`tests/integration/test_billing_api.py`、`tests/integration/test_stats_api.py` |
| F | `app/web/routes.py`、`app/templates/**`、`app/static/**`、`scripts/seed.py`、`scripts/run_dev.ps1`、`README.md`、`docker/**`、`tests/integration/test_web_smoke.py` |

> 需要新建的 service 文件按上表命名；不要两人共写一个文件。

---

## 2. 代码约定

### 2.1 分层
- **路由层** `app/api/v1/*.py`：参数校验 + 依赖注入 + 调 service + `return ok(...)`；不写业务规则。
- **服务层** `app/services/*_service.py`：业务规则、状态机流转、审计、通知、事务（`db.commit()`）。
- **算法层** `app/algorithms/*.py`：纯函数（输入 dict/list/数值，输出 dict/数值），不碰 DB，便于单测。
- **模型层**：已冻结，直接 import 使用。

### 2.2 统一响应
```python
from app.core.response import ok, paged
return ok({"id": 1})                    # {"code":0,"message":"ok","data":{...}}
return paged(items, total, page, size)  # data.items / data.total / data.page ...
```

### 2.3 异常
```python
from app.core.exceptions import NotFound, Forbidden, Conflict, RuleViolation, NeedsManual
raise RuleViolation("同一时段已存在预约")   # → HTTP 400, code=RULE_VIOLATION
```
禁止返回裸 dict 表达错误，禁止 `raise HTTPException`。

### 2.4 时间与金额
- 时间统一 naive `datetime`（本地时区，系统即东八区）；测试中用 `freezegun.freeze_time`。
- 金额用 `Decimal`（`Numeric(10,2)` 字段），比例/距离用 `float`。
- 分账取整：四舍五入到分（`quantize(Decimal("0.01"))`）。

### 2.5 分页
```python
def list_xxx(page: int = 1, size: int = 20, db: Session = Depends(get_db), ...):
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = db.execute(stmt.order_by(...).offset((page-1)*size).limit(size)).scalars().all()
```

---

## 3. 认证、权限、数据范围、审计

### 3.1 登录（A1 实现）
- `POST /api/v1/auth/login` body `{"username","password"}` → `data: {token, user:{id,username,real_name,role,role_name,org_id,data_scope,permissions}}`
- 同时 `Set-Cookie: access_token=...; path=/; httponly`（供页面使用）
- `GET /api/v1/auth/me` 返回当前用户；`POST /api/v1/auth/logout` 清 cookie。

### 3.2 鉴权写法
```python
from app.core.deps import get_current_user, require
user: User = Depends(require("elder:read"))     # 同时返回用户对象
user: User = Depends(get_current_user)          # 仅登录即可
```
权限码只能取 `app/core/permissions.py` 中定义的，共 35 个。

### 3.3 数据范围
```python
from app.core.deps import apply_scope, ensure_elder_visible, visible_elder_ids
stmt = apply_scope(select(WorkOrder), WorkOrder, user, db)
ensure_elder_visible(db, user, elder_id)   # 越权抛 403
```
规则：super/street 不限；org_admin 按 `community_org_id/org_id`；grid 按 `grid_org_id`；nurse 按 `nurse_id`；elder 按 `elder_id`。

### 3.4 敏感字段
```python
from app.core.security import encrypt_field, decrypt_field, field_digest, mask_id_card, mask_phone
elder.id_card_enc = encrypt_field(raw); elder.id_card_hash = field_digest(raw)
```
列表默认脱敏；查看明文必须 `AccessGrant`（本人、10 分钟有效、理由必填、留痕）。

### 3.5 审计
```python
from app.core.audit import write_audit
write_audit(db, user=user, action="update", entity_type="elder", entity_id=elder.id,
            before=before_dict, after=after_dict, request=request)
```
必须记录的动作：登录/登录失败、档案变更、导出、派单/改派、退费、删除、备份/恢复、二次授权。

---

## 4. 状态机（强制）

```python
from app.core.state_machine import APPOINTMENT, ORDER, ALERT, COMPLAINT, BILL
new_status = APPOINTMENT.next(appt.status, "accept")   # 非法流转抛 Conflict(409)
appt.status = new_status
```
- 不得直接赋状态值绕过 `next()`。
- 伴随流转必须写日志表：`AppointmentLog`（预约）、`AlertAction`（预警）；工单/投诉/账单在服务层记录。
- 状态枚举见 `app/core/enums.py`（值即字符串）。

---

## 5. 冻结 API 路由表（前端与测试都按此实现）

> 统一前缀 `/api/v1`；除登录外都需要 Bearer Token；响应统一 `{code,message,data}`。

### auth（A1）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/auth/login` | 登录 | 无 |
| POST | `/auth/logout` | 登出 | 登录 |
| GET | `/auth/me` | 当前用户 | 登录 |

### elders（A2）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/elders` | 列表（keyword/ability_level/tag/grid_id/community_id/page/size） | elder:read |
| POST | `/elders` | 新增 | elder:write |
| GET | `/elders/{id}` | 详情（脱敏） | elder:read |
| PUT | `/elders/{id}` | 编辑（写变更日志） | elder:write |
| DELETE | `/elders/{id}` | 逻辑删除 | elder:delete |
| GET | `/elders/{id}/changes` | 档案变更日志 | elder:read |
| POST | `/elders/{id}/grants` | 申请二次授权 `{fields,reason}` | elder:grant |
| GET | `/elders/{id}/sensitive` | 凭 grant 查明文 `?grant_id=` | elder:grant |
| GET/POST | `/elders/{id}/tags` | 标签查询/新增 | elder:read / elder:write |
| GET/POST | `/elders/{id}/assessments` | 能力评估记录/新增 | elder:read / elder:write |
| POST | `/elders/import` | 批量导入（JSON 数组） | elder:write |
| GET | `/elders/export` | 导出 CSV | elder:export |

### catalog（B）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/catalog/items` | 项目列表 | catalog:read |
| POST | `/catalog/items` | 新建项目（自动建 v1 价格） | catalog:write |
| PUT | `/catalog/items/{id}` | 编辑（不含价格） | catalog:write |
| POST | `/catalog/items/{id}/status` | 上下架 `{status:on/off}` | catalog:write |
| GET | `/catalog/items/{id}/prices` | 价格版本列表 | catalog:read |
| POST | `/catalog/items/{id}/prices` | 新增价格版本 `{unit_price,effective_from}` | catalog:write |

### appointments（B）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/appointments` | 提交预约（三重校验） | appointment:create |
| GET | `/appointments` | 列表（status/elder_id/日期） | appointment:read |
| GET | `/appointments/{id}` | 详情（含状态日志） | appointment:read |
| POST | `/appointments/{id}/accept` | 受理 | appointment:accept |
| POST | `/appointments/{id}/cancel` | 取消 `{reason}`（2 小时计费规则） | appointment:cancel |
| POST | `/appointments/{id}/to-dispatch` | 转待派单 | appointment:accept |

### dispatch（B）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/dispatch/{appointment_id}/candidates` | 前 N 候选 + 四项因子明细 | order:dispatch |
| POST | `/dispatch/{appointment_id}/assign` | 派单 `{nurse_id}` → 生成工单 | order:dispatch |
| POST | `/dispatch/orders/{order_id}/reassign` | 改派 `{nurse_id, reason}` | order:reassign |
| POST | `/dispatch/scan-timeout` | 手动触发 30 分钟未接单顺延 | order:dispatch |

### orders（C）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/orders` | 列表（status/elder_id/nurse_id/日期） | order:read |
| GET | `/orders/{id}` | 详情（含项目勾选/照片/轨迹） | order:read |
| POST | `/orders/{id}/accept` | 接单 | order:execute |
| POST | `/orders/{id}/reject` | 拒单 `{reason}` | order:execute |
| POST | `/orders/{id}/checkin` | 签到 `{lat,lng,note?}`（200 米） | order:execute |
| POST | `/orders/{id}/items` | 逐项勾选 `{items:[{name,completed}]}` | order:execute |
| POST | `/orders/{id}/photos` | 上传照片（multipart file，≤3 张，压缩） | order:execute |
| POST | `/orders/{id}/checkout` | 签退 `{summary, metrics?}` → 时长异常判定 | order:execute |
| POST | `/orders/{id}/anomaly` | 上报异常 `{anomaly_type,description}` | order:execute |

### health（D）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/health/metrics` | 单条录入 | health:write |
| POST | `/health/metrics/batch` | 批量/可穿戴上报 | health:write |
| GET | `/health/elders/{elder_id}/metrics` | 指标列表（code/时间范围） | health:read |
| GET | `/health/elders/{elder_id}/series` | 趋势序列（按日聚合） | health:read |
| GET/POST | `/health/devices` | 设备列表/绑定 | health:read / health:write |

### alerts（D）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/alerts` | 列表（level/status/elder_id） | alert:read |
| GET | `/alerts/{id}` | 详情 + 处置流水 | alert:read |
| POST | `/alerts/{id}/handle` | 闭环 `{resolution_type, measure, note}` | alert:handle |
| POST | `/alerts/{id}/ack` | 确认响应（记录响应时长） | alert:handle |
| GET/POST/PUT | `/alerts/rules`、`/alerts/rules/{id}` | 规则查询/新建/编辑 | alert:read / alert:rule |
| POST | `/alerts/scan` | 手动触发超时升级 + 静默检测 | alert:rule |
| GET | `/alerts/stats` | 响应中位数、误报率等 | alert:read |

### quality（C）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/quality/evaluations` | 提交评价（4 项 1-5 星） | quality:evaluate |
| GET | `/quality/evaluations` | 列表 | quality:read |
| GET | `/quality/callbacks` | 回访任务 | quality:read |
| POST | `/quality/callbacks/{id}/finish` | 完成回访 `{result,note}` | quality:handle |
| POST | `/quality/complaints` | 投诉登记 | quality:read |
| GET | `/quality/complaints` | 列表 | quality:read |
| POST | `/quality/complaints/{id}/assign` | 指派 `{handler_id}` | quality:handle |
| POST | `/quality/complaints/{id}/conclude` | 结论 `{conclusion,is_established,resolution}` | quality:handle |
| GET | `/quality/performance` | 月度绩效榜 `?period=` | quality:read |

### billing（E）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/billing/generate` | 生成月度账单 `{period}` | bill:generate |
| GET | `/billing/bills` | 账单列表 | bill:read |
| GET | `/billing/bills/{id}` | 账单详情 + 差异清单 | bill:read |
| POST | `/billing/bills/{id}/submit` | 提交复核 | bill:generate |
| POST | `/billing/bills/{id}/confirm` | 复核通过（差异>1% 拦截） | bill:review |
| POST | `/billing/bills/{id}/pay` | 登记缴费 | bill:pay |
| POST | `/billing/bills/{id}/reverse` | 红冲 | bill:refund |
| GET/POST | `/billing/diffs`、`/billing/diffs/{id}/handle` | 差异清单/处理 | bill:read / bill:review |
| GET | `/billing/bills/{id}/export` | 导出 Excel | bill:read |

### stats（E）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/stats/community/overview` | 社区看板 | stats:read |
| GET | `/stats/org/overview` | 机构看板 | stats:read |
| GET | `/stats/street/overview` | 街道看板 | stats:read |
| GET | `/stats/trend` | 趋势 `?metric=&from=&to=&granularity=` | stats:read |
| GET | `/stats/ranking` | 排行 `?type=nurse/community` | stats:read |
| GET | `/stats/distribution` | 分布 `?type=satisfaction/ability` | stats:read |
| GET | `/stats/export` | 导出报表 `?report=&from=&to=` | stats:export |

### admin（A1）
| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET/POST | `/admin/users`、`PUT /admin/users/{id}`、`POST /admin/users/{id}/reset-password` | 用户管理 | admin:user |
| GET | `/admin/roles`、`/admin/permissions` | 角色权限 | admin:user |
| GET/POST | `/admin/orgs` | 组织 | admin:org |
| GET | `/admin/audit-logs` | 审计查询（action/entity/时间） | admin:audit |
| GET | `/admin/audit/verify` | 审计链校验 | admin:audit |
| POST | `/admin/backup`、`GET /admin/backups`、`POST /admin/restore` | 备份与恢复 | admin:backup |
| GET/PUT | `/admin/config` | 运行期配置（派单权重等） | admin:config |

### 页面（F）
`/login`、`/`、`/elders`、`/elders/{id}`、`/appointments`、`/dispatch`、`/orders`、`/orders/{id}`、`/health/{elder_id}`、`/alerts`、`/quality`、`/billing`、`/stats`、`/admin/users`、`/admin/audit`、`/admin/backup`、`/admin/config`

---

## 6. 测试要求

- 每个模块至少 6 个单测 + 3 个接口集成测试；核心边界必须覆盖。
- 使用 `tests/conftest.py` 已有夹具：`client`、`db`、`orgs`、`rbac`、`accounts`、`headers`、`login`、`make_elder`、`make_staff`。
- 示例：
```python
def test_create_appointment(client, headers, accounts):
    resp = client.post("/api/v1/appointments", json={...}, headers=headers("elder"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] == 0
```
- 断言错误：`resp.json()["code"] == "RULE_VIOLATION"` 等。
- 提交前自查：`pytest tests -q` 你自己的文件全绿 + `ruff check` 无告警。

---

## 7. 交付与沟通

1. 完成后 `send_message` 给 `lead`，内容：完成文件清单 / 接口清单 / 测试结果（通过数）/ 遗留问题。
2. 任何跨模块需求（改模型、改契约、改共享文件）→ 先发消息等确认。
3. 不要等待他人；你负责的模块内如需他模块数据，用已有模型直接查询。
4. 若发现别人代码 bug，不要直接改对方文件，发消息给 lead 或在报告中说明。
