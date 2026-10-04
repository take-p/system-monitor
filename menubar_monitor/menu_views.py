"""クリックで開くメニューに並べる、グラフ付きの各セクションの描画。

各セクションは1つのDrawingViewで、文字もグラフもdrawRect_内で自前で描く。
座標はflipped(左上原点、yは下向き)。
"""
import math

import objc

from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSCompositingOperationSourceOver,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageSymbolConfiguration,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSTrackingMouseMoved,
    NSView,
    NSZeroRect,
)

from network_info import format_bytes
from wifi_congestion import congestion_rating

MENU_WIDTH = 400
PAD_X = 14            # 左右の余白(pt)。通常のメニュー項目の文字位置に合わせる
PAD_Y = 6             # 上下の余白(pt)
TITLE_HEIGHT = 22     # 見出し行の高さ(pt)
ROW_HEIGHT = 18       # 本文1行の高さ(pt)
SMALL_ROW_HEIGHT = 15
CONGESTION_ROW_HEIGHT = 17
BAR_HEIGHT = 8
CHART_HEIGHT = 40

HISTORY = 60          # 推移グラフの点数(2秒間隔で2分)
HEAT_CELL_HEIGHT = 8
HEAT_ROW_GAP = 2
COLLAPSED_PROCESSES = 3   # CPU・メモリのランキングを折りたたんだときの件数
PROCESS_PAGE = 5          # 「さらに表示」1回で増やす件数
COLLAPSE_ZONE_WIDTH = 100 # ランキング末尾の行で「折りたたむ」として扱う右端の幅(pt)
TOP_GPU_PROCESSES = 1     # GPUのランキングを折りたたんだときの件数
TOP_DISK_PROCESSES = 1    # ディスク読み書きのランキングを折りたたんだときの件数
MIN_SCALE_MBPS = 1.0

GB = 1024 ** 3
STORAGE_GB = 1000 ** 3

# network_info.pyの評価レベル -> 表示色
# Wi-Fiアイコンは緑や黄だと灰色がかったメニュー背景で見えにくいので、見出しのアイコンと同じ標準色にする。
# 点灯が1本以下(弱い・悪い)のときだけ、注意を引くよう赤にする
ALERT_SIGNAL_LEVELS = {"weak", "poor"}

# wifi_congestion.pyの混雑度の評価レベル -> 表示色
CONGESTION_COLORS = {
    "free": NSColor.systemGreenColor,
    "moderate": NSColor.systemYellowColor,
    "busy": NSColor.systemOrangeColor,
    "heavy": NSColor.systemRedColor,
}

# 電波の評価 -> Wi-Fiアイコンの点灯段数(0〜1。アイコンは点+3本の扇形)
SIGNAL_LEVELS = {"excellent": 1.0, "good": 0.75, "fair": 0.5, "weak": 0.25, "poor": 0.0}

# メモリ内訳バーの区分。色はmac_memory_monitor_grouped.pyのCATEGORY_STYLESに合わせる
MEMORY_CATEGORIES = (
    ("app", "App", NSColor.systemBlueColor),
    ("wired", "Wired", NSColor.systemRedColor),
    ("compressed", "Compressed", NSColor.systemYellowColor),
    ("cached", "Cached Files", NSColor.systemTealColor),
    ("free", "Free/Other", NSColor.quaternaryLabelColor),
)

def value_color(percent):
    if percent >= 80:
        return NSColor.systemRedColor()
    if percent >= 50:
        return NSColor.systemOrangeColor()
    # 通常時は標準色(ライト/ダーク自動追従)のままにする
    return None

def nice_ceil(value):
    """グラフの上限を1/2/5×10^nの切りの良い値に切り上げる(network_monitor.pyと同じ)。"""
    value = max(value, MIN_SCALE_MBPS)
    exponent = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if value <= step * exponent:
            return step * exponent

# ---------------------------------------------------------------------------
# 描画の部品
# ---------------------------------------------------------------------------

def body_font():
    return NSFont.menuFontOfSize_(0)

def small_font():
    return NSFont.systemFontOfSize_weight_(11, NSFontWeightRegular)

def title_font():
    return NSFont.systemFontOfSize_weight_(body_font().pointSize(), NSFontWeightSemibold)

def mono_small_font(size=9):
    return NSFont.monospacedDigitSystemFontOfSize_weight_(size, NSFontWeightRegular)

def attributed(text, font, color=None):
    # labelColor等は描画時のアピアランスで解決される動的色なので、ライト/ダークに追従する
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color or NSColor.labelColor(),
    }
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)

def draw_parts(parts, x, y, font):
    """parts: [(文字列, 色 or None[, 太字にするか])] を左から順に描き、描き終えたx座標を返す"""
    bold = NSFont.systemFontOfSize_weight_(font.pointSize(), NSFontWeightSemibold)
    for text, color, *flags in parts:
        string = attributed(text, bold if flags and flags[0] else font, color)
        string.drawAtPoint_((x, y))
        x += string.size().width
    return x

def draw_text(text, x, y, font, color=None):
    return draw_parts([(text, color)], x, y, font)

def draw_text_right(text, right, y, font, color=None):
    string = attributed(text, font, color)
    string.drawAtPoint_((right - string.size().width, y))

def draw_text_fit(text, x, y, max_width, font, color=None):
    """max_widthに収まらない場合は末尾を「…」で省略して描く"""
    string = attributed(text, font, color)
    while string.size().width > max_width and len(text) > 1:
        text = text[:-1]
        string = attributed(text + "…", font, color)
    string.drawAtPoint_((x, y))

def draw_symbol(name, x, y, row_height, color, variable=None, align_right=False):
    """SF Symbolsのアイコンを行の上下中央に描き、右端のx座標を返す。
    variable(0〜1)を渡すと、Wi-Fiアイコンなどの点灯段数をその値に応じて変える。
    align_right=Trueならxを右端としてその左側に描く"""
    if variable is None:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    else:
        image = NSImage.imageWithSystemSymbolName_variableValue_accessibilityDescription_(
            name, variable, None
        )
    if image is None:
        return x
    # 階層カラーにすると、点灯していない段は同じ色の薄い色で描かれる
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        body_font().pointSize(), NSFontWeightRegular
    ).configurationByApplyingConfiguration_(
        NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color)
    )
    image = image.imageWithSymbolConfiguration_(config)
    w, h = image.size()
    left = x - w if align_right else x
    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        ((left, y + (row_height - h) / 2), (w, h)), NSZeroRect, NSCompositingOperationSourceOver,
        1.0, True, None,
    )
    return left + w

def fill_rect(x, y, w, h, color, radius=0):
    color.setFill()
    if radius:
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((x, y), (w, h)), radius, radius).fill()
    else:
        NSBezierPath.fillRect_(((x, y), (w, h)))

def draw_bar(x, y, w, ratio, color, h=BAR_HEIGHT):
    """背景トラック付きの横棒(ratioは0〜1)"""
    fill_rect(x, y, w, h, NSColor.quaternaryLabelColor(), radius=h / 2)
    ratio = min(max(ratio, 0), 1)
    if ratio > 0:
        fill_rect(x, y, max(w * ratio, h), h, color or NSColor.systemBlueColor(), radius=h / 2)

def draw_title(width, y, title, value_parts):
    """見出し行: 左にタイトル、右に値(部分ごとに色指定可)"""
    font = title_font()
    draw_text(title, PAD_X, y, font)
    value_font = NSFont.monospacedDigitSystemFontOfSize_weight_(font.pointSize(), NSFontWeightRegular)
    total = sum(attributed(text, value_font).size().width for text, _ in value_parts)
    draw_parts(value_parts, width - PAD_X - total, y, value_font)

def draw_history_chart(x, y, w, h, values, max_value, color, downward=False):
    """推移の面グラフ(アクティビティモニタ風)。背景の箱は付けず、淡い単色の塗りに
    くっきりした線を重ね、基準線を系列と同じ色で引く。valuesは古い順で、
    履歴が満杯になるまでは右詰めで描く。downward=Trueなら上端を基準線にして下向きに描く"""
    baseline = y if downward else y + h
    direction = 1 if downward else -1
    if values and max_value > 0:
        step = w / (HISTORY - 1)
        start = HISTORY - len(values)
        points = [
            (x + (start + i) * step, baseline + direction * min(value / max_value, 1) * h)
            for i, value in enumerate(values)
        ]

        area = NSBezierPath.bezierPath()
        area.moveToPoint_((points[0][0], baseline))
        for point in points:
            area.lineToPoint_(point)
        area.lineToPoint_((points[-1][0], baseline))
        area.closePath()
        color.colorWithAlphaComponent_(0.25).setFill()
        area.fill()

        line = NSBezierPath.bezierPath()
        line.moveToPoint_(points[0])
        for point in points[1:]:
            line.lineToPoint_(point)
        line.setLineWidth_(1.5)
        color.setStroke()
        line.stroke()

    color.setFill()
    # 基準線はグラフの内側に1pt分引く(上下対称のグラフで上下の基準線が重ならないように)
    NSBezierPath.fillRect_(((x, baseline if downward else baseline - 1), (w, 1)))

def heat_color(percent):
    """20%刻みの5段階で色分けする(青/緑/黄/橙/赤)"""
    if percent >= 80:
        return NSColor.systemRedColor()
    if percent >= 60:
        return NSColor.systemOrangeColor()
    if percent >= 40:
        return NSColor.systemYellowColor()
    if percent >= 20:
        return NSColor.systemGreenColor()
    return NSColor.systemBlueColor()

MIRROR_HALF_HEIGHT = 24  # 上下対称グラフの片側の高さ(pt)
# draw_mirror_chart 1つ分の高さ(上の見出し + グラフ上下 + 下の見出し + 余白)
MIRROR_CHART_HEIGHT = SMALL_ROW_HEIGHT * 2 + MIRROR_HALF_HEIGHT * 2 + 6

def draw_mirror_chart(x, y, width, unit, top, bottom):
    """上下対称の推移グラフ(Activity Monitorのネットワークのグラフと同じ形)。ネットワーク・ストレージで共用する。
    top/bottom: (ラベル, 現在値, ピーク, 合計の文字列, 履歴, 色, 補足)。topは中央から上向き・見出しは上、
    bottomは中央から下向き・見出しは下に描く。縦軸は上下で別々に伸縮し、小さい方が平らにならないようにする。
    補足はエラー数などで、目盛りの横に小さく添える。次の描画位置のyを返す"""
    chart_y = y + SMALL_ROW_HEIGHT
    middle = chart_y + MIRROR_HALF_HEIGHT
    for (label, value, peak, total_text, history, color, note), downward in ((top, False), (bottom, True)):
        values = list(history)
        # 表示中の最大値に合わせて縦軸を自動伸縮する
        scale = nice_ceil(max(values) if values else 0)
        header_y = middle + MIRROR_HALF_HEIGHT if downward else y
        # 系列の色は文字に付けると灰色がかった背景で読みにくいので、凡例の四角で示す
        fill_rect(x, header_y + 3, 8, 8, color, radius=2)
        draw_parts([(f"{label}  ", None), (f"{value:.2f} {unit}", None)], x + 12, header_y, small_font())
        # 起動からの合計は、どちらの向きの合計か分かるよう各系列の見出しに並べる
        draw_text_right(f"Peak {peak:.2f} {unit} · Total {total_text}", x + width, header_y,
                        small_font(), NSColor.secondaryLabelColor())
        # 外側の端(上半分は上端、下半分は下端)に、縦軸の上限の目盛り線を薄く引く(GPUのグラフと同じ)
        edge_y = middle + MIRROR_HALF_HEIGHT - 0.5 if downward else chart_y
        fill_rect(x, edge_y, width, 0.5, NSColor.tertiaryLabelColor())
        draw_history_chart(x, middle if downward else chart_y, width, MIRROR_HALF_HEIGHT, values, scale, color,
                           downward=downward)
        # 目盛り(縦軸の上限)は、グラフの外側の端(上半分は左上、下半分は左下)に小さく添える
        font = mono_small_font()
        label_y = (middle + MIRROR_HALF_HEIGHT - font.ascender() + font.descender() - 1) if downward else chart_y + 1
        draw_text(f"{scale:g} {unit}{note}", x + 3, label_y, font, NSColor.tertiaryLabelColor())
    return y + MIRROR_CHART_HEIGHT

def percent_parts(percent, fmt="{:.1f}%"):
    if percent is None:
        return [("--", NSColor.secondaryLabelColor())]
    # 灰色がかったメニュー背景では赤/オレンジの文字が読みにくいので、数値は標準色にする
    return [(fmt.format(percent), None)]

# ---------------------------------------------------------------------------
# DrawingView: drawerに渡した関数で中身を描くビュー
# ---------------------------------------------------------------------------

class DrawingView(NSView):
    """drawer(幅, ホバー中のマウス位置 or None)で中身を描くビュー。
    メニュー表示中もマウスの移動を受け取り、位置が変わるたびに描き直す"""

    def isFlipped(self):
        return True

    def updateTrackingAreas(self):
        for area in self.trackingAreas():
            self.removeTrackingArea_(area)
        # メニュー表示中はアプリがアクティブでないため、ActiveAlwaysでないとイベントが来ない
        options = (NSTrackingMouseMoved | NSTrackingMouseEnteredAndExited
                   | NSTrackingActiveAlways | NSTrackingInVisibleRect)
        self.addTrackingArea_(
            NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(self.bounds(), options, self, None)
        )
        objc.super(DrawingView, self).updateTrackingAreas()

    def mouseMoved_(self, event):
        self.hover_point = self.convertPoint_fromView_(event.locationInWindow(), None)
        self.setNeedsDisplay_(True)

    def mouseExited_(self, _event):
        self.hover_point = None
        self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        # ビュー付きのメニュー項目はクリックしてもメニューが閉じず、イベントはビューに届く
        on_click = getattr(self, "on_click", None)
        if on_click is not None:
            on_click(self.convertPoint_fromView_(event.locationInWindow(), None))

    def drawRect_(self, _rect):
        drawer = getattr(self, "drawer", None)
        if drawer is None:
            return
        try:
            drawer(self.bounds().size.width, getattr(self, "hover_point", None))
        except Exception as e:
            # 描画中の例外でアプリごと落とさず、エラー内容をその場に出す
            draw_text(f"描画エラー: {e}", PAD_X, PAD_Y, small_font(), NSColor.systemRedColor())

def make_section_view(height):
    view = DrawingView.alloc().initWithFrame_(((0, 0), (MENU_WIDTH, height)))
    view.drawer = None
    view.on_click = None
    return view

def set_section(view, height, drawer, on_click=None):
    """セクションの中身(描画関数・クリック時の処理)と高さを差し替えて再描画させる"""
    view.drawer = drawer
    view.on_click = on_click
    if view.frame().size.height != height:
        view.setFrameSize_((MENU_WIDTH, height))
    view.setNeedsDisplay_(True)

# ---------------------------------------------------------------------------
# 各セクション。いずれも(高さ, 描画関数)を返す
# ---------------------------------------------------------------------------

def cpu_section(percent, core_rows, processes=None, visible=COLLAPSED_PROCESSES, on_more=None, on_collapse=None):
    """core_rows: [(ラベル, 使用率の履歴)] を表示する順に並べたもの、
    processes: CpuProcessSampler.sample()の結果(None=集計中)。
    ランキングは上位visible件を出し、末尾の行で「さらに表示」「折りたたむ」を選べる(メモリ欄と同じ)"""
    rows = len(core_rows)
    heat_height = rows * (HEAT_CELL_HEIGHT + HEAT_ROW_GAP)
    # コアごとのヒートマップと見分けやすいよう、バーはメモリ欄と同じグレー1色にする
    table = UsageTable(PAD_Y + TITLE_HEIGHT + heat_height + SMALL_ROW_HEIGHT + 8, processes, visible,
                       COLLAPSED_PROCESSES, "CPU", NSColor.secondaryLabelColor(), "CPUを使っているアプリはありません")
    height = table.y + table.height + PAD_Y

    def on_click(point):
        table.click(point, on_more, on_collapse)

    def draw(width, hover=None):
        draw_title(width, PAD_Y, "CPU", percent_parts(percent))
        label_font = mono_small_font(8)
        label_offset = (HEAT_CELL_HEIGHT - label_font.ascender() + label_font.descender()) / 2
        # ラベル欄は一番長いラベルの幅に合わせる
        label_w = max((attributed(label, label_font).size().width for label, _ in core_rows), default=0)
        grid_x = PAD_X + label_w + 4
        grid_w = width - PAD_X - grid_x
        cell_w = grid_w / HISTORY
        y = PAD_Y + TITLE_HEIGHT
        for core, (label, history) in enumerate(core_rows):
            row_y = y + core * (HEAT_CELL_HEIGHT + HEAT_ROW_GAP)
            draw_text_right(label, grid_x - 4, row_y + label_offset, label_font,
                            NSColor.secondaryLabelColor())
            fill_rect(grid_x, row_y, grid_w, HEAT_CELL_HEIGHT,
                      NSColor.quaternaryLabelColor().colorWithAlphaComponent_(0.25))
            start = HISTORY - len(history)
            for i, value in enumerate(history):
                # セル間に0.5ptの隙間を空けて、時間の区切りが見えるようにする
                fill_rect(grid_x + (start + i) * cell_w, row_y, max(cell_w - 0.5, 0.5),
                          HEAT_CELL_HEIGHT, heat_color(value))
        footer_y = y + heat_height + 1
        draw_text("2分前", grid_x, footer_y, small_font(), NSColor.tertiaryLabelColor())
        draw_text_right("現在", width - PAD_X, footer_y, small_font(), NSColor.tertiaryLabelColor())

        # CPUを使っているアプリの上位(CPU全体=100%)
        table.draw(width, hover)

    return height, draw, on_click

class PagerRow:
    """一覧の末尾に置く「さらに表示」「折りたたむ」の行。
    shown件を表示中で、さらにremaining件あるとき、左側で次のpage件を、右端で初期件数(collapsed)に戻す"""

    def __init__(self, y, shown, remaining, collapsed, rest_note):
        self.y = y
        self.remaining = remaining
        self.can_collapse = shown > collapsed
        self.rest_note = rest_note  # 「残り 276グループ · 4.8 GB」のような補足
        self.visible = bool(remaining) or self.can_collapse
        self.collapse_x = MENU_WIDTH - PAD_X - COLLAPSE_ZONE_WIDTH

    def zone(self, point):
        """指している側("more" / "collapse" / None)"""
        if point is None or not self.visible or not self.y <= point.y < self.y + ROW_HEIGHT:
            return None
        if self.can_collapse and (point.x >= self.collapse_x or not self.remaining):
            return "collapse"
        return "more" if self.remaining else None

    def click(self, point, on_more, on_collapse):
        handler = {"more": on_more, "collapse": on_collapse}.get(self.zone(point))
        if handler is not None:
            handler()

    def draw(self, width, hover):
        if not self.visible:
            return
        font, secondary = body_font(), NSColor.secondaryLabelColor()
        # 押せることが分かるよう、ホバー中の側だけ背景を薄く敷く
        zone = self.zone(hover)
        if zone == "more":
            right = self.collapse_x if self.can_collapse else width - PAD_X + 6
            fill_rect(PAD_X - 6, self.y, right - (PAD_X - 6), ROW_HEIGHT, NSColor.quaternaryLabelColor(), radius=4)
        elif zone == "collapse":
            left = self.collapse_x if self.remaining else PAD_X - 6
            fill_rect(left, self.y, width - PAD_X + 6 - left, ROW_HEIGHT, NSColor.quaternaryLabelColor(), radius=4)
        if self.remaining:
            end = draw_symbol("chevron.down", PAD_X, self.y, ROW_HEIGHT, secondary)
            end = draw_text(f"さらに{min(self.remaining, PROCESS_PAGE)}件表示", end + 4, self.y, font, secondary)
            draw_text(f"  {self.rest_note}", end, self.y + 2, small_font(), NSColor.tertiaryLabelColor())
        if self.can_collapse:
            text = attributed("折りたたむ", font, secondary)
            text_x = width - PAD_X - text.size().width
            text.drawAtPoint_((text_x, self.y))
            draw_symbol("chevron.up", text_x - 4, self.y, ROW_HEIGHT, secondary, align_right=True)

class UsageTable:
    """「Process (grouped)」の見出し、アプリごとの使用率(名前・バー・%)、末尾の「さらに表示」の行からなる表。
    processes: [(アプリ名, 値)](None=集計中)。値は既定では使用率(全体=100%)で、
    上位同士の比較ではなく全体に対する割合が分かるようバーも100%基準で描く。
    行数は数秒ごとに増減するので、初期件数分の高さは常に確保してメニューの揺れを抑える"""

    def __init__(self, y, processes, visible, collapsed, value_header, color, empty_text,
                 format_value="{:.1f}%".format, bar_max=100):
        """format_value: 値を表示文字列にする関数、bar_max: バーが満杯になる値(Noneなら1位の値)"""
        self.y = y
        self.processes = processes
        self.shown = (processes or [])[:visible]
        remaining = (processes or [])[visible:]
        self.value_header, self.color, self.empty_text = value_header, color, empty_text
        self.format_value = format_value
        self.bar_max = bar_max if bar_max is not None else max((v for _, v in processes or []), default=0)
        self.pager = PagerRow(y + SMALL_ROW_HEIGHT + ROW_HEIGHT * len(self.shown), len(self.shown),
                              len(remaining), collapsed,
                              f"残り {len(remaining)}アプリ · {format_value(sum(v for _, v in remaining))}")
        self.height = SMALL_ROW_HEIGHT + ROW_HEIGHT * (max(len(self.shown), collapsed) + self.pager.visible)

    def click(self, point, on_more, on_collapse):
        self.pager.click(point, on_more, on_collapse)

    def draw(self, width, hover):
        name_w = 210
        bar_x = PAD_X + name_w + 10
        value_right = width - PAD_X
        font = body_font()
        mono = NSFont.monospacedDigitSystemFontOfSize_weight_(font.pointSize(), NSFontWeightRegular)
        # 値の欄は「959 KB/s」のような長い値でもバーに重ならないよう、表示中の値の幅に合わせる
        value_w = max([attributed(self.format_value(v), mono).size().width for _, v in self.shown] + [40])
        bar_len = value_right - value_w - 10 - bar_x
        secondary = NSColor.secondaryLabelColor()
        draw_text("Process (grouped)", PAD_X, self.y, small_font(), secondary)
        draw_text_right(self.value_header, value_right, self.y, small_font(), secondary)

        row_y = self.y + SMALL_ROW_HEIGHT
        if self.processes is None:
            draw_text("集計中…", PAD_X, row_y, font, secondary)
            return
        if not self.processes:
            draw_text(self.empty_text, PAD_X, row_y, font, secondary)
            return
        for name, value in self.shown:
            draw_text_fit(name, PAD_X, row_y, name_w, font)
            draw_bar(bar_x, row_y + 5, bar_len, min(value / self.bar_max, 1) if self.bar_max else 0, self.color, h=6)
            draw_text_right(self.format_value(value), value_right, row_y, mono)
            row_y += ROW_HEIGHT
        self.pager.draw(width, hover)

def gpu_section(gpu, history, processes=None, visible=TOP_GPU_PROCESSES, on_more=None, on_collapse=None):
    """gpu: menubar_monitor.get_gpu_usage()の戻り値(None=取得失敗)、
    processes: GpuProcessSampler.sample()の結果(None=集計中)。
    ランキングは上位visible件を出し、末尾の行で「さらに表示」「折りたたむ」を選べる(メモリ欄と同じ)"""
    table = UsageTable(PAD_Y + TITLE_HEIGHT + CHART_HEIGHT + SMALL_ROW_HEIGHT + 8, processes, visible,
                       TOP_GPU_PROCESSES, "GPU", NSColor.systemPurpleColor(), "GPUを使っているアプリはありません")
    height = table.y + table.height + PAD_Y

    def on_click(point):
        table.click(point, on_more, on_collapse)

    def draw(width, hover=None):
        if gpu is None:
            draw_title(width, PAD_Y, "GPU", percent_parts(None))
        else:
            # 見出しの右: GPUが使用中のメモリと使用率
            memory = [("Memory Usage ", NSColor.secondaryLabelColor()), (f"{gpu['memory'] / GB:.1f} GB  ", None)]
            draw_title(width, PAD_Y, "GPU", (memory if gpu["memory"] is not None else [])
                       + percent_parts(gpu["percent"], "{:.0f}%"))
        chart_y = PAD_Y + TITLE_HEIGHT
        # 縦軸は0〜100%固定。上端に100%の目盛り線を薄く引き、ほかのグラフと同じく左上に目盛りの値を添える
        fill_rect(PAD_X, chart_y, width - PAD_X * 2, 0.5, NSColor.tertiaryLabelColor())
        draw_history_chart(PAD_X, chart_y, width - PAD_X * 2, CHART_HEIGHT, list(history), 100,
                           NSColor.systemPurpleColor())
        draw_text("100%", PAD_X + 3, chart_y + 1, mono_small_font(), NSColor.tertiaryLabelColor())
        footer_y = chart_y + CHART_HEIGHT + 1
        draw_text("2分前", PAD_X, footer_y, small_font(), NSColor.tertiaryLabelColor())
        draw_text_right("現在", width - PAD_X, footer_y, small_font(), NSColor.tertiaryLabelColor())

        # GPUを使っているアプリの上位。メモリ欄のランキングと同じ並び(名前・バー・値)にする
        table.draw(width, hover)

    return height, draw, on_click

def format_footprint(mb):
    # 1GB未満は「0.0 GB」ばかりになるのでMBで出す
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"

def memory_section(mem, groups, visible=COLLAPSED_PROCESSES, on_more=None, on_collapse=None):
    """mem: menubar_monitor.get_memory_usage()の戻り値(None=取得失敗)、groups: collect_grouped()の結果。
    ランキングは上位visible件を出し、末尾の行で「さらに表示」(on_more)と「折りたたむ」(on_collapse)を選べる"""
    groups = groups or []
    # 他ユーザーのプロセス等はサイズを取得できず0になる。1件ずつ並べても意味がないので最後に件数だけ出す
    measured = [g for g in groups if g[3] > 0]
    unmeasured = len(groups) - len(measured)
    shown, remaining = measured[:visible], measured[visible:]
    show_unmeasured = not remaining and unmeasured > 0
    table_y = PAD_Y + TITLE_HEIGHT + BAR_HEIGHT + 6 + SMALL_ROW_HEIGHT * 2 + 8
    pager = PagerRow(table_y + SMALL_ROW_HEIGHT + ROW_HEIGHT * (len(shown) + show_unmeasured),
                     len(shown), len(remaining), COLLAPSED_PROCESSES,
                     f"残り {len(remaining)}グループ · {format_footprint(sum(g[3] for g in remaining))}")
    process_rows = (len(shown) + show_unmeasured + pager.visible) if groups else 1
    height = table_y + SMALL_ROW_HEIGHT + ROW_HEIGHT * process_rows + PAD_Y

    def on_click(point):
        pager.click(point, on_more, on_collapse)

    def draw(width, hover=None):
        if mem is None:
            draw_title(width, PAD_Y, "Memory", percent_parts(None))
            return
        draw_title(width, PAD_Y, "Memory", [
            (f"{mem['used'] / GB:.1f} / {mem['total'] / GB:.0f} GB  ", None),
            *percent_parts(mem["percent"]),
        ])

        # 内訳の積み上げバー
        bar_y = PAD_Y + TITLE_HEIGHT
        bar_w = width - PAD_X * 2
        fill_rect(PAD_X, bar_y, bar_w, BAR_HEIGHT, NSColor.quaternaryLabelColor(), radius=2)
        x = PAD_X
        for key, _label, color in MEMORY_CATEGORIES:
            w = bar_w * mem[key] / mem["total"]
            fill_rect(x, bar_y, w, BAR_HEIGHT, color())
            x += w

        # 凡例(3列×2行)
        legend_y = bar_y + BAR_HEIGHT + 6
        col_w = bar_w / 3
        for i, (key, label, color) in enumerate(MEMORY_CATEGORIES):
            lx = PAD_X + (i % 3) * col_w
            ly = legend_y + (i // 3) * SMALL_ROW_HEIGHT
            fill_rect(lx, ly + 3, 8, 8, color(), radius=2)
            draw_text(f"{label} {mem[key] / GB:.1f} GB", lx + 12, ly, small_font())

        # アプリ単位のメモリ使用量ランキング
        name_w = 170
        procs_right = PAD_X + name_w + 40
        bar_x = procs_right + 10
        size_right = width - PAD_X
        bar_len = size_right - 62 - bar_x
        secondary = NSColor.secondaryLabelColor()
        draw_text("Process (grouped)", PAD_X, table_y, small_font(), secondary)
        draw_text_right("Procs", procs_right, table_y, small_font(), secondary)
        draw_text_right("Footprint", size_right, table_y, small_font(), secondary)

        row_y = table_y + SMALL_ROW_HEIGHT
        font = body_font()
        if not groups:
            draw_text("集計中…", PAD_X, row_y, font, secondary)
            return
        max_mb = measured[0][3] if measured else 1
        mono = NSFont.monospacedDigitSystemFontOfSize_weight_(font.pointSize(), NSFontWeightRegular)
        for _root, name, count, footprint_mb, _percent in shown:
            draw_text_fit(name, PAD_X, row_y, name_w, font)
            draw_text_right(str(count), procs_right, row_y, mono, secondary)
            # 上の内訳バー(Wired=赤, Compressed=黄)と混同しないよう、バーはグレー1色にする
            draw_bar(bar_x, row_y + 5, bar_len, footprint_mb / max_mb, secondary, h=6)
            draw_text_right(format_footprint(footprint_mb), size_right, row_y, mono)
            row_y += ROW_HEIGHT
        if show_unmeasured:
            draw_text(f"サイズを取得できない {unmeasured}グループ", PAD_X, row_y, font, secondary)
        pager.draw(width, hover)

    return height, draw, on_click

def format_rate(bytes_per_sec):
    """ディスクの読み書き速度(バイト/秒)を桁に応じてKB/s・MB/sで表す"""
    if bytes_per_sec >= 1_000_000:
        return f"{bytes_per_sec / 1_000_000:.1f} MB/s"
    return f"{bytes_per_sec / 1_000:.0f} KB/s"

def storage_section(storage, disk_io=None, read_history=(), write_history=(),
                    processes=None, visible=TOP_DISK_PROCESSES, on_more=None, on_collapse=None):
    """storage: get_storage_usage()の戻り値、disk_io: DiskIOSampler.sample()の戻り値(None=取得失敗)、
    processes: ディスクのGroupRateSamplerの結果(None=集計中)。
    容量のバー・空きの内訳の下に、読み書き速度のグラフとアプリごとの読み書き速度の表を置く"""
    charts_y = PAD_Y + TITLE_HEIGHT + BAR_HEIGHT + 4 + SMALL_ROW_HEIGHT + 4
    charts_height = MIRROR_CHART_HEIGHT if disk_io else 0
    # バーはディスク全体の読み書き速度に占める割合にする(1位基準だと数KB/sでも満杯に見えてしまう)。
    # 計測のタイミングがずれてアプリの値が全体を上回ることがあるので、1位の値も下限にする
    disk_total = (disk_io["read"] + disk_io["write"]) * 1_000_000 if disk_io else 0
    top = processes[0][1] if processes else 0
    table = UsageTable(charts_y + charts_height + 4, processes, visible, TOP_DISK_PROCESSES, "Read+Write",
                       NSColor.secondaryLabelColor(), "ディスクを読み書きしているアプリはありません",
                       format_value=format_rate, bar_max=max(disk_total, top))
    height = table.y + table.height + PAD_Y

    def on_click(point):
        table.click(point, on_more, on_collapse)

    def draw(width, hover=None):
        if storage is None:
            draw_title(width, PAD_Y, "Storage", percent_parts(None))
        else:
            draw_title(width, PAD_Y, "Storage", [
                (f"{storage['used'] / STORAGE_GB:.0f} / {storage['total'] / STORAGE_GB:.0f} GB  ", None),
                *percent_parts(storage["percent"]),
            ])
            bar_y = PAD_Y + TITLE_HEIGHT
            draw_bar(PAD_X, bar_y, width - PAD_X * 2, storage["percent"] / 100, value_color(storage["percent"]))
            # 空きは「今すぐ使える空き」と「macOSが必要に応じて消すパージ可能領域」に分けて出す
            draw_text(f"空き {storage['free'] / STORAGE_GB:.1f} GB ＋ パージ可能 {storage['purgeable'] / STORAGE_GB:.1f} GB",
                      PAD_X, bar_y + BAR_HEIGHT + 4, small_font(), NSColor.secondaryLabelColor())

        # 読み書き速度。色はActivity Monitorのディスクのグラフと同じく読み込み=青、書き込み=赤。
        # ネットワーク欄(上り/下り)とそろえて、↑書き込みを上、↓読み込みを下に描く
        if disk_io:
            peaks, totals = disk_io["peaks"], disk_io["totals"]
            draw_mirror_chart(
                PAD_X, charts_y, width - PAD_X * 2, "MB/s",
                ("↑ Write", disk_io["write"], peaks["write"], format_bytes(totals["write"]), write_history,
                 NSColor.systemRedColor(), ""),
                ("↓ Read", disk_io["read"], peaks["read"], format_bytes(totals["read"]), read_history,
                 NSColor.systemBlueColor(), ""),
            )

        # ディスクを読み書きしているアプリの上位
        table.draw(width, hover)

    return height, draw, on_click

def network_section(net, dl_history, ul_history, congestion=None):
    """net: NetworkSampler.sample()の戻り値(None=取得失敗)、
    congestion: CongestionScanner.snapshot()の戻り値(Wi-Fi以外・未接続ならNone)"""
    if net:
        spec_row, quality_row = (net["details"] + [[], []])[:2]
        ratings = {label: rating for label, _, rating in quality_row if rating}
        # 通信品質の目安としては電波の強さ(RSSI)よりノイズとの差(SNR)が的確なので、
        # アイコンはSNRで決める。ノイズが取れずSNRが無い場合だけSignalで代用する
        signal_rating = ratings.get("SNR") or ratings.get("Signal")
    else:
        spec_row, quality_row, signal_rating = [], [], None
    # Link + 規格 + 電波
    text_rows = 1 + len(spec_row) + bool(signal_rating)
    congestion_block = congestion_height(congestion)
    height = (PAD_Y + TITLE_HEIGHT + ROW_HEIGHT * text_rows + congestion_block + 4
              + MIRROR_CHART_HEIGHT + PAD_Y)

    def draw(width, hover=None):
        if net is None:
            draw_title(width, PAD_Y, "Network", percent_parts(None))
            return
        font = body_font()
        secondary = NSColor.secondaryLabelColor()

        # 見出し: 右端に接続方式のアイコンとインターフェース名
        draw_text("Network", PAD_X, PAD_Y, title_font())
        iface_text = attributed(f"{net['iface'] or 'N/A'} · {net['kind']}", font)
        text_x = width - PAD_X - iface_text.size().width
        iface_text.drawAtPoint_((text_x, PAD_Y))
        symbol = "wifi" if net["kind"] == "Wi-Fi" else "cable.connector.horizontal"
        draw_symbol(symbol, text_x - 4, PAD_Y, ROW_HEIGHT, NSColor.labelColor(), align_right=True)
        y = PAD_Y + TITLE_HEIGHT

        # 速度のグラフは一番よく見る情報なので、CPU/GPUと同じく見出しのすぐ下に置く。
        # メニューバーのNET表示(上段↑/下段↓)とそろえて、上り→下りの順に並べる
        totals, peaks = net["totals"], net["peaks"]
        # エラー/ドロップ数(起動からの合計)は、目盛りの横の空いている所に小さく添える。
        # macOSでは送信側のドロップ数を取得できない(psutilが常に0を返す)ので、0ではなく「—」で示す
        y = draw_mirror_chart(
            PAD_X, y, width - PAD_X * 2, "Mbps",
            ("↑ Upload", net["ul"], peaks["ul"], format_bytes(totals["bytes_sent"]), ul_history,
             NSColor.systemRedColor(), f" · Err {totals['errout']} · Drop —"),
            ("↓ Download", net["dl"], peaks["dl"], format_bytes(totals["bytes_recv"]), dl_history,
             NSColor.systemBlueColor(), f" · Err {totals['errin']} · Drop {totals['dropin']}"),
        )

        # 接続の詳細(電波・リンク速度・規格・混雑度)は速度の原因を調べるための情報なのでグラフの下に置く
        y += 4

        label_w = 44  # 「電波」「Link」の見出し列の幅

        # 電波品質: 左にWi-Fiアイコン(点灯段数=SNRの評価)とSNR、右に電波の強さとノイズを添える
        if signal_rating:
            rating_label, level = signal_rating
            values = {label: (value, rating) for label, value, rating in quality_row}
            draw_text("電波", PAD_X, y, font, secondary)
            icon_color = NSColor.systemRedColor() if level in ALERT_SIGNAL_LEVELS else NSColor.labelColor()
            end = draw_symbol("wifi", PAD_X + label_w, y, ROW_HEIGHT, icon_color,
                              variable=SIGNAL_LEVELS[level])
            end = draw_text(f" {rating_label}", end + 2, y, NSFont.systemFontOfSize_weight_(
                font.pointSize(), NSFontWeightSemibold))
            if "SNR" in values:
                end = draw_text(f"  SNR {values['SNR'][0]}", end, y, font)

            # 右側は左側と重ならない幅に収める。入りきらなければ単位→評価の順に省き、それでも駄目なら末尾を省略する
            def details(unit, with_rating):
                parts = []
                for label in ("Signal", "Noise"):
                    if label in values:
                        value, rating = values[label]
                        value = value if unit else value.replace(" dBm", "")
                        parts.append(f"{label} {value}" + (f" ({rating[0]})" if rating and with_rating else ""))
                return " · ".join(parts)

            available = width - PAD_X - end - 8
            for unit, with_rating in ((True, True), (False, True), (False, False)):
                text = details(unit, with_rating)
                if attributed(text, small_font()).size().width <= available:
                    break
            text_w = min(attributed(text, small_font()).size().width, available)
            draw_text_fit(text, width - PAD_X - text_w, y + 2, available, small_font(), secondary)
            y += ROW_HEIGHT

        # リンク速度: 理論最大速度との差が分かるようゲージで見せる
        draw_text("Link", PAD_X, y, font, secondary)
        link, max_rate = net["link_speed"], net["max_rate"]
        if link and max_rate:
            value = f"{link:.0f} / {max_rate:.0f} Mbps"
            value_w = attributed(value, font).size().width
            gauge_w = width - PAD_X * 2 - label_w - value_w - 12
            draw_bar(PAD_X + label_w, y + 5, gauge_w, link / max_rate, NSColor.systemBlueColor())
            draw_text(value, width - PAD_X - value_w, y, font)
        else:
            # 理論最大速度が分からない(有線・推定中など)場合はリンク速度だけを出す
            draw_text(f"{link:.0f} Mbps" if link else "N/A", PAD_X + label_w, y, font)
        y += ROW_HEIGHT

        # 規格・帯域(1項目1行)
        for label, value, _rating in spec_row:
            draw_parts([(f"{label}: ", secondary), (value, None)], PAD_X, y, font)
            y += ROW_HEIGHT

        if congestion:
            draw_congestion(congestion, PAD_X, y, width, label_w, hover)
            y += congestion_height(congestion)

    return height, draw

def congestion_height(congestion):
    if not congestion:
        return 0
    rows = len(congestion["bands"]) if congestion["bands"] else 1
    return CONGESTION_ROW_HEIGHT * rows + SMALL_ROW_HEIGHT + 2

def draw_congestion(congestion, x, y, width, label_w, hover=None):
    """全帯域のチャネル別混雑度を、帯域ごとに色付きセルの1列(ヒートマップ風)で描く。
    セルの大きさは全帯域でそろえ、接続中のチャネル群は枠で囲む。
    右端は接続中の帯域なら接続中の混雑度、それ以外はその帯域で最も空いているチャネルを出す"""
    font = body_font()
    secondary = NSColor.secondaryLabelColor()
    draw_text("混雑", x, y, font, secondary)
    bands = congestion["bands"]
    if bands is None:
        draw_text("スキャン中…", x + label_w, y, font, secondary)
        return

    # 帯域名の欄は最も長い「2.4GHz」の幅に合わせる
    band_label_w = attributed("2.4GHz", small_font()).size().width + 6
    summary_w = 96
    strip_x = x + label_w + band_label_w
    strip_w = width - PAD_X - summary_w - 8 - strip_x
    cell_w = strip_w / max(len(b["channels"]) for b in bands)
    cell_h = 10
    bold = NSFont.systemFontOfSize_weight_(font.pointSize(), NSFontWeightSemibold)
    hovered = None
    for row, band in enumerate(bands):
        row_y = y + row * CONGESTION_ROW_HEIGHT
        cell_y = row_y + (ROW_HEIGHT - cell_h) / 2
        draw_text(band["band"], x + label_w, row_y + 2, small_font(), secondary)
        is_current = band["band"] == congestion["current_band"]
        current_index = []
        for i, (number, value) in enumerate(band["channels"]):
            _label, level = congestion_rating(value)
            fill_rect(strip_x + i * cell_w, cell_y, max(cell_w - 1.5, 1), cell_h,
                      CONGESTION_COLORS[level](), radius=2)
            if is_current and number in congestion["current"]:
                current_index.append(i)
            # マウスが乗っているセル(行の高さ全体を判定範囲にする)
            if hover is not None and (strip_x + i * cell_w <= hover.x < strip_x + (i + 1) * cell_w
                                      and row_y <= hover.y < row_y + CONGESTION_ROW_HEIGHT):
                hovered = (band["band"], number, value, band["ap_counts"].get(number, 0),
                           strip_x + i * cell_w, cell_y)
        if current_index:
            # 接続中のチャネル群(80MHzなら4ch分)を枠で囲む
            left = strip_x + min(current_index) * cell_w - 1.5
            right = strip_x + (max(current_index) + 1) * cell_w
            frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                ((left, cell_y - 2), (right - left, cell_h + 4)), 3, 3
            )
            frame.setLineWidth_(1.5)
            NSColor.labelColor().setStroke()
            frame.stroke()

        if is_current:
            percent = congestion["current_percent"]
            rating_label, _level = congestion_rating(percent)
            draw_parts_right([(f"{percent:.0f}% ", None, False), (rating_label, None, True)],
                             width - PAD_X, row_y, font, bold)
        else:
            number, percent = band["best"]
            draw_text_right(f"空き ch {number} ({percent:.0f}%)", width - PAD_X, row_y + 2,
                            small_font(), secondary)

    note_y = y + len(bands) * CONGESTION_ROW_HEIGHT
    if hovered:
        # ホバー中のセルを枠で強調し、補足の行をそのチャネルの情報に差し替える
        band_name, number, value, ap_count, cell_x, cell_top = hovered
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((cell_x - 1, cell_top - 1), (cell_w + 0.5, cell_h + 2)), 2.5, 2.5
        )
        frame.setLineWidth_(1.5)
        NSColor.labelColor().setStroke()
        frame.stroke()
        rating_label, _level = congestion_rating(value)
        draw_parts([(f"{band_name} ch {number}", None, True),
                    (f" · {value:.0f}% {rating_label} · AP {ap_count}台", None)],
                   x + label_w, note_y, small_font())
        return

    notes = []
    if congestion["primary"] is not None:
        # 束ねて使っているチャネルの範囲は「Band:」の行に出しているので、ここは代表番号だけにする
        current_aps = congestion.get("current_ap_count")
        notes.append(f"接続中 ch {congestion['primary']}"
                     + (f"（AP {current_aps}台）" if current_aps is not None else ""))
    age = congestion["age"]
    notes.append("スキャン中" if congestion["scanning"] else f"{age:.0f}秒前" if age is not None else "")
    notes.append("推定値")
    draw_text(" · ".join(n for n in notes if n), x + label_w, note_y, small_font(), secondary)

def draw_parts_right(parts, right, y, font, bold_font):
    """[(文字列, 色, 太字か)]を右端そろえで描く"""
    strings = [attributed(text, bold_font if is_bold else font, color) for text, color, is_bold in parts]
    x = right - sum(string.size().width for string in strings)
    for string in strings:
        string.drawAtPoint_((x, y))
        x += string.size().width
