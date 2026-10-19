# correspond.channels.discord

Discord: a bot reading and posting in channels over REST, through discorddol.

Reference: `discord:<channel id>`, a channel or a thread (Discord numbers both with
snowflakes). A bot reads only the channels its server roles let it see, and since Discord
made message content a privileged intent, it reads the text of a message only when the
Message Content intent is enabled for it (Developer Portal, Bot, Privileged Gateway
Intents; a bot in more than 100 servers needs Discord’s approval) or when the message
mentions it.

- `read` fetches the channel’s history (the latest 50 messages unless `limit` says).
- `listen` polls the channel for messages newer than the last one seen (the cursor is
  that message’s id; a first poll looks back a day). It does not open the gateway, so it
  sees new messages, not edits or deletions, and one channel per reference.
- `send` posts a message (2,000 characters at most); a title is prepended in bold.

Senders are `platform`: Discord authenticated the account, and correspond fetched the
message from the API itself. The bot token never appears in an error or a plan.

Needs the `discord` extra (discorddol, over discord.py in REST-only mode), imported on
first use, not when the channel is listed.

```pycon
>>> Discord().parse_ref("123456789012345678").encoded
'discord:123456789012345678'
```

### Classes

| [`Discord`](#correspond.channels.discord.Discord)(\*[, backend, http, run])   | A Discord bot over REST: read a channel, poll it for new messages, post to it.   |
|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|

### *class* correspond.channels.discord.Discord(\*, backend=None, http=<function urllib_http>, run=<function run>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A Discord bot over REST: read a channel, poll it for new messages, post to it.

#### audience(ref, , draft=None)

The members of the channel’s server who can view it: a group nobody lists here.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* backend *: [Any](https://docs.python.org/3/library/typing.html#typing.Any)*

discorddol’s REST backend with the configured bot token, or the one passed in.

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

A bot’s view of Discord, over REST.

#### parse_ref(id)

A channel or thread id.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

Messages newer than `cursor` (a message id), oldest first; a first poll looks back a day.

#### read(ref, , since=None, limit=None)

The channel’s latest messages (`limit`, else 50), oldest first; with `since`, only those after it.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

Post `draft.text` (the title, if any, prepended in bold) to the channel.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)
