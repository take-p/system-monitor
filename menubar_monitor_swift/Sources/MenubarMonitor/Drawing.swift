import AppKit

// クリックで開くメニューに並べる、各欄の描画の部品。座標はflipped(左上原点、yは下向き)

enum Layout {
    static let menuWidth: CGFloat = 400
    /// 左右の余白。通常のメニュー項目の文字位置に合わせる
    static let padX: CGFloat = 14
    /// 上下の余白
    static let padY: CGFloat = 6
    /// 見出し行の高さ
    static let titleHeight: CGFloat = 22
    /// 本文1行の高さ
    static let rowHeight: CGFloat = 18
    static let smallRowHeight: CGFloat = 15
    static let congestionRowHeight: CGFloat = 17
    static let barHeight: CGFloat = 8
    /// GPU・ディスクの推移グラフの高さ
    static let chartHeight: CGFloat = 30
    /// CPUヒートマップ1行の高さ。コア数ぶん並ぶので細めにする
    static let heatCellHeight: CGFloat = 6
    static let heatRowGap: CGFloat = 2
    /// draw_percent_chart 1つ分の高さ
    static let percentChartHeight = chartHeight + smallRowHeight + 1
    /// 上下対称グラフの片側の高さ
    static let mirrorHalfHeight: CGFloat = 24
    /// drawMirrorChart 1つ分の高さ(上の見出し + グラフ上下 + 下の見出し + 余白)
    static let mirrorChartHeight = smallRowHeight * 2 + mirrorHalfHeight * 2 + 6
}

enum Ranking {
    /// CPU・メモリのランキングを折りたたんだときの件数
    static let collapsed = 3
    /// 「さらに表示」1回で増やす件数
    static let page = 10
    /// ランキング末尾の行で「折りたたむ」として扱う右端の幅
    static let collapseZoneWidth: CGFloat = 100
    /// GPU・ディスク・通信量のランキングを折りたたんだときの件数
    static let topGPU = 1
    static let topDisk = 1
    static let topNet = 1
}

/// 推移グラフの記録と表示の長さ
@MainActor
enum HistoryConfig {
    /// 推移の記録間隔(秒)。更新間隔と同じ
    static let step: TimeInterval = 2
    /// 記録しておく最大の点数(10分)。表示する長さはメニューの「履歴の長さ」で選ぶ
    static let maxCount = 300
    /// 表示する点数(既定は2分)
    private(set) static var count = 60
    private(set) static var label = "2分前"

    static func setMinutes(_ minutes: Int) {
        count = min(Int(Double(minutes) * 60 / step), maxCount)
        label = "\(minutes)分前"
    }

    /// 記録(古い順)のうち、表示する長さ分の直近の点だけを返す
    static func recent(_ history: [Double]) -> ArraySlice<Double> {
        history.suffix(count)
    }
}

/// 古い順に最大maxCount点を持つ推移の記録。メニューを閉じている間も記録し続ける
struct HistoryBuffer {
    private(set) var values: [Double] = []

    @MainActor
    mutating func append(_ value: Double) {
        values.append(value)
        if values.count > HistoryConfig.maxCount {
            values.removeFirst(values.count - HistoryConfig.maxCount)
        }
    }
}

// MARK: - 色

/// 使用率の色。通常時は標準色(nil)のままにする
func valueColor(_ percent: Double) -> NSColor? {
    if percent >= 80 { return .systemRed }
    if percent >= 50 { return .systemOrange }
    return nil
}

/// ストレージの残り容量の警告のしきい値(バイト)。判断に使うのは割合ではなく残りのGB数なので絶対量で決める
enum FreeSpace {
    /// 大きなアップデートや書き出しが厳しくなり始める
    static let warning: Int64 = 50 * 1_000_000_000
    /// macOSの動作にも影響が出やすい
    static let critical: Int64 = 20 * 1_000_000_000

    /// 残り容量の文字色。メニュー内のStorage欄とメニューバーのSSDで共用する
    static func color(_ available: Int64) -> NSColor? {
        if available < critical { return .systemRed }
        if available < warning { return .systemOrange }
        return nil
    }
}

// 補助の文字色。メニューの自前描画にはmacOS標準のメニューのようなバイブランシーが効かず、
// secondaryLabelColor/tertiaryLabelColorのままだと灰色の背景に対して薄すぎて読みにくい。
// 標準のメニューの見出しに近い濃さになるよう、本文の色の不透明度を下げて作る(ライト/ダークに追従する)

/// 「Process (grouped)」「電波」「さらに表示」など、本文より一段控えめな文字
func secondaryText() -> NSColor { NSColor.labelColor.withAlphaComponent(0.75) }

/// 「10分前 / 現在」「残り◯アプリ」や目盛りの値など、さらに控えめな文字
func tertiaryText() -> NSColor { NSColor.labelColor.withAlphaComponent(0.55) }

/// 25%刻みの4段階で色分けする(0〜25%未満: 青、〜50%未満: 緑、〜75%未満: 黄、75%以上: 赤)
func heatColor(_ percent: Double) -> NSColor {
    if percent >= 75 { return .systemRed }
    if percent >= 50 { return .systemYellow }
    if percent >= 25 { return .systemGreen }
    return .systemBlue
}

/// グラフの上限を1/2/5×10^nの切りの良い値に切り上げる
func niceCeil(_ value: Double, minimum: Double = 1) -> Double {
    let value = max(value, minimum)
    let exponent = pow(10, floor(log10(value)))
    for step in [1.0, 2, 5, 10] where value <= step * exponent {
        return step * exponent
    }
    return 10 * exponent
}

// MARK: - 書式

func formatBytes(_ bytes: UInt64) -> String {
    var number = Double(bytes)
    for unit in ["B", "KB", "MB", "GB"] {
        if number < 1024 { return String(format: "%.1f %@", number, unit) }
        number /= 1024
    }
    return String(format: "%.1f TB", number)
}

/// 小さい値が「0.0」ばかりにならないよう、1Mbps未満は小数2桁にする
func formatMbps(_ mbps: Double) -> String {
    String(format: mbps < 1 ? "%.2f" : "%.1f", mbps)
}

/// 1GB未満は「0.0 GB」ばかりになるのでMBで出す
func formatFootprint(_ megabytes: Double) -> String {
    megabytes >= 1024 ? String(format: "%.1f GB", megabytes / 1024) : String(format: "%.0f MB", megabytes)
}

// MARK: - 文字

enum Fonts {
    static func body() -> NSFont { NSFont.menuFont(ofSize: 0) }
    static func small() -> NSFont { NSFont.systemFont(ofSize: 11, weight: .regular) }
    static func title() -> NSFont { NSFont.systemFont(ofSize: body().pointSize, weight: .semibold) }
    static func monoSmall(_ size: CGFloat = 9) -> NSFont { NSFont.monospacedDigitSystemFont(ofSize: size, weight: .regular) }
    static func mono(_ size: CGFloat) -> NSFont { NSFont.monospacedDigitSystemFont(ofSize: size, weight: .regular) }
    static func bold(_ size: CGFloat) -> NSFont { NSFont.systemFont(ofSize: size, weight: .semibold) }
}

/// 色(nilなら標準色)と太字の指定を持つ文字列の断片
struct TextPart {
    var text: String
    var color: NSColor?
    var bold = false

    init(_ text: String, _ color: NSColor? = nil, bold: Bool = false) {
        self.text = text
        self.color = color
        self.bold = bold
    }
}

/// labelColor等は描画時のアピアランスで解決される動的色なので、ライト/ダークに追従する
func attributed(_ text: String, _ font: NSFont, _ color: NSColor? = nil) -> NSAttributedString {
    NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color ?? NSColor.labelColor])
}

func textWidth(_ text: String, _ font: NSFont) -> CGFloat {
    attributed(text, font).size().width
}

/// partsを左から順に描き、描き終えたx座標を返す
@discardableResult
func drawParts(_ parts: [TextPart], _ x: CGFloat, _ y: CGFloat, _ font: NSFont) -> CGFloat {
    let bold = Fonts.bold(font.pointSize)
    var x = x
    for part in parts {
        let string = attributed(part.text, part.bold ? bold : font, part.color)
        string.draw(at: NSPoint(x: x, y: y))
        x += string.size().width
    }
    return x
}

@discardableResult
func drawText(_ text: String, _ x: CGFloat, _ y: CGFloat, _ font: NSFont, _ color: NSColor? = nil) -> CGFloat {
    drawParts([TextPart(text, color)], x, y, font)
}

func drawTextRight(_ text: String, _ right: CGFloat, _ y: CGFloat, _ font: NSFont, _ color: NSColor? = nil) {
    let string = attributed(text, font, color)
    string.draw(at: NSPoint(x: right - string.size().width, y: y))
}

/// partsを右端そろえで描く
func drawPartsRight(_ parts: [TextPart], _ right: CGFloat, _ y: CGFloat, _ font: NSFont) {
    let bold = Fonts.bold(font.pointSize)
    let width = parts.reduce(0) { $0 + attributed($1.text, $1.bold ? bold : font).size().width }
    drawParts(parts, right - width, y, font)
}

/// maxWidthに収まらない場合は末尾を「…」で省略して描く
func drawTextFit(_ text: String, _ x: CGFloat, _ y: CGFloat, _ maxWidth: CGFloat, _ font: NSFont, _ color: NSColor? = nil) {
    var text = text
    var string = attributed(text, font, color)
    while string.size().width > maxWidth && text.count > 1 {
        text.removeLast()
        string = attributed(text + "…", font, color)
    }
    string.draw(at: NSPoint(x: x, y: y))
}

/// SF Symbolsのアイコンを本文の文字の大きさで作る
func symbolImage(_ name: String, _ color: NSColor, variable: Double? = nil) -> NSImage? {
    let base = variable.map { NSImage(systemSymbolName: name, variableValue: $0, accessibilityDescription: nil) }
        ?? NSImage(systemSymbolName: name, accessibilityDescription: nil)
    // 階層カラーにすると、点灯していない段は同じ色の薄い色で描かれる
    let config = NSImage.SymbolConfiguration(pointSize: Fonts.body().pointSize, weight: .regular)
        .applying(NSImage.SymbolConfiguration(hierarchicalColor: color))
    return base?.withSymbolConfiguration(config)
}

/// SF Symbolsのアイコンを行の上下中央に描き、右端のx座標を返す。
/// variable(0〜1)を渡すと、Wi-Fiアイコンなどの点灯段数をその値に応じて変える。
/// alignRightならxを右端としてその左側に描く
@discardableResult
func drawSymbol(_ name: String, _ x: CGFloat, _ y: CGFloat, _ rowHeight: CGFloat, _ color: NSColor,
                variable: Double? = nil, alignRight: Bool = false) -> CGFloat {
    guard let image = symbolImage(name, color, variable: variable) else { return x }
    let size = image.size
    let left = alignRight ? x - size.width : x
    image.draw(in: NSRect(x: left, y: y + (rowHeight - size.height) / 2, width: size.width, height: size.height),
               from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
    return left + size.width
}

// MARK: - 図形

func fillRect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ color: NSColor, radius: CGFloat = 0) {
    color.setFill()
    let rect = NSRect(x: x, y: y, width: w, height: h)
    if radius > 0 {
        NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
    } else {
        rect.fill(using: .sourceOver)
    }
}

/// 背景トラック付きの横棒(ratioは0〜1)
func drawBar(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ ratio: Double, _ color: NSColor?, h: CGFloat = Layout.barHeight) {
    fillRect(x, y, w, h, .quaternaryLabelColor, radius: h / 2)
    let ratio = min(max(ratio, 0), 1)
    if ratio > 0 {
        fillRect(x, y, max(w * ratio, h), h, color ?? .systemBlue, radius: h / 2)
    }
}

/// 見出し行: 左にタイトル、右に値(部分ごとに色指定可)
func drawTitle(_ width: CGFloat, _ y: CGFloat, _ title: String, _ valueParts: [TextPart]) {
    let font = Fonts.title()
    drawText(title, Layout.padX, y, font)
    let valueFont = Fonts.mono(font.pointSize)
    let total = valueParts.reduce(0) { $0 + textWidth($1.text, valueFont) }
    drawParts(valueParts, width - Layout.padX - total, y, valueFont)
}

/// 見出しの右に出す使用率。灰色がかったメニュー背景では赤/オレンジの文字が読みにくいので、数値は標準色にする
func percentParts(_ percent: Double?, _ format: String = "%.1f%%") -> [TextPart] {
    guard let percent else { return [TextPart("--", secondaryText())] }
    return [TextPart(String(format: format, percent))]
}

// MARK: - グラフ

/// 推移の面グラフ(アクティビティモニタ風)。背景の箱は付けず、淡い単色の塗りにくっきりした線を重ね、
/// 基準線を系列と同じ色で引く。valuesは古い順で、履歴が満杯になるまでは右詰めで描く。
/// downwardなら上端を基準線にして下向きに描く
@MainActor
func drawHistoryChart(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ values: ArraySlice<Double>,
                      _ maxValue: Double, _ color: NSColor, downward: Bool = false) {
    let baseline = downward ? y : y + h
    let direction: CGFloat = downward ? 1 : -1
    let count = HistoryConfig.count
    if !values.isEmpty && maxValue > 0 {
        let step = w / CGFloat(count - 1)
        let start = count - values.count
        let points = values.enumerated().map { i, value in
            NSPoint(x: x + CGFloat(start + i) * step, y: baseline + direction * CGFloat(min(value / maxValue, 1)) * h)
        }
        let area = NSBezierPath()
        area.move(to: NSPoint(x: points[0].x, y: baseline))
        points.forEach { area.line(to: $0) }
        area.line(to: NSPoint(x: points[points.count - 1].x, y: baseline))
        area.close()
        color.withAlphaComponent(0.25).setFill()
        area.fill()

        let line = NSBezierPath()
        line.move(to: points[0])
        points.dropFirst().forEach { line.line(to: $0) }
        line.lineWidth = 1.5
        color.setStroke()
        line.stroke()
    }
    // 基準線はグラフの内側に1pt分引く(上下対称のグラフで上下の基準線が重ならないように)
    fillRect(x, downward ? baseline : baseline - 1, w, 1, color)
}

/// 縦軸0〜100%固定の推移グラフと、その下の「◯分前 / 現在」の行。GPUの使用率・ディスクのビジー率で共用する。
/// 上端に100%の目盛り線を薄く引き、ほかのグラフと同じく左上に目盛りの値を添える
@MainActor
func drawPercentChart(_ x: CGFloat, _ y: CGFloat, _ width: CGFloat, _ history: [Double], _ color: NSColor) {
    fillRect(x, y, width, 0.5, .tertiaryLabelColor)
    drawHistoryChart(x, y, width, Layout.chartHeight, HistoryConfig.recent(history), 100, color)
    drawText("100%", x + 3, y + 1, Fonts.monoSmall(), tertiaryText())
    let footerY = y + Layout.chartHeight + 1
    drawText(HistoryConfig.label, x, footerY, Fonts.small(), tertiaryText())
    drawTextRight("現在", x + width, footerY, Fonts.small(), tertiaryText())
}

/// 上下対称グラフの1系列
struct MirrorSeries {
    var label: String
    var value: Double
    var peak: Double
    var totalText: String
    var history: [Double]
    var color: NSColor
    /// エラー数などの補足。目盛りと同じ高さの右端に小さく添える
    var note: String?
}

/// 上下対称の推移グラフ(Activity Monitorのネットワークのグラフと同じ形)。
/// topは中央から上向き・見出しは上、bottomは中央から下向き・見出しは下に描く。
/// 縦軸は上下で別々に伸縮し、小さい方が平らにならないようにする。次の描画位置のyを返す
@MainActor
func drawMirrorChart(_ x: CGFloat, _ y: CGFloat, _ width: CGFloat, _ unit: String,
                     top: MirrorSeries, bottom: MirrorSeries) -> CGFloat {
    let chartY = y + Layout.smallRowHeight
    let half = Layout.mirrorHalfHeight
    let middle = chartY + half
    for (series, downward) in [(top, false), (bottom, true)] {
        let values = HistoryConfig.recent(series.history)
        // 表示中の最大値に合わせて縦軸を自動伸縮する
        let scale = niceCeil(values.max() ?? 0)
        let headerY = downward ? middle + half : y
        // 系列の色は文字に付けると灰色がかった背景で読みにくいので、凡例の四角で示す
        fillRect(x, headerY + 3, 8, 8, series.color, radius: 2)
        drawParts([TextPart("\(series.label)  "), TextPart(String(format: "%.2f %@", series.value, unit))],
                  x + 12, headerY, Fonts.small())
        // 起動からの合計は、どちらの向きの合計か分かるよう各系列の見出しに並べる
        drawTextRight(String(format: "Peak %.2f %@ · Total %@", series.peak, unit, series.totalText), x + width, headerY,
                      Fonts.small(), secondaryText())
        // 外側の端(上半分は上端、下半分は下端)に、縦軸の上限の目盛り線を薄く引く
        fillRect(x, downward ? middle + half - 0.5 : chartY, width, 0.5, .tertiaryLabelColor)
        drawHistoryChart(x, downward ? middle : chartY, width, half, values, scale, series.color, downward: downward)
        // 目盛り(縦軸の上限)は、グラフの外側の端(上半分は左上、下半分は左下)に小さく添える
        let font = Fonts.monoSmall()
        let labelY = downward ? middle + half - font.ascender + font.descender - 1 : chartY + 1
        drawText(String(format: "%g %@", scale, unit), x + 3, labelY, font, tertiaryText())
        if let note = series.note {
            drawTextRight(note, x + width - 3, labelY, font, tertiaryText())
        }
    }
    return y + Layout.mirrorChartHeight
}
