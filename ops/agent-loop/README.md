# 在 Linux VPS 上自循环迭代

这套脚本让一个 Agent 在 Linux 服务器上按 `docs/ITERATION_PLAN.md` 一轮一轮地开发 Chamo，另一个身份做独立评审，GitHub Actions 负责三端测试（见 `docs/CI_TESTING.md`）。它不依赖具体的 Agent 工具：只要你的 harness 能"从标准输入读提示词、在当前目录干活、干完退出"，就能接入。

## 结构

```
chamo-agent（开发）                         chamo-review（评审，另一个 Unix 用户）
  run_loop.sh 每轮：                          review_prs.sh 每 10 分钟：
    总开关 → 每日上限 → 连续失败熔断            总开关 → 只看本仓库分支的 PR
    → HARNESS_CMD < goal.md + round.md         → 受保护路径直接拦下，等维护者
    → 把本轮报告发到状态 Issue                  → diff 过大 / 隐私命中 拦下
                                               → REVIEW_CMD 给出结论
                                               → PR 评论 + 提交状态 agent-review
GitHub：main 分支保护，必需检查 = CI 全部作业 + agent-review
```

| 文件 | 作用 |
| --- | --- |
| `run_loop.sh` | 开发循环（`--once` 只跑一轮，`DRY_RUN=1` 不访问 GitHub） |
| `review_prs.sh` | 独立评审，设置 `agent-review` 状态 |
| `approve_pr.sh` | 维护者在自己电脑上运行：亲自审核后放行被拦下的 PR |
| `privacy_scan.py` | 隐私扫描：关键词只存哈希，命中只报编号 |
| `hooks/pre-push` | 推送前扫描，经全局 `core.hooksPath` 生效 |
| `lib.sh` | 公共函数：配置加载、总开关、发布前扫描 |
| `prompts/` | 目标、单轮、评审三份提示词 |
| `config/` | `loop.conf`、两份 `.env` 的模板 |
| `systemd/` | 开发服务、评审服务与定时器 |
| `setup_vps.sh` | 初始化服务器（Debian/Ubuntu，root 运行，可重复执行） |

## 安装步骤

### 1. 在你的 Mac 上准备（不要在 VPS 上输入真实信息）

生成隐私关键词的哈希文件。真实值只在你的 Mac 上出现：

```bash
python3 ops/agent-loop/privacy_scan.py hash > private-keywords.txt
```

逐行输入真实姓名（中文和拼音的各种写法）、手机号、个人邮箱、GitHub 用户名，按 Ctrl-D 结束。然后在文件末尾追加两行允许规则，让用户名可以以仓库所有者身份出现（仓库链接、`gh -R` 的仓库名、bundle ID）。把 `<用户名>` 换成你的 GitHub 用户名（匹配不区分大小写）：

```
allow:<用户名>/chamo
allow:io\.github\.<用户名>\.chamo
```

本说明里不直接写出这两行，是因为第二行的反斜杠写法本身会被扫描器当作命中。

注意：哈希只能防止意外泄露。手机号这类短值可以被暴力还原，所以这个文件仍然要当成机密保管。

### 2. GitHub 上（需要你本人操作）

1. 建一个状态 Issue（例如"自循环状态"），记下编号。
2. 新建标签 `agent-stop`（总开关）和 `accepted`（只有带这个标签的 Issue 才会被 Agent 当作任务）。
3. 建两个细粒度令牌，都只授权 `WangMingCF/chamo` 这一个仓库：
   - 开发令牌：Contents 读写、Pull requests 读写、Issues 读写、Actions 只读。**不要**给 Commit statuses 和 Administration。
   - 评审令牌：Commit statuses 读写、Pull requests 读写、Issues 读写、Contents 只读。
4. 设置 main 分支保护。`enforce_admins` 打开后，你自己也不能绕过；被拦下的 PR 用 `approve_pr.sh` 放行：

```bash
gh api -X PUT repos/WangMingCF/chamo/branches/main/protection --input - <<'EOF'
{
  "required_status_checks": {"strict": true, "contexts": [
    "engine (ubuntu-latest)", "engine (windows-latest)", "engine (macos-latest)",
    "linux package", "windows package", "swift (macos-15)", "ops scripts", "agent-review"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

`unit` 工作流要先在 main 上跑过一次，`swift (macos-15)` 和 `ops scripts` 这两个检查名才会出现在 GitHub 里。

### 3. 在 VPS 上

```bash
git clone https://github.com/WangMingCF/chamo.git /root/chamo-src
sudo bash /root/chamo-src/ops/agent-loop/setup_vps.sh
```

接着：

1. 编辑 `/etc/chamo-agent/loop.conf`，至少填 `STATUS_ISSUE`。
2. 填写两份 `.env`：开发用户和评审用户各用各的令牌。`HARNESS_CMD` 和 `REVIEW_CMD` 填你的 harness 的真实命令。
3. 把第 1 步生成的 `private-keywords.txt` 复制到两个用户的 `~/.config/chamo-agent/` 下，权限设为 600。
4. 为两个用户分别安装并登录你的 harness。
5. 试跑与启动：

```bash
sudo -u chamo-agent DRY_RUN=1 /opt/chamo-agent/run_loop.sh --once
sudo -u chamo-review DRY_RUN=1 /opt/chamo-agent/review_prs.sh
systemctl start chamo-review.timer chamo-agent.service
journalctl -u chamo-agent -f
```

## 刹车

| 方式 | 效果 |
| --- | --- |
| `touch /etc/chamo-agent/STOP` | 两个循环都停（只有 root 能创建这个文件） |
| 开一个带 `agent-stop` 标签的 Issue | 同上，可以在手机上操作 |
| `MAX_ROUNDS_PER_DAY` | 每天最多跑几轮 |
| `MAX_CONSECUTIVE_FAILURES` | 连续失败达到次数后暂停，并在状态 Issue 里通知；删除 `~/.local/state/chamo-agent/failures` 后恢复 |
| `ROUND_TIMEOUT` | 单轮超时后强制结束 |
| 模型平台的消费上限 | 在 DeepSeek 控制台设置每日或每月额度（这套脚本无法统计 API 花费） |

## 已知局限（如实说明）

- **pre-push 钩子挡不住故意绕过**：开发 Agent 和钩子是同一个 Unix 用户，它可以用 `git push --no-verify` 或改仓库级 `core.hooksPath` 来跳过。提示词禁止这样做，评审还会对 PR 再扫描一遍；但推送到分支的那一刻内容就已经公开了。想彻底挡住，需要在服务端做检查（例如 GitHub 的 push ruleset），目前没有实现。
- **所有令牌都属于同一个 GitHub 账号**：GitHub 不允许账号批准自己的 PR，所以评审结论用提交状态 `agent-review` 表达，不是 GitHub 的 Approve。开发令牌没有 Commit statuses 权限，所以不能自己设置这个状态。
- **macOS 只能由 CI 验证编译与单测**：界面点击、打包、安装仍要在 Mac 上验收（见 `docs/CI_TESTING.md` 第 4 节）。
- **状态报告会公开**：报告发布前会扫描隐私关键词和令牌；harness 日志只留在服务器上，从不发布。
