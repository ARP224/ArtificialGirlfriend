"""
backend/elyth_cycle_logger.py

Operational logging for ELYTH cycles. Captures every LLM exchange (request +
response) for every character in a cycle into a single JSON file at a fixed
path. Each new cycle overwrites the previous file, so disk usage stays bounded.

This is always on; it doubles as the troubleshooting trail when something goes
wrong (LLM behaviour, ELYTH API errors, prompt structure regressions). The
file is also self-describing for token / cost analysis.

Output: logs/elyth_cycle_log.json (overwritten every cycle)

Lifecycle (called from elyth_session_manager):
    start_cycle()              ← _run_cycle entry, after enabled chars resolved
    start_character()          ← _session_task, after _build_elyth_prompt
        log_request() / log_response()  ← per turn in _elyth_tool_call_loop
    end_character()            ← _session_task, after the tool-call loop
    [repeat per character]
    end_cycle()                ← _run_cycle exit
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.shared.constants import LOGS_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CWD相対だと別CWDから起動したとき logs/ が迷子になる。リポジトリ固定の
# LOGS_DIR を使う(他のELYTHログは既にこれを使用)。
_OUTPUT_PATH = LOGS_DIR / "elyth_cycle_log.json"

# Sonnet 4.6 pricing (USD per million tokens)
_PRICING = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.30,
    "cache_creation": 3.75,
}

# System prompt sections wrapped by XML-like tags in _build_elyth_prompt.
_SECTION_PATTERN = re.compile(
    r"<(elyth_system_prompt|elyth_instructions|elyth_notes|"
    r"elyth_relationships|relationship|notes|long_term_memory)>"
    r"\n?(.*?)\n?</\1>",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _byte_len(content: Any) -> int:
    """UTF-8 byte length. Handles str, dict/list (serialized), other (str())."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    try:
        return len(json.dumps(content, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(content).encode("utf-8"))


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class ELYTHCycleLogger:
    """Captures one ELYTH cycle (multi-character) into a single JSON file.

    State machine:
        IDLE → start_cycle → CYCLE_OPEN
        CYCLE_OPEN → start_character → CHAR_OPEN
        CHAR_OPEN → log_request/log_response → CHAR_OPEN
        CHAR_OPEN → end_character → CYCLE_OPEN
        CYCLE_OPEN → end_cycle → IDLE

    Methods are defensive: if called out of order, they log a warning and
    skip rather than corrupting state.
    """

    def __init__(self, output_path: Optional[Path] = None):
        self.output_path = output_path or _OUTPUT_PATH
        self._data: Dict[str, Any] = {}
        self._current_char_idx: Optional[int] = None
        # Per-character running aggregates
        self._char_tool_max: Dict[str, int] = {}

    # ----- Cycle-level ------------------------------------------------------

    def start_cycle(self, enabled_character_ids: List[str], interval_seconds: int) -> None:
        """Initialize a fresh cycle log, overwriting any prior file."""
        self._data = {
            "cycle_started_at": datetime.now().isoformat(),
            "cycle_ended_at": None,
            "interval_seconds": interval_seconds,
            "enabled_character_ids": list(enabled_character_ids),
            "characters": [],
            "cycle_totals": None,
        }
        self._current_char_idx = None
        self._char_tool_max = {}
        self._flush()

    def end_cycle(self) -> None:
        """Finalize the cycle: aggregate cycle-level totals and flush."""
        self._data["cycle_ended_at"] = datetime.now().isoformat()
        self._data["cycle_totals"] = self._aggregate_cycle_totals()
        self._flush()

    # ----- Character-level --------------------------------------------------

    def start_character(
        self,
        character_id: str,
        character_name: str,
        provider: str,
        past_msg_count: int,
    ) -> None:
        """Open a new character entry within the current cycle."""
        if not self._data:
            logger.warning("[ELYTH CycleLog] start_character called without start_cycle")
            return
        char_entry: Dict[str, Any] = {
            "character_id": character_id,
            "character_name": character_name,
            "provider": provider,
            "started_at": datetime.now().isoformat(),
            "ended_at": None,
            "end_reason": None,
            "past_msg_count_at_start": past_msg_count,
            "totals": None,
            "cost_usd_estimate": None,
            "largest_tool_results_observed": None,
            "turns": [],
        }
        self._data["characters"].append(char_entry)
        self._current_char_idx = len(self._data["characters"]) - 1
        self._char_tool_max = {}
        self._flush()

    def end_character(self, end_reason: str) -> None:
        """Close the active character entry: aggregate totals and flush."""
        char = self._current_char()
        if char is None:
            return
        char["ended_at"] = datetime.now().isoformat()
        char["end_reason"] = end_reason
        totals = self._aggregate_char_totals(char)
        char["totals"] = totals
        char["cost_usd_estimate"] = self._compute_cost(totals)
        char["largest_tool_results_observed"] = sorted(
            [{"name": k, "max_bytes": v} for k, v in self._char_tool_max.items()],
            key=lambda x: x["max_bytes"], reverse=True,
        )[:10]
        self._current_char_idx = None
        self._char_tool_max = {}
        self._flush()

    # ----- Turn-level -------------------------------------------------------

    def log_request(self, turn: int, messages: List[Any], tools: List[Any]) -> None:
        """Append/update the current turn entry with the request side."""
        char = self._current_char()
        if char is None:
            return
        try:
            digest = self._build_request_digest(messages, tools)
            turn_entry = self._get_or_create_turn(char, turn)
            turn_entry["timestamp"] = datetime.now().isoformat()
            turn_entry["request_digest"] = digest
            turn_entry["request_full"] = {"messages": messages, "tools": tools}
            self._flush()
        except Exception as e:
            logger.warning(f"[ELYTH CycleLog] log_request failed turn {turn + 1}: {e}")

    def log_response(self, turn: int, response: Any, duration_ms: int) -> None:
        """Update the current turn entry with the response side."""
        char = self._current_char()
        if char is None:
            return
        try:
            raw_usage = {}
            if hasattr(response, "raw_response"):
                raw_usage = (response.raw_response or {}).get("usage", {}) or {}

            tool_calls_log = [
                {"name": tc.get("name"), "arguments": tc.get("arguments")}
                for tc in (getattr(response, "tool_calls", []) or [])
            ]

            resp_data = {
                "input_tokens": getattr(response, "input_tokens", 0),
                "output_tokens": getattr(response, "output_tokens", 0),
                "cache_read_input_tokens": raw_usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": raw_usage.get("cache_creation_input_tokens", 0),
                "content": getattr(response, "content", ""),
                "tool_calls": tool_calls_log,
            }

            turn_entry = self._get_or_create_turn(char, turn)
            turn_entry["duration_ms"] = duration_ms
            turn_entry["response"] = resp_data
            self._flush()
        except Exception as e:
            logger.warning(f"[ELYTH CycleLog] log_response failed turn {turn + 1}: {e}")

    # ----- Internals --------------------------------------------------------

    def _current_char(self) -> Optional[Dict[str, Any]]:
        if self._current_char_idx is None or not self._data:
            return None
        chars = self._data.get("characters", [])
        if 0 <= self._current_char_idx < len(chars):
            return chars[self._current_char_idx]
        return None

    @staticmethod
    def _get_or_create_turn(char: Dict[str, Any], turn: int) -> Dict[str, Any]:
        """Find an existing turn entry by `turn` (0-indexed) or append a new one."""
        turn_no = turn + 1
        for t in char["turns"]:
            if t.get("turn") == turn_no:
                return t
        new_entry = {"turn": turn_no}
        char["turns"].append(new_entry)
        return new_entry

    def _build_request_digest(
        self, messages: List[Any], tools: List[Any],
    ) -> Dict[str, Any]:
        """Section/byte breakdown of one request payload."""
        # Separate system vs non-system messages
        system_contents: List[str] = []
        non_system: List[Any] = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                c = m.get("content", "")
                system_contents.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
            else:
                non_system.append(m)

        system_text = "\n\n".join(system_contents)
        sections: Dict[str, int] = {}
        for m in _SECTION_PATTERN.finditer(system_text):
            sections[m.group(1)] = _byte_len(m.group(2))

        char = self._current_char()
        past_end = 0
        if char is not None:
            past_end = min(char.get("past_msg_count_at_start", 0), len(non_system))
        past_msgs = non_system[:past_end]
        current_msgs = non_system[past_end:]

        past_tools = self._scan_tool_results(past_msgs)
        current_tools = self._scan_tool_results(current_msgs)

        # Track running max per tool name (for end_character summary)
        for name, sizes in {**past_tools, **current_tools}.items():
            if sizes:
                mx = max(sizes)
                if mx > self._char_tool_max.get(name, 0):
                    self._char_tool_max[name] = mx

        return {
            "system": {
                "total_bytes": _byte_len(system_text),
                "sections": sections,
            },
            "past_sessions": {
                "message_count": len(past_msgs),
                "total_bytes": sum(_byte_len(m) for m in past_msgs),
                "tool_results_by_name": self._tool_stats(past_tools),
            },
            "current_session": {
                "message_count": len(current_msgs),
                "total_bytes": sum(_byte_len(m) for m in current_msgs),
                "tool_results_by_name": self._tool_stats(current_tools),
            },
            "tools": {
                "count": len(tools),
                "bytes": _byte_len(tools),
            },
        }

    @staticmethod
    def _tool_stats(tools_dict: Dict[str, List[int]]) -> Dict[str, Dict[str, int]]:
        return {
            name: {
                "count": len(sizes),
                "total_bytes": sum(sizes),
                "max_bytes": max(sizes) if sizes else 0,
            }
            for name, sizes in tools_dict.items()
        }

    @staticmethod
    def _scan_tool_results(messages: List[Any]) -> Dict[str, List[int]]:
        """Extract tool_result byte sizes grouped by tool name. Provider-aware."""
        out: Dict[str, List[int]] = {}
        id_to_name: Dict[str, str] = {}

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            mtype = msg.get("type")

            # Anthropic assistant tool_use → learn id→name
            if role == "assistant" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id", "")
                        tname = block.get("name", "")
                        if tid:
                            id_to_name[tid] = tname

            # OpenAI/xAI response_output wrapper → learn call_id → name
            if mtype == "response_output":
                for item in msg.get("items", []):
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        cid = item.get("call_id") or item.get("id", "")
                        tname = item.get("name", "")
                        if cid:
                            id_to_name[cid] = tname

            # Anthropic tool_result
            if role == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        tname = id_to_name.get(tid, "_unknown")
                        tr_content = block.get("content", "")
                        if isinstance(tr_content, list):
                            text = "".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in tr_content
                            )
                        else:
                            text = tr_content if isinstance(tr_content, str) else str(tr_content)
                        out.setdefault(tname, []).append(_byte_len(text))

            # OpenAI/xAI function_call_output
            if mtype == "function_call_output":
                cid = msg.get("call_id", "")
                tname = id_to_name.get(cid, "_unknown")
                out.setdefault(tname, []).append(_byte_len(msg.get("output", "")))

            # Google function response
            if role == "function" and isinstance(msg.get("parts"), list):
                for part in msg["parts"]:
                    if isinstance(part, dict):
                        fr = part.get("functionResponse")
                        if isinstance(fr, dict):
                            tname = fr.get("name", "_unknown")
                            resp = fr.get("response", {})
                            result = resp.get("result", resp) if isinstance(resp, dict) else resp
                            out.setdefault(tname, []).append(_byte_len(result))

        return out

    @staticmethod
    def _aggregate_char_totals(char: Dict[str, Any]) -> Dict[str, int]:
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        for t in char.get("turns", []):
            resp = t.get("response") or {}
            for k in totals:
                totals[k] += int(resp.get(k, 0) or 0)
        return totals

    def _aggregate_cycle_totals(self) -> Dict[str, Any]:
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        completed = 0
        end_reasons: List[Optional[str]] = []
        for char in self._data.get("characters", []):
            ct = char.get("totals") or {}
            for k in totals:
                totals[k] += int(ct.get(k, 0) or 0)
            if char.get("ended_at") is not None:
                completed += 1
            end_reasons.append(char.get("end_reason"))
        return {
            **totals,
            "cost_usd_estimate": self._compute_cost(totals),
            "characters_logged": len(self._data.get("characters", [])),
            "characters_completed": completed,
            "end_reasons": end_reasons,
        }

    @staticmethod
    def _compute_cost(totals: Dict[str, int]) -> Dict[str, Any]:
        def _c(tokens: int, rate: float) -> float:
            return round(tokens * rate / 1_000_000, 6)
        cost = {
            "input": _c(totals["input_tokens"], _PRICING["input"]),
            "output": _c(totals["output_tokens"], _PRICING["output"]),
            "cache_read": _c(totals["cache_read_input_tokens"], _PRICING["cache_read"]),
            "cache_creation": _c(totals["cache_creation_input_tokens"], _PRICING["cache_creation"]),
        }
        cost["total"] = round(
            cost["input"] + cost["output"] + cost["cache_read"] + cost["cache_creation"], 6
        )
        cost["pricing_note"] = (
            f"Sonnet 4.6: in=${_PRICING['input']}/MTok "
            f"out=${_PRICING['output']}/MTok "
            f"cache_write=${_PRICING['cache_creation']}/MTok "
            f"cache_read=${_PRICING['cache_read']}/MTok"
        )
        return cost

    def _flush(self) -> None:
        """Atomic write of the current data dict to disk."""
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.output_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(str(tmp_path), str(self.output_path))
        except Exception as e:
            logger.warning(f"[ELYTH CycleLog] flush failed: {e}")
