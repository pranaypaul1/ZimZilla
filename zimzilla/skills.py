"""Skills — a loadable library of technique documents.

ZimZilla ships one system prompt and whatever the model already knows. This adds
the missing layer: a directory of SKILL.md files (frontmatter + markdown) that
the agent can discover by name/description and read in full when a task matches.

It is deliberately the same shape Hermes/Claude skills use, so an existing library
(~/.hermes/skills, ~/.claude/skills) works unchanged — no conversion step.

Three tools:
  skill_list  — the index (name + description), optionally filtered by keyword
  skill_read  — the full SKILL.md body for one skill
  skill_search— ripgrep-style search across every skill body, for when the agent
                doesn't know which skill covers a topic
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MAX_BODY = 20000

DEFAULT_ROOTS = [
    Path.home() / ".hermes" / "skills",
    Path.home() / ".claude" / "skills",
]


def roots() -> list[Path]:
    raw = os.environ.get("ZIMZILLA_SKILLS_DIRS", "")
    if raw:
        return [Path(p).expanduser() for p in raw.split(os.pathsep) if p]
    return [p for p in DEFAULT_ROOTS if p.is_dir()]


def _frontmatter(text: str) -> dict:
    """Minimal YAML-ish frontmatter: enough for name/description, tolerant."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out = {}
    key = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^[A-Za-z0-9_-]+\s*:", line):
            key, _, value = line.partition(":")
            key = key.strip()
            out[key] = value.strip().strip("'\"")
        elif key:
            out[key] = (out.get(key, "") + " " + line.strip()).strip()
    return out


def _body(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def discover() -> list[dict]:
    """Every SKILL.md under every root that is a file with a name."""
    found, seen = [], set()
    for root in roots():
        for path in sorted(root.rglob("SKILL.md")):
            name = path.parent.name
            if name in seen:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _frontmatter(text)
            entry = {
                "name": fm.get("name") or name,
                "description": fm.get("description") or _first_line(_body(text)),
                "path": str(path),
                "root": str(root),
            }
            seen.add(name)
            found.append(entry)
    return found


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:160]
    return ""


def _index(query: str | None = None) -> list[dict]:
    items = discover()
    if query:
        q = query.lower()
        items = [i for i in items
                 if q in i["name"].lower() or q in i["description"].lower()]
    return items


# --------------------------------------------------------------------- tools

def _res(text: str, is_error: bool = False) -> "ToolResult":
    from .tools import ToolResult
    return ToolResult(text, is_error=is_error)


def _tool_skill_list(args: dict, cfg) -> "ToolResult":
    query = args.get("query")
    items = _index(query)
    if not items:
        return _res(f"no skills matched {query!r}. Roots: "
                    + ", ".join(str(r) for r in roots()) or "none configured")
    limit = int(args.get("limit") or 120)
    lines = [f"{len(items)} skills" + (f" matching {query!r}" if query else "") + ":"]
    for i in items[:limit]:
        lines.append(f"- {i['name']}: {i['description']}")
    if len(items) > limit:
        lines.append(f"… {len(items) - limit} more (narrow with query=)")
    lines.append("\nRead one with skill_read(name=...). Search bodies with skill_search(pattern=...).")
    return _res("\n".join(lines))


def _tool_skill_read(args: dict, cfg) -> "ToolResult":
    wanted = str(args.get("name") or "").strip()
    if not wanted:
        return _res("name is required — get one from skill_list", is_error=True)
    for item in discover():
        if item["name"] == wanted:
            try:
                text = Path(item["path"]).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return _res(f"cannot read {item['path']}: {e}", is_error=True)
            body = _body(text)
            if len(body) > MAX_BODY:
                body = body[:MAX_BODY] + f"\n… [truncated; full file at {item['path']}]"
            return _res(f"# {item['name']}\n{body}")
    return _res(f"no skill named {wanted!r}. Call skill_list to see what exists.", is_error=True)


def _tool_skill_search(args: dict, cfg) -> "ToolResult":
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return _res("pattern is required", is_error=True)
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    hits, files = [], 0
    for item in discover():
        try:
            text = Path(item["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matched = False
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matched = True
                if len(hits) < int(args.get("limit") or 60):
                    hits.append(f"{item['name']}:{n}: {line.strip()[:160]}")
        if matched:
            files += 1
    if not hits:
        return _res(f"no match for /{pattern}/ in {len(discover())} skills")
    return _res(f"{len(hits)} hits across {files} skills for /{pattern}/:\n" + "\n".join(hits))


SCHEMAS = [
    {
        "name": "skill_list",
        "description": (
            "List the available technique skills (name + one-line description). These are "
            "the operator's own playbooks — bug-bounty methodology, vuln-class techniques, "
            "tool command references. Call this before starting a phase of work, then "
            "skill_read the ones that match. Optional query filters by keyword."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring match on name/description."},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "skill_read",
        "description": (
            "Load one skill's full instructions. Use before testing: these encode the "
            "operator's proven payloads, bypass tables and pitfalls, which beats improvising."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "skill_search",
        "description": (
            "Regex search across every skill body. Use when you don't know which skill "
            "covers a topic ('jwt confusion', 'cache poisoning', 'SAML')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["pattern"],
        },
    },
]

DISPATCH = {
    "skill_list": _tool_skill_list,
    "skill_read": _tool_skill_read,
    "skill_search": _tool_skill_search,
}
