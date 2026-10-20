# correspond.channels

The built-in channel adapters, one module each.

The registry imports a module only when its channel is first used, so an adapter’s
dependencies (none, for the v0.1 adapters) are never paid for by code that does not use
that channel.

### Modules

| [`apprise`](correspond.channels.apprise.html.md#module-correspond.channels.apprise)               | Apprise: send-only notifications to the long tail of services (about 155), each named by an alias.             |
|-----------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| [`discord`](correspond.channels.discord.html.md#module-correspond.channels.discord)               | Discord: a bot reading and posting in channels over REST, through discorddol.                                  |
| [`gdrive`](correspond.channels.gdrive.html.md#module-correspond.channels.gdrive)                 | Google Drive: a shared folder, read only, where files that a conversation announces arrive.                    |
| [`github`](correspond.channels.github.html.md#module-correspond.channels.github)                 | GitHub: issues, pull request conversations and discussions, through the `gh` CLI.                              |
| [`github_webhook`](correspond.channels.github_webhook.html.md#module-correspond.channels.github_webhook) | GitHub webhook receiver: `discussion_comment` deliveries, so listen sees every discussion reply.               |
| [`macos`](correspond.channels.macos.html.md#module-correspond.channels.macos)                   | macOS Notification Centre: a banner on this Mac (send only).                                                   |
| [`mail`](correspond.channels.mail.html.md#module-correspond.channels.mail)                     | Email: IMAP to read and listen, SMTP to send, with the standard library.                                       |
| [`ntfy`](correspond.channels.ntfy.html.md#module-correspond.channels.ntfy)                     | ntfy: push notifications to a phone or a desktop through an ntfy server (send only).                           |
| [`signal`](correspond.channels.signal.html.md#module-correspond.channels.signal)                 | Signal through `signal-cli-rest-api` (JSON-RPC mode): listen, read what was logged, send.                      |
| [`slack`](correspond.channels.slack.html.md#module-correspond.channels.slack)                   | Slack: drafts in Slack's own formatting; reading, polling and posting with a token; posting through a webhook. |
| [`slack_events`](correspond.channels.slack_events.html.md#module-correspond.channels.slack_events)     | Slack Events API receiver: Slack pushes a channel's messages, edits and deletions, so listen need not poll.    |
| [`telegram`](correspond.channels.telegram.html.md#module-correspond.channels.telegram)             | Telegram: a bot over the Bot API, with the standard library.                                                   |
| [`webinbox`](correspond.channels.webinbox.html.md#module-correspond.channels.webinbox)             | The web inbox: a small ASGI collector web pages post reports to, and the channel that reads them.              |
