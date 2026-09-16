"""
freya/mcp_adapter.py -- real Obsidian MCP write adapter (mcp-tools-istefox).

Talks to the same http://127.0.0.1:27200/mcp JSON-RPC endpoint FREYA's
read-side already depends on. This is the ONLY canonical write mechanism
Phase 8 uses -- there is no direct filesystem fallback anywhere in this
module or in canonical_executor.py. If this adapter can't reach or
authenticate to the server, ping() returns False and the executor reports
BLOCKED -- it never falls back to open()/write().
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:27200/mcp"


DEFAULT_VAULT_ROOT = "/Users/yoboxo/Developer/Developer"
_PLUGIN_DATA_RELPATH = ".obsidian/plugins/mcp-tools-istefox/data.json"


def _resolve_token_from_plugin_config(vault_root: str = DEFAULT_VAULT_ROOT):
    """Reads the mcp-tools-istefox plugin's own bearer token from its own
    local config file (the existing local credential mechanism for this
    server). Read-only; never writes, never logs the value; returns None
    (never raises) if unavailable."""
    path = Path(vault_root) / _PLUGIN_DATA_RELPATH
    try:
        import json as _json
        data = _json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    transport = data.get("mcpTransport") or {}
    token = transport.get("bearerToken")
    if token:
        return token
    tokens = transport.get("tokens") or []
    if tokens and isinstance(tokens, list):
        first = tokens[0]
        if isinstance(first, dict):
            return first.get("token")
    return None

class McpUnavailable(Exception):
    pass


class ObsidianMCPAdapter:
    def __init__(self, base_url: str = DEFAULT_URL, api_key: Optional[str] = None, timeout: float = 10.0):
        self.base_url = base_url
        # No credential is bundled or hunted for here. If the server
        # requires auth (it does, per manual probe: 401 without a token)
        # and OBSIDIAN_MCP_TOKEN isn't set, every call correctly fails and
        # ping() returns False -- BLOCKED, not a silent bypass.
        self.api_key = api_key or os.environ.get("OBSIDIAN_MCP_TOKEN") or _resolve_token_from_plugin_config()
        self.timeout = timeout
        self._id = 0

    def _call(self, method: str, params: dict) -> dict:
        self._id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise McpUnavailable(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise McpUnavailable(str(e)) from e
        for line in reversed(body.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                line = line[len("data:"):].strip()
            if line.startswith("{"):
                data = json.loads(line)
                if "error" in data:
                    raise McpUnavailable(str(data["error"]))
                return data.get("result", {})
        raise McpUnavailable("no JSON-RPC result found in response")

    def _tool(self, name: str, arguments: dict) -> dict:
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def ping(self) -> bool:
        try:
            self._call("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "freya-phase8", "version": "0.1"},
            })
            return True
        except McpUnavailable:
            return False

    def read(self, canonical_path: str) -> Optional[str]:
        try:
            result = self._tool("get_vault_file", {"path": canonical_path})
        except McpUnavailable as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return None
            raise
        content = result.get("content")
        if isinstance(content, list) and content:
            content = content[0].get("text", "")
        return content

    def create(self, canonical_path: str, content: str) -> bool:
        result = self._tool("create_vault_file", {"path": canonical_path, "content": content})
        return not result.get("isError", False)

    def append_to_section(self, canonical_path: str, section: str, content: str) -> bool:
        result = self._tool("patch_vault_file", {
            "path": canonical_path, "targetType": "heading", "target": section,
            "operation": "append", "content": content,
        })
        return not result.get("isError", False)

    def replace_section(self, canonical_path: str, section: str, content: str) -> bool:
        result = self._tool("patch_vault_file", {
            "path": canonical_path, "targetType": "heading", "target": section,
            "operation": "replace", "content": content,
        })
        return not result.get("isError", False)

    def overwrite(self, canonical_path: str, content: str) -> bool:
        """Used only for restore-on-rollback, writing back an exact prior
        snapshot -- not a general canonical write path for proposals."""
        result = self._tool("create_vault_file", {"path": canonical_path, "content": content, "overwrite": True})
        return not result.get("isError", False)

    def delete(self, canonical_path: str) -> bool:
        result = self._tool("delete_vault_file", {"path": canonical_path})
        return not result.get("isError", False)
