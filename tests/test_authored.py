"""Authored text: the sender's own words, apart from quoted replies, forwards, signatures and disclaimers.

Every fixture is synthetic, shaped after what real clients produce (Gmail's plain-text
rendering, Outlook's header blocks with ``<mailto:…>`` debris, mobile footers, inline
answers written inside the quote without ``>`` markers).
"""

import json
import re

from correspond import tools
from correspond.authored import DEFAULT_MARKERS, Markers, authored, authored_thread

GMAIL_TOP_POST = """\
Fine by me, let's print the swatches on Friday.

Best,
Ada

On Mon, Mar 3, 2025 at 10:00 AM Grace Hopper <grace@example.org>
wrote:

> Can we print on Friday?
>
> Grace
"""

OUTLOOK_REPLY = """\
Agreed. Send me the exact records and I will change them tonight.

/Ada

*From:* Grace Hopper <grace@example.org<mailto:grace@example.org>>
*Sent:* Tuesday, March 4, 2025 1:21 PM
*To:* Ada Lovelace <ada@example.org<mailto:ada@example.org>>
*Subject:* Re: DNS for the new domain

Hi Ada,
Here is what needs to change.
"""

GRACE_ASKS = """\
Hi Ada,

Three questions before the call.

1. What share split do you have in mind for the founders?
2. Who owns the capture app after the pilot?
3. When does the pilot end?

Thanks,
Grace
"""

ADA_ANSWERS_INLINE = """\
Responses below.

From: Grace Hopper <grace@example.org<mailto:grace@example.org>>
Sent: Thursday, March 6, 2025 5:51 PM
To: Ada Lovelace <ada@example.org<mailto:ada@example.org>>
Subject: Re: questions

Hi Ada,

Three questions before the call.

   1. *What share split do you have in mind for the founders?*
Equal shares for the three of us, with a small pool kept back.
   2. *Who owns the capture app after the pilot?*
You do, end to end.
   3. *When does the pilot end?*

Thanks,
Grace
"""


def test_top_post_with_a_wrapped_gmail_header():
    found = authored(GMAIL_TOP_POST)
    assert found.text == "Fine by me, let's print the swatches on Friday.\n\nBest,\nAda"
    assert found.quoted.startswith("On Mon, Mar 3")
    assert found.inline == () and not found.forwarded


def test_outlook_header_block_with_bold_and_mailto_debris():
    found = authored(OUTLOOK_REPLY)
    assert found.text.endswith("/Ada")
    assert "Here is what needs to change" not in found.text
    assert found.quoted.startswith("*From:*")


def test_other_languages_and_a_mobile_footer():
    french = authored(
        "D'accord pour vendredi.\n\nLe lun. 3 mars 2025 à 10:00, Grace <grace@example.org> a écrit :\n> On imprime vendredi ?"
    )
    assert french.text == "D'accord pour vendredi."
    swedish = authored(
        "Låter bra.\n\nSkickat från min iPhone\n\nDen 3 mars 2025 kl. 10:00 skrev Grace <grace@example.org>:\n> Fredag?"
    )
    assert swedish.text == "Låter bra."
    assert swedish.signature == "Skickat från min iPhone"
    original = authored(
        "Yes.\n\n-----Original Message-----\nFrom: Grace\nSent: Monday\nFriday?"
    )
    assert original.text == "Yes."


def test_a_bare_forward_has_no_authored_text():
    found = authored(
        "From: Grace Hopper <grace@example.org>\nSent: Sunday, March 2, 2025 4:47 PM\n"
        "To: Ada Lovelace <ada@example.org>\nSubject: FW: the plan\n\nAda, see below.\n/Grace"
    )
    assert found.text == "" and found.forwarded


def test_a_forward_keeps_the_note_above_it():
    found = authored(
        "Have a look at this before Monday.\n\n---------- Forwarded message ---------\n"
        "From: Grace <grace@example.org>\nDate: Mon, Mar 3, 2025\nSubject: plan\n\nThe plan."
    )
    assert found.text == "Have a look at this before Monday." and found.forwarded
    assert found.inline == ()


def test_inline_answers_are_recovered_from_the_thread():
    _, answers = authored_thread([GRACE_ASKS, ADA_ANSWERS_INLINE])
    assert answers.text == "Responses below." and answers.inline_announced
    assert answers.inline == (
        "Equal shares for the three of us, with a small pool kept back.",
        "You do, end to end.",
    )
    assert "You do, end to end." in answers.full_text
    assert answers.notes == ()


def test_inline_answers_need_the_earlier_message():
    alone = authored(ADA_ANSWERS_INLINE)
    assert alone.inline == () and alone.inline_announced
    assert "earlier" in alone.notes[0]
    unrelated = authored(ADA_ANSWERS_INLINE, earlier=["Lunch on Tuesday?\nSure, noon."])
    assert unrelated.inline == () and "not recovered" in unrelated.notes[0]


def test_inline_answers_between_quote_markers():
    found = authored(
        "See my answers inline.\n\nOn Tue, Mar 4, 2025 at 9:00 AM Grace <grace@example.org> wrote:\n"
        "> Is the printer calibrated?\n\nYes, since Monday.\n\n> Do we have enough paper?\n"
        "> We used a lot last week.\n\nTwo boxes left, I ordered more.\n"
    )
    assert found.inline == ("Yes, since Monday.", "Two boxes left, I ordered more.")


def test_signature_after_the_sign_off_and_disclaimers():
    found = authored(
        "The order went out this morning.\n\n/Ada\n\nAda Lovelace\nCEO, Example Org\n"
        "Tel: +33 1 00 00 00 00\nwww.example.org\n\n"
        "This e-mail and any attachments are confidential and intended solely for the addressee.\n"
    )
    assert found.text == "The order went out this morning.\n\n/Ada"
    assert found.signature.startswith("Ada Lovelace")
    assert found.disclaimer.startswith("This e-mail")


def test_short_messages_are_never_taken_for_signatures():
    assert authored("OK, send it").text == "OK, send it"
    assert authored("Thanks Grace\nAda").text == "Thanks Grace\nAda"


def test_the_markers_are_a_seam():
    custom = Markers(
        reply_headers=DEFAULT_MARKERS.reply_headers + (re.compile(r"^=== earlier ===$"),)
    )
    assert authored("Mine.\n=== earlier ===\nTheirs.", markers=custom).text == "Mine."
    assert authored("Mine.\n=== earlier ===\nTheirs.").text.endswith("Theirs.")


def test_the_tools_take_a_body_and_a_thread_file(tmp_path):
    one = tools.authored(GMAIL_TOP_POST)
    assert one["ok"] and one["text"].startswith("Fine by me")
    thread = tmp_path / "thread.json"
    thread.write_text(
        json.dumps(
            {
                "messages": [
                    {"sender": "grace@example.org", "plaintextBody": GRACE_ASKS},
                    {"sender": "ada@example.org", "plaintextBody": ADA_ANSWERS_INLINE},
                ]
            }
        )
    )
    result = tools.authored_thread(str(thread), author="ada@")
    assert result["ok"] and result["count"] == 1
    assert result["messages"][0]["inline"][1] == "You do, end to end."
    assert result["messages"][0]["quoted"] is None
    assert result["messages"][0]["quoted_chars"] > 0


def test_older_header_forms():
    thunderbird = authored(
        "Thanks, I will read it.\n/Ada\n\nGrace Hopper wrote:\n>\n> Here is the list.\n"
    )
    assert thunderbird.text == "Thanks, I will read it.\n/Ada"
    old_gmail = authored(
        "Agreed.\n\n2008/7/7 Grace Hopper <grace@example.org >:\n\n> Shall we?\n"
    )
    assert old_gmail.text == "Agreed."


def test_outlook_mobile_footers():
    found = authored(
        "On my way.\n\nSent from Outlook for Android<https://example.org/app>\n"
    )
    assert found.text == "On my way."
    assert authored("Yes.\n(from handheld)\n").text == "Yes."


GRACE_SAYS = "No concerts these days, just friends and clients.\nMaybe again one day."


def test_unmarked_text_above_a_marked_quote_is_not_an_answer():
    # A mobile client quotes the previous reply unmarked and only the older levels with ">".
    reply = (
        "Good to hear!\n/Ada\n\n-----Original Message-----\nFrom: Grace <grace@example.org>\n"
        f"Date: Thu, 10 Dec 2009 12:00\nTo: <ada@example.org>\nSubject: Re: news\n\n{GRACE_SAYS}\n\n"
        "On Thu, Dec 10, 2009 at 8:47 AM, Ada <ada@example.org> wrote:\n\n"
        "> Are you giving concerts?\n> Paths may cross.\n"
    )
    found = authored(
        reply, earlier=["Are you giving concerts?\nPaths may cross.", GRACE_SAYS]
    )
    assert found.text == "Good to hear!\n/Ada" and found.inline == ()
    assert authored(reply).inline == ()


def test_a_pasted_back_quote_with_no_header_is_not_authored():
    found = authored(
        "Done, all four.\n\n/Ada\n\nIn the zone, change these records:\nthe apex A record to the new server\n"
        "the www CNAME to the apex name\nthe mail MX to the provider\n",
        earlier=[
            "In the zone, change these records:\nthe apex A record to the new server\n"
            "the www CNAME to the apex name\nthe mail MX to the provider"
        ],
    )
    assert found.text == "Done, all four.\n\n/Ada"


def test_only_the_replied_to_message_can_hold_inline_answers():
    # The deeper quote holds a message from outside the thread: it is history, not an answer.
    body = ADA_ANSWERS_INLINE + (
        "\nFrom: Someone Else <someone@example.org>\nSent: Monday, March 3, 2025\n"
        "To: Grace Hopper <grace@example.org>\nSubject: unrelated\n\nA message this thread never saw.\n"
    )
    _, answers = authored_thread([GRACE_ASKS, body])
    assert answers.inline == (
        "Equal shares for the three of us, with a small pool kept back.",
        "You do, end to end.",
    )


def test_debris_is_never_an_inline_answer():
    body = ADA_ANSWERS_INLINE.replace(
        "Subject: Re: questions\n",
        "Cc: Lin <lin@example.org>; 'Bo' <bo@example.org>; 'Karin\nVega' <karin@example.org>\nSubject: Re: questions\n",
    ).replace(
        "Thanks,\nGrace",
        "[image: Image removed by sender.]\nThanks,\nGrace\nT: +33 1 00 00 00 00",
    )
    _, answers = authored_thread([GRACE_ASKS, body])
    assert answers.inline == (
        "Equal shares for the three of us, with a small pool kept back.",
        "You do, end to end.",
    )
