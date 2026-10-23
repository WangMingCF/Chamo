import SwiftUI
import AppKit
import UniformTypeIdentifiers
import QuickLookThumbnailing

struct WorkspaceView: View {
    @EnvironmentObject var m: Workbench
    @State private var targeted = false

    var body: some View {
        VStack(spacing: 0) {
            if m.items.isEmpty { DropZone(targeted: targeted) } else { FileTable() }
            Divider()
            ActionBar()
        }
        .navigationTitle(m.tool.title)
        .navigationSubtitle(subtitle)
        .toolbar { toolbar }
        // The inspector is attached one level up, to the NavigationSplitView in
        // ContentView. [Hypothesis, not yet confirmed: verify-tool-inspector-
        // constraint-20261023] Mounting it on the detail column here is believed to
        // make macOS abort with "more Update Constraints in Window passes than
        // there are views" the moment a tool page appears (even with no files, no
        // resize and no conversion), because the split view and the detail would
        // then both own the inspector's width constraints. The abort reproduces only
        // on macOS 27, not on this repo's CI runners, so the cause stays a
        // hypothesis until the Mac request reports back.
        .onDrop(of: [UTType.fileURL], isTargeted: $targeted) { providers in
            guard !m.running else { return false }
            for p in providers { _ = p.loadObject(ofClass: URL.self) { url, _ in if let url { Task { @MainActor in m.add([url]) } } } }
            return true
        }
        .overlay {
            if targeted && !m.items.isEmpty {
                RoundedRectangle(cornerRadius: 10).strokeBorder(Color.accentColor, lineWidth: 3)
                    .background(Color.accentColor.opacity(0.06)).padding(6).allowsHitTesting(false)
            }
        }
    }

    var subtitle: String {
        guard !m.items.isEmpty else { return m.tool.subtitle }
        let pages = m.processable.compactMap(\.pages).filter { $0 > 0 }.reduce(0, +)
        return "\(m.items.count) 项" + (pages > 0 && m.processable.contains { $0.kind == .pdf } ? " · 共 \(pages) 页" : "") + " · " + m.tool.subtitle
    }

    @ToolbarContentBuilder var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            if m.tool.ordered && m.items.count > 1 {
                Menu {
                    Button("按文件名排序") { m.sortNames() }
                    Divider()
                    Button("上移所选") { m.move(-1) }.keyboardShortcut(.upArrow, modifiers: [.command, .option])
                    Button("下移所选") { m.move(1) }.keyboardShortcut(.downArrow, modifiers: [.command, .option])
                } label: { Label("排列顺序", systemImage: "arrow.up.arrow.down") }
                .help("合并时按列表顺序排列").disabled(m.running)
            }
            Button { m.remove() } label: { Label("从列表移除", systemImage: "minus") }
                .help("从列表移除所选（不会删除文件）").disabled(m.selection.isEmpty || m.running)
            Menu {
                Button("添加文件…") { m.chooseFiles() }
                Button("添加文件夹…") { m.chooseFiles(folder: true) }
            } label: { Label("添加", systemImage: "plus") } primaryAction: { m.chooseFiles() }
                .help("添加文件（⌘O），或直接拖入窗口").disabled(m.running)
        }
        ToolbarItem(placement: .primaryAction) {
            Button { withAnimation { m.showInspector.toggle() } } label: { Label("设置面板", systemImage: "sidebar.trailing") }
                .help(m.showInspector ? "隐藏设置面板" : "显示设置面板")
        }
    }
}

// MARK: - Empty state

struct DropZone: View {
    @EnvironmentObject var m: Workbench
    let targeted: Bool
    var body: some View {
        VStack(spacing: 18) {
            ZStack {
                RoundedRectangle(cornerRadius: 20, style: .continuous).fill(m.tool.tint.opacity(0.14)).frame(width: 84, height: 84)
                Image(systemName: targeted ? "arrow.down.circle.fill" : m.tool.symbol).font(.system(size: 36, weight: .medium)).foregroundStyle(m.tool.tint)
                    .contentTransition(.symbolEffect(.replace))
            }
            VStack(spacing: 6) {
                Text(targeted ? "松开即可添加" : "拖入" + (m.tool.accepts.first?.isASCII == true ? " " : "") + m.tool.accepts).font(.title2.weight(.semibold))
                Text(m.operation == "rag" ? "选择资料根目录，将按原目录结构整理输出" : "也可以拖入文件夹，会自动找出其中可处理的文件")
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button { m.chooseFiles(folder: m.operation == "rag") } label: {
                    Text(m.operation == "rag" ? "选择文件夹…" : "选择文件…").frame(minWidth: 90)
                }.buttonStyle(.borderedProminent).keyboardShortcut("o")
                Button { m.chooseFiles(folder: m.operation != "rag") } label: {
                    Text(m.operation == "rag" ? "选择文件…" : "选择文件夹…").frame(minWidth: 90)
                }
            }.controlSize(.large)
            Label("全程本机处理，原件不会被修改", systemImage: "lock.shield").font(.caption).foregroundStyle(.tertiary).padding(.top, 6)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 2, dash: [7, 6]))
                .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.35))
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(targeted ? Color.accentColor.opacity(0.07) : .clear))
        }
        .padding(24)
        .animation(.easeOut(duration: 0.15), value: targeted)
    }
}

// MARK: - File table

enum RowFilter: Hashable { case all, failed, cancelled, done, skipped }

struct FileTable: View {
    @EnvironmentObject var m: Workbench
    @State private var sortOrder: [KeyPathComparator<QueueItem>] = []
    @State private var filter = RowFilter.all

    var body: some View {
        VStack(spacing: 0) {
            if showsFilter { filterBar; Divider() }
            table
        }
        .onChange(of: m.phase) { if m.phase == .running { filter = .all } }
    }

    // Mail/Transmit-style status filter, shown once there is something to filter by.
    var showsFilter: Bool { m.items.count > 1 && (m.phase != .idle || m.items.contains { m.incompatibility($0) != nil }) }
    var filterBar: some View {
        // A cancelled file is not a failure and must not be counted as one: the
        // run stopped, the sources are intact, and the count is what the user
        // reads as "how many things went wrong".
        let failed = m.items.filter { $0.status == .failed }.count
        let cancelled = m.items.filter { $0.status == .cancelled }.count
        let done = m.items.filter { $0.status == .done || $0.status == .resumed }.count
        let skipped = m.items.filter { $0.status == .ready && m.incompatibility($0) != nil }.count
        return HStack(spacing: 10) {
            Picker("显示", selection: $filter) {
                Text("全部 \(m.items.count)").tag(RowFilter.all)
                if done > 0 { Text("完成 \(done)").tag(RowFilter.done) }
                if failed > 0 { Text("异常 \(failed)").tag(RowFilter.failed) }
                if cancelled > 0 { Text("已取消 \(cancelled)").tag(RowFilter.cancelled) }
                if skipped > 0 { Text("不适用 \(skipped)").tag(RowFilter.skipped) }
            }
            .pickerStyle(.segmented).labelsHidden().fixedSize()
            Spacer()
            if filter == .skipped && skipped > 0 && !m.running {
                Button("从列表移除这 \(skipped) 项") { m.removeIncompatible(); filter = .all }.controlSize(.small)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 8)
    }

    var rows: [QueueItem] {
        switch filter {
        case .all: m.items
        case .failed: m.items.filter { $0.status == .failed }
        case .cancelled: m.items.filter { $0.status == .cancelled }
        case .done: m.items.filter { $0.status == .done || $0.status == .resumed }
        case .skipped: m.items.filter { $0.status == .ready && m.incompatibility($0) != nil }
        }
    }

    var table: some View {
        // Row numbers are looked up once per render, not per cell (large batches stay O(n)).
        let order = m.tool.ordered ? Dictionary(uniqueKeysWithValues: m.items.enumerated().map { ($1.id, $0 + 1) }) : [:]
        return Table(of: QueueItem.self, selection: $m.selection, sortOrder: $sortOrder) {
            TableColumn("名称", value: \.name) { item in NameCell(item: item, index: order[item.id]) }
                .width(min: 220, ideal: 360)
            TableColumn("页数", value: \.sortPages) { item in
                Text(pagesText(item)).foregroundStyle(.secondary).monospacedDigit()
            }.width(min: 56, ideal: 72)
            TableColumn("大小", value: \.size) { item in
                Text(item.size > 0 ? ByteCountFormatter.string(fromByteCount: item.size, countStyle: .file) : "—").foregroundStyle(.secondary).monospacedDigit()
            }.width(min: 60, ideal: 80)
            TableColumn("结果", value: \.sortResult) { item in ResultCell(item: item, compare: m.operation.hasPrefix("image_") || m.operation == "pdf_compress") }
                .width(min: 70, ideal: 120)
            TableColumn("状态") { item in StatusCell(item: item, reason: m.incompatibility(item)) }
                .width(min: 120, ideal: 200)
        } rows: {
            // Merge tools follow list order, so rows can be dragged into place.
            // Rows carry their id (for merge reordering) and, once converted, the result
            // file, so results can be dragged straight into Finder, Mail or chat apps.
            ForEach(rows) { item in
                TableRow(item).itemProvider {
                    let provider = NSItemProvider(object: item.id.uuidString as NSString)
                    if let url = item.outputs.first { provider.registerObject(url as NSURL, visibility: .all) }
                    return provider
                }
            }
                .dropDestination(for: String.self) { index, ids in m.reorder(ids.compactMap(UUID.init), to: index) }
        }
        .tableStyle(.inset(alternatesRowBackgrounds: false))
        .contextMenu(forSelectionType: UUID.self) { ids in
            let rows = m.items.filter { ids.contains($0.id) }
            if !rows.isEmpty {
                Button("快速查看") { m.quickLook(rows.map { $0.outputs.first ?? $0.url }) }.keyboardShortcut("y")
                Button("打开原件") { rows.forEach { NSWorkspace.shared.open($0.url) } }
                Button("在 Finder 中显示原件") { m.showInFinder(rows.map(\.url)) }
                let outputs = rows.flatMap(\.outputs)
                if !outputs.isEmpty {
                    Button("在 Finder 中显示结果") { m.showInFinder(outputs) }
                }
                let errors = rows.filter { !$0.detail.isEmpty }
                if !errors.isEmpty {
                    Button("拷贝异常说明") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(errors.map { "\($0.url.lastPathComponent)：\($0.detail)" }.joined(separator: "\n"), forType: .string)
                    }
                }
                Divider()
                if m.tool.ordered { Button("上移") { m.move(-1) }.disabled(m.running); Button("下移") { m.move(1) }.disabled(m.running); Divider() }
                Button("从列表移除") { m.remove(ids) }.disabled(m.running)
            }
        } primaryAction: { ids in
            m.items.filter { ids.contains($0.id) }.forEach { NSWorkspace.shared.open($0.outputs.first ?? $0.url) }
        }
        // Finder-style column sorting; it reorders the list itself, so merge order follows it.
        .onChange(of: sortOrder) { if !m.running { m.items.sort(using: sortOrder) } }
        .onDeleteCommand { m.remove() }
        .onKeyPress(.space) {
            let rows = m.items.filter { m.selection.contains($0.id) }
            guard !rows.isEmpty else { return .ignored }
            m.quickLook(rows.map { $0.outputs.first ?? $0.url }); return .handled
        }
    }

    func pagesText(_ item: QueueItem) -> String {
        if item.kind == .folder { return item.fileCount.map { "\($0) 个文件" } ?? "…" }
        if let p = item.pages { return item.kind == .image ? "1 张" : p < 0 ? "无法读取" : "\(p) 页" }
        return item.kind == .pdf ? "…" : "—"
    }
}

/// Output size, and for image tools the change versus the original (as in ImageOptim).
struct ResultCell: View {
    let item: QueueItem
    let compare: Bool
    var body: some View {
        if let out = item.outputBytes, out > 0 {
            HStack(spacing: 6) {
                Text(ByteCountFormatter.string(fromByteCount: out, countStyle: .file)).monospacedDigit()
                if compare, item.size > 0 {
                    let change = Double(out - item.size) / Double(item.size)
                    Text(change <= 0 ? "−\(min(99, Int((-change * 100).rounded())))%" : "+\(Int((change * 100).rounded()))%")
                        .font(.caption.monospacedDigit().weight(.medium))
                        .foregroundStyle(change < -0.005 ? .green : change > 0.005 ? .orange : .secondary)
                }
            }
        } else {
            Text("—").foregroundStyle(.tertiary)
        }
    }
}

struct NameCell: View {
    let item: QueueItem
    let index: Int?
    var body: some View {
        HStack(spacing: 10) {
            if let index { Text("\(index)").font(.caption.monospacedDigit()).foregroundStyle(.tertiary).frame(width: 18, alignment: .trailing) }
            Thumbnail(url: item.url, kind: item.kind).frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                Text(item.url.deletingLastPathComponent().path.replacingOccurrences(of: NSHomeDirectory(), with: "~"))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
            }
        }
        .padding(.vertical, 3)
        .help(item.url.path)
        .accessibilityElement(children: .combine)
    }
}

struct StatusCell: View {
    let item: QueueItem
    let reason: String?
    var body: some View {
        let (text, symbol, tint) = info
        HStack(spacing: 6) {
            if item.status == .running { ProgressView().controlSize(.small).scaleEffect(0.75).frame(width: 14, height: 14) }
            else { Image(systemName: symbol).foregroundStyle(tint) }
            VStack(alignment: .leading, spacing: 1) {
                Text(text).foregroundStyle(tint == .secondary ? .secondary : .primary)
                if item.status == .running && item.partTotal > 0 {
                    ProgressView(value: Double(item.partDone), total: Double(item.partTotal)).progressViewStyle(.linear).controlSize(.mini).frame(maxWidth: 140)
                }
                if let sub = subtitle { Text(sub).font(.caption).foregroundStyle(item.status == .failed ? .orange : .secondary).lineLimit(1).truncationMode(.tail) }
            }
        }
        .help(item.detail.isEmpty ? (reason ?? text) : item.detail)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel([text, subtitle].compactMap { $0 }.joined(separator: "，"))
    }
    var subtitle: String? {
        if item.status == .ready, let reason { return reason }
        if !item.detail.isEmpty { return item.detail }
        return nil
    }
    var info: (String, String, Color) {
        switch item.status {
        case .ready: reason == nil ? ("待转换", "circle", .secondary) : ("将跳过", "minus.circle", .secondary)
        case .queued: ("排队中", "clock", .secondary)
        case .running: (item.partTotal > 0 ? "处理中 \(item.partDone)/\(item.partTotal) 页" : "处理中…", "", .accentColor)
        case .done: (item.outputs.count > 1 ? "完成 · \(item.outputs.count) 个文件" : "完成", "checkmark.circle.fill", .green)
        case .resumed: ("已完成（核验复用）", "checkmark.circle", .green)
        case .failed: ("异常 · 原件保留", "exclamationmark.triangle.fill", .orange)
        case .cancelled: ("已取消", "xmark.circle", .secondary)
        }
    }
}

// MARK: - Bottom bar

struct ActionBar: View {
    @EnvironmentObject var m: Workbench
    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            switch m.phase {
            case .running: running
            case .finished: finished
            case .idle: idle
            }
        }
        .padding(.horizontal, 18).padding(.vertical, 12)
        .frame(minHeight: 64)
        .background(.bar)
    }

    // Idle: what will happen, where it goes, and one primary action.
    @ViewBuilder var idle: some View {
        VStack(alignment: .leading, spacing: 3) {
            if m.items.isEmpty {
                Text("尚未添加文件").foregroundStyle(.secondary)
            } else {
                Text(summary).font(.callout.weight(.medium))
                if m.processable.isEmpty, let best = m.suggestions.first {
                    // Wrong tool for these files: offer the best match instead of a dead end.
                    HStack(spacing: 6) {
                        Label("这些文件不适用\(m.tool.title)", systemImage: "exclamationmark.circle.fill").font(.caption).foregroundStyle(.orange)
                        Button("改用「\(best.tool.title)」（\(best.count) 项）") { m.open(best.tool.id) }.buttonStyle(.link).font(.caption)
                    }
                } else if let warning = m.blockers.first {
                    Label(warning, systemImage: "exclamationmark.circle.fill").font(.caption).foregroundStyle(.orange)
                } else {
                    Text([m.estimate, "原件保留"].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        Spacer(minLength: 12)
        OutputPicker()
        Button { m.start() } label: { Text("开始转换").frame(minWidth: 84) }
            .buttonStyle(.borderedProminent).controlSize(.large)
            .keyboardShortcut(.return, modifiers: .command)
            .disabled(!m.blockers.isEmpty)
            .help(m.blockers.first ?? "开始转换（⌘↩）")
    }

    var summary: String {
        let ok = m.processable.count, skip = m.items.count - ok
        var parts = ["\(ok) 项待转换"]
        if m.totalBytes > 0 { parts.append(ByteCountFormatter.string(fromByteCount: m.totalBytes, countStyle: .file)) }
        if skip > 0 { parts.append("\(skip) 项不适用，将跳过") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder var running: some View {
        Image(systemName: m.paused ? "pause.circle.fill" : "gearshape.2.fill").font(.title2).foregroundStyle(m.paused ? .orange : .accentColor)
            .symbolEffect(.rotate, isActive: !m.paused)
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(m.paused ? "已暂停" : m.total == 0 ? "正在扫描文件…" : "正在处理 \(min(m.done + 1, m.total)) / \(m.total)").font(.callout.weight(.medium))
                if !m.currentName.isEmpty && !m.paused { Text(m.currentName).font(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle) }
                Spacer(minLength: 0)
                TimelineView(.periodic(from: m.startedAt, by: 1)) { ctx in
                    Text(([m.eta.isEmpty ? nil : m.eta, "已用 " + Duration.seconds(ctx.date.timeIntervalSince(m.startedAt)).formatted(.time(pattern: .minuteSecond))] as [String?]).compactMap { $0 }.joined(separator: " · "))
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            ProgressView(value: m.fractionDone).progressViewStyle(.linear)
            if !m.controlNote.isEmpty { Text(m.controlNote).font(.caption).foregroundStyle(.orange) }
        }.frame(maxWidth: 640)
        Spacer(minLength: 12)
        Button(m.paused ? "继续" : "暂停") { m.command(m.paused ? "resume" : "pause") }
            .help(m.paused ? "恢复工作进程" : "暂停所有工作进程（不再占用 CPU 与磁盘）")
        Button("取消", role: .cancel) { m.command("cancel") }.help("停止排队并终止正在处理的文件；已完成的结果保留")
    }

    @ViewBuilder var finished: some View {
        let r = m.lastRun ?? RunSummary()
        let ok = r.failed == 0 && !r.cancelled
        Image(systemName: r.cancelled ? "stop.circle.fill" : ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
            .font(.title).foregroundStyle(r.cancelled ? Color.secondary : ok ? .green : .orange)
        VStack(alignment: .leading, spacing: 3) {
            Text(r.cancelled ? "已取消 · 完成的 \(r.success) 项已保留" : ok ? "全部完成 · 生成 \(r.outputs) 个文件" : "完成 \(r.success) 项 · \(r.failed) 项异常")
                .font(.callout.weight(.semibold))
            Text(detailLine(r)).font(.caption).foregroundStyle(.secondary)
        }
        Spacer(minLength: 12)
        if r.failed > 0 && !r.cancelled {
            Button("重试异常项") { m.retryFailed() }.help("已完成的结果会核验后复用，只重新处理异常的文件")
        }
        Button("新任务") { m.clear() }.help("清空列表，开始新的转换")
        Button("查看报告") { m.reveal(m.reportFile) }.disabled(r.report.isEmpty)
        Button { m.revealResults() } label: { Text("在 Finder 中显示").frame(minWidth: 96) }
            .help("在 Finder 中选中本次生成的结果")
            .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.return, modifiers: .command)
    }

    func detailLine(_ r: RunSummary) -> String {
        var parts: [String] = []
        if r.inputBytes > 0 && r.outputBytes > 0 && (m.operation.hasPrefix("image_") || m.operation == "pdf_compress") {
            let f = { ByteCountFormatter.string(fromByteCount: $0, countStyle: .file) }
            let saved = r.inputBytes - r.outputBytes
            parts.append(saved > 0 ? "\(f(r.inputBytes)) → \(f(r.outputBytes))，节省 \(Int(Double(saved) / Double(r.inputBytes) * 100))%" : "\(f(r.inputBytes)) → \(f(r.outputBytes))")
        }
        if r.failed > 0 { parts.append("异常文件原件保留，详情见列表或报告") }
        if m.excluded > 0 { parts.append("已排除 \(m.excluded) 个音视频") }
        if m.skipped > 0 { parts.append("跳过 \(m.skipped) 个不适用文件") }
        parts.append(r.seconds < 1 ? "用时不到 1 秒" : "用时 " + Duration.seconds(r.seconds).formatted(.units(allowed: [.minutes, .seconds], width: .narrow, fractionalPart: .hide)))
        return parts.joined(separator: " · ")
    }
}

struct OutputPicker: View {
    @EnvironmentObject var m: Workbench
    var spread: Bool { m.besideSource && m.besideFolder == nil && !m.processable.isEmpty }
    var label: String {
        if m.besideSource { return m.besideFolder.map { "原文件旁/" + $0.lastPathComponent } ?? (m.processable.isEmpty ? "原文件旁" : URL(fileURLWithPath: m.output).lastPathComponent) }
        return URL(fileURLWithPath: m.output).lastPathComponent
    }
    var body: some View {
        HStack(spacing: 6) {
            Text("输出到").font(.callout).foregroundStyle(.secondary)
            Menu {
                Picker("保存位置", selection: $m.besideSource) {
                    Text("固定文件夹：\(URL(fileURLWithPath: m.output).lastPathComponent)").tag(false)
                    Text("原文件旁（新建“\(Workbench.besideFolderName)”）").tag(true)
                }.pickerStyle(.inline)
                Divider()
                Button("更改固定文件夹…") { m.chooseOutput() }
                Button("在 Finder 中打开") {
                    let target = m.targetOutput
                    try? FileManager.default.createDirectory(atPath: target, withIntermediateDirectories: true)
                    m.reveal(target)
                }
                Toggle("完成后在 Finder 中显示结果", isOn: $m.openWhenDone)
                Divider()
                Text(m.targetOutput.replacingOccurrences(of: NSHomeDirectory(), with: "~"))
                if spread { Text("来源位于多个文件夹，本次使用固定文件夹") }
            } label: {
                Label(label, systemImage: spread ? "exclamationmark.triangle.fill" : "folder.fill")
            }
            .fixedSize()
            .help(spread ? "来源位于多个文件夹，本次使用固定文件夹：\(m.output)" : m.targetOutput)
        }
    }
}

/// Finder-style file thumbnail, generated by Quick Look off the main thread and cached.
struct Thumbnail: View {
    let url: URL
    let kind: FileKind
    @State private var image: NSImage?
    private static let cache = NSCache<NSURL, NSImage>()

    var body: some View {
        Group {
            if let image { Image(nsImage: image).resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 3)).shadow(color: .black.opacity(0.15), radius: 0.5, y: 0.5) }
            else { Image(systemName: kind.symbol).font(.system(size: 18)).foregroundStyle(kind.tint) }
        }
        .task(id: url) {
            guard kind != .folder && kind != .media else { return }
            if let cached = Self.cache.object(forKey: url as NSURL) { image = cached; return }
            let request = QLThumbnailGenerator.Request(fileAt: url, size: CGSize(width: 30, height: 30), scale: 2, representationTypes: .thumbnail)
            if let rep = try? await QLThumbnailGenerator.shared.generateBestRepresentation(for: request) {
                Self.cache.setObject(rep.nsImage, forKey: url as NSURL); image = rep.nsImage
            }
        }
    }
}
