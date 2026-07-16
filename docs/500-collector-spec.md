# 500 足球竞彩采集器规范

> 版本：V1.0  
> 更新日期：2026-07-15

## 1. 来源与采集范围

竞彩发现取以下页面 fixture ID 并集：默认页以及 `playid=269`、`312`、`271`、`270`、`272` 的 `g=2` 页面。任何包含数字型 `data-fixtureid` 的比赛都进入原始层，不按赛事、显示、销售或玩法状态过滤。

每场市场包括 `ouzhi`、`yazhi`、`daxiao`、`rangqiu`。赛果候选来自 500 完场页和 `shuju-<fixture_id>` 分析页。采集器不抓取新闻、伤停、阵容、天气或推荐内容。

## 2. 稳定记录

所有 JSON/JSONL 使用 `schema_version=1`、UTF-8 和 RFC 3339 UTC 时间。

- `RawBlob`：请求 URL、方法、状态、响应头、开始和完成时间、来源编码、SHA-256、相对路径。
- `FixtureIdentity`：fixture、赛事/赛季、球队及来源 ID、首次发现时间。
- `DiscoveryObservation`：页面、开球、销售截止、状态、让球和原始行哈希。
- `SportteryPoolObservation`：玩法、选项、SP、让球和观察时间。
- `SnapshotBatch`：目标切点、有效窗口、批次时间和资格。
- `MarketSnapshot`：市场、原始响应、公司数、解析与来源可用状态。
- `BookmakerMarketRow`：公司、初盘、即时盘、水位、变盘时间和原始单元格。
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

三个核心市场都在窗口内成功取得且身份、时钟无严重问题时，批次才可标记 `strict_eligible=true`。让球指数缺失不影响三核心市场资格。

## 4. 存储和调度

原始 blob 路径为 `data/500/raw/blobs/<前两位>/<sha256>.<ext>`。每次发现和市场任务生成唯一 manifest；标准化记录按 UTC 日期追加 JSONL。所有写入先进入同目录临时文件，再原子重命名。

SQLite 表只保存比赛当前身份、任务、运行、事件、配置游标和已写记录 ID。状态库不是原始事实来源，可通过 discovery manifests 重建当前比赛和未来任务。

初始执行参数：单请求并发、请求最小间隔 1.5 秒、重试 2/5/15 秒、每轮时间预算 100 秒。任务按窗口结束时间排序；同场优先欧赔、亚盘、大小球，再处理让球指数。

## 5. 错误分类

- `http_failure`：请求异常或非成功 HTTP。
- `blocked_response`：验证码、登录或拦截页面。
- `invalid_excel`：文件过小、空工作簿或无法读取。
- `parser_failure`：有效响应无法转为标准化结构。
- `source_market_unavailable`：页面正常但没有该市场。
- `identity_conflict`：同 fixture 的身份字段冲突。
- `inventory_mismatch`：原始正则 ID 与 DOM ID 不一致。
- `clock_drift`：实时竞彩发现页的 HTTP Date 与本机 UTC 相差超过 30 秒。
- `source_http_date_stale`：缓存盘口页的 HTTP Date 滞后；记录警告但使用最近 60 分钟内的发现页时钟校准判断资格。
- `late_for_cutoff` / `missed_before_discovery`：切点迟到或发现过晚。
- `result_scope_ambiguous`：不能证明是 90 分钟赛果。

来源缺盘不得计入程序失败率。

## 6. 赛果规则

完场页明确为“完”且全场比分与分析页一致时生成候选赛果。只有赛事登记为 `regular_time_only` 时才能自动生成已验证赛果；`may_have_extra_time` 或 `unknown` 必须人工或由未来独立适配器确认。

人工 CSV 字段固定为：

```text
fixture_id,home_goals,away_goals,source_url,confirmed_at,notes
```

已验证赛果冲突时拒绝覆盖，写入质量事件并阻止该比赛进入严格训练。
