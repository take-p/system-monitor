import AppKit

/// メニューの欄とメニューバーの項目の対応。並びはメニューバーと同じCPU/RAM/GPU/SSD/NETの順
enum SectionKey: String, CaseIterable {
    case cpu, memory, gpu, storage, network

    /// メニューでの名前
    var title: String {
        switch self {
        case .cpu: "CPU"
        case .memory: "Memory"
        case .gpu: "GPU"
        case .storage: "Storage"
        case .network: "Network"
        }
    }
}

/// メニューバーの項目と、定期的な更新・メニューの表示を受け持つ
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    /// 更新間隔。推移グラフの記録間隔と同じにする
    static let updateInterval = HistoryConfig.step
    /// メニューの「履歴の長さ」で選べる長さ(分)。最長はHistoryConfig.maxCountに合わせる
    private static let historyMinutesChoices = [1, 2, 5, 10]
    private static let defaultHistoryMinutes = 2

    private var statusItem: NSStatusItem!
    private var barHeight: CGFloat = 22
    private let defaults = UserDefaults.standard

    // 取得
    private let cpu = CPUSampler()
    private let diskIO = DiskIOSampler()
    private let network = NetworkSampler()
    private let congestion = CongestionScanner()
    private let collector = ProcessCollector()

    // 直近の値
    private var cpuPercent: Double?
    private var memory: MemoryUsage?
    private var gpu: GPUUsage?
    private var storage: StorageUsage?
    private var disk: DiskIO?
    private var net: NetworkStatus?

    // 推移グラフ用の履歴。メニューを閉じている間も記録し続ける
    private var coreHistories: [HistoryBuffer] = []
    private var coreLabels: [String] = []
    /// メニューでの並び順(高性能コア(P)を上、高効率コア(E)を下。各種類内は番号順)
    private var coreOrder: [Int] = []
    private var gpuHistory = HistoryBuffer()
    private var downloadHistory = HistoryBuffer()
    private var uploadHistory = HistoryBuffer()
    private var diskBusyHistory = HistoryBuffer()

    // アプリ別の一覧(メニューを開いている間だけ集計する。nil=集計中)
    private var menuOpen = false
    private var memoryGroups: [MemoryGroup]?
    private var cpuProcesses: [UsageEntry]?
    private var gpuProcesses: [UsageEntry]?
    private var diskProcesses: [UsageEntry]?
    private var netProcesses: [UsageEntry]?
    /// 集計中ならtrue(前回の集計が終わっていなければ次を飛ばし、処理が積み上がらないようにする)
    private var collecting = false
    /// メニューを閉じるたびに増やす。閉じる前に始めた集計の結果を、次に開いたメニューに出さないために使う
    private var menuGeneration = 0
    /// 一覧に出している件数(メニューを閉じると初期件数に戻す)
    private var visibleCounts: [SectionKey: Int] = [:]

    // 設定
    private var displayMode = DisplayMode.number
    private var historyMinutes = defaultHistoryMinutes
    /// 非表示にした項目(メニューバーの項目とメニュー内の欄の両方を隠す)
    private var hiddenSections: Set<SectionKey> = []

    // メニュー
    private var sectionViews: [SectionKey: SectionView] = [:]
    private var sectionItems: [SectionKey: NSMenuItem] = [:]
    /// 各欄の上に置く区切り線(一番上に見えている欄では隠す)
    private var sectionSeparators: [SectionKey: NSMenuItem] = [:]
    private var visibilityItems: [SectionKey: NSMenuItem] = [:]
    private var modeItems: [DisplayMode: NSMenuItem] = [:]
    private var historyItems: [Int: NSMenuItem] = [:]

    private static func initialVisible(_ key: SectionKey) -> Int {
        switch key {
        case .cpu, .memory: Ranking.collapsed
        case .gpu: Ranking.topGPU
        case .storage: Ranking.topDisk
        case .network: Ranking.topNet
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let statusBar = NSStatusBar.system
        statusItem = statusBar.statusItem(withLength: NSStatusItem.variableLength)
        barHeight = statusBar.thickness
        resetVisibleCounts()
        loadSettings()

        let coreCount = ProcessInfo.processInfo.activeProcessorCount
        coreHistories = Array(repeating: HistoryBuffer(), count: coreCount)
        coreLabels = CPUSampler.coreLabels(count: coreCount)
        coreOrder = (0..<coreCount).filter { coreLabels[$0].hasPrefix("P") } + (0..<coreCount).filter { !coreLabels[$0].hasPrefix("P") }

        statusItem.menu = buildMenu()

        // 初回は基準値の取得のみ
        _ = cpu.sample()
        update()
        let timer = Timer(timeInterval: Self.updateInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.update() }
        }
        // commonモードに登録しないと、メニューを開いている間は更新が止まる
        RunLoop.main.add(timer, forMode: .common)
    }

    // MARK: - 設定

    private func loadSettings() {
        displayMode = defaults.string(forKey: "displayMode").flatMap(DisplayMode.init) ?? .number
        let minutes = defaults.integer(forKey: "historyMinutes")
        historyMinutes = Self.historyMinutesChoices.contains(minutes) ? minutes : Self.defaultHistoryMinutes
        HistoryConfig.setMinutes(historyMinutes)
        hiddenSections = Set((defaults.stringArray(forKey: "hiddenSections") ?? []).compactMap(SectionKey.init))
        if hiddenSections.count >= SectionKey.allCases.count {
            hiddenSections = []  // 全部隠れているとメニューを開けなくなるので戻す
        }
    }

    private func resetVisibleCounts() {
        for key in SectionKey.allCases {
            visibleCounts[key] = Self.initialVisible(key)
        }
    }

    // MARK: - メニュー

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        for key in SectionKey.allCases {
            let separator = NSMenuItem.separator()
            menu.addItem(separator)
            sectionSeparators[key] = separator
            let view = SectionView()
            let item = NSMenuItem()
            item.view = view
            menu.addItem(item)
            sectionViews[key] = view
            sectionItems[key] = item
        }
        menu.addItem(.separator())

        // 表示する項目の切り替えは、メニューが長くならないようサブメニューにまとめる
        let sectionsMenu = NSMenu()
        // 最後の1つを外せなくするのにisEnabledを使うので、自動での有効化を切る
        sectionsMenu.autoenablesItems = false
        for key in SectionKey.allCases {
            let item = sectionsMenu.addItem(withTitle: key.title, action: #selector(toggleSection(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = key.rawValue
            visibilityItems[key] = item
        }
        menu.addItem(withTitle: "表示項目", action: nil, keyEquivalent: "").submenu = sectionsMenu
        applySectionVisibility()

        // 表示形式と履歴の長さも、メニューが長くならないようサブメニューにする
        let modes = addChoiceSubmenu(menu, "表示形式", DisplayMode.allCases.map { ($0.rawValue, $0.title) },
                                     #selector(changeDisplayMode(_:)))
        modeItems = Dictionary(uniqueKeysWithValues: modes.compactMap { key, item in DisplayMode(rawValue: key).map { ($0, item) } })
        syncModeCheckmarks()
        let histories = addChoiceSubmenu(menu, "履歴の長さ", Self.historyMinutesChoices.map { (String($0), "\($0)分") },
                                         #selector(changeHistoryLength(_:)))
        historyItems = Dictionary(uniqueKeysWithValues: histories.compactMap { key, item in Int(key).map { ($0, item) } })
        syncHistoryCheckmarks()

        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        return menu
    }

    /// choices: [(値, 項目名)] から選択肢のサブメニューを作ってmenuに足し、[(値, 項目)] を返す
    private func addChoiceSubmenu(_ menu: NSMenu, _ title: String, _ choices: [(String, String)],
                                  _ action: Selector) -> [(String, NSMenuItem)] {
        let submenu = NSMenu()
        let items = choices.map { value, label in
            let item = submenu.addItem(withTitle: label, action: action, keyEquivalent: "")
            item.target = self
            item.representedObject = value
            return (value, item)
        }
        menu.addItem(withTitle: title, action: nil, keyEquivalent: "").submenu = submenu
        return items
    }

    /// 非表示の設定を、メニュー内の欄・区切り線・サブメニューのチェックに反映する
    private func applySectionVisibility() {
        var first = true
        for key in SectionKey.allCases {
            let hidden = hiddenSections.contains(key)
            sectionItems[key]?.isHidden = hidden
            sectionSeparators[key]?.isHidden = hidden || first
            first = first && hidden
            visibilityItems[key]?.state = hidden ? .off : .on
        }
        // 最後の1つまで隠すとメニューバーのアイコンが消えてメニューを開けなくなるので、外せないようにする
        let visible = SectionKey.allCases.filter { !hiddenSections.contains($0) }
        for (key, item) in visibilityItems {
            item.isEnabled = !(visible.count == 1 && visible.contains(key))
        }
    }

    private func syncModeCheckmarks() {
        for (mode, item) in modeItems {
            item.state = mode == displayMode ? .on : .off
        }
    }

    private func syncHistoryCheckmarks() {
        for (minutes, item) in historyItems {
            item.state = minutes == historyMinutes ? .on : .off
        }
    }

    @objc private func changeDisplayMode(_ sender: NSMenuItem) {
        guard let mode = (sender.representedObject as? String).flatMap(DisplayMode.init) else { return }
        displayMode = mode
        defaults.set(mode.rawValue, forKey: "displayMode")
        syncModeCheckmarks()
        // 次のタイマーを待たず、直近の値のまま描き直す
        render()
    }

    @objc private func toggleSection(_ sender: NSMenuItem) {
        guard let key = (sender.representedObject as? String).flatMap(SectionKey.init) else { return }
        if hiddenSections.contains(key) {
            hiddenSections.remove(key)
        } else {
            hiddenSections.insert(key)
        }
        defaults.set(hiddenSections.map(\.rawValue).sorted(), forKey: "hiddenSections")
        applySectionVisibility()
        render()
    }

    @objc private func changeHistoryLength(_ sender: NSMenuItem) {
        guard let minutes = (sender.representedObject as? String).flatMap({ Int($0) }) else { return }
        historyMinutes = minutes
        defaults.set(minutes, forKey: "historyMinutes")
        syncHistoryCheckmarks()
        // 記録は最長分を取ってあるので、表示する長さを変えるだけで過去の分もすぐ出る
        HistoryConfig.setMinutes(minutes)
        if menuOpen {
            refreshSections()
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        guard menu === statusItem.menu else { return }
        menuOpen = true
        // Wi-Fiスキャンは通信を一瞬遅らせるので、メニューを開いている間だけ行う
        congestion.activate()
        refreshProcesses()
        refreshSections()
    }

    func menuDidClose(_ menu: NSMenu) {
        guard menu === statusItem.menu else { return }
        menuOpen = false
        congestion.deactivate()
        menuGeneration += 1
        // 次に開いたとき、閉じていた間の平均ではなく直近の使用率を出すため基準を捨てる
        collector.reset()
        gpuProcesses = nil
        cpuProcesses = nil
        diskProcesses = nil
        netProcesses = nil
        // 「さらに表示」で広げた一覧は、次に開いたときは初期件数に戻しておく
        resetVisibleCounts()
    }

    // MARK: - 更新

    private func update() {
        if let sample = cpu.sample() {
            cpuPercent = sample.total
            for (i, value) in sample.cores.enumerated() where i < coreHistories.count {
                coreHistories[i].append(value)
            }
        }
        memory = MemoryInfo.usage()
        gpu = GPUInfo.usage()
        if let gpu { gpuHistory.append(gpu.percent) }
        storage = StorageInfo.usage()
        let disk = diskIO.sample()
        self.disk = disk
        diskBusyHistory.append(disk.busy)
        let net = network.sample()
        self.net = net
        downloadHistory.append(net.download)
        uploadHistory.append(net.upload)
        render()

        if menuOpen {
            refreshProcesses()
            refreshSections()
        }
    }

    private func render() {
        func percentSegment(_ label: String, _ value: Double?, suffix: [[TextPart]]? = nil) -> StatusSegment {
            StatusSegment(label: label, value: value.map { .percent($0) } ?? .unavailable,
                          color: value.flatMap(valueColor), suffix: suffix)
        }
        let segments: [(SectionKey, StatusSegment)] = [
            (.cpu, percentSegment("CPU", cpuPercent)),
            (.memory, percentSegment("RAM", memory?.percent)),
            (.gpu, percentSegment("GPU", gpu?.percent)),
            // SSDは容量(ほとんど変わらない)ではなく、今の混み具合が分かるビジー率を出す。容量はメニュー内に出す。
            // 右に残り容量(パージ可能領域を含む。Finderと同じ)を「Free / 142GB」の2段で添える
            (.storage, percentSegment("SSD", disk?.busy, suffix: StatusImage.freeLines(storage?.available))),
            (.network, StatusSegment(label: "NET", value: net.map {
                .lines([StatusImage.rateLine("↑", $0.upload), StatusImage.rateLine("↓", $0.download)])
            } ?? .unavailable)),
        ]
        // 非表示にした項目はメニューバーからも外す
        let visible = segments.filter { !hiddenSections.contains($0.0) }.map(\.1)
        statusItem.button?.image = StatusImage.build(visible, height: barHeight, mode: displayMode)
    }

    /// アプリ別の集計はメニューを開いている間だけ、バックグラウンドで行う。結果が届いたら欄を描き直す
    private func refreshProcesses() {
        guard !collecting else { return }
        collecting = true
        let generation = menuGeneration
        collector.collect { [weak self] results in
            guard let self else { return }
            collecting = false
            guard generation == menuGeneration else {
                // 閉じる前に始めた集計だった。もう開き直していれば、改めて集計する
                if menuOpen { refreshProcesses() }
                return
            }
            // メモリの一覧は閉じても残し、次に開いたときに前回の一覧を出して欄の高さの変化を抑える
            memoryGroups = results.memory
            cpuProcesses = results.cpu
            gpuProcesses = results.gpu
            diskProcesses = results.disk
            netProcesses = results.net
            if menuOpen { refreshSections() }
        }
    }

    private func actions(_ key: SectionKey) -> PagerActions {
        PagerActions(more: { [weak self] visible in
            self?.visibleCounts[key] = visible
            // 次のタイマーを待たず、その場で高さを変えて描き直す
            self?.refreshSections()
        }, collapse: { [weak self] in
            self?.visibleCounts[key] = Self.initialVisible(key)
            self?.refreshSections()
        })
    }

    private func refreshSections() {
        func visible(_ key: SectionKey) -> Int { visibleCounts[key] ?? Self.initialVisible(key) }
        let sections: [SectionKey: Section] = [
            .cpu: cpuSection(percent: cpuPercent, coreRows: coreOrder.map { (coreLabels[$0], coreHistories[$0].values) },
                             processes: cpuProcesses, visible: visible(.cpu), actions: actions(.cpu)),
            .memory: memorySection(mem: memory, groups: memoryGroups, visible: visible(.memory), actions: actions(.memory)),
            .gpu: gpuSection(gpu: gpu, history: gpuHistory.values, processes: gpuProcesses, visible: visible(.gpu),
                             actions: actions(.gpu)),
            .storage: storageSection(storage: storage, diskIO: disk, busyHistory: diskBusyHistory.values,
                                     processes: diskProcesses, visible: visible(.storage), actions: actions(.storage)),
            .network: networkSection(net: net, downloadHistory: downloadHistory.values, uploadHistory: uploadHistory.values,
                                     congestion: congestion.snapshot(), processes: netProcesses, visible: visible(.network),
                                     actions: actions(.network)),
        ]
        for (key, section) in sections {
            sectionViews[key]?.setSection(section)
        }
    }
}
