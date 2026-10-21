import SwiftUI
import AppKit

struct FeedbackView: View {
    @ObservedObject var model: Workbench
    @Environment(\.dismiss) private var dismiss
    @State private var draft = FeedbackDraft()
    @State private var error = ""
    private var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "未知" }
    private var system: String { ProcessInfo.processInfo.operatingSystemVersionString }
    private var repository: GitHubRepository? { try? GitHubRepository(model.githubRepository) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("反馈与建议").font(.title2.bold())
                Spacer()
                Button("关闭") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("反馈将发往 GitHub Issues。请先检查内容，勿填写密码、令牌或私人文档。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let repository = repository {
                Text(repository.url.absoluteString).font(.caption).textSelection(.enabled)
            } else {
                Text("尚未设置有效仓库，请到「设置 → GitHub 仓库」填写地址。").foregroundStyle(.orange)
            }
            Picker("反馈类型", selection: $draft.kind) {
                ForEach(FeedbackKind.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)
            TextField("简短标题（例如：某类 PDF 拆分失败）", text: $draft.title).textFieldStyle(.roundedBorder)
            Text("问题描述 / 功能建议").font(.headline)
            TextEditor(text: $draft.description).frame(minHeight: 100).border(.quaternary)
                .accessibilityLabel("反馈描述")
            HStack(alignment: .top) {
                VStack(alignment: .leading) {
                    Text("复现步骤（可选）").font(.caption)
                    TextEditor(text: $draft.steps).frame(height: 70).border(.quaternary).accessibilityLabel("复现步骤")
                }
                VStack(alignment: .leading) {
                    Text("预期结果（可选）").font(.caption)
                    TextEditor(text: $draft.expected).frame(height: 70).border(.quaternary).accessibilityLabel("预期结果")
                }
            }
            Toggle("附上应用版本、macOS 版本与架构", isOn: $draft.includeSystemInfo)
            if draft.includeSystemInfo {
                Text("Chamo \(version) · \(system) · Apple Silicon").font(.caption).foregroundStyle(.secondary)
            }
            Text("不会自动附带文件、文件名、路径或日志。点击下方按钮会将填写内容交给 GitHub；仍需在浏览器登录并确认发布。私有仓库只允许有权限的账号反馈。")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if !error.isEmpty { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            HStack {
                Button("查看已有反馈") { if let repo = repository { open(repo.issuesURL) } }.disabled(repository == nil)
                Spacer()
                Button("复制全文并打开 GitHub") { copyAndOpen() }.disabled(repository == nil)
                Button("在 GitHub 检查并提交") { submit() }.buttonStyle(.borderedProminent).disabled(repository == nil)
            }
        }.padding(24).frame(width: 640)
            .onChange(of: draft.title) { error = "" }
            .onChange(of: draft.description) { error = "" }
    }

    private func open(_ url: URL) {
        if !NSWorkspace.shared.open(url) { error = "无法打开默认浏览器。请复制仓库地址在浏览器中打开。" }
    }
    private func submit() {
        error = ""
        do {
            guard let repository = repository else { throw FeedbackError.invalidRepository }
            open(try draft.issueURL(repository: repository, version: version, system: system))
        } catch { self.error = error.localizedDescription }
    }
    private func copyAndOpen() {
        do {
            try draft.validate()
            guard let repository = repository else { throw FeedbackError.invalidRepository }
            let text = "[\(draft.kind.rawValue)] \(draft.title)\n\n" + draft.body(version: version, system: system)
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            open(repository.newIssueURL)
            error = "已复制全文。请在 GitHub 中填写标题并粘贴内容，检查后发布。"
        } catch { self.error = error.localizedDescription }
    }
}
