"""B2: random contact-card sets against /create_chat, /match_chat, /contacts/search and the voice path.

In-process only. Contacts come from the real load_contacts() (a fake BlueBubbles contact list), the
sends go into the suite's recording BlueBubbles client, chat.db is the synthetic database (emptied and
refilled per seed). Every number is from the 555 range or a drama/documentation range; e-mail is example.com.
"""
from __future__ import annotations

import collections
import os
import random
import re
from typing import Any

import pytest

from tests import compat_fixture as cf
from tests.fixtures import builders
from tests.test_relay_compat import compat_db, relay_state  # noqa: F401  (fixtures)
from tests.test_send_path import AUTH, FakeResponse, bb, client, osa, r  # noqa: F401  (fixtures)

TEXT = "synthetic hello"
RESEMBLED: list = []
SELF = cf.SELF_PHONE
US = ["+1555010%04d" % i for i in range(200, 224)]
UK = ["+4477009001%02d" % i for i in range(10, 16)]
IL = ["+9725255512%02d" % i for i in range(10, 14)]
SAME10 = ["+445550100200", "+445550100201", "+9725550100202"]     # same last ten digits as US[0..2]
MAIL = ["p%02d@example.com" % i for i in range(12)]
POOL = US + UK + IL + SAME10 + MAIL
UNUSABLE = ["555-0100", "+1 555 010 0200 x12", "*67 555 010 0201", "1-800-FLOWERS", "+1 055 010 0200",
            "0155 010 0202", "555 010 0203 ext 4", "+1 555 010 0204,,12", "5550100205;ext=9", "+15550100206#"]
NAMES = ["Alex Rivera", "Blair Chen", "Chris Park", "Chris Park", "Dana Kim", "Dan Kim", "Sara Lee", "Sarah Lee",
         "Sam Quinn", "sam quinn", "Sam Reyes", "Pat Quinn", "Mom", "Ann", "Ann Marie", "Ann Marie Cole",
         "Jon Smith", "John Smith", "Karen Smith", "Kaden Smith", "Wei", "Hu", "Robin Vale", "Robyn Vale",
         "Bo", "Bob Stone", "Rob Stone", "Acme Plumbing", "Jordan Lake", "Morgan Lake", "Taylor Reed"]


def _ok() -> FakeResponse:
    return FakeResponse(200, {"status": 200, "data": {"guid": "synthetic-bb-guid"}})


def spell(rng, a: str) -> str:
    if "@" in a:
        return rng.choice([a, a.upper(), a.capitalize(), " " + a, "mailto:" + a])
    if a.startswith("+1"):
        n = a[2:]
        return rng.choice([a, f"({n[:3]}) {n[3:6]}-{n[6:]}", f"{n[:3]}-{n[3:6]}-{n[6:]}", f"1-{n[:3]}-{n[3:6]}-{n[6:]}",
                           f"+1 ({n[:3]}) {n[3:6]}-{n[6:]}", "tel:" + a, "‪+1 %s %s %s‬" % (n[:3], n[3:6], n[6:]),
                           a + "X-SHARED-PHOTO-DISPLAY-PREF", n, "1" + n, "001" + n, "011 1 " + n])
    cc = "44" if a.startswith("+44") else "972"
    n = a[1 + len(cc):]
    return rng.choice([a, f"+{cc} {n[:4]} {n[4:]}", f"+{cc} (0){n[:4]} {n[4:]}", f"011 {cc} {n}", f"00{cc}{n}",
                       f"+{cc}-{n[:2]}-{n[2:]}"])


def make_cards(rng):
    """-> [(name, [canonical addresses], [unusable strings])]"""
    free = POOL[:]
    rng.shuffle(free)
    names = rng.sample(NAMES, rng.randint(4, 12))
    cards = []
    for name in names:
        own = [free.pop() for _ in range(rng.choice([0, 1, 1, 2, 2, 3])) if free]
        cards.append([name, own, []])
    for _ in range(rng.choice([0, 1, 1, 2, 3])):                       # shared addresses
        used = [a for c in cards for a in c[1]]
        a = free.pop() if (free and rng.random() < 0.6) or not used else rng.choice(used)
        for c in rng.sample(cards, min(len(cards), rng.choice([2, 2, 3]))):
            if a not in c[1]:
                c[1].append(a)
    for _ in range(rng.choice([0, 0, 1, 2, 3])):                       # duplicates, subsets, supersets
        src = rng.choice(cards)
        kind = rng.choice(["exact", "exact", "rename", "subset", "superset", "case", "soundalike"])
        if kind == "exact":
            cards.append([src[0], src[1][:], []])
        elif kind == "rename":
            cards.append([rng.choice(NAMES), src[1][:], []])
        elif kind == "subset" and len(src[1]) >= 2:
            cards.append([src[0], rng.sample(src[1], rng.randint(1, len(src[1]) - 1)), []])
        elif kind == "superset" and free:
            cards.append([src[0], src[1][:] + [free.pop()], []])
        elif kind == "case":
            cards.append([src[0].upper(), src[1][:] if rng.random() < 0.5 else ([free.pop()] if free else []), []])
        elif kind == "soundalike":
            cards.append([src[0] + "h", [free.pop()] if free and rng.random() < 0.7 else src[1][:], []])
    for c in cards:
        if rng.random() < 0.15:
            c[2].append(rng.choice(UNUSABLE))
    if rng.random() < 0.3:                                             # the owner's own card
        mine = [SELF]
        if free and rng.random() < 0.5:
            mine.append(free.pop())
        used = [a for c in cards for a in c[1]]
        if used and rng.random() < 0.5:
            mine.append(rng.choice(used))
        cards.append(["Jordan Owner", mine, []])
    rng.shuffle(cards)
    for c in cards:
        rng.shuffle(c[1])
    return cards, free


def load(r, monkeypatch, rng, cards):
    records = []
    for name, addrs, bad in cards:
        entries = [spell(rng, a) for a in addrs] + bad
        rng.shuffle(entries)
        rec = {"phoneNumbers": [{"address": a} for a in entries if "@" not in a],
               "emails": [{"address": a} for a in entries if "@" in a]}
        if rng.random() < 0.8:
            rec["displayName"] = name
        else:
            parts = name.split(" ", 1)
            rec["firstName"] = parts[0]
            if len(parts) > 1:
                rec["lastName"] = parts[1]
        records.append(rec)

    class _Client:
        def __init__(self, **_: Any):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def get(self, url: str, params: Any = None, **_: Any):
            return FakeResponse(200, {"status": 200, "data": records})

    monkeypatch.setattr(r.httpx, "Client", _Client)
    assert r.load_contacts() is True


def wipe(w):
    have = {row[0] for row in w.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("chat_message_join", "chat_handle_join", "message_attachment_join", "attachment", "message", "chat", "handle"):
        if t in have:
            w.execute(f"DELETE FROM {t}")


class World:
    def __init__(self, r, client, bb, w, monkeypatch, seed):
        self.r, self.client, self.bb, self.seed = r, client, bb, seed
        rng = self.rng = random.Random(seed)
        self.cards, free = make_cards(rng)
        self.cards_of = collections.defaultdict(set)
        for i, (_, addrs, _bad) in enumerate(self.cards):
            for a in addrs:
                self.cards_of[a].add(i)
        self.cards_of = {a: frozenset(s) for a, s in self.cards_of.items()}
        me = self.cards_of.get(SELF)
        self.selfset = {SELF} | ({a for a, s in self.cards_of.items() if s == me} if me else set())
        self.inuse = sorted(self.cards_of)
        self.strangers = free[:3]
        wipe(w)
        self.chats = {}
        n = 0
        order = self.inuse + self.strangers
        rng.shuffle(order)
        groups = []
        everyone = [a for a in order if a != SELF]
        for _ in range(5):
            if len(everyone) >= 2:
                groups.append(rng.sample(everyone, min(len(everyone), rng.choice([2, 2, 3, 4]))))
        for _, addrs, _bad in self.cards:
            if len(addrs) >= 2 and rng.random() < 0.5:
                groups.append(rng.sample(addrs, 2))                      # a "group" of one person's two addresses
            if addrs and rng.random() < 0.3 and everyone:
                groups.append(list({rng.choice(addrs), rng.choice(everyone)}))
        groups = [g for g in groups if len(g) >= 1]
        todo = [("one", a) for a in order] + [("group", g) for g in groups]
        rng.shuffle(todo)
        for kind, what in todo:
            n += 1
            if kind == "one":
                svc = "iMessage"
                guid = f"{svc};-;{what}"
                chat = builders.add_chat(w, guid, 45, what, handles=[what])
                self.chats[guid] = [what]
                if rng.random() < 0.2:
                    g2 = f"SMS;-;{what}"
                    c2 = builders.add_chat(w, g2, 45, what, handles=[what])
                    builders.add_message(w, c2, guid=f"B2-{seed}-{n}s", text="synthetic", is_from_me=1)
                    self.chats[g2] = [what]
            else:
                guid = f"iMessage;+;chat{seed}x{n}"
                members = list(dict.fromkeys(list(what) + ([SELF] if rng.random() < 0.15 else [])))
                chat = builders.add_chat(w, guid, 43, f"chat{seed}x{n}", handles=members)
                self.chats[guid] = list(what)
            builders.add_message(w, chat, guid=f"B2-{seed}-{n}", text="synthetic", is_from_me=1)
        self.groups = groups
        load(r, monkeypatch, rng, self.cards)

    def same(self, a, b):
        return a == b or (a in self.cards_of and self.cards_of.get(a) == self.cards_of.get(b))

    def create(self, spelled):
        self.bb.calls.clear()
        self.bb.answers = [_ok()]
        resp = self.client.post("/create_chat", json={"addresses": spelled, "text": TEXT}, headers=AUTH)
        out = []
        for c in self.bb.calls:
            if c.path == "/api/v1/message/text":
                out.append(("text", c.json["chatGuid"]))
            elif c.path == "/api/v1/chat/new":
                out.append(("new", tuple(c.json["addresses"])))
        return resp.status_code, out

    def judge(self, want, sends):
        """want: canonical addresses asked for. -> None if right, else a reason."""
        if len(sends) != 1:
            return f"{len(sends)} sends"
        kind, what = sends[0]
        if kind == "new":
            return None if list(what) == list(want) else f"new chat with {what}, asked {want}"
        members = self.chats.get(what)
        if members is None:
            return f"sent into unknown chat {what}"
        q = {a for a in want if a not in self.selfset}
        m = {a for a in members if a not in self.selfset}
        if all(any(self.same(x, y) for y in q) for x in m) and all(any(self.same(y, x) for x in m) for y in q):
            return None
        return f"sent into {what} with members {sorted(m)}, asked {sorted(q)}"


def run_seed(r, client, bb, w, monkeypatch, seed, stats, bad):
    W = World(r, client, bb, w, monkeypatch, seed)
    rng = W.rng

    def note(kind, detail):
        bad.append((seed, kind, detail, [(n, a, u) for n, a, u in W.cards]))

    # keys: one number, one key; two numbers, two keys
    keys = {}
    for a in W.inuse + W.strangers:
        ks = {r.norm_key(spell(rng, a)) for _ in range(4)} | {r.norm_key(a)}
        if len(ks) != 1:
            note("split-key", (a, ks))
        k = next(iter(ks))
        if k in keys and keys[k] != a:
            note("key-collision", (a, keys[k], k))
        keys[k] = a

    # 1:1, as written on the card and as the canonical address
    for a in W.inuse + W.strangers:
        if a in W.selfset:
            continue
        for s in (spell(rng, a), a):
            code, sends = W.create([s])
            stats["one"] += 1
            if code != 200:
                stats["one-refused"] += 1
                if sends:
                    note("send-on-error", (s, sends))
                continue
            why = W.judge([a], sends)
            if why:
                note("one-to-one", (s, why))
            elif sends[0][0] == "new":
                stats["one-new(fail-closed)"] += 1

    # groups: the exact members, a member swapped for another address, one dropped, one added, random sets
    everyone = [a for a in W.inuse + W.strangers if a not in W.selfset]
    asks = []
    for g in W.groups:
        asks.append(list(g))
        for i, mem in enumerate(g):
            sib = [x for x in W.inuse if x != mem and W.same(x, mem)]
            if sib:
                asks.append(g[:i] + [rng.choice(sib)] + g[i + 1:])
        if everyone:
            i = rng.randrange(len(g))
            asks.append(g[:i] + [rng.choice(everyone)] + g[i + 1:])
            asks.append(list(g) + [rng.choice(everyone)])
        if len(g) > 1:
            asks.append(g[1:])
        if W.selfset and rng.random() < 0.3:
            asks.append(list(g) + [rng.choice(sorted(W.selfset))])
    for _ in range(6):
        if len(everyone) >= 2:
            asks.append(rng.sample(everyone, min(len(everyone), rng.choice([2, 2, 3]))))
    for want in asks:
        want = list(dict.fromkeys(want))
        if not [a for a in want if a not in W.selfset]:
            continue
        code, sends = W.create([spell(rng, a) for a in want])
        stats["group-asks"] += 1
        if code != 200:
            stats["group-refused"] += 1
            if sends:
                note("send-on-error", (want, sends))
            continue
        if sends and sends[0][0] == "text":
            stats["group-into-existing"] += 1
        why = W.judge(want, sends)
        if why:
            note("group", (want, why))

    # typeahead: every offered recipient is an address on a card with that name
    by_name = collections.defaultdict(set)
    for n, addrs, _bad in W.cards:
        by_name[n].update(addrs)
    qs = {n[:3].lower() for n, _, _ in W.cards} | {"555", "7700", "exam", "972", "010"}
    for q in qs:
        res = client.get("/contacts/search", params={"q": q}, headers=AUTH).json()["results"]
        for row in res:
            stats["offered"] += 1
            if row["address"] not in by_name.get(row["name"], ()):
                note("search", (q, row))

    # voice
    lower = collections.defaultdict(list)
    for i, (n, addrs, _bad) in enumerate(W.cards):
        lower[n.lower()].append(frozenset(addrs))
    for said in sorted({n for n, _, _ in W.cards}):
        ask = client.post("/v/prepare", data={"query": f"text {said} {TEXT}"}, headers=AUTH).text
        stats["voice-asked"] += 1
        m = re.match(r"^Send (.*) to (.+)\?$", ask)
        if m:
            answer, display = "yes", m.group(2)
        elif ask.startswith("Did you mean") and said in ask:
            answer, display = said, said
        else:
            stats["voice-refused"] += 1
            continue
        bb.calls.clear()
        bb.answers = [_ok()]
        client.post("/v/confirm", data={"answer": answer}, headers=AUTH)
        sends = []
        for c in bb.calls:
            if c.path == "/api/v1/message/text":
                sends.append(("text", c.json["chatGuid"]))
            elif c.path == "/api/v1/chat/new":
                sends.append(("new", tuple(c.json["addresses"])))
        if not sends:
            stats["voice-no-send"] += 1
            continue
        stats["voice-sent"] += 1
        if display.lower() != said.lower():
            stats["voice-other-name-read-back"] += 1
            RESEMBLED.append((seed, said, display, ask, sends, [(n, a, u) for n, a, u in W.cards if n in (said, display)]))
        people = set(lower.get(display.lower(), ()))
        if len(people) != 1:
            note("voice-namesake", (said, display, sends, sorted(map(sorted, people))))
            continue
        card = next(iter(people))
        kind, what = sends[0]
        got = list(what) if kind == "new" else W.chats.get(what, ["?"])
        if len(got) != 1 or got[0] not in card:
            note("voice-wrong-chat", (said, display, sends, sorted(card)))
        elif len(W.cards_of[got[0]]) > 1 and any(len(W.cards_of[a]) == 1 for a in card):
            # allowed only when every card listing it is this same person (exact duplicate cards)
            if any(frozenset(W.cards[i][1]) != card for i in W.cards_of[got[0]]):
                note("voice-shared-address-used", (said, display, sends, sorted(card)))


# Two chunks of forty random contact books by default (seconds); RELAY_FUZZ="6 500" is the reviewers' run.
CHUNKS, PER = (int(x) for x in __import__("os").environ.get("RELAY_FUZZ", "2 40").split())


@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_b2_random_cards(r, client, bb, osa, compat_db, monkeypatch, chunk):
    per = PER
    stats, bad = collections.Counter(), []
    for seed in range(chunk * per, (chunk + 1) * per):
        run_seed(r, client, bb, compat_db.writer, monkeypatch, seed, stats, bad)
    kinds = collections.Counter(k for _, k, _, _ in bad)
    print(f"\n[B2 chunk {chunk}] seeds {chunk * per}..{(chunk + 1) * per - 1} | {dict(stats)} | VIOLATIONS {dict(kinds)}")
    for row in RESEMBLED[:6]:
        print("  read back another name:", row)
    RESEMBLED.clear()
    shown = collections.Counter()
    for seed, kind, detail, cards in bad:
        if shown[kind] < 3:
            shown[kind] += 1
            print(f"  seed {seed} [{kind}] {detail}\n      cards: {cards}")
    assert not bad
