"""Server-asserted brain identity for the P3 titlebar badge (two-brains plan).

Fail-closed by construction: any failure to locate, read, or parse brain.json
yields the literal string UNKNOWN, which the shell renders verbatim.
"""
import html
import json
import os
import re
from pathlib import Path

UNKNOWN = "UNKNOWN"
_BRAIN_ENV_VAR = "HERMES_WEBUI_BRAIN_JSON"
_BRAIN_FILE_NAME = "brain.json"
_SAFE_BRAIN_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def brain_json_path() -> Path:
    override = os.getenv(_BRAIN_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    from api.profiles import get_active_hermes_home
    return get_active_hermes_home() / _BRAIN_FILE_NAME


def brain_identity() -> str:
    try:
        raw = brain_json_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return UNKNOWN
    if not isinstance(data, dict):
        return UNKNOWN
    value = data.get("brain")
    if not isinstance(value, str):
        return UNKNOWN
    value = value.strip()
    if not _SAFE_BRAIN_RE.fullmatch(value):
        return UNKNOWN
    return value


def brain_identity_html() -> str:
    return html.escape(brain_identity(), quote=True)
