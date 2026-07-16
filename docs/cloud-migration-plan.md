# 阿里云杭州迁移前准备计划

> 版本：V1.0
> 状态：准备中
> 更新日期：2026-07-16

本文记录从本地 Windows 验证环境迁移到阿里云 ECS 前必须完成的准备、验证和切换规则。迁移不改变产品边界：预测只限常规时间 90 分钟及补时，不输出投注金额、组合、成本、收益、ROI 或回报承诺。

## 1. 目标架构

首版云端采用单台阿里云杭州 ECS，同机运行采集器和 PostgreSQL 17。

| 项目 | 默认值 |
| --- | --- |
| 地域 | `cn-hangzhou` |
| 系统 | Ubuntu 24.04 LTS x86_64 |
| 规格 | 2 vCPU / 4 GiB |
| 磁盘 | 40 GB 系统盘 + 100 GB ESSD PL0 数据盘 |
| 数据库 | PostgreSQL 17，本机 Unix socket 或回环地址 |
| 备份 | 私有 OSS + 本地 G 盘迁移副本 |
| 当前不部署 | RDS、Docker、Web、域名、负载均衡 |

正式切换后只能保留一个采集写入者。本地可以继续作为只读核验和冷备，但不得与云端同时写入同一套前瞻事实。

## 2. 切换前硬门槛

- 本地完成精确 24 小时窗口报告，不能使用自然日日报替代。
- 人工完成首日页面比赛数量和至少 3 场解析数据核验。
- `football-cups-collector health` 显示 SQLite、心跳、磁盘和任务状态正常。
- `football-cups-db status` 显示数据库导入无空窗。
- 本地 G 盘迁移副本可恢复，OSS 风格备份可恢复到空目录。
- 云端能通过六个竞彩页面、四市场、完场页和分析页 smoke test。
- 云端 NTP、磁盘挂载、systemd timer、PostgreSQL、OSS 凭据方式和告警检查完成。

未满足以上门槛前，不停止本地采集任务，不宣称云端接管。

## 3. 新增稳定命令

```text
football-cups-collector report-window --start <RFC3339> --end <RFC3339>
football-cups-collector smoke-live --active-fixture-id <id> --completed-fixture-id <id>
football-cups-collector backup-oss
football-cups-collector verify-oss-backup --run-id <id> --target <empty-dir>
football-cups-collector health
```

`report-window` 用于精确 24 小时统计。`backup-oss` 在本地生成与 OSS 对象存储一致的内容寻址布局；真实上云时可用 `ossutil` 将该布局同步到私有 Bucket。

## 4. OSS 备份契约

备份根目录结构固定为：

```text
<Prefix>/
  objects/sha256/<first-two>/<sha256>
  runs/<backup-run-id>/manifest.json
  runs/<backup-run-id>/complete.json
```

只有 `complete.json` 存在且其中的 `manifest_sha256` 与 `manifest.json` 实际哈希一致时，备份批次才有效。恢复必须逐文件校验 SHA-256。无完成标记、中断批次、哈希不一致或缺对象的备份不得恢复为有效数据。

`FOOTBALL_CUPS_OSS_BACKUP_DIR` 是当前实现的本地 OSS 布局根目录。云端正式使用 OSS 时，优先使用 ECS RAM Role 和杭州同地域内网 Endpoint：

```text
https://oss-cn-hangzhou-internal.aliyuncs.com
```

真实 AccessKey、Secret、STS Token、数据库密码和 SSH 私钥不得进入代码、文档、Git、日志或聊天。

## 5. Linux 运行规则

- 使用专用 Linux 用户运行采集器和导入器。
- 工作目录固定为仓库目录，数据目录挂载到独立数据盘。
- systemd timer 每 2 分钟执行 `run-once`，每 5 分钟执行数据库导入。
- `run-once` 单次最长 100 秒；systemd 层额外设置超时保护。
- 退出码 `0` 不重试；`1` 可 5 分钟后重试最多 3 次；`2` 不自动重试并告警；`3` 阻止市场下载，保留发现和健康检查。
- 锁文件记录 `hostname`、`boot_time`、`pid` 和 `process_create_time`；ECS 重启后的旧锁可自动恢复，同启动周期活进程锁不可删除。
- PostgreSQL 是可重建分析层，不能替代原始 blob、manifest 和 JSONL。

## 6. 迁移步骤

1. 本地完成精确 24 小时窗口报告：

   ```powershell
   .\.venv\Scripts\football-cups-collector.exe report-window --workspace . --start <UTC-start> --end <UTC-end>
   ```

2. 本地生成 G 盘副本和 OSS 风格备份，并恢复到空目录验证：

   ```powershell
   .\.venv\Scripts\football-cups-collector.exe backup --workspace .
   .\.venv\Scripts\football-cups-collector.exe backup-oss --workspace .
   .\.venv\Scripts\football-cups-collector.exe verify-oss-backup --workspace . --run-id <run-id> --target <empty-dir>
   ```

3. 购买 ECS 和创建私有 OSS Bucket。不要在采购前写入真实密钥到仓库。

4. 云端安装 Python 3.11+、PostgreSQL 17、Git、`ossutil 2.x`，挂载数据盘并校准时间。

5. 克隆仓库，创建 `.env`，只写入云端真实路径和本机安全配置。

6. 从 G 盘副本或 OSS 恢复 `data/500/`，运行：

   ```bash
   football-cups-collector rebuild-state --workspace .
   football-cups-db init --workspace .
   football-cups-db import-files --workspace .
   football-cups-collector health --workspace .
   ```

7. 选择一个当前活跃 fixture 和一个已完赛 fixture，在云端执行 smoke test：

   ```bash
   football-cups-collector smoke-live --workspace . --active-fixture-id <id> --completed-fixture-id <id>
   ```

8. 安装 systemd timer，先短时间观察日志和心跳。

   仓库提供基础模板：

   ```text
   scripts/linux/football-cups-collector.service
   scripts/linux/football-cups-collector.timer
   scripts/linux/football-cups-db-import.service
   scripts/linux/football-cups-db-import.timer
   ```

9. 停止本地 Windows 采集任务，记录切换时间，再启用云端正式采集。数据库导入随云端采集运行。

10. 云端正式切换后，7 天技术验收和 30 天稳定性验收重新计时。本地和云端运行时间不得拼接为连续验收。

## 7. 回滚规则

如果云端连续失败、时钟漂移、磁盘不足、OSS 备份不可恢复或 500 页面被云端网络拦截，则停止云端采集，保留云端数据目录和日志，恢复本地 Windows 单写入者。回滚后必须在 `docs/project-status.md` 记录切换时间、失败原因和新的唯一下一步。

## 8. 当前待办

- [ ] 等待本地精确 24 小时窗口结束后生成 `report-window` 报告。
- [ ] 人工核验至少 3 场比赛页面数量和解析字段。
- [ ] 配置本地 G 盘 `FOOTBALL_CUPS_BACKUP_DIR` 和 `FOOTBALL_CUPS_OSS_BACKUP_DIR` 测试目录。
- [ ] 完成一次 G 盘恢复和一次 OSS 风格恢复演练。
- [ ] 采购 ECS 前再次确认预算、地域、磁盘和备份策略。
