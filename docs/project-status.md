# 项目当前状态

> 更新日期：2026-07-16
> 当前阶段：阶段 3 - 标准化数据库
> 阶段状态：数据库首版通过真实 PostgreSQL 验收并持续导入；采集验证并行运行

本文件是当前进度的唯一入口。产品边界见 `docs/product-plan.md`，阶段门槛见 `docs/execution-plan.md`，历史决策见 `docs/decision-log.md`。

## 已完成

- [x] 仓库治理、Git 忽略和 Agent 恢复协议。
- [x] 纯盘口、90 分钟、append-only 和 observed-at 防泄漏边界。
- [x] Football-Data 五大联赛 10 个 CSV 下载和范围统计。
- [x] OddsHarvester/CentroQuote 单场三市场、历史变盘和 50 场批量研究。
- [x] 确认 CentroQuote 批量成功率 46%，只作为历史辅助研究。
- [x] 分析 `D:\2026-worldCup` 的 500 四市场抓取与赛果记录实现。
- [x] 实测 500 竞彩六个玩法入口可返回带稳定 fixture ID 的比赛行。
- [x] 确认 500 完场页和分析页可按 fixture ID 提供候选比分证据。
- [x] 完成 500 全赛事长期采集计划 V2 的设计与审查。
- [x] 完成六个竞彩玩法入口真实 smoke test：10 场、6/6 来源成功、0 身份冲突。
- [x] 完成首批四市场 smoke test：6 场三核心市场全部成功；让球指数缺失或失败独立记录。
- [x] 完成验证采集器 CLI、不可变 blob、标准化 JSONL、SQLite 调度、日报、备份和状态重建。
- [x] 完成 10 场首见市场闭环：9 场三核心市场完整，1 场来源持续失败后标记 `partial`。
- [x] Python 3.11 项目 `.venv` 安装完成，离线测试 18 项通过，依赖检查无冲突。
- [x] 安装 `FootballCups-500-Collector` 交互式验证任务，首次运行结果为 0。
- [x] 记录 D-015，明确提前建设数据库不等于采集验收通过。
- [x] 完成 PostgreSQL 两个版本化迁移、九类核心实体、manifest 证据表和原始 JSONB 保留。
- [x] 完成 JSONL 字节检查点、文件事务、`record_id` 幂等、append-only 校验和并发 advisory lock。
- [x] 在 D 盘安装 PostgreSQL 17.10，本地仅监听 `127.0.0.1:55432`。
- [x] 完成首次导入、重复导入和独立空库全量重建；最终空库重建为 81 个 manifest、17,252 条记录、0 个未知类型。
- [x] 完成损坏文件回滚、并发锁和 as-of 截止后零越界验证，真实集成测试 30 项通过。
- [x] 安装 `FootballCups-Database-Import` 交互式任务，每 5 分钟增量导入，首次任务结果为 0。
- [x] 记录 D-016，确定阿里云杭州单机 ECS 迁移准备路线。
- [x] 新增 `docs/cloud-migration-plan.md`，同步迁移前硬门槛、OSS 备份契约、systemd 运行和回滚规则。
- [x] 新增迁移准备命令接口：`report-window`、`health`、`backup-oss`、`verify-oss-backup` 和 `smoke-live`。
- [x] 新增 Linux systemd 采集与数据库导入 timer 模板。

## 当前目标

保持采集和数据库导入任务连续运行，完成 24 小时及后续采集验证、赛事格式登记和 90 分钟赛果闭环。阶段 4 仍需至少 500 场严格快照及已验证赛果，当前不得开始模型。

同时准备阿里云迁移，但正式切换前必须先完成精确 24 小时窗口报告、人工抽查、备份恢复和云端 smoke test。云端切换后 7 天和 30 天验收重新计时。

## 当前运行证据

截至 2026-07-16 09:50 Asia/Shanghai：

- Windows 任务最后一次返回码为 `0`，无漏跑，最后心跳为 09:50。
- 验证窗口内发现轮次为 28/28 完整成功；累计发现 10 场，无身份冲突。
- 验证窗口内 `run-once` 为 437/440 成功，3 次 `partial` 来自同一比赛的来源端市场 HTTP 500。
- HTTP 请求为 320/336 成功，标准化解析为 168/168 成功；两者必须按来源失败和程序解析分别解释。
- SQLite `quick_check` 为 `ok`，离线测试 18 项通过。

以上是未满 24 小时的中期证据，不是验收结论。自然日质量日报包含验证开始前的 smoke test，不得代替精确的 24 小时窗口统计。

## 当前阻塞与风险

- 7 天技术验收尚未开始，采集器不能标记为技术通过。
- 30 天稳定性验收尚未开始；D-015 只提前解除 PostgreSQL 数据库建设门禁，不解除采集器生产、模型或 Web 门禁。
- 本地 PostgreSQL 使用 trust 认证且仅绑定回环地址；不得转发或暴露端口。
- PostgreSQL 程序和主/测试/重放数据库当前约占 D 盘 1.05 GB；数据库可重建，但文件事实层仍必须异盘备份。
- 当前数据库中 `ResultCandidate=0`、`VerifiedResult=0`，阶段 4 的 500 场门禁尚不具备任何有效赛果样本。
- 本地 Windows 长期运行需要人工保证联网、不休眠和系统时间准确。
- 当前 Codex 进程非管理员，S4U 任务注册被 Windows 拒绝；24 小时验证使用显式 `-Interactive` 回退，30 天验收前必须提升权限重装默认模式。
- 尚未提供另一物理磁盘或网络备份目录；这不阻止开发和 7 天验证，但阻止 30 天验收通过。
- 尚未配置 `FOOTBALL_CUPS_OSS_BACKUP_DIR` 并完成 OSS 风格备份恢复；这阻止阿里云正式切换。
- 当前任务只运行 `run-once`，会自动生成自然日日报，但不会自动执行异盘备份；30 天验收前必须补充独立备份调度并完成恢复演练。
- 杯赛及赛事格式未知的比分不能自动视为 90 分钟赛果。
- `config/competition-formats.json` 尚未登记当前出现的美职足、欧冠、巴甲、挪超和世界杯赛事格式。
- 广泛赛事可能来源缺盘；必须与程序失败分开统计。
- 首批 10 场中有 1 场亚盘、大小球和让球指数导出持续返回 HTTP 500；已保留欧赔和失败证据并标记 `partial`。

## 人工待办

- [ ] 2026-07-16 19:11 Asia/Shanghai 后完成首日页面比赛数量和至少 3 场解析数据核验。
- [ ] 7 天验证期间每天继续核对一次网页比赛数量和至少 3 场解析数据，累计至少 21 场。
- [ ] 确认 Windows 在验证期间不休眠、保持登录并保持网络连接。
- [ ] 确认当前赛事格式，并由 Agent 写入 `config/competition-formats.json`；新赛事出现时继续登记。
- [ ] 30 天验收前设置 `FOOTBALL_CUPS_BACKUP_DIR`，指向另一物理磁盘或网络路径。
- [ ] 阿里云迁移前设置 `FOOTBALL_CUPS_OSS_BACKUP_DIR`，执行 `backup-oss` 并恢复到空目录验证。
- [ ] 采购 ECS 前确认阿里云杭州规格、数据盘、OSS Bucket 和 RAM Role 方案。
- [ ] 长期无人值守前，在提升的 PowerShell 中把采集和数据库导入任务都重装为 S4U 模式。

## Agent 唯一下一步

在 2026-07-16 19:11 Asia/Shanghai 后运行 `football-cups-collector report-window --start <UTC-start> --end <UTC-end>` 生成精确 24 小时采集评估，完成首日人工抽查，并执行本地 G 盘和 OSS 风格备份恢复验证；通过后再进行阿里云采购和云端 smoke test。

## 恢复工作时首先执行

1. 按 `AGENTS.md` 顺序读取权威文档。
2. 检查 Git 和本地 `data/500/` 状态。
3. 运行 `football-cups-collector report-daily` 查看采集心跳，并运行 `football-cups-db status` 查看数据库导入状态。
4. 从本页“Agent 唯一下一步”继续，不重复历史 URL 调研，不越过阶段门禁。
