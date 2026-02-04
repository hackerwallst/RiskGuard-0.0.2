from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent if (ROOT.parent / ".git").exists() else ROOT
LOG_DIR = ROOT / "logger" / "logs"
LOG_FILE = LOG_DIR / "update.log"
STATUS_PATH = ROOT / ".rg_update_status.json"


def _log(message: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} - {message}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _write_status(ok: bool, message: str, version: str = "") -> None:
    payload = {"ok": bool(ok), "message": message, "version": version}
    try:
        STATUS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    except Exception:
        pass


def _run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    _log("RUN: " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        _log("OUT: " + result.stdout.strip())
    if result.stderr:
        _log("ERR: " + result.stderr.strip())
    return result


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(*args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=REPO_ROOT, env=_git_env())


def _ensure_git() -> Tuple[bool, str]:
    result = _run(["git", "--version"], cwd=REPO_ROOT, env=_git_env())
    if result.returncode != 0:
        return False, "Git nao encontrado no sistema."
    return True, ""


def _ensure_repo_clean() -> Tuple[bool, str]:
    result = _git("status", "--porcelain")
    if result.returncode != 0:
        return False, "Falha ao verificar o status do repositorio."
    if result.stdout.strip():
        return False, "Repositorio com alteracoes locais. Salve/commit antes de atualizar."
    return True, ""


def _latest_tag() -> str:
    result = _git("for-each-ref", "--sort=-creatordate", "--format=%(refname:short)", "refs/tags")
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        tag = line.strip()
        if tag:
            return tag
    return ""


def _pip_install(requirements: Path) -> Tuple[bool, str]:
    if not requirements.exists():
        return True, ""
    python_exe = _find_python()
    cmd = [
        str(python_exe),
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "-r",
        str(requirements),
    ]
    result = _run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return False, "Falha ao instalar dependencias. Veja update.log."
    return True, ""


def _find_python() -> Path:
    venv_python = ROOT / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def _load_terminal_path() -> str:
    cfg = ROOT / ".rg_terminal.json"
    if not cfg.exists():
        return ""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("terminal_path") or "").strip()
    return ""

def _restart_main() -> None:
    main_py = ROOT / "main.py"
    if not main_py.exists():
        return
    python_exe = _find_python()
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    env = os.environ.copy()
    env["RG_NO_PROMPT"] = "1"
    terminal_path = _load_terminal_path()
    if terminal_path:
        env["RG_TERMINAL_PATH"] = terminal_path
    try:
        subprocess.Popen([str(python_exe), str(main_py)], cwd=str(ROOT), creationflags=flags, env=env)
    except Exception:
        pass

def _restart_ui() -> None:
    ui = ROOT / "riskguard_ui.py"
    if not ui.exists():
        return
    python_exe = _find_python()
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.Popen([str(python_exe), str(ui)], cwd=str(ROOT), creationflags=flags)
    except Exception:
        pass


def main() -> int:
    _log("Update start.")
    ok, msg = _ensure_git()
    if not ok:
        _write_status(False, msg)
        return 1

    ok, msg = _ensure_repo_clean()
    if not ok:
        _write_status(False, msg)
        return 2

    result = _git("rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        _write_status(False, "Diretorio nao e um repositorio Git valido.")
        return 3

    result = _git("fetch", "--tags", "--prune")
    if result.returncode != 0:
        _write_status(False, "Falha ao buscar atualizacoes do GitHub.")
        return 4

    version = ""
    tag = _latest_tag()
    if tag:
        result = _git("checkout", "-B", "release", tag)
        if result.returncode != 0:
            _write_status(False, f"Falha ao aplicar tag {tag}.")
            return 5
        version = tag
    else:
        result = _git("pull", "--ff-only")
        if result.returncode != 0:
            _write_status(False, "Falha ao atualizar a branch principal.")
            return 6
        head = _git("rev-parse", "--short", "HEAD")
        if head.returncode == 0:
            version = head.stdout.strip()

    ok, msg = _pip_install(ROOT / "requirements.txt")
    if not ok:
        _write_status(False, msg, version=version)
        return 7

    _write_status(True, "Atualizacao concluida com sucesso.", version=version)

    if "--restart-ui" in sys.argv:
        _restart_main()
        _restart_ui()

    _log("Update done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
