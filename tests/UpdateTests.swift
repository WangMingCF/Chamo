import Foundation

@main struct UpdateTests {
    static func main() async throws {
        precondition(AppVersion("0.2.0")! > AppVersion("0.1.3")!)
        precondition(AppVersion("v0.10.0")! > AppVersion("0.9.9")!)
        precondition(AppVersion("1.0")! == AppVersion("1.0.0")!)
        for bad in ["", "abc", "1..2", "1.-1", "1.2.3.4.5"] { precondition(AppVersion(bad) == nil, bad) }

        let repo = try GitHubRepository("https://github.com/example/chamo")
        func release(_ tag: String, url: String = "https://github.com/example/chamo/releases/tag/v9", draft: Bool = false, pre: Bool = false) -> ReleaseInfo {
            ReleaseInfo(tag_name: tag, html_url: url, body: "## 新功能\n\n- **更快** 的转换\n- `⌘K` 搜索\n", draft: draft, prerelease: pre)
        }
        guard case .available(let v, let page, let summary) = try UpdateCheck.evaluate(current: "0.2.0", release: release("v0.3.0"), repository: repo) else { fatalError("newer not offered") }
        precondition(v.description == "0.3.0" && page.host == "github.com")
        precondition(summary == "• 新功能\n• 更快 的转换\n• ⌘K 搜索", summary)
        precondition(UpdateCheck.summary(String(repeating: "长", count: 80)) == "• " + String(repeating: "长", count: 59) + "…")
        guard case .upToDate = try UpdateCheck.evaluate(current: "0.2.0", release: release("v0.2.0"), repository: repo) else { fatalError("same version offered") }
        guard case .upToDate = try UpdateCheck.evaluate(current: "0.2.0", release: release("v0.1.3"), repository: repo) else { fatalError("older offered") }
        guard case .upToDate = try UpdateCheck.evaluate(current: "0.2.0", release: release("v0.9.0", draft: true), repository: repo) else { fatalError("draft offered") }
        guard case .upToDate = try UpdateCheck.evaluate(current: "0.2.0", release: release("v0.9.0", pre: true), repository: repo) else { fatalError("prerelease offered") }
        // Only this repository's github.com release pages may be opened.
        for evil in ["https://evil.test/example/chamo/releases/tag/v9", "http://github.com/example/chamo/releases/tag/v9",
                     "https://github.com/other/repo/releases/tag/v9"] {
            do { _ = try UpdateCheck.evaluate(current: "0.2.0", release: release("v0.3.0", url: evil), repository: repo); fatalError("accepted \(evil)") }
            catch UpdateError.invalidRelease { }
        }
        if CommandLine.arguments.contains("--live") {
            let live = try await UpdateCheck.latest(try GitHubRepository("https://github.com/WangMingCF/chamo"))
            print("live latest:", live.tag_name, live.html_url, "draft:", live.draft)
            let result = try UpdateCheck.evaluate(current: "0.1.3", release: live, repository: try GitHubRepository("https://github.com/WangMingCF/chamo"))
            if case .available(let v, _, let s) = result { print("0.1.3 would be offered", v, "\n" + s) } else { fatalError("0.1.3 not offered an update") }
        }
        print("Update tests passed: version order, newer-only, drafts/prereleases ignored, github.com-only pages.")
    }
}
