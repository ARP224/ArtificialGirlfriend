"""
backend/command_security.py

Security gate for AI character command execution.
Whitelist + greylist approach: anything not explicitly listed is blocked.

Judgment order:
1. AG self-protection (processes + paths)
2. Redirect detection (> >>)
3. Operator splitting (| & && ||) — mechanical, no quote awareness
4. Command type classification (NirCMD / CMD / other)
5. Default: block
"""

import re
import logging
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional

from backend.shared.constants import BASE_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Security verdict
# ---------------------------------------------------------------------------

class SecurityVerdict(Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


@dataclass
class SecurityResult:
    verdict: SecurityVerdict
    reason: str


def _reason(key: str, language: str, **kwargs) -> str:
    """SecurityResult.reason はツール結果(res.command.blocked の {block_reason})
    としてLLMに届くモデル向け文字列 — カタログ(sec.*)で言語化する。"""
    from backend.shared.prompt_i18n import prompt_text
    return prompt_text(key, language, **kwargs)


# ---------------------------------------------------------------------------
# CMD internal commands
# ---------------------------------------------------------------------------

CMD_WHITELIST = frozenset({"dir", "tasklist"})
CMD_GREYLIST = frozenset({"taskkill"})

# All 78 CMD internal commands (from `help`).  Only 3 above are allowed.
CMD_ALL_INTERNAL = frozenset({
    "assoc", "attrib", "break", "bcdedit", "cacls", "call", "cd", "chcp",
    "chdir", "chkdsk", "chkntfs", "cls", "cmd", "color", "comp", "compact",
    "convert", "copy", "date", "del", "dir", "diskpart", "doskey", "driverquery",
    "echo", "endlocal", "erase", "exit", "fc", "find", "findstr", "for",
    "format", "fsutil", "ftype", "goto", "gpresult", "graftabl", "help",
    "icacls", "if", "label", "md", "mkdir", "mklink", "mode", "more", "move",
    "openfiles", "path", "pause", "popd", "print", "prompt", "pushd", "rd",
    "recover", "rem", "ren", "rename", "replace", "rmdir", "robocopy", "set",
    "setlocal", "sc", "schtasks", "shift", "shutdown", "sort", "start",
    "subst", "systeminfo", "taskkill", "tasklist", "time", "title", "tree",
    "type", "ver", "verify", "vol", "xcopy", "wmic",
})


# ---------------------------------------------------------------------------
# NirCMD commands
# ---------------------------------------------------------------------------

NIRCMD_WHITELIST = frozenset({
    # Volume control
    "setvolume", "setsysvolume", "setsysvolume2",
    "changesysvolume", "changesysvolume2", "mutesysvolume",
    "setappvolume", "changeappvolume", "muteappvolume",
    "mutesubunitvolume", "setsubunitvolumedb",
    "showsounddevices", "setdefaultsounddevice",
    # Notifications
    "infobox", "qbox", "qboxtop",
    "trayballoon",
    # Sound
    "beep", "stdbeep", "mediaplay",
    # Control
    "wait",
})

NIRCMD_GREYLIST = frozenset({
    # System power / session
    "exitwin", "hibernate", "standby", "lockws",
    # Monitor
    "monitor", "setbrightness", "changebrightness",
    # Screenshot
    "savescreenshot", "savescreenshotfull", "savescreenshotwin",
    # Cursor
    "movecursor", "setcursor", "setcursorwin",
    # Other
    "cdrom", "screensaver",
})

# NirCMD `win` sub-actions
NIRCMD_WIN_WHITELIST = frozenset({
    "activate", "focus", "flash", "max", "min", "normal",
    "togglemin", "togglemax", "redraw", "show", "hide",
    "hideshow", "togglehide", "setsize", "move", "center",
    "trans", "settopmost", "settext",
})

NIRCMD_WIN_GREYLIST = frozenset({
    "close", "disable", "enable", "toggledisable",
})


# ---------------------------------------------------------------------------
# TASKKILL system process protection
# ---------------------------------------------------------------------------

PROTECTED_SYSTEM_PROCESSES = frozenset({
    "csrss.exe", "smss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
    "explorer.exe", "system", "registry",
})


# ---------------------------------------------------------------------------
# AG self-protection
# ---------------------------------------------------------------------------

AG_PROTECTED_PROCESSES = frozenset({
    "python.exe", "pythonw.exe",
    "ollama.exe", "ollama_llama_server.exe",
})

AG_PROTECTED_PROCESS_PATTERNS = [
    re.compile(r"style.?bert", re.IGNORECASE),
]

# The AG folder itself, wherever it was cloned (install location is free;
# a hardcoded C:\ArtificialGirlfriend only protected that one location)
AG_PROTECTED_PATHS = [
    str(BASE_DIR),
]


# ---------------------------------------------------------------------------
# Operator / redirect patterns
# ---------------------------------------------------------------------------

_REDIRECT_PATTERN = re.compile(r"(?<!\w)>>?(?!\w)")
_PIPE_AND_PATTERN = re.compile(r"\|{1,2}|&&?")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(s: str) -> str:
    """Strip and collapse whitespace."""
    return " ".join(s.split())


def _split_by_operators(command: str) -> List[str]:
    """Split command string by |, &, &&, || operators.
    Mechanical split — no quote awareness (by design)."""
    parts = _PIPE_AND_PATTERN.split(command)
    return [p.strip() for p in parts if p.strip()]


def _extract_command_name(part: str) -> str:
    """Extract the first token (command name) from a command part."""
    tokens = part.split()
    return tokens[0].lower() if tokens else ""


def _check_ag_protection(command: str, language: str) -> Optional[SecurityResult]:
    """Check if command targets AG-protected processes or paths."""
    cmd_lower = command.lower()

    # Process protection
    for proc in AG_PROTECTED_PROCESSES:
        if proc.lower() in cmd_lower:
            return SecurityResult(
                SecurityVerdict.BLOCK,
                _reason("sec.ag_process", language, name=proc)
            )

    for pattern in AG_PROTECTED_PROCESS_PATTERNS:
        if pattern.search(cmd_lower):
            return SecurityResult(
                SecurityVerdict.BLOCK,
                _reason("sec.ag_process", language, name="Style-BERT-VITS2")
            )

    # Path protection
    for path in AG_PROTECTED_PATHS:
        if path.lower() in cmd_lower:
            return SecurityResult(
                SecurityVerdict.BLOCK,
                _reason("sec.ag_path", language, path=path)
            )

    return None


def _check_taskkill_target(command: str, language: str) -> Optional[SecurityResult]:
    """Check if TASKKILL targets a system-critical or AG-protected process."""
    cmd_lower = command.lower()

    # Check system-critical processes
    for proc in PROTECTED_SYSTEM_PROCESSES:
        if proc.lower() in cmd_lower:
            return SecurityResult(
                SecurityVerdict.BLOCK,
                _reason("sec.system_process", language, name=proc)
            )

    # Check AG processes (already covered by _check_ag_protection,
    # but explicit for TASKKILL specificity)
    for proc in AG_PROTECTED_PROCESSES:
        if proc.lower() in cmd_lower:
            return SecurityResult(
                SecurityVerdict.BLOCK,
                _reason("sec.ag_process", language, name=proc)
            )

    for pattern in AG_PROTECTED_PROCESS_PATTERNS:
        if pattern.search(cmd_lower):
            return SecurityResult(
                SecurityVerdict.BLOCK,
                _reason("sec.ag_process", language, name="Style-BERT-VITS2")
            )

    return None


def _classify_single_part(part: str, language: str) -> SecurityResult:
    """Classify a single command part (no operators)."""
    part = _normalise(part)
    if not part:
        return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.empty_command", language))

    cmd_name = _extract_command_name(part)

    # --- NirCMD ---
    if cmd_name == "nircmd" or cmd_name == "nircmd.exe" or cmd_name == "nircmdc" or cmd_name == "nircmdc.exe":
        tokens = part.split()
        if len(tokens) < 2:
            return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.nircmd_no_subcommand", language))

        subcmd = tokens[1].lower()

        # nircmd win <action> ...
        if subcmd == "win":
            if len(tokens) < 3:
                return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.nircmd_win_no_action", language))
            action = tokens[2].lower()
            if action in NIRCMD_WIN_WHITELIST:
                return SecurityResult(SecurityVerdict.ALLOW, _reason("sec.nircmd_win_whitelist", language, action=action))
            elif action in NIRCMD_WIN_GREYLIST:
                return SecurityResult(SecurityVerdict.REQUIRE_APPROVAL, _reason("sec.nircmd_win_approval", language, action=action))
            else:
                return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.nircmd_win_not_allowed", language, action=action))

        # Normal nircmd commands
        if subcmd in NIRCMD_WHITELIST:
            return SecurityResult(SecurityVerdict.ALLOW, _reason("sec.nircmd_whitelist", language, subcmd=subcmd))
        elif subcmd in NIRCMD_GREYLIST:
            return SecurityResult(SecurityVerdict.REQUIRE_APPROVAL, _reason("sec.nircmd_approval", language, subcmd=subcmd))
        else:
            return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.nircmd_not_allowed", language, subcmd=subcmd))

    # --- CMD internal commands ---
    if cmd_name in CMD_WHITELIST:
        return SecurityResult(SecurityVerdict.ALLOW, _reason("sec.cmd_whitelist", language, name=cmd_name))

    if cmd_name in CMD_GREYLIST:
        # TASKKILL: additional system process check
        if cmd_name == "taskkill":
            block = _check_taskkill_target(part, language)
            if block:
                return block
        return SecurityResult(SecurityVerdict.REQUIRE_APPROVAL, _reason("sec.cmd_approval", language, name=cmd_name))

    if cmd_name in CMD_ALL_INTERNAL:
        return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.cmd_not_allowed", language, name=cmd_name))

    # --- PowerShell ---
    if cmd_name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.powershell_blocked", language))

    # --- Everything else ---
    return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.other_not_allowed", language, name=cmd_name))


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def check_command(command: str, language: str) -> SecurityResult:
    """
    Main security check entry point.

    Args:
        command: The raw command string from LLM tool_call.
        language: Prompt language for the reason strings — the reason reaches
            the LLM as part of the tool result (res.command.blocked).

    Returns:
        SecurityResult with verdict (ALLOW / REQUIRE_APPROVAL / BLOCK)
        and a human-readable reason.
    """
    command = command.strip()
    if not command:
        return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.empty_command", language))

    # 1. AG self-protection
    ag_check = _check_ag_protection(command, language)
    if ag_check:
        logger.info(f"Command blocked by AG protection: {command}")
        return ag_check

    # 2. Redirect detection (> >>)
    has_redirect = bool(_REDIRECT_PATTERN.search(command))

    # 3. Operator splitting (| & && ||)
    parts = _split_by_operators(command)
    if not parts:
        return SecurityResult(SecurityVerdict.BLOCK, _reason("sec.parse_failed", language))

    # 4. Classify each part
    overall_verdict = SecurityVerdict.ALLOW
    overall_reason_parts = []

    for part in parts:
        result = _classify_single_part(part, language)

        if result.verdict == SecurityVerdict.BLOCK:
            # Any blocked part → entire command blocked
            logger.info(f"Command blocked: {command} (part: {part}, reason: {result.reason})")
            return SecurityResult(
                SecurityVerdict.BLOCK,
                result.reason
            )

        if result.verdict == SecurityVerdict.REQUIRE_APPROVAL:
            overall_verdict = SecurityVerdict.REQUIRE_APPROVAL

        overall_reason_parts.append(result.reason)

    # 5. Redirect forces approval
    if has_redirect and overall_verdict == SecurityVerdict.ALLOW:
        overall_verdict = SecurityVerdict.REQUIRE_APPROVAL
        overall_reason_parts.append(_reason("sec.redirect_approval", language))

    reason = "; ".join(overall_reason_parts)
    logger.info(f"Command security check: {command} → {overall_verdict.value} ({reason})")
    return SecurityResult(overall_verdict, reason)
