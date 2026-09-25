"""
backend/server/tailscale.py

Tailscale utilities and SSL certificate management for server mode.
Handles Tailscale IP/hostname retrieval, certificate lifecycle
(acquisition, expiry check, renewal), and startup validation.
"""

import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from backend.shared.i18n import t

logger = logging.getLogger(__name__)

# Project root / certs directory (absolute, CWD-independent).
# NOTE: this module lives at backend/server/, so go up 3 levels to reach the repo root.
CERTS_DIR = Path(__file__).resolve().parent.parent.parent / "certs"

# CLI locations checked when `tailscale` is not on PATH (Mac port plan 0-6).
# macOS: the App Store build keeps its CLI inside the app bundle (never on
# PATH), and applet-launched processes get a minimal PATH anyway; Homebrew
# (arm64) is the other common install. Windows: default installer dir, for
# setups where the installer didn't update PATH.
_TAILSCALE_FALLBACK_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/opt/homebrew/bin/tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
)

_resolved_cli: Optional[str] = None
_warned_not_found = False


def resolve_tailscale_cli() -> str:
    """
    Resolve the tailscale CLI to an absolute path (PATH first, then the
    known install locations). Falls back to the bare name so callers keep
    their existing FileNotFoundError handling. Successful resolution is
    cached; the result is logged once for startup-log diagnosis.
    """
    global _resolved_cli, _warned_not_found
    if _resolved_cli:
        return _resolved_cli
    found = shutil.which("tailscale")
    if not found:
        for candidate in _TAILSCALE_FALLBACK_PATHS:
            if Path(candidate).exists():
                found = candidate
                break
    if found:
        _resolved_cli = found
        logger.info(f"Tailscale CLI resolved: {found}")
        return found
    if not _warned_not_found:
        logger.warning("Tailscale CLI not found (PATH + known locations)")
        _warned_not_found = True
    return "tailscale"


# ---------------------------------------------------------------------------
# Tailscale utilities
# ---------------------------------------------------------------------------

def is_tailscale_installed() -> bool:
    """Tailscale CLI が存在するか(インストール有無の静的判定・subprocess不使用)。

    フールプルーフのグレーアウト条件はこちら(未インストール=UI/トレイで
    サーバーモード切替を無効化)。「インストール済みだが未起動」は稼働状態が
    変動するため静的グレーにせず、押下時の check_tailscale_available() の
    実行時エラーに委ねる(稜裁定 2026-07-25)。resolve_tailscale_cli は
    成功時のみキャッシュするため、アプリ稼働中のインストールは次回解決で
    拾われる。
    """
    return resolve_tailscale_cli() != "tailscale" or shutil.which("tailscale") is not None


def check_tailscale_available() -> bool:
    """
    Check if Tailscale is installed and running.

    Returns:
        True if Tailscale is responsive, False otherwise.
    """
    try:
        result = subprocess.run(
            [resolve_tailscale_cli(), "status"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5
        )
        return result.returncode == 0
    except FileNotFoundError:
        logger.warning("Tailscale CLI not found in PATH")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Tailscale status command timed out")
        return False
    except Exception as e:
        logger.warning(f"Failed to check Tailscale availability: {e}")
        return False


def get_tailscale_ip() -> str:
    """
    Get the Tailscale IPv4 address (e.g. "100.x.x.x").

    Returns:
        Tailscale IPv4 address string.

    Raises:
        RuntimeError: If Tailscale IP cannot be obtained.
    """
    try:
        result = subprocess.run(
            [resolve_tailscale_cli(), "ip", "-4"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5
        )
        stdout = result.stdout or ""
        if result.returncode != 0 or not stdout.strip():
            raise RuntimeError(t('tailscale.ip_failed'))
        ip = stdout.strip()
        logger.info(f"Tailscale IP: {ip}")
        return ip
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(t('tailscale.ip_error', error=e))


def get_tailscale_hostname() -> str:
    """
    Get the Tailscale FQDN (e.g. "machine.tailnet.ts.net").

    Returns:
        Tailscale FQDN without trailing dot.

    Raises:
        RuntimeError: If Tailscale hostname cannot be obtained.
    """
    try:
        result = subprocess.run(
            [resolve_tailscale_cli(), "status", "--json"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10
        )
        stdout = result.stdout or ""
        if result.returncode != 0 or not stdout.strip():
            raise RuntimeError(t('tailscale.hostname_failed',
                                 returncode=result.returncode,
                                 stdout_empty=not stdout.strip()))
        data = json.loads(stdout)
        dns_name = data["Self"]["DNSName"].rstrip(".")
        if not dns_name:
            raise RuntimeError(t('tailscale.dnsname_empty'))
        logger.info(f"Tailscale hostname: {dns_name}")
        return dns_name
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(t('tailscale.status_parse_failed', error=e))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(t('tailscale.hostname_error', error=e))


# ---------------------------------------------------------------------------
# SSL certificate management
# ---------------------------------------------------------------------------

def find_cert_files(hostname: str) -> Optional[Tuple[str, str]]:
    """
    Look for existing certificate files for the given hostname.

    Args:
        hostname: Tailscale FQDN (e.g. "machine.tailnet.ts.net")

    Returns:
        (cert_path, key_path) as strings if both exist, None otherwise.
    """
    cert_path = CERTS_DIR / f"{hostname}.crt"
    key_path = CERTS_DIR / f"{hostname}.key"

    if cert_path.exists() and key_path.exists():
        logger.debug(f"Found cert files: {cert_path}, {key_path}")
        return str(cert_path), str(key_path)

    if cert_path.exists() != key_path.exists():
        logger.warning(
            f"Certificate file mismatch: "
            f"crt={'exists' if cert_path.exists() else 'missing'}, "
            f"key={'exists' if key_path.exists() else 'missing'}"
        )
    return None


def check_cert_expiry(cert_path: str) -> int:
    """
    Check certificate expiry and return days remaining.

    Args:
        cert_path: Path to the .crt file.

    Returns:
        Number of days until expiry (negative if expired).
    """
    from cryptography import x509

    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())

    expiry = cert.not_valid_after_utc
    now = datetime.now(timezone.utc)
    delta = expiry - now
    days_remaining = delta.days

    logger.info(
        f"Certificate expiry: {expiry.strftime('%Y-%m-%d')} "
        f"({days_remaining} days remaining)"
    )
    return days_remaining


# Retry policy for ``tailscale cert`` (2026-08-04, Mac first-run finding):
# a first-ever issuance can fail transiently (the Let's Encrypt ACME order
# goes invalid); a freshly created order usually succeeds. Only fast
# non-zero exits are retried — timeouts (already 30s) and exec errors
# (CLI missing) cannot heal within seconds and stay fail-fast.
_CERT_ATTEMPTS = 3
_CERT_RETRY_WAIT_SEC = 5

# UI が注入する進捗コールバックの型。dict イベント
# {"phase": "attempt"|"retry_wait", "attempt", "total", "wait_sec"} を受ける。
CertProgress = Callable[[dict], None]


def _notify_cert_progress(progress: Optional[CertProgress],
                          phase: str, attempt: int) -> None:
    """進捗コールバックへの通知。コールバック側の不具合で証明書取得を
    殺さない(失敗はデバッグログのみ)。"""
    if progress is None:
        return
    try:
        progress({
            "phase": phase,
            "attempt": attempt,
            "total": _CERT_ATTEMPTS,
            "wait_sec": _CERT_RETRY_WAIT_SEC,
        })
    except Exception as e:
        logger.debug(f"cert progress callback failed: {e}")


def run_tailscale_cert(hostname: str,
                       progress: Optional[CertProgress] = None) -> Tuple[str, str]:
    """
    Run ``tailscale cert`` to obtain/renew SSL certificates.

    Transient failures (non-zero exit) are retried up to _CERT_ATTEMPTS
    times, _CERT_RETRY_WAIT_SEC seconds apart. Each attempt start / retry
    wait is reported to the injected ``progress`` callback (if any).

    Args:
        hostname: Tailscale FQDN (e.g. "machine.tailnet.ts.net")
        progress: optional CertProgress callback for UI progress display.

    Returns:
        (cert_path, key_path) as strings.

    Raises:
        RuntimeError: If certificate acquisition fails.
    """
    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    cert_path = CERTS_DIR / f"{hostname}.crt"
    key_path = CERTS_DIR / f"{hostname}.key"

    for attempt in range(1, _CERT_ATTEMPTS + 1):
        logger.info(
            f"Running tailscale cert for {hostname} "
            f"(attempt {attempt}/{_CERT_ATTEMPTS})..."
        )
        _notify_cert_progress(progress, "attempt", attempt)
        try:
            result = subprocess.run(
                [
                    resolve_tailscale_cli(), "cert",
                    f"--cert-file={cert_path}",
                    f"--key-file={key_path}",
                    hostname,
                ],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30
            )
        except subprocess.TimeoutExpired:
            logger.error(
                f"tailscale cert timed out after 30s "
                f"(attempt {attempt}/{_CERT_ATTEMPTS}) for {hostname}"
            )
            raise RuntimeError(t('tailscale.cert_timeout'))
        except Exception as e:
            logger.error(f"tailscale cert execution failed for {hostname}: {e}")
            raise RuntimeError(t('tailscale.cert_exec_error', error=e))
        if result.returncode == 0:
            break
        stderr = (result.stderr or "").strip()
        if attempt < _CERT_ATTEMPTS:
            logger.warning(
                f"tailscale cert attempt {attempt}/{_CERT_ATTEMPTS} failed "
                f"(exit code {result.returncode}); retrying in "
                f"{_CERT_RETRY_WAIT_SEC}s: {stderr}"
            )
            _notify_cert_progress(progress, "retry_wait", attempt)
            time.sleep(_CERT_RETRY_WAIT_SEC)
            continue
        logger.error(
            f"tailscale cert failed after {_CERT_ATTEMPTS} attempts "
            f"(exit code {result.returncode}): {stderr}"
        )
        raise RuntimeError(t('tailscale.cert_failed',
                             returncode=result.returncode, stderr=stderr))

    if not cert_path.exists() or not key_path.exists():
        raise RuntimeError(t('tailscale.cert_missing',
                             cert_path=cert_path, key_path=key_path))

    logger.info(f"Certificate acquired: {cert_path}")
    return str(cert_path), str(key_path)


def ensure_valid_cert(progress: Optional[CertProgress] = None) -> Tuple[str, str]:
    """
    Ensure a valid SSL certificate is available for server mode.

    Orchestration function called at server-mode startup:
      - No cert  → acquire via ``tailscale cert``
      - <30 days → attempt renewal
      - >=30 days → use as-is

    Args:
        progress: optional CertProgress callback, passed through to
            run_tailscale_cert (no events fire when the cached cert is used).

    Returns:
        (certfile_path, keyfile_path) as strings.

    Raises:
        RuntimeError: If certificate cannot be obtained at all.
    """
    hostname = get_tailscale_hostname()
    existing = find_cert_files(hostname)

    if existing is None:
        # No certificate — first-time acquisition
        logger.info("No existing certificate found. Acquiring new certificate...")
        return run_tailscale_cert(hostname, progress)

    cert_path, key_path = existing
    days_remaining = check_cert_expiry(cert_path)

    if days_remaining >= 30:
        logger.info(f"Certificate is valid ({days_remaining} days remaining)")
        return cert_path, key_path

    # Less than 30 days remaining — attempt renewal
    logger.info(
        f"Certificate expires in {days_remaining} days. "
        "Attempting renewal..."
    )
    try:
        return run_tailscale_cert(hostname, progress)
    except RuntimeError as e:
        if days_remaining > 0:
            # Existing cert is still valid — continue with warning
            logger.warning(
                f"Certificate renewal failed, but existing cert is still valid "
                f"({days_remaining} days remaining): {e}"
            )
            return cert_path, key_path
        else:
            # Expired and renewal failed — cannot continue
            raise
