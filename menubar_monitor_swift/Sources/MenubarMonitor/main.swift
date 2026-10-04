import AppKit

// メニューバー常駐アプリとして起動する(Dockにアイコンは出さない。Info.plistのLSUIElementでも指定)
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
