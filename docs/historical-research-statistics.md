# 历史研究数据统计

> 更新日期：2026-07-20
> 数据范围：隔离历史研究层 `data/research/`
> 口径：`research_only=true`、`backfill=true`、`strict_backtest_eligible=false`、`cutoff_eligible=false`

本页整理 D-022 允许的公开静态历史研究数据。它只用于回答“纯盘口方法是否值得继续研究”，不得直接作为正式当前比赛预测、正式模型、Web/API，也不得计入阶段 4 的严格样本门禁。D-029 允许的影子预测是隔离派生输出，仍必须保持 research-only。

## 1. 总量

最新离线统计重新生成于 `2026-07-20 14:24 Asia/Shanghai`：

- 覆盖报告：`data/research/reports/coverage/20260720T062410389935Z-b9974fa6.json`
- 基线评估：`data/research/reports/evaluation/20260720T062422647516Z-2c22adc4.json`
- 数据集哈希：`d58d611927407b6ce5a32047b1f7c831f8ddf48763a54c7e8c8d48a930ac5c84`

| 指标 | 数量 |
| --- | ---: |
| 总记录 | 124,011 |
| 来源静态资产 | 20 |
| 来源资产字节 | 6,410,259 |
| ResearchFixture | 7,074 |
| ResearchFeatureRow | 330 |
| fixture 与派生行合计 | 7,404 |
| 市场观察 | 116,073 |
| 研究可用赛果 | 6,890 |
| 质量事件 | 514 |

所有质量事件均为 `result_scope_ambiguous`，主要来自世界杯预选赛 90 分钟口径不足。世界杯 2026 正赛也有少量记录因口径不足未进入研究可用赛果。

## 2. 来源与年份

| 来源 | 行数 | 说明 |
| --- | ---: | --- |
| `football-data` | 7,074 | Football-Data 静态 CSV/XLSX 归一化 fixture |
| `k1-derived-core3` | 330 | K1 派生三 outcome 特征行，不作为原始盘口导入 |

| 年份 | fixture/派生行 |
| --- | ---: |
| 2025 | 5,161 |
| 2026 | 2,243 |

基线评估使用 2025 作为滚动先验训练段，2026 作为已知非盲的时间分离评估段。训练 fixture 为 4,671，评估 fixture 为 2,218，两者无重叠。

## 3. 赛事覆盖

| 赛事 | 合计 | 2025 | 2026 | 研究可用赛果 | 主胜 | 平 | 客胜 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina / Liga Profesional | 764 | 510 | 254 | 764 | 312 | 244 | 208 |
| Brazil / Serie A | 557 | 380 | 177 | 557 | 278 | 146 | 133 |
| Bundesliga | 477 | 306 | 171 | 477 | 191 | 122 | 164 |
| China / Super League | 380 | 240 | 140 | 380 | 179 | 96 | 105 |
| Finland / Veikkausliiga | 267 | 177 | 90 | 267 | 117 | 67 | 83 |
| Japan / J1 League | 380 | 380 | 0 | 380 | 168 | 97 | 115 |
| K1 | 330 | 228 | 102 | 330 | 122 | 93 | 115 |
| La Liga | 579 | 370 | 209 | 579 | 272 | 142 | 165 |
| Ligue 1 | 476 | 314 | 162 | 476 | 220 | 105 | 151 |
| Norway / Eliteserien | 337 | 240 | 97 | 337 | 169 | 66 | 102 |
| Premier League | 572 | 378 | 194 | 572 | 241 | 145 | 186 |
| Serie A | 582 | 368 | 214 | 582 | 229 | 158 | 195 |
| Sweden / Allsvenskan | 337 | 240 | 97 | 337 | 135 | 78 | 124 |
| USA / MLS | 758 | 540 | 218 | 758 | 344 | 183 | 231 |
| World Cup 2026 | 102 | 0 | 102 | 94 | 46 | 20 | 28 |
| World Cup 2026 Qualifiers | 506 | 490 | 16 | 0 | 0 | 0 | 0 |

研究可用赛果合计 6,890 场：主胜 3,023 场、平局 1,762 场、客胜 2,105 场，对应约 43.9%、25.6%、30.6%。

## 4. 市场覆盖

| 市场 | 观察数 |
| --- | ---: |
| 1x2 | 66,256 |
| 亚洲让球 | 24,917 |
| 大小球 | 24,900 |

| 来源 | cohort | 市场 | 覆盖 fixture | 平均公司/行 | 最小 | 最大 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| football-data | closing | 1x2 | 7,073 | 5.88 | 1 | 11 |
| football-data | closing | asian_handicap | 2,686 | 4.64 | 3 | 5 |
| football-data | closing | total | 2,686 | 4.63 | 3 | 5 |
| football-data | opening | 1x2 | 2,686 | 9.19 | 5 | 11 |
| football-data | opening | asian_handicap | 2,681 | 4.64 | 3 | 5 |
| football-data | opening | total | 2,686 | 4.64 | 3 | 5 |

要点：closing 1x2 基本覆盖 Football-Data 全部研究 fixture，但缺 1 场可用观察；opening 1x2、亚盘和大小球主要覆盖五大联赛 2,686 场，不应外推到全部 16 个赛事组。

## 5. 基线表现

指标含义：Log Loss、Brier、RPS 越低越好；ECE 越低表示置信度校准越接近真实命中率。当前只比较三类简单基线：均匀概率、赛事历史先验、去水市场概率。

| 来源 | cohort | 年份 | 基线 | 样本 | Log Loss | Brier | RPS | ECE |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| football-data | closing | all | uniform | 6,559 | 1.0986 | 0.6667 | 0.2354 | 0.1090 |
| football-data | closing | all | prior | 6,559 | 1.0779 | 0.6520 | 0.2305 | 0.0249 |
| football-data | closing | all | market | 6,559 | 0.9873 | 0.5890 | 0.2011 | 0.0182 |
| football-data | closing | 2026 | uniform | 2,116 | 1.0986 | 0.6667 | 0.2340 | 0.1152 |
| football-data | closing | 2026 | prior | 2,116 | 1.0743 | 0.6493 | 0.2272 | 0.0439 |
| football-data | closing | 2026 | market | 2,116 | 0.9876 | 0.5895 | 0.1994 | 0.0347 |
| football-data | opening | 2026 | market | 950 | 0.9943 | 0.5936 | 0.2002 | 0.0396 |
| k1-derived-core3 | derived_closing_features | 2026 | uniform | 102 | 1.0986 | 0.6667 | 0.2239 | 0.1340 |
| k1-derived-core3 | derived_closing_features | 2026 | prior | 102 | 1.1320 | 0.6904 | 0.2340 | 0.1840 |
| k1-derived-core3 | derived_closing_features | 2026 | market | 102 | 1.0574 | 0.6394 | 0.2097 | 0.1180 |

结论：

- Football-Data 2026 closing 去水市场概率的 Log Loss 为 0.9876，优于赛事先验 1.0743 和均匀基线 1.0986。
- Football-Data 2026 opening 市场也优于先验和均匀基线，但样本只覆盖五大联赛 950 场。
- K1 2026 派生市场基线优于均匀和先验，但只有 102 场，不能单独形成高置信结论。
- opening 全年样本 Log Loss 0.9742 低于 closing 全年 0.9873，但 2026 时间分离段中 closing 0.9876 略优于 opening 0.9943；目前不宜据此下结论说开盘或收盘稳定更强。

## 6. 2026 分赛事 closing 市场表现

| 赛事 | 样本 | Log Loss | Brier | RPS | ECE |
| --- | ---: | ---: | ---: | ---: | ---: |
| World Cup 2026 | 93 | 0.7834 | 0.4535 | 0.1435 | 0.1440 |
| Norway / Eliteserien | 97 | 0.9459 | 0.5572 | 0.1990 | 0.1331 |
| Bundesliga | 171 | 0.9591 | 0.5670 | 0.1886 | 0.0734 |
| Serie A | 214 | 0.9601 | 0.5704 | 0.1933 | 0.0752 |
| Finland / Veikkausliiga | 90 | 0.9787 | 0.5854 | 0.1878 | 0.1296 |
| La Liga | 209 | 0.9816 | 0.5822 | 0.2031 | 0.0831 |
| Sweden / Allsvenskan | 97 | 0.9890 | 0.5880 | 0.2007 | 0.1235 |
| Brazil / Serie A | 177 | 0.9992 | 0.5979 | 0.2015 | 0.0901 |
| USA / MLS | 218 | 1.0008 | 0.5969 | 0.2133 | 0.0767 |
| Ligue 1 | 162 | 1.0066 | 0.6044 | 0.2045 | 0.1048 |
| China / Super League | 140 | 1.0128 | 0.6079 | 0.2126 | 0.1057 |
| Argentina / Liga Profesional | 254 | 1.0308 | 0.6213 | 0.2028 | 0.1339 |
| Premier League | 194 | 1.0555 | 0.6395 | 0.2082 | 0.0806 |

World Cup 2026 的数值看起来最好，但只有 93 场，且该历史研究集不是盲测；同时仍有 8 场正赛因口径不足未纳入研究可用赛果。该分组只能作为后续正式前瞻采集的重点观察对象，不能作为当前预测依据。

## 7. 后续建议

- 保留当前历史研究结论：去水 1x2 市场概率值得继续作为阶段 4 市场基线的第一优先级。
- 不在历史研究层推进复杂增强模型；继续等待正式前瞻样本达到按切点 500 场严格门禁。
- 后续若补充授权历史数据，应优先补齐多赛事 opening、亚盘和大小球覆盖，而不是只增加 closing 1x2。
- 研究报告后续可增加可靠性分箱和按赔率强弱分桶，但仍需保持 `research_only` 隔离。
