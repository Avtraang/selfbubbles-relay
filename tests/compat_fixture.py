"""The synthetic ``chat.db`` the compat / glue tests and the golden recorder share.

One ``macos27`` database (built with the library's fixture builders, copied into
``tests/fixtures/``) covers every case DESIGN.md section 7.4 lists: text column
vs blob-only, ``""`` text with a blob, a reply to an attachment-only target, a
reply to a > 120-character message, URL balloons with an embedded image /
``imageMetadata`` only / a string-scan image, a HEIC attachment with a NULL mime,
a plugin-payload attachment, a hidden-but-not-plugin PNG, every tapback type
2000-2006 / 3000-3002 / 1000 plus a 2006 emoji row, iMessageLite / SMS / RCS
messages, a group whose ``chat_handle_join`` lacks one sender, a ``date_edited``
bump, one-to-one chats, groups with overlapping members and a self identity
that must be excluded from matching.

For Phase 2 + 3 the same database also carries: more URL balloons (a JPEG embed,
two embeds of different sizes, a decoy blob that is not an image, a plist
without ``$objects``, a plist that is a list, an empty payload), a link-only
plain message, a text message mentioning ``NSString`` (the search's
attribute-key false positive), a URL row with no sender, attachments backed by
real files under the test's ``tmp_path`` (PNG / MP4 / text / NULL mime+name)
plus one whose file is gone, an SMS group, a one-to-one chat whose service is
unknown, and a chat with no messages at all (absent from ``fetch_threads``).

This module also holds the case tables (which guids, address sets, search
queries, states ... the tests run), the normalisation of the two endpoint
outcomes that are not plain JSON (``link_image`` bytes -> sha256 + size,
``/attachment`` -> path relative to the files directory) and the golden-file
access, so the recorder
(``tests/record_golden.py``) and the tests cannot drift apart: a golden file is
what the recorder produced for exactly these cases on exactly this database.

Only synthetic handles (``+1555...``, ``*@example.invalid``) and synthetic
names appear here; the database path is always under a temporary directory.
"""

from __future__ import annotations

import functools
import hashlib
import json
import plistlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from imessage_chatdb import LINK_BALLOON

from tests.fixtures import builders
from tests.fixtures.keyed_archive_writer import make_link_payload
from tests.fixtures.typedstream_writer import encode_attributed_body

# ---------------------------------------------------------------------------
# synthetic identities (never real)
# ---------------------------------------------------------------------------

ALICE_PHONE = "+15550000001"
ALICE_EMAIL = "alice@example.invalid"
BOB_PHONE = "+15550000002"
CARL_PHONE = "+15550000003"          # no contact entry: resolves to the raw handle
DANA_PHONE = "+15550000004"          # single-word contact name (no first-name split)
EVE_PHONE = "+15550000005"           # has a chat but never a message
FRANK_PHONE = "+15550000006"         # one-to-one chat whose service is unknown
SELF_PHONE = "+15550009999"          # own identity registered elsewhere (IMSG_SELF)
NOBODY_PHONE = "+15550000777"        # never in the database

#: Address -> contact name; ``seed_relay_state`` keys them with the module's own
#: ``norm_key`` so the phone and the email collapse onto one person.
SYNTHETIC_CONTACTS = {
    ALICE_PHONE: "Alice Anders",
    ALICE_EMAIL: "Alice Anders",
    BOB_PHONE: "Bob Brown",
    DANA_PHONE: "Dana",
}

C1_GUID = f"iMessage;-;{ALICE_PHONE}"           # 1:1 Alice (phone), busiest chat
C1E_GUID = f"iMessage;-;{ALICE_EMAIL}"          # 1:1 Alice (email)
C1S_GUID = f"SMS;-;{ALICE_PHONE}"               # 1:1 Alice, stale SMS service_name
CSMS_GUID = f"SMS;-;{CARL_PHONE}"               # 1:1 Carl, all SMS
CRCS_GUID = f"RCS;-;{BOB_PHONE}"                # 1:1 Bob, all RCS
G1_GUID = "iMessage;+;chat100"                  # Alice, Bob + Carl (Carl has no join row)
G2_GUID = "iMessage;+;chat200"                  # Alice, Bob, SELF (named)
G3_GUID = "iMessage;+;chat300"                  # five participants -> "..." in group_title
G4_GUID = "iMessage;+;chat400"                  # Alice by phone AND email -> one person
G5_GUID = "SMS;+;chat500"                       # Bob + Carl, all SMS (group labels)
CNONE_GUID = f"any;-;{FRANK_PHONE}"             # 1:1 Frank, no service anywhere
EMPTY_GUID = f"iMessage;-;{EVE_PHONE}"          # chat row with no messages
NO_SUCH_GUID = "iMessage;-;+15550000000"

#: Attachments backed by real files under tmp_path (guid -> (mime, transfer_name,
#: file name, bytes)); ``ATT-NONAME`` has neither mime nor transfer_name, so the
#: /attachment name falls back to the file's basename.
FILE_ATTACHMENTS = {
    "ATT-FILE-PNG": ("image/png", "pic.png", "pic.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
    "ATT-FILE-MP4": ("video/mp4", "clip.mp4", "clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64),
    "ATT-FILE-TXT": ("text/plain", "notes.txt", "notes.txt", b"synthetic notes\n"),
    "ATT-NONAME": (None, None, "noname.bin", b"\x00\x01\x02\x03"),
}

LONG_TEXT = ("This synthetic message is deliberately longer than one hundred and twenty "
             "characters so that the reply quote has to be truncated by the relay code.")
assert len(LONG_TEXT) > 120

#: A ROWID / chat ROWID / attachment guid that never exists in the database.
MISSING_ROWID = 10_000_000
MISSING_CHAT_ROWID = 999_999


# ---------------------------------------------------------------------------
# the one synthetic database
# ---------------------------------------------------------------------------

def build_compat_db(w: sqlite3.Connection, files_dir: Path) -> SimpleNamespace:
    """Populate ``w`` (a macos27 writer) with every case listed in the module docstring.

    ``files_dir`` (under a temporary directory) receives the small synthetic
    files behind ``FILE_ATTACHMENTS``; their ``attachment.filename`` is the
    absolute path, so ``/attachment`` can resolve them without touching ``~``.
    """
    files_dir = Path(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)
    h_alice = builders.add_handle(w, ALICE_PHONE)
    h_alice_email = builders.add_handle(w, ALICE_EMAIL)
    h_bob = builders.add_handle(w, BOB_PHONE)
    h_carl = builders.add_handle(w, CARL_PHONE)
    h_dana = builders.add_handle(w, DANA_PHONE)
    h_self = builders.add_handle(w, SELF_PHONE)
    h_carl_sms = builders.add_handle(w, CARL_PHONE, service="SMS")
    h_bob_rcs = builders.add_handle(w, BOB_PHONE, service="RCS")
    h_eve = builders.add_handle(w, EVE_PHONE)
    h_frank = builders.add_handle(w, FRANK_PHONE)

    c1 = builders.add_chat(w, C1_GUID, 45, ALICE_PHONE, handles=[h_alice])
    c1e = builders.add_chat(w, C1E_GUID, 45, ALICE_EMAIL, handles=[h_alice_email])
    c1s = builders.add_chat(w, C1S_GUID, 45, ALICE_PHONE, service_name="SMS", handles=[h_alice])
    csms = builders.add_chat(w, CSMS_GUID, 45, CARL_PHONE, service_name="SMS", handles=[h_carl_sms])
    crcs = builders.add_chat(w, CRCS_GUID, 45, BOB_PHONE, service_name="RCS", handles=[h_bob_rcs])
    g1 = builders.add_chat(w, G1_GUID, 43, "chat100", handles=[h_alice, h_bob])
    g2 = builders.add_chat(w, G2_GUID, 43, "chat200", display_name="Synthetic Crew",
                           handles=[h_alice, h_bob, h_self])
    g3 = builders.add_chat(w, G3_GUID, 43, "chat300",
                           handles=[h_alice, h_bob, h_dana, h_carl, h_alice_email])
    g4 = builders.add_chat(w, G4_GUID, 43, "chat400", handles=[h_alice, h_alice_email])
    g5 = builders.add_chat(w, G5_GUID, 43, "chat500", service_name="SMS",
                           handles=[h_bob, h_carl_sms])
    cnone = builders.add_chat(w, CNONE_GUID, 45, FRANK_PHONE, handles=[h_frank])
    empty = builders.add_chat(w, EMPTY_GUID, 45, EVE_PHONE, handles=[h_eve])   # no messages

    m = {}
    # --- C1: the kitchen sink -------------------------------------------------
    m["text"] = builders.add_message(w, c1, text="hello from alice", handle=h_alice,
                                     guid="G-TEXT", date_read=builders.BASE_DATE_NS + 5)
    # The search's attribute-key false positive: every attributedBody blob carries
    # the bytes "NSString", so an instr() hit is not a text hit.  This plain-text
    # row really says it, and sits OLDER than every blob row so a small limit's
    # 2x oversample is exhausted by the false positives (pinned in the glue tests).
    m["needle"] = builders.add_message(w, c1, text="mentions NSString in plain text",
                                       handle=h_alice, guid="G-NEEDLE")
    m["blob"] = builders.add_message(w, c1, body=encode_attributed_body("blob only text"),
                                     is_from_me=1, guid="G-BLOB")
    m["empty_blob"] = builders.add_message(w, c1, text="",
                                           body=encode_attributed_body("empty column, blob body"),
                                           handle=h_alice, guid="G-EMPTY-BLOB")
    m["mutable_blob"] = builders.add_message(
        w, c1, body=encode_attributed_body("mutable skeleton éè \U0001F60A", mutable=True),
        handle=h_alice, guid="G-MUTABLE")
    m["att_only_heic"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-ATT-HEIC")
    builders.add_attachment(w, m["att_only_heic"], "ATT-HEIC-1", None, "IMG_0001.HEIC",
                            "~/synthetic/IMG_0001.HEIC")
    m["reply_to_att"] = builders.add_message(w, c1, text="nice photo", is_from_me=1,
                                             reply_to="G-ATT-HEIC", reply_to_part="0:0:0",
                                             guid="G-REPLY-ATT")
    m["long"] = builders.add_message(w, c1, text=LONG_TEXT, is_from_me=1, guid="G-LONG")
    m["reply_to_long"] = builders.add_message(w, c1, text="re: long", handle=h_alice,
                                              reply_to="G-LONG", guid="G-REPLY-LONG")
    m["reply_to_blob"] = builders.add_message(w, c1, text="re: blob", handle=h_alice,
                                              reply_to="G-BLOB", guid="G-REPLY-BLOB")
    m["ws"] = builders.add_message(w, c1, text="  photo￼   caption\n\n here ",
                                   handle=h_alice, guid="G-WS")
    m["reply_to_ws"] = builders.add_message(w, c1, text="re: ws", is_from_me=1,
                                            reply_to="G-WS", guid="G-REPLY-WS")
    m["reply_dangling"] = builders.add_message(w, c1, text="re: nothing", is_from_me=1,
                                               reply_to="G-DOES-NOT-EXIST", guid="G-REPLY-NONE")
    # URL balloons
    m["link_embedded"] = builders.add_message(
        w, c1, body=encode_attributed_body("https://news.example.invalid/story"),
        handle=h_alice, balloon=LINK_BALLOON, has_att=1, guid="G-LINK-EMBED",
        payload=make_link_payload("https://news.example.invalid/story", "Story title",
                                  "A summary", "News Site", embed="png",
                                  image_meta_url="https://img.example.invalid/hero.jpg"))
    builders.add_attachment(w, m["link_embedded"], "ATT-PLUGIN", None,
                            "x.pluginPayloadAttachment", "~/synthetic/x.pluginPayloadAttachment",
                            hide=1)
    m["link_meta"] = builders.add_message(
        w, c1, text="https://blog.example.invalid/post", is_from_me=1,
        balloon=LINK_BALLOON, guid="G-LINK-META",
        payload=make_link_payload("https://blog.example.invalid/post", "Post title",
                                  wrapped=True,
                                  image_meta_url="https://img.example.invalid/hero2.jpg"))
    m["link_scan"] = builders.add_message(
        w, c1, text="https://site.example.invalid/page", handle=h_alice,
        balloon=LINK_BALLOON, guid="G-LINK-SCAN",
        payload=make_link_payload("https://site.example.invalid/page", "Page",
                                  extra_strings=["https://site.example.invalid/page/",
                                                 "https://site.example.invalid/favicon.ico",
                                                 "https://cdn.example.invalid/pic.png",
                                                 "https://cdn.example.invalid/second.png"]))
    m["link_title_only"] = builders.add_message(
        w, c1, text="title only", handle=h_alice, balloon=LINK_BALLOON,
        guid="G-LINK-TITLE", payload=make_link_payload(title="Just a title"))
    m["link_empty"] = builders.add_message(
        w, c1, text="no url no title", handle=h_alice, balloon=LINK_BALLOON,
        guid="G-LINK-EMPTY", payload=make_link_payload(summary="only a summary"))
    m["link_garbage"] = builders.add_message(
        w, c1, text="garbage payload", handle=h_alice, balloon=LINK_BALLOON,
        guid="G-LINK-GARBAGE", payload=b"not a plist at all")
    m["payload_no_balloon"] = builders.add_message(
        w, c1, text="payload but not a balloon", handle=h_alice, guid="G-PAYLOAD-NB",
        payload=make_link_payload("https://ignored.example.invalid/", "Ignored"))
    # more URL balloons (Phase 2: /link_image's three 404s and the largest-blob rule)
    m["link_jpeg"] = builders.add_message(
        w, c1, text="https://photo.example.invalid/j", is_from_me=1,
        balloon=LINK_BALLOON, guid="G-LINK-JPEG",
        payload=make_link_payload("https://photo.example.invalid/j", "Photo", embed="jpeg"))
    m["link_two_blobs"] = builders.add_message(
        w, c1, body=encode_attributed_body("https://two.example.invalid/b"), handle=h_alice,
        balloon=LINK_BALLOON, guid="G-LINK-TWO",
        payload=make_link_payload("https://two.example.invalid/b", "Two blobs",
                                  embed=[("png", 3200), ("jpeg", 4000), ("png", 4000)]))
    m["link_decoy"] = builders.add_message(
        w, c1, text="https://decoy.example.invalid/", handle=h_alice,
        balloon=LINK_BALLOON, guid="G-LINK-DECOY",
        payload=make_link_payload("https://decoy.example.invalid/", "Decoy",
                                  embed=[("raw", 5000), ("png", 2000)]))
    m["link_no_objects"] = builders.add_message(
        w, c1, text="plist without objects", handle=h_alice, balloon=LINK_BALLOON,
        guid="G-LINK-NOOBJ", payload=plistlib.dumps({"foo": "bar"}, fmt=plistlib.FMT_BINARY))
    m["link_list_plist"] = builders.add_message(
        w, c1, text="plist that is a list", handle=h_alice, balloon=LINK_BALLOON,
        guid="G-LINK-LIST", payload=plistlib.dumps(["a", "b"], fmt=plistlib.FMT_BINARY))
    m["link_empty_payload"] = builders.add_message(
        w, c1, text="balloon with empty payload", handle=h_alice,
        balloon=LINK_BALLOON, guid="G-LINK-EMPTYPAY", payload=b"")
    m["link_only"] = builders.add_message(
        w, c1, text="https://only.example.invalid/path?x=1", handle=h_alice, guid="G-LINK-ONLY")
    # attachments of every flavour
    m["hidden_png"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-HIDDEN-PNG")
    builders.add_attachment(w, m["hidden_png"], "ATT-PNG-HIDDEN", "image/png", "shot.png",
                            "~/synthetic/shot.png", hide=1)
    m["video"] = builders.add_message(w, c1, is_from_me=1, has_att=1, guid="G-VIDEO")
    builders.add_attachment(w, m["video"], "ATT-VID", "video/quicktime", "clip.mov",
                            "~/synthetic/clip.mov")
    m["pdf"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-PDF")
    builders.add_attachment(w, m["pdf"], "ATT-PDF", "application/pdf", "doc.pdf",
                            "~/synthetic/doc.pdf")
    m["null_att"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-NULL-ATT")
    builders.add_attachment(w, m["null_att"], "ATT-NULL", None, None, None)
    m["flag_no_rows"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-FLAG-ONLY")
    m["two_atts"] = builders.add_message(w, c1, text="two files", handle=h_alice, has_att=1,
                                         guid="G-TWO")
    builders.add_attachment(w, m["two_atts"], "ATT-M1", "image/jpeg", "a.jpg", "~/synthetic/a.jpg")
    builders.add_attachment(w, m["two_atts"], "ATT-M2", "image/heic", "b.heic", "~/synthetic/b.heic")
    m["failed_heic"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-FAILED-HEIC")
    builders.add_attachment(w, m["failed_heic"], "ATT-HEIC-FAILED", "image/heic", "c.heic",
                            "~/synthetic/c.heic")
    m["att_flag_off"] = builders.add_message(w, c1, text="rows but flag off", handle=h_alice,
                                             has_att=0, guid="G-FLAG-OFF")
    builders.add_attachment(w, m["att_flag_off"], "ATT-UNFLAGGED", "image/png", "u.png",
                            "~/synthetic/u.png")
    # tapbacks: 2000-2006, 3000-3002, 1000, plus the 2006 emoji row
    verbs = ["Loved", "Liked", "Disliked", "Laughed at", "Emphasized", "Questioned"]
    for t in range(2000, 2006):
        m[f"tap{t}"] = builders.add_message(
            w, c1, text=f'{verbs[t - 2000]} “hello from alice”', handle=h_alice,
            assoc_guid="p:0/G-TEXT", assoc_type=t, guid=f"G-TAP-{t}")
    m["tap2006"] = builders.add_message(
        w, c1, text='Reacted \U0001F525 to “hello from alice”', handle=h_alice,
        assoc_guid="p:0/G-TEXT", assoc_type=2006, assoc_emoji="\U0001F525", guid="G-TAP-2006")
    for t in range(3000, 3003):
        m[f"tap{t}"] = builders.add_message(
            w, c1, text=f'Removed a {verbs[t - 3000].lower()}', is_from_me=1,
            assoc_guid="p:0/G-TEXT", assoc_type=t, guid=f"G-TAP-{t}")
    m["sticker"] = builders.add_message(w, c1, handle=h_alice, assoc_guid="p:0/G-TEXT",
                                        assoc_type=1000, has_att=1, guid="G-STICKER")
    builders.add_attachment(w, m["sticker"], "ATT-STICKER", "image/heic", "sticker.heic",
                            "~/synthetic/sticker.heic", sticker=1)
    m["tap_balloon"] = builders.add_message(w, c1, text="Loved a link", handle=h_alice,
                                            assoc_guid="bp:G-LINK-META", assoc_type=2000,
                                            guid="G-TAP-BP")
    # services
    m["lite"] = builders.add_message(w, c1, text="from a satellite", handle=h_alice,
                                     service="iMessageLite", guid="G-LITE")
    m["svc_none"] = builders.add_message(w, c1, text="service unknown", handle=h_alice,
                                         service=None, guid="G-SVC-NONE")
    m["edited_at_insert"] = builders.add_message(w, c1, text="edited once", is_from_me=1,
                                                 date_edited=builders.BASE_DATE_NS + 10_000,
                                                 guid="G-EDITED-1")
    m["urls"] = builders.add_message(
        w, c1, text="see https://a.example.invalid/x. and https://a.example.invalid/x "
                    "then (https://b.example.invalid/y); done", is_from_me=1, guid="G-URLS")
    # attachments backed by real files under tmp_path (Phase 2: /attachment)
    for guid, (mime, tname, fname, data) in FILE_ATTACHMENTS.items():
        f = files_dir / fname
        f.write_bytes(data)
        key = "file_" + guid.split("-")[-1].lower()
        m[key] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid=f"G-{guid}",
                                      is_from_me=1 if guid == "ATT-FILE-MP4" else 0)
        builders.add_attachment(w, m[key], guid, mime, tname, str(f))
    m["file_gone"] = builders.add_message(w, c1, handle=h_alice, has_att=1, guid="G-ATT-GONE")
    builders.add_attachment(w, m["file_gone"], "ATT-GONE", "image/png", "gone.png",
                            str(files_dir / "gone.png"))                  # never written

    # --- other 1:1 chats -------------------------------------------------------
    m["c1e_1"] = builders.add_message(w, c1e, text="via email", handle=h_alice_email, guid="G-C1E-1")
    m["c1s_sms"] = builders.add_message(w, c1s, text="old sms", handle=h_alice, service="SMS",
                                        guid="G-C1S-1")
    m["c1s_imsg"] = builders.add_message(w, c1s, text="now imessage", is_from_me=1,
                                         service="iMessage", guid="G-C1S-2")
    m["sms_in"] = builders.add_message(w, csms, text="sms in", handle=h_carl_sms, service="SMS",
                                       guid="G-SMS-1")
    m["sms_out"] = builders.add_message(w, csms, text="sms out", is_from_me=1, service="SMS",
                                        guid="G-SMS-2")
    m["rcs_in"] = builders.add_message(w, crcs, text="rcs in", handle=h_bob_rcs, service="RCS",
                                       guid="G-RCS-1")
    m["rcs_out"] = builders.add_message(w, crcs, text="rcs out", is_from_me=1, service="RCS",
                                        guid="G-RCS-2")
    m["rcs_blank_svc"] = builders.add_message(w, crcs, text="blank service", is_from_me=1,
                                              service="", guid="G-RCS-3")
    m["none_in"] = builders.add_message(w, cnone, text="no service here", handle=h_frank,
                                        service=None, guid="G-NONE-1")
    # Incoming row with no handle at all (handle_id = 0): the media link's sender
    # is resolve(None) -> None and the search snippet carries no "who:" prefix.
    m["no_sender_url"] = builders.add_message(
        w, cnone, text="system note https://sys.example.invalid/n.", service=None,
        guid="G-NONE-2")

    # --- groups ----------------------------------------------------------------
    m["g1_alice"] = builders.add_message(w, g1, text="g1 alice", handle=h_alice, guid="G-G1-A")
    m["g1_bob"] = builders.add_message(w, g1, text="g1 bob", handle=h_bob, guid="G-G1-B")
    m["g1_carl"] = builders.add_message(w, g1, text="g1 carl (no join row)", handle=h_carl,
                                        guid="G-G1-C")
    m["g1_me"] = builders.add_message(w, g1, text="g1 me", is_from_me=1, guid="G-G1-ME")
    m["g1_reply"] = builders.add_message(w, g1, text="g1 reply", handle=h_bob,
                                         reply_to="G-G1-C", guid="G-G1-REPLY")
    m["g2_alice"] = builders.add_message(w, g2, text="g2 alice", handle=h_alice, guid="G-G2-A")
    m["g2_self"] = builders.add_message(w, g2, text="g2 from my other identity", handle=h_self,
                                        guid="G-G2-SELF")
    m["g2_me"] = builders.add_message(w, g2, text="g2 me", is_from_me=1, guid="G-G2-ME")
    m["g3_dana"] = builders.add_message(w, g3, text="g3 dana", handle=h_dana, guid="G-G3-D")
    m["g4_alice"] = builders.add_message(w, g4, text="g4 alice", handle=h_alice, guid="G-G4-A")
    m["g4_email"] = builders.add_message(w, g4, text="g4 alice by email", handle=h_alice_email,
                                         guid="G-G4-E")
    m["g5_bob"] = builders.add_message(w, g5, text="g5 bob by sms", handle=h_bob, service="SMS",
                                       guid="G-G5-B")
    m["g5_me"] = builders.add_message(w, g5, text="g5 me https://g5.example.invalid/x",
                                      is_from_me=1, service="SMS", guid="G-G5-ME")

    # --- the date_edited bump (newest edit mark) -------------------------------
    builders.set_date_edited(w, m["text"], builders.BASE_DATE_NS + 20_000)

    return SimpleNamespace(
        messages=m,
        chats={"c1": c1, "c1e": c1e, "c1s": c1s, "csms": csms, "crcs": crcs,
               "g1": g1, "g2": g2, "g3": g3, "g4": g4, "g5": g5, "cnone": cnone},
        chat_guids=[C1_GUID, C1E_GUID, C1S_GUID, CSMS_GUID, CRCS_GUID,
                    G1_GUID, G2_GUID, G3_GUID, G4_GUID, G5_GUID, CNONE_GUID],
        empty_chat=empty,
        files_dir=files_dir,
        all_rowids=sorted(m.values()),
    )


def populate_compat_db(fx, files_dir: Path) -> SimpleNamespace:
    """``build_compat_db`` on a ``FixtureDB`` (``conftest.create_fixture_db``), WAL
    checkpointed so the file is complete; returns the info namespace with
    ``path`` and ``writer`` attached."""
    info = build_compat_db(fx.writer, files_dir)
    fx.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    info.path = fx.path
    info.writer = fx.writer
    return info


def add_null_identifier_chat(w: sqlite3.Connection) -> int:
    """A 1:1 chat whose ``chat_identifier`` is NULL with one incoming row from
    Alice (``contact_recency`` must ignore it); returns the chat ROWID."""
    h = builders.handle_rowid(w, ALICE_PHONE, create=False)
    cid = builders.add_chat(w, "any;-;synthetic-null-identifier", 45, "placeholder", handles=[h])
    w.execute("UPDATE chat SET chat_identifier = NULL WHERE ROWID = ?", (cid,))
    w.commit()
    builders.add_message(w, cid, text="synthetic row in a NULL-identifier chat", handle=h)
    return cid


# ---------------------------------------------------------------------------
# relay state the database expects
# ---------------------------------------------------------------------------

#: The relay-state globals ``fetch_threads`` reads.
THREAD_STATE_GLOBALS = ("PINS", "ARCHIVED", "AUTO_TRANSLATE", "FORCED_UNREAD", "NO_ICON")


def seed_relay_state(module) -> None:
    """Give the relay module the synthetic contacts (keyed with its own
    ``norm_key``), self identity and failed-HEIC guid the compat database expects."""
    names = {module.norm_key(addr): name for addr, name in SYNTHETIC_CONTACTS.items()}
    # One synthetic name is one contact card: its addresses share the card's smallest key.
    canon = {k: min(x for x, n in names.items() if n == name) for k, name in names.items()}
    module.set_contacts(names, canon)
    module.SELF_RAW[:] = [SELF_PHONE]
    module.FAILED_HEIC.clear(); module.FAILED_HEIC.add("ATT-HEIC-FAILED")


def seed_relay_thread_state(relay, state: dict) -> None:
    """Write ``state`` as the relay's state file and re-derive the globals
    relay.py derives from it at import (PINS / NO_ICON / ARCHIVED /
    AUTO_TRANSLATE / FORCED_UNREAD) the same way.  ``relay`` is the conftest's
    ``RelayNew`` (``.module`` + ``.state_path``)."""
    r = relay.module
    relay.state_path.write_text(json.dumps(state))
    relay.state_path.with_suffix(".bak").unlink(missing_ok=True)
    st = r.load_state()
    assert st == json.loads(json.dumps(state))
    r.PINS[:] = list(st.get("pins", []))
    r.NO_ICON.clear()
    r.NO_ICON.update({k: float(v) for k, v in (st.get("no_icon") or {}).items()})
    r.ARCHIVED.clear(); r.ARCHIVED.update(set(st.get("archived", [])))
    r.AUTO_TRANSLATE.clear(); r.AUTO_TRANSLATE.update(set(st.get("auto_translate", [])))
    r.FORCED_UNREAD.clear(); r.FORCED_UNREAD.update(set(st.get("forced_unread", [])))


def thread_state_first_run(now: float) -> dict:
    """Pins, archived, auto-translate, forced unread, a fresh and an expired
    no-icon entry (TTL 3600) -- and no reads / baseline, so the first
    ``fetch_threads`` initialises the baseline."""
    return {"pins": [G1_GUID, C1_GUID], "archived": [CSMS_GUID], "auto_translate": [CRCS_GUID],
            "forced_unread": [C1E_GUID],
            "no_icon": {G2_GUID: now, G3_GUID: now - 7200},
            "last_rowid": 5}


def thread_state_with_reads(now: float, m: dict) -> dict:
    """``thread_state_first_run`` plus a baseline and per-chat read marks."""
    return dict(thread_state_first_run(now),
                reads_baseline=m["link_scan"],
                reads={C1_GUID: m["text"], G1_GUID: m["g1_bob"], CSMS_GUID: MISSING_ROWID,
                       C1E_GUID: MISSING_ROWID})


# ---------------------------------------------------------------------------
# the case tables (what the recorder records and the tests replay)
# ---------------------------------------------------------------------------

#: ``fetch_thread_messages(guid, 50, None)`` for each.
THREAD_MESSAGE_GUIDS = [C1_GUID, C1E_GUID, C1S_GUID, CSMS_GUID, CRCS_GUID,
                        G1_GUID, G2_GUID, G3_GUID, G4_GUID, NO_SUCH_GUID]


def preview_cases(info) -> dict[str, list[int]]:
    """``last_message_previews`` rowid sets, by case name."""
    m = info.messages
    return {
        "all": list(info.all_rowids),
        "attachments": [m["att_only_heic"], m["video"], m["pdf"], m["null_att"],
                        m["flag_no_rows"], m["two_atts"], m["failed_heic"], m["sticker"]],
        "tapbacks": [m[f"tap{t}"] for t in (*range(2000, 2007), *range(3000, 3003))],
        "unordered": [m["urls"], m["text"], m["g4_email"], m["blob"]],
        "unknown": [m["text"], MISSING_ROWID],
        "empty": [],
    }


FIND_CASES = {
    "one_to_one_phone": [ALICE_PHONE],
    "one_to_one_email": [ALICE_EMAIL],
    "one_to_one_carl_sms": [CARL_PHONE],
    "one_to_one_bob_rcs": [BOB_PHONE],
    "group_with_missing_join_row": [ALICE_PHONE, BOB_PHONE, CARL_PHONE],
    "group_self_included": [ALICE_PHONE, BOB_PHONE, SELF_PHONE],
    "group_self_implicit": [ALICE_PHONE, BOB_PHONE],
    "two_addresses_one_person": [ALICE_PHONE, ALICE_EMAIL],
    "five_people": [ALICE_PHONE, BOB_PHONE, DANA_PHONE, CARL_PHONE, ALICE_EMAIL],
    "no_match_unknown": [NOBODY_PHONE],
    "no_match_group": [ALICE_PHONE, BOB_PHONE, DANA_PHONE],
    "only_self": [SELF_PHONE],
    "empty": [],
}

#: What each FIND_CASES entry must resolve to (``None`` = no match).
FIND_EXPECTED_GUID = {
    # The SMS-named chat holds Alice's newest rows, so "newest activity wins"
    # picks it over the iMessage one (the startswith tie-break never fires).
    "one_to_one_phone": C1S_GUID, "one_to_one_email": C1S_GUID,
    "one_to_one_carl_sms": CSMS_GUID, "one_to_one_bob_rcs": CRCS_GUID,
    "group_with_missing_join_row": G1_GUID, "group_self_included": G2_GUID,
    "group_self_implicit": G2_GUID, "two_addresses_one_person": G4_GUID,
    "five_people": G3_GUID,
}


def chat_services_cases(info) -> dict[str, list]:
    rids = list(info.chats.values())
    return {"all": rids, "with_unknown": [rids[0], MISSING_CHAT_ROWID],
            "none_and_str": [None, str(rids[1])], "empty": []}


#: ``group_title(conn, chat, display_name)`` cases as (chat key, display name);
#: ``"missing"`` is a chat ROWID that does not exist.
GROUP_TITLE_CASES = [("g1", None), ("g2", "Synthetic Crew"), ("g2", None), ("g3", None),
                     ("g4", None), ("c1", None), ("missing", None), ("missing", "Named Anyway")]


def chat_rowid(info, key: str) -> int:
    return MISSING_CHAT_ROWID if key == "missing" else info.chats[key]


def group_title_key(key: str, display: str | None) -> str:
    return f"{key}|{display!r}"


SEARCH_CASES = [
    ("alice", 30),                                   # text-column hits across chats
    ("blob only", 30), ("empty column", 30),         # blob-only / "" column + blob
    ("hello", 30), ("HELLO", 30), ("Hello From", 30), ("hELLo fRoM", 30),   # case variants
    ("éè", 30), ("\U0001F60A", 30),                  # non-ASCII inside a mutable-skeleton blob
    ("NSString", 30),                                # class name in every blob; real text once
    ("kIMMessagePart", 30), ("streamtyped", 30),     # attribute key / header: never real text
    ("Loved", 30), ("Removed a", 30), ("Reacted", 30),   # tapback rows are excluded
    ("example.invalid", 30), ("example.invalid", 3), ("example.invalid", 1),   # limit
    ("https://", 4), ("g1", 30), ("sms", 30), ("rcs", 30), ("system note", 30),
    ("two", 30), ("  hello  ", 30), ("synthetic", 30),
    ("a", 30), (" ", 30), ("", 30), (" x ", 30),     # the len(q) < 2 guard (after strip)
]


def search_key(q: str, limit: int) -> str:
    return f"{q!r}:{limit}"


def newer_blob_rows(conn: sqlite3.Connection, info) -> int:
    """How many ``attributedBody`` rows are newer than the ``NSString`` needle row:
    the search's ``limit * 2`` oversample has to get past all of them."""
    return conn.execute(
        "SELECT count(*) FROM message WHERE attributedBody IS NOT NULL AND ROWID > ?",
        (info.messages["needle"],)).fetchone()[0]


LINK_IMAGE_OK = {"link_embedded": "image/png", "link_jpeg": "image/jpeg",
                 "link_two_blobs": "image/jpeg"}
LINK_IMAGE_404 = {
    "text": "no payload", "link_empty_payload": "no payload", "link_only": "no payload",
    "link_garbage": "unparseable payload", "link_no_objects": "unparseable payload",
    "link_list_plist": "unparseable payload",
    "link_meta": "no embedded image", "link_scan": "no embedded image",
    "link_decoy": "no embedded image", "link_title_only": "no embedded image",
    "link_empty": "no embedded image", "payload_no_balloon": "no embedded image",
}
#: Golden key for ``link_image(MISSING_ROWID)``.
LINK_IMAGE_MISSING_KEY = "missing_rowid"


def thread_media_guids(info) -> list[str]:
    return [*info.chat_guids, EMPTY_GUID, NO_SUCH_GUID]


#: ``/attachment`` guids that 404 before the HEIC branch, with the detail.
ATTACHMENT_404 = {"NO-SUCH-ATT": "unknown attachment",
                  "ATT-NULL": "file missing on disk",        # NULL filename
                  "ATT-GONE": "file missing on disk"}        # path never written


# ---------------------------------------------------------------------------
# endpoint outcomes that are not plain JSON, normalised
# ---------------------------------------------------------------------------

OCTET_STREAM = "application/octet-stream"       # the /attachment endpoint's default media type


def relative_path(path: str, files_dir: Path) -> str:
    """``path`` relative to the fixture's files directory; refuses anything outside it
    (an absolute path is a temporary directory and must never reach a golden file)."""
    return str(Path(path).resolve().relative_to(Path(files_dir).resolve()))


def bytes_outcome(body: bytes) -> dict:
    return {"status": 200, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}


def link_image_outcome(r, rowid: int) -> dict:
    """``r.link_image(rowid)`` as ``{"status": 200, "sha256", "size"}`` or ``{"status", "detail"}``."""
    try:
        resp = r.link_image(rowid)
    except HTTPException as e:
        return {"status": e.status_code, "detail": e.detail}
    return bytes_outcome(bytes(resp.body))


def attachment_outcome(r, guid: str, files_dir: Path) -> dict:
    """``r.attachment(guid)`` as ``{"status": 200, "path" (relative), "media_type", "filename"}``
    or ``{"status", "detail"}``.  Non-HEIC guids only: the HEIC branch runs ``sips``."""
    try:
        resp = r.attachment(guid)
    except HTTPException as e:
        return {"status": e.status_code, "detail": e.detail}
    return {"status": 200, "path": relative_path(resp.path, files_dir),
            "media_type": resp.media_type, "filename": resp.filename}


# ---------------------------------------------------------------------------
# golden files
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

#: Every golden file the recorder writes (``<name>.json``).
GOLDEN_NAMES = ("fetch_new", "fetch_edited", "scalars", "fetch_thread_messages",
                "last_message_previews", "find_chat_for_addresses", "chat_services",
                "last_rowid_for", "group_title", "search", "link_image", "thread_media",
                "fetch_threads", "attachment", "contact_recency", "health")


def dumps(obj: Any) -> str:
    """The comparison form: what the relay's JSON responses are made of."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=False)


def golden_text(obj: Any) -> str:
    """The on-disk form (readable, diffable); ``json.loads`` of it dumps back to ``dumps(obj)``."""
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


@functools.lru_cache(maxsize=None)
def load_golden(name: str) -> dict:
    """The recorded cases of ``tests/golden/<name>.json`` (cached; never mutated)."""
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


#: The keys step R2 (plan 2026-10-06, section 4) added to the authenticated
#: ``/health`` body. ``health.json`` was re-recorded with them in the R2 commit
#: (section 7, objection 4), so a relay that predates R2 is compared against
#: the golden WITHOUT them: the default suite stays green on the live
#: ``relay.py`` until the swap lands, and the golden on disk stays the R2 one.
R2_HEALTH_KEYS = ("engines", "features", "protocol")

#: The key step R6 (edit and undo send, 2026-10-07) added after them, by the
#: same rule: ``health.json`` was re-recorded with it, and a relay that
#: predates R6 (``relay.py`` until the swap) is compared without it.
R6_HEALTH_KEYS = ("capabilities",)


def has_r2_health(module: Any) -> bool:
    """Does this relay module answer ``/health`` with the R2 keys? (The
    send-engine chain and the keys landed in the same step.)"""
    return hasattr(module, "_chain")


def has_r6_health(module: Any) -> bool:
    """Does this relay module answer ``/health`` with the R6 key? (The
    ``/edit`` route and the key landed in the same step; an R6 relay is an R2
    relay too.)"""
    return has_r2_health(module) and hasattr(module, "edit_message")


def _without(golden: dict, keys: tuple[str, ...]) -> dict:
    return {**golden, "full_shape": [k for k in golden["full_shape"] if k not in keys]}


def strip_r6_health(golden: dict) -> dict:
    """The ``health`` golden without the R6 key (what an R2..R5 relay answers)."""
    return _without(golden, R6_HEALTH_KEYS)


def strip_r2_health(golden: dict) -> dict:
    """The ``health`` golden without the R2 keys and without the R6 key that
    came after them (what a pre-R2 relay answers)."""
    return _without(golden, R2_HEALTH_KEYS + R6_HEALTH_KEYS)


def health_golden_for(golden: dict, *, r2: bool, r6: bool) -> dict:
    """``golden`` as a relay with those steps answers it: unchanged with both,
    the R6 key dropped without R6, the R2 keys dropped as well without R2."""
    if not r2:
        return strip_r2_health(golden)
    return golden if r6 else strip_r6_health(golden)


def expected_health_golden(module: Any, golden: dict) -> dict:
    """``golden`` as the ``/health`` test should expect it from ``module``:
    unchanged for an R6 relay, the keys of the steps it predates dropped for
    an earlier one."""
    return health_golden_for(golden, r2=has_r2_health(module), r6=has_r6_health(module))
