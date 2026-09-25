#!/usr/bin/env python
"""
tests/scripts/seed_memory.py

長期記憶の抽出発火を「あと数件」の位置まで種データで押し上げる実機テスト用スクリプト。
100往復も会話せずに「抽出直前」の状態を作り、抽出／relationship／シャットダウンブロックを
実機で確認するための道具（Mac 実機テスト 2026-08-16 で初回作成・Windows 対応済み）。

製品と同じ書き込み経路（MemoryManager.add_message）を使うので、スキーマ差の
ズレは原理的に起きない。埋め込みだけ MemoryManager.DISABLE_EMBEDDINGS = True で
無効化する（種メッセージの埋め込みは抽出時の「関連既存記憶」の重心にしか
使われず、無くても抽出は正常動作する。長期記憶側の埋め込みは実機の抽出時に作られる）。
API キーやモデル設定・製品コード（backend/ ui/）には一切触らない。

ファイル配置（本体は tests/scripts/ 直下。seed_scripts/ の中に入れると REPO_ROOT の
逆算がズレて起動時の自己診断で止まる）:
    tests/scripts/seed_memory.py                  ← 本体
    tests/scripts/seed_scripts/ja_default.json    ← 日本語台本 50往復=100件（user から交互）
    tests/scripts/seed_scripts/ja_default_facts.md← 台本に仕込んだ「抽出されるべき事実」12件の照合表
    （英語キャラ用 en_default.json は未作成。必要になったら同形式で作るか --script で指定）

手順:
    1. UI からテスト専用のキャラを1体新規作成する
       （既にメッセージが入っている DB には投入しない設計＝既存キャラを汚さない安全弁。
         provider が ollama なら閾値 50 件、API プロバイダなら 100 件。台本はキャラの
         faster_whisper_config.language で <lang>_default.json を選ぶ）
    2. AG を完全に終了する（本体だけでなくトレイ＝通知領域/メニューバーのアイコンも。
       トレイが生きていると本体を再起動できてしまうため、トレイも検出して中止する）
    3. --dry-run で件数と発火予測だけ見る → 外して投入
    4. AG を起動して確認（下の「実機で見る項目」）

使い方:
    # Windows
    venv\\Scripts\\python.exe tests\\scripts\\seed_memory.py --list
    venv\\Scripts\\python.exe tests\\scripts\\seed_memory.py --character <uuid> --before 4 --dry-run
    venv\\Scripts\\python.exe tests\\scripts\\seed_memory.py --character <uuid> --before 4
    # macOS / Linux
    venv/bin/python tests/scripts/seed_memory.py --character <uuid> --before 4 --dry-run
    venv/bin/python tests/scripts/seed_memory.py --character <uuid> --before 4

引数:
    --character <uuid>  投入先。キャラが1体しか無いときは省略可（一覧は --list）
    --before N          閾値の何件手前で止めるか（既定 4＝2往復手前。偶数にすること）
    --script <path>     台本 JSON を指定（既定はキャラ言語の seed_scripts/<lang>_default.json）
    --dry-run           件数と発火予測を表示するだけ。DB を作りも書きもしない
    --list              キャラ一覧（uuid / provider / model / 言語）を出して終了
    --append-existing   既にメッセージがある DB にも追記する（既定は中止）
    --assume-stopped    AG の起動確認が機械的にできない環境で、終了済みだと確認したうえで続行

安全弁:
  - AG が起動中（tray_app.py / run.py / ArtificialGirlfriend.pyw / 127.0.0.1:7860 待受）
    なら投入せず中止。Windows は PowerShell の Get-CimInstance Win32_Process で
    コマンドラインを引く（tasklist はイメージ名しか出ず AG か判別できない）。
    プロセス一覧を引けなかったときは「停止している」と断定せず確認プロンプトへ
    （非対話環境では --assume-stopped が無ければ中止）。
  - 対象 DB に msg_count > 0 が既にあるなら投入せず中止（--append-existing で明示続行）
  - 台本の件数が足りなければ「巡回して埋める」ことはせずエラーで中止
    （同じ会話の繰り返しは抽出結果を濁す。Ollama・--before 4 なら 46 件、API なら 96 件が最低ライン）

台本を差し替えるとき:
  - [{"role":"user","content":"…"},{"role":"assistant","content":"…"},…] で user から始まり
    厳密に交互（違うと起動時に弾く）。先頭から順に使われるので抽出させたい事実は前半に。

投入直後の状態と実機で見る項目（--before 4 の場合）:
  - 起動→キャラ選択: 会話画面に台本の直近20件が復元。History の統計が
    「未処理 46（API なら 96）」、カウントダウン「あと 4 メッセージ」、「💬 会話履歴」の帯がほぼ満杯。
  - 1往復目: 未処理≥30 かつ relationship が初期テンプレ → relationship 早期更新
    （logs/app.log に Scheduling initial relationship update → Relationship updated、
      character_data/relationship/<uuid>.txt が新規作成）。更新中は Exit/Restart/サーバーモード
      がグレー・トレイの終了は拒否＋通知（2026-08-16 以降=is_memory_task_running）。
  - 2往復目: 未処理≥閾値 → 抽出発火（Scheduling background memory extraction）。抽出中〜
    relationship 更新完了までグレー/拒否が続き、Relationship updated の直後に緑へ戻る。
    Applied: N additions, M updates に対し History「📚 記憶」の件数が N+M。
    カウントダウンが閾値へリセット。ja_default_facts.md の12項目で○×を数える。
  - 次の会話: プロンプトログに <relationship> と <long_term_memory> セクションが載り、
    「好きな食べ物は？」に台本の事実で答える。
  - 切り分け: Failed to parse extraction response はモデル側の JSON 崩れ（配線ではない）。
    3回連続で Extraction circuit breaker active（1時間停止）。

動作の前提（接地事実）:
  - 発火判定は MemoryManager.add_message() の返り値 needs_extraction
    ＝ (msg_count − last_extracted) >= 閾値。ターン数ではなくメッセージ件数
    （user と assistant を別々に数える）。
"""

import argparse
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path


def _force_utf8_stdio():
    """コンソールを UTF-8 にする（run.py の同名処理と同じ理由）。

    このスクリプトは日本語をそのまま print するので、Windows の cp932
    コンソールだと UnicodeEncodeError で落ちる。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (stream.encoding or '').lower() not in ('utf-8', 'utf8'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


_force_utf8_stdio()

REPO_ROOT = Path(__file__).resolve().parents[2]

# 置き場所の自己診断。台本 JSON は seed_scripts/ の中だが、このスクリプト本体は
# その1階層上（tests/scripts/）が定位置。seed_scripts/ の中に置かれると
# REPO_ROOT が tests/ を指し、素の ModuleNotFoundError しか出ずに詰まる。
if not (REPO_ROOT / "backend").is_dir():
    sys.exit(
        "!! 置き場所が違います。\n"
        f"   いまの場所           : {Path(__file__).resolve()}\n"
        f"   逆算したリポジトリルート: {REPO_ROOT}  ← backend/ が見つかりません\n"
        "   正しい置き場所       : <リポジトリ>/tests/scripts/seed_memory.py\n"
        "   （seed_scripts/ の中に入れるのは台本 JSON と md だけ。本体は1階層上）"
    )

sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_DIR = Path(__file__).resolve().parent / "seed_scripts"

IS_WINDOWS = platform.system() == "Windows"

# AG 起動検出に使うプロセス名パターン。
#   Mac : .app → venv/bin/python launcher/tray_app.py → run.py
#   Win : ArtificialGirlfriend.pyw → venv\Scripts\pythonw.exe launcher\tray_app.py → run.py
# コマンドライン文字列に対する部分一致で見るので、区切り文字は含めない。
PROCESS_PATTERNS = ("tray_app.py", "run.py", "ArtificialGirlfriend.pyw")
WEB_PORT = 7860  # ui/app.py の launch_config launcher.web_port 既定値


# ---------------------------------------------------------------------------
# 安全弁
# ---------------------------------------------------------------------------


def _probe_processes_unix():
    """pgrep + ps でコマンドラインを引く。(hits, probe_ok) を返す。"""
    hits = []
    probe_ok = False
    for pattern in PROCESS_PATTERNS:
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            # pgrep 自体が無い/動かない = 調べられなかった（停止の証拠ではない）
            continue
        probe_ok = True
        for pid in out.stdout.split():
            if pid.strip() == str(os.getpid()):
                continue
            try:
                cmd = subprocess.run(
                    ["ps", "-p", pid, "-o", "command="],
                    capture_output=True, text=True, timeout=10
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                cmd = "(取得失敗)"
            # 自分自身（seed_memory.py）を掴まないよう除外
            if "seed_memory.py" in cmd:
                continue
            hits.append((pattern, pid, cmd))
    return hits, probe_ok


def _probe_processes_windows():
    """PowerShell の CIM でコマンドラインを引く。(hits, probe_ok) を返す。

    tasklist はイメージ名（python.exe / pythonw.exe）しか出さず、AG か
    無関係の Python かを区別できない。コマンドラインを見られるのは
    Win32_Process なので PowerShell 経由で引く。PowerShell が使えない
    環境では probe_ok=False を返し、呼び手が確認プロンプトへ落とす。
    """
    ps_script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine } | "
        "Select-Object ProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    for exe in ("powershell.exe", "pwsh.exe"):
        try:
            out = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0 or not out.stdout.strip():
            continue
        try:
            data = json.loads(out.stdout)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = [data]
        hits = []
        for proc in data:
            cmd = (proc.get("CommandLine") or "").strip()
            pid = str(proc.get("ProcessId", "?"))
            if pid == str(os.getpid()) or "seed_memory.py" in cmd:
                continue
            for pattern in PROCESS_PATTERNS:
                if pattern.lower() in cmd.lower():
                    hits.append((pattern, pid, cmd))
                    break
        return hits, True
    return [], False


def find_running_processes():
    """AG らしきプロセスを列挙する。

    Returns:
        (hits, probe_ok): hits は (pattern, pid, cmdline) の一覧。
        probe_ok=False は「調べられなかった」を意味し、**停止の証拠ではない**。
    """
    if IS_WINDOWS:
        return _probe_processes_windows()
    return _probe_processes_unix()


def web_port_is_open(port=WEB_PORT):
    """Gradio の待受ポートが開いていれば AG が動いているとみなす。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def assert_ag_stopped(assume_stopped=False):
    procs, probe_ok = find_running_processes()
    port_open = web_port_is_open()

    if procs or port_open:
        print("!! AG が起動中と判定されました。DB が使用中なので投入を中止します。")
        for pattern, pid, cmd in procs:
            print(f"   - pid={pid} ({pattern}): {cmd}")
        if port_open:
            print(f"   - 127.0.0.1:{WEB_PORT} が待受中")
        print("   トレイ →「終了」で AG を完全に終了してから、もう一度実行してください。")
        sys.exit(2)

    if probe_ok:
        print(f"[safe] AG は停止しています（プロセスなし / :{WEB_PORT} 未待受）")
        return

    # プロセス一覧を引けなかった。ポートが空いていることしか分かっていない
    # ので「停止している」と断定しない。
    print("!! AG のプロセス一覧を取得できませんでした"
          f"（:{WEB_PORT} は未待受）。起動中かどうかを機械的に確認できません。")
    print("   AG の本体とトレイ（メニューバー／通知領域のアイコン）の両方を")
    print("   終了してから続けてください。")
    if assume_stopped:
        print("   --assume-stopped が指定されているので続行します。")
        return
    if not sys.stdin or not sys.stdin.isatty():
        sys.exit("!! 対話できない環境です。AG を終了したうえで --assume-stopped を付けて実行してください。")
    answer = input("   AG は完全に終了していますか？ [y/N]: ").strip().lower()
    if answer != "y":
        sys.exit("!! 中止しました。")


# ---------------------------------------------------------------------------
# キャラ config / DB
# ---------------------------------------------------------------------------


def load_config(character_id):
    from backend.shared.constants import CHARACTER_CONFIGS_DIR
    path = CHARACTER_CONFIGS_DIR / f"{character_id}.json"
    if not path.exists():
        sys.exit(f"!! キャラ config が見つかりません: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_characters():
    from backend.shared.constants import CHARACTER_CONFIGS_DIR
    out = []
    for path in sorted(CHARACTER_CONFIGS_DIR.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        out.append(cfg)
    return out


def resolve_db_path(config, character_id):
    from backend.shared.constants import BASE_DIR, MEMORY_DIR
    raw = config.get("db_file_path") or ""
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (BASE_DIR / p)
    return MEMORY_DIR / f"{character_id}.db"


def read_meta_readonly(db_path, character_id):
    """DB を読むだけ（作らない）で msg_count / last_extracted を返す。"""
    counts = {"msg_count": 0, "last_extracted": 0, "exists": False}
    if not Path(db_path).exists() or Path(db_path).stat().st_size == 0:
        return counts
    counts["exists"] = True
    ns = json.dumps([character_id, "meta"], separators=(",", ":"))
    # 既存 DB が読めないまま「新規扱い」で進むと、既存メッセージの安全弁を
    # 素通りしてしまう。ro で開けないとき（WAL の -shm が作れない等）は
    # 読み書きモードで開き直し、それも駄目なら新規扱いにせず中止する。
    # Windows のパス（C:\...）は file: URI にそのまま埋め込めないので
    # Path.as_uri() で組む（file:///C:/... になる）。
    try:
        ro_dsn = Path(db_path).resolve().as_uri() + "?mode=ro"
    except ValueError:
        ro_dsn = None

    conn = None
    candidates = [(str(db_path), {})]
    if ro_dsn:
        candidates.insert(0, (ro_dsn, {"uri": True}))
    for dsn, kwargs in candidates:
        try:
            conn = sqlite3.connect(dsn, **kwargs)
            conn.execute("SELECT 1 FROM kv_store LIMIT 1")
            break
        except sqlite3.Error:
            if conn is not None:
                conn.close()
                conn = None
    if conn is None:
        sys.exit(
            f"!! DB が存在するのに読めませんでした: {db_path}\n"
            f"   既存メッセージの有無を判定できないため中止します"
            f"（AG が起動中でないか確認してください）"
        )
    try:
        for key in ("msg_count", "last_extracted"):
            try:
                row = conn.execute(
                    "SELECT value FROM kv_store WHERE namespace=? AND key=?",
                    (ns, key)
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row:
                value = json.loads(row[0])
                counts[key] = value.get("count", value.get("index", 0))
    finally:
        conn.close()
    return counts


# ---------------------------------------------------------------------------
# 台本
# ---------------------------------------------------------------------------


def load_script(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        sys.exit(f"!! 台本が配列ではないか空です: {path}")
    for i, msg in enumerate(data):
        if not isinstance(msg, dict):
            sys.exit(f"!! 台本 {i} 番目が dict ではありません")
        role = msg.get("role")
        content = msg.get("content")
        expected = "user" if i % 2 == 0 else "assistant"
        if role != expected:
            sys.exit(
                f"!! 台本 {i} 番目の role が交互になっていません "
                f"（expected={expected}, got={role}）"
            )
        if not isinstance(content, str) or not content.strip():
            sys.exit(f"!! 台本 {i} 番目の content が空です")
    return data


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="長期記憶抽出の発火直前まで会話履歴を種投入する（実機テスト用）"
    )
    parser.add_argument("--character", help="キャラの uuid（省略時は config が1体ならそれ）")
    parser.add_argument("--before", type=int, default=4,
                        help="閾値の何件手前で止めるか（既定 4＝2往復手前）")
    parser.add_argument("--script", help="台本 JSON のパス（省略時は言語既定を使う）")
    parser.add_argument("--dry-run", action="store_true",
                        help="件数と最終状態を表示するだけ（DB を作らない・書かない）")
    parser.add_argument("--append-existing", action="store_true",
                        help="既に msg_count>0 の DB にも追記する（既定は中止）")
    parser.add_argument("--assume-stopped", action="store_true",
                        help="AG の起動確認が機械的にできない環境で、"
                             "終了済みだと確認したうえで続行する")
    parser.add_argument("--list", action="store_true", help="キャラ一覧を出して終了")
    args = parser.parse_args()

    from backend.shared.constants import (
        EXTRACTION_THRESHOLD_API,
        EXTRACTION_THRESHOLD_OLLAMA,
        RELATIONSHIP_INITIAL_THRESHOLD,
        RELATIONSHIP_DIR,
    )

    configs = list_characters()
    if args.list:
        for cfg in configs:
            print(f"{cfg.get('character_id')}  name={cfg.get('name')!r} "
                  f"provider={cfg.get('model_provider')} model={cfg.get('model_name')} "
                  f"lang={cfg.get('faster_whisper_config', {}).get('language')}")
        return

    character_id = args.character
    if not character_id:
        if len(configs) == 1:
            character_id = configs[0].get("character_id")
        else:
            sys.exit(
                f"!! キャラが {len(configs)} 体あります。--character <uuid> を指定してください"
                f"（一覧は --list）"
            )

    config = load_config(character_id)
    provider = config.get("model_provider", "ollama")
    language = config.get("faster_whisper_config", {}).get("language", "ja")
    threshold = (EXTRACTION_THRESHOLD_OLLAMA if provider == "ollama"
                 else EXTRACTION_THRESHOLD_API)
    db_path = resolve_db_path(config, character_id)

    script_path = Path(args.script) if args.script else SCRIPTS_DIR / f"{language}_default.json"
    if not script_path.exists():
        sys.exit(f"!! 台本が見つかりません: {script_path}")

    print("=" * 70)
    print(f"キャラ      : {config.get('name')!r}  ({character_id})")
    print(f"provider    : {provider}  model={config.get('model_name')}")
    print(f"言語        : {language}")
    print(f"DB          : {db_path}")
    print(f"閾値        : {threshold} 件（{'ollama' if provider == 'ollama' else 'API'}）")
    print(f"台本        : {script_path}")
    print("=" * 70)

    meta = read_meta_readonly(db_path, character_id)
    print(f"投入前 msg_count      : {meta['msg_count']}")
    print(f"投入前 last_extracted : {meta['last_extracted']}")

    if meta["msg_count"] > 0 and not args.append_existing:
        print()
        print("!! この DB には既にメッセージがあります（上書きしません）。")
        print("   別のキャラを使うか、意図的に追記する場合は --append-existing を付けてください。")
        sys.exit(3)

    if args.before < 0:
        sys.exit("!! --before は 0 以上にしてください")
    if args.before % 2 != 0:
        print(f"[warn] --before={args.before} は奇数です。"
              f"最後の種メッセージが user 側になり、会話再開時の順序が不自然になります。")

    needed = threshold - args.before - meta["msg_count"] + meta["last_extracted"]
    if needed <= 0:
        sys.exit(f"!! 投入不要（必要件数 {needed}）。--before を見直してください。")

    script = load_script(script_path)
    if len(script) < needed:
        sys.exit(
            f"!! 台本の件数が足りません: 必要 {needed} 件 / 台本 {len(script)} 件。\n"
            f"   台本を巡回して埋めることはしません。台本を作り直してください。"
        )

    to_write = script[:needed]
    print(f"投入予定件数          : {needed} 件（台本の先頭 {needed}/{len(script)} 件）")
    print(f"投入後の見込み msg_count : {meta['msg_count'] + needed}")
    print(f"投入後の未処理件数       : "
          f"{meta['msg_count'] + needed - meta['last_extracted']} / 閾値 {threshold}")

    rel_path = RELATIONSHIP_DIR / f"{character_id}.txt"
    print(f"relationship ファイル    : "
          f"{rel_path}  ({'あり' if rel_path.exists() else 'なし＝初期テンプレ扱い'})")

    unprocessed_after = meta["msg_count"] + needed - meta["last_extracted"]
    print()
    print("-- 投入後に実機で何が起きるかの予測 --")
    turn = 0
    u = unprocessed_after
    while u < threshold and turn < 10:
        turn += 1
        u += 2
        if u >= threshold:
            print(f"  会話 {turn} 往復目 → 未処理 {u} 件 ≥ {threshold} "
                  f"＝ 長期記憶の抽出が発火（+ relationship 同時更新）")
        elif u >= RELATIONSHIP_INITIAL_THRESHOLD:
            print(f"  会話 {turn} 往復目 → 未処理 {u} 件 ≥ "
                  f"{RELATIONSHIP_INITIAL_THRESHOLD} ＝ relationship の早期更新が発火"
                  f"（relationship が初期テンプレのときのみ・1回きり）")
        else:
            print(f"  会話 {turn} 往復目 → 未処理 {u} 件（何も発火しない）")
    print("  ※ ツール（カメラ・画像生成・Note 等）が動くと1往復で3件以上増えるため、"
          "発火が1往復早まることがあります。")

    if args.dry_run:
        print()
        print("[dry-run] DB には一切書き込みませんでした。")
        print(f"[dry-run] 先頭2件: {to_write[0]['content'][:40]}... / "
              f"{to_write[1]['content'][:40]}...")
        print(f"[dry-run] 末尾1件({to_write[-1]['role']}): {to_write[-1]['content'][:60]}...")
        return

    assert_ag_stopped(assume_stopped=args.assume_stopped)

    from backend.memory.memory_manager import MemoryManager

    # 種投入では埋め込みを作らない（抽出は埋め込み無しでも正常動作する）
    MemoryManager.DISABLE_EMBEDDINGS = True

    db_path.parent.mkdir(parents=True, exist_ok=True)
    mm = MemoryManager(str(db_path), character_id)

    print()
    print(f"投入中 ... ({needed} 件)")
    for i, msg in enumerate(to_write, start=1):
        result = mm.add_message(msg["role"], msg["content"])
        if not result.get("success"):
            sys.exit(f"!! {i} 件目の add_message が失敗しました: {result}")
        if i % 10 == 0 or i == needed:
            print(f"   {i}/{needed} 件 (unprocessed={result.get('unprocessed_count')}, "
                  f"needs_extraction={result.get('needs_extraction')})")

    try:
        mm.store.commit()
    except Exception as e:
        print(f"[warn] commit（WAL チェックポイント）に失敗: {e}")

    after = read_meta_readonly(db_path, character_id)
    remaining = threshold - (after["msg_count"] - after["last_extracted"])
    print()
    print("=" * 70)
    print(f"現在 {after['msg_count'] - after['last_extracted']} 件"
          f"（msg_count={after['msg_count']} / last_extracted={after['last_extracted']}）")
    print(f"閾値 {threshold} ／ あと {remaining} 件で発火"
          f"＝ 会話 {(remaining + 1) // 2} 往復で発火")
    print("=" * 70)
    print("AG を起動して、History ページのカウントダウンが"
          f"「あと {remaining} メッセージ」になっていることを確認してください。")


if __name__ == "__main__":
    main()
