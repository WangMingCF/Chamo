import Foundation

@main struct FeedbackTests {
    static func main() throws {
        let repo = try GitHubRepository(" https://github.com/example/chamo.git/ ")
        precondition(repo.url.absoluteString == "https://github.com/example/chamo")
        for bad in [
            "http://github.com/a/b", "https://github.com.evil.test/a/b", "https://evil.test/a/b",
            "https://me:secret@github.com/a/b", "https://github.com:443/a/b",
            "https://github.com/a/b?token=secret", "https://github.com/a/b#x",
            "https://github.com/a/b/issues", "https://github.com/a/..", "file:///a/b"
        ] {
            do { _ = try GitHubRepository(bad); fatalError("Accepted unsafe destination: \(bad)") }
            catch FeedbackError.invalidRepository { }
        }
        var draft = FeedbackDraft()
        do { _ = try draft.issueURL(repository: repo, version: "0.1.1", system: "test"); fatalError("Empty accepted") }
        catch FeedbackError.emptyTitle { }
        draft.title = "中文 & + ? # %"
        draft.description = "描述 &body=evil + %2B\n下一行：😀"
        let url = try draft.issueURL(repository: repo, version: "0.1.1", system: "test")
        let parts = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        precondition(!parts.percentEncodedQuery!.contains("+"))
        precondition(parts.percentEncodedQuery!.contains("%2B"))
        // Model form-query decoding, not just Foundation's URL round trip.
        let formBody = parts.percentEncodedQuery!.split(separator: "&")
            .first(where: { $0.hasPrefix("body=") })!.dropFirst(5)
            .replacingOccurrences(of: "+", with: " ").removingPercentEncoding!
        precondition(formBody == draft.body(version: "0.1.1", system: "test"))
        precondition(parts.queryItems?.count == 3)
        precondition(parts.queryItems?.first(where: { $0.name == "title" })?.value == "[问题反馈] 中文 & + ? # %")
        let body = parts.queryItems!.first(where: { $0.name == "body" })!.value!
        precondition(body.contains(draft.description) && !body.contains("运行环境"))
        precondition(url.host == "github.com" && url.path == "/example/chamo/issues/new")
        draft.includeSystemInfo = true
        precondition(draft.body(version: "0.1.1", system: "test").contains("运行环境"))
        precondition(!draft.body(version: "0.1.1", system: "test").contains("/Users/"))
        draft.description = String(repeating: "中文", count: 2000)
        do { _ = try draft.issueURL(repository: repo, version: "0.1.1", system: "test"); fatalError("Oversize URL accepted") }
        catch FeedbackError.tooLong { }
        precondition(draft.body(version: "0.1.1", system: "test").contains(draft.description))
        print("Feedback tests passed: destination validation, UTF-8 encoding, opt-in diagnostics, long-text fallback.")
    }
}
