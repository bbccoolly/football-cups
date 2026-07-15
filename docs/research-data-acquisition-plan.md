# 免费研究数据获取计划

> 版本：V1.0
> 状态：部分执行
> 更新日期：2026-07-15
> 适用范围：个人学习研究数据获取，不代表正式产品数据源验收通过

本文档固化 2025 年至当前时间的免费研究数据获取步骤。它用于验证字段形态、覆盖范围、盘口结构和时间风险，不能替代 `docs/data-source-evaluation.md` 中的授权数据源验收。

## 1. 固定边界

- 数据时间范围：从 `2025-01-01T00:00:00Z` 到每次执行时的真实时间。
- 研究输入：欧赔 1X2、亚洲让球、大小球、多公司盘口变化及相关时间字段。
- 预测口径：常规时间 90 分钟及补时。
- 研究目的：学习、样本探索、字段验证和方法验证。
- 阶段门禁：免费研究数据不能让项目跳过正式数据源授权验收；正式数据库、模型和 Web 产品仍等待阶段 1 完成。

## 2. 推荐免费来源组合

| 来源 | 用途 | 当前定位 | 注意事项 |
| --- | --- | --- | --- |
| Football-Data | 五大联赛赛果、开盘/收盘赔率基线 | 基础 CSV 来源 | 没有完整四切点历史时间线 |
| OddsHarvester | 历史盘口研究，覆盖多赛事、多市场、多公司 | 主要研究来源 | 需验证历史变盘时间年份修正问题 |
| FlashscoreScraper | 单场人工交叉验证 | 辅助校验来源 | 只用于小样本人工核对 |

已确认 Football-Data 的五大联赛 2024/25 和 2025/26 CSV 可访问；已确认 OddsHarvester `0.4.0` 支持 `1x2`、具体大小球枚举如 `over_under_2_5`、具体亚洲让球枚举如 `asian_handicap_0`、`--odds-history`、JSON/CSV、多公司和历史赛季。以上确认时间为 2026-07-15。

## 3. 数据分层规则

历史回抓数据必须与未来前瞻采集数据分层保存和标记：

| 字段 | 历史回抓 | 前瞻采集 |
| --- | --- | --- |
| `backfill` | `true` | `false` |
| `observed_at` | 实际下载时间，不得倒填 | 系统首次看到记录的真实时间 |
| `strict_backtest_eligible` | `false` | 仅当满足防泄漏条件时为 `true` |
| 用途 | 覆盖研究、字段验证、非严格历史分析 | 严格 observed-at 回测和未来产品验证 |

历史回抓数据的 `source_event_time` 可以用于研究变盘顺序，但不得伪装成本系统在当时已经观察到的数据。

## 4. 本地目录

所有研究数据放在被 Git 忽略的 `data/research/` 下：

```text
data/research/
  raw/football-data/<run_id>/
  raw/oddsharvester/<run_id>/
  raw/flashscore/<run_id>/
  derived/<run_id>/
  manifests/
  manual/
  reports/
```

`run_id` 使用执行时间，例如 `20260715T143000Z`。任何原始文件不得覆盖，重复执行时创建新的 `run_id`。

## 5. 环境准备

当前本机可用 Python 包括 Python 3.14 和 Python 3.11；OddsHarvester `0.4.0` 要求 Python `>=3.12`，优先使用 Python 3.14。

建议命令：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install oddsharvester==0.4.0
.\.venv\Scripts\python.exe -m playwright install chromium
```

`.venv/` 和 `data/**` 已被 `.gitignore` 忽略。安装后先确认 `.env`、原始数据、日志和缓存没有进入 Git。

## 6. Football-Data 获取步骤

目标：获取五大联赛从 2024/25 到 2025/26 的 CSV，用于赛果、开盘/收盘赔率和基础字段核验。

联赛代码：

| 联赛 | 代码 |
| --- | --- |
| 英超 | `E0` |
| 西甲 | `SP1` |
| 德甲 | `D1` |
| 意甲 | `I1` |
| 法甲 | `F1` |

赛季代码：

| 赛季 | 代码 | 用途 |
| --- | --- | --- |
| 2024/25 | `2425` | 截取 2025-01-01 之后比赛 |
| 2025/26 | `2526` | 覆盖 2025-08 至当前时间 |

执行步骤：

1. 创建 `data/research/raw/football-data/<run_id>/`。
2. 下载 10 个 CSV：`2425`、`2526` 乘以五大联赛代码。
3. 为每个文件记录 URL、下载时间、HTTP 状态、文件大小和 SHA256。
4. 生成范围统计：最早比赛日期、最晚比赛日期、行数、目标日期范围内行数。
5. 只保留 `2025-01-01` 至当前时间的记录进入派生层。
6. 输出 `data/research/reports/<run_id>-football-data-summary.md`。

验收点：

- 10 个 CSV 均下载成功。
- 日期解析正确。
- 目标范围内的赛果字段存在。
- 能识别 1X2、大小球、亚洲让球和收盘字段。

## 7. OddsHarvester 获取步骤

目标：获取目标赛事的历史盘口和历史变盘数据，用于研究多公司、三类市场和时间线结构。

已确认 slug：

| 赛事 | slug |
| --- | --- |
| 英超 | `england-premier-league` |
| 西甲 | `spain-laliga` |
| 德甲 | `germany-bundesliga` |
| 意甲 | `italy-serie-a` |
| 法甲 | `france-ligue-1` |
| 欧冠 | `champions-league` |
| 欧联 | `europa-league` |
| 欧协联 | `conference-league` |
| 世界杯 | `world-cup` |
| 韩国 K1 | `south-korea-k-league-1` |

暂未确认世界杯预选赛 slug，需要人工通过公开页面核验比赛 URL。

建议执行顺序：

1. 无历史变盘小样本：英超 `2025-2026`、`1x2`、`--max-pages 1`。
2. 三场跨年份历史变盘小样本：2025 年 1 至 5 月、2025 年 8 至 12 月、2026 年 K1/欧战/世界杯各选其一。
3. 英超完整研究样本。
4. 五大联赛完整研究样本。
5. 韩国 K1 研究样本。
6. 欧冠、欧联、欧协联研究样本。
7. 世界杯研究样本。
8. 世界杯预选赛人工 URL 样本。

市场逐项运行，不混在一个文件中。OddsHarvester 不使用泛称 `over_under` 或 `asian_handicap`，需要指定具体盘口线：

```text
1x2
over_under_2_5
asian_handicap_0
```

后续批量研究时，应先用 `over_under_2_5` 和 `asian_handicap_0` 验证结构，再根据赛事样本扩展到其他盘口线枚举。

运行规则：

- 使用 `--timezone UTC --locale en-GB`。
- 每个赛事、赛季、市场独立输出 JSON。
- 开启 `--odds-history` 前先完成无历史小样本。
- 并发设为 `1`，请求间隔至少 `2` 秒。
- 不覆盖旧文件；每次执行创建新 `run_id`。
- 每个命令记录开始时间、结束时间、退出码、目标赛事、赛季、市场和输出路径。

## 8. 历史变盘时间修正规则

已识别 OddsHarvester `0.4.0` 的潜在风险：历史变盘弹窗时间解析可能使用当前年份，导致 2026 年回抓 2025 年比赛时，变盘时间年份被错误标为 2026。

处理规则：

1. 原始 JSON 不修改。
2. 派生数据中新增修正后的时间字段，不覆盖原字段。
3. 从 `kickoff_year` 和 `kickoff_year - 1` 中选择最接近且不晚于开球的候选时间。
4. 晚于开球、距离开球超过 180 天或无法判断的记录进入隔离报告。
5. 每次修正输出数量统计和样例，人工确认后再扩大批量。

该规则只适用于研究派生层，不构成正式数据源时间语义验收。

## 9. 人工任务

人工需要先补齐以下材料：

- [ ] 找 3 场跨年份 OddsPortal 比赛 URL：2025 年 1 至 5 月、2025 年 8 至 12 月、2026 年 K1/欧战/世界杯。
- [ ] 核验世界杯各赛区预选赛页面，并记录可访问比赛 URL。
- [ ] 完成 40 场浅检：五大联赛、欧战、世界杯或预选赛、K1 各 10 场。
- [ ] 完成 12 场深检：每类 3 场，每场核对 2 至 3 家公司。
- [ ] 记录 UTC 开球时间、90 分钟赛果、三类盘口、盘口线、公司名称和时间偏差。

人工核验记录建议放入 `data/research/manual/<run_id>/`，如需提交到 Git，必须先确认不包含受限原始数据。

## 10. 自动化任务

Agent 后续可以执行以下范围受限的研究任务：

1. 创建 `data/research` 目录结构。
2. 下载 Football-Data 的 10 个 CSV，生成 manifest 和范围统计。
3. 安装 OddsHarvester 到 `.venv`。
4. 运行英超 `2025-2026`、`1x2`、`--max-pages 1` 的无历史测试。
5. 使用人工提供的比赛 URL 测试 `--odds-history`。
6. 检查实际 JSON 结构和历史年份问题。
7. 编写研究派生转换脚本和隔离报告。
8. 更新 `docs/project-status.md` 和相关研究报告。

这些任务不得升级为正式采集器、正式数据库 schema 或模型训练，除非阶段 1 正式验收完成。

## 11. 小样本通过标准

进入批量研究前，小样本必须同时满足：

- Football-Data CSV 下载、哈希和日期范围统计正常。
- OddsHarvester 无历史样本能稳定输出 JSON。
- 至少 1 场 `--odds-history` 样本可解析三类市场中的至少一类。
- 能明确识别比赛开球时间和盘口记录时间。
- 历史年份修正规则能产出隔离报告，而不是静默修正。
- 所有原始数据仍位于 `data/` 下且未进入 Git。

## 12. 下一步

下一次执行应从补齐跨时段、跨赛事样本开始：

1. 人工提供 2025 年 8 至 12 月比赛 URL，以及 2026 年 K1/欧战/世界杯比赛 URL。
2. 每个新时段先执行 `1x2`、`over_under_2_5`、`asian_handicap_0` 单场无历史验证。
3. 每个新时段至少执行一类市场的 `--odds-history`，核对年份修正与隔离统计。
4. 使用 `scripts/research/normalize_oddsharvester_sample.py` 生成研究派生数据，不修改原始 JSON。
5. 不扩大 CentroQuote 完整历史批量回填；英超单页成功率仅 46%，不足以承担完整历史来源。

若没有新的人工 URL，当前免费研究获取工作停在已验证范围，不重复扩大 CentroQuote 批量抓取，也不应把单场结果外推到 K1、欧战或世界杯。

## 13. 执行记录

### 2026-07-15 小样本执行

- Football-Data：已完成 10 个 CSV 下载，`run_id=20260715T063843Z`。
- Football-Data 结果：下载成功 10/10，总行数 3504，日期解析成功 3504，目标范围内 2685 行。
- Football-Data 报告：`data/research/reports/20260715T063843Z-football-data-summary.md`。
- OddsHarvester：已在 `data/research/runtime/venv-py314` 安装 `oddsharvester==0.4.0` 并验证 CLI 可用。
- OddsHarvester CLI：实际入口为 `python -m oddsharvester historic`；无历史样本命令需要使用 `historic -s football -l england-premier-league --season 2025-2026 -m 1x2 --max-pages 1`。
- Playwright Chromium：默认 CDN、`npmmirror` 和 `--only-shell` 下载均未完成；已通过 junction 将 `C:\Users\lcz\AppData\Local\ms-playwright\chromium-1228\chrome-win64` 指向本机 Chrome 安装目录，使 `--no-headless` 可启动。
- OddsPortal 主站：英超历史页提示 selected bookmakers 无可用赔率，列表页没有比赛行。
- CentroQuote 镜像：`https://www.centroquote.it` 可显示英超 2024/25 列表页，诊断样本显示 `eventRow=50`。
- OddsHarvester 本地研究补丁：允许 `www.centroquote.it` 作为 `--match-link` 域名，并支持 CentroQuote 的意大利月份缩写；可用 `scripts/research/patch_oddsharvester_centroquote.py` 在重建环境后重复应用。
- OddsHarvester 1X2 单场：`Liverpool 5-1 Tottenham` 样本成功，输出 `data/research/raw/oddsharvester/20260715T072931Z/single-liverpool-tottenham-2024-2025-1x2-centroquote.json`。
- OddsHarvester 三市场单场：修复月份解析后，1X2、`over_under_2_5` 和 `asian_handicap_0` 均成功；OU 解析 2 家公司，AH 解析 1 家公司。
- OddsHarvester 历史变盘：1X2 `--odds-history` 成功解析 2 家公司；12 个时间戳把 2025 年错误写成 2026 年，已在派生层修正且无隔离项。
- 赔率格式：CentroQuote 在切换历史场次时仍返回 Money Line。原始层保留 `-500`、`+600` 等值；派生脚本明确按 American Odds 公式生成十进制字段。
- 派生样本：单场历史样本转换 18 个赔率值、修正 12 个时间戳；`backfill=true`，`strict_backtest_eligible=false`。
- 英超单页批量：`run_id=20260715T075122Z`，50 个链接成功 23、失败 27，成功率 46%；成功记录日期为 2025-04-22 至 2025-05-25，比分与市场无缺失，共 46 个公司行。
- 批量结论：CentroQuote 只能作为免费研究辅助源，不能作为完整历史回填主源；未解析 fragment 的场次必须丢弃，禁止退回 stale JSON。

重建研究环境后执行本地兼容补丁：

```powershell
data\research\runtime\venv-py314\Scripts\python.exe `
  scripts\research\patch_oddsharvester_centroquote.py
```

原始 JSON 抓取完成后生成派生样本：

```powershell
data\research\runtime\venv-py314\Scripts\python.exe `
  scripts\research\normalize_oddsharvester_sample.py `
  <raw-json> <derived-json> `
  --source-timezone Europe/Rome
```

派生脚本只接受已确认是 American Odds 的 CentroQuote 样本。若网站返回格式改变，必须先人工核验，不得直接套用转换。
