# Football Cups

基于 500 足球竞彩及其多公司盘口数据的长期前瞻采集、赛前概率预测、临场监控与赛后复盘项目。采集覆盖竞彩页面出现的全部比赛；模型只使用满足时间、盘口和赛果资格的数据。

预测口径为常规时间 90 分钟及补时，不包含加时赛和点球大战。项目不提供投注金额、组合、成本或收益建议。

## 当前状态

项目处于“阶段 3：标准化数据库”。项目负责人已授权在采集验证继续运行的同时提前建设 PostgreSQL 可重建分析层；这不代表 24 小时、7 天或 30 天采集验收已经通过。当前仍不开发模型或 Web 产品，也不将验证采集器标记为长期生产可用。

当前进度及唯一下一步见 `docs/project-status.md`。隔离的公开历史研究基线已经可运行，但不属于正式阶段 4。阿里云杭州 ECS 已创建，但目前只允许隔离 smoke；数据盘、OSS 和正式切换门禁尚未完成。

## 文档入口

- `AGENTS.md`：Agent 工作规则、恢复顺序和阶段门禁
- `docs/product-plan.md`：产品边界与长期目标
- `docs/execution-plan.md`：分阶段开发和验收计划
- `docs/project-status.md`：当前状态、风险和唯一下一步
- `docs/decision-log.md`：已接受与暂定决策
- `docs/data-source-evaluation.md`：500 技术验收记录
- `docs/500-collector-spec.md`：采集器数据契约和行为规范
- `docs/collector-runbook.md`：Windows 运行、备份和恢复手册
- `docs/database-design.md`：PostgreSQL 数据模型和防泄漏查询契约
- `docs/database-runbook.md`：数据库安装、迁移、导入和恢复手册
- `docs/cloud-migration-plan.md`：阿里云迁移前准备、备份、切换和回滚计划
- `docs/research-data-acquisition-plan.md`：历史免费研究路线及已有结果

## 采集器

安装开发环境后可运行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\football-cups-collector.exe init --workspace .
.\.venv\Scripts\football-cups-collector.exe discover --workspace .
.\.venv\Scripts\football-cups-collector.exe run-once --workspace .
.\.venv\Scripts\football-cups-collector.exe health --workspace .
```

当前采集器读取的配置项和默认值见 `.env.example`，完整命令、任务计划、备份与恢复步骤见 `docs/collector-runbook.md`。本地默认运行数据写入被 Git 忽略的 `data/500/`。长时间中断后先按 `AGENTS.md` 恢复，不依赖聊天历史。

迁移到阿里云前必须先完成精确窗口报告、备份恢复和云端 smoke test，步骤见 `docs/cloud-migration-plan.md`。

云端正式环境使用 `FOOTBALL_CUPS_REQUIRED_MOUNT=/srv/football-cups` 防止数据盘掉线后写入系统盘；40 GB 系统盘 smoke 使用独立的 `/var/lib/football-cups-smoke/500`，不得启用长期 timer。

## 标准化数据库

本地 PostgreSQL 程序、数据和日志默认全部位于 D 盘的 `data/` 下：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\local_postgres.ps1 -Action Install -Workspace .
.\.venv\Scripts\football-cups-db.exe init --workspace .
.\.venv\Scripts\football-cups-db.exe import-files --workspace .
.\.venv\Scripts\football-cups-db.exe status --workspace .
```

数据库是可重建分析层，不能替代原始 blob、manifest 和 JSONL。完整操作见 `docs/database-runbook.md`。

## 历史研究

公开静态历史数据使用完全隔离的命令和 schema：

```powershell
.\.venv\Scripts\football-cups-research.exe catalog --workspace .
.\.venv\Scripts\football-cups-research.exe normalize --workspace . --since 2025-01-01
.\.venv\Scripts\football-cups-research.exe db-import --workspace .
.\.venv\Scripts\football-cups-research.exe report-coverage --workspace .
.\.venv\Scripts\football-cups-research.exe evaluate-baseline --workspace .
```

该路线不抓取 500 历史页面，不进入正式 `football` schema，也不能替代严格前瞻验收。来源、K1 导入和访问频率规则见 `docs/research-data-acquisition-plan.md`。

## 安全提示

真实密钥、密码和连接串只保存在未跟踪的 `.env` 或系统密钥服务中。原始数据、数据库、备份、日志和模型产物不得提交 Git。
