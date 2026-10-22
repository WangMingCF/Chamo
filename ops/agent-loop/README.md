# 在群晖 Docker 容器里自循环迭代

开发 Agent（例如 DeepSeek 的 harness）跑在群晖 NAS 上的 Docker 容器里，**只有开发能力**：读写代码、用 git 和 gh。测试、评审和隐私把关全部放在 GitHub 上完成。容器里不需要 Python 依赖、LibreOffice 或 macOS。

## 结构

```
群晖容器（开发 Agent）                     GitHub
  目标模式提示词 prompts/goal.md
  写代码 → git commit（身份固定 Chamo）
  git push ──[pre-push 隐私扫描]──────────▶ 分支
  scripts/ci_remote_test.sh ─────────────▶ test-matrix：任意平台、可重复运行
  gh pr create ──────────────────────────▶ PR
                                            ├─ cross-platform → all checks（三端测试、打包、Swift）
                                            └─ agent-review   → 另一个模型评审 + 服务端隐私/身份检查
  读评审意见和 CI 日志，修改后再推送          main 分支保护：all checks + agent-review 都通过才能合并
```

| 文件 | 作用 |
| --- | --- |
| `prompts/goal.md` | 目标模式提示词，交给 harness |
| `prompts/round.md` | 单轮提示词（只在用 `run_loop.sh` 按轮驱动时使用） |
| `prompts/review.md` | 评审标准，`agent-review` 工作流使用 |
| `container_init.sh` | 容器启动时运行：固定 git 身份、挂上推送钩子、检查工具与令牌、克隆仓库 |
| `hooks/pre-push` | 推送前隐私扫描 |
| `privacy_scan.py` | 隐私扫描：关键词只存哈希，命中只报编号 |
| `review_llm.py` | 评审工作流调用模型 API 的客户端（OpenAI 兼容接口，默认 DeepSeek） |
| `run_loop.sh`、`lib.sh` | 可选的按轮驱动：总开关、每日上限、连续失败熔断、单轮超时、往状态 Issue 发报告 |
| `approve_pr.sh` | 维护者在自己电脑上运行：亲自审核后放行被拦下的 PR |
| `config/chamo-agent.env.example` | 容器环境变量模板 |

## 安装步骤

### 1. 在你的 Mac 上生成隐私关键词（真实值只在 Mac 上出现）

```bash
python3 ops/agent-loop/privacy_scan.py hash > private-keywords.txt
```

逐行输入真实姓名（中文和拼音的各种写法）、手机号、个人邮箱、GitHub 用户名，按 Ctrl-D 结束。然后在文件末尾追加两行允许规则，让用户名可以以仓库所有者身份出现（仓库链接、`gh -R` 的仓库名、bundle ID）。把 `<用户名>` 换成你的 GitHub 用户名（匹配不区分大小写）：

```
allow:<用户名>/chamo
allow:io\.github\.<用户名>\.chamo
```

本说明里不直接写出这两行，是因为第二行的反斜杠写法本身会被扫描器当作命中。

哈希只能防止意外泄露：手机号这类短值可以被暴力还原，所以这个文件仍要当作机密保管。

### 2. GitHub 上（需要你本人操作）

1. **Secrets 和 Variables**（仓库 Settings → Secrets and variables → Actions）：
   - Secret `PRIVACY_KEYWORDS`：第 1 步生成的文件的全部内容。没有它，评审一律拒绝放行。
   - Secret `REVIEW_API_KEY`：评审用的模型 API key。
   - Variable `REVIEW_MODEL`：评审用的模型名。建议与开发 Agent 不同，至少用不同的模式（比如推理模型）。
   - Variable `REVIEW_API_BASE`（可选）：默认 `https://api.deepseek.com`。
2. **标签**：`agent-stop`（总开关）和 `accepted`（只有带这个标签的 Issue 才会被当作任务）。再建一个状态 Issue，记下编号。
3. **开发令牌**（细粒度，只授权这一个仓库）：Contents 读写、Pull requests 读写、Issues 读写、Actions 读写（用来触发 `test-matrix`）。
   - **不要**给 Commit statuses：这样 Agent 无法自己设置 `agent-review`。
   - **不要**给 Workflows：这样 Agent 无法修改 CI 和评审工作流。
   - **不要**给 Administration。
4. **合并 PR 后设置 main 分支保护**。`enforce_admins` 打开后你自己也不能绕过；被拦下的 PR 用 `approve_pr.sh` 放行：

```bash
gh api -X PUT repos/WangMingCF/chamo/branches/main/protection --input - <<'EOF'
{
  "required_status_checks": {"strict": true, "contexts": ["all checks", "agent-review"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

### 3. 群晖上

1. 在 NAS 上建三个文件夹并挂进容器：
   - `rails`：把 `ops/agent-loop/` 整个复制进去，**只读**挂载，例如挂到 `/rails`。Agent 改不了自己的规则；规则更新时，由你从 main 重新复制。
   - `config`：放 `private-keywords.txt`，只读挂载到 `/config`。如果使用 `run_loop.sh`，再单独可写挂载一个子目录 `/config/state`。
   - `work`：Agent 的工作目录，可写，挂到 `/work`。
2. 容器里需要 `git`、`python3`（只用标准库）和 `gh`。`gh` 是单个静态二进制文件，镜像里没有的话，可以放进 `rails/bin` 后加入 PATH。
3. 按 `config/chamo-agent.env.example` 在 Container Manager 里设置环境变量（或用 env_file）。令牌只填在这里。
4. 容器启动时运行一次：

```bash
REPO=WangMingCF/chamo WORKDIR=/work/chamo bash /rails/container_init.sh
```

5. 把 `/rails/prompts/goal.md` 的内容作为 harness 的目标模式提示词，把 `{{REPO}}`、`{{WORKDIR}}`、`{{RELEASE_MODE}}` 换成实际值。工作目录设为 `/work/chamo`。
6. 可选：如果 harness 能"从标准输入读提示词、跑完一轮就退出"，可以用 `/rails/run_loop.sh` 按轮驱动，获得每日上限、熔断和状态汇报；否则只用 harness 自带的目标模式。

## 刹车

| 方式 | 效果 |
| --- | --- |
| 开一个带 `agent-stop` 标签的 Issue | 提示词要求 Agent 立刻停下；`run_loop.sh` 也会检查。手机上就能操作 |
| 在 NAS 的 `config` 文件夹里放一个 `STOP` 文件 | `run_loop.sh` 停止 |
| 在群晖上停止容器 | 立即停止 |
| 在 DeepSeek 控制台设置消费上限 | 控制花费（这套工具无法统计 API 花费） |
| `run_loop.sh` 的每日上限、熔断、超时 | 见 env 模板 |

## 已知局限（如实说明）

- **pre-push 钩子挡不住故意绕过**：Agent 可以用 `git push --no-verify` 跳过它。提示词禁止这样做。另外 `agent-review` 会在服务端再查一遍 PR 的新增内容、说明和提交身份，有命中就拦下，不让合并；但推送到分支的那一刻内容已经公开了。
- **所有令牌都属于同一个 GitHub 账号**：GitHub 不允许账号批准自己的 PR，所以评审结论用提交状态 `agent-review` 表达，不是 GitHub 的 Approve。
- **评审工作流要先合并到 main 才会运行**：它用 `pull_request_target`，总是运行 main 上的版本，所以引入它的这个 PR 本身不会被它评审。
- **macOS 只验证到编译与单测**：界面点击、打包、安装仍要在 Mac 上验收（见 `docs/CI_TESTING.md` 第 7 节）。
- **状态报告会公开**：报告发布前会扫描隐私关键词和令牌；harness 日志只留在 NAS 上，从不发布。
