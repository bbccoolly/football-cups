# 项目当前状态

> 更新日期：2026-07-20
> 当前阶段：阶段 3 - 标准化数据库
> 阶段状态：正式数据库持续导入；隔离历史研究基线与影子预测能力已实施；采集验证并行运行

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
- [x] 完成 PostgreSQL 三个版本化迁移、九类核心实体、manifest 证据表、自动赛果资格视图和原始 JSONB 保留。
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
- [x] 记录 D-018，暂缓购买 ECS 数据盘并维持 Windows 本地正式采集。
- [x] 完成首个 24 小时发现子系统连续验证：2026-07-15 19:11 至 2026-07-16 19:11 Asia/Shanghai，完整发现成功率 100%，解析成功率 100%。
- [x] 记录 D-019，赛果闭环改为自动采集、自动验证和无法证明时自动隔离，不再设置人工赛果待办。
- [x] 完成日期直播页唯一 fixture、`status=4` 和固定比分节点解析，分析页降为一致性证据。
- [x] 完成赛事格式自动同步、`T+3h/T+6h/T+24h` 与 `R+2d` 至 `R+7d` 自动补偿、历史 `reconcile-results` 命令。
- [x] 完成 PostgreSQL `003_automated_results` 迁移、当前有效赛果视图和按切点严格资格视图。
- [x] 全自动赛果改动通过 58 项测试，包含真实 PostgreSQL 迁移、冲突排除、重复导入、SQLite 赛果状态重建和空库重放。
- [x] 完成真实自动赛果补偿：13 场中生成 10 条候选、4 条普通联赛已验证赛果、6 条可能加时自动隔离，0 程序失败、0 比分冲突。
- [x] 记录 D-020，实现备份一致性快照、共享锁等待、完成 manifest、备份年龄健康检查和 `RunnerSkip` 审计。
- [x] 确认 D 盘为物理磁盘 0、G 盘为物理磁盘 1，并配置 G 盘每日镜像与每周内容寻址目录。
- [x] 完成 G 盘真实双层备份和空目录恢复：1,385 个文件逐一通过 SHA-256，恢复 SQLite、日报和独立 PostgreSQL 空库重放成功。
- [x] Windows 备份与任务账户改动通过 73 项测试，包含真实 PostgreSQL 集成测试和 PowerShell 语法检查。
- [x] 完成四任务首轮 S4U 手工触发验证；该历史配置中的数据库任务随后由 D-021 兼容方案替代。
- [x] 从 S4U 内容寻址批次恢复 1,385 个文件，恢复库首次导入 56,498 条、重复导入 0 条、未知类型 0。
- [x] 创建专用非管理员 `football-cups-runner`，配置 Python 基础运行时最小 ACL，并将数据库任务改为 Windows LSA 加密密码登录；停止 PostgreSQL 后自动启动和增量导入返回 0。
- [x] 记录 D-022，允许隔离的公开历史研究与离线市场基线，但不解除正式阶段4门禁。
- [x] 实现 `football-cups-research` 来源注册、robots 缓存、跨进程预算、条件 GET、熔断、不可变存储和稳定命令。
- [x] 完成19个 Football-Data 静态资产低频下载：20次总请求含 robots、6,234,841字节、0拦截、0重试。
- [x] 导入五大联赛、八个额外联赛、世界杯工作簿和K1派生特征；历史层共7,404场/派生行、116,073条市场观察。
- [x] 完成 PostgreSQL `004`、`005` 研究迁移和独立 importer；首次123,497条、质量事件增量514条、重复导入0条，正式各切点计数保持4。
- [x] 生成覆盖和时间分离评估报告；世界杯514场口径歧义逐场形成质量事件。
- [x] 历史研究改动通过85项测试，包含正式与研究 importer 的真实 PostgreSQL 空库重放和隔离验证。
- [x] 完成历史研究数据统计整理，新增 `docs/historical-research-statistics.md`；重新生成 coverage 与 baseline 报告，确认研究层为7,404场/派生行、116,073条市场观察、6,890场研究可用赛果，Football-Data 2026 closing 去水市场 Log Loss 为0.9876。
- [x] 完成 Windows 重启无人值守验证：G盘映射保持、PostgreSQL自动启动、数据库导入返回0、采集在10分钟内恢复并刷新完整发现。
- [x] 记录 D-023，完成盘口标准化 V2：中文页可靠解码、亚盘/大小球数值化、三类非欧赔市场 HTML 直解析、公司角色分类和独立让球指数记录。
- [x] 完成141个既有市场 manifest 的全量离线审计与重放：零网络请求、零乱码、零已知盘口转换失败、零 HTML/Excel 数值差异。
- [x] 完成迁移006/007、V2 importer、current/as-of 与模型资格视图；导入11,616行V2公司盘口、426条市场标准化、130条唯一批次评估和940条让球指数，重复导入新增0。
- [x] 完成维护前后事实层核验：raw 1,098个、manifest 256个、非修复 normalized 23个文件与G盘维护前副本逐文件SHA-256差异为0。
- [x] 完成首个实时 V2 正式切点：fixture `1362048` 的 `T-12h` 三核心市场均接受，完整 bookmaker 为53/16/16，模型严格资格为 true。
- [x] 记录 D-024，赛事格式登记增加稳定 `competition_id` fallback，并在直播源遗漏 fixture 时使用 `shuju` 与 `ouzhi` 分析双端点一致比分补偿普通联赛赛果。
- [x] 完成 2026-07-15 至 2026-07-20 赛果补偿：新增 15 条候选和 14 条已验证赛果；`ResultCandidate=54`、`VerifiedResult=34`、`current_verified_results=34`、`unsupported_records=0`。
- [x] 记录 D-025，将 fixture `1358414` 按中国体彩人工核验证据追加标记为无效场次；取消4个待执行赛果任务，保留全部事实并从覆盖分母、模型资格和阶段门禁中排除。
- [x] 无效场次改动通过107项全量离线测试；迁移008已在主库应用，幂等复查返回 `unchanged`，`health=ok` 且采集与数据库任务继续正常运行。
- [x] 记录 D-026，并实现中国体彩官方 90 分钟赛果证据层：官方 scope、清单批次、fixture 精确映射、head/fixed 详情一致性、官方候选和自动 `VerifiedResult`。
- [x] 新增 `reconcile-results --source sporttery --dry-run/--apply`、`sporttery-smoke`、`audit-result-evidence`、数据库迁移009和 importer 类型分派；相关解析、映射和 typed insert 测试通过。
- [x] 安装 Playwright 1.61 并验证系统 Microsoft Edge headless 可启动；真实官方页面 scope 文本可见并已保存原始字节和哈希。
- [x] 记录 D-027，将体彩官方补偿按24小时节流接入 `run-once`；567/CORS/不完整清单改为来源失败，且不影响500主采集退出码。
- [x] 完成指定 fixture smoke、官方证据引用审计、历史无乱码 `FixtureIdentity` 溯源和迁移010；主库重复导入新增0且 `unsupported_records=0`。
- [x] 体彩官方证据改动通过120项全量测试，包含真实PostgreSQL迁移006中途导入、升级至010、官方typed insert、重复导入和空库重放；`git diff --check`与密钥扫描通过。
- [x] 记录 D-028，实现 `confirm-candidate-results`：只确认现有唯一且双页面一致的候选口径，不接收比分，最多100场并执行整批预检；声明固定为 `project-owner-manual-declaration/self_attestation`。
- [x] 完成迁移011、importer和 current/strict 视图收紧；旧 `manual`/`manual-import` 不具备严格资格，完整引用同 fixture、同比分候选的负责人声明可进入严格门禁。
- [x] 项目负责人声明的8场欧战/世界杯候选已一次批量确认，重复执行新增0；`ResultCandidate=54`保持不变，`VerifiedResult/current_verified_results`从34增至42。
- [x] 人工与自动指标已分离；体彩官方补偿会继续检查人工声明结果，官方一致时追加自动证据，不一致时产生冲突并退出严格资格。
- [x] 上线前后各完成一次G盘镜像和内容寻址备份；确认后批次分别为 `20260720T050659064134Z-4a9d06c5` 和 `20260720T050709000256Z-e0199b1c`，覆盖3397个文件且SQLite校验正常。
- [x] 记录 D-029，允许隔离 research-only 影子预测；不解除正式阶段4、Web/API 或投注输出门禁。
- [x] 完成迁移012、研究 flag V2 校验、模型数据集/版本/激活/影子预测/评估表和独立 importer 类型分派。
- [x] 实现 `football-cups-research build-model-dataset`、`train-model`、`shadow-predict` 和 `evaluate-shadow`；影子预测只读取 live V2 合格欧赔快照，并按真实发布窗口写入或 abstain。
- [x] 注册 `FootballCups-Research-Shadow-Prediction` 独立 Windows 任务；数据库任务和影子任务均使用 `football-cups-runner` 的 Task Scheduler `Password` 登录、`Limited`、`IgnoreNew`。
- [x] G 盘每日镜像和内容寻址备份纳入 `data/research`，以 `research/...` 前缀保存，并使用 `research-facts.lock` 防止研究写入并发。

## 当前目标

保持采集、数据库导入和影子预测任务连续运行，完成发现、赛果和盘口 V2 各自的连续子窗口。盘口 V2 的 7 天与 30 天子窗口从 2026-07-17 17:56:56 Asia/Shanghai 计时；历史离线重放不计入。持续积累自动候选、已验证赛果和隔离证据。阶段 4 仍需按切点至少 500 个不同 fixture 同时具有模型严格快照及唯一有效赛果；D-029 的影子预测只能用于研究观察，当前不得作为正式模型、Web/API 或阶段4结论。

阿里云杭州 ECS 已完成隔离 smoke，但根据 D-018 暂缓购买数据盘并暂停正式迁移。没有正式数据盘前，ECS 只作为部署验证和未来迁移预备环境；Windows 本地继续作为唯一正式采集写入者。未来重新启动云端切换前仍必须完成精确 24 小时窗口报告、人工抽查、至少 100 GB 数据盘、真实 OSS 恢复和云端完整 smoke。

## 当前运行证据

截至 2026-07-20 14:26 Asia/Shanghai：

- 本地增强 `health=ok`：最后心跳约77秒，完整发现和时钟检查约17分钟，SQLite `quick_check=ok`，0个逾期任务，56个未来待办；每日和每周G盘备份均为 `ok`。
- 人工确认当日日报中，自动已验证覆盖率保持 `88.8889%`，人工声明覆盖率单列为 `11.1111%`，合并已验证覆盖率为100%；体彩567和自动验收结论未被改写。
- 数据库导入检查点为69个文件、181,586行；主库 `records=181,586`、`collection_manifests=630`、`quality_events=10,977`、`ResultCandidate=54`、`VerifiedResult=42`、`current_verified_results=42`、`current_invalid_fixtures=1`、`unsupported_records=0`。
- 历史研究统计已重新生成：coverage 报告 `20260720T062410389935Z-b9974fa6`，baseline 报告 `20260720T062422647516Z-2c22adc4`；数据集哈希 `d58d611927407b6ce5a32047b1f7c831f8ddf48763a54c7e8c8d48a930ac5c84` 与既有研究层一致。
- 原8场口径歧义候选已经项目负责人声明为常规时间，新增8条 append-only 已验证结果；自动方法仍为 `500-two-page-regular-time-competition=30`、`500-analysis-pair-regular-time-competition=4`，人工方法为 `project-owner-manual-declaration=8`。
- 阶段 4 严格快照加已验证赛果计数：`T-48h=4`、`T-24h=25`、`T-12h=30`、`T-6h=41`、`T-3h=41`、`T-60m=41`、`T-30m=41`、`T-10m=41`；距离各切点500场门槛仍很远。
- 主库已应用迁移011；首次人工结果导入新增16条（8条结果、8条质量事件），第二次导入新增0。重复确认8场返回 `confirmed_count=0`、`unchanged_count=8`。
- fixture `1358414` 在四个500端点持续缺失或显示 `VS`，后经项目负责人在中国体彩竞彩足球赛果页核验为无效场次。10:00 Asia/Shanghai 执行幂等 `invalidate-fixture`，追加1条 `fixture_invalidated/excluded` 证据并取消4个待执行任务；重复执行返回 `unchanged`。
- 中国体彩官方页面通过标准 headless Edge 能看到“全场比分（90分钟）包含伤停补时阶段”，但页面自身的 `getUniformMatchResultV1` XHR 以及标准浏览器直接访问均返回 EdgeOne 567/CORS；`getMatchHeadV1` 同样567，`getFixedBonusV1` 当前仍为200。不能在缺少完整清单和双详情一致性时写入官方比分。
- 首次低频自动 `--apply` 处理7场歧义 fixture，保存3条 scope、3条不完整清单和7条失败映射证据，0条官方观察、0条官方候选、0条新增验证。修正后指定 fixture `1359167` smoke 返回 `partial/failure=1`；审计为 `warning`，明确报告3个不完整清单、7个非 accepted link和0个官方已验证赛果。
- 主库已应用迁移009/010/011；`sporttery_scope_evidence=3`、`sporttery_inventory_batches=3`、`sporttery_fixture_links=7`、`sporttery_result_observations=0`。人工确认不改变官方证据计数；下次自动官方补偿为2026-07-21 11:57 Asia/Shanghai，并会继续检查仍在8天窗口内的人工声明结果。

截至 2026-07-20 18:20 Asia/Shanghai 的影子预测实施证据：

- 主库已应用迁移012；`research.model_datasets=1`、`model_versions=1`、`model_activations=1`、`shadow_predictions=0`、`shadow_evaluations=1`，正式 `football.records` 未被研究导入污染。
- `devig-consensus-v1` 训练/激活完成：数据集哈希 `998a80d6d3aaae67285fd536232a32e97f9f6e3bb24d6544477447d1d176896a`，3396场多公司 closing 1X2 样本，训练2447场，2026时间分离评估949场。
- 模型版本 `devig-consensus-v1-998a80d6d3aa-20260720T101711Z`；训练 Log Loss `0.9727`，2026时间分离 Log Loss `0.9920`。该指标仅为研究观察，不是正式阶段4模型结论。
- 手工执行 `shadow-predict` 返回 `unchanged`，因为当前没有位于真实发布窗口的新机会；没有历史补发预测。
- `FootballCups-Research-Shadow-Prediction` 手工触发返回0；重新注册后的 `FootballCups-Database-Import` 手工触发也返回0。两者均为 `football-cups-runner`、`Password`、`Limited`、`IgnoreNew`。
- 研究导入幂等复查新增0；`strict_fixture_results_by_cutoff` 保持 `T-48h=4`、`T-24h=25`、`T-12h=30`、`T-6h/T-3h/T-60m/T-30m/T-10m=41`。

截至 2026-07-17 18:02 Asia/Shanghai 的盘口 V2 上线证据：

- 维护前增量备份 `20260717T094434311801Z-9acb12fc` 和内容寻址备份 `20260717T094444339203Z-31aceec3` 均完成，SQLite `quick_check=ok`。
- 主库在数据库任务停用前自动应用006/007，短时导致V2资格视图为空；未发生事实层丢失。修复批次首次导入因重试 manifest 重复生成批次评估而事务回滚，坏批次原样隔离，算法版本2按批次聚合后重新发布。
- 上线后重复重放测试发现批次摘要曾纳入已是V2的实时 manifest，产生一个未导入的冗余目录；该目录原样隔离，选择器已在摘要前排除V2 manifest，重复 `--apply` 现稳定返回原批次 `unchanged`。
- 有效修复批次为 `market-v2-c2992bbb5be5d6b7c20a89cb`：11,616行公司盘口、426条标准化、130条批次评估、940条让球指数，93个不同 fixture/切点获得模型资格。
- 数据库当前 V2 视图11,616行、记录ID全部唯一、有效自然键重复0、观察时间越界0、拒绝标准化泄漏0；`unsupported_records=0`。
- 17:56:56 恢复正式任务；采集与数据库任务手工及后续定时运行均返回0，`health=ok`、逾期任务0。
- 18:01:07 完成首个实时 `1362048/T-12h`：meta声明选择GB18030而拒绝错误Latin-1推断，三核心市场V2标准化均接受，模型严格资格为true。
- 首个 V2 精确窗口报告按日期、巴甲、四市场和 `T-12h` 输出完整拆分：市场数据完整率100%，亚盘/大小球盘口转换成功率100%，模型资格率100%，乱码0。
- 最终自动测试102项全部通过，包含真实 PostgreSQL 两阶段迁移、首次/重复导入、空库重放和 V2 as-of 查询。
- 最终上线后增量备份 `20260717T101941362183Z-04de621a` 和内容寻址备份 `20260717T102001624916Z-f81c3382` 均完成，共覆盖1,535个文件；SQLite `quick_check=ok`。

截至 2026-07-17 15:54 Asia/Shanghai 的 Windows 重启证据：

- 系统于15:49:06启动；D盘仍为物理磁盘0，G盘仍为物理磁盘1，两块NVMe均在线。
- `FootballCups-Database-Import` 于15:49:49自动运行并返回0；PostgreSQL在 `127.0.0.1:55432` 正常监听，随后数据库状态查询成功。
- `FootballCups-500-Collector` 于15:51:51和15:53:53自动运行并返回0；第二轮将完整发现刷新到15:53:34，健康状态为 `ok`、逾期任务0。
- 每日和每周备份任务保持 S4U、Limited、IgnoreNew；G盘备份可访问且每日/每周备份健康均为 `ok`。
- 重启恢复子项通过；本次重启没有形成“注销后持续至少10分钟”的独立证据，注销验收仍待执行。

截至 2026-07-17 15:11 Asia/Shanghai 的历史研究证据：

- 来源注册表不包含任何500历史URL；CentroQuote/OddsHarvester批量路线保持禁用。
- 公开静态层共7,404场/派生行：Football-Data 7,074场，K1 330场；2025年5,161，2026年2,243。
- 五大联赛按日期边界实得2,686场；旧摘要漏计2025-01-01 Brentford 1-3 Arsenal，已按原始CSV纠正。
- 市场观察116,073条；研究可用结果6,890场；世界杯及预选赛608场中514场因90分钟口径不足隔离。
- 研究库 `source_assets=20`、`fixtures=7074`、`market_observations=116073`、`feature_rows=330`、`quality_events=514`。
- 2026 Football-Data closing 去水市场 Log Loss 0.99，优于赛事先验1.07和均匀基线1.10；K1 102场派生市场基线为1.06，只作为小样本研究结论。
- 研究导入前后 `T-24h/T-6h/T-60m/T-10m` 等正式切点计数均保持4，未污染 `football` schema。

截至 2026-07-17 13:32 Asia/Shanghai：

- 本地增强 `health` 返回 `ok`：SQLite `quick_check=ok`，最后心跳年龄约 75 秒，完整发现和时钟校验年龄约 16 分钟，0 个逾期任务，180 个未来待办，磁盘状态正常。
- 赛果修复部署期间两个 Windows 任务已暂停并完成 SQLite 在线备份；2026-07-17 10:44:44 Asia/Shanghai 恢复并手工触发后持续返回 `0`，采集和数据库导入任务在10:58:58自动运行均为 `0`。正式30天赛果窗口从10:44:44计时。
- 2026-07-17 自然日日报：运行 299 次，完整发现成功率 100%，HTTP 成功率 89.2308%，解析成功率 100%，候选赛果覆盖率 0%。
- 2026-07-15 19:11 至 2026-07-16 19:11 Asia/Shanghai 的首个 24 小时发现验证窗口：运行 765 次，完整发现成功率 100%，HTTP 成功率 96.1983%，解析成功率 100%；其中 `runner:partial=3`、`snapshot_batch:partial=3`、`source_http_date_stale:warning=82`，均需继续跟踪但不推翻发现子系统 24 小时结论。
- 从 2026-07-16 19:11 Asia/Shanghai 到 2026-07-17 09:21 Asia/Shanghai 的进行中窗口报告：运行 452 次，完整发现成功率 100%，HTTP 成功率 89.6721%，解析成功率 100%，候选赛果覆盖率 0%。
- 数据库导入状态：23 个文件、55,215 行检查点、55,215 条记录；其中 `fixture_identities=1024`、`collection_manifests=222`、`market_snapshots=367`、`bookmaker_market_rows=8097`、`snapshot_batches=115`、`quality_events=3965`、`result_candidates=10`、`verified_results=4`、`unsupported_records=0`。
- 主库已应用迁移 `003_automated_results`；`current_verified_results=4`，`T-24h`、`T-12h`、`T-6h`、`T-3h`、`T-60m`、`T-30m`、`T-10m` 的严格 fixture 赛果计数均为 4。
- 赛事格式已同步到 SQLite：美职足、巴甲、挪超、瑞超为 `regular_time_only`；欧冠、欧罗巴、世界杯为 `may_have_extra_time`。
- 新版真实四市场 smoke 对 fixture `1362704` 全部成功；HTML 日期直播页切换清单后不再返回旧 fixture，系统随后使用页面自身的日期 Full 数据流完成同源回退。
- `reconcile-results` 重查 2026-07-15 至 2026-07-17 的 13 个已开赛 fixture，生成 10 条候选；其中 4 场普通联赛自动验证，6 场欧冠/欧罗巴/世界杯自动隔离，另 3 场来源仍未给出可用完场结果。所有新证据使用真实观察时间。
- 文件事实层当前约 121.08 MB，其中 raw 约 70.67 MB、normalized 约 31.09 MB、manifest 约 2.19 MB；D 盘剩余约 356.69 GB。
- G 盘增量镜像批次 `20260717T034613254060Z-7163a59e` 完成 1,385 个文件，SQLite `quick_check=ok`；内容寻址批次 `20260717T034744341329Z-e1f9915b` 写入 1,303 个对象、复用 82 个对象并完成逐文件恢复。
- 恢复副本重建 29 个 fixture；独立数据库 `football_cups_restore_20260717_test` 首次导入 56,489 条，重复导入 0 条，`unsupported_records=0`，`current_verified_results=4`。
- 最新 `health=ok`：每日与每周备份状态均为 `ok`，G 盘备份可用空间约 457 GB。采集和两个备份任务为 `S4U`；数据库任务为专用非管理员 `football-cups-runner` 的 `Password` 登录，四个任务均为 `Limited` 和 `IgnoreNew`。
- S4U 批次 `20260717T040531490919Z-f63678b0` 完成增量镜像，`20260717T040535988384Z-ac0d321e` 完成内容寻址备份；后者恢复到新的空目录并重建 29 个 fixture。
- S4U 恢复数据库 `football_cups_s4u_restore_20260717_test` 在迁移后首次导入连接瞬时超时一次；55432 监听和 PostgreSQL 进程持续正常，立即重试成功导入 56,498 条且第二次为 0，没有半批检查点。
- 停止 PostgreSQL 后执行旧数据库 S4U 任务，确认 `lcz` 虽为 `RunLevel=Limited` 仍携带管理员组身份，PostgreSQL 明确拒绝启动。当前主机又拒绝为另一个本地标准账户注册 S4U，因此新增 D-021：数据库任务改用专用非管理员账户和 Task Scheduler 加密密码登录。
- 2026-07-17 13:24:35，数据库任务在 PostgreSQL 已停止状态下自动启动服务器，13:24:37 返回 0；随后检查点更新到 58,918 条且 `unsupported_records=0`。13:32 的后续周期任务继续返回 0。

2026-07-15 19:11 至 2026-07-16 19:11 Asia/Shanghai 可作为首个 24 小时发现子系统连续验证结论；这不代表 7 天技术验收、30 天稳定性验收、赛果闭环或模型门禁通过。自然日质量日报不能代替精确窗口统计；2026-07-16 19:11 起的新窗口仍在积累中。

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

- 24 小时发现子系统连续验证已通过；7 天技术验收正在积累但尚未完成，采集器不能标记为技术通过。
- 30 天稳定性验收尚未完成；赛果子窗口必须从全自动赛果修复版恢复定时运行后重新计时，D-015 不解除采集器生产、模型或 Web 门禁。
- 盘口 V2 的7天技术子窗口和30天切点资格子窗口从2026-07-17 17:56:56重新计时；最早分别于2026-07-24和2026-08-16同一时刻评估。
- 本地 PostgreSQL 使用 trust 认证且仅绑定回环地址；不得转发或暴露端口。
- PostgreSQL 程序和主/测试/重放数据库当前约占 D 盘 1.05 GB；数据库可重建，但文件事实层仍必须异盘备份。
- 当前有42场具有唯一有效赛果、1场无效 fixture 被逻辑排除；其中34场为自动验证、8场为负责人声明。V2 模型合格批次与赛果交集仍远低于“同切点模型严格快照+唯一赛果”的500场门禁。
- 本地 Windows 长期运行需要人工保证联网、不休眠和系统时间准确。
- 采集和两个备份 S4U 任务、真实备份批次恢复、专用非管理员数据库任务自动启动及 Windows 重启恢复均已通过；注销 10 分钟完成前，30 天无人值守门禁保持未通过。
- 本地 `FOOTBALL_CUPS_OSS_BACKUP_DIR` 和内容寻址恢复已完成；私有阿里云 OSS 的真实上传下载仍暂停并继续阻止云端正式切换。
- ECS 当前只有 40 GB 系统盘；根据 D-017 和 D-018，只能用于隔离 smoke，暂不购买数据盘，至少 100 GB 数据盘挂载并通过重启验证前不得启用正式 timer。
- ECS 尚未安装 PostgreSQL 17、私有 OSS/RAM Role 和正式环境文件；这些工作暂停，必须等待正式数据盘和重新启动云端迁移决策。
- ECS smoke 健康状态为 `warning`，唯一原因是隔离环境未执行正式 `run-once`，因而没有心跳；这不等于正式运行健康验收通过。
- 备份代码、S4U/Password 任务和真实任务批次恢复均已完成；后续必须持续检查备份年龄，并完成注销及重启验证。
- 影子预测任务已注册并返回0，但当前 `shadow_predictions=0`，尚未经历自然切点内的真实发布。首次发布前不能评价实时概率稳定性。
- HTML 日期直播页按当前竞彩清单工作；清单切换后使用同源 Full 数据流补偿。前瞻任务和最多 7 天自动补偿仍必须用真实覆盖率验证，修复后取得的旧结果不能倒填为 24 小时成功。
- 杯赛及赛事格式未知的比分默认自动隔离，不创建人工确认待办；项目负责人可按 D-028 主动声明现有候选口径，但不能手工补写比分。中国体彩 scope 浏览器证据已通过，但官方清单和 head API 当前被 EdgeOne 567 阻断；系统每日低频自动重试并保存失败证据，禁止代理、stealth、Cookie/Token 重放或验证码处理。首次完整官方成功前，官方来源子窗口不能起算。
- 广泛赛事可能来源缺盘；必须与程序失败分开统计。
- 首批 10 场中有 1 场亚盘、大小球和让球指数导出持续返回 HTTP 500；已保留欧赔和失败证据并标记 `partial`。

## 人工待办

- [ ] 2026-07-16 19:11 Asia/Shanghai 后完成首日页面比赛数量和至少 3 场解析数据核验。
- [ ] 7 天验证期间每天继续核对一次网页比赛数量和至少 3 场解析数据，累计至少 21 场。
- [ ] 确认 Windows 在验证期间不休眠、保持登录并保持网络连接。
- [x] 设置 `FOOTBALL_CUPS_BACKUP_DIR` 到独立 G 盘并完成增量镜像。
- [x] 设置本地 `FOOTBALL_CUPS_OSS_BACKUP_DIR` 并完成内容寻址空目录恢复和数据库重放。
- [ ] 阿里云迁移前完成私有 OSS 的真实上传、重新下载和 SHA-256 恢复。
- [x] 在 ECS 安装项目虚拟环境和无密钥 smoke 配置，完成隔离 `discover + smoke-live`。
- [ ] 若未来重新启动云端正式迁移，先增加至少 100 GB ESSD，人工确认设备后运行数据盘安全脚本并完成重启挂载验证。
- [ ] 若未来重新启动云端正式迁移，创建杭州私有 OSS Bucket 和最小权限 ECS RAM Role，完成真实上传、重新下载及 SHA-256 恢复。
- [x] 在提升的 PowerShell 中把采集、每日备份和每周备份任务注册为 S4U。
- [x] 创建 `football-cups-runner`，将数据库任务改绑专用非管理员加密密码登录，并验证停止 PostgreSQL 后能够自动启动和导入。
- [ ] 注销 Windows 用户至少 10 分钟，重新登录后确认采集至少两轮、数据库至少一轮且 `health=ok`。
- [x] 安排一次 Windows 重启，确认 G 盘映射、PostgreSQL 自动启动、四个无人值守任务和采集心跳恢复。

## Agent 唯一下一步

观察 `FootballCups-Research-Shadow-Prediction` 的首个真实发布窗口：若自然切点内存在 live V2 合格欧赔快照，则确认只追加一条 research-only 影子预测并随后由研究 importer 入库；若无窗口则保持 `unchanged`。同时继续等待 2026-07-21 11:57 Asia/Shanghai 的低频体彩官方补偿，不扩大访问频率；阿里云 ECS 保持所有 timer 禁用。

## 恢复工作时首先执行

1. 按 `AGENTS.md` 顺序读取权威文档。
2. 检查 Git 和本地 `data/500/` 状态。
3. 运行 `football-cups-collector report-daily` 查看采集心跳，并运行 `football-cups-db status` 查看数据库导入状态。
4. 从本页“Agent 唯一下一步”继续，不重复历史 URL 调研，不越过阶段门禁。
