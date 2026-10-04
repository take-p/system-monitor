import IOKit

/// IOKitレジストリの読み取り。ioregコマンドを起動せずに同じ情報を取る
enum IORegistry {
    /// クラス名に一致するサービスごとにbodyを呼ぶ
    static func forEachService(matching className: String, _ body: (io_registry_entry_t) -> Void) {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching(className), &iterator) == KERN_SUCCESS else {
            return
        }
        defer { IOObjectRelease(iterator) }
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            body(entry)
            IOObjectRelease(entry)
        }
    }

    /// 直下の子エントリごとにbodyを呼ぶ。GPUのユーザークライアントのような未登録(!registered)の
    /// エントリはIOServiceGetMatchingServicesでは見つからないので、親からたどる
    static func forEachChild(of parent: io_registry_entry_t, _ body: (io_registry_entry_t) -> Void) {
        var iterator: io_iterator_t = 0
        guard IORegistryEntryGetChildIterator(parent, kIOServicePlane, &iterator) == KERN_SUCCESS else {
            return
        }
        defer { IOObjectRelease(iterator) }
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            body(entry)
            IOObjectRelease(entry)
        }
    }

    static func property(_ entry: io_registry_entry_t, _ key: String) -> Any? {
        IORegistryEntryCreateCFProperty(entry, key as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue()
    }

    static func properties(_ entry: io_registry_entry_t) -> [String: Any]? {
        var dict: Unmanaged<CFMutableDictionary>?
        guard IORegistryEntryCreateCFProperties(entry, &dict, kCFAllocatorDefault, 0) == KERN_SUCCESS else {
            return nil
        }
        return dict?.takeRetainedValue() as? [String: Any]
    }
}
