import Foundation

/// Dotted numeric version ("0.2.0", "v0.10.1"); missing parts count as zero.
struct AppVersion: Comparable, CustomStringConvertible {
    let parts: [Int]
    init?(_ raw: String) {
        var text = raw.trimmingCharacters(in: .whitespaces)
        if text.lowercased().hasPrefix("v") { text.removeFirst() }
        let parts = text.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
        guard !parts.isEmpty, parts.count <= 4, parts.allSatisfy({ $0 != nil && $0! >= 0 }) else { return nil }
        self.parts = parts.map { $0! }
    }
    static func < (a: AppVersion, b: AppVersion) -> Bool {
        for i in 0..<max(a.parts.count, b.parts.count) {
            let x = i < a.parts.count ? a.parts[i] : 0, y = i < b.parts.count ? b.parts[i] : 0
            if x != y { return x < y }
        }
        return false
    }
    static func == (a: AppVersion, b: AppVersion) -> Bool { !(a < b) && !(b < a) }
    var description: String { parts.map(String.init).joined(separator: ".") }
}

/// The fields we read from GitHub's "latest release" API.
struct ReleaseInfo: Decodable {
    let tag_name: String
    let html_url: String
    let body: String?
    let draft: Bool
    let prerelease: Bool
}

enum UpdateResult {
    case upToDate(current: AppVersion)
    case available(version: AppVersion, page: URL, summary: String)
}

enum UpdateError: LocalizedError {
    case network(String), badResponse, invalidRelease
    var errorDescription: String? {
        switch self {
        case .network(let m): return "无法连接 GitHub：\(m)"
        case .badResponse: return "GitHub 暂时没有返回可用的版本信息，请稍后再试。"
        case .invalidRelease: return "最新发布的信息无法识别，请到 GitHub 发布页查看。"
        }
    }
}

/// Manual or opt-in update check against GitHub Releases. Sends no data beyond the
/// HTTPS request itself, and only ever opens a github.com release page (never downloads).
enum UpdateCheck {
    static func evaluate(current: String, release: ReleaseInfo, repository: GitHubRepository) throws -> UpdateResult {
        guard let mine = AppVersion(current) else { throw UpdateError.invalidRelease }
        guard !release.draft, !release.prerelease, let latest = AppVersion(release.tag_name) else { return .upToDate(current: mine) }
        guard let page = URL(string: release.html_url), page.scheme == "https", page.host?.lowercased() == "github.com",
              page.path.hasPrefix("/\(repository.owner)/\(repository.name)/releases/") else { throw UpdateError.invalidRelease }
        return latest > mine ? .available(version: latest, page: page, summary: summary(release.body ?? "")) : .upToDate(current: mine)
    }

    /// First few meaningful lines of the release notes, with Markdown markers removed.
    static func summary(_ notes: String, lines: Int = 6) -> String {
        var out: [String] = []
        for raw in notes.split(whereSeparator: \.isNewline) {
            var line = raw.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "")
            line = String(line.drop(while: { "#-*• ".contains($0) }))
            guard !line.isEmpty else { continue }
            if line.count > 60 { line = String(line.prefix(59)) + "…" }
            out.append("• " + line)
            if out.count == lines { break }
        }
        return out.joined(separator: "\n")
    }

    static func latest(_ repository: GitHubRepository) async throws -> ReleaseInfo {
        var request = URLRequest(url: URL(string: "https://api.github.com/repos/\(repository.owner)/\(repository.name)/releases/latest")!)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 15
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let data: Data, response: URLResponse
        do { (data, response) = try await URLSession.shared.data(for: request) }
        catch { throw UpdateError.network(error.localizedDescription) }
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw UpdateError.badResponse }
        do { return try JSONDecoder().decode(ReleaseInfo.self, from: data) } catch { throw UpdateError.badResponse }
    }
}
