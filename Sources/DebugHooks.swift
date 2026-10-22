#if CHAMO_DEBUG
import SwiftUI
import AppKit
import Quartz

/// UI-review hooks compiled only with -D CHAMO_DEBUG (never in release builds).
/// CHAMO_TOOL, CHAMO_ROUTE=history, CHAMO_ADD=a|b, CHAMO_SELECT=n, CHAMO_START=1, CHAMO_DARK=1,
/// CHAMO_SNAPSHOT=out.png and CHAMO_SNAPSHOT_DELAY=seconds render the window to a PNG and quit.
/// CHAMO_SCENARIO=steps.json runs a scripted session instead (see Scenario below).
@MainActor enum DebugHooks {
    static func run(_ m: Workbench) {
        let env = ProcessInfo.processInfo.environment
        if let path = env["CHAMO_SCENARIO"] {
            Scenario.start(m, path: path, out: env["CHAMO_SCENARIO_OUT"] ?? NSTemporaryDirectory())
            return
        }
        if let tool = env["CHAMO_TOOL"] { m.switchTool(tool); m.route = .tool(tool) }
        if env["CHAMO_ROUTE"] == "history" { m.route = .history }
        if env["CHAMO_PALETTE"] == "1" { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { m.showPalette = true } }
        if env["CHAMO_WHATSNEW"] == "1" { UserDefaults.standard.removeObject(forKey: "whatsNewSeen") }
        if env["CHAMO_UPDATE"] == "1" { DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { m.showWhatsNew = false; m.checkForUpdates(manual: true) } }
        if env["CHAMO_SHORTCUTS"] == "1" { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { m.showShortcuts = true } }
        if let out = env["CHAMO_OUTPUT"] { m.output = out }
        if env["CHAMO_BESIDE"] == "1" { m.besideSource = true }
        if env["CHAMO_LOSSY"] == "1" { m.lossy = true; m.format = "jpeg"; m.quality = 15 }
        // Image-compression acceptance: capture what config() really sends for the
        // lossy WebP preset (quality) and for the lossless PNG mode (no quality).
        if let format = env["CHAMO_IMAGE_FORMAT"] { m.format = format }
        if let quality = env["CHAMO_IMAGE_QUALITY"], let n = Int(quality) { m.quality = n }
        if let edge = env["CHAMO_IMAGE_MAX_EDGE"], let n = Int(edge) { m.maxEdge = n }
        // PDF-compression acceptance: the two modes send different payloads.
        if env["CHAMO_PDF_LOSSY"] == "1" { m.compressLossy = true }
        if let dpi = env["CHAMO_PDF_DPI"], let n = Int(dpi) { m.compressDPI = n }
        if let quality = env["CHAMO_PDF_QUALITY"], let n = Int(quality) { m.compressQuality = n }
        if let add = env["CHAMO_ADD"] { m.add(add.split(separator: "|").map { URL(fileURLWithPath: String($0)) }) }
        if let s = env["CHAMO_SELECT"], let n = Int(s), m.items.indices.contains(n) { m.selection = [m.items[n].id] }
        if let range = env["CHAMO_RANGE"] { m.useRange = true; m.pageRange = range }
        if env["CHAMO_QL"] == "1", let first = m.items.first {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                m.quickLook([first.url])
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    let panel = QLPreviewPanel.shared()
                    FileHandle.standardError.write(Data("QL visible=\(panel?.isVisible ?? false) controller=\(String(describing: panel?.currentController)) item=\(String(describing: panel?.currentPreviewItem))\n".utf8))
                }
            }
        }
        if env["CHAMO_DUMP"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                // In-process self-check of real AppKit views (no accessibility permission needed).
                // SwiftUI builds its accessibility tree only for a connected AX client, so only
                // structure is checked here: sidebar row count and the Dock menu items.
                func outlines(_ v: NSView) -> [NSOutlineView] { (v as? NSOutlineView).map { [$0] } ?? v.subviews.flatMap(outlines) }
                for window in NSApp.windows where window.isVisible { for o in outlines(window.contentView!) { print("SIDEBAR rows=\(o.numberOfRows)") } }
                func all(_ v: NSView) -> [NSView] { [v] + v.subviews.flatMap(all) }
                var looks: [String: Int] = [:]
                for window in NSApp.windows where window.isVisible { for v in all(window.contentView!.superview!) { looks[v.effectiveAppearance.name.rawValue, default: 0] += 1 } }
                print("APPEARANCE", looks)
                if let menu = NSApp.delegate?.applicationDockMenu?(NSApp) {
                    print("DOCK", menu.items.map { $0.isSeparatorItem ? "—" : $0.title + ($0.isEnabled ? "" : "(禁用)") }.joined(separator: ", "))
                }
                exit(0)
            }
        }
        if let rowText = env["CHAMO_CLICK_ROW"], let row = Int(rowText) {
            // Inject a real mouse click on a sidebar row (in-process, no accessibility permission).
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                func outlines(_ v: NSView) -> [NSOutlineView] { (v as? NSOutlineView).map { [$0] } ?? v.subviews.flatMap(outlines) }
                guard let window = NSApp.windows.first(where: { $0.isVisible && $0.sheetParent == nil }),
                      let outline = outlines(window.contentView!).max(by: { $0.numberOfRows < $1.numberOfRows }) else { print("CLICK no outline"); exit(1) }
                let rect = outline.rect(ofRow: row)
                let point = outline.convert(NSPoint(x: rect.midX, y: rect.midY), to: nil)
                print("CLICK before route=\(m.route) row=\(row) rect=\(rect) hit=\(String(describing: window.contentView?.superview?.hitTest(point)).prefix(90))")
                NSApp.activate(); window.makeKeyAndOrderFront(nil)
                if env["CHAMO_SELECT_ONLY"] == "1" {
                    outline.selectRowIndexes([row], byExtendingSelection: false)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { print("SELECT after route=\(m.route) selectedRow=\(outline.selectedRow)"); exit(0) }
                    return
                }
                for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                    let e = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0)!
                    NSApp.postEvent(e, atStart: false)  // queued, so the table's mouse-tracking loop sees the mouse-up
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { print("CLICK after route=\(m.route) selectedRow=\(outline.selectedRow)"); exit(0) }
            }
        }
        if env["CHAMO_START"] == "1" { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { m.start() } }
        if let s = env["CHAMO_CANCEL_AFTER"], let seconds = Double(s) {
            // Cancel through the same control pipe the UI's cancel button uses.
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { print("HOOK cancel after \(seconds)s"); m.command("cancel") }
        }
        if let s = env["CHAMO_RETRY_AFTER"], let seconds = Double(s) {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { print("HOOK retry failed items"); m.retryFailed() }
        }
        if let s = env["CHAMO_REVEAL_AFTER"], let seconds = Double(s) {
            // The same call the "show in Finder" button makes; no accessibility permission involved.
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
                print("REVEAL plan=\(m.resultEntries.map { $0.lastPathComponent }.sorted().joined(separator: ","))")
                if m.resultEntries.isEmpty { print("REVEAL empty"); exit(4) }
                m.revealResults()
            }
        }
        if env["CHAMO_RESUME"] == "1" {
            // Resume the newest history entry, the way its row does.
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                guard let entry = m.histories.first else { print("HOOK resume none"); exit(5) }
                print("HOOK resume id=\(entry.id) status=\(entry.status) request=\(entry.request)")
                m.resume(entry)
            }
        }
        if let s = env["CHAMO_SUMMARY_AFTER"], let seconds = Double(s) {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
                print("SUMMARY " + m.debugSummary())
                exit(0)
            }
        }
        if let path = env["CHAMO_SNAPSHOT"] {
            let delay = Double(env["CHAMO_SNAPSHOT_DELAY"] ?? "2") ?? 2
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                let sheet = env["CHAMO_PALETTE"] == "1" || env["CHAMO_SHORTCUTS"] == "1" || env["CHAMO_WHATSNEW"] == "1" || env["CHAMO_UPDATE"] == "1"
                if let error = snapshot(to: path, preferSheet: sheet) { print("SNAPSHOT " + error); exit(2) }
                if env["CHAMO_KEEP"] != "1" { exit(0) }
            }
        }
    }

    /// Renders the frontmost window (or its sheet) to a PNG in-process with
    /// cacheDisplay, so no screen-recording permission is involved. Returns an
    /// error description, or nil on success.
    static func snapshot(to path: String, preferSheet: Bool) -> String? {
        guard var window = NSApp.windows.first(where: { $0.isVisible && $0.contentView != nil && $0.sheetParent == nil }) else { return "no visible window" }
        if preferSheet, let sheet = window.attachedSheet { window = sheet }
        guard let frame = window.contentView?.superview else { return "window has no frame view" }
        guard let rep = frame.bitmapImageRepForCachingDisplay(in: frame.bounds) else { return "cannot allocate bitmap" }
        // Draw with the window's own appearance, not the process default (else dark mode renders light).
        window.effectiveAppearance.performAsCurrentDrawingAppearance { frame.cacheDisplay(in: frame.bounds, to: rep) }
        guard let png = rep.representation(using: .png, properties: [:]) else { return "cannot encode PNG" }
        do { try png.write(to: URL(fileURLWithPath: path)) } catch { return "cannot write \(path): \(error.localizedDescription)" }
        return nil
    }
}

/// CHAMO_SCENARIO=steps.json runs a scripted session for the automated UI tests in
/// uitests/macos. Each step is a JSON object with one action:
///   {"tool": "pdf_images"} {"route": "home"|"history"} {"add": [paths]} {"output": path}
///   {"set": {"format", "dpi", "quality", "max_edge", "lossy", "range", "beside"}}
///   {"appearance": "dark"|"light"} {"start": true} {"cancel": true} {"retry": true}
///   {"wait": "finished", "timeout": seconds} {"sleep": seconds}
///   {"snapshot": name, "sheet": bool} {"summary": name}
/// Results go to CHAMO_SCENARIO_OUT: name.png per snapshot, name.json per summary
/// (Workbench.debugSummary), and result.json ({"ok", "step", "error"}); the process
/// exits 0 when every step ran and 1 otherwise. Everything happens in-process on the
/// main thread: no accessibility or screen-recording permission, which is what makes
/// it usable on CI runners.
@MainActor enum Scenario {
    static func start(_ m: Workbench, path: String, out: String) {
        let dir = URL(fileURLWithPath: out)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        guard let data = FileManager.default.contents(atPath: path),
              let steps = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else {
            finish(dir, ok: false, step: -1, error: "cannot read scenario \(path)"); return
        }
        // Let the window appear and the first layout settle before the first step.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { step(m, steps, 0, dir) }
    }

    static func step(_ m: Workbench, _ steps: [[String: Any]], _ i: Int, _ dir: URL) {
        guard i < steps.count else { finish(dir, ok: true, step: i, error: nil); return }
        let s = steps[i]
        let next = { (delay: Double) in DispatchQueue.main.asyncAfter(deadline: .now() + delay) { step(m, steps, i + 1, dir) } }
        if let tool = s["tool"] as? String { m.switchTool(tool); m.route = .tool(tool); next(0.5); return }
        if let route = s["route"] as? String { m.route = route == "history" ? .history : .home; next(0.5); return }
        if let paths = s["add"] as? [String] { m.add(paths.map { URL(fileURLWithPath: $0) }); next(1.0); return }
        if let output = s["output"] as? String { m.output = output; next(0.2); return }
        if let set = s["set"] as? [String: Any] { apply(m, set); next(0.3); return }
        if let look = s["appearance"] as? String {
            NSApp.appearance = NSAppearance(named: look == "dark" ? .darkAqua : .aqua); next(0.8); return
        }
        // A known content size, because the runner's window is whatever the display
        // allows: the narrow-window checks are about what the layout does at 1024
        // points, not about the runner's default.
        if let size = s["resize"] as? [String: Any],
           let width = (size["width"] as? NSNumber)?.doubleValue,
           let height = (size["height"] as? NSNumber)?.doubleValue {
            // The main window, not "the first visible one": a status-bar panel is
            // also in NSApp.windows and also visible, and resizing it would leave
            // the test looking at the runner's default size while claiming 1024.
            guard let window = NSApp.mainWindow ?? NSApp.keyWindow
                    ?? NSApp.windows.first(where: { $0.contentView != nil && $0.canBecomeMain }) else {
                finish(dir, ok: false, step: i, error: "resize: no main window to resize")
                return
            }
            window.setContentSize(NSSize(width: width, height: height))
            next(0.6); return
        }
        if s["start"] != nil { m.start(); next(0.2); return }
        if s["cancel"] != nil { m.command("cancel"); next(0.2); return }
        if s["retry"] != nil { m.retryFailed(); next(0.2); return }
        if let seconds = s["sleep"] as? Double { next(seconds); return }
        if s["wait"] as? String == "finished" {
            let deadline = Date().addingTimeInterval(s["timeout"] as? Double ?? 120)
            func poll() {
                if !m.running { next(0.6); return }
                if Date() > deadline { finish(dir, ok: false, step: i, error: "timed out waiting for the run to finish"); return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { poll() }
            }
            poll(); return
        }
        if let name = s["snapshot"] as? String {
            if let error = DebugHooks.snapshot(to: dir.appendingPathComponent(name + ".png").path, preferSheet: s["sheet"] as? Bool ?? false) {
                finish(dir, ok: false, step: i, error: error); return
            }
            next(0.1); return
        }
        if let name = s["summary"] as? String {
            try? Data(m.debugSummary().utf8).write(to: dir.appendingPathComponent(name + ".json"))
            next(0.1); return
        }
        finish(dir, ok: false, step: i, error: "unknown step \(s)")
    }

    static func apply(_ m: Workbench, _ set: [String: Any]) {
        if let v = set["format"] as? String { m.format = v }
        if let v = set["dpi"] as? Int { m.dpi = v }
        if let v = set["quality"] as? Int { m.quality = v }
        if let v = set["max_edge"] as? Int { m.maxEdge = v }
        if let v = set["lossy"] as? Bool { m.lossy = v }
        if let v = set["range"] as? String { m.useRange = true; m.pageRange = v }
        if let v = set["beside"] as? Bool { m.besideSource = v }
    }

    static func finish(_ dir: URL, ok: Bool, step: Int, error: String?) {
        let payload: [String: Any] = ["ok": ok, "step": step, "error": error ?? NSNull()]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) {
            try? data.write(to: dir.appendingPathComponent("result.json"))
        }
        print("SCENARIO \(ok ? "ok" : "failed") step=\(step) \(error ?? "")")
        exit(ok ? 0 : 1)
    }
}

extension Workbench {
    /// Machine-readable snapshot of what the UI itself believes, for acceptance
    /// runs. It only reports state; no behaviour depends on it.
    func debugSummary() -> String {
        func statusName(_ status: QueueItem.Status) -> String {
            switch status {
            case .ready: return "ready"; case .queued: return "queued"; case .running: return "running"
            case .done: return "done"; case .resumed: return "resumed"; case .failed: return "failed"
            case .cancelled: return "cancelled"
            }
        }
        let items: [[String: Any]] = items.map { item in
            ["name": item.name, "status": statusName(item.status), "detail": item.detail,
             "outputs": item.outputs.map(\.path)]
        }
        let histories: [[String: Any]] = histories.map {
            ["status": $0.status, "output": $0.output, "report": $0.report, "request": $0.request]
        }
        let payload: [String: Any] = ["tool": operation, "route": String(describing: route),
                                      "phase": String(describing: phase), "running": running,
                                      "notice": notice, "report": report, "output": targetOutput,
                                      "items": items, "histories": histories]
        let data = (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{}"
    }
}
#endif
