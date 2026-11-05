"""Chrome bridge — drive the user's real, logged-in Chrome through the
Connect Openly extension (local HTTP bridge on 127.0.0.1:4974).

Why this exists: ZimZilla's web_fetch is a plain HTTP GET. It cannot see a page
that needs a login, a JS-rendered SPA, or a flow that spans clicks. The bridge
runs inside the user's own Chrome via chrome.debugger, so the agent gets the
real DOM, real console, real cookies-backed session — and can look at the page.

Safety: this is the operator's personal browser. Tools here are read-mostly by
default; chrome_click / chrome_type actually move the user's browser, so they
are gated and should only be used when the task genuinely needs it.

Every call is a POST to <bridge>/call with {"tool": ..., "args": {...}}.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoids a circular import at runtime
    from .tools import ToolResult

BASE = os.environ.get("CONNECT_OPENLY_BRIDGE", "http://127.0.0.1:4974").rstrip("/")
DEFAULT_TIMEOUT = 95
MAX_TEXT = 12000


def _post(tool: str, args: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    body = json.dumps({"tool": tool, "args": args, "timeout": timeout}).encode()
    req = urllib.request.Request(
        BASE + "/call", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"bridge HTTP {e.code}: {e.read()[:300]!r}"}
    except Exception as e:  # connection refused, timeout, DNS — all the same story
        return {"ok": False,
                "error": f"bridge unreachable at {BASE} ({e}). "
                         f"Run: bash ~/.hermes/skills/security/connect-openly/scripts/preflight.sh"}


def health() -> dict:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _result(payload: dict, limit: int = MAX_TEXT) -> tuple[str, bool]:
    """Flatten a bridge response into (text, is_error)."""
    if not payload.get("ok", False):
        return str(payload.get("error") or payload), True
    result = payload.get("result", payload)
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=1, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + f"\n… [truncated at {limit} chars of {len(text)}]"
    return text, False


# --------------------------------------------------------------------- tools

def _tool_chrome_tabs(args: dict, cfg) -> "ToolResult":
    return _wrap(_post("tabs_context", {}))


def _tool_chrome_navigate(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    url = str(args.get("url") or "")
    if not tab or not url:
        return _err("tabId and url are required (url may be 'back' or 'forward')")
    return _wrap(_post("navigate", {"tabId": tab, "url": url}, timeout=150))


def _tool_chrome_read(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    if not tab:
        return _err("tabId is required (call chrome_tabs first)")
    body = {"tabId": tab, "filter": args.get("filter", "interactive")}
    if args.get("ref_id"):
        body["ref_id"] = args["ref_id"]
    return _wrap(_post("read_page", body, timeout=120), limit=int(args.get("max_chars") or MAX_TEXT))


def _tool_chrome_text(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    if not tab:
        return _err("tabId is required")
    return _wrap(_post("get_page_text",
                       {"tabId": tab, "max_chars": int(args.get("max_chars") or MAX_TEXT)}),
                 limit=int(args.get("max_chars") or MAX_TEXT))


def _tool_chrome_js(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    code = str(args.get("code") or "")
    if not tab or not code:
        return _err("tabId and code are required")
    return _wrap(_post("javascript_tool", {"tabId": tab, "code": code}, timeout=120))


def _tool_chrome_find(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    query = str(args.get("query") or "")
    if not tab or not query:
        return _err("tabId and query are required")
    return _wrap(_post("find", {"tabId": tab, "query": query}, timeout=120))


def _tool_chrome_shot(args: dict, cfg) -> "ToolResult":
    """Screenshot -> PNG on disk (never base64 into the transcript)."""
    tab = int(args.get("tabId") or 0)
    if not tab:
        return _err("tabId is required")
    payload = _post("computer", {"tabId": tab, "action": "screenshot",
                                 "full_page": bool(args.get("full_page"))}, timeout=120)
    if not payload.get("ok", False):
        text, _ = _result(payload)
        return _res(text, is_error=True)
    image = (payload.get("result") or {}).get("image", "")
    if not image.startswith("data:image"):
        return _err(f"bridge returned no image ({str(image)[:120]})")
    raw = base64.b64decode(image.split(",", 1)[1])
    out = Path(args.get("path") or "").expanduser() if args.get("path") else \
        Path(getattr(cfg, "workdir", ".")) / f"recon/shot-{tab}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return _res(f"saved screenshot ({len(raw)} bytes) -> {out}")


def _tool_chrome_click(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    if not tab:
        return _err("tabId is required")
    body = {"tabId": tab, "action": args.get("action", "left_click")}
    if args.get("ref"):
        body["ref"] = args["ref"]
    elif args.get("coordinate"):
        body["coordinate"] = args["coordinate"]
    else:
        return _err("pass ref (from chrome_read) or coordinate [x,y]")
    return _wrap(_post("computer", body, timeout=120))


def _tool_chrome_type(args: dict, cfg) -> "ToolResult":
    tab = int(args.get("tabId") or 0)
    text = args.get("text")
    if not tab or text is None:
        return _err("tabId and text are required")
    return _wrap(_post("computer", {"tabId": tab, "action": "type", "text": str(text)},
                       timeout=120))


def _res(text: str, is_error: bool = False) -> "ToolResult":
    """Late-bound import so tools.py -> bridge.py stays circular-safe."""
    from .tools import ToolResult
    return ToolResult(text, is_error=is_error)


def _wrap(payload: dict, limit: int = MAX_TEXT) -> "ToolResult":
    text, is_error = _result(payload, limit=limit)
    return _res(text, is_error)


def _err(msg: str) -> "ToolResult":
    return _res(msg, is_error=True)


# -------------------------------------------------------------------- schema

SCHEMAS = [
    {
        "name": "chrome_tabs",
        "description": (
            "List the tabs in the operator's real Chrome (via the local Connect Openly "
            "bridge). Returns tabId, url, title, active. Call this FIRST — tabId is "
            "required by every other chrome_* tool and changes when tabs are recreated."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "chrome_read",
        "description": (
            "Accessibility tree of a live Chrome tab with [ref_N] handles and coordinates. "
            "This is how you see a JS-rendered page, a logged-in dashboard, or anything "
            "web_fetch cannot read. filter='interactive' for clickable/typeable nodes only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "filter": {"type": "string", "enum": ["interactive", "all"]},
                "ref_id": {"type": "string", "description": "Expand one subtree, e.g. ref_12."},
                "max_chars": {"type": "integer"},
            },
            "required": ["tabId"],
        },
    },
    {
        "name": "chrome_text",
        "description": "Article-prioritised plain text of a live Chrome tab — fastest way to read page content.",
        "input_schema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}, "max_chars": {"type": "integer"}},
            "required": ["tabId"],
        },
    },
    {
        "name": "chrome_js",
        "description": (
            "Run JavaScript in the page's main world and return the last expression "
            "(promises awaited). Used for state checks, extracting structured data, "
            "reading tokens from localStorage, or calling an API with the page's own session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}, "code": {"type": "string"}},
            "required": ["tabId", "code"],
        },
    },
    {
        "name": "chrome_navigate",
        "description": "Navigate a tab to a URL, or 'back'/'forward'. Never navigate the operator's active tab unless the task needs it — prefer chrome_tabs_new.",
        "input_schema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}, "url": {"type": "string"}},
            "required": ["tabId", "url"],
        },
    },
    {
        "name": "chrome_find",
        "description": "Find text/selectors in a tab; returns up to 20 matches with refs and coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}, "query": {"type": "string"}},
            "required": ["tabId", "query"],
        },
    },
    {
        "name": "chrome_shot",
        "description": "Screenshot a tab to a PNG on disk and return the path. Use it for layout, canvas, CAPTCHAs, or visual proof — not for reading text (use chrome_text).",
        "input_schema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "path": {"type": "string", "description": "Where to write the PNG."},
                "full_page": {"type": "boolean"},
            },
            "required": ["tabId"],
        },
    },
    {
        "name": "chrome_click",
        "description": "Click an element in a live tab by ref (preferred) or [x,y] coordinate. This moves the operator's browser — gate it and confirm the target first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "ref": {"type": "string"},
                "coordinate": {"type": "array", "items": {"type": "integer"}},
                "action": {"type": "string",
                           "enum": ["left_click", "right_click", "double_click", "hover"]},
            },
            "required": ["tabId"],
        },
    },
    {
        "name": "chrome_type",
        "description": "Type text into the focused element of a live tab. Click the field first, or use chrome_fill.",
        "input_schema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["tabId", "text"],
        },
    },
]

DISPATCH = {
    "chrome_tabs": _tool_chrome_tabs,
    "chrome_read": _tool_chrome_read,
    "chrome_text": _tool_chrome_text,
    "chrome_js": _tool_chrome_js,
    "chrome_navigate": _tool_chrome_navigate,
    "chrome_find": _tool_chrome_find,
    "chrome_shot": _tool_chrome_shot,
    "chrome_click": _tool_chrome_click,
    "chrome_type": _tool_chrome_type,
}
