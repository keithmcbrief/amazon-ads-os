#!/usr/bin/env python3
"""Idempotent venv + dependency bootstrap for amazon-ads-os.

Uses ONLY the Python standard library so it works before any deps are
installed. Called from the SessionStart hook so dependency setup happens
silently on first use, and again only when requirements.txt changes.

Inputs (positional args):
  argv[1]  CLAUDE_PLUGIN_ROOT  (where the plugin files live)
  argv[2]  CLAUDE_PLUGIN_DATA  (writable persistent storage for this plugin)

Side effects:
  Creates  ${CLAUDE_PLUGIN_DATA}/.venv/
  Copies   ${CLAUDE_PLUGIN_ROOT}/requirements.txt
              → ${CLAUDE_PLUGIN_DATA}/requirements.lock

Exit codes:
  0  ok, venv ready (no-op if up to date)
  1  hard failure (no Python, no venv module, network down, etc.) —
     prints a plain-English fix and exits.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)


def _err(msg: str) -> int:
    sys.stderr.write(f"[amazon-ads-os] {msg}\n")
    return 1


def _info(msg: str) -> None:
    sys.stderr.write(f"[amazon-ads-os] {msg}\n")


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python3"


def _check_python_version() -> int:
    if sys.version_info < MIN_PYTHON:
        return _err(
            f"need Python {'.'.join(map(str, MIN_PYTHON))}+ to bootstrap; "
            f"found {sys.version.split()[0]}. Install a newer Python from python.org or Homebrew, "
            f"then restart Claude Code."
        )
    return 0


def _venv_module_available() -> bool:
    """Some minimal Debian/Ubuntu Pythons don't ship the venv module."""
    try:
        import venv  # noqa: F401
        return True
    except ImportError:
        return False


def _create_venv(venv_dir: Path) -> int:
    if not _venv_module_available():
        return _err(
            "the venv module isn't available in this Python install. "
            "On Debian/Ubuntu: sudo apt install python3-venv. "
            "Then restart Claude Code."
        )
    _info(f"creating venv at {venv_dir}")
    try:
        # with_pip=True ensures pip is bootstrapped inside the venv (PEP 668-safe)
        import venv as _venv
        builder = _venv.EnvBuilder(with_pip=True, clear=False, upgrade=False)
        builder.create(str(venv_dir))
    except Exception as e:
        return _err(f"venv creation failed: {e}")
    return 0


def _install_requirements(venv_dir: Path, requirements: Path) -> int:
    pyexe = _venv_python(venv_dir)
    if not pyexe.exists():
        return _err(f"venv python missing at {pyexe}")
    _info(f"installing requirements from {requirements}")
    try:
        # Upgrade pip first so requests resolves cleanly on older interpreters
        subprocess.run(
            [str(pyexe), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            check=True, capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            [str(pyexe), "-m", "pip", "install", "--quiet", "-r", str(requirements)],
            check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        return _err(
            f"pip install failed (exit {e.returncode}). "
            f"stderr: {(e.stderr or '').strip()[:500]}. "
            f"If you're behind a proxy or offline, set up Python access and rerun /amazon-doctor."
        )
    except subprocess.TimeoutExpired:
        return _err("pip install timed out. Check your network and rerun /amazon-doctor.")
    return 0


def _venv_is_healthy(venv_dir: Path, requirements: Path, lock: Path) -> bool:
    pyexe = _venv_python(venv_dir)
    if not pyexe.exists():
        return False
    if not lock.exists():
        return False
    try:
        if lock.read_bytes() != requirements.read_bytes():
            return False
    except OSError:
        return False
    # Cheap import check
    try:
        result = subprocess.run(
            [str(pyexe), "-c", "import requests"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return _err("usage: bootstrap.py <CLAUDE_PLUGIN_ROOT> <CLAUDE_PLUGIN_DATA>")

    plugin_root = Path(argv[1]).resolve()
    plugin_data = Path(argv[2]).resolve()

    requirements = plugin_root / "requirements.txt"
    if not requirements.exists():
        return _err(f"missing requirements.txt at {requirements}")

    venv_dir = plugin_data / ".venv"
    lock = plugin_data / "requirements.lock"

    if _check_python_version() != 0:
        return 1

    if _venv_is_healthy(venv_dir, requirements, lock):
        return 0  # silent success — fast path

    plugin_data.mkdir(parents=True, exist_ok=True)

    if not _venv_python(venv_dir).exists():
        rc = _create_venv(venv_dir)
        if rc != 0:
            return rc

    rc = _install_requirements(venv_dir, requirements)
    if rc != 0:
        return rc

    # Write the lock file LAST — only on success — so a partial install
    # is retried on the next run.
    shutil.copy2(requirements, lock)
    _info("ready")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
