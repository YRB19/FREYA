"""
FREYA — Classifier (Layer 3)

Cheap, deterministic, no-LLM classification. This runs on every fs event
before anything expensive (OpenCode, Claude) is ever invoked. Per spec:
"Do NOT use Claude for deterministic tasks."

Two jobs:
  1. classify(path) -> category, ignorable
  2. scan_for_secrets(path) -> list of (line_no, secret_type) WITHOUT ever
     returning the secret value itself. FREYA must never move secret bytes
     into canonical knowledge, and it must not even hold them in memory
     longer than the scan requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---- noise / never-ingest patterns -------------------------------------

IGNORE_DIR_NAMES = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".turbo", "target", ".cache", ".pytest_cache", ".mypy_cache",
    ".obsidian",  # explicit hard rule, never touched regardless of content
}

IGNORE_FILE_SUFFIXES = {
    ".pyc", ".pyo", ".o", ".so", ".dylib", ".dll", ".exe", ".class",
    ".log",  # build logs are noise unless explicitly promoted; see classify()
    ".lock",  # package-lock.json etc handled separately (dependency, not noise)
    ".DS_Store",
}

SECRET_FILE_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "id_rsa", "id_ed25519"}

CANONICAL_ROOT_MARKER = "Notion"
HISTORICAL_ROOT_MARKER = "Claude Data"
PROTECTED_ROOT_MARKER = ".obsidian"

CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".c", ".cpp", ".sh"}
DOC_SUFFIXES = {".md", ".rst", ".txt"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg"}
SPEC_HINT_NAMES = {"adr", "spec", "design", "rfc", "architecture"}


@dataclass
class Classification:
    category: str          # see CATEGORY list in module docstring / spec
    ignorable: bool
    protected: bool         # true for .obsidian / Claude Data (read-only zones)
    canonical_zone: bool    # true if under Notion/**
    reason: str


def is_protected_path(vault_relative_path: str) -> bool:
    parts = Path(vault_relative_path).parts
    return len(parts) > 0 and parts[0] == PROTECTED_ROOT_MARKER


def is_historical_path(vault_relative_path: str) -> bool:
    parts = Path(vault_relative_path).parts
    return len(parts) > 0 and parts[0] == HISTORICAL_ROOT_MARKER


def is_canonical_path(vault_relative_path: str) -> bool:
    parts = Path(vault_relative_path).parts
    return len(parts) > 0 and parts[0] == CANONICAL_ROOT_MARKER


def classify(path: Path) -> Classification:
    parts = path.parts
    name = path.name
    suffix = path.suffix.lower()

    # hard-noise directories anywhere in the path
    if any(p in IGNORE_DIR_NAMES for p in parts):
        return Classification("cache_or_vendor", True, False, False,
                               "path contains an ignored directory segment")

    if name in SECRET_FILE_NAMES or name.startswith(".env"):
        return Classification("secret_bearing", True, False, False,
                               "filename matches known secret-bearing pattern; never ingested")

    if suffix in IGNORE_FILE_SUFFIXES:
        return Classification("generated_or_binary", True, False, False,
                               f"suffix {suffix} is routine noise")

    if suffix in CODE_SUFFIXES:
        return Classification("source_code", False, False, False, "recognized code suffix")

    if suffix in DOC_SUFFIXES:
        lower_stem = name.lower()
        if any(h in lower_stem for h in SPEC_HINT_NAMES):
            return Classification("project_specification", False, False, False,
                                   "doc filename suggests spec/ADR/design content")
        return Classification("documentation", False, False, False, "recognized doc suffix")

    if suffix in CONFIG_SUFFIXES or name in {"Dockerfile", "docker-compose.yml"}:
        return Classification("configuration", False, False, False, "recognized config file")

    return Classification("unknown", False, False, False, "no rule matched; requires content inspection")


# ---- secret scanning ----------------------------------------------------
# Detection only. Never returns or logs the matched value itself.

_SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("generic_api_key", re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]")),
    ("private_key_block", re.compile(r"-----BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("basic_auth_url", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/]+:[^\s@/]+@")),
]


def scan_for_secrets(path: Path, max_bytes: int = 2_000_000) -> list[tuple[int, str]]:
    """Returns [(line_number, secret_type), ...]. Never the matched text."""
    hits: list[tuple[int, str]] = []
    try:
        if path.stat().st_size > max_bytes:
            return hits  # don't slurp huge files just to scan; handled as 'unknown' upstream
        text = path.read_text(errors="ignore")
    except (OSError, UnicodeDecodeError):
        return hits
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, label))
    return hits


def redact_for_extraction(text: str) -> str:
    """Belt-and-suspenders: if extracted text ever contains secret-shaped
    substrings, redact before it can reach canonical knowledge."""
    redacted = text
    for label, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted
