# Cartridge Station for RetroFreak 利用ガイド

[English user guide](USER_GUIDE.md) · [English README](README.md) · [English documentation](docs/README.md)

RetroFreak（レトロフリーク）のカートリッジアダプターをUSBで接続し、手持ちのカートリッジからROMを取り込むツールです。同じ読み出し処理を2つのEditionで使います。

| Edition | 用途 | 起動 |
|---|---|---|
| **Windows Edition** | PCやスマホのエミュレーターで使うROMを取り込む | `Cartridge_Station_for_RetroFreak_Windows.cmd` |
| **MiSTer FPGA Edition** | テレビとゲームパッドだけで取り込み、既存コアで遊ぶ | Scripts → `Cartridge_Station_for_RetroFreak` |

このツールはご自身が所有するカートリッジのバックアップ用です。ROMデータは含みません。セーブデータの読み書きはしません。RetroFreak、CYBER Gadget、MiSTerプロジェクトとは無関係の非公式ツールです。

- 変更の経緯：[CHANGELOG.md](CHANGELOG.md)
- 設計書の一覧：[docs/ja/README.md](docs/ja/README.md)

## 対応機種

| 機種 | 判別の方法 | 詳細 |
|---|---|---|
| FC（ファミコン） | 部分ハッシュでDB候補を検索 | [FC/NES](docs/ja/SYSTEM_FC_NES.md) |
| GB／GBC | ヘッダー（MBC・容量） | [GB／GBC](docs/ja/SYSTEM_GB_GBC.md) |
| GBA | ヘッダー＋全体ハッシュ | [GBA](docs/ja/SYSTEM_GBA.md) |
| メガドライブ | ヘッダー | [MD](docs/ja/SYSTEM_MD.md) |
| PCエンジン（HuCard／SuperGrafx） | 読み出し内容とDB | [PCE](docs/ja/SYSTEM_PCE.md) |
| SFC（スーパーファミコン） | ヘッダー（LoROM／HiROM・特殊チップ） | [SFC](docs/ja/SYSTEM_SFC.md) |

DBに収録されていることと、そのカートリッジで読み出しを確認したことは別です。NESのカートリッジはNESカートリッジコンバーターが必要で、NES実物は未検証です。

## 必要なもの

- レトロフリーク本体から外したカートリッジアダプター（USB給電で動作。USB ID `F00D:1337`）
- **Windows**：Python 3.9以上
- **MiSTer**：MiSTer標準のPython 3.9とlibusb。pyserial／PyUSBはMiSTer用ZIPの `vendor/` に同梱しているので、pipでの導入は不要です

## 導入

### Windows

ZIPを展開したフォルダー（またはリポジトリ）でPowerShellを開きます。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

以後は `Cartridge_Station_for_RetroFreak_Windows.cmd` をダブルクリックして起動します。設計は [Windows GUI](docs/ja/FRONTEND_WINDOWS_GUI.md)。

### MiSTer

`Cartridge_Station_for_RetroFreak_MiSTer.zip` を `/media/fat/Scripts` に転送し、SSHで展開します。

```sh
cd /media/fat/Scripts
unzip -o Cartridge_Station_for_RetroFreak_MiSTer.zip
```

上書き展開しても、個人設定（`config/*.json`）と保存済みROMは変わりません。配置と設定の設計は [配布設計](docs/ja/DISTRIBUTION.md) と [設定の設計](docs/ja/CONFIGURATION.md)。旧名（MiSTer Cart Station）から移る場合は、後者の「旧名の設置先からの設定の引き継ぎ」を参照してください。

## 使い方

### Windows

1. 接続方法（Auto-detect か COMポート）と保存先を選び、**Connect** を押す。起動しただけではリーダーに接続しません
2. カートリッジを挿すと自動で確認し、候補を表示します。候補が複数あって自動で決められない場合は、挿したタイトルを一覧から選びます
3. **Read cartridge** を押し、タイトルと保存先を確認して **Yes** を選ぶ
4. 2回の読み出しが一致し、DBの全体ハッシュとも一致したら保存します
5. **Open save folder** で保存したROMを開けます

PCEは、接続後の自動認識で一度ROM全体を読みます。この段階では保存せず、**Read cartridge** の確認後に独立した2回の読み出しを行って保存します。MiSTerでも同じ手順です。

読み出し中・識別中は **Cancel reading** で安全な区切りで止められます。

### MiSTer（テレビとゲームパッド）

メニューは上から運用順に並んでいます。

| メニュー | 内容 |
|---|---|
| CHECK / RETRY | 接続とカートリッジを確認し直す。エラーの後もここから再開 |
| SELECT CARTRIDGE | 候補が複数あるときに、挿したタイトルを選ぶ |
| DUMP CARTRIDGE | 確認画面（標準はNO）でYESを選ぶと取り込み開始 |
| GAME START | 保存したROMを再照合し、対応コアで起動 |
| EXIT TO MiSTer | MiSTerのメニューへ戻る |

十字キーで選び、決定ボタンで実行、戻るボタンで取り消します。ゲームからはRebootでConsoleModeへ戻ります。新しく取り込んだゲームをConsoleModeの一覧に出すには、ConsoleMode側でDBを再構築してください。設計は [MiSTer管理画面](docs/ja/FRONTEND_MISTER_MANAGER.md)。

## 保存のしくみ

- **保存の条件**：独立した2回の読み出しが一致し、同梱DBの全体ハッシュ（SHA-1、収録があればSHA-256も）と一致した場合だけ保存します
- **ファイル名**：DBのタイトル名を使います
- **同じ名前のファイルがあるとき**：内容が同じなら再利用、違うなら元のファイルを残してハッシュ付きの別名で保存します。設計は [取り込み・検証・保存](docs/ja/CAPTURE_FLOW.md)
- **書き込み方**：一時ファイルに書いてから、既存ファイルを置換しない方法で公開します。同名・別内容の既存ROMは残します

## 設定

保存先とコアの設定は `config/` にあります。標準のままで使う場合、設定は不要です。

| ファイル | 内容 | 既定値 |
|---|---|---|
| `output_paths.json` | 機種ごとの保存先 | PC：`~/RetroFreakDumps`、MiSTer：`/media/fat/games/<コアのフォルダー>` |
| `mister_cores.json` | GAME STARTで開くコア | `_Console/NES` などMiSTer標準のコア |

ZIPやリポジトリに入っているのは、既定値を書いた `output_paths.example.json` と `mister_cores.example.json` だけです。**設定を変えたいときは、`.example.json` をコピーして `.example` を外した名前にし、そのコピーを編集します。**

```sh
cd config
cp output_paths.example.json output_paths.json    # Windowsは copy
```

- **読み込みの順序**：`.json` があればそれを使い、なければ `.example.json` を使います。
- **更新しても消えない**：ZIPを上書き展開しても、自分で作った `.json` は上書きされません。`.example.json` を直接編集すると、更新のたびに既定値へ戻ります。
- **Gitには上がらない**：`.json` は `.gitignore` の対象で、リポジトリには含まれません。

保存先は、コマンドラインの `--output-dir` が最優先です。設定ファイルを別の場所に置く場合は `--paths-config` で指定します。

## うまくいかないとき

- **リーダーの状態を確認する**：`python cart_station.py --port COM5 probe`（MiSTerでは `--usb probe`）で、製品情報とカートリッジの状態を表示します
- **MiSTerの環境を確認する**：`sh cart_station_check.sh` で、Python、通信ライブラリー、USB機器の認識状態を表示します（リーダーにはコマンドを送りません）
- **通信エラー**：受信できたバイト数などを画面に表示します。設計は [通信設計](docs/ja/PROTOCOL.md)
- **取り込み時間**：最新1回分を `runtime/last_capture_metrics.json` に記録します。記録内容は [取り込み・検証・保存](docs/ja/CAPTURE_FLOW.md) に書いてあります
- **MiSTerのログ**：`runtime/manager.log`

## コマンドライン

```sh
python cart_station.py --help
python cart_station.py list                    # シリアルポートを列挙（接続しない）
python cart_station.py --usb identify-sfc      # ヘッダーの確認
python cart_station.py --usb dump-sfc          # 取り込み
python cart_station.py list-db --query "<タイトルの一部>" # FCのDB検索（接続しない）
```

機種ごとに `identify-*`／`dump-*`（`sfc`・`md`・`gba`・`gb`・`pce`・`db`=FC）があります。コマンドの一覧は [コマンドライン設計](docs/ja/CLI.md) を参照してください。

## ライセンス

本体はGPL-3.0-onlyです（[LICENSE](LICENSE)）。pyserialとPyUSBのライセンス全文は `licenses/` にあります。利用者がpipで導入するPyQt6とQt関連パッケージを含む依存物の扱いは [LICENSES.md](docs/ja/LICENSES.md) を参照してください。
