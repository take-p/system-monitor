import AppKit

/// メニューバーの値の表示形式
enum DisplayMode: String, CaseIterable {
    case number, pie, donut, bar

    var title: String {
        switch self {
        case .number: "数値"
        case .pie: "円グラフ"
        case .donut: "ドーナツグラフ"
        case .bar: "縦棒グラフ"
        }
    }
}

/// メニューバーの1項目
struct StatusSegment {
    enum Value {
        /// 使用率(%)。表示形式に従って数値やグラフで出す
        case percent(Double)
        /// 取得失敗。どの表示形式でも「--」
        case unavailable
        /// 複数行のテキスト(ネットワークの上り/下り)。表示形式に関係なくそのまま出す
        case lines([[TextPart]])
    }

    var label: String
    var value: Value
    var color: NSColor?
    /// 値の右に左寄せで添える行(SSDの残り容量など)
    var suffix: [[TextPart]]?
}

/// 縦書きラベル付きのメニューバー画像
enum StatusImage {
    private static let labelFontSize: CGFloat = 7
    private static let valueFontSize: CGFloat = 12
    /// 縦書きラベルの文字間
    private static let labelLetterGap: CGFloat = 1.5
    /// ラベル列と数値の間
    private static let labelValueGap: CGFloat = 2
    /// CPU/GPU/RAM/SSD/NETの各ブロック間
    private static let segmentGap: CGFloat = 7
    /// 数値欄はこの文字列の幅で固定し右寄せする(値が変わっても表示幅が揺れないように)
    private static let valueWidthSample = "100%"
    /// 円グラフ/ドーナツグラフの直径
    private static let graphDiameter: CGFloat = 15
    /// ドーナツグラフのリングの太さ
    private static let donutLineWidth: CGFloat = 3
    /// 縦棒グラフの幅。高さは円グラフの直径と揃える
    private static let barWidth: CGFloat = 7
    /// 値と補足(SSDの残り容量)の間
    private static let suffixGap: CGFloat = 4
    /// ネットワークの上り/下り2段表示の文字サイズ
    private static let linesFontSize: CGFloat = 9
    /// 上り/下りの行間
    private static let lineGap: CGFloat = 2.5
    /// 上り/下りの欄はこの文字列の幅で固定し右寄せする(1Gbps超の回線でも幅が揺れないように)
    private static let linesWidthSample = "↓ 9999.9 Mbps"
    /// 数値は千の位まで0埋めし、先頭の埋め草の0だけ薄く表示して欄の隙間を埋める
    private static let netDigits = 4
    /// 使用率も百の位まで同様に0埋めする
    private static let percentDigits = 3

    /// 「↓ 0012.4 Mbps」形式。先頭の埋め草の0だけ薄い色にする
    static func rateLine(_ arrow: String, _ mbps: Double) -> [TextPart] {
        let number = String(format: "%.1f", mbps)
        let integerDigits = number.split(separator: ".").first?.count ?? number.count
        return [
            TextPart("\(arrow) "),
            TextPart(String(repeating: "0", count: max(netDigits - integerDigits, 0)), .tertiaryLabelColor),
            TextPart("\(number) Mbps"),
        ]
    }

    /// 「004%」形式。先頭の埋め草の0だけ薄い色にする
    private static func percentParts(_ percent: Double, _ color: NSColor?) -> [TextPart] {
        let number = String(format: "%.0f", percent)
        return [TextPart(String(repeating: "0", count: max(percentDigits - number.count, 0)), .tertiaryLabelColor),
                TextPart("\(number)%", color)]
    }

    /// SSDの残り容量を「Free / 142GB」の2段にする。
    /// 残り容量はほとんど変わらず表示幅も揺れにくいので、使用率やネットワークと違って0埋めはしない
    static func freeLines(_ available: Int64?) -> [[TextPart]] {
        guard let available else { return [[TextPart("Free")], [TextPart("--GB", .secondaryLabelColor)]] }
        // 残りが少ないときは、メニュー内のStorage欄と同じしきい値で色を付けて警告する
        return [[TextPart("Free")], [TextPart(String(format: "%.0fGB", Double(available) / 1_000_000_000), FreeSpace.color(available))]]
    }

    private static func joined(_ parts: [TextPart], _ font: NSFont, _ color: NSColor? = nil) -> NSAttributedString {
        let line = NSMutableAttributedString()
        parts.forEach { line.append(attributed($0.text, font, $0.color ?? color)) }
        return line
    }

    /// flipped座標でdraw(at:)に渡すyは行の上端。大文字(数字)の上端をcapTopに揃えるため、ascenderとcapHeightの差だけ上にずらす
    private static func drawY(_ font: NSFont, capTop: CGFloat) -> CGFloat {
        capTop - (font.ascender - font.capHeight)
    }

    static func build(_ segments: [StatusSegment], height: CGFloat, mode: DisplayMode) -> NSImage {
        let labelFont = NSFont.systemFont(ofSize: labelFontSize, weight: .semibold)
        let valueFont = NSFont.monospacedDigitSystemFont(ofSize: valueFontSize, weight: .regular)
        let linesFont = NSFont.monospacedDigitSystemFont(ofSize: linesFontSize, weight: .regular)
        let diameter = min(graphDiameter, height - 4)
        let percentWidth = mode == .number
            ? textWidth(valueWidthSample, valueFont)
            : max(mode == .bar ? barWidth : diameter, textWidth("--", valueFont))
        let linesWidth = textWidth(linesWidthSample, linesFont)

        struct Placed {
            var x: CGFloat
            var labelWidth: CGFloat
            var letters: [NSAttributedString]
            var segment: StatusSegment
            var valueWidth: CGFloat
            var suffixWidth: CGFloat
        }
        var placed: [Placed] = []
        var x: CGFloat = 0
        for (i, segment) in segments.enumerated() {
            if i > 0 { x += segmentGap }
            let letters = segment.label.map { attributed(String($0), labelFont) }
            let labelWidth = letters.map { $0.size().width }.max() ?? 0
            var valueWidth = percentWidth
            if case .lines = segment.value { valueWidth = linesWidth }
            // 補足の幅は一番長い行に合わせる(残り容量は桁がめったに変わらないので固定幅にしない)
            let suffixWidth = segment.suffix?.map { joined($0, linesFont).size().width }.max() ?? 0
            placed.append(Placed(x: x, labelWidth: labelWidth, letters: letters, segment: segment,
                                 valueWidth: valueWidth, suffixWidth: suffixWidth))
            x += labelWidth + labelValueGap + valueWidth
            if segment.suffix != nil { x += suffixGap + suffixWidth }
        }

        let labelCap = labelFont.capHeight, valueCap = valueFont.capHeight, linesCap = linesFont.capHeight

        /// 複数行のテキストを上下中央にまとめて置く(ネットワークの上り/下り、SSDの残り容量)
        func drawLines(_ lines: [[TextPart]], left: CGFloat, width: CGFloat, color: NSColor?, alignRight: Bool) {
            let blockHeight = CGFloat(lines.count) * linesCap + CGFloat(lines.count - 1) * lineGap
            var capTop = (height - blockHeight) / 2
            for parts in lines {
                let line = joined(parts, linesFont, color)
                let lineX = alignRight ? left + width - line.size().width : left
                line.draw(at: NSPoint(x: lineX, y: drawY(linesFont, capTop: capTop)))
                capTop += linesCap + lineGap
            }
        }

        return NSImage(size: NSSize(width: x, height: height), flipped: true) { _ in
            for item in placed {
                // ラベルの文字を縦に積み、全体を上下中央に置く
                let stackHeight = CGFloat(item.letters.count) * labelCap + CGFloat(item.letters.count - 1) * labelLetterGap
                var capTop = (height - stackHeight) / 2
                for letter in item.letters {
                    letter.draw(at: NSPoint(x: item.x + (item.labelWidth - letter.size().width) / 2,
                                            y: drawY(labelFont, capTop: capTop)))
                    capTop += labelCap + labelLetterGap
                }

                let valueLeft = item.x + item.labelWidth + labelValueGap
                if let suffix = item.segment.suffix {
                    // 「Free」の見出しと値の頭がそろうよう左寄せにする
                    drawLines(suffix, left: valueLeft + item.valueWidth + suffixGap, width: item.suffixWidth, color: nil, alignRight: false)
                }
                let color = item.segment.color
                let value: NSAttributedString
                switch item.segment.value {
                case .lines(let lines):
                    drawLines(lines, left: valueLeft, width: item.valueWidth, color: color, alignRight: true)
                    continue
                case .percent(let percent) where mode != .number:
                    drawGraph(mode, center: NSPoint(x: valueLeft + item.valueWidth / 2, y: height / 2),
                              radius: diameter / 2, percent: percent, color: color)
                    continue
                case .percent(let percent):
                    value = joined(percentParts(percent, color), valueFont)
                case .unavailable:
                    value = attributed("--", valueFont, color)
                }
                value.draw(at: NSPoint(x: valueLeft + item.valueWidth - value.size().width,
                                       y: drawY(valueFont, capTop: (height - valueCap) / 2)))
            }
            return true
        }
    }

    private static func drawGraph(_ mode: DisplayMode, center: NSPoint, radius: CGFloat, percent: Double, color: NSColor?) {
        let fillColor = color ?? .labelColor
        switch mode {
        case .number:
            break
        case .pie:
            let oval = NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
            NSColor.tertiaryLabelColor.setFill()
            NSBezierPath(ovalIn: oval).fill()
            guard percent > 0 else { return }
            fillColor.setFill()
            if percent >= 100 {
                NSBezierPath(ovalIn: oval).fill()
                return
            }
            // flipped座標では角度が増える向きが見た目の時計回りになる。12時(-90°)から時計回りに塗る
            let wedge = NSBezierPath()
            wedge.move(to: center)
            wedge.appendArc(withCenter: center, radius: radius, startAngle: -90, endAngle: -90 + 360 * percent / 100, clockwise: false)
            wedge.close()
            wedge.fill()
        case .donut:
            // 線幅の半分だけ内側を通る円にして、外径を円グラフと揃える
            let ringRadius = radius - donutLineWidth / 2
            let track = NSBezierPath()
            track.appendArc(withCenter: center, radius: ringRadius, startAngle: 0, endAngle: 360, clockwise: false)
            track.lineWidth = donutLineWidth
            NSColor.tertiaryLabelColor.setStroke()
            track.stroke()
            guard percent > 0 else { return }
            let arc = NSBezierPath()
            arc.appendArc(withCenter: center, radius: ringRadius, startAngle: -90, endAngle: -90 + 360 * min(percent, 100) / 100,
                          clockwise: false)
            arc.lineWidth = donutLineWidth
            fillColor.setStroke()
            arc.stroke()
        case .bar:
            // 角丸の溝を描き、その内側を下から使用率の高さまで塗る(レベルメーター風)
            let rect = NSRect(x: center.x - barWidth / 2, y: center.y - radius, width: barWidth, height: radius * 2)
            let track = NSBezierPath(roundedRect: rect, xRadius: 2, yRadius: 2)
            NSColor.tertiaryLabelColor.setFill()
            track.fill()
            guard percent > 0 else { return }
            // 塗りを溝の角丸で切り抜いて、満杯に近いときも角がはみ出さないようにする
            NSGraphicsContext.saveGraphicsState()
            track.addClip()
            let fillHeight = radius * 2 * min(percent, 100) / 100
            fillColor.setFill()
            NSRect(x: rect.minX, y: center.y + radius - fillHeight, width: barWidth, height: fillHeight).fill(using: .sourceOver)
            NSGraphicsContext.restoreGraphicsState()
        }
    }
}
