# CI 工具与独立审查

- `privacy_scan.py` 扫描推送提交、文本、文件及发布资产；私有关键词哈希缺失时拒绝放行。提交身份为 `Chamo <dev@chamo.invalid>`。
- `hooks/pre-push` 可安装到可信工具目录，推送前运行隐私扫描。
- `dsh_review.py` 验证独立 DSH 审查报告，写入 GitHub 必需状态 `agent-review`。
- 准备入口在签名、生成投递文件或上传前扫描原始 JSON 和解码后的完整公开正文；工作流在进程内读取事件文件，报告和签名不放入会被 runner 提前打印的步骤环境变量。
- `test_*.py` 验证隐私和评审边界。这些工具不负责调度开发会话。

## 完整代码审查

长期执行端为 NAS-DSH、Mac-DSH、Mac-Claude；每项任务的实施端和独立审查端必须不同。Mac-Codex 仅负责此次迁移。每项任务、PRD 和报告标明设备名；Git 提交不使用设备所有者的个人身份。

1. 固定 PR 的完整 head/base SHA，在独立目录读取 PRD、所有变动文件的全文、调用方、依赖、相关测试与真实 CI 日志。删除的文件也要核实原用途和引用；不能只检查 diff 或复述作者说明。
2. 实际运行必要的正常与失败场景，记录命令、结果和证据，确认断言覆盖验收要求。涉及完整代码上下文，不意味着每次重复机械扫描全仓。另持续选择未审查的高风险存量模块，发现缺陷登记到需求队列。
3. 形成以下 JSON 报告，`reviewed_files` 至少包含全部变动文件，并补充实际检查的调用链文件。`source_inspection` 和 `test_evidence` 写具体范围和证据；未验证内容明确标出。存在阻塞发现时必须 `REQUEST_CHANGES`。
4. 仅独立审查者在自己的设备上对报告原始 UTF-8 字节作 Ed25519 签名。私钥只留所属执行端的私有目录，不跨端同步或进入 Git/日志；公钥在 `reviewers/<执行端>.pub`。实现者不得替自己签独立批准。
5. 将原始报告与签名分别 Base64 编码，通过 `agent-review.yml` 的 `workflow_dispatch` 输入 `pr`、`report`、`signature` 提交。工作流从可信默认分支加载验证器，不执行候选代码。PR 更新只请求审查并保持 pending；有效独立报告才能通过。
6. head 或 base 改变就更新审查证据并重新提交。合并前同步最新 main、重新验证最终组合的 `all checks` 与 `agent-review`，绑定已验证 head 合并。许可证变更仍需维护者处理。

```json
{
  "schema": 1,
  "pr": 123,
  "head_sha": "<40 位 head SHA>",
  "base_sha": "<40 位 base SHA>",
  "implementation_device": "NAS-DSH",
  "reviewer_device": "Mac-DSH",
  "reviewer_session": "<审查会话 ID>",
  "reviewed_files": ["engine/example.py", "tests/test_example.py"],
  "summary": "<结论与未验证范围>",
  "source_inspection": "<完整源码、调用链及失败路径的检查证据>",
  "test_evidence": "<实际命令、通过/失败/跳过数量、日志或 CI 链接>",
  "verdict": "APPROVE",
  "blocking_findings": []
}
```

签名验证只证明报告来源与版本，不证明推理正确，不能代替实际审查或测试。持有仓库管理权限的账号仍可能更改规则；不能把同一管理员账号下的协作约定称为绝对权限隔离。初次迁移评审入口也必须有独立审查证据和必需 CI，禁止使用无证据的直接放行脚本。

首次部署时，独立审查者可对已审 PR44 分支版本使用 `gh workflow run agent-review.yml --ref feat/project-structure`，提交其签名报告；本机凭据只发起 dispatch，验证器使用工作流限定的 Actions token。该路径仍是经过独立复核的候选代码，不冒称已经部署的主干信任根。默认分支缺少验证器时，工作流只在 PR44、指定分支、2026-10-24 00:00 UTC 前且报告绑定运行 SHA 时加载固定已审快照，逐文件核对哈希后才运行原验证器。合并后默认分支已有验证器，快照回退路径停止使用；必须另对一个有有效独立报告的开放 PR 完成真实 main dispatch，确认旧批准入口已移除并保留原分支保护，才能宣布迁移完成。任何步骤失败均保持门禁，不手写成功状态。

测试标准见 [CI_TESTING.md](../../docs/CI_TESTING.md)。
