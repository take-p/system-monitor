import AppKit

/// メニューの1つの欄。高さ・描画・クリック時の処理をまとめて差し替える
struct Section {
    /// 見出しの名前(ピンのボタンを見出しの右に置くのに使う)
    var title: String
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
    /// 設定すると、見出しの右にピンのボタンを出し、押されたら呼ぶ(メニューの一番上の欄で、パネルに固定表示する)
    var onPin: (() -> Void)? {
        didSet { needsDisplay = true }
    }

    /// ピンのボタンの範囲
    private var pinRect: NSRect? {
        guard onPin != nil, let title = section?.title else { return nil }
        let x = Layout.padX + textWidth(title, Fonts.title()) + 4
        return NSRect(x: x, y: Layout.padY, width: 24, height: 18)
    }

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

    override func mouseUp(with event: NSEvent) {
        // ビュー付きのメニュー項目はクリックしてもメニューが閉じず、イベントはビューに届く
        let point = convert(event.locationInWindow, from: nil)
        if let pinRect, pinRect.contains(point) {
            onPin?()
            return
        }
        section?.onClick?(point)
    }

    override func draw(_ dirtyRect: NSRect) {
        section?.draw(bounds.width, hoverPoint)
        if let pinRect {
            // 押せることが分かるよう、ホバー中だけ背景を薄く敷く
            if let hoverPoint, pinRect.contains(hoverPoint) {
                fillRect(pinRect.minX, pinRect.minY, pinRect.width, pinRect.height, .quaternaryLabelColor, radius: 4)
            }
            let iconWidth = symbolImage("pin", secondaryText())?.size.width ?? 0
            drawSymbol("pin", pinRect.midX - iconWidth / 2, pinRect.minY, pinRect.height, secondaryText())
        }
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
