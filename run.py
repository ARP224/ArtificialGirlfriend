#!/usr/bin/env python
"""
run.py

Main launcher script for Artificial Girlfriend application.
This script allows the application to be started from the root directory
while properly handling module imports.

Usage:
    python run.py
"""

import json
import sys
import os


def _force_utf8_stdio() -> None:
    """
    Reconfigure console streams to UTF-8 for direct `python run.py` runs.

    The tray launcher injects PYTHONIOENCODING=utf-8 when spawning this
    process, but a manual launch on a non-UTF-8 console (cp437/cp1252 on
    non-Japanese Windows) would make the stdout log handler choke on
    Japanese log lines. Streams may be None under pythonw.exe.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (stream.encoding or '').lower() not in ('utf-8', 'utf8'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass  # never block startup over console encoding


_force_utf8_stdio()


def _apply_launcher_env() -> None:
    """
    Apply environment overrides from launch_config.json (launcher.env).

    Machine-specific GPU pinning (CUDA_VISIBLE_DEVICES etc.) lives in the
    user's launch_config.json, not in the repo. Must run before any
    CUDA/PyTorch import, so the file is read with stdlib json directly —
    importing backend.* here would pull heavy modules first.

    Values already present in the environment win (the tray launcher
    injects the same settings when spawning this process).
    """
    # settings_store.get_settings_dir と同型のOS分岐(stdlibのみ・重い
    # importを避けるためここでは複製)。nt=%APPDATA%、他=~/.config。
    if os.name == 'nt':
        base = os.environ.get('APPDATA')
        if not base:
            return
    else:
        base = os.path.join(os.path.expanduser('~'), '.config')
    # 正式綴り優先・旧綴り(Airtificial)ディレクトリはフォールバック
    # (移行は settings_store / tray が行う)
    for dirname in ('ArtificialGirlfriend', 'AirtificialGirlfriend'):
        config_path = os.path.join(base, dirname, 'launch_config.json')
        try:
            with open(config_path, 'r', encoding='utf-8-sig') as f:
                env = json.load(f).get('launcher', {}).get('env', {})
            for key, value in env.items():
                os.environ.setdefault(key, str(value))
            return
        except (OSError, ValueError):
            continue


_apply_launcher_env()

# Gradio の利用統計送信(api.gradio.app への initiated/launched 送信+
# バージョン確認)を無効化。機能には無関係で、AGは「API+Tailscale以外は
# 通信しない」仕様(稜裁定 2026-07-19)。setdefault なので環境変数で明示
# 上書きされていればそちらが勝つ。
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

# Add the current directory to Python path to ensure proper imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import and run the main function from ui.app
# (env overrides above must run before this import pulls in CUDA/torch)
from ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()