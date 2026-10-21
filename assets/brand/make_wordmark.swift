// Outlines the "Chamo" wordmark into font-independent SVG paths with CoreText.
// "Cha" is green and "mo" amber: the name itself has half-changed colour.
// Run on macOS: swift make_wordmark.swift [dark] > chamo-wordmark[-dark].svg
import AppKit
import CoreText

let size: CGFloat = 128
let base = NSFont.systemFont(ofSize: size, weight: .heavy)
let rounded = base.fontDescriptor.withDesign(.rounded).flatMap { NSFont(descriptor: $0, size: size) } ?? base
// Pass "dark" for the version used on dark backgrounds.
let dark = CommandLine.arguments.dropFirst().contains("dark")
let parts: [(String, String)] = dark ? [("Cha", "#A8EBD3"), ("mo", "#FFC85C")] : [("Cha", "#0B4D3B"), ("mo", "#E8961E")]

func svgPath(_ path: CGPath, dx: CGFloat, baseline: CGFloat) -> String {
    var d = ""
    func p(_ pt: CGPoint) -> String { String(format: "%.1f %.1f", pt.x + dx, baseline - pt.y) }
    path.applyWithBlock { el in
        let e = el.pointee, pts = e.points
        switch e.type {
        case .moveToPoint: d += "M\(p(pts[0]))"
        case .addLineToPoint: d += "L\(p(pts[0]))"
        case .addQuadCurveToPoint: d += "Q\(p(pts[0])) \(p(pts[1]))"
        case .addCurveToPoint: d += "C\(p(pts[0])) \(p(pts[1])) \(p(pts[2]))"
        case .closeSubpath: d += "Z"
        @unknown default: break
        }
    }
    return d
}

let tracking: CGFloat = -3, baseline: CGFloat = 124
var x: CGFloat = 8
var body = ""
for (text, color) in parts {
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: [.font: rounded]))
    var d = ""
    for run in CTLineGetGlyphRuns(line) as! [CTRun] {
        let n = CTRunGetGlyphCount(run)
        var glyphs = [CGGlyph](repeating: 0, count: n), positions = [CGPoint](repeating: .zero, count: n), advances = [CGSize](repeating: .zero, count: n)
        CTRunGetGlyphs(run, CFRange(location: 0, length: n), &glyphs)
        CTRunGetPositions(run, CFRange(location: 0, length: n), &positions)
        CTRunGetAdvances(run, CFRange(location: 0, length: n), &advances)
        for i in 0..<n {
            if let path = CTFontCreatePathForGlyph(rounded as CTFont, glyphs[i], nil) { d += svgPath(path, dx: x + positions[i].x + CGFloat(i) * tracking, baseline: baseline) }
        }
        x += advances.reduce(0) { $0 + $1.width } + CGFloat(n) * tracking
    }
    body += "  <path fill=\"\(color)\" d=\"\(d)\"/>\n"
}
let width = Int(x.rounded(.up)) + 8
print("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 \(width) 160\" width=\"\(width)\" height=\"160\">\n\(body)</svg>")
