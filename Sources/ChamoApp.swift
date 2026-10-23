import SwiftUI
import AppKit
import Quartz

struct ContentView: View {
    @EnvironmentObject var m: Workbench

    var body: some View {
        NavigationSplitView {
            Sidebar().navigationSplitViewColumnWidth(min: 200, ideal: 220, max: 280)
        } detail: {
            switch m.route {
            case .home: HomeView()
            case .history: HistoryView()
            case .tool: WorkspaceView()
            }
        }
        // Back to a flat minimum: the inspector is no longer a split-view column, so it
        // no longer adds its own 300 points here (see WorkspaceView). The earlier 680
        // variant assumed the crash was a total-width problem; Mac-DSH reproduced it at
        // a 1024-point window, where 680 + 300 = 980 already fits, so that hypothesis is
        // dead and the minimum stays as it was.
        .frame(minWidth: 980, minHeight: 640)
        .sheet(isPresented: $m.showFeedback) { FeedbackView(model: m) }
        .sheet(isPresented: $m.showPalette) { CommandPalette() }
        .sheet(isPresented: $m.showShortcuts) { ShortcutsSheet() }
        .sheet(isPresented: $m.showWhatsNew) { WhatsNewSheet() }
        .onAppear { m.checkWhatsNew(); m.checkForUpdates(manual: false) }
        .alert(m.updateMessage?.title ?? "", isPresented: Binding(get: { m.updateMessage != nil }, set: { if !$0 { m.updateMessage = nil } })) {
            if let page = m.updateMessage?.page {
                Button("前往下载") { NSWorkspace.shared.open(page) }
                Button("以后再说", role: .cancel) {}
            } else {
                Button("好", role: .cancel) {}
            }
        } message: { Text(m.updateMessage?.body ?? "") }
        .background(UndoBridge())
        .alert("Chamo", isPresented: $m.showAlert) { Button("好", role: .cancel) {} } message: { Text(m.alert) }
        .overlay(alignment: .bottom) { Toast().padding(.bottom, 76) }
    }
}

struct Sidebar: View {
    @EnvironmentObject var m: Workbench
    var body: some View {
        List(selection: m.routeBinding) {
            Label("开始", systemImage: "square.grid.2x2").tag(Route.home)
            ForEach(Tool.groups, id: \.0) { group in
                Section(group.0) {
                    ForEach(group.1) { tool in
                        Label { Text(tool.title) } icon: { Image(systemName: tool.symbol).foregroundStyle(tool.tint) }
                            // Mail-style counts: how many queued files this tool can take.
                            .badge(m.items.isEmpty ? 0 : m.compatibleCount(tool.id))
                            .opacity(m.running && tool.id != m.operation ? 0.45 : 1)
                            // Finder-sidebar style: drop files on a tool to open it with them.
                            .dropDestination(for: URL.self) { urls, _ in
                                guard !m.running, !urls.isEmpty else { return false }
                                m.open(tool.id); m.add(urls); return true
                            }
                            // The selection tag must be the outermost modifier, or List cannot select the row.
                            .tag(Route.tool(tool.id))
                    }
                }
            }
            Section("记录") {
                Label("历史记录", systemImage: "clock.arrow.circlepath")
                    .badge(m.histories.filter { $0.status.contains("中断") }.count)
                    .tag(Route.history)
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                Divider()
                Button { m.showFeedback = true } label: { Label("反馈与建议", systemImage: "bubble.left.and.text.bubble.right") }
                    .buttonStyle(.plain)
                HStack {
                    Label("本机处理 · v\(m.version)", systemImage: "lock.shield").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    SettingsLink { Image(systemName: "gearshape") }.buttonStyle(.borderless).help("设置（⌘,）")
                }
            }
            .padding(.horizontal, 14).padding(.bottom, 12)
        }
    }
}

/// Shown once per feature release, like Apple's apps: the few things worth discovering.
struct WhatsNewSheet: View {
    @EnvironmentObject var m: Workbench
    @Environment(\.dismiss) private var dismiss
    let features: [(String, Color, String, String)] = [
        ("square.and.arrow.down.on.square", .accentColor, "拖进来就推荐", "把任意文件拖到“开始”页或侧栏工具上，自动推荐能用的转换。"),
        ("magnifyingglass", .purple, "⌘K 搜索一切", "输入“压缩”“wps”“pdf 图片”即可找到工具；⌘1–9 直接切换，⌘/ 查看全部快捷键。"),
        ("bolt.fill", .orange, "批量快 20 倍", "上千个小文件几秒完成；大 PDF 逐页显示进度和剩余时间。"),
        ("slider.horizontal.below.square.and.square.filled", .teal, "预设与前后对比", "一键套用常用设置；图片结果可拖动分隔线对比画质。"),
        ("folder.badge.plus", .green, "更好找的结果", "讲义/讲义_001.png 式命名，可保存到原文件旁；完成后在 Finder 中直接选中结果。"),
    ]
    var body: some View {
        VStack(spacing: 22) {
            VStack(spacing: 6) {
                Text("Chamo 的新功能").font(.title.bold())
                Text("全部在本机处理，原件始终保留").foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 16) {
                ForEach(features, id: \.2) { symbol, tint, title, detail in
                    HStack(alignment: .top, spacing: 14) {
                        Image(systemName: symbol).font(.title2).foregroundStyle(tint).frame(width: 36)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(title).font(.headline)
                            Text(detail).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            Button { dismiss() } label: { Text("开始使用").frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
        }
        .padding(32).frame(width: 480)
    }
}

/// ⌘/ cheat sheet, grouped like the menus (Things / Slack style).
struct ShortcutsSheet: View {
    @Environment(\.dismiss) private var dismiss
    let groups: [(String, [(String, String)])] = [
        ("导航", [("⌘K", "搜索工具或操作"), ("⌘0", "开始（推荐操作）"), ("⌘1 – ⌘9", "切换到对应工具"), ("⇧⌘Y", "历史记录"), ("⌘,", "设置")]),
        ("文件", [("⌘O", "添加文件"), ("⇧⌘O", "添加文件夹"), ("空格 / ⌘Y", "快速查看所选"), ("⌫", "从列表移除所选（不删除文件）"), ("⌘Z", "撤销移除"), ("⌥⌘↑ / ⌥⌘↓", "合并时调整顺序")]),
        ("任务", [("⌘↩", "开始转换 / 完成后在 Finder 中显示"), ("⇧⌘P", "暂停或继续"), ("⌘.", "取消任务")]),
    ]
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack { Text("键盘快捷键").font(.title2.bold()); Spacer(); Button("完成") { dismiss() }.keyboardShortcut(.cancelAction) }
            ForEach(groups, id: \.0) { group in
                VStack(alignment: .leading, spacing: 6) {
                    Text(group.0).font(.headline).foregroundStyle(.secondary)
                    ForEach(group.1, id: \.0) { key, action in
                        HStack {
                            Text(key).font(.callout.monospaced()).padding(.horizontal, 6).padding(.vertical, 2)
                                .background(.quaternary, in: RoundedRectangle(cornerRadius: 5)).frame(width: 120, alignment: .leading)
                            Text(action)
                            Spacer()
                        }
                    }
                }
            }
        }
        .padding(24).frame(width: 460)
    }
}

/// Hands the window's undo manager to the model so list edits support ⌘Z.
struct UndoBridge: View {
    @EnvironmentObject var m: Workbench
    @Environment(\.undoManager) var undo
    var body: some View { Color.clear.onAppear { m.undoManager = undo }.onChange(of: undo) { m.undoManager = undo } }
}

/// Transient confirmation for non-blocking events (saved rules, refused drops).
struct Toast: View {
    @EnvironmentObject var m: Workbench
    var body: some View {
        if !m.notice.isEmpty {
            Text(m.notice).font(.callout).padding(.horizontal, 14).padding(.vertical, 8)
                .background(.regularMaterial, in: Capsule()).shadow(radius: 4, y: 2)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .task(id: m.notice) { try? await Task.sleep(for: .seconds(2.5)); withAnimation { m.notice = "" } }
        }
    }
}

// MARK: - History

struct HistoryView: View {
    @EnvironmentObject var m: Workbench
    @State private var query = ""

    /// Mail/Finder-style date groups, newest first.
    var groups: [(String, [History])] {
        let cal = Calendar.current, now = Date()
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        let list = q.isEmpty ? m.histories : m.histories.filter { ($0.operation + " " + $0.status + " " + $0.output).lowercased().contains(q) }
        var result: [(String, [History])] = []
        for h in list {
            let key = cal.isDateInToday(h.date) ? "今天" : cal.isDateInYesterday(h.date) ? "昨天"
                : (cal.dateComponents([.day], from: h.date, to: now).day ?? 99) < 7 ? "最近 7 天" : "更早"
            if let i = result.firstIndex(where: { $0.0 == key }) { result[i].1.append(h) } else { result.append((key, [h])) }
        }
        return result
    }

    var body: some View {
        Group {
            if m.histories.isEmpty {
                ContentUnavailableView("还没有任务记录", systemImage: "clock", description: Text("每次转换都会记录在这里，中断的任务可以核验后继续。"))
            } else if groups.isEmpty {
                ContentUnavailableView.search(text: query)
            } else {
                List {
                    ForEach(groups, id: \.0) { group in
                        Section(group.0) {
                            ForEach(group.1) { h in
                                HistoryRow(h: h).contextMenu {
                                    Button("打开输出文件夹") { m.reveal(h.output) }
                                    if !h.report.isEmpty { Button("查看报告") { m.reveal(h.report) } }
                                    Divider()
                                    Button("从记录中移除") { m.removeHistory(h.id) }.disabled(h.status == "运行中")
                                }
                            }
                        }
                    }
                }
                .listStyle(.inset(alternatesRowBackgrounds: false))
            }
        }
        .searchable(text: $query, placement: .toolbar, prompt: "搜索工具、状态或输出位置")
        .navigationTitle("历史记录")
        .navigationSubtitle(m.histories.isEmpty ? "恢复时会重新核对原件与已有结果" : "共 \(m.histories.count) 次任务 · 恢复时只补处理未完成的部分")
    }
}

struct HistoryRow: View {
    @EnvironmentObject var m: Workbench
    let h: History
    var tool: Tool? { Tool.all.first { $0.title == h.operation } ?? Tool.all.first { h.operation.contains($0.title.prefix(4)) } }
    var interrupted: Bool { h.status.contains("中断") || h.status.contains("取消") }
    var failed: Bool { h.status.contains("异常") && !h.status.contains("异常 0 项") }

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: tool?.symbol ?? "doc").font(.title3).foregroundStyle(tool?.tint ?? .secondary)
                .frame(width: 38, height: 38).background((tool?.tint ?? .gray).opacity(0.13), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(h.operation).font(.headline)
                    Text(h.date.formatted(.relative(presentation: .named))).font(.caption).foregroundStyle(.secondary)
                }
                Label(h.status, systemImage: interrupted ? "pause.circle.fill" : failed ? "exclamationmark.triangle.fill" : h.status == "运行中" ? "gearshape.fill" : "checkmark.circle.fill")
                    .font(.callout).foregroundStyle(interrupted ? .orange : failed ? .orange : .secondary)
                Text(h.output.replacingOccurrences(of: NSHomeDirectory(), with: "~")).font(.caption).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            if interrupted {
                Button("核验并继续") { m.resume(h) }.buttonStyle(.borderedProminent).disabled(m.running)
            }
            Button("打开输出") { m.reveal(h.output) }
            Menu {
                if !h.report.isEmpty { Button("查看报告") { m.reveal(h.report) } }
                if !interrupted { Button("核验并重新执行") { m.resume(h) }.disabled(m.running) }
            } label: { Image(systemName: "ellipsis") }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        }
        .padding(.vertical, 6)
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var m: Workbench
    // The setting itself is the state: reading it here is what redraws the window
    // the moment the picker changes, instead of on the next open.
    @AppStorage(L10n.defaultsKey) private var languageRaw: String = AppLanguage.followSystem.rawValue
    private var lang: String { (AppLanguage(rawValue: languageRaw) ?? .followSystem).resolved }

    var body: some View {
        TabView {
            Form {
                Section {
                    LabeledContent {
                        Picker("", selection: $languageRaw) {
                            ForEach(AppLanguage.allCases) { choice in
                                Text(choice.title(lang)).tag(choice.rawValue)
                            }
                        }
                        .labelsHidden()
                        .pickerStyle(.menu)
                    } label: {
                        Text(L10n.text("settings.language", lang))
                    }
                } footer: {
                    Text(L10n.text("settings.language.note", lang)).font(.caption).foregroundStyle(.secondary)
                }
                Section {
                    LabeledContent {
                        HStack {
                            Text(m.output.replacingOccurrences(of: NSHomeDirectory(), with: "~")).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary)
                            Button(L10n.text("settings.change", lang)) { m.chooseOutput() }
                        }
                    } label: {
                        Text(L10n.text("settings.outputLocation", lang))
                    }
                    Picker(L10n.text("settings.workers", lang), selection: $m.workers) {
                        Text(L10n.text("settings.workers.auto", lang, ["count": "\(Workbench.autoWorkers)"])).tag(0)
                        Divider()
                        ForEach(1...8, id: \.self) {
                            Text(L10n.text("settings.workers.count", lang, ["count": "\($0)"])).tag($0)
                        }
                    }
                    Toggle(L10n.text("settings.revealWhenDone", lang), isOn: $m.openWhenDone)
                    Picker(L10n.text("settings.saveLocation", lang), selection: $m.besideSource) {
                        Text(L10n.text("settings.saveLocation.fixedFolder", lang)).tag(false)
                        Text(L10n.text("settings.saveLocation.besideSource", lang)).tag(true)
                    }
                    Picker(L10n.text("settings.naming", lang), selection: $m.cleanNaming) {
                        Text(L10n.text("settings.naming.clean", lang)).tag(true)
                        Text(L10n.text("settings.naming.tagged", lang)).tag(false)
                    }
                    HStack {
                        Toggle(L10n.text("settings.autoUpdate", lang), isOn: $m.autoCheckUpdates)
                        Spacer()
                        Button(m.checkingUpdate ? L10n.text("settings.checking", lang)
                                                : L10n.text("settings.checkNow", lang)) {
                            m.checkForUpdates(manual: true)
                        }.disabled(m.checkingUpdate)
                    }
                } footer: {
                    Text(L10n.text("settings.generalFooter", lang)).font(.caption).foregroundStyle(.secondary)
                }
                Section { Button(L10n.text("settings.savePreset", lang)) { m.savePreset() } }
            }
            .formStyle(.grouped).disabled(m.running)
            .tabItem { Label(L10n.text("tab.general", lang), systemImage: "gearshape") }

            Form {
                Section {
                    TextField(L10n.text("feedback.repository", lang), text: $m.githubRepository,
                              prompt: Text(L10n.text("feedback.repositoryPlaceholder", lang)))
                    HStack {
                        Button(L10n.text("common.save", lang)) { m.saveRepository() }
                        Spacer()
                        Button(L10n.text("feedback.openWindow", lang)) { m.showFeedback = true }
                    }
                } header: { Text(L10n.text("feedback.sectionTitle", lang)) } footer: {
                    Text(L10n.text("feedback.footer", lang)).font(.caption).foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .tabItem { Label(L10n.text("tab.feedback", lang), systemImage: "bubble.left.and.text.bubble.right") }

            Form {
                Section {
                    LabeledContent { Text("\(m.version) · Apple Silicon") }
                        label: { Text(L10n.text("about.version", lang)) }
                    LabeledContent { Text(L10n.text("about.processingValue", lang)) }
                        label: { Text(L10n.text("about.processing", lang)) }
                    LabeledContent { Text(L10n.text("about.signingValue", lang)) }
                        label: { Text(L10n.text("about.signing", lang)) }
                } footer: {
                    Text(L10n.text("about.footer", lang)).font(.caption).foregroundStyle(.secondary)
                }
                Section { Button(L10n.text("about.openDocs", lang)) { m.reveal(Bundle.main.resourceURL!.appendingPathComponent("Documentation").path) } }
            }
            .formStyle(.grouped)
            .tabItem { Label(L10n.text("tab.about", lang), systemImage: "info.circle") }
        }
        .frame(width: 520, height: 400)
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: Workbench?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let m = model, m.running else { return .terminateNow }
        let a = NSAlert(); a.messageText = "转换仍在进行"; a.informativeText = "请先取消任务再退出。已完成的结果会保留，之后可在历史记录中核验并继续。"
        a.addButton(withTitle: "返回任务"); a.runModal(); return .terminateCancel
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { !(model?.running ?? false) }
    func application(_ app: NSApplication, open urls: [URL]) { Task { @MainActor in model?.add(urls) } }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()
    }

    /// Dock right-click menu: jump straight to a tool or add files (Safari/Xcode style).
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        let add = NSMenuItem(title: "添加文件…", action: #selector(dockAddFiles), keyEquivalent: ""); add.target = self; menu.addItem(add)
        let search = NSMenuItem(title: "搜索工具…", action: #selector(dockSearch), keyEquivalent: ""); search.target = self; menu.addItem(search)
        menu.addItem(.separator())
        for tool in Tool.all {
            let item = NSMenuItem(title: tool.title, action: #selector(dockOpenTool(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = tool.id
            item.image = NSImage(systemSymbolName: tool.symbol, accessibilityDescription: nil)
            item.isEnabled = !(model?.running ?? false) || model?.operation == tool.id
            menu.addItem(item)
        }
        return menu
    }
    @objc func dockAddFiles() { Task { @MainActor in NSApp.activate(); model?.chooseFiles() } }
    @objc func dockSearch() { Task { @MainActor in NSApp.activate(); model?.showPalette = true } }
    @objc func dockOpenTool(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        Task { @MainActor in NSApp.activate(); model?.open(id) }
    }

    /// Finder > Services > 用 Chamo 处理: queue the files and show tool suggestions.
    @objc func openFiles(_ pboard: NSPasteboard, userData: String?, error: AutoreleasingUnsafeMutablePointer<NSString?>) {
        let urls = (pboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
        guard !urls.isEmpty else { error.pointee = "没有可处理的文件" as NSString; return }
        Task { @MainActor in
            guard let m = model else { return }
            m.add(urls)
            if !m.running { m.route = .home }
            NSApp.activate()
        }
    }

    // Quick Look panel control (the app delegate sits at the end of the responder chain).
    override func acceptsPreviewPanelControl(_ panel: QLPreviewPanel!) -> Bool { true }
    override func beginPreviewPanelControl(_ panel: QLPreviewPanel!) { panel.dataSource = QuickLook.shared }
    override func endPreviewPanelControl(_ panel: QLPreviewPanel!) { panel.dataSource = nil }
}

@main struct ChamoApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = Workbench()
    #if CHAMO_DEBUG
    init() { if ProcessInfo.processInfo.environment["CHAMO_DARK"] == "1" { NSApplication.shared.appearance = NSAppearance(named: .darkAqua) } }
    #endif
    var body: some Scene {
        Window("Chamo", id: "main") {
            ContentView().environmentObject(model).onAppear {
                delegate.model = model
                #if CHAMO_DEBUG
                DebugHooks.run(model)
                #endif
            }
        }
        .defaultSize(width: 1320, height: 840)
        .commands {
            CommandGroup(after: .appInfo) {
                Button(model.checkingUpdate ? "正在检查更新…" : "检查更新…") { model.checkForUpdates(manual: true) }.disabled(model.checkingUpdate)
            }
            CommandGroup(replacing: .newItem) {
                Button("添加文件…") { model.chooseFiles() }.keyboardShortcut("o")
                Button("添加文件夹…") { model.chooseFiles(folder: true) }.keyboardShortcut("o", modifiers: [.command, .shift])
            }
            CommandMenu("工具") {
                Button("搜索工具…") { model.showPalette = true }.keyboardShortcut("k")
                Button("开始") { model.route = .home }.keyboardShortcut("0")
                Divider()
                ForEach(Array(Tool.all.enumerated()), id: \.element.id) { i, tool in
                    // ⌘1–⌘9 stay bound to the first nine tools. The tenth has no
                    // digit shortcut, and Character("10") would trap when the menu
                    // is built, so it gets a plain item instead.
                    if i < 9 {
                        Button(tool.title) { model.open(tool.id) }.keyboardShortcut(KeyEquivalent(Character("\(i + 1)")))
                    } else {
                        Button(tool.title) { model.open(tool.id) }
                    }
                }
                Divider()
                Button("历史记录") { model.route = .history }.keyboardShortcut("y", modifiers: [.command, .shift])
            }
            CommandMenu("任务") {
                Button("开始转换") { model.start() }.keyboardShortcut(.return, modifiers: .command).disabled(!model.blockers.isEmpty || model.running)
                Button(model.paused ? "继续" : "暂停") { model.command(model.paused ? "resume" : "pause") }.keyboardShortcut("p", modifiers: [.command, .shift]).disabled(!model.running)
                Button("取消任务") { model.command("cancel") }.keyboardShortcut(".", modifiers: .command).disabled(!model.running)
                Button("快速查看所选") { model.quickLook(model.items.filter { model.selection.contains($0.id) }.map { $0.outputs.first ?? $0.url }) }
                    .keyboardShortcut("y").disabled(model.selection.isEmpty)
                Divider()
                Button("清空列表") { model.clear() }.disabled(model.running || model.items.isEmpty)
                Button("选择输出位置…") { model.chooseOutput() }.disabled(model.running)
            }
            CommandGroup(replacing: .help) {
                Button("键盘快捷键") { model.showShortcuts = true }.keyboardShortcut("/")
                Button("反馈与建议…") { model.showFeedback = true }
                Button("使用说明") { model.reveal(Bundle.main.resourceURL!.appendingPathComponent("Documentation/USER_GUIDE.txt").path) }
            }
        }
        Settings { SettingsView().environmentObject(model) }
    }
}
