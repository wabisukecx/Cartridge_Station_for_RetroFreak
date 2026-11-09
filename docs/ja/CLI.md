# コマンドライン設計

[English](../CLI.md)

## 入口

- `cart_station.py` が唯一の入口。`src` を検索パスの先頭に加え、vendor があればさらにその前に加えて、CLIを実行する。
- 終了時のエラーは `Stopped: <内容>` として標準エラーに出し、終了コード1で終わる。
- `cart_station.py` は起動専用。Pythonから利用・テストする場合は、`cart_station_cli`、`cart_station_transport`、`cart_station_protocol` などの実装モジュールを直接 import する。旧入口での再公開と `sys.modules` の差し替えは行わない。

## 共通オプション

| オプション | 内容 |
|---|---|
| `--port <ポート>` | シリアルポート（COM3、/dev/ttyACM0 など）。`--usb` と排他 |
| `--usb` | USB直接通信（MiSTerとCLI用。Windows GUIでは受け付けない） |
| `--environment auto\|pc\|mister` | 環境の判定を上書き（既定は自動） |
| `--paths-config <ファイル>` | 保存先設定を明示 |

## サブコマンド

| コマンド | 内容 |
|---|---|
| `list` | シリアルポートの列挙（VID／PIDつき）。リーダーを開かない |
| `probe` | 製品情報とカートリッジ状態の確認（ROMは読まない） |
| `gui` | Windows用のPyQt6 GUI。MiSTerでは使えない。`--output-dir`、`--db` を受け付ける |
| `manager` | MiSTer用のテレビ・パッド画面。`--output-dir`、`--core-config`、`--db`、`--preview`（実機を開かずに表示だけ確認） |
| `list-db` | FCのDBを検索する（接続しない）。`--db`、`--query`、`--mapper`、`--limit`（1〜1000） |
| `identify-db` / `dump-db` | FCの候補を識別、確認つきで取り込み。`dump-db` は `--profile <ID>`（識別された候補の中から選ぶ）または `--catalog-profile <ID>`（部分一致がなくてもDBの読み出し計画を明示。全体検証は必須）、`--output-dir` |
| `identify-gb` / `dump-gb` | GB／GBCのヘッダー確認、取り込み。`--gb-db`。`--output-dir` は `dump-gb` のみ |
| `identify-gba` / `dump-gba` | GBA。`--gba-db`。`--output-dir` は `dump-gba` のみ |
| `identify-md` / `dump-md` | MD。`--md-db`、`--controller None\|SAndN\|SuperSf2\|MortalK2`。`--output-dir` は `dump-md` のみ |
| `probe-pce` / `identify-pce` / `dump-pce` | PCE／SGX。`--pce-db`、`--controller Linear\|Interval\|Sf2Dash`。`--output-dir` は `dump-pce` のみ |
| `identify-sfc` / `dump-sfc` | SFC。`--sfc-db`。`--output-dir` は `dump-sfc` のみ |
| `diagnose-gb` | GBヘッダーの診断（3回読み）。`--reset`、`--trace <新規ファイル>` |

- 取り込み系のコマンドは、すべて対話端末で確認の入力（y）を求める。確認前にヘッダーや部分ハッシュを読み、確認後に保存用の独立2回読みを始める。`identify-pce` は保存せずに一度ROM全体を読んでDB照合する。
- Mapper 0（NROM）を含むFCの取り込みは `dump-db` に統合している。容量・ミラーリングを利用者が指定してDB照合なしに保存する旧コマンド（`dump-nrom`／`watch-nrom`）は廃止した。
- 出力先は、`--output-dir`、設定ファイルの機種別の保存先、既定の順に決まる。
- 保存は共通の保存処理を使い、同名・別内容は別名で保存する。保存後にディスク上のファイルを再照合し、「Saved and verified」または「Already saved (identical)」を表示する。GB／GBC、GBA、MD、PCE、SFCはSHA-1も表示する。

## 出力形式

- 識別系のコマンドは、JSONで結果を出力する（state、system、title、rom_size、readable、limitation など）。
- 進捗や再試行の回数は標準エラーへ出し、JSON出力に混ぜない。

## Pythonからの利用

- `cart_station_cli` はコマンドライン処理を担当し、旧APIをまとめて再公開する互換モジュールではない。
- 通信は `cart_station_transport`、共通定数は `cart_station_protocol`、DBは `cart_station_nes_db`、機種別の読み出しは各実装モジュールから直接 import する。
