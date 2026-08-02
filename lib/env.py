"""Secrets, from the environment or a gitignored .env. Stdlib only.

Two rules, both of which exist because a key in a repo is forever:

  * The environment always wins. CI sets real secrets as environment variables,
    and a stale `.env` on a laptop must never silently override them.
  * `.env` is never written by this pipeline, only read. It is already in
    `.gitignore`; `.env.example` is the committed half and holds names, not
    values.

Keys are read at call time rather than import time so a test can set one and a
long-running process can be corrected without a restart.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loaded = False


def _load_dotenv():
    """Parse .env once. KEY=value, `export` tolerated, # comments ignored."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = os.path.join(ROOT, ".env")
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        # Quotes are a shell affordance, not part of the value. A key pasted as
        # "abc123" and stored with the quotes fails auth in a way that reads as
        # a bad key rather than a bad parse.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def get(name, default=None):
    _load_dotenv()
    return os.environ.get(name, default)


def require(name, hint=""):
    """Fail loudly and usefully. A missing key should say where to put it."""
    v = get(name)
    if not v:
        raise RuntimeError(
            f"{name} is not set. Add it to {os.path.join(ROOT, '.env')} as "
            f"`{name}=...` (gitignored) or export it in your shell."
            + (f" {hint}" if hint else ""))
    return v
