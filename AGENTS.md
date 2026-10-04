# AGENTS.md

## メニューバーモニター(Swift版)

普段使いしているのは Swift 版(`menubar_monitor_swift/`)を `/Applications/MenubarMonitor.app` に置いたもの。
Python 版(`menubar_monitor/`)は移植元として残しているだけで、常駐させない。

### アプリを更新したら /Applications の版を置き換える

`menubar_monitor_swift/` を変更してコミットしたら、Release ビルドして `/Applications` の版を置き換える。

```sh
cd menubar_monitor_swift
xcodegen generate
xcodebuild -project MenubarMonitor.xcodeproj -scheme MenubarMonitor -configuration Release \
  -destination 'platform=macOS,arch=arm64' build

# 動いている版を終了してから置き換え、起動し直す。
# dittoは既存のバンドルに上書きで混ぜるので、古い版のファイルが残らないよう先に消す
pkill -x MenubarMonitor
/bin/rm -rf /Applications/MenubarMonitor.app
ditto ~/Library/Developer/Xcode/DerivedData/MenubarMonitor-*/Build/Products/Release/MenubarMonitor.app \
  /Applications/MenubarMonitor.app
codesign --verify --deep --strict /Applications/MenubarMonitor.app
open /Applications/MenubarMonitor.app
```

- ビルド先はデフォルトの DerivedData のままにする。Desktop 配下にビルドすると、Finder の拡張属性が付いて署名に失敗する
- 署名はアドホック(`CODE_SIGN_IDENTITY: "-"`)。App Store での配信は保留中
- `.xcodeproj` は生成物なのでコミットしない。ファイルを追加・削除したら `xcodegen generate` で作り直す

### アプリアイコン

アイコンは `menubar_monitor_swift/Tools/draw_icon.swift` でコードから描いている。デザインを変えたら描き直して各サイズを置き換える。

```sh
cd menubar_monitor_swift
swift Tools/draw_icon.swift /tmp/icon_1024.png
D=Sources/MenubarMonitor/Assets.xcassets/AppIcon.appiconset
for s in 16 32 128 256 512; do
  sips -z $s $s /tmp/icon_1024.png --out $D/icon_${s}x${s}.png
  sips -z $((s*2)) $((s*2)) /tmp/icon_1024.png --out $D/icon_${s}x${s}@2x.png
done
```

### 確認用のオプション

- `MenubarMonitor --dump`: 取得した値を一度出力して終了する(Python 版との突き合わせ用)
- `MenubarMonitor --render <ディレクトリ>`: メニューバーの画像を表示形式・外観ごとに PNG で書き出す
