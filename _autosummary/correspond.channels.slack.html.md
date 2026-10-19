# correspond.channels.slack

Slack: drafts in Slack’s own formatting; reading, polling and posting with a token; posting through a webhook.

Reference: `slack:<channel>`, a Slack channel by its name (`#` optional, lower-cased;
1-80 of `a-z`, `0-9`, `-` and `_`) or by its id (`C024BE91L`, kept as Slack
writes it). A post goes through the channel’s **incoming webhook**, which Slack ties to one
channel when it is created: `$CORRESPOND_SLACK_WEBHOOK_<CHANNEL>` (upper case, `-` as
`_` and `_` as `__`, so no two channels share a variable), else on macOS the Keychain
item `correspond-slack-webhook-<channel>`. The webhook URL is a credential: it never
appears in a reference, a plan or an error.

Three uses, one channel, each needing a little more set up:

- **A draft the operator sends.** `send(..., dry_run=True)` needs nothing configured: the
  plan holds the text rendered as Slack `mrkdwn` ([`to_mrkdwn()`](#correspond.channels.slack.to_mrkdwn)), its length against
  Slack’s limit, and says nothing was sent. That is the whole interaction when an agent
  writes the reply and a person pastes it, including on a workspace where no app will
  ever be installed.
- **A post through a webhook.** With the channel’s webhook, `send` posts the same text
  there, as the app that owns the webhook. A webhook cannot reply in a thread.
- **The Web API, with a token** (`CORRESPOND_SLACK_TOKEN`: a bot token `xoxb-` acts as
  the app, a user token `xoxp-` as the person). `read` returns a channel’s latest
  top-level messages (`conversations.history`), `listen` polls for newer ones (the
  cursor is a message `ts`), and `send` posts with `chat.postMessage`, replying in a
  thread when `reply_to` is a message’s `ts`. A channel given by name is looked up
  once with `conversations.list`. Install the app in your own workspace: Slack limits a
  commercially distributed app outside its Marketplace to one history request a minute.
  [`Slack.verify()`](#correspond.channels.slack.Slack.verify) checks a request Slack sends (`X-Slack-Signature`, with the
  > signing secret), graded `crypto`.

Slack’s `mrkdwn` is not Markdown: `*bold*`, `_italic_`, `~strike~`, `<url|text>`
links, no headings, no tables, and `&`, `<` and `>` escaped. [`to_mrkdwn()`](#correspond.channels.slack.to_mrkdwn)
converts the common Markdown into it, so a draft written as GitHub Markdown does not paste
as literal asterisks and pipes. Because every `<` in the text is escaped, a draft cannot
carry a Slack mention (`<!channel>`, `<@U...>`) that would notify people.

Messages read are `platform`: Slack authenticated the account, and correspond fetched
them itself. Neither the token nor a webhook URL appears in a plan or an error.

```pycon
>>> to_mrkdwn("**Fixed** in [#12](https://example.org/12) & shipped")
'*Fixed* in <https://example.org/12|#12> &amp; shipped'
```

### Module Attributes

| [`MAX_TEXT_CHARS`](#correspond.channels.slack.MAX_TEXT_CHARS)   | Slack truncates a message's text past this many characters.   |
|-------------------------------------------------------------------|---------------------------------------------------------------|

### Functions

| [`to_mrkdwn`](#correspond.channels.slack.to_mrkdwn)(markdown)   | Common Markdown as Slack `mrkdwn`: bold, italic, strike, links, headings, lists, code, quotes, tables as code.   |
|------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------|
| [`webhook_env`](#correspond.channels.slack.webhook_env)(name)     | The environment variable holding one name's webhook URL.                                                         |

### Classes

| [`Slack`](#correspond.channels.slack.Slack)(\*[, http, keychain, run, clock, sleep])   | Slack: drafts in Slack's own formatting; reading, polling and posting with a token; posting through a webhook.   |
|---------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------|

### correspond.channels.slack.MAX_TEXT_CHARS *= 40000*

Slack truncates a message’s text past this many characters.

### *class* correspond.channels.slack.Slack(\*, http=<function urllib_http>, keychain=None, run=<function run>, clock=<built-in function time>, sleep=<built-in function sleep>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Slack: drafts in Slack’s own formatting; reading, polling and posting with a token; posting through a webhook.

#### audience(ref, , draft=None)

The channel’s members, never listed; a shared channel reaches other workspaces.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

post; with nothing: a draft.

* **Type:**
  With a token
* **Type:**
  read, poll, post and reply in threads; with a webhook

#### parse_ref(id)

A channel’s id as Slack writes it, or its name, `#` optional, lower-cased.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

Top-level messages newer than `cursor` (a `ts`), oldest first; a first poll looks back a day.

#### read(ref, , since=None, limit=None)

The channel’s latest top-level messages (`limit`, else 50), oldest first.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.html.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

Render `draft` as mrkdwn, then post it with the token (`chat.postMessage`), else through the channel’s webhook.

`draft.reply_to` (a message’s `ts`) posts in that thread with a token; through a
webhook, which cannot thread, it renders in a dry run for the operator to paste.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

#### verify(headers, body)

`crypto` when `X-Slack-Signature` matches the signing secret over a fresh timestamp, `forged` otherwise.

* **Return type:**
  [`Authenticity`](correspond.model.html.md#correspond.model.Authenticity)

### correspond.channels.slack.to_mrkdwn(markdown)

Common Markdown as Slack `mrkdwn`: bold, italic, strike, links, headings, lists, code, quotes, tables as code.

Only `http`, `https` and `mailto` targets become links: a Markdown link to
`!channel` or `@U123` stays text, so no draft can notify a channel or a person.

```pycon
>>> to_mrkdwn("# Release\n- **new**: _faster_ export\n- ~~old~~ gone")
'*Release*\n• *new*: _faster_ export\n• ~old~ gone'
>>> to_mrkdwn("> quoted <b> & `a < b`")
'> quoted &lt;b&gt; &amp; `a &lt; b`'
>>> to_mrkdwn("[ping](!channel)")
'ping (!channel)'
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.channels.slack.webhook_env(name)

The environment variable holding one name’s webhook URL.

```pycon
>>> webhook_env("team-alerts"), webhook_env("team_alerts")
('CORRESPOND_SLACK_WEBHOOK_TEAM_ALERTS', 'CORRESPOND_SLACK_WEBHOOK_TEAM__ALERTS')
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)
