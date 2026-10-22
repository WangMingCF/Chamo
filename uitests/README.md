# 界面自动化测试

两层，都在 GitHub Actions 上运行（`cross-platform` 的 `ui` 作业），本地也能跑：

| 目录 | 测什么 | 怎么驱动 |
| --- | --- | --- |
| `browser/` | `chamo ui` 本机浏览器界面（Windows / Linux / macOS） | Playwright 无头 Chromium 操作真实页面，断言落到输出文件上 |
| `macos/` | macOS 原生应用 | `-D CHAMO_DEBUG` 构建的应用执行 JSON 剧本（`Sources/DebugHooks.swift` 的 `Scenario`），进程内操作和截图，不需要辅助功能或屏幕录制授权 |

截图比对：`visual.py`。基准图按平台放在 `baselines/<platform>/<名字>.png`，因为各平台字体渲染不同。只比对确定性的画面（初始状态、固定输入下的列表）；含耗时、路径等动态内容的画面只作为产物上传，不比对，或者用遮罩盖住动态区域。

更新基准（界面有意变化时）：在 PR 说明里附前后对比，然后用 `scripts/ui_baselines.sh <运行 ID>` 下载 CI 产出的截图并替换基准。绝不在测试里自动覆盖基准。

本地运行：

```sh
python scripts/build_dev_app.py --out build/dev --python-prefix third_party/python   # macOS
python -m pytest uitests/macos -q
python -m pip install -r uitests/requirements.txt && python -m playwright install chromium
python -m pytest uitests/browser -q
```

新增界面功能时，至少补一条端到端剧本：操作 → 等待完成 → 断言输出文件内容与原件不变；有固定画面的再加一张基准截图。

## 已知问题

- macOS 进程内截图（`cacheDisplay`）画不出使用系统毛玻璃材质的区域：侧栏工具列表和工具栏图标在截图里是空白。这些区域用结构检查（`CHAMO_DUMP` 的侧栏行数）补上，不靠截图。另外，"已添加"这类提示几秒后才消失，需要比对的截图前要先等它消失。

- 取消时正在处理的那个文件，引擎报成 `failed`（说明是"任务已取消，原件保留"），窗口里显示成红色"失败"，而不是"已取消"。数据安全没有问题（输出目录保持原样、原件不变，`test_cancel_leaves_the_output_folder_clean` 守着），只是状态文案不准确。修复时把该用例的状态断言收紧为 `cancelled`。
