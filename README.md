# Football Cups

基于欧赔、亚洲让球和大小球市场数据的足球赛前概率预测、临场监控与赛后复盘项目。预测口径为常规时间 90 分钟及补时，不包含加时赛和点球大战，也不提供投注金额、组合或收益建议。

## 当前状态

项目处于“阶段 1：数据源候选与验收”。当前需要先确认数据源的合法使用权、目标赛事与盘口覆盖、历史变盘完整度、稳定 ID 和时间语义；数据源通过验收前不进入正式开发。

当前进度及唯一下一步见 `docs/project-status.md`。

## 文档入口

- `AGENTS.md`：项目总控 Agent 工作规则和恢复协议
- `docs/product-plan.md`：产品方案与产品边界
- `docs/execution-plan.md`：长期分阶段执行计划
- `docs/project-status.md`：当前状态、阻塞项和人工待办
- `docs/decision-log.md`：已接受与暂定决策
- `docs/data-source-evaluation.md`：候选数据源证据、评分和验收记录
- `docs/research-data-acquisition-plan.md`：2025 年至当前时间的免费研究数据获取步骤

中断较长时间后，按照 `AGENTS.md` 的启动顺序恢复工作，不依赖聊天历史。

## 安全提示

真实 API Key、密码和数据库连接串只保存在未跟踪的 `.env` 或云端密钥服务中。原始数据、数据库、备份和模型产物不得提交到 Git。
