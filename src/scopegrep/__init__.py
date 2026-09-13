"""scopegrep -- attention-scored context retrieval over a declared repo scope.

The MCP server lives in `scopegrep.server`; the client pre-warm helper lives
in `scopegrep.prewarm`. Neither is meant to be imported for its functions --
the server is a stdio MCP process (`scopegrep-server` console script, or the
plugin's `.mcp.json` invokes it directly) and the pre-warm helper is a CLI
(`scopegrep-prewarm`). This package exists so both can be `pip install`ed and
found on PATH without depending on the plugin's on-the-fly `uv run` dependency
resolution.
"""

__version__ = "0.3.1"
