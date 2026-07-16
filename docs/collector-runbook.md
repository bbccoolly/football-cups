# 500 采集器 Windows 运行手册

> 版本：V1.0  
> 更新日期：2026-07-15

## 1. 安装

```powershell
cd D:\2026-football-cups\football-cups
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .[dev]
football-cups-collector init --workspace .
```

真实配置放入未跟踪的 `.env` 或系统环境变量。`FOOTBALL_CUPS_DATA_DIR` 为空时默认使用 `<workspace>\data\500`。

## 2. 单次验证

```powershell
football-cups-collector discover --workspace .
football-cups-collector report-daily --workspace .
```

检查最新 discovery manifest、日报和 `data/500/logs/collector.log`。确认六页面清单、fixture 并集、原始 blob 和标准化 JSONL 均已生成。

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
football-cups-collector run-once --workspace .
football-cups-collector report-daily --workspace .
football-cups-collector backup --workspace .
```

- 每日确认最后心跳、发现轮次、失败、来源缺盘、切点和磁盘。
- 7 天验证期每天人工核对页面数量和 3 场数据。
- 每周查看连续失败；每月抽查 20 场并执行备份恢复。

## 5. 备份

设置指向另一物理磁盘或网络目录的环境变量：

```powershell
$env:FOOTBALL_CUPS_BACKUP_DIR = 'E:\football-cups-backup'
football-cups-collector backup --workspace .
```

备份命令使用 SQLite backup API，并增量复制原始 blobs、manifests、normalized、results 和 reports。与数据目录位于同一卷时命令拒绝执行。

恢复测试应恢复到临时目录，运行：

```powershell
football-cups-collector rebuild-state --workspace <restored-workspace>
football-cups-collector report-daily --workspace <restored-workspace>
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
football-cups-collector verify-results --workspace . --input verified-results.csv
```

比分必须是常规时间 90 分钟及补时。冲突记录不会覆盖已有结果。
