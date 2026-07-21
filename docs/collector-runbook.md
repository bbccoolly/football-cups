# 500 采集器 Windows 运行手册

> 版本：V2.3
> 更新日期：2026-07-21

## 1. 安装

```powershell
cd D:\2026-football-cups\football-cups
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\football-cups-collector.exe init --workspace .
```

若需要使用中国体彩官方页面的标准 headless Edge scope 证据，额外安装浏览器依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[browser]'
```

该路径使用本机 Microsoft Edge，不下载或启用 stealth、代理、Cookie 轮换或验证码处理。

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
| `COLLECTOR_SPORTTERY_RECONCILE_ENABLED` | `true`；是否启用每日体彩官方补偿 |
| `COLLECTOR_SPORTTERY_RECONCILE_INTERVAL_HOURS` | `24` |
| `COLLECTOR_SPORTTERY_RECONCILE_MINIMUM_AGE_HOURS` | `24`；只处理开球至少24小时的比赛 |
| `COLLECTOR_SPORTTERY_RECONCILE_LOOKBACK_DAYS` | `8` |
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

安装前确保虚拟环境、采集器、数据库 CLI 和研究 CLI 存在。提升的 PowerShell 中先预演，再原地注册三个 S4U 任务和两个专用非管理员任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\configure_database_task_user.ps1 -Workspace . -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\install_backup_tasks.ps1 -Workspace . -WhatIf
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\windows\configure_database_task_user.ps1 -Workspace .
powershell -ExecutionPolicy Bypass -File scripts\windows\install_backup_tasks.ps1 -Workspace .
```

采集任务每 2 分钟运行，数据库每 5 分钟导入，研究影子预测每 2 分钟运行，每日 03:30 执行增量镜像，每周日 04:30 生成内容寻址批次。五个任务均禁止并行、允许唤醒并在失败后重试。采集和两个备份任务使用当前用户的 S4U；数据库和影子预测任务必须使用专用非管理员 `football-cups-runner`，否则 PostgreSQL 可能拒绝在重启后启动。

当前独立 Windows 主机拒绝为另一个本地标准账户注册 S4U。`configure_database_task_user.ps1` 因此在内存中生成随机密码，将账户加入内置 `Users`、授予工作区、数据目录和 `.venv` 基础 Python 的最小 ACL，并以 Task Scheduler `Password` 登录类型注册数据库任务和 `FootballCups-Research-Shadow-Prediction`。凭据由 Windows LSA 加密保存，不写入任务 XML、Git、日志或聊天；脚本再次运行会轮换密码并同步更新任务。S4U 通常不能访问需要交互凭据的网络共享，当前使用本机 G 盘。

任务注册和专用账户配置需要提升的 PowerShell。非管理员会话可以仅为 24 小时验证安装显式回退任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -Interactive
```

`-Interactive` 只在当前用户保持登录时运行，不能通过 30 天验收。正式连续运行前必须卸载回退任务，并在提升的 PowerShell 中重新安装默认无人值守模式。

注册后逐项手工触发并轮询到 `Ready`，确认 `LastTaskResult=0` 和新的完成 manifest。影子预测任务可能因尚未到真实发布窗口返回 `unchanged`，但不得失败。随后记录任务 `LastRunTime`，注销用户至少 10 分钟；重新登录后确认采集至少两轮、数据库至少一轮且 `health=ok`。最终还需重启一次 Windows，确认 G 盘仍属于预期物理磁盘、PostgreSQL 可由导入任务启动且心跳在 10 分钟内恢复。

影子预测使用 `config/research-competition-profiles.json` 的显式赛事ID和版本化置信策略。任务只在 `T-24h`、`T-6h`、`T-60m`、`T-10m` 的真实发布窗口追加记录；窗口外返回 `unchanged`。运维检查：

韩职K1还读取`config/research-k1-guardrail.json`。只有`prediction_cutoff >= effective_at`的自然K1机会才在同一原子JSONL追加shadow assessment；相关源码未提交时记录`unavailable`而不修改基础预测。新批次manifest必须携带记录哈希和prediction/assessment计数。策略首版拒绝`active`，不得通过任务参数绕过。

```powershell
.\.venv\Scripts\football-cups-research.exe shadow-predict --workspace . --channel research-shadow-v1 --dry-run
Get-ChildItem data\research\normalized\shadow-predictions -Recurse -Filter *.jsonl |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content
.\.venv\Scripts\football-cups-research.exe evaluate-shadow --workspace . --channel research-shadow-v1
```

新记录必须包含截止前身份、注册表文件/canonical双哈希、赛事等级、置信和风险字段。未知或分层冲突赛事只能 abstain；迁移前记录保持 `legacy_unclassified`，不得重发。赛事分层不修改胜平负概率，也不解除正式模型门禁。

卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_collector_task.ps1 -Uninstall
powershell -ExecutionPolicy Bypass -File scripts\windows\install_shadow_prediction_task.ps1 -Workspace . -Uninstall
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
.\.venv\Scripts\football-cups-collector.exe reconcile-results --workspace . --source sporttery --since <RFC3339> --until <RFC3339> --dry-run
.\.venv\Scripts\football-cups-collector.exe reconcile-results --workspace . --source sporttery --since <RFC3339> --until <RFC3339> --apply
.\.venv\Scripts\football-cups-collector.exe sporttery-smoke --workspace . --fixture-id <id> --since <RFC3339> --until <RFC3339>
.\.venv\Scripts\football-cups-collector.exe audit-result-evidence --workspace .
.\.venv\Scripts\football-cups-collector.exe audit-market-data --workspace .
```

- 每日确认最后心跳、发现轮次、失败、来源缺盘、切点和磁盘。
- 7 天验证期每天人工核对页面数量，并抽查至少 3 场的身份、开球/销售截止时间、竞彩 SP 和三核心市场；结果写入 `docs/data-source-evaluation.md`。
- 每周查看连续失败；每月抽查 20 场并执行备份恢复。

`run-once` 会在跨日后自动生成上一自然日的日报。备份任务把每次运行写入 `data/500/logs/backup-task.jsonl`；任务返回 0 仍须同时存在新的完成 manifest。每月应从真实定时批次完成一次恢复。

`health` 可能返回 `ok`、`warning` 或 `failed`，退出码分别为 0、1、3。除心跳、发现、时钟、SQLite、磁盘和挂载点外，还报告 `backup_status`、`oss_backup_status`、最后完成时间和 G 盘剩余空间。每日备份超过 26 小时或每周备份超过 8 天为警告，分别超过 48 小时或 15 天为失败。

## 5. 盘口 V2 审计与离线修复

正常采集时欧赔读取 Excel，亚盘、大小球和让球指数直接解析正确解码的 HTML。采集成功只表示来源与切点取得；`SnapshotEligibilityAssessment.model_strict_eligible` 还要求三个核心市场各至少 3 家完整 bookmaker。让球指数不计入 V1 模型资格。

维护前确认未来 15 分钟没有即将关闭的市场窗口，停止采集和数据库任务并完成双层备份。先执行只读审计和 dry-run：

```powershell
.\.venv\Scripts\football-cups-collector.exe audit-market-data --workspace .
.\.venv\Scripts\football-cups-collector.exe reparse-markets `
  --workspace . `
  --since <RFC3339-inclusive> `
  --until <RFC3339-exclusive> `
  --dry-run
```

确认 `network_requests=0`、乱码和已知盘口转换失败为0后，执行 `--apply`。只有含合法 `complete.json` 且 manifest/JSONL 哈希一致的修复目录才会被数据库导入；重复 `--apply` 和重复导入必须新增0。失败或契约错误的修复批次原样移到 `data/500/quarantine/repairs/`，不得修改内容后冒充完成批次。

V2 正式发布后依次核对 `unsupported_records=0`、current/as-of 只返回整数版本2、每个 `fixture+target` 只有一个当前模型批次，以及非 repairs 的 V1 文件与维护前备份逐文件 SHA-256 相同。修复报告位于 `data/500/reports/repairs/`，不计入实时 7 天或 30 天指标。

## 6. 备份

使用配置脚本验证源和目标的物理磁盘编号，并在不覆盖其他配置的前提下原子更新未跟踪 `.env`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\configure_local_backup.ps1 `
  -Workspace . `
  -BackupDir G:\football-cups-backup `
  -OssBackupDir G:\football-cups-oss-layout
```

`backup` 和 `backup-oss` 等待共享锁最多 300 秒。持锁阶段只固定清单、快照活动 JSONL 和 SQLite；复制及哈希在释放锁后进行。若存在 `data/research`，备份还会短暂取得 `research-facts.lock` 并以 `research/...` 前缀纳入同一批次。锁超时返回 1，配置错误返回 2，一致性、SQLite 或存储错误返回 3。因备份锁跳过的 `run-once` 保存 `RunnerSkip` manifest 并进入日报。

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

## 7. 故障恢复

- `skipped_locked`：已有实例或备份快照持锁；采集跳过会保存独立 manifest，备份等待超时则返回 1。
- 退出码 1：存在可重试网络或解析失败，查看日报和日志。
- 退出码 2：配置、输入或 schema 错误，修复后再运行。
- 退出码 3：时钟、磁盘或本地存储严重错误，停止定时任务并先处理基础环境。
- SQLite 损坏：保留损坏文件，运行 `rebuild-state`；已错过的历史切点只能标记缺口。
- 页面结构变化：保留原始响应，停止受影响解析，不手工改写历史数据。

## 8. 赛果闭环

日期直播页确定性比分自动形成候选；HTML 清单切换后自动读取页面自身的日期 Full 数据流。普通联赛在分析页一致后自动形成已验证赛果。若直播源持续遗漏 fixture，采集器会尝试 `shuju` 与 `ouzhi` 分析端点双证据 fallback；两端比分一致且赛事 ID 登记为 `regular_time_only` 时才自动验证。可能加时、未知、身份冲突或比分冲突的比赛默认隔离；不创建人工待办，但项目负责人可按 D-028 主动声明现有唯一候选的90分钟口径。

中国体彩官方源用于补充明确 90 分钟口径。先执行 `--dry-run` 核对 queued、mapping、candidate 和 verified 计数，再执行 `--apply` 写入 `SportteryScopeEvidence`、`SportteryInventoryBatch`、`SportteryFixtureLink`、`SportteryResultObservation`、官方候选和已验证赛果。官方清单分页不完整、scope 文本不可见、详情不一致或 WAF 阻断时不得自动验证。官方来源失败单独统计，不计入 500 盘口采集失败。

`run-once` 已接入低频官方补偿：每天最多一次，默认扫描开球后24小时至8天且尚无自动验证证据的 fixture；已有负责人声明的 fixture 仍继续核验。`last_sporttery_reconcile_attempt_at` 记录所有尝试，只有完整成功才更新 `last_sporttery_reconcile_success_at`。EdgeOne 567、CORS 或清单不完整返回 `partial`，保存原始响应和失败原因，但不生成映射缺失或官方比分。标准 headless Edge 只用于读取官方页面 scope；不得使用代理、stealth、Cookie/Token 重放或验证码处理。

证据审计：

```powershell
.\.venv\Scripts\football-cups-collector.exe audit-result-evidence --workspace .
.\.venv\Scripts\football-cups-collector.exe audit-result-evidence --workspace . --fixture-id <id>
```

`ok` 表示至少存在一条引用完整的官方已验证赛果；`warning` 表示只有失败/不完整尝试或尚无官方已验证赛果；引用断裂、比分或 fixture 不一致返回 `failed`。数据库重复导入必须新增0，且 `status` 中四张 `sporttery_*` 证据表和 `unsupported_records` 可审计。

检查日报中的以下指标：

```text
result_candidate_coverage_24h
verified_result_coverage
automatic_verified_result_coverage
manual_declared_result_coverage
verified_result_count_by_method
result_unresolved_count
result_conflict_count
result_scope_ambiguous_count
result_success_rate_by_target
strict_fixture_result_count_by_cutoff
```

`T+24h` 后仍缺少候选或一致性证据时，采集器每日补偿至 `T+7d`。`reconcile-results` 可立即重查历史缺口，但新证据使用真实观察时间，不能倒填。`verify-results` 仅为旧接口兼容，不属于正式操作流程。

项目负责人已明确核验现有候选为常规时间结果时执行：

```powershell
.\.venv\Scripts\football-cups-collector.exe confirm-candidate-results `
  --workspace . `
  --fixture-id <id> `
  --confirm-90-minutes `
  --note "Project owner confirmed the candidate score as a regular-time result"
```

可重复传入最多100个 `--fixture-id`。命令不接收比分，先验证全部候选、身份、无效状态和冲突；任一失败时整批不写。成功后运行两次 `football-cups-db import-files --workspace .`，第二次新增必须为0，并检查 `current_verified_results`、严格切点计数及按方法拆分的覆盖率。人工声明不改变体彩接口成功率，8天窗口内的官方自动补偿继续运行。

人工核验确认场次无效、取消或未结算时，保留证据 URL 并执行：

```powershell
.\.venv\Scripts\football-cups-collector.exe invalidate-fixture `
  --workspace . `
  --fixture-id <id> `
  --reason invalid_match `
  --source-url <evidence-url> `
  --note <audit-note>
```

命令只做追加式逻辑排除并取消待执行任务。执行后运行数据库导入和日报，确认 `current_invalid_fixtures` 包含该 fixture、赛果分母已剥离；不得删除任何事实文件或人工补写比分。
