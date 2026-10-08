"""Fabricate ``NSKeyedArchiver`` blobs shaped like Messages' ``payload_data``.

:func:`make_link_payload` builds::

    {"$archiver": "NSKeyedArchiver", "$version": 100000,
     "$top": {"root": UID(1)}, "$objects": ["$null", root, ...]}

with :class:`plistlib.UID` pointers, archived ``NSURL`` objects
(``{"$class": UID(k), "NS.base": UID(0), "NS.relative": UID(j)}``), an optional
Tahoe ``RichLink`` wrapper, optional embedded image blobs, an optional
``imageMetadata`` dictionary, extra loose strings, and an optional UID cycle.
:func:`build_link_archive` returns the plist dictionary before serialisation so
tests can mutate it for odd shapes (string root, out-of-range UIDs, ...).

Only synthetic URLs (``*.example.invalid``) are ever used here.
"""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from typing import Any

EmbedSpec = str | tuple[str, int]
"""An image kind (``"png"``, ``"jpeg"``, ``"heic"``, ``"raw"``) or ``(kind, size)``."""

DEFAULT_EMBED_SIZE = 3100

IMAGE_HEADS: dict[str, bytes] = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff\xe0",
    "heic": b"\x00\x00\x00\x18ftypheic",
    # A decoy: large, but none of the three magic heads.
    "raw": b"RAWBYTES-NOT-AN-IMAGE",
}


def make_image_blob(kind: str = "png", size: int = DEFAULT_EMBED_SIZE) -> bytes:
    """A ``size``-byte blob with the magic head of ``kind``, padded deterministically."""
    head = IMAGE_HEADS[kind]
    if size <= len(head):
        return head[:size]
    fill = bytes([size % 251 + 1])
    return head + fill * (size - len(head))


class ArchiveBuilder:
    """Append objects to a ``$objects`` table and hand back UID pointers."""

    def __init__(self) -> None:
        self.objects: list[Any] = ["$null"]
        self._classes: dict[str, plistlib.UID] = {}

    def add(self, obj: Any) -> plistlib.UID:
        self.objects.append(obj)
        return plistlib.UID(len(self.objects) - 1)

    def cls(self, name: str, *bases: str) -> plistlib.UID:
        uid = self._classes.get(name)
        if uid is None:
            uid = self.add({"$classname": name, "$classes": [name, *bases, "NSObject"]})
            self._classes[name] = uid
        return uid

    def nsurl(self, url: str) -> plistlib.UID:
        rel = self.add(url)
        return self.add(
            {"$class": self.cls("NSURL"), "NS.base": plistlib.UID(0), "NS.relative": rel}
        )

    def plist(self, root: plistlib.UID) -> dict[str, Any]:
        return {
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$top": {"root": root},
            "$objects": self.objects,
        }


def _embed_specs(embed: EmbedSpec | Sequence[EmbedSpec] | None) -> list[tuple[str, int]]:
    if embed is None:
        return []
    if isinstance(embed, str):
        return [(embed, DEFAULT_EMBED_SIZE)]
    if isinstance(embed, tuple) and len(embed) == 2 and isinstance(embed[1], int):
        return [(str(embed[0]), embed[1])]
    out: list[tuple[str, int]] = []
    for spec in embed:
        out.extend(_embed_specs(spec))
    return out


def build_link_archive(
    url: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    site: str | None = None,
    *,
    original_url: str | None = None,
    plain_url: bool = False,
    wrapped: bool = False,
    embed: EmbedSpec | Sequence[EmbedSpec] | None = None,
    image_meta_url: str | None = None,
    extra_strings: Sequence[str] = (),
    cyclic: bool = False,
) -> dict[str, Any]:
    """Return the keyed-archive plist dictionary for :func:`make_link_payload`.

    ``url`` becomes ``URL`` (an archived ``NSURL``, or a plain string when
    ``plain_url``); ``original_url`` becomes ``originalURL`` (always an archived
    ``NSURL``).  ``title``/``summary``/``site`` are plain strings under
    ``title``/``summary``/``siteName``.  ``wrapped`` puts the metadata behind a
    ``RichLink.richLinkMetadata`` pointer (the Tahoe shape).  ``embed`` adds
    image blobs from :func:`make_image_blob`.  ``image_meta_url`` adds
    ``imageMetadata: {"URL": <NSURL>}``.  ``extra_strings`` are appended as
    loose strings.  ``cyclic`` makes ``$top.root`` point at a UID that points
    at itself.
    """
    b = ArchiveBuilder()
    root_slot = b.add(None)  # reserved index 1: filled below
    assert root_slot.data == 1

    meta: dict[str, Any] = {"$class": b.cls("LPLinkMetadata")}
    if wrapped:
        outer: dict[str, Any] = {"$class": b.cls("RichLink")}
        outer["richLinkMetadata"] = b.add(meta)
        b.objects[1] = outer
    else:
        b.objects[1] = meta

    if original_url is not None:
        meta["originalURL"] = b.nsurl(original_url)
    if url is not None:
        meta["URL"] = b.add(url) if plain_url else b.nsurl(url)
    if title is not None:
        meta["title"] = b.add(title)
    if summary is not None:
        meta["summary"] = b.add(summary)
    if site is not None:
        meta["siteName"] = b.add(site)
    if image_meta_url is not None:
        meta["imageMetadata"] = b.add(
            {"$class": b.cls("LPImageMetadata"), "URL": b.nsurl(image_meta_url)}
        )
    for kind, size in _embed_specs(embed):
        b.add(make_image_blob(kind, size))
    for s in extra_strings:
        b.add(s)

    root = plistlib.UID(1)
    if cyclic:
        self_index = len(b.objects)
        b.add(plistlib.UID(self_index))
        root = plistlib.UID(self_index)
    return b.plist(root)


def dump_archive(plist: dict[str, Any]) -> bytes:
    """Serialise an archive dictionary as a binary plist (what Messages writes)."""
    return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)


def make_link_payload(
    url: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    site: str | None = None,
    *,
    original_url: str | None = None,
    plain_url: bool = False,
    wrapped: bool = False,
    embed: EmbedSpec | Sequence[EmbedSpec] | None = None,
    image_meta_url: str | None = None,
    extra_strings: Sequence[str] = (),
    cyclic: bool = False,
) -> bytes:
    """A binary-plist ``payload_data`` blob; see :func:`build_link_archive`."""
    return dump_archive(
        build_link_archive(
            url,
            title,
            summary,
            site,
            original_url=original_url,
            plain_url=plain_url,
            wrapped=wrapped,
            embed=embed,
            image_meta_url=image_meta_url,
            extra_strings=extra_strings,
            cyclic=cyclic,
        )
    )
