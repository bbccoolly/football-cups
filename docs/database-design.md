# 标准化数据库设计

> 版本：V1.8
> 更新日期：2026-07-21
> PostgreSQL：17

## 1. 定位与边界

PostgreSQL 是从 `data/500/manifests/` 和 `data/500/normalized/` 重建的分析层，不是原始事实来源。删除数据库后必须能够从文件层恢复；数据库写入不得反向修改 blob、manifest、JSONL、已验证赛果或采集调度 SQLite。

阶段 3 只解决结构化查询、重放和 as-of 防泄漏。它不解除模型或 Web 门禁，也不代表 500 的 24 小时、7 天或 30 天采集验收已经通过。

公开历史数据和 D-029 影子预测位于独立 `research` schema，使用独立 importer、记录类型和检查点。研究 importer 不调用正式 `insert_record()`，不得写入 `football.records`，不得与正式资格视图建立联合视图。

## 2. 数据流

```text
500 HTTP response
  -> immutable blob + manifest
  -> normalized append-only JSONL
  -> checkpointed PostgreSQL importer
  -> typed tables + original JSONB payload
  -> as-of query functions
```

导入器不嵌入采集进程。采集器即使在 PostgreSQL 停止时也必须继续保存文件事实；数据库恢复后再从检查点补入。

## 3. Schema

所有对象位于 `football` schema。

| 对象 | 作用 |
| --- | --- |
| `schema_migrations` | 迁移版本、名称和不可变 SHA-256 |
| `import_runs` | 每次 JSONL 导入状态、提交数量和失败原因 |
| `import_checkpoints` | 每个 JSONL 的字节偏移、行号和最后记录 |
| `collection_manifests` | 不可变发现、市场和赛果 manifest 证据 |
| `records` | 所有标准化记录的全局 `record_id`、原始 JSONB 和来源位置 |
| `fixture_identities` | 比赛身份观察版本 |
| `discovery_observations` | 六页面发现、销售和状态观察 |
| `sporttery_pool_observations` | 官方玩法、选项和 SP |
| `snapshot_batches` | 切点窗口、核心市场完整性和严格资格 |
| `market_snapshots` | 市场级响应、解析、来源和时间证据 |
| `bookmaker_market_rows` | 公司级初盘、即时盘、盘口线和变盘时间 |
| `market_normalizations` | 市场快照的解析版本、来源哈希、有效公司数和接受状态 |
| `snapshot_eligibility_assessments` | 采集资格、字段完整性、模型严格资格和不合格原因 |
| `handicap_index_rows` | 独立让球指数行，不参与 V1 模型资格 |
| `sporttery_scope_evidence` | 中国体彩官方赛果页 90 分钟 scope 证据 |
| `sporttery_inventory_batches` | 官方赛果清单批次、分页完整性和来源哈希 |
| `sporttery_fixture_links` | 500 fixture 与官方 `sportteryMatchId` 的精确映射证据 |
| `sporttery_result_observations` | 官方 90 分钟比分及 head/fixed 详情一致性证据 |
| `result_candidates` | 尚未确认 90 分钟口径的候选赛果 |
| `verified_results` | 已确认的 90 分钟赛果版本；负责人声明额外保存证据等级、声明人和说明 |
| `quality_events` | 失败、缺盘、冲突、迟到和运行事件 |
| `current_verified_results` | 唯一、未被替代且没有冲突事件的当前有效赛果 |
| `current_invalid_fixtures` | 经来源或项目负责人证据确认无效、取消或未结算的 fixture |
| `strict_fixture_results_by_cutoff` | 按切点连接严格快照和当前有效赛果的阶段 4 资格视图 |

每个类型化表通过 `record_id` 引用 `records`。原始记录完整保存在 `records.payload`，常用字段同时提取为强类型列。公司盘口额外提取主、平、客、盘口线、大小球水位的 `numeric(18,8)` 值；原始文本继续保留在 JSONB。

`latest_fixture_identities` 提供每场最后一次身份观察。`unsupported_records` 保留数据库尚未认识的新 `record_type`，未知类型不得静默丢弃。

`006_market_normalization_v2` 增加 V2 表、字段和候选视图，但不切换原 current/as-of 接口。`007_activate_market_normalization_v2` 在历史重放验收后将 `current_bookmaker_market_rows`、`market_rows_as_of()` 和严格赛果资格切换到 V2。`008_fixture_invalidation` 增加无效 fixture 视图，并从当前赛果和模型资格视图中统一排除。`009_sporttery_result_evidence` 增加中国体彩官方赛果证据表，并扩展候选赛果的官方证据引用字段。`010_sporttery_evidence_provenance` 保存不完整清单失败原因，并让官方 fixture link 引用实际用于映射的 `FixtureIdentity.record_id`。`011_project_owner_result_attestation` 增加 `evidence_level`、`attestor_id`、`attestation_note`，只让完整引用同 fixture、同比分、双页一致候选的负责人声明进入 current/strict 视图；旧 `manual` 和 `manual-import` 不具备严格资格。`current_model_eligible_snapshot_batches` 按 `model_strict_eligible`、核心市场最晚观察时间、完成时间和记录 ID 确定性选择每个 fixture/切点的当前批次。

### Research Schema

`004_research_history` 创建 `research.source_assets`、`fixtures`、`market_observations`、`feature_rows`、`quality_events` 及独立导入表。`005_research_asset_observations` 允许同一内容被不同来源观察版本引用。K1 派生特征只进入 `feature_rows`；Football-Data 可还原赔率才进入 `market_observations`。

`012_research_shadow_predictions` 将研究事实拆为 `research_kind=historical|model_artifact|shadow_event`。历史和模型产物必须 `backfill=true`；当前比赛影子发布必须 `backfill=false`；三者都必须 `research_only=true`、`strict_backtest_eligible=false`、`cutoff_eligible=false`。新增 `model_datasets`、`model_versions`、`model_activations`、`shadow_predictions`、`retrospective_evaluations` 和 `shadow_evaluations`。`shadow_predictions` 对 `(channel, fixture_id, target, prediction_cutoff)` 建唯一约束，防止同一发布机会被不同模型版本覆盖。

`013_research_competition_profiles` 为影子预测追加赛事类型、市场证据等级、注册表双哈希、as-of身份引用、公司分歧、置信和风险字段。旧预测字段允许为空并按 `legacy_unclassified` 报告。数据库只约束枚举和数值范围；A/B/C/D对应的置信上限属于版本化应用策略，不固化为数据库约束。研究表保存正式身份 `record_id` 文本引用，但不建立跨 schema 外键或联合正式资格视图。

`014_research_k1_guardrail_assessments` 新增append-only `research.k1_guardrail_assessments`。记录引用不可变影子预测，保存策略快照及双哈希、相关源码指纹、精确批次/快照/公司行、R0-R6判定和`keep/caution/downgrade/abstain`拟议动作。首版只允许`policy_status=shadow`，不修改原预测或`football` schema。新shadow批次只有在completed manifest中的路径、SHA-256、字节数、行数和分类计数全部一致时才允许导入；旧批次继续兼容原manifest。

根据D-034，未来K1 assessment的`raw_features`还保存工作流版本及双哈希、基础欧赔/护栏三市场的独立canonical输入指纹、相邻切点更新状态、发布时样本成熟度、核心输入质量和R4/R5可评估原因。这些字段不进入数据库CHECK、不参与R0-R6动作重算，也不修改影子预测；缺少该结构的既有assessment按`legacy_analysis_flow`读取。前向评估V2仍写入既有`research.shadow_evaluations`，以自动赛果证据集合哈希去重，人工声明只纳入敏感性统计。

## 4. 导入契约

- manifest 以相对路径为主键并保存 SHA-256；同路径内容变化立即报 append-only 违规。
- JSONL 按文件字节偏移增量读取，只处理本轮开始时已经完整落盘的行。
- 每个文件是独立事务；失败时该文件记录和检查点同时回滚，先前已完成文件不回滚。
- `record_id` 是全局幂等键；清空检查点后重放不会产生重复记录。
- 检查点恢复时核对文件大小、换行边界和最后 `record_id`；截断或尾部变化立即停止。
- PostgreSQL advisory lock 保证同一时刻只有一个导入器推进检查点。
- 只接受 `schema_version=1`；新版本必须先新增迁移和解析规则。
- 修复目录必须同时具有合法 `manifest.json` 和 `complete.json`，且逐文件大小和 SHA-256 一致；staging 和 quarantine 不导入。

## 5. As-Of 防泄漏

稳定查询入口：

```sql
football.market_rows_as_of(fixture_id, prediction_cutoff)
football.market_snapshots_as_of(fixture_id, prediction_cutoff)
```

两个函数都强制：

```sql
observed_at <= prediction_cutoff
AND (corrected_at IS NULL OR corrected_at <= prediction_cutoff)
```

训练、回测和未来 API 不得绕过函数直接选择“最新记录”。`football-cups-db as-of` 同时返回越界审计；`observed_after_cutoff` 和 `corrected_after_cutoff` 必须为 0。

V2 `market_rows_as_of()` 只读取最新已接受的整数标准化版本，并额外返回 `parser_version`、`normalization_version` 和 `normalization_record_id`。`normalized_at` 只是派生解析时间，不参与防泄漏；仍以来源真实 `observed_at` 为过滤边界。

## 6. 已验证证据

截至2026-07-21，在本地 PostgreSQL 17.10 完成：

- 十四个版本化迁移真实执行；`003_automated_results` 增加自动赛果证据，`004/005` 建立隔离研究层，`006/007` 添加并启用盘口标准化 V2，`008` 启用无效 fixture 逻辑排除，`009` 增加中国体彩官方 90 分钟赛果证据，`010` 增加清单失败分类和映射身份溯源，`011` 增加负责人声明字段和候选引用资格，`012` 增加隔离影子预测表和研究 flag V2 约束，`013` 增加赛事分层和置信审计字段，`014` 增加K1规则护栏assessment。
- 首次导入 80 个 manifest 和 16,764 条当时已有记录；最终独立空库以两个迁移重建 81 个 manifest 和 17,252 条记录。
- 无新增数据时重复导入为 0；采集器新增记录能够按检查点增量进入。
- 独立空库全量重建与主库逐表数量一致。
- 清空检查点重放只产生 existing 记录，不产生重复行。
- 损坏 JSONL 的同文件记录和检查点全部回滚。
- 两个并发导入器只能有一个获得 advisory lock。
- fixture `1358418` 的 as-of 审计返回 56 行，两个截止后越界计数均为 0。
- 数据库完全停止后，Windows 导入任务能够自动启动服务、补数并以 0 退出。

自动赛果修复上线前文件事实中没有 `ResultCandidate` 或 `VerifiedResult`。2026-07-17首次自动补偿导入10条候选和4条普通联赛已验证赛果，6场可能加时比赛自动隔离。2026-07-20增加赛事ID口径登记和 `shuju`/`ouzhi` 分析双端点fallback，并通过D-028追加8场负责人候选口径声明；fixture `1358414` 通过迁移008逻辑排除。截至2026-07-21，`ResultCandidate=58`、`VerifiedResult=46`、`current_verified_results=46`、`current_invalid_fixtures=1`、`unsupported_records=0`；自动验证38场，负责人声明8场。

迁移013应用后，研究库保留10条迁移前影子预测并按 `legacy_unclassified` 评估，不回写历史赛事等级。当前分层评估涉及4个fixture，样本不足以形成校准结论；新策略只作用于未来自然发布机会。

迁移014于2026-07-21应用。K1历史复现严格校验330场、2025年228场、2026年102场及三类固定哈希；热门校准残差为`+2.49pp`，90%按周block-bootstrap区间为`[-1.49pp, +6.46pp]`，不能支持直接启用护栏动作。正式K1 assessment必须等待策略生效后的自然cutoff，不能历史补发。

2026-07-20 体彩官方低频补偿首次正式调度保存3条 scope、3条不完整清单和7条失败映射证据；官方 JSON 清单被 EdgeOne 567 阻断，因此没有生成 `SportteryResultObservation`、官方候选或新增 `VerifiedResult`。迁移010应用后第二次导入新增0，`unsupported_records=0`。失败证据可重放，但不得计为官方赛果闭环通过。

2026-07-17 盘口 V2 离线修复导入 11,616 行公司盘口、426 条市场标准化、130 条唯一批次评估和 940 条让球指数。第二次导入新增0，`unsupported_records=0`；当前 V2 视图11,616行无重复，93个 fixture/切点批次获得模型资格，观察时间越界和拒绝标准化泄漏均为0。原始层、manifest 和非修复 V1 JSONL 与维护前 G 盘副本逐文件 SHA-256 完全一致。
