# 测试报告

验证日期：2026-11-12。环境：Windows、Python 3.12.14、pytest 8.3.0、SQLite 内存测试库。

## 最终结果

数据来自 `reports/junit.xml` 和 `reports/coverage.xml`。

| 指标 | 结果 |
|---|---:|
| 用例总数 / 通过 | 410 / 410 |
| 失败 / 错误 / 跳过 | 0 / 0 / 0 |
| pytest 耗时 | 264.870 秒 |
| app 语句覆盖率 | 89.14% |
| 可执行语句 / 已覆盖 | 7553 / 6733 |
| Ruff | 通过 |
| JS 语法检查 | node --check app/static/js/app.js 通过 |
| Git 补丁空白检查 | 通过 |
| Docker Compose 配置 | 校验通过；引擎未运行，未验证构建部署 |

主要模块覆盖率：派单算法 92%、派单服务 92%、档案服务 96%、预约服务 89%、结算服务 92%、工单服务 90%、质量服务 88%、统计服务 94%、数据范围依赖 94%、页面路由 76%。完整 HTML 明细：`reports/htmlcov/index.html`。

## 新增回归

| 文件 | 用例数 | 验证重点 |
|---|---:|---|
| integration/test_competition_regressions.py | 32 | 四周周期预约与整组回滚、派单/改派硬约束、服务日负荷、29分59秒/30分钟超时、自动换候选及人工协调、取消/爽约释放时段、价格快照、跨网格读写、未绑定账号、本人预约授权、合并账单隔离、照片授权、机构越权授权、可穿戴黄红预警、30天评分窗口、禁止重开终态 |
| unit/test_competition_safety.py | 18 | 非法权重、贡献与降权、CSV/XLSX 公式转义、保持数字类型、EXIF 清理与旋转、像素和原图大小限制 |
| unit/test_billing.py 新增部分 | 4 | 分币累计退费精确归零、重复退费拒绝、部分退费后整单红冲拒绝、零/负金额拒绝 |

既有预约、派单、预警、执行、质量、结算、审计和备份用例仍全部通过。旧用例按赛题修正：超时从派单时刻起算；日负荷对应服务日期；评分对比使用真实评价记录；无关老人的预约使用有权限的账号创建。

## 端到端和真实 HTTP 验收

`integration/test_flows_e2e.py` 的 7 个用例全部通过，覆盖预约派单执行评价、健康预警闭环、超时升级、低分回访投诉退费、结算差异缴费红冲、权限审计、备份恢复。

另执行 `python -m scripts.verify_demo`，报告为 `reports/demo-smoke.json`：

- 独立临时数据库和上传目录，两次执行种子脚本后 40 张表记录数一致。
- 实际启动 uvicorn，经 /health 检查后登录 super、street、orgadmin、grid1、nurse1、elder1 六类账号。
- 13 次关键页面访问均返回 200，覆盖首页、统计、审计、账单、预约、派单、质量和工单。
- 结束后关闭验收服务、清理临时库。曾发现 Windows 下记录数查询连接未显式关闭造成临时库占用；修复后已重新通过真实启动验收。

## 复现命令

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

依次运行 Ruff、全量 pytest 与覆盖率、真实 HTTP 演示验收。也可以分别运行：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest tests -q --tb=short --junitxml=reports/junit.xml --cov=app --cov-report=xml:reports/coverage.xml --cov-report=html:reports/htmlcov
.\.venv\Scripts\python.exe -m scripts.verify_demo
```

## 验证范围

测试和覆盖率针对 Python 应用逻辑；脚本、CSS、JS 不计入 app 覆盖率。页面通过服务器渲染与真实 HTTP 检查，未完成浏览器视觉检查、移动端尺寸测量或键盘可访问性人工验收。

未验证 PostgreSQL/Docker 实际运行、高并发一致性、多实例调度、真实短信送达、生产备份恢复和真实监管抽样。调度器装配不在单测中启动，tasks/scheduler.py 覆盖率为 0%；备份、月结及预警任务函数已有独立用例，不能据此推断实际到点调度已验收。

完整能力边界和提交事项见 [赛题验收优化说明](赛题验收优化说明.md)。
