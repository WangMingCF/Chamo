# 多端功能单测方案（GitHub Actions）

本文件规定 Chamo 的功能测试在哪里跑、怎么判定、新功能必须补什么。开发 Agent、评审和维护者都以它为准；改动测试体系时同步更新本文件。

**前提：开发 Agent 运行在只有开发能力的容器里，本地不跑测试。GitHub Actions 就是唯一的测试环境。** 本地"通过"不算数。CI 全绿只说明下文"已覆盖"的部分没有回退；没覆盖的部分必须按第 7 节另外验收。

## 1. 三类工作流

| 工作流 | 何时运行 | 作用 |
| --- | --- | --- |
| `cross-platform` | 每个 PR、每次推送到 main | 合并门禁：三端引擎测试、打包后转换、Swift 编译与单测、自循环工具测试；汇总为一项检查 `all checks` |
| `agent-review` | PR 打开或更新时 | 独立评审：用另一个模型审 diff，并做服务端隐私与提交身份检查，结果写成提交状态 `agent-review` |
| `test-matrix` | 手动触发（含 `scripts/ci_remote_test.sh`）；每晚一次 | 按需在任意平台跑任意测试、可重复运行找不稳定测试；夜间在更多系统版本和 arm64 上把全套跑 3 遍 |

main 分支保护只需要两项必需检查：`all checks` 和 `agent-review`。

## 2. 合并门禁 `cross-platform` 的作业

| 作业名 | 运行环境 | 验证内容 |
| --- | --- | --- |
| `engine (ubuntu-latest)` | Linux x64 | 引擎全部 pytest；装真实 LibreOffice，Office 转换测试必须实际运行并通过 |
| `engine (windows-latest)` | Windows x64 | 同上；覆盖 Windows 独有问题（MoveFileExW、保留文件名、winerror、控制台编码） |
| `engine (macos-latest)` | macOS arm64 | 同上；覆盖 macOS 的原子发布（renamex_np）与 HEIC 等平台路径 |
| `linux package` | Linux x64 | 构建 `.deb`，安装后用安装出来的 `chamo` 做真实转换 |
| `windows package` | Windows x64 | 构建便携 zip，解压后转换，并驱动打包后的本机界面 |
| `swift (macos-15)` | macOS arm64 | 编译 macOS 应用（正式版与 `CHAMO_DEBUG` 版）和 HEIC 辅助程序；运行 Swift 单测（`tests/*Tests.swift`） |
| `ui (ubuntu-latest / windows-latest / macos-15)` | 三端 | 界面自动化：Playwright 驱动 `chamo ui` 真实页面（三端），macOS 上再用调试钩子剧本驱动原生应用；断言落到输出文件上，固定画面与基准截图比对。约定见 `uitests/README.md` |
| `ops scripts` | Linux x64 | 自循环工具：隐私扫描、评审 API 客户端测试、shellcheck |
| `all checks` | Linux x64 | 只有上面每个作业都成功才通过；任何一个被跳过或取消也算失败 |

三个平台的引擎作业共用 `.github/actions/setup-engine`：Python 3.12、`third_party/requirements.lock` 锁定的依赖（带 pip 缓存）和真实的 LibreOffice。`test-matrix` 用的也是同一个环境。

## 3. 判定规则：`scripts/ci_report.py`

每个引擎作业都把 pytest 结果写成 JUnit XML，再交给 `scripts/ci_report.py` 判定，判定结果写进作业摘要（Actions 页面上的 Summary）。以下任何一种情况都会让作业失败：

- **失败**：测试失败或出错。
- **不稳定**：重复运行时，同一个测试有时失败、有时通过。
- **未登记的跳过**：跳过不等于通过。每个平台允许的跳过登记在 `tests/ci_allowed_skips.json`，按文件和跳过原因完全匹配（`reason`），并写明理由（`why`）；原因里带实测数值的，才用前缀匹配（`reason_prefix`）。登记时以源码中的原因为准：pytest 日志末尾的跳过汇总可能被截断。
- **空结果**：一个测试都没运行。

新增一条合理的跳过时，必须同时有在该平台真正运行、覆盖同一行为的测试，并在 PR 里说明。

## 4. 开发过程中怎么验证（给容器里的 Agent）

1. 推送分支。runner 测的是 GitHub 上的分支，不是本地工作区。
2. 定向验证，例如在 Windows 上把某个测试跑 3 遍：

   ```sh
   scripts/ci_remote_test.sh --platforms windows --repeat 3 tests/test_page_resume.py
   scripts/ci_remote_test.sh --platforms core --filter "cancel" tests        # 三端
   scripts/ci_remote_test.sh --platforms extended tests                       # 更多系统版本与 arm64
   ```

   脚本会触发 `test-matrix`、等待结束、打印各平台汇总，退出码就是结论。`test-matrix.yml` 要先合并到 main 才能被手动触发；之后用 `--ref` 可以测任何分支。
3. 开 PR 后看 `all checks` 与 `agent-review`。失败时用 `gh run view <id> --log-failed` 看日志，或用 `gh run download <id>` 下载 JUnit XML 与汇总。
4. 令牌需要 Actions 读写权限才能触发运行（见 `ops/agent-loop/README.md`）。

## 5. 新功能必须补的测试

每个新增或改动的功能，在同一个 PR 里补齐：

1. **引擎功能测试**（`tests/test_*.py`，三端都会运行）：
   - 成功路径：用合成样例在临时目录里现场生成输入（参考 `tests/conftest.py` 和 `scripts/create_fixtures.py`），不要提交大的二进制样例。
   - 断言要落到结果本身：页数、像素或渲染比对、文本抽取、哈希、清单内容，而不只是"没报错"或"文件存在"。
   - 失败路径各至少一条：加密、损坏、同名冲突（不覆盖）、取消、中断后续跑；涉及磁盘空间的加低磁盘用例。
   - 原件不变：处理前后对原件做哈希比对。
2. **平台差异**：只在某个平台成立的行为用 `pytest.mark.skipif` 并写明原因，登记到 `tests/ci_allowed_skips.json`；同时必须有在该平台真正运行的用例。
3. **并发、取消与时序**：这类测试先用 `ci_remote_test.sh --repeat 5` 在三端各跑 5 遍，全部通过再提交 PR，避免把不稳定测试带进门禁。
4. **打包后的行为**：功能需要新依赖或新文件时，把它加进 `linux package` / `windows package` 作业的转换步骤，证明安装后的产物也能用；macOS 打包见第 6 节。
5. **Swift 逻辑**：界面以外的纯逻辑（解析、校验、版本比较等）抽成独立文件，在 `tests/` 下写 `XxxTests.swift`，并在 `cross-platform.yml` 的 `swift` 作业里注册。这一步改的是 `.github/`，属于受保护路径，PR 需要维护者审核。
6. **界面**：用户能看到的改动，在 `uitests/` 里补端到端用例。浏览器界面写在 `uitests/browser`；macOS 应用写成 `uitests/macos` 里的 JSON 剧本（需要新动作时扩展 `Sources/DebugHooks.swift` 的 `Scenario`）。画面有意变化时，按 `uitests/README.md` 更新基准截图。
7. **CLI 与浏览器界面**：新操作要同时出现在 `chamo` CLI 和 `chamo ui` 里，或在文档中写明某端不支持；`tests/test_cli.py`、`tests/test_ui.py` 补对应用例。

## 6. 已规划、尚未实现的 CI

| 项 | 计划 | 归属版本 |
| --- | --- | --- |
| macOS `.app`/DMG 打包与包内冒烟（`bootstrap_dependencies.py` → `build_app.py` → `package_dmg.py` → `packaged_smoke.py`） | tag 触发的 `release.yml`，也可每晚运行 | ITERATION_PLAN 第 0 轮 |
| Release 资产校验（下载、SHA-256、隐私扫描、干净安装与转换） | `release.yml` | 第 0 轮 |
| 性能基准冒烟（只报告，不卡门禁） | 固定语料、关闭页缓存、记录波动 | 0.2.0 |
| 质量检查器（渲染比对、SSIM、文字层） | 被各功能测试复用 | 0.2.0 |
| 打包后的界面测试（用安装后的 deb、zip、`.app` 跑 `uitests`） | `CHAMO_UI_COMMAND` 已支持指定已安装的 `chamo` | 第 0 轮之后 |

这些作业落地前，相关内容一律视为"未经 CI 验证"。

## 7. CI 覆盖不到、必须另外验收的部分

- **macOS 系统级交互**（Finder 服务、拖到 Dock、通知授权、侧栏与工具栏的毛玻璃画面）：`ui` 作业覆盖应用窗口内的流程和截图，这些系统级部分仍需在 Mac 上用打包后的应用验收，在 PR 和计划里标"待 Mac 验收"。
- **真实文档的保真度**：只用维护者提供的小份只读样本，结果写进计划，样本不入库。
- **签名、公证、Gatekeeper、干净系统安装**：没有证据就不写"已通过"。

## 8. 已知问题（2026-10-22 核实）

现有测试里有两个在 main 上间歇失败，与本方案无关，但会让门禁偶尔变红。修复前不得用重试或跳过掩盖：

| 测试 | 平台 | 现象 | 原因与处理 |
| --- | --- | --- | --- |
| `tests/test_page_resume.py::test_cancelled_then_resumed_outputs_match_a_clean_run` | macOS | 取消后输出目录里残留 `.Chamo暂存/<任务>/…_page00041.jpeg`，违反"取消后输出目录保持原样"的约定 | 引擎竞态：线程池在 `submit()` 里按需启动线程，取消异常落在启动等待里时，新线程不在池的线程集合中，`shutdown(wait=True)` 不等它，它在暂存目录被删时写入一页。引擎改为所有页面编码经过一道“门”，`render()` 退出前关门并等正在写的页完成；新增确定性回归测试 `tests/test_cancel_encode_race.py`。PR #2；macos-latest 重复 20/20 通过 |
| `tests/test_shard_equivalence.py::test_content_text_is_not_sharded_and_not_slower` | Windows | `content.text.timing` 断言失败 | 计时噪声，不是变慢：文本 PDF 上 auto 只多约 2 ms 的内容判定；windows-latest 上同一代码 36 对实测，单对比值 0.95–1.16。Windows 拿不到子进程 CPU 时间，墙钟改按“每臂最快一次”判定，预算仍为 5%，超预算时补测到最多 6 对再下结论，去掉“无法判定”跳过。PR #3；windows-latest 重复 10/10 通过 |

## 9. 纪律

- 不得删减、跳过或放宽已有测试与门禁来让 CI 变绿。不稳定的测试先开 Issue 写明现象，再修复；不加自动重试来掩盖。
- 合并门禁里 pytest 用 `-x`：遇到第一个失败就停，修完再推。`test-matrix` 不用 `-x`，以便比较多次运行的结果。
- 依赖版本以 `third_party/requirements.lock` 为准；升级依赖单独成 PR，并附三端结果。
- 单个作业的运行时间增加超过 3 分钟，要在 PR 中说明理由。

## 10. 维护者在 Mac 上复现

```sh
python3 -m pip install -r third_party/requirements.lock
python3 -m pytest tests -q -rs -x --junitxml=results.xml
python3 scripts/ci_report.py --platform macos --skips tests/ci_allowed_skips.json results.xml
python3 -m pytest ops/agent-loop -q
```

Swift 的编译与单测命令见 `.github/workflows/cross-platform.yml` 的 `swift` 作业。
