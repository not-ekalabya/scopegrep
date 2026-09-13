# The Python module

This covers `scopegrep` as an installed Python package — for anyone running
it outside the Claude Code plugin path: another MCP-capable agent host, a
raw MCP client, or a CI step that warms the service before a demo.

If you installed via `claude plugin install`, you don't need any of this —
the plugin's `.mcp.json` invokes the server directly through `uv run` and
resolves its two dependencies on the fly. This doc is for the `pip install`
path.

## Install

```bash
pip install git+https://github.com/not-ekalabya/scopegrep.git
```

This puts two console scripts on `PATH` and installs the `scopegrep` package
(`src/scopegrep/`). Requires Python 3.10+.

## Package layout

```
scopegrep/
├── server.py    # the MCP server: the four tools, all retrieval/rendering logic
└── prewarm.py   # a small CLI that pings the hosted service until it's ready
```

Neither module is meant to be imported for its functions — both are
entry points, not a library API. `server.py` is a stdio MCP process;
`prewarm.py` is a CLI. This split exists so both are `pip install`able and
discoverable on `PATH` without depending on the plugin's on-the-fly `uv run`
dependency resolution.

## Console scripts

### `scopegrep-server`

Starts the MCP server on stdio. This is what any MCP-speaking client should
launch as a subprocess — the protocol is standard JSON-RPC over stdio
(`initialize`, `tools/list`, `tools/call`), so it works with any MCP host,
not only Claude Code.

Reads its configuration entirely from environment variables (see below) —
no config file, no CLI flags.

Minimal manual invocation, to see the tool list a fresh install exposes:

```bash
export SCOPEGREP_URL='<your service URL>'
export SCOPEGREP_TOKEN='<your access code>'
python3 -c "
import subprocess, json
p = subprocess.Popen(['scopegrep-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
p.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'probe','version':'0'}}}) + '\n')
p.stdin.write(json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list'}) + '\n')
p.stdin.flush()
print(p.stdout.readline())
print(p.stdout.readline())
"
```

### `scopegrep-prewarm`

Sends a request to the service's health endpoint and waits for it to report
ready, so a session's *first real query* doesn't pay the cold-start cost
(see "First request may be slow" in the main README).

```bash
scopegrep-prewarm                 # ping once, wait for ready, exit
scopegrep-prewarm --watch 120     # keep the service warm for 120s
                                   # (use this while you're live-demoing)
```

Reads `SCOPEGREP_URL` and `SCOPEGREP_TOKEN` the same way the server does.
`tools/prewarm.sh` is the equivalent bash script, kept for environments
without Python on `PATH`.

## Environment variables

| variable | required | what it does |
|---|---|---|
| `SCOPEGREP_URL` | yes | Base URL of the hosted retrieval service you were given. |
| `SCOPEGREP_TOKEN` | yes* | Your access code. *Not required as an env var if a token file is present (see below). |
| `SCOPEGREP_ROOT` | no | Repo root to resolve relative `include=[...]` globs against. Defaults to the current working directory of whatever process launched the server. |

`SCOPEGREP_TOKEN` can also live in a file instead of the environment, so it
never needs to be exported per-shell or risk landing in a committed dotfile:
`~/.config/scopegrep/token`, or `.scopegrep_token` at the repo root you
installed into. The env var wins if both are set.

## Exit / failure behavior

- Missing `SCOPEGREP_URL` or no resolvable token: the server still starts
  (so `tools/list` works for discovery) but every tool call returns an
  error explaining what's missing, rather than the process refusing to boot.
- A `401` from the service (bad or missing token) is surfaced verbatim in
  the tool's response text — it is not retried or silently swallowed.
- A cold-start timeout is surfaced as a plain error telling you to run
  `scopegrep-prewarm` first; it is not retried automatically, since a
  retry from inside a tool call would just pay the same cold-start cost
  twice in the same turn.

## Using it from a non-Claude MCP host

Any host that can launch a subprocess and speak MCP over its stdio can use
this server. Point your host's MCP client config at the `scopegrep-server`
executable (or `python3 -m scopegrep.server` from a checkout) with the three
environment variables above set, the same way you would configure any other
stdio MCP server. The tool schemas returned by `tools/list` are the same
regardless of which host calls them.

## Running the tests

```bash
python3 tests/test_outbound.py
```

No network and no hosted service required — this suite only exercises the
outbound-call-site logic, which builds and inspects real local git
repositories per test case. It does not touch `SCOPEGREP_URL`.
