# correspond.channels.slack

Slack: messages formatted as Slack writes them, posted through an incoming webhook.

Reference: `slack:<channel>`, a Slack channel by its name (`#` optional, lower-cased;
1-80 of `a-z`, `0-9`, `-` and `_`) or by its id (`C024BE91L`, kept as Slack
writes it). A post goes through the channel’s **incoming webhook**, which Slack ties to one
channel when it is created: `$CORRESPOND_SLACK_WEBHOOK_<CHANNEL>` (upper case, `-` as
`_` and `_` as `__`, so no two channels share a variable), else on macOS the Keychain
item `correspond-slack-webhook-<channel>`. The webhook URL is a credential: it never
appears in a reference, a plan or an error.

Two uses, one channel:

- **A draft the operator sends.** `send(..., dry_run=True)` needs nothing configured: the
  plan holds the text rendered as Slack `mrkdwn` ([`to_mrkdwn()`](#correspond.channels.slack.to_mrkdwn)), its length against
  Slack’s limit, and says nothing was sent. That is the whole interaction when an agent
  writes the reply and a person pastes it, including on a workspace where no app will
  ever be installed.
- **A post.** With a webhook for the name, `send` posts the same text to its channel.

Slack’s `mrkdwn` is not Markdown: `*bold*`, `_italic_`, `~strike~`, `<url|text>`
links, no headings, no tables, and `&`, `<` and `>` escaped. [`to_mrkdwn()`](#correspond.channels.slack.to_mrkdwn)
converts the common Markdown into it, so a draft written as GitHub Markdown does not paste
as literal asterisks and pipes. Because every `<` in the text is escaped, a draft cannot
carry a Slack mention (`<!channel>`, `<@U...>`) that would notify people.

Reading and listening need a Slack app and token (`slack-sdk`), and are not built yet.

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

| [`Slack`](#correspond.channels.slack.Slack)(\*[, http, keychain])   | Slack: a draft in Slack's own formatting, and a post through an incoming webhook.   |
|--------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|

### correspond.channels.slack.MAX_TEXT_CHARS *= 40000*

Slack truncates a message’s text past this many characters.

### *class* correspond.channels.slack.Slack(\*, http=<function urllib_http>, keychain=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Slack: a draft in Slack’s own formatting, and a post through an incoming webhook.

#### audience(ref, , draft=None)

The members of the webhook’s channel, never listed; a shared channel reaches other workspaces.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

A dry run always; a post where a webhook is configured.

#### parse_ref(id)

A channel’s id as Slack writes it, or its name, `#` optional, lower-cased.

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### send(ref, draft, , dry_run=False)

Render `draft` as mrkdwn; post it through the channel’s webhook unless `dry_run`.

A reply (`draft.reply_to`, the message’s `ts`) renders in a dry run, for the
operator to paste in that thread; a webhook cannot post one.

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

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
