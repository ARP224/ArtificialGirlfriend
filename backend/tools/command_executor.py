"""
backend/command_executor.py

Command execution engine and logging for AI character command feature.
Executes commands on the host OS via subprocess with timeout and output limits.
"""

import logging
import os
import subprocess
import time
import shutil
from dataclasses import dataclass
from typing import Optional, List, Dict

from backend.tools.tool_schemas import format_tools_for_provider, localize_tools
from threading import Lock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMAND_TIMEOUT = 30  # seconds
MAX_TOOL_RESULT_LENGTH = 5000


def _oem_encoding() -> str:
    """Console (OEM) codepage of this Windows system, e.g. cp932 / cp437."""
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp932"  # last resort: previous hardcoded behaviour

# ---------------------------------------------------------------------------
# Tool definition (provider-agnostic)
# ---------------------------------------------------------------------------

EXECUTE_COMMAND_TOOL = {
    "name": "execute_command",
    "description": (
        "PCに対してコマンドを実行する。感情の表現、イタズラ、心配、甘えなど、"
        "キャラクターとしての行動をPCを通じて表す。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "実行するコマンド（Windows CMD / NirCMD）"
            },
            "reason": {
                "type": "string",
                "description": "なぜこのコマンドを実行したいか（感情や動機）。ユーザー確認UIで表示される。"
            }
        },
        "required": ["command", "reason"]
    }
}


def get_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools([EXECUTE_COMMAND_TOOL], language))


# ---------------------------------------------------------------------------
# Tool result templates
# ---------------------------------------------------------------------------

# ステータス名→カタログキー。文言はカタログ(res.command.*)が真実源。
_RESULT_STATUSES = frozenset({"blocked", "pending", "denied", "executed", "error", "interrupted"})


def format_tool_result_text(status: str, language: str, **kwargs) -> str:
    """Format a tool result string from the prompt catalog."""
    if status not in _RESULT_STATUSES:
        return f"UNKNOWN STATUS: {status}"
    from backend.shared.prompt_i18n import prompt_text
    return prompt_text(f"res.command.{status}", language, **kwargs)


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    success: bool
    output: str = ""
    error_message: str = ""
    return_code: Optional[int] = None
    timed_out: bool = False


def truncate_result(output: str, language: str) -> str:
    """Truncate output to MAX_TOOL_RESULT_LENGTH characters."""
    if len(output) > MAX_TOOL_RESULT_LENGTH:
        from backend.shared.prompt_i18n import prompt_text
        return output[:MAX_TOOL_RESULT_LENGTH] + "\n" + prompt_text("res.command.truncated_suffix", language)
    return output


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

def execute_command(command: str, language: str) -> CommandResult:
    """
    Execute a command on the host OS.

    Uses subprocess.run with:
    - shell=True (for CMD internal commands)
    - timeout=30s
    - capture_output=True
    - text=True with utf-8 + error handling

    Returns:
        CommandResult with output, error, return code, timeout flag.
    """
    # Check if nircmd is needed and available
    cmd_lower = command.strip().lower()
    if cmd_lower.startswith("nircmd"):
        nircmd_path = shutil.which("nircmd") or shutil.which("nircmdc")
        # Also check AG-local nircmd-x64 folder
        if not nircmd_path:
            # This module lives at backend/tools/, so go up 3 levels to reach the repo root.
            ag_nircmd = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "nircmd-x64", "nircmd.exe")
            if os.path.isfile(ag_nircmd):
                nircmd_path = ag_nircmd
                # Rewrite command to use full path.
                # 先頭トークンは command_security が許可する別名
                # (nircmd / nircmdc / nircmd.exe / nircmdc.exe)のいずれか。
                # 旧実装は常に先頭6文字("nircmd")だけ剥がしていたため、
                # nircmdc → "...nircmd.exec"、nircmd.exe → "...nircmd.exe.exe" と
                # 不正なパスに書き換わっていた。トークン全体を置換する。
                stripped = command.strip()
                first_token = stripped.split(None, 1)[0]
                # フルパスは shell=True 実行なので引用符で囲む。現行の空白なし
                # パスでは動くが、空白を含む場所へ移設した瞬間に壊れる。
                command = f'"{ag_nircmd}"' + stripped[len(first_token):]
        if not nircmd_path:
            from backend.shared.prompt_i18n import prompt_text
            return CommandResult(
                success=False,
                error_message=prompt_text("res.command.nircmd_missing", language),
            )

    logger.info(f"Executing command: {command}")
    try:
        # Windows CMD outputs in the system's OEM codepage (cp932 on Japanese
        # Windows, cp437 on English, ...). Resolve it at runtime instead of
        # hardcoding cp932 — with errors="replace" a hardcoded wrong codepage
        # never raises, it just silently mojibakes the output fed to the LLM.
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            encoding=_oem_encoding(),
            errors="replace",
        )

        from backend.shared.prompt_i18n import prompt_text
        stdout = truncate_result(result.stdout, language) if result.stdout else ""
        stderr = truncate_result(result.stderr, language) if result.stderr else ""

        if result.returncode == 0:
            output = stdout or prompt_text("res.command.no_output", language)
            logger.info(f"Command executed successfully: {command} (rc={result.returncode})")
            return CommandResult(
                success=True,
                output=output,
                return_code=result.returncode,
            )
        else:
            error_msg = stderr or stdout or prompt_text("res.command.exit_code", language, code=result.returncode)
            logger.warning(f"Command failed: {command} (rc={result.returncode}, err={stderr[:200]})")
            return CommandResult(
                success=False,
                output=stdout,
                error_message=error_msg,
                return_code=result.returncode,
            )

    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out ({COMMAND_TIMEOUT}s): {command}")
        from backend.shared.prompt_i18n import prompt_text
        return CommandResult(
            success=False,
            error_message=prompt_text("res.command.timeout", language, seconds=COMMAND_TIMEOUT),
            timed_out=True,
        )
    except Exception as e:
        logger.error(f"Command execution error: {command} — {e}")
        return CommandResult(
            success=False,
            error_message=str(e),
        )


# ---------------------------------------------------------------------------
# Command log
# ---------------------------------------------------------------------------

@dataclass
class CommandLogEntry:
    timestamp: float
    command: str
    reason: str
    verdict: str  # "executed", "blocked", "denied", "interrupted", "error"
    result_summary: str
    character_id: str


class CommandLog:
    """In-memory command execution log (per session)."""

    MAX_ENTRIES = 500

    def __init__(self):
        self._entries: List[CommandLogEntry] = []
        self._lock = Lock()

    def add(self, command: str, reason: str, verdict: str,
            result_summary: str, character_id: str) -> None:
        """Add a log entry."""
        entry = CommandLogEntry(
            timestamp=time.time(),
            command=command,
            reason=reason,
            verdict=verdict,
            result_summary=result_summary[:500],  # Keep summary short
            character_id=character_id,
        )
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = self._entries[-self.MAX_ENTRIES:]

    def get_entries(self, limit: int = 50) -> List[CommandLogEntry]:
        """Get recent log entries."""
        with self._lock:
            return list(self._entries[-limit:])

    def format_for_display(self, limit: int = 50) -> str:
        """Format log entries for UI display."""
        entries = self.get_entries(limit)
        if not entries:
            return "No command executions logged yet."

        lines = []
        for entry in reversed(entries):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.timestamp))
            icon = {
                "executed": "[OK]",
                "blocked": "[BLOCKED]",
                "denied": "[DENIED]",
                "interrupted": "[INTERRUPTED]",
                "error": "[ERROR]",
            }.get(entry.verdict, "[?]")

            lines.append(f"{ts} {icon} {entry.command}")
            lines.append(f"  Reason: {entry.reason}")
            if entry.result_summary:
                summary = entry.result_summary.replace("\n", "\n  ")
                lines.append(f"  Result: {summary}")
            lines.append("")

        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all log entries."""
        with self._lock:
            self._entries.clear()


# Global command log instance
_command_log: Optional[CommandLog] = None


def get_command_log() -> CommandLog:
    """Get the global command log instance."""
    global _command_log
    if _command_log is None:
        _command_log = CommandLog()
    return _command_log
