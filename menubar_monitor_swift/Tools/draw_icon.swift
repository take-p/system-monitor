import AppKit

// アプリアイコンの元画像(1024px)を描く。macOSのアイコンの枠(824pxの角丸四角、周囲に余白)に合わせる。
// 使い方: swift Tools/draw_icon.swift icon_1024.png
// 描いた画像を各サイズに縮小し、Sources/MenubarMonitor/Assets.xcassets/AppIcon.appiconset に置く(AGENTS.md参照)
let size: CGFloat = 1024
let body = NSRect(x: 100, y: 100, width: 824, height: 824)
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255, blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

// 影
let tile = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 24, color: NSColor.black.withAlphaComponent(0.35).cgColor)
rgb(0x10131c).setFill()
tile.fill()
ctx.restoreGState()

// 背景: 濃紺のグラデーション
ctx.saveGState()
tile.addClip()
NSGradient(starting: rgb(0x262c40), ending: rgb(0x0b0d14))!.draw(in: body, angle: -90)

// 目盛り線
rgb(0xffffff, 0.06).setFill()
for i in 1...3 {
    NSRect(x: body.minX + 90, y: body.minY + 250 + CGFloat(i) * 120, width: body.width - 180, height: 4).fill()
}

// 推移グラフ(面+線)。メニューのグラフと同じ形
let left = body.minX + 90, right = body.maxX - 90
let baseY = body.minY + 330
let values: [CGFloat] = [0.20, 0.34, 0.26, 0.48, 0.36, 0.58, 0.44, 0.70, 0.56, 0.88]
let points = values.enumerated().map { i, v in
    NSPoint(x: left + (right - left) * CGFloat(i) / CGFloat(values.count - 1), y: baseY + v * 380)
}
let line = NSBezierPath()
line.move(to: points[0])
points.dropFirst().forEach { line.line(to: $0) }
let area = line.copy() as! NSBezierPath
area.line(to: NSPoint(x: right, y: baseY))
area.line(to: NSPoint(x: left, y: baseY))
area.close()
ctx.saveGState()
area.addClip()
NSGradient(starting: rgb(0x34c8e8, 0.55), ending: rgb(0x3a6cf0, 0.05))!.draw(in: NSRect(x: left, y: baseY, width: right - left, height: 420), angle: -90)
ctx.restoreGState()
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 28, color: rgb(0x4fd8ff, 0.9).cgColor)
line.lineWidth = 22
line.lineCapStyle = .round
line.lineJoinStyle = .round
rgb(0x7fe6ff).setStroke()
line.stroke()
ctx.restoreGState()
// 最新の点
let last = points[points.count - 1]
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 30, color: rgb(0xffffff, 0.9).cgColor)
NSColor.white.setFill()
NSBezierPath(ovalIn: NSRect(x: last.x - 26, y: last.y - 26, width: 52, height: 52)).fill()
ctx.restoreGState()

// 下段: 使用率のメーター(CPU・メモリ・GPU・SSDの色)
let meters: [(UInt32, CGFloat)] = [(0x34c759, 0.55), (0xffcc00, 0.8), (0xbf5af2, 0.4), (0x30c8c8, 0.65)]
let meterW: CGFloat = 120, gap: CGFloat = (right - left - meterW * 4) / 3
for (i, (color, ratio)) in meters.enumerated() {
    let x = left + CGFloat(i) * (meterW + gap)
    let track = NSRect(x: x, y: body.minY + 110, width: meterW, height: 150)
    rgb(0xffffff, 0.08).setFill()
    NSBezierPath(roundedRect: track, xRadius: 26, yRadius: 26).fill()
    let fill = NSRect(x: x, y: track.minY, width: meterW, height: track.height * ratio)
    ctx.saveGState()
    NSBezierPath(roundedRect: track, xRadius: 26, yRadius: 26).addClip()
    NSGradient(starting: rgb(color), ending: rgb(color, 0.7))!.draw(in: fill, angle: 90)
    ctx.restoreGState()
}

// 上端の光沢
NSGradient(starting: rgb(0xffffff, 0), ending: rgb(0xffffff, 0.09))!.draw(in: NSRect(x: body.minX, y: body.midY, width: body.width, height: body.height / 2), angle: 90)
ctx.restoreGState()

// 縁取り
rgb(0xffffff, 0.12).setStroke()
let edge = NSBezierPath(roundedRect: body.insetBy(dx: 2, dy: 2), xRadius: 183, yRadius: 183)
edge.lineWidth = 4
edge.stroke()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
