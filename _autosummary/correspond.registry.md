# correspond.registry

Which channels exist: the built-in channel table, the registry built from it, and what each channel needs.

The table (`CHANNELS`) is the one place a channel is described: its adapter’s
factory, the optional modules and extra it needs, the binaries and platforms, and every
setting with its environment variable and where to get it. Adapters read settings through
[`value()`](#correspond.registry.value) and [`require()`](#correspond.registry.require), so a variable name is written once.

The process registry ([`channels()`](#correspond.registry.channels)) is an `xdol.Registry` built from the table on
first use: a channel registers only when its optional modules import, and its adapter is
constructed only when first asked for. [`check_requirements()`](#correspond.registry.check_requirements) explains what a channel
is missing, without printing a secret.

```pycon
>>> info("telegram").setting("token").env
'TELEGRAM_BOT_TOKEN'
>>> info("discord").planned
'https://github.com/thorwhalen/correspond/issues/2'
```

### Functions

| [`build_registry`](#correspond.registry.build_registry)([infos, name])                     | A fresh `xdol.Registry` with a lazy entry for every built channel whose optional modules import.                                                             |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`channels`](#correspond.registry.channels)()                                        | The process registry: built on first use, then shared.                                                                                                       |
| [`check_requirements`](#correspond.registry.check_requirements)(channel, \*[, registry, ...])  | What a channel needs and what is missing: the install command, binaries, platform, and every setting's source.                                               |
| [`info`](#correspond.registry.info)(channel)                                     | The table entry for a built-in channel.                                                                                                                      |
| [`load_factory`](#correspond.registry.load_factory)(ref)                                 | The object a `module:attribute` reference names.                                                                                                             |
| [`register_channel`](#correspond.registry.register_channel)(adapter, \*[, name, ...])        | Add an adapter (tests, or a channel defined outside correspond).                                                                                             |
| [`require`](#correspond.registry.require)(channel, key, \*[, config, run])          | A channel setting's value, or [`MissingRequirement`](correspond.errors.md#correspond.errors.MissingRequirement) saying how to set it. |
| [`resolve`](#correspond.registry.resolve)(channel, setting, \*[, config, run, ...]) | `(value, source)`; source is `env`, `keychain`, `config`, `default` or `missing`.                                                                            |
| [`unregister_channel`](#correspond.registry.unregister_channel)(name, \*[, registry])          | Remove a channel from the registry.                                                                                                                          |
| [`value`](#correspond.registry.value)(channel, key, \*[, config, run, keychain])  | A channel setting's value, or `None` when it is not set and has no default (`keychain=False`: skip the Keychain).                                            |

### Classes

| [`ChannelInfo`](#correspond.registry.ChannelInfo)(\*, name, summary[, factory, ...])    | Everything correspond knows about a channel before constructing its adapter.                       |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| [`Setting`](#correspond.registry.Setting)(\*, key, env, what[, where, secret, ...]) | One value a channel reads: its config key, its environment variable, what it is and how to get it. |

### *class* correspond.registry.ChannelInfo(, name, summary, factory='', modules=(), extra=None, binaries=(), platforms=(), settings=(), notes=(), check='', planned=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Everything correspond knows about a channel before constructing its adapter.

#### setting(key)

The setting named `key`.

* **Return type:**
  [`Setting`](#correspond.registry.Setting)

### *class* correspond.registry.Setting(, key, env, what, where='', secret=False, required=False, default=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One value a channel reads: its config key, its environment variable, what it is and how to get it.

A `secret` is never read from the config file: it comes from `env`, else (on macOS)
the Keychain.

### correspond.registry.build_registry(infos=(ChannelInfo(name='github', summary="GitHub issues, pull request conversations and discussions, through the gh CLI (the machine's gh login; correspond holds no token)", factory='correspond.channels.github:GitHub', modules=(), extra=None, binaries=(('gh',),), platforms=(), settings=(Setting(key='webhook_secret', env='GITHUB_WEBHOOK_SECRET', what='the webhook secret, used only to verify deliveries', where='the secret set on the repository or organization webhook (Settings, Webhooks)', secret=True, required=False, default=None),), notes=('log in once with \`gh auth login\` (https://cli.github.com/); check with \`gh auth status\`',), check='', planned=None), ChannelInfo(name='email', summary='Email: IMAP to read and listen, SMTP to send, with the standard library', factory='correspond.channels.mail:Email', modules=(), extra=None, binaries=(), platforms=(), settings=(Setting(key='imap_host', env='CORRESPOND_EMAIL_IMAP_HOST', what='the IMAP server', where="your mail provider's IMAP settings", secret=False, required=True, default=None), Setting(key='imap_port', env='CORRESPOND_EMAIL_IMAP_PORT', what='the IMAP port (TLS)', where='', secret=False, required=False, default='993'), Setting(key='smtp_host', env='CORRESPOND_EMAIL_SMTP_HOST', what='the SMTP server', where="your mail provider's SMTP settings", secret=False, required=True, default=None), Setting(key='smtp_port', env='CORRESPOND_EMAIL_SMTP_PORT', what='the SMTP port: 465 for TLS, 587 for STARTTLS', where='', secret=False, required=False, default='465'), Setting(key='user', env='CORRESPOND_EMAIL_USER', what='the mailbox login, usually its address', where='', secret=False, required=True, default=None), Setting(key='password', env='CORRESPOND_EMAIL_PASSWORD', what='the mailbox password, preferably an app password', where="your mail provider's app-password page (for Gmail: https://myaccount.google.com/apppasswords)", secret=True, required=True, default=None), Setting(key='from_address', env='CORRESPOND_EMAIL_FROM', what='the From address of sends (default: the login)', where='', secret=False, required=False, default=None), Setting(key='folder', env='CORRESPOND_EMAIL_FOLDER', what='the folder read and listened to', where='', secret=False, required=False, default='INBOX'), Setting(key='trusted_authserv_ids', env='CORRESPOND_EMAIL_TRUSTED_AUTHSERV_IDS', what='comma-separated authserv-ids of your own receiving server; only their Authentication-Results can make a sender \`domain\`', where='the topmost Authentication-Results header of a message your provider delivered (for Gmail: mx.google.com)', secret=False, required=False, default=None), Setting(key='own_domains', env='CORRESPOND_EMAIL_OWN_DOMAINS', what="comma-separated mail domains that are your own (and their subdomains); a recipient anywhere else makes an email's audience external", where='', secret=False, required=False, default=None), Setting(key='lists', env='CORRESPOND_EMAIL_LISTS', what='comma-separated mailing-list addresses, or @domain for a whole list server; local parts such as list, all, team, dev, announce and info count as lists anyway', where='', secret=False, required=False, default=None)), notes=(), check='', planned=None), ChannelInfo(name='ntfy', summary='Push notifications through an ntfy server (send only)', factory='correspond.channels.ntfy:Ntfy', modules=(), extra=None, binaries=(), platforms=(), settings=(Setting(key='url', env='NTFY_URL', what='the ntfy server', where='', secret=False, required=False, default='https://ntfy.sh'), Setting(key='topic', env='NTFY_TOPIC', what='the default topic, used by \`ntfy:\` with no topic', where='any long, hard-to-guess string; subscribe to it in the ntfy app (https://ntfy.sh)', secret=True, required=False, default=None), Setting(key='token', env='NTFY_TOKEN', what='an access token, for a server that requires one', where="your ntfy server's account page", secret=True, required=False, default=None), Setting(key='topic_remote', env='CORRESPOND_NTFY_TOPIC_REMOTE', what='an ssh host asked for the default topic when neither the environment nor the Keychain has it', where='', secret=False, required=False, default=None), Setting(key='topic_remote_file', env='CORRESPOND_NTFY_TOPIC_REMOTE_FILE', what='the file on that host holding a NTFY_TOPIC=... line', where='', secret=False, required=False, default=None), Setting(key='denies_anonymous_read', env='CORRESPOND_NTFY_DENIES_ANONYMOUS_READ', what='true when the server denies anonymous reads (its auth-default-access), so only the accounts it grants can read a topic; unset, anyone who knows a topic can', where='', secret=False, required=False, default=None), Setting(key='cache_duration', env='CORRESPOND_NTFY_CACHE_DURATION', what='how long the server keeps a message for subscribers who connect later (its cache-duration)', where='', secret=False, required=False, default='12h')), notes=('anyone who knows an unauthenticated topic can publish to it and read it: treat it as a secret',), check='', planned=None), ChannelInfo(name='macos', summary='Notification Centre banners on this Mac (send only)', factory='correspond.channels.macos:MacOS', modules=(), extra=None, binaries=(('terminal-notifier', 'osascript'),), platforms=('darwin',), settings=(), notes=('terminal-notifier (\`brew install terminal-notifier\`) keeps bodies intact; osascript is the fallback',), check='', planned=None), ChannelInfo(name='telegram', summary='A Telegram bot over the Bot API: listen with getUpdates, read what was logged, send, edit, react', factory='correspond.channels.telegram:Telegram', modules=(), extra=None, binaries=(), platforms=(), settings=(Setting(key='token', env='TELEGRAM_BOT_TOKEN', what='the bot token', where='create a bot with BotFather in Telegram (https://core.telegram.org/bots/tutorial)', secret=True, required=True, default=None), Setting(key='api_url', env='CORRESPOND_TELEGRAM_API_URL', what='the Bot API server', where='', secret=False, required=False, default='https://api.telegram.org')), notes=('a bot can write only to chats that wrote to it first, or groups it was added to', 'privacy mode (on by default) hides group messages that do not address the bot'), check='', planned=None), ChannelInfo(name='webinbox', summary="Reports posted from web pages to correspond's ASGI collector, and a reader over what it stored", factory='correspond.channels.webinbox:WebInbox', modules=(), extra=None, binaries=(), platforms=(), settings=(Setting(key='sites', env='CORRESPOND_WEBINBOX_SITES', what='comma-separated site names the collector accepts', where='', secret=False, required=False, default=None), Setting(key='origins', env='CORRESPOND_WEBINBOX_ORIGINS', what='comma-separated page origins allowed to post, e.g. https://app.example.org', where='', secret=False, required=False, default=None), Setting(key='secret', env='CORRESPOND_WEBINBOX_SECRET', what="the HMAC key shared with the host application's server, which signs its logged-in user (a single site; several sites need CORRESPOND_WEBINBOX_SECRET_<SITE> each)", where='generate one with: python -c "import secrets; print(secrets.token_hex(32))"', secret=True, required=False, default=None), Setting(key='max_age_s', env='CORRESPOND_WEBINBOX_MAX_AGE_S', what='how long a signed identity stays valid, in seconds', where='', secret=False, required=False, default='86400'), Setting(key='rate_per_minute', env='CORRESPOND_WEBINBOX_RATE_PER_MINUTE', what='reports accepted per client per minute, per site', where='', secret=False, required=False, default='10'), Setting(key='burst', env='CORRESPOND_WEBINBOX_BURST', what='reports a client may send in a burst', where='', secret=False, required=False, default='5'), Setting(key='max_body_bytes', env='CORRESPOND_WEBINBOX_MAX_BODY_BYTES', what='the largest request accepted, attachments included', where='', secret=False, required=False, default='5000000'), Setting(key='trusted_proxies', env='CORRESPOND_WEBINBOX_TRUSTED_PROXIES', what='how many reverse proxies of yours stand in front of the collector; the address rate-limited is read that many hops from the right of X-Forwarded-For (0: the connecting address)', where='', secret=False, required=False, default='0')), notes=('serve the collector on localhost behind your own server, e.g. \`uvicorn --factory correspond.channels.webinbox:app_from_env --host 127.0.0.1\`', 'for a reverse proxy on another host set CORRESPOND_WEBINBOX_TRUSTED_PROXIES (1 for one proxy), or every visitor shares one rate limit; uvicorn already resolves a proxy on 127.0.0.1', "several sites on one collector need a secret each, CORRESPOND_WEBINBOX_SECRET_<SITE> (upper case, - as \_) or the Keychain item correspond-webinbox-secret-<site>, so no site's server can sign for another"), check='correspond.channels.webinbox:requirements_problems', planned=None), ChannelInfo(name='discord', summary='Discord: read and post over REST, listen on the gateway', factory='', modules=(), extra='discord', binaries=(), platforms=(), settings=(), notes=(), check='', planned='https://github.com/thorwhalen/correspond/issues/2'), ChannelInfo(name='slack', summary='Slack: Socket Mode or the Events API', factory='', modules=(), extra='slack', binaries=(), platforms=(), settings=(), notes=(), check='', planned='https://github.com/thorwhalen/correspond/issues/3'), ChannelInfo(name='signal', summary='Signal through signal-cli-rest-api', factory='', modules=(), extra='signal', binaries=(), platforms=(), settings=(), notes=(), check='', planned='https://github.com/thorwhalen/correspond/issues/4'), ChannelInfo(name='apprise', summary='Apprise as a send-only writer for about 155 notification services', factory='', modules=(), extra='apprise', binaries=(), platforms=(), settings=(), notes=(), check='', planned='https://github.com/thorwhalen/correspond/issues/5')), , name='correspond channels')

A fresh `xdol.Registry` with a lazy entry for every built channel whose optional modules import.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.registry.channels()

The process registry: built on first use, then shared.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### correspond.registry.check_requirements(channel, \*, registry=None, config=None, run=<function run>)

What a channel needs and what is missing: the install command, binaries, platform, and every setting’s source. Never a secret’s value.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.registry.info(channel)

The table entry for a built-in channel.

* **Return type:**
  [`ChannelInfo`](#correspond.registry.ChannelInfo)

### correspond.registry.load_factory(ref)

The object a `module:attribute` reference names.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

### correspond.registry.register_channel(adapter, , name=None, replace=False, registry=None)

Add an adapter (tests, or a channel defined outside correspond). `replace` swaps out an existing one.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

### correspond.registry.require(channel, key, \*, config=None, run=<function run>)

A channel setting’s value, or [`MissingRequirement`](correspond.errors.md#correspond.errors.MissingRequirement) saying how to set it.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.registry.resolve(channel, setting, \*, config=None, run=<function run>, keychain=True)

`(value, source)`; source is `env`, `keychain`, `config`, `default` or `missing`.

`keychain=False` skips the Keychain, so nothing runs: what a dry run does.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### correspond.registry.unregister_channel(name, , registry=None)

Remove a channel from the registry.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### correspond.registry.value(channel, key, \*, config=None, run=<function run>, keychain=True)

A channel setting’s value, or `None` when it is not set and has no default (`keychain=False`: skip the Keychain).

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)
