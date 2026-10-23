<p align="center">
  <img src="assets/brand/png/chamo-icon-1024.png" width="128" alt="Chamo 图标">
</p>

<h1 align="center">Chamo 卡莫</h1>

<p align="center">
  像变色龙换颜色一样，给文件换格式。<br>
  本地运行的文件转换工具：PDF、Office/WPS、图片互转与压缩，以及 RAG 知识库前置处理。
</p>

<p align="center">
  <a href="https://github.com/WangMingCF/chamo/releases/latest">下载</a> ·
  <a href="docs/USER_GUIDE.txt">使用说明</a> ·
  <a href="docs/RELEASE_NOTES.txt">更新说明</a> ·
  <a href="README.en.md">English</a>
</p>

<p align="center">
  <img src="assets/brand/png/chamo-mascot-1024.png" width="220" alt="Chamo 吉祥物：趴在文档上的变色龙">
</p>

## 为什么用 Chamo

- **文件不离开电脑**：所有转换都在本机完成，不上传、不联网，不需要账号。
- **原件始终保留**：结果写到单独的位置；遇到同名文件自动改名，绝不覆盖。
- **拖进来就会推荐**：把任意文件拖到“开始”页，自动列出能用的转换。
- **批量又快又稳**：上千个小文件几秒完成；大 PDF 逐页显示进度，中断后已完成的页直接复用。
- **说清楚有什么变化**：有损操作需要你主动选择；做不到无损时会如实报告，不会悄悄降级。

## 下载

在 [Releases](https://github.com/WangMingCF/chamo/releases/latest) 下载对应平台的安装包：

| 平台 | 安装包 | 说明 |
| --- | --- | --- |
| macOS（Apple Silicon，macOS 15+） | `Chamo-版本号-AppleSilicon.dmg` | 打开后把 Chamo 拖入“应用程序”。内置 Python、PDF/图片引擎与 LibreOffice，无需另装任何东西 |
| Windows 10/11 x64 | `chamo-cli_版本号_windows-x64.zip` | 解压后运行 `chamo.cmd ui` 打开本机界面 |
| Debian/Ubuntu（x86_64、arm64） | `chamo-cli_版本号_amd64.deb` / `_arm64.deb` | 安装后运行 `chamo ui` |

Windows 与 Linux 的界面是只在本机（127.0.0.1）提供、带随机令牌的浏览器页面，与 macOS 应用使用同一个引擎、同一套规则。

macOS 版采用 ad-hoc 签名，尚未经过 Developer ID 签名和 Apple 公证；首次打开如被系统阻止，请在“系统设置 → 隐私与安全性”中允许打开。Windows 便携包未签名。

## 能做什么

| 转换 | 说明 |
| --- | --- |
| PDF 转图片 | PNG/JPEG，可选页范围和 DPI；静态导出，不保留文字层与交互 |
| PDF 拆分 / 合并 | 拆分同时限制页数与实际大小；合并按列表顺序，拒绝无法完整保留的复杂结构 |
| PDF 压缩 | 无损清理，或主动选择的有损重采样；加密和带数字签名的 PDF 拒绝处理 |
| Office 转 PDF / 图片 | Word、PPT、Excel、WPS 文档本地渲染；复杂字体与版式需要复核 |
| 图片合成 PDF | 按顺序合成，支持原尺寸或 A4/A3/Letter 加边距，不裁剪 |
| 图片转换 / 压缩 | PNG/JPEG/WebP/TIFF/BMP 输出，HEIC、AVIF 输入；PNG 无损优化 |
| RAG 预处理 | 保持目录结构与原格式、排除音视频，逐项校验后生成上传清单 |

OCR、PDF 转可编辑 Office、长图拼接、压缩包解包与音视频处理属于后续规划，见[路线图](docs/ROADMAP.txt)。

## 隐私

Chamo 不收集任何数据。检查更新默认关闭；只有你手动检查或主动开启自动检查时，才会向 GitHub 查询最新版本号。应用内的“反馈与建议”会在浏览器中打开预填好的 GitHub Issue，由你检查后提交；不会自动附带文件、路径或日志。

## 反馈

欢迎在 [Issues](https://github.com/WangMingCF/chamo/issues) 提交问题和建议。反馈内容是公开的，请不要附带私人资料。

## 开发

构建、测试与项目结构见 [开发说明](docs/DEVELOPMENT.md)。需求集中在 [提案总目录](proposals/INDEX.md)；其他资料见 [文档目录](docs/DEVELOPMENT.md)。

## 许可证

Chamo 以 [MIT 许可证](LICENSE) 开源。内置的第三方组件（LibreOffice、Python 及其依赖等）保留各自的许可证，见[第三方许可](docs/LICENSES.txt)与 [RAG 引擎归属](engine/THIRD_PARTY_NOTICES.md)。
