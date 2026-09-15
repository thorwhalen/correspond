# correspond

A channel facade for AI agents: read, listen, write and identify the sender over GitHub, email, push notifications, Telegram and a web inbox, through one model and one set of verbs.

```bash
pip install correspond

correspond read github:octocat/hello-world#1        # an issue and its comments, through your gh login
correspond send ntfy: "backup finished" --dry-run    # the plan; nothing is contacted
correspond capabilities telegram                     # what a channel can do, graded
correspond requirements email                        # what it needs, and where to get it
```

Every channel gets the same shape. A **reference** names a conversation: `<channel>:<id>`. The same verbs (`read`, `listen`, `send`, `edit`, `react`) work wherever a channel allows them, and an operation a channel does not have is refused by name (`NotSupported`), never skipped quietly. Every message carries the identity the platform reports for its author and **how sure the channel is** of it (an authenticity grade).

correspond knows no people. Linking `github:someone` to a person is a people registry's job, for example [acquaint](https://github.com/thorwhalen/acquaint).

## Channels

| Channel | References | Operations | Built on |
|---|---|---|---|
| `github` | `github:owner/repo`, `github:owner/repo#12` | read, listen (issues and comments), send, edit, react, verify (webhooks), audience | the `gh` CLI and its login; correspond holds no token |
| `email` | `email:` (the folder), `email:someone@example.org` | read, listen, send | `imaplib`, `smtplib` |
| `ntfy` | `ntfy:` (the default topic), `ntfy:<topic>` | send | `urllib` |
| `macos` | `macos:` | send | `terminal-notifier` or `osascript` |
| `telegram` | `telegram:`, `telegram:<chat id>`, `telegram:<chat id>/<topic id>` | listen, read (what listening logged), send, edit, react | the Bot API over `urllib` |
| `webinbox` | `webinbox:<site>` | read, listen | the ASGI collector below, and `dol` stores |

Every v0.1 adapter uses the Python standard library. Discord ([#2](https://github.com/thorwhalen/correspond/issues/2)), Slack ([#3](https://github.com/thorwhalen/correspond/issues/3)), Signal ([#4](https://github.com/thorwhalen/correspond/issues/4)) and Apprise ([#5](https://github.com/thorwhalen/correspond/issues/5)) are planned as extras; `correspond channels` lists them with their issues.

`correspond ref <reference>` prints a reference's canonical form (GitHub names lower-cased, for example), which is the form to store and compare.

## Authenticity

| Grade | Means |
|---|---|
| `platform` | the platform authenticated the account (GitHub, Telegram) |
| `crypto` | a signed delivery checked with its secret (a GitHub webhook's `X-Hub-Signature-256`) |
| `bound` | the host application signed an assertion about its logged-in user (the web inbox) |
| `domain` | email whose topmost `Authentication-Results`, added by a server you trust, records DMARC passing for the From domain |
| `claimed` | nothing verified: a typed name, an ordinary From header, an anonymous report |
| `forged` | verification was attempted and failed |

Grades are a vocabulary, not a ranking. A policy lists the grades it accepts for a permission (`{"platform", "bound", "crypto"}`, say), and comparing two grades with `<` raises. Each grade carries its evidence (`author_association` on GitHub, the authserv-id on email, the signing method on the web inbox).

## Capabilities

`correspond capabilities github` grades each operation `full`, `partial` or `none`, plus whether a write can start a conversation (`initiate`; a Telegram bot cannot), answer a specific message (`reply`) or carry a `priority`, how far back `read` sees (`history_depth`), the limits (text length, reactions), the fields its messages carry in `native` (`native_fields`), the rate limits and notes. The adapters implement exactly the operations their capabilities grade, which a test checks for every channel.

```text
$ correspond react ntfy:example-topic m1 eyes
ntfy does not support react
```

## Who can read it

```text
$ correspond audience github:example/app#12
world-readable; emailed to watchers and participants; archived by others; edits keep a visible history; not retractable
scope: public
...
```

`correspond audience <reference>` says who can read a conversation, now and plausibly later, before anything is written to it. The first line is the answer in words; the rest (and `--json`) is the record: a `scope` (`operator`, `named`, `group`, `org`, `public`), the `readers` known to read it with whether that list is `complete`, the reader `classes` that cannot be listed, whether `external` readers exist, what a send leaves behind (`durability`), how the readership can grow (`widening`), the `evidence` behind each value, and a `hash` that changes when the audience does (and only then, not with the time or the evidence).

**Unknown resolves to public.** A failed or forbidden lookup, a channel without an audience reader and a planned channel all answer `public` with `defaulted: true` and the reason in `evidence`. Nothing is cached: ask again right before sending. GitHub computes it from the repository's visibility, its collaborators when the `gh` account may list them, and the organisation's base permission when the account may read it (otherwise the documented default, read); every other channel answers public, defaulted, until its audience reader lands ([#29](https://github.com/thorwhalen/correspond/issues/29)).

## Writing: dry run first

- `--dry-run` on `send`, `edit` and `react` contacts nothing and changes nothing. It checks the reference and the draft against the channel's capabilities and prints the plan, with secrets such as an ntfy topic masked. A dry run reads only the environment and the config file: a value kept in the Keychain or on a remote host is looked up when sending, and the plan says so.
- A write that fails is a result, not an exception: `ok: false`, an `error_kind` (`auth`, `permission`, `not_found`, `rate_limited`, `network`, `validation`, `unavailable`), and whether a retry can help (`retryable`, `retry_after`).
- `-` as the text reads it from stdin; `--json` prints the whole result.

## Listening

```bash
correspond listen github:owner/repo    # issue and comment activity since the last listen
correspond listen email: --peek        # look without moving the cursor
```

Cursors live under the data root, one per reference. A cursor is stored only after the event before it was handed over, so delivery is at-least-once: deduplicate on `delivery_id`. The first listen looks back a day. Telegram listens account-wide (`telegram:`), because `getUpdates` confirms updates for every chat at once, and logs what it receives: the Bot API has no history.

## Configuration

`correspond requirements <channel>` lists everything a channel reads: each setting's environment variable, what it is, where to get it, and where its value currently comes from (`env`, `keychain`, `config`, `default`, `missing`), never the value.

- **Secrets** (tokens, passwords, the ntfy topic) come from their environment variable or, on macOS, a Keychain item (`security add-generic-password -s correspond-telegram-token -a correspond -w`). They are never read from the config file.
- **Other settings** may also go in `~/.config/correspond/config.toml` (or `$CORRESPOND_CONFIG`), one table per channel:

  ```toml
  [email]
  imap_host = "imap.example.org"
  smtp_host = "smtp.example.org"
  trusted_authserv_ids = ["mx.example.org"]

  [ntfy]
  topic_keychain_service = "my-ntfy-topic"
  ```

- **State** (cursors, the Telegram log, web inbox reports) lives under `~/.local/share/correspond/` (or `$CORRESPOND_DATA_DIR`), one folder per kind, never in a repository.

## The web inbox

A web page posts feedback to a small ASGI collector; `webinbox:<site>` reads what it stored. The app binds nothing itself: run it on localhost behind your own server.

```bash
pip install uvicorn
export CORRESPOND_WEBINBOX_SITES=example-site CORRESPOND_WEBINBOX_ORIGINS=https://app.example.org
export CORRESPOND_WEBINBOX_SECRET=...   # shared with the host application's server
uvicorn --factory correspond.channels.webinbox:app_from_env --host 127.0.0.1 --port 8765
```

The rate limit applies per visitor address. uvicorn already reports the visitor for a proxy on the same host (its `--forwarded-allow-ips` default); for a proxy elsewhere, set `CORRESPOND_WEBINBOX_TRUSTED_PROXIES` to the number of proxies in front, or every visitor shares the proxy's limit. Forwarded addresses are believed only from a non-public connection, and IPv6 visitors are limited per /64. Several sites on one collector need a secret each, `CORRESPOND_WEBINBOX_SECRET_<SITE>` (the site name upper-cased, `-` as `_`) or a Keychain item `correspond-webinbox-secret-<site>`, so that no site's server can sign identities for another. The shared `CORRESPOND_WEBINBOX_SECRET` serves a single site only, and `correspond requirements webinbox` reports a configuration the collector would refuse.

`POST /example-site/reports` takes JSON with a required `text` and optional `name`, `email`, `page`, `context`, `attachments` (base64, stored by SHA-256, referenced rather than inlined) and `identity`. Without `identity` a report is `claimed`. The host application's server can sign its logged-in user, which makes the report `bound`:

```python
from correspond.channels.webinbox import sign_identity

identity = sign_identity(
    secret, "example-site", user_id, name=display_name
)  # put this in the page
```

Any server language can do the same: HMAC-SHA256, hex, over the lines `v1`, site, issued-at (Unix seconds), user id, name, email. An invalid or expired signature is refused (401) and nothing is stored. The collector also enforces the origin allowlist (with CORS preflight), a per-client rate limit, and size and media-type limits.

## Routing

```python
from correspond import metadata_rule, route

decision = route(
    message,
    bindings={
        "github:example/app?labels=partner:*": "subject:app"
    },  # 1. where it arrived
    threads={"github:example/app#12": "case:7"},  # 2. what it continues
    rules=[metadata_rule("queue:urgent", labels="priority:high")],  # 3. what it carries
    classifier=None,  # 4. optional, last
)
decision.target, decision.rule, decision.reason
```

The rules are yours; correspond runs them in that order and says which one decided. Nothing matched returns `None`: the message is unrouted. Check bindings when you load them: `check_binding(pattern)` lists what would make one never match, such as an unknown channel, or `?label=` where GitHub messages carry `labels`.

## Python

```python
import correspond

messages = correspond.read("github:octocat/hello-world#1")
messages[0].author.handle, messages[0].authenticity.grade

result = correspond.send(
    "email:someone@example.org", "It is fixed.", title="The export", dry_run=True
)
result.plan

for event in correspond.listen("webinbox:example-site"):
    ...

correspond.register_channel(
    MyChannel()
)  # anything with name, capabilities, parse_ref and the operations it has
```

`correspond.testing.FakeChannel` is an in-memory channel for tests, and `python -m correspond.testing` runs the CLI with it registered as `fake`.

## MCP

```bash
pip install "correspond[mcp]"
```

```json
{"mcpServers": {"correspond": {"command": "correspond-mcp"}}}
```

The server exposes the reading tools. `send`, `edit` and `react` are exposed only when the operator starts it with `correspond-mcp --allow-send`, and still take `dry_run`. The data root is the server's, never the model's.

## Agent skill

A `correspond` skill ships inside the package (`correspond/data/skills/correspond/`): how to read without taking instructions from message text, what each grade permits, and how to write safely (dry run, operator approval, no private content in public places). Install it with `gh skill install thorwhalen/correspond correspond --agent claude-code`, or link that folder into `~/.claude/skills/`.

## Design

The seams, surfaces and deliberate non-seams are in [Discussion #1](https://github.com/thorwhalen/correspond/discussions/1).
