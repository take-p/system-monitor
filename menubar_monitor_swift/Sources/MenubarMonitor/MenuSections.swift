import AppKit

// メニューの各欄。いずれもSection(高さ・描画・クリック時の処理)を返す

private let padX = Layout.padX, padY = Layout.padY
private let rowHeight = Layout.rowHeight, smallRowHeight = Layout.smallRowHeight
private let gigabyte = 1024.0 * 1024 * 1024
private let storageGigabyte = 1_000_000_000.0

/// 「さらに表示」で次の表示件数を、「折りたたむ」で初期件数に戻すよう求める
struct PagerActions {
    var more: (Int) -> Void
    var collapse: () -> Void
}

/// 一覧の末尾に置く「さらに表示」「折りたたむ」の行。
/// shown件を表示中で、さらにremaining件あるとき、左側で次の件数を表示し、
/// 展開中(表示件数visibleCountが初期件数collapsedより多い)なら右端で初期件数に戻す。
/// ほかに無いときは左側にemptyNoteを出す
struct PagerRow {
    enum Zone { case more, collapse }

    var y: CGFloat
    var remaining: Int
    var nextVisible: Int
    var canCollapse: Bool
    /// 「残り 276グループ · 4.8 GB」のような補足
    var restNote: String
    var emptyNote: String
    var visible: Bool
    let collapseX = Layout.menuWidth - padX - Ranking.collapseZoneWidth

    init(y: CGFloat, shown: Int, remaining: Int, collapsed: Int, restNote: String, visibleCount: Int,
         emptyNote: String = "ほかのアプリはありません") {
        self.y = y
        self.remaining = remaining
        // 「さらに表示」で増やすのは、押した時点で実際に表示できる件数(最大page件)だけにする。
        // 確保する高さは表示件数で固定されるので、ボタンの「さらに◯件」と増える行数を一致させる
        nextVisible = shown + min(remaining, Ranking.page)
        // 展開中かどうかは表示件数で決める(展開後にアプリが減っても折りたためるように)
        canCollapse = visibleCount > collapsed
        self.restNote = restNote
        self.emptyNote = emptyNote
        visible = remaining > 0 || canCollapse
    }

    /// 指している側。「折りたたむ」は右端だけで反応する
    func zone(_ point: NSPoint?) -> Zone? {
        guard let point, visible, (y..<y + rowHeight).contains(point.y) else { return nil }
        if canCollapse && point.x >= collapseX { return .collapse }
        return remaining > 0 ? .more : nil
    }

    func click(_ point: NSPoint, _ actions: PagerActions?) {
        switch zone(point) {
        case .more: actions?.more(nextVisible)
        case .collapse: actions?.collapse()
        case nil: break
        }
    }

    /// showEmptyなら、折りたたまれていてもほかに無いことを示す文言を出す。
    /// noteがfalseなら左側の文言を出さない(一覧自体が空で、その旨を別に出しているとき)
    func draw(_ width: CGFloat, _ hover: NSPoint?, showEmpty: Bool = false, note: Bool = true) {
        guard visible || showEmpty else { return }
        let font = Fonts.body(), secondary = secondaryText()
        // 押せることが分かるよう、ホバー中の側だけ背景を薄く敷く
        switch zone(hover) {
        case .more:
            let right = canCollapse ? collapseX : width - padX + 6
            fillRect(padX - 6, y, right - (padX - 6), rowHeight, .quaternaryLabelColor, radius: 4)
        case .collapse:
            fillRect(collapseX, y, width - padX + 6 - collapseX, rowHeight, .quaternaryLabelColor, radius: 4)
        case nil:
            break
        }
        if remaining > 0 {
            var end = drawSymbol("chevron.down", padX, y, rowHeight, secondary)
            end = drawText("さらに\(min(remaining, Ranking.page))件表示", end + 4, y, font, secondary)
            // 補足が長い(上り/下りの値など)と右端の「折りたたむ」に重なるので、収まらなければ末尾を省略する
            let noteRight = canCollapse ? collapseX - 4 : width - padX
            drawTextFit("  \(restNote)", end, y + 2, noteRight - end, Fonts.small(), tertiaryText())
        } else if note {
            drawText(emptyNote, padX, y, font, tertiaryText())
        }
        if canCollapse {
            let text = attributed("折りたたむ", font, secondary)
            let textX = width - padX - text.size().width
            text.draw(at: NSPoint(x: textX, y: y))
            drawSymbol("chevron.up", textX - 4, y, rowHeight, secondary, alignRight: true)
        }
    }
}

/// アプリ別の一覧の1行
struct UsageEntry {
    var name: String
    /// 並び順とバーに使う値
    var value: Double
    /// ネットワークの上り/下り(Mbps)
    var up = 0.0
    var down = 0.0
}

/// 「Process (grouped)」の見出し、アプリごとの使用率(名前・バー・値)、末尾の「さらに表示」の行からなる表。
/// processesがnilなら集計中。値は既定では使用率(全体=100%)で、上位同士の比較ではなく全体に対する割合が
/// 分かるようバーも100%基準で描く。行数は数秒ごとに増減するので、表示件数分と末尾の行の高さは常に確保し、
/// アプリが足りない行は空けてメニューの揺れを抑える
struct UsageTable {
    var y: CGFloat
    var processes: [UsageEntry]?
    var shown: [UsageEntry]
    var valueHeader: String
    var color: NSColor
    var emptyText: String
    var barMax: Double
    var nameWidth: CGFloat
    var formatEntry: (UsageEntry) -> String
    var pager: PagerRow
    var height: CGFloat

    /// barMax: バーが満杯になる値、nameWidth: 名前の欄の幅(値の文字列が長い表では狭めてバーの幅を確保する)、
    /// formatEntry: 行から表示文字列を作る関数、formatRest: 一覧に出ていない残りの行から末尾の行の補足を作る関数
    init(y: CGFloat, processes: [UsageEntry]?, visible: Int, collapsed: Int, valueHeader: String, color: NSColor,
         emptyText: String, barMax: Double = 100, nameWidth: CGFloat = 210,
         formatEntry: ((UsageEntry) -> String)? = nil, formatRest: (([UsageEntry]) -> String)? = nil) {
        self.y = y
        self.processes = processes
        let all = processes ?? []
        shown = Array(all.prefix(visible))
        let remaining = Array(all.dropFirst(visible))
        self.valueHeader = valueHeader
        self.color = color
        self.emptyText = emptyText
        self.barMax = barMax
        self.nameWidth = nameWidth
        let formatValue = { (value: Double) in String(format: "%.1f%%", value) }
        self.formatEntry = formatEntry ?? { formatValue($0.value) }
        let restText = formatRest?(remaining) ?? formatValue(remaining.reduce(0) { $0 + $1.value })
        let rows = max(visible, collapsed)
        pager = PagerRow(y: y + smallRowHeight + rowHeight * CGFloat(rows), shown: shown.count, remaining: remaining.count,
                         collapsed: collapsed, restNote: "残り \(remaining.count)アプリ · \(restText)", visibleCount: visible)
        height = smallRowHeight + rowHeight * CGFloat(rows + 1)
    }

    func click(_ point: NSPoint, _ actions: PagerActions?) {
        pager.click(point, actions)
    }

    func draw(_ width: CGFloat, _ hover: NSPoint?) {
        let barX = padX + nameWidth + 10
        let valueRight = width - padX
        let font = Fonts.body()
        let mono = Fonts.mono(font.pointSize)
        // 値の欄は「959 KB/s」のような長い値でもバーに重ならないよう、表示中の値の幅に合わせる
        let valueWidth = max(shown.map { textWidth(formatEntry($0), mono) }.max() ?? 0, 40)
        let barLength = valueRight - valueWidth - 10 - barX
        let secondary = secondaryText()
        drawText("Process (grouped)", padX, y, Fonts.small(), secondary)
        drawTextRight(valueHeader, valueRight, y, Fonts.small(), secondary)

        var rowY = y + smallRowHeight
        guard let processes, !processes.isEmpty else {
            drawText(processes == nil ? "集計中…" : emptyText, padX, rowY, font, secondary)
            // 展開中なら「折りたたむ」だけは出す
            pager.draw(width, hover, note: false)
            return
        }
        for entry in shown {
            drawTextFit(entry.name, padX, rowY, nameWidth, font)
            // 値の文字列が長い(上り/下りなど)とバーの幅が取れないので、狭すぎるときは描かない
            if barLength >= 20 {
                drawBar(barX, rowY + 5, barLength, barMax > 0 ? min(entry.value / barMax, 1) : 0, color, h: 6)
            }
            drawTextRight(formatEntry(entry), valueRight, rowY, mono)
            rowY += rowHeight
        }
        pager.draw(width, hover, showEmpty: true)
    }
}

// MARK: - CPU

/// coreRows: [(ラベル, 使用率の履歴)] を表示する順に並べたもの、processes: アプリ別の使用率(nil=集計中)。
/// ランキングは上位visible件を出し、末尾の行で「さらに表示」「折りたたむ」を選べる
@MainActor
func cpuSection(percent: Double?, coreRows: [(label: String, history: [Double])], processes: [UsageEntry]?,
                visible: Int, actions: PagerActions?) -> Section {
    let heatHeight = CGFloat(coreRows.count) * (Layout.heatCellHeight + Layout.heatRowGap)
    // コアごとのヒートマップと見分けやすいよう、バーはメモリ欄と同じグレー1色にする
    let table = UsageTable(y: padY + Layout.titleHeight + heatHeight + smallRowHeight + 8, processes: processes,
                           visible: visible, collapsed: Ranking.collapsed, valueHeader: "CPU",
                           color: .secondaryLabelColor, emptyText: "CPUを使っているアプリはありません")
    return Section(title: "CPU", height: table.y + table.height + padY, draw: { width, hover in
        drawTitle(width, padY, "CPU", percentParts(percent))
        let labelFont = Fonts.monoSmall(7)
        let labelOffset = (Layout.heatCellHeight - labelFont.ascender + labelFont.descender) / 2
        // ラベル欄は一番長いラベルの幅に合わせる
        let labelWidth = coreRows.map { textWidth($0.label, labelFont) }.max() ?? 0
        let gridX = padX + labelWidth + 4
        let gridWidth = width - padX - gridX
        let count = HistoryConfig.count
        let cellWidth = gridWidth / CGFloat(count)
        let y = padY + Layout.titleHeight
        for (core, row) in coreRows.enumerated() {
            let rowY = y + CGFloat(core) * (Layout.heatCellHeight + Layout.heatRowGap)
            drawTextRight(row.label, gridX - 4, rowY + labelOffset, labelFont, secondaryText())
            fillRect(gridX, rowY, gridWidth, Layout.heatCellHeight, NSColor.quaternaryLabelColor.withAlphaComponent(0.25))
            let history = HistoryConfig.recent(row.history)
            let start = count - history.count
            for (i, value) in history.enumerated() {
                // セル間に0.5ptの隙間を空けて、時間の区切りが見えるようにする。
                // 履歴が長くセルが細いと隙間ばかりになるので、そのときは隙間を空けない
                fillRect(gridX + CGFloat(start + i) * cellWidth, rowY, cellWidth >= 3 ? cellWidth - 0.5 : cellWidth + 0.2,
                         Layout.heatCellHeight, heatColor(value))
            }
        }
        let footerY = y + heatHeight + 1
        drawText(HistoryConfig.label, gridX, footerY, Fonts.small(), tertiaryText())
        drawTextRight("現在", width - padX, footerY, Fonts.small(), tertiaryText())
        // CPUを使っているアプリの上位(CPU全体=100%)
        table.draw(width, hover)
    }, onClick: { table.click($0, actions) })
}

// MARK: - Memory

/// メモリ内訳バーの区分
@MainActor private let memoryCategories: [(label: String, color: NSColor, value: (MemoryUsage) -> UInt64)] = [
    ("App", .systemBlue, \.app),
    ("Wired", .systemRed, \.wired),
    ("Compressed", .systemYellow, \.compressed),
    ("Cached Files", .systemTeal, \.cached),
    ("Free/Other", .quaternaryLabelColor, \.free),
]

typealias MemoryGroup = (root: pid_t, name: String, count: Int, megabytes: Double, percent: Double)

/// mem: メモリ全体(nil=取得失敗)、groups: アプリ単位のメモリ使用量(nil=集計中)。
/// ランキングは上位visible件を出し、末尾の行で「さらに表示」と「折りたたむ」を選べる
@MainActor
func memorySection(mem: MemoryUsage?, groups: [MemoryGroup]?, visible: Int, actions: PagerActions?) -> Section {
    let groups = groups ?? []
    // 他ユーザーのプロセス等はサイズを取得できず0になる。1件ずつ並べても意味がないので最後に件数だけ出す
    let measured = groups.filter { $0.megabytes > 0 }
    let unmeasured = groups.count - measured.count
    let shown = Array(measured.prefix(visible)), remaining = Array(measured.dropFirst(visible))
    let showUnmeasured = remaining.isEmpty && unmeasured > 0
    let tableY = padY + Layout.titleHeight + Layout.barHeight + 6 + smallRowHeight * 2 + 8
    // 表示件数分の行を常に確保し(CPU・GPUの表と同じ)、末尾の行はその下の決まった位置に置く
    let rows = max(visible, shown.count + (showUnmeasured ? 1 : 0))
    let pager = PagerRow(y: tableY + smallRowHeight + rowHeight * CGFloat(rows), shown: shown.count,
                         remaining: remaining.count, collapsed: Ranking.collapsed,
                         restNote: "残り \(remaining.count)グループ · \(formatFootprint(remaining.reduce(0) { $0 + $1.megabytes }))",
                         visibleCount: visible, emptyNote: "ほかのグループはありません")
    let processRows = groups.isEmpty ? 1 : rows + (pager.visible ? 1 : 0)
    let height = tableY + smallRowHeight + rowHeight * CGFloat(processRows) + padY

    return Section(title: "Memory", height: height, draw: { width, hover in
        guard let mem else {
            drawTitle(width, padY, "Memory", percentParts(nil))
            return
        }
        drawTitle(width, padY, "Memory", [
            TextPart(String(format: "%.1f / %.0f GB  ", Double(mem.used) / gigabyte, Double(mem.total) / gigabyte)),
        ] + percentParts(mem.percent))

        // 内訳の積み上げバー
        let barY = padY + Layout.titleHeight
        let barWidth = width - padX * 2
        fillRect(padX, barY, barWidth, Layout.barHeight, .quaternaryLabelColor, radius: 2)
        var x = padX
        for category in memoryCategories {
            let w = barWidth * CGFloat(Double(category.value(mem)) / Double(mem.total))
            fillRect(x, barY, w, Layout.barHeight, category.color)
            x += w
        }

        // 凡例(3列×2行)
        let legendY = barY + Layout.barHeight + 6
        let columnWidth = barWidth / 3
        for (i, category) in memoryCategories.enumerated() {
            let lx = padX + CGFloat(i % 3) * columnWidth
            let ly = legendY + CGFloat(i / 3) * smallRowHeight
            fillRect(lx, ly + 3, 8, 8, category.color, radius: 2)
            drawText(String(format: "%@ %.1f GB", category.label, Double(category.value(mem)) / gigabyte), lx + 12, ly, Fonts.small())
        }

        // アプリ単位のメモリ使用量ランキング
        let nameWidth: CGFloat = 170
        let procsRight = padX + nameWidth + 40
        let barX = procsRight + 10
        let sizeRight = width - padX
        let barLength = sizeRight - 62 - barX
        let secondary = secondaryText()
        drawText("Process (grouped)", padX, tableY, Fonts.small(), secondary)
        drawTextRight("Procs", procsRight, tableY, Fonts.small(), secondary)
        drawTextRight("Footprint", sizeRight, tableY, Fonts.small(), secondary)

        var rowY = tableY + smallRowHeight
        let font = Fonts.body()
        if groups.isEmpty {
            drawText("集計中…", padX, rowY, font, secondary)
            return
        }
        let maxMB = measured.first?.megabytes ?? 1
        let mono = Fonts.mono(font.pointSize)
        for group in shown {
            drawTextFit(group.name, padX, rowY, nameWidth, font)
            drawTextRight(String(group.count), procsRight, rowY, mono, secondary)
            // 上の内訳バー(Wired=赤, Compressed=黄)と混同しないよう、バーはグレー1色にする
            drawBar(barX, rowY + 5, barLength, group.megabytes / maxMB, .secondaryLabelColor, h: 6)
            drawTextRight(formatFootprint(group.megabytes), sizeRight, rowY, mono)
            rowY += rowHeight
        }
        if showUnmeasured {
            drawText("サイズを取得できない \(unmeasured)グループ", padX, rowY, font, secondary)
        }
        pager.draw(width, hover)
    }, onClick: { pager.click($0, actions) })
}

// MARK: - GPU

/// gpu: GPU全体(nil=取得失敗)、processes: アプリ別の使用率(nil=集計中)
@MainActor
func gpuSection(gpu: GPUUsage?, history: [Double], processes: [UsageEntry]?, visible: Int, actions: PagerActions?) -> Section {
    let table = UsageTable(y: padY + Layout.titleHeight + Layout.percentChartHeight + 7, processes: processes,
                           visible: visible, collapsed: Ranking.topGPU, valueHeader: "GPU", color: .systemPurple,
                           emptyText: "GPUを使っているアプリはありません")
    return Section(title: "GPU", height: table.y + table.height + padY, draw: { width, hover in
        if let gpu {
            // 見出しの右: GPUが使用中のメモリと使用率
            let memory = gpu.memory.map {
                [TextPart("Memory Usage ", secondaryText()), TextPart(String(format: "%.1f GB  ", Double($0) / gigabyte))]
            } ?? []
            drawTitle(width, padY, "GPU", memory + percentParts(gpu.percent, "%.0f%%"))
        } else {
            drawTitle(width, padY, "GPU", percentParts(nil))
        }
        drawPercentChart(padX, padY + Layout.titleHeight, width - padX * 2, history, .systemPurple)
        // GPUを使っているアプリの上位。メモリ欄のランキングと同じ並び(名前・バー・値)にする
        table.draw(width, hover)
    }, onClick: { table.click($0, actions) })
}

// MARK: - Storage

/// storage: 容量(nil=取得失敗)、diskIO: 読み書き速度とビジー率、processes: アプリ別の読み書き速度(バイト/秒)。
/// 見出しの下に、ビジー率の推移(右上に今の読み書き速度)とアプリごとの表を置く。
/// 読み書き速度の推移は普段ほぼ平らで見る機会が少ないので、混み具合が分かるビジー率の推移を出す。
/// 残り容量は割合より絶対量で知りたいことが多いので、使用率のバーは置かず、見出しのFreeの値を色で警告する
@MainActor
func storageSection(storage: StorageUsage?, diskIO: DiskIO?, busyHistory: [Double], processes: [UsageEntry]?,
                    visible: Int, actions: PagerActions?) -> Section {
    let chartsY = padY + Layout.titleHeight
    let chartsHeight = diskIO != nil ? Layout.percentChartHeight : 0
    // アプリごとのビジー率(ディスクを占有していた時間)はsudoなしでは取れないので、
    // 全体のビジー率を各アプリの読み書き量の割合で配分した推定値を出す。合計は見出しのBusyと一致する。
    // 小さなファイルを大量に扱う処理は量の割りに時間がかかるため、実際の占有時間とはずれることがある
    var estimated: [UsageEntry]?
    if let diskIO, let processes {
        let totalRate = processes.reduce(0) { $0 + $1.value }
        estimated = processes.map { UsageEntry(name: $0.name, value: totalRate > 0 ? diskIO.busy * $0.value / totalRate : 0) }
    }
    let table = UsageTable(y: chartsY + chartsHeight + 4, processes: estimated, visible: visible, collapsed: Ranking.topDisk,
                           valueHeader: "Busy (est.)", color: .systemTeal, emptyText: "ディスクを読み書きしているアプリはありません")
    return Section(title: "Storage", height: table.y + table.height + padY, draw: { width, hover in
        if let storage {
            // 見出しの右: 残り容量(パージ可能領域を含む)/全体と、ビジー率。どちらもメニューバーのSSDの表示と同じ値
            let secondary = secondaryText()
            let busy = diskIO.map { [TextPart("   Busy ", secondary), TextPart(String(format: "%.0f%%", $0.busy))] } ?? []
            drawTitle(width, padY, "Storage", [
                TextPart("Free ", secondary),
                TextPart(String(format: "%.0f GB", Double(storage.available) / storageGigabyte), FreeSpace.color(storage.available)),
                TextPart(String(format: " / %.0f GB", Double(storage.total) / storageGigabyte)),
            ] + busy)
        } else {
            drawTitle(width, padY, "Storage", percentParts(nil))
        }
        // ビジー率の推移(GPUの使用率と同じく0〜100%固定)。色はGPU(紫)・ネットワーク(赤/青)と区別できる青緑
        if let diskIO {
            drawPercentChart(padX, chartsY, width - padX * 2, busyHistory, .systemTeal)
            // 今の読み書き速度は、左上の目盛り(100%)の反対側に添える(ネットワークと同じく↑書き込み・↓読み込みの順)
            drawTextRight(String(format: "↑ %.1f MB/s   ↓ %.1f MB/s", diskIO.write, diskIO.read), width - padX - 3,
                          chartsY + 1, Fonts.monoSmall(), secondaryText())
        }
        // ディスクを読み書きしているアプリの上位
        table.draw(width, hover)
    }, onClick: { table.click($0, actions) })
}

// MARK: - Network

/// Wi-Fiアイコンは緑や黄だと灰色がかったメニュー背景で見えにくいので、見出しのアイコンと同じ標準色にする。
/// 点灯が1本以下(弱い・悪い)のときだけ、注意を引くよう赤にする
private let alertSignalLevels: Set<SignalLevel> = [.weak, .poor]

/// 電波の評価 -> Wi-Fiアイコンの点灯段数(0〜1。アイコンは点+3本の扇形)
private func signalIconLevel(_ level: SignalLevel) -> Double {
    switch level {
    case .excellent: 1
    case .good: 0.75
    case .fair: 0.5
    case .weak: 0.25
    case .poor: 0
    }
}

private func congestionColor(_ level: CongestionLevel) -> NSColor {
    switch level {
    case .free: .systemGreen
    case .moderate: .systemYellow
    case .busy: .systemOrange
    case .heavy: .systemRed
    }
}

/// net: ネットワークの状態(nil=取得失敗)、congestion: Wi-Fiの混雑度(Wi-Fi以外・未接続ならnil)、
/// processes: アプリ別の上り/下り(nil=集計中)。欄の末尾にアプリ別の上り/下りの表を置く
@MainActor
func networkSection(net: NetworkStatus?, downloadHistory: [Double], uploadHistory: [Double], congestion: CongestionSnapshot?,
                    processes: [UsageEntry]?, visible: Int, actions: PagerActions?) -> Section {
    // 通信品質の目安としては電波の強さ(RSSI)よりノイズとの差(SNR)が的確なので、
    // アイコンはSNRで決める。ノイズが取れずSNRが無い場合だけSignalで代用する
    let signalRating = net?.quality.map { $0.snrRating ?? $0.signalRating }
    // Link + 接続(有線でも種類とインターフェース名を出す) + 電波
    let textRows = net != nil ? 2 + (signalRating != nil ? 1 : 0) : 0
    let congestionBlock = congestionHeight(congestion)
    // バーは回線全体(グラフのUpload+Download)に占める割合。計測のずれでアプリが全体を上回ることがあるので1位も下限にする
    let lineTotal = net.map { $0.upload + $0.download } ?? 0
    let barMax = max(lineTotal, processes?.first?.value ?? 0)
    // アプリ別の表は、接続の詳細(電波・リンク・規格・混雑度)の下、欄の末尾に置く
    let tableY = padY + Layout.titleHeight + Layout.mirrorChartHeight + 4 + rowHeight * CGFloat(textRows) + congestionBlock + 4
    let table = UsageTable(
        y: tableY, processes: processes, visible: visible, collapsed: Ranking.topNet, valueHeader: "↑ / ↓  Share",
        // バーは上り+下りの合計なので、Download(青)と紛らわしくないようグレーにする
        color: .secondaryLabelColor, emptyText: "通信しているアプリはありません", barMax: barMax, nameWidth: 140,
        // 値の後ろに、バーと同じ基準(回線全体の上り+下りに占める割合)の数値を添える
        formatEntry: { entry in
            "\(formatMbps(entry.up)) / \(formatMbps(entry.down)) Mbps  "
                + String(format: "%3.0f%%", barMax > 0 ? min(entry.value / barMax, 1) * 100 : 0)
        },
        formatRest: { rest in
            "\(formatMbps(rest.reduce(0) { $0 + $1.up })) / \(formatMbps(rest.reduce(0) { $0 + $1.down })) Mbps"
        })

    return Section(title: "Network", height: table.y + table.height + padY, draw: { width, hover in
        guard let net else {
            drawTitle(width, padY, "Network", percentParts(nil))
            return
        }
        let font = Fonts.body()
        let secondary = secondaryText()

        // 見出しの右: 今のアップロード/ダウンロードの速度(接続の種類とインターフェース名は「接続」の行に出す)
        drawTitle(width, padY, "Network", [
            TextPart("↑ ", secondary), TextPart("\(formatMbps(net.upload)) Mbps   "),
            TextPart("↓ ", secondary), TextPart("\(formatMbps(net.download)) Mbps"),
        ])
        var y = padY + Layout.titleHeight

        // 速度のグラフは一番よく見る情報なので、CPU/GPUと同じく見出しのすぐ下に置く。
        // メニューバーのNET表示(上段↑/下段↓)とそろえて、上り→下りの順に並べる。
        // エラー/ドロップ数(起動からの合計)は、目盛りと同じ高さの右端に小さく添える。
        // macOSでは送信側のドロップ数を取得できないので、0ではなく「—」で示す
        let totals = net.totals
        y = drawMirrorChart(
            padX, y, width - padX * 2, "Mbps",
            top: MirrorSeries(label: "↑ Upload", value: net.upload, peak: net.peakUpload, totalText: formatBytes(totals.bytesSent),
                              history: uploadHistory, color: .systemRed, note: "Err \(totals.errorsOut) · Drop —"),
            bottom: MirrorSeries(label: "↓ Download", value: net.download, peak: net.peakDownload,
                                 totalText: formatBytes(totals.bytesReceived), history: downloadHistory, color: .systemBlue,
                                 note: "Err \(totals.errorsIn) · Drop \(totals.dropsIn)"))

        // 接続の詳細(電波・リンク速度・規格・混雑度)は速度の原因を調べるための情報なのでグラフの下に置く
        y += 4
        let labelWidth: CGFloat = 44  // 「電波」「Link」の見出し列の幅

        // 電波品質: 左にWi-Fiアイコン(点灯段数=SNRの評価)とSNR、右に電波の強さとノイズを添える
        if let signalRating, let quality = net.quality {
            drawText("電波", padX, y, font, secondary)
            let iconColor: NSColor = alertSignalLevels.contains(signalRating.level) ? .systemRed : .labelColor
            var end = drawSymbol("wifi", padX + labelWidth, y, rowHeight, iconColor, variable: signalIconLevel(signalRating.level))
            end = drawText(" \(signalRating.label)", end + 2, y, Fonts.bold(font.pointSize))
            if let snr = quality.snr {
                end = drawText("  SNR \(snr) dB", end, y, font)
            }

            // 右側は左側と重ならない幅に収める。入りきらなければ単位→評価の順に省き、それでも駄目なら末尾を省略する
            func details(unit: Bool, withRating: Bool) -> String {
                var parts = ["Signal \(quality.signal)\(unit ? " dBm" : "")"
                    + (withRating ? " (\(quality.signalRating.label))" : "")]
                if let noise = quality.noise {
                    parts.append("Noise \(noise)\(unit ? " dBm" : "")")
                }
                return parts.joined(separator: " · ")
            }
            let available = width - padX - end - 8
            var text = ""
            for (unit, withRating) in [(true, true), (false, true), (false, false)] {
                text = details(unit: unit, withRating: withRating)
                if textWidth(text, Fonts.small()) <= available { break }
            }
            let textW = min(textWidth(text, Fonts.small()), available)
            drawTextFit(text, width - padX - textW, y + 2, available, Fonts.small(), secondary)
            y += rowHeight
        }

        // リンク速度: 理論最大速度との差が分かるようゲージで見せる
        drawText("Link", padX, y, font, secondary)
        if let link = net.linkSpeed, let maxRate = net.maxRate {
            let value = String(format: "%.0f / %.0f Mbps", link, maxRate)
            let valueWidth = textWidth(value, font)
            let gaugeWidth = width - padX * 2 - labelWidth - valueWidth - 12
            // 青はDownloadのグラフと紛らわしいので、上り/下りのどちらでもないリンク速度はグレーにする
            drawBar(padX + labelWidth, y + 5, gaugeWidth, link / maxRate, .secondaryLabelColor)
            drawText(value, width - padX - valueWidth, y, font)
        } else {
            // 理論最大速度が分からない(有線・推定中など)場合はリンク速度だけを出す
            drawText(net.linkSpeed.map { String(format: "%.0f Mbps", $0) } ?? "N/A", padX + labelWidth, y, font)
        }
        y += rowHeight

        // 接続: 「Wi-Fi 5 (802.11ac, 5GHz) / 80MHz (ch 52–64) / en0」。有線は「Ethernet / en7」
        drawText("接続", padX, y, font, secondary)
        drawTextFit("\(net.connection ?? net.kind) / \(net.iface ?? "N/A")", padX + labelWidth, y,
                    width - padX * 2 - labelWidth, font)
        y += rowHeight

        if let congestion {
            drawCongestion(congestion, padX, y, width, labelWidth, hover)
        }

        // 通信しているアプリの上位(上り/下り)
        table.draw(width, hover)
    }, onClick: { table.click($0, actions) })
}

private func congestionHeight(_ congestion: CongestionSnapshot?) -> CGFloat {
    guard let congestion else { return 0 }
    return Layout.congestionRowHeight * CGFloat(congestion.bands?.count ?? 1) + smallRowHeight + 2
}

/// 全帯域のチャネル別混雑度を、帯域ごとに色付きセルの1列(ヒートマップ風)で描く。
/// セルの大きさは全帯域でそろえ、接続中のチャネル群は枠で囲む。
/// 右端にはどの帯域もその帯域で最も空いているチャネルを出し、接続中の混雑度は下の補足の行の先頭に出す
private func drawCongestion(_ congestion: CongestionSnapshot, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat,
                            _ labelWidth: CGFloat, _ hover: NSPoint?) {
    let font = Fonts.body()
    let secondary = secondaryText()
    drawText("混雑", x, y, font, secondary)
    guard let bands = congestion.bands, !bands.isEmpty else {
        drawText("スキャン中…", x + labelWidth, y, font, secondary)
        return
    }

    // 帯域名の欄は最も長い「2.4GHz」の幅に合わせる
    let bandLabelWidth = textWidth("2.4GHz", Fonts.small()) + 6
    let summaryWidth: CGFloat = 96
    let stripX = x + labelWidth + bandLabelWidth
    let stripWidth = width - padX - summaryWidth - 8 - stripX
    let cellWidth = stripWidth / CGFloat(bands.map(\.channels.count).max() ?? 1)
    let cellHeight: CGFloat = 10
    var hovered: (band: String, number: Int, percent: Double, apCount: Int, cellX: CGFloat, cellY: CGFloat)?
    for (row, band) in bands.enumerated() {
        let rowY = y + CGFloat(row) * Layout.congestionRowHeight
        let cellY = rowY + (rowHeight - cellHeight) / 2
        drawText(band.band, x + labelWidth, rowY + 2, Fonts.small(), secondary)
        let isCurrent = band.band == congestion.currentBand
        var currentIndex: [Int] = []
        for (i, channel) in band.channels.enumerated() {
            let cellX = stripX + CGFloat(i) * cellWidth
            fillRect(cellX, cellY, max(cellWidth - 1.5, 1), cellHeight, congestionColor(congestionRating(channel.percent).level), radius: 2)
            if isCurrent && congestion.current.contains(channel.number) {
                currentIndex.append(i)
            }
            // マウスが乗っているセル(行の高さ全体を判定範囲にする)
            if let hover, (cellX..<cellX + cellWidth).contains(hover.x), (rowY..<rowY + Layout.congestionRowHeight).contains(hover.y) {
                hovered = (band.band, channel.number, channel.percent, band.apCounts[channel.number] ?? 0, cellX, cellY)
            }
        }
        if let first = currentIndex.min(), let last = currentIndex.max() {
            // 接続中のチャネル群(80MHzなら4ch分)を枠で囲む
            let left = stripX + CGFloat(first) * cellWidth - 1.5
            let right = stripX + CGFloat(last + 1) * cellWidth
            let frame = NSBezierPath(roundedRect: NSRect(x: left, y: cellY - 2, width: right - left, height: cellHeight + 4),
                                     xRadius: 3, yRadius: 3)
            frame.lineWidth = 1.5
            NSColor.labelColor.setStroke()
            frame.stroke()
        }

        drawTextRight(String(format: "空き ch %d (%.0f%%)", band.best.number, band.best.percent), width - padX, rowY + 2,
                      Fonts.small(), secondary)
    }

    let noteY = y + CGFloat(bands.count) * Layout.congestionRowHeight
    if let hovered {
        // ホバー中のセルを枠で強調し、補足の行をそのチャネルの情報に差し替える
        let frame = NSBezierPath(roundedRect: NSRect(x: hovered.cellX - 1, y: hovered.cellY - 1, width: cellWidth + 0.5,
                                                     height: cellHeight + 2), xRadius: 2.5, yRadius: 2.5)
        frame.lineWidth = 1.5
        NSColor.labelColor.setStroke()
        frame.stroke()
        drawParts([TextPart("\(hovered.band) ch \(hovered.number)", bold: true),
                   TextPart(String(format: " · %.0f%% %@ · AP %d台", hovered.percent, congestionRating(hovered.percent).label, hovered.apCount))],
                  x + labelWidth, noteY, Fonts.small())
        return
    }
    // 通常時は、接続中の混雑度と、スキャン結果がいつのものかを出す(接続中のチャネルは枠と「接続」の行で分かる)
    var parts: [TextPart] = []
    if let percent = congestion.currentPercent {
        parts += [TextPart(String(format: "%.0f%% ", percent)), TextPart(congestionRating(percent).label, bold: true),
                  TextPart("  ")]
    }
    let note = congestion.scanning ? "スキャン中" : congestion.age.map { String(format: "%.0f秒前の推定値", $0) } ?? ""
    drawParts(parts + [TextPart(note, secondary)], x + labelWidth, noteY, Fonts.small())
}
