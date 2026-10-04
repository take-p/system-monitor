import AppKit

/// 左上原点で欄を上から並べるためのビュー
private final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

/// メニューと同じ欄を載せた、ほかの場所をクリックしても閉じないパネル。
/// メニューは外をクリックすると必ず閉じる仕組みなので、常に見ておきたいときはこちらに出す。
/// ほかのウィンドウより常に手前に出し、内容が画面に入りきらないときはスクロールする
@MainActor
final class MonitorPanel: NSObject, NSWindowDelegate {
    private static let frameName = "MonitorPanel"
    /// 欄の間の区切り線を含む隙間(メニューの区切り線に合わせる)
    private static let separatorBlock: CGFloat = 11
    private static let verticalMargin: CGFloat = 4

    private let panel: NSPanel
    private let container = FlippedView()
    private(set) var sectionViews: [SectionKey: SectionView] = [:]
    private var separators: [SectionKey: NSBox] = [:]
    /// 非表示にした欄(メニューと同じ設定)
    var hiddenSections: Set<SectionKey> = [] {
        didSet { layoutSections() }
    }
    /// ×で閉じたときに呼ぶ
    var onClose: (() -> Void)?

    var isVisible: Bool { panel.isVisible }

    override init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: Layout.menuWidth, height: 400),
                        styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel, .fullSizeContentView],
                        backing: .buffered, defer: true)
        super.init()
        panel.title = "MenubarMonitor"
        panel.titlebarAppearsTransparent = true
        panel.isFloatingPanel = true
        panel.level = .floating
        // このアプリはアクティブにならないので、既定のままだとほかのアプリを使っている間に隠れてしまう
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        // デスクトップ(操作スペース)を切り替えても、フルスクリーンのアプリの上でも出したままにする
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.delegate = self

        // 背景はメニューと同じすりガラス状にする
        let effect = NSVisualEffectView()
        effect.material = .menu
        effect.blendingMode = .behindWindow
        effect.state = .active
        panel.contentView = effect

        let scrollView = NSScrollView(frame: effect.bounds)
        scrollView.autoresizingMask = [.width, .height]
        scrollView.drawsBackground = false
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        // タイトルバーの下に潜らないよう、スクロールの範囲をタイトルバーの分だけ下げる
        scrollView.automaticallyAdjustsContentInsets = true
        scrollView.documentView = container
        effect.addSubview(scrollView)

        for key in SectionKey.allCases {
            let separator = NSBox()
            separator.boxType = .separator
            container.addSubview(separator)
            separators[key] = separator
            let view = SectionView()
            container.addSubview(view)
            sectionViews[key] = view
        }

        // 位置は前回の場所を覚えておく。初めて出すときは画面の右上に置く
        let hasSavedFrame = UserDefaults.standard.string(forKey: "NSWindow Frame \(Self.frameName)") != nil
        panel.setFrameAutosaveName(Self.frameName)
        if !hasSavedFrame, let screen = NSScreen.main {
            let visible = screen.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: visible.maxX - panel.frame.width - 20, y: visible.maxY - 10))
        }
    }

    func show() {
        layoutSections()
        // アプリをアクティブにせずに手前に出す
        panel.orderFrontRegardless()
    }

    func close() {
        panel.close()
    }

    func windowWillClose(_ notification: Notification) {
        onClose?()
    }

    /// 欄を上から並べ直し、パネルの高さを内容に合わせる(上端の位置は保つ)。欄の高さが変わったら呼ぶ
    func layoutSections() {
        var y = Self.verticalMargin
        var first = true
        for key in SectionKey.allCases {
            let hidden = hiddenSections.contains(key)
            guard let view = sectionViews[key], let separator = separators[key] else { continue }
            view.isHidden = hidden
            separator.isHidden = hidden || first
            guard !hidden else { continue }
            if !first {
                separator.frame = NSRect(x: Layout.padX, y: y + Self.separatorBlock / 2, width: Layout.menuWidth - Layout.padX * 2, height: 1)
                y += Self.separatorBlock
            }
            first = false
            view.frame = NSRect(x: 0, y: y, width: Layout.menuWidth, height: view.frame.height)
            y += view.frame.height
        }
        let contentHeight = y + Self.verticalMargin
        container.frame = NSRect(x: 0, y: 0, width: Layout.menuWidth, height: contentHeight)

        // 画面に入りきる範囲で、内容がすべて見える高さにする。入りきらない分はスクロールで見る
        let titlebarHeight = panel.frame.height - panel.contentLayoutRect.height
        let maxHeight = (panel.screen ?? NSScreen.main)?.visibleFrame.height ?? 800
        let height = min(contentHeight + titlebarHeight, maxHeight - 20)
        var frame = panel.frame
        guard abs(frame.height - height) > 0.5 || frame.width != Layout.menuWidth else { return }
        frame.origin.y += frame.height - height
        frame.size = NSSize(width: Layout.menuWidth, height: height)
        panel.setFrame(frame, display: true)
    }
}
