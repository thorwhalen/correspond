# correspond.channels

The built-in channel adapters, one module each.

The registry imports a module only when its channel is first used, so an adapter’s
dependencies (none, for the v0.1 adapters) are never paid for by code that does not use
that channel.

### Modules

| [`github`](correspond.channels.github.html.md#module-correspond.channels.github)     | GitHub: issues, pull request conversations and discussions, through the `gh` CLI.                 |
|-----------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| [`macos`](correspond.channels.macos.html.md#module-correspond.channels.macos)       | macOS Notification Centre: a banner on this Mac (send only).                                      |
| [`mail`](correspond.channels.mail.html.md#module-correspond.channels.mail)         | Email: IMAP to read and listen, SMTP to send, with the standard library.                          |
| [`ntfy`](correspond.channels.ntfy.html.md#module-correspond.channels.ntfy)         | ntfy: push notifications to a phone or a desktop through an ntfy server (send only).              |
| [`telegram`](correspond.channels.telegram.html.md#module-correspond.channels.telegram) | Telegram: a bot over the Bot API, with the standard library.                                      |
| [`webinbox`](correspond.channels.webinbox.html.md#module-correspond.channels.webinbox) | The web inbox: a small ASGI collector web pages post reports to, and the channel that reads them. |
