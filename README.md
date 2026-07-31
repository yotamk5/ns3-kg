# ns3-kg

A local code-lookup service for an [ns-3](https://www.nsnam.org/) source tree.
It has two parts:

- **`ns3kg-index`** — a CLI that parses the whole tree once (tree-sitter for
  C++, `ast` for Python) into a single SQLite file.
- **`ns3kg-server`** — an [MCP](https://modelcontextprotocol.io/) server
  (stdio) that answers precise questions from that file in milliseconds, with
  small capped responses, so a coding agent never has to read whole files.

The server is client-agnostic: anything that can launch an MCP stdio server
works — Claude Code, or any other agent harness/LLM (see
[Registering with other clients](#registering-with-other-agent-clients)).

## Tools the server offers

| Tool | Question it answers |
|---|---|
| `search_symbols` | "What's named something like X?" |
| `get_signature` | "Exact declaration(s) of X?" |
| `get_source` | "Show me lines N–M of that file" (max 200 lines) |
| `find_usages` | "Who calls X?" (textual candidates) |
| `get_index_status` | "Is the index fresh?" |
| `get_typeid_attributes` | "What attributes/traces does this ns-3 class register?" |
| `find_trace_sources` | "Which trace sources match this name?" |
| `get_inheritance_chain` | "Ancestors and subclasses of this class?" |
| `resolve_config_path` | "Which class owns the attribute at the end of this Config path?" |

All list outputs are capped at 30 results / ~8,000 characters per page with
cursor pagination, and every failure returns an instructive error with
closest-match suggestions.

## Install

**For step-by-step setup on a new machine (Ubuntu or Windows), including
the porting checklist, see [INSTALL.md](INSTALL.md).** The short version:

Requires Python 3.10+ and pip. On Windows use `.venv\Scripts\python`; on
Linux/macOS use `.venv/bin/python` (and `sudo apt install python3-venv`
first on Ubuntu).

```bash
cd ns3-kg
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # dev extra = pytest
```

## Build the index

```bash
.venv/Scripts/ns3kg-index "C:/path/to/ns-3.48" --db ns3.db
```

For the full ns-3.48 tree (~2,900 files) the first build produces a ~30 MB
`ns3.db`. It takes **~1–2 minutes on Linux** but **tens of minutes on
Windows** — the parser is fast either way (~7 ms/file); the database is
committed once per file, which NTFS makes expensive and ext4 does not. You
only pay this once. Re-runs are incremental: an
unchanged tree re-checks in about **1 second**; only files whose content
changed are re-parsed. `--full` forces a complete re-parse (needed only
after upgrading ns3-kg itself, since new extraction logic must revisit
unchanged files).

## Re-indexing workflow (deliberate choice: manual, no `--watch`)

There is no file-watcher mode, on purpose:

1. A watcher writing to the DB during an edit burst can hand agents a
   half-updated index; an explicit run has a clear before/after.
2. The failure mode is already covered: agents call `get_index_status`,
   which lists stale/new files and tells them to ask you to re-index.
3. An incremental run costs ~1 s, so there is nothing meaningful to save.

Workflow: after you (or an agent) edit ns-3 source, run the `ns3kg-index`
command above again. That's it.

## Register with Claude Code

Add to `.mcp.json` in the folder where you start Claude Code (project scope),
using absolute paths:

```json
{
  "mcpServers": {
    "ns3-kg": {
      "command": "C:/Users/User/Desktop/Projects/ns3 map/ns3-kg/.venv/Scripts/ns3kg-server.exe",
      "args": ["--db", "C:/Users/User/Desktop/Projects/ns3 map/ns3-kg/ns3.db"]
    }
  }
}
```

Start a **new** session in that folder; approve the server when prompted.
MCP servers are loaded at session start, so config changes need a new
session.

## Registering with other agent clients

The server speaks standard MCP over stdio — nothing in it is Claude-specific.
For any MCP-capable client (OpenAI Agents SDK, LangGraph, Cline, a custom
MiniMax harness, …) supply the same two things in that client's MCP-server
config format: the **command** (`ns3kg-server` from the venv, or equivalently
`python -m ns3kg.server.app`) and the **args** (`--db <absolute path to
ns3.db>`). Generic example (OpenAI Agents SDK, Python):

```python
from agents.mcp import MCPServerStdio

ns3kg = MCPServerStdio(params={
    "command": "C:/.../ns3-kg/.venv/Scripts/python.exe",
    "args": ["-m", "ns3kg.server.app", "--db", "C:/.../ns3-kg/ns3.db"],
})
```

If your harness has no MCP support at all, the fallback is to expose each
tool as a plain function that shells out to a small MCP stdio client — but
prefer a harness with native MCP; the tool descriptions are written to guide
weaker models and survive verbatim only through MCP.

Tips for weaker models: the tool descriptions embed the calling patterns
("use search_symbols first, then get_signature"), every error includes a
recovery hint, and responses are small enough to keep context clean — no
extra prompt engineering should be needed beyond registering the server.

## Running tests

```bash
.venv/Scripts/python -m pytest -q
```

19 tests: extraction ground truth against real ns-3 files, an end-to-end
MCP-stdio integration test of all 9 tools, and a constraint sweep
(caps/pagination enforcement, instructive-error paths).

## Troubleshooting

- **"index not found" at server start** — the `--db` path is wrong or the
  index was never built. Build it (see above) with the exact path from your
  client config.
- **Client shows no ns3-kg tools** — MCP servers load at session start:
  end the session and start a new one in the folder containing `.mcp.json`.
  In Claude Code, the server must also be approved once.
- **Tool answers contradict the source** — the index is stale. Any agent can
  confirm via `get_index_status`; re-run `ns3kg-index`.
- **`PARSE FAILED` lines during indexing** — that file is skipped, everything
  else is indexed; the run still succeeds. Usually exotic C++ the grammar
  rejects. The file's symbols simply won't be findable.
- **Attributes missing for a class** — attributes registered by a *parent*
  class are listed on the parent (mirrors ns-3's own TypeId inheritance);
  use `get_inheritance_chain`, then query the ancestor.
- **Windows paths** — use forward slashes or escaped backslashes in JSON;
  spaces in paths are fine as long as the string is quoted.

## Known limitations

- `find_usages` is textual: C++ virtual dispatch and overloads are not
  resolved — results are leads, not proof.
- `resolve_config_path` cannot know what a wildcard/index selects at
  runtime, does not model object aggregation, and only follows
  intermediate segments typed `Pointer<Class>`.
- Template-parameter bases (e.g. `SimpleRefCount`'s `PARENT`) end the
  inheritance chain with a `location: null` entry.
- Attributes/traces built with macros or non-literal names are extracted
  as raw expression text rather than resolved strings.
