import Foundation
import SwiftUI

/// The language the Settings window is shown in.
///
/// "followSystem" keeps the previous behaviour (whatever the system prefers, and
/// Chinese when the system language is not one we translate). The other cases pin
/// a language regardless of the system, which is what a user who reads English on
/// a Chinese macOS is asking for.
enum AppLanguage: String, CaseIterable, Identifiable {
    case followSystem = "system"
    case simplifiedChinese = "zh-Hans"
    case english = "en"

    var id: String { rawValue }

    /// The language actually used for lookup, with the fallback applied.
    var resolved: String {
        switch self {
        case .simplifiedChinese: return "zh-Hans"
        case .english: return "en"
        case .followSystem: return Self.systemPreferred
        }
    }

    /// System language if we translate it, otherwise the table's default.
    static var systemPreferred: String {
        for tag in Locale.preferredLanguages {
            let lower = tag.lowercased()
            if lower.hasPrefix("zh") { return "zh-Hans" }
            if lower.hasPrefix("en") { return "en" }
        }
        return LocalizedStrings.defaultLanguage
    }

    /// Name of this choice, in its own language where that makes sense.
    func title(_ current: String) -> String {
        switch self {
        case .followSystem: return L10n.text("settings.language.system", current)
        default: return LocalizedStrings.displayNames[resolved] ?? rawValue
        }
    }
}

/// Lookup for the generated table.
///
/// A missing key returns a visibly wrong value ("!key") rather than the key
/// itself: a window that silently shows "settings.outputLocation" looks like a
/// translation problem, while "!settings.outputLocation" is unmistakably a bug,
/// and tests/test_localization.py makes sure it never ships in the first place.
enum L10n {
    static let defaultsKey = "ui.language"

    static func text(_ key: String, _ language: String) -> String {
        guard let entry = LocalizedStrings.table[key] else { return "!" + key }
        return entry[language] ?? entry[LocalizedStrings.defaultLanguage] ?? "!" + key
    }

    /// Replaces {name} tokens. Unknown tokens are left alone so a mistake is visible.
    static func text(_ key: String, _ language: String, _ values: [String: String]) -> String {
        var result = text(key, language)
        for (name, value) in values {
            result = result.replacingOccurrences(of: "{" + name + "}", with: value)
        }
        return result
    }
}

/// A Text that follows the language setting.
///
/// Reading the setting through @AppStorage is what makes the window redraw the
/// moment the picker changes: without it the user would have to reopen Settings.
struct LText: View {
    let key: String
    let values: [String: String]
    // Stored as the raw string rather than the enum: @AppStorage's RawRepresentable
    // overload differs between SDK versions, and a plain String always works.
    @AppStorage(L10n.defaultsKey) private var selectionRaw: String = AppLanguage.followSystem.rawValue

    init(_ key: String, values: [String: String] = [:]) {
        self.key = key
        self.values = values
    }

    private var language: String {
        (AppLanguage(rawValue: selectionRaw) ?? .followSystem).resolved
    }

    var body: some View {
        Text(values.isEmpty ? L10n.text(key, language)
                            : L10n.text(key, language, values))
    }
}
