# 設定・名称・配置の設計

[English](../CONFIGURATION.md)

## 名称

| 用途 | 名称 |
|---|---|
| 共通名 | Cartridge Station for RetroFreak |
| Windows画面 | Cartridge Station for RetroFreak — Windows Edition |
| MiSTer画面 | Cartridge Station for RetroFreak / MiSTer FPGA Edition |
| アプリフォルダー | Cartridge_Station_for_RetroFreak（Windows・MiSTer共通） |
| Windows起動 | Cartridge_Station_for_RetroFreak_Windows.cmd |
| MiSTer Scripts起動 | Cartridge_Station_for_RetroFreak.sh |
| 共通CLI | cart_station.py |
| 配布ZIP | Cartridge_Station_for_RetroFreak_Windows.zip、Cartridge_Station_for_RetroFreak_MiSTer.zip（検証用は末尾に _Validation） |

- Pythonモジュール名、CLIのコマンド名、DBのIDと内容、JSON設定名と形式、保存先の既定は固定する。ROMの移動・改名・削除は行わない。

## フォルダー構成

開発ソースと実行時生成物を含めた全体像。配布ZIPの収録範囲は下記の注記と [配布設計](DISTRIBUTION.md) を参照する。

```text
Cartridge_Station_for_RetroFreak/
  cart_station.py                 共通の起動入口（srcを検索パスへ加える）
  Cartridge_Station_for_RetroFreak.sh            MiSTer起動
  Cartridge_Station_for_RetroFreak_Windows.cmd   Windows起動
  src/                            読み出し・通信・検証・保存・画面
  data/                           標準DB（メタデータJSON）
  config/                         設定の例（*.example.json）と個人設定
  licenses/                       pyserial・PyUSBのライセンス全文
  docs/                           設計書（英語。日本語の原本は docs/ja/）
  vendor/                         MiSTer版の純Python依存（pyserial、PyUSB）
  runtime/                        実行時に生成するログ・MGL・計測（個人環境のみ）
  tools/ tests/ windows_tests/    開発・検証用（通常配布には含めない）
  requirements.txt                共通の依存
  requirements-windows.txt        Windowsの依存（PyQt6を含む）
  LICENSE、CHANGELOG.md、README.md、USER_GUIDE.md、README.ja.md
```

- 利用ガイドの USER_GUIDE.md（英語）と README.ja.md（日本語）を、README.md とともにZIPに含む。README.md は開発ソースの入口も兼ねるため、ビルドと試験の手順を含む。
- Windows起動ファイル・requirements-windows.txt・Windows GUIはWindows版と検証用ZIPに含める。vendor はMiSTer版だけに含める。
- 起動にtools・testsは不要。
- ROM、個人設定、ログ、仮想環境、キャッシュ、バックアップは配布物に含めない。

## 設定ファイル

設定は2種類。どちらも JSON、UTF-8（BOM付きも読める）、`version` は整数の1。

### 保存先（output_paths.json）

```json
{
  "version": 1,
  "pc":     { "NES": "~/RetroFreakDumps", "GB": "~/RetroFreakDumps", "...": "..." },
  "mister": { "NES": "/media/fat/games/NES", "GB": "/media/fat/games/Gameboy", "...": "..." }
}
```

- 機種キー：NES、GB、GBC、GBA、MD、PCE、SGX、SFC。環境キー：pc、mister。
- 既定値

| 機種 | PC | MiSTer |
|---|---|---|
| NES | ~/RetroFreakDumps | /media/fat/games/NES |
| GB | ~/RetroFreakDumps | /media/fat/games/Gameboy |
| GBC | ~/RetroFreakDumps | /media/fat/games/GBC |
| GBA | ~/RetroFreakDumps | /media/fat/games/GBA |
| MD | ~/RetroFreakDumps | /media/fat/games/MegaDrive |
| PCE | ~/RetroFreakDumps | /media/fat/games/TGFX16 |
| SGX | ~/RetroFreakDumps | /media/fat/games/TGFX16 |
| SFC | ~/RetroFreakDumps | /media/fat/games/SNES |

- GBとGBCを別の保存先にするのは、ConsoleModeがGBCのROMをGameboyフォルダーでは認識せず、GBCフォルダーで認識するため。
- 検証：値は空でない文字列。MiSTerは絶対Linuxパス（バックスラッシュ不可）。PCは絶対パスまたは `~/` で始まるパス。違反は補わずエラーにする。
- 設定ファイルにNES以外のキーがなくても、上の既定で補う（設定ファイル自体は書き換えない）。設定ファイルが存在しなければエラー。
- 優先順位：画面で選んだ保存先、CLIの `--output-dir` が最優先。次に `--paths-config` で明示したファイル。次に個人設定ファイル。最後に既定値。
- 機種の判定結果に従って保存先を切り替える（挿入された機種ごとに保存先が変わる）。保存先を画面やCLIで明示した場合は切り替えない。
- 環境（pc／mister）は自動判定する。判定はARM系Linuxで、かつSDカードのレイアウト（/media/fat/MiSTer ファイルと /media/fat/linux ディレクトリ）が存在するときだけMiSTerとする。`--environment` で上書きできる。

### MiSTerコア（mister_cores.json）

```json
{
  "version": 1,
  "NES": { "rbf": "_Console/NES", "delay": 2, "index": 1, "type": "f" }
}
```

| 機種 | rbf | index |
|---|---|---|
| NES | _Console/NES | 1 |
| GB、GBC | _Console/Gameboy | 1 |
| GBA | _Console/GBA | 1 |
| MD | _Console/MegaDrive | 1 |
| PCE | _Console/TurboGrafx16 | 0 |
| SGX | _Console/TurboGrafx16 | 1 |
| SFC | _Console/SNES | 1 |

- 共通：type は f、delay は2（1〜30秒）。
- index はPCEが0、その他が1に固定し、設定で異なる値を指定すると拒否する。変更できるのはrbfとdelay。
- rbf はSDルートからの相対パスで、`.rbf` と日付を付けない。実際のファイル（`<名前>.rbf` または `<名前>_日付.rbf`）が存在することを起動前に確認する。
- 機種のキーがなければ上の既定を使う。

### 個人DB（nes_profiles.json）

- FCの標準DBを置き換える個人DB。置かなければ標準DBを使う。形式はデータベース設計のFC用profiles形式。

## 設定ファイルの解決順

1. config/ の個人設定（例：config/output_paths.json）
2. config/ の例（config/output_paths.example.json）
- 上書き更新でも個人設定は書き換えない。ZIPが配るのは例（*.example.json）だけで、例が個人設定を上書きすることはない。
- 設定ファイルを置かなくても、例の値で動作する。変更したい場合に例を同じ場所へ `.example` なしの名前でコピーして編集する。
- 個人DBは config/nes_profiles.json。

## 旧名の設置先からの設定の引き継ぎ

- 旧名（MiSTer Cart Station）の設置先からの自動移行・旧フォルダーの自動探索は行わない。新しいアプリフォルダーへ展開し、設定は現在の `config/` から読み込む。
- 保存先・コアの設定を引き継ぐ場合は、新しい `config/*.example.json` を `.example` なしの名前へコピーし、旧設定から必要な値を転記する。形式とキーは本書の設定例に合わせる。
- 個人DBを引き継ぐ場合は、[データベース設計](DATABASE.md) のprofiles形式に合うものを `config/nes_profiles.json` に置く。
- 既存ROMを引き続き使う場合は、その保存先を設定する。アプリは移行のためのROMの移動・改名・削除を行わない。Windowsの仮想環境は新しい設置先で作り直す。

## 実行時に生成するもの（runtime/）

| ファイル | 内容 |
|---|---|
| manager.log | MiSTer管理画面の詳細ログ（通信エラーの詳細を含む） |
| last_capture_metrics.json | 直近1回の取り込みの計測 |
| <機種>_<raw SHA-1>_<ヘッダーハッシュ>.mgl | ゲーム起動用のMGL |
- MiSTerでは、起動しているアプリのフォルダーが Scripts 直下のアプリフォルダーならその runtime を使い、そうでなければ Scripts/Cartridge_Station_for_RetroFreak の runtime を使う。

## 起動スクリプト

- MiSTer（.sh）：Scripts直下のアプリのフォルダー、スクリプト自身のあるフォルダーの順にアプリを探し、`TERM=linux` を設定して `python3 cart_station.py manager` を実行する。見つからなければメッセージを出して終了する。
- Windows（.cmd）：スクリプトのあるフォルダーへ移動し、`.venv\Scripts\python.exe` がなければ環境作成を案内して止まる。あれば `cart_station.py gui` を実行する。
- 診断スクリプト（cart_station_check.sh）：システム情報、Python・pyserial・PyUSBの有無、USBディスクリプター、sysfs、シリアルノードを表示する。リーダーへコマンドを送らず、ドライバーも変更しない。

## 依存ライブラリー

| 名称 | 版 | 配置 |
|---|---|---|
| pyserial | 3.5 | MiSTerはvendorに同梱。Windowsはpip |
| PyUSB | 1.3.1 | MiSTerはvendorに同梱。Windowsはpip |
| PyQt6／PyQt6-Qt6／PyQt6-sip | 6.11.0／6.11.2／13.13.0 | Windowsのみ。pipで導入し、同梱しない |
| libusb | システム | 同梱しない。MiSTerの既存libusb-1.0.so.0を使う |
- vendor が存在すれば、それを検索パスの先頭に加える。
- MiSTer標準のPythonは日本語コーデックを持たない場合があるため、Shift-JISの変換は必須にしない。タイトルの変換に失敗したときはASCII代替を使い、判定用のバイト列やROMのハッシュは変えない。
