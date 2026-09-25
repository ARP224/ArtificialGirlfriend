"""Bundled model characters (Momo / Cecilia / Stella) — seeded once per install.

`model_characters/<Name>/` at the repo root ships three ready-made characters
so a fresh clone has someone to talk to without going through the creation
form first (稜裁定 2026-09-25):

    character.json      id / name / summary / language / voice / Motion folder
    system_prompt.txt   the system prompt (UTF-8, LF — .gitattributes pins it)
    icon.png            512px square, the same shape the UI's icon upload makes

Their Motion assets live in `MotionPNGPlayer/Asset/<Name>/` (the one tracked
exception to Asset/ being user data — see MotionPNGPlayer/.gitignore).

How seeding works
- init_backend calls seed_model_characters() right after the character-config
  migration. Each bundled character goes through the ordinary create_character
  path with its fixed character_id, so it gets every side effect a UI-created
  character gets (config file, icon copy, memory .db, tuning defaults).
- `character_data/model_characters_seeded.json` records every id ever seeded.
  A character the user deletes is never re-created (its id stays in the
  marker); wiping character_data/ starts over and seeds again. A config that
  already exists (e.g. marker lost) is left alone and just recorded.
- The LLM is left unset on purpose: nothing installable can be assumed on a
  fresh machine (no Ollama model is pulled, no API key exists), so the user
  picks one in "Edit Existing Character" before the first conversation. The
  two Japanese characters also need a Style-Bert-VITS2 voice (user-supplied);
  Stella ships with a Kokoro voice, which the installer fetches.
- Failures are per character and non-fatal (collected in "errors"); the
  caller logs them. Layer: domain (this module) <- (injected
  atomic_file_operation) app, same as create_character.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from backend.shared.constants import BASE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

MODEL_CHARACTERS_DIR = BASE_DIR / "model_characters"
SEED_MARKER_FILE = DATA_DIR / "model_characters_seeded.json"

_REQUIRED_KEYS = ("character_id", "name", "language", "motion_pngtuber_folder")


def list_bundled_characters() -> List[Dict[str, Any]]:
    """Read every `model_characters/<Name>/` into a create_character info dict.

    Returns the dicts sorted by folder name. A folder that is missing a file
    or a required key raises ValueError (the caller decides how to report).
    Empty list when the directory does not exist (e.g. removed by the user).
    """
    root = Path(MODEL_CHARACTERS_DIR)
    if not root.is_dir():
        return []
    infos: List[Dict[str, Any]] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        cfg_path = folder / "character.json"
        prompt_path = folder / "system_prompt.txt"
        icon_path = folder / "icon.png"
        for required in (cfg_path, prompt_path, icon_path):
            if not required.is_file():
                raise ValueError(f"{folder.name}: missing {required.name}")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for key in _REQUIRED_KEYS:
            if not cfg.get(key):
                raise ValueError(f"{folder.name}: character.json lacks '{key}'")
        with open(prompt_path, "r", encoding="utf-8", newline="") as f:
            system_prompt = f.read()
        if not system_prompt.strip():
            raise ValueError(f"{folder.name}: system_prompt.txt is empty")
        infos.append({
            "character_id": cfg["character_id"],
            "name": cfg["name"],
            "icon_path": str(icon_path),
            "summary_text": cfg.get("summary_text", ""),
            "system_prompt": system_prompt,
            "faster_whisper_config": {"language": cfg["language"]},
            # Unset on purpose — see the module docstring.
            "model_provider": "ollama",
            "model_name": "",
            "tts_model_config": cfg.get("tts_model_config") or {
                "provider": "sbv2",
                "model_path": "",
                "config_path": "",
                "style_vectors_path": "",
            },
            "motion_pngtuber_folder": cfg["motion_pngtuber_folder"],
        })
    return infos


def _load_marker() -> Dict[str, Any]:
    marker = Path(SEED_MARKER_FILE)
    if not marker.is_file():
        return {"version": 1, "seeded": {}}
    try:
        with open(marker, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("seeded"), dict):
            raise ValueError("'seeded' is not an object")
        return data
    except Exception as e:
        # A corrupt marker must not re-seed characters the user deleted, so
        # treat it as "nothing recorded" only for ids whose config exists;
        # ids without a config would be re-created — log loudly instead.
        logger.warning(f"Model character marker unreadable ({e}); rewriting it")
        return {"version": 1, "seeded": {}}


def _save_marker(data: Dict[str, Any], atomic_file_operation) -> None:
    marker = Path(SEED_MARKER_FILE)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with atomic_file_operation(marker, "write") as temp_file:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def seed_model_characters(atomic_file_operation) -> Dict[str, Any]:
    """Create the bundled characters that were never seeded on this install.

    Returns {"seeded": [names created now], "skipped": count, "errors": [str]}.
    Never raises for a single character's failure; the marker is written
    only when something changed.
    """
    result: Dict[str, Any] = {"seeded": [], "skipped": 0, "errors": []}
    try:
        bundled = list_bundled_characters()
    except Exception as e:
        result["errors"].append(f"bundle unreadable: {e}")
        return result
    if not bundled:
        return result

    # Same-package import; kept local so importing this module stays cheap.
    from backend.conversation.character_manager import (
        _find_config_file_by_id,
        create_character,
    )

    marker = _load_marker()
    seeded: Dict[str, Any] = marker["seeded"]
    changed = False
    for info in bundled:
        cid = info["character_id"]
        if cid in seeded:
            result["skipped"] += 1
            continue
        if _find_config_file_by_id(cid) is not None:
            # Exists without a record (marker lost/corrupt): adopt, don't touch.
            seeded[cid] = {"name": info["name"], "at": _now(), "adopted": True}
            result["skipped"] += 1
            changed = True
            continue
        try:
            create_character(info, atomic_file_operation)
        except Exception as e:
            result["errors"].append(f"{info['name']}: {e}")
            continue
        seeded[cid] = {"name": info["name"], "at": _now()}
        result["seeded"].append(info["name"])
        changed = True

    if changed:
        try:
            _save_marker(marker, atomic_file_operation)
        except Exception as e:
            result["errors"].append(f"marker not saved: {e}")
    return result


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
