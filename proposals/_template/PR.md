# PR：<标题>

## 登记与去重
- ID / feature_key：与 [proposal.json](<proposal.json>) 一致。
- 目标 / 非目标 / 范围：[PRD](<PRD.md>)。
- 调研：至少两路独立分析的结论、来源、分歧与取舍（只写决策摘要，不提交研究日志）。
- 已检索：最新 main SHA、open/closed PR、Issue、提案目录、远端 registry；记录时间和链接，不能只搜标题。
- 所有者 / 执行平台 / 分支 / 依赖：见元数据。
- 提案 PR 链接 / 实现 PR 链接：待建立。
- 远端认领：待成功；check 不代表持锁；原始 token 只留本机忽略目录。

## 验收与证据
| AC | 实际命令或人工操作 | SHA / 平台 / CI链接 | 结果 | 未覆盖与下一步 |
| --- | --- | --- | --- | --- |
| AC-1 | 待补 | 待补 | 未验证 | 待补 |

## Review 与交接
- 独立 reviewer / 基于的 head SHA / 发现问题 / 修复与回归：待补。
- 覆盖率：基线→当前（同平台同口径），新增关键行为测试与覆盖缺口；不以数字代替功能验收。
- 当前状态 / 下一步 / 阻塞 / 最新同步 main SHA：待补。
- 回滚方案：最小 revert PR、用户数据不回滚/不删；新增能力必要时默认关闭。
- 合并 SHA / 主干验证 / 发布版本与校验 / 认领释放 / 分支清理：待执行。

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
