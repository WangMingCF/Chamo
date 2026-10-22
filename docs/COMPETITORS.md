# Chamo 竞品对比（第一版）

> 建立日期：2026-10-22。维护人：自循环 Agent（产品角色）。每次调研只增补事实与链接，不改写历史结论；被推翻的结论移到文末「已推翻」。

## 0. 方法与证据等级

只为**官方来源**记账：官网、官方文档、定价页、发布说明、开源仓库。每条事实带来源编号与访问日期，编号表见第 5 节。

| 等级 | 含义 |
| --- | --- |
| **宣称** | 官方说法，我们没有验证 |
| **观察** | 我们读过官方页面或文档，确认存在该能力描述 |
| **实测** | 我们在同一份语料上用同一口径跑过对比（脚本可复跑） |
| **推测** | 由前几级推断，未被任何来源直接支持 |

**本版没有任何「实测」。** 商业产品一律标「未实测」；开源项目具备实测条件（`scripts/bench.py` 的语料与口径可直接复用），但尚未做——这是下一版的产品/工程任务，不是本文件能声称的结论。

复核说明（如实记录）：

- 本版结论来自 2026-10-22 对官方页面的抓取；
- 成稿时复核发现两处抓不到：`docs.stirlingpdf.com/Tools/Guides/Compress/` 抓取超时，iLovePDF 的「PDF→Markdown」页面返回 404。这两条在矩阵里标 **待复核**，不作为决策依据；
- Kumquat 官网（kumquat.app / www.kumquat.app）当日 404，检索未定位到官网，本文件不写它的任何能力。

## 1. 对比矩阵 A：文档 / PDF 工具

| 能力 | Stirling PDF | Sejda | iLovePDF | PDF24 | macOS 预览 | WPS |
| --- | --- | --- | --- | --- | --- | --- |
| PDF→图片 | ✓观察 多格式＋72–600 DPI [4] | ✓观察 JPG/PNG/TIFF [11] | ✓观察 [14] | ✓观察 JPG/PNG＋DPI [20] | △ 使用手册未列该路径 [32] | △ 未确认 |
| Office→PDF | ✓观察 Office 全系 [4] | ✓观察 Word/HTML→PDF [11] | ✓观察 [14] | ✓观察 Creator 导入转 PDF [17] | — 未见 [32] | ✓观察 Word/Excel/PPT [38] |
| PDF 压缩 | ✓观察 质量 1–9 或**填目标体积** [5]（**待复核**） | ✓观察 [11] | ✓观察 [14] | ✓观察 多参数 [19] | ✓观察 预设档「缩小文件大小」[30] | ✓观察 [38] |
| 图片压缩 | — 只压 PDF 内图像 [5] | — 未见 | ✓观察 走 iLoveIMG [14] | △ 未确认 | ✓观察 导出可调质量 [29] | ✓观察 [37] |
| OCR | ✓观察 桌面端需云/自建服务器 [6][8] | ✓观察 年付上限 100 页 [11][13] | ✓观察 [14] | ✓观察 [16] | — 未列 [32] | △ 仅提及 [39] |
| RAG / 结构化导出 | △ 有 Documents & RAG 条目，未展开 [3] | — 未见 | △ 见「Convert PDF」分类，PDF→Markdown **待复核**（页面 404） | — 未见 | — 未见 | — 未见 |
| 批量 | ✓观察 流水线 / 目录监听 [7] | ✓观察 多文件 / Workflows [11][13] | ✓观察 档位限次 [15] | ✓观察 Creator 批处理 [17] | ✓观察 多图同窗转换 [29] | ✓宣称 [38] |
| 自动化入口 | ✓观察 REST＋MCP＋Automate [7][9] | ✓观察 Workflows，无 CLI [11] | ✓观察 Workflows＋iLoveAPI [14] | ✓观察 虚拟打印机/Profiles，无 CLI [17] | — 无 CLI [29] | — 无 CLI [37] |
| 文件名模板 | ✓观察 仅「用标题重命名」[3] | ✓观察 按页内文字重命名 [11] | — 未见 [14] | — 未见 [17] | — 未见 | — 未见 |
| 目标体积 | ✓观察 Desired File Size [5]（**待复核**） | — 未见 | — 未见 | — 未见 | — 预设档 [30] | — 未见 |
| 完全本地（不上传） | △ 桌面端 Local Only，但 OCR / Office→PDF 需云 [8] | △ 桌面本地、Web 上传 [12][13] | △ Desktop 离线、Web 上传 [14] | △ Creator 本地、在线版 1 小时后删 [17][18] | ✓观察 系统本地 [29][30][31] | △ 与云文档深度绑定 [37][39] |
| 平台 | Web/桌面/自托管 [1][10] | Web＋Win/Mac/Linux [12] | Web＋Mac/Win＋移动 [14] | 在线全平台＋Creator 仅 Win [17][18] | macOS/iOS [32] | Win/Mac/Linux/安卓/iOS [37] |

## 2. 对比矩阵 B：图片 / 入口 / RAG 解析

| 能力 | ImageOptim | Squoosh | Permute | Docling | Marker | Unstructured | MinerU | Hazel | Dropzone |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 图片压缩 | ✓观察 无损＋可选有损 [21] | ✓观察 [24] | ✓观察 [25] | — | — | — | — | — | ✓观察 [26] |
| 图片格式转换 | — 只优化不转格式 [21] | ✓观察 [23][24] | ✓观察 100+ 格式 [25] | — | — | — | — | — | △ 仅缩放 [26] |
| PDF→图片 | — | — | ✓观察 页面导图 [25] | — | — | — | — | — | — |
| OCR | — | — | ✓观察 图→文 [25] | ✓观察 [33] | ✓观察 [34] | △ 未见明确说明 [35] | ✓观察 Basic 档 [36] | — | — |
| RAG / 结构化导出 | — | — | — | ✓观察 MD/HTML/DocTags/JSON [33] | ✓观察 MD/JSON/chunks/HTML [34] | ✓观察 elements→JSON [35] | ✓观察 9 种输出 [36] | — | — |
| 批量 | ✓观察 拖目录 [21] | — 单图 [24] | ✓观察 [25] | — 单文档 CLI | — 单文档 CLI | △ | ✓观察 无状态批量 [36] | ✓观察 规则批处理 [27] | ✓观察 多文件 [26] |
| 自动化入口 | ✓观察 同步/异步 CLI [40] | — | △ 拖放/自动化，无 CLI [25] | ✓观察 CLI＋docling-serve [33] | ✓观察 CLI＋--use_llm [34] | △ 以 SDK/容器为主 [35] | ✓观察 mineru-kit CLI [36] | ✓观察 Shortcuts/AppleScript/Automator [27] | △ 可调 Shortcuts [26] |
| 文件名模板 | — | — | — | — | — | — | — | ✓观察 模式匹配重命名 [27] | — |
| 目标体积 | — 未见 [21] | — 未见 [24] | ✓观察 可设目标大小 [25] | — | — | — | — | — | — |
| 完全本地 | ✓观察 [21] | ✓观察 图片不出设备 [24] | ✓观察 [25] | ✓观察 支持 air-gapped [33] | ✓观察 可本地跑 [34] | ✓观察 库在本地运行 [35] | ✓观察 不自动上传 [36] | ✓观察 [27] | ✓观察 [26] |

## 3. 我们落后在哪、领先在哪

**落后**

1. **「目标体积」已经不是加分项，而是基线。** Stirling 压缩可填目标体积 [5]（待复核），Permute 可设目标输出大小 [25]（观察）。Chamo 现在的压缩只能选质量档，给不出「压到 ≤ X MB」。
2. **文件名模板。** Hazel 能按名称/日期/属性组合重命名并做模式匹配 [27]，Sejda 能按页内文字重命名 [11]（均观察）。Chamo 的输出命名是固定规则。
3. **批量与目录监听。** Stirling 有 Automate＋目录监听 [7]、PDF24 Creator 有批处理 [17]、Sejda 有 Workflows [11]（观察）。单文件交互已经不够。
4. **结构化导出（RAG）。** Docling/Marker/MinerU 直接产出 MD/JSON/chunks [33][34][36]（观察）。iLovePDF 是否已提供 PDF→Markdown 待复核，不作为依据。
5. **OCR 的「本地」是空档，不是别人的护城河。** Stirling 桌面版 OCR 需云或自建服务器 [8]，iLovePDF/PDF24 在线版都会上传 [14][18]（观察）。我们没做 OCR，但做「纯本地 OCR」在这个品类里是差异点，不是跟随。

**领先**

1. **三端 × 完全本地。** Sejda/iLovePDF/PDF24 的在线版都上传服务器 [12][14][18]；Permute/ImageOptim/预览/Hazel/Dropzone 都是单平台 macOS [21][25][26][27][32]（观察）。「macOS 原生＋Win/Linux CLI＋共用引擎」且处理全在本机，目前没有直接对手。
2. **免费、无账号、无配额。** iLovePDF Premium 5 美元/月 [15]、Permute 14.99 美元 [25]、Hazel 42 美元 [27]（观察）。Chamo 全部本地处理，不存在按页计费这件事。
3. **数据安全口径写死在产品里。** 原件不动、绝不覆盖不同内容、有损操作必须主动选择——竞品页面普遍不承诺这三点；这是我们的可信度资产，但**要先能被验证**（见下）。

**持平**

1. **自动化入口。** 竞品强在被其他程序调用（REST/MCP/iLoveAPI [7][9][14]），Chamo 有 CLI，但缺少可供第三方稳定调用的接口文档与契约测试（计划 1.9.0）。

**警号**

1. **格式广度。** Stirling 的 Convert 覆盖 Office/电子书/邮件/漫画 [4]，iLovePDF 覆盖 Office 双向 [14]。只做「几种常见格式」会在 Office→PDF 这一栏被直接比下去。
2. **我们的「领先」目前是官方页面级的观察，不是实测。** 要变成结论，需要同语料对比（下一节）。

## 4. 下一步（与 ITERATION_PLAN 的对应）

1. **同口径实测（0.2.0 的技术底座已就绪）**：用 `scripts/bench_corpus.py` 的合成语料与 `scripts/bench.py` 的口径，对可本机运行的开源/免费工具做实测（Stirling 自托管、ImageOptim CLI、Docling、MinerU），只报实测数字；不能在本机跑的商业产品继续标「未实测」。
2. **能力补课优先级**（按用户价值 ÷ 成本，详细条目写进 `docs/ITERATION_PLAN.md` 第 5 节）：目标体积 → 文件名模板 → Office 转换前审计 → 结构化导出。
3. **把「可信」变成可核验的东西**：处理报告导出（来源/成品 SHA-256、保真标记），竞品页面里没有人做，而我们已经具备数据基础。

## 5. 来源（访问日期均为 2026-10-22）

- [1] https://www.stirlingpdf.com/ ｜ [2] https://www.stirlingpdf.com/pricing ｜ [3] https://docs.stirlingpdf.com/Tools/
- [4] https://docs.stirlingpdf.com/Tools/Guides/Convert/ ｜ [5] https://docs.stirlingpdf.com/Tools/Guides/Compress/ ｜ [6] https://docs.stirlingpdf.com/Tools/Guides/OCR/
- [7] https://docs.stirlingpdf.com/Tools/Guides/Automate/ ｜ [8] https://docs.stirlingpdf.com/Desktop/Where-Tools-Run/ ｜ [9] https://docs.stirlingpdf.com/Developers/API/ ｜ [10] https://github.com/Stirling-Tools/Stirling-PDF
- [11] https://www.sejda.com/ ｜ [12] https://www.sejda.com/desktop ｜ [13] https://www.sejda.com/pricing
- [14] https://www.ilovepdf.com/ ｜ [15] https://www.ilovepdf.com/pricing
- [16] https://www.pdf24.org/en/ ｜ [17] https://www.pdf24.org/en/creator ｜ [18] https://tools.pdf24.org/en/ ｜ [19] https://tools.pdf24.org/en/compress-pdf ｜ [20] https://tools.pdf24.org/en/pdf-to-images
- [21] https://imageoptim.com/mac ｜ [22] https://imageoptim.com/api ｜ [40] https://imageoptim.com/command-line.html
- [23] https://github.com/GoogleChromeLabs/squoosh ｜ [24] https://squoosh.app/
- [25] https://software.charliemonroe.net/permute/
- [26] https://aptonic.com/ ｜ [27] https://www.noodlesoft.com/
- [28] https://support.apple.com/guide/shortcuts-mac/apd1d9413cfb/mac
- [29] https://support.apple.com/guide/preview/prvw1012/mac ｜ [30] https://support.apple.com/guide/preview/prvw1509/mac ｜ [31] https://support.apple.com/guide/preview/prvw43696/mac ｜ [32] https://support.apple.com/guide/preview/welcome/mac
- [33] https://github.com/docling-project/docling ｜ [34] https://github.com/datalab-to/marker ｜ [35] https://github.com/Unstructured-IO/unstructured ｜ [36] https://github.com/opendatalab/MinerU
- [37] https://www.wps.com/ ｜ [38] https://www.wps.com/feature/pdf-creator-software/ ｜ [39] https://www.wps.com/pdf-to-word/

原始笔记（逐条事实＋等级＋日期，不入库）：`/config/state/research/competitors-raw.md`。

## 6. 已推翻

（暂无。结论被证据推翻时移到这里并写明新证据。）

---

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
