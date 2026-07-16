# Football Cups

基于 500 足球竞彩及其多公司盘口数据的长期前瞻采集、赛前概率预测、临场监控与赛后复盘项目。采集覆盖竞彩页面出现的全部比赛；模型只使用满足时间、盘口和赛果资格的数据。

预测口径为常规时间 90 分钟及补时，不包含加时赛和点球大战。项目不提供投注金额、组合、成本或收益建议。

## 当前状态

项目处于“阶段 1：500 竞彩全赛事发现与技术验收”。当前允许运行验证采集器；7 天技术验收前不开发正式分析数据库、模型或 Web 产品，30 天稳定性验收前不将采集器标记为长期生产可用。

当前进度及唯一下一步见 `docs/project-status.md`。

## 文档入口

- `AGENTS.md`：Agent 工作规则、恢复顺序和阶段门禁
- `docs/product-plan.md`：产品边界与长期目标
- `docs/execution-plan.md`：分阶段开发和验收计划
- `docs/project-status.md`：当前状态、风险和唯一下一步
- `docs/decision-log.md`：已接受与暂定决策
- `docs/data-source-evaluation.md`：500 技术验收记录
- `docs/500-collector-spec.md`：采集器数据契约和行为规范
- `docs/collector-runbook.md`：Windows 运行、备份和恢复手册
- `docs/research-data-acquisition-plan.md`：历史免费研究路线及已有结果

## 采集器

安装开发环境后可运行：

```powershell
py -3.14 -m pip install -e .[dev]
football-cups-collector init --workspace .
football-cups-collector discover --workspace .
football-cups-collector run-once --workspace .
```

所有运行数据写入被 Git 忽略的 `data/500/`。长时间中断后先按 `AGENTS.md` 恢复，不依赖聊天历史。

## 安全提示

真实密钥、密码和连接串只保存在未跟踪的 `.env` 或系统密钥服务中。原始数据、数据库、备份、日志和模型产物不得提交 Git。
