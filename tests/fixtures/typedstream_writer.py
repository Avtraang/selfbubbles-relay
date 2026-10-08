"""Fabricate ``message.attributedBody`` blobs (DESIGN.md section 8.2).

Two skeletons, both verified byte-for-byte against the live layouts: a plain
``NSAttributedString`` and an ``NSMutableAttributedString`` (whose string is an
``NSMutableString``).  After the skeleton come LEN, the UTF-8 bytes, then the
trailer: ``0x86``, ``84 02 69 49``, the attribute-run length in UTF-16 code
units, the attribute count, and the fixed ``NSDictionary`` /
``__kIMMessagePartAttributeName`` tail.

Only synthetic text is ever encoded here.
"""

from __future__ import annotations

__all__ = [
    "PLAIN_SKELETON",
    "MUTABLE_SKELETON",
    "TRAILER_HEAD",
    "TRAILER_TAIL",
    "LEN_TAGS",
    "encode_int",
    "encode_attributed_body",
]

PLAIN_SKELETON = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
)
MUTABLE_SKELETON = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x0fNSMutableString\x01"
    b"\x84\x84\x08NSString\x01\x95\x84\x01+"
)

# Byte right after the string, then the start of the attribute run: "84 02 69 49".
TRAILER_HEAD = b"\x86\x84\x02iI"
# After run length + attribute count: one __kIMMessagePartAttributeName = 0 attribute.
TRAILER_TAIL = (
    b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x97"
    b"\x1d__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08NSNumber\x00"
    b"\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86"
)

# tag -> payload width in bytes (little-endian unsigned)
LEN_TAGS: dict[int, int] = {0x81: 2, 0x82: 4, 0x83: 8}

_ATTRIBUTE_COUNT = 1


def encode_int(value: int, *, force_tag: int | None = None) -> bytes:
    """Typedstream integer: one byte ``< 0x80``, else ``0x81``/``0x82``/``0x83`` + LE payload.

    ``force_tag`` emits that wider tag even for a value that would fit in fewer
    bytes.  A tag too narrow for ``value`` or an unknown tag raises ``ValueError``.
    """
    if value < 0:
        raise ValueError(f"typedstream integers are unsigned here, got {value}")
    if force_tag is None:
        if value < 0x80:
            return bytes([value])
        for tag, width in LEN_TAGS.items():
            if value < 1 << (8 * width):
                return bytes([tag]) + value.to_bytes(width, "little")
        raise ValueError(f"{value} does not fit in a u64 length")
    forced_width = LEN_TAGS.get(force_tag)
    if forced_width is None:
        raise ValueError(f"unknown length tag {force_tag:#x}; expected one of {sorted(LEN_TAGS)}")
    if value >= 1 << (8 * forced_width):
        raise ValueError(
            f"{value} does not fit in the {forced_width}-byte payload of tag {force_tag:#x}"
        )
    return bytes([force_tag]) + value.to_bytes(forced_width, "little")


def encode_attributed_body(
    text: str, *, mutable: bool = False, force_len_tag: int | None = None
) -> bytes:
    """Build a complete ``attributedBody`` blob carrying ``text``.

    ``mutable`` selects the ``NSMutableAttributedString`` skeleton.
    ``force_len_tag`` (``0x81``, ``0x82`` or ``0x83``) widens the text LEN tag for
    a short string so the decoder's wide-tag paths can be exercised.
    """
    skeleton = MUTABLE_SKELETON if mutable else PLAIN_SKELETON
    utf8 = text.encode("utf-8")
    run_units = len(text.encode("utf-16-le")) // 2
    return (
        skeleton
        + encode_int(len(utf8), force_tag=force_len_tag)
        + utf8
        + TRAILER_HEAD
        + encode_int(run_units)
        + encode_int(_ATTRIBUTE_COUNT)
        + TRAILER_TAIL
    )
