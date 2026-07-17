# PostgreSQL 数据库运行手册

> 版本：V1.2
> 更新日期：2026-07-17

## 1. 本地运行方式

本地验证使用 PostgreSQL 17.10 便携运行包，不注册 Windows 服务、不修改系统 PATH。默认位置：

```text
data/runtime/postgresql/17.10-2/   程序
data/postgresql/17-main/           数据、WAL 和服务日志
127.0.0.1:55432                    唯一监听地址
```

本地集群使用 trust 认证，只允许绑定本机回环地址。不得把端口转发、代理或暴露到局域网/公网；远程部署必须使用独立账号、密码或证书和网络访问控制。

PostgreSQL 服务日志写入 `data/postgresql/17-main/log/`，按自然日轮转并循环使用日期文件名，保留约 31 天。启动器的标准输出和错误日志位于集群根目录，每次启动覆盖。

## 2. 安装与启停

预演和安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\local_postgres.ps1 -Action Install -Workspace . -WhatIf
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\local_postgres.ps1 -Action Install -Workspace .
```

安装器固定校验 PostgreSQL 压缩包 SHA-256，将程序和数据放在被 Git 忽略的 `data/`。日常命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\local_postgres.ps1 -Action Status -Workspace .
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\local_postgres.ps1 -Action Start -Workspace .
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\local_postgres.ps1 -Action Stop -Workspace .
```

## 3. 迁移与导入

```powershell
.\.venv\Scripts\football-cups-db.exe init --workspace .
.\.venv\Scripts\football-cups-db.exe import-files --workspace .
.\.venv\Scripts\football-cups-db.exe status --workspace .
```

`init` 只执行尚未应用的迁移，并拒绝已经应用后又被修改的 SQL 文件。`import-files` 先核对不可变 manifest，再按 JSONL 检查点增量导入。`import-jsonl` 只用于诊断，不替代日常 `import-files`。

`status` 额外返回 `current_verified_results` 和 `strict_fixture_results_by_cutoff`。后者按不同预测切点统计不同 fixture，不能把同场多个切点相加作为阶段 4 的 500 场门禁。

退出码：

- `0`：成功、无新增数据或另一个导入器已持有锁。
- `1`：连接、存储、append-only 或 PostgreSQL 错误。
- `2`：输入、schema 或记录契约无效。

## 4. 定时导入

预演并安装每 5 分钟执行的任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_database_import_task.ps1 -Workspace . -Interactive -WhatIf
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_database_import_task.ps1 -Workspace . -Interactive
```

当前任务名为 `FootballCups-Database-Import`。`-Interactive` 要求用户保持登录；长期无人值守前应在提升的 PowerShell 中移除 `-Interactive` 重装 S4U 模式。任务会先启动本地 PostgreSQL，再运行 `import-files`。卸载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_database_import_task.ps1 -Workspace . -Uninstall
```

## 5. As-Of 查询

```powershell
.\.venv\Scripts\football-cups-db.exe as-of `
  --workspace . `
  --fixture-id <fixture-id> `
  --cutoff <RFC3339-UTC-time> `
  --limit 1000
```

输出中的 `observed_after_cutoff` 和 `corrected_after_cutoff` 必须为 0。模型和回测只能通过相同过滤语义取数。

## 6. 外部 PostgreSQL

本地 D 盘集群存在且没有显式配置时，数据库 CLI 自动使用该集群。外部数据库使用未跟踪 `.env` 中的 `DATABASE_URL`，值采用带占位符的标准格式：

```text
postgresql://<user>:<password>@<host>:<port>/<database>
```

也可使用 libpq 的 `PGHOST`、`PGPORT`、`PGUSER`、`PGDATABASE` 和安全密码服务。真实连接串和密码不得写入文档、代码、Git、任务参数或日志。

## 7. 重建与故障处理

- `connection timeout expired`：先运行本地 `Start` 和 `Status`，再检查端口监听。
- manifest 内容变化：停止导入，核对文件事实，不更新数据库哈希绕过错误。
- JSONL 截断或尾部变化：停止采集和导入，保留现场；禁止手工推进检查点。
- schema migration hash 变化：新增后续迁移，不修改已经应用的迁移。
- 数据库损坏：保留故障目录，新建空集群/数据库，执行 `init` 和 `import-files`。
- 数据库备份只是加速恢复；原始 blob、manifest 和 JSONL 的异盘备份仍是最高优先级。

真实集成测试使用名称以 `_test` 结尾的隔离数据库：

```powershell
$env:PGHOST = '127.0.0.1'
$env:PGPORT = '55432'
$env:PGUSER = 'football_cups'
$env:PGDATABASE = 'football_cups_test'
$env:FOOTBALL_CUPS_TEST_DATABASE = '1'
.\.venv\Scripts\python.exe -m pytest -q
```

测试会删除并重建测试库中的 `football` schema，不得把这些变量指向主库。

## 8. 阿里云 Linux 约束

实际 ECS 使用 Ubuntu 22.04 和 PostgreSQL 17。正式集群数据目录位于 `/srv/football-cups/postgresql/17-main`，只监听 Unix socket 和 `127.0.0.1`；安全组和主机防火墙不得开放 5432。

2 vCPU / 4 GiB 默认使用 `shared_buffers=512MB`、`effective_cache_size=2GB`、`work_mem=8MB`、`maintenance_work_mem=128MB` 和 `max_connections=20`。数据库 service 可以失败并稍后补数，但不得阻止采集器继续保存文件事实。完整安装、数据盘和切换门禁见 `docs/cloud-migration-plan.md`。
