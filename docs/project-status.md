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
- [x] 根据实际 ECS 记录 D-017，将 Ubuntu 22.04 和 40 GB 单系统盘限定为隔离 smoke 环境。
- [x] 增加可配置磁盘阈值、必需挂载点门禁和带时效判断的 `health` 状态。
- [x] 修正 systemd 重试限制位置，采集器不再依赖 PostgreSQL，并限制正式服务写入数据盘。
- [x] 增加 Ubuntu smoke、数据盘安全准备、systemd 安装和 OSS 往返校验脚本。
- [x] 云迁移 V1.1 改动通过 45 项测试，包含真实隔离 PostgreSQL 集成测试；Bash 语法和文档链接检查通过。
- [x] ECS 完成 Ubuntu 软件包更新、Python 3.11.15、2 GiB swap 和 `football-cups` 服务用户初始化；系统未要求重启。
- [x] ECS fast-forward 到发布提交 `7395407`，完成 Python 3.11 虚拟环境、无密钥 smoke 配置和只读代码权限；云端离线测试 45 项通过、1 项 PostgreSQL 集成测试按预期跳过。
- [x] ECS 隔离发现 smoke 通过：六个竞彩页面全部成功、每页 20 场、正则与 DOM 清单一致、0 身份冲突。
- [x] ECS 四市场与赛果 smoke 通过：欧赔 56 行、亚盘 7 行、大小球 5 行、让球指数 29 行，完场页与分析页均成功。

## 当前目标

保持采集和数据库导入任务连续运行，完成 24 小时及后续采集验证、赛事格式登记和 90 分钟赛果闭环。阶段 4 仍需至少 500 场严格快照及已验证赛果，当前不得开始模型。

阿里云杭州 ECS 已完成隔离 smoke，但根据 D-018 暂缓购买数据盘并暂停正式迁移。没有正式数据盘前，ECS 只作为部署验证和未来迁移预备环境；Windows 本地继续作为唯一正式采集写入者。未来重新启动云端切换前仍必须完成精确 24 小时窗口报告、人工抽查、至少 100 GB 数据盘、真实 OSS 恢复和云端完整 smoke。

## 当前运行证据

截至 2026-07-16 09:50 Asia/Shanghai：

- Windows 任务最后一次返回码为 `0`，无漏跑，最后心跳为 09:50。
- 验证窗口内发现轮次为 28/28 完整成功；累计发现 10 场，无身份冲突。
- 验证窗口内 `run-once` 为 437/440 成功，3 次 `partial` 来自同一比赛的来源端市场 HTTP 500。
- HTTP 请求为 320/336 成功，标准化解析为 168/168 成功；两者必须按来源失败和程序解析分别解释。
- SQLite `quick_check` 为 `ok`，离线测试 18 项通过。

以上是未满 24 小时的中期证据，不是验收结论。自然日质量日报包含验证开始前的 smoke test，不得代替精确的 24 小时窗口统计。

截至 2026-07-16 18:07 Asia/Shanghai 的 ECS smoke 证据：

- Ubuntu 22.04.5、2 vCPU / 4 GiB、40 GB 系统盘约 35 GB 可用，仍无数据盘。
- NTP 已同步，时区为 Asia/Shanghai。
- 已安装 Python 3.11.15，2 GiB `/swapfile` 已启用，`football-cups` 系统用户存在。
- 已更新 53 个 Ubuntu 软件包；`cloud-init` 保持暂缓更新，系统未生成 `/var/run/reboot-required`。
- ECS 仓库已 fast-forward 并核对发布提交 `7395407`，Git 工作区干净；服务用户不能写入代码目录。
- 云端离线测试结果为 45 passed、1 skipped；跳过项是当前未安装 PostgreSQL 时的集成测试。
- 六个发现页面均 HTTP 200，全部返回同一组 20 场比赛，正则与 DOM 清单一致且无身份冲突。
- 活跃 fixture `1363485` 的四市场全部成功；完场 fixture `1438672` 的完场页和分析页均成功。
- smoke 日报显示发现、HTTP 获取和解析成功率均为 100%；隔离数据约 4.5 MB，14 个 blob，健康状态仅因未运行 `run-once` 缺少心跳而为 `warning`。
- 当前没有启用云端 systemd timer，本地 Windows 仍是唯一正式采集写入者。

2026-07-16 17:03 Asia/Shanghai 本地增强 `health` 返回 `ok`：SQLite 正常、心跳年龄约 76 秒、完整发现和时钟校验年龄约 23 分钟、191 个未来待办、0 个逾期任务、磁盘正常。

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
- ECS 当前只有 40 GB 系统盘；根据 D-017 和 D-018，只能用于隔离 smoke，暂不购买数据盘，至少 100 GB 数据盘挂载并通过重启验证前不得启用正式 timer。
- ECS 尚未安装 PostgreSQL 17、私有 OSS/RAM Role 和正式环境文件；这些工作暂停，必须等待正式数据盘和重新启动云端迁移决策。
- ECS smoke 健康状态为 `warning`，唯一原因是隔离环境未执行正式 `run-once`，因而没有心跳；这不等于正式运行健康验收通过。
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
- [x] 在 ECS 安装项目虚拟环境和无密钥 smoke 配置，完成隔离 `discover + smoke-live`。
- [ ] 若未来重新启动云端正式迁移，先增加至少 100 GB ESSD，人工确认设备后运行数据盘安全脚本并完成重启挂载验证。
- [ ] 若未来重新启动云端正式迁移，创建杭州私有 OSS Bucket 和最小权限 ECS RAM Role，完成真实上传、重新下载及 SHA-256 恢复。
- [ ] 长期无人值守前，在提升的 PowerShell 中把采集和数据库导入任务都重装为 S4U 模式。

## Agent 唯一下一步

继续保持 Windows 本地采集和数据库导入连续运行；完成首日人工页面数量和至少 3 场解析数据核验，并开始配置异盘或网络备份目录。阿里云 ECS 暂停正式迁移，继续保持所有云端 timer 禁用并保留 Windows 单写入者。

## 恢复工作时首先执行

1. 按 `AGENTS.md` 顺序读取权威文档。
2. 检查 Git 和本地 `data/500/` 状态。
3. 运行 `football-cups-collector report-daily` 查看采集心跳，并运行 `football-cups-db status` 查看数据库导入状态。
4. 从本页“Agent 唯一下一步”继续，不重复历史 URL 调研，不越过阶段门禁。
