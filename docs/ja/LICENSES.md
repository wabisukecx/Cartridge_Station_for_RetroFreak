# ライセンスと出典の設計

[English](../LICENSES.md)

## 本体のライセンス

- 本プロジェクトの独自コードと文書は GPL-3.0-only。全文はリポジトリ直下の LICENSE にあり、すべてのZIPに含める。
- Windows GUIが使うPyQt6がGPLv3または商用のデュアルライセンスであるため、GPLv3の条件にそろえた。

## 依存ライブラリー

| 対象 | 配布の扱い | 条件 |
|---|---|---|
| pyserial 3.5 | MiSTer版のvendorに純Pythonソースを同梱。Windowsはpip | BSD-3-Clause。ライセンス全文を licenses に収録し、著作権表示・条件・免責を保持 |
| PyUSB 1.3.1 | MiSTer版のvendorに純Pythonソースを同梱。Windowsはpip | BSD-3-Clause。ライセンス全文を licenses に収録。vendor内の原文も維持 |
| PyQt6 6.11.0 | Windowsで利用者がpip導入。ZIPに同梱しない | GPL-3.0-only（GPLv3／商用のデュアル） |
| PyQt6-Qt6 6.11.2 | 同上。バイナリを同梱しない | LGPL v3。再配布する場合はモジュールごとの条件を確認する |
| PyQt6-sip 13.13.0 | 同上。バイナリを同梱しない | BSD-2-Clause |
| Python、libusb | 利用環境のものを使う。同梱しない | 将来同梱するなら、その版と条件を別に記録する |

- `licenses/` に収録する全文はpyserialとPyUSBの2件。PyQt6とQt関連パッケージのライセンスは、pipで導入する各パッケージに付属するものを参照する。
- exe化しないことと、利用するライブラリーの条件を満たすことは別の問題として扱う。
- 同梱するバイナリ・フォントは持たない。

## 参考にした公開情報

- マッパーの仕様は、NESdev Wiki、gamebank.jp ダンパーのマッパー情報、Mesen（GPL v3）のマッパー処理を参考に、本プロジェクトで実装した。一覧は [DATABASE](DATABASE.md) の「参考にした公開情報」にある。
- Mesen のライセンスは GPL v3 で、本体の GPL-3.0-only と両立する。Mesen のソースコードを取り込む場合は、その著作権表示を保持する。

## データの方針

- ROMデータ、セーブデータはリポジトリにもZIPにも含めない。
- 本ツールの目的は、利用者自身が所有するカートリッジのバックアップであり、他者の著作物の配布ではない。

## 名称と商標

- 「RetroFreak」「レトロフリーク」は、それぞれの権利者の名称。本ツールはそれらの製品のカートリッジアダプターを読み出しに使う非公式ツールで、権利者とは関係がない。
- 「CYBER Gadget」「MiSTer」「Nintendo」「SEGA」「NEC」等の名称は、対応機種や互換性を示すためだけに使う。
- 製品名は、対応するハードウェアを示す説明的な用途にとどめる。

## 未確定事項

- 実機で読み出しを確認した範囲。確認済みの範囲は [HARDWARE_VALIDATION](HARDWARE_VALIDATION.md) に分けて記録する。
