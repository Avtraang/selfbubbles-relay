"""B2: fuzz normalize_address / norm_key. Synthetic numbers only (random digits in
national number shapes; nothing is sent, nothing leaves the process)."""
from __future__ import annotations

import collections
import random

import pytest

from tests.test_send_path import r  # noqa: F401  (fixture)

D = "0123456789"


def nd(rng, n):
    return "".join(rng.choice(D) for _ in range(n))


# cc -> (trunk prefix, generator of a national significant number, that country's own international prefixes)
COUNTRIES = {
    "1":   ("1", lambda g: g.choice("23456789") + nd(g, 2) + g.choice("23456789") + nd(g, 6), ["011"]),
    "44":  ("0", lambda g: g.choice(["7" + nd(g, 9), "20" + nd(g, 8), "1" + g.choice("23456789") + nd(g, 8), "1" + g.choice("23456789") + nd(g, 7)]), ["00"]),
    "33":  ("0", lambda g: g.choice("12345679") + nd(g, 8), ["00"]),
    "49":  ("0", lambda g: g.choice(["15" + nd(g, 9), "17" + nd(g, 8), "30" + nd(g, g.choice([6, 7, 8])), "89" + nd(g, 7), "2" + g.choice("123456789") + nd(g, g.choice([5, 6, 7, 8]))]), ["00"]),
    "39":  ("", lambda g: g.choice(["0" + g.choice("123456789") + nd(g, g.choice([6, 7, 8, 9])), "3" + nd(g, 9)]), ["00"]),
    "7":   ("8", lambda g: g.choice("3489") + nd(g, 9), ["810"]),
    "52":  ("", lambda g: g.choice("23456789") + nd(g, 9), ["00"]),
    "61":  ("0", lambda g: g.choice("23478") + nd(g, 8), ["0011"]),
    "91":  ("0", lambda g: g.choice("6789") + nd(g, 9), ["00"]),
    "972": ("0", lambda g: g.choice(["5" + nd(g, 8), g.choice("23489") + nd(g, 7), "7" + nd(g, 8)]), ["00", "012", "013", "014"]),
    "81":  ("0", lambda g: g.choice(["90" + nd(g, 8), "80" + nd(g, 8), "3" + nd(g, 8), "6" + nd(g, 8)]), ["010"]),
    "86":  ("0", lambda g: g.choice(["1" + g.choice("3456789") + nd(g, 9), "10" + nd(g, 8), "21" + nd(g, 8)]), ["00"]),
    "55":  ("0", lambda g: g.choice("123456789") + g.choice("123456789") + g.choice(["9" + nd(g, 8), g.choice("2345") + nd(g, 7)]), ["0021", "0015", "0041"]),
    "82":  ("0", lambda g: g.choice(["10" + nd(g, 8), "2" + nd(g, g.choice([7, 8]))]), ["001", "002", "00700"]),
    "65":  ("", lambda g: g.choice("689") + nd(g, 7), ["001", "002", "008"]),
    "34":  ("", lambda g: g.choice("6789") + nd(g, 8), ["00"]),
    "31":  ("0", lambda g: g.choice("123456") + nd(g, 8), ["00"]),
    "41":  ("0", lambda g: g.choice("234789") + nd(g, 8), ["00"]),
    "46":  ("0", lambda g: "7" + nd(g, 8), ["00"]),
    "27":  ("0", lambda g: g.choice("678") + nd(g, 8), ["00"]),
    "234": ("0", lambda g: g.choice("789") + g.choice("01") + nd(g, 8), ["009"]),
    "254": ("0", lambda g: "7" + nd(g, 8), ["000"]),
    "20":  ("0", lambda g: "1" + nd(g, 9), ["00"]),
    "90":  ("0", lambda g: "5" + nd(g, 9), ["00"]),
    "62":  ("0", lambda g: "8" + nd(g, g.choice([8, 9, 10])), ["001", "007", "008"]),
    "63":  ("0", lambda g: "9" + nd(g, 9), ["00"]),
    "64":  ("0", lambda g: "2" + nd(g, g.choice([7, 8, 9])), ["00"]),
    "353": ("0", lambda g: "8" + nd(g, 8), ["00"]),
    "351": ("", lambda g: "9" + nd(g, 8), ["00"]),
    "30":  ("", lambda g: "69" + nd(g, 8), ["00"]),
    "48":  ("", lambda g: g.choice("5678") + nd(g, 8), ["00"]),
    "47":  ("", lambda g: g.choice("49") + nd(g, 7), ["00"]),
    "45":  ("", lambda g: g.choice("2345") + nd(g, 7), ["00"]),
    "852": ("", lambda g: g.choice("5969") + nd(g, 7), ["001"]),
    "971": ("0", lambda g: "5" + nd(g, 8), ["00"]),
    "92":  ("0", lambda g: "3" + nd(g, 9), ["00"]),
    "66":  ("0", lambda g: g.choice("689") + nd(g, 8), ["001"]),
    "54":  ("0", lambda g: "9" + nd(g, 10), ["00"]),
    "57":  ("", lambda g: "3" + nd(g, 9), ["005", "007", "009"]),
    "380": ("0", lambda g: g.choice("3569") + nd(g, 8), ["00"]),
    "36":  ("06", lambda g: g.choice(["20", "30", "70"]) + nd(g, 7), ["00"]),
    "370": ("8", lambda g: "6" + nd(g, 7), ["00"]),
    "290": ("", lambda g: g.choice("25") + nd(g, 4), ["00"]),
    "298": ("", lambda g: g.choice("2345") + nd(g, 5), ["00"]),
    "674": ("", lambda g: "55" + nd(g, 5), ["00"]),
}
SEPS = [" ", "-", ".", "", " ", " - ", "/", " ", "‑", "–"]


def grouped(rng, digits):
    out, i = [], 0
    while i < len(digits):
        n = rng.choice([1, 2, 3, 3, 4, 4, 5])
        out.append(digits[i:i + n])
        i += n
    sep = rng.choice(SEPS)
    return sep.join(out)


def paren_first(rng, digits):
    k = rng.choice([1, 2, 3, 4])
    return "(" + digits[:k] + ") " + grouped(rng, digits[k:]) if len(digits) > k + 2 else digits


def dress(rng, s):
    """Things a contact card or a paste carries around a number."""
    w = rng.random()
    if w < 0.06:
        return "‪" + s + "‬"
    if w < 0.10:
        return "‎" + s
    if w < 0.14:
        return "tel:" + s
    if w < 0.17:
        return "  " + s + " "
    if w < 0.20:
        return s.translate({ord(c): 0xFF10 + int(c) for c in D})       # full-width digits
    if w < 0.22:
        return s.translate({ord(c): 0x0660 + int(c) for c in D})       # Arabic-Indic digits
    return s


def international_forms(rng, cc, trunk, n):
    f = [("plus", "+" + cc + n), ("plus", "+" + cc + " " + grouped(rng, n)), ("plus", "+" + cc + "-" + grouped(rng, n)),
         ("plus", "(+" + cc + ") " + grouped(rng, n)), ("plus", "+" + cc + " " + paren_first(rng, n)),
         ("plus", "+ " + cc + " " + grouped(rng, n)), ("plus", "＋" + cc + " " + grouped(rng, n)),
         ("00", "00" + cc + n), ("00", "00 " + cc + " " + grouped(rng, n)), ("00", "00" + cc + "-" + paren_first(rng, n))]
    if trunk == "0":
        f += [("plus(0)", "+" + cc + " (0)" + grouped(rng, n)), ("plus(0)", "+" + cc + "(0) " + grouped(rng, n)),
              ("plus(0)", "+" + cc + " ( 0 ) " + grouped(rng, n))]
    return [(kind, dress(rng, s)) for kind, s in f]


def national_forms(rng, trunk, n):
    f = [("nat-trunk", trunk + n), ("nat-trunk", trunk + grouped(rng, n)), ("nat-trunk", paren_first(rng, trunk + n)),
         ("nat-trunk", trunk + " " + grouped(rng, n))]
    if trunk:
        f += [("nat-bare", n), ("nat-bare", grouped(rng, n))]
    return [(kind, dress(rng, s)) for kind, s in f]


def try_norm(r, s):
    try:
        return r.normalize_address(s)
    except r.BadAddress:
        return None


def run(r, monkeypatch, default, seeds):
    monkeypatch.setattr(r, "DEFAULT_COUNTRY_CODE", default)
    wrong = collections.defaultdict(list)      # (kind, cc) -> examples that came back as ANOTHER number
    refused = collections.Counter()
    ok = collections.Counter()
    keys: dict[str, set] = collections.defaultdict(set)     # norm_key -> true numbers
    twokeys = []
    for seed in range(seeds):
        rng = random.Random(seed * 1000 + int(default))
        cc = rng.choice(list(COUNTRIES))
        trunk, gen, prefixes = COUNTRIES[cc]
        n = gen(rng)
        truth = "+" + cc + n
        forms = international_forms(rng, cc, trunk, n)
        if default == "1":
            forms += [("011", "011" + cc + n), ("011", "011 " + cc + " " + grouped(rng, n))]
        if cc == default:
            forms += national_forms(rng, trunk, n)
        else:
            forms += [("foreign-" + k, s) for k, s in national_forms(rng, trunk, n)]
            forms += [("cc-no-plus", cc + " " + grouped(rng, n)), ("cc-no-plus", cc + n)]
        # the default country's OWN international prefix in front of this number
        for p in COUNTRIES[default][2]:
            if p not in ("00", "011"):
                forms.append(("own-intl-" + p, p + " " + cc + " " + grouped(rng, n)))
        seen_keys = set()
        for kind, s in forms:
            got = try_norm(r, s)
            if got is None:
                refused[kind] += 1
                continue
            if got == truth:
                ok[kind] += 1
                k = r.norm_key(s)
                seen_keys.add(k)
                keys[k].add(truth)
            else:
                wrong[(kind, cc)].append((s, truth, got))
        if len(seen_keys) > 1:
            twokeys.append((truth, seen_keys))
    collide = {k: v for k, v in keys.items() if len(v) > 1}
    return wrong, refused, ok, collide, twokeys


def report(default, wrong, refused, ok, collide, twokeys):
    print(f"\n=== default country {default} ===")
    print("accepted as the number written:", dict(ok))
    print("refused:", dict(refused))
    by_kind = collections.Counter()
    for (kind, cc), ex in wrong.items():
        by_kind[kind] += len(ex)
    print("ANOTHER NUMBER returned, by form:", dict(by_kind))
    shown = collections.Counter()
    for (kind, cc), ex in sorted(wrong.items()):
        if shown[kind] < 4:
            shown[kind] += 1
            s, truth, got = ex[0]
            print(f"   [{kind}] cc {cc}: wrote {s!r} (= {truth}) -> {got}  ({len(ex)} such)")
    print("two numbers, one key:", len(collide), "| one number, two keys:", len(twokeys))


@pytest.mark.parametrize("default", ["1", "44", "972", "49", "39", "7", "33", "61", "91", "52", "81", "82", "55", "234", "86", "65"])
def test_b2_fuzz_numbers(r, monkeypatch, default):
    wrong, refused, ok, collide, twokeys = run(r, monkeypatch, default, int(__import__("os").environ.get("RELAY_FUZZ_NUMBERS", "250")))
    report(default, wrong, refused, ok, collide, twokeys)
    # decisive: a number written WITH its country code ("+", 00, 011) must never come back as another number
    explicit = {k: v for k, v in wrong.items() if k[0] in ("plus", "plus(0)", "00", "011")}
    national = {k: v for k, v in wrong.items() if k[0] in ("nat-trunk", "nat-bare")}
    print("DECISIVE explicit-country-code wrong:", sum(len(v) for v in explicit.values()),
          "| own-country national wrong:", sum(len(v) for v in national.values()),
          "| key collisions:", len(collide), "| split keys:", len(twokeys))
    assert not explicit and not national and not collide and not twokeys
