import AppKit

/// メニューバーの項目と、定期的な更新を受け持つ
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// 更新間隔(秒)。Python版のUPDATE_INTERVALと同じ
    static let updateInterval: TimeInterval = 2.0

    private var statusItem: NSStatusItem!
    private let cpu = CPUSampler()
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = menu

        // 初回は基準値の取得のみ
        _ = cpu.sample()
        update()
        let timer = Timer(timeInterval: Self.updateInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.update() }
        }
        // CommonModesに登録しないと、メニューを開いている間は更新が止まる
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func update() {
        let percent = cpu.sample()
        statusItem.button?.title = percent.map { String(format: "CPU %.0f%%", $0) } ?? "CPU --"
    }
}
