# 多端功能单测方案（GitHub Actions）

本文件规定 Chamo 的功能测试在哪里跑、覆盖什么、新功能必须补什么。开发 Agent 和维护者都以它为准；改动测试体系时同步更新本文件。

核心原则：**CI 全绿只说明下表"已覆盖"的部分没有回退。** 表里写着"未覆盖"的部分，必须用其他方式验收（见第 4 节），不能拿 CI 结果代替。

## 1. 作业一览（每个 PR 和每次推送到 main 都会运行）

| 工作流 / 作业名（即必需检查名） | 运行环境 | 验证内容 |
| --- | --- | --- |
| `cross-platform` / `engine (ubuntu-latest)` | Linux x64 | 引擎全部 pytest（`tests/`）；装真实 LibreOffice，Office 转换测试必须实际运行并通过，跳过算失败 |
| `cross-platform` / `engine (windows-latest)` | Windows x64 | 同上；覆盖 Windows 独有问题（MoveFileExW、保留文件名、winerror、控制台编码） |
| `cross-platform` / `engine (macos-latest)` | macOS arm64 | 同上；覆盖 macOS 的原子发布（renamex_np）与 HEIC 等平台路径 |
| `cross-platform` / `linux package` | Linux x64 | 构建 `.deb` → 安装 → 用安装后的 `chamo` 做真实转换 |
| `cross-platform` / `windows package` | Windows x64 | 构建便携 zip → 解压 → 转换 → 驱动打包后的本机界面 |
| `unit` / `swift (macos-15)` | macOS arm64 | 编译 macOS 应用（正式版与 `CHAMO_DEBUG` 版）和 HEIC 辅助程序；运行 Swift 单测（`tests/*Tests.swift`） |
| `unit` / `ops scripts` | Linux x64 | 自循环工具：隐私扫描测试、shellcheck |
| `agent-review`（提交状态，非工作流） | VPS 上的独立评审 | 自动评审结论，见 `ops/agent-loop/README.md` |

建议在 main 分支保护里把上表全部 8 项设为必需检查。

## 2. 新功能必须补的测试

每个新增或改动的功能，在同一个 PR 里补齐：

1. **引擎功能测试**（`tests/test_*.py`，三端都会运行）：
   - 成功路径：用合成样例在临时目录里现场生成输入（参考 `tests/conftest.py` 和 `scripts/create_fixtures.py`），不要提交大的二进制样例。
   - 正确性断言要落到结果本身：页数、像素或渲染比对、文本抽取、哈希、清单内容，而不只是"没报错"或"文件存在"。
   - 失败路径各至少一条：加密、损坏、同名冲突（不覆盖）、取消、中断后续跑；涉及磁盘空间的加低磁盘用例。
   - 原件不变：处理前后对原件做哈希比对。
2. **平台差异**：只在某个平台成立的行为，用 `pytest.mark.skipif` 并写明原因；同时必须有在该平台上真正运行的用例。目标平台上出现跳过视为失败。
3. **打包后的行为**：功能需要新依赖或新文件时，把它加进 `linux package` / `windows package` 作业的转换步骤，证明安装后的产物也能用；macOS 打包见第 3 节。
4. **Swift 逻辑**：界面以外的纯逻辑（解析、校验、版本比较等）抽成独立文件，在 `tests/` 下写 `XxxTests.swift`，并在 `unit.yml` 的 Swift 测试步骤里注册（这一步改的是 `.github/`，属于受保护路径，PR 需要维护者审核）。
5. **CLI 与浏览器界面**：新操作要同时出现在 `chamo` CLI 和 `chamo ui` 里，或在文档中写明某端不支持；`tests/test_cli.py`、`tests/test_ui.py` 补对应用例。

## 3. 已规划、尚未实现的 CI

| 项 | 计划 | 归属版本 |
| --- | --- | --- |
| macOS `.app`/DMG 打包与包内冒烟（`bootstrap_dependencies.py` → `build_app.py` → `package_dmg.py` → `packaged_smoke.py`） | tag 触发的 `release.yml`；也可每晚运行一次 | ITERATION_PLAN 第 0 轮 |
| Release 资产校验（下载、SHA-256、隐私扫描、干净安装与转换） | `release.yml` | 第 0 轮 |
| 性能基准冒烟（只报告，不卡门禁） | 固定语料、关闭页缓存、记录波动 | 0.2.0 |
| 质量检查器（渲染比对、SSIM、文字层） | 被各功能测试复用 | 0.2.0 |
| 应用内 GUI 冒烟（`CHAMO_DEBUG` 钩子注入点击并截图） | 先在 macOS runner 上验证是否稳定，再决定是否作为必需检查 | 0.4.0 前 |

这些作业落地前，相关内容一律视为"未经 CI 验证"。

## 4. CI 覆盖不到、必须另外验收的部分

- **macOS 界面的真实操作**（点击、拖放、Finder 服务、Dock、通知）：在 Mac 上用打包后的应用验收。Linux 上的开发 Agent 做不到，在 PR 和计划里标"待 Mac 验收"。
- **真实文档的保真度**：只用维护者提供的小份只读样本，结果写进计划，样本不入库。
- **签名、公证、Gatekeeper、干净系统安装**：没有证据就不写"已通过"。

## 5. 纪律

- 不得删减、跳过或放宽已有测试与门禁来让 CI 变绿。确实不稳定的测试，先开 Issue 写明现象，修复它；不加自动重试来掩盖。
- `pytest -x` 遇到第一个失败就停，修完再推。
- 依赖版本以 `third_party/requirements.lock` 为准；升级依赖单独成 PR 并附三端结果。
- 单个作业运行时间的增量超过 3 分钟要在 PR 中说明理由。

## 6. 本地复现

```sh
python3 -m pip install -r third_party/requirements.lock
python3 -m pytest tests -q -rs -x          # 引擎功能测试
python3 -m pytest ops/agent-loop -q        # 自循环工具
shellcheck -x ops/agent-loop/*.sh ops/agent-loop/hooks/pre-push
```

Swift（仅 macOS）：命令见 `.github/workflows/unit.yml` 的 `swift` 作业，或 `docs/DEVELOPMENT.md`。
