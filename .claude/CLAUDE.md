Seams: (1) adapters `registry=` = lazily built `xdol.Registry` of stdlib adapters · (2) platform transport `run=` / `http=` / `imap=` + `smtp=` = subprocess / urllib / imaplib + smtplib · (3) state `cursors=` / `store=` / `blobs=` = dol files under the data root · (4) last routing rule `classifier=` = none (the rule chain decides).
Surfaces planned for v0.1: CLI (`cw` over `correspond.tools`), shipped `correspond` skill, MCP over stdio (`py2mcp` string refs, `[mcp]` extra). Asked, not built: Claude Code channel server, remote MCP / HTTP.
Rationale and the provisional defaults: the "v0.1 architecture: seams and surfaces" Discussion.

# correspond — dev map

## Seam table (decided before the first commit)

| # | Seam | v0.1 default (no new dependency) | Replacement you can point at |
|---|---|---|---|
| 1 | which adapters exist: `registry=` on the facade functions | `xdol.Registry`, built lazily from the built-in channel table; an entry registers only when its optional modules import | `correspond.testing.FakeChannel` (tests, and liaise's offline suite); Discord over `discorddol` (tracking issue) |
| 2 | how an adapter reaches its platform: `run=` (gh, macOS notifiers), `http=` (ntfy, Telegram), `imap=` / `smtp=` (email) | `subprocess.run`, `urllib.request`, `imaplib` / `smtplib` | the scripted fakes in the tests; `imap-tools` behind `imap=` / `smtp=` (the GitHub adapter parses `gh api` output, so a GitHub App through `githubkit` is a second GitHub adapter added at seam 1, not a `run=` swap) |
| 3 | where state lives: `cursors=` (listen), `store=` / `blobs=` (web inbox, Telegram log) | `dol` files under `~/.local/share/correspond/<kind>/` (`CORRESPOND_DATA_DIR` overrides the root) | a `dict` (tests); `s3dol` for a server's web inbox |
| 4 | the last routing rule: `classifier=` on `route` | none: bindings → thread continuity → metadata decide, otherwise the message is unrouted | liaise's "is this message about subject X?" classifier |

```
Surfaces for v0.1: CLI + shipped skill + MCP stdio (write tools only with --allow-send); Claude Code channel server and remote MCP/HTTP asked, not built
NOT seams: per-channel ref grammar, authenticity-grade and error-kind vocabularies, the seven operation names, the built-in channel table format, config file format and env var names, data-root layout, web inbox payload and identity-token format, rate and size defaults (config values), CLI rendering, dry-run plan format
```

One-command test (the definition of v0.1, fully offline):

```bash
python -m correspond.testing send fake:example/demo "hello" --dry-run \
  && python -m correspond.testing read fake:example/demo \
  && python -m correspond.testing ref fake:example/demo \
  && ! python -m correspond.testing react fake:example/demo m1 eyes   # NotSupported: react
```

## Rules for working in this repo

- **No real addresses, tokens, topics, chat ids or personal data, anywhere**: code, tests, fixtures, docs, issues, commit messages, PR text. Fictional placeholders only (`example.org` addresses, `example/demo` repositories, `example-topic`). `tests/test_no_personal_data.py` enforces the mechanical part; the rest is on you.
- **Send nothing real** while developing or testing: sends go to `correspond.testing.FakeChannel`, to a scripted transport, or through `--dry-run`. Reads of public GitHub content are fine.
- `correspond/tools.py` is the SSOT. Every surface (CLI, MCP, skill) is generated from or written against that one list. Never author a second list of operations.
- Dependency-injection parameters (`registry=`, `run=`, `http=`, `imap=`, `smtp=`, `cursors=`, `store=`, `blobs=`, `classifier=`) live in the core modules. `tools.py` takes flat, serialisable arguments only.
- Importing `correspond` must not import `cw`, `py2mcp`, `fastmcp`, `mcp` or any ASGI server (a test checks this).
- correspond knows no people. Identity stops at what the platform attests (`ChannelIdentity` + `Authenticity`); linking a handle to a person is acquaint's job.
- Listeners bind to localhost by default; anything internet-facing verifies signatures and is opt-in.
- State (cursors, web inbox reports, the Telegram log) lives under the data root, never in this repository.
