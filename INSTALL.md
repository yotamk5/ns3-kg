# INSTALL — ns3-kg

Step-by-step setup on a new machine, for **Ubuntu/Linux** (primary) and
**Windows**. Every command below was run and verified on Ubuntu 24.04 /
Python 3.12.3 and on Windows 11 / Python 3.12.4.

---

## 0. Before you start: two rules that save time

**Rule 1 — the server must run on the same machine as the ns-3 source.**
It reads the real files on disk to return exact line ranges. Put ns3-kg
wherever ns-3 lives: if ns-3 is on an Ubuntu box, install ns3-kg there too.

**Rule 2 — never copy `ns3.db` between machines. Rebuild it.**
The index stores the source-tree root as an *absolute* path (e.g.
`/home/you/ns-3.48`). A database copied from another machine points at a
directory that does not exist there: `get_source` fails and staleness
checks misfire. Rebuilding takes ~1–2 minutes on Linux — just rebuild.

---

## 1. Prerequisites

| | Ubuntu / Debian | Windows |
|---|---|---|
| Python | 3.10 or newer (24.04 ships 3.12 ✔) | 3.10+ from python.org |
| Extra packages | `python3-venv`, `python3-pip` | none (venv is bundled) |
| Network | needed once, for `pip install` | same |
| An ns-3 tree | e.g. `~/ns-3.48` | e.g. `C:\...\ns-3.48` |

On Ubuntu, install the Python extras first (venv creation fails without them):

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
```

Check your version is 3.10+:

```bash
python3 --version
```

---

## 2. Install ns3-kg (Ubuntu / Linux)

Copy the `ns3-kg` folder to the machine — **source only**. Do *not* bring
`.venv/`, `ns3.db`, or `__pycache__/` from another machine; they are
platform- and path-specific. You need: `src/`, `tests/`, `pyproject.toml`,
and the `.md` files.

```bash
cd ~/ns3-kg
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

Confirm the install (both commands should appear, and all tests pass):

```bash
ls .venv/bin | grep ns3kg && .venv/bin/python -m pytest -q
```

Expected: `ns3kg-index`, `ns3kg-server`, and `19 passed`.

> **Windows equivalent:** identical, except `python -m venv .venv` and the
> tools live in `.venv\Scripts\` (`ns3kg-index.exe`, `ns3kg-server.exe`).
> Use `.venv\Scripts\python -m pytest -q` to verify.

---

## 3. Build the index

Point it at your ns-3 root and choose where the database lives:

```bash
.venv/bin/ns3kg-index ~/ns-3.48 --db ~/ns3-kg/ns3.db
```

What to expect:

| | Linux (ext4) | Windows (NTFS) |
|---|---|---|
| First full build, ~2,900 files | **~1–2 minutes** (measured: 424 files in 10.4 s) | tens of minutes (one-time) |
| Re-run, nothing changed | ~1 s | ~1 s |
| Database size | ~30 MB | ~30 MB |

`failed: 0` in the summary means every file parsed. A few `PARSE FAILED`
lines are survivable — those files are skipped, everything else is indexed.

**Keep the tree and the database on the native filesystem.** Under WSL,
indexing a tree that lives on `/mnt/c/...` is dramatically slower than one
in the Linux home directory.

---

## 4. Register the server with your AI client

The server is launched by the **MCP client** (the assistant extension), not
by the IDE itself. All clients need the same two things:

- **command** — the absolute path to `ns3kg-server`
- **args** — `["--db", "<absolute path to ns3.db>"]`

### Claude Code (project scope)

Create `.mcp.json` in the folder where you start the assistant:

```json
{
  "mcpServers": {
    "ns3-kg": {
      "command": "/home/you/ns3-kg/.venv/bin/ns3kg-server",
      "args": ["--db", "/home/you/ns3-kg/ns3.db"]
    }
  }
}
```

On Windows, use `C:/Users/You/.../ns3kg-server.exe` — forward slashes, or
escaped backslashes.

Then **start a new session** in that folder and approve the server when
prompted. MCP servers load at session start; an already-open session will
not see it.

### Any other MCP-capable client

Same two fields in that client's own config format. If it accepts a plain
command line, this also works and avoids the entry-point path:

```bash
/home/you/ns3-kg/.venv/bin/python -m ns3kg.server.app --db /home/you/ns3-kg/ns3.db
```

### VS Code + WSL

If ns-3 lives in WSL, install ns3-kg in WSL too and open the folder with the
WSL remote extension. The assistant extension then runs *inside* Linux, so
every path in the config is a Linux path (`/home/you/...`), not `C:\...`.

---

## 5. Verify it works

Ask the assistant, in a fresh session:

> using the ns3-kg tools, what is the signature of ApWifiMac::Enqueue?

A correct result looks like
`void ApWifiMac::Enqueue(Ptr<WifiMpdu> mpdu, Mac48Address to, Mac48Address from)`
with a `src/wifi/model/ap-wifi-mac.cc` location — obtained via a tool call,
**not** by opening files. If it opens files instead, the server is not
registered (or the session predates the config).

To test without any AI client, query the database directly:

```bash
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('ns3.db');print(c.execute('select count(*) from symbols').fetchone())"
```

---

## 6. Daily workflow

1. Work normally; the assistant queries the index on its own.
2. After editing ns-3 source, re-run the same `ns3kg-index` command (~1 s).
3. If you forget, the server detects stale files and tells the assistant to
   ask you — nothing silently goes wrong.

There is no file-watcher by design: an explicit re-index gives a clear
before/after and cannot capture a half-finished edit.

---

## 7. Troubleshooting

**`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`**
You installed `mcp` 2.x, which removed that module. `pyproject.toml` pins
`mcp>=1.0,<2`; if you installed before that pin existed, fix it with:

```bash
.venv/bin/pip install -e ".[dev]" --upgrade
```

Confirm with `.venv/bin/pip show mcp` — expect 1.x.

**`ensurepip is not available` / venv creation fails (Ubuntu)**
Install `python3-venv` (see §1). On some images the versioned package is
required, e.g. `sudo apt install python3.12-venv`.

**`index not found` when the server starts**
The `--db` path in the client config is wrong, or the index was never
built. Use the exact absolute path from §3.

**The client shows no ns3-kg tools**
MCP servers load at session start — open a *new* session in the folder
containing the config, and approve the server if prompted.

**Answers contradict the source / `get_source` says a file is missing**
Either the index is stale (re-run `ns3kg-index`), or the database was
copied from another machine (see Rule 2 — rebuild it locally).

**Indexing is extremely slow**
Under WSL, check the tree is not on `/mnt/c/...`. On Windows, the first
full build is genuinely slow (per-file database commits); later runs are
incremental and fast.

---

## 8. Porting checklist (e.g. to a MiniMax-driven setup)

Work through these in order — stop at the first "no":

- [ ] **Does the target harness support MCP stdio servers?** This is the
      real gate. If yes, everything below is routine. If no, you need a
      harness that does, or a shim exposing each tool as a function that
      calls the server.
- [ ] Python 3.10+ available on the target machine (§1).
- [ ] ns3-kg source copied — without `.venv/`, `ns3.db`, `__pycache__/`.
- [ ] `pip install -e ".[dev]"` succeeds and `pytest` reports 19 passed (§2).
- [ ] Index **rebuilt locally** against the local ns-3 path (§3, Rule 2).
- [ ] Server registered in that harness's config with absolute paths (§4).
- [ ] Verified with the `ApWifiMac::Enqueue` question (§5).
- [ ] **Then** the open question worth measuring: does the weaker model
      actually *choose* these tools? Run the same task with and without the
      server registered and compare — that is where the benefit should be
      largest, and it has not been tested on a small model yet.

Nothing in the server is Claude-specific: the tool descriptions carry their
own usage guidance, responses are capped small, and errors include recovery
hints — the things a weaker model needs travel inside the server itself.
