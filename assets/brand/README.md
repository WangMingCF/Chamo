# Chamo 品牌资源

Chamo（卡莫）的形象是一只变色龙：它趴在一页文档上，身体一半已经变了颜色，文档下半截也变成另一种颜色（格式），卷起的尾巴末端是一个转换箭头。寓意是：换格式，就像变色龙换颜色。

## 文件

| 文件 | 用途 |
|---|---|
| `chamo-icon.svg` | 应用图标主稿（macOS 图标网格：1024 画布，824 底板，四周各留 100） |
| `chamo-icon-small.svg` | 16–32 px 专用简化版 |
| `Chamo.icns` | macOS 应用图标（16–1024 共 10 个尺寸） |
| `chamo-mascot.svg` | 吉祥物，不带底板，用于 README、官网、空状态 |
| `chamo-wordmark.svg` / `-dark.svg` | 字样（已转成轮廓，不依赖字体）：浅色背景用 / 深色背景用 |
| `png/` | README 使用的图标与吉祥物 PNG（字样 PNG 由 `build.sh` 输出到 `build/`，不提交） |

## 颜色

| 用途 | 色值 |
|---|---|
| 底板渐变 | `#22B083` → `#0E7356` |
| 原格式（薄荷绿） | `#A8EBD3` |
| 新格式（琥珀） | `#FFC85C`，深一档 `#E8961E` |
| 墨绿（脚、背脊、浅色字样） | `#0B4D3B` |

字样中，“Cha”用原格式色，“mo”用新格式色，表示名字本身也“变色”了一半。

## 重新生成

所有资源都由脚本生成，修改源脚本后运行：

```sh
assets/brand/build.sh
```

`make_chamo.py` 在 120 单位的设计空间里画图标、小图标和吉祥物，三者共用同一套几何。`make_wordmark.swift` 用 CoreText 把 SF Pro Rounded Heavy 字形转成轮廓路径。`render_svg.swift` 用 AppKit 把 SVG 渲染成 PNG，`iconutil` 打包成 `.icns`。只能在 macOS 上运行。
