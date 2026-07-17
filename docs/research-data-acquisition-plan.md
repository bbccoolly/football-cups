# 公开历史数据研究与离线基线运行手册

> 版本：V2.1
> 状态：已实施，可重复运行
> 更新日期：2026-07-17

## 1. 定位

本路线使用 2025 年至当前时间的公开静态历史文件，快速验证纯盘口市场基线。所有记录固定为：

```text
research_only=true
backfill=true
strict_backtest_eligible=false
cutoff_eligible=false
```

它不属于阶段 4，不生成当前比赛预测，不替代 7 天、30 天或每切点 500 场严格门禁。

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

## 7. 2026-07-17 首次执行证据

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

## 8. 退出码

- `0`：成功或静态资产未变化。
- `1`：部分成功或可重试来源失败。
- `2`：配置、输入或 schema 无效。
- `3`：哈希、存储、访问门禁或数据库完整性失败。
