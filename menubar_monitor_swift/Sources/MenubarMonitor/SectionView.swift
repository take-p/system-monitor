import AppKit

/// メニューの1つの欄。高さ・描画・クリック時の処理をまとめて差し替える
struct Section {
    var height: CGFloat
    /// (幅, ホバー中のマウス位置 or nil)で中身を描く
    var draw: (CGFloat, NSPoint?) -> Void
    var onClick: ((NSPoint) -> Void)?
}

/// Sectionの描画関数で中身を描くビュー。文字もグラフもdraw(_:)内で自前で描く。
/// メニュー表示中もマウスの移動を受け取り、位置が変わるたびに描き直す
final class SectionView: NSView {
    private var section: Section?
    private var hoverPoint: NSPoint?
    /// trueなら、押したままマウスを動かすとウィンドウごと動かす(パネルで使う。メニューでは動かさない)
    var allowsWindowDrag = false
    /// ドラッグの起点(押した時点のイベント)と、ドラッグに切り替えたかどうか
    private var mouseDownEvent: NSEvent?
    private var dragging = false
    /// これ以上動いたらクリックではなくドラッグとみなす距離(pt)
    private static let dragThreshold: CGFloat = 3

    override var isFlipped: Bool { true }

    init() {
        super.init(frame: NSRect(x: 0, y: 0, width: Layout.menuWidth, height: 1))
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    override func updateTrackingAreas() {
        trackingAreas.forEach(removeTrackingArea)
        // メニュー表示中はアプリがアクティブでないため、activeAlwaysでないとイベントが来ない
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
        super.updateTrackingAreas()
    }

    override func mouseMoved(with event: NSEvent) {
        hoverPoint = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        hoverPoint = nil
        needsDisplay = true
    }

    // パネルはアクティブにならないので、最初のクリックからボタンや「さらに表示」に反応させる
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    // ウィンドウのドラッグはmouseDraggedで自前で始めるので、AppKitの背景ドラッグには任せない(クリックが届かなくなる)
    override var mouseDownCanMoveWindow: Bool { false }

    override func mouseDown(with event: NSEvent) {
        // メニューでは押した時点の扱いをメニューに任せる(ここで止めるとメニューの操作が乱れる)
        guard allowsWindowDrag else {
            super.mouseDown(with: event)
            return
        }
        mouseDownEvent = event
        dragging = false
    }

    override func mouseDragged(with event: NSEvent) {
        guard allowsWindowDrag else {
            super.mouseDragged(with: event)
            return
        }
        guard !dragging, let down = mouseDownEvent, let window else { return }
        let dx = event.locationInWindow.x - down.locationInWindow.x
        let dy = event.locationInWindow.y - down.locationInWindow.y
        guard hypot(dx, dy) >= Self.dragThreshold else { return }
        dragging = true
        // 押した時点のイベントを渡すと、そこからウィンドウのドラッグが始まり、離すまでAppKitが追従させる
        window.performDrag(with: down)
    }

    override func mouseUp(with event: NSEvent) {
        mouseDownEvent = nil
        // ドラッグで動かしただけのときは、離した位置のボタンを押したことにしない
        if dragging {
            dragging = false
            return
        }
        // ビュー付きのメニュー項目はクリックしてもメニューが閉じず、イベントはビューに届く
        section?.onClick?(convert(event.locationInWindow, from: nil))
    }

    override func draw(_ dirtyRect: NSRect) {
        section?.draw(bounds.width, hoverPoint)
    }

    /// 欄の中身と高さを差し替えて再描画させる
    func setSection(_ section: Section) {
        self.section = section
        if frame.height != section.height {
            setFrameSize(NSSize(width: Layout.menuWidth, height: section.height))
            // メニューを開いている間に高さが変わっても、NSMenuはほかの項目を並べ直さない(itemChangedでも同じ)。
            // そのままだと広がった欄が上下の欄に重なって描かれるので、ビューを付け直して配置を計算し直させる
            if let item = enclosingMenuItem {
                item.view = nil
                item.view = self
            }
        }
        needsDisplay = true
    }
}
