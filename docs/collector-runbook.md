# 500 采集器 Windows 运行手册

> 版本：V1.5
> 更新日期：2026-07-17

## 1. 安装

```powershell
cd D:\2026-football-cups\football-cups
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\football-cups-collector.exe init --workspace .
```

真实配置放入未跟踪的 `.env` 或系统环境变量。`FOOTBALL_CUPS_DATA_DIR` 为空时默认使用 `<workspace>\data\500`。

当前采集器读取以下配置：

| 配置项 | 默认值或作用 |
| --- | --- |
| `APP_TIMEZONE` | `Asia/Shanghai` |
| `LOG_LEVEL` | `INFO` |
| `FOOTBALL_CUPS_DATA_DIR` | `<workspace>\data\500` |
| `FOOTBALL_CUPS_BACKUP_DIR` | 无默认值，备份前必须设置 |
| `FOOTBALL_CUPS_OSS_BACKUP_DIR` | 无默认值，OSS 风格备份前必须设置 |
| `FOOTBALL_CUPS_REQUIRED_MOUNT` | 无默认值；设置后必须是实际挂载点，否则拒绝创建数据目录 |
| `COLLECTOR_DISCOVERY_INTERVAL_MINUTES` | `30` |
| `COLLECTOR_REQUEST_MIN_INTERVAL_SECONDS` | `1.5` |
| `COLLECTOR_RUN_TIME_BUDGET_SECONDS` | `100` |
| `COLLECTOR_CLOCK_DRIFT_LIMIT_SECONDS` | `30` |
| `COLLECTOR_DISK_WARNING_FREE_GB` / `CRITICAL_FREE_GB` | `50` / `20` |
| `COLLECTOR_DISK_WARNING_FREE_PERCENT` / `CRITICAL_FREE_PERCENT` | `20` / `10` |
| `COLLECTOR_HEALTH_HEARTBEAT_MAX_AGE_MINUTES` | `10` |
| `COLLECTOR_HEALTH_DISCOVERY_MAX_AGE_MINUTES` | `45` |
| `COLLECTOR_HEALTH_CLOCK_MAX_AGE_MINUTES` | `45` |
| `COLLECTOR_BACKUP_LOCK_WAIT_SECONDS` / `LOCK_POLL_SECONDS` | `300` / `5` |
| `COLLECTOR_BACKUP_WARNING_MAX_AGE_HOURS` / `FAILED_MAX_AGE_HOURS` | `26` / `48` |
| `COLLECTOR_OSS_BACKUP_WARNING_MAX_AGE_DAYS` / `FAILED_MAX_AGE_DAYS` | `8` / `15` |

`.env.example` 中数据库、授权 API 和邮件告警变量是后续阶段占位符，当前验证采集器不读取。

## 2. 单次验证

```powershell
.\.venv\Scripts\football-cups-collector.exe discover --workspace .
.\.venv\Scripts\football-cups-collector.exe report-daily --workspace .
```

检查最新 discovery manifest、日报和 SQLite 事件。确认六页面清单、fixture 并集、原始 blob 和标准化 JSONL 均已生成。`data/500/logs/collector.log` 当前主要记录未捕获异常，正常运行时可以为空，不能单独用它判断任务是否运行。

24 小时发现验证可以在完整 `run-once` 负载下执行，但只按发现轮次、六页面清单和身份数据验收。`report-daily` 按北京时间自然日统计，不能替代跨自然日的精确 24 小时窗口报告。

## 3. 安装任务计划

安装前确保虚拟环境、采集器和数据库 CLI 存在。提升的 PowerShell 中先预演，再原地注册四个 S4U 任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\install_database_import_task.ps1 -Workspace . -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\install_backup_tasks.ps1 -Workspace . -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\windows\install_database_import_task.ps1 -Workspace .
powershell -ExecutionPolicy Bypass -File scripts\windows\install_backup_tasks.ps1 -Workspace .
```

采集任务每 2 分钟运行，数据库每 5 分钟导入，每日 03:30 执行增量镜像，每周日 04:30 生成内容寻址批次。四个任务使用 S4U、禁止并行、允许唤醒并在失败后重试。S4U 通常不能访问需要交互凭据的网络共享，当前使用本机 G 盘。

S4U 注册需要提升的 PowerShell。非管理员会话可以仅为 24 小时验证安装显式回退任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -Interactive
```

`-Interactive` 只在当前用户保持登录时运行，不能通过 30 天验收。正式连续运行前必须卸载回退任务，并在提升的 PowerShell 中重新安装默认 S4U 模式。

注册后逐项手工触发并轮询到 `Ready`，确认 `LastTaskResult=0` 和新的完成 manifest。随后记录任务 `LastRunTime`，注销用户至少 10 分钟；重新登录后确认采集至少两轮、数据库至少一轮且 `health=ok`。最终还需重启一次 Windows，确认 G 盘仍属于预期物理磁盘、PostgreSQL 可由导入任务启动且心跳在 10 分钟内恢复。

卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -Uninstall
```

## 4. 日常操作

```powershell
.\.venv\Scripts\football-cups-collector.exe run-once --workspace .
.\.venv\Scripts\football-cups-collector.exe report-daily --workspace .
.\.venv\Scripts\football-cups-collector.exe report-window --workspace . --start <RFC3339> --end <RFC3339>
.\.venv\Scripts\football-cups-collector.exe backup --workspace .
.\.venv\Scripts\football-cups-collector.exe backup-oss --workspace .
.\.venv\Scripts\football-cups-collector.exe health --workspace .
.\.venv\Scripts\football-cups-collector.exe reconcile-results --workspace . --since <RFC3339> --until <RFC3339>
```

- 每日确认最后心跳、发现轮次、失败、来源缺盘、切点和磁盘。
- 7 天验证期每天人工核对页面数量，并抽查至少 3 场的身份、开球/销售截止时间、竞彩 SP 和三核心市场；结果写入 `docs/data-source-evaluation.md`。
- 每周查看连续失败；每月抽查 20 场并执行备份恢复。

`run-once` 会在跨日后自动生成上一自然日的日报。备份任务把每次运行写入 `data/500/logs/backup-task.jsonl`；任务返回 0 仍须同时存在新的完成 manifest。每月应从真实定时批次完成一次恢复。

`health` 可能返回 `ok`、`warning` 或 `failed`，退出码分别为 0、1、3。除心跳、发现、时钟、SQLite、磁盘和挂载点外，还报告 `backup_status`、`oss_backup_status`、最近完成时间和 G 盘剩余空间。每日备份超过 26 小时或每周备份超过 8 天为警告，分别超过 48 小时或 15 天为失败。

## 5. 备份

使用配置脚本验证源和目标的物理磁盘编号，并在不覆盖其他配置的前提下原子更新未跟踪 `.env`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\configure_local_backup.ps1 `
  -Workspace . `
  -BackupDir G:\football-cups-backup `
  -OssBackupDir G:\football-cups-oss-layout
```

`backup` 和 `backup-oss` 等待共享锁最多 300 秒。持锁阶段只固定清单、快照活动 JSONL 和 SQLite；复制及哈希在释放锁后进行。锁超时返回 1，配置错误返回 2，一致性、SQLite 或存储错误返回 3。因备份锁跳过的 `run-once` 保存 `RunnerSkip` manifest 并进入日报。

手工运行与定时任务使用同一包装脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\run_backup_task.ps1 -Workspace . -Mode Incremental
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\run_backup_task.ps1 -Workspace . -Mode ContentAddressed
.\.venv\Scripts\football-cups-collector.exe verify-oss-backup --workspace . --run-id <run-id> --target D:\football-cups-restore-test\data\500
```

该命令生成 `objects/sha256/`、`runs/<run-id>/manifest.json` 和 `complete.json`。只有完成标记和 manifest 哈希一致的批次才能用于恢复。

本地 OSS 风格目录只是对象布局暂存区。云端验收必须使用私有 OSS 完成上传、下载到全新空目录，再运行 `verify-oss-backup`；同一磁盘上的目录副本不能称为异机备份。

恢复测试必须使用空目录；验证哈希后将临时 `FOOTBALL_CUPS_DATA_DIR` 指向恢复数据，重建 SQLite、生成日报并按 `docs/database-runbook.md` 导入独立 `_test` 数据库：

```powershell
$env:FOOTBALL_CUPS_DATA_DIR = 'D:\football-cups-restore-test\data\500'
.\.venv\Scripts\football-cups-collector.exe rebuild-state --workspace .
.\.venv\Scripts\football-cups-collector.exe report-daily --workspace .
```

## 6. 故障恢复

- `skipped_locked`：已有实例或备份快照持锁；采集跳过会保存独立 manifest，备份等待超时则返回 1。
- 退出码 1：存在可重试网络或解析失败，查看日报和日志。
- 退出码 2：配置、输入或 schema 错误，修复后再运行。
- 退出码 3：时钟、磁盘或本地存储严重错误，停止定时任务并先处理基础环境。
- SQLite 损坏：保留损坏文件，运行 `rebuild-state`；已错过的历史切点只能标记缺口。
- 页面结构变化：保留原始响应，停止受影响解析，不手工改写历史数据。

## 7. 全自动赛果闭环

日期直播页确定性比分自动形成候选；HTML 清单切换后自动读取页面自身的日期 Full 数据流。普通联赛在分析页一致后自动形成已验证赛果。可能加时、未知、身份冲突或比分冲突的比赛自动隔离，不要求人工确认，也不得进入训练。

检查日报中的以下指标：

```text
result_candidate_coverage_24h
verified_result_coverage
result_unresolved_count
result_conflict_count
result_scope_ambiguous_count
result_success_rate_by_target
strict_fixture_result_count_by_cutoff
```

`T+24h` 后仍缺少候选或一致性证据时，采集器每日补偿至 `T+7d`。`reconcile-results` 可立即重查历史缺口，但新证据使用真实观察时间，不能倒填。`verify-results` 仅为旧接口兼容，不属于正式操作流程。
