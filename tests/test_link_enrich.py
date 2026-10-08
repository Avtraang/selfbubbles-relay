"""Unit tests for link_enrich: fakes and tmp_path only -- no network, no database."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

import link_enrich as le
from link_enrich import Enricher, Fetched, FetchError, GuardedFetcher, find_apple_news_url

APPLE = "https://apple.news/AbCdEfGhIjKlMnOpQrStUv"
PUB = "https://www.example-news.com/2026/story?a=1&b=2"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def apple_page(target: str | None, *, quote: str = '"') -> bytes:
    """A trimmed copy of the page apple.news serves a non-Apple browser."""
    call = (f'redirectToUrlAfterTimeout({quote}{target}{quote}, 0);' if target is not None else "")
    call2 = (f'redirectToUrl({quote}{target}{quote});' if target is not None else "")
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8" />
<script>
   window.onload = function() {{
       var curUserAgent = navigator.userAgent.toLowerCase();
       if (shouldRedirectToApp(curUserAgent)) {{
           if (shouldRedirectToCanonicalUrl(curUserAgent)) {{
               {call}
           }}
       }} else if (!isSocialMediaBot(curUserAgent)) {{if (shouldRedirectToCanonicalUrl(curUserAgent)) {{
               {call2}
           }}
       }}function redirectToUrl(url) {{
           top.location.replace(url);
       }}
       function redirectToUrlAfterTimeout(url, timeout) {{
           setTimeout(function() {{ redirectToUrl(url) }}, timeout);
       }}
   }};
</script>
<title>A headline</title><meta name="Author" content="Example News" />
<meta property="og:title" content="A headline" /></head><body></body></html>""".encode()


def publisher_page(**meta: str) -> bytes:
    tags = "".join(f'<meta property="{k}" content="{v}">' for k, v in meta.items())
    return f"<!doctype html><html><head><title>t</title>{tags}</head><body>x</body></html>".encode()


class FakeFetch:
    """Maps URL -> Fetched | Exception | callable; records every call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, int, str]] = []
        self.agents: list[tuple[str, str | None]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, url, *, max_bytes, accept="html", user_agent=None):
        self.calls.append((url, max_bytes, accept))
        self.agents.append((url, user_agent))
        if self.gate is not None:
            await self.gate.wait()
        got = self.routes.get(url, FetchError("http 404"))
        if isinstance(got, Exception):
            raise got
        if isinstance(got, tuple) and not isinstance(got, Fetched):
            ctype, body = got
            return Fetched(url, ctype, body[:max_bytes], len(body) > max_bytes)
        return got

    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def standard_routes(image: bytes = PNG, **meta: str) -> dict:
    meta = {"og:title": "Big  story\n here", "og:description": "Summary text", "og:image": "https://cdn.example-news.com/a.png",
            "og:site_name": "Example News", **meta}
    return {
        APPLE: ("text/html", apple_page(PUB.replace("/", "\\/").replace("&", "\\u0026"))),
        PUB: ("text/html; charset=utf-8", publisher_page(**meta)),
        "https://cdn.example-news.com/a.png": ("image/png", image),
    }


def make(tmp_path: Path, routes: dict | None = None, **kw) -> tuple[Enricher, FakeFetch]:
    fake = FakeFetch(standard_routes() if routes is None else routes)
    return Enricher(tmp_path / "cache", fetch=fake, **kw), fake


def key_of(image_url: str) -> str:
    return hashlib.sha256(image_url.encode()).hexdigest()[:32]


# ------------------------------------------------------- find_apple_news_url

@pytest.mark.parametrize("text, expected", [
    (APPLE, APPLE),
    (f"look at this {APPLE} wow", APPLE),
    (f"Did you see {APPLE}.", APPLE),
    (f"({APPLE})", APPLE),
    (f"{APPLE}!?", APPLE),
    (f"{APPLE},", APPLE),
    (f"first https://example.com/x then {APPLE}", APPLE),
    ("http://apple.news/Axyz", "http://apple.news/Axyz"),
    ("HTTPS://APPLE.NEWS/Axyz", "HTTPS://APPLE.NEWS/Axyz"),
    (f"{APPLE} and https://apple.news/Second", APPLE),
    ("https://apple.news.evil.example/Axyz", None),
    ("https://notapple.news/Axyz", None),
    ("https://evil.example/apple.news/Axyz", None),
    ("https://evil.example/?u=https://apple.news/Axyz", None),
    ("https://apple.news@evil.example/Axyz", None),
    ("https://user@apple.news/Axyz", None),
    ("https://www.apple.news/Axyz", None),
    ("ftp://apple.news/Axyz", None),
    ("apple.news/Axyz", None),
    ("no links here", None),
    ("", None),
    (None, None),
])
def test_find_apple_news_url(text, expected):
    assert find_apple_news_url(text) == expected


def test_find_apple_news_url_non_string():
    assert find_apple_news_url(123) is None  # type: ignore[arg-type]


# ------------------------------------------------------- redirect extraction

@pytest.mark.parametrize("literal, quote", [
    (PUB, '"'),
    (PUB.replace("/", "\\/"), '"'),
    (PUB.replace("/", "\\/").replace("&", "\\u0026"), '"'),
    (PUB.replace("&", "&amp;"), '"'),
    (PUB.replace("&", "\\x26"), "'"),
    (PUB, "'"),
])
def test_extract_redirect_url_escapes(literal, quote):
    assert le.extract_redirect_url(apple_page(literal, quote=quote).decode()) == PUB


def test_extract_redirect_url_ignores_function_definition_and_missing():
    assert le.extract_redirect_url(apple_page(None).decode()) is None
    assert le.extract_redirect_url("") is None


@pytest.mark.parametrize("target", [
    "https://apple.news/Aother", "https://www.apple.com/news", "https://news.apple.com/x",
    "https://apps.apple.com/app/id1", "https://www.icloud.com/x", "javascript:alert(1)",
    "/relative/path", "ftp://example.com/x", "https://user@example.com/x", "",
])
def test_extract_redirect_url_rejects_apple_and_non_http(target):
    assert le.extract_redirect_url(apple_page(target).decode()) is None


def test_extract_redirect_falls_back_to_plain_redirect_call():
    page = '<script>if (x) { redirectToUrl("https:\\/\\/pub.example\\/a"); }</script>'
    assert le.extract_redirect_url(page) == "https://pub.example/a"


def test_resolve_without_redirect_is_negative_for_seven_days(tmp_path):
    clock = Clock()
    e, fake = make(tmp_path, {APPLE: ("text/html", apple_page(None))}, now=clock)
    assert asyncio.run(e.resolve(APPLE)) is None
    assert fake.urls() == [APPLE]
    assert e.lookup(APPLE) is None and e.reason(APPLE) == "no publisher URL"
    assert not e.needs(APPLE)
    clock.t += 7 * 86400 - 1
    assert not e.needs(APPLE)
    clock.t += 1
    assert e.needs(APPLE)


# ------------------------------------------------------------- Open Graph

def test_og_basic_and_whitespace():
    og = le.parse_open_graph(publisher_page(**{
        "og:title": "  A   title\n\twith space ", "og:description": "desc", "og:image": "https://i.example/p.jpg",
        "og:site_name": "The Site"}).decode(), "https://www.pub.example/a")
    assert og == {"title": "A title with space", "summary": "desc", "site": "The Site",
                  "images": ["https://i.example/p.jpg"]}


def test_og_attribute_order_and_name_vs_property_and_case():
    page = """<html><head>
      <meta content="Content first" property="og:title">
      <META NAME="og:description" CONTENT="via name">
      <meta name='twitter:image' content='https://i.example/t.jpg'/>
      <meta property="OG:SITE_NAME" content="Site">
    </head></html>"""
    og = le.parse_open_graph(page, "https://pub.example/a")
    assert og["title"] == "Content first" and og["summary"] == "via name"
    assert og["site"] == "Site" and og["images"] == ["https://i.example/t.jpg"]


@pytest.mark.parametrize("tags, expected", [
    ({"og:image:secure_url": "https://i.example/s.jpg"}, ["https://i.example/s.jpg"]),
    ({"twitter:image": "https://i.example/t.jpg"}, ["https://i.example/t.jpg"]),
    ({"twitter:image:src": "https://i.example/ts.jpg"}, ["https://i.example/ts.jpg"]),
    ({"twitter:image": "https://i.example/t.jpg", "og:image": "https://i.example/o.jpg"},
     ["https://i.example/o.jpg", "https://i.example/t.jpg"]),
    ({"og:image": "https://i.example/o.jpg", "twitter:image": "https://i.example/o.jpg"}, ["https://i.example/o.jpg"]),
    ({"og:image": "/img/rel.jpg"}, ["https://www.pub.example/img/rel.jpg"]),
    ({"og:image": "rel.jpg"}, ["https://www.pub.example/news/rel.jpg"]),
    ({"og:image": "//cdn.example/x.jpg"}, ["https://cdn.example/x.jpg"]),
    ({"og:image": "data:image/png;base64,AAAA"}, []),
    ({"og:image": "javascript:alert(1)"}, []),
    ({}, []),
])
def test_og_image_fallbacks_and_relative(tags, expected):
    og = le.parse_open_graph(publisher_page(**tags).decode(), "https://www.pub.example/news/story")
    assert og["images"] == expected


def test_og_entities_unescaped():
    page = ('<meta property="og:title" content="Tom &amp; Jerry&#39;s &quot;day&quot; &#x2014; ok">'
            '<meta property="og:image" content="https://i.example/p.jpg?w=1&amp;h=2">')
    og = le.parse_open_graph(page, "https://pub.example/")
    assert og["title"] == "Tom & Jerry's \"day\" — ok"
    assert og["images"] == ["https://i.example/p.jpg?w=1&h=2"]


def test_og_missing_tags_and_site_fallback():
    og = le.parse_open_graph("<html><head><title>only a title</title></head></html>", "https://www.pub.example/a")
    assert og == {"title": None, "summary": None, "site": "pub.example", "images": []}
    assert le.parse_open_graph("", "https://news.pub.example/a")["site"] == "news.pub.example"
    assert le.parse_open_graph('<meta property="og:title" content="">', "https://p.example/")["title"] is None


def test_og_first_tag_wins_and_summary_capped():
    page = ('<meta property="og:title" content="one"><meta property="og:title" content="two">'
            f'<meta property="og:description" content="{"word " * 200}">')
    og = le.parse_open_graph(page, "https://p.example/")
    assert og["title"] == "one"
    assert len(og["summary"]) == 300 and og["summary"].endswith("…")


def test_og_garbage_never_raises():
    for page in ("<meta property=og:title content=unquoted>", "<meta", "<<<>>>", "<meta property='og:title' content='x"):
        le.parse_open_graph(page, "https://p.example/")
    assert le.parse_open_graph("<meta property=og:title content=unquoted>", "https://p.example/")["title"] == "unquoted"


def test_huge_publisher_page_truncated_at_cap(tmp_path):
    head = publisher_page(**{"og:title": "Early"}).replace(b"</head><body>x</body></html>", b"")
    late = b'<meta property="og:description" content="too late">'
    body = head + b"<!--" + b"x" * (le.PUBLISHER_PAGE_MAX + 1000) + b"-->" + late
    routes = {APPLE: ("text/html", apple_page(PUB)), PUB: ("text/html", body)}
    e, fake = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE))
    assert fake.calls[1] == (PUB, 1024 * 1024, "html") and fake.calls[0] == (APPLE, 512 * 1024, "html")
    assert entry == {"resolved_url": PUB, "title": "Early", "summary": None, "site": "example-news.com", "image": None}


def test_publisher_must_be_html(tmp_path):
    clock = Clock()
    routes = {APPLE: ("text/html", apple_page(PUB)), PUB: ("application/pdf", b"%PDF-1.7")}
    e, _ = make(tmp_path, routes, now=clock)
    assert asyncio.run(e.resolve(APPLE)) is None
    assert e.reason(APPLE) == "publisher: not html"
    clock.t += 3600
    assert e.needs(APPLE)


# ------------------------------------------------------------ image sniffing

@pytest.mark.parametrize("data, ext", [
    (PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (b"GIF87a" + b"\0" * 8, "gif"), (WEBP, "webp"),
    (SVG, None), (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None), (b"<!doctype html><html>", None),
    (b"", None), (b"RIFF\x00\x00\x00\x00WAVEfmt ", None), (b"RIFF", None), (b"BM" + b"\0" * 20, None),
    (b"\x00\x00\x01\x00", None), (b"%PDF-1.7", None),
])
def test_sniff_image(data, ext):
    assert le.sniff_image(data) == ext


@pytest.mark.parametrize("image, ext", [(PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp")])
def test_resolve_stores_accepted_image(tmp_path, image, ext):
    e, fake = make(tmp_path, standard_routes(image=image))
    entry = asyncio.run(e.resolve(APPLE))
    key = key_of("https://cdn.example-news.com/a.png")
    assert entry == {"resolved_url": PUB, "title": "Big story here", "summary": "Summary text",
                     "site": "Example News", "image": f"/link_preview_image/{key}"}
    path = e.image_path(key)
    assert path == tmp_path / "cache" / f"{key}.{ext}" and path.read_bytes() == image
    assert fake.calls[2] == ("https://cdn.example-news.com/a.png", 5 * 1024 * 1024, "image")
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == sorted([f"{key}.{ext}", "index.json"])


@pytest.mark.parametrize("image", [SVG, b"<html>not an image</html>", b"", PNG + b"\0" * (5 * 1024 * 1024)])
def test_resolve_rejects_bad_image_but_keeps_entry(tmp_path, image):
    e, _ = make(tmp_path, standard_routes(image=image))
    entry = asyncio.run(e.resolve(APPLE))
    assert entry is not None and entry["image"] is None and entry["title"] == "Big story here"
    assert [p.name for p in (tmp_path / "cache").iterdir()] == ["index.json"]


def test_image_at_exact_cap_is_accepted_and_one_over_is_not(tmp_path):
    exact = PNG + b"\0" * (le.IMAGE_MAX - len(PNG))
    e, _ = make(tmp_path / "a", standard_routes(image=exact))
    assert asyncio.run(e.resolve(APPLE))["image"]
    e2, _ = make(tmp_path / "b", standard_routes(image=exact + b"\0"))
    assert asyncio.run(e2.resolve(APPLE))["image"] is None


def test_image_fetch_error_and_fallback_candidate(tmp_path):
    routes = standard_routes(**{"twitter:image": "/t.jpg"})
    routes["https://cdn.example-news.com/a.png"] = FetchError("http 403")
    routes["https://www.example-news.com/t.jpg"] = ("image/jpeg", JPEG)
    e, _ = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE))
    assert entry["image"] == "/link_preview_image/" + key_of("https://www.example-news.com/t.jpg")


def test_relative_image_resolved_against_final_page_url(tmp_path):
    final = "https://m.example-news.com/amp/story"
    routes = {APPLE: ("text/html", apple_page(PUB)),
              PUB: Fetched(final, "text/html", publisher_page(**{"og:image": "pic.webp"})),
              "https://m.example-news.com/amp/pic.webp": ("image/webp", WEBP)}
    e, fake = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE))
    assert fake.urls()[-1] == "https://m.example-news.com/amp/pic.webp"
    assert entry["resolved_url"] == PUB and entry["site"] == "m.example-news.com" and entry["image"]


# ---------------------------------------------------------------- SSRF guard

PUBLIC = {"pub.example": ["93.184.216.34"], "apple.news": ["17.253.144.10"], "cdn.example": ["1.1.1.1", "2606:4700::1111"]}


def resolver(extra: dict | None = None):
    table = {**PUBLIC, **(extra or {})}
    seen: list[str] = []

    def resolve(host: str) -> list[str]:
        seen.append(host)
        if host not in table:
            raise OSError("nodename nor servname provided")
        return table[host]

    resolve.seen = seen  # type: ignore[attr-defined]
    return resolve


class RawStream(httpx.AsyncByteStream):
    """Wire bytes exactly as given (httpx never decodes or pre-reads them)."""

    def __init__(self, data: bytes, chunk: int = 65536):
        self.data, self.chunk, self.sent = data, chunk, 0

    async def __aiter__(self):
        for i in range(0, len(self.data), self.chunk):
            self.sent += len(self.data[i:i + self.chunk])
            yield self.data[i:i + self.chunk]


def mock_transport(handler) -> httpx.MockTransport:
    """MockTransport whose bodies arrive as a stream, as they do off a socket
    (a ``content=`` response is pre-read, which the raw reader cannot consume)."""
    def restream(request: httpx.Request) -> httpx.Response:
        resp = handler(request)
        if resp.is_stream_consumed or resp.is_closed:
            return httpx.Response(resp.status_code, headers=resp.headers, stream=RawStream(resp.content))
        return resp
    return httpx.MockTransport(restream)


def guarded(handler, extra: dict | None = None, **kw) -> tuple[GuardedFetcher, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return GuardedFetcher(resolve_host=resolver(extra), transport=mock_transport(record), **kw), requests


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>hello</html>")


BLOCKED_URLS = [
    "http://127.0.0.1/", "http://127.0.0.1:80/x", "https://127.8.9.1/", "http://10.0.0.5/", "http://10.255.255.255/",
    "http://172.16.0.1/", "http://192.168.0.10/", "http://192.168.0.20:443/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://0.0.0.0/", "http://224.0.0.1/", "http://240.0.0.1/", "http://255.255.255.255/",
    "http://192.0.2.1/", "http://198.18.0.1/",
    "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fd12:3456:789a::1]/", "http://[fe80::1]/",
    "http://[ff02::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.0.0.1]/", "http://[::ffff:192.168.1.1]/",
    "http://[::ffff:169.254.169.254]/", "http://[2002:7f00:1::]/", "http://[2002:c0a8:101::]/",
    "http://[64:ff9b::7f00:1]/", "http://[64:ff9b::c0a8:101]/", "http://[2001:db8::1]/", "http://[fe80::1%25en0]/",
    "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/",
    "http://private.example/", "http://mixed.example/", "http://mapped.example/", "http://meta.example/",
    "http://empty.example/", "http://unknown.example/", "http://localhost/", "http://garbage.example/",
    "http://user@pub.example/", "http://user:pw@pub.example/", "https://pub.example@127.0.0.1/",
    "http://pub.example:8080/", "https://pub.example:8443/", "http://pub.example:22/", "http://pub.example:0/",
    "http://pub.example:99999/", "http://pub.example:abc/",
    "ftp://pub.example/x", "file:///etc/passwd", "javascript:alert(1)", "gopher://pub.example/", "data:text/html,hi",
    "//pub.example/x", "pub.example/x", "", "http:///x", "http://pub.example\\@127.0.0.1/", "http://pub.example/\r\nX: y",
]
PRIVATE_HOSTS = {"private.example": ["10.1.2.3"], "mixed.example": ["93.184.216.34", "192.168.1.10"],
                 "mapped.example": ["::ffff:127.0.0.1"], "meta.example": ["169.254.169.254"], "empty.example": [],
                 "localhost": ["127.0.0.1", "::1"], "garbage.example": ["not-an-ip"]}


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_ssrf_guard_refuses_without_a_request(url):
    fetch, requests = guarded(ok, PRIVATE_HOSTS)
    with pytest.raises(FetchError):
        asyncio.run(fetch(url, max_bytes=1000))
    assert requests == []


@pytest.mark.parametrize("address, public", [
    ("93.184.216.34", True), ("1.1.1.1", True), ("2606:4700::1111", True), ("::ffff:1.1.1.1", True),
    ("127.0.0.1", False), ("10.0.0.1", False), ("192.168.0.1", False), ("169.254.169.254", False),
    ("::1", False), ("fc00::1", False), ("fdff::1", False), ("::ffff:127.0.0.1", False), ("0.0.0.0", False),
    ("::", False), ("100.100.100.100", False), ("not-an-ip", False), ("", False), ("fe80::1%en0", False),
])
def test_is_public_address(address, public):
    assert le.is_public_address(address) is public


def test_guard_literal_ip_does_not_call_resolver():
    r = resolver()
    assert le.check_url("https://1.1.1.1/x", r).addresses == ("1.1.1.1",) and r.seen == []
    with pytest.raises(FetchError) as exc:
        le.check_url("http://127.0.0.1/", r)
    assert exc.value.blocked and r.seen == []


def test_guarded_fetch_ok_pins_validated_address_and_sends_no_credentials():
    fetch, requests = guarded(ok, user_agent="UA/1")
    got = asyncio.run(fetch("https://cdn.example/a/b?x=1&y=2", max_bytes=1000))
    assert got == Fetched("https://cdn.example/a/b?x=1&y=2", "text/html", b"<html>hello</html>", False)
    (req,) = requests
    assert req.url.host == "1.1.1.1" and req.url.raw_path == b"/a/b?x=1&y=2"      # connection pinned, IPv4 first
    assert req.headers["host"] == "cdn.example" and req.extensions["sni_hostname"] == "cdn.example"
    assert req.headers["user-agent"] == "UA/1"
    assert "cookie" not in req.headers and "authorization" not in req.headers


def test_guarded_fetch_truncates_at_cap():
    def big(_r):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 5000)
    fetch, _ = guarded(big)
    got = asyncio.run(fetch("https://pub.example/", max_bytes=1000))
    assert got.body == b"a" * 1000 and got.truncated
    assert not asyncio.run(fetch("https://pub.example/", max_bytes=5000)).truncated


def test_redirect_hop_to_private_address_is_refused_before_the_request():
    for location in ("http://192.168.0.10/api", "http://private.example/", "http://[::1]/", "http://169.254.169.254/",
                     "file:///etc/passwd", "http://pub.example:8080/", "//127.0.0.1/x", "http://u:p@pub.example/"):
        fetch, requests = guarded(lambda r, loc=location: httpx.Response(302, headers={"location": loc}), PRIVATE_HOSTS)
        with pytest.raises(FetchError) as exc:
            asyncio.run(fetch("https://pub.example/start", max_bytes=1000))
        assert exc.value.blocked, location
        assert [r.headers["host"] for r in requests] == ["pub.example"], location


def test_redirects_followed_manually_relative_and_cookies_not_carried():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/next", "set-cookie": "sid=secret; Path=/"})
        if request.url.path == "/next":
            return httpx.Response(302, headers={"location": "https://cdn.example/final"})
        return ok(request)
    fetch, requests = guarded(handler)
    got = asyncio.run(fetch("https://pub.example/start", max_bytes=1000))
    assert got.final_url == "https://cdn.example/final"
    assert [(r.headers["host"], r.url.path) for r in requests] == [
        ("pub.example", "/start"), ("pub.example", "/next"), ("cdn.example", "/final")]
    assert all("cookie" not in r.headers for r in requests)


def test_redirect_limit():
    def loop(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})
    fetch, requests = guarded(loop)
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://pub.example/0", max_bytes=1000))
    assert exc.value.reason == "too many redirects" and len(requests) == 6     # the first request + 5 redirects

    def five(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return ok(request) if n == 5 else httpx.Response(307, headers={"location": f"/{n + 1}"})
    fetch, requests = guarded(five)
    assert asyncio.run(fetch("https://pub.example/0", max_bytes=1000)).final_url == "https://pub.example/5"


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
def test_http_errors_and_redirect_without_location(status):
    fetch, _ = guarded(lambda r: httpx.Response(status))
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://pub.example/", max_bytes=10))
    assert exc.value.reason == f"http {status}" and not exc.value.blocked
    fetch, _ = guarded(lambda r: httpx.Response(302))
    with pytest.raises(FetchError):
        asyncio.run(fetch("https://pub.example/", max_bytes=10))


def test_network_errors_and_timeout_become_fetch_errors():
    def boom(request):
        raise httpx.ConnectError("refused")
    fetch, requests = guarded(boom)
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://cdn.example/", max_bytes=10))
    assert exc.value.reason == "network error"
    assert [r.url.host for r in requests] == ["1.1.1.1", "2606:4700::1111"]      # each validated address tried once

    def slow(request):
        raise httpx.ReadTimeout("slow")
    fetch, _ = guarded(slow)
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://pub.example/", max_bytes=10))
    assert exc.value.reason == "timeout"

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200)
    fetch = GuardedFetcher(resolve_host=resolver(), transport=httpx.MockTransport(hang), timeout=0.05)
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://pub.example/", max_bytes=10))
    assert exc.value.reason == "timeout"


def test_default_enricher_fetch_is_guarded_end_to_end(tmp_path):
    """The Enricher's own default fetch: a publisher that redirects to the LAN is refused."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        if request.headers["host"] == "apple.news":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=apple_page("https://pub.example/a"))
        return httpx.Response(302, headers={"location": "http://192.168.0.10:8080/"})

    e = Enricher(tmp_path, resolve_host=resolver())
    assert isinstance(e._fetch, GuardedFetcher)
    e._fetch.transport = mock_transport(handler)
    assert asyncio.run(e.resolve("https://apple.news/Axyz")) is None
    assert hosts == ["apple.news", "pub.example"]
    assert e.reason("https://apple.news/Axyz") == "publisher: blocked (port)"


def test_publisher_url_on_private_address_never_fetched(tmp_path):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        return httpx.Response(200, headers={"content-type": "text/html"}, content=apple_page("http://192.168.0.10/admin"))

    e = Enricher(tmp_path, resolve_host=resolver())
    e._fetch.transport = mock_transport(handler)
    assert asyncio.run(e.resolve("https://apple.news/Axyz")) is None
    assert hosts == ["apple.news"] and e.reason("https://apple.news/Axyz") == "publisher: blocked (address)"


# ------------------------------------------------------------------ resolve

def test_resolve_only_accepts_apple_news(tmp_path):
    e, fake = make(tmp_path)
    for url in ("https://example.com/x", "https://apple.news.evil.example/A", "ftp://apple.news/A", "", None, 5):
        assert asyncio.run(e.resolve(url)) is None  # type: ignore[arg-type]
    assert fake.calls == [] and not (tmp_path / "cache").exists()


def test_resolve_never_raises(tmp_path):
    class Boom:
        async def __call__(self, url, **kw):
            raise RuntimeError("secret detail https://apple.news/Aprivate")
    clock = Clock()
    e = Enricher(tmp_path, fetch=Boom(), now=clock)
    assert asyncio.run(e.resolve(APPLE)) is None
    assert e.reason(APPLE) == "apple.news: network error (RuntimeError)"
    assert not e.needs(APPLE)


@pytest.mark.parametrize("error, reason", [
    (FetchError("http 503"), "apple.news: http 503"),
    (FetchError("timeout"), "apple.news: timeout"),
    (TimeoutError(), "apple.news: timeout"),
    (FetchError("network error"), "apple.news: network error"),
])
def test_apple_page_errors_retry_after_one_hour(tmp_path, error, reason):
    clock = Clock()
    e, fake = make(tmp_path, {APPLE: error}, now=clock)
    assert asyncio.run(e.resolve(APPLE)) is None
    assert e.reason(APPLE) == reason and not e.needs(APPLE)
    assert asyncio.run(e.resolve(APPLE)) is None and len(fake.calls) == 1      # not due: no network
    clock.t += 3599
    assert not e.needs(APPLE)
    clock.t += 1
    assert e.needs(APPLE)
    fake.routes.update(standard_routes())
    assert asyncio.run(e.resolve(APPLE))["title"] == "Big story here"
    assert e.reason(APPLE) is None and not e.needs(APPLE)


def test_publisher_error_is_negative_one_hour(tmp_path):
    clock = Clock()
    e, _ = make(tmp_path, {APPLE: ("text/html", apple_page(PUB)), PUB: FetchError("http 403")}, now=clock)
    assert asyncio.run(e.resolve(APPLE)) is None
    assert e.reason(APPLE) == "publisher: http 403"
    clock.t += 3600
    assert e.needs(APPLE)


def test_reasons_never_contain_urls(tmp_path):
    e, _ = make(tmp_path, {APPLE: ("text/html", apple_page(PUB)), PUB: FetchError("http 403")})
    asyncio.run(e.resolve(APPLE))
    assert "/" not in e.reason(APPLE) and "example" not in e.reason(APPLE)


def test_positive_entry_without_any_og_tags(tmp_path):
    e, _ = make(tmp_path, {APPLE: ("text/html", apple_page(PUB)), PUB: ("text/html", b"<html></html>")})
    assert asyncio.run(e.resolve(APPLE)) == {
        "resolved_url": PUB, "title": None, "summary": None, "site": "example-news.com", "image": None}


def test_resolve_cached_positive_does_no_network(tmp_path):
    e, fake = make(tmp_path)
    first = asyncio.run(e.resolve(APPLE))
    n = len(fake.calls)
    assert asyncio.run(e.resolve(APPLE)) == first and len(fake.calls) == n


def test_inflight_deduplication(tmp_path):
    e, fake = make(tmp_path)

    async def scenario():
        fake.gate = asyncio.Event()
        a = asyncio.create_task(e.resolve(APPLE))
        b = asyncio.create_task(e.resolve(APPLE))
        await asyncio.sleep(0.01)
        assert len(fake.calls) == 1
        fake.gate.set()
        return await asyncio.gather(a, b)

    a, b = asyncio.run(scenario())
    assert a == b and a["title"] == "Big story here"
    assert fake.urls() == [APPLE, PUB, "https://cdn.example-news.com/a.png"]
    assert e._inflight == {}


def test_cancelled_waiter_does_not_cancel_shared_resolve(tmp_path):
    e, fake = make(tmp_path)

    async def scenario():
        fake.gate = asyncio.Event()
        a = asyncio.create_task(e.resolve(APPLE))
        b = asyncio.create_task(e.resolve(APPLE))
        await asyncio.sleep(0.01)
        a.cancel()
        fake.gate.set()
        return await b

    assert asyncio.run(scenario())["title"] == "Big story here"
    assert len(fake.calls) == 3


def test_different_urls_resolve_independently(tmp_path):
    other = "https://apple.news/Aother"
    routes = standard_routes()
    routes[other] = ("text/html", apple_page(None))
    e, fake = make(tmp_path, routes)

    async def scenario():
        return await asyncio.gather(e.resolve(APPLE), e.resolve(other))

    a, b = asyncio.run(scenario())
    assert a["resolved_url"] == PUB and b is None and e.reason(other) == "no publisher URL"


# -------------------------------------------------------------------- cache

def test_cache_persists_across_instances(tmp_path):
    clock = Clock()
    other = "https://apple.news/Aother"
    routes = standard_routes()
    routes[other] = ("text/html", apple_page(None))
    e, _ = make(tmp_path, routes, now=clock)
    first = asyncio.run(e.resolve(APPLE))
    asyncio.run(e.resolve(other))

    e2, fake2 = make(tmp_path, {}, now=clock)
    assert e2.lookup(APPLE) == first and not e2.needs(APPLE)
    assert e2.lookup(other) is None and not e2.needs(other) and e2.reason(other) == "no publisher URL"
    assert e2.image_path(first["image"].rsplit("/", 1)[1]).is_file()
    assert asyncio.run(e2.resolve(APPLE)) == first and fake2.calls == []
    index = json.loads((tmp_path / "cache" / "index.json").read_text())
    assert set(index) == {APPLE, other}
    assert not [p for p in (tmp_path / "cache").iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("content", [b"", b"{not json", b"[1, 2, 3]", b'"a string"', b"\xff\xfe\x00", b'{"https://apple.news/A": 5}'])
def test_corrupt_index_tolerated(tmp_path, content):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "index.json").write_bytes(content)
    e, _ = make(tmp_path)
    assert e.lookup(APPLE) is None and e.needs(APPLE) and e.needs("https://apple.news/A")
    assert e.apply({"text": APPLE, "link": None}) == APPLE
    assert asyncio.run(e.resolve(APPLE))["title"] == "Big story here"
    assert APPLE in json.loads((cache / "index.json").read_text())


def test_missing_cache_dir_and_unknown_url(tmp_path):
    e, _ = make(tmp_path)
    assert e.lookup(APPLE) is None and e.needs(APPLE) and e.reason(APPLE) is None
    assert not (tmp_path / "cache").exists()        # reading creates nothing


def test_lookup_drops_image_whose_file_vanished(tmp_path):
    e, _ = make(tmp_path)
    entry = asyncio.run(e.resolve(APPLE))
    e.image_path(entry["image"].rsplit("/", 1)[1]).unlink()
    assert e.lookup(APPLE) == {**entry, "image": None}


def test_index_is_thread_safe(tmp_path):
    import threading
    e, _ = make(tmp_path)
    asyncio.run(e.resolve(APPLE))
    errors: list[BaseException] = []

    def reader():
        try:
            for _ in range(200):
                msg = {"text": APPLE, "link": None}
                assert e.apply(msg) is None and msg["link"]["title"] == "Big story here"
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    def writer(i):
        try:
            for j in range(40):
                e._store(f"https://apple.news/A{i}x{j}", e._negative("t", 10))
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)] + [threading.Thread(target=writer, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(json.loads((tmp_path / "cache" / "index.json").read_text())) == 121


# --------------------------------------------------------------- image_path

@pytest.mark.parametrize("key", [
    "", "abc", "0123456789abcde", "g" * 32, "A" * 32, "0" * 65, "../index", "../../etc/passwd", "..%2f..%2fetc%2fpasswd",
    "0123456789abcdef/../../x", "/etc/passwd", "0123456789abcdef.png", "0123456789abcdef\n", "0123456789abcdef\x00",
    "index.json", "0123456789abcdef ", None, 5, b"0123456789abcdef",
])
def test_image_path_refuses_bad_keys_without_touching_the_filesystem(tmp_path, monkeypatch, key):
    e, _ = make(tmp_path)

    def forbidden(*a, **k):
        raise AssertionError("filesystem touched")

    for name in ("is_file", "exists", "stat", "open", "resolve", "glob", "iterdir"):
        monkeypatch.setattr(Path, name, forbidden)
    assert e.image_path(key) is None


def test_image_path_valid_key(tmp_path):
    e, _ = make(tmp_path)
    assert e.image_path("0123456789abcdef0123456789abcdef") is None       # well-formed but unknown
    entry = asyncio.run(e.resolve(APPLE))
    key = entry["image"].rsplit("/", 1)[1]
    assert e.image_path(key).parent == tmp_path / "cache"


# -------------------------------------------------------------------- apply

def relay_msg(text: str | None, link: dict | None) -> dict:
    return {"rowid": 7, "guid": "G-7", "text": text, "date": 1.5, "date_read": None, "date_edited": None,
            "is_from_me": False, "sender": "Someone", "sender_handle": "h", "chat_guid": "c", "chat_name": "n",
            "is_group": False, "has_attachments": False, "assoc_guid": None, "assoc_type": 0, "attachments": [],
            "link": link, "reply_to_guid": None, "reply_to": None, "service": "iMessage"}


def resolved(tmp_path, routes=None, **kw) -> Enricher:
    e, _ = make(tmp_path, routes, **kw)
    asyncio.run(e.resolve(APPLE))
    return e


UNTOUCHED = [
    relay_msg("hello", None),
    relay_msg(None, None),
    relay_msg("", None),
    relay_msg("see https://example.com/a", None),
    relay_msg("https://example.com/a", {"url": "https://example.com/a", "title": "T", "summary": None, "site": None, "image": None}),
    relay_msg("https://apple.news.evil.example/A", None),
    relay_msg("https://notapple.news/A", {"url": "https://notapple.news/A", "title": None, "summary": None, "site": None, "image": None}),
    # a non-Apple link card wins even when the text also mentions apple.news
    relay_msg(f"https://example.com/a {APPLE}", {"url": "https://example.com/a", "title": "T", "summary": None, "site": None, "image": "/link_image/7"}),
    relay_msg(APPLE, {"url": None, "title": "T", "summary": None, "site": None, "image": None}),
    relay_msg(APPLE, {"title": "no url key"}),
    relay_msg(APPLE, "not-a-dict"),  # type: ignore[arg-type]
    {"type": "something else"},
    {},
]


@pytest.mark.parametrize("msg", UNTOUCHED)
def test_apply_leaves_other_messages_byte_identical(tmp_path, msg):
    e = resolved(tmp_path)
    before = json.dumps(msg)
    assert e.apply(msg) is None
    assert json.dumps(msg) == before


def test_apply_non_dict_message(tmp_path):
    e = resolved(tmp_path)
    assert e.apply(None) is None and e.apply("x") is None  # type: ignore[arg-type]


def test_apply_overlays_existing_link_keeping_truthy_fields(tmp_path):
    e = resolved(tmp_path)
    key = key_of("https://cdn.example-news.com/a.png")
    msg = relay_msg(APPLE, {"url": APPLE, "title": "Apple's title", "summary": None, "site": "", "image": None})
    link_obj = msg["link"]
    assert e.apply(msg) is None
    assert msg["link"] is link_obj
    assert msg["link"] == {"url": APPLE, "title": "Apple's title", "summary": "Summary text", "site": "Example News",
                           "image": f"/link_preview_image/{key}", "resolved_url": PUB}
    assert list(msg["link"]) == ["url", "title", "summary", "site", "image", "resolved_url"]
    assert msg["text"] == APPLE and list(msg) == list(relay_msg(None, None))


def test_apply_keeps_existing_image_and_all_truthy_fields(tmp_path):
    e = resolved(tmp_path)
    link = {"url": APPLE, "title": "T", "summary": "S", "site": "Site", "image": "/link_image/7"}
    msg = relay_msg(APPLE, dict(link))
    e.apply(msg)
    assert msg["link"] == {**link, "resolved_url": PUB}


def test_apply_is_idempotent_and_resolved_url_stays_last(tmp_path):
    e = resolved(tmp_path)
    msg = relay_msg(APPLE, {"url": APPLE, "resolved_url": "stale", "title": None, "summary": None, "site": None, "image": None})
    e.apply(msg)
    once = json.dumps(msg)
    assert list(msg["link"])[-1] == "resolved_url" and msg["link"]["resolved_url"] == PUB
    e.apply(msg)
    assert json.dumps(msg) == once


def test_apply_link_url_matched_by_host_not_text(tmp_path):
    e = resolved(tmp_path)
    msg = relay_msg("read this", {"url": APPLE, "title": None, "summary": None, "site": None, "image": None})
    assert e.apply(msg) is None and msg["link"]["title"] == "Big story here" and msg["link"]["url"] == APPLE


def test_apply_synthesises_link_from_text(tmp_path):
    e = resolved(tmp_path)
    key = key_of("https://cdn.example-news.com/a.png")
    msg = relay_msg(f"check {APPLE}.", None)
    assert e.apply(msg) is None
    assert msg["link"] == {"url": APPLE, "title": "Big story here", "summary": "Summary text", "site": "Example News",
                           "image": f"/link_preview_image/{key}", "resolved_url": PUB}
    assert list(msg["link"]) == ["url", "title", "summary", "site", "image", "resolved_url"]
    assert msg["text"] == f"check {APPLE}."


def test_apply_synthesises_with_image_only_or_title_only(tmp_path):
    e = resolved(tmp_path / "a", standard_routes(**{"og:title": ""}))
    msg = relay_msg(APPLE, None)
    e.apply(msg)
    assert msg["link"]["title"] is None and msg["link"]["image"]
    e = resolved(tmp_path / "b", standard_routes(image=SVG))
    msg = relay_msg(APPLE, None)
    e.apply(msg)
    assert msg["link"]["title"] == "Big story here" and msg["link"]["image"] is None


def test_apply_does_not_synthesise_a_card_with_neither_title_nor_image(tmp_path):
    e = resolved(tmp_path, {APPLE: ("text/html", apple_page(PUB)),
                            PUB: ("text/html", publisher_page(**{"og:description": "only a summary"}))})
    assert e.lookup(APPLE)["summary"] == "only a summary"
    msg = relay_msg(APPLE, None)
    before = json.dumps(msg)
    assert e.apply(msg) is None and json.dumps(msg) == before and msg["link"] is None


def test_apply_uncached_returns_url_and_leaves_message_alone(tmp_path):
    e, fake = make(tmp_path)
    for msg in (relay_msg(f"hey {APPLE}", None),
                relay_msg(APPLE, {"url": APPLE, "title": "T", "summary": None, "site": None, "image": None})):
        before = json.dumps(msg)
        assert e.apply(msg) == APPLE
        assert json.dumps(msg) == before
    assert fake.calls == []                              # apply never does network I/O


def test_apply_negative_entry_not_due_then_due(tmp_path):
    clock = Clock()
    e, _ = make(tmp_path, {APPLE: ("text/html", apple_page(None))}, now=clock)
    asyncio.run(e.resolve(APPLE))
    msg = relay_msg(APPLE, {"url": APPLE, "title": "T", "summary": None, "site": None, "image": None})
    before = json.dumps(msg)
    assert e.apply(msg) is None and json.dumps(msg) == before
    clock.t += 7 * 86400
    assert e.apply(msg) == APPLE and json.dumps(msg) == before


def test_apply_disabled_feature_touches_nothing(tmp_path):
    e = resolved(tmp_path)
    off, fake = make(tmp_path, enabled=False)           # same cache directory, already populated
    for msg in (relay_msg(APPLE, None), relay_msg(APPLE, {"url": APPLE, "title": None, "summary": None, "site": None, "image": None})):
        before = json.dumps(msg)
        assert off.apply(msg) is None and json.dumps(msg) == before
    assert asyncio.run(off.resolve(APPLE)) is None and fake.calls == []
    assert e.apply(relay_msg(APPLE, None)) is None


def test_apply_from_worker_thread_while_resolving(tmp_path):
    e, fake = make(tmp_path)

    async def scenario():
        msg = relay_msg(APPLE, None)
        assert await asyncio.to_thread(e.apply, msg) == APPLE
        await e.resolve(APPLE)
        assert await asyncio.to_thread(e.apply, msg) is None
        return msg

    assert asyncio.run(scenario())["link"]["resolved_url"] == PUB


# ===========================================================================
# Regression tests for the review findings
# ===========================================================================

import gzip
import threading
import time as _time
import tracemalloc

APPLE_IMG = "https://c.apple.news/AgEXQUJDRA"


def apple_page_with_image(target: str | None, image: str = APPLE_IMG) -> bytes:
    """What apple.news really serves: the redirect script AND its own Open Graph tags."""
    extra = (f'<meta property="og:description" content="Apple summary" />'
             f'<meta property="og:image" content="{image}" /></head>').encode()
    return apple_page(target).replace(b"</head>", extra)


def raw_response(body: bytes, encoding: str | None, chunk: int = 65536) -> tuple[httpx.Response, RawStream]:
    stream = RawStream(body, chunk)
    headers = {"content-type": "text/html"}
    if encoding:
        headers["content-encoding"] = encoding
    return httpx.Response(200, headers=headers, stream=stream), stream


# ---- finding 1: decompression bomb

def test_fetch_asks_for_identity_encoding():
    fetch, requests = guarded(ok)
    asyncio.run(fetch("https://pub.example/", max_bytes=1000))
    assert requests[0].headers["accept-encoding"] == "identity"


def test_gzip_bomb_is_inflated_only_up_to_the_cap():
    bomb = gzip.compress(b"\0" * (200 * 1024 * 1024), compresslevel=9)       # ~200 KB on the wire
    assert len(bomb) < 300_000
    fetch, _ = guarded(lambda _r: raw_response(bomb, "gzip", chunk=len(bomb))[0])
    tracemalloc.start()
    try:
        got = asyncio.run(fetch("https://pub.example/", max_bytes=1024 * 1024))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got.truncated and got.body == b"\0" * (1024 * 1024)
    assert peak < 16 * 1024 * 1024          # cap + one wire chunk + slack, not 200 MB


@pytest.mark.parametrize("encoding", ["gzip, gzip", "br", "deflate", "zstd", "gzip, identity"])
def test_stacked_or_unknown_content_encoding_is_refused_unread(encoding):
    streams: list[RawStream] = []

    def handler(_r):
        resp, stream = raw_response(gzip.compress(gzip.compress(b"\0" * 1_000_000)), encoding)
        streams.append(stream)
        return resp

    fetch, _ = guarded(handler)
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://pub.example/", max_bytes=1000))
    assert exc.value.reason == "encoded response" and streams[0].sent == 0


def test_honest_gzip_and_identity_bodies_still_work():
    page = publisher_page(**{"og:title": "Zipped"})
    for body, encoding in ((gzip.compress(page), "gzip"), (gzip.compress(page), "x-gzip"), (page, "identity"), (page, None)):
        fetch, _ = guarded(lambda _r, b=body, e=encoding: raw_response(b, e, chunk=7)[0])
        got = asyncio.run(fetch("https://pub.example/", max_bytes=100_000))
        assert got.body == page and not got.truncated
    fetch, _ = guarded(lambda _r: raw_response(b"not gzip at all", "gzip")[0])
    with pytest.raises(FetchError) as exc:
        asyncio.run(fetch("https://pub.example/", max_bytes=1000))
    assert exc.value.reason == "encoded response"


# ---- finding 2: charset picks the codec / decode on the event loop

def test_hostile_charset_cannot_pick_a_slow_codec(tmp_path):
    body = b"a" * 300_000 + b"-" + b"9" * 700_000
    started = _time.monotonic()
    text = le._decode(body, "text/html; charset=punycode")
    assert _time.monotonic() - started < 2 and text == body.decode()

    routes = standard_routes()
    routes[PUB] = ("text/html; charset=punycode", body)
    e, _ = make(tmp_path, routes)
    started = _time.monotonic()
    entry = asyncio.run(e.resolve(APPLE))
    assert _time.monotonic() - started < 2
    assert entry["resolved_url"] == PUB and entry["title"] is None


@pytest.mark.parametrize("charset", ["punycode", "idna", "rot13", "zlib", "hex", "base64", "uu", "unicode_escape",
                                     "raw_unicode_escape", "utf-7", "undefined", "bz2"])
def test_only_allow_listed_charsets_are_honoured(charset):
    assert le._decode("é+AGE-".encode(), f"text/html; charset={charset}") == "é+AGE-"


def test_allowed_charsets_decode_correctly():
    assert le._decode("café".encode("latin-1"), "text/html; charset=ISO-8859-1") == "café"
    assert le._decode("“q”".encode("cp1252"), "text/html; charset=windows-1252") == "“q”"
    assert le._decode("記事".encode("shift_jis"), 'text/html; charset="Shift_JIS"') == "記事"
    assert le._decode("é".encode(), "text/html") == "é"


def test_pages_are_decoded_and_parsed_off_the_event_loop(tmp_path, monkeypatch):
    threads: list[threading.Thread] = []
    real = le._decode

    def spy(body, content_type):
        threads.append(threading.current_thread())
        return real(body, content_type)

    monkeypatch.setattr(le, "_decode", spy)
    e, _ = make(tmp_path)
    assert asyncio.run(e.resolve(APPLE))["title"] == "Big story here"
    assert len(threads) == 2 and all(t is not threading.main_thread() for t in threads)


# ---- finding 3: cache key normalisation and bounded cache

@pytest.mark.parametrize("variant", [
    APPLE + "#0", APPLE + "#frag/ment", APPLE + "?utm_source=x", APPLE + "?a=1#b",
    APPLE.replace("https://apple.news", "HTTPS://Apple.News"), APPLE.replace("https://", "http://"),
    APPLE.replace("apple.news", "apple.news:443"),
])
def test_normalize_collapses_variants(variant):
    assert le.normalize_apple_news_url(variant) == APPLE


def test_normalize_keeps_path_case_and_refuses_other_hosts():
    assert le.normalize_apple_news_url("https://apple.news/aBc") != le.normalize_apple_news_url("https://apple.news/abc")
    assert le.normalize_apple_news_url("https://apple.news") == "https://apple.news/"
    for bad in ("https://apple.news.evil.example/A", "https://u@apple.news/A", "ftp://apple.news/A", None, 5):
        assert le.normalize_apple_news_url(bad) is None


def test_fragment_variants_share_one_resolve_and_one_entry(tmp_path):
    e, fake = make(tmp_path)

    async def scenario():
        for i in range(20):
            msg = relay_msg(f"{APPLE}#{i}", None)
            need = e.apply(msg)
            if i == 0:
                assert need == APPLE                    # the normalised key is what gets scheduled
                await e.resolve(f"{APPLE}#{i}")
            else:
                assert need is None
                assert msg["link"]["url"] == f"{APPLE}#{i}"        # "url" is never rewritten
                assert msg["link"]["resolved_url"] == PUB

    asyncio.run(scenario())
    assert len(fake.calls) == 3 and fake.urls()[0] == APPLE
    index = json.loads((tmp_path / "cache" / "index.json").read_text())
    assert list(index) == [APPLE]
    images = [p for p in (tmp_path / "cache").iterdir() if p.suffix == ".png"]
    assert len(images) == 1


def test_variants_resolving_concurrently_share_one_fetch(tmp_path):
    e, fake = make(tmp_path)

    async def scenario():
        fake.gate = asyncio.Event()
        tasks = [asyncio.create_task(e.resolve(u)) for u in (APPLE, APPLE + "#x", APPLE + "?y=1",
                                                              APPLE.replace("https", "http"))]
        await asyncio.sleep(0.02)
        fake.gate.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(scenario())
    assert all(r == results[0] and r["resolved_url"] == PUB for r in results)
    assert fake.urls().count(APPLE) == 1 and len(fake.calls) == 3
    assert e.needs(APPLE + "#other") is False and e.lookup("HTTP://APPLE.NEWS" + APPLE[18:]) == results[0]


def _article(i: int, image: bytes = PNG) -> tuple[str, dict]:
    url, pub, img = f"https://apple.news/Article{i:03d}", f"https://pub.example/{i}", f"https://cdn.example/{i}.png"
    return url, {url: ("text/html", apple_page(pub)),
                 pub: ("text/html", publisher_page(**{"og:title": f"T{i}", "og:image": img})),
                 img: ("image/png", image)}


def test_entry_count_is_capped_oldest_first_and_evicted_images_deleted(tmp_path):
    clock = Clock()
    routes: dict = {}
    urls = []
    for i in range(6):
        url, r = _article(i)
        urls.append(url)
        routes.update(r)
    e, _ = make(tmp_path, routes, now=clock, max_entries=3)
    for url in urls:
        clock.t += 10
        assert asyncio.run(e.resolve(url))["image"]
    index = json.loads((tmp_path / "cache" / "index.json").read_text())
    assert list(index) == urls[3:]
    assert [e.lookup(u) is not None for u in urls] == [False] * 3 + [True] * 3
    files = sorted(p.name for p in (tmp_path / "cache").iterdir())
    assert files == sorted(["index.json"] + [f"{key_of(f'https://cdn.example/{i}.png')}.png" for i in (3, 4, 5)])
    assert e.needs(urls[0])                 # evicted, so it may be resolved again


def test_image_bytes_are_capped_oldest_first(tmp_path):
    clock = Clock()
    big = PNG + b"\0" * 100_000
    routes: dict = {}
    urls = []
    for i in range(5):
        url, r = _article(i, big)
        urls.append(url)
        routes.update(r)
    e, _ = make(tmp_path, routes, now=clock, max_cache_bytes=250_000)
    for url in urls:
        clock.t += 10
        asyncio.run(e.resolve(url))
        images = [p for p in (tmp_path / "cache").iterdir() if p.suffix == ".png"]
        assert sum(p.stat().st_size for p in images) <= 250_000
    assert [e.lookup(u) is not None for u in urls] == [False, False, False, True, True]
    assert e.lookup(urls[-1])["image"] is not None


def test_orphaned_image_files_are_removed_but_fresh_downloads_are_not(tmp_path):
    e, _ = make(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    orphan = cache / ("ab" * 16 + ".jpg")
    orphan.write_bytes(JPEG)
    pending = cache / ("cd" * 16 + ".jpg")
    pending.write_bytes(JPEG)
    unrelated = cache / "notes.txt"
    unrelated.write_text("x")
    e._pending.add("cd" * 16)               # another resolve wrote it and has not stored its entry yet
    entry = asyncio.run(e.resolve(APPLE))
    assert not orphan.exists() and pending.exists() and unrelated.exists()
    assert e.image_path(entry["image"].rsplit("/", 1)[1]).is_file()


def test_rotating_image_url_does_not_accumulate_files(tmp_path):
    clock = Clock()
    e, fake = make(tmp_path, now=clock, max_entries=1)
    for i in range(8):
        url, r = _article(i)
        fake.routes.update(r)
        clock.t += 1
        asyncio.run(e.resolve(url))
        assert len([p for p in (tmp_path / "cache").iterdir() if p.suffix == ".png"]) == 1


# ---- finding 4: title and site caps

def test_title_and_site_are_capped(tmp_path):
    routes = standard_routes(**{"og:title": "T" * 900_000, "og:site_name": "S" * 90_000, "og:description": "D" * 5000})
    e, _ = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE))
    assert len(entry["title"]) == le.TITLE_MAX == 200 and entry["title"].endswith("…")
    assert len(entry["site"]) == le.SITE_MAX == 80 and len(entry["summary"]) == 300
    assert (tmp_path / "cache" / "index.json").stat().st_size < 2000
    msg = relay_msg(APPLE, None)
    e.apply(msg)
    assert len(json.dumps(msg)) < 2000


def test_oversized_fields_in_an_old_index_are_clipped_on_the_way_out(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "index.json").write_text(json.dumps({APPLE: {
        "ok": True, "resolved_url": PUB, "title": "T" * 50_000, "summary": "D" * 50_000, "site": "S" * 50_000,
        "image": None, "at": 1}}))
    e, _ = make(tmp_path, {})
    got = e.lookup(APPLE)
    assert (len(got["title"]), len(got["summary"]), len(got["site"])) == (200, 300, 80)


def test_overlong_publisher_or_image_url_is_ignored(tmp_path):
    long_pub = "https://pub.example/" + "a" * 3000
    e, fake = make(tmp_path, {APPLE: ("text/html", apple_page(long_pub))})
    assert asyncio.run(e.resolve(APPLE)) is None and e.reason(APPLE) == "no publisher URL"
    assert fake.urls() == [APPLE]
    og = le.parse_open_graph(publisher_page(**{"og:image": "https://cdn.example/" + "a" * 3000}).decode(), PUB)
    assert og["images"] == []


# ---- finding 7: the apple.news page's own Open Graph tags as fallback

def test_link_without_publisher_gets_image_from_the_apple_page(tmp_path):
    routes = {APPLE: ("text/html", apple_page_with_image(None)), APPLE_IMG: ("image/jpeg", JPEG)}
    e, fake = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE + "#x"))
    assert fake.urls() == [APPLE, APPLE_IMG]
    assert entry == {"resolved_url": APPLE, "title": "A headline", "summary": "Apple summary",
                     "site": "Apple News", "image": "/link_preview_image/" + key_of(APPLE_IMG)}
    assert e.image_path(key_of(APPLE_IMG)).suffix == ".jpg" and not e.needs(APPLE)
    # today's card keeps its own title/site; only the image (and resolved_url) are added
    msg = relay_msg(APPLE, {"url": APPLE, "title": "Native title", "summary": None, "site": "Apple News", "image": None})
    assert e.apply(msg) is None
    assert msg["link"] == {"url": APPLE, "title": "Native title", "summary": "Apple summary", "site": "Apple News",
                           "image": "/link_preview_image/" + key_of(APPLE_IMG), "resolved_url": APPLE}


@pytest.mark.parametrize("image_route", [None, ("image/svg+xml", SVG), FetchError("http 404")])
def test_link_without_publisher_and_without_usable_image_stays_negative(tmp_path, image_route):
    clock = Clock()
    routes = {APPLE: ("text/html", apple_page_with_image(None))}
    if image_route is not None:
        routes[APPLE_IMG] = image_route
    e, _ = make(tmp_path, routes, now=clock)
    assert asyncio.run(e.resolve(APPLE)) is None
    assert e.reason(APPLE) == "no publisher URL" and not e.needs(APPLE)
    clock.t += 7 * 86400
    assert e.needs(APPLE)


@pytest.mark.parametrize("failure, reason", [
    (FetchError("http 406"), "publisher: http 406"),
    (FetchError("timeout"), "publisher: timeout"),
    (FetchError("network error"), "publisher: network error"),
    (("application/pdf", b"%PDF-1.7"), "publisher: not html"),
])
def test_failed_publisher_falls_back_to_the_apple_page(tmp_path, failure, reason):
    routes = {APPLE: ("text/html", apple_page_with_image(PUB)), PUB: failure, APPLE_IMG: ("image/jpeg", JPEG)}
    e, _ = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE))
    assert entry == {"resolved_url": PUB, "title": "A headline", "summary": "Apple summary",
                     "site": "example-news.com", "image": "/link_preview_image/" + key_of(APPLE_IMG)}
    assert e.reason(APPLE) is None and not e.needs(APPLE)

    # without an image on the apple page the failure is recorded and retried in an hour, as before
    clock = Clock()
    e2 = Enricher(tmp_path / "other", fetch=FakeFetch({APPLE: ("text/html", apple_page(PUB)), PUB: failure}), now=clock)
    assert asyncio.run(e2.resolve(APPLE)) is None and e2.reason(APPLE) == reason
    clock.t += 3600
    assert e2.needs(APPLE)


def test_fallback_never_exposes_a_publisher_url_the_guard_refused(tmp_path):
    lan = "http://192.168.0.10/admin"
    routes = {APPLE: ("text/html", apple_page_with_image(lan)),
              lan: FetchError("blocked (address)", blocked=True), APPLE_IMG: ("image/jpeg", JPEG)}
    e, fake = make(tmp_path, routes)
    entry = asyncio.run(e.resolve(APPLE))
    assert entry["resolved_url"] == APPLE and entry["site"] == "Apple News" and entry["image"]
    assert fake.urls().count(lan) == 1          # a blocked URL is not retried with another agent


def test_publisher_success_does_not_touch_the_apple_image(tmp_path):
    routes = standard_routes()
    routes[APPLE] = ("text/html", apple_page_with_image(PUB))
    e, fake = make(tmp_path, routes)
    assert asyncio.run(e.resolve(APPLE))["title"] == "Big story here"
    assert APPLE_IMG not in fake.urls()


# ---- finding 8: one crawler-agent retry for a publisher bot wall

class BotWall(FakeFetch):
    """The publisher page fails with ``error`` unless asked for as the crawler."""

    def __init__(self, routes: dict, error: Exception):
        super().__init__(routes)
        self.error = error

    async def __call__(self, url, *, max_bytes, accept="html", user_agent=None):
        if url == PUB and user_agent != le.CRAWLER_USER_AGENT:
            self.calls.append((url, max_bytes, accept))
            self.agents.append((url, user_agent))
            raise self.error
        return await super().__call__(url, max_bytes=max_bytes, accept=accept, user_agent=user_agent)


@pytest.mark.parametrize("error", [FetchError("http 406"), FetchError("http 403"), FetchError("timeout"), TimeoutError()])
def test_publisher_bot_wall_is_retried_once_as_a_crawler(tmp_path, error):
    fake = BotWall(standard_routes(), error)
    e = Enricher(tmp_path / "cache", fetch=fake)
    entry = asyncio.run(e.resolve(APPLE))
    assert entry["title"] == "Big story here" and entry["image"]
    assert fake.agents == [(APPLE, None), (PUB, None), (PUB, le.CRAWLER_USER_AGENT),
                           ("https://cdn.example-news.com/a.png", None)]


@pytest.mark.parametrize("error, tries", [
    (FetchError("http 406"), 2), (FetchError("timeout"), 2),
    (FetchError("http 500"), 1), (FetchError("network error"), 1), (FetchError("too many redirects"), 1),
    (FetchError("blocked (address)", blocked=True), 1),
])
def test_crawler_retry_happens_at_most_once_and_only_for_4xx_or_timeout(tmp_path, error, tries):
    e, fake = make(tmp_path, {APPLE: ("text/html", apple_page(PUB)), PUB: error})
    assert asyncio.run(e.resolve(APPLE)) is None
    assert [agent for url, agent in fake.agents if url == PUB] == [None, le.CRAWLER_USER_AGENT][:tries]
    assert e.reason(APPLE) == f"publisher: {error.reason}"


def test_apple_news_is_never_fetched_as_a_crawler(tmp_path):
    e, fake = make(tmp_path, {APPLE: FetchError("http 406")})
    assert asyncio.run(e.resolve(APPLE)) is None
    assert fake.agents == [(APPLE, None)]


def test_guarded_fetcher_user_agent_override():
    fetch, requests = guarded(ok, user_agent="UA/1")
    asyncio.run(fetch("https://pub.example/", max_bytes=100))
    asyncio.run(fetch("https://pub.example/", max_bytes=100, user_agent=le.CRAWLER_USER_AGENT))
    assert [r.headers["user-agent"] for r in requests] == ["UA/1", le.CRAWLER_USER_AGENT]
    assert le.CRAWLER_USER_AGENT != le.DEFAULT_USER_AGENT and "Android" in le.DEFAULT_USER_AGENT
