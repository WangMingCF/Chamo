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

剧本步骤（`Scenario` 解析的键）：

| 键 | 作用 |
| --- | --- |
| `tool` / `route` | 切换工具或页面 |
| `add` / `output` / `set` | 加入文件、设输出目录、改格式与参数 |
| `appearance` | light / dark |
| `resize` | 显式设定窗口内容尺寸（例：width 1024、height 700）。runner 的默认尺寸取决于显示器，窄窗口检查必须显式设定；载荷非法会让步骤失败而不是静默跳过 |
| `start` / `cancel` / `retry` / `sleep` / `wait` | 控制一次运行 |
| `snapshot` / `summary` | 截图、导出状态 JSON |

## 已知问题

- macOS 进程内截图（`cacheDisplay`）在本机 Mac 上可能画不出使用系统毛玻璃材质的侧栏和工具栏，GitHub 的 macOS runner 上则能正常画出；基准截图一律以 runner 为准。"已添加"这类提示几秒后才消失，需要比对的截图前要先等它消失。
- 窄窗口（1024）下列表列被截断的问题已修复（Issue #12）：五列都设了显式上限，合计小于表格视口。它现在由剧本 `test_the_window_at_1024_keeps_every_column_readable` 和基准 `macos/narrow-window.png` 守着——基准是 1024×700 的截图，改了列宽就会变成像素差异。

