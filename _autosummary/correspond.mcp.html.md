# correspond.mcp

MCP over stdio: the same tools, for Claude Desktop and other local MCP clients.

Run `correspond-mcp` (install the extra first: `pip install "correspond[mcp]"`). Tools
are named by `correspond.tools:<name>` reference, so the core never imports an MCP
library. By default the server exposes only the tools that read (`channels`,
`requirements`, `capabilities`, `ref`, `read`, `listen`, `audience`). The writes (`send`,
`edit`, `react`) are exposed only when the operator starts it with `--allow-send`,
and every write still takes `dry_run`.

The data root is the server’s, never the model’s: `data_dir` is removed from every tool’s
schema. Point the server elsewhere with `CORRESPOND_DATA_DIR` in the client configuration:

```default
{"mcpServers": {"correspond": {"command": "correspond-mcp"}}}
```

### Functions

| [`main`](#correspond.mcp.main)([argv])                | Serve the tools over stdio; `--allow-send` also exposes send, edit and react.   |
|------------------------------------------------------------------------------|---------------------------------------------------------------------------------|
| [`mk_server`](#correspond.mcp.mk_server)(\*[, allow_send]) | A FastMCP server over the exposed tools, with `data_dir` hidden (not started).  |
| [`refs`](#correspond.mcp.refs)(\*[, allow_send])      | `correspond.tools:<name>` references for the tools the server exposes.          |

### correspond.mcp.main(argv=None)

Serve the tools over stdio; `--allow-send` also exposes send, edit and react.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.mcp.mk_server(, allow_send=False)

A FastMCP server over the exposed tools, with `data_dir` hidden (not started).

### correspond.mcp.refs(, allow_send=False)

`correspond.tools:<name>` references for the tools the server exposes.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

```pycon
>>> "correspond.tools:read" in refs(), "correspond.tools:send" in refs(), "correspond.tools:send" in refs(allow_send=True)
(True, False, True)
```
