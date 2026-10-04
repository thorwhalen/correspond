# correspond.authored

What the sender of an email actually wrote: their text, apart from quoted replies, forwarded blocks, signatures and disclaimers.

A reply body carries the whole conversation beneath it, so a profile, a summary or a search
built from raw bodies mostly measures the people being quoted. [`authored()`](#correspond.authored.authored) splits one
body into its parts:

```pycon
>>> body = '''Fine by me, ship it Friday.
...
... /Ada
...
... On Mon, 3 Mar 2025 at 10:00, Grace Hopper <grace@example.org> wrote:
... > Can we ship on Friday?
... '''
>>> found = authored(body)
>>> found.text
'Fine by me, ship it Friday.\n\n/Ada'
>>> found.quoted.splitlines()[-1]
'> Can we ship on Friday?'
```

Three shapes are handled:

- **top-posting**: the text above the first reply header (`On … wrote:` in several
  languages, an Outlook `From: … Sent: …` block, `-----Original Message-----`) or above
  a run of `>` lines;
- **forwarding**: a forwarded block is never authored text (`forwarded` is set);
- **inline replies**: answers written *inside* the quoted text with no `>` marker, as
  Outlook users do (“Responses below.”). These are recovered only when the earlier messages
  of the thread are passed as `earlier`: a line of the quoted region that appears in none
  of them was written by this sender. [`authored_thread()`](#correspond.authored.authored_thread) does that for a whole thread.

Signatures (after 

```
``
```

– 

```
``
```

, a mobile footer, or a contact block after the sign-off) and legal
disclaimers are split off; the sign-off itself (“Best,”, “/Ada”) stays in the text, since it
is part of how the person writes.

The reply-header and footer vocabulary is a seam: pass `markers=` a [`Markers`](#correspond.authored.Markers) with
other patterns. The defaults cover English, French, German, Swedish, Spanish, Italian and
Dutch clients. Prior art: `email-reply-parser` (top-posting, English) and
`mail-parser-reply` (multilingual headers); neither recovers inline replies.

### Functions

| [`authored`](#correspond.authored.authored)(text, \*[, earlier, markers, ...])   | Split one email body (plain text) into what its sender wrote and everything else.                                                                |
|------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| [`authored_thread`](#correspond.authored.authored_thread)(bodies, \*[, markers, ...])   | [`authored()`](#correspond.authored.authored) for each body of a thread, oldest first, each given every earlier body as `earlier`. |

### Classes

| [`Authored`](#correspond.authored.Authored)(text[, quoted, signature, ...])   | One email body split into what its sender wrote and everything else.   |
|---------------------------------------------------------------------------------------------|------------------------------------------------------------------------|
| [`Markers`](#correspond.authored.Markers)([reply_headers, ...])              | The patterns that recognise reply headers, forwards and footers.       |

### *class* correspond.authored.Authored(text, quoted='', signature='', disclaimer='', inline=(), forwarded=False, inline_announced=False, notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One email body split into what its sender wrote and everything else.

#### *property* full_text *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

everything this sender wrote.

* **Type:**
  The top text and any recovered inline answers, in order

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* correspond.authored.Markers(reply_headers=(re.compile('^on\\\\\\\\b.{0, 300}\\\\\\\\bwrote\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^le\\\\\\\\b.{0, 300}\\\\\\\\ba [ée]crit\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^am\\\\\\\\b.{0, 300}\\\\\\\\bschrieb\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile('^(den|on)\\\\\\\\b.{0, 300}\\\\\\\\bskrev\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile('^el\\\\\\\\b.{0, 300}\\\\\\\\bescribi[óo]\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^il\\\\\\\\b.{0, 300}\\\\\\\\bha scritto\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^op\\\\\\\\b.{0, 300}\\\\\\\\bschreef\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile("^-{2, }\\\\\\\\s\*(original message|message d'origine|ursprüngliche nachricht|ursprungligt meddelande|mensaje original|messaggio originale|oorspronkelijk bericht)\\\\\\\\s\*-{2, }$", re.IGNORECASE)), reply_header_tails=(re.compile('^.{0, 120}\\\\\\\\b(wrote|a [ée]crit|schrieb|skrev|escribi[óo]|ha scritto|schreef)\\\\\\\\s\*: $', re.IGNORECASE), ), forward_headers=(re.compile('^-{2, }\\\\\\\\s\*(forwarded message|message transf[ée]r[ée]|weitergeleitete nachricht|vidarebefordrat meddelande|mensaje reenviado|messaggio inoltrato|doorgestuurd bericht)\\\\\\\\s\*-{2, }$', re.IGNORECASE), re.compile('^begin forwarded message\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^d[ée]but du message r[ée]exp[ée]di[ée]\\\\\\\\s\*: $', re.IGNORECASE)), header_from=(re.compile('^\\\\\\\\\*\*(from|de|från|von|van|da|fra)\\\\\\\\s\*:\\\\\\\\\*\*\\\\\\\\s\*\\\\\\\\S.\*$', re.IGNORECASE), ), header_field=(re.compile('^\\\\\\\\\*\*(sent|date|envoy[ée]|skickat|datum|gesendet|verzonden|inviato|enviado|fecha|to|à|till|an|aan|a|para|subject|objet|ämne|betreff|onderwerp|oggetto|asunto)\\\\\\\\s\*:\\\\\\\\\*\*(\\\\\\\\s|$)', re.IGNORECASE), ), separators=(re.compile('^_{10, }$', re.IGNORECASE), re.compile('^-{20, }$', re.IGNORECASE)), signature_starts=(re.compile('^--\\\\\\\\s\*$', re.IGNORECASE), re.compile('^(sent|envoy[ée]|skickat|gesendet|enviado|inviato|verzonden) (from|de|depuis|från|von|desde|da|vanaf) (my|mon|ma|min|mein|meinem|mi|il mio|mijn)\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^von meinem \\\\\\\\S+ gesendet$', re.IGNORECASE), re.compile('^get outlook for\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^(obtenir|télécharger) outlook pour\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^h[äa]mta outlook f[öo]r\\\\\\\\b.\*$', re.IGNORECASE)), sign_offs=(re.compile('^/\\\\\\\\s?\\\\\\\\w[\\\\\\\\w .-]{0, 30}$', re.IGNORECASE), re.compile('^(best|best regards|kind regards|warm regards|regards|cheers|thanks|thank you|many thanks|all the best|br|brgds|rgds|cordialement|bien cordialement|bien à (vous|toi)|amitiés|bonne journée|merci|mvh|m, re.IGNORECASE)), contact=(re.compile('(\\\\\\\\+|00)\\\\\\\\d[\\\\\\\\d ().-]{6, }\\\\\\\\d', re.IGNORECASE), re.compile('\\\\\\\\b(tel|tél|phone|mobile|mob|portable|cell|fax)\\\\\\\\b\\\\\\\\.?\\\\\\\\s\*: ?', re.IGNORECASE), re.compile('https?: //|\\\\\\\\bwww\\\\\\\\.', re.IGNORECASE), re.compile('[\\\\\\\\w.+-]+@[\\\\\\\\w-]+\\\\\\\\.[\\\\\\\\w.]+', re.IGNORECASE)), disclaimers=(re.compile('\\\\\\\\b(this|the) (e-?mail|message)( and any (attachments?|files?))?.{0, 80}\\\\\\\\b(confidential|privileged|intended (solely |only )?for)\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bconfidentiality notice\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bce (message|courriel|mail)( et (toutes )?(les|ses) pi[èe]ces jointes)?.{0, 80}\\\\\\\\b(confidentiel|destin[ée] exclusivement)', re.IGNORECASE), re.compile('\\\\\\\\bdetta (e-?post|meddelande).{0, 80}\\\\\\\\bkonfidentiell', re.IGNORECASE), re.compile('\\\\\\\\bplease consider the environment before printing\\\\\\\\b', re.IGNORECASE), re.compile("\\\\\\\\bpensez à l'environnement avant d'imprimer\\\\\\\\b", re.IGNORECASE)), inline_announcements=(re.compile('\\\\\\\\b(see )?(my )?(answers?|responses?|replies|comments?|notes?) (are )?(below|inline|in (red|blue|bold|green))\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(see|cf\\\\\\\\.?) below\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(réponses?|commentaires?) (ci-dessous|en (rouge|bleu|gras)|dans le texte)\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bvoir ci-dessous\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(svar|kommentarer) (nedan|i texten|med (rött|blått))\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(antworten|kommentare) (unten|im text|in rot)\\\\\\\\b', re.IGNORECASE)), header_window=4)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The patterns that recognise reply headers, forwards and footers. Every field is a tuple of compiled regexes matched against one stripped line.

#### contact *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('(\\\\+|00)\\\\d[\\\\d ().-]{6,}\\\\d', re.IGNORECASE), re.compile('\\\\b(tel|tél|phone|mobile|mob|portable|cell|fax)\\\\b\\\\.?\\\\s\*:?', re.IGNORECASE), re.compile('https?://|\\\\bwww\\\\.', re.IGNORECASE), re.compile('[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.]+', re.IGNORECASE))*

A line that looks like contact details (phone, URL, address) — the body of a signature block.

#### disclaimers *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('\\\\b(this|the) (e-?mail|message)( and any (attachments?|files?))?.{0,80}\\\\b(confidential|privileged|intended (solely |only )?for)\\\\b', re.IGNORECASE), re.compile('\\\\bconfidentiality notice\\\\b', re.IGNORECASE), re.compile('\\\\bce (message|courriel|mail)( et (toutes )?(les|ses) pi[èe]ces jointes)?.{0,80}\\\\b(confidentiel|destin[ée] exclusivement)', re.IGNORECASE), re.compile('\\\\bdetta (e-?post|meddelande).{0,80}\\\\bkonfidentiell', re.IGNORECASE), re.compile('\\\\bplease consider the environment before printing\\\\b', re.IGNORECASE), re.compile("\\\\bpensez à l'environnement avant d'imprimer\\\\b", re.IGNORECASE))*

A line that opens a legal disclaimer; it and everything after it are dropped.

#### forward_headers *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^-{2,}\\\\s\*(forwarded message|message transf[ée]r[ée]|weitergeleitete nachricht|vidarebefordrat meddelande|mensaje reenviado|messaggio inoltrato|doorgestuurd bericht)\\\\s\*-{2,}$', re.IGNORECASE), re.compile('^begin forwarded message\\\\s\*:$', re.IGNORECASE), re.compile('^d[ée]but du message r[ée]exp[ée]di[ée]\\\\s\*:$', re.IGNORECASE))*

A line that starts a forwarded block.

#### header_field *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^\\\\\*\*(sent|date|envoy[ée]|skickat|datum|gesendet|verzonden|inviato|enviado|fecha|to|à|till|an|aan|a|para|subject|objet|ämne|betreff|onderwerp|oggetto|asunto)\\\\s\*:\\\\\*\*(\\\\s|$)', re.IGNORECASE),)*

A line that must follow `header_from` within a few lines for it to count as a header block.

#### header_from *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^\\\\\*\*(from|de|från|von|van|da|fra)\\\\s\*:\\\\\*\*\\\\s\*\\\\S.\*$', re.IGNORECASE),)*

Ada <ada@…>”).

* **Type:**
  The first line of an Outlook-style header block (”From

#### header_window *: [int](https://docs.python.org/3/builtins/functions.html#int)* *= 4*

How many lines after `header_from` may hold the `header_field` that confirms it.

#### inline_announcements *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('\\\\b(see )?(my )?(answers?|responses?|replies|comments?|notes?) (are )?(below|inline|in (red|blue|bold|green))\\\\b', re.IGNORECASE), re.compile('\\\\b(see|cf\\\\.?) below\\\\b', re.IGNORECASE), re.compile('\\\\b(réponses?|commentaires?) (ci-dessous|en (rouge|bleu|gras)|dans le texte)\\\\b', re.IGNORECASE), re.compile('\\\\bvoir ci-dessous\\\\b', re.IGNORECASE), re.compile('\\\\b(svar|kommentarer) (nedan|i texten|med (rött|blått))\\\\b', re.IGNORECASE), re.compile('\\\\b(antworten|kommentare) (unten|im text|in rot)\\\\b', re.IGNORECASE))*

Short top text that announces answers written inside the quote (“Responses below.”).

#### reply_header_tails *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^.{0,120}\\\\b(wrote|a [ée]crit|schrieb|skrev|escribi[óo]|ha scritto|schreef)\\\\s\*:$', re.IGNORECASE),)*

“), never authored text.

* **Type:**
  The second line of a wrapped reply header (”wrote

#### reply_headers *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^on\\\\b.{0,300}\\\\bwrote\\\\s\*:$', re.IGNORECASE), re.compile('^le\\\\b.{0,300}\\\\ba [ée]crit\\\\s\*:$', re.IGNORECASE), re.compile('^am\\\\b.{0,300}\\\\bschrieb\\\\b.{0,300}:$', re.IGNORECASE), re.compile('^(den|on)\\\\b.{0,300}\\\\bskrev\\\\b.{0,300}:$', re.IGNORECASE), re.compile('^el\\\\b.{0,300}\\\\bescribi[óo]\\\\s\*:$', re.IGNORECASE), re.compile('^il\\\\b.{0,300}\\\\bha scritto\\\\s\*:$', re.IGNORECASE), re.compile('^op\\\\b.{0,300}\\\\bschreef\\\\b.{0,300}:$', re.IGNORECASE), re.compile("^-{2,}\\\\s\*(original message|message d'origine|ursprüngliche nachricht|ursprungligt meddelande|mensaje original|messaggio originale|oorspronkelijk bericht)\\\\s\*-{2,}$", re.IGNORECASE))*

“, “—–Original Message—–“).

* **Type:**
  A line that, on its own, starts the quoted region (”On … wrote

#### separators *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^_{10,}$', re.IGNORECASE), re.compile('^-{20,}$', re.IGNORECASE))*

A separator line Outlook puts above its header block.

#### sign_offs *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^/\\\\s?\\\\w[\\\\w .-]{0,30}$', re.IGNORECASE), re.compile('^(best|best regards|kind regards|warm regards|regards|cheers|thanks|thank you|many thanks|all the best|br|brgds|rgds|cordialement|bien cordialement|bien à (vous|toi)|amitiés|bonne journée|merci|mvh|m, re.IGNORECASE))*

what is after it, in a trailing block of short lines, is a signature.

* **Type:**
  A sign-off line

#### signature_starts *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Pattern](https://docs.python.org/3/library/re.html#re.Pattern), ...]* *= (re.compile('^--\\\\s\*$', re.IGNORECASE), re.compile('^(sent|envoy[ée]|skickat|gesendet|enviado|inviato|verzonden) (from|de|depuis|från|von|desde|da|vanaf) (my|mon|ma|min|mein|meinem|mi|il mio|mijn)\\\\b.\*$', re.IGNORECASE), re.compile('^von meinem \\\\S+ gesendet$', re.IGNORECASE), re.compile('^get outlook for\\\\b.\*$', re.IGNORECASE), re.compile('^(obtenir|télécharger) outlook pour\\\\b.\*$', re.IGNORECASE), re.compile('^h[äa]mta outlook f[öo]r\\\\b.\*$', re.IGNORECASE))*

A line that starts a signature on its own (”– “, a mobile footer).

### correspond.authored.authored(text, \*, earlier=(), markers=Markers(reply_headers=(re.compile('^on\\\\\\\\b.{0, 300}\\\\\\\\bwrote\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^le\\\\\\\\b.{0, 300}\\\\\\\\ba [ée]crit\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^am\\\\\\\\b.{0, 300}\\\\\\\\bschrieb\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile('^(den|on)\\\\\\\\b.{0, 300}\\\\\\\\bskrev\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile('^el\\\\\\\\b.{0, 300}\\\\\\\\bescribi[óo]\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^il\\\\\\\\b.{0, 300}\\\\\\\\bha scritto\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^op\\\\\\\\b.{0, 300}\\\\\\\\bschreef\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile("^-{2, }\\\\\\\\s\*(original message|message d'origine|ursprüngliche nachricht|ursprungligt meddelande|mensaje original|messaggio originale|oorspronkelijk bericht)\\\\\\\\s\*-{2, }$", re.IGNORECASE)), reply_header_tails=(re.compile('^.{0, 120}\\\\\\\\b(wrote|a [ée]crit|schrieb|skrev|escribi[óo]|ha scritto|schreef)\\\\\\\\s\*: $', re.IGNORECASE), ), forward_headers=(re.compile('^-{2, }\\\\\\\\s\*(forwarded message|message transf[ée]r[ée]|weitergeleitete nachricht|vidarebefordrat meddelande|mensaje reenviado|messaggio inoltrato|doorgestuurd bericht)\\\\\\\\s\*-{2, }$', re.IGNORECASE), re.compile('^begin forwarded message\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^d[ée]but du message r[ée]exp[ée]di[ée]\\\\\\\\s\*: $', re.IGNORECASE)), header_from=(re.compile('^\\\\\\\\\*\*(from|de|från|von|van|da|fra)\\\\\\\\s\*:\\\\\\\\\*\*\\\\\\\\s\*\\\\\\\\S.\*$', re.IGNORECASE), ), header_field=(re.compile('^\\\\\\\\\*\*(sent|date|envoy[ée]|skickat|datum|gesendet|verzonden|inviato|enviado|fecha|to|à|till|an|aan|a|para|subject|objet|ämne|betreff|onderwerp|oggetto|asunto)\\\\\\\\s\*:\\\\\\\\\*\*(\\\\\\\\s|$)', re.IGNORECASE), ), separators=(re.compile('^_{10, }$', re.IGNORECASE), re.compile('^-{20, }$', re.IGNORECASE)), signature_starts=(re.compile('^--\\\\\\\\s\*$', re.IGNORECASE), re.compile('^(sent|envoy[ée]|skickat|gesendet|enviado|inviato|verzonden) (from|de|depuis|från|von|desde|da|vanaf) (my|mon|ma|min|mein|meinem|mi|il mio|mijn)\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^von meinem \\\\\\\\S+ gesendet$', re.IGNORECASE), re.compile('^get outlook for\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^(obtenir|télécharger) outlook pour\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^h[äa]mta outlook f[öo]r\\\\\\\\b.\*$', re.IGNORECASE)), sign_offs=(re.compile('^/\\\\\\\\s?\\\\\\\\w[\\\\\\\\w .-]{0, 30}$', re.IGNORECASE), re.compile('^(best|best regards|kind regards|warm regards|regards|cheers|thanks|thank you|many thanks|all the best|br|brgds|rgds|cordialement|bien cordialement|bien à (vous|toi)|amitiés|bonne journée|merci|mvh|m, re.IGNORECASE)), contact=(re.compile('(\\\\\\\\+|00)\\\\\\\\d[\\\\\\\\d ().-]{6, }\\\\\\\\d', re.IGNORECASE), re.compile('\\\\\\\\b(tel|tél|phone|mobile|mob|portable|cell|fax)\\\\\\\\b\\\\\\\\.?\\\\\\\\s\*: ?', re.IGNORECASE), re.compile('https?: //|\\\\\\\\bwww\\\\\\\\.', re.IGNORECASE), re.compile('[\\\\\\\\w.+-]+@[\\\\\\\\w-]+\\\\\\\\.[\\\\\\\\w.]+', re.IGNORECASE)), disclaimers=(re.compile('\\\\\\\\b(this|the) (e-?mail|message)( and any (attachments?|files?))?.{0, 80}\\\\\\\\b(confidential|privileged|intended (solely |only )?for)\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bconfidentiality notice\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bce (message|courriel|mail)( et (toutes )?(les|ses) pi[èe]ces jointes)?.{0, 80}\\\\\\\\b(confidentiel|destin[ée] exclusivement)', re.IGNORECASE), re.compile('\\\\\\\\bdetta (e-?post|meddelande).{0, 80}\\\\\\\\bkonfidentiell', re.IGNORECASE), re.compile('\\\\\\\\bplease consider the environment before printing\\\\\\\\b', re.IGNORECASE), re.compile("\\\\\\\\bpensez à l'environnement avant d'imprimer\\\\\\\\b", re.IGNORECASE)), inline_announcements=(re.compile('\\\\\\\\b(see )?(my )?(answers?|responses?|replies|comments?|notes?) (are )?(below|inline|in (red|blue|bold|green))\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(see|cf\\\\\\\\.?) below\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(réponses?|commentaires?) (ci-dessous|en (rouge|bleu|gras)|dans le texte)\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bvoir ci-dessous\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(svar|kommentarer) (nedan|i texten|med (rött|blått))\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(antworten|kommentare) (unten|im text|in rot)\\\\\\\\b', re.IGNORECASE)), header_window=4), min_coverage=0.6)

Split one email body (plain text) into what its sender wrote and everything else.

`earlier` is the text of messages that came before it in the thread (raw bodies are
fine); with it, answers written inside the quoted text are recovered into `inline`.
They are trusted only when at least `min_coverage` of the quoted lines are found in
`earlier`, since a thread missing its earlier messages would make every quoted line look new.

* **Return type:**
  [`Authored`](#correspond.authored.Authored)

```pycon
>>> authored("Thanks!\n\n-- \nAda Lovelace\nAnalytical Engines Ltd").signature
'--\nAda Lovelace\nAnalytical Engines Ltd'
```

### correspond.authored.authored_thread(bodies, \*, markers=Markers(reply_headers=(re.compile('^on\\\\\\\\b.{0, 300}\\\\\\\\bwrote\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^le\\\\\\\\b.{0, 300}\\\\\\\\ba [ée]crit\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^am\\\\\\\\b.{0, 300}\\\\\\\\bschrieb\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile('^(den|on)\\\\\\\\b.{0, 300}\\\\\\\\bskrev\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile('^el\\\\\\\\b.{0, 300}\\\\\\\\bescribi[óo]\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^il\\\\\\\\b.{0, 300}\\\\\\\\bha scritto\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^op\\\\\\\\b.{0, 300}\\\\\\\\bschreef\\\\\\\\b.{0, 300}: $', re.IGNORECASE), re.compile("^-{2, }\\\\\\\\s\*(original message|message d'origine|ursprüngliche nachricht|ursprungligt meddelande|mensaje original|messaggio originale|oorspronkelijk bericht)\\\\\\\\s\*-{2, }$", re.IGNORECASE)), reply_header_tails=(re.compile('^.{0, 120}\\\\\\\\b(wrote|a [ée]crit|schrieb|skrev|escribi[óo]|ha scritto|schreef)\\\\\\\\s\*: $', re.IGNORECASE), ), forward_headers=(re.compile('^-{2, }\\\\\\\\s\*(forwarded message|message transf[ée]r[ée]|weitergeleitete nachricht|vidarebefordrat meddelande|mensaje reenviado|messaggio inoltrato|doorgestuurd bericht)\\\\\\\\s\*-{2, }$', re.IGNORECASE), re.compile('^begin forwarded message\\\\\\\\s\*: $', re.IGNORECASE), re.compile('^d[ée]but du message r[ée]exp[ée]di[ée]\\\\\\\\s\*: $', re.IGNORECASE)), header_from=(re.compile('^\\\\\\\\\*\*(from|de|från|von|van|da|fra)\\\\\\\\s\*:\\\\\\\\\*\*\\\\\\\\s\*\\\\\\\\S.\*$', re.IGNORECASE), ), header_field=(re.compile('^\\\\\\\\\*\*(sent|date|envoy[ée]|skickat|datum|gesendet|verzonden|inviato|enviado|fecha|to|à|till|an|aan|a|para|subject|objet|ämne|betreff|onderwerp|oggetto|asunto)\\\\\\\\s\*:\\\\\\\\\*\*(\\\\\\\\s|$)', re.IGNORECASE), ), separators=(re.compile('^_{10, }$', re.IGNORECASE), re.compile('^-{20, }$', re.IGNORECASE)), signature_starts=(re.compile('^--\\\\\\\\s\*$', re.IGNORECASE), re.compile('^(sent|envoy[ée]|skickat|gesendet|enviado|inviato|verzonden) (from|de|depuis|från|von|desde|da|vanaf) (my|mon|ma|min|mein|meinem|mi|il mio|mijn)\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^von meinem \\\\\\\\S+ gesendet$', re.IGNORECASE), re.compile('^get outlook for\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^(obtenir|télécharger) outlook pour\\\\\\\\b.\*$', re.IGNORECASE), re.compile('^h[äa]mta outlook f[öo]r\\\\\\\\b.\*$', re.IGNORECASE)), sign_offs=(re.compile('^/\\\\\\\\s?\\\\\\\\w[\\\\\\\\w .-]{0, 30}$', re.IGNORECASE), re.compile('^(best|best regards|kind regards|warm regards|regards|cheers|thanks|thank you|many thanks|all the best|br|brgds|rgds|cordialement|bien cordialement|bien à (vous|toi)|amitiés|bonne journée|merci|mvh|m, re.IGNORECASE)), contact=(re.compile('(\\\\\\\\+|00)\\\\\\\\d[\\\\\\\\d ().-]{6, }\\\\\\\\d', re.IGNORECASE), re.compile('\\\\\\\\b(tel|tél|phone|mobile|mob|portable|cell|fax)\\\\\\\\b\\\\\\\\.?\\\\\\\\s\*: ?', re.IGNORECASE), re.compile('https?: //|\\\\\\\\bwww\\\\\\\\.', re.IGNORECASE), re.compile('[\\\\\\\\w.+-]+@[\\\\\\\\w-]+\\\\\\\\.[\\\\\\\\w.]+', re.IGNORECASE)), disclaimers=(re.compile('\\\\\\\\b(this|the) (e-?mail|message)( and any (attachments?|files?))?.{0, 80}\\\\\\\\b(confidential|privileged|intended (solely |only )?for)\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bconfidentiality notice\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bce (message|courriel|mail)( et (toutes )?(les|ses) pi[èe]ces jointes)?.{0, 80}\\\\\\\\b(confidentiel|destin[ée] exclusivement)', re.IGNORECASE), re.compile('\\\\\\\\bdetta (e-?post|meddelande).{0, 80}\\\\\\\\bkonfidentiell', re.IGNORECASE), re.compile('\\\\\\\\bplease consider the environment before printing\\\\\\\\b', re.IGNORECASE), re.compile("\\\\\\\\bpensez à l'environnement avant d'imprimer\\\\\\\\b", re.IGNORECASE)), inline_announcements=(re.compile('\\\\\\\\b(see )?(my )?(answers?|responses?|replies|comments?|notes?) (are )?(below|inline|in (red|blue|bold|green))\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(see|cf\\\\\\\\.?) below\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(réponses?|commentaires?) (ci-dessous|en (rouge|bleu|gras)|dans le texte)\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\bvoir ci-dessous\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(svar|kommentarer) (nedan|i texten|med (rött|blått))\\\\\\\\b', re.IGNORECASE), re.compile('\\\\\\\\b(antworten|kommentare) (unten|im text|in rot)\\\\\\\\b', re.IGNORECASE)), header_window=4), min_coverage=0.6)

[`authored()`](#correspond.authored.authored) for each body of a thread, oldest first, each given every earlier body as `earlier`.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Authored`](#correspond.authored.Authored)]

```pycon
>>> first, second = authored_thread([
...     "Can we ship Friday?\nAnd who writes the notes?",
...     "Answers below.\n\nFrom: Ada <ada@example.org>\nSent: Monday\n"
...     "Can we ship Friday?\nYes, Friday works.\nAnd who writes the notes?\nGrace does.",
... ])
>>> second.inline
('Yes, Friday works.', 'Grace does.')
```
