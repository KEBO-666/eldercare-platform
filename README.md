# 社区智慧养老服务与健康监测平台

> 面向社区居家养老场景的一体化平台：老人档案（敏感字段加密 + 二次授权）、能力评估、服务目录与价格版本、
> 预约三重校验、智能派单（四因子加权 Top3）、上门服务全流程留痕、健康数据接入与三级预警、
> 评价投诉与回访、费用结算与政府补贴、统计看板、系统管理与审计（哈希链）。

- **后端**：FastAPI + SQLAlchemy 2.0 + Pydantic v2 + SQLite / PostgreSQL
- **前端**：Jinja2 服务端渲染 + 页面内 `fetch` 调用 REST API + Bootstrap 5 + ECharts 5（**全部本地静态资源，断网可演示**）
- **安全**：JWT + RBAC（35 个权限点 / 6 类角色）+ 四级数据范围 + AES-256-GCM 字段加密 + HMAC 摘要检索 + 审计哈希链
- **适老化**：正文 18px+、按钮 ≥44px、高对比配色、表格斑马纹、红色预警置顶、键盘可访问

---

## 一、快速开始（Windows · 演示环境）

```powershell
# 0) 进入项目根目录
cd 'D:\Program Files (x86)\巅峰\python\eldercare-platform'

# 1) 创建并激活虚拟环境（已存在可跳过）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) 安装依赖
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt

# 3) 建库 + 写入演示数据（幂等，可重复执行）
& '.\.venv\Scripts\python.exe' -m scripts.seed

# 4) 启动服务
powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1
```

打开 <http://127.0.0.1:8000> 即可登录（默认密码 **`Admin@123`**）；接口文档 <http://127.0.0.1:8000/docs>。

> `scripts/run_dev.ps1` 会自动把 `TEMP/TMP` 指向项目内 `data\tmp`、缺库时自动执行种子脚本，
> 然后以 `uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload` 启动。

### 种子脚本常用命令

| 命令 | 说明 |
|---|---|
| `python -m scripts.seed` | 首次初始化；已初始化则幂等跳过交易数据（主数据仍幂等补齐） |
| `python -m scripts.seed --force` | 清空交易类演示数据后重新生成（保留账号/老人/目录等主数据） |
| `python -m scripts.seed --reset` | 删除 SQLite 演示库文件后全量重建（危险） |

种子数据规模（实测）：组织 10（1 街道 + 3 社区 + 6 网格）、账号 33、老人 20、护理员 8、服务项目 10（13 个价格版本）、
补贴政策 12、预约 30、工单 120、健康指标 200、预警 6（规则 4）、评价 74、回访 12、投诉 3、账单 2（明细 16/差异 2）、
审计日志 7、配置项 9、工单照片 6。数据使用固定随机种子（`20261108`），可复现。

---

## 二、默认账号（密码统一 `Admin@123`）

| 用户名 | 角色 | 数据范围 | 主要功能 |
|---|---|---|---|
| `super` | 系统超级管理员 | 全街道 | 全部功能 + 用户/组织/审计/备份/系统配置 |
| `street` | 街道监管员 | 全街道 | 监管看板、审计日志、备份恢复、账单复核 |
| `orgadmin` | 服务机构管理员 | 本机构/社区 | 运营看板、账单复核、用户管理、投诉处理 |
| `grid1` / `grid2` | 社区网格员 | 本网格 | 老人档案、预约受理、**智能派单**、预警处置 |
| `nurse1` … `nurse8` | 护理员 | 本人 | 我的工单、签到签退、健康录入、异常上报 |
| `elder1` … `elder20` | 老人 / 家属 | 本人 | 我的预约、我的工单、健康趋势、预警、账单、评价 |

> 密码在 `scripts/seed.py` 中统一为 `Admin@123`；生产环境请立即修改 `SECRET_KEY` / `ENCRYPTION_KEY` / `FIELD_HASH_KEY`。

---

## 三、功能清单

| 模块 | 能力 |
|---|---|
| **A1 认证与系统管理** | 登录（JWT + HttpOnly Cookie）、登出、当前用户；用户 CRUD/停用/重置密码、组织树、审计日志（哈希链校验）、运行期配置、备份与按时间点恢复（二次确认 + 完整性校验 + 逐表核对） |
| **A2 老人档案与评估** | 列表/筛选/分页/数据范围过滤、默认脱敏；身份证/手机/住址 AES 加密 + HMAC 摘要检索；变更日志；**二次授权查看明文**（10 分钟、理由必填、留痕）；标签、慢病用药、能力评估（同步失能等级）；CSV 导入导出（BOM） |
| **B 服务目录与派单** | 项目 CRUD、上下架、价格版本化（历史工单金额不变）；预约三重校验（±2 小时冲突 / 社区范围 / 能力适配）与 8 状态机、2 小时取消计费；派单候选过滤（技能 / 3km / 当日 <6 单 / 时段冲突 / 重点关注降权）+ **四因子加权 0.4/0.3/0.2/0.1** Top3 + 一键派单 + 改派留痕 + 30 分钟未接单顺延 |
| **C 执行留痕与质量** | 接单/拒单、**签到**（Haversine 距离，>200 米必填说明）、逐项勾选、照片上传（≤3 张、压缩 1280px）、**签退**（时长与标准偏差 >30 分钟标记异常，可写体征并触发预警）、异常上报；4 项 1–5 星评价、≤3 星自动生成 48 小时回访、投诉指派/复核/结论（成立可退费/换人/扣分）、月度绩效 |
| **D 健康监测与三级预警** | 指标录入/批量上报、按日趋势聚合、设备绑定；可配置规则（阈值越界 / 连续 N 次异常 / 静默超时）、同老人同规则 30 分钟冷却去重；蓝/黄/红三级预警、红色 5 分钟推送、30 分钟未处置自动升级街道、闭环三态（已处置/误报/已转诊）、响应与处置时长统计 |
| **E 结算与统计** | 补贴政策矩阵（失能等级 × 困难类型 + 月度上限）、长护险、个人自付（Decimal 到分）；月账单 + 四类差异拦截（重复计费/补贴超限/未完成/时长>3 倍）、差异率 >1% 需复核、账单状态机、红冲退费、Excel 导出；社区/机构/街道三视角看板、趋势、排行、分布与导出 |
| **F 前端与演示数据** | 六类角色差异化导航与首页看板、老人档案列表/详情（含二次授权）、预约与派单（Top3 因子进度条）、工单执行、健康趋势、预警处置、评价投诉、账单差异、统计看板（≥4 类图表）、系统管理页；演示数据脚本；`run_dev.ps1` + Docker |

---

## 四、页面清单

| 路径 | 页面 | 主要角色 |
|---|---|---|
| `/login` | 登录（含演示账号提示） | 全部 |
| `/` | 角色看板（服务量/待办/预警/账单 + 趋势与占比图） | 全部 |
| `/elders` | 老人档案列表（筛选/分页/脱敏） | 护理员、网格员、机构、街道、超管 |
| `/elders/{id}` | 档案详情（基本信息、标签、慢病用药、评估、变更日志、二次授权） | 同上 |
| `/appointments` | 预约列表（受理 / 转待派单 / 取消计费留痕） | 老人、网格员、机构、街道、超管 |
| `/appointments/new` | 提交预约（三重校验提示） | 老人、网格员、机构、超管 |
| `/dispatch` | 智能派单（待派单列表 → Top3 候选 + 四因子进度条 → 一键派单） | 网格员、超管 |
| `/orders`、`/orders/{id}` | 工单列表与执行（接单/签到/勾选/照片/签退/异常） | 全部 |
| `/monitoring`、`/monitoring/{elder_id}` | 健康监测（血压/心率/血氧/步数趋势 + 录入） | 全部 |
| `/alerts` | 预警看板（红色置顶、确认响应、处置弹窗、规则维护） | 全部 |
| `/quality` | 评价、回访任务、投诉处理、月度绩效 | 全部 |
| `/billing` | 账单列表/明细/对账差异、复核/缴费/红冲/导出 | 老人（只读）、网格员、机构、街道、超管 |
| `/stats` | 统计看板（折线/饼图/条形排行/柱状分布，7/30/90 天切换） | 网格员、机构、街道、超管 |
| `/admin/users`、`/admin/orgs`、`/admin/audit`、`/admin/backup`、`/admin/config` | 系统管理 | 机构/街道/超管 |

> 说明：健康监测页面使用 `/monitoring` 前缀，避免与基座自检接口 `GET /health`（JSON）冲突。

---

## 五、演示流程（六条）

1. **老人发起预约**：用 `elder1` 登录 → 「我的预约」→ 提交预约（明天 9:00，助浴服务）→ 记录预约号。
2. **网格员受理与派单**：`grid1` 登录 → 「预约服务」受理 → 转待派单 → 「智能派单」查看 Top3 候选与**四因子进度条**（技能/距离/负荷/评分）→ 一键派单生成工单。
3. **护理员上门执行**：`nurse1` 登录 → 「我的工单」→ 接单 → 签到（浏览器定位；偏差 >200 米需说明）→ 逐项勾选 → 上传照片 → 签退（填写小结与血压）→ 页面自动提示触发预警。
4. **预警闭环**：`grid1` 登录 → 「预警看板」→ 红色预警置顶 → 确认响应（记录响应时长）→ 处置闭环（已处置/误报/转诊）→ 查看规则配置。
5. **档案脱敏与二次授权**：`orgadmin` 登录 → 老人档案 → 详情页显示脱敏信息 → 填写理由申请二次授权 → 10 分钟内查看身份证/手机/住址明文（审计留痕）。
6. **结算与监管**：`orgadmin` 登录 → 「费用账单」生成/复核上月账单（差异率 >1% 会被拦截）→ 「统计看板」查看趋势/排行/分布并导出；`street` 登录 → 审计日志校验哈希链 → 备份恢复（需 `RESTORE` 二次确认）。

---

## 六、Docker 启动

```bash
# 应用 + PostgreSQL 16
docker compose -f docker/docker-compose.yml up -d --build

# 查看日志 / 停止
docker compose -f docker/docker-compose.yml logs -f app
docker compose -f docker/docker-compose.yml down
```

- 访问 <http://127.0.0.1:8000>（默认密码 `Admin@123`）
- 应用容器首次启动自动建表并写入演示数据（`python -m scripts.seed`，幂等）
- 仅构建应用镜像：`docker build -f docker/Dockerfile -t eldercare-platform:1.0.0 .`
- `DATABASE_URL` 已在 compose 中指向 `postgresql+psycopg://postgres:postgres@db:5432/eldercare`

---

## 七、测试

```powershell
# 全量
& '.\.venv\Scripts\python.exe' -m pytest tests -q

# 前端页面冒烟（登录保护/角色菜单/本地静态资源/页面渲染）
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_web_smoke.py -q

# 静态检查
& '.\.venv\Scripts\python.exe' -m ruff check app tests
```

页面冒烟测试覆盖：`/login` 200（含演示账号、无外网 CDN）、未登录访问页面 303 → `/login?next=`、
页面登录写 Cookie、超管/网格员/护理员/老人**菜单差异**、13 个业务页面渲染 200、
`GET /health` 仍返回 JSON、找不到数据返回友好 404 页而非 500。

---

## 八、目录结构

```
eldercare-platform/
├── app/
│   ├── main.py                # 应用工厂：异常处理、静态挂载、路由注册
│   ├── api/v1/                # REST 接口（auth/elders/catalog/appointments/dispatch/orders/
│   │                          #   health/alerts/quality/billing/stats/admin）
│   ├── algorithms/            # 纯函数：dispatch 评分、alert_rules 规则、subsidy 补贴
│   ├── core/                  # 配置、枚举、权限、依赖（RBAC/数据范围）、安全、状态机、审计、响应
│   ├── db/                    # Base / 引擎与会话
│   ├── models/                # ORM 模型（33 张表）
│   ├── schemas/               # Pydantic 模型
│   ├── services/              # 业务服务层（*_service.py）
│   ├── tasks/                 # 定时任务（预警超时、月度结算、每日备份、调度器）
│   ├── web/routes.py          # 页面路由（Jinja2 服务端渲染 + 登录态 Cookie）
│   ├── templates/             # Jinja2 模板（layouts / auth / elders / orders / admin …）
│   └── static/                # 本地静态资源：css/js/vendor（Bootstrap 5、ECharts 5 已本地化）
├── data/                      # 运行期：app.db、uploads、backups、tmp
├── docker/                    # Dockerfile + docker-compose.yml（app + postgres:16）
├── docs/CONTRACTS.md          # 并行开发契约（模型/路由/文件所有权）
├── scripts/
│   ├── seed.py                # 演示数据（幂等，固定随机种子）
│   └── run_dev.ps1            # 一键开发启动
├── tests/                     # unit / integration
├── requirements.txt / pyproject.toml / pytest.ini / .env.example
└── README.md
```

---

## 九、环境变量（`.env`，见 `.env.example`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/app.db` | 也可用 `postgresql+psycopg://user:pass@host:5432/db` |
| `SECRET_KEY` | dev 占位 | JWT 签名密钥（生产必须替换） |
| `ENCRYPTION_KEY` | dev 占位 | 敏感字段 AES 密钥 |
| `FIELD_HASH_KEY` | dev 占位 | 敏感字段 HMAC 摘要密钥 |
| `UPLOAD_DIR` / `BACKUP_DIR` | `./data/uploads` / `./data/backups` | 附件与备份目录 |
| `DISPATCH_WEIGHTS` | `0.4,0.3,0.2,0.1` | 派单四因子权重（技能/距离/负荷/评分） |
| `ORDER_DAILY_LIMIT` / `SERVICE_RADIUS_KM` | `6` / `3.0` | 派单当日单量上限与服务半径 |
| `CHECKIN_DISTANCE_THRESHOLD_M` / `DURATION_ANOMALY_MINUTES` | `200` / `30` | 签到偏差阈值与时长异常阈值 |
| `ALERT_*` | 5/30/48/30 | 红色推送、升级、静默、冷却 |
| `DIFF_RATIO_THRESHOLD` | `0.01` | 账单差异率复核阈值 |
| `ENABLE_SCHEDULER` | `true` | 是否启用定时任务调度器 |

---

## 十、常见问题

- **静态样式/图表不显示**：确认 `app/static/vendor/` 下已包含 `bootstrap.min.css`、`bootstrap.bundle.min.js`、
  `echarts.min.js`（仓库已内置，运行期不访问外网 CDN）。
- **端口被占用**：`run_dev.ps1` 默认 8000，可改脚本内 `--port` 或先释放端口。
- **数据库需要重置**：`python -m scripts.seed --reset`（SQLite 演示库）。
- **登录后页面空白/接口 401**：Cookie 过期（默认 480 分钟）；重新登录，或检查浏览器是否禁用了 Cookie。
- **中文乱码**：所有文件 UTF-8；PowerShell 中建议先设置 `$env:PYTHONIOENCODING='utf-8'`。
