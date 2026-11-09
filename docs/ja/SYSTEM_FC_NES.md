# FC/NES 読み出し設計

[English](../SYSTEM_FC_NES.md)

## 対象と前提

- FC（ファミリーコンピュータ）カートリッジをFCスロットで読む。NES（海外版）の実物はNESカートリッジコンバーターを介して接続する。処理上は区別しない。
- カートリッジ状態コードは3。
- 読み出すのはPRG ROMとCHR ROMだけ。CHR RAM、PRG RAM、セーブ（バッテリーバックアップ）、EEPROM、フラッシュ、音源、拡張メモリの書き込みと読み出しは行わない。
- 出力は、DBのヘッダー（16バイトのiNES／NES 2.0）にPRG・CHRを続けた .nes ファイル。ヘッダーは推測せず、検証済みのDBのヘッダーを使う。
- 対応範囲は「DBの各プロファイルが、下の読み出し条件に当てはまるか」で決まる。条件外は理由を示して取り込みを無効にする。

## 識別

- 書き込みもバンク切替も行わず、初期状態のPRGの先頭1 KiBと末尾1 KiBの部分ハッシュで、DBの候補を探す（0x8000からの1 KiBダミー読み出しの後、0x8000と0xFC00から1 KiBずつ）。
- 候補が1件ならそれを選択する。同じ読み出し構成の複数候補は、読み出し後の全体ハッシュで決める（自動判定）。それ以外は利用者が選ぶ。
- 取り込み開始の直前にもう一度識別し、選択した候補が含まれていなければ停止する。

## 読み出しの基本構造

`MapperReader` はバンクの走査と選択手順を持ち、`adapter.nes`（`cart_station_nes_registers.NesRegisters`）を通してレジスターを操作する。許可アドレス・値の検査と保護を含む一連の書き込みはこの専用モジュールにまとめ、転送層にマッパー番号の分岐を置かない。共通のバンク走査は `cart_station_banks.read_banks` を直接参照する。

1. モード開始（04(1)、05、200ms待機）。
2. バンクごとにマッパーのレジスターへ書き込み、CPU空間（コマンド0x11）またはPPU空間（コマンド0x13）から読む。PRGは8 KiB／16 KiB／32 KiB単位、CHRは1 KiB〜8 KiB単位など、マッパーごとの窓の大きさに従う。
3. 全バンクを順に読み、PRG、CHRの順に連結する。
4. 同じ手順をもう一度行い（独立2回読み）、2回が一致することを確認する。
5. PRG、CHR、PRG+CHR、（あれば）完成ファイルのSHA-1をDBと照合する。
6. モード終了（04(0)、05）。成功・失敗・中止のどれでも試みる。
- バンク走査は共通の関数が持つ。バンク番号、端数、進捗、短い応答の検出を共通化し、マッパーごとにバス操作だけを差し替える。
- 通信失敗が起きたら、以後のレジスター操作を続けず、再試行もしない。外側ゲートなどの閉鎖が完了したとは保証しない。通常のモード終了処理は実行する。

### レジスター書き込みの方針

- 書き込みはROMバンクの切替レジスターだけ。通常の書き込み（コマンド0x12）の後は、0x8000から8バイトのダミー読み出しを行う。
- 汎用の書き込み口（0x8000〜0xFFFF、値0〜255）の他に、基板専用の書き込み口を持つ。専用の口は許可するアドレスと値を厳密に絞り、セーブRAMの有効化、IDやアンロックの鍵、フラッシュ書き換え、EEPROM操作へ到達させない。低アドレス（0x4000〜0x7FFF）のレジスターが必要な基板は専用の口を使う。
- バスコンフリクトのある基板では、書き込むバイト値と同じ値を持つROM上のアドレスを探して書く（探索付き書き込み）。

### 通信順序の保持

- 基準通信記録を基準にして、読み出しのアドレス・サイズ・書き込み順を比較する。基板固有の補正（初期化の追加、SRAM保護書き込み、外側バンクのゲート操作）は、基準記録に挿入した差分として明示して比較する。

## 対応マッパー

### Mapper 0（NROM）

- 副番号0、PRG 16／32 KiB、CHR 0／8 KiB。バンク切替なし。PRGは0x8000から16 KiBずつ、CHRはPPU 0から8 KiB。

### 全体の条件

- 副番号は表に示すものだけ。示されない副番号は未対応。
- 容量は表の範囲内で、単位の倍数であること。DBのヘッダーで表現できる大きさに限る。
- 表の条件は「読み出しを試行できる条件」であり、全タイトル・全基板で成功することの保証ではない。実機未検証の基板は、そのことを明記する。

### 一覧

| Mapper | 基準通信記録 | 適用条件 |
|---|---|---|
| 1 | mapper001、mapper001_chr8k、mapper001_prg64k | Submapper 0、PRG 32～512 KiB、CHR 0～128 KiB。サイズに合うCHR 8 KiB用・PRG 64 KiB用も全体ハッシュで照合 |
| 1.5 | mapper001に固定PRG用経路を追加 | PRG固定32 KiB、CHR ROM 8/16/32/64/128 KiB。PRGバンク操作なし |
| 2 | mapper002 | Submapper 0/1/2、PRG 16 KiB単位で最大4 MiB、CHR ROMなし |
| 3 | mapper003、mapper003_ex | Submapper 0/1/2、PRG 16/32 KiB、CHR 8 KiB単位で最大2 MiB。CHR 32 KiB超はExのバンク値を使用 |
| 4 | mapper004 | Submapper 0/1/3、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で最大256 KiB |
| 5 | mapper005（MMC5）、初期モードを修正 | Submapper 0、PRG 8 KiB単位で32 KiB～1 MiB、CHR 2 KiB単位で8～512 KiB。SRAM保護を追加。大容量CHRの上位ビット経路は未対応 |
| 7 | mapper007（AxROM） | Submapper 0/1/2、PRG 32 KiB単位で32 KiB～8 MiB、CHR ROMなし。バイト値による探索付き切替 |
| 9 | mapper009（MMC2） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 4 KiB単位で8 KiB～1 MiB。上限はバンク番号（バイト値）で表現できる範囲 |
| 10 | mapper010（MMC4） | Submapper 0、PRG 16 KiB単位で32 KiB～4 MiB、CHRは9と同条件。ROMのみで、セーブの読み書きは行わない |
| 11 | mapper011（Color Dreams） | Submapper 0、PRG 32 KiB単位で最大128 KiB、CHR 8 KiB単位で最大128 KiB。実機未検証 |
| 13 | mapper013（CPROM） | Submapper 0、PRG固定32 KiB、CHR ROMなしのみ。CHR RAMは読み出さない。実機未検証 |
| 16 | mapper016（Bandai FCG） | Submapper 4/5、PRG 16 KiB単位で16～256 KiB、CHR 1 KiB単位で8～256 KiB。副番号0などは未対応 |
| 18 | mapper018（Jaleco SS88006） | Submapper 0、PRG 8 KiB単位で32～512 KiB、CHR 1 KiB単位で8～256 KiB。分割レジスターを設定 |
| 19 | mapper019（Namco 129/163） | Submapper 0/2/3/4/5、PRG・CHR容量は18と同じ。CPU 0xA000からPRGを読み、DBの副番号をヘッダーに維持 |
| 21 | mapper021_vrc4a / mapper021_vrc4c | Submapper 1/2、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～512 KiB。副番号0は未対応 |
| 22 | mapper022（VRC2a） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。PRG窓はCPU 0xA000、CHR番号を左へ1ビットシフトして分割設定 |
| 23 | mapper023_vrc4f / mapper023_vrc4e / mapper023_vrc2b | Submapper 1/2/3。PRGは21と同じ、CHRは1/2が最大512 KiB、3は最大256 KiB。副番号0は未対応 |
| 25 | mapper025_vrc4b / mapper025_vrc4d / mapper025_vrc2c | Submapper 1/2/3。容量条件は23と同じ。副番号0は未対応 |
| 24 / 26 | mapper024 / mapper026（VRC6a/b） | Submapper 0、PRG 16 KiB単位で32 KiB～4 MiB、CHR 1 KiB単位で8～256 KiB。CHRごとに0xB003を0に設定 |
| 30 | mapper030（UNROM512） | Submapper 0、PRG 16 KiB単位で16～512 KiB、CHR ROMなし。0xC000～0xFFFFで探索付き書込。フラッシュ書換えは行わない |
| 32 | mapper032（Irem G-101） | Submapper 0はPRG 8 KiB単位で32 KiB～2 MiB。副番号1は32～256 KiB、固定8+8+16F方式。CHRは両方とも1 KiB単位で8～256 KiB |
| 33 | mapper033（Taito TC0190） | Submapper 0、PRG・CHR容量は32と同じ。CHR読出し窓はPPU 0x1000 |
| 34 BNROM | mapper034_bnrom | Submapper 0/2、PRG 32 KiB単位で32 KiB～8 MiB、CHR ROMなし |
| 34.1 | mapper034_nina001 | PRG 32/64 KiB、CHR 4 KiB単位で8～64 KiB。0x7FFD/0x7FFEだけに専用書込。実機未検証 |
| 37 / 47 | mapper037 / mapper047、外側ゲート追加・47境界修正 | Submapper 0、PRG 256 KiB＋CHR 256 KiB、バッテリーなし・PRG RAM指定なし。PRG 8 KiB、CHR 1 KiBで読む。実機未検証 |
| 48 | mapper048（Taito TC0690） | Submapper 0/1、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。CHR窓はPPU 0x1000 |
| 64 | mapper064（Tengen RAMBO-1） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。CHRセレクターは32、PPU窓は0 |
| 65 | mapper065（Irem H3001） | Submapper 0、PRG・CHR容量は32と同じ。PRG読出し窓はCPU 0xA000 |
| 66 | mapper066（GxROM） | Submapper 0、PRG 32 KiB単位で最大512 KiB、CHR 8 KiB単位で最大128 KiB。バイト値・4ビットマスクで表現可能な範囲に限定 |
| 67 | mapper067（Sunsoft-3） | Submapper 0、PRG 16 KiB単位で最大4 MiB、CHR 2 KiB単位で8～512 KiB |
| 68.0 | mapper068（Sunsoft-4通常） | PRG 16 KiB単位で最大256 KiB、CHR 2 KiB単位で8～512 KiB |
| 68.1 | mapper068_ex（Sunsoft-4 Nante+Kogame） | PRG 128/144 KiB、CHR 2 KiB単位で8～512 KiB。内部8バンク＋外部1バンクまで |
| 69 | mapper069（Sunsoft FME-7） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。コマンド9/0でPRG/CHRを選択 |
| 70 | mapper070 | Submapper 0、PRG 16 KiB単位で最大256 KiB、CHR 8 KiB単位で最大128 KiB |
| 71 | mapper071 | Submapper 0、PRG 16 KiB単位で最大4 MiB、CHR ROMなし、実機未検証 |
| 72 | mapper072 | Submapper 0、PRG 16 KiB単位で16 KiB～2 MiB、CHR 8 KiB単位で8～512 KiB。探索付き2段階書込後にCPU 0x8000から読む |
| 73 | mapper073（VRC3） | Submapper 0、PRG 16 KiB単位で16 KiB～4 MiB、CHR ROMなし。0xF000でPRG切替 |
| 75 | mapper075（VRC1） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 4 KiB単位で8～128 KiB |
| 76 | mapper076（NAMCOT-3446） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 2 KiB単位で8～512 KiB |
| 77 | mapper077 | Submapper 0、PRG 32 KiB単位で32～512 KiB、CHR 2 KiB単位で8～32 KiB。PPU 0の2 KiB ROM窓だけを読む |
| 78 | mapper078 | Submapper 0/1/3、PRG 16 KiB単位で最大128 KiB、CHR 8 KiB単位で最大128 KiB。DB副番号はヘッダーに保持 |
| 80 | mapper080（Taito X1-005） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。低レジスターは書き込みコマンド0x15 |
| 82 | mapper082（Taito X1-017） | Submapper 0、PRG 8 KiB単位で32～512 KiB、CHR 1 KiB単位で8～256 KiB。初期化・PRG番号左2ビットシフト・CHRペア設定を実施 |
| 552 | mapper552（Taito X1-017、PRGレジスター6ビット逆順配線） | Submapper 0、PRG 8 KiB単位で32～512 KiB、CHR 1 KiB単位で8～256 KiB。初期化は82と同じ。PRG番号は6ビットを逆順にして0x7EFAへ書き、CHRは0x7EF2のみ（0x7EF3は書かない） |
| 85 | mapper085（VRC7） | Submapper 0/1/2、PRG 8 KiB単位で32 KiB～2 MiB。CHR ROMなし、または1 KiB単位で8～256 KiB |
| 86 | mapper086（JF-13） | Submapper 0、PRG 32 KiB単位で32～128 KiB、CHR 8 KiB単位で8～64 KiB。0x6000専用書込 |
| 87 | mapper087 | Submapper 0、PRG固定16/32 KiB、CHR 16/32 KiB。各読出しでPRG全体一致後に限定したCHR切替を行う |
| 88 | mapper088 | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 2 KiB単位で8～128 KiB。64 KiBから上位窓・ペア設定へ切替 |
| 89 | mapper089（Sunsoft-2） | Submapper 0、PRG 16 KiB単位で最大128 KiB、CHR 8 KiB単位で最大128 KiB。CHR番号bit 3をレジスターbit 7へ配置 |
| 92 | mapper092 | Submapper 0、容量条件は72と同じ。PRG窓はCPU 0xC000 |
| 93 | mapper093 | Submapper 0、PRG 16 KiB単位で16～128 KiB、CHR ROMなし。0x6000書込後に0xD000～0xFFFFを探索して書込 |
| 94 | mapper094（HVC-UN1ROM） | Submapper 0、PRG 16 KiB単位で最大1 MiB、CHR ROMなし |
| 95 | mapper095（NAMCOT-3425） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。PPU 0x1000から読む |
| 96 | mapper096 | Submapper 0、PRG 32 KiB単位で32～128 KiB、CHR ROMなしのみ。CHR RAMをROMへ追加しない |
| 97 | mapper097（Irem TAM-S1） | Submapper 0、PRG 16 KiB単位で最大4 MiB、CHR ROMなし。0x8000へ書き、0xC000から読む |
| 118 | mapper118 | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～128 KiB |
| 119 | mapper119 | Submapper 0、PRGは118と同じ、CHR ROM 1 KiB単位で8～64 KiB。実機未検証、CHR RAMは読み出し対象外 |
| 140 | mapper140 | Submapper 0、PRG 32 KiB単位で32～512 KiB、CHR 8 KiB単位で8～128 KiB。0x6000専用書込 |
| 148 | mapper148 | Submapper 0、PRG 32/64 KiB、CHR 8 KiB単位で8～64 KiB。実機未検証 |
| 152 | mapper152 | Submapper 0、PRG 16 KiB単位で最大128 KiB、CHR 8 KiB単位で最大128 KiB |
| 153 | mapper153（Bandai FCG＋SRAM） | Submapper 0、PRG 16 KiB単位で32～512 KiB、CHR ROMなし。各バンク前にSRAM無効化。セーブ読書きは対象外 |
| 154 | mapper154（NAMCOT-3453） | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 2 KiB単位で8～128 KiB。64 KiB境界でペア設定・PPU窓を切替 |
| 155 | mapper155（MMC1A） | Submapper 0、PRG 16 KiB単位で32～512 KiB、CHR 4 KiB単位で0～128 KiB。MMC1のタイトル専用変種は試行しない |
| 157 | mapper157（Datach Joint ROM） | Submapper 0、PRG 16 KiB単位で16 KiB～4 MiB、CHR ROMなし。4個の外側番号レジスターとPRG番号を設定 |
| 158 | mapper158 | Submapper 0、PRGは64と同じ、CHR 1 KiB単位で8～128 KiB。実機未検証 |
| 159 | mapper159（Bandai FCG） | Submapper 0、容量条件は16と同じ。EEPROM操作は対象外 |
| 180 | mapper180 | Submapper 0、PRG 16 KiB単位で最大4 MiB、CHR ROMなし。切替PRGの読出し窓は0xC000 |
| 184 | mapper184（Sunsoft-1） | Submapper 0、PRG固定16/32 KiB、CHR 4 KiB単位で8～32 KiB。固定PRGの検証後だけCHR選択 |
| 185 | mapper185（CNROM-CP） | Submapper 4/5/6/7、PRG固定16/32 KiB。既知PRG CRCの解除だけを許可。通常CHR 8 KiB、9CF17FABだけ16 KiB。49FF2E0Bは副番号5・PRG 32 KiB・CHR 8 KiBのみ。SHA-1・実CRC照合後に解除 |
| 186 | mapper186（StudyBox） | Submapper 0、PRG 16 KiB単位で16 KiB～4 MiB、CHR ROMなし。先頭は0xC000から、以後0x4201で選択して0x8000から読む |
| 188 | mapper188 / mapper188_ex | Submapper 0、PRG 16 KiB単位で16～256 KiB、CHR ROMなし。8番バンク境界で内部／外部選択値が変わる |
| 206.0 | mapper206 | PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。バンク切替型 |
| 206.1 | mapper206_prg32k | PRG固定32 KiB、CHR 1 KiB単位で8～256 KiB。PRGレジスターへ書かず2個の16 KiB窓から読む |
| 207 | mapper207 | Submapper 0、PRG・CHR容量は80と同じ。低レジスターは通常書込コマンド0x12 |
| 210 | mapper210（Namco 175/340） | Submapper 0/1/2、PRG 8 KiB単位で32～512 KiB、CHR 1 KiB単位で8～256 KiB。PRG番号を6ビットに制限 |
| 406 | mapper406_axr / mapper406_inl | Submapper 0、PRG 8 KiB単位で32 KiB～2 MiB、CHR 1 KiB単位で8～256 KiB。ROM用レジスター変種を全体ハッシュで選択。INLは実機未検証 |

### Mapper 185のCHR解除値

| PRG CRC32 | 解除値の順序 |
|---|---|
| 2A629F7D | 240 → 15 |
| 725D5CC4 | 0 → 51 |
| AA755715 | 0 → 17 |
| 87A3F91E / DAADEBFA / 81A743F5 | 32 → 34 |
| 73DE64AD | 0 → 42 |
| B36457C7 | 19 → 33 |
| A03C98A7 | 33 → 32 |
| 9CF17FAB | CHRバンク0は16 → 18、次は0 → 19 |
| 49FF2E0B | 0 → 17（副番号5・PRG 32 KiB・CHR 8 KiB） |

### VRC2／VRC4のCHR上位レジスター

| Mapper.Submapper | 実装 | CHR上位レジスター |
|---|---|---|
| 21.1 | mapper021_vrc4a | 0xB002 |
| 21.2 | mapper021_vrc4c | 0xB040 |
| 23.1 | mapper023_vrc4f | 0xB001 |
| 23.2 | mapper023_vrc4e | 0xB004 |
| 23.3 | mapper023_vrc2b | 0xB001 |
| 25.1 | mapper025_vrc4b | 0xB002 |
| 25.2 | mapper025_vrc4d | 0xB008 |
| 25.3 | mapper025_vrc2c | 0xB002 |

### 基板ごとの設計メモ

#### Mapper 1（MMC1）系
- 通常（副番号0）はPRG 32〜512 KiB、CHR 0〜128 KiB。CHRが8 KiB固定のもの、PRGが64 KiBのものは別の読み出し変種を持ち、複数の変種が当てはまる場合は、最初の読み出しの全体ハッシュで変種を選ぶ。一度一致した変種で2回目に失敗したら、再試行せず停止する。
- 1.5（副番号5）は、PRGが物理的に32 KiB固定の基板（SEROM／SHROM／SH1ROM）。PRGバンク操作をせず、CHR ROMのバンクだけを読む。

#### Mapper 5（MMC5）
- 副番号0、PRG 8 KiB単位で32 KiB〜1 MiB、CHR 2 KiB単位で8〜512 KiB。
- PRGモード（0x5100）は、8 KiB窓の選択と整合させるため、PRGモード3に設定する。
- 毎回の読み出し初期化の先頭で、SRAMの書き込み保護（0x5102、0x5103を0）を設定する。セーブ解除の値、RAM選択、拡張RAM、音源のレジスターは使わない。
- PRGは0x5114にROM指定ビットを立てた番号を書き、0x8000から8 KiB読む。
- 大容量CHRの上位ビット経路は未対応のため、CHRは上限を超えない構成に限る。

#### Mapper 37／47（複数ゲーム基板）
- 副番号0、PRG 256 KiB＋CHR 256 KiBで、バッテリー・PRG RAM指定がないものに限る。
- 外側バンクを0x6000で選び、MMC3のセレクター6でPRG 8 KiBを0x8000から、セレクター2でCHR 1 KiBをPPU 0x1000から読む。
- 37は、PRGの最初の64 KiBを外側8、次の64 KiBを11、残りの128 KiBを12で選ぶ。CHRは前半128 KiBが8、後半が12。
- 47は、外側バンクをPRG 16個の8 KiBバンク、CHR 128個の1 KiBバンクの単位で扱う。
- 外側ラッチを更新するときだけ書き込みゲート（0xA001=128）を開いて0x6000へ書き、その後に閉じる。

#### Mapper 32 副番号1
- PRGが 8+8+16F 方式で固定され、モード切替ビットを使わない。0x9000=0 でモード0を指定し、0x8000へバンク番号を書いて0x8000から読む。CHRは0xB000。PRG上限は256 KiB。

#### Mapper 82／552（Taito X1-017）
- 82は、初期化（0x7EF7／8／9に255、0x7EF6に0）、PRG番号を0x7EFAへ（左2ビットシフト）、CHRペアの設定。
- 552は、PRGレジスターの6ビットが逆順に配線された基板。PRG番号を6ビット逆順にして0x7EFAへ、CHR番号を0x7EF2へ書く。初期化は82と同じで、CHRペア用の0x7EF3は書かない。
- 82と552は、6ビットのPRGレジスターを持つため、PRGは最大512 KiB。

#### Mapper 34（BNROM／NINA-001）
- BNROM（副番号0／2）：PRG 32 KiB単位でCHR ROMなし。
- NINA-001（副番号1）：0x7FFDにPRG番号（0／1）、0x7FFEにCHR番号（0〜15）を書く専用の口を使う。許可するアドレスは0x7FFDと0x7FFEだけ。PRG 32／64 KiB、CHR 4 KiB単位で8〜64 KiB。

#### Mapper 153（Bandai FCG＋SRAM）
- 各PRGバンクの読み出し前に、SRAM無効化（0x800D=0）を書く。番号÷16を0x8000〜0x8003へ、番号を0x6008と0x8008へ書く。バンク番号は0〜31。セーブの読み書きは行わない。

#### Mapper 186（StudyBox）
- 先頭のPRGバンクは書き込みなしで0xC000から16 KiB読む。以降は固定のIO口0x4201へ番号（1〜255）を書き、0x8000から読む。CHR RAMは出力に含めない。

#### Mapper 406
- AXR（セレクター0x8000／データ0x8002）とINL（セレクター0xE000／データ0xE001）の2つの変種を、AXR、INLの順に試し、全体ハッシュで決める。

#### Mapper 185（CNROM-CP）
- PRGは固定。CHR ROMは保護されており、既知のPRG CRC32に対する解除の書き込み（2値の組）だけを許す。未登録のCRCは通信前に停止する。
- 通常はCHR 8 KiB。CRC 9CF17FABだけは16 KiB。CRC 49FF2E0B（Mighty Bomb Jack PRG1）は副番号5、PRG 32 KiB、CHR 8 KiBに限り、0→0x11を送る。
- 固定PRGの全体SHA-1と実CRCで確認してから、CHRの保護を解除する。

#### Mapper 87
- PRGは固定16／32 KiB。各読み出しでPRG全体が一致した後にだけ、0x6000〜0x7FFFの専用の口（値は0〜3）でCHRを切り替える。

#### Mapper 206 副番号1
- PRGは固定32 KiB。PRGレジスターへ書かず、2つの16 KiB窓から読む。CHRは1 KiB単位。
- 副番号0（通常の206）はバンク切替型。

#### 容量に関する制限
- バンク番号をバイト値（0〜255）で扱う箇所があり、表の上限はその表現できる範囲に限る。上限を超える容量は通信前に停止する。
- CHR RAMだけの基板、MMC1の個別タイトル専用変種は対象外。

## 計測と最適化

- 取り込み時間は通信が支配的。固定の探索領域（Mapper 2、3の探索付き書き込み）は、1回の読み出しの中だけで再利用し、2回目は探索も本体も読み直す。独立2回読みの独立性を保つため。

## 検証の位置づけ

- 各マッパーは、アドレスデコードを独立に模したモデルでROMを再構成して検証する。実機未検証の基板や、実物がないものは、モデルでの検証にとどまる。
- DBに収録されていること、読み出しが実装されていること、実カートリッジで成功したことは、それぞれ別の事実として扱う。
