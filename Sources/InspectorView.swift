import SwiftUI
import AppKit
import PDFKit
import QuickLookThumbnailing

struct InspectorView: View {
    @EnvironmentObject var m: Workbench
    @State private var customDPI = false
    private let dpiPresets = [(72, "72 · 屏幕"), (150, "150 · 标准"), (200, "200 · 清晰"), (300, "300 · 打印")]

    var body: some View {
        Form {
            if let item = m.selected, item.status == .failed {
                Section("异常说明") {
                    Label { Text(item.detail.isEmpty ? "处理失败，原件保留" : item.detail).fixedSize(horizontal: false, vertical: true) }
                        icon: { Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
                    ForEach(m.fixIts(for: item)) { fix in
                        Button(fix.title, action: fix.run).buttonStyle(.link)
                    }
                }
            }

            if !m.presets.isEmpty {
                Section("常用预设") {
                    HStack(spacing: 8) {
                        ForEach(m.presets) { preset in PresetChip(preset: preset) }
                    }
                }.disabled(m.running)
            }

            settings.disabled(m.running)

            Section("保真说明") {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(m.fidelity.title).fontWeight(.medium)
                        Text(m.fidelity.detail).font(.caption).foregroundStyle(.secondary)
                    }
                } icon: { Image(systemName: m.fidelity.symbol).foregroundStyle(m.fidelity.tint) }
                Text(m.tool.notice + (formatCaveat.map { "\n" + $0 } ?? ""))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }

            Section("预览") { PreviewPane(item: m.selected) }

            if m.operation != "rag" && m.operation != "pdf_split" {
                Section {
                    let (src, out) = m.exampleName(for: m.processable.first(where: { $0.kind != .folder }))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(src).font(.callout).lineLimit(1).truncationMode(.middle)
                        Label { Text(out).font(.callout.monospaced()).lineLimit(2).truncationMode(.middle) } icon: { Image(systemName: "arrow.turn.down.right").foregroundStyle(.secondary) }
                    }
                    Picker("命名方式", selection: $m.cleanNaming) {
                        Text("简洁").tag(true); Text("带任务标识").tag(false)
                    }.pickerStyle(.segmented).disabled(m.running)
                } header: { Text("输出命名") } footer: {
                    Text(m.cleanNaming
                         ? "多页结果放进以原文件命名的文件夹；遇到同名文件自动加“ (2)”，绝不覆盖。"
                         : "文件名保留原扩展名并附加 8 位任务标识，便于追溯。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Section("输出结构") {
                    Text("保持输入根目录的相对结构，不额外嵌套目录；超限 PDF 拆为多个分片。输出目录只放成品：单个文件转换不写任何额外文件；一次转换多个文件、各自产出成品时，目录里多一个「清单.json」列明每个来源产出了什么（不会覆盖你自己的同名文件，会另建「清单 (2).json」；合并类任务不写清单）。运行记录保存在系统的应用数据目录中。")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .formStyle(.grouped)
        .onAppear { customDPI = !dpiPresets.contains { $0.0 == m.dpi } }
    }

    var formatCaveat: String? {
        if m.format == "jpeg" && (m.operation == "image_convert" || (m.operation == "image_compress" && m.lossy)) { return "JPEG 不支持透明，透明区域会填充为白色。" }
        return nil
    }

    // MARK: Per-tool settings

    @ViewBuilder var settings: some View {
        switch m.operation {
        case "pdf_images", "office_images":
            Section("输出") {
                formatPicker
                Picker("分辨率", selection: dpiBinding) {
                    ForEach(dpiPresets, id: \.0) { Text($0.1).tag($0.0) }
                    Divider()
                    Text("自定义…").tag(-1)
                }
                if customDPI { NumberField(title: "自定义分辨率", value: $m.dpi, unit: "DPI", range: 36...600) }
                if m.showsQuality { QualitySlider(value: $m.quality) }
            }
            pageSection
        case "office_pdf":
            pageSection
        case "images_pdf":
            Section {
                Picker("纸张", selection: $m.paper) {
                    Text("与图片相同").tag("original"); Text("A4").tag("a4"); Text("A3").tag("a3"); Text("Letter").tag("letter")
                }
                if m.paper != "original" { NumberField(title: "页边距", value: $m.marginMM, unit: "mm", range: 0...50) }
            } header: { Text("页面") } footer: {
                Text(m.paper == "original" ? "每页尺寸与图片一致，不缩放、不裁剪。" : "图片等比缩放放入纸张，不裁剪；按列表顺序合并，可在工具栏调整顺序。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        case "image_convert":
            Section("输出") {
                formatPicker
                if m.showsQuality { QualitySlider(value: $m.quality) }
                Toggle(m.format == "webp" ? "允许有损压缩与缩小尺寸" : "允许缩小尺寸", isOn: $m.lossy)
                if m.lossy { NumberField(title: "最长边", value: $m.maxEdge, unit: "px", range: 0...16000, hint: "0 表示保持原尺寸") }
            }
        case "image_compress":
            Section {
                Picker("方式", selection: $m.lossy) {
                    Text("无损优化").tag(false); Text("降低质量 / 尺寸").tag(true)
                }.pickerStyle(.segmented)
                if m.lossy {
                    formatPicker
                    if m.showsQuality { QualitySlider(value: $m.quality) }
                    NumberField(title: "最长边", value: $m.maxEdge, unit: "px", range: 0...16000, hint: "0 表示保持原尺寸")
                }
            } header: { Text("压缩") } footer: {
                Text(m.lossy
                     ? "有损输出为 JPEG 或 WebP：画质或尺寸会降低，原件保留；PNG 只在无损档处理。"
                     : "像素完全不变，只重新压缩 PNG 数据；其他格式请选择「降低质量 / 尺寸」。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        case "pdf_compress":
            Section {
                Picker("方式", selection: $m.compressLossy) {
                    Text("无损清理").tag(false); Text("有损重采样").tag(true)
                }.pickerStyle(.segmented)
                if m.compressLossy {
                    NumberField(title: "分辨率", value: $m.compressDPI, unit: "DPI", range: 72...600)
                    QualitySlider(value: $m.compressQuality, title: "JPEG 质量", range: 1...100)
                }
            } header: { Text("PDF 压缩") } footer: {
                Text(m.compressLossy
                     ? "有损重采样：每一页渲染成 JPEG 后再重建 PDF，文字层、链接和表单不再保留（保真变化）；\(m.compressDPI) DPI、JPEG 质量 \(m.compressQuality)。原件保留。"
                     : "无损清理：只重新压缩 PDF 内部结构，页面内容不变；部分文件可能没有收益，此时会保留与原文件相同的副本。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        case "pdf_split":
            splitSection
        case "rag":
            splitSection
            Section {
                NumberField(title: "图片", value: $m.imageMB, unit: "MB", range: 1...100_000)
                NumberField(title: "Word / PPT 等文档", value: $m.documentMB, unit: "MB", range: 1...100_000)
                NumberField(title: "纯文本", value: $m.textMB, unit: "MB", range: 1...100_000)
                NumberField(title: "表格", value: $m.tableMB, unit: "MB", range: 1...100_000)
                NumberField(title: "表格行数", value: $m.tableRows, unit: "行", range: 1...100_000_000)
            } header: { Text("单文件上限") } footer: {
                Text("超出上限的文件会拆分或列为异常，并在报告中说明原因。1 MB = 1,000,000 字节。").font(.caption).foregroundStyle(.secondary)
            }
            Section {
                NumberField(title: "每批文件数", value: $m.batchFiles, unit: "个", range: 1...1000)
                Button("保存为默认规则") { m.savePreset() }
            } header: { Text("上传批次") } footer: {
                Text("批次是上传清单中的索引，不会复制文件。").font(.caption).foregroundStyle(.secondary)
            }
        default: EmptyView()
        }
    }

    var formatPicker: some View {
        Picker("格式", selection: $m.format) {
            ForEach(m.formatOptions, id: \.0) { Text($0.1).tag($0.0) }
        }
        .pickerStyle(.segmented)
        .onAppear { if !m.formatOptions.contains(where: { $0.0 == m.format }), let f = m.formatOptions.first { m.format = f.0 } }
        .onChange(of: m.lossy) { if !m.formatOptions.isEmpty, !m.formatOptions.contains(where: { $0.0 == m.format }) { m.format = m.formatOptions[0].0 } }
    }

    var splitSection: some View {
        Section {
            NumberField(title: "每份最多", value: $m.pdfPages, unit: "页", range: 1...10000)
            NumberField(title: "每份不超过", value: $m.pdfMB, unit: "MB", range: 1...1_000_000)
        } header: { Text("PDF 拆分") } footer: {
            Text("同时满足页数与大小两个条件；未超限的 PDF 原样保留。").font(.caption).foregroundStyle(.secondary)
        }
    }

    var pageSection: some View {
        Section {
            Picker("页面", selection: $m.useRange) { Text("全部页面").tag(false); Text("指定页面").tag(true) }.pickerStyle(.segmented)
            if m.useRange {
                TextField("页码", text: $m.pageRange, prompt: Text("例如 1-5,8,12-20"))
                switch m.rangeStatus {
                case .success(let text)?: Label(text, systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(.green)
                case .failure(let e)?: Label(e.text, systemImage: "exclamationmark.circle.fill").font(.caption).foregroundStyle(.red)
                case nil: EmptyView()
                }
            }
        } header: { Text("页面范围") }
    }

    var dpiBinding: Binding<Int> {
        Binding(get: { customDPI ? -1 : m.dpi }, set: { v in
            if v < 0 { customDPI = true } else { customDPI = false; m.dpi = v }
        })
    }
}

struct NumberField: View {
    let title: String
    @Binding var value: Int
    var unit = ""
    var range: ClosedRange<Int>
    var hint: String?
    var body: some View {
        LabeledContent {
            HStack(spacing: 6) {
                TextField(title, value: $value, format: .number.grouping(.never))
                    .labelsHidden().multilineTextAlignment(.trailing).frame(width: 84)
                if !unit.isEmpty { Text(unit).foregroundStyle(.secondary).frame(minWidth: 26, alignment: .leading) }
            }
        } label: {
            Text(title)
            if !range.contains(value) { Text("应为 \(range.lowerBound)–\(range.upperBound)").foregroundStyle(.red) }
            else if let hint { Text(hint) }
        }
    }
}

struct QualitySlider: View {
    @Binding var value: Int
    var title = "质量"
    /// Image tools keep 40…100; PDF compression accepts the engine's full 1…100.
    var range: ClosedRange<Int> = 40...100
    var body: some View {
        LabeledContent(title) {
            HStack {
                Slider(value: Binding(get: { Double(value) }, set: { value = Int($0) }), in: Double(range.lowerBound)...Double(range.upperBound), step: 1)
                Text("\(value)").monospacedDigit().frame(width: 28, alignment: .trailing)
            }
        }
    }
}

// MARK: - Preview

struct PreviewPane: View {
    enum Mode: Hashable { case original, result, compare }
    let item: QueueItem?
    @State private var mode = Mode.original
    @State private var resultIndex = 0

    /// Before/after slider (Squoosh / ImageOptim) for image-to-image results.
    var canCompare: Bool {
        guard let item, item.kind == .image, let out = item.outputs.first else { return false }
        return FileKind.imageExt.contains(out.pathExtension.lowercased())
    }

    var body: some View {
        VStack(spacing: 8) {
            if let item, !item.outputs.isEmpty {
                Picker("预览内容", selection: $mode) {
                    Text("原件").tag(Mode.original); Text("结果（\(item.outputs.count)）").tag(Mode.result)
                    if canCompare { Text("对比").tag(Mode.compare) }
                }.pickerStyle(.segmented).labelsHidden()
            }
            if mode == .compare, let item, let out = item.outputs.first {
                CompareView(original: item.url, result: out).frame(height: 300)
            } else {
                FilePreview(url: url).frame(height: 300)
            }
            if let item, let url {
                HStack(spacing: 6) {
                    if mode == .result && item.outputs.count > 1 {
                        Button { resultIndex -= 1 } label: { Image(systemName: "chevron.left") }.disabled(resultIndex == 0)
                        Text("\(resultIndex + 1) / \(item.outputs.count)").font(.caption.monospacedDigit())
                        Button { resultIndex += 1 } label: { Image(systemName: "chevron.right") }.disabled(resultIndex >= item.outputs.count - 1)
                    }
                    Text(mode == .compare ? "拖动中间的分隔线对比画质" : url.lastPathComponent).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button { NSWorkspace.shared.open(url) } label: { Image(systemName: "arrow.up.forward.app") }.help("用默认应用打开")
                    Button { NSWorkspace.shared.activateFileViewerSelecting([url]) } label: { Image(systemName: "folder") }.help("在 Finder 中显示")
                }.buttonStyle(.borderless)
            }
        }
        .onChange(of: item?.id) { resetMode() }
        .onChange(of: item?.outputs.count) { resetMode() }
    }

    func resetMode() {
        mode = (item?.outputs.isEmpty ?? true) ? .original : .result; resultIndex = 0
        #if CHAMO_DEBUG
        if ProcessInfo.processInfo.environment["CHAMO_COMPARE"] == "1" && canCompare { mode = .compare }
        #endif
    }

    var url: URL? {
        guard let item else { return nil }
        if mode != .original, !item.outputs.isEmpty { return item.outputs[min(resultIndex, item.outputs.count - 1)] }
        return item.url
    }
}

enum ImageThumb {
    /// Bounded, orientation-corrected decode off the main thread (never full-size in the UI).
    static func load(_ url: URL, maxPixel: Int) async -> NSImage? {
        let data = await Task.detached(priority: .utility) { () -> Data? in
            guard let s = CGImageSourceCreateWithURL(url as CFURL, nil),
                  let c = CGImageSourceCreateThumbnailAtIndex(s, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: maxPixel, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else { return nil }
            return NSBitmapImageRep(cgImage: c).representation(using: .png, properties: [:])
        }.value
        return data.flatMap(NSImage.init(data:))
    }
}

struct CompareView: View {
    let original: URL
    let result: URL
    @State private var before: NSImage?
    @State private var after: NSImage?
    @State private var split = 0.5

    var body: some View {
        GeometryReader { geo in
            ZStack {
                if let before, let after {
                    let fit = fitted(before.size, in: geo.size)
                    ZStack(alignment: .topLeading) {
                        Image(nsImage: after).resizable().frame(width: fit.width, height: fit.height)
                        Image(nsImage: before).resizable().frame(width: fit.width, height: fit.height)
                            .mask(alignment: .leading) { Rectangle().frame(width: fit.width * split) }
                        Rectangle().fill(.white).frame(width: 2, height: fit.height).shadow(radius: 1)
                            .offset(x: fit.width * split - 1)
                        Circle().fill(.white).frame(width: 22, height: 22).shadow(radius: 2)
                            .overlay(Image(systemName: "arrow.left.and.right").font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary))
                            .offset(x: fit.width * split - 11, y: fit.height / 2 - 11)
                        tag("原件 · " + size(original)).padding(6)
                        tag("结果 · " + size(result)).padding(6).frame(width: fit.width, alignment: .trailing)
                    }
                    .frame(width: fit.width, height: fit.height)
                    .contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 0).onChanged { split = min(1, max(0, $0.location.x / fit.width)) })
                    .accessibilityElement().accessibilityLabel("原件与结果对比").accessibilityValue("\(Int(split * 100))%")
                    .accessibilityAdjustableAction { split = min(1, max(0, split + ($0 == .increment ? 0.1 : -0.1))) }
                } else {
                    ProgressView().controlSize(.small)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .background(Color(nsColor: .underPageBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
        .task(id: result) {
            async let b = ImageThumb.load(original, maxPixel: 1600)
            async let a = ImageThumb.load(result, maxPixel: 1600)
            (before, after) = await (b, a)
        }
    }

    func tag(_ text: String) -> some View {
        Text(text).font(.caption2.weight(.medium)).padding(.horizontal, 6).padding(.vertical, 2).background(.black.opacity(0.55), in: Capsule()).foregroundStyle(.white)
    }
    func size(_ url: URL) -> String {
        ByteCountFormatter.string(fromByteCount: Int64((try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0), countStyle: .file)
    }
    func fitted(_ image: CGSize, in box: CGSize) -> CGSize {
        guard image.width > 0, image.height > 0 else { return box }
        let scale = min((box.width - 16) / image.width, (box.height - 16) / image.height)
        return CGSize(width: image.width * scale, height: image.height * scale)
    }
}

/// Real file content only: PDFKit for PDFs, ImageIO thumbnails for images, Quick Look for the rest.
struct FilePreview: View {
    let url: URL?
    @State private var image: NSImage?
    @State private var loading = false

    var body: some View {
        Group {
            if let url, url.pathExtension.lowercased() == "pdf" {
                PDFPagePreview(url: url)
            } else if let image {
                Image(nsImage: image).resizable().scaledToFit().padding(10)
            } else if loading {
                ProgressView().controlSize(.small)
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "eye.slash").font(.title2).foregroundStyle(.tertiary)
                    Text(url == nil ? "选中文件后在这里预览" : "此文件暂无法预览").font(.callout).foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .underPageBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
        .task(id: url) {
            image = nil
            guard let u = url, u.pathExtension.lowercased() != "pdf" else { return }
            loading = true; defer { loading = false }
            if FileKind.imageExt.contains(u.pathExtension.lowercased()) {
                // Bounded thumbnail instead of decoding full-size images in the UI.
                image = await ImageThumb.load(u, maxPixel: 900)
            } else {
                let request = QLThumbnailGenerator.Request(fileAt: u, size: CGSize(width: 600, height: 600), scale: 2, representationTypes: .thumbnail)
                image = try? await QLThumbnailGenerator.shared.generateBestRepresentation(for: request).nsImage
            }
        }
    }
}

struct PDFPagePreview: View {
    let url: URL
    @State private var document: PDFDocument?
    @State private var page = 0
    var body: some View {
        VStack(spacing: 0) {
            PDFKitView(document: document, page: page)
            if let document, document.pageCount > 0 {
                HStack(spacing: 10) {
                    Button { page -= 1 } label: { Image(systemName: "chevron.left") }.disabled(page == 0)
                    Text("第 \(page + 1) 页 / 共 \(document.pageCount) 页").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    Button { page += 1 } label: { Image(systemName: "chevron.right") }.disabled(page >= document.pageCount - 1)
                }.buttonStyle(.borderless).padding(.vertical, 6)
            } else if document == nil {
                Text("无法读取此 PDF（可能已加密或损坏）").font(.caption).foregroundStyle(.secondary).padding(6)
            }
        }
        .task(id: url) { page = 0; document = PDFDocument(url: url) }
    }
}

struct PDFKitView: NSViewRepresentable {
    let document: PDFDocument?
    let page: Int
    func makeNSView(context: Context) -> PDFView {
        let v = PDFView(); v.autoScales = true; v.displayMode = .singlePage; v.displaysPageBreaks = false
        v.backgroundColor = .clear; v.pageShadowsEnabled = true
        return v
    }
    func updateNSView(_ v: PDFView, context: Context) {
        if v.document !== document { v.document = document }
        if let p = document?.page(at: page), v.currentPage != p { v.go(to: p) }
    }
}

struct PresetChip: View {
    @EnvironmentObject var m: Workbench
    let preset: Workbench.Preset
    var body: some View {
        let active = preset.matches(m)
        Button { withAnimation(.easeOut(duration: 0.15)) { preset.apply(m) } } label: {
            VStack(spacing: 2) {
                Text(preset.title).font(.callout.weight(.medium))
                Text(preset.detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1).minimumScaleFactor(0.7)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 7).padding(.horizontal, 4)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(active ? Color.accentColor.opacity(0.16) : Color.primary.opacity(0.05)))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(active ? Color.accentColor : .clear, lineWidth: 1.5))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}
