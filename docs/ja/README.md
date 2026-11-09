# 設計書

[English](../README.md)

Cartridge Station for RetroFreak の設計を、項目ごとにまとめた文書群です。各文書は単独で読めるように、必要な事実をその文書の中に書いています。

## まず読むもの

| 知りたいこと | 読む文書 |
|---|---|
| 全体像・設計原則 | [OVERVIEW](OVERVIEW.md) |
| 使い方（画面・コマンド） | [FRONTEND_WINDOWS_GUI](FRONTEND_WINDOWS_GUI.md)、[FRONTEND_MISTER_MANAGER](FRONTEND_MISTER_MANAGER.md)、[CLI](CLI.md) |
| 配布ZIPの入手・配置・更新 | [DISTRIBUTION](DISTRIBUTION.md)、[CONFIGURATION](CONFIGURATION.md) |
| 特定機種の読み出し | 下の「機種別設計」 |
| 開発・試験・実機検証 | [TESTING](TESTING.md)、[HARDWARE_VALIDATION](HARDWARE_VALIDATION.md) |
| ライセンス・データの出典 | [LICENSES](LICENSES.md)、[DATABASE](DATABASE.md) |

## 共通設計

| 文書 | 内容 |
|---|---|
| [OVERVIEW](OVERVIEW.md) | 目的、設計原則、対象外、動作環境、レイヤー構成、取り込みの全体手順 |
| [PROTOCOL](PROTOCOL.md) | リーダー、転送路（シリアル・USB）、パケット、コマンド、状態コード、エラー設計 |
| [CAPTURE_FLOW](CAPTURE_FLOW.md) | サービス層、識別と候補選択、2回読みと検証、保存、ゲーム起動、取り込み時間の計測 |
| [DATABASE](DATABASE.md) | 同梱DBの形式、収録内容、照合、出典と更新方針 |
| [CONFIGURATION](CONFIGURATION.md) | 名称、フォルダー構成、設定ファイル、解決順、依存ライブラリー |
| [DISTRIBUTION](DISTRIBUTION.md) | 配布ZIPの種類と中身、ビルド、配置と更新、リポジトリの運用 |

## 機種別設計

| 文書 | 内容 |
|---|---|
| [SYSTEM_FC_NES](SYSTEM_FC_NES.md) | FC/NES：識別、読み出しの構造、マッパー別の条件と設計メモ |
| [SYSTEM_GB_GBC](SYSTEM_GB_GBC.md) | GB／GBC：ヘッダー、MBCごとの読み出し、分類の診断 |
| [SYSTEM_GBA](SYSTEM_GBA.md) | GBA：ヘッダー、DBによる容量の確定 |
| [SYSTEM_MD](SYSTEM_MD.md) | メガドライブ：ヘッダー、読み出し方式 |
| [SYSTEM_PCE](SYSTEM_PCE.md) | PCエンジン／SuperGrafx：方式判定、データ順、挿入時の識別 |
| [SYSTEM_SFC](SYSTEM_SFC.md) | SFC：ヘッダー、ヘッダー不正の限定再試行、読み出し方式 |

## 画面とコマンド

| 文書 | 内容 |
|---|---|
| [FRONTEND_WINDOWS_GUI](FRONTEND_WINDOWS_GUI.md) | Windows版のPyQt6 GUI |
| [FRONTEND_MISTER_MANAGER](FRONTEND_MISTER_MANAGER.md) | MiSTer版のテレビ・パッド向け文字メニュー |
| [CLI](CLI.md) | コマンドラインのサブコマンドと動作 |

## 品質・権利

| 文書 | 内容 |
|---|---|
| [TESTING](TESTING.md) | 試験の構成と観点 |
| [HARDWARE_VALIDATION](HARDWARE_VALIDATION.md) | 実機検証の未完了範囲、優先順位、記録する証拠 |
| [LICENSES](LICENSES.md) | 本体のライセンス、依存ライブラリー、データの方針、名称と商標、実機検証への参照 |
