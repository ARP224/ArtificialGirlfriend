"""
check_character_data.py — character_data/ の点検(孤児・付随ファイル・ジャンクの有無)

使い方(リポジトリ直下で):
    venv\\Scripts\\python.exe tests\\scripts\\check_character_data.py      (Windows)
    venv/bin/python tests/scripts/check_character_data.py                (Mac)

何も変更しない(読むだけ)。出力:
  1. キャラ数と memory/ の内訳(.db / -wal,-shm 付随ファイル / その他)
  2. キャラごとのフォルダ全部を横断して「config の無い uuid」(孤児)を列挙
  3. どのキャラの config からも参照されていないアイコン
終了コード: きれい=0 / 何か見つかった=1

期待値(2026-08-17 の対策後):
  - アプリ終了後は付随ファイル 0 本(起動中は使っているキャラの分だけ 2 本=正常)
  - ただし起動直後の WAL sweep 時に AV 等が side file を掴むと SQLite の削除が
    失敗し、0バイトの殻が全キャラ分そのセッション中残ることがある(2026-08-21 実証)。
    データ影響なし・次回起動の sweep で自然回収=異常ではない
  - 孤児 0・その他 0
  - 添付は attachments/<uuid>/ に分離済(2026-08-20)。memory/ 直下の
    uuid フォルダは旧レイアウトの残骸=「その他」として異常検出する
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.shared.constants import (  # noqa: E402
    CHARACTER_CONFIGS_DIR, CHARACTER_ICONS_DIR, MEMORY_DIR, RELATIONSHIP_DIR, NOTE_DIR,
    ELYTH_NOTE_DIR, ELYTH_RELATIONSHIP_DIR, ELYTH_SESSION_DIR, ELYTH_THREAD_STATE_DIR,
    GENERATED_IMAGES_DIR, ATTACHMENTS_DIR, DATA_DIR,
)

UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(.*)$")

# OS が勝手に置くメタファイル(Finder/Explorer で開いただけで生まれる)＝ジャンクではない
OS_METADATA = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _is_os_metadata(name: str) -> bool:
    return name in OS_METADATA or name.startswith("._")  # ._* = macOS AppleDouble

# キャラ id を名前に持つファイル/フォルダが置かれるフォルダ(memory/ 以外)
PER_CHARACTER_DIRS = [
    ("relationship", RELATIONSHIP_DIR),
    ("notes", NOTE_DIR),
    # ELYTH系4フォルダは 2026-08-20 に character_data/elyth/ 配下へ集約
    ("elyth/notes", ELYTH_NOTE_DIR),
    ("elyth/relationships", ELYTH_RELATIONSHIP_DIR),
    # 2026-08-18 に logs/elyth_sessions/ から移設(短期記憶=運用ログではない)。
    # 網が character_data/ と logs/ に割れていたせいで削除時の remover が
    # 付いておらず、孤児がこの点検からも漏れていた
    ("elyth/sessions", ELYTH_SESSION_DIR),
    ("elyth/thread_state", ELYTH_THREAD_STATE_DIR),
    ("generated_images", GENERATED_IMAGES_DIR),
    # 会話添付。2026-08-20 に memory/<uuid>/ から分離(.db との同居解消)
    ("attachments", ATTACHMENTS_DIR),
]


def load_characters():
    """{character_id: config} — config が壊れていても id はファイル名から拾う。"""
    chars = {}
    if not CHARACTER_CONFIGS_DIR.is_dir():
        return chars
    for p in sorted(CHARACTER_CONFIGS_DIR.glob("*.json")):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        cid = (cfg.get("character_id") or p.stem).strip().lower()
        chars[cid] = cfg
    return chars


def main() -> int:
    chars = load_characters()
    problems = 0
    print(f"character_data: {DATA_DIR}")
    print(f"キャラ数(config): {len(chars)}")
    print()

    # ---- 1. memory/ の内訳 ----------------------------------------------
    live_db = side = 0
    others = []      # 非 uuid 名・uuid だが config 無し・旧レイアウト残骸・想定外の拡張子
    if MEMORY_DIR.is_dir():
        for p in sorted(MEMORY_DIR.iterdir()):
            if _is_os_metadata(p.name):
                continue
            m = UUID_RE.match(p.name)
            if not m:
                others.append(p.name)
                continue
            cid, rest = m.groups()
            if cid not in chars:
                others.append(p.name)  # 孤児(下でも列挙)
            elif p.is_dir() and rest == "":
                # 旧添付レイアウト(〜2026-08-20)の残骸。現行は attachments/<uuid>/
                others.append(p.name + "/ (旧添付レイアウト残骸)")
            elif rest == ".db":
                live_db += 1
            elif rest in (".db-wal", ".db-shm"):
                side += 1
            else:
                others.append(p.name)
    print("[memory/]")
    print(f"  .db(現存キャラ)            : {live_db}")
    print(f"  -wal/-shm 付随ファイル     : {side}   (終了後は 0 が正常・起動中は使用中キャラ分+起動時の0バイト殻がありうる)")
    print(f"  その他(孤児・非uuid・想定外): {len(others)}")
    for name in others:
        print(f"      - {name}")
    problems += len(others)
    print()

    # ---- 2. 孤児 uuid の横断列挙 ---------------------------------------
    print("[孤児(config の無い uuid)]")
    orphans_found = 0
    for label, d in [("memory", MEMORY_DIR)] + PER_CHARACTER_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            m = UUID_RE.match(p.name)
            if m and m.group(1) not in chars:
                print(f"  {label}/{p.name}")
                orphans_found += 1
    # ELYTH own-handle registry (共有 JSON のキー)
    reg = ELYTH_THREAD_STATE_DIR / "_own_handles.json"
    if reg.exists():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for cid in sorted(data):
            if cid.strip().lower() not in chars:
                print(f"  elyth/thread_state/_own_handles.json のキー {cid}")
                orphans_found += 1
    if orphans_found == 0:
        print("  なし")
    # memory/ の孤児は「その他」で数えているので二重計上しない
    problems += max(0, orphans_found - sum(1 for n in others if UUID_RE.match(n)))
    print()

    # ---- 3. 参照されていないアイコン -------------------------------------
    print("[どの config からも参照されていないアイコン]")
    referenced = set()
    for cfg in chars.values():
        ip = cfg.get("icon_path")
        if ip:
            referenced.add(Path(ip).name)
    unref = []
    if CHARACTER_ICONS_DIR.is_dir():
        unref = sorted(p.name for p in CHARACTER_ICONS_DIR.iterdir()
                       if p.is_file() and p.name not in referenced and not _is_os_metadata(p.name))
    for name in unref:
        print(f"  {name}")
    if not unref:
        print("  なし")
    problems += len(unref)
    print()

    print("結果: " + ("きれい(問題なし)" if problems == 0 else f"要確認 {problems} 件"))
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
