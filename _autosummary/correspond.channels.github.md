# correspond.channels.github

GitHub: issues, pull request conversations and discussions, through the `gh` CLI.

correspond never holds a GitHub token: every call is `gh api`, authenticated as whatever
account `gh` is logged in as on this machine, so writes act as that user. Request bodies
go to `gh` on stdin, never on its command line, so a long comment cannot hit the argument
limit and never shows in the process table.

### References

- `github:owner/repo`: the repository. `read` lists recent open issues and pull
  requests, `listen` polls its issue and comment activity, `send` opens an issue (a
  title is required).
- `github:owner/repo#N`: an issue, a pull request, or a discussion. GitHub numbers the
  three in one sequence; correspond asks which. `label` and `unlabel` work on an issue
  or pull request only: GitHub discussions have categories, not labels.

Message ids match the anchors in GitHub’s own URLs: `issue-N` (the opening post of issue
or pull request N), `issuecomment-ID`, `discussion-N`, `discussioncomment-ID`.

Messages read through the API are `platform` (GitHub authenticated the account), with the
author’s `author_association` (`OWNER`, `MEMBER`, `CONTRIBUTOR`, `NONE`, …) as
`authority`. A webhook delivery is graded by [`GitHub.verify()`](#correspond.channels.github.GitHub.verify): `crypto` when its
`X-Hub-Signature-256` matches the configured secret, `forged` otherwise.

[`GitHub.audience()`](#correspond.channels.github.GitHub.audience) says who can read a conversation: the repository’s readership,
from its visibility, and its collaborators when the account may list them.

```pycon
>>> GitHub().parse_ref("OctoCat/Hello-World#1").encoded
'github:octocat/hello-world#1'
>>> signature("a-secret", b"{}")
'sha256=...'
```

### Module Attributes

| [`WATCHERS`](#correspond.channels.github.WATCHERS)   | Reader classes of a repository that cannot be listed, as [`GitHub.audience()`](#correspond.channels.github.GitHub.audience) words them.   |
|-------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------|

### Functions

| [`signature`](#correspond.channels.github.signature)(secret, body)   | The `X-Hub-Signature-256` value GitHub sends for `body` under `secret`.   |
|----------------------------------------------------------------------------|---------------------------------------------------------------------------|

### Classes

| [`GitHub`](#correspond.channels.github.GitHub)(\*[, run, gh])   | GitHub through `gh`: read, listen, send, edit, react, verify webhook deliveries, and say who reads a conversation.   |
|--------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|

### *class* correspond.channels.github.GitHub(\*, run=<function run>, gh='gh')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

GitHub through `gh`: read, listen, send, edit, react, verify webhook deliveries, and say who reads a conversation.

#### audience(ref, , draft=None)

Who can read a repository’s issues, pull requests and discussions, asked of GitHub now.

Every conversation in a repository has the repository’s readership. A draft changes
nothing: a mention decides who is notified, not who can read. One
`gh api repos/{owner}/{repo}` call gives the visibility and the owner’s type:

- `public`: scope `public`; readers are not listed.
- `private`: scope `named` when a user owns it, `org` otherwise.
- `internal`: scope `org`, with every member of the enterprise as a class.
- a 404 or a 403: `public`, defaulted. GitHub answers 404 both for a repository
  that does not exist and for one the account cannot see.

For a private or internal repository, collaborators (team members, members reading
through the base permission, and owners included) are listed only when the account
can write to it, since GitHub lists them to no one else. The list is a lower bound
and the audience is never `complete`: installed apps and webhooks read the
repository without being listable through `gh`, and an organisation’s
security-manager teams read every repository. An organisation’s base permission is
read when the account may see it (owners only), and otherwise assumed to be the
documented default, read; `read`, `write` and `admin` reach the same readers
and give the same class. The `gh` account itself is never consulted, so the answer,
and its hash, do not depend on who asks. Every repository emails bodies to watchers
and participants, and nothing sent is retractable. Nothing is cached. A rate limit or
a transport failure raises; [`correspond.ops.audience()`](correspond.ops.md#correspond.ops.audience) resolves it to public.

* **Return type:**
  [`Audience`](correspond.model.md#correspond.model.Audience)

#### *property* capabilities *: [Capabilities](correspond.model.md#correspond.model.Capabilities)*

Issues, pull request conversations and discussions.

#### edit(ref, message_id, draft, , dry_run=False)

Replace the body of an issue, a comment, a discussion or a discussion comment.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### label(ref, labels, , dry_run=False)

Add labels to an issue or pull request (`POST .../labels`).

GitHub creates a label that does not already exist in the repository rather than
rejecting it, so `landed` in the plan is mostly a confirmation: the requested
names the response’s own (now-current) label list actually carries, which is read
back and reported the way an assignee is not (a login that is not a member of the
repository is silently dropped from `assignees`; a label name never is). The
response lists every label now on the issue, including ones this call never
mentioned, so `landed` is filtered to the requested names, not the full list.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### parse_ref(id)

`owner/repo` or `owner/repo#N`, lower-cased (GitHub names are case-insensitive).

* **Return type:**
  [`ConversationRef`](correspond.model.md#correspond.model.ConversationRef)

#### poll(ref, , cursor=None, limit=None)

Issue and comment activity since `cursor`; a first poll looks back a day.

The cursor is opaque JSON: the update time the next poll starts from (`t`; GitHub’s
`since` includes it), the creation time from which an issue still counts as new
(`l`, which stays behind while the issues list is being read in parts), and the
events already delivered at `t` (`seen`). So neither `limit` nor several changes
within one second can make a poll repeat itself, and a list cut short (more than
`MAX_PAGES` pages changed) is resumed from where it was cut. A bare ISO time is also
accepted as a cursor.

#### react(ref, message_id, reaction, , dry_run=False)

Add one of `REACTIONS` to an issue, a comment, a discussion or a discussion comment.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### read(ref, , since=None, limit=None)

An issue, pull request or discussion with its comments; for a repository, recent open issues and pull requests.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Message`](correspond.model.md#correspond.model.Message)]

#### send(ref, draft, , dry_run=False)

Comment on an issue, pull request or discussion; on a repository, open an issue (`title` required).

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### unlabel(ref, labels, , dry_run=False)

Remove labels from an issue or pull request.

GitHub has no bulk removal: one `DELETE .../labels/{name}` per label. A label
already absent from the issue answers 404 the same way a missing issue does, so the
issue’s existence is checked first (`_require_issue()`) and every 404 after that
is read as “was not on the issue”, not as an error.

* **Return type:**
  [`SendResult`](correspond.model.md#correspond.model.SendResult)

#### verify(headers, body)

`crypto` when `X-Hub-Signature-256` matches the webhook secret, `forged` otherwise.

* **Return type:**
  [`Authenticity`](correspond.model.md#correspond.model.Authenticity)

### correspond.channels.github.WATCHERS *= 'watchers and participants receive the body by email'*

Reader classes of a repository that cannot be listed, as [`GitHub.audience()`](#correspond.channels.github.GitHub.audience) words them.

### correspond.channels.github.signature(secret, body)

The `X-Hub-Signature-256` value GitHub sends for `body` under `secret`.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)
