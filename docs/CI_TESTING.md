# 多端功能单测方案（GitHub Actions）

本文件规定 Chamo 的功能测试在哪里跑、怎么判定、新功能必须补什么。开发 Agent、评审和维护者都以它为准；改动测试体系时同步更新本文件。

Mac-DSH、Mac-Claude、NAS-DSH 均可在具备依赖的隔离环境运行适用测试，并负责调度跨平台 CI。真机验收由 Mac 端执行。每份结果标明执行端、准确 SHA、平台与命令，本地通过不能替代其他平台的必需检查。CI 全绿只说明下文"已覆盖"的部分没有回退；没覆盖的部分必须按第 7 节另外验收。

## 1. 三类工作流

| 工作流 | 何时运行 | 作用 |
| --- | --- | --- |
| `cross-platform` | 每个 PR、每次推送到 main | 合并门禁：三端引擎测试、打包后转换、Swift 编译与单测、CI 工具测试；汇总为一项检查 `all checks` |
| `agent-review` | PR 打开或更新时 | 未参与实现的另一执行端独立读取完整源码、调用链和测试并实际验证；GitHub 校验签名、head/base、隐私及提交身份后写入 `agent-review`。流程见 [评审入口](../scripts/ci/README.md) |
| `test-matrix` | 按需触发（含 `scripts/ci_remote_test.sh`） | 按需在任意平台跑任意测试、可重复运行找不稳定测试；负责人根据改动和历史不稳定范围选择平台与轮次，不做无变化的固定全量重复 |

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
| `ops scripts` | Linux x64 | CI 工具：隐私扫描、签名审查证据验证测试、shellcheck |
| `all checks` | Linux x64 | 只有上面每个作业都成功才通过；任何一个被跳过或取消也算失败 |

三个平台的引擎作业共用 `.github/actions/setup-engine`：Python 3.12、`third_party/requirements.lock` 锁定的依赖（带 pip 缓存）和真实的 LibreOffice。`test-matrix` 用的也是同一个环境。

## 3. 判定规则：`scripts/ci_report.py`

每个引擎作业都把 pytest 结果写成 JUnit XML，再交给 `scripts/ci_report.py` 判定，判定结果写进作业摘要（Actions 页面上的 Summary）。以下任何一种情况都会让作业失败：

- **失败**：测试失败或出错。
- **不稳定**：重复运行时，同一个测试有时失败、有时通过。
- **未登记的跳过**：跳过不等于通过。每个平台允许的跳过登记在 `tests/ci_allowed_skips.json`，按文件和跳过原因完全匹配（`reason`），并写明理由（`why`）；原因里带实测数值的，才用前缀匹配（`reason_prefix`）。登记时以源码中的原因为准：pytest 日志末尾的跳过汇总可能被截断。
- **不完整结果**：任何 XML 缺失或为空、重复传入、预期轮数不足、重复选择中的测试消失，或真实进程退出码非零/缺失。浏览器与原生 UI 的不同套件可以汇总，只有明确重复运行才要求测试集合相同。

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
4. 令牌需要 Actions 读写权限才能触发运行。

## 5. 新功能必须补的测试

每个新增或改动的功能，在同一个 PR 里补齐：

1. **引擎功能测试**（`tests/test_*.py`，三端都会运行）：
   - 成功路径：用合成样例在临时目录里现场生成输入（参考 `tests/conftest.py` 和 `scripts/create_fixtures.py`），不要提交大的二进制样例。
   - 断言要落到结果本身：页数、像素或渲染比对、文本抽取、哈希、清单内容，而不只是"没报错"或"文件存在"。
   - 失败路径各至少一条：加密、损坏、同名冲突（不覆盖）、取消、中断后续跑；涉及磁盘空间的加低磁盘用例。
   - 原件不变：处理前后对原件做哈希比对。
2. **平台差异**：只在某个平台成立的行为用 `pytest.mark.skipif` 并写明原因，登记到 `tests/ci_allowed_skips.json`；同时必须有在该平台真正运行的用例。
3. **并发、取消与时序**：先提交需求 Draft PR，再用 `ci_remote_test.sh --repeat 5` 在适用平台验证候选实现；真实结果通过后才进入合并，不能为规避失败而反复重跑。
4. **打包后的行为**：功能需要新依赖或新文件时，把它加进 `linux package` / `windows package` 作业的转换步骤，证明安装后的产物也能用；macOS 打包见第 6 节。
5. **Swift 逻辑**：界面以外的纯逻辑（解析、校验、版本比较等）抽成独立文件，在 `tests/` 下写 `XxxTests.swift`，并在 `cross-platform.yml` 的 `swift` 作业里注册。CI 改动也需要独立完整代码审查及对应工作流验证。
6. **界面**：用户能看到的改动，在 `uitests/` 里补端到端用例。浏览器界面写在 `uitests/browser`；macOS 应用写成 `uitests/macos` 里的 JSON 剧本（需要新动作时扩展 `Sources/DebugHooks.swift` 的 `Scenario`）。画面有意变化时，按 `uitests/README.md` 更新基准截图。
7. **CLI 与浏览器界面**：新操作要同时出现在 `chamo` CLI 和 `chamo ui` 里，或在文档中写明某端不支持；`tests/test_cli.py`、`tests/test_ui.py` 补对应用例。

## 6. 包体、发布与质量验证

| 项 | 实际状态与验收要求 |
| --- | --- |
| macOS `.app`/DMG | release.yml 已实现构建、挂载安装和包内 smoke；每个候选引用准确运行与资产，不代表签名/公证或干净系统已验收 |
| Linux/Windows 发布包 | 已有安装/解包后转换；发布前扫描项目文本、核对版本与 SHA-256 |
| Release | 已有 v0.1.0；后续候选由负责人触发构建、核对必要真机结果后正式发布，再下载验证 |
| 质量检查器 | bench_quality.py 已在主干；有像素、页数、文本/哈希检查，仍需针对各操作补反例并接入包体验收 |
| 性能基准与打包 UI | 以具体 PR、CI 和产物的证据判定；存在源码测试不能自动推定已安装包通过 |

100% 单测行与分支覆盖是目标。当前 .coveragerc 只是 Python 引擎混合路径报告，不能声称全语言纯单测已达标；保留分母和缺口，逐项补测，覆盖率也不能代替用户流程、平台及最终包体验收。

## 7. CI 覆盖不到、必须另外验收的部分

- **macOS 系统级交互**（Finder 服务、拖到 Dock、通知授权、侧栏与工具栏的毛玻璃画面）：`ui` 作业覆盖应用窗口内的流程和截图，这些系统级部分仍需在 Mac 上用打包后的应用验收，在 PR 和计划里标"待 Mac 验收"。
- **真实文档的保真度**：只用维护者提供的小份只读样本，结果写进计划，样本不入库。
- **签名、公证、Gatekeeper、干净系统安装**：没有证据就不写"已通过"。

## 8. 已知问题（2026-10-22 核实）

现有测试里有两个在 main 上间歇失败，与本方案无关，但会让门禁偶尔变红。修复前不得用重试或跳过掩盖：

| 测试 | 平台 | 现象 | 原因与处理 |
| --- | --- | --- | --- |
| `tests/test_page_resume.py::test_cancelled_then_resumed_outputs_match_a_clean_run` | macOS | 取消后输出目录里残留 `.Chamo暂存/<任务>/…_page00041.jpeg`，违反"取消后输出目录保持原样"的约定 | 引擎竞态：线程池在 `submit()` 里按需启动线程，取消异常落在启动等待里时，新线程不在池的线程集合中，`shutdown(wait=True)` 不等它，它在暂存目录被删时写入一页。引擎改为所有页面编码经过一道“门”，`render()` 退出前关门并等正在写的页完成；新增确定性回归测试 `tests/test_cancel_encode_race.py`。PR #2；macos-latest 重复 20/20 通过 |
| `tests/test_shard_equivalence.py::test_content_text_is_not_sharded_and_not_slower` | Windows、macOS | `content.text.timing` 断言失败 | 计时噪声，不是变慢：文本 PDF 上 auto 只多“导入 pageclass + 内容判定”，实测约 20 ms CPU。Windows：墙钟在同样工作下 5.0–13.3 s，改测 CPU 时间（引擎挂起启动、放进专用 Job Object 再恢复，作业记账累计整棵进程树，包括工作进程所在的嵌套作业；`test_tree_cpu_clock_counts_a_worker_in_its_own_group` 在三端验证）。两种时钟都改为每臂取最小值，超预算时补测到最多 14 对，预算 5% 不变。macOS（#10）：同样的工作 CPU 秒数可高可低（误差是双向的，疑与 M1 runner 的性能核/能效核调度有关），3 台 runner 每台 12 对，每臂最小值之比仍在 0.89–1.11，固定单个编码线程也一样（变异系数 8.5–11%）；42 组交替实测 auto/off 中位数 0.97–1.00，没有真实变慢。维护者决定：① 三端都新增 `content.text.sample_cost`：在新进程里测内容判定的 CPU，占 off 运行 CPU 的比例 ≤ 5%（实测约 0.25%）；② Linux、Windows 的端到端 auto/off 比值继续作为门禁；③ macOS 的端到端比值照常测量，以 REPORT 状态打印，不作判定（`verify_shard.TEXT_END_TO_END_JUDGED`）。Windows 上 Job Object 无法建立时退回墙钟，此时 sample_cost 也用墙钟比较，两边口径一致。`content.text.sample_cost` 另有绝对上限 0.25 s（实测约 0.02 s）。**残余风险（已接受）：** macOS 上若引擎在内容判定之外引入了真实的端到端变慢，门禁不会自动拦下，只会在 REPORT 中看到；Linux、Windows 上的端到端门禁仍会拦下这类回归，而引擎代码三端相同。不跳过、不重试。PR #7；三端重复结果见 PR |
| `tests/test_cli.py::test_ctrl_c_does_not_wait_for_queued_page_encodes` | macOS | 偶发 `the run never encoded a page; the fixture or the hook is broken` | 测试的时序依赖：启动后固定等 1 s 就发 Ctrl-C，慢 runner 上启动加第一页 300 dpi 渲染超过 1 s，中断到达时还没有任何编码开始，前置条件不成立。改为等编码记录里真的出现第一页再中断（最多等 60 s，超时即失败），不跳过、不重试。`scripts/ci_remote_test.sh --platforms core --repeat 10` 结果见对应 PR |

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
python3 -m pytest scripts/ci -q
```

Swift 的编译与单测命令见 `.github/workflows/cross-platform.yml` 的 `swift` 作业。

## 11. 覆盖率目标与当前报告

最终目标是100%单测行与分支覆盖率，不能因此删测、弱化断言或缩小统计范围。存量缺口需要记录并持续补齐；覆盖率本身不能保证功能正确，仍需以上真实转换、平台、界面与包体验证。

当前工作区的覆盖率改造是报告阶段：`.coveragerc` 默认统计 Python 引擎，`fail_under = 0`；引擎作业运行的测试包含集成路径，因此不能称为全量单测覆盖率，也不包含 Swift、UI 和安装包作业。尚未建立全量100%门禁，不得宣称已达标。报告、实际门禁与最终目标必须分别说明。
