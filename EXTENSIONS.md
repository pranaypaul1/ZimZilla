# Local extensions (this fork)

Two capabilities added on top of upstream ZimZilla. Both live in their own modules and are
merged into the tool layer at import time, so an upstream update to `tools.py` does not
collide with them — worst case an extension fails to import and the harness carries on.

## 1. Chrome bridge — `zimzilla/bridge.py`

Nine `chrome_*` tools that drive the operator's real, logged-in Chrome through a local HTTP
bridge (the Connect Openly extension, `127.0.0.1:4974`). `web_fetch` can only do an
unauthenticated GET; this sees the actual DOM of a session-backed SPA, runs JS in the page's
own context, and clicks by accessibility ref.

| Tool | Does |
|---|---|
| `chrome_tabs` | list tabs (tabId is required by everything else) |
| `chrome_read` | accessibility tree with `ref_N` handles |
| `chrome_text` | article-prioritised page text |
| `chrome_js` | run JS in the page world, last expression returned |
| `chrome_navigate` | go to URL / back / forward |
| `chrome_find` | find text, returns refs + coordinates |
| `chrome_shot` | screenshot to a PNG on disk (never base64 into the transcript) |
| `chrome_click` | click by ref or coordinate |
| `chrome_type` | type into the focused element |

Start the bridge first (it is a separate service):

```bash
bash ~/.hermes/skills/security/connect-openly/scripts/preflight.sh
```

Override the endpoint with `CONNECT_OPENLY_BRIDGE`.

## 2. Skill library — `zimzilla/skills.py`

A directory of `SKILL.md` files (YAML frontmatter + markdown) the agent can discover and load.
Frontmatter-compatible with Hermes/Claude skills, so an existing library works unchanged.

- roots: `~/.hermes/skills`, `~/.claude/skills` (override with `ZIMZILLA_SKILLS_DIRS`)
- tools: `skill_list`, `skill_read`, `skill_search`
- slash commands: `/skills` (type-to-filter picker), `/skills <filter>` (listing),
  `/skill <name>` (loads and injects into the model's context), `/skill-search <regex>`

## Tool-layer wiring (the only change to upstream files)

```python
# tools.py, after TOOL_NAMES
try:
    from . import bridge as _bridge_mod
    TOOL_SCHEMAS.extend(_bridge_mod.SCHEMAS)
except Exception:
    _bridge_mod = None

# after the _DISPATCH literal
if _bridge_mod is not None:
    _DISPATCH.update(_bridge_mod.DISPATCH)
```

Handlers import `ToolResult` late (inside the handler) so the `tools -> extension -> tools`
cycle cannot break at import:

```python
def _res(text, is_error=False):
    from .tools import ToolResult
    return ToolResult(text, is_error=is_error)
```

## Keeping it current with upstream

```bash
git remote add upstream https://github.com/Mr-Destroyer/ZimZilla.git   # once
git fetch upstream && git merge upstream/main     # or: git rebase upstream/main
```

The extension files are new paths, so merges only conflict if upstream touches the same
three lines in `tools.py` or the command tables in `ui/app.py` / `ui/complete.py`.
