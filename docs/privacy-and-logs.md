# Privacy: what the relay writes to disk, and what to redact

The relay never writes to Messages' own database, but it keeps several files
of its own beside `relay.py`, and most of them contain message content or
identifiers. This page lists every one of them, what is in it, how long it
stays, and what to strip before pasting anything into a GitHub issue.

Paths below are relative to the checkout (where `relay.py` lives) unless
noted. `RELAY_DATA_DIR` (`.env.example`) moves only what `ft-autoadmit.sh`
writes (the FaceTime rig's log, lock and trigger files) and the `imessage-cli/`
folder of the edit tool; the rig's helper app
keeps its screenshots and its own log under `~/imsg-relay/`, the caches and
the state file stay beside `relay.py`, and `IMSG_STATE` moves the state file
on its own.

## At a glance

| File / folder | Written by | Contains | Grows |
| --- | --- | --- | --- |
| `relay.log` | launchd `StandardOutPath` (stdout) | the relay's own `[tag]` lines **and** uvicorn's HTTP access log | forever, no rotation |
| `relay.err` | launchd `StandardErrorPath` (stderr) | uvicorn's startup/shutdown and WebSocket lines, Python tracebacks | forever, no rotation |
| `send_ids.json`, `send_ids.json.tmp` | `SendIds` in `relay.py` | the ids of text sends that carried one (`client_id`), each with a fingerprint of its message, what became of it, a time and the delivery path. No message text and no recipient. Mode 600 | small: ids are kept for 48 hours, at most 2,000, rewritten in place |
| `relay_state.json`, `relay_state.bak`, `relay_state.tmp` | `save_state()` in `relay.py` | cursor, read marks, pins, archive list, auto-translate list, unread marks, icon misses, FCM device tokens | small, rewritten in place |
| `icons/` | `/chat_icon/{guid}` | group-chat photos, one file per chat, named after the chat identifier | until `POST /chat_icon/refresh` |
| `thumb_cache/` | `/thumbnail/{guid}` | Quick Look previews (`<guid>.png`) of videos, PDFs, documents | forever |
| `heic_cache/` | `/attachment/{guid}` | JPEG transcodes (`<guid>.jpg`) of HEIC photos | forever |
| `audio_cache/` | `/attachment/{guid}` | M4A transcodes (`<guid>.m4a`) of voice messages | forever |
| `link_cache/` | `link_enrich.py` | `index.json` (Apple News link → title, summary, site, resolved URL) and the preview images | self-bounded: 2000 entries / 200 MB |
| `~/Library/Messages/RelayOutbox/` | `engines/applescript.py` | copies of files you sent through the AppleScript engine | deleted after 1 hour, on the next file send |
| `imessage-cli/` (under `RELAY_DATA_DIR`) | `imessage-cli` itself, which the relay starts for each edit with this folder as its `--data-dir` | the tool's own working state; the relay neither reads nor writes inside it, and what the tool keeps there has not been examined: treat it as message content. The relay creates the folder with mode 0700, tightens a wider mode before each edit, and does not hand the tool a folder that is a symbolic link or belongs to another user | only on a Mac where the tool is installed and an edit was made; nothing prunes it |
| `ft-auto.log`, `ft-auto.lock`, the trigger files (`ft-snap`, `ft-shot`, `ft-clickxy`) | the FaceTime auto-admit rig's shell script (`ft-autoadmit.sh`) | step log with the start of each FaceTime link | only on a Mac where the rig is enabled |
| `ft-frame-out.txt`, `shots/`, `facetime-admit.log` | the rig's helper app (`ft-loader.applescript` + `ft-admit-logic.applescript`, which use fixed paths under `~/imsg-relay/`) | the last FaceTime window frame; **captures of the Mac display**; the helper's step log, and after a manual `ft-dump` / `ft-nc` debug trigger also the text of the FaceTime or Notification Center windows | only on a Mac where the rig is enabled |

Everything in this table that lives in the checkout is git-ignored
(`.gitignore`; `imessage-cli/` only where it is the folder of that name at
the top of the checkout, which is where it is unless `RELAY_DATA_DIR` moves
it), apart from the rig's trigger files and `relay_state.tmp`,
which exist only for a moment. An `outbox/` folder beside `relay.py`, if
you have one, is not used by the current code (files are staged under
`~/Library/Messages/RelayOutbox/`) and can be deleted.

## `relay.log`

Two streams end up here. Both pass through the masking described in
[SECURITY.md](../SECURITY.md), which rewrites three shapes: any
`token=<value>` becomes `token=***`, any `password=<value>` becomes
`password=***` (the BlueBubbles password travels as that query parameter;
no relay line prints it, the masking is there for an upstream error that
quotes its own URL), and the query string of a voice route is replaced
whole (`/v/prepare?***`). Nothing else is masked.

**Message text.** None of the relay's own lines prints the text of a
message. Three used to, and were changed before publication (the tests in
`tests/test_log_privacy.py` pin the new shapes): the voice assistant's
`[assist]` lines, the AppleScript engine's `[fallback]` lines and the send
chain's `[send] ... failed` line. A fourth, `[beeper] send failed`, quoted
the start of Beeper's answer, which may or may not repeat the message; it
no longer quotes it. The **access log** used to hold a dictated message as
well, whenever a client put it in the query string of a voice route; that
query string is masked now (see below, `tests/test_auth_logging.py`). What
is left is the error text that another program hands back (the lines listed
at the end of this section): the relay logs it as received.

### The relay's own lines

Startup, every time the process starts:

- `[self] excluding N own identities from chat matching`: a **count** only;
  the numbers themselves are not printed.
- One `[auth]` line, which states the configuration:
  `[auth] token required`;
  `[auth] IMSG_TOKEN IS A PLACEHOLDER — every request is refused until a
  real token is set`;
  `[auth] NO TOKEN SET — relay.py does not start like this; …` (no token, and
  either no `IMSG_ALLOW_NO_TOKEN=1` or a bind address that is not loopback);
  or `[auth] NO TOKEN SET — IMSG_ALLOW_NO_TOKEN is 1: every request is
  answered WITHOUT authentication (a test on this Mac only)`.
  After the second and the third the relay exits (status 78): three more
  `[auth]` lines, the first beginning `refusing to start`, go to stderr and
  so to `relay.err`. Under launchd the pair repeats at every restart. Only
  the fourth means that the relay is running without authentication. None
  of these lines contains a value.
- `[engines] send chain: beeper, bluebubbles, applescript; features: …`:
  engine and feature names.
- The doctor table (`[check] relay doctor`): status words only, plus the
  address and port the relay binds (`listening on`: `127.0.0.1:8700`,
  `0.0.0.0:8700`, or an address of the Mac if you set one in `IMSG_BIND`)
  and up to three paths: the data directory, the Python interpreter when
  `chat.db` is not readable (both contain your macOS user name), and the
  location of the `imessage-cli` binary when one was found (`edit / unsend`
  row; `/opt/homebrew/bin/imessage-cli` for a Homebrew install).
- `[fcm] initialized — push enabled`, `[fcm] FCM_CREDS not set — …` or
  `[fcm] init failed: <exception text>` (which can quote the `FCM_CREDS`
  path); `[beeper] enabled — watching Google Messages` /
  `[beeper] disabled (no BEEPER_TOKEN)`.

On a first run only, or after the state file is reset:
`[poll] initialized cursor at ROWID N`, `[poll] initialized edit mark at …`
and `[reads] baseline initialized at ROWID N`.

While running. The lines that carry something personal are marked **(P)**:

- `[contacts] N contacts from BlueBubbles -> M phone/email keys`: counts.
- `[fcm] HH:MM:SS push: imsg|gm chat -> N device(s)`: no content, no
  identifiers; `[fcm] registered device token (N total)`;
  `[fcm] pruned N dead token(s)`.
- `[reads] Messages on the Mac was not told that a chat was read (HTTP <status>)`
  (or `(BlueBubbles failed (<class name>))` when BlueBubbles could not be
  reached) and `[reads] Messages on the Mac did not answer within N s: a chat
  stays unread there`: a `/read` whose BlueBubbles mark-read call failed; no
  identifier, and nothing is printed when it worked.
- `[fcm] skip push for archived chat <chat_guid>` **(P)**: a chat
  identifier (see below).
- `[send] attachment <filename> (<bytes> bytes) -> <chat_guid> via <engine>`
  **(P)**: the file name you sent and the chat identifier.
- `[send] delivered via <engine> -> <chat_guid>` **(P)**, printed when an
  earlier engine failed first.
- `[send] <engine> failed (<reason>)`, followed by ` — trying <next>` when
  another engine will be tried **(P)**. The reason is the engine's own words
  (`AppleScript fallback failed`, `Google Messages send failed`,
  `BlueBubbles failed (<connection error>)`) or, when BlueBubbles answered
  with an HTTP error, `HTTP <status>: <BlueBubbles' JSON error body>` cut at
  200 characters and **without its `data` member**. BlueBubbles server
  1.9.9 puts the message it could not send, text included, under `data`;
  the rest of the body is a status, an error type and an error sentence,
  which can still name a chat identifier. A body that is not a JSON object
  is not quoted at all (`HTTP <status>, body not quoted (…)`). One more
  form, `<engine> failed (<exception text>)`, is logged when an engine
  raises something it did not handle itself; for a file sent through the
  AppleScript engine that is an operating-system error naming the staged
  copy under `RelayOutbox/`, path and file name included. The HTTP answer
  the phone gets is unchanged and does carry BlueBubbles' text.
- `[unsend] <engine>: confirmed in chat.db`, `[edit] <engine>: confirmed in
  chat.db`, and `[unsend] <engine>: reported ok, but chat.db did not change`
  (likewise `[edit]`): the outcome of an Undo Send or an edit, with the
  engine's label (`bb`, `imessage-cli`). No chat, no message identifier, no
  text. A request that is refused before an engine is asked (not your
  message, too late, unknown, another edit running) logs nothing.
- `[edit] <engine>: applied, but chat.db holds another text than the one
  asked for`: an edit landed and Messages kept a different text than it was
  given. Neither text is in the line.
- `[edit] <engine>: failed, but chat.db shows the change` (likewise
  `[unsend]`): the engine reported a failure after it had reached Messages,
  and the database shows the change all the same; it follows the engine's
  own failure line below.
- `[unsend] <engine> failed (<reason>)` and `[edit] <engine> failed
  (<reason>)`: the send chain's failure line under the action's own tag. For
  `imessage-cli` the reason is one of a fixed set of phrases (`imessage-cli
  timed out`, `imessage-cli exited <status>`, `imessage-cli reported an
  error`, `imessage-cli could not be started`, `imessage-cli needs the
  Accessibility grant for the relay's Python`, `imessage-cli cannot take
  ...`): the tool prints its arguments, the new text among them, and none of
  its output is logged. For BlueBubbles it is `BlueBubbles failed (<error
  class>)` when the server could not be reached or, when it answered with an
  HTTP error, `HTTP <status>: <BlueBubbles' JSON error body>` cut at 200
  characters and without its `data` member, as for a failed send; that body
  is BlueBubbles' own wording (seen: `Selected message does not exist!`).
  Should such a body quote the request it answers, the server password (in
  any URL encoding), the server address, the message identifier and the
  chat identifier are replaced by `***` before the line is printed. Unlike
  a failed send, the HTTP answer to an edit or an unsend carries none of
  BlueBubbles' text or status: it is `502` with one of the relay's own
  fixed phrases.
- `[change] chat.db could not be read (<error class>)`: an edit or unsend
  was refused because the database could not be read at that moment.
- `[imessage-cli] N extra Messages instance(s) left running`: after an edit,
  more Messages.app processes were running than before it. A count, nothing
  else; the relay does not quit or kill them.
- `[create] new chat <guid> -> [<addresses>]` and
  `[create] reused existing chat <guid> for [<addresses>]` **(P)**: the
  phone numbers / email addresses of a new chat's members.
- `[archive] archived|restored <chat_guid>` **(P)**;
  `[translate] auto=on|off for <chat_guid>` **(P)**.
- `[assist] <N>-character message -> <contact name> (confident)`,
  `[assist] <N>-character message -> ambiguous: [<names>]`,
  `[assist] sent to <name> via …`, `[assist] started new chat with <name>`,
  `[assist] send failed: HTTP <status>` and `[assist] new-chat failed: HTTP
  <status>` (or the exception's class name when the failure has no status)
  **(P)**: the voice assistant logs the resolved names and the **length**
  of the message it was asked to send, never its text. (A spoken phone
  number is logged in place of a name.) Only present if you use the
  `/assistant` or `/v` endpoints; see the access log below for what a
  request to `/v/prepare` leaves there.
- `[facetime] incoming call <uuid> from <caller>` **(P)**: the caller's
  contact name, or the raw number / address when the contact is unknown;
  `[facetime] call <uuid> ended`, `[facetime] answered <uuid> -> link
  generated`, `[facetime] auto-admit (…) launched for <first 40 characters
  of the FaceTime link>...`, `[facetime] auto-admit skipped: helper app
  missing (FT_ADMIT_APP)`; `[facetime] answer <uuid> failed: …`,
  `[facetime] decline <uuid> failed: …` and `[facetime] link failed: …`,
  where the rest is `BlueBubbles unreachable (<error class>)`, `BlueBubbles
  returned an unexpected answer (<error class>)` or `HTTP <status>: <first
  200 characters of BlueBubbles' answer>`.
- `[icon] cached group icon for <guid> (N bytes)` and
  `[icon] <guid> has no group photo — won't ask again` **(P)**;
  `[icon] cache cleared (N files)`.
- `[links] HH:MM:SS apple.news -> <publisher host> (image=yes|no)`: the
  site a shared Apple News article belongs to, not the article.
- `[beeper] HH:MM:SS new message in <bp: guid> (dormant=…, age=…)` and
  `[beeper] fetch_messages(<bp: guid>) failed: …` **(P)**: a Beeper chat
  identifier (a local chat number such as `bp:401`);
  `[beeper] send failed HTTP <status>`, with ` (<code>)` appended when
  Beeper's answer is JSON carrying a short error code. Nothing else of
  Beeper's answer is logged: whether it repeats the message it was asked to
  send is not known, and earlier versions printed its first 200 characters.
  `[beeper] refused a chat id that is not one path segment`: a `bp:` guid
  whose id part is not a chat id (see [SECURITY.md](../SECURITY.md),
  control 12); the id itself is not printed.
- `[poll] ROWID N no text/att …`, `[poll] edit detected on ROWID N`,
  `[poll] error: …`: database row numbers and exception text.
- `[attachment] sips failed for <guid>: … <first 200 characters of stderr>`
  and `[attachment] heic transcode failed for <guid>: <exception text>`
  **(P)**: tool errors, which can quote the attachment's path on disk and
  with it the file name (on a timeout the exception text is the whole
  `sips` command line); `[attachment] afconvert failed for <guid>: rc=…`.
- `[fallback] osascript error: <exception class>` and `[fallback] osascript
  failed: rc=<exit status> <error wording>`: a failed send through
  Messages.app. The first is the class name alone (`TimeoutExpired` while
  the Automation consent question is unanswered). The second is the first
  line of what `osascript` reported, with everything in quotes replaced by
  `"..."` and any occurrence of the chat identifier, the message text or
  the staged file's path and name replaced by `<arg>`, cut at 200
  characters. The path is looked for as the relay wrote it and in
  AppleScript's own spelling (`Macintosh HD:Users:<you>:…:<name>` becomes
  `Macintosh HD:<arg>`; the volume name stays). What remains is Messages'
  error sentence and number, for example `Can’t get chat id "...".
  (-1728)` or `File Macintosh HD:<arg> wasn’t found. (-43)`. This rests on
  the error wordings the tests feed it, not on a catalogue of everything
  Messages can say: read the line before sharing it. Earlier versions
  printed the
  exception text here, which on a timeout was the whole `osascript`
  command, chat identifier and message text included.

The remaining lines are error reports whose text comes from the failing
library or service: `[beeper] send error: …`, `[beeper] fetch_threads
failed: …`, `[beeper] asset fetch failed: …`, `[beeper] websocket error:
…`, `[beeper] watermark seed failed: …`, `[beeper] bridge DB type lookup
failed: …`, `[contacts] …` (BlueBubbles' answer to the contact request, 200
characters, or a connection error), `[service] chat service lookup failed:
…`, `[fcm] init failed: …`, `[fcm] send error: …`, `[facetime] fcm send
error: …`, `[facetime] auto-admit launch failed: …`, `[translate] marian
unavailable (…)`, `[poll] error: …` and `[poll] error saving re-initialized
cursor: …`. The relay does not control what they quote. None of them is
known to repeat a message, but the relay does not check: read them before
sharing.

A few more lines carry nothing personal and are listed for completeness:
`[beeper] websocket connected + subscribed`, `[beeper] message.upserted: N
entries, M new`, `[links] HH:MM:SS apple.news unresolved (<reason or error
class>)`, `[links] HH:MM:SS overlay failed (<error class>)` / `update
failed (<error class>)`, `[translate] marian HTTP <status> — treating as
skip`, `[fcm] firebase-admin not installed — …`, `[facetime] pruned N dead
token(s)`, `[poll] <reason> — re-initialized cursor at ROWID N`, and
`[contacts] BB_PASSWORD not set in the relay's env — …`.

### uvicorn's access log

uvicorn writes one line per HTTP request to **stdout**, so it lands in
`relay.log` too:

```
INFO:     203.0.113.7:0 - "GET /thread/iMessage%3B-%3B%2B15555550100/messages?limit=50 HTTP/1.1" 200 OK
INFO:     203.0.113.7:0 - "GET /thread/any%3B-%3Bname%40example.com/messages?limit=50 HTTP/1.1" 200 OK
INFO:     203.0.113.7:0 - "GET /search?q=dentist+appointment HTTP/1.1" 200 OK
INFO:     203.0.113.7:0 - "GET /attachment/<attachment-guid>?f=jpg HTTP/1.1" 200 OK
INFO:     203.0.113.7:0 - "POST /v/prepare?*** HTTP/1.1" 200 OK
INFO:     127.0.0.1:52160 - "POST /bb_event?token=*** HTTP/1.1" 200 OK
```

There is no setting to turn the access log off (`uvicorn.run` is called with
fixed arguments). The controls are truncation, file permissions and keeping
the checkout out of backups: see [Retention and cleanup](#retention-and-cleanup).

What the lines mean in practice:

- **Chat identifiers are phone numbers and email addresses.** An iMessage
  chat's guid looks like `iMessage;-;+15555550100` or `any;-;name@example.com`;
  a group chat's ends in `chatNNNNNNNNN…`; a Google Messages chat's is
  `bp:…`. In the access log they are URL-encoded: `;` is `%3B`, `+` is `%2B`
  and `@` is `%40`. Every route that carries the chat in its path
  (`/thread/<chat_guid>/messages`, `/thread/<chat_guid>/media`,
  `/chat_icon/<guid>`, `/search?chat=<chat_guid>`) therefore names who you
  were talking to, and when. (Routes that take the chat in a POST body, such
  as `/read`, `/pin` and `/archive`, log only the path; the relay's own
  `[archive]` and `[translate]` lines name the chat instead.)
- **Search terms are logged.** `/search?q=…`, `/contacts/search?q=…` and
  `/contacts/lookup?q=…` carry what was typed into the search box or the
  compose field.
- **A dictated message in the URL is masked.** The voice endpoints accept
  their fields as a JSON body, a form body or query parameters. Sent as
  query parameters of the POST, `/v/prepare?query=…` (or `q=`) and
  `/assistant/prepare?query=…` carry the whole spoken sentence, message
  text included, and `/v/confirm?answer=…` (or `a=`) the reply. The relay
  replaces the query string of these four routes before the line is
  written: the log shows `"POST /v/prepare?*** HTTP/1.1"`, with no parameter
  names either. This holds for what the relay writes. Anything in front of
  it that keeps its own request log (a reverse proxy, a tunnel's dashboard,
  the automation app's history) still sees the URL, so if your automation
  app can POST a body, configure it that way; the SelfBubbles app's own
  voice screen does. A log written by a version from before 2026-10-07 has
  these lines in full.
- **Attachment and Beeper asset identifiers** (`/attachment/<guid>`,
  `/thumbnail/<guid>`, `/bp_asset?u=mxc%3A%2F%2F…`) are opaque, but they
  map to real files, and a `/bp_asset?u=file%3A%2F%2F…` value can contain a
  path with your macOS user name.
- **The address at the start of the line is the client, and behind a tunnel
  that is the phone.** On a direct connection it is the peer's IP and port.
  Behind Tailscale Serve or a Cloudflare Tunnel pointed at `127.0.0.1`,
  uvicorn trusts the proxy's `X-Forwarded-For` header (its default for
  loopback peers, checked in the pinned uvicorn 0.49), and both services
  add that header according to their documentation. The line then shows the
  phone's own address (its public IP through Cloudflare, its tailnet IP
  through Tailscale) as `IP:0`, on every request. Treat the log as a record
  of where the phone was. Requests from the Mac itself, such as the
  BlueBubbles webhook, show `127.0.0.1`.

## `relay.err`

uvicorn's own logger writes to **stderr**: `Started server process`,
`Uvicorn running on http://<bind address>:<port>`, `Application startup
complete`, `203.0.113.7:0 - "WebSocket /ws" [accepted]` / `403` (a refused
upgrade: wrong or missing token), `connection open` / `closed`, and any
Python traceback the relay raises. The three `[auth]` lines of a refused
start (the first begins `refusing to start`) go here too. The WebSocket
lines carry the same client address as the access log.
A traceback can quote the request that caused it (a path with a chat
identifier, an exception message with a file name). The masking applies
here as well.

## `relay_state.json`

A small JSON object, rewritten atomically (`relay_state.tmp` → rename;
the previous version is kept as `relay_state.bak`, and is read back if
`relay_state.json` is missing or does not parse). Keys written by
`relay.py`:

| Key | Meaning | Personal? |
| --- | --- | --- |
| `last_rowid`, `last_edit`, `reads_baseline` | poll cursors into `chat.db` | no |
| `beeper_seen` | the time of the newest Google Messages message the relay has accounted for (a number), so that a restart can tell what arrived meanwhile; see "Forget the Google Messages mark" below before returning from an older relay | no |
| `reads` | `{chat_guid: rowid}`: the newest message you have read, per chat | chat identifiers |
| `pins`, `archived`, `auto_translate`, `forced_unread` | lists of chat identifiers | chat identifiers |
| `no_icon` | `{chat_guid: timestamp}`: groups known to have no photo | chat identifiers |
| `push_tokens` | the FCM registration tokens of every phone that registered for push | yes: each one lets the holder of your Firebase service account push to that phone |

It does **not** hold the contact map: contact names come from BlueBubbles
at startup and every six hours and live only in memory.

## The caches

`icons/`, `thumb_cache/`, `heic_cache/`, `audio_cache/` and `link_cache/`
are **message content**: the photo of a group chat, a preview frame of every
video or PDF someone sent, a JPEG of every HEIC photo that was viewed on the
phone, an M4A of every voice message that was played, and the title, summary
and preview image of every Apple News link that was shared. Files are named
after attachment or chat identifiers. In `icons/` the name is the chat
identifier with punctuation replaced by `_`, for example
`iMessage___chat123…`; the relay advertises an icon only for group chats, so
in normal use these are group identifiers, not people's numbers.

They exist so a request is answered once per attachment instead of
re-transcoding on every scroll. All of them are regenerated on demand, so
their contents can always be deleted. `link_cache/` prunes itself (oldest
first, 2000 entries, 200 MB of images); the other four have no automatic
cleanup. `~/Library/Messages/RelayOutbox/` holds a copy of each file sent
through the AppleScript engine and deletes copies older than an hour the
next time a file is sent.

## The FaceTime rig's files

Only on a Mac where `FaceTimeAdmit.app` exists and `FT_AUTOADMIT` is not
`0`. `ft-auto.log` records each run with the first 44 characters of the
FaceTime link. The trigger files (`ft-snap`, `ft-shot`, `ft-clickxy`) are
consumed within about a second; `ft-frame-out.txt` holds the last FaceTime
window frame and stays between runs. `shots/` holds **captures of the whole
display** (`full.png`, `latest.png`) and of a region of it (`region.png`),
taken to locate the Admit button. Whatever else was on screen at that moment
is in them. `facetime-admit.log` is the helper app's step log; after a
manual `ft-dump` or `ft-nc` debug trigger it also holds the names and text
of everything in the FaceTime or Notification Center windows.

The helper app's AppleScript has `~/imsg-relay/` hard-coded for `shots/`,
`ft-frame-out.txt`, `facetime-admit.log` and for where it looks for the
trigger files, so those stay there whatever `RELAY_DATA_DIR` says.

## What leaves the Mac

Not files, but worth listing in the same breath:

- **Firebase Cloud Messaging** (if `FCM_CREDS` is set and a phone has
  registered): for each incoming message that is not a tapback and not in an
  archived chat, a data message with the chat identifier (`chat_guid`: for a
  one-to-one chat, the other person's phone number or email address), the
  chat name, the sender's contact name (or the raw number / address when the
  contact is unknown), up to 300 characters of text, the message's row
  number and guid and, when the message has a picture, the relay-relative
  path of the first image (`/attachment/<guid>` or `/bp_asset?u=…`; no
  hostname, no token). For a FaceTime ring: the call id, the caller's
  address and name. Google can read all of it; TLS only protects it between
  hops.
- **Ollama / MarianMT** (`/translate`): the message text being translated.
  Local services by default (`OLLAMA_URL`, `MARIAN_URL`).
- **BlueBubbles**, **Beeper Desktop**: what you send, plus your BlueBubbles
  password / Beeper token, both on `localhost` by default. For an Undo Send,
  BlueBubbles gets the message's identifier.
- **`imessage-cli`** (an edit): a program on the same Mac, started with the
  chat identifier, the message identifier and the new text as command-line
  arguments. For the few seconds it runs, those are visible to anything that
  can list your processes (`ps`). The tool prints them back; the relay sends
  that output to an unnamed temporary file in its own temporary directory
  (never to a log), reads it for the tool's success line and closes it, at
  which point the file is gone. It is given `HOME`, `PATH`, `LANG` and
  `TMPDIR` and none of the relay's secrets. What the tool itself does with
  them, and what it keeps under `imessage-cli/`, is the tool's own matter:
  it is Beeper's open-source `platform-imessage`, not part of this
  repository.
- **Home Assistant** (`/locations`): nothing of yours; your HA token.
- **apple.news and publisher sites** (unless `APPLE_NEWS_PREVIEWS=0`): the
  URL of each Apple News link shared with you, fetched from the Mac's
  address with an Android Chrome user agent (a publisher page that refuses
  it gets one retry with a link-preview crawler agent).

## Retention and cleanup

Nothing rotates on its own. Suggestions:

- **Truncate the logs in place** rather than renaming them: launchd keeps
  them open, so a rename would leave the relay writing into the renamed
  file until its next restart.

  ```sh
  cd /path/to/selfbubbles-relay
  : > relay.log; : > relay.err
  ```

  Run that by hand, from a weekly LaunchAgent, or restart the relay after a
  rotation (`launchctl kickstart -k gui/$UID/org.selfbubbles.relay`).
- **Age out the caches.** This is safe while the relay is running; a deleted
  file is regenerated on the next request:

  ```sh
  find thumb_cache heic_cache audio_cache -type f -mtime +90 -delete
  rm -rf icons/            # group photos; re-fetched from BlueBubbles when next shown
  ```

  While the relay is running, delete files, not the `thumb_cache`,
  `heic_cache` and `audio_cache` folders themselves: those three are created
  only at start. (`icons/` and `link_cache/` are recreated on demand.)
- **Keep the checkout out of synced folders.** If the relay lives under
  iCloud Drive, Dropbox or a folder a backup tool copies, every cache and
  `relay_state.json` go with it. Exclude it from Time Machine with
  `tmutil addexclusion /path/to/selfbubbles-relay`, or at least the five
  cache folders.
- **Permissions**: `chmod 600 .env` and the LaunchAgent plist; the checkout
  itself should not be world-readable on a shared Mac.
- **Clear `push_tokens`** in `relay_state.json` when you retire a phone. The
  entries are opaque FCM registration tokens, so you cannot tell one phone's
  from another's: empty the list and let the phones you still use register
  again (the app does that each time it starts, and whenever a Save or Reset
  changes its relay settings). The relay drops a token on
  its own only when Firebase reports it as unregistered. The relay has to be
  stopped for the edit, and a plain kill does not stop it (`KeepAlive`
  restarts it):

  ```sh
  cd /path/to/selfbubbles-relay
  launchctl bootout gui/$UID/org.selfbubbles.relay
  sleep 2
  venv/bin/python -c "import json,pathlib; p=pathlib.Path('relay_state.json'); s=json.loads(p.read_text()); s['push_tokens']=[]; p.write_text(json.dumps(s))"
  rm -f relay_state.bak   # still holds the old list, and is read if relay_state.json fails to parse
  launchctl bootstrap gui/$UID ~/Library/LaunchAgents/org.selfbubbles.relay.plist
  ```

  The same steps are part of
  [rotating the token](../SECURITY.md#rotating-the-token).

- **Forget the Google Messages mark** (`beeper_seen`) before you start this
  relay again after an older one ran in its place (a rollback), or after
  Google Messages was switched off for a while. Nothing moved the mark in
  that time, so the first start would take every unread text that arrived in
  between (up to six hours back, at most twenty) for one it had missed, and
  announce it a second time. Without the mark the relay starts as on a first
  run: what Beeper Desktop already holds is history, and only what arrives
  from then on is announced. The relay has to be stopped for the edit:

  ```sh
  cd /path/to/selfbubbles-relay
  launchctl bootout gui/$UID/org.selfbubbles.relay
  sleep 2
  venv/bin/python -c "import json,pathlib; p=pathlib.Path('relay_state.json'); s=json.loads(p.read_text()); s.pop('beeper_seen', None); p.write_text(json.dumps(s))"
  rm -f relay_state.bak   # still holds the old mark, and is read if relay_state.json fails to parse
  launchctl bootstrap gui/$UID ~/Library/LaunchAgents/org.selfbubbles.relay.plist
  ```

  Going back to an older relay needs nothing: it ignores the mark.

## Redacting before you paste a log into an issue

Issues are public forever. Before attaching `relay.log`, `relay.err`,
`relay.py --check` output or a traceback:

1. **Chat identifiers**: replace every `+1555…`, `%2B1555…`, email address
   (`name@…` and `name%40…`), `iMessage;-;…`, `any;-;…`, `chatNNN…` and
   `bp:…` with a placeholder such as `<chat>`. Remember they are URL-encoded
   in access-log lines.
2. **Names** in `[assist]`, `[facetime] incoming call … from …` and
   `[create] … -> [...]` lines.
3. **Search terms**: every `/search?q=`, `/contacts/search?q=` and
   `/contacts/lookup?q=` line. **Message text** only in a log written by a
   version from before 2026-10-07: there, every `/v/prepare?…`,
   `/assistant/prepare?…` and `/v/confirm?…` access line, every `[assist] …`
   line, every `[fallback] osascript …` line, every `[send] … failed (…)`
   line and every `[beeper] send failed …` line. They printed the dictated
   text, the whole `osascript` command, BlueBubbles' answer with the message
   in it and the start of Beeper's answer.
4. **File names** in `[send] attachment …` lines and tool errors
   (`[attachment] …`).
5. **Paths**: the doctor's data-directory and interpreter rows contain your
   user name; so do traceback frames (`/Users/<you>/…`).
6. **Addresses**: the client `IP:port` at the start of access-log and
   WebSocket lines (behind a tunnel, that is the phone's own IP address),
   the doctor's `listening on` row and uvicorn's `Uvicorn running on …`
   line if you bound the relay to an address of the Mac, and your relay
   hostname if it appears.
7. **Confirm the masking held**: `grep -n 'token=' relay.log relay.err |
   grep -v 'token=\*\*\*'` must print nothing, and neither must
   `grep -n 'password=' relay.log relay.err | grep -v 'password=\*\*\*'`.
   The relay has no line that prints the BlueBubbles password; the second
   command is the check that nothing upstream quoted it either.
8. Never attach `relay_state.json` (push tokens), `.env`, the plist,
   `fcm-key.json`, anything from `shots/`, or any cache file.

A starting point. It was tested on synthetic lines of every shape listed
above, on macOS's own `sed`:

```sh
sed -E \
  -e '/^\[assist\]/d' \
  -e 's/(%2B|\+)[0-9]{7,15}/<number>/g' \
  -e 's/[A-Za-z0-9._%+-]+(@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<email>/g' \
  -e 's/chat[0-9]{6,}/<group>/g' \
  -e 's/([0-9]{1,3}\.){3}[0-9]{1,3}(:[0-9]+)?/<ip>/g' \
  -e 's#/Users/[^/ ]+#/Users/<you>#g' \
  -e 's/([?&](q|query|a|answer|u|src)=)[^ &"]+/\1<redacted>/g' \
  -e 's/(incoming call [^ ]+ from ).*/\1<caller>/' \
  -e 's/(\[send\] attachment ).*( \([0-9]+ bytes\))/\1<file>\2/' \
  relay.log > relay.redacted.log
```

It deletes the `[assist]` lines (they carry contact names) and replaces
`+`-prefixed numbers, email addresses (plain and URL-encoded), group
identifiers, IPv4 addresses, home directory names, the values of the `q`,
`query`, `a`, `answer`, `u` and `src` query parameters (search terms, what
an older version logged for the voice routes, Beeper asset URLs), FaceTime
caller names and sent file names. It does **not** remove
numbers written without a `+`
(short codes, for instance), names anywhere else, text quoted inside error
lines (`[send] … failed (…)`, tracebacks), IPv6 addresses, your relay
hostname or `bp:` chat numbers. A regular expression cannot know every shape
your contacts' names take: delete or edit those lines by hand, then read
`relay.redacted.log` top to bottom before attaching it.
