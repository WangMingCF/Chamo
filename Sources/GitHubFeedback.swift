import Foundation

/// Only accept a repository root. Credentials, arbitrary hosts and query
/// strings must never become destinations for a feedback payload.
struct GitHubRepository: Equatable {
    let owner: String
    let name: String

    init(_ raw: String) throws {
        guard let url = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme?.lowercased() == "https",
              url.host?.lowercased() == "github.com",
              url.user == nil, url.password == nil, url.port == nil,
              url.query == nil, url.fragment == nil else {
            throw FeedbackError.invalidRepository
        }
        let parts = url.path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        guard parts.count == 2 else { throw FeedbackError.invalidRepository }
        let repo = parts[1].hasSuffix(".git") ? String(parts[1].dropLast(4)) : parts[1]
        guard parts[0].range(of: #"^[A-Za-z0-9-]{1,39}$"#, options: .regularExpression) != nil,
              repo.range(of: #"^[A-Za-z0-9_.-]{1,100}$"#, options: .regularExpression) != nil,
              repo != ".", repo != ".." else {
            throw FeedbackError.invalidRepository
        }
        owner = parts[0]; name = repo
    }

    var url: URL { URL(string: "https://github.com/\(owner)/\(name)")! }
    var issuesURL: URL { url.appendingPathComponent("issues") }
    var newIssueURL: URL { issuesURL.appendingPathComponent("new") }
}

enum FeedbackError: LocalizedError {
    case invalidRepository, emptyTitle, emptyDescription, tooLong
    var errorDescription: String? {
        switch self {
        case .invalidRepository: return "请填写 GitHub 仓库地址：https://github.com/账号/仓库"
        case .emptyTitle: return "请填写反馈标题。"
        case .emptyDescription: return "请描述遇到的问题或建议。"
        case .tooLong: return "反馈过长，无法完整放入浏览器链接。可复制全文，再打开 GitHub 粘贴提交。"
        }
    }
}

enum FeedbackKind: String, CaseIterable {
    case bug = "问题反馈", feature = "功能建议"
}

struct FeedbackDraft {
    var kind = FeedbackKind.bug
    var title = ""
    var description = ""
    var steps = ""
    var expected = ""
    var includeSystemInfo = false

    func validate() throws {
        if title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { throw FeedbackError.emptyTitle }
        if description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { throw FeedbackError.emptyDescription }
    }

    func body(version: String, system: String) -> String {
        var sections = ["### 类型\n\(kind.rawValue)", "### 描述\n\(description)"]
        if !steps.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            sections.append("### 复现步骤\n\(steps)")
        }
        if !expected.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            sections.append("### 预期结果\n\(expected)")
        }
        // This opt-in block deliberately has no paths, filenames, reports,
        // machine name, serial number, account identity or document contents.
        if includeSystemInfo {
            sections.append("### 运行环境\nChamo \(version)\nmacOS \(system)\n架构：Apple Silicon")
        }
        return sections.joined(separator: "\n\n")
    }

    func issueURL(repository: GitHubRepository, version: String, system: String) throws -> URL {
        try validate()
        var components = URLComponents(url: repository.newIssueURL, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "template", value: "feedback.md"),
            URLQueryItem(name: "title", value: "[\(kind.rawValue)] \(title.trimmingCharacters(in: .whitespacesAndNewlines))"),
            URLQueryItem(name: "body", value: body(version: version, system: system))
        ]
        // GitHub decodes query values as form data, where a literal "+" means
        // a space. Foundation leaves "+" unescaped, so protect it explicitly.
        components.percentEncodedQuery = components.percentEncodedQuery?
            .replacingOccurrences(of: "+", with: "%2B")
        guard let result = components.url, result.absoluteString.utf8.count <= 7000 else {
            throw FeedbackError.tooLong
        }
        return result
    }
}
