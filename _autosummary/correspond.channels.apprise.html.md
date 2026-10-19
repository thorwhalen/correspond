# correspond.channels.apprise

Apprise: send-only notifications to the long tail of services (about 155), each named by an alias.

### References

`apprise:<alias>`, an alias being 1-64 of `a-z`, `0-9` and `-` (case is
folded; `all` and `always` are Apprise’s match-everything tags, so they are refused). A
reference never holds an Apprise URL, because the URL
carries the service’s credentials (`discord://webhook_id/webhook_token`,
`tgram://bot_token/chat_id`…). The alias is resolved, in this order:

1. `$CORRESPOND_APPRISE_URL_<ALIAS>` (upper case, `-` as `_`), holding ONE URL;
2. on macOS, the Keychain item `correspond-apprise-url-<alias>`, likewise;
3. the local Apprise configuration file named by `config` in the `[apprise]` table (or
   `$CORRESPOND_APPRISE_CONFIG`), where the alias is an Apprise **tag**: every URL in the
   file carrying exactly that tag is notified (see the configuration-file documentation
   linked from [https://pypi.org/project/apprise/](https://pypi.org/project/apprise/)). URLs tagged `always` are NOT added:
   an alias names its recipients, and a typo must not reach anyone.

A dry run reads only the environment and the config file: a Keychain alias is looked up when
sending, and the plan says so. The plan names the service (the URL’s scheme), never the URL,
and Apprise’s own logging, which prints URLs, is silenced while correspond calls it.

Needs the `apprise` extra: `pip install "correspond[apprise]"`. It is imported when a
message is sent, not when the channel is listed.

```pycon
>>> Apprise().parse_ref("team-alerts").kind
'alias'
```

### Functions

| [`alias_env`](#correspond.channels.apprise.alias_env)(alias)   | The environment variable holding one alias's Apprise URL.   |
|---------------------------------------------------------------------|-------------------------------------------------------------|

### Classes

| [`Apprise`](#correspond.channels.apprise.Apprise)(\*[, apprise, keychain, ...])   | Apprise URLs and tags, behind aliases: send only.   |
|------------------------------------------------------------------------------------------|-----------------------------------------------------|

### *class* correspond.channels.apprise.Apprise(\*, apprise=<function \_import_apprise>, keychain=None, keychain_available=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Apprise URLs and tags, behind aliases: send only.

#### audience(ref, , draft=None)

Public and incomplete: Apprise reaches anything from one phone to a public channel, and nothing here can tell which.

Nothing is resolved or asked: not the URL, not the service.

* **Return type:**
  [`Audience`](correspond.model.html.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.html.md#correspond.model.Capabilities)*

Send only, with a title; who reads depends on the service behind each alias.

#### parse_ref(id)

An alias: 1-64 of `a-z`, `0-9` and `-`, case folded; never `all` or `always`.

One spelling per alias, so each has exactly one environment variable and one
Keychain item (`a-b` and `a_b` would share `..._A_B`).

* **Return type:**
  [`ConversationRef`](correspond.model.html.md#correspond.model.ConversationRef)

#### send(ref, draft, , dry_run=False)

Notify every service behind the alias with `draft.text` (and its title).

* **Return type:**
  [`SendResult`](correspond.model.html.md#correspond.model.SendResult)

### correspond.channels.apprise.alias_env(alias)

The environment variable holding one alias’s Apprise URL.

```pycon
>>> alias_env("team-alerts")
'CORRESPOND_APPRISE_URL_TEAM_ALERTS'
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)
