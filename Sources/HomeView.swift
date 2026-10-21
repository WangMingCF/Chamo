import SwiftUI
import UniformTypeIdentifiers

/// Drop-first start page: add any files, then pick from tools ranked by how many
/// of them they can process (Smallpdf / iLovePDF / WPS 转换中心 style).
struct HomeView: View {
    @EnvironmentObject var m: Workbench
    @State private var targeted = false
    private let columns = [GridItem(.adaptive(minimum: 220, maximum: 320), spacing: 12)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                if m.items.isEmpty { dropArea } else { queueSummary }
                if !m.items.isEmpty && !m.suggestions.isEmpty {
                    section("推荐操作", subtitle: "按可处理的文件数量排序") {
                        LazyVGrid(columns: columns, spacing: 12) {
                            ForEach(m.suggestions.prefix(4), id: \.tool.id) { s in ToolCard(tool: s.tool, badge: "适用 \(s.count) 项", prominent: true) }
                        }
                    }
                }
                ForEach(Tool.groups, id: \.0) { group in
                    let tools = m.items.isEmpty ? group.1 : group.1.filter { t in !m.suggestions.prefix(4).contains { $0.tool.id == t.id } }
                    if !tools.isEmpty {
                        section(m.items.isEmpty ? group.0 : "其他 · \(group.0)", subtitle: nil) {
                            LazyVGrid(columns: columns, spacing: 12) {
                                ForEach(tools) { t in ToolCard(tool: t, badge: badge(t), prominent: false) }
                            }
                        }
                    }
                }
            }
            .padding(28)
            .frame(maxWidth: 1100)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("开始")
        .navigationSubtitle(m.items.isEmpty ? "拖入文件，或选择一个工具" : m.kindSummary)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button { m.showPalette = true } label: { Label("搜索工具", systemImage: "magnifyingglass") }.help("搜索工具（⌘K）")
                Menu {
                    Button("添加文件…") { m.chooseFiles() }
                    Button("添加文件夹…") { m.chooseFiles(folder: true) }
                } label: { Label("添加", systemImage: "plus") } primaryAction: { m.chooseFiles() }
            }
        }
        .onDrop(of: [UTType.fileURL], isTargeted: $targeted) { providers in
            for p in providers { _ = p.loadObject(ofClass: URL.self) { url, _ in if let url { Task { @MainActor in m.add([url]) } } } }
            return true
        }
        .overlay {
            if targeted && !m.items.isEmpty {
                RoundedRectangle(cornerRadius: 10).strokeBorder(Color.accentColor, lineWidth: 3).background(Color.accentColor.opacity(0.06)).padding(6).allowsHitTesting(false)
            }
        }
    }

    var dropArea: some View {
        VStack(spacing: 14) {
            Image(systemName: targeted ? "arrow.down.circle.fill" : "square.and.arrow.down.on.square")
                .font(.system(size: 40, weight: .regular)).foregroundStyle(Color.accentColor)
                .contentTransition(.symbolEffect(.replace))
            Text(targeted ? "松开即可添加" : "把文件拖到这里").font(.title2.weight(.semibold))
            Text("PDF、Office / WPS 文档、图片或整个文件夹都可以，会自动推荐能用的转换").foregroundStyle(.secondary)
            HStack(spacing: 10) {
                Button { m.chooseFiles() } label: { Text("选择文件…").frame(minWidth: 90) }.buttonStyle(.borderedProminent).keyboardShortcut("o")
                Button { m.chooseFiles(folder: true) } label: { Text("选择文件夹…").frame(minWidth: 90) }
            }.controlSize(.large)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 44)
        .background {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 2, dash: [7, 6]))
                .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.35))
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(targeted ? Color.accentColor.opacity(0.07) : Color.clear))
        }
        .animation(.easeOut(duration: 0.15), value: targeted)
    }

    var queueSummary: some View {
        HStack(spacing: 16) {
            ZStack {
                RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.accentColor.opacity(0.12)).frame(width: 56, height: 56)
                Image(systemName: "tray.full.fill").font(.title2).foregroundStyle(Color.accentColor)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("已添加 \(m.items.count) 项").font(.title3.weight(.semibold))
                Text(m.kindSummary).foregroundStyle(.secondary)
            }
            Spacer()
            Button("继续添加…") { m.chooseFiles() }
            Button("清空") { m.clear() }
        }
        .padding(16)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    func badge(_ t: Tool) -> String? {
        guard !m.items.isEmpty, let s = m.suggestions.first(where: { $0.tool.id == t.id }) else { return nil }
        return "适用 \(s.count) 项"
    }

    func section<Content: View>(_ title: String, subtitle: String?, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title).font(.headline)
                if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
            }
            content()
        }
    }
}

struct ToolCard: View {
    @EnvironmentObject var m: Workbench
    let tool: Tool
    let badge: String?
    let prominent: Bool
    @State private var hover = false

    var body: some View {
        Button { m.open(tool.id) } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: tool.symbol).font(.title3).foregroundStyle(tool.tint)
                    .frame(width: 40, height: 40).background(tool.tint.opacity(0.14), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(tool.title).font(.body.weight(.semibold)).foregroundStyle(.primary)
                        if let badge { Text(badge).font(.caption2.weight(.semibold)).padding(.horizontal, 6).padding(.vertical, 2).background(tool.tint.opacity(0.16), in: Capsule()).foregroundStyle(tool.tint) }
                    }
                    Text(tool.subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .padding(12)
            .frame(maxWidth: .infinity, minHeight: 72, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(hover ? Color.primary.opacity(0.06) : Color.primary.opacity(prominent ? 0.04 : 0.025)))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(prominent ? tool.tint.opacity(0.35) : Color.primary.opacity(0.08)))
            .contentShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(tool.notice)
    }
}

// MARK: - ⌘K tool search (Raycast / Spotlight style)

struct CommandPalette: View {
    @EnvironmentObject var m: Workbench
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var index = 0
    @FocusState private var focused: Bool

    struct Entry: Identifiable { let id: String; let title: String; let detail: String; let symbol: String; let tint: Color; let keywords: String; let run: () -> Void }

    var entries: [Entry] {
        var list = Tool.all.map { t in
            Entry(id: t.id, title: t.title, detail: t.subtitle, symbol: t.symbol, tint: t.tint, keywords: t.keywords, run: { m.open(t.id) })
        }
        list += [
            Entry(id: "home", title: "开始", detail: "拖入文件并获取推荐操作", symbol: "square.grid.2x2", tint: .accentColor, keywords: "home 首页 推荐", run: { m.route = .home }),
            Entry(id: "history", title: "历史记录", detail: "查看、核验并继续以前的任务", symbol: "clock.arrow.circlepath", tint: .secondary, keywords: "history 历史 恢复", run: { m.route = .history }),
            Entry(id: "add", title: "添加文件…", detail: "从磁盘选择文件", symbol: "plus", tint: .secondary, keywords: "add open 打开 添加", run: { m.chooseFiles() }),
            Entry(id: "output", title: "选择输出位置…", detail: m.output.replacingOccurrences(of: NSHomeDirectory(), with: "~"), symbol: "folder", tint: .secondary, keywords: "output 输出 目录", run: { m.chooseOutput() }),
            Entry(id: "shortcuts", title: "键盘快捷键", detail: "查看全部快捷键（⌘/）", symbol: "keyboard", tint: .secondary, keywords: "shortcut keyboard 快捷键 按键", run: { m.showShortcuts = true }),
            Entry(id: "update", title: "检查更新…", detail: "查询 GitHub 上的最新正式版本", symbol: "arrow.down.circle", tint: .secondary, keywords: "update 更新 新版本 升级", run: { m.checkForUpdates(manual: true) }),
            Entry(id: "whatsnew", title: "新功能介绍", detail: "查看这个版本的新功能", symbol: "sparkles", tint: .secondary, keywords: "new whatsnew 新功能 更新", run: { m.showWhatsNew = true }),
            Entry(id: "feedback", title: "反馈与建议", detail: "在 GitHub 提交问题或建议", symbol: "bubble.left.and.text.bubble.right", tint: .secondary, keywords: "feedback github 反馈 bug", run: { m.showFeedback = true }),
        ]
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return list }
        let terms = q.split(separator: " ").map(String.init)
        return list.filter { e in
            let hay = (e.title + " " + e.detail + " " + e.keywords).lowercased()
            return terms.allSatisfy { hay.contains($0) }
        }
    }

    var body: some View {
        let results = entries
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("搜索工具或操作，例如 “pdf 图片”、“压缩”、“wps”", text: $query)
                    .textFieldStyle(.plain).font(.title3).focused($focused)
                    .onSubmit { run(results) }
                    .onKeyPress(.downArrow) { index = min(index + 1, max(0, results.count - 1)); return .handled }
                    .onKeyPress(.upArrow) { index = max(index - 1, 0); return .handled }
                    .onKeyPress(.escape) { dismiss(); return .handled }
            }
            .padding(14)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 2) {
                        ForEach(Array(results.enumerated()), id: \.element.id) { i, e in
                            HStack(spacing: 12) {
                                Image(systemName: e.symbol).foregroundStyle(e.tint).frame(width: 28, height: 28)
                                    .background(e.tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 7))
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(e.title).fontWeight(.medium)
                                    Text(e.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer()
                                if let n = Tool.all.firstIndex(where: { $0.id == e.id }), n < 9 { Text("⌘\(n + 1)").font(.caption.monospaced()).foregroundStyle(.tertiary) }
                            }
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .background(i == index ? Color.accentColor.opacity(0.18) : .clear, in: RoundedRectangle(cornerRadius: 8))
                            .contentShape(Rectangle())
                            .onTapGesture { index = i; run(results) }
                            .id(e.id)
                        }
                        if results.isEmpty { Text("没有匹配的工具").foregroundStyle(.secondary).padding(24) }
                    }
                    .padding(8)
                }
                .onChange(of: index) { if results.indices.contains(index) { proxy.scrollTo(results[index].id) } }
            }
            .frame(height: 340)
        }
        .frame(width: 560)
        .onAppear { focused = true }
        .onChange(of: query) { index = 0 }
    }

    func run(_ results: [Entry]) {
        guard results.indices.contains(index) else { return }
        let entry = results[index]
        dismiss()
        DispatchQueue.main.async { entry.run() }
    }
}
