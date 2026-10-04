import AppKit

/// `MenubarMonitor --dump` で各値を一度だけ出力して終了する。Python版と値を突き合わせるための確認用
enum DebugDump {
    /// `MenubarMonitor --render <ディレクトリ>` でメニューバーの画像を表示形式ごとにPNGで書き出す(見た目の確認用)
    @MainActor
    static func renderStatusImages(to directory: String) {
        let segments = [
            StatusSegment(label: "CPU", value: .percent(7), color: nil),
            StatusSegment(label: "RAM", value: .percent(83), color: valueColor(83)),
            StatusSegment(label: "GPU", value: .percent(55), color: valueColor(55)),
            StatusSegment(label: "SSD", value: .percent(4), color: nil, suffix: StatusImage.freeLines(141_000_000_000)),
            StatusSegment(label: "NET", value: .lines([StatusImage.rateLine("↑", 0.09), StatusImage.rateLine("↓", 123.4)])),
        ]
        for appearance in [NSAppearance.Name.darkAqua, .aqua] {
            NSAppearance(named: appearance)?.performAsCurrentDrawingAppearance {
                for mode in DisplayMode.allCases {
                    let image = StatusImage.build(segments, height: 24, mode: mode)
                    let scale: CGFloat = 4
                    let size = NSSize(width: image.size.width * scale, height: image.size.height * scale)
                    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
                    NSGraphicsContext.saveGraphicsState()
                    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
                    (appearance == .darkAqua ? NSColor.black : NSColor.white).setFill()
                    NSRect(origin: .zero, size: size).fill()
                    image.draw(in: NSRect(origin: .zero, size: size))
                    NSGraphicsContext.restoreGraphicsState()
                    let name = "\(directory)/status_\(mode.rawValue)_\(appearance == .darkAqua ? "dark" : "light").png"
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: name))
                }
            }
        }
    }

    static func run() {
        let cpu = CPUSampler(), disk = DiskIOSampler(), gpuProcs = GPUProcessSampler()
        let cpuProcs = GroupRateSampler.cpu(), diskProcs = GroupRateSampler.disk(), netProcs = NetProcessSampler()
        // 速度系は2回の差分なので、基準を取ってから更新間隔だけ待つ
        var snapshot = ProcessSnapshot.take()
        _ = cpu.sample(); _ = disk.sample(); _ = gpuProcs.sample(snapshot)
        _ = cpuProcs.sample(snapshot); _ = diskProcs.sample(snapshot); _ = netProcs.sample(snapshot)
        Thread.sleep(forTimeInterval: 2)
        let start = monotonicNow()
        snapshot = ProcessSnapshot.take()
        let snapshotTime = monotonicNow() - start

        func gb(_ bytes: UInt64) -> String { String(format: "%.2fGB", Double(bytes) / 1024 / 1024 / 1024) }
        if let (total, cores) = cpu.sample() {
            let labels = CPUSampler.coreLabels(count: cores.count)
            print(String(format: "CPU %.1f%%  ", total) + zip(labels, cores).map { String(format: "%@ %.0f", $0, $1) }.joined(separator: " "))
        }
        if let m = MemoryInfo.usage() {
            print(String(format: "Memory %.1f%%", m.percent), "used", gb(m.used), "app", gb(m.app), "wired", gb(m.wired),
                  "compressed", gb(m.compressed), "cached", gb(m.cached), "free", gb(m.free), "total", gb(m.total))
        }
        if let g = GPUInfo.usage() {
            print(String(format: "GPU %.0f%%", g.percent), "memory", g.memory.map(gb) ?? "-")
        }
        if let s = StorageInfo.usage() {
            print(String(format: "Storage total %.0fGB available %.0fGB", Double(s.total) / 1e9, Double(s.available) / 1e9))
        }
        let io = disk.sample()
        print(String(format: "Disk read %.2fMB/s write %.2fMB/s busy %.1f%%", io.read, io.write, io.busy))
        print(String(format: "Processes %d (snapshot %.0fms)", snapshot.procs.count, snapshotTime * 1000))
        print("Memory top:", snapshot.groupedMemory(total: MemoryInfo.total).prefix(5).map { String(format: "%@ %.0fMB(%d)", $0.name, $0.megabytes, $0.count) })
        print("CPU top:", (cpuProcs.sample(snapshot) ?? []).prefix(5).map { String(format: "%@ %.1f%%", $0.name, $0.rate) })
        print("GPU top:", (gpuProcs.sample(snapshot) ?? []).prefix(5).map { String(format: "%@ %.1f%%", $0.name, $0.percent) })
        print("Disk top:", (diskProcs.sample(snapshot) ?? []).prefix(5).map { String(format: "%@ %.2fMB/s", $0.name, $0.rate / 1e6) })
        print("Net top:", (netProcs.sample(snapshot) ?? []).prefix(5).map { String(format: "%@ %.3fMbps", $0.name, $0.total) })
        dumpNetwork()
    }

    private static func dumpNetwork() {
        let network = NetworkSampler(), scanner = CongestionScanner()
        scanner.activate()
        _ = network.sample()
        Thread.sleep(forTimeInterval: 2)
        let net = network.sample()
        print(String(format: "Network %@ %@ ↑%.3f ↓%.3f Mbps", net.iface ?? "-", net.kind, net.upload, net.download),
              "link", net.linkSpeed.map { String(format: "%.0f", $0) } ?? "-", "max", net.maxRate.map { String(format: "%.0f", $0) } ?? "-")
        print("  totals", net.totals)
        print("  connection", net.connection ?? "-")
        if let q = net.quality {
            print("  signal", q.signal, q.signalRating.label, "noise", q.noise ?? 0, "snr", q.snr ?? 0, q.snrRating?.label ?? "-")
        }
        // スキャンは数秒かかる。ストリーム数はsystem_profilerの完了後に分かる
        Thread.sleep(forTimeInterval: 8)
        print("  max (after system_profiler)", network.sample().maxRate.map { String(format: "%.0f", $0) } ?? "-")
        if let c = scanner.snapshot() {
            print("Congestion", c.currentBand ?? "-", c.primary ?? 0, c.widthMHz ?? 0, c.current.sorted(),
                  "current", c.currentPercent.map { String(format: "%.0f%%", $0) } ?? "-", "APs", c.apCount ?? 0,
                  "age", c.age.map { String(format: "%.0fs", $0) } ?? "-")
            for band in c.bands ?? [] {
                print("  \(band.band) best ch\(band.best.number) \(Int(band.best.percent))%",
                      band.channels.filter { $0.percent > 0 }.map { "\($0.number):\(Int($0.percent))" }.joined(separator: " "))
            }
        }
    }
}
