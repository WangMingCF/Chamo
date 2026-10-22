import SwiftUI
import AppKit
import PDFKit
import Combine
@preconcurrency import UserNotifications

// MARK: - File kinds

enum FileKind: Equatable {
    case pdf, word, slides, sheet, image, media, folder, other
    static let imageExt: Set<String> = ["jpg","jpeg","jfif","png","bmp","gif","tif","tiff","webp","heic","heif","avif"]
    static let mediaExt: Set<String> = ["ts","mp4","mov","mkv","avi","flv","wmv","mpg","mpeg","webm","mp3","wav","aac","amr","flac","m4a","ogg","opus","wma"]

    init(url: URL) {
        if (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true { self = .folder; return }
        let ext = url.pathExtension.lowercased()
        switch ext {
        case "pdf": self = .pdf
        // Must match OFFICE in engine/workbench.py.
        case "doc","docx","docm","dot","dotx","odt","rtf","txt","wps","wpt": self = .word
        case "ppt","pptx","pptm","pps","ppsx","pot","potx","odp","odg","dps","dpt": self = .slides
        case "xls","xlsx","xlsm","xlsb","xlt","xltx","ods","csv","et","ett": self = .sheet
        default: self = FileKind.imageExt.contains(ext) ? .image : FileKind.mediaExt.contains(ext) ? .media : .other
        }
    }
    var isOffice: Bool { self == .word || self == .slides || self == .sheet }
    var symbol: String {
        switch self {
        case .pdf: "doc.richtext.fill"
        case .word: "doc.text.fill"
        case .slides: "play.rectangle.fill"
        case .sheet: "tablecells.fill"
        case .image: "photo.fill"
        case .media: "film.fill"
        case .folder: "folder.fill"
        case .other: "doc.fill"
        }
    }
    var tint: Color {
        switch self {
        case .pdf: .red
        case .word: .blue
        case .slides: .orange
        case .sheet: .green
        case .image: .teal
        case .folder: .accentColor
        case .media, .other: .gray
        }
    }
    func name(_ url: URL) -> String {
        switch self {
        case .pdf: "PDF"
        case .word: "文字文档"
        case .slides: "演示文稿"
        case .sheet: "表格"
        case .image: url.pathExtension.uppercased() + " 图片"
        case .media: "音视频"
        case .folder: "文件夹"
        case .other: url.pathExtension.isEmpty ? "文件" : url.pathExtension.uppercased()
        }
    }
}

// MARK: - Tools

struct Tool: Identifiable {
    let id: String
    let title: String
    let subtitle: String
    let symbol: String
    let tint: Color
    /// Shown in the drop zone, e.g. "拖入 PDF 文件".
    let accepts: String
    let notice: String
    /// Merge tools process files in list order, so the order is user-editable.
    var ordered = false

    static let all: [Tool] = [
        Tool(id:"pdf_images", title:"PDF 转图片", subtitle:"把每一页导出为 PNG 或 JPEG", symbol:"photo.on.rectangle.angled", tint:.red, accepts:"PDF 文件",
             notice:"逐页静态导出：图片不含原 PDF 的文字层、链接和附件。"),
        Tool(id:"pdf_split", title:"PDF 拆分", subtitle:"按页数和文件大小双重限制拆分", symbol:"scissors", tint:.red, accepts:"PDF 文件",
             notice:"严格拆分：无法验证内容完整保留的 PDF 会列为异常，不会降级处理。"),
        Tool(id:"pdf_merge", title:"PDF 合并", subtitle:"按列表顺序合并为一个 PDF", symbol:"doc.on.doc", tint:.red, accepts:"PDF 文件",
             notice:"含表单、附件、结构标签或交互批注的 PDF 暂不合并，会列为异常。", ordered:true),
        Tool(id:"office_pdf", title:"Office 转 PDF", subtitle:"Word、PPT、Excel、WPS 文档本地转为 PDF", symbol:"doc.text", tint:.blue, accepts:"Office 或 WPS 文档",
             notice:"本地 LibreOffice 渲染：缺失字体会被替代；动画、交互和演讲者备注不进入 PDF。"),
        Tool(id:"office_images", title:"Office 转图片", subtitle:"PPT、Word 逐页导出为图片", symbol:"rectangle.stack", tint:.orange, accepts:"PPT、Word、Excel 或 WPS 文档",
             notice:"本地渲染为逐页图片：请复核字体与版式；动画、备注和嵌入媒体不进入图片。"),
        Tool(id:"images_pdf", title:"图片合成 PDF", subtitle:"多张图片按顺序合成一个 PDF", symbol:"doc.richtext", tint:.teal, accepts:"图片",
             notice:"图片原样嵌入 PDF，不重新压缩；多帧图片（如 GIF 动图）会列为异常。", ordered:true),
        Tool(id:"image_convert", title:"图片格式转换", subtitle:"PNG、JPEG、WebP、TIFF 互转，支持读取 HEIC、AVIF", symbol:"arrow.triangle.2.circlepath", tint:.teal, accepts:"图片",
             notice:"EXIF 方向会规范化；多帧图片不会被静默丢帧。"),
        Tool(id:"image_compress", title:"图片压缩", subtitle:"无损优化，或主动选择降低质量与尺寸", symbol:"arrow.down.right.and.arrow.up.left", tint:.teal, accepts:"图片",
             notice:"默认只做 PNG 无损优化。降低质量或尺寸需要你主动开启。"),
        Tool(id:"rag", title:"RAG 预处理", subtitle:"整理知识库资料：保持目录、严格拆分、生成上传清单", symbol:"books.vertical", tint:.purple, accepts:"资料文件夹",
             notice:"严格保真：保持目录结构，Word / PPT 保留原格式不转 PDF，音视频自动排除；逐项校验通过才进入上传清单。"),
        // Appended (not inserted) so the existing tools keep ⌘1–⌘9: there is no ⌘10,
        // and shifting a middle tool would silently rebind every shortcut after it.
        Tool(id:"pdf_compress", title:"PDF 压缩", subtitle:"两档压缩：无损清理，或把每页渲染成图片的有损重采样", symbol:"arrow.down.right.and.arrow.up.left", tint:.red, accepts:"PDF 文件",
             notice:"无损清理只重写 PDF 内部结构，页面内容不变，但部分文件可能没有收益（此时保留原样副本）；有损重采样会把每页渲染成 JPEG 再重建 PDF，文字层、链接和表单不再保留。"),
    ]
    static let groups: [(String, [Tool])] = [
        ("PDF", all.filter { $0.id.hasPrefix("pdf_") }),
        ("Office", all.filter { $0.id.hasPrefix("office_") }),
        ("图片", all.filter { $0.id.hasPrefix("image") }),
        ("知识库", all.filter { $0.id == "rag" }),
    ]
    static func by(_ id: String) -> Tool { all.first { $0.id == id } ?? all[0] }
    /// Extra search terms for the ⌘K palette (English, abbreviations, common phrasings).
    var keywords: String {
        switch id {
        case "pdf_images": "pdf image png jpg jpeg 导出 截图 转换 页面"
        case "pdf_split": "pdf split 分割 切分 分片 大小 页数"
        case "pdf_merge": "pdf merge combine 合并 拼接"
        case "office_pdf": "word ppt excel wps docx pptx xlsx pdf 导出 转换 et dps"
        case "office_images": "ppt pptx word 幻灯片 课件 图片 png jpg 导出"
        case "images_pdf": "image photo jpg png pdf 合成 扫描 拼接"
        case "image_convert": "image heic avif webp png jpg jpeg tiff bmp 格式 转换"
        case "image_compress": "image compress 压缩 瘦身 减小 体积 webp jpeg"
        case "pdf_compress": "pdf compress 压缩 瘦身 减小 体积 无损 有损 重采样"
        case "rag": "rag 知识库 上传 预处理 dify fastgpt 拆分 清单"
        default: ""
        }
    }
}

enum Fidelity {
    case verified, rendered, derived, lossy
    var title: String {
        switch self {
        case .verified: "内容校验"
        case .rendered: "静态渲染"
        case .derived: "派生导出"
        case .lossy: "有损 · 已主动选择"
        }
    }
    var detail: String {
        switch self {
        case .verified: "逐项核对内容完整，不通过的文件列为异常"
        case .rendered: "按页面外观导出，交互与文字层等可能变化"
        case .derived: "生成新文件，像素与内容尽量保持不变"
        case .lossy: "画质或尺寸会降低，原件不受影响"
        }
    }
    var symbol: String {
        switch self {
        case .verified: "checkmark.shield.fill"
        case .rendered: "photo.fill"
        case .derived: "arrow.triangle.branch"
        case .lossy: "exclamationmark.triangle.fill"
        }
    }
    var tint: Color {
        switch self {
        case .verified: .green
        case .rendered: .blue
        case .derived: .secondary
        case .lossy: .orange
        }
    }
}

// MARK: - Page ranges

enum PageRange {
    /// Mirrors engine `pages()`: 1-based, comma separated, no overlap, within bounds.
    static func count(_ spec: String, total: Int) throws -> Int {
        let trimmed = spec.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return total }
        var seen = Set<Int>()
        for part in trimmed.split(separator: ",", omittingEmptySubsequences: false) {
            let nums = part.split(separator: "-", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
            guard nums.count <= 2, let a = Int(nums[0]), let b = Int(nums[nums.count - 1]) else { throw Message("格式应为 1-5,8,12-20") }
            guard 1 <= a, a <= b, b <= total else { throw Message("页码超出范围 1–\(total)") }
            for n in a...b where !seen.insert(n).inserted { throw Message("第 \(n) 页重复") }
        }
        return seen.count
    }
}

struct Message: LocalizedError { let text: String; init(_ t: String) { text = t }; var errorDescription: String? { text } }

// MARK: - Queue

struct QueueItem: Identifiable {
    enum Status { case ready, queued, running, done, resumed, failed, cancelled }
    let id = UUID()
    let url: URL
    let kind: FileKind
    var size: Int64 = 0
    var pages: Int?
    var fileCount: Int?
    var status = Status.ready
    var detail = ""
    var outputs: [URL] = []
    var outputBytes: Int64?
    /// Page progress of a running multi-page export, from engine "progress" events.
    var partDone = 0, partTotal = 0
    // Sort keys for the table columns.
    var name: String { url.lastPathComponent }
    var sortPages: Int { kind == .folder ? (fileCount ?? 0) : (pages ?? 0) }
    var sortResult: Int64 { outputBytes ?? -1 }

    init(url: URL) {
        self.url = url
        kind = FileKind(url: url)
        size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map { Int64($0) } ?? 0
        if kind == .image { pages = 1 }
    }
}

struct History: Identifiable, Codable {
    var id: String; var date: Date; var operation: String; var status: String; var request: String; var report: String; var output: String
}

struct RunSummary {
    var success = 0, failed = 0, outputs = 0
    /// Sizes of the sources that produced output and of those outputs, for the savings line.
    var inputBytes: Int64 = 0, outputBytes: Int64 = 0
    var seconds = 0.0
    var cancelled = false
    var report = ""
}

enum Route: Hashable { case home, tool(String), history }
enum Phase { case idle, running, finished }

// MARK: - Workbench

@MainActor final class Workbench: ObservableObject {
    @Published var route = Route.home
    @Published var showPalette = false
    @Published var showShortcuts = false
    @Published var showWhatsNew = false
    /// Off by default: the app makes no network request unless asked to.
    @Published var autoCheckUpdates = false
    @Published var updateMessage: (title: String, body: String, page: URL?)?
    @Published var checkingUpdate = false

    /// Manual checks always report; automatic ones (opt-in, at most daily) only report a new version.
    func checkForUpdates(manual: Bool) {
        guard !checkingUpdate, let repo = try? GitHubRepository(githubRepository) else {
            if manual { fail("请先在设置中填写有效的 GitHub 仓库地址") }; return
        }
        if !manual {
            let last = UserDefaults.standard.double(forKey: "lastUpdateCheck")
            guard autoCheckUpdates, Date().timeIntervalSince1970 - last > 86_400 else { return }
        }
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "lastUpdateCheck")
        checkingUpdate = true
        Task {
            defer { checkingUpdate = false }
            do {
                let release = try await UpdateCheck.latest(repo)
                switch try UpdateCheck.evaluate(current: version, release: release, repository: repo) {
                case .available(let v, let page, let summary):
                    updateMessage = ("有新版本 \(v)", "当前版本 \(version)。\n\n" + summary, page)
                case .upToDate(let current):
                    if manual { updateMessage = ("已是最新版本", "Chamo \(current) 是目前的最新正式版本。", nil) }
                }
            } catch {
                if manual { updateMessage = ("无法检查更新", error.localizedDescription, nil) }
            }
        }
    }
    /// Bump when a release has features worth introducing; shown once per bump.
    static let whatsNewEdition = 2
    func checkWhatsNew() {
        let seen = UserDefaults.standard.integer(forKey: "whatsNewSeen")
        // New installs and upgrades both see it once; it never reappears for the same edition.
        if seen < Workbench.whatsNewEdition {
            UserDefaults.standard.set(Workbench.whatsNewEdition, forKey: "whatsNewSeen")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { self.showWhatsNew = true }
        }
    }
    weak var undoManager: UndoManager?
    @Published var operation = "pdf_images"
    @Published var items: [QueueItem] = []
    @Published var selection = Set<UUID>()
    @Published var showInspector = true

    @Published var format = "png"; @Published var dpi = 150; @Published var quality = 90
    @Published var paper = "original"; @Published var marginMM = 10
    @Published var useRange = false; @Published var pageRange = ""
    @Published var pdfPages = 100; @Published var pdfMB = 100
    @Published var imageMB = 20; @Published var documentMB = 150; @Published var textMB = 10; @Published var tableMB = 10; @Published var tableRows = 100000; @Published var batchFiles = 50
    /// 0 = automatic (see effectiveWorkers).
    @Published var workers = 0; @Published var lossy = false; @Published var maxEdge = 0
    /// PDF compression has its own two-mode settings: its lossy defaults (150 DPI / 75)
    /// must not leak into, or inherit stale values from, the image/Office export tools.
    @Published var compressLossy = false; @Published var compressDPI = 150; @Published var compressQuality = 75
    @Published var output = ""
    /// Save next to the sources (WPS/Permute default) instead of the fixed folder.
    @Published var besideSource = false
    /// The folder the current/last run actually writes to.
    @Published var runOutput = ""

    @Published var phase = Phase.idle
    @Published var paused = false
    @Published var controlNote = ""
    @Published var done = 0; @Published var total = 0
    @Published var currentName = ""
    @Published var startedAt = Date()
    @Published var excluded = 0; @Published var skipped = 0
    @Published var lastRun: RunSummary?
    @Published var report = ""

    @Published var histories: [History] = []
    @Published var alert = ""; @Published var showAlert = false
    @Published var showFeedback = false; @Published var githubRepository = ""
    @Published var notice = ""
    @Published var openWhenDone = false
    /// Clean names (讲义/讲义_001.png) vs the legacy tagged names with a task fingerprint.
    @Published var cleanNaming = true
    @Published var eta = ""
    private var lastRequest: [String: Any]?
    private var persistence: AnyCancellable?

    private var process: Process?; private var input: Pipe?; private var runID = ""
    private var buffer = Data()
    private var pump: EventPump?
    let storage: URL

    init() {
        githubRepository = UserDefaults.standard.string(forKey: "githubRepository") ?? (Bundle.main.object(forInfoDictionaryKey: "GitHubRepositoryURL") as? String ?? "")
        #if CHAMO_DEBUG
        // Automated UI runs keep history and records out of the user's real folder.
        storage = ProcessInfo.processInfo.environment["CHAMO_APP_SUPPORT"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Chamo")
        #else
        storage = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Chamo")
        #endif
        try? FileManager.default.createDirectory(at: storage, withIntermediateDirectories: true)
        if let d = try? Data(contentsOf: storage.appendingPathComponent("history.json")), let h = try? JSONDecoder().decode([History].self, from: d) { histories = h }
        for i in histories.indices where histories[i].status == "运行中" { histories[i].status = "已中断 · 可恢复" }
        output = UserDefaults.standard.string(forKey: "outputPath")
            ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("Chamo输出").path
        restoreSettings()
        if let data = UserDefaults.standard.data(forKey: "preset"), let p = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            pdfPages = p["pdf_pages"] as? Int ?? 100; pdfMB = (p["pdf_bytes"] as? Int ?? 100000000) / 1000000
            batchFiles = p["batch_files"] as? Int ?? 50
            imageMB = (p["image_bytes"] as? Int ?? 20000000) / 1000000; documentMB = (p["document_bytes"] as? Int ?? 150000000) / 1000000
            textMB = (p["text_bytes"] as? Int ?? 10000000) / 1000000; tableMB = (p["table_bytes"] as? Int ?? 10000000) / 1000000; tableRows = p["table_rows"] as? Int ?? 100000
        }
        // Remember the last-used options (like Permute/WPS) without a separate save step.
        persistence = objectWillChange.debounce(for: .milliseconds(600), scheduler: RunLoop.main).sink { [weak self] in self?.persistSettings() }
    }

    private static let settingsKey = "ui-settings-v1"
    private func persistSettings() {
        let d: [String: Any] = ["operation": operation, "format": format, "dpi": dpi, "quality": quality, "paper": paper, "margin": marginMM,
                                "workers": workers, "openWhenDone": openWhenDone, "inspector": showInspector, "cleanNaming": cleanNaming, "besideSource": besideSource, "autoCheckUpdates": autoCheckUpdates,
                                "compressLossy": compressLossy, "compressDPI": compressDPI, "compressQuality": compressQuality]
        UserDefaults.standard.set(d, forKey: Self.settingsKey)
    }
    private func restoreSettings() {
        guard let d = UserDefaults.standard.dictionary(forKey: Self.settingsKey) else { return }
        if let op = d["operation"] as? String, Tool.all.contains(where: { $0.id == op }) { operation = op }
        format = d["format"] as? String ?? format; dpi = d["dpi"] as? Int ?? dpi; quality = d["quality"] as? Int ?? quality
        paper = d["paper"] as? String ?? paper; marginMM = d["margin"] as? Int ?? marginMM
        compressLossy = d["compressLossy"] as? Bool ?? compressLossy; compressDPI = d["compressDPI"] as? Int ?? compressDPI; compressQuality = d["compressQuality"] as? Int ?? compressQuality
        workers = d["workers"] as? Int ?? workers; openWhenDone = d["openWhenDone"] as? Bool ?? false; showInspector = d["inspector"] as? Bool ?? true; cleanNaming = d["cleanNaming"] as? Bool ?? true; besideSource = d["besideSource"] as? Bool ?? false; autoCheckUpdates = d["autoCheckUpdates"] as? Bool ?? false
    }

    /// Automatic worker count: leave two cores for the system and the UI, at most six.
    static let autoWorkers = min(6, max(2, ProcessInfo.processInfo.activeProcessorCount - 2))

    /// Protocol version this build speaks; engine/protocol.py owns the number.
    /// Sending it lets the engine refuse a mismatch instead of guessing, and is
    /// what makes the versioning real rather than CLI-only.
    static let engineProtocol = 1
    var effectiveWorkers: Int { workers > 0 ? workers : Workbench.autoWorkers }

    var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "未知" }
    var tool: Tool { Tool.by(operation) }
    var running: Bool { phase == .running }
    var selected: QueueItem? { items.first { selection.contains($0.id) } ?? items.first }
    var python: URL { Bundle.main.resourceURL!.appendingPathComponent("python/bin/python3") }
    var engine: URL { Bundle.main.resourceURL!.appendingPathComponent("engine/workbench.py") }
    func fail(_ text: String) { alert = text; showAlert = true }

    // MARK: Navigation

    /// Sidebar selection. Switching tools mid-run is refused: the queue belongs to the running task.
    var routeBinding: Binding<Route?> {
        Binding(get: { self.route }, set: { new in
            guard let new else { return }
            if case .tool(let id) = new {
                if self.running && id != self.operation { NSSound.beep(); return }
                if id != self.operation { self.switchTool(id) }
            }
            self.route = new
        })
    }

    /// Opens a tool from the home screen, palette or menu, keeping the queued files.
    func open(_ id: String) {
        guard !running || id == operation else { NSSound.beep(); return }
        if id != operation { switchTool(id) }
        route = .tool(id)
    }

    func switchTool(_ id: String) {
        operation = id
        // A page range typed for one export must not silently carry over to another tool.
        useRange = false; pageRange = ""
        if id == "image_compress" { lossy = false; maxEdge = 0; format = "jpeg" }
        else if id == "image_convert" { lossy = false; maxEdge = 0 }
        else if !["png","jpeg"].contains(format) { format = "png" }
        if id == "pdf_images" || id == "office_images" { if !["png","jpeg"].contains(format) { format = "png" } }
        resetStatuses()
    }

    func resetStatuses() {
        guard !running else { return }
        if phase == .finished { phase = .idle; lastRun = nil }
        for i in items.indices { items[i].status = .ready; items[i].detail = ""; items[i].outputs = [] }
    }

    // MARK: Compatibility

    var acceptsPNGOnly: Bool { operation == "image_compress" && !lossy }

    /// Why a tool (default: the current one) cannot process this item, or nil when it can.
    func incompatibility(_ item: QueueItem, tool: String? = nil) -> String? {
        let operation = tool ?? self.operation
        switch item.kind {
        case .folder: return nil  // the engine skips unreadable files inside folders and reports the count
        case .media: return "音视频暂不支持"
        default: break
        }
        switch operation {
        case "pdf_images", "pdf_split", "pdf_merge", "pdf_compress": return item.kind == .pdf ? nil : "仅支持 PDF"
        case "office_pdf", "office_images": return item.kind.isOffice ? nil : "仅支持 Office / WPS 文档"
        case "images_pdf", "image_convert": return item.kind == .image ? nil : "仅支持图片"
        case "image_compress":
            guard item.kind == .image else { return "仅支持图片" }
            // Other tools' counts assume the user may pick lossy compression there.
            if tool == nil && acceptsPNGOnly && item.url.pathExtension.lowercased() != "png" { return "无损优化仅支持 PNG，可改用「质量 / 尺寸调整」" }
            return nil
        default: return nil
        }
    }
    var processable: [QueueItem] { items.filter { incompatibility($0) == nil } }
    func compatibleCount(_ tool: String) -> Int { items.filter { incompatibility($0, tool: tool) == nil }.count }

    /// Tools ranked by how many queued files they can process (the "drop first, choose later"
    /// flow of Smallpdf / WPS 转换中心). RAG is suggested only for folders, its natural input.
    var suggestions: [(tool: Tool, count: Int)] {
        let hasFolder = items.contains { $0.kind == .folder }
        return Tool.all.compactMap { t in
            if t.id == "rag" && !hasFolder { return nil }
            let n = items.filter { $0.kind != .folder && incompatibility($0, tool: t.id) == nil }.count + (hasFolder && t.id == "rag" ? 1 : 0)
            // Merging needs at least two files (a folder may hold several).
            if t.ordered && n < 2 && !hasFolder { return nil }
            return n > 0 ? (t, n) : nil
        }.sorted { $0.count > $1.count }
    }

    /// "2 个 PDF · 3 张图片" style breakdown of the queue.
    var kindSummary: String {
        var counts: [(String, Int)] = []
        for item in items {
            let label = switch item.kind {
                case .pdf: "个 PDF"; case .word: "份文字文档"; case .slides: "份演示文稿"; case .sheet: "份表格"
                case .image: "张图片"; case .folder: "个文件夹"; case .media: "个音视频"; case .other: "个其他文件"
            }
            if let i = counts.firstIndex(where: { $0.0 == label }) { counts[i].1 += 1 } else { counts.append((label, 1)) }
        }
        return counts.map { "\($0.1) \($0.0)" }.joined(separator: " · ")
    }

    // MARK: Presets (one-click setting bundles, as in Permute)

    struct Preset: Identifiable { let id: String; let title: String; let detail: String; let apply: (Workbench) -> Void; let matches: (Workbench) -> Bool }
    var presets: [Preset] {
        switch operation {
        case "pdf_images", "office_images": [
            Preset(id: "screen", title: "屏幕阅读", detail: "JPEG · 96 DPI", apply: { $0.format = "jpeg"; $0.dpi = 96; $0.quality = 85 }, matches: { $0.format == "jpeg" && $0.dpi == 96 }),
            Preset(id: "standard", title: "标准", detail: "PNG · 150 DPI", apply: { $0.format = "png"; $0.dpi = 150 }, matches: { $0.format == "png" && $0.dpi == 150 }),
            Preset(id: "print", title: "高清打印", detail: "PNG · 300 DPI", apply: { $0.format = "png"; $0.dpi = 300 }, matches: { $0.format == "png" && $0.dpi == 300 }),
        ]
        case "image_compress": [
            Preset(id: "lossless", title: "无损", detail: "PNG 像素不变", apply: { $0.lossy = false; $0.maxEdge = 0 }, matches: { !$0.lossy }),
            Preset(id: "web", title: "网页", detail: "JPEG 80 · 1920px", apply: { $0.lossy = true; $0.format = "jpeg"; $0.quality = 80; $0.maxEdge = 1920 }, matches: { $0.lossy && $0.format == "jpeg" && $0.quality == 80 && $0.maxEdge == 1920 }),
            Preset(id: "tiny", title: "极致压缩", detail: "WebP 70 · 1600px", apply: { $0.lossy = true; $0.format = "webp"; $0.quality = 70; $0.maxEdge = 1600 }, matches: { $0.lossy && $0.format == "webp" && $0.quality == 70 && $0.maxEdge == 1600 }),
        ]
        case "image_convert": [
            Preset(id: "jpeg", title: "转 JPEG", detail: "质量 90", apply: { $0.format = "jpeg"; $0.quality = 90; $0.lossy = false; $0.maxEdge = 0 }, matches: { $0.format == "jpeg" && !$0.lossy }),
            Preset(id: "png", title: "转 PNG", detail: "无损", apply: { $0.format = "png"; $0.lossy = false; $0.maxEdge = 0 }, matches: { $0.format == "png" && !$0.lossy }),
            Preset(id: "webp", title: "转 WebP", detail: "无损", apply: { $0.format = "webp"; $0.lossy = false; $0.maxEdge = 0 }, matches: { $0.format == "webp" && !$0.lossy }),
        ]
        case "images_pdf": [
            Preset(id: "original", title: "原图尺寸", detail: "不缩放", apply: { $0.paper = "original" }, matches: { $0.paper == "original" }),
            Preset(id: "a4", title: "A4 文档", detail: "边距 10 mm", apply: { $0.paper = "a4"; $0.marginMM = 10 }, matches: { $0.paper == "a4" && $0.marginMM == 10 }),
        ]
        case "pdf_split", "rag": [
            Preset(id: "default", title: "常规", detail: "100 页 · 100 MB", apply: { $0.pdfPages = 100; $0.pdfMB = 100 }, matches: { $0.pdfPages == 100 && $0.pdfMB == 100 }),
            Preset(id: "small", title: "小分片", detail: "50 页 · 20 MB", apply: { $0.pdfPages = 50; $0.pdfMB = 20 }, matches: { $0.pdfPages == 50 && $0.pdfMB == 20 }),
        ]
        default: []
        }
    }

    // MARK: Validation & estimates

    var rangeApplies: Bool { ["pdf_images","office_images","office_pdf"].contains(operation) }
    var formatOptions: [(String, String)] {
        switch operation {
        case "pdf_images", "office_images": [("png","PNG"),("jpeg","JPEG")]
        case "image_convert": [("png","PNG"),("jpeg","JPEG"),("webp","WebP"),("tiff","TIFF"),("bmp","BMP")]
        // PNG has no quality knob and is the lossless mode's format; offering it
        // for a lossy request is exactly the combination the engine rejects.
        case "image_compress": lossy ? [("jpeg","JPEG"),("webp","WebP")] : []
        default: []
        }
    }
    var showsQuality: Bool {
        switch operation {
        case "pdf_images", "office_images": format == "jpeg"
        case "image_convert": format == "jpeg" || (format == "webp" && lossy)
        case "image_compress": lossy && ["jpeg","webp"].contains(format)
        default: false
        }
    }

    /// Validation message for the page range against the PDFs in the queue.
    var rangeStatus: Result<String, Message>? {
        guard rangeApplies, useRange else { return nil }
        if pageRange.trimmingCharacters(in: .whitespaces).isEmpty { return .failure(Message("请输入页码，例如 1-5,8")) }
        let pdfs = processable.filter { ($0.pages ?? 0) > 0 && $0.kind == .pdf }
        guard let shortest = pdfs.min(by: { $0.pages! < $1.pages! }) else {
            do { _ = try PageRange.count(pageRange, total: 100000); return .success("转换时按实际页数校验") }
            catch let e as Message { return .failure(e) } catch { return .failure(Message("页码格式无效")) }
        }
        do {
            let n = try PageRange.count(pageRange, total: shortest.pages!)
            return .success(pdfs.count > 1 ? "每个文件导出 \(n) 页" : "导出 \(n) 页")
        } catch let e as Message {
            return .failure(pdfs.count > 1 ? Message("「\(shortest.url.lastPathComponent)」只有 \(shortest.pages!) 页：\(e.text)") : e)
        } catch { return .failure(Message("页码格式无效")) }
    }

    /// Reasons the start button is disabled; empty means ready.
    var blockers: [String] {
        var out: [String] = []
        if items.isEmpty { return ["先添加文件"] }
        if processable.isEmpty { out.append("列表中没有\(tool.title)可处理的文件") }
        if (operation == "pdf_merge" || operation == "images_pdf") && !processable.contains(where: { $0.kind == .folder }) && processable.count < 2 { out.append("至少需要 2 个文件才能合并") }
        if case .failure(let e)? = rangeStatus { out.append(e.text) }
        let checks: [(Bool, String)] = [
            ((36...600).contains(dpi) || !["pdf_images","office_images"].contains(operation), "分辨率应为 36–600 DPI"),
            ((1...100).contains(quality) || !showsQuality, "质量应为 1–100"),
            ((0...50).contains(marginMM), "边距应为 0–50 mm"),
            ((0...16000).contains(maxEdge), "最长边应为 0–16000 px"),
            ((1...10000).contains(pdfPages), "每份页数应为 1–10000"),
            (pdfMB >= 1, "每份大小至少 1 MB"),
            ([imageMB, documentMB, textMB, tableMB, tableRows].allSatisfy { $0 > 0 }, "单文件上限必须大于 0"),
            ((1...1000).contains(batchFiles), "每批文件数应为 1–1000"),
            ((0...8).contains(workers), "工作进程应为 1–8"),
        ]
        out += checks.filter { !$0.0 }.map { $0.1 }
        if operation == "pdf_compress" && compressLossy {
            if !(72...600).contains(compressDPI) { out.append("分辨率应为 72–600 DPI") }
            if !(1...100).contains(compressQuality) { out.append("质量应为 1–100") }
        }
        if operation == "image_compress" {
            if lossy {
                // quality only reaches the JPEG/WebP encoders; a lossy PNG request
                // would be rejected by the engine, so never let it start.
                if !["jpeg","webp"].contains(format) { out.append("有损压缩的输出格式需为 JPEG 或 WebP") }
            } else if maxEdge > 0 {
                // The lossless path preserves pixels; the engine refuses a resize.
                out.append("无损优化不支持缩小尺寸；请选择「降低质量 / 尺寸」")
            }
        }
        if operation == "image_convert" && maxEdge > 0 && !lossy { out.append("缩小尺寸需要先开启「允许缩小尺寸」") }
        return out
    }

    var totalBytes: Int64 { processable.reduce(0) { $0 + $1.size } }

    /// A short "what you'll get" line; nil when it cannot be stated honestly.
    var estimate: String? {
        let ok = processable
        guard !ok.isEmpty else { return nil }
        let hasFolder = ok.contains { $0.kind == .folder }
        switch operation {
        case "pdf_images":
            guard !hasFolder, ok.allSatisfy({ ($0.pages ?? 0) > 0 }) else { return "每页生成一张 \(format.uppercased()) 图片" }
            let n = ok.reduce(0) { sum, item in sum + ((try? PageRange.count(useRange ? pageRange : "", total: item.pages!)) ?? 0) }
            return "预计生成 \(n) 张 \(format.uppercased()) 图片"
        case "office_images": return "每页生成一张 \(format.uppercased()) 图片"
        case "office_pdf": return hasFolder ? "每个文档生成一个 PDF" : "预计生成 \(ok.count) 个 PDF"
        case "pdf_merge":
            let pages = ok.compactMap(\.pages).filter { $0 > 0 }.reduce(0, +)
            return hasFolder || pages == 0 ? "合并为 1 个 PDF" : "合并为 1 个 PDF，共 \(pages) 页"
        case "images_pdf": return hasFolder ? "合并为 1 个 PDF" : "合并为 1 个 PDF，共 \(ok.count) 页"
        case "pdf_split": return "按每份 ≤\(pdfPages) 页且 ≤\(pdfMB) MB 拆分"
        case "pdf_compress":
            return compressLossy ? "有损重采样：每页按 \(compressDPI) DPI 渲染为 JPEG（文字层不保留）" : "无损清理：页面内容不变；部分文件可能没有收益"
        case "image_convert", "image_compress": return hasFolder ? "每张图片生成一个结果" : "预计生成 \(ok.count) 张图片"
        default: return nil
        }
    }

    var fidelity: Fidelity {
        // The lossless rewrite re-checks page count but does not verify every
        // content stream, so it must not claim 「内容校验」.
        if operation == "pdf_compress" { return compressLossy ? .lossy : .derived }
        if lossy || (operation == "image_convert" && format == "jpeg") { return .lossy }
        switch operation {
        case "rag", "pdf_split", "image_compress": return .verified
        case "pdf_images", "office_pdf", "office_images": return .rendered
        default: return .derived
        }
    }

    /// Example output name, following the engine's naming rule (name + suffix + 8-char task fingerprint).
    func exampleName(for item: QueueItem?) -> (String, String) {
        let fallback: String = switch operation {
            case "office_pdf", "office_images": "课件.pptx"
            case "images_pdf", "image_convert", "image_compress": "照片.png"
            default: "讲义.pdf"
        }
        let name = item.flatMap { $0.kind == .folder ? nil : $0.url.lastPathComponent } ?? fallback
        if cleanNaming {
            let base = (name as NSString).deletingPathExtension
            let ext = { (f: String) in f == "jpeg" ? "jpg" : f }
            let pages = item?.pages.flatMap { $0 > 0 ? $0 : nil } ?? 12
            let pad = String(repeating: "0", count: max(3, String(pages).count) - 1) + "1"
            let result: String = switch operation {
                case "pdf_images", "office_images": "\(base)/\(base)_\(pad).\(ext(format == "jpeg" ? "jpeg" : "png"))"
                case "office_pdf": "\(base).pdf"
                case "pdf_merge", "images_pdf": "\(base)_合并.pdf"
                case "image_compress": lossy ? "\(base).\(ext(format))" : name
                case "image_convert": "\(base).\(ext(format))"
                case "pdf_compress": "\(base)_压缩.pdf"
                default: name
            }
            return (name, result)
        }
        let fp = "a1b2c3d4"
        let result: String = switch operation {
            case "pdf_images", "office_images": "\(name)_page00001_\(fp).\(format == "jpeg" ? "jpeg" : "png")"
            case "office_pdf": "\(name)_\(fp).pdf"
            case "pdf_merge", "images_pdf": "\(name)_merged_\(fp).pdf"
            case "image_compress": lossy ? "\(name)_\(fp).\(format)" : "\(name)_optimized_\(fp).png"
            case "image_convert": "\(name)_\(fp).\(format)"
            case "pdf_compress": "\((name as NSString).deletingPathExtension)_压缩_\(fp).pdf"
            default: name
        }
        return (name, result)
    }

    // MARK: Queue editing

    func add(_ urls: [URL]) {
        guard !running else { notice = "任务运行中，结束后再添加文件"; return }
        if phase == .finished { resetStatuses() }
        // Files inside the output folder are results, not inputs (the engine rejects them too).
        let roots = [output, runOutput].filter { !$0.isEmpty }.map { URL(fileURLWithPath: $0).standardizedFileURL.resolvingSymlinksInPath().path + "/" }
        let inOutput = urls.filter { u in
            let path = u.standardizedFileURL.resolvingSymlinksInPath().path
            // Also skip any earlier "save beside source" result folder.
            return roots.contains { path.hasPrefix($0) } || u.pathComponents.contains(Workbench.besideFolderName)
        }
        let urls = urls.filter { u in !inOutput.contains(u) }
        if !inOutput.isEmpty { notice = "已忽略 \(inOutput.count) 个位于输出文件夹中的文件" }
        var added: [QueueItem] = []
        for u in urls where !items.contains(where: { $0.url.standardizedFileURL == u.standardizedFileURL }) && !added.contains(where: { $0.url == u }) {
            added.append(QueueItem(url: u))
        }
        items += added
        if selection.isEmpty, let first = added.first { selection = [first.id] }
        if !added.isEmpty, inOutput.isEmpty, case .tool = route {
            let skip = added.filter { incompatibility($0) != nil }.count
            notice = "已添加 \(added.count) 项" + (skip > 0 ? "，其中 \(skip) 项不适用\(tool.title)" : "")
        }
        for item in added { loadDetails(item) }
    }

    /// Page counts and folder sizes load off the main thread so large batches stay responsive.
    private func loadDetails(_ item: QueueItem) {
        let id = item.id, url = item.url, kind = item.kind
        guard kind == .pdf || kind == .folder else { return }
        Task.detached(priority: .utility) {
            // -1 marks a PDF PDFKit cannot open (damaged or encrypted); the engine reports the exact reason.
            let pages = kind == .pdf ? (PDFDocument(url: url)?.pageCount ?? -1) : nil
            let folder = kind == .folder ? Workbench.folderStats(url) : nil
            await MainActor.run {
                guard let i = self.items.firstIndex(where: { $0.id == id }) else { return }
                if let pages { self.items[i].pages = pages }
                if let folder { self.items[i].fileCount = folder.files; self.items[i].size = folder.bytes }
            }
        }
    }

    nonisolated static func folderStats(_ url: URL) -> (files: Int, bytes: Int64) {
        var n = 0, bytes: Int64 = 0
        guard let e = FileManager.default.enumerator(at: url, includingPropertiesForKeys: [.fileSizeKey, .isRegularFileKey], options: [.skipsHiddenFiles]) else { return (0, 0) }
        while let f = e.nextObject() as? URL, n < 200_000 {
            let v = try? f.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            if v?.isRegularFile == true { n += 1; bytes += Int64(v?.fileSize ?? 0) }
        }
        return (n, bytes)
    }

    static let besideFolderName = "Chamo输出"
    /// "<common parent>/Chamo输出" when every input shares one folder; nil when sources are spread out.
    var besideFolder: URL? {
        let parents = Set(processable.map { $0.url.deletingLastPathComponent().standardizedFileURL.path })
        guard parents.count == 1, let parent = parents.first else { return nil }
        return URL(fileURLWithPath: parent).appendingPathComponent(Workbench.besideFolderName)
    }
    var targetOutput: String { besideSource ? (besideFolder?.path ?? output) : output }

    func chooseFiles(folder: Bool = false) {
        let p = NSOpenPanel(); p.allowsMultipleSelection = true; p.canChooseDirectories = folder; p.canChooseFiles = !folder
        p.prompt = "添加"; p.message = folder ? "选择要处理的文件夹，将保持其中的目录结构" : "选择要\(tool.title)的\(tool.accepts)"
        p.begin { r in if r == .OK { Task { @MainActor in self.add(p.urls) } } }
    }
    func chooseOutput() {
        let p = NSOpenPanel(); p.canChooseDirectories = true; p.canChooseFiles = false; p.canCreateDirectories = true
        p.prompt = "选择"; p.message = "转换结果将保存到这个文件夹，原件不会被修改"
        p.directoryURL = URL(fileURLWithPath: output).deletingLastPathComponent()
        p.begin { r in if r == .OK, let u = p.url { Task { @MainActor in self.output = u.path; UserDefaults.standard.set(u.path, forKey: "outputPath") } } }
    }
    func remove(_ ids: Set<UUID>? = nil) {
        guard !running else { return }
        let target = ids ?? selection
        // Finder-style ⌘Z: removal only edits the list, so it can always be restored.
        let removed = items.enumerated().filter { target.contains($0.element.id) }
        guard !removed.isEmpty else { return }
        items.removeAll { target.contains($0.id) }
        selection.subtract(target)
        if items.isEmpty { phase = .idle; lastRun = nil }
        undoManager?.registerUndo(withTarget: self) { m in
            for (i, item) in removed { m.items.insert(item, at: min(i, m.items.count)) }
            m.selection = Set(removed.map(\.element.id))
        }
        undoManager?.setActionName("移除 \(removed.count) 项")
    }
    func removeIncompatible() { remove(Set(items.filter { incompatibility($0) != nil }.map(\.id))) }
    func clear() { guard !running else { return }; remove(Set(items.map(\.id))); phase = .idle; lastRun = nil }
    /// Drag-reorder for merge tools; other tools ignore it because order does not matter there.
    func reorder(_ ids: [UUID], to index: Int) {
        guard tool.ordered, !running else { return }
        let moving = IndexSet(items.indices.filter { ids.contains(items[$0].id) })
        guard !moving.isEmpty else { return }
        items.move(fromOffsets: moving, toOffset: index)
    }
    func sortNames() { items.sort { $0.url.lastPathComponent.localizedStandardCompare($1.url.lastPathComponent) == .orderedAscending } }
    func move(_ offset: Int) {
        let idx = items.indices.filter { selection.contains(items[$0].id) }
        guard !idx.isEmpty, let lo = idx.first, let hi = idx.last, items.indices.contains(offset < 0 ? lo + offset : hi + offset) else { return }
        for i in (offset < 0 ? idx : idx.reversed()) { items.swapAt(i, i + offset) }
    }

    // MARK: Engine

    /// The request body the engine receives. lossy/dpi/quality are all listed in
    /// engine/protocol.py's CONFIG_FIELDS, so the whitelist does not drop them.
    func config() -> [String: Any] {
        var c: [String: Any] = [
            "operation": operation, "naming": cleanNaming ? "clean" : "tagged", "format": format,
            "range": rangeApplies && useRange ? pageRange : "",
            "paper": paper, "margin_mm": marginMM, "pdf_pages": pdfPages, "pdf_bytes": pdfMB * 1000000, "workers": effectiveWorkers,
            "image_bytes": imageMB * 1000000, "document_bytes": documentMB * 1000000, "text_bytes": textMB * 1000000, "table_bytes": tableMB * 1000000, "table_rows": tableRows, "batch_files": batchFiles,
            "soffice": Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/LibreOffice.app/Contents/MacOS/soffice").path,
        ]
        if operation == "pdf_compress" {
            c["lossy"] = compressLossy
            // Only the lossy mode has DPI/quality; sending them for the lossless
            // mode would describe settings that do not exist (the CLI rejects them too).
            if compressLossy { c["dpi"] = compressDPI; c["quality"] = compressQuality }
        } else if operation == "image_compress" {
            // quality is an encoder knob of JPEG and WebP only. The lossless mode is
            // a PNG pixel-preserving optimisation: it carries no quality and no
            // resize, and it must not put PNG into the lossy format slot — the
            // engine refuses PNG+quality, and sending it would abort the whole run.
            c["lossy"] = lossy
            c["dpi"] = dpi
            if lossy {
                c["format"] = ["jpeg","webp"].contains(format) ? format : "jpeg"
                c["quality"] = quality
                c["max_edge"] = maxEdge
            } else {
                c["format"] = "png"
            }
        } else {
            c["lossy"] = lossy; c["dpi"] = dpi; c["quality"] = quality; c["max_edge"] = maxEdge
        }
        return c
    }
    func savePreset() {
        if let d = try? JSONSerialization.data(withJSONObject: config()) { UserDefaults.standard.set(d, forKey: "preset"); notice = "规则已保存，下次启动自动使用" }
    }
    func saveRepository() {
        do { let repo = try GitHubRepository(githubRepository); githubRepository = repo.url.absoluteString; UserDefaults.standard.set(githubRepository, forKey: "githubRepository"); notice = "反馈仓库已保存" }
        catch { fail(error.localizedDescription) }
    }
    func saveHistory() { if let d = try? JSONEncoder().encode(histories) { try? d.write(to: storage.appendingPathComponent("history.json"), options: .atomic) } }

    func start() {
        guard !running, blockers.isEmpty else { return }
        // Only items the tool can read are sent; incompatible rows stay visible but untouched.
        launch(["protocol": Workbench.engineProtocol, "gui_session": true, "inputs": processable.map { $0.url.path }, "output": targetOutput, "config": config()])
    }

    func resume(_ h: History) {
        guard !running else { return }
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: h.request))
            guard var req = try JSONSerialization.jsonObject(with: data) as? [String: Any], var cfg = req["config"] as? [String: Any] else { throw Message("任务记录已损坏") }
            cfg["soffice"] = config()["soffice"]; req["config"] = cfg; req["gui_session"] = true
            let op = cfg["operation"] as? String ?? "rag"
            switchTool(op); route = .tool(op)
            items = []; add((req["inputs"] as? [String] ?? []).map { URL(fileURLWithPath: $0) })
            launch(req)
        } catch { fail("无法恢复任务：\(error.localizedDescription)") }
    }

    func launch(_ req: [String: Any]) {
        var req = req
        // Warm LibreOffice profiles live with the app so later runs start faster.
        req["office_profile_root"] = storage.appendingPathComponent("office-profiles").path
        lastRequest = req
        runOutput = req["output"] as? String ?? output
        do {
            runID = UUID().uuidString
            let requestURL = storage.appendingPathComponent(runID + ".json")
            try JSONSerialization.data(withJSONObject: req).write(to: requestURL, options: .atomic)
            guard FileManager.default.isExecutableFile(atPath: python.path), FileManager.default.fileExists(atPath: engine.path) else { throw Message("安装包内的转换运行时缺失，请重新安装 Chamo") }
            let p = Process(); p.executableURL = python; p.arguments = ["-I", "-B", engine.path, "--request", requestURL.path]
            let out = Pipe(), err = Pipe(), control = Pipe(); p.standardOutput = out; p.standardError = err; p.standardInput = control; input = control
            buffer = Data(); done = 0; total = processable.count; paused = false; report = ""; controlNote = ""; currentName = ""
            excluded = 0; skipped = 0; lastRun = nil; startedAt = Date(); eta = ""; phase = .running; updateDock()
            for i in items.indices { items[i].status = incompatibility(items[i]) == nil ? .queued : .ready; items[i].detail = ""; items[i].outputs = []; items[i].outputBytes = nil; items[i].partDone = 0; items[i].partTotal = 0 }
            histories.insert(History(id: runID, date: Date(), operation: tool.title, status: "运行中", request: requestURL.path, report: "", output: runOutput), at: 0); saveHistory()
            let pump = EventPump(); self.pump = pump
            out.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                pump.push(data) { batch in self?.consume(batch) }
            }
            err.fileHandleForReading.readabilityHandler = { handle in if handle.availableData.isEmpty { handle.readabilityHandler = nil } }
            p.terminationHandler = { [weak self] p in DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self?.exited(p.terminationStatus) } }
            process = p; try p.run()
        } catch { phase = .idle; fail("无法开始转换：\(error.localizedDescription)") }
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let end = buffer.firstIndex(of: 10) {
            let line = buffer.prefix(upTo: end); buffer.removeSubrange(...end)
            if let d = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { receive(d) }
        }
    }

    func receive(_ e: [String: Any]) {
        switch e["type"] as? String {
        case "scan":
            total = e["total"] as? Int ?? 0; report = e["report"] as? String ?? ""
            excluded = e["excluded"] as? Int ?? 0; skipped = e["skipped"] as? Int ?? 0
            // Folders expand into their files; keep already-known page counts.
            if let sources = e["sources"] as? [String] {
                let known = Dictionary(items.map { ($0.url.standardizedFileURL.path, $0) }, uniquingKeysWith: { a, _ in a })
                let sizes = e["sizes"] as? [Int64] ?? []
                let expanded = sources.enumerated().map { i, path -> QueueItem in
                    var row = QueueItem(url: URL(fileURLWithPath: path))
                    if let k = known[row.url.standardizedFileURL.path] { row.pages = k.pages }
                    if i < sizes.count { row.size = sizes[i] }
                    row.status = .queued
                    return row
                }
                // Merge tools report one job for the whole list; their rows stay as added.
                if operation != "pdf_merge" && operation != "images_pdf" {
                    let untouched = items.filter { $0.status != .queued }
                    items = expanded + untouched
                    selection = items.first.map { [$0.id] } ?? []
                }
            }
        case "item":
            let state = e["state"] as? String ?? ""
            if let i = e["index"] as? Int {
                // Merge tools run as a single job covering every queued row.
                let rows = (operation == "pdf_merge" || operation == "images_pdf") ? items.indices.filter { items[$0].status == .queued || items[$0].status == .running } : (items.indices.contains(i) ? [i] : [])
                for r in rows {
                    items[r].status = ["running": .running, "complete": .done, "resumed": .resumed, "failed": .failed, "cancelled": .cancelled][state] ?? .failed
                    items[r].detail = Workbench.friendly(e["detail"] as? String ?? "")
                    if let paths = e["output_paths"] as? [String] { items[r].outputs = paths.map { URL(fileURLWithPath: $0) } }
                    if let bytes = e["output_bytes"] as? NSNumber, state != "running" { items[r].outputBytes = bytes.int64Value }
                }
                if state == "running", let r = rows.first { currentName = items[r].url.lastPathComponent }
            }
            if let n = e["done"] as? Int { done = n; updateETA(); updateDock() }
        case "progress":
            if let i = e["index"] as? Int, items.indices.contains(i), operation != "pdf_merge", operation != "images_pdf" {
                items[i].partDone = e["done"] as? Int ?? 0; items[i].partTotal = e["total"] as? Int ?? 0
                updateETA(); updateDock()
            }
        case "control":
            paused = e["state"] as? String == "paused"
            controlNote = e["detail"] as? String ?? ""
        case "finished":
            done = total; report = e["report"] as? String ?? report
            var s = RunSummary()
            s.success = e["success"] as? Int ?? 0; s.failed = e["failed"] as? Int ?? 0; s.outputs = e["outputs"] as? Int ?? 0
            s.seconds = (e["seconds"] as? NSNumber)?.doubleValue ?? 0; s.cancelled = e["state"] as? String == "cancelled"; s.report = report
            let produced = items.filter { $0.outputBytes != nil && ($0.status == .done || $0.status == .resumed) }
            s.inputBytes = produced.reduce(0) { $0 + $1.size }; s.outputBytes = produced.reduce(0) { $0 + ($1.outputBytes ?? 0) }
            lastRun = s
            notifyFinished(s)
            if openWhenDone && s.success > 0 && !s.cancelled { revealResults() }
            finishHistory(s.cancelled ? "已取消 · 已完成的结果保留" : "完成 \(s.success) 项 · 异常 \(s.failed) 项 · 输出 \(s.outputs) 个")
        case "fatal":
            let detail = e["detail"] as? String ?? "任务失败"
            finishHistory("未能开始：" + detail); fail(detail)
        default: break
        }
    }

    /// Removes only the history entry; outputs and run records on disk are left untouched.
    func removeHistory(_ id: String) {
        guard let i = histories.firstIndex(where: { $0.id == id }) else { return }
        let entry = histories.remove(at: i); saveHistory()
        undoManager?.registerUndo(withTarget: self) { m in m.histories.insert(entry, at: min(i, m.histories.count)); m.saveHistory() }
        undoManager?.setActionName("移除记录")
    }

    private func finishHistory(_ status: String) {
        if let i = histories.firstIndex(where: { $0.id == runID }) { histories[i].status = status; histories[i].report = report; saveHistory() }
    }
    private func exited(_ code: Int32) {
        if let rest = pump?.drain(), !rest.isEmpty { consume(rest) }
        process = nil; input = nil; paused = false; eta = ""; updateDock()
        for i in items.indices where items[i].status == .queued || items[i].status == .running { items[i].status = lastRun == nil ? .ready : .cancelled }
        if lastRun == nil {
            phase = .idle
            if let h = histories.first(where: { $0.id == runID }), h.status == "运行中" { finishHistory("已中断 · 可恢复"); fail("转换进程意外结束（代码 \(code)）。已完成的结果保留，可在历史记录中核验并恢复。") }
        } else { phase = .finished }
    }
    /// Third-party libraries report in English; say what happened and what to do next instead.
    /// Xcode-style fix-its: the next concrete step for a failed file, by failure kind.
    struct FixIt: Identifiable { let id: String; let title: String; let run: () -> Void }
    func fixIts(for item: QueueItem) -> [FixIt] {
        let d = item.detail
        var out: [FixIt] = []
        if d.contains("加密") {
            out.append(FixIt(id: "preview", title: "用“预览”打开，另存为未加密副本") {
                NSWorkspace.shared.open([item.url], withApplicationAt: URL(fileURLWithPath: "/System/Applications/Preview.app"), configuration: NSWorkspace.OpenConfiguration())
            })
        }
        if d.contains("Office 渲染失败") || d.contains("格式不支持") {
            out.append(FixIt(id: "open", title: "用默认应用打开，另存为 .docx/.pptx 后重试") { NSWorkspace.shared.open(item.url) })
        }
        if d.contains("6400 万像素") {
            out.append(FixIt(id: "dpi", title: "改为 150 DPI 后重试") { [weak self] in self?.dpi = 150; self?.retryFailed() })
        }
        if d.contains("已完成输出被改变") {
            out.append(FixIt(id: "output", title: "选择新的输出位置…") { [weak self] in self?.chooseOutput() })
        }
        if d.contains("权限") {
            out.append(FixIt(id: "privacy", title: "打开“隐私与安全性”设置") {
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!)
            })
        }
        if d.contains("损坏") || d.contains("不完整") || out.isEmpty {
            out.append(FixIt(id: "reveal", title: "在 Finder 中显示原件") { NSWorkspace.shared.activateFileViewerSelecting([item.url]) })
        }
        out.append(FixIt(id: "copy", title: "拷贝异常说明") {
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString("\(item.url.path)\n\(d)", forType: .string)
        })
        return out
    }

    nonisolated static func friendly(_ raw: String) -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return "" }
        let lower = text.lowercased()
        let known: [([String], String)] = [
            (["stream has ended", "eof marker", "invalid pdf header", "startxref", "pdfreaderror", "not a pdf"], "PDF 已损坏或不完整，请换用完整的原件"),
            (["cannot identify image", "truncated", "image file is truncated"], "图片已损坏或格式无法识别，请换用原件"),
            (["password", "encrypt"], "文件已加密，请先在原软件中用密码另存一份未加密副本"),
            (["permission denied", "operation not permitted"], "没有读取权限，请检查文件权限或在“系统设置 → 隐私与安全性”中允许访问"),
            (["no space left"], "磁盘空间不足，请腾出空间后从历史记录继续"),
            (["timed out", "timeout"], "处理超时，文件可能过大或过于复杂"),
        ]
        if text.unicodeScalars.contains(where: { (0x4E00...0x9FFF).contains($0.value) }) { return text }
        for (keys, message) in known where keys.contains(where: lower.contains) { return message }
        return "处理失败（\(text.prefix(80))）"
    }
    /// Re-runs the last request: verified results are reused by fingerprint, so only failed or unfinished items do real work.
    func retryFailed() {
        guard !running, let req = lastRequest else { return }
        launch(req)
    }

    /// Completed items plus the page fraction of running ones, so a single large PDF
    /// still shows real movement (and a meaningful ETA) instead of 0 → 1.
    var fractionDone: Double {
        guard total > 0 else { return 0 }
        let partial = items.filter { $0.status == .running && $0.partTotal > 0 }.reduce(0.0) { $0 + Double($1.partDone) / Double($1.partTotal) }
        return min(1, (Double(done) + partial) / Double(total))
    }

    private func updateETA() {
        let elapsed = Date().timeIntervalSince(startedAt), f = fractionDone
        guard f > 0.02, f < 1, elapsed > 3 else { eta = ""; return }
        let remaining = elapsed / f * (1 - f)
        eta = remaining < 60 ? "预计不到 1 分钟" : "预计还需约 " + Duration.seconds(remaining).formatted(.units(allowed: [.hours, .minutes], width: .wide, maximumUnitCount: 2))
    }

    private func updateDock() {
        NSApp.dockTile.badgeLabel = running && total > 0 ? "\(Int(fractionDone * 100))%" : nil
    }

    private func notifyFinished(_ s: RunSummary) {
        guard !NSApp.isActive else { return }
        NSApp.requestUserAttention(.informationalRequest)
        let content = UNMutableNotificationContent()
        content.title = s.cancelled ? "\(tool.title)已取消" : s.failed == 0 ? "\(tool.title)完成" : "\(tool.title)完成，\(s.failed) 项异常"
        content.body = "生成 \(s.outputs) 个文件 · 用时 " + Duration.seconds(s.seconds).formatted(.units(allowed: [.minutes, .seconds], width: .narrow))
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    func quickLook(_ urls: [URL]) { QuickLook.shared.show(urls) }

    func command(_ cmd: String) { try? input?.fileHandleForWriting.write(contentsOf: Data(("{\"command\":\"\(cmd)\"}\n").utf8)) }
    func reveal(_ path: String) { guard !path.isEmpty else { return }; NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
    func showInFinder(_ urls: [URL]) { NSWorkspace.shared.activateFileViewerSelecting(urls) }

    /// This run's results as top-level entries of the output folder (page folders or files),
    /// so Finder selects exactly what was produced instead of just opening the folder.
    var resultEntries: [URL] {
        let root = URL(fileURLWithPath: runOutput.isEmpty ? output : runOutput).standardizedFileURL
        var seen = Set<String>(), entries: [URL] = []
        for url in items.flatMap(\.outputs) {
            let parts = url.standardizedFileURL.pathComponents, base = root.pathComponents
            guard parts.count > base.count, Array(parts.prefix(base.count)) == base else { continue }
            let top = root.appendingPathComponent(parts[base.count])
            if seen.insert(top.path).inserted { entries.append(top) }
        }
        return entries
    }
    func revealResults() {
        let entries = resultEntries
        if entries.isEmpty || entries.count > 200 { reveal(runOutput.isEmpty ? output : runOutput) } else { showInFinder(entries) }
    }
    var reportFile: String {
        let txt = URL(fileURLWithPath: report).appendingPathComponent("处理报告.txt")
        return FileManager.default.fileExists(atPath: txt.path) ? txt.path : report
    }
}

/// Coalesces engine output so the UI applies events in batches (≤ 20 per second)
/// instead of re-rendering the whole table for every file in a large batch.
final class EventPump: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private var scheduled = false

    func push(_ chunk: Data, flush: @escaping @MainActor (Data) -> Void) {
        lock.lock(); data.append(chunk); let schedule = !scheduled; scheduled = true; lock.unlock()
        guard schedule else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [self] in
            let batch = drain()
            MainActor.assumeIsolated { flush(batch) }
        }
    }

    func drain() -> Data {
        lock.lock(); defer { lock.unlock() }
        let batch = data; data = Data(); scheduled = false
        return batch
    }
}
