# 500 足球竞彩采集器规范

> 版本：V1.7
> 更新日期：2026-07-20

## 1. 来源与采集范围

竞彩发现取以下页面 fixture ID 并集：默认页以及 `playid=269`、`312`、`271`、`270`、`272` 的 `g=2` 页面。任何包含数字型 `data-fixtureid` 的比赛都进入原始层，不按赛事、显示、销售或玩法状态过滤。

每场市场包括 `ouzhi`、`yazhi`、`daxiao`、`rangqiu`。赛果候选优先来自按开球北京时间生成的 `https://live.500.com/?e=YYYYMMDD` 日期直播页；HTML 清单切换后使用页面自身加载的 `jczq/<UTC-date>Full.txt` 同源数据流回退。`shuju-<fixture_id>` 分析页通常只提供一致性证据；当直播源遗漏 fixture 时，`shuju` 与 `ouzhi` 两个分析端点比分一致可作为补偿候选证据。中国体彩官方竞彩足球赛果页和 `webapi.sporttery.cn` 详情接口只作为 90 分钟赛果证据源，不替代 500 的比赛发现和盘口采集。采集器不抓取新闻、伤停、阵容、天气或推荐内容。

欧赔以现有 Excel 响应为主数据。亚盘、大小球和让球指数直接解析 HTML 的 `xls` 表格行，不为解析回退额外请求导出接口。HTML 解码依次采用 meta 声明、HTTP `Content-Type`、GB18030、UTF-8，来源推断只作最后候选；500 中文页不得优先选择 Latin-1。解码结果必须通过替换字符、已知乱码模式和预期表头检查。

## 2. 稳定记录

所有 JSON/JSONL 使用 `schema_version=1`、UTF-8 和 RFC 3339 UTC 时间。

- `RawBlob`：请求 URL、方法、状态、响应头、开始和完成时间、来源编码、SHA-256、相对路径。
- `FixtureIdentity`：fixture、赛事/赛季、球队及来源 ID、首次发现时间。
- `DiscoveryObservation`：页面、开球、销售截止、状态、让球和原始行哈希。
- `SportteryPoolObservation`：玩法、选项、SP、让球和观察时间。
- `SnapshotBatch`：目标切点、有效窗口、批次时间和资格。
- `MarketSnapshot`：市场、原始响应、公司数、解析与来源可用状态。
- `BookmakerMarketRow`：公司、初盘、即时盘、水位、变盘时间和原始单元格。
- `MarketNormalization`：每个市场快照的解析版本、来源哈希、有效公司数、盘口转换失败和接受/拒绝状态。
- `SnapshotEligibilityAssessment`：采集资格、字段完整性、模型严格资格、逐市场统计和不合格原因。
- `HandicapIndexRow`：让球指数三项指数、概率、返还率和 Kelly；不参与 V1 模型资格。
- `SportteryScopeEvidence` / `SportteryInventoryBatch` / `SportteryFixtureLink` / `SportteryResultObservation`：中国体彩官方赛果 scope、清单批次、fixture 精确映射和 90 分钟比分观察。
- `ResultCandidate` / `VerifiedResult`：候选比分与明确 90 分钟比分。
- `QualityEvent`：所有失败、缺失、冲突、迟到、结构和时间问题。

每条记录都有稳定 `record_id`。SQLite 用于减少重复写入；状态库重建后出现相同 `record_id` 时，下游仍必须幂等去重。

## 3. 时间与资格

`observed_at` 是响应完整接收时间，`ingested_at` 是 blob 和 manifest 安全落盘时间。公司行的来源变盘时间单独保存为 `source_event_time`。无法确认时区或年份时保留原文并产生质量事件，不静默猜测。

切点与窗口：

| 切点 | 有效窗口 |
| --- | --- |
| T-48h、T-24h | 截止前 2 小时至截止 |
| T-12h、T-6h | 截止前 30 分钟至截止 |
| T-3h | 截止前 15 分钟至截止 |
| T-60m | 截止前 10 分钟至截止 |
| T-30m | 截止前 5 分钟至截止 |
| T-10m | 截止前 3 分钟至截止 |

三个核心市场都在窗口内成功取得且身份、时钟无严重问题时，批次才可标记 `strict_eligible=true`。该字段只表示采集层资格。模型严格资格还要求三个核心市场均有 `status=accepted` 的 V2 标准化，且每个市场至少 3 家不同的字段完整 `bookmaker`。公司汇总行和竞彩官方行不计入门槛；没有 V2 评估时默认模型不合格。让球指数缺失或解析失败不影响三个核心市场资格。

亚盘数值以主队视角保存：主让为负、受让为正，斜线盘取两端平均值，升降后缀单独保存。大小球分盘取两端平均值且两端必须相差 0.5，合法范围为 0 至 20。无法转换时保留原文、数值为空并产生 `market_line_unparsed`，不得猜测。

## 4. 存储和调度

原始 blob 路径为 `data/500/raw/blobs/<前两位>/<sha256>.<ext>`。每次发现和市场任务生成唯一 manifest；标准化记录按 UTC 日期追加 JSONL。所有写入先进入同目录临时文件，再原子重命名。

SQLite 表只保存比赛当前身份、任务、运行、事件、配置游标和已写记录 ID。状态库不是原始事实来源，可通过 discovery manifests 重建当前比赛和未来任务。

历史盘口修复写入 `normalized/repairs/<run-id>/`，先在 `state/reparse-staging/` 生成并校验全部 JSONL、manifest 和完成标记，再原子发布。`event_origin=reprocess` 不进入实时日报和窗口验收；稳定 ID 不包含 `normalized_at`。重放不得访问网络、修改旧 blob/manifest/V1 JSONL、改变来源时间或补造迟到切点。

日报和精确窗口报告按日期、赛事、市场和切点拆分 V2 证据，并至少输出 `market_data_complete_rate`、`valid_bookmaker_rows_by_market`、`market_line_parse_success_rate`、`mojibake_detected_count`、`source_event_time_coverage`、`model_eligible_rate_by_cutoff`、`collection_eligible_but_data_incomplete` 和 `ineligibility_reasons`。默认只统计 `event_origin=live`。

备份使用与采集器相同的单实例锁。持锁期间固定文件清单、快照当前 UTC 日期下仍追加的 `normalized/*.jsonl` 并使用 SQLite backup API；锁释放后复制不可变文件或生成内容寻址对象。锁等待默认 300 秒，超时为可重试失败。只有源文件稳定、SQLite `quick_check=ok` 且全部对象落盘后才能写完成 manifest。`run-once` 因备份锁跳过时保存独立 `RunnerSkip` manifest，并进入日报和窗口报告。

初始执行参数：单请求并发、请求最小间隔 1.5 秒、重试 2/5/15 秒、每轮时间预算 100 秒。任务按窗口结束时间排序；同场优先欧赔、亚盘、大小球，再处理让球指数。

磁盘默认在剩余空间低于 20% 或 50 GB 时预警，低于 10% 或 20 GB 时停止市场下载；绝对值可按验证环境配置。设置 `FOOTBALL_CUPS_REQUIRED_MOUNT` 后，采集器必须确认该路径是实际挂载点，挂载缺失时拒绝创建数据目录或继续运行。

健康检查同时审计 SQLite、心跳、完整发现、时钟校验、磁盘、积压任务、必需挂载点和备份新鲜度。初始化证据缺失为 `warning`；心跳超过 10 分钟、完整发现/时钟校验超过 45 分钟、每日备份超过 48 小时或每周内容寻址备份超过 15 天为 `failed`。

## 5. 错误分类

- `http_failure`：请求异常或非成功 HTTP。
- `blocked_response`：验证码、登录或拦截页面。
- `invalid_excel`：文件过小、空工作簿或无法读取。
- `parser_failure`：有效响应无法转为标准化结构。
- `source_market_unavailable`：页面正常但没有该市场。
- `identity_conflict`：同 fixture 的身份字段冲突。
- `inventory_mismatch`：原始正则 ID 与 DOM ID 不一致。
- `clock_drift`：实时竞彩发现页的 HTTP Date 与本机 UTC 相差超过 30 秒。
- `source_http_date_stale`：缓存盘口页的 HTTP Date 滞后；记录警告但使用过去 60 分钟内最新的发现页时钟校准判断资格。
- `late_for_cutoff` / `missed_before_discovery`：切点迟到或发现过晚。
- `result_scope_ambiguous`：不能证明是 90 分钟赛果。
- `result_conflict`：历史候选、日期直播页或分析页比分不一致。
- `result_unresolved`：自动补偿持续 7 天后仍无确定性候选或一致性证据。

来源缺盘不得计入程序失败率。

## 6. 赛果规则

日期直播页必须精确匹配唯一 `tr[fid=<fixture>]` 或 `tr[id=a<fixture>]`、`status=4`、唯一 `div.pk` 以及唯一 ASCII 整数 `clt1`/`clt3`。同源 Full 数据流必须精确匹配唯一 fixture 数组、状态 4，并只读取主客状态字段的首个非负整数。满足任一确定性来源即可生成候选，不从日期或任意比分形文本猜测。HTTP 567、EdgeOne 拦截页、非 200、重复行和畸形比分必须拒绝。

分析页不可用不删除候选。只有分析页 fixture 和比分一致、身份无冲突且赛事登记为 `regular_time_only` 时自动生成已验证赛果。若直播页和 Full 数据流均遗漏 fixture，必须同时满足 `shuju` 与 `ouzhi` 两个分析端点比分一致，才能生成 `analysis_pair_fallback` 候选；该候选仍只在 `regular_time_only` 赛事中自动验证。`may_have_extra_time` 和 `unknown` 默认自动隔离，不创建人工赛果待办；项目负责人可按 D-028 主动声明现有唯一候选的90分钟口径，但不得输入或修改比分。

结果任务在 `T+3h`、`T+6h`、`T+24h` 执行；仍缺失或分析页不可用时从 `T+2d` 到 `T+7d` 每日补偿。超过 7 天产生 `result_unresolved` 并停止重试。历史补偿命令为：

```text
football-cups-collector reconcile-results --workspace . --since <RFC3339> --until <RFC3339>
football-cups-collector confirm-candidate-results --workspace . --fixture-id <id> --confirm-90-minutes --note <text>
football-cups-collector invalidate-fixture --workspace . --fixture-id <id> --reason <reason> --source-url <url> --note <text>
```

已验证赛果冲突时拒绝覆盖，写入质量事件并阻止该比赛进入严格训练。修正只能形成带 `supersedes_record_id` 和 `correction_reason` 的新版本。根据 D-028，`confirm-candidate-results` 只允许项目负责人确认现有唯一且 `analysis_consistency=passed` 的候选属于90分钟结果；命令无比分参数，最多100场，整批预检失败时零写入。生成记录固定为 `project-owner-manual-declaration`、`self_attestation` 并引用候选。兼容命令 `verify-results` 不进入正式运行或验收流程。

`invalidate-fixture` 仅用于有人工来源证据的无效、取消或未结算场次。命令追加 `fixture_invalidated/excluded` 质量事件并停止该 fixture 的待执行任务；重复执行返回 `unchanged`。它不删除原始响应、manifest、JSONL 或盘口记录，也不能写入比分。

中国体彩官方补偿命令为：

```text
football-cups-collector reconcile-results --workspace . --source sporttery --since <RFC3339> --until <RFC3339> --dry-run
football-cups-collector reconcile-results --workspace . --source sporttery --since <RFC3339> --until <RFC3339> --apply
football-cups-collector sporttery-smoke --workspace . --fixture-id <id> --since <RFC3339> --until <RFC3339>
football-cups-collector audit-result-evidence --workspace .
```

官方自动验证必须同时满足：官方页面 scope 文本可见、清单批次完整、`match_number` 精确一致、北京时间开球误差不超过 5 分钟、主客顺序和名称精确匹配、`getMatchHeadV1` 与 `getFixedBonusV1` 比分一致且未显示取消/无效。清单分页不完整时可以处理已取得行，但不得声明映射缺失；阻断、验证码、EdgeOne 567 或详情不一致只产生失败或隔离证据。500 未确认口径候选与官方 90 分钟比分不同产生 `result_scope_difference`，但不阻止接受官方结果；明确 90 分钟来源之间冲突才产生 `result_conflict`。

`run-once` 默认每 24 小时最多执行一次体彩官方补偿，只处理开球已超过 24 小时、位于最近 8 天内且尚无自动验证证据的 fixture；已有负责人声明的 fixture 仍继续核验。失败不会改变 500 主采集退出码，但必须写入独立质量事件。映射优先引用文件事实层中最近一条无乱码 `FixtureIdentity` 并保存其 `record_id`；SQLite 中被旧编码污染的身份不得用于精确映射。`audit-result-evidence` 只有在官方候选、观察、完整清单、accepted link、scope 和 `VerifiedResult` 引用链完整时返回 `ok`；只有失败尝试时返回 `warning`。
