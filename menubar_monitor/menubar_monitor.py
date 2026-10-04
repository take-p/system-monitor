#!/usr/bin/env python3
import plistlib
import re
import subprocess
from collections import deque

import objc
import psutil
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import (
    NSMakeSize,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
    NSURL,
    NSUserDefaults,
)

import menu_views
import process_rates
from disk_info import DiskIOSampler
from gpu_info import GpuProcessSampler
from net_processes import NetProcessSampler
from memory_info import collect_grouped, compute_breakdown, get_vm_stat_counts, snapshot_grouped_processes
from menu_views import value_color
from network_info import NetworkSampler
from wifi_congestion import CongestionScanner

UPDATE_INTERVAL = menu_views.HISTORY_STEP  # 推移グラフの記録間隔と同じにする

LABEL_FONT_SIZE = 7
VALUE_FONT_SIZE = 12
LABEL_LETTER_GAP = 1.5  # 縦書きラベルの文字間(pt)
LABEL_VALUE_GAP = 2     # ラベル列と数値の間(pt)
SEGMENT_GAP = 7         # CPU/GPU/RAM/SSD/NETの各ブロック間(pt)
# 数値欄はこの文字列の幅で固定し右寄せする(値が変わっても表示幅が揺れないように)
VALUE_WIDTH_SAMPLE = "100%"
GRAPH_DIAMETER = 15     # 円グラフ/ドーナツグラフの直径(pt)
DONUT_LINE_WIDTH = 3    # ドーナツグラフのリングの太さ(pt)
BAR_WIDTH = 7           # 縦棒グラフの幅(pt)。高さは円グラフの直径と揃える
SUFFIX_GAP = 4          # 値と補足(SSDの残り容量)の間(pt)
NET_FONT_SIZE = 9       # ネットワークの上り/下り2段表示の文字サイズ(pt)
NET_LINE_GAP = 2.5      # 上り/下りの行間(pt)
# 上り/下りの欄はこの文字列の幅で固定し右寄せする(1Gbps超の回線でも幅が揺れないように)
NET_WIDTH_SAMPLE = "↓ 9999.9 Mbps"
# 数値は千の位まで0埋めし、先頭の埋め草の0だけ薄く表示して欄の隙間を埋める
NET_DIGITS = 4
PERCENT_DIGITS = 3      # 使用率も百の位まで同様に0埋めする

DISPLAY_MODES = {"number": "数値", "pie": "円グラフ", "donut": "ドーナツグラフ", "bar": "縦棒グラフ"}
# メニューの欄とメニューバーの項目の対応。並びはメニューバーと同じCPU/RAM/GPU/SSD/NETの順
SECTIONS = (("cpu", "CPU", "CPU"), ("memory", "Memory", "RAM"), ("gpu", "GPU", "GPU"),
            ("storage", "Storage", "SSD"), ("network", "Network", "NET"))  # (キー, メニューの名前, メニューバーのラベル)
HISTORY_MINUTES = (1, 2, 5, 10)  # メニューの「履歴の長さ」で選べる長さ(分)。最長はmenu_views.MAX_HISTORYに合わせる
DEFAULT_HISTORY_MINUTES = 2
DEFAULTS_SUITE = "local.system-monitor.menubar"

def get_cpu_percent():
    # interval=Noneは前回呼び出しからの差分で計算するため、タイマーをブロックしない
    return psutil.cpu_percent(interval=None)

def get_core_labels(count):
    """psutilのコア番号順に「P1」「E1」のようなラベルを返す。
    Apple SiliconはioregのCPUノードにcluster-type(P=高性能/E=高効率)があり、
    logical-cpu-idがpsutil(host_processor_info)の番号と一致する。取れない場合は「Core 1」形式にする"""
    try:
        output = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-a", "-c", "IOPlatformDevice", "-k", "cluster-type"],
            capture_output=True, check=True,
        ).stdout
        types = {
            node["logical-cpu-id"]: bytes(node["cluster-type"]).rstrip(b"\0").decode()
            for node in plistlib.loads(output)
        }
    except (OSError, subprocess.CalledProcessError, plistlib.InvalidFileException, KeyError, ValueError):
        types = {}
    if sorted(types) != list(range(count)):
        return [f"Core {i + 1}" for i in range(count)]
    labels, seen = [], {}
    for i in range(count):
        seen[types[i]] = seen.get(types[i], 0) + 1
        labels.append(f"{types[i]}{seen[types[i]]}")
    return labels

def get_gpu_usage():
    # ioregのIOAccelerator配下にあるPerformanceStatisticsはsudo不要で取得できる。
    # 使用率の取得方法はsimple_monitor/gpu_monitor.pyと同じ
    output = subprocess.run(
        ["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"],
        capture_output=True, text=True, check=True,
    ).stdout
    match = re.search(r'"Device Utilization %"\s*=\s*(\d+)', output)
    if match is None:
        # 0%と区別できるよう、キーが見つからない場合は「--」表示に回す
        raise ValueError("Device Utilization % not found")
    # GPUが今使っている統合メモリの量(バイト)。キーが無い機種では表示を省く
    memory = re.search(r'"In use system memory"\s*=\s*(\d+)', output)
    return {"percent": float(match.group(1)), "memory": int(memory.group(1)) if memory else None}

def get_storage_usage():
    # psutil.disk_usage("/")はAPFSの読み取り専用システムボリューム分しか数えず、実使用量と大きくずれる。
    # Finder/システム設定と同じく、パージ可能領域を空きに含めた「重要な用途に使える空き容量」で計算する。
    # NSURLはリソース値をキャッシュするため、毎回新しく作る
    url = NSURL.fileURLWithPath_("/")
    keys = ["NSURLVolumeTotalCapacityKey", "NSURLVolumeAvailableCapacityForImportantUsageKey"]
    values, error = url.resourceValuesForKeys_error_(keys, None)
    if values is None:
        raise OSError(str(error))
    return {
        "total": int(values["NSURLVolumeTotalCapacityKey"]),
        "available": int(values["NSURLVolumeAvailableCapacityForImportantUsageKey"]),
    }

def get_memory_usage():
    # Activity Monitorの「使用済みメモリ」(app + wired + compressed)に合わせる。
    # 内訳の計算はmemory_info.compute_breakdown(mac_memory_monitor_grouped.pyから移植)を使う
    mem = psutil.virtual_memory()
    breakdown = compute_breakdown(mem, get_vm_stat_counts())
    return {**breakdown, "total": mem.total, "percent": breakdown["used"] / mem.total * 100}

def _attributed(text, font, color=None):
    # labelColorは描画時のアピアランスで解決される動的色なので、
    # drawingHandler内で使えばライト/ダーク切り替えにも追従する
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color or NSColor.labelColor(),
    }
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)

def _draw_y_for_cap_top(font, cap_top):
    # flipped座標でdrawAtPoint_に渡すyは行の上端。
    # 大文字(数字)の上端をcap_topに揃えるため、ascenderとcapHeightの差だけ上にずらす
    return cap_top - (font.ascender() - font.capHeight())

def _draw_pie(cx, cy, radius, percent, color):
    NSColor.tertiaryLabelColor().setFill()
    NSBezierPath.bezierPathWithOvalInRect_(
        ((cx - radius, cy - radius), (radius * 2, radius * 2))
    ).fill()
    if percent <= 0:
        return
    (color or NSColor.labelColor()).setFill()
    if percent >= 100:
        NSBezierPath.bezierPathWithOvalInRect_(
            ((cx - radius, cy - radius), (radius * 2, radius * 2))
        ).fill()
        return
    # flipped座標では角度が増える向きが見た目の時計回りになる。12時(-90°)から時計回りに塗る
    wedge = NSBezierPath.bezierPath()
    wedge.moveToPoint_((cx, cy))
    wedge.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        (cx, cy), radius, -90, -90 + 360 * percent / 100, False
    )
    wedge.closePath()
    wedge.fill()

def _draw_donut(cx, cy, radius, percent, color):
    # 線幅の半分だけ内側を通る円にして、外径を円グラフと揃える
    ring_radius = radius - DONUT_LINE_WIDTH / 2
    track = NSBezierPath.bezierPath()
    track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        (cx, cy), ring_radius, 0, 360, False
    )
    track.setLineWidth_(DONUT_LINE_WIDTH)
    NSColor.tertiaryLabelColor().setStroke()
    track.stroke()
    if percent <= 0:
        return
    arc = NSBezierPath.bezierPath()
    arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        (cx, cy), ring_radius, -90, -90 + 360 * min(percent, 100) / 100, False
    )
    arc.setLineWidth_(DONUT_LINE_WIDTH)
    (color or NSColor.labelColor()).setStroke()
    arc.stroke()

def _draw_bar(cx, cy, radius, percent, color):
    # 角丸の溝を描き、その内側を下から使用率の高さまで塗る(レベルメーター風)
    rect = ((cx - BAR_WIDTH / 2, cy - radius), (BAR_WIDTH, radius * 2))
    track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 2, 2)
    NSColor.tertiaryLabelColor().setFill()
    track.fill()
    if percent <= 0:
        return
    # 塗りを溝の角丸で切り抜いて、満杯に近いときも角がはみ出さないようにする
    NSGraphicsContext.saveGraphicsState()
    track.addClip()
    fill_height = radius * 2 * min(percent, 100) / 100
    (color or NSColor.labelColor()).setFill()
    NSBezierPath.fillRect_(((cx - BAR_WIDTH / 2, cy + radius - fill_height), (BAR_WIDTH, fill_height)))
    NSGraphicsContext.restoreGraphicsState()

GRAPH_DRAWERS = {"pie": _draw_pie, "donut": _draw_donut, "bar": _draw_bar}

def format_rate_line(arrow, mbps):
    """「↓ 0012.4 Mbps」形式の[(文字列, 色)]を返す。先頭の埋め草の0だけ薄い色にする"""
    number = f"{mbps:.1f}"
    integer_digits = len(number.split(".")[0])
    padding = "0" * max(NET_DIGITS - integer_digits, 0)
    return [
        (f"{arrow} ", None),
        (padding, NSColor.tertiaryLabelColor()),
        (f"{number} Mbps", None),
    ]

def format_percent_parts(percent, color):
    """「004%」形式の[(文字列, 色)]を返す。先頭の埋め草の0だけ薄い色にする"""
    number = f"{percent:.0f}"
    padding = "0" * max(PERCENT_DIGITS - len(number), 0)
    return [(padding, NSColor.tertiaryLabelColor()), (f"{number}%", color)]

def format_free_lines(available_bytes):
    """SSDの残り容量を「Free / 142GB」の2段にする。
    残り容量はほとんど変わらず表示幅も揺れにくいので、使用率やネットワークと違って0埋めはしない"""
    if available_bytes is None:
        return [[("Free", None)], [("--GB", NSColor.secondaryLabelColor())]]
    # 残りが少ないときは、メニュー内のStorage欄と同じしきい値で色を付けて警告する
    return [[("Free", None)], [(f"{available_bytes / 1000 ** 3:.0f}GB", menu_views.free_color(available_bytes))]]

def build_status_image(segments, height, mode):
    """segments: [(label, value, color[, suffix])] から縦書きラベル付きの画像を作る。
    valueは使用率(float)、取得失敗のNone、または複数行表示する行のリスト(各行は[(文字列, 色)])。
    suffix(行のリスト)を渡すと、値の右に2段などで左寄せに添える(SSDの残り容量など)。
    mode: "number" / "pie" / "donut" / "bar"。使用率の項目だけがmodeに従い、
    Noneはどのモードでも「--」、行のリストはmodeに関係なくそのまま表示する
    """
    label_font = NSFont.systemFontOfSize_weight_(LABEL_FONT_SIZE, NSFontWeightSemibold)
    value_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
        VALUE_FONT_SIZE, NSFontWeightRegular
    )
    lines_font = NSFont.monospacedDigitSystemFontOfSize_weight_(NET_FONT_SIZE, NSFontWeightRegular)
    graph_diameter = min(GRAPH_DIAMETER, height - 4)
    if mode in GRAPH_DRAWERS:
        graph_width = BAR_WIDTH if mode == "bar" else graph_diameter
        percent_width = max(graph_width, _attributed("--", value_font).size().width)
    else:
        percent_width = _attributed(VALUE_WIDTH_SAMPLE, value_font).size().width
    lines_width = _attributed(NET_WIDTH_SAMPLE, lines_font).size().width

    layout = []
    x = 0.0
    for i, (label, value, color, *rest) in enumerate(segments):
        if i > 0:
            x += SEGMENT_GAP
        letters = [_attributed(ch, label_font) for ch in label]
        label_width = max(letter.size().width for letter in letters)
        value_width = lines_width if isinstance(value, list) else percent_width
        suffix = rest[0] if rest else None
        # 補足の幅は一番長い行に合わせる(残り容量は桁がめったに変わらないので固定幅にしない)
        suffix_width = max(
            (sum(_attributed(text, lines_font).size().width for text, _ in parts) for parts in suffix),
            default=0,
        ) if suffix else 0
        layout.append((x, label_width, letters, value, color, value_width, suffix, suffix_width))
        x += label_width + LABEL_VALUE_GAP + value_width
        if suffix is not None:
            x += SUFFIX_GAP + suffix_width
    total_width = x

    label_cap = label_font.capHeight()
    value_cap = value_font.capHeight()
    lines_cap = lines_font.capHeight()

    def draw_lines(lines, left, width, color, align_right=True):
        """複数行のテキストを上下中央にまとめて置く(ネットワークの上り/下り、SSDの残り容量)"""
        block_height = len(lines) * lines_cap + (len(lines) - 1) * NET_LINE_GAP
        line_cap_top = (height - block_height) / 2
        for parts in lines:
            line = NSMutableAttributedString.alloc().init()
            for text, part_color in parts:
                line.appendAttributedString_(_attributed(text, lines_font, part_color or color))
            line_x = left + width - line.size().width if align_right else left
            line.drawAtPoint_((line_x, _draw_y_for_cap_top(lines_font, line_cap_top)))
            line_cap_top += lines_cap + NET_LINE_GAP

    def draw(_rect):
        for seg_x, label_width, letters, percent, color, value_width, suffix, suffix_width in layout:
            # ラベルの文字を縦に積み、全体を上下中央に置く
            stack_height = len(letters) * label_cap + (len(letters) - 1) * LABEL_LETTER_GAP
            cap_top = (height - stack_height) / 2
            for letter in letters:
                letter_x = seg_x + (label_width - letter.size().width) / 2
                letter.drawAtPoint_((letter_x, _draw_y_for_cap_top(label_font, cap_top)))
                cap_top += label_cap + LABEL_LETTER_GAP

            value_left = seg_x + label_width + LABEL_VALUE_GAP
            if suffix is not None:
                # 「Free」の見出しと値の頭がそろうよう左寄せにする
                draw_lines(suffix, value_left + value_width + SUFFIX_GAP, suffix_width, None, align_right=False)
            if isinstance(percent, list):
                draw_lines(percent, value_left, value_width, color)
                continue

            if percent is not None and mode in GRAPH_DRAWERS:
                GRAPH_DRAWERS[mode](
                    value_left + value_width / 2, height / 2, graph_diameter / 2, percent, color
                )
                continue

            if percent is None:
                value = _attributed("--", value_font, color)
            else:
                value = NSMutableAttributedString.alloc().init()
                for text, part_color in format_percent_parts(percent, color):
                    value.appendAttributedString_(_attributed(text, value_font, part_color))
            value_x = value_left + value_width - value.size().width
            value_cap_top = (height - value_cap) / 2
            value.drawAtPoint_((value_x, _draw_y_for_cap_top(value_font, value_cap_top)))
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(
        NSMakeSize(total_width, height), True, draw
    )

class MonitorController(NSObject, protocols=[objc.protocolNamed("NSMenuDelegate")]):
    def init(self):
        self = objc.super(MonitorController, self).init()
        if self is None:
            return None

        status_bar = NSStatusBar.systemStatusBar()
        self.status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.bar_height = status_bar.thickness()
        self.segments = []
        self.network = NetworkSampler()
        self.disk_io = DiskIOSampler()
        self.congestion = CongestionScanner()
        self.menu_open = False
        self.process_groups = None
        self.cpu_processes = None
        self.cpu_visible = menu_views.COLLAPSED_PROCESSES
        self.cpu_sampler = process_rates.cpu_sampler()
        self.disk_processes = None
        self.disk_visible = menu_views.TOP_DISK_PROCESSES
        self.disk_sampler = process_rates.disk_sampler()
        self.net_processes = None
        self.net_visible = menu_views.TOP_NET_PROCESSES
        self.net_sampler = NetProcessSampler()
        # メモリのランキングに出している件数(メニューを閉じると初期件数に戻す)
        self.memory_visible = menu_views.COLLAPSED_PROCESSES
        self.gpu_processes = None
        self.gpu_visible = menu_views.TOP_GPU_PROCESSES
        self.gpu_sampler = GpuProcessSampler()

        # 推移グラフ用の履歴。メニューを閉じている間も記録し続ける
        core_count = psutil.cpu_count(logical=True)
        self.core_histories = [deque(maxlen=menu_views.MAX_HISTORY) for _ in range(core_count)]
        # メニューでは高性能コア(P)を上、高効率コア(E)を下に並べる(sortedは安定なので各種類内は番号順)
        self.core_rows = sorted(zip(get_core_labels(core_count), self.core_histories),
                                key=lambda row: not row[0].startswith("P"))
        self.gpu_history = deque(maxlen=menu_views.MAX_HISTORY)
        self.dl_history = deque(maxlen=menu_views.MAX_HISTORY)
        self.ul_history = deque(maxlen=menu_views.MAX_HISTORY)
        self.disk_busy_history = deque(maxlen=menu_views.MAX_HISTORY)

        # python本体(org.python.python)のドメインを汚さないよう、専用のsuiteに保存する
        self.defaults = NSUserDefaults.alloc().initWithSuiteName_(DEFAULTS_SUITE)
        self.display_mode = self.defaults.stringForKey_("displayMode")
        if self.display_mode not in DISPLAY_MODES:
            self.display_mode = "number"
        self.history_minutes = self.defaults.integerForKey_("historyMinutes")
        if self.history_minutes not in HISTORY_MINUTES:
            self.history_minutes = DEFAULT_HISTORY_MINUTES
        menu_views.set_history_minutes(self.history_minutes)
        # 非表示にした項目(メニューバーの項目とメニュー内の欄の両方を隠す)
        self.hidden_sections = {str(key) for key in (self.defaults.arrayForKey_("hiddenSections") or [])}
        if len(self.hidden_sections & {key for key, _, _ in SECTIONS}) >= len(SECTIONS):
            self.hidden_sections = set()  # 全部隠れているとメニューを開けなくなるので戻す

        menu = NSMenu.alloc().init()
        menu.setDelegate_(self)
        self.sections = {}
        self.section_items = {}
        self.section_separators = {}  # 各欄の上に置く区切り線(一番上に見えている欄では隠す)
        for key, _, _ in SECTIONS:
            separator = NSMenuItem.separatorItem()
            menu.addItem_(separator)
            self.section_separators[key] = separator
            view = menu_views.make_section_view(1)
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            menu.addItem_(item)
            self.sections[key] = view
            self.section_items[key] = item
        menu.addItem_(NSMenuItem.separatorItem())
        # 表示する項目の切り替えは、メニューが長くならないようサブメニューにまとめる
        sections_menu = NSMenu.alloc().init()
        # 最後の1つを外せなくするのにsetEnabled_を使うので、自動での有効化を切る
        sections_menu.setAutoenablesItems_(False)
        self.visibility_items = {}
        for key, title, _ in SECTIONS:
            item = sections_menu.addItemWithTitle_action_keyEquivalent_(title, "toggleSection:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(key)
            self.visibility_items[key] = item
        sections_item = menu.addItemWithTitle_action_keyEquivalent_("表示項目", None, "")
        menu.setSubmenu_forItem_(sections_menu, sections_item)
        self._apply_section_visibility()
        # 表示形式と履歴の長さも、メニューが長くならないようサブメニューにする
        self.mode_items = self._add_choice_submenu(
            menu, "表示形式", DISPLAY_MODES.items(), "changeDisplayMode:")
        self._sync_mode_checkmarks()
        self.history_items = self._add_choice_submenu(
            menu, "履歴の長さ", ((minutes, f"{minutes}分") for minutes in HISTORY_MINUTES), "changeHistoryLength:")
        self._sync_history_checkmarks()
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Quit", "terminate:", "q")
        self.status_item.setMenu_(menu)

        # 初回は基準値の取得のみ(常に0.0が返る)
        get_cpu_percent()
        psutil.cpu_percent(interval=None, percpu=True)
        self.update_(None)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            UPDATE_INTERVAL, self, "update:", None, True
        )
        # CommonModesに登録しないと、メニューを開いている間は更新が止まる
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        return self

    @objc.python_method
    def _sync_mode_checkmarks(self):
        for mode, item in self.mode_items.items():
            item.setState_(NSControlStateValueOn if mode == self.display_mode else NSControlStateValueOff)

    @objc.python_method
    def _add_choice_submenu(self, menu, title, choices, action):
        """choices: [(値, 項目名)] から選択肢のサブメニューを作ってmenuに足し、{値: 項目} を返す"""
        submenu = NSMenu.alloc().init()
        items = {}
        for value, label in choices:
            item = submenu.addItemWithTitle_action_keyEquivalent_(label, action, "")
            item.setTarget_(self)
            item.setRepresentedObject_(value)
            items[value] = item
        parent = menu.addItemWithTitle_action_keyEquivalent_(title, None, "")
        menu.setSubmenu_forItem_(submenu, parent)
        return items

    @objc.python_method
    def _apply_section_visibility(self):
        """非表示の設定を、メニュー内の欄・区切り線・サブメニューのチェックに反映する"""
        first = True
        for key, _, _ in SECTIONS:
            hidden = key in self.hidden_sections
            self.section_items[key].setHidden_(hidden)
            self.section_separators[key].setHidden_(hidden or first)
            first = first and hidden
            item = self.visibility_items[key]
            item.setState_(NSControlStateValueOff if hidden else NSControlStateValueOn)
        # 最後の1つまで隠すとメニューバーのアイコンが消えてメニューを開けなくなるので、外せないようにする
        visible = [key for key, _, _ in SECTIONS if key not in self.hidden_sections]
        for key, item in self.visibility_items.items():
            item.setEnabled_(not (len(visible) == 1 and key in visible))

    @objc.python_method
    def _sync_history_checkmarks(self):
        for minutes, item in self.history_items.items():
            item.setState_(NSControlStateValueOn if minutes == self.history_minutes else NSControlStateValueOff)

    @objc.python_method
    def _render(self):
        # 非表示にした項目はメニューバーからも外す
        hidden_labels = {label for key, _, label in SECTIONS if key in self.hidden_sections}
        segments = [segment for segment in self.segments if segment[0] not in hidden_labels]
        self.status_item.button().setImage_(build_status_image(segments, self.bar_height, self.display_mode))

    @objc.python_method
    def _refresh_processes(self):
        # 全プロセスの走査はメニューを開いている間だけ行う(1回あたり数十ms)
        # メモリとCPUのランキングは同じスナップショットから作る
        try:
            snapshot = snapshot_grouped_processes()
        except Exception:
            snapshot = None
        try:
            self.process_groups = collect_grouped(psutil.virtual_memory().total, snapshot)
        except Exception:
            self.process_groups = None
        try:
            self.cpu_processes = self.cpu_sampler.sample(snapshot) if snapshot else None
        except Exception:
            self.cpu_processes = None
        try:
            self.disk_processes = self.disk_sampler.sample(snapshot) if snapshot else None
        except Exception:
            self.disk_processes = None
        try:
            self.net_processes = self.net_sampler.sample(snapshot) if snapshot else None
        except Exception:
            self.net_processes = None
        try:
            self.gpu_processes = self.gpu_sampler.sample()
        except Exception:
            self.gpu_processes = None

    @objc.python_method
    def _refresh_sections(self):
        sections = {
            "cpu": menu_views.cpu_section(self.latest["cpu"], self.core_rows, self.cpu_processes,
                                          self.cpu_visible, self._show_more_cpu, self._collapse_cpu),
            "memory": menu_views.memory_section(self.latest["memory"], self.process_groups,
                                                self.memory_visible, self._show_more_memory,
                                                self._collapse_memory),
            "gpu": menu_views.gpu_section(self.latest["gpu"], self.gpu_history, self.gpu_processes,
                                          self.gpu_visible, self._show_more_gpu, self._collapse_gpu),
            "storage": menu_views.storage_section(
                self.latest["storage"], self.latest["disk_io"], self.disk_busy_history,
                self.disk_processes, self.disk_visible, self._show_more_disk, self._collapse_disk,
            ),
            "network": menu_views.network_section(
                self.latest["network"], self.dl_history, self.ul_history, self._congestion_snapshot(),
                self.net_processes, self.net_visible, self._show_more_net, self._collapse_net,
            ),
        }
        for key, (height, drawer, *on_click) in sections.items():
            menu_views.set_section(self.sections[key], height, drawer, *on_click)

    @objc.python_method
    def _show_more_memory(self, visible):
        self.memory_visible = visible
        # 次のタイマーを待たず、その場で高さを変えて描き直す
        self._refresh_sections()

    @objc.python_method
    def _collapse_memory(self):
        self.memory_visible = menu_views.COLLAPSED_PROCESSES
        self._refresh_sections()

    @objc.python_method
    def _show_more_cpu(self, visible):
        self.cpu_visible = visible
        self._refresh_sections()

    @objc.python_method
    def _collapse_cpu(self):
        self.cpu_visible = menu_views.COLLAPSED_PROCESSES
        self._refresh_sections()

    @objc.python_method
    def _show_more_disk(self, visible):
        self.disk_visible = visible
        self._refresh_sections()

    @objc.python_method
    def _collapse_disk(self):
        self.disk_visible = menu_views.TOP_DISK_PROCESSES
        self._refresh_sections()

    @objc.python_method
    def _show_more_net(self, visible):
        self.net_visible = visible
        self._refresh_sections()

    @objc.python_method
    def _collapse_net(self):
        self.net_visible = menu_views.TOP_NET_PROCESSES
        self._refresh_sections()

    @objc.python_method
    def _show_more_gpu(self, visible):
        self.gpu_visible = visible
        self._refresh_sections()

    @objc.python_method
    def _collapse_gpu(self):
        self.gpu_visible = menu_views.TOP_GPU_PROCESSES
        self._refresh_sections()

    @objc.python_method
    def _congestion_snapshot(self):
        try:
            return self.congestion.snapshot()
        except Exception:
            return None

    def menuWillOpen_(self, _menu):
        self.menu_open = True
        # Wi-Fiスキャンは通信を一瞬遅らせるので、メニューを開いている間だけ行う
        self.congestion.activate()
        self._refresh_processes()
        self._refresh_sections()

    def menuDidClose_(self, _menu):
        self.menu_open = False
        self.congestion.deactivate()
        # 次に開いたとき、閉じていた間の平均ではなく直近の使用率を出すため基準を捨てる
        self.gpu_sampler.reset()
        self.gpu_processes = None
        self.cpu_sampler.reset()
        self.cpu_processes = None
        self.disk_sampler.reset()
        self.disk_processes = None
        self.net_sampler.reset()
        self.net_processes = None
        # 「さらに表示」で広げた一覧は、次に開いたときは初期件数に戻しておく
        self.cpu_visible = menu_views.COLLAPSED_PROCESSES
        self.memory_visible = menu_views.COLLAPSED_PROCESSES
        self.gpu_visible = menu_views.TOP_GPU_PROCESSES
        self.disk_visible = menu_views.TOP_DISK_PROCESSES
        self.net_visible = menu_views.TOP_NET_PROCESSES

    def changeDisplayMode_(self, sender):
        self.display_mode = str(sender.representedObject())
        self.defaults.setObject_forKey_(self.display_mode, "displayMode")
        self._sync_mode_checkmarks()
        # 次のタイマーを待たず、直近の値のまま描き直す
        self._render()

    def toggleSection_(self, sender):
        key = str(sender.representedObject())
        self.hidden_sections ^= {key}
        self.defaults.setObject_forKey_(sorted(self.hidden_sections), "hiddenSections")
        self._apply_section_visibility()
        self._render()

    def changeHistoryLength_(self, sender):
        self.history_minutes = int(sender.representedObject())
        self.defaults.setInteger_forKey_(self.history_minutes, "historyMinutes")
        self._sync_history_checkmarks()
        # 記録は最長分を取ってあるので、表示する長さを変えるだけで過去の分もすぐ出る
        menu_views.set_history_minutes(self.history_minutes)
        if self.menu_open:
            self._refresh_sections()

    def update_(self, _timer):
        # 取得に失敗しても例外でアプリごと落とさず、その項目だけ「--」表示にする
        latest = {}

        def collect(key, getter):
            try:
                latest[key] = getter()
            except Exception:
                latest[key] = None
            return latest[key]

        cpu = collect("cpu", get_cpu_percent)
        try:
            for history, value in zip(self.core_histories, psutil.cpu_percent(interval=None, percpu=True)):
                history.append(value)
        except Exception:
            pass
        mem = collect("memory", get_memory_usage)
        gpu = collect("gpu", get_gpu_usage)
        gpu = gpu and gpu["percent"]
        if gpu is not None:
            self.gpu_history.append(gpu)
        storage = collect("storage", get_storage_usage)
        disk_io = collect("disk_io", self.disk_io.sample)
        if disk_io is not None:
            self.disk_busy_history.append(disk_io["busy"])
        net = collect("network", self.network.sample)
        if net is not None:
            self.dl_history.append(net["dl"])
            self.ul_history.append(net["ul"])
        self.latest = latest

        self.segments = [
            ("CPU", cpu, value_color(cpu) if cpu is not None else None),
            ("RAM", mem and mem["percent"], value_color(mem["percent"]) if mem else None),
            ("GPU", gpu, value_color(gpu) if gpu is not None else None),
            # SSDは容量(ほとんど変わらない)ではなく、今の混み具合が分かるビジー率を出す。容量はメニュー内に出す
            # 右に残り容量(パージ可能領域を含む。Finderと同じ)を「Free / 142GB」の2段で添える
            ("SSD", disk_io and disk_io["busy"], value_color(disk_io["busy"]) if disk_io else None,
             format_free_lines(storage and storage["available"])),
            ("NET", [format_rate_line("↑", net["ul"]), format_rate_line("↓", net["dl"])] if net else None, None),
        ]
        self._render()

        if self.menu_open:
            self._refresh_processes()
            self._refresh_sections()

def main():
    app = NSApplication.sharedApplication()
    # Dockにアイコンを出さないメニューバー常駐アプリとして動かす
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    controller = MonitorController.alloc().init()  # noqa: F841 (参照を保持してGCを防ぐ)
    app.run()

if __name__ == "__main__":
    main()
