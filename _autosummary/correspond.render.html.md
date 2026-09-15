# correspond.render

Turning a tool’s result into terminal output: `(stdout, stderr, exit code)`.

Used by the CLI only. The rules, in order: a result with a `value` prints just the value
(one line per item, so it pipes); otherwise its `text`, else its `summary`. A result
with `ok: False` exits 1 and puts its summary on stderr.

```pycon
>>> render({"ok": True, "value": "github:octocat/hello-world#1", "summary": "…"})
('github:octocat/hello-world#1', '', 0)
>>> render({"ok": False, "summary": "ntfy does not support react"})
('', 'ntfy does not support react', 1)
```

### Functions

| [`render`](#correspond.render.render)(result)   | `(stdout, stderr, exit_code)` for one tool result.   |
|-------------------------------------------------------------------|------------------------------------------------------|

### correspond.render.render(result)

`(stdout, stderr, exit_code)` for one tool result.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`int`](https://docs.python.org/3/builtins/functions.html#int)]
