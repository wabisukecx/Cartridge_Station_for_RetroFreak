# 配布設計

[English](../DISTRIBUTION.md)

## 配布物の種類

| ZIP | 対象 | 内容 |
|---|---|---|
| Cartridge_Station_for_RetroFreak_MiSTer.zip | MiSTerの通常利用 | アプリ、標準DB、設定の例、起動スクリプト、設計書、依存ライブラリーのライセンス、vendor（pyserial・PyUSBの純Pythonソース）。Windows GUIとPyQt6は含まない |
| Cartridge_Station_for_RetroFreak_Windows.zip | Windowsの通常利用 | MiSTer版の内容からvendorを除き、Windows GUI、requirements-windows.txt、Windows起動ファイルを加えたもの |
| Cartridge_Station_for_RetroFreak_MiSTer_Validation.zip | MiSTerでの全件試験・診断・開発 | 通常版と同じアプリ・DB・vendorに、Windows GUI・Windows起動ファイル・requirements-windows.txt、tools、tests、fixtures、windows_tests を加えた単独で使える一式（GUI試験にはWindows側の依存導入が必要） |
| Cartridge_Station_for_RetroFreak_Windows_Validation.zip | Windowsでの試験・診断・開発 | 同上のWindows版（vendorなし） |

- 通常ZIPが主たる成果物。検証用ZIPは任意で、通常の導入や取り込みの前提にしない。
- 検証用ZIPは差分ではなく一式。同じ機種の通常版と共通ファイルの内容が一致する。
- 各ZIPの隣に、LF改行の `.zip.sha256`（`<SHA-256>  <ファイル名>`）を置く。
- ZIP内のアプリフォルダーは Cartridge_Station_for_RetroFreak。
- exe化はしない。Pythonとライブラリーの実行環境は利用者側にある。

## 含めるもの・含めないもの

| 区分 | 通常 | 検証用 |
|---|---|---|
| src、標準DB（data/*.json）、設定の例（config/*.example.json） | 含む | 含む |
| 起動入口（cart_station.py、.sh）、診断スクリプト | 含む | 含む |
| Windows GUI、.cmd、requirements-windows.txt | Windows版のみ | 両Editionに含む |
| vendor（pyserial・PyUSB） | MiSTer版のみ | MiSTer版のみ |
| 設計書（英語と日本語）、Windows GUIの画面画像 | 含む | 含む |
| LICENSE、CHANGELOG.md、README.md・USER_GUIDE.md（英語）、README.ja.md（日本語）、requirements.txt | 含む | 含む |
| licenses（pyserial・PyUSBのライセンス全文） | 含む | 含む |
| tools、tests（fixturesを含む）、windows_tests | 含まない | 含む |
| ROM、セーブデータ | 含まない | 含まない |
| 個人設定（config/*.json で .example でないもの）、ログ、runtime | 含まない | 含まない |
| 仮想環境、キャッシュ、バックアップ、dist、調査用の未分類DB | 含まない | 含まない |
| ネイティブバイナリ（.dll、.pyd、.so、.exe） | 含まない（収録パターンで除外。MiSTer版は追加の拡張子検査あり） | 含まない |

- 収録ファイルは、明示したパターンで選ぶ。余分なローカルファイルがあってもパターンに合わなければ入らない。
- Windows GUI（cart_station_windows.py）は、Windows版と検証用だけに入る。
- MiSTerのランチャー（.sh）は実行権限つきで格納する。

## 同梱するライブラリー

- MiSTer版は、ビルド環境にpipで導入したpyserial 3.5とPyUSB 1.3.1の純Pythonソースとdist-infoを `vendor/` へ複製する。ビルド環境には requirements.txt の導入が必要。
- システムのPythonやlibusbは書き換えない。ネイティブライブラリーは同梱しない。
- Windows版は requirements-windows.txt（PyQt6、PyQt6-Qt6、PyQt6-sip、pyserial、pyusb）を利用者が導入する。Qt本体は同梱しない。

## manifest

- ZIPのアプリフォルダー直下に manifest（MiSTer：BUNDLE_MANIFEST.json、Windows：WINDOWS_MANIFEST.json）を入れる。
- 共通項目：product、edition、package_kind（runtime または validation）、files（相対パス→SHA-256）。
- MiSTer版はさらに python_syntax（3.9）、external_files（Scripts直下へ別格納したランチャーのSHA-256）を持つ。Windows版は dependencies（requirements-windows.txt）を持ち、python_syntax・external_files は持たない。
- ビルド時に、すべての .py を Python 3.9 の構文として解析し、通らなければ失敗させる。

## ビルド手順

開発ソースまたは検証用ZIPのルートで実行する。

```sh
python tools/build_mister_bundle.py
python tools/build_mister_bundle.py --validation
python tools/build_windows_bundle.py
python tools/build_windows_bundle.py --validation
```

- 出力先は dist/。ZIP、.zip.sha256 の順に生成する。
- git clone しただけのソースには dist/ がない。MiSTerへ配置するにはこの手順でZIPを作る。

## 配置と更新

- MiSTer：ZIPを `/media/fat/Scripts` へ転送し、`unzip -o` で展開する。配置先は `/media/fat/Scripts/Cartridge_Station_for_RetroFreak`。Scriptsメニューの Cartridge_Station_for_RetroFreak から起動する。
- Windows：任意のフォルダーへ展開し、そのフォルダーで仮想環境を作って requirements-windows.txt を導入し、起動ファイルまたは `cart_station.py gui` で起動する。古い仮想環境は新しいフォルダーへ移動・コピーしない。
- 上書き展開は、ZIPに含まれるファイルを更新するだけで、個人設定・ROM・ログは変更しない。ZIPが配る設定は例（*.example.json）だけ。
- 上書き展開では、ZIPに含まれなくなった tools・tests は削除されない。通常起動はそれらを使わない。
- 全件試験を行うときは検証用ZIPで更新する。ZIPに含まれなくなった tests が残ったまま実行しない（コードとテストの版が混在して失敗する）。

## 通常ZIPでの動作範囲

- 通常ZIPには tests がないため、展開先で `unittest discover` を実行しない。
- 取り込み、候補選択、DB検索、管理画面のプレビュー表示は、tools・tests なしで動作する。

## ソース公開リポジトリ

- 公開するソースには、開発ツールと試験を残す。
- .gitattributes：
  - 既定は `text=auto`。
  - `*.sh` は LF、`*.cmd` は CRLF。
  - `*.json` は `-text`。同梱DBとテスト用fixtureのバイト列を、改行変換で変えないため。
  - `*.png`、`*.zip` はバイナリ。
- 次はコミットしない：ROM・セーブ（拡張子で除外）、個人設定（config/*.json の例以外）、runtime、dist、仮想環境、キャッシュ、バックアップ、未分類の調査用カタログ。
- このソースにはCI設定を含めていない。推奨する検証環境と実行手順は [試験設計](TESTING.md) を参照する。
