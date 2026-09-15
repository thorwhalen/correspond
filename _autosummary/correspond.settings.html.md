# correspond.settings

Where correspond keeps state and reads configuration, and how it finds a secret.

- **The data root**: the `data_dir` argument, else `$CORRESPOND_DATA_DIR`, else
  `data_dir` in the config file, else `~/.local/share/correspond`
  (`%LOCALAPPDATA%\correspond` on Windows). Each kind of state has its own folder under
  it (`cursors/`, `webinbox/`, `telegram/`); nothing is written into a repository.
- **The config file**: `$CORRESPOND_CONFIG`, else `~/.config/correspond/config.toml`
  (`$XDG_CONFIG_HOME`; `%APPDATA%` on Windows). One table per channel:
  ```default
  [ntfy]
  url = "https://ntfy.example.org"
  topic_keychain_service = "my-ntfy-topic"
  ```
- **A value** comes from its environment variable, else the channel’s table in the config
  file, else its default. **A secret** is never read from the config file: it comes from
  its environment variable, else (on macOS) a Keychain item whose service name is
  `<key>_keychain_service` in the config (default `correspond-<channel>-<key>`).

Nothing here prints or logs a value; [`mask()`](#correspond.settings.mask) is for showing that one is set.

### Functions

| [`channel_config`](#correspond.settings.channel_config)(channel, \*[, config])        | The `[channel]` table of the config file (`{}` when absent).                                                               |
|-----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|
| [`config_path`](#correspond.settings.config_path)()                                | `$CORRESPOND_CONFIG`, else `~/.config/correspond/config.toml`.                                                             |
| [`data_dir`](#correspond.settings.data_dir)([data_dir])                         | The data root: the argument, else `$CORRESPOND_DATA_DIR`, else `data_dir` in the config, else `~/.local/share/correspond`. |
| [`keychain_available`](#correspond.settings.keychain_available)()                         | Whether a Keychain lookup can happen here (macOS with the `security` tool).                                                |
| [`keychain_get`](#correspond.settings.keychain_get)(service, \*[, run])             | A generic password from the macOS Keychain, or `""` when absent, not on macOS, or slow.                                    |
| [`keychain_service`](#correspond.settings.keychain_service)(channel, key, \*[, config]) | The macOS Keychain service a secret is looked up under.                                                                    |
| [`load_config`](#correspond.settings.load_config)([path])                          | The parsed config file, or `{}` when there is none.                                                                        |
| [`mask`](#correspond.settings.mask)(value)                                  | Enough of a secret to recognise it, never enough to use it.                                                                |

### correspond.settings.channel_config(channel, , config=None)

The `[channel]` table of the config file (`{}` when absent).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.settings.config_path()

`$CORRESPOND_CONFIG`, else `~/.config/correspond/config.toml`.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### correspond.settings.data_dir(data_dir=None)

The data root: the argument, else `$CORRESPOND_DATA_DIR`, else `data_dir` in the config, else `~/.local/share/correspond`.

The environment variable and the config value must be absolute paths: a relative one
would put state wherever the current directory happens to be (a repository, say).

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

```pycon
>>> data_dir("state").is_absolute()
True
```

### correspond.settings.keychain_available()

Whether a Keychain lookup can happen here (macOS with the `security` tool).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### correspond.settings.keychain_get(service, \*, run=<function run>)

A generic password from the macOS Keychain, or `""` when absent, not on macOS, or slow.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### correspond.settings.keychain_service(channel, key, , config=None)

The macOS Keychain service a secret is looked up under.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> keychain_service("telegram", "token", config={})
'correspond-telegram-token'
```

### correspond.settings.load_config(path=None)

The parsed config file, or `{}` when there is none. A broken file raises, naming it.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### correspond.settings.mask(value)

Enough of a secret to recognise it, never enough to use it.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> mask("example-topic-3f9a"), mask("short"), mask(None)
('ex…9a', '…', '(not set)')
```
