# 公开历史数据研究与离线基线运行手册

> 版本：V2.3
> 状态：已实施，可重复运行
> 更新日期：2026-07-21

## 1. 定位

本路线使用 2025 年至当前时间的公开静态历史文件，快速验证纯盘口市场基线。历史记录固定为：

```text
research_only=true
research_kind=historical
backfill=true
strict_backtest_eligible=false
cutoff_eligible=false
```

它不属于阶段 4，不替代 7 天、30 天或每切点 500 场严格门禁。D-029 额外允许隔离影子预测：模型产物使用 `research_kind=model_artifact/backfill=true`，当前比赛影子发布使用 `research_kind=shadow_event/backfill=false`，但全部仍为 `research_only=true`、`strict_backtest_eligible=false`、`cutoff_eligible=false`。

## 2. 来源注册表

首版只允许 `football_cups.research.registry` 中的显式 URL：

- Football-Data 五大联赛 2024/25、2025/26 共10个 CSV。
- Football-Data `ARG`、`BRA`、`CHN`、`JPN`、`USA`、`FIN`、`NOR`、`SWE` 八个额外联赛 CSV。
- Football-Data `WorldCup2026.xlsx` 的 `WorldCup2026` 和 `WorldCup2026Qualifiers` 工作表。
- 人工指定的 K1 `k1_core3_features.csv` 和 metadata；两者只作为已加工派生特征导入。

CentroQuote、OddsHarvester 批量任务和 500 历史页面保持禁用。注册表不通过页面爬取自动扩展。

## 3. 访问控制

- 单并发，同域至少间隔10秒，并增加最多20%的非身份性抖动。
- 每域24小时最多60次请求和200MB；SQLite 跨进程持久计数。
- 使用来源锁、条件 GET、ETag 和 Last-Modified，不先 HEAD 再 GET。
- `429` 遵守 `Retry-After`；网络或 `5xx` 只在60秒和300秒后重试。
- 连续三次失败暂停24小时；`401/403/567`、验证码或拦截页暂停7天。
- 单文件上限50MB，XLSX解压上限200MB。
- 禁止代理/IP/Cookie轮换、指纹伪装、验证码绕过和未注册域名跳转。
- 初次导入后不安装定时任务；更新由人工执行条件下载。

访问状态位于 `data/research/state/research.sqlite3`，被 Git 忽略。

## 4. 命令

查看固定来源：

```powershell
.\.venv\Scripts\football-cups-research.exe catalog --workspace .
```

低频下载全部注册的 Football-Data 静态资产：

```powershell
.\.venv\Scripts\football-cups-research.exe fetch `
  --workspace . `
  --source football-data `
  --since 2025-01-01
```

只做单资产 smoke 可增加 `--asset <asset-id>`。下载器只保存原始 blob、状态和 manifest，不自动安装计划任务。

导入已接受的 K1 派生特征：

```powershell
.\.venv\Scripts\football-cups-research.exe import-k1 `
  --workspace . `
  --input D:\2026-worldCup\data\features\korea\k1_core3_features.csv `
  --metadata D:\2026-worldCup\data\features\korea\k1_core3_features.metadata.json
```

命令固定校验 CSV SHA-256、metadata SHA-256、metadata `inputHash`、330个唯一 fixture 以及 2025/2026 的228/102分布。导入后不再依赖外部仓库路径。

标准化、数据库导入和报告：

```powershell
.\.venv\Scripts\football-cups-research.exe normalize --workspace . --since 2025-01-01
.\.venv\Scripts\football-cups-research.exe db-import --workspace .
.\.venv\Scripts\football-cups-research.exe report-coverage --workspace .
.\.venv\Scripts\football-cups-research.exe evaluate-baseline --workspace .
```

隔离影子预测命令：

```powershell
.\.venv\Scripts\football-cups-research.exe build-model-dataset `
  --workspace . `
  --training-before-date 2026-01-01

.\.venv\Scripts\football-cups-research.exe train-model `
  --workspace . `
  --training-before-date 2026-01-01 `
  --activate `
  --channel research-shadow-v1

.\.venv\Scripts\football-cups-research.exe db-import --workspace .

.\.venv\Scripts\football-cups-research.exe shadow-predict `
  --workspace . `
  --channel research-shadow-v1

.\.venv\Scripts\football-cups-research.exe evaluate-shadow `
  --workspace . `
  --channel research-shadow-v1
```

`shadow-predict` 默认只处理 `T-24h`、`T-6h`、`T-60m`、`T-10m` 四个产品切点。它只读取正式库中 `event_origin=live` 的 V2 模型合格批次，且必须在 `prediction_cutoff` 至 `min(prediction_cutoff+10m, kickoff_at-1m)` 之间发布；错过窗口不得历史补发。输出仅为90分钟胜/平/负概率，不含投注选择或收益信息。

K1 可使用 `analyze-k1 --dry-run` 展示中文护栏后方案，或使用 `blind-test-k1-guardrail` 对正式 live V2 快照执行只读 as-of 回放。盲测默认不查询赛果，只有 `--reveal-result` 才在判断完成后揭示；两条命令均不写研究事实。close 固定为该 target 截止前且在合法发布时间内可用的最后一个合格批次即时值。

`analyze-k1`默认输出详细中文报告，也支持`--format summary|json`和`--audit`。历史上下文只读取固定K1派生资产中在prediction cutoff前已产生标签的比赛，采用版本化固定cohort；final closing统计不调整当前as-of概率和护栏动作。

赛事策略读取 `config/research-competition-profiles.json`。发布前按 `observed_at <= prediction_cutoff` 重新选择身份，优先使用显式 `competition_id`，ID缺失时才允许精确名称别名。注册表保存文件字节哈希和 canonical JSON 哈希；未知或冲突赛事记录 abstention。赛事等级只限制置信，不修改去水共识概率。初始赛事均为 `provisional`；A/B当前最高 medium、C最高 low、D只观察。

## 5. 数据语义

- Football-Data 五大联赛保留 opening 和 closing 的 1X2、亚洲让球、大小球字段。
- 额外联赛只提供 closing 1X2，固定为 `1x2_only`。
- 世界杯工作簿只进入 closing 1X2。`Finished=90 minutes` 且比分合法时才获得90分钟研究标签；资格赛和加时/点球记录自动隔离。
- K1 进入 `derived_closing_features`，不得反向构造公司盘口行。
- opening/closing 均不得映射成正式 `T-24h` 等切点。

## 6. 评估规则

2025 年用于滚动赛事先验，2026 年用于时间分离评估。由于 K1 2026 已在外部项目中做过 replay，报告固定标记 `retrospective_time_separated_not_blind`。

首版比较：

- 均匀概率。
- 只使用过去赛果的赛事先验。
- 1X2 去水市场概率。

按来源、赛事、cohort 和年份报告 Log Loss、Brier、RPS、ECE。ECE 使用10个等频分箱；少于100场不输出校准结论。数据集哈希、Git 提交、训练 fixture 和评估 fixture 全部随报告保存。

影子评估单独按切点、模型版本、赛事ID、赛事类型、市场证据等级、置信、自动赛果和全部有效赛果报告。人工声明标签可进入“全部有效赛果”分组，但不得提升自动赛果覆盖率，也不得解除 high 置信样本门禁。同一赛事和切点至少200场自动验证fixture且跨度至少90天后只标记为可复审，不自动修改等级。影子指标只用于研究观察，不能作为阶段4工程通过条件。

## 7. Windows 影子任务

独立任务名称为 `FootballCups-Research-Shadow-Prediction`，默认每2分钟运行：

```powershell
scripts\windows\run_shadow_prediction.ps1 -Workspace . -Channel research-shadow-v1
```

注册脚本为：

```powershell
scripts\windows\install_shadow_prediction_task.ps1 -Workspace .
```

在无人值守配置中，`configure_database_task_user.ps1` 会使用同一轮内存随机密码为数据库导入任务和影子预测任务注册 `Password` 登录，不把密码写入文件、日志或 Git。

## 8. 2026-07-17 首次执行证据

- robots 请求和19个静态资产共20次请求，6,234,841字节；0次拦截、0次重试、0次熔断。
- 后续单资产条件 GET 返回 HTTP 304、正文0字节，确认 ETag/Last-Modified 缓存路径有效。
- 五大联赛按 `match_date >= 2025-01-01` 实得2,686场。旧摘要的2,685场漏计了2025-01-01 Brentford 1-3 Arsenal，现按原始 CSV 修正。
- Football-Data 共7,074场；K1派生特征330场；合计7,404场/派生行。
- 2025年5,161，2026年2,243；市场观察116,073条。
- 世界杯与预选赛608场，其中514场因不能证明90分钟口径形成质量事件并排除标签。
- 研究可用结果6,890场。
- PostgreSQL 首次导入123,497条，补齐质量事件后增量514条，累计124,011条；无变化重复导入0条，`research.feature_rows=330`。
- 导入前后正式各切点严格赛果计数均为4。
- 2026 Football-Data closing 去水市场 Log Loss `0.99`，赛事先验 `1.07`，均匀基线 `1.10`。
- 2026 K1派生市场基线 Log Loss `1.06`，样本102场，只作为小样本研究结果。

首次执行的覆盖和评估 JSON 位于 `data/research/reports/`，不进入 Git。

## 9. 影子预测运行证据

截至2026-07-21，研究库包含1个数据集、1个模型版本、1个激活版本、10条迁移013前影子预测和3条影子评估。10条预测涉及4个fixture且均已有自动赛果；Log Loss为1.1828、方向命中率40%，样本不足，不能形成校准或赛事升降级结论。

迁移013已启用D-030赛事分层。旧预测按 `legacy_unclassified` 保留；第一条携带as-of身份、双哈希、赛事等级、置信和风险字段的新记录必须由未来自然窗口生成，不允许历史补发。

迁移014已启用D-031 K1规则护栏shadow层。`evaluate-k1-guardrail-history`复现330场聚合资产，但固定标记`historical_exact_evaluable=false`，不构造公司一致率、精确R1/R2、R4或R5。精确assessment只从`prediction_cutoff >= effective_at`的未来live V2自然发布产生；shadow只记录拟议动作，不修改概率、置信或发布状态。

D-034增加`research-k1-analysis-workflow.json`。未来K1 assessment冻结基础欧赔输入和护栏三市场输入的独立canonical哈希，并与前一自然切点比较`unchanged/partial_update/full_update`；模拟回放只读展示时必须标记模拟前序来源。工作流字段不参与R0-R6动作。`evaluate-k1-guardrail-forward`输出V2概率评分、均匀基线、风险捕获和自动/人工拆分，只有自动证据集合哈希变化才写入新评估。

迁移015实现D-035欧战盘口差异护栏。适用范围严格为欧冠`competition_id=101`和欧罗巴`63`，策略文件为`config/research-europe-guardrail.json`。它不依赖`ResearchModelActivation`：同一 target、截止时间和可用时间内的最后一个模型合格 V2 批次提供三类市场，基础概率只取逐公司去水欧赔中位数。公司 ID 不可用时只允许 NFC、去首尾空格和合并空白后的精确名称；ID/名称映射冲突或重复行只作为不可配对机构证据，不能模糊合并或删除真实异议。异常、持续不变、单点跳变、跨市场冲突、MAD 和 leave-one-out 全部保存为审计细节，规则不得修改基础概率或反转方向。

```powershell
.\.venv\Scripts\football-cups-research.exe europe-guardrail-shadow --workspace . --dry-run
.\.venv\Scripts\football-cups-research.exe analyze-europe --workspace . --fixture-id <id> --target T-60m --format detailed --dry-run
.\.venv\Scripts\football-cups-research.exe replay-europe-guardrail --workspace . --fixture-id <id> --target T-60m
.\.venv\Scripts\football-cups-research.exe evaluate-europe-guardrail-forward --workspace . --channel research-europe-guardrail-v1
```

`replay-europe-guardrail`默认隐藏赛果，使用`--reveal-result`只在分析完成后展示，并始终标记`retrospective_as_of_replay/persisted=false/forward_gate_eligible=false`。自然窗口仅在`prediction_cutoff >= effective_at`时追加 assessment；无数据追加 abstention，错过窗口不得补发。前向评估只使用策略生效后的自然 assessment，自动赛果与负责人声明分开报告；达到同赛事、同切点200场自动验证且90天跨度、单规则50次触发时只产生`review_eligible`，不自动启用任何动作。

```powershell
.\.venv\Scripts\football-cups-research.exe evaluate-k1-guardrail-history --workspace .
.\.venv\Scripts\football-cups-research.exe evaluate-k1-guardrail-forward --workspace . --channel research-shadow-v1
```

## 10. 退出码

- `0`：成功或静态资产未变化。
- `1`：部分成功或可重试来源失败。
- `2`：配置、输入或 schema 无效。
- `3`：哈希、存储、访问门禁或数据库完整性失败。
