"""Tiny .env reader/writer (no external deps). Values are never printed."""
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_env(path: Path = ENV_PATH) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def update_env(set_vars: dict, remove: tuple = (), path: Path = ENV_PATH) -> None:
    """Set/replace keys in set_vars, drop keys in remove, keep everything else."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in remove:
            continue
        if key in set_vars:
            out.append(f"{key}={set_vars[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in set_vars.items() if k not in seen]
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
