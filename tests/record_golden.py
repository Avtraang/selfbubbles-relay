"""Record the golden JSON the compat / glue tests compare against.

    venv/bin/python -m tests.record_golden            # (re)write tests/golden/*.json
    venv/bin/python -m tests.record_golden --check    # compare, write nothing, exit 1 on a diff

Builds the SAME synthetic database the tests build (``tests.compat_fixture`` on
``conftest.create_fixture_db``, in a fresh temporary directory), runs every
case of every function the tests compare (the case tables live in
``tests.compat_fixture`` so they cannot drift) and dumps the results with
``json.dumps(..., ensure_ascii=False, indent=1, sort_keys=False)``.

The implementation recorded is the shipped module named by ``RELAY_MODULE``
(``relay`` unless set), imported under the conftest's placeholder environment
exactly as the tests import it, with ``chatdb_adapter`` configured with relay's
own hooks.  The files in ``tests/golden/`` were recorded ONCE from the frozen
pre-extraction copy of relay.py (``legacy_chatdb``, since deleted; DESIGN.md
section 7.5 step 7) and the shipped module reproduced them byte for byte.
Re-run this only after a DELIBERATE, versioned JSON change; the diff of
``tests/golden/`` is then the review artefact.

Outcomes that are not plain JSON are normalised (``tests.compat_fixture``):
``link_image`` -> ``{"status": 200, "sha256", "size"}`` or
``{"status": 404, "detail"}``; ``/attachment`` -> ``{"status": 200, "path",
"media_type", "filename"}`` with ``path`` RELATIVE to the fixture's files
directory (the absolute path is a temporary directory and never recorded) or
``{"status": 404, "detail"}``.

Synthetic data only: ``sqlite3.connect`` is guarded (``~/Library`` is
unreachable) before anything is imported, the database lives in a temporary
directory that is deleted afterwards, ``.env`` is never read, and nothing in
the output can name a real person, handle, token or path.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import compat_fixture as cf
from tests.conftest import (
    RELAY_MODULE,
    RELAY_STUB_ENV,
    RelayNew,
    assert_safe_db_path,
    create_fixture_db,
    guard_sqlite_connect,
    import_relay_stubbed,
)

# ---------------------------------------------------------------------------
# the implementation under record
# ---------------------------------------------------------------------------

def relay_side(relay: RelayNew, info) -> SimpleNamespace:
    """The shipped path: ``chatdb_adapter`` (configured with relay's hooks) for the
    database layer and the relay module for everything that lives in relay.py."""
    from fastapi.testclient import TestClient

    r = relay.module
    cf.seed_relay_state(r)
    relay.configure(info.path)

    def health_shape() -> list[str]:
        body = TestClient(r.app).get(
            "/health", headers={"X-Imsg-Token": RELAY_STUB_ENV["IMSG_TOKEN"]}).json()
        return list(body)

    return SimpleNamespace(
        name=relay.name, db=r.db,
        fetch_new=r.fetch_new, fetch_edited=r.fetch_edited,
        max_rowid=r.max_rowid, max_date_edited=r.max_date_edited,
        fetch_thread_messages=r.fetch_thread_messages,
        last_message_previews=r.last_message_previews,
        find_chat_for_addresses=r.find_chat_for_addresses,
        chat_services=r.chat_services, last_rowid_for=r.last_rowid_for,
        group_title=r.group_title,
        search=lambda q, limit: r.search_messages(q, limit=limit),
        link_image=lambda rowid: cf.link_image_outcome(r, rowid), thread_media=r.thread_media,
        seed_thread_state=lambda state: cf.seed_relay_thread_state(relay, state),
        fetch_threads=r.fetch_threads,
        attachment=lambda guid: cf.attachment_outcome(r, guid, info.files_dir),
        contact_recency=r.contact_recency, health_shape=health_shape,
        has_r2_health=cf.has_r2_health(r), has_r6_health=cf.has_r6_health(r),
    )


# ---------------------------------------------------------------------------
# comparing / writing one golden file
# ---------------------------------------------------------------------------
# ``health.json`` is the newest shape: the R2 keys (plan 2026-10-06 section 7,
# objection 4) and, since step R6, ``capabilities``. A source that predates a
# step (the live relay.py until its swap) answers without that step's keys;
# it is compared against the golden without them, exactly as
# tests/test_relay_glue.py expects it, and never allowed to overwrite the
# golden with the older shape (risk 5, golden drift).

def _health_note(has_r2_health: bool, has_r6_health: bool) -> tuple[str, tuple[str, ...]]:
    """``(step name, keys)`` a source lacks, or ``("", ())`` for a current one."""
    if not has_r2_health:
        return "pre-R2", cf.R2_HEALTH_KEYS + cf.R6_HEALTH_KEYS
    if not has_r6_health:
        return "pre-R6", cf.R6_HEALTH_KEYS
    return "", ()


def check_line(name: str, path: Path, text: str, has_r2_health: bool,
               has_r6_health: bool = True) -> tuple[bool, str]:
    """``(same, report line)`` for ``--check``; nothing is written."""
    current = path.read_text(encoding="utf-8") if path.exists() else None
    note = ""
    step, keys = _health_note(has_r2_health, has_r6_health)
    if name == "health" and step and current is not None:
        current = cf.golden_text(cf.health_golden_for(json.loads(current), r2=has_r2_health,
                                                      r6=has_r6_health))
        note = f" ({step} source: compared without " + "/".join(keys) + ")"
    same = current == text
    return same, f"{'same   ' if same else 'DIFFERS'} {path}{note}"


def write_line(name: str, path: Path, text: str, has_r2_health: bool,
               has_r6_health: bool = True) -> str:
    """Write ``text`` to ``path`` and report it; keeps a newer ``health.json``
    when the source predates a step and would only drop that step's keys."""
    step, keys = _health_note(has_r2_health, has_r6_health)
    if name == "health" and step and path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        older = cf.health_golden_for(current, r2=has_r2_health, r6=has_r6_health)
        if current != json.loads(text) and older == json.loads(text):
            return (f"kept  {path} ({step} source would drop " + "/".join(keys)
                    + "; re-record from a current relay)")
    path.write_text(text, encoding="utf-8")
    return f"wrote {path} ({len(text.encode('utf-8'))} bytes)"


# ---------------------------------------------------------------------------
# the recording (one entry per golden file, cases keyed as the tests key them)
# ---------------------------------------------------------------------------

def record(side: SimpleNamespace, info) -> dict[str, dict]:
    m = info.messages
    out: dict[str, dict] = {}

    out["fetch_new"] = {
        "from_zero": side.fetch_new(0),
        "mid_cursor": side.fetch_new(m["link_scan"]),
        "at_max": side.fetch_new(side.max_rowid()),
    }
    edited_mid = side.fetch_edited(cf.builders.BASE_DATE_NS + 10_000)
    out["fetch_edited"] = {
        "from_zero": side.fetch_edited(0),
        "from_mid_mark": edited_mid,
        "from_top": side.fetch_edited(edited_mid[1]),
    }
    out["scalars"] = {"max_rowid": side.max_rowid(), "max_date_edited": side.max_date_edited()}
    out["fetch_thread_messages"] = {
        "limit_50": {guid: side.fetch_thread_messages(guid, 50, None)
                     for guid in cf.THREAD_MESSAGE_GUIDS},
        "c1_limit_50_before_link_scan": side.fetch_thread_messages(cf.C1_GUID, 50, m["link_scan"]),
        "c1_limit_3_before_urls": side.fetch_thread_messages(cf.C1_GUID, 3, m["urls"]),
        "c1_limit_3_before_0": side.fetch_thread_messages(cf.C1_GUID, 3, 0),
    }

    conn = side.db()
    try:
        out["last_message_previews"] = {name: side.last_message_previews(conn, rowids)
                                        for name, rowids in cf.preview_cases(info).items()}
        out["find_chat_for_addresses"] = {case: side.find_chat_for_addresses(conn, addrs)
                                          for case, addrs in cf.FIND_CASES.items()}
        out["chat_services"] = {name: side.chat_services(conn, rids)
                                for name, rids in cf.chat_services_cases(info).items()}
        out["last_rowid_for"] = {**{name: side.last_rowid_for(conn, rid)
                                    for name, rid in info.chats.items()},
                                 "missing": side.last_rowid_for(conn, cf.MISSING_CHAT_ROWID)}
        out["group_title"] = {cf.group_title_key(key, display):
                              side.group_title(conn, cf.chat_rowid(info, key), display)
                              for key, display in cf.GROUP_TITLE_CASES}
        newer_blobs = cf.newer_blob_rows(conn, info)
    finally:
        conn.close()

    out["search"] = {
        "cases": {cf.search_key(q, limit): side.search(q, limit) for q, limit in cf.SEARCH_CASES},
        "oversample": {"newer_blobs": newer_blobs,
                       "NSString": {str(limit): side.search("NSString", limit)
                                    for limit in range(1, newer_blobs + 2)}},
    }
    out["link_image"] = {**{key: side.link_image(m[key])
                            for key in (*cf.LINK_IMAGE_OK, *cf.LINK_IMAGE_404)},
                         cf.LINK_IMAGE_MISSING_KEY: side.link_image(cf.MISSING_ROWID)}
    out["thread_media"] = {guid: side.thread_media(guid) for guid in cf.thread_media_guids(info)}

    now = time.time()
    side.seed_thread_state(cf.thread_state_first_run(now))
    first = side.fetch_threads(200)
    first_again = side.fetch_threads(200)
    side.seed_thread_state(cf.thread_state_with_reads(now, m))
    out["fetch_threads"] = {
        "first_run": first,
        "first_run_second_call": first_again,
        "with_reads": side.fetch_threads(200),
        "with_reads_limit_3": side.fetch_threads(3),
        "with_reads_limit_0": side.fetch_threads(0),
    }
    out["attachment"] = {guid: side.attachment(guid)
                         for guid in (*cf.FILE_ATTACHMENTS, *cf.ATTACHMENT_404)}
    out["health"] = {"full_shape": side.health_shape()}

    # Last: this mutates the database (a new chat and message).
    initial = side.contact_recency()
    cf.add_null_identifier_chat(info.writer)
    out["contact_recency"] = {"initial": initial,
                              "after_null_identifier_chat": side.contact_recency()}

    assert set(out) == set(cf.GOLDEN_NAMES), set(out) ^ set(cf.GOLDEN_NAMES)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="compare with tests/golden/ instead of writing; exit 1 on any difference")
    ap.add_argument("--out", type=Path, default=cf.GOLDEN_DIR, help="output directory")
    args = ap.parse_args(argv)

    mp = pytest.MonkeyPatch()
    guard_sqlite_connect(mp)
    try:
        with tempfile.TemporaryDirectory(prefix="imsg-golden-") as tmp:
            base = Path(tmp)
            fx = create_fixture_db(assert_safe_db_path(base / "compat.db", base), "macos27")
            try:
                info = cf.populate_compat_db(fx, base / "files")
                side = relay_side(import_relay_stubbed(mp, RELAY_MODULE, base / "relay_env"), info)
                goldens = record(side, info)
            finally:
                fx.writer.close()
    finally:
        mp.undo()

    status = 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name in cf.GOLDEN_NAMES:
        path = args.out / f"{name}.json"
        text = cf.golden_text(goldens[name])
        if args.check:
            same, line = check_line(name, path, text, side.has_r2_health, side.has_r6_health)
            status |= 0 if same else 1
            print(line)
        else:
            print(write_line(name, path, text, side.has_r2_health, side.has_r6_health))
    print(f"source: {side.name}; {'check' if args.check else 'write'} {'failed' if status else 'ok'}")
    return status


if __name__ == "__main__":
    sys.exit(main())
