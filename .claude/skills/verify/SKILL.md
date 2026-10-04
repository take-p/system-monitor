---
name: verify
description: メニューバーモニター(Swift版)の変更を、実際のアプリを起動・操作して確かめる手順
---

# Swift版メニューバーモニターの動作確認

ビルドと配置は AGENTS.md の手順(ビルド先は `MenubarMonitor.noindex`)に従う。

## 操作の道具

実際のマウス操作は小さな Swift のツールで行う(プロジェクトの Python 環境には Quartz が無い)。

- クリック: `CGEvent` で mouseMoved → leftMouseDown → leftMouseUp を `.cghidEventTap` に送る。座標はメイン画面の左上原点の全体座標(外部ディスプレイは負の値になりうる)
- ウィンドウの確認: `CGWindowListCopyWindowInfo` で所有者が `MenubarMonitor` のものを見る。メニューは layer=101、パネルは layer=3
- メニューバー項目の位置: `osascript -e 'tell application "System Events" to tell process "MenubarMonitor" to get {position, size} of menu bar item 1 of menu bar 1'`
- 撮影: `screencapture -x -R x,y,w,h`(全体座標)。全ディスプレイなら出力ファイルを画面の数だけ渡す

## 確かめる流れ

1. メニューバー項目の中央を実クリック → layer=101 のメニューが出る
2. メニューの「パネルで表示」を選ぶ → メニューが消え、layer=3 のパネルが出る。もう一度選ぶとパネルが閉じる(パネル表示中はチェックが付く)
   - アクセシビリティで操作できる: `osascript -e 'tell application "System Events" to tell process "MenubarMonitor" to click menu item "パネルで表示" of menu 1 of menu bar item 1 of menu bar 1'`(先にメニューバー項目をクリックしてメニューを開いておく)
3. パネル内の「さらに表示」を押す → パネルの高さが10行分(180pt)伸びる
4. パネルの外(ほかのアプリのタイトルバーなど)をクリック → パネルが残る
5. アプリを終了して起動し直す → パネルの表示状態と位置が戻る

## 注意

- ユーザーが操作していると、メニューが開かなかったり状態が変わったりする。始める前に触らないよう頼む
- ホバーの表示は、ビューに入った最初の1回の移動では出ない(mouseEntered だけになる)。数回動かしてから撮る
- 座標の見積もりは外れやすい。先にマウスを乗せて撮影し、位置を確かめてからクリックする
- 確認を始める前のパネルの表示状態(`defaults read local.system-monitor.MenubarMonitor panelVisible`)を控え、終わったら戻す
