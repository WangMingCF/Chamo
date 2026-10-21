import AppKit
import Quartz

/// Finder-style Quick Look (⌘Y / Space) for queue rows and results. The app delegate
/// takes panel control through the responder chain and hands it this data source.
final class QuickLook: NSObject, QLPreviewPanelDataSource {
    static let shared = QuickLook()
    private(set) var urls: [URL] = []

    @MainActor func show(_ urls: [URL]) {
        guard !urls.isEmpty, let panel = QLPreviewPanel.shared() else { return }
        if panel.isVisible && self.urls == urls { panel.orderOut(nil); return }
        self.urls = urls
        panel.updateController()
        panel.dataSource = self
        panel.reloadData()
        panel.currentPreviewItemIndex = 0
        panel.makeKeyAndOrderFront(nil)
    }

    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int { urls.count }
    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> QLPreviewItem! { urls[index] as NSURL }
}
