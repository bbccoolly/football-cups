# 标准化数据库设计

> 版本：V1.2
> 更新日期：2026-07-17
> PostgreSQL：17

## 1. 定位与边界

PostgreSQL 是从 `data/500/manifests/` 和 `data/500/normalized/` 重建的分析层，不是原始事实来源。删除数据库后必须能够从文件层恢复；数据库写入不得反向修改 blob、manifest、JSONL、已验证赛果或采集调度 SQLite。

阶段 3 只解决结构化查询、重放和 as-of 防泄漏。它不解除模型或 Web 门禁，也不代表 500 的 24 小时、7 天或 30 天采集验收已经通过。

公开历史数据位于独立 `research` schema，使用独立 importer、记录类型和检查点。研究 importer 不调用正式 `insert_record()`，不得写入 `football.records`，不得与正式资格视图建立联合视图。

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
| `result_candidates` | 尚未确认 90 分钟口径的候选赛果 |
| `verified_results` | 已确认的 90 分钟赛果版本 |
| `quality_events` | 失败、缺盘、冲突、迟到和运行事件 |
| `current_verified_results` | 唯一、未被替代且没有冲突事件的当前有效赛果 |
| `strict_fixture_results_by_cutoff` | 按切点连接严格快照和当前有效赛果的阶段 4 资格视图 |

每个类型化表通过 `record_id` 引用 `records`。原始记录完整保存在 `records.payload`，常用字段同时提取为强类型列。公司盘口额外提取主、平、客、盘口线、大小球水位的 `numeric(18,8)` 值；原始文本继续保留在 JSONB。

`latest_fixture_identities` 提供每场最后一次身份观察。`unsupported_records` 保留数据库尚未认识的新 `record_type`，未知类型不得静默丢弃。

### Research Schema

`004_research_history` 创建 `research.source_assets`、`fixtures`、`market_observations`、`feature_rows`、`quality_events` 及独立导入表。`005_research_asset_observations` 允许同一内容被不同来源观察版本引用。K1 派生特征只进入 `feature_rows`；Football-Data 可还原赔率才进入 `market_observations`。所有研究 payload 强制四个资格标记，并由数据库 CHECK 约束验证。

## 4. 导入契约

- manifest 以相对路径为主键并保存 SHA-256；同路径内容变化立即报 append-only 违规。
- JSONL 按文件字节偏移增量读取，只处理本轮开始时已经完整落盘的行。
- 每个文件是独立事务；失败时该文件记录和检查点同时回滚，先前已完成文件不回滚。
- `record_id` 是全局幂等键；清空检查点后重放不会产生重复记录。
- 检查点恢复时核对文件大小、换行边界和最后 `record_id`；截断或尾部变化立即停止。
- PostgreSQL advisory lock 保证同一时刻只有一个导入器推进检查点。
- 只接受 `schema_version=1`；新版本必须先新增迁移和解析规则。

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

## 6. 已验证证据

2026-07-16 至 2026-07-17 在本地 PostgreSQL 17.10 完成：

- 三个版本化迁移真实执行；`003_automated_results` 增加自动赛果证据、版本字段和资格视图。
- 首次导入 80 个 manifest 和 16,764 条当时已有记录；最终独立空库以两个迁移重建 81 个 manifest 和 17,252 条记录。
- 无新增数据时重复导入为 0；采集器新增记录能够按检查点增量进入。
- 独立空库全量重建与主库逐表数量一致。
- 清空检查点重放只产生 existing 记录，不产生重复行。
- 损坏 JSONL 的同文件记录和检查点全部回滚。
- 两个并发导入器只能有一个获得 advisory lock。
- fixture `1358418` 的 as-of 审计返回 56 行，两个截止后越界计数均为 0。
- 数据库完全停止后，Windows 导入任务能够自动启动服务、补数并以 0 退出。

自动赛果修复上线前文件事实中没有 `ResultCandidate` 或 `VerifiedResult`。2026-07-17 首次自动补偿导入 10 条候选和 4 条普通联赛已验证赛果，6 场可能加时比赛自动隔离；当前有效赛果为 4，各主要已有切点的严格 fixture 赛果计数均为 4。数据库状态持续报告当前有效赛果数和各切点严格 fixture 数量。
