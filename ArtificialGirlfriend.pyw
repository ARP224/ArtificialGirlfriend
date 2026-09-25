"""
ArtificialGirlfriend.pyw — double-click entry point (no console).

Boots the tray launcher (launcher/tray_app.py) with the repo's venv
pythonw and exits immediately. The tray launcher itself handles the
already-running cases (focus the existing front / signal the running
tray instance), so this file stays dumb on purpose.

Runs under whatever Python .pyw is associated with (system pyw.exe),
so it must remain stdlib-only and version-tolerant.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHONW = ROOT / 'venv' / 'Scripts' / 'pythonw.exe'
TRAY_APP = ROOT / 'launcher' / 'tray_app.py'


def fatal(message):
    """No console exists — surface fatal errors via a message box."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, 'Artificial Girlfriend', 0x10)
    sys.exit(1)


if not PYTHONW.exists():
    fatal('venv が見つかりません:\n{}\n\n'
          'README のセットアップ手順に従って venv を作成してください。'.format(PYTHONW))
if not TRAY_APP.exists():
    fatal('トレイランチャーが見つかりません:\n{}'.format(TRAY_APP))

subprocess.Popen(
    [str(PYTHONW), str(TRAY_APP), '--open-front'],
    cwd=str(ROOT),
    close_fds=True,
)
