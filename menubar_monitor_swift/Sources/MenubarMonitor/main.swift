import AppKit

if CommandLine.arguments.contains("--dump") {
    DebugDump.run()
    exit(0)
}
if let index = CommandLine.arguments.firstIndex(of: "--render"), index + 1 < CommandLine.arguments.count {
    MainActor.assumeIsolated { DebugDump.renderStatusImages(to: CommandLine.arguments[index + 1]) }
    exit(0)
}

// メニューバー常駐アプリとして起動する(Dockにアイコンは出さない。Info.plistのLSUIElementでも指定)
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
