"""Hand-written golden snapshot helper.

Design goals (decision 2026-06-20):
- Golden files are plain *.txt under tests/golden/snapshots/ so 稜 can read
  diffs directly during review ("スナップショットを実際に開く").
- Regenerate with UPDATE_GOLDEN=1; otherwise compare byte-for-byte.
- First run (file missing) writes the snapshot. Honesty against "嘘の緑" is
  enforced by the author *opening and inspecting* the generated file before
  declaring green — not by the helper.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).parent / "golden" / "snapshots"


def _should_update() -> bool:
    return os.environ.get("UPDATE_GOLDEN", "") not in ("", "0", "false", "False")


def _assert_text(path: Path, content: str, name: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if _should_update() or not path.exists():
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    expected = path.read_text(encoding="utf-8")
    assert content == expected, (
        f"Golden mismatch for {name!r}.\n"
        f"Snapshot: {path}\n"
        f"Re-run with UPDATE_GOLDEN=1 to regenerate (only after confirming the change is intended)."
    )


def assert_golden(name: str, content: str) -> None:
    """Compare ``content`` against tests/golden/snapshots/<name>.txt.

    Writes the file when it is missing or when UPDATE_GOLDEN is set; otherwise
    asserts an exact (byte-for-byte) match.
    """
    _assert_text(SNAPSHOT_DIR / f"{name}.txt", content, name)


def assert_golden_json(name: str, obj) -> None:
    """Compare ``obj`` against tests/golden/snapshots/<name>.json (byte-exact).

    Serialised with insertion order preserved (NOT sorted) so a reordering of
    keys — e.g. an accidental swap inside a provider-specific transform — is
    caught, and ``ensure_ascii=False`` so Japanese descriptions stay readable in
    diffs during review. Used by the tool-schema golden.
    """
    content = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    _assert_text(SNAPSHOT_DIR / f"{name}.json", content, name)


def render_trace(events: list, result: dict) -> str:
    """Render a control-flow trace (ordered events + final result) as a
    deterministic, human-readable text block. Shared by the trace golden test
    and the bug-injection meta test (so both compare byte-identically)."""
    out: list[str] = ["===== events ====="]
    for ev in events:
        kind = ev["event"]
        if kind == "enqueue":
            out.append(f"[enqueue] task={ev['task']}")
        elif kind == "tts":
            out.append(f"[tts] {ev['text']}")
        elif kind == "ui_update":
            data = ev["data"]
            fields = " ".join(f"{k}={data[k]!r}" for k in sorted(data))
            out.append(f"[ui_update] {ev['type']} | {fields}")
        else:  # guards against silently dropping a new event kind
            raise AssertionError(f"unknown trace event {kind!r}; update render_trace")

    out.append("")
    out.append("===== result =====")
    out.append(f"success={result.get('success')}")
    out.append(f"response={result.get('response')!r}")
    if "error_type" in result:
        out.append(f"error_type={result['error_type']}")
    steps = result.get("steps")
    if steps is None:
        out.append("steps: (none)")
    else:
        out.append("steps:")
        for s in steps:
            stype = s.get("type")
            extra = {k: v for k, v in s.items() if k not in ("type", "pre_displayed")}
            extra_str = " ".join(f"{k}={extra[k]!r}" for k in sorted(extra))
            out.append(f"  - {stype}: {extra_str}")
    out.append("")
    return "\n".join(out)


def render_messages(messages: list[dict]) -> str:
    """Render the ``messages`` list returned by ``_build_prompt_messages`` into a
    deterministic, human-readable text block.

    Lossless-by-construction: every message must consist only of known keys
    (role/content/images/documents). An unknown key raises, forcing the renderer
    to be updated rather than silently dropping data (anti-"嘘の緑").
    """
    known = {"role", "content", "images", "documents"}
    out: list[str] = []
    for i, msg in enumerate(messages):
        unknown = set(msg.keys()) - known
        if unknown:
            raise AssertionError(
                f"message[{i}] has unexpected keys {sorted(unknown)}; "
                f"update tests/_snapshot.render_messages to render them."
            )
        out.append(f"===== message[{i}] role={msg.get('role', '<none>')} =====")
        out.append(msg.get("content", ""))
        images = msg.get("images")
        if images:
            out.append("----- images -----")
            for p in images:
                # Render basename only: image paths must be absolute+real on disk
                # for the existence filter (conversation_manager.py:2593) to keep
                # them, but absolute paths are machine-specific. Basename keeps the
                # snapshot deterministic while still showing which/how many images.
                out.append(Path(p).name)
        documents = msg.get("documents")
        if documents:
            out.append("----- documents -----")
            for d in documents:
                out.append(repr(d))
        out.append("")  # trailing blank line between messages
    return "\n".join(out)
