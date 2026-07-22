# 韩职 K1 盘口规则护栏设计

> 版本：V4.3
> 日期：2026-07-22
> 状态：shadow-v2 as-of输入、历史证据展示和无存储回放已实现；等待首个自然v2 assessment

## 1. 结论与目标

本功能不是判断“庄家是否收割”，也不是寻找反向投注口诀。它是 K1 专用的 research-only 规则护栏：在现有 `devig-consensus-v1` 胜平负概率之外，检查欧赔、亚洲让球、大小球和让球指数之间是否存在可重复的方向冲突，并给出：

```text
keep       未发现达到阈值的结构风险
caution    存在辅助风险，只提示，不降置信
downgrade  存在明确方向冲突，建议限制置信
abstain    数据不可信，或多个独立市场同时反对当前方向
```

护栏固定遵守：

- 不修改主胜、平局、客胜概率，不反转预测方向。
- 不输出比分、投注选择、金额、组合、收益或 ROI。
- 只适用于截止时间前身份明确为 `competition_id=16` 的韩职 K1，不按名称猜测，不适用于 K2。
- 当前 `status=shadow` 时只写 `proposed_action` 和 `proposed_confidence_cap`，不修改已有预测的 `confidence_label`、`risk_flags` 或发布状态。
- 前向验证通过只产生 `review_eligible=true`；必须由项目负责人新增决策并启用新的策略版本，才允许实际 `downgrade` 或 `abstain`。

330 场历史数据只用于提出和冻结候选规则。它不能证明因果关系，也不能直接授权生产动作。

根据D-033，人工分析可额外展示截止前可用的历史closing上下文。历史数据按冻结分箱选择不少于30场的最具体cohort，报告整体、赛季、规则代理和5场样例；它固定为证据解释层，不修改当前概率或护栏动作。历史回放还必须满足`label_available_at <= prediction_cutoff`。

根据D-034，基础欧赔概率输入与护栏输入必须分别哈希：基础指纹只覆盖实际参与去水共识的即时欧赔，护栏指纹覆盖opening稳定性排除后的欧赔、亚盘和大小球行。相邻切点只比较冻结输入并标记`unchanged/partial_update/full_update`；该状态只说明输入变化，不调整R1-R6或基础概率。R4/R5的不可评估原因、发布时自动样本成熟度和前向错误捕获指标均属于research-only审计字段。

### 1.1 As-of close

`opening`来自最终选中 V2 公司行的来源开盘字段；`close`来自预测 cutoff 前且在合法发布时间内已经完成的最后一个模型合格批次的 `current_*`。自然预测以实际 `published_at` 为可用时间，未发布历史模拟以 `min(cutoff+10m,kickoff-1m)` 为可用时间。不得跨 target 或批次拼接。多段响应只用于 opening 稳定性和同 target R5。

## 2. 历史证据与限制

固定研究资产：

```text
asset_id=k1-core3-features-2025-2026
cohort=derived_closing_features
csv_sha256=E26210D45DF9D691BB81B68C078D494705DDB0AADAD73EBC1FAAE4DE36B7A931
metadata_sha256=6E7452951C098E30AFD47EA2CCA729C94B9FE4609011E463FF0E5D3ADD20D710
input_hash=6285cc00625cb1675881c4c8ec41e8d8938ca5402371d95902809bc3b3344455
```

| 赛季 | 时间范围（Asia/Shanghai） | 场次 | 性质 |
| --- | --- | ---: | --- |
| 2025 | 2025-02-15 12:00 至 2025-11-30 15:30 | 228 | 回顾性 closing 派生特征及赛果 |
| 2026 | 2026-02-28 13:00 至 2026-07-12 18:30 | 102 | 回顾性 closing 派生特征及赛果 |
| 合计 | 2025-02-15 至 2026-07-12 | 330 | 330 个唯一 fixture |

2023、2024 各有 228 场赛果，但没有完整多市场盘口，不能加入盘口规则样本。330 行只有 opening/closing 两点，不能伪装成 `T-24h`、`T-6h`、`T-60m` 或 `T-10m` 的真实轨迹。

现有结果不足以支持旧版 0-7 分制：

| 分组 | 场次 | 平均热门概率 | 实际热门胜率 | `actual - expected` |
| --- | ---: | ---: | ---: | ---: |
| 全部 | 330 | 44.78% | 46.97% | +2.19pp |
| `0.10 <= prob_gap <= 0.20` | 101 | 42.98% | 42.57% | -0.40pp |
| 亚盘 opening/closing 线相同 | 176 | 44.69% | 43.75% | -0.94pp |
| 主队热门且主赔上升 | 108 | 44.32% | 45.37% | +1.05pp |

2025 chronological OOS 108 场和 2026 retrospective walk-forward 102 场均为 `FAIL`。因此删除固定基线分、总分、`critical/high/medium/low` 旧风险等级，以及“低胜率等于异常”的解释。

## 3. 输入冻结与符号约定

### 3.1 截止时间和批次

每条 assessment 必须绑定现有不可变影子预测，并冻结：

```text
(channel, fixture_id, target, prediction_cutoff)
identity_record_id
selected_batch_record_id
snapshot_record_ids_by_market
source_row_record_ids
source_page_sha256_by_market
```

输入必须同时满足：

- `FixtureIdentity.observed_at <= prediction_cutoff`。
- `competition_id == 16`，且赛事 ID 与显式名称映射不冲突。
- 所有市场来自同一 target 选定的同一个 `SnapshotBatch`，禁止拼接其他切点或批次。
- 只读取 `event_origin=live`、已接受 V2 且 `observed_at <= prediction_cutoff` 的行。
- 欧赔、亚盘、大小球各至少 3 家不同且字段完整的 `bookmaker`；summary 和 official 不计数。
- 输入引用和 blob/hash 完整；任一硬条件失败直接 `abstain`，原因标记为 `data_integrity_failure`。

### 3.2 视角统一

所有计算先保存主队视角原值，再转换为“当前胜平负热门方”视角。热门只允许 `home` 或 `away`，平局概率最高时标记 `favorite_side=draw`，不执行依赖让球热门方向的 R1、R2、R4。

欧赔：每家公司分别去水，再计算开盘和即时的三项概率。对结果 `j`：

```text
p_j_i = (1 / odds_j_i) / sum_k(1 / odds_k_i)
delta_p_j_i = p_j_current_i - p_j_open_i
```

亚盘线固定为主队视角：主让为负、主受让为正。为使“热门支持增强”为正数：

```text
favorite_line_i = -home_line_i  if favorite_side == home
favorite_line_i =  home_line_i  if favorite_side == away
delta_favorite_line_i = favorite_line_current_i - favorite_line_open_i
```

例如主队从 `-0.25` 到 `-0.50`，热门支持变化为 `+0.25`；客队热门时主队从 `+0.25` 到 `+0.50`，同样为 `+0.25`。退盘则为负。

大小球以进球线数值表示：

```text
delta_total_line_i = total_line_current_i - total_line_open_i
```

正数表示总球预期升高，负数表示降低。水位变化必须与盘口线变化分开保存，禁止把升水直接称为升盘。

### 3.3 两级差值

禁止直接用聚合赔率相减掩盖公司组成变化。每个市场先在同一家公司的 opening/current 配对行上计算差值，再跨公司稳健聚合：

```text
median_delta = median(delta_i)
support_ratio = count(delta_i >= threshold) / paired_bookmaker_count
oppose_ratio = count(delta_i <= -threshold) / paired_bookmaker_count
delta_mad = median(abs(delta_i - median_delta))
```

公司配对仅按标准化后仍完全相同的 bookmaker 名称，不做模糊合并。无法配对的公司只参与当前截面共识，不参与变化率。规则需要变化信息时至少 3 家配对公司；不足时该规则为 `not_evaluable`，不能按“没有变化”处理。

为防止二进制浮点边界漂移，盘口线先转换为四分之一球整数单位：`-0.25 -> -1`、`-0.50 -> -2`；赔率和概率使用 Decimal 或固定量化精度计算。比较使用原始未格式化值，报告才四舍五入。

### 3.4 冻结特征

首版只允许以下预注册特征：

```text
p_home/p_draw/p_away_open_consensus
p_home/p_draw/p_away_current_consensus
delta_p_favorite_median
delta_p_draw_median
delta_p_opponent_median
prob_gap_open
prob_gap_current
prob_gap_delta
favorite_odds_delta_median
asian_line_open_median
asian_line_current_median
delta_favorite_line_median
total_line_open_median
total_line_current_median
delta_total_line_median
paired_bookmaker_count_by_market
support_ratio_by_signal
bookmaker_dispersion_by_market
handicap_index_cover_conflict
live_observation_count
live_observation_span_seconds
```

不得加入排名、球队状态、伤病、阵容、天气、主客队名称编码或赛后字段。

## 4. 规则定义

所有阈值均为 `k1-guardrail-shadow-v1` 的待前向验证阈值，不是已经证明有效的规律。

### R0 数据完整性

任一条件成立直接提出 `abstain`：

- 三个核心市场任一不完整或少于 3 家有效 bookmaker。
- 身份、赛事映射、cutoff、target、批次、快照引用或哈希冲突。
- 使用截止时间后的盘口。
- R1/R2 所需的欧赔或亚盘 opening/current 配对公司少于 3 家；R3-R6 的可选来源不足只将对应规则标记为 `not_evaluable`。
- 基础预测概率不合法或和不在容差范围内。

### R1 浅盘热门降温

同时满足：

```text
favorite_side in {home, away}
abs(asian_line_current_median) in {0.25, 0.50}
delta_p_favorite_median <= -0.015
favorite_odds_delta_median >= 0.03
favorite_cooling_support_ratio >= 0.60
draw_or_opponent_strengthening_ratio >= 0.60
```

提出 `downgrade`。概率下降和赔率上升来自同一批已配对公司，不能用不同公司集合拼出信号。

### R2 欧亚方向冲突

同时满足：

```text
favorite_probability_current >= 0.45
prob_gap_current >= 0.07
delta_favorite_line_median <= -0.25
asian_retreat_support_ratio >= 0.60
```

或者当前欧赔方向明确，但亚洲盘仍为 `0` 且 `delta_p_favorite_median > 0`、至少 60% 公司没有给出同向让步时，标记 `euro_asian_support_mismatch`。首种退盘冲突提出 `downgrade`；仅“欧强亚浅”提出 `caution`，不得只凭浅盘降置信。

### R3 低总球平局尾部

同时满足：

```text
total_line_current_median <= 2.25
p_draw_current_consensus >= 0.28
abs(asian_line_current_median) <= 0.50
```

单独命中只提出 `caution`。与 R1 或 R2 同时命中时，作为独立节奏证据将组合动作提高一级。低总球不能单独反转胜负方向。

### R4 让球指数覆盖冲突

当胜平负支持热门，但让球指数显示热门在当前整数或半整数让步下“不覆盖”的去水概率明显高于“覆盖”：

```text
non_cover_probability - cover_probability >= 0.10
handicap_index_valid_bookmakers >= 3
handicap_index_conflict_support_ratio >= 0.60
```

产生 `cover_risk`。它只描述赢球方向与穿盘能力不同：单独命中为 `caution`，与 R1/R2 组合时才提高动作。让球指数来源不足或定义无法统一时标记 `not_evaluable`，不阻止其他规则。

### R5 实时盘口稳定

历史 opening/closing 两点不能证明“稳定”。实时稳定必须满足：

```text
live_observation_count >= 3
live_observation_span_seconds >= 1800
max_favorite_line - min_favorite_line <= 0.25
max_favorite_probability - min_favorite_probability <= 0.015
```

单独只产生 `caution`，且标签必须为 `live_market_stability`。若三次观察实际来自相同响应哈希，只计一次。

### R6 公司分歧

沿用 D-030 的当前截面 MAD 口径。任一概率分量 MAD 大于 `0.035` 时标记 `high_bookmaker_dispersion`，单独只产生 `caution`。它与 R1/R2 同时出现时表示方向证据不稳定，允许组合升级。

## 5. 动作矩阵

规则不计总分，按证据层级确定性组合：

| 条件 | `proposed_action` | shadow 行为 | 未来 active 行为上限 |
| --- | --- | --- | --- |
| R0 | `abstain` | 记录原因，不改预测 | 不发布该机会 |
| 无规则 | `keep` | 记录 | 保持原置信 |
| 仅 R3/R4/R5/R6 | `caution` | 记录 | 只加风险标签 |
| R1 或明确退盘型 R2 | `downgrade` | 记录 | 置信最多降一级且不高于 `low` |
| R1/R2 + 任一独立 R3/R4 | `abstain` | 记录 | 不发布该机会 |
| R1/R2 + R6 | `abstain` | 记录 | 不发布该机会 |

R3 与 R5 都属于节奏/轨迹证据，不算两个独立市场；R4 只有在来源和定义完整时才算独立证据。多个辅助标签不能在没有 R1/R2 时累加成 `downgrade`。

优先级固定为 `abstain > downgrade > caution > keep`。最终 active 置信上限还要与 D-030 的赛事上限、provisional 上限和样本上限取最小值。护栏绝不提高置信。

## 6. Shadow、复审与启用

### 6.1 策略配置

已新增 `config/research-k1-guardrail.json`：

```json
{
  "schema_version": 1,
  "policy_version": "k1-guardrail-shadow-v1",
  "policy_revision": 1,
  "status": "shadow",
  "effective_at": "<真实启用UTC时间>",
  "competition_id": "16",
  "targets": ["T-24h", "T-6h", "T-60m", "T-10m"],
  "minimum_bookmakers_per_market": 3,
  "minimum_paired_bookmakers": 3,
  "thresholds": {
    "favorite_probability_drop": 0.015,
    "favorite_odds_rise": 0.03,
    "signal_support_ratio": 0.60,
    "asian_material_move": 0.25,
    "low_total_line": 2.25,
    "draw_tail_probability": 0.28,
    "high_dispersion": 0.035
  }
}
```

保存文件字节 SHA-256、canonical JSON SHA-256、历史数据哈希和代码提交。策略生效后不得原地修改；阈值或动作变化必须增加 `policy_revision`、新 `policy_version` 和新的未来 `effective_at`。

### 6.2 前向门禁

每个 `(competition_id, target, rule_id)` 独立评估，禁止混合切点：

- 至少 100 个自动验证的不同 fixture，跨度至少 90 天。
- 被评估规则至少命中 50 场。
- 按时间拆为两个不重叠批次，每批至少 25 场。
- 两批热门校准残差方向一致。
- 命中组 `actual_favorite_win_rate - expected_favorite_probability <= -0.03`。
- 相对未命中组的校准残差至少低 3 个百分点。
- 按比赛周 block-bootstrap 的 90% 区间上界小于 0。
- Log Loss、Brier、RPS 不因拟议动作筛选而出现无法解释的恶化。
- 时间、身份、快照、哈希、自动赛果和重复记录审计全部通过。

人工声明赛果只作敏感性报告，不计入启用门禁。达到门槛只生成 `review_eligible=true`，不自动修改配置。

### 6.3 启用要求

从 shadow 切换为 active 必须同时满足：

1. 前向门禁通过。
2. 项目负责人审阅按 target、规则和时间批次拆分的报告。
3. 新增治理决策，明确允许的规则、动作和生效时间。
4. 新策略版本从未来自然发布机会生效。
5. 先启用 `caution`，观察至少 30 个新结算 fixture 后，才允许启用 `downgrade/abstain`。

active 版本若连续两个 25 场窗口的校准残差不再为负、输入覆盖低于 90%，或数据结构发生变化，自动熔断回 shadow；熔断只影响未来机会，不改写历史预测。

## 7. 文件与数据库接口

新增记录 `ResearchK1GuardrailAssessment`：

```text
prediction_record_id
channel
fixture_id
competition_id
target
prediction_cutoff
assessed_at
policy_version
policy_revision
policy_status
policy_file_sha256
policy_canonical_sha256
historical_dataset_sha256
code_commit
identity_record_id
selected_batch_record_id
snapshot_record_ids
source_row_record_ids
source_hashes
raw_features
rule_evaluations
rule_flags
proposed_action
proposed_confidence_cap
reasons
audit_status=eligible|unavailable
```

唯一约束：

```text
(prediction_record_id, policy_version)
```

稳定 ID 不包含 `assessed_at`。同一策略相同输入重复运行新增 0；相同稳定 ID 出现不同输入哈希必须退出 3。

迁移 `014_research_k1_guardrail_assessments.sql` 已新增且未修改迁移 012/013：

- 新增 `research.k1_guardrail_assessments`，外键引用 `research.shadow_predictions`。
- 核心引用、版本、状态和动作使用独立列；原始特征和逐规则判定使用 JSONB。
- 当前视图按整数 `policy_revision`、`assessed_at` 和 `record_id` 确定性选择，不按版本字符串排序。
- importer 验证引用预测的 fixture、target、cutoff、赛事和身份完全一致。
- 不写 `football` schema，不与正式严格资格视图联合。
- 迁移前预测标记 `legacy_unassessed`，不得回填为前向记录。

## 8. 命令与运行流程

稳定命令：

```powershell
football-cups-research evaluate-k1-guardrail-history --workspace .

football-cups-research k1-guardrail `
  --workspace . `
  --fixture-id <id> `
  --target T-6h `
  --dry-run

football-cups-research analyze-k1 `
  --workspace . `
  --fixture-id <id> `
  --target T-6h `
  --format detailed `
  --dry-run

football-cups-research blind-test-k1-guardrail `
  --workspace . `
  --fixture-id <id> `
  --target T-6h `
  --reveal-result

football-cups-research evaluate-k1-guardrail-forward `
  --workspace . `
  --channel research-shadow-v1
```

现有 `shadow-predict` 在未来自然发布后追加 assessment：

1. 先发布不可变基础影子概率。
2. 仅对截止前身份为 K1 的新预测读取指定批次和快照。
3. 纯函数计算特征、逐规则结果和拟议动作。
4. shadow 模式只追加 assessment，不回滚或修改基础预测。
5. importer 幂等导入；赛果产生后更新独立前向评估报告。

所有命令离线运行，不增加 500 请求。退出码：`0` 成功或无变化，`1` 部分 assessment 不可用，`2` 参数/配置/schema 错误，`3` 哈希/引用/存储/数据库完整性错误。

## 9. 实施顺序

### Phase 1：历史复现和规则冻结（已完成）

1. 复算固定哈希、330 行、赛季数量及本文统计。
2. 对每条候选规则输出按赛季和 rolling-origin 拆分的校准残差。
3. 检查规则重叠、触发率、公司配对覆盖和阈值敏感性。
4. 冻结 shadow-v1 配置和真实 `effective_at`；历史结果不得产生 active 动作。

### Phase 2：append-only shadow 护栏（v2代码完成，等待自然窗口）

1. 实现截止时间、合法发布时间和as-of身份冻结查询及纯规则函数。
2. 新增迁移 014、record/importer/current view 和报告。
3. 接入现有 shadow 任务，不重新注册任务，不新增网络请求。
4. 对未来自然窗口先 dry-run，再正式追加 assessment。
5. 不补发策略生效前预测。
6. 历史回放固定为只读、零持久化且不进入前向门禁。

### Phase 3：前向评估和人工启用

1. 每累计 25 个自动结算 fixture 更新报告，不调整阈值。
2. 按 target 和 rule 独立执行门禁。
3. 门禁通过只标记 `review_eligible`。
4. 经新决策后采用新版本逐级启用，并保留熔断能力。

## 10. 测试与验收

必须覆盖：

- 固定数据三类哈希、330 行、赛季数量和准确时间范围。
- 主队/客队热门视角下亚盘差值符号一致。
- 同公司配对后再聚合，新增/消失公司不制造虚假变化。
- 四分之一球边界、概率/赔率阈值、60% 共识边界和 MAD。
- 平局最高时依赖热门让球的规则不可评估。
- R0-R6 单规则及全部组合矩阵。
- 辅助规则不能自行累加成 downgrade。
- R1/R2 与独立 R3/R4/R6 的 abstain 组合。
- 实时稳定要求 3 个不同响应和至少 30 分钟跨度。
- 截止后身份或盘口不可见；跨 target、批次或快照混用失败。
- 重复 assessment/import 新增 0；相同 ID 不同输入拒绝覆盖。
- shadow 模式下原概率、置信、风险、发布状态和唯一键完全不变。
- 自动与人工赛果分开，人工结果不能解除 active 门禁。
- 首次导入、重复导入、失败回滚和 PostgreSQL 空库重放。
- `football.records`、模型资格、严格切点计数完全不变。
- 全部离线测试禁止网络。

最终验收：

- 历史报告能确定性复现 330 场事实，且明确标记 retrospective。
- 每条拟议动作都能回溯到同一 cutoff、批次、公司行、差值和规则阈值。
- 不出现模糊公司合并、两点数据伪装实时稳定或结果方向反转。
- shadow 记录不改变已有预测行为。
- 前向评估按 target、rule、策略版本和赛果方法拆分。
- `review_eligible` 不自动启用规则。
- `unsupported_records=0`，幂等导入和空库重放一致。
- `pytest`、PostgreSQL 集成测试、`git diff --check`、密钥扫描和 Git 忽略检查通过。

## 11. 默认结论

- K1 护栏是最终目标功能，当前先 shadow，不是永久停留在报告层。
- 盘口差值只有在同公司、同批次、同切点、截止前证据中才有解释力。
- 330 场历史数据只用于发现候选和冻结阈值，不能直接启用降置信。
- 护栏永不修改市场概率、提高置信或产生反向投注结论。
- 本功能不解除 7 天、30 天、阶段 4 每切点 500 场或 Web 门禁。
- 不使用球队级特征，不推断庄家操纵或比赛异常。
