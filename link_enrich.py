"""link_enrich -- Apple News link previews for the relay.

People send ``https://apple.news/XXXX`` links.  Apple's own link payload for
those carries a title but almost never an image.  A non-Apple client that
fetches the apple.news page gets HTML containing
``redirectToUrlAfterTimeout("<publisher article URL>", 0)``; the publisher page
exposes Open Graph tags.  :class:`Enricher` resolves a link in the background,
caches the outcome on disk and overlays it on a relay message dict.

Standard library + httpx only; this module never imports the relay.  All
network I/O goes through one injectable async ``fetch`` callable, so every
branch is unit-testable with fakes.

Network safety (the default fetch, :class:`GuardedFetcher`):

* every URL -- the apple.news page, the publisher page, each redirect hop and
  the image -- passes :func:`check_url` before a request is made: http/https
  only, no userinfo, port absent/80/443, and EVERY address the host resolves to
  must be globally routable;
* the connection is then made to the address that was validated (``Host`` and
  TLS SNI carry the real hostname), so a second DNS answer cannot redirect the
  request to a private address after the check;
* redirects are followed by hand (max 5), no cookies are kept between requests,
  the environment (proxies, ``.netrc``) is ignored, and nothing but a user
  agent and an ``Accept`` header is sent;
* bodies are read undecoded off the wire (``Accept-Encoding: identity``); a
  server that gzips anyway is inflated with a hard output bound, so the byte
  caps hold for what is allocated, not just for what is kept.  Any other
  ``Content-Encoding`` (stacked, brotli, ...) is refused.

Cache keys are normalised (``https://apple.news/<path>``: lower-case host, no
query, no fragment), so textual variants of one link share one entry, and the
cache is bounded (entry count and image bytes, oldest evicted first).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import zlib
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

import httpx

__all__ = [
    "find_apple_news_url", "Enricher", "GuardedFetcher", "Fetched", "FetchError",
    "check_url", "is_public_address", "extract_redirect_url", "parse_open_graph",
    "sniff_image", "normalize_apple_news_url", "DEFAULT_USER_AGENT", "CRAWLER_USER_AGENT",
]

# An Android Chrome agent: apple.news serves it the canonical-URL redirect page
# (an iOS/macOS agent is sent to the News app instead).
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")

# Publisher bot walls (HTTP 406 / a stalled connection) turn the browser agent
# away but answer a link-preview crawler; used for ONE retry of the publisher
# page only -- apple.news always gets the agent above.
CRAWLER_USER_AGENT = "WhatsApp/2.23.20.0"

APPLE_PAGE_MAX = 512 * 1024
PUBLISHER_PAGE_MAX = 1024 * 1024
IMAGE_MAX = 5 * 1024 * 1024
SUMMARY_MAX = 300
TITLE_MAX = 200
SITE_MAX = 80
URL_MAX = 2048                      # publisher / image URLs longer than this are ignored
MAX_ENTRIES = 2000                  # index entries kept (oldest evicted first)
MAX_CACHE_BYTES = 200 * 1024 * 1024  # cached image bytes kept (oldest evicted first)
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 10.0
RETRY_NO_PUBLISHER = 7 * 24 * 3600
RETRY_ERROR = 3600
REASON_NO_PUBLISHER = "no publisher URL"
IMAGE_ROUTE = "/link_preview_image/"
ALLOWED_PORTS = (None, 80, 443)
IMAGE_EXTS = ("jpg", "png", "gif", "webp")

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>'\"’”…"
_KEY_RE = re.compile(r"[0-9a-f]{16,64}")
_IMAGE_FILE_RE = re.compile(r"([0-9a-f]{16,64})\.(?:jpg|png|gif|webp)")
_HTTP_4XX_RE = re.compile(r"http 4\d\d")
# Charsets a publisher's Content-Type may select.  Anything else (Python has
# codecs such as punycode that take a minute on 1 MB) falls back to UTF-8.
_CHARSETS = {
    "utf-8": "utf-8", "utf8": "utf-8",
    "us-ascii": "cp1252", "ascii": "cp1252", "iso-8859-1": "cp1252", "iso8859-1": "cp1252",
    "latin-1": "cp1252", "latin1": "cp1252", "windows-1252": "cp1252", "cp1252": "cp1252",
    "iso-8859-2": "iso-8859-2", "iso-8859-15": "iso-8859-15",
    "windows-1250": "cp1250", "windows-1251": "cp1251", "koi8-r": "koi8-r",
    "utf-16": "utf-16", "utf-16le": "utf-16-le", "utf-16be": "utf-16-be",
    "shift_jis": "shift_jis", "shift-jis": "shift_jis", "sjis": "shift_jis", "euc-jp": "euc_jp",
    "gbk": "gbk", "gb2312": "gbk", "gb18030": "gb18030", "big5": "big5", "euc-kr": "euc_kr",
}
_WS_RE = re.compile(r"\s+")
_REDIRECT_RES = (
    re.compile(r"""redirectToUrlAfterTimeout\(\s*(["'])((?:\\.|(?!\1)[^\\])*)\1"""),
    re.compile(r"""redirectToUrl\(\s*(["'])((?:\\.|(?!\1)[^\\])*)\1"""),
)
_APPLE_HOSTS = ("apple.news", "apple.com", "icloud.com", "itunes.com", "mzstatic.com",
                "cdn-apple.com", "apple")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


# ---------------------------------------------------------------- URL helpers

def _host(url: object) -> str | None:
    """Lower-cased hostname of an http(s) URL without userinfo, else ``None``."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or "@" in parts.netloc:
        return None
    return (parts.hostname or "").lower() or None


def _is_apple_news(url: object) -> bool:
    return _host(url) == "apple.news"


def _is_apple_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _APPLE_HOSTS)


def normalize_apple_news_url(url: object) -> str | None:
    """The cache key for an apple.news link: ``https://apple.news/<path>``.

    Host case, http vs https, port, query and fragment do not select a different
    article, so they must not select a different cache entry (or a new fetch).
    ``None`` when ``url`` is not an apple.news link.
    """
    if not _is_apple_news(url):
        return None
    path = urlsplit(url.strip()).path or "/"      # type: ignore[union-attr]
    return "https://apple.news" + path


def find_apple_news_url(text: str | None) -> str | None:
    """First http(s) URL in ``text`` whose host is exactly ``apple.news``."""
    if not text or not isinstance(text, str):
        return None
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(_TRAILING)
        if _is_apple_news(url):
            return url
    return None


# ----------------------------------------------------------------- SSRF guard

class FetchError(Exception):
    """A fetch that did not produce a body.  ``reason`` is content-free (no URL)."""

    def __init__(self, reason: str, *, blocked: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.blocked = blocked


class Fetched(NamedTuple):
    final_url: str        # the URL the body came from, after redirects
    content_type: str     # raw Content-Type header ("" when absent)
    body: bytes           # at most ``max_bytes`` bytes
    truncated: bool = False   # True when the body was longer than ``max_bytes``


class Target(NamedTuple):
    scheme: str
    host: str
    port: int | None
    addresses: tuple[str, ...]   # validated, IPv4 first


def is_public_address(value: str) -> bool:
    """True only for a globally routable unicast address.

    Private, loopback, link-local, multicast, reserved, unspecified, shared
    (CGNAT) and documentation ranges are refused, as are IPv4-mapped, 6to4,
    Teredo and NAT64 addresses that embed such an address.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = [ip.ipv4_mapped, ip.sixtofour]
        if ip.teredo:
            embedded.extend(ip.teredo)
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        for inner in embedded:
            if inner is not None and not is_public_address(str(inner)):
                return False
        if ip.ipv4_mapped is not None:
            return True          # judged entirely by the embedded IPv4 address
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return ip.is_global


def default_resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def check_url(url: str, resolve_host: Callable[[str], list[str]]) -> Target:
    """Validate ``url`` for fetching or raise ``FetchError(blocked=True)``.

    Makes no request.  ``resolve_host`` is only called for a non-literal host.
    """
    def refuse(why: str) -> FetchError:
        return FetchError(f"blocked ({why})", blocked=True)

    if not isinstance(url, str) or any(c in url for c in "\r\n\t\x00 \\"):
        raise refuse("malformed url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise refuse("malformed url") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise refuse("scheme")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise refuse("userinfo")
    if port not in ALLOWED_PORTS:
        raise refuse("port")
    host = (parts.hostname or "").lower()
    if not host or "%" in host:
        raise refuse("host")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        candidates = [host]
    else:
        if not re.fullmatch(r"[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*\.?",
                            host) or host.rstrip(".").rsplit(".", 1)[-1].isdigit():
            # not a plain DNS name (this also refuses "127.1", "0x7f.1", "2130706433")
            raise refuse("host")
        try:
            candidates = [str(a).split("%", 1)[0] for a in resolve_host(host)]
        except Exception:
            raise FetchError("dns failure") from None
        if not candidates:
            raise FetchError("dns failure")
    if not all(is_public_address(a) for a in candidates):
        raise refuse("address")
    ordered = sorted(dict.fromkeys(candidates), key=lambda a: ":" in a)   # IPv4 first
    return Target(scheme, host, port, tuple(ordered))


async def _read_capped(resp: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most ``max_bytes`` (+1 to detect truncation) of the response body
    from the RAW wire bytes.  Never allocates more than the cap plus one wire
    chunk: a gzip body is inflated with a bounded ``max_length``; any other
    Content-Encoding -- including stacked ones -- is refused."""
    encoding = resp.headers.get("content-encoding", "").strip().lower()
    if encoding in ("", "identity"):
        inflater = None
    elif encoding in ("gzip", "x-gzip"):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    else:
        raise FetchError("encoded response")
    body = bytearray()
    async for chunk in resp.aiter_raw():
        if inflater is None:
            body += chunk
        else:
            data = chunk
            try:
                while data and len(body) <= max_bytes and not inflater.eof:
                    body += inflater.decompress(data, max_bytes + 1 - len(body))
                    data = inflater.unconsumed_tail
            except zlib.error:
                raise FetchError("encoded response") from None
        if len(body) > max_bytes:
            return bytes(body[:max_bytes]), True
        if inflater is not None and inflater.eof:
            break
    return bytes(body), False


class GuardedFetcher:
    """The default ``fetch``: httpx, manual redirects, SSRF guard on every hop.

    ``transport`` is for tests (``httpx.MockTransport``); production leaves it
    ``None``.  Each hop uses a fresh client, so no cookie survives a redirect.
    """

    def __init__(self, *, resolve_host: Callable[[str], list[str]] | None = None,
                 user_agent: str = DEFAULT_USER_AGENT, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = REQUEST_TIMEOUT, max_redirects: int = MAX_REDIRECTS):
        self.resolve_host = resolve_host or default_resolve_host
        self.user_agent = user_agent
        self.transport = transport
        self.timeout = timeout
        self.max_redirects = max_redirects

    async def __call__(self, url: str, *, max_bytes: int, accept: str = "html",
                       user_agent: str | None = None) -> Fetched:
        headers = {
            "User-Agent": user_agent or self.user_agent,
            "Accept": ("image/webp,image/png,image/jpeg,image/gif;q=0.9,image/*;q=0.5"
                       if accept == "image" else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"),
            "Accept-Language": "en-US,en;q=0.8",
            # httpx would otherwise offer gzip/deflate and inflate each wire chunk
            # in full BEFORE the byte cap is looked at (a decompression bomb)
            "Accept-Encoding": "identity",
        }
        current = url
        for _hop in range(self.max_redirects + 1):
            try:
                async with asyncio.timeout(self.timeout):
                    outcome = await self._one_hop(current, headers, max_bytes)
            except TimeoutError:
                raise FetchError("timeout") from None
            if isinstance(outcome, Fetched):
                return outcome
            current = outcome
        raise FetchError("too many redirects")

    async def _one_hop(self, url: str, headers: dict[str, str], max_bytes: int) -> Fetched | str:
        """One guarded request.  Returns the body, or the next URL for a redirect."""
        target = await asyncio.to_thread(check_url, url, self.resolve_host)
        parts = urlsplit(url)
        default_port = 443 if target.scheme == "https" else 80
        host_header = target.host if target.port in (None, default_port) else f"{target.host}:{target.port}"
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        last: Exception | None = None
        for address in target.addresses[:3]:
            literal = f"[{address}]" if ":" in address else address
            pinned = f"{target.scheme}://{literal}{'' if target.port is None else f':{target.port}'}{path}"
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
            try:
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                             transport=self.transport, timeout=self.timeout) as client:
                    async with client.stream("GET", pinned, headers={**headers, "Host": host_header},
                                             extensions=extensions) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            location = resp.headers.get("location")
                            if not location:
                                raise FetchError(f"http {resp.status_code}")
                            return urljoin(url, location.strip())
                        if not 200 <= resp.status_code < 300:
                            raise FetchError(f"http {resp.status_code}")
                        body, truncated = await _read_capped(resp, max_bytes)
                        return Fetched(url, resp.headers.get("content-type", ""), body, truncated)
            except FetchError:
                raise
            except httpx.ConnectError as e:      # try the next validated address
                last = e
            except httpx.TimeoutException:
                raise FetchError("timeout") from None
            except httpx.HTTPError:
                raise FetchError("network error") from None
        raise FetchError("network error") from last


# -------------------------------------------------------------------- parsing

def _js_unescape(raw: str, quote: str) -> str:
    """Decode a JavaScript/JSON string literal body (``\\/``, ``\\u0026``, ``\\x26`` ...)."""
    try:
        if quote == '"':
            return json.loads(f'"{raw}"')
    except ValueError:
        pass

    def one(m: re.Match[str]) -> str:
        esc = m.group(1)
        if esc[0] in "ux":
            try:
                return chr(int(esc[1:], 16))
            except ValueError:
                return esc
        return {"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", one, raw)


def extract_redirect_url(page: str) -> str | None:
    """The publisher URL an apple.news page redirects a non-Apple browser to.

    Returns ``None`` when the page has no redirect call, or its target is not
    an absolute http(s) URL, or the target is apple.news / an Apple host.
    """
    for pattern in _REDIRECT_RES:
        for m in pattern.finditer(page or ""):
            url = html.unescape(_js_unescape(m.group(2), m.group(1))).strip()
            host = _host(url)
            if host and not _is_apple_host(host) and not any(c.isspace() for c in url):
                return url
    return None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        a = {k.lower(): v for k, v in attrs if v is not None}
        content = (a.get("content") or "").strip()
        if not content:
            return
        for attr in ("property", "name"):
            key = (a.get(attr) or "").strip().lower()
            if key and key not in self.found:
                self.found[key] = content

    handle_startendtag = handle_starttag


def _clean(value: str | None, limit: int | None = None) -> str | None:
    text = _WS_RE.sub(" ", value or "").strip()
    if not text:
        return None
    if limit is not None and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def parse_open_graph(page: str, base_url: str) -> dict:
    """``{"title", "summary", "site", "images"}`` from a publisher page.

    ``images`` is the ordered list of absolute http(s) candidates (og:image,
    og:image:secure_url, twitter:image, twitter:image:src), de-duplicated.
    """
    parser = _MetaParser()
    try:
        parser.feed(page or "")
        parser.close()
    except Exception:       # html.parser is lenient; a truncated page must never raise
        pass
    meta = parser.found
    images: list[str] = []
    for key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"):
        raw = meta.get(key)
        if not raw:
            continue
        try:
            absolute = urljoin(base_url, raw.strip())
        except ValueError:
            continue
        if _host(absolute) and len(absolute) <= URL_MAX and absolute not in images:
            images.append(absolute)
    host = _host(base_url) or ""
    return {
        "title": _clean(meta.get("og:title"), TITLE_MAX),
        "summary": _clean(meta.get("og:description"), SUMMARY_MAX),
        "site": _clean(meta.get("og:site_name"), SITE_MAX) or (host[4:] if host.startswith("www.") else host) or None,
        "images": images,
    }


def sniff_image(data: bytes) -> str | None:
    """File extension for PNG / JPEG / GIF / WebP by magic bytes, else ``None``."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _decode(body: bytes, content_type: str) -> str:
    """Decode a page.  Only an allow-listed charset is honoured: the header is
    publisher-controlled and must not be able to pick an arbitrary Python codec."""
    m = re.search(r"charset=[\"']?([\w.:-]+)", content_type or "", re.IGNORECASE)
    codec = _CHARSETS.get(m.group(1).lower(), "utf-8") if m else "utf-8"
    try:
        return body.decode(codec, errors="replace")
    except (LookupError, ValueError):
        return body.decode("utf-8", errors="replace")


def _parse_apple_page(body: bytes, content_type: str, base_url: str) -> tuple[str | None, dict]:
    """(publisher URL or None, the apple.news page's own Open Graph data).  Runs in a thread."""
    text = _decode(body[:APPLE_PAGE_MAX], content_type)
    target = extract_redirect_url(text)
    if target and len(target) > URL_MAX:
        target = None
    return target, parse_open_graph(text, base_url)


def _parse_publisher_page(body: bytes, content_type: str, base_url: str) -> dict:
    """Decode + parse off the event loop (both are CPU-bound on up to 1 MB)."""
    return parse_open_graph(_decode(body[:PUBLISHER_PAGE_MAX], content_type), base_url)


# ------------------------------------------------------------------- Enricher

Fetch = Callable[..., Awaitable[Fetched]]
_FIELDS = ("title", "summary", "site", "image")


class Enricher:
    """Resolve, cache and overlay Apple News link previews.

    ``apply`` / ``lookup`` / ``needs`` are synchronous and thread-safe (the
    relay calls them from worker threads); ``resolve`` runs on the event loop.

    Every method accepts any spelling of an apple.news link; the cache is keyed
    by :func:`normalize_apple_news_url`.  ``fetch`` is called as
    ``fetch(url, max_bytes=..., accept="html"|"image")`` and, for the single
    crawler retry of a publisher page, with ``user_agent=...`` as well.
    """

    def __init__(self, cache_dir: Path, *, enabled: bool = True, fetch: Fetch | None = None,
                 resolve_host: Callable[[str], list[str]] | None = None,
                 now: Callable[[], float] = time.time, user_agent: str = DEFAULT_USER_AGENT,
                 max_entries: int = MAX_ENTRIES, max_cache_bytes: int = MAX_CACHE_BYTES):
        self.cache_dir = Path(cache_dir)
        self.enabled = bool(enabled)
        self._fetch: Fetch = fetch or GuardedFetcher(resolve_host=resolve_host, user_agent=user_agent)
        self._now = now
        self.max_entries = max(1, int(max_entries))
        self.max_cache_bytes = max(0, int(max_cache_bytes))
        self._lock = threading.Lock()
        self._index: dict[str, dict] | None = None
        self._inflight: dict[str, asyncio.Task] = {}
        self._pending: set[str] = set()     # image keys written but not yet in the index

    # ---- cache

    @property
    def _index_path(self) -> Path:
        return self.cache_dir / "index.json"

    def _load(self) -> dict[str, dict]:
        """The index (lock held by the caller).  Corrupt or missing -> empty."""
        if self._index is None:
            data: object = None
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            self._index = ({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
                           if isinstance(data, dict) else {})
        return self._index

    @staticmethod
    def _image_key(entry: dict) -> str | None:
        image = entry.get("image")
        if entry.get("ok") and isinstance(image, str) and image.startswith(IMAGE_ROUTE):
            key = image[len(IMAGE_ROUTE):]
            if _KEY_RE.fullmatch(key):
                return key
        return None

    def _prune(self, index: dict[str, dict], keep: str) -> None:
        """Bound the cache (lock held): at most ``max_entries`` entries and
        ``max_cache_bytes`` of images, oldest entries first; image files no
        entry refers to are deleted.  ``keep`` (the entry just stored) stays."""
        def age(item: tuple[str, dict]) -> float:
            at = item[1].get("at")
            return at if isinstance(at, (int, float)) else 0.0

        oldest = [k for k, _ in sorted(index.items(), key=age) if k != keep]
        while len(index) > self.max_entries and oldest:
            del index[oldest.pop(0)]

        files: dict[str, list[tuple[Path, int]]] = {}
        try:
            for path in self.cache_dir.iterdir():
                m = _IMAGE_FILE_RE.fullmatch(path.name)
                if m:
                    try:
                        files.setdefault(m.group(1), []).append((path, path.stat().st_size))
                    except OSError:
                        pass
        except OSError:
            return

        def drop(key: str) -> int:
            freed = 0
            for path, size in files.pop(key, ()):
                try:
                    path.unlink()
                except OSError:
                    pass
                freed += size
            return freed

        referenced = {k for k in map(self._image_key, index.values()) if k}
        for key in [k for k in files if k not in referenced and k not in self._pending]:
            drop(key)
        total = sum(size for key in files if key in referenced for _, size in files[key])
        for url in oldest:
            if total <= self.max_cache_bytes:
                break
            entry = index.get(url)
            key = self._image_key(entry) if entry else None
            if not key or key not in files:
                continue
            del index[url]
            if not any(self._image_key(e) == key for e in index.values()):
                total -= drop(key)

    def _store(self, url: str, entry: dict, made: set[str] | None = None) -> None:
        with self._lock:
            index = self._load()
            index[url] = entry
            try:
                self._prune(index, url)
            finally:
                self._pending.difference_update(made or ())
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".index-", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(index, f, ensure_ascii=False)
                    os.replace(tmp, self._index_path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except OSError:
                pass        # an unwritable cache only costs a re-resolve after a restart

    def _entry(self, url: object) -> dict | None:
        key = normalize_apple_news_url(url)
        if key is None:
            return None
        with self._lock:
            return self._load().get(key)

    def _public(self, entry: dict) -> dict:
        image = entry.get("image")
        if isinstance(image, str) and image.startswith(IMAGE_ROUTE):
            if self.image_path(image[len(IMAGE_ROUTE):]) is None:
                image = None        # the cached file was removed
        else:
            image = None

        def text(field: str, limit: int) -> str | None:
            value = entry.get(field)
            return _clean(value, limit) if isinstance(value, str) else None

        return {"resolved_url": entry["resolved_url"], "title": text("title", TITLE_MAX),
                "summary": text("summary", SUMMARY_MAX), "site": text("site", SITE_MAX), "image": image}

    def lookup(self, url: str) -> dict | None:
        """The cached positive entry for ``url``, else ``None``."""
        entry = self._entry(url)
        if not entry or not entry.get("ok") or not isinstance(entry.get("resolved_url"), str):
            return None
        return self._public(entry)

    def needs(self, url: str) -> bool:
        """True when ``url`` has no entry, or a negative entry is past its retry time."""
        entry = self._entry(url)
        if not entry:
            return True
        if entry.get("ok") and isinstance(entry.get("resolved_url"), str):
            return False
        retry_at = entry.get("retry_at")
        return not isinstance(retry_at, (int, float)) or self._now() >= retry_at

    def reason(self, url: str) -> str | None:
        """The content-free failure reason of a cached negative entry (for log lines)."""
        entry = self._entry(url)
        if entry and not entry.get("ok"):
            return entry.get("reason")
        return None

    def image_path(self, key: str) -> Path | None:
        """The cached image file for ``key``; malformed keys never reach the filesystem."""
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            return None
        for ext in IMAGE_EXTS:
            path = self.cache_dir / f"{key}.{ext}"
            if path.is_file():
                return path
        return None

    # ---- overlay

    def apply(self, msg: dict) -> str | None:
        """Overlay the cached preview on ``msg`` in place.

        Returns the apple.news URL that still needs resolving (in its normalised
        form, so every spelling of one link is scheduled once), else ``None``.
        A message without an apple.news link is not touched at all.
        """
        if not self.enabled or not isinstance(msg, dict):
            return None
        link = msg.get("link")
        apple_url: str | None = None
        if isinstance(link, dict):
            url = link.get("url")
            if _is_apple_news(url):
                apple_url = url
        elif link is None:
            apple_url = find_apple_news_url(msg.get("text"))
        if not apple_url:
            return None
        entry = self.lookup(apple_url)
        if entry is None:
            return normalize_apple_news_url(apple_url) if self.needs(apple_url) else None
        if isinstance(link, dict):
            merged = link
        else:
            merged = {"url": apple_url, "title": None, "summary": None, "site": None, "image": None}
            if not entry["title"] and not entry["image"]:
                return None             # nothing worth a card: the link stays None
        for field in _FIELDS:
            if not merged.get(field):
                merged[field] = entry[field]
        merged.pop("resolved_url", None)
        merged["resolved_url"] = entry["resolved_url"]      # always the last key
        msg["link"] = merged
        return None

    # ---- resolve

    async def resolve(self, url: str) -> dict | None:
        """Resolve ``url`` over the network (once), cache the outcome, return the
        positive entry or ``None``.  Never raises.  Concurrent calls for the same
        link (any spelling) share one fetch sequence; a fresh cached outcome is
        returned as is."""
        try:
            key = normalize_apple_news_url(url) if self.enabled else None
            if key is None:
                return None
            cached = self.lookup(key)
            if cached is not None:
                return cached
            if not self.needs(key):
                return None
            loop = asyncio.get_running_loop()
            task = self._inflight.get(key)
            if task is None or task.done() or task.get_loop() is not loop:
                task = loop.create_task(self._resolve_once(key))
                self._inflight[key] = task
                task.add_done_callback(lambda t, u=key: self._forget(u, t))
            return await asyncio.shield(task)
        except Exception:
            return None

    def _forget(self, url: str, task: asyncio.Task) -> None:
        if self._inflight.get(url) is task:
            del self._inflight[url]
        if not task.cancelled():
            task.exception()        # mark retrieved

    async def _resolve_once(self, url: str) -> dict | None:
        made: set[str] = set()      # image keys this resolve wrote (shielded from pruning until stored)
        try:
            try:
                entry = await self._work(url, made)
            except FetchError as e:
                entry = self._negative(e.reason, RETRY_ERROR)
            except Exception as e:
                entry = self._negative(f"error ({type(e).__name__})", RETRY_ERROR)
            await asyncio.to_thread(self._store, url, entry, made)
        finally:
            with self._lock:
                self._pending.difference_update(made)
        return self._public(entry) if entry.get("ok") else None

    def _negative(self, reason: str, retry_after: float) -> dict:
        now = self._now()
        return {"ok": False, "reason": reason, "at": now, "retry_at": now + retry_after}

    async def _stage(self, stage: str, url: str, *, max_bytes: int, accept: str, **extra: str) -> Fetched:
        try:
            return await self._fetch(url, max_bytes=max_bytes, accept=accept, **extra)
        except FetchError as e:
            raise FetchError(f"{stage}: {e.reason}", blocked=e.blocked) from None
        except (TimeoutError, asyncio.TimeoutError):
            raise FetchError(f"{stage}: timeout") from None
        except Exception as e:
            raise FetchError(f"{stage}: network error ({type(e).__name__})") from None

    async def _publisher_page(self, target: str) -> Fetched:
        """The publisher page; a 4xx or a timeout (a bot wall) gets ONE more try
        as a link-preview crawler."""
        try:
            return await self._stage("publisher", target, max_bytes=PUBLISHER_PAGE_MAX, accept="html")
        except FetchError as e:
            inner = e.reason.removeprefix("publisher: ")
            if e.blocked or not (inner == "timeout" or _HTTP_4XX_RE.fullmatch(inner)):
                raise
        return await self._stage("publisher", target, max_bytes=PUBLISHER_PAGE_MAX, accept="html",
                                 user_agent=CRAWLER_USER_AGENT)

    async def _first_image(self, candidates: list[str], made: set[str]) -> str | None:
        for candidate in candidates[:2]:
            key = await self._download_image(candidate, made)
            if key:
                return IMAGE_ROUTE + key
        return None

    async def _apple_fallback(self, og: dict, resolved_url: str, site: str | None, made: set[str]) -> dict | None:
        """A positive entry built from the apple.news page's own Open Graph tags,
        for a link with no publisher URL or whose publisher page cannot be read.
        Only worth it when it brings an image (today's card already has a title)."""
        image = await self._first_image(og["images"], made)
        if not image:
            return None
        return {"ok": True, "resolved_url": resolved_url, "title": og["title"], "summary": og["summary"],
                "site": site, "image": image, "at": self._now(), "via": "apple.news"}

    async def _work(self, url: str, made: set[str]) -> dict:
        apple = await self._stage("apple.news", url, max_bytes=APPLE_PAGE_MAX, accept="html")
        target, apple_og = await asyncio.to_thread(_parse_apple_page, apple.body, apple.content_type, url)
        if not target:
            # an Apple News+ exclusive: nowhere else to send the reader
            site = apple_og["site"] if apple_og["site"] != "apple.news" else "Apple News"
            return (await self._apple_fallback(apple_og, url, site, made)
                    or self._negative(REASON_NO_PUBLISHER, RETRY_NO_PUBLISHER))

        host = _host(target) or ""
        try:
            page = await self._publisher_page(target)
            if not (page.content_type or "").strip().lower().startswith("text/html"):
                raise FetchError("publisher: not html")
        except FetchError as e:
            # never hand the app a URL the guard refused to fetch
            fallback = await self._apple_fallback(
                apple_og, url if e.blocked else target,
                "Apple News" if e.blocked else (host[4:] if host.startswith("www.") else host) or None, made)
            if fallback:
                return fallback
            raise
        final_url = page.final_url if _host(page.final_url) else target
        og = await asyncio.to_thread(_parse_publisher_page, page.body, page.content_type, final_url)
        image = await self._first_image(og["images"], made)
        return {"ok": True, "resolved_url": target, "title": og["title"], "summary": og["summary"],
                "site": og["site"], "image": image, "at": self._now()}

    async def _download_image(self, image_url: str, made: set[str]) -> str | None:
        """Fetch, sniff and store one image; returns its cache key or ``None``."""
        try:
            got = await self._fetch(image_url, max_bytes=IMAGE_MAX, accept="image")
        except Exception:
            return None
        if got.truncated or len(got.body) > IMAGE_MAX:
            return None
        ext = sniff_image(got.body)
        if ext is None:
            return None
        key = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:32]
        with self._lock:
            self._pending.add(key)
        made.add(key)
        try:
            await asyncio.to_thread(self._write_image, key, ext, got.body)
        except OSError:
            return None
        return key

    def _write_image(self, key: str, ext: str, body: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".img-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            for other in IMAGE_EXTS:        # one file per key, whatever it sniffed as last time
                if other != ext:
                    try:
                        os.unlink(self.cache_dir / f"{key}.{other}")
                    except OSError:
                        pass
            os.replace(tmp, self.cache_dir / f"{key}.{ext}")
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
