# 项目当前状态

> 更新日期：2026-07-15
> 当前阶段：阶段 1 - 数据源候选与验收
> 阶段状态：免费研究数据三市场与单页批量验证完成，正式数据源验收尚未开始
> 下一次更新触发：补齐跨时段/跨赛事 URL、执行跨时段历史变盘验证、获得首批候选数据源信息或本页任一状态发生变化

本文件是当前进度的唯一入口。产品边界以 `docs/product-plan.md` 为准，阶段要求以 `docs/execution-plan.md` 为准，历史选择以 `docs/decision-log.md` 为准。免费研究数据获取步骤见 `docs/research-data-acquisition-plan.md`。

## 1. 已完成

- [x] 初始化 Git 仓库和基础忽略规则。
- [x] 完成产品方案 V1.1。
- [x] 完成长期执行计划 V1.2。
- [x] 建立项目总控 Agent 契约、恢复顺序和阶段门禁。
- [x] 建立项目状态、决策日志和数据源评估模板。
- [x] 固化 2025 年至当前时间的免费研究数据获取计划。
- [x] 创建 `data/research` 本地研究目录结构。
- [x] 下载 Football-Data 五大联赛 2024/25 和 2025/26 共 10 个 CSV。
- [x] 生成 Football-Data manifest 和范围统计报告。
- [x] 安装并验证 `oddsharvester==0.4.0` CLI。
- [x] 通过本机 Chrome junction 绕过 Playwright Chromium 下载阻塞。
- [x] 使用 CentroQuote 区域镜像完成 OddsHarvester 单场 1X2 JSON 样本。
- [x] 完成 CentroQuote 单场 1X2、大小球 2.5、亚洲让球 0 三市场验证。
- [x] 完成单场 1X2 历史变盘抓取，确认 OddsHarvester 将 2025 年错误写成当前年份 2026。
- [x] 建立可重复的 CentroQuote 本地补丁脚本与研究派生转换脚本。
- [x] 完成英超 2024/25 单页 50 场批量验证：成功 23，失败 27，成功率 46%。

## 2. 当前目标

收集足够证据，筛选出 2 至 4 个可合法保存、分析和建模的授权盘口 API 候选，并准备进入试用验证。同时可按 `docs/research-data-acquisition-plan.md` 获取个人学习研究样本，用于字段和覆盖验证。当前不开发正式数据库、模型、采集器或 Web 产品。

## 3. 当前阻塞项

- 尚无候选授权盘口 API 信息。
- 尚无独立公开赔率展示源。
- 尚无独立赛果校验源。
- 数据使用授权、历史变盘完整度和时间语义均未验证。
- CentroQuote 英超单页批量成功率仅 46%，不能作为完整历史回填主源。
- 尚缺 2025 年 8 至 12 月及 2026 年 K1/欧战/世界杯比赛 URL，跨时段和跨赛事稳定性未验证。

以上阻塞不会妨碍候选调研，但会阻止项目进入阶段 2。

## 4. 人工待办

### 4.1 授权 API 候选

请查找 2 至 4 个候选，每个候选填写到 `docs/data-source-evaluation.md`：

- [ ] 官网和产品名称
- [ ] API 文档地址
- [ ] 套餐价格、试用条件和退款规则
- [ ] 请求频率和月度配额
- [ ] 五大联赛、欧战、世界杯及预选赛、K1 覆盖情况
- [ ] 欧赔、亚洲让球、大小球覆盖情况
- [ ] 是否提供带时间戳的完整历史变盘
- [ ] 比赛、赛事、球队和公司 ID 是否稳定
- [ ] 时间字段定义、格式和时区
- [ ] 是否明确允许本地保存、分析、建模和长期使用
- [ ] 服务条款和数据许可链接及核验日期
- [ ] 试用申请和技术支持方式

不要购买长期套餐，也不要把 API Key 写入文档或聊天。

### 4.2 独立校验源

- [ ] 提供 1 个公开赔率展示源及网址，仅用于人工盘口对比。
- [ ] 提供 1 个独立赛果源及网址，用于比赛身份和常规时间赛果校验。
- [ ] 记录两个来源的访问限制和核验日期。

公开网页在条款未明确允许前不得自动化采集。

### 4.3 免费研究数据小样本

按 `docs/research-data-acquisition-plan.md` 执行前，需要补齐：

- [x] 取得 2025 年 1 至 5 月样本 URL：Liverpool vs Tottenham，2025-04-27。
- [ ] 提供 2025 年 8 至 12 月 OddsPortal 或 CentroQuote 比赛 URL。
- [ ] 提供 2026 年 K1/欧战/世界杯 OddsPortal 或 CentroQuote 比赛 URL。
- [ ] 核验世界杯各赛区预选赛页面，并记录可访问比赛 URL。
- [x] 确认研究数据只用于个人学习，不改变正式授权数据源验收门禁。

## 5. Agent 下一步

没有人工 URL 时，Agent 的下一步是：

1. 等待人工提供 2025 年 8 至 12 月及 2026 年 K1/欧战/世界杯比赛 URL。
2. 对每个新时段先运行单场三市场无历史样本，再运行一类市场的 `--odds-history`。
3. 使用研究派生脚本转换 American Odds，并对错误年份时间戳修正或隔离。
4. 不扩大 CentroQuote 完整批量回填；其 46% 单页成功率只适合作为辅助研究样本。

收到正式候选 API 信息后，Agent 的下一步是：

1. 将每条信息及证据链接录入数据源评估表。
2. 标记缺失证据，不根据营销描述推断授权或技术能力。
3. 检查硬性淘汰项并按 100 分规则初评。
4. 输出首选试用候选、备选候选和待人工确认问题。
5. 只有候选初评达到 80 分且无硬性淘汰项，才准备试用验证；此时仍不代表数据源最终验收通过。

## 6. 当前风险

| 风险 | 状态 | 处理方式 |
| --- | --- | --- |
| 供应商只提供当前赔率，没有历史变盘 | 未评估 | 核对文档和样本；必要时评估常驻采集可行性 |
| 数据许可不允许长期保存或建模 | 未评估 | 以条款和供应商书面答复为证据，触发时直接淘汰 |
| K1 或杯赛公司覆盖不足 | 未评估 | 分赛事统计覆盖率，不用五大联赛结果代替 |
| 时间字段含义不明确导致数据泄漏 | 未评估 | 核对字段文档和样本，无法确认时不得作为主数据源 |
| 暂定预算不足 | 未评估 | 获得真实报价后重新决策，不预先放宽数据要求 |
| 历史回抓数据被误用为严格回测数据 | 已识别 | 回抓数据标记 `backfill=true`、`strict_backtest_eligible=false` |
| OddsHarvester 历史变盘年份错误 | 已复现并隔离 | 单场 12 个时间戳由 2026 修正至 2025 UTC；无法满足开球前 180 天约束时进入隔离区 |
| Playwright Chromium 下载不稳定 | 已绕过 | 已用 junction 将 Playwright 预期 Chromium 路径指向本机 Chrome |
| OddsPortal 主站无历史赔率行 | 已发生 | 主站提示 selected bookmakers 无可用赔率；改用 `https://www.centroquote.it` |
| CentroQuote 单场存在 fragment mismatch | 部分缓解 | 意大利月份解析后部分场次可安全使用 DOM-first；无法确认目标场次时直接丢弃 |
| CentroQuote 批量覆盖不足 | 已确认 | 英超单页 23/50 成功（46%）；不得作为完整历史回填主源 |
| CentroQuote 输出为 Money Line | 已处理 | 原始层保留；派生脚本按 American Odds 公式生成十进制字段 |

## 7. 本地研究产物

| 产物 | 路径 | 状态 |
| --- | --- | --- |
| Football-Data 原始 CSV | `data/research/raw/football-data/20260715T063843Z/` | 已生成，Git 忽略 |
| Football-Data manifest | `data/research/manifests/20260715T063843Z-football-data-manifest.csv` | 已生成，Git 忽略 |
| Football-Data 摘要 | `data/research/reports/20260715T063843Z-football-data-summary.md` | 已生成，Git 忽略 |
| OddsHarvester 研究环境 | `data/research/runtime/venv-py314/` | 已安装包，Git 忽略 |
| OddsHarvester 1X2 单场样本 | `data/research/raw/oddsharvester/20260715T072931Z/` | 已生成，Git 忽略 |
| OddsHarvester 三市场单场样本 | `data/research/raw/oddsharvester/20260715T074243Z/` 等 | 1X2、OU 2.5、AH 0 均成功，Git 忽略 |
| OddsHarvester 历史变盘样本 | `data/research/raw/oddsharvester/20260715T074617Z/` | 1 场、2 家公司，Git 忽略 |
| OddsHarvester 英超单页批量 | `data/research/raw/oddsharvester/20260715T075122Z/` | 23/50 成功，Git 忽略 |
| OddsHarvester 派生样本 | `data/research/derived/oddsharvester/` | 十进制赔率与修正时间，Git 忽略 |
| OddsHarvester 页面诊断 | `data/research/reports/oddsharvester-diagnostics/` | 已生成，Git 忽略 |

## 8. 恢复工作时首先执行

1. 按根目录 `AGENTS.md` 的顺序阅读权威文档。
2. 确认本页更新时间、当前阶段和阻塞项是否仍有效。
3. 检查 `docs/data-source-evaluation.md` 是否已有新的候选证据。
4. 检查 Git 是否存在未提交或未推送内容。
5. 检查 `docs/research-data-acquisition-plan.md` 的研究小样本是否已执行。
6. 从第 4 节第一个未完成的人工待办或第 5 节 Agent 下一步继续，不重复已完成调研，不越过阶段门禁。
