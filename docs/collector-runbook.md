# 500 采集器 Windows 运行手册

> 版本：V1.3
> 更新日期：2026-07-16

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

`.env.example` 中数据库、授权 API 和邮件告警变量是后续阶段占位符，当前验证采集器不读取。

## 2. 单次验证

```powershell
.\.venv\Scripts\football-cups-collector.exe discover --workspace .
.\.venv\Scripts\football-cups-collector.exe report-daily --workspace .
```

检查最新 discovery manifest、日报和 SQLite 事件。确认六页面清单、fixture 并集、原始 blob 和标准化 JSONL 均已生成。`data/500/logs/collector.log` 当前主要记录未捕获异常，正常运行时可以为空，不能单独用它判断任务是否运行。

24 小时发现验证可以在完整 `run-once` 负载下执行，但只按发现轮次、六页面清单和身份数据验收。`report-daily` 按北京时间自然日统计，不能替代跨自然日的精确 24 小时窗口报告。

## 3. 安装任务计划

安装前确保 `.venv\Scripts\football-cups-collector.exe` 存在：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1
```

任务每 2 分钟执行 `run-once`，使用 S4U 账户、禁止并行、允许唤醒、失败后 5 分钟重试 3 次。S4U 通常不能访问需要交互凭据的网络共享，备份优先使用另一块本地磁盘或预先配置好的服务账户权限。

S4U 注册需要提升的 PowerShell。非管理员会话可以仅为 24 小时验证安装显式回退任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -Interactive
```

`-Interactive` 只在当前用户保持登录时运行，不能通过 30 天验收。正式连续运行前必须卸载回退任务，并在提升的 PowerShell 中重新安装默认 S4U 模式。

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
```

- 每日确认最后心跳、发现轮次、失败、来源缺盘、切点和磁盘。
- 7 天验证期每天人工核对页面数量，并抽查至少 3 场的身份、开球/销售截止时间、竞彩 SP 和三核心市场；结果写入 `docs/data-source-evaluation.md`。
- 每周查看连续失败；每月抽查 20 场并执行备份恢复。

`run-once` 会在跨日后自动生成上一自然日的日报，但当前 Windows 任务不会自动执行 `backup`。30 天验收前必须另建每日备份任务或执行等价的固定调度，并完成一次恢复验证。

`health` 可能返回 `ok`、`warning` 或 `failed`，退出码分别为 0、1、3。初始化后还没有心跳时返回 `warning`；心跳超过 10 分钟、完整发现或时钟校验超过 45 分钟、SQLite 损坏、严重磁盘不足或必需挂载点缺失时返回 `failed`。

## 5. 备份

设置指向另一物理磁盘或网络目录的环境变量：

```powershell
$env:FOOTBALL_CUPS_BACKUP_DIR = 'E:\football-cups-backup'
.\.venv\Scripts\football-cups-collector.exe backup --workspace .
```

备份命令使用 SQLite backup API，并增量复制原始 blobs、manifests、normalized、results 和 reports。与数据目录位于同一卷时命令拒绝执行。

迁移阿里云前还应执行 OSS 风格内容寻址备份：

```powershell
$env:FOOTBALL_CUPS_OSS_BACKUP_DIR = 'G:\football-cups-oss-layout'
.\.venv\Scripts\football-cups-collector.exe backup-oss --workspace .
.\.venv\Scripts\football-cups-collector.exe verify-oss-backup --workspace . --run-id <run-id> --target G:\football-cups-restore-test
```

该命令生成 `objects/sha256/`、`runs/<run-id>/manifest.json` 和 `complete.json`。只有完成标记和 manifest 哈希一致的批次才能用于恢复。

本地 OSS 风格目录只是对象布局暂存区。云端验收必须使用私有 OSS 完成上传、下载到全新空目录，再运行 `verify-oss-backup`；同一磁盘上的目录副本不能称为异机备份。

恢复测试应恢复到临时目录，运行：

```powershell
.\.venv\Scripts\football-cups-collector.exe rebuild-state --workspace <restored-workspace>
.\.venv\Scripts\football-cups-collector.exe report-daily --workspace <restored-workspace>
```

## 6. 故障恢复

- `skipped_locked`：已有实例运行，正常等待下一轮。
- 退出码 1：存在可重试网络或解析失败，查看日报和日志。
- 退出码 2：配置、输入或 schema 错误，修复后再运行。
- 退出码 3：时钟、磁盘或本地存储严重错误，停止定时任务并先处理基础环境。
- SQLite 损坏：保留损坏文件，运行 `rebuild-state`；已错过的历史切点只能标记缺口。
- 页面结构变化：保留原始响应，停止受影响解析，不手工改写历史数据。

## 7. 人工赛果确认

对杯赛或未知赛事准备 CSV：

```powershell
.\.venv\Scripts\football-cups-collector.exe verify-results --workspace . --input verified-results.csv
```

比分必须是常规时间 90 分钟及补时。冲突记录不会覆盖已有结果。
