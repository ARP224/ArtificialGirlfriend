"""
Centralized constants for the backend module.
This file contains all shared constants used across backend components.
"""

from pathlib import Path
from typing import Dict, Any
import os
import json
import logging

# Base directory (project root, anchored to this file instead of the launch
# CWD so that persisted repo-relative paths resolve no matter where the app
# is started from)
BASE_DIR = Path(__file__).resolve().parents[2]

# Directory paths (as Path objects for better cross-platform compatibility).
# Per-user runtime data lives under character_data/, log output under logs/,
# user-supplied SBV2 voice models under sbv2_models/ and app images
# (logo/favicon) under app_images/. sbv2_models/ is user drop-in territory
# only — installer-fetched assets live at the repo root (kokoro/, bert/) so a
# user folder named "Kokoro"/"bert" can never merge into them (稜サブOSテスト
# 2026-08-01: case-insensitive FS merges folders at copy time, before any code
# runs). 旧名 tts_models は 2026-08-02 に改名 (assets/ 解体は 2026-07-18).
DATA_DIR = BASE_DIR / "character_data"
CHARACTER_CONFIGS_DIR = DATA_DIR / "character_configs"
MEMORY_DIR = DATA_DIR / "memory"
# 会話添付(ユーザー添付の画像/文書)の永続置き場。旧来は memory/<uuid>/ で
# .db と同居していたが 2026-08-20 に分離(稜裁定)。書き手は
# conversation_manager の _copy_*_to_permanent_storage・削除は
# character_manager._remove_memory_artifacts / remove_attachment_files。
# 需要時に mkdir する(YOUTUBE_DIR と同様、空フォルダを作らない)。
ATTACHMENTS_DIR = DATA_DIR / "attachments"
CHARACTER_ICONS_DIR = DATA_DIR / "character_icons"
TTS_MODELS_DIR = BASE_DIR / "sbv2_models"
LOGS_DIR = BASE_DIR / "logs"
NOTE_DIR = DATA_DIR / "notes"
RELATIONSHIP_DIR = DATA_DIR / "relationship"
GENERATED_IMAGES_DIR = DATA_DIR / "generated_images"

# Per-user settings files (under character_data/ since 2026-07-05; both are
# created on demand, so a fresh clone needs no migration)
API_SETTINGS_FILE = DATA_DIR / "api_settings.json"
TUNING_DEFAULTS_FILE = DATA_DIR / "tuning_defaults.json"

# ELYTH directory paths — character_data/elyth/ 配下に集約(2026-08-20 稜裁定・
# youtube/ と同型)。旧配置は character_data/ 直下の elyth_notes 等4フォルダ
# (移設は使い捨てスクリプトで実施済み・新規cloneは最初からこの形)。
ELYTH_DIR = DATA_DIR / "elyth"
ELYTH_NOTE_DIR = ELYTH_DIR / "notes"
# セッションログ=直近5件のFIFO短期記憶で、うち3件が次セッションのプロンプトに
# 再生される(elyth_memory.load_session_logs → elyth_session_manager)。つまり
# 運用ログではなくキャラの記憶データなので character_data/ 側が正しい
# (通常会話の短期記憶も character_data/memory/<uuid>.db に入る)。
# 2026-08-18 に logs/elyth_sessions/ から移設。旧名 ELYTH_SESSION_LOG_DIR の
# "LOG" が「ログだから消さなくてよい」の誤読を招き、キャラ削除時の
# remove_*(character_id) 漏れ(=孤児化)の原因になったため同時に改名した。
ELYTH_SESSION_DIR = ELYTH_DIR / "sessions"
ELYTH_RELATIONSHIP_DIR = ELYTH_DIR / "relationships"
ELYTH_THREAD_STATE_DIR = ELYTH_DIR / "thread_state"

# YouTube comment auto-reply: credentials + persistence live under
# character_data/youtube/ (whole character_data/ is gitignored, so
# client_secret.json / token.json stay out of the repo). Created on demand
# by backend/youtube — not in ensure_directories_exist, so installs that
# never enable the feature get no empty folder.
YOUTUBE_DIR = DATA_DIR / "youtube"

# ELYTH session reply/post balancing (see docs: reply-overweight mitigation)
# Turn index (0-based) from which the session enters "wind-down" mode: no new
# replies / feed fetches, only create_post / mark_notifications_read / like /
# get_aituber / notes. 5 == the 6th turn onward.
ELYTH_POST_ONLY_FROM_TURN = 5
# Hard cap on create_reply calls per session. Excess calls are rejected in-code
# (no API hit) with a message nudging the model to post or finish.
ELYTH_MAX_REPLIES_PER_SESSION = 5
# Per-thread cumulative reply cap. Once we have replied into a thread in this
# many distinct sessions, further notifications from that thread are suppressed
# (and auto-marked-read). Threads where the counterpart is one of our own
# characters are exempt (unlimited).
ELYTH_THREAD_REPLY_CAP = 3

def resolve_data_path(path_str: str) -> str:
    """Resolve a persisted (possibly repo-relative) path to an absolute one.

    Persisted paths are stored repo-root-relative so the repository stays
    portable (GitHub clones, moved installs). Absolute paths — legacy configs
    or user-specified external locations — pass through unchanged, as do
    empty values.
    """
    if not path_str:
        return path_str
    p = Path(path_str)
    if p.is_absolute():
        return str(p)
    return str(BASE_DIR / p)


def to_repo_relative(path_str: str) -> str:
    """Return the repo-root-relative POSIX form for paths inside the repo.

    Paths outside the repo (and empty values) are returned unchanged — only
    in-repo paths are made portable.
    """
    if not path_str:
        return path_str
    p = Path(path_str)
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        return p.resolve().relative_to(BASE_DIR).as_posix()
    except (ValueError, OSError):
        return path_str


# Legacy string paths (for backward compatibility)
CHARACTER_CONFIGS_DIR_STR = str(CHARACTER_CONFIGS_DIR)
MEMORY_DIR_STR = str(MEMORY_DIR)
CHARACTER_ICONS_DIR_STR = str(CHARACTER_ICONS_DIR)
TTS_MODELS_DIR_STR = str(TTS_MODELS_DIR)

# File extensions
VALID_CONFIG_EXTENSIONS = (".json", ".yaml", ".yml")
VALID_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
VALID_ATTACHMENT_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DEFAULT_CONFIG_EXTENSION = ".json"

# Image attachment constants
MAX_IMAGES_PER_MESSAGE = 5        # Maximum images user can attach to a single message
MAX_IMAGES_IN_PROMPT = 10         # Maximum recent images included in LLM prompt

# Live Camera constants (server-orchestrated client-camera capture; the
# frame is prompt-only for the current turn — never persisted to history)
AMBIENT_CAPTURE_TIMEOUT = 1.5           # sec: max extra wait at generation entry
AMBIENT_FRAME_MAX_EDGE = 1280           # px: client downscales the frame before sending
AMBIENT_JPEG_QUALITY = 0.8              # JPEG quality for the downscaled frame
AMBIENT_TIMEOUT_DEGRADE_THRESHOLD = 2   # consecutive timeouts -> stop waiting until re-announce
AMBIENT_FRAME_MAX_AGE = 30.0            # sec: a fired-but-unconsumed request/frame older than
                                        # this is discarded (fire→reject strand must not leak
                                        # stale scenery into a later turn)
AMBIENT_TOOL_CAPTURE_TIMEOUT = 3.0      # sec: capture_camera tool wait (AI-initiated; runs
                                        # inside the tool loop, so a longer wait than the
                                        # send-path barrier is acceptable)

# Document attachment constants
VALID_ATTACHMENT_DOCUMENT_EXTENSIONS = (
    # Text/config
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".log", ".ini", ".toml",
    # Code
    ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp",
    ".h", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".bat", ".ps1",
    # PDF/DOCX は動作検証未了のため公開範囲から除外(稜裁定 2026-08-15)
)
MAX_DOCUMENTS_PER_MESSAGE = 10        # Maximum documents user can attach to a single message
MAX_DOCUMENTS_IN_PROMPT = 10          # Maximum documents included in LLM prompt (across history)
MAX_DOCUMENT_CHARS_IN_PROMPT = 30000  # Maximum total document characters in LLM prompt

# Permanent storage limits (per character)
MAX_PERMANENT_IMAGES = 100            # Maximum image files kept in memory/{char}/images/
MAX_PERMANENT_DOCUMENTS = 100         # Maximum document files kept in memory/{char}/documents/

# Memory extraction constants
EXTRACTION_THRESHOLD_OLLAMA = 50   # Messages before triggering extraction (Ollama)
EXTRACTION_THRESHOLD_API = 100     # Messages before triggering extraction (API providers)
MAX_MEMORY_ENTRIES = 500           # Maximum memory entries per character
MIN_MEMORY_RELEVANCE = 0.3        # Minimum cosine similarity for prompt inclusion
                                  # (nomic-embed-text hand calibration; used only as
                                  # the last-resort fallback — see get_memory_relevance_threshold)

# Cosine scales differ per embedding model (ST6 §7-4): 0.3 silently disabled
# long-term memory on text-embedding-3-large (relevant pairs score ~0.19-0.55
# there vs ~0.65-0.83 on nomic). Shipped seeds for known models, used until the
# settings-UI calibration (backend/memory/embedding_calibration.py) persists a
# measured value for the selected model.
MIN_MEMORY_RELEVANCE_SEEDS = {
    # Keep the shipped hand-calibrated value (behavior-preserving for existing
    # installs); re-saving the model in Settings auto-calibrates (~0.62).
    "ollama::nomic-embed-text": 0.3,
    # Real calibration 2026-07-04 (rel 0.186-0.547 / irr 0.085-0.283).
    "openai::text-embedding-3-large": 0.1321,
}
MEMORY_TOKEN_BUDGET_OLLAMA = 1500  # Token budget for memories in Ollama prompts
MEMORY_TOKEN_BUDGET_API = 3000     # Token budget for memories in API prompts
MEMORY_SEARCH_TOP_K = 50          # Candidates for semantic search
MEMORY_RELATED_EXISTING_TOP_K = 20 # Existing memories to pass to extraction LLM
MAX_MESSAGE_RETENTION = 5000       # Maximum messages to retain per character

# Relationship section constants (all providers; C8 lifted the Ollama exclusion)
RELATIONSHIP_INITIAL_THRESHOLD = 30   # 初期テンプレート時の更新トリガー（未処理メッセージ件数=user/assistant別カウント・約15往復・本番値）
MEMORY_CATEGORIES = [              # Valid memory categories
    "user_fact", "user_preference", "character_relationship",
    "shared_experience", "user_opinion", "elyth", "youtube"
]

# LLM related constants
DEFAULT_EMBEDDING_SIZE = 768   # nomic-embed-text embedding dimension
EMBEDDING_RETRY_DELAY = 0.5    # Delay between embedding retries
EMBEDDING_RETRY_LIMIT = 3      # Number of retries for embedding generation
EXTRACTION_MAX_FAILURES = 3    # Max consecutive failures before circuit breaker
EXTRACTION_FAILURE_COOLDOWN = 3600  # Cooldown period in seconds (1 hour)

# Queue manager constants
MAX_QUEUE_SIZE = 1000         # Maximum number of tasks in queue
DEFAULT_TASK_TIMEOUT = 300    # Default task execution timeout (5 minutes)
# 背景の記憶タスク(抽出/relationship)がキュー完了を待つ上限。LLM 1回の HTTP
# タイムアウト(180s)×並びうる回数(Ollama 2分割抽出+後ろに並ぶ relationship)
# を包む値。待ち手は 0.5 秒刻みで待つので完了すれば即戻る=上限でしかない。
# 既定 90 秒だと遅いモデルで先に切れ「ブロック早期解除+queue の CANCELLED
# 例外」を起こした(2026-08-16 Windows 実機・稜裁定で延長)。
MEMORY_TASK_QUEUE_TIMEOUT = 600
LOCK_TIMEOUT = 30.0           # Timeout for acquiring locks (seconds)

# Ollama integration constants
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT = os.environ.get("OLLAMA_PORT", "11434")
OLLAMA_CONNECTION_TIMEOUT = int(os.environ.get("OLLAMA_CONNECTION_TIMEOUT", "5"))
OLLAMA_GENERATION_TIMEOUT = 180  # LLM生成タイムアウト（3分）
OLLAMA_KEEP_ALIVE = "30m"  # モデルをVRAMに保持する時間（30分）

# Validate port is a valid number
try:
    port_num = int(OLLAMA_PORT)
    if port_num < 1 or port_num > 65535:
        OLLAMA_PORT = "11434"
except ValueError:
    OLLAMA_PORT = "11434"

OLLAMA_SERVER_URL = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"

# LLM call logging configuration
# LOG_ALL_LLM_CALLS: true/false - すべてのLLM呼び出しをログに保存するか
# デフォルトはfalse（エラー時のみ保存）、デバッグ時にtrueに設定
LOG_ALL_LLM_CALLS = os.environ.get("LOG_ALL_LLM_CALLS", "false").lower() in ("true", "1", "yes")

# Retry configuration for direct API
DIRECT_API_CONNECTION_RETRY_COUNT = 1  # 接続エラー時のリトライ回数
DIRECT_API_RETRY_DELAY = 1.0           # リトライ間隔（秒）

# LLM Context Window - 用途別の設定
# 通常会話用の設定(Ollama)。トークン管理はAPI方式と統一
# (上限+削減優先順位=API_CONVERSATION_CONFIG.trim_priority を共用・
# 2026-08-11 稜裁定)。旧固定セクション配分(token_allocation)は撤去済み。
# num_ctx はユーザー設定化済み: 真実源は api_settings.get_ollama_num_ctx
# (既定32000)+モデル別クランプ ollama_capabilities.get_effective_num_ctx。
CONVERSATION_CONFIG = {
    # num_predict は TUNING_DEFAULTS で管理
    "disable_thinking": True  # Thinkingモードを無効化（生成速度優先）
}

# API利用時の会話設定（128Kコンテキスト、簡易トークン管理）
API_CONVERSATION_CONFIG = {
    "max_context": 128000,  # 上限のみ管理（セクション別予算は持たない）
    "trim_priority": [       # 超過時の削減優先順位（先に削る順）
        "long_term_memory",
        "location",
        "pc_status",
        "talk_theme",
        "notes",
        # system_prompt と recent_messages は最後まで残す
    ],
    "disable_thinking": True
}

# FIXED_CONTEXT_LENGTH は削除済み - 用途別設定を使用してください
# 通常会話(Ollama): CONVERSATION_CONFIG
# 通常会話(API): API_CONVERSATION_CONFIG
# メモリ抽出: CONVERSATION_CONFIG + EXTRACTION_TUNING

# ============================================================================
# Command Execution - AIキャラクターによるコマンド実行機能
# ============================================================================

MAX_COMMANDS_PER_TURN = 10       # 1ターンあたりの最大コマンド実行回数
COMMAND_RATE_WINDOW = 10         # レート制限監視ターン数
COMMAND_RATE_MAX = 10            # 監視ウィンドウ内の最大コマンド実行回数
# グレーリスト承認待ちの上限秒。タスク側タイムアウト(300s)より手前で必ず戻し、
# 超過時は拒否扱いにする(無期限waitだとWS通知失敗時にLLMキューが恒久停止する)
COMMAND_APPROVAL_TIMEOUT = 240.0

# ============================================================================
# Image Generation - AIキャラクターによる画像生成機能
# ============================================================================

IMAGE_GEN_RATE_WINDOW = 10       # レート制限監視ターン数
IMAGE_GEN_RATE_MAX = 3           # 監視ウィンドウ内の最大画像生成回数
IMAGE_GEN_PER_TURN_MAX = 1      # 1ターンあたりの最大画像生成回数

# ============================================================================
# Camera Capture - AIキャラクターによるカメラ撮影機能
# ============================================================================

CAMERA_RATE_WINDOW = 10          # レート制限監視ターン数
CAMERA_RATE_MAX = 3              # 監視ウィンドウ内の最大撮影回数
CAMERA_PER_TURN_MAX = 1          # 1ターンあたりの最大撮影回数

# ============================================================================
# Deep Search - AIキャラクターによるWeb検索・ページ読み取り機能
# ============================================================================

SEARCH_WEB_RATE_WINDOW = 10      # レート制限監視ターン数
SEARCH_WEB_RATE_MAX = 5          # 監視ウィンドウ内の最大検索回数
SEARCH_WEB_PER_TURN_MAX = 2      # 1ターンあたりの最大検索回数

READ_WEBPAGE_RATE_WINDOW = 10    # レート制限監視ターン数
READ_WEBPAGE_RATE_MAX = 5        # 監視ウィンドウ内の最大読み取り回数
READ_WEBPAGE_PER_TURN_MAX = 3    # 1ターンあたりの最大読み取り回数

# ============================================================================
# Map Search - GoogleMap検索（search_places / get_place_details / get_directions）
# ============================================================================

SEARCH_PLACES_RATE_WINDOW = 10          # レート制限監視ターン数
SEARCH_PLACES_RATE_MAX = 5              # 監視ウィンドウ内の最大検索回数
SEARCH_PLACES_PER_TURN_MAX = 1          # 1ターンあたりの最大検索回数

GET_PLACE_DETAILS_RATE_WINDOW = 10      # レート制限監視ターン数
GET_PLACE_DETAILS_RATE_MAX = 10         # 監視ウィンドウ内の最大詳細取得回数
GET_PLACE_DETAILS_PER_TURN_MAX = 3      # 1ターンあたりの最大詳細取得回数

GET_DIRECTIONS_RATE_WINDOW = 10         # レート制限監視ターン数
GET_DIRECTIONS_RATE_MAX = 5             # 監視ウィンドウ内の最大経路案内回数
GET_DIRECTIONS_PER_TURN_MAX = 1         # 1ターンあたりの最大経路案内回数

MAP_SEARCH_RESULT_MAX_LENGTH = 5000     # Map検索結果の最大文字数

# ============================================================================
# Tuning Parameters - キャラクターごとのLLMパラメータ調整
# ============================================================================

# デフォルト値の保存ファイル＝TUNING_DEFAULTS_FILE（ファイル冒頭のパス集約部で定義）

# ハードコードデフォルト（フォールバック＆初期値）
TUNING_HARDCODED_DEFAULTS = {
    "temperature": 0.7,
    "top_k": 40,
    "top_p": 0.9,
    "min_p": 0.05,
    "repeat_last_n": 500,
    # 1.0=無効が既定。強いペナルティ×広い窓は「未出トークンへの逃避」
    # （絵文字連発）を誘発する（gemma4:12b 実測 2026-08-14）。Gemma系は特に敏感。
    "repeat_penalty": 1.0,
    "presence_penalty": 0.4,
    "frequency_penalty": 0.5,
    "num_predict": 800
}


def get_tuning_defaults() -> Dict[str, Any]:
    """
    Get current tuning defaults.

    Reads from tuning_defaults.json if it exists,
    otherwise returns hardcoded defaults.

    Returns:
        Dict with tuning parameters
    """
    if TUNING_DEFAULTS_FILE.exists():
        try:
            with open(TUNING_DEFAULTS_FILE, 'r', encoding='utf-8') as f:
                saved_defaults = json.load(f)
                # Merge with hardcoded defaults to ensure all keys exist
                result = TUNING_HARDCODED_DEFAULTS.copy()
                result.update(saved_defaults)
                return result
        except Exception as e:
            logging.warning(f"Failed to load tuning defaults from file: {e}")

    return TUNING_HARDCODED_DEFAULTS.copy()


def save_tuning_defaults(tuning: Dict[str, Any]) -> Dict[str, Any]:
    """
    Save tuning defaults to file.

    Args:
        tuning: Dictionary of tuning parameters

    Returns:
        Dict with success status
    """
    # Validate parameters
    errors = []
    for param, value in tuning.items():
        if param not in TUNING_RANGES:
            continue

        range_def = TUNING_RANGES.get(param)
        if range_def is None:  # range=None は検証免除（現在該当なし・防御）
            continue

        min_val, max_val = range_def
        try:
            numeric_value = float(value)
            if not (min_val <= numeric_value <= max_val):
                errors.append(f"{param}: {value} is out of range ({min_val}-{max_val})")
        except (TypeError, ValueError):
            errors.append(f"{param}: invalid numeric value")

    if errors:
        return {
            "success": False,
            "error": "; ".join(errors)
        }

    try:
        with open(TUNING_DEFAULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(tuning, f, indent=2, ensure_ascii=False)

        logging.info(f"Saved tuning defaults to {TUNING_DEFAULTS_FILE}")
        return {
            "success": True,
            "message": "Default parameters saved. New characters will use these values."
        }
    except Exception as e:
        logging.error(f"Failed to save tuning defaults: {e}")
        return {
            "success": False,
            "error": str(e)
        }


# 後方互換性のためのエイリアス（非推奨：get_tuning_defaults()を使用してください）
TUNING_DEFAULTS = TUNING_HARDCODED_DEFAULTS

# パラメータの許容範囲（バリデーション用）
# None = バリデーションなし
TUNING_RANGES = {
    "temperature": (0.0, 2.0),
    "top_k": (0, 100),
    "top_p": (0.1, 1.0),
    "min_p": (0.0, 1.0),
    "repeat_last_n": (0, 10000),
    "repeat_penalty": (0.8, 2.0),
    "presence_penalty": (0.0, 2.0),
    "frequency_penalty": (0.0, 2.0),
    "num_predict": (10, 1000)
}

# メモリ抽出用固定パラメータ（保守的な設定）
EXTRACTION_TUNING = {
    "temperature": 0.3,
    "top_k": 20,
    "top_p": 0.8,
    "min_p": 0.0,
    "repeat_last_n": 64,
    "repeat_penalty": 1.0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "num_predict": 3000
}

# Timezone configuration
USE_UTC_TIMESTAMPS = True  # Use UTC timestamps for consistency across timezones


def ensure_directories_exist():
    """
    Ensure all required directories exist.
    This function should be called during backend initialization.
    """
    directories = [
        CHARACTER_CONFIGS_DIR,
        MEMORY_DIR,
        CHARACTER_ICONS_DIR,
        TTS_MODELS_DIR,
        LOGS_DIR,
        ELYTH_NOTE_DIR,
        ELYTH_SESSION_DIR,
        ELYTH_RELATIONSHIP_DIR,
    ]
    
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.error(f"Failed to create directory {directory}: {e}")
            raise RuntimeError(f"Failed to create required directory: {directory}") from e