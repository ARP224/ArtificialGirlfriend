#!/usr/bin/env python
"""Layer 2 — full-fidelity sanity baseline.

Purpose: detect *probabilistic* degradation of the real
generation path — real LLM (Claude / Ollama) + real STT + real TTS — that the
cheap deterministic Layer 1 smoke (tests/smoke/) cannot see. Run MANUALLY
(needs GPU / models / API keys; a full run makes real, billable API calls),
then compared against a stored reference.

Metrics recorded (-> tests/baseline/runs/<timestamp>.json):
  - completion rate (overall + per category + per provider)
  - error types: timeout / memory / schema / tool_fail / other  (heuristic, human-reviewable)
  - latency p50 / p95 (turn, plus STT / TTS sub-latencies)

Machine gate: ONLY completion rate is a hard gate — if it
drops more than 5 percentage points below tests/baseline/runs/_reference.json the script
exits non-zero. Latency and the appearance of new error types are surfaced but
left to HUMAN judgement (they print a ⚠, they do not fail the run).

Hard rail: the three spine files (ui/app.py, conversation_manager.py,
backend.py) are NOT edited. This script reaches the real ConversationManager
through the same approved seams the test harness uses — state injection + a few
monkeypatches (synchronous enqueue, no-op in-turn TTS play point, no-op WS) —
and lets the REAL `_ensure_llm_in_cache` build the REAL provider client from the
REAL character_data/api_settings.json. No production wiring is re-implemented.

Usage (run from repo root, with the project venv):
  venv/Scripts/python.exe tests/scripts/baseline.py --dry-run        # cheap wiring check, no keys/models
  venv/Scripts/python.exe tests/scripts/baseline.py --list           # list selected scenarios
  venv/Scripts/python.exe tests/scripts/baseline.py                  # full run (needs keys/models/GPU)
  venv/Scripts/python.exe tests/scripts/baseline.py --save-reference # establish tests/baseline/runs/_reference.json
  venv/Scripts/python.exe tests/scripts/baseline.py --filter tool,voice --providers anthropic

See `--help` for all flags.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SCENARIOS = REPO_ROOT / "tests" / "baseline" / "scenarios" / "scenarios.json"
DEFAULT_OUT = REPO_ROOT / "tests" / "baseline" / "runs"
SCENARIO_CHAR_DIR = REPO_ROOT / "tests" / "baseline" / "scenarios" / "characters"
REFERENCE_NAME = "_reference.json"


def _repo_relative_str(p: Path) -> str:
    """Report paths repo-relative (falls back to absolute for paths outside the repo)."""
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(p)
COMPLETION_FAIL_THRESHOLD = 0.05  # -5 percentage points = machine FAIL

ERROR_TYPES = ("timeout", "memory", "schema", "tool_fail", "other")


# ---------------------------------------------------------------------------
# Injected state container (mirrors tests/conftest.py make_state, extended with
# the attributes the full generate_reply -> _generate_reply_task ->
# _run_tool_call_loop path reads — enumerated from `self.state.*` in
# backend/conversation_manager.py). Spine is untouched; this is data injection.
# ---------------------------------------------------------------------------
_STATE_DEFAULTS = dict(
    # prompt-build / tool gates
    talk_theme_enabled=False,
    pc_status_enabled=False,
    screen_capture_enabled=False,
    server_mode=False,
    command_execution_enabled=False,
    notes_enabled=False,
    camera_capture_enabled=False,
    image_generation_enabled=False,
    deep_search_enabled=False,
    elyth_enabled=False,
    speechless_enabled=False,
    # turn lifecycle
    conversation_active=True,
    active_character_id=None,
    _pending_interruption=False,
    # containers
    memory_managers={},
    short_term_buffer={},
    message_count_cache={},
    prompt_truncation_history=[],
    active_llm_cache=OrderedDict(),
    pending_requests={},
    background_tasks=[],
    # grey-list command approval (overridden per-scenario when auto_approve)
    command_approval_pending=False,
    command_pending_info=None,
    command_approval_result=None,
)


def make_state(**overrides):
    base = copy.deepcopy(_STATE_DEFAULTS)
    base.update(overrides)
    base["memory_lock"] = threading.Lock()
    # shutdown_flag mirrors the real BackendState type (threading.Event, see
    # backend.py) — a bool here crashes the real background tasks that call
    # `.is_set()`. Pre-set it so the async fire-and-forget tasks (memory
    # extraction / relationship update) early-return: they are out of Layer 2's
    # deterministic scope (they spawn unbounded LLM calls + leak daemon threads).
    # The synchronous memory read/save paths (--real-memory) are unaffected —
    # they do not consult shutdown_flag.
    base["shutdown_flag"] = threading.Event()
    base["shutdown_flag"].set()
    return SimpleNamespace(**base)


class _AutoApproveEvent:
    """A threading.Event stand-in modelling "the user approves between
    _set_pending_approval and _wait_for_approval".

    _set_pending_approval nulls command_approval_result, and execution requires
    the exact value "accepted" (not "approved"). So wait() must *inject*
    command_approval_result = "accepted" on the state — merely returning True and
    pre-seeding "approved" measured a denied/interrupted path, not the approval
    path. Mirrors tests/golden/test_control_flow.py's _AutoApproveEvent."""

    def __init__(self, state=None, result="accepted"):
        self._state = state
        self._result = result

    def wait(self, timeout=None):
        if self._state is not None:
            self._state.command_approval_result = self._result
        return True

    def set(self):
        pass

    def clear(self):
        pass

    def is_set(self):
        return True


class BaselineMemoryManager:
    """Memory stand-in for the long-context scenario: serves a fixed history +
    fixed long-term memories into the prompt, and absorbs the async-save calls
    so no real DB / extraction / embedding runs (those are probabilistic in ways
    Layer 2 is not measuring). Shapes mirror the real contracts."""

    def __init__(self, memories=None, messages=None):
        self._memories = memories or []
        self._messages = messages or []

    # read paths used by _build_prompt_messages
    def get_memories_for_prompt(self, user_text, provider):
        return copy.deepcopy(self._memories)

    def get_messages_for_prompt(self, limit=20):
        return copy.deepcopy(self._messages)[-limit:]

    # write paths used by the async-save branch (no-op, no extraction). The real
    # add_message takes images=/documents= too, so accept **kwargs.
    def add_message(self, role, content, metadata=None, **kwargs):
        return {"needs_extraction": False, "unprocessed_count": 0}

    def get_message_count(self):
        return len(self._messages)


# ---------------------------------------------------------------------------
# Real memory layer (--real-memory): exercise the REAL MemoryManager — real
# embeddings + SQLite store + semantic search — instead of the fixed stand-in:
# the long-context scenario then drives the real READ path (get_memories_for_
# prompt -> query embedding -> cosine search) and the real SAVE path
# (add_message -> embedding -> DB write). Embedding provider/model come from the
# real character_data/api_settings.json unless overridden by --embedding-model.
# ---------------------------------------------------------------------------
def build_real_memory_manager(cid, scenario, db_dir):
    """Construct a real MemoryManager on a fresh temp DB and seed it with the
    scenario's history (-> messages namespace) + long-term memories (-> memories
    namespace), generating REAL embeddings for both. Returns the manager."""
    from backend.memory.memory_manager import MemoryManager, _get_timestamp

    db_path = Path(db_dir) / f"{cid}.db"
    mm = MemoryManager(str(db_path), cid)

    # history -> real messages (each embedded), feeding get_messages_for_prompt
    for msg in scenario.get("history") or []:
        res = mm.add_message(msg["role"], msg["content"])
        if not res.get("success"):
            raise RuntimeError(f"seed add_message failed for {cid}")

    # long-term memories -> memories namespace with real embeddings, preserving
    # the scenario's category/content/id. add_memory_manual would reject the
    # scenario's display categories, so seed the store directly the same way it
    # does (real _get_embedding + _validate_and_convert_embedding + commit).
    ltm = scenario.get("long_term_memories") or []
    if ltm:
        meta_ns = (cid, "meta")
        memory_ns = (cid, "memories")
        with mm.db_lock:
            count_item = mm.store.get(meta_ns, "memory_count")
            count = count_item.value.get("count", 0) if count_item else 0
            for entry in ltm:
                count += 1
                emb = mm._get_embedding(entry["content"])
                now = _get_timestamp()
                mm.store.put(memory_ns, f"mem_{count}", {
                    "category": entry.get("category", "unknown"),
                    "content": entry["content"],
                    "embedding": mm._validate_and_convert_embedding(emb),
                    # provenance stamp: search skips entries
                    # whose stamp differs from the current embedding model, so
                    # seeded memories must be stamped like production writes
                    "embedding_model": mm._current_embedding_key(),
                    "source_range": [],
                    "created_at": now,
                    "updated_at": now,
                    "last_retrieved": now,
                    "pinned": False,
                })
            mm.store.put(meta_ns, "memory_count", {"count": count})
            mm.store.commit()
    return mm


def apply_embedding_override(encoded_value):
    """Force the memory layer to use a specific embedding model regardless of
    character_data/api_settings.json. `_get_embedding`/`_get_embeddings_batch` re-import
    get_embedding_model at call time, so patching the module function suffices.
    Returns (provider, model_name)."""
    import backend.shared.api_settings as api_mod
    provider, model_name = api_mod.decode_model_value(encoded_value)
    api_mod.get_embedding_model = lambda: (provider, model_name)
    return provider, model_name


# ---------------------------------------------------------------------------
# Seams. Applied once before the run. Direct setattr (process is one-shot).
# ---------------------------------------------------------------------------
def apply_seams(patch_api_settings_empty: bool):
    """Wire the non-spine seams.

    Always:
      - _enqueue_llm_task -> synchronous direct call (no queue thread; the LLM /
        tool execution still runs for REAL). Per-scenario timeout is enforced by
        running each turn in a worker thread (run_with_timeout).
      - _play_tts_blocking -> no-op. The real one blocks on a browser playback
        ACK (WS layer, out of scope here). Real TTS synthesis is run explicitly
        by the script on the final response (run_real_tts).
      - send_ui_update -> no-op (no WS server running).
      - consume_unrecorded_location -> None (avoid global/disk leak).
      - disk-reading leaf prompt providers -> fixed safe values, so synthetic
        baseline character_ids (which have no on-disk relationship/note files)
        don't raise spuriously. These do NOT change what Layer 2 measures.

    api_settings is kept REAL in a real run (so the LLM key + image/map tool
    gates reflect the real environment). In --dry-run it is patched empty
    (patch_api_settings_empty=True) so the wiring check needs no keys.
    """
    import backend.conversation_manager as cm_mod
    import backend.memory.relationship_manager as rel_mod
    import backend.memory.note_manager as note_mod
    import backend.elyth.elyth_note_manager as elyth_note_mod
    import backend.tools.location_manager as loc_mod
    import backend.shared.image_storage as img_mod
    import backend.server.websocket_server as ws_mod

    def _direct_enqueue(self, task_callable, *args, **kwargs):
        kwargs.pop("request_id", None)
        kwargs.pop("timeout", None)
        return task_callable(*args, **kwargs)

    cm_mod.ConversationManager._enqueue_llm_task = _direct_enqueue
    cm_mod.ConversationManager._play_tts_blocking = lambda self, text: None

    ws_mod.send_ui_update = lambda *a, **k: None
    loc_mod.consume_unrecorded_location = lambda: None

    # fixed safe leaves (disk readers)
    rel_mod.build_relationship_prompt = lambda character_id, language="ja": "【関係性】ベースライン用の固定関係性サマリ。"
    note_mod.build_note_prompt = lambda character_id, language="ja": "【ノート】ベースライン用の固定ノート。"
    elyth_note_mod.build_elyth_note_prompt = (
        lambda character_id, include_system_prompt=False, language="ja": "【ELYTHノート】ベースライン用固定ノート。"
    )
    loc_mod.get_location_for_prompt = lambda language="ja": ""
    loc_mod.has_valid_location = lambda: False
    img_mod.get_thumbnail_path_for = lambda full_path: None

    if patch_api_settings_empty:
        import backend.shared.api_settings as api_mod
        api_mod.load_api_settings = lambda: {}
        api_mod.get_image_generation_model = lambda: ("", "")
        api_mod.get_google_maps_api_key = lambda: ""


# ---------------------------------------------------------------------------
# Scenario state wiring
# ---------------------------------------------------------------------------
def load_character_config(scenario):
    name = scenario["character"]
    path = SCENARIO_CHAR_DIR / name
    config = json.loads(path.read_text(encoding="utf-8"))
    # ELYTH key must not live in a git-tracked file. If the config still holds the
    # placeholder, take the real key from env AG_ELYTH_API_KEY (keeps the secret
    # out of the repo). No-op when neither is set (ELYTH scenarios then fail auth,
    # recorded honestly).
    k = config.get("elyth_api_key")
    if (not k or "REPLACE" in k) and os.environ.get("AG_ELYTH_API_KEY"):
        config = dict(config)
        config["elyth_api_key"] = os.environ["AG_ELYTH_API_KEY"]
    return config


def build_state_and_cm(scenario, config, args=None):
    from backend.conversation_manager import ConversationManager

    cid = scenario.get("character_id", scenario["id"])
    flags = scenario.get("flags", {})
    overrides = dict(flags)
    overrides["active_character_id"] = cid

    auto_approve_event = None
    if scenario.get("auto_approve"):
        auto_approve_event = _AutoApproveEvent()  # state wired after make_state
        overrides["command_approval_event"] = auto_approve_event

    # long-context: serve history + long-term memory. Default = fixed stand-in.
    # --real-memory swaps in the REAL MemoryManager (real embeddings + SQLite +
    # semantic search). Never in --dry-run (no keys / no DB seed there).
    history = scenario.get("history")
    ltm = scenario.get("long_term_memories")
    if history or ltm:
        use_real = bool(args and getattr(args, "real_memory", False)
                        and not getattr(args, "dry_run", False))
        if use_real:
            mm = build_real_memory_manager(cid, scenario, args._mem_db_dir)
        else:
            mm = BaselineMemoryManager(memories=ltm or [], messages=history or [])
        overrides["memory_managers"] = {cid: mm}

    # Per-scenario location: scenarios with a "location" field get a fixed
    # <user_location> in the prompt so location-dependent tools (map_search)
    # actually fire instead of the model declining for lack of location.
    import backend.tools.location_manager as loc_mod
    loc = scenario.get("location")
    loc_mod.get_location_for_prompt = (lambda language="ja", v=loc: v) if loc else (lambda language="ja": "")
    loc_mod.has_valid_location = (lambda: True) if loc else (lambda: False)

    state = make_state(**overrides)
    if auto_approve_event is not None:
        # wait() injects command_approval_result = "accepted" on this state
        auto_approve_event._state = state
    cm = ConversationManager(
        state,
        lambda character_id: config,  # config loader
        lambda *a, **k: None,         # character activator (not used here)
        lambda *a, **k: None,         # find config file (not used here)
    )
    return state, cm, cid


# ---------------------------------------------------------------------------
# Real STT / TTS helpers
# ---------------------------------------------------------------------------
class AudioRig:
    """Lazily loads the real whisper / TTS models once, shared across scenarios."""

    def __init__(self, args):
        self.args = args
        self.stt_ready = False
        self.tts_ready = False

    def ensure_stt(self):
        if self.stt_ready:
            return
        import audio_input.audio_input as stt
        stt.init_audio_input(model_size=self.args.whisper, device=self.args.device)
        stt.configure_stt(self.args.stt_language)
        self._stt = stt
        self.stt_ready = True

    def transcribe(self, wav_path):
        self.ensure_stt()
        return self._stt.transcribe_file(str(REPO_ROOT / wav_path))

    def ensure_tts(self, config):
        """Configure TTS from CLI --tts-* (priority) or the character's
        tts_model_config. Returns True if a model is loaded."""
        if self.tts_ready:
            return True
        import audio_output.audio_output as tts
        a = self.args
        if a.tts_model_path and a.tts_config_path and a.tts_style_path:
            mp, cp, sp = a.tts_model_path, a.tts_config_path, a.tts_style_path
        else:
            tc = config.get("tts_model_config") or {}
            mp = tc.get("model_path")
            cp = tc.get("config_path")
            sp = tc.get("style_vectors_path")
        if not (mp and cp and sp):
            return False
        tts.configure_tts_model(
            model_path=mp, config_path=cp, style_vectors_path=sp,
            device=(a.device or "cuda"),
        )
        self._tts = tts
        self.tts_ready = True
        return True

    def synthesize(self, text, config):
        if not self.ensure_tts(config):
            return None  # TTS not configured -> caller marks "skipped"
        audio, sr = self._tts.text_to_speech(text)
        return audio, sr


# ---------------------------------------------------------------------------
# Turn execution + classification
# ---------------------------------------------------------------------------
def run_with_timeout(fn, timeout):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - faithfully record any failure
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, TimeoutError(f"turn exceeded {timeout}s")
    return box.get("value"), box.get("error")


def classify_error(result, exc):
    """Heuristic mapping into the error buckets. Human-reviewable."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, MemoryError):
        return "memory"
    if exc is not None:
        m = str(exc).lower()
        if "timeout" in m or "timed out" in m:
            return "timeout"
        if "memory" in m or "out of memory" in m or "cuda" in m and "memory" in m:
            return "memory"
        if "schema" in m or "validation" in m or "invalid" in m and "argument" in m:
            return "schema"
        if "tool" in m:
            return "tool_fail"
        return "other"
    if isinstance(result, dict):
        et = (result.get("error_type") or "").upper()
        if et == "RESOURCE_LIMIT":
            return "memory"
        if "TIMEOUT" in et:
            return "timeout"
        if "SCHEMA" in et or et == "VALIDATION":
            return "schema"
        if "TOOL" in et:
            return "tool_fail"
        if et:
            return "other"
    return "other"


def scenario_timeout(scenario, override):
    if override:
        return override
    flags = scenario.get("flags", {})
    if flags.get("command_execution_enabled") or flags.get("deep_search_enabled"):
        return 300.0
    if flags.get("elyth_enabled"):
        return 300.0
    return 120.0


def tool_calls_from_steps(result):
    """Record which tool/action steps fired this turn. The real step shapes vary:
    deep_search/map_search carry a "tool" key, while image_gen/note/command/elyth
    carry only "type". Capture the tool name when present, else the step type;
    skip plain "ai_text" steps (= the assistant's text, not a tool)."""
    if not isinstance(result, dict):
        return []
    actions = []
    for s in result.get("steps") or []:
        if isinstance(s, dict):
            t = s.get("type")
            if t and t != "ai_text":
                actions.append(s.get("tool") or t)
    return actions


def run_scenario(scenario, rig, args):
    cid = scenario.get("character_id", scenario["id"])
    rec = {
        "id": scenario["id"],
        "category": scenario["category"],
        "provider": scenario.get("provider", "?"),
        "character_id": cid,
        "completed": False,
        "error_type": None,
        "error": None,
        "latency_s": None,
        "stt_text": None,
        "stt_latency_s": None,
        "tts_status": None,
        "tts_latency_s": None,
        "tool_calls": [],
        "expected_tool": scenario.get("expected_tool"),
        "response_preview": None,
    }
    config = load_character_config(scenario)
    state, cm, cid = build_state_and_cm(scenario, config, args)

    # ---- input: text or wav->STT
    inp = scenario["input"]
    if inp["type"] == "wav":
        try:
            t0 = time.time()
            user_text = rig.transcribe(inp["wav"])
            rec["stt_latency_s"] = round(time.time() - t0, 3)
            rec["stt_text"] = user_text
        except Exception as e:  # noqa: BLE001
            rec["error_type"] = "other"
            rec["error"] = f"STT failed: {e}"
            return rec
        if not user_text or not user_text.strip():
            rec["error_type"] = "other"
            rec["error"] = "STT returned empty text"
            return rec
    else:
        user_text = inp["text"]

    # ---- attachments
    att = scenario.get("attachments", {})
    images = [str(REPO_ROOT / p) for p in att.get("images", [])] or None
    documents = att.get("documents") or None

    # ---- the turn (real LLM + real tools), bounded by a worker-thread timeout
    timeout = scenario_timeout(scenario, args.timeout)
    t0 = time.time()
    result, exc = run_with_timeout(
        lambda: cm.generate_reply(user_text, character_id=cid, images=images, documents=documents),
        timeout,
    )
    rec["latency_s"] = round(time.time() - t0, 3)

    reply_ok = bool(isinstance(result, dict) and result.get("success") and exc is None)
    if isinstance(result, dict):
        rec["tool_calls"] = tool_calls_from_steps(result)
        resp = result.get("response") or ""
        rec["response_preview"] = resp[:200]
    if not reply_ok:
        rec["error_type"] = classify_error(result, exc)
        rec["error"] = (str(exc) if exc is not None
                        else (result.get("error") if isinstance(result, dict) else "no result"))
        return rec

    # ---- real TTS on the final response (voice scenarios)
    if scenario.get("tts"):
        try:
            t0 = time.time()
            out = rig.synthesize(rec["response_preview"] or "（応答）", config)
            if out is None:
                rec["tts_status"] = "skipped"  # TTS not configured -> doesn't fail completion
            else:
                audio, sr = out
                nonempty = audio is not None and len(audio) > 0 and sr > 0
                rec["tts_latency_s"] = round(time.time() - t0, 3)
                rec["tts_status"] = "ok" if nonempty else "empty"
                if not nonempty:
                    rec["error_type"] = "tool_fail"
                    rec["error"] = "TTS produced empty audio"
                    return rec  # completion fails: TTS configured but failed
        except Exception as e:  # noqa: BLE001
            rec["tts_status"] = "error"
            rec["error_type"] = "tool_fail"
            rec["error"] = f"TTS failed: {e}"
            return rec

    rec["completed"] = True
    return rec


def dry_run_scenario(scenario):
    """Build state + assemble the prompt only (no LLM/STT/TTS). Validates wiring
    and scenario configs on ANY machine (no keys/models needed)."""
    rec = {"id": scenario["id"], "category": scenario["category"], "ok": False, "error": None}
    try:
        config = load_character_config(scenario)
        state, cm, cid = build_state_and_cm(scenario, config)
        inp = scenario["input"]
        user_text = inp.get("text") or "(dry-run: wav not transcribed)"
        if inp["type"] == "wav":
            wav = REPO_ROOT / inp["wav"]
            if not wav.exists():
                raise FileNotFoundError(f"WAV fixture missing: {wav}")
        for p in scenario.get("attachments", {}).get("images", []):
            if not (REPO_ROOT / p).exists():
                raise FileNotFoundError(f"image fixture missing: {p}")
        messages = cm._build_prompt_messages(cid, user_text=user_text, config=config)
        if not messages:
            raise AssertionError("empty prompt messages")
        rec["ok"] = True
        rec["messages"] = len(messages)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


# ---------------------------------------------------------------------------
# Aggregation + report
# ---------------------------------------------------------------------------
def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(s[int(k)], 3)
    return round(s[f] * (c - k) + s[c] * (k - f), 3)


def _rate(records):
    total = len(records)
    done = sum(1 for r in records if r["completed"])
    return {"total": total, "completed": done,
            "completion_rate": round(done / total, 4) if total else 0.0}


def aggregate(records):
    latencies = [r["latency_s"] for r in records if r["latency_s"] is not None]
    err_counts = {t: 0 for t in ERROR_TYPES}
    for r in records:
        if not r["completed"] and r["error_type"]:
            err_counts[r["error_type"]] = err_counts.get(r["error_type"], 0) + 1

    by_cat, by_prov = {}, {}
    for r in records:
        by_cat.setdefault(r["category"], []).append(r)
        by_prov.setdefault(r["provider"], []).append(r)

    summary = _rate(records)
    summary["error_types"] = err_counts
    summary["latency"] = {
        "p50": percentile(latencies, 0.5),
        "p95": percentile(latencies, 0.95),
        "n": len(latencies),
    }
    return {
        "summary": summary,
        "by_category": {k: _rate(v) for k, v in by_cat.items()},
        "by_provider": {k: _rate(v) for k, v in by_prov.items()},
    }


def git_info():
    def _run(cmd):
        try:
            return subprocess.check_output(cmd, cwd=REPO_ROOT, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except Exception:  # noqa: BLE001
            return None
    return {"branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
            "commit": _run(["git", "rev-parse", "--short", "HEAD"])}


def build_report(records, args, timestamp):
    agg = aggregate(records)
    return {
        "timestamp": timestamp,
        "git": git_info(),
        "config": {
            "providers": args.providers,
            "filter": args.filter,
            "whisper": args.whisper,
            "device": args.device,
            "tts_configured": bool(args.tts_model_path),
            "timeout_override": args.timeout,
            "scenarios_file": _repo_relative_str(args.scenarios),
            "real_memory": getattr(args, "real_memory", False),
            "embedding_model": getattr(args, "embedding_model", None),
            "embedding_model_resolved": getattr(args, "_resolved_embedding", None),
        },
        **agg,
        "scenarios": records,
    }


# ---------------------------------------------------------------------------
# Reference comparison (the only machine gate)
# ---------------------------------------------------------------------------
def compare_reference(report, out_dir):
    ref_path = out_dir / REFERENCE_NAME
    cur = report["summary"]["completion_rate"]
    if not ref_path.exists():
        print("\n[reference] none found at", ref_path)
        print("  -> first run. Inspect this report, then re-run with --save-reference")
        print("     (or copy it to tests/baseline/runs/_reference.json) to establish the baseline.")
        return 0
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    ref_rate = ref.get("summary", {}).get("completion_rate", 0.0)
    delta_pt = (cur - ref_rate) * 100
    print(f"\n[reference] completion rate: current={cur:.1%}  baseline={ref_rate:.1%}  "
          f"delta={delta_pt:+.1f}pt  (ref={ref.get('timestamp')})")
    # human-judgement signals (NOT a gate)
    rl = report["summary"]["latency"]
    refl = ref.get("summary", {}).get("latency", {})
    if rl.get("p95") and refl.get("p95") and rl["p95"] > refl["p95"] * 1.5:
        print(f"  ⚠ latency p95 up >50% ({refl['p95']}s -> {rl['p95']}s) — human review")
    for et, n in report["summary"]["error_types"].items():
        ref_n = ref.get("summary", {}).get("error_types", {}).get(et, 0)
        if n > 0 and ref_n == 0:
            print(f"  ⚠ new error type appeared: {et} x{n} — human review")
    if cur < ref_rate - COMPLETION_FAIL_THRESHOLD:
        print(f"\n[GATE] ❌ FAIL — completion rate dropped > {COMPLETION_FAIL_THRESHOLD*100:.0f}pt below baseline.")
        return 1
    print(f"\n[GATE] ✅ PASS — completion rate within {COMPLETION_FAIL_THRESHOLD*100:.0f}pt of baseline.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def select_scenarios(all_scenarios, args):
    out = all_scenarios
    if args.filter:
        cats = set(args.filter.split(","))
        out = [s for s in out if s["category"] in cats]
    if args.providers:
        provs = set(args.providers.split(","))
        out = [s for s in out if s.get("provider") in provs]
    if args.ids:
        ids = set(args.ids.split(","))
        out = [s for s in out if s["id"] in ids]
    return out


def parse_args(argv):
    p = argparse.ArgumentParser(description="Layer 2 full-fidelity baseline.")
    p.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--filter", help="comma-separated categories (plain_chat,voice,tool,elyth,multimodal,long_context)")
    p.add_argument("--providers", help="comma-separated providers to keep (anthropic,ollama)")
    p.add_argument("--ids", help="comma-separated scenario ids")
    p.add_argument("--whisper", default="turbo", help="faster-whisper model size (default: turbo)")
    p.add_argument("--device", default=None, help="cuda|cpu (default: auto)")
    p.add_argument("--stt-language", default="ja")
    p.add_argument("--tts-model-path", default=None)
    p.add_argument("--tts-config-path", default=None)
    p.add_argument("--tts-style-path", default=None)
    p.add_argument("--timeout", type=float, default=None, help="override per-scenario timeout (s)")
    p.add_argument("--save-reference", action="store_true", help="write this run to tests/baseline/runs/_reference.json")
    p.add_argument("--dry-run", action="store_true", help="assemble prompts only; no LLM/STT/TTS, no keys needed")
    p.add_argument("--list", action="store_true", help="list selected scenarios and exit")
    p.add_argument("--real-memory", action="store_true",
                   help="exercise the REAL MemoryManager (real embeddings + SQLite + semantic "
                        "search) for memory scenarios instead of the fixed stand-in")
    p.add_argument("--embedding-model", default=None,
                   help="override embedding model as 'provider::model' (e.g. "
                        "openai::text-embedding-3-large or ollama::nomic-embed-text); "
                        "default = configured character_data/api_settings.json. Only used with --real-memory")
    return p.parse_args(argv)


def main(argv=None):
    # Console output carries Japanese + status glyphs (✅/❌/⚠); the default
    # Windows console codepage (cp932) can't encode them and would crash mid-run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - older/odd streams: best effort
            pass

    args = parse_args(argv)
    manifest = json.loads(args.scenarios.read_text(encoding="utf-8"))
    scenarios = select_scenarios(manifest["scenarios"], args)

    if args.list:
        print(f"{len(scenarios)} scenario(s):")
        for s in scenarios:
            print(f"  {s['id']:28} {s['category']:13} {s.get('provider','?')}")
        return 0

    if not scenarios:
        print("No scenarios selected.", file=sys.stderr)
        return 2

    if args.dry_run:
        apply_seams(patch_api_settings_empty=True)
        print(f"[dry-run] assembling prompts for {len(scenarios)} scenario(s) (no LLM/STT/TTS)\n")
        ok = 0
        for s in scenarios:
            r = dry_run_scenario(s)
            status = "OK " if r["ok"] else "ERR"
            extra = f"messages={r['messages']}" if r["ok"] else r["error"]
            print(f"  [{status}] {r['id']:28} {extra}")
            ok += int(r["ok"])
        print(f"\n[dry-run] {ok}/{len(scenarios)} assembled cleanly.")
        return 0 if ok == len(scenarios) else 1

    # ---- real run
    apply_seams(patch_api_settings_empty=False)

    # --real-memory: real MemoryManager on a throwaway temp DB dir + optional
    # embedding-model override (default = configured character_data/api_settings.json).
    args._mem_db_dir = None
    args._resolved_embedding = None
    if args.real_memory:
        args._mem_db_dir = tempfile.mkdtemp(prefix="ag_baseline_mem_")
        if args.embedding_model:
            prov, model = apply_embedding_override(args.embedding_model)
        else:
            import backend.shared.api_settings as _api
            _em = _api.get_embedding_model()
            if _em is None:
                raise SystemExit(
                    "[real-memory] embedding model is not configured — set it in "
                    "the API Setting tab or pass --embedding-model provider::model"
                )
            prov, model = _em
        args._resolved_embedding = f"{prov}::{model}"
        print(f"[real-memory] real MemoryManager enabled — embeddings: {prov}::{model}")

    rig = AudioRig(args)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    print(f"[baseline] {len(scenarios)} scenario(s) — real LLM/STT/TTS. timestamp={timestamp}\n")

    records = []
    try:
        for i, s in enumerate(scenarios, 1):
            print(f"  ({i}/{len(scenarios)}) {s['id']} ...", flush=True)
            rec = run_scenario(s, rig, args)
            mark = "✅" if rec["completed"] else f"❌ {rec['error_type']}"
            lat = f"{rec['latency_s']}s" if rec["latency_s"] is not None else "-"
            print(f"        {mark}  latency={lat}"
                  + (f"  tts={rec['tts_status']}" if rec["tts_status"] else "")
                  + (f"  err={rec['error'][:80]}" if rec["error"] else ""))
            records.append(rec)
    finally:
        if args._mem_db_dir:
            shutil.rmtree(args._mem_db_dir, ignore_errors=True)

    report = build_report(records, args, timestamp)

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"{timestamp}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[report] {out_path}")

    s = report["summary"]
    print(f"[summary] completed {s['completed']}/{s['total']} = {s['completion_rate']:.1%}  "
          f"| latency p50={s['latency']['p50']}s p95={s['latency']['p95']}s  "
          f"| errors={ {k: v for k, v in s['error_types'].items() if v} }")
    for cat, r in report["by_category"].items():
        print(f"          {cat:13} {r['completed']}/{r['total']} = {r['completion_rate']:.0%}")

    rc = compare_reference(report, args.out)

    if args.save_reference:
        ref_path = args.out / REFERENCE_NAME
        ref_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[reference] saved -> {ref_path}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
