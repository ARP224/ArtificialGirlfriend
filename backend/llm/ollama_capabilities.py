"""
backend/llm/ollama_capabilities.py

Ollamaモデルの capability（tools/vision）とネイティブ文脈長の照会＋永続キャッシュ。

機能可用性（feature_availability）の2軸判定・vision非対応時の画像破棄・
num_ctx のモデル別クランプが読む真実源。判定は /api/show の
``capabilities`` フィールド直照会（名前ヒューリスティックなし＝稜裁定
2026-08-11）。capability はモデル blob の静的属性なので「モデル名＋digest」
をキーに character_data/ へ永続キャッシュし、同名タグの再 pull は
/api/tags の digest 差分で検知して再照会する。

呼び出しの3入口:
  - :func:`get_caps` — 遅延ゲッター（真実源）。キャッシュ命中ならHTTPなし。
    ミス時のみ /api/show を1回（短timeout）。可用性判定の同期経路から
    呼ばれるため、接続失敗は負キャッシュ（TTL）して連続照会でUIを
    待たせない。
  - :func:`prefetch` — キャラ編集のモデル一覧構築などリスト時の先読み。
    /api/tags の digest と突き合わせ、新規・変更モデルだけ /api/show する
    （全量渡しの時は削除済みモデルの掃除も行う）。
  - :func:`reset_state` — テスト用（キャッシュ状態の初期化）。

「capabilities フィールド自体が無い」= 旧Ollamaサーバーは known=False の
まま返す（fail-closed は消費者側の責務。理由文の出し分けも消費者側）。

思考無効化プローブ（2026-08-16 稜裁定「think:false を無視するモデルは非対応」）:
  - :func:`probe_think_disable` — ``thinking`` capability を持つモデルに
    ``think:false`` で数トークンだけ生成させ、応答に thinking が乗れば
    「無効化できない」と判定する。/api/show では判別不能（qwen3-vl と
    gemma4 は capabilities も template も同形で片方だけ無視する＝実測）。
    結果はエントリの ``think_probe`` に Ollama バージョン付きで保存し、
    **完全一致名でのみ**照会する（Thinking 版と instruct 版はベース名が同じ
    ため、ベース名フォールバックに乗せると誤適用する）。
  - :func:`ensure_model_supported` — キャラ作成/編集/選択の入口が呼ぶ。
    非対応なら AGError(model_thinking_unsupported) を raise（UI が翻訳）。
    不達・不明は通す（fail-open。停止中は既存の LLM 未接続表示の領域）。
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from backend.shared.constants import DATA_DIR, OLLAMA_SERVER_URL
from backend.shared.errors import AGError

logger = logging.getLogger(__name__)

CAPABILITIES_CACHE_FILE = Path(DATA_DIR) / "ollama_capabilities.json"

# 可用性判定の同期経路から呼ばれるため短く保つ（check_ollama_running と同水準）
_HTTP_TIMEOUT = 2.0

# 接続失敗の負キャッシュTTL（秒）。Ollama停止中に判定のたび再照会して
# UIをtimeout分待たせない。prefetch はユーザー操作起点なのでTTLを無視して
# 常に試行し、成功したら解除する。
_FAILURE_TTL = 30.0

# Guards the in-memory cache dict and the cache-file write (settings_store と
# 同じ理由: Windows では読み手が開いたままだと書き手の os.replace が
# PermissionError で落ちる)。
_lock = threading.RLock()
_cache: Optional[Dict[str, Dict[str, Any]]] = None  # model name -> entry
_last_failure_at: float = 0.0  # time.monotonic(); server-level negative cache


def reset_state() -> None:
    """テスト用: メモリ上のキャッシュと負キャッシュを初期化する。"""
    global _cache, _last_failure_at
    with _lock:
        _cache = None
        _last_failure_at = 0.0


def notify_server_reachable() -> None:
    """外部プローブがOllama生存を確認したとき負キャッシュを解除する。

    ステータスインジケーターのdown→up遷移検知(ui/status_checker)が呼ぶ。
    これが無いと、Ollamaを後から起動したユーザーの可用性再判定がTTL分
    (最大30秒)遅れる。
    """
    _clear_failure()


# ---------------------------------------------------------------------------
# Cache file I/O
# ---------------------------------------------------------------------------


def _load_cache() -> Dict[str, Dict[str, Any]]:
    """キャッシュファイルを読み込む（初回のみ）。破損・不在は空扱い。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            if CAPABILITIES_CACHE_FILE.exists():
                # utf-8-sig: 手編集のBOMを許容（settings_store と同判断）
                with open(CAPABILITIES_CACHE_FILE, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                models = data.get("models", {})
                _cache = models if isinstance(models, dict) else {}
            else:
                _cache = {}
        except Exception as e:
            logger.warning(f"Failed to load Ollama capabilities cache: {e}")
            _cache = {}
        return _cache


def _save_cache() -> None:
    """Atomic write（tmp + os.replace + PermissionError リトライ）。

    書き込み中クラッシュで半端なJSONを残さない＋Windowsで外部プロセス
    （AVスキャン等）がハンドルを握っていても書き込みを失わない
    （settings_store の正準形）。
    """
    with _lock:
        if _cache is None:
            return
        try:
            CAPABILITIES_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = CAPABILITIES_CACHE_FILE.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "models": _cache}, f,
                          ensure_ascii=False, indent=2)
            for attempt in range(10):
                try:
                    os.replace(str(tmp_path), str(CAPABILITIES_CACHE_FILE))
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05)
        except Exception as e:
            logger.warning(f"Failed to save Ollama capabilities cache: {e}")


# ---------------------------------------------------------------------------
# HTTP fetch helpers
# ---------------------------------------------------------------------------


def _note_failure() -> None:
    global _last_failure_at
    _last_failure_at = time.monotonic()


def _failure_fresh() -> bool:
    return (time.monotonic() - _last_failure_at) < _FAILURE_TTL


def _clear_failure() -> None:
    global _last_failure_at
    _last_failure_at = 0.0


def _fetch_tags() -> Optional[Dict[str, str]]:
    """/api/tags からインストール済みモデルの name→digest を取得。失敗は None。"""
    try:
        response = requests.get(f"{OLLAMA_SERVER_URL}/api/tags",
                                timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        result: Dict[str, str] = {}
        for model in response.json().get("models", []):
            if isinstance(model, dict) and "name" in model:
                result[model["name"]] = str(model.get("digest", ""))
        _clear_failure()
        return result
    except Exception as e:
        logger.debug(f"Capabilities: /api/tags fetch failed: {e}")
        _note_failure()
        return None


def _fetch_show(model_name: str) -> Optional[Dict[str, Any]]:
    """/api/show の生JSONを取得。失敗は None。"""
    try:
        response = requests.post(f"{OLLAMA_SERVER_URL}/api/show",
                                 json={"name": model_name},
                                 timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        _clear_failure()
        return response.json()
    except (requests.ConnectionError, requests.Timeout) as e:
        logger.debug(f"Capabilities: /api/show failed for {model_name}: {e}")
        _note_failure()
        return None
    except Exception as e:
        # 4xx/5xx やパース失敗はサーバー生存とは別問題=負キャッシュしない
        logger.debug(f"Capabilities: /api/show error for {model_name}: {e}")
        return None


def _extract_context_length(show_json: Dict[str, Any]) -> Optional[int]:
    """model_info の「{arch}.context_length」キーからネイティブ文脈長を拾う。"""
    model_info = show_json.get("model_info", {})
    if isinstance(model_info, dict):
        for key, value in model_info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
    return None


def _entry_from_show(digest: Optional[str],
                     show_json: Dict[str, Any]) -> Dict[str, Any]:
    """/api/show 応答からキャッシュエントリを組む。

    capabilities フィールド欠落（旧Ollama）は None のまま保存し、
    「非対応」([]) と区別する。
    """
    capabilities = show_json.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = None
    else:
        capabilities = [str(c) for c in capabilities]
    return {
        "digest": digest,
        "capabilities": capabilities,
        "context_length": _extract_context_length(show_json),
        "checked_at": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_UNKNOWN = {"known": False, "tools": False, "vision": False,
            "completion": False, "embedding": False, "thinking": False,
            "context_length": None}


def _caps_from_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    capabilities = entry.get("capabilities")
    if capabilities is None:
        # 旧Ollama（capabilitiesフィールド無し）= 判定不能
        return {"known": False, "tools": False, "vision": False,
                "completion": False, "embedding": False, "thinking": False,
                "context_length": entry.get("context_length")}
    return {
        "known": True,
        "tools": "tools" in capabilities,
        "vision": "vision" in capabilities,
        # completion無し=embedding専用等の会話不能モデル（会話一覧の除外judge）
        "completion": "completion" in capabilities,
        # embedding=設定>埋め込みモデル一覧の絞り込みjudge
        "embedding": "embedding" in capabilities,
        # thinking=思考無効化プローブの対象judge（無ければ思考しない=対応）
        "thinking": "thinking" in capabilities,
        "context_length": entry.get("context_length"),
    }


def _lookup_entry(model_name: str) -> Optional[Dict[str, Any]]:
    """キャッシュ照会。完全一致→ベース名一意一致（タグ表記ゆれ吸収=
    embedding ガードと同じ流儀）。"""
    cache = _load_cache()
    if model_name in cache:
        return cache[model_name]
    base = model_name.split(":")[0]
    matches = [entry for name, entry in cache.items()
               if name.split(":")[0] == base]
    if len(matches) == 1:
        return matches[0]
    return None


def get_caps(model_name: str) -> Dict[str, Any]:
    """モデルの capability を返す（真実源・遅延ゲッター）。

    Returns:
        {"known": bool, "tools": bool, "vision": bool, "completion": bool,
         "embedding": bool, "thinking": bool, "context_length": Optional[int]}
        known=False は「判定不能」（未照会・旧Ollama・サーバー停止）。
        fail-closed（不能=非対応扱い）にするかは消費者の責務。
    """
    if not model_name or not isinstance(model_name, str):
        return dict(_UNKNOWN)
    model_name = model_name.strip()

    with _lock:
        entry = _lookup_entry(model_name)
        if entry is not None:
            return _caps_from_entry(entry)

        # キャッシュミス: 負キャッシュ中はHTTPせず不明を返す
        if _failure_fresh():
            return dict(_UNKNOWN)

        show_json = _fetch_show(model_name)
        if show_json is None:
            return dict(_UNKNOWN)

        # digest は /api/show では取れない。None で保存し、次の prefetch が
        # /api/tags の実 digest と突き合わせて再照会・補完する（自己修復）。
        entry = _entry_from_show(None, show_json)
        cache = _load_cache()
        cache[model_name] = entry
        _save_cache()
        logger.info(f"Ollama capabilities cached (lazy): {model_name} -> "
                    f"{entry['capabilities']}")
        return _caps_from_entry(entry)


def get_effective_num_ctx(model_name: str) -> int:
    """実効 num_ctx = min(ユーザー設定, モデルのネイティブ文脈長)。

    ネイティブ長を超える指定は rope 外挿で品質が壊れ VRAM も無駄なため
    クランプする（稜承認計画 C4.6）。ネイティブ長不明（未照会・旧Ollama）は
    設定値をそのまま使う。全Ollama経路（会話・抽出・warm・token manager・
    モデル情報表示）がこの1関数を読む＝真実源。
    """
    from backend.shared.api_settings import get_ollama_num_ctx
    setting = get_ollama_num_ctx()
    if not model_name:
        return setting
    native = get_caps(model_name).get("context_length")
    if isinstance(native, int) and native > 0:
        return min(setting, native)
    return setting


# ---------------------------------------------------------------------------
# Thinking-disable probe（思考を無効化できないモデル = 非対応・稜裁定 2026-08-16）
# ---------------------------------------------------------------------------

UNSUPPORTED_MODEL_CODE = "model_thinking_unsupported"  # locales の err.<code>
_THINK_PROBE_KEY = "think_probe"
_THINK_PROBE_NUM_PREDICT = 16
# 短い推論質問: 思考が乗るモデルなら最初の数トークンで thinking に現れる
_THINK_PROBE_PROMPT = "2+2=?"


def _fetch_version() -> Optional[str]:
    """/api/version の version 文字列。失敗は None（負キャッシュ）。"""
    try:
        response = requests.get(f"{OLLAMA_SERVER_URL}/api/version",
                                timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        _clear_failure()
        return str(response.json().get("version") or "")
    except Exception as e:
        logger.debug(f"Capabilities: /api/version fetch failed: {e}")
        _note_failure()
        return None


def _think_probe_verdict(chat_json: Dict[str, Any]) -> bool:
    """/api/chat 応答から「think:false が尊重されたか」を判定（True=対応）。

    thinking フィールド非空（パーサ付きモデル: qwen3-vl / deepseek-r1 /
    gpt-oss で実測）か、content に <think> 直書き（パーサ無しテンプレ。AG は
    剥がすが出力枠は食われる＝同じ失敗）なら「無効化できない」。
    """
    message = chat_json.get("message") or {}
    if not isinstance(message, dict):
        return True
    thinking = message.get("thinking") or ""
    content = message.get("content") or ""
    if isinstance(thinking, str) and thinking.strip():
        return False
    if isinstance(content, str) and "<think>" in content:
        return False
    return True


def _run_think_probe(model_name: str) -> Optional[bool]:
    """think:false で数トークン生成して判定。None=判定不能（不達・HTTPエラー）。

    keep_alive は会話と同じ値=ロードがそのままウォームアップになる。
    タイムアウトは生成と同じ（初回はモデルロード込み）。
    """
    from backend.shared.constants import OLLAMA_GENERATION_TIMEOUT, OLLAMA_KEEP_ALIVE
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _THINK_PROBE_PROMPT}],
        "stream": False,
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"num_predict": _THINK_PROBE_NUM_PREDICT, "temperature": 0},
    }
    try:
        response = requests.post(f"{OLLAMA_SERVER_URL}/api/chat", json=payload,
                                 timeout=OLLAMA_GENERATION_TIMEOUT)
        response.raise_for_status()
        return _think_probe_verdict(response.json())
    except (requests.ConnectionError, requests.Timeout) as e:
        logger.debug(f"Think probe: /api/chat failed for {model_name}: {e}")
        _note_failure()
        return None
    except Exception as e:
        # 404（モデル無し）等はサーバー生存とは別問題=負キャッシュしない
        logger.debug(f"Think probe: /api/chat error for {model_name}: {e}")
        return None


def _unload_model(model_name: str) -> None:
    """非対応判定したモデルを即アンロード（keep_alive 0）。VRAM に30分居座らせない。"""
    try:
        requests.post(f"{OLLAMA_SERVER_URL}/api/generate",
                      json={"model": model_name, "keep_alive": 0},
                      timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.debug(f"Think probe: unload of {model_name} skipped: {e}")


def probe_think_disable(model_name: str) -> Optional[bool]:
    """モデルが think:false を尊重するか（True=対応・False=非対応・None=不明）。

    - thinking capability の無いモデルは思考しない=対応（HTTPなし）。
    - capability 不明（未照会・旧Ollama・停止中）は None。
    - 判定済み（同じ Ollama バージョン）ならキャッシュを返す。エントリは
      **完全一致名のみ**（ベース名フォールバック不使用）。再 pull で digest が
      変わると prefetch がエントリを作り直すので判定も自然に消える。
    - HTTP はロック外で行う（最大で生成タイムアウト分。ロック内だと他スレッドの
      get_caps を止める）。
    """
    if not model_name or not isinstance(model_name, str):
        return None
    model_name = model_name.strip()

    caps = get_caps(model_name)
    if not caps.get("known"):
        return None
    if not caps.get("thinking"):
        return True

    version = _fetch_version()
    if version is None:
        return None

    with _lock:
        cache = _load_cache()
        entry = cache.get(model_name)  # 完全一致のみ
        probe = (entry or {}).get(_THINK_PROBE_KEY)
        if (isinstance(probe, dict) and probe.get("ollama_version") == version
                and isinstance(probe.get("disable_ok"), bool)):
            return probe["disable_ok"]

    verdict = _run_think_probe(model_name)  # ロック外
    if verdict is None:
        return None

    with _lock:
        cache = _load_cache()
        entry = cache.get(model_name)
        if entry is None:
            # get_caps がベース名フォールバックで拾った場合は完全一致エントリが
            # 無い。判定は名前に紐づくので完全一致エントリを作ってから保存する
            show_json = _fetch_show(model_name)
            if show_json is not None:
                entry = _entry_from_show(None, show_json)
                cache[model_name] = entry
        if entry is not None:
            entry[_THINK_PROBE_KEY] = {
                "ollama_version": version,
                "disable_ok": verdict,
                "checked_at": datetime.now().isoformat(),
            }
            _save_cache()

    if verdict:
        logger.info(f"Ollama think-disable probe: {model_name} -> ok (ollama {version})")
    else:
        logger.warning(
            f"Ollama think-disable probe: {model_name} ignores think:false "
            f"(thinking cannot be disabled) -> unsupported (ollama {version})")
        _unload_model(model_name)
    return verdict


def check_model_supported(provider: str, model_name: str) -> bool:
    """会話モデルとしてこのアプリで使えるか。Ollama 以外は常に True。

    Ollama は probe_think_disable が False のときだけ False（不明は True=通す）。
    """
    if (provider or "ollama") != "ollama":
        return True
    return probe_think_disable(model_name) is not False


def ensure_model_supported(provider: str, model_name: str) -> None:
    """check_model_supported が False なら AGError(model_thinking_unsupported)。

    キャラ作成/編集(モデル変更時)/選択の入口が呼ぶ。standardize_response が
    error_code に載せ、UI は t("err.model_thinking_unsupported") で翻訳する。
    """
    if not check_model_supported(provider, model_name):
        raise AGError(
            UNSUPPORTED_MODEL_CODE,
            f"Model '{model_name}' cannot disable thinking (Ollama think:false is "
            f"ignored); this app does not support it",
            model=model_name,
        )


def prefetch(model_names: Optional[List[str]] = None) -> None:
    """リスト時の先読み。新規・digest変更モデルだけ /api/show する。

    Args:
        model_names: 対象を絞る場合に指定。None は /api/tags の全モデルを
            対象にし、アンインストール済みモデルのエントリも掃除する。

    ユーザー操作起点（モデル一覧構築）なので負キャッシュTTLは無視して
    常に試行する。Ollama停止中は静かに何もしない。
    """
    tags = _fetch_tags()
    if tags is None:
        return

    with _lock:
        cache = _load_cache()
        targets = tags if model_names is None else {
            name: digest for name, digest in tags.items()
            if name in model_names
        }

        dirty = False
        for name, digest in targets.items():
            entry = cache.get(name)
            if entry is not None and entry.get("digest") == digest:
                continue  # 既知かつ同一 blob → 照会不要
            show_json = _fetch_show(name)
            if show_json is None:
                continue
            cache[name] = _entry_from_show(digest, show_json)
            dirty = True
            logger.info(f"Ollama capabilities cached: {name} -> "
                        f"{cache[name]['capabilities']}")

        # 全量モードではアンインストール済みモデルの残骸を掃除
        if model_names is None:
            removed = [name for name in cache if name not in tags]
            for name in removed:
                del cache[name]
                dirty = True

        if dirty:
            _save_cache()
