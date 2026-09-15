# correspond.channels.webinbox

The web inbox: a small ASGI collector web pages post reports to, and the channel that reads them.

**The collector** ([`mk_app()`](#correspond.channels.webinbox.mk_app); or `uvicorn --factory correspond.channels.webinbox:app_from_env`)
accepts `POST /<site>/reports` with a JSON body:

```default
{"text": "The export drops the last row.",
 "name": "Ada", "email": "ada@example.org",
 "page": "https://app.example.org/export",
 "context": {"viewport": "1280x800"},
 "identity": {"user": "u-42", "name": "Ada", "email": null, "issued_at": 1757592000, "sig": "<hex>"},
 "attachments": [{"name": "screen.png", "media_type": "image/png", "data": "<base64>"}]}
```

Only `text` is required.

- **Identity.** Without `identity` a report is `claimed` (`name` and `email` are
  whatever was typed). With one, the host application’s *server* has signed its logged-in
  user with the site’s secret ([`sign_identity()`](#correspond.channels.webinbox.sign_identity)); a valid, fresh signature makes the
  report `bound`. An invalid or expired signature is refused with 401 and nothing is
  stored.
- **Origins.** A browser’s `Origin` must be on the site’s allowlist (403 otherwise); a
  request without one is refused unless the site allows server-to-server posts. CORS
  preflight is answered for allowed origins only. An origin check stops other pages from
  posting through a visitor’s browser; it authenticates no one.
- **Limits.** A per-client token bucket (429 with `Retry-After`), the body size (413),
  caps on text, context and attachments, and a media-type allowlist.
- **Storage.** A report is JSON in `store` under `<site>/<sortable id>.json`;
  attachments are bytes in `blobs` under their SHA-256, referenced and never inlined.

The app binds nothing: run it on localhost behind your own server, and tell it how many
reverse proxies stand in front (`trusted_proxies`) so the rate limit applies per visitor
rather than to the proxy. The limiter lives in memory, per process. Each site has its own
secret, so a server that can sign for one site cannot sign for another.

**The channel** ([`WebInbox`](#correspond.channels.webinbox.WebInbox)): `webinbox:<site>` reads and listens to what the
collector stored. It has no writer: a reply to a reporter goes out on another channel.

The identity payload is plain lines, so any server language can sign it:

```pycon
>>> identity_payload("example-site", "u-42", 1757592000, name="Ada")
b'v1\nexample-site\n1757592000\nu-42\nAda\n'
```

### Functions

| [`app_from_env`](#correspond.channels.webinbox.app_from_env)()                                     | The collector configured from the environment and the `[webinbox]` config table (for `uvicorn --factory`).                                   |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| [`identity_payload`](#correspond.channels.webinbox.identity_payload)(site, user, issued_at, \*[, ...]) | The bytes a host application signs for its logged-in user: `v1`, site, issued-at, user, name, email, one per line.                           |
| [`mk_app`](#correspond.channels.webinbox.mk_app)(sites, \*[, store, blobs, ...])             | The collector as an ASGI app, storing reports in `store` and attachments in `blobs` (files under the data root by default).                  |
| [`requirements_problems`](#correspond.channels.webinbox.requirements_problems)()                            | What would stop the collector starting from this configuration; nothing when no site is configured, since reading stored reports needs none. |
| [`sign_identity`](#correspond.channels.webinbox.sign_identity)(secret, site, user, \*[, name, ...]) | What a host application's server gives its page for the logged-in user; the page sends it as `identity`.                                     |
| [`site_secret_env`](#correspond.channels.webinbox.site_secret_env)(site)                              | The environment variable holding one site's own secret.                                                                                      |
| [`verify_identity`](#correspond.channels.webinbox.verify_identity)(identity, \*, site, now)           | `bound` for a valid, fresh signature from the site's host application; `forged` with the reason otherwise.                                   |

### Classes

| [`Site`](#correspond.channels.webinbox.Site)(\*, name[, origins, secret, ...])           | One inbox the collector accepts reports for: who may post from where, and the key signed identities are checked with.   |
|---------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------|
| [`TokenBucket`](#correspond.channels.webinbox.TokenBucket)(\*, rate_per_minute, burst[, clock]) | Per-key token buckets: `rate_per_minute` sustained, `burst` at once.                                                    |
| [`WebInbox`](#correspond.channels.webinbox.WebInbox)(\*[, store, blobs])                     | Reports the collector stored, as a channel: read and listen per site.                                                   |

### *class* correspond.channels.webinbox.Site(, name, origins=(), secret=None, allow_no_origin=False, max_age_s=86400)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One inbox the collector accepts reports for: who may post from where, and the key signed identities are checked with.

### *class* correspond.channels.webinbox.TokenBucket(\*, rate_per_minute, burst, clock=<built-in function monotonic>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Per-key token buckets: `rate_per_minute` sustained, `burst` at once. In memory, one process.

```pycon
>>> bucket = TokenBucket(rate_per_minute=60, burst=1, clock=lambda: 0.0)
>>> bucket.take("client"), bucket.take("client")
(None, 1.0)
```

#### take(key)

`None` when a token was available (and is now spent); otherwise the seconds until one is.

* **Return type:**
  [`float`](https://docs.python.org/3/builtins/functions.html#float) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *class* correspond.channels.webinbox.WebInbox(, store=None, blobs=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Reports the collector stored, as a channel: read and listen per site.

#### *property* blobs *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [bytes](https://docs.python.org/3/builtins/stdtypes.html#bytes)]*

The attachment store, keyed by SHA-256.

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Read and listen; no writer.

#### parse_ref(id)

A site name.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

Reports stored after the one `cursor` names (a report id).

#### read(ref, , since=None, limit=None)

The site’s reports, oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### *property* store *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]*

The report store (JSON files under the data root by default).

### correspond.channels.webinbox.app_from_env()

The collector configured from the environment and the `[webinbox]` config table (for `uvicorn --factory`).

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.webinbox.identity_payload(site, user, issued_at, , name=None, email=None)

The bytes a host application signs for its logged-in user: `v1`, site, issued-at, user, name, email, one per line.

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

### correspond.channels.webinbox.mk_app(sites, \*, store=None, blobs=None, rate_per_minute=10, burst=5, max_body_bytes=5000000, trusted_proxies=0, clock=<built-in function time>)

The collector as an ASGI app, storing reports in `store` and attachments in `blobs` (files under the data root by default).

Behind reverse proxies of your own, set `trusted_proxies` to how many stand in front,
or every visitor shares the proxy’s rate limit.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis), [`Awaitable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Awaitable)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### correspond.channels.webinbox.requirements_problems()

What would stop the collector starting from this configuration; nothing when no site is configured, since reading stored reports needs none.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### correspond.channels.webinbox.sign_identity(secret, site, user, , name=None, email=None, issued_at=None)

What a host application’s server gives its page for the logged-in user; the page sends it as `identity`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.channels.webinbox.site_secret_env(site)

The environment variable holding one site’s own secret.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> site_secret_env("example-site")
'CORRESPOND_WEBINBOX_SECRET_EXAMPLE_SITE'
```

### correspond.channels.webinbox.verify_identity(identity, , site, now)

`bound` for a valid, fresh signature from the site’s host application; `forged` with the reason otherwise.

* **Return type:**
  [`Authenticity`](correspond.model.html.md#correspond.model.Authenticity)
