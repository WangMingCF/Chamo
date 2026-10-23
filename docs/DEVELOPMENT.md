# 开发说明

需求见 [提案目录](../proposals/README.md)，测试见 [CI_TESTING.md](CI_TESTING.md)。

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `Sources/` | macOS 原生应用（SwiftUI/AppKit）。`ImageDecode.swift` 是单独编译的 HEIC 解码辅助程序 |
| `engine/` | 转换引擎（Python）：`workbench.py` 负责任务编排与转换，`chamo.py` 是命令行入口，`ui.py` 是 Windows/Linux 的本机浏览器界面，`platform_*.py` 是平台适配层，`cloud_kb_pack/` 是 RAG 严格拆分引擎 |
| `scripts/` | 依赖下载、构建、打包与 CI 验收脚本 |
| `tests/` | 引擎测试（pytest）与 Swift 单元测试 |
| `assets/brand/` | Logo、图标与吉祥物的源文件和生成脚本 |
| `proposals/` | 需求总目录、每项详细 PRD（含状态与设备负责人） |
| `docs/` | 使用、架构、开发流程、测试规则与版本队列 |
| `scripts/ci/` | 隐私检查与 DSH 独立评审结果验证工具 |
| `.development/` | 私有持续开发规范与部署记录，Git 忽略 |
| `uitests/` | 浏览器与原生界面的端到端测试及基准截图 |
| `build/`、`dist/`、`release/` | 可再生构建产物、覆盖率与验证结果，不提交 |
| `local/`、`research/`、`artifacts/` | 本地资料与工作记录，不提交；确认用途前不批量删除 |
| `third_party/` | 本地运行时；只提交依赖清单和锁文件 |
| `config/release.json` | 版本号、构建号与仓库地址 |

macOS 应用、命令行和浏览器界面调用同一个引擎，规则只有一份。平台差异集中在 `engine/platform_*.py`，架构概览见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 准备环境

需要 Apple Silicon Mac、Xcode Command Line Tools 和 Python 3。

```sh
python3 scripts/bootstrap_dependencies.py
```

它会下载固定版本的独立 Python 与 LibreOffice，校验 SHA-256 后安装锁定版本的 Python 依赖，全部放在 `third_party/`（不进入 Git）。

## 测试

```sh
third_party/python/bin/python3 -m pytest tests -q

mkdir -p build
swiftc -parse-as-library Sources/GitHubFeedback.swift tests/FeedbackTests.swift -o build/feedback-tests
./build/feedback-tests
swiftc -parse-as-library Sources/GitHubFeedback.swift Sources/UpdateCheck.swift tests/UpdateTests.swift -o build/update-tests
./build/update-tests
```

测试只使用合成样例（`tests/fixtures/`，可用 `scripts/create_fixtures.py` 重新生成），不读取真实用户文件。部分用例只适用于某个平台，在其他平台上会打印原因并跳过。

## 构建与打包

```sh
python3 scripts/build_app.py                          # build/Chamo.app
third_party/python/bin/python3 scripts/collect_sources.py
python3 scripts/package_dmg.py                        # release/Chamo-<version>-AppleSilicon.dmg
python3 scripts/packaged_smoke.py build/Chamo.app     # 用包内运行时跑核心转换流程
```

Windows 与 Linux 包由 CI 构建：`scripts/build_windows.py --out dist` 生成便携 ZIP，`scripts/build_linux.py --out dist` 生成 `.deb`。

## 持续集成

`.github/workflows/cross-platform.yml` 在 Ubuntu、Windows、macOS 上安装真实 LibreOffice 并运行同一套引擎测试；Office 用例必须真正执行，不允许被跳过。它还会构建 Linux deb 和 Windows 便携包，安装或解压后用真实产物跑转换与界面验收（`scripts/verify_deb.sh`、`scripts/verify_windows.py`、`scripts/verify_ui.py`）。

## 界面调试

用 `-D CHAMO_DEBUG` 编译的调试版会读取一组 `CHAMO_*` 环境变量（见 `Sources/DebugHooks.swift`），用于自动添加文件、切换工具、开始转换和截图。这些钩子只存在于调试版，发布版二进制中不包含。

## 品牌资源

修改 `assets/brand/` 下的源脚本后运行 `assets/brand/build.sh`，会重新生成 SVG、`Chamo.icns` 和 README 用到的 PNG。说明见 [assets/brand/README.md](../assets/brand/README.md)。

## 文档入口

使用与下载见 [使用说明](USER_GUIDE.txt)、[更新说明](RELEASE_NOTES.txt)；产品方向见 [路线图](ROADMAP.txt)，开发顺序与事实记录见 [迭代计划](ITERATION_PLAN.md)。界面验收见 [UI 测试](../uitests/README.md)，依赖许可见 [第三方许可](LICENSES.txt)，调研证据见 [竞品资料](COMPETITORS.md)。需求只在 proposals 中维护，详细执行提示词与设备日志保存在被 Git 忽略的 .development 中。
