# Security policy

SelfBubbles relay is a single Python process that runs on a Mac signed into
Messages, reads that Mac's `chat.db`, and serves the message history, the
attachments and a send API to the
[SelfBubbles Android app](https://github.com/Avtraang/selfbubbles).
Everything it protects is, in the end, one person's complete message history
and the ability to send as them. This document says what the relay defends
against, how, where that is in the code, and what it does **not** defend
against.

Companion documents: [docs/privacy-and-logs.md](docs/privacy-and-logs.md)
lists what the relay writes to disk (logs, state, caches) and what to redact
before sharing any of it. The app has its own
[SECURITY.md](https://github.com/Avtraang/selfbubbles/blob/main/SECURITY.md).

## Supported versions

Only the `main` branch is supported. There are no release branches or
backports: a fix lands on `main` and you get it by pulling and restarting
the relay. Nothing announces a security fix to a running install. (`GET
/health` → `protocol`, from `PROTOCOL` in `engines/__init__.py`, only tells
the app, when you tap Test connection, that the relay's HTTP contract is
older than the app expects.)

The author runs `main` daily on macOS 27.0 with the Python 3.14 virtualenv
whose packages are pinned in `requirements.txt`; no other macOS or Python
version has been tested.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(**Security** tab → **Report a vulnerability**). Please do not open a public
issue for anything that could expose someone's messages, and please do not
send reports by email.

If the **Report a vulnerability** button is missing, open a public issue
that says only "security contact requested", with no details, and the author
will set up a private channel with you.

Useful in a report: the relay commit (`git rev-parse HEAD`), how the relay is
exposed (Tailscale Serve, Cloudflare Tunnel + Access, something else), the
output of `venv/bin/python relay.py --check`, and the relevant log lines.
Redact all of it first, the `--check` table included (its `data dir` row
contains your macOS user name, and its `listening on` row an address of the
Mac if you bound one), as described in
[docs/privacy-and-logs.md](docs/privacy-and-logs.md#redacting-before-you-paste-a-log-into-an-issue).

This is a one-person project with no security team and no bounty programme.
The author reads reports personally and will reply as soon as they can.

## Threat model

What the relay holds or can do:

- every message in `chat.db`, with sender names resolved through BlueBubbles'
  Contacts endpoint, and every attachment on disk;
- Google Messages threads too, when Beeper Desktop is connected, and, to a
  caller who names a chat by its number, every other chat Beeper Desktop
  holds (see [What the token unlocks](#what-the-token-unlocks));
- sending texts, files, tapbacks and new chats as the Mac's user;
- editing and unsending the owner's own recent iMessages (with
  `imessage-cli` and BlueBubbles; Apple's 15-minute and 2-minute windows);
- answering, declining and minting FaceTime calls (with BlueBubbles);
- family positions from Home Assistant (`/locations`), when configured;
- the owner's own phone numbers / addresses (`IMSG_SELF`);
- the credentials of everything it talks to (BlueBubbles password, Beeper and
  Home Assistant tokens, MapKit JS token, Firebase service-account file).

Who it is designed to keep out:

| Adversary | Control |
| --- | --- |
| Anyone on the internet who finds the relay's hostname | the shared token on every request and WebSocket; optionally Cloudflare Access in front |
| Anyone who knows the example files | a shipped placeholder is never accepted as a token or password, and the relay does not start without a real token |
| Anyone else on your LAN | the example files bind the relay to loopback (`IMSG_BIND=127.0.0.1`); see control 5 for the built-in default |
| Anyone who can read the relay's log files, or a backup of them | token values, `password=` values and the voice routes' query strings are masked before they reach `relay.log` / `relay.err`; `relay.py --check` prints no values |
| A hostile link in an incoming message | the SSRF guard around the only outbound fetches the relay makes for message content (Apple News previews) |
| A bug or crash that could write to Messages' database | `chat.db` and the Beeper bridge database are opened read-only |

Who it does **not** keep out (see [What is not protected](#what-is-not-protected)):
anyone holding the token, anyone with access to the Mac's user account, and
the services the relay itself talks to.

## Controls, verified in the code

### 1. One shared token on every request

`IMSG_TOKEN` (`.env.example`, `launchd/org.selfbubbles.relay.plist.example`)
is required by the HTTP middleware `require_token` in `relay.py` on every
route except `/health`, and by the WebSocket handler `/ws`, which refuses the
upgrade on a bad or missing token: the client gets **HTTP 403 at the
handshake** and no WebSocket is ever opened. (The handler closes before
accepting, and uvicorn turns that into the 403. The close code 1008 that the
handler passes never reaches a client; only the in-process test client
reports it. `tests/test_auth_logging.py` checks the 403 over a real socket.)
Comparison is constant-time (`token_matches`, `hmac.compare_digest` on the
UTF-8 bytes).

The token is accepted in two places:

- the `X-Imsg-Token` request header: what the Android app sends, on HTTP
  requests and on the WebSocket upgrade alike;
- the `?token=` query parameter. This exists for two callers that cannot set
  a header: the BlueBubbles server, which registers its FaceTime webhook URL
  (`POST /bb_event`) with the token in the URL, and a browser loading the
  relay's own `/map` page. The app never puts the token in a URL.

**A placeholder is never the token.** The example files ship
`IMSG_TOKEN=change-me-to-a-long-random-string` (`.env.example`) and
`CHANGE-ME-long-random-string` (the plist example). Both strings are public.
The relay recognises them, and anything else that starts with `change-me`,
`changeme`, `replace-with` or `your-` in any capitalisation
(`placeholders.py`), and never accepts such a value as the token. A key
present in the LaunchAgent plist always beats `.env` (`relay.py` loads `.env`
with `override=False`), so a placeholder left in the plist hides the real
token you put in `.env`: the relay then refuses to start, as described next,
instead of running with the public string.

**Without a real token the relay does not start.** `python relay.py` with
`IMSG_TOKEN` missing or still a placeholder prints three `[auth]` lines to
stderr (`refusing to start: IMSG_TOKEN is not set.` or `... is still the
placeholder from the example file.`, then how to fix it) and exits with
status 78 (`EX_CONFIG`). `relay.py --check` still runs and shows `NOT SET` or
`PLACEHOLDER` in the `token (IMSG_TOKEN)` row. `tests/test_doctor.py` runs
both in a child process. Under launchd, `KeepAlive` starts the relay again
and it exits again until the token is fixed; the author has not watched that
loop, it follows from what launchd documents.

The refusal is lifted only by `IMSG_ALLOW_NO_TOKEN=1` **together with a
loopback bind** (`IMSG_BIND` set to `127.0.0.1`, `::1` or `localhost`). On
any other address, the built-in `0.0.0.0` included, the flag is not honoured
and the relay exits with status 78 as before, saying so
(`startup_refusal()` in `relay.py`; `tests/test_doctor.py`). With both in
place the relay starts with **no authentication at all** (a placeholder
token is ignored, not used), prints `[auth] NO TOKEN SET — IMSG_ALLOW_NO_TOKEN
is 1: every request is answered WITHOUT authentication (a test on this Mac
only)`, and the doctor's hint reads `running WITHOUT authentication`. Treat
that mode as "every process on this Mac can read my messages and send as me"
and use it only for a first local smoke test. That includes a web page open
in a browser on the Mac: the voice routes accept plain GET requests
(`/v/prepare?q=...`, then `/v/confirm?a=yes`), which any page can make a
browser send to `127.0.0.1` without reading the answer, and the relay has no
`Host` check against DNS rebinding. With a token set, such a page cannot
authenticate. The smoke test means `curl http://127.0.0.1:8700/threads` on
the Mac itself: the Android app cannot be used in this mode. It requires a
token, and without one configured `/health` never returns the authenticated
answer, so the app's Test connection reports `The relay rejected the token`.

The check runs only when `relay.py` is started as a program. If you import
the module into another server (`uvicorn relay:app`), nothing exits and the
bind address is that server's business: with no token the relay is open, as
it is with `IMSG_ALLOW_NO_TOKEN=1`, and with a placeholder and no such flag
it is locked instead (every route but `/health` answers 401, because no
token can match). `tests/test_placeholders.py` pins both. The `[auth]` line
printed at import says which of these applies; for a configuration that
`relay.py` would refuse it reads `NO TOKEN SET — relay.py does not start like
this; ...`, so a refused start never leaves "running without
authentication" in `relay.log`.

### 2. `/health` discloses no message data without the token

`/health` is exempt from the middleware so a tunnel can be probed. Without a
valid token a plain `GET /health` answers exactly `{"ok":true}`. With the
token it adds the cursor, the contact count, the `IMSG_SELF` identities,
whether BlueBubbles is reachable, the engine names, the feature map and the
protocol number.

There is no other unauthenticated answer. Earlier versions had one: `GET
/health?nonce=<x>` returned `{"ok":true,"hmac":"<HMAC-SHA256(token, x)>"}` to
**any** caller, for a LAN probe of an earlier app design. One such answer was
enough to test token guesses offline, with no further requests, so the branch
was removed. A `nonce` parameter is now ignored like any other unknown query
parameter: unauthenticated, `/health?nonce=x` answers exactly `{"ok":true}`
(`tests/test_relay_glue.py`).

### 3. The token stays out of the logs

uvicorn's access log records the full request line, so a `?token=` value
would otherwise land in `relay.log`. `relay.py` installs `TokenMaskFilter` on
uvicorn's loggers and on every handler of its log config
(`install_log_masking`, `masked_log_config`) and wraps `sys.stdout` /
`sys.stderr` in `MaskingStream` before the server starts, so every
`token=` / `*_token=` query value is rewritten to `***` in the access log,
in the WebSocket lines and in anything the relay prints. The behaviour is
pinned by `tests/test_auth_logging.py`.

The same masking (`mask_token`) covers two more shapes:

- `password=<value>`. The BlueBubbles password travels as that query
  parameter. No line of the relay prints it, and the connection, timeout and
  protocol errors of the pinned `httpx` do not quote the URL (checked for
  those kinds, not for every error it can raise). But three log lines quote
  the start of an upstream error body, and a server at `BB_URL` that echoed
  the request URL there would otherwise put the password in the log.
- the whole query string of the voice routes (`/v/prepare`, `/v/confirm`,
  `/assistant/prepare`, `/assistant/confirm`). They read their fields from
  the query string when an automation app calls them that way, so the access
  line would hold the dictated message; it is logged as
  `"GET /v/prepare?*** HTTP/1.1"`.

Limits: the masking matches the **shapes** `token=<value>`,
`password=<value>` and a voice route's query string, not the values
themselves: a secret printed in any other shape is not caught. Chat
identifiers, names, filenames, search terms and client addresses are still
logged: see [docs/privacy-and-logs.md](docs/privacy-and-logs.md).

### 4. The doctor prints status words, never values

`venv/bin/python relay.py --check` (and the same table at every start) lists
one row per dependency with a status such as `set`, `NOT SET`, `reachable`,
`token REJECTED`, `PLACEHOLDER`. It never prints a token, a password, a URL
or a hostname. The only paths it prints are the Python interpreter (so the
Full Disk Access hint names the exact binary to grant), the data directory
(both contain your macOS user name) and, when one was found, the location of
the `imessage-cli` binary (`edit / unsend` row; a value of `IMESSAGE_CLI`
that names no executable file is not echoed). The only address it prints is
the one the relay binds (`listening on`, from `IMSG_BIND` and `IMSG_PORT`).
`tests/test_doctor.py::test_doctor_prints_no_secret_values` checks that no
value from the test environment appears in the table.

### 5. Exposure: never put port 8700 on the internet or the LAN directly

The relay speaks **plain HTTP** on `IMSG_BIND`:`IMSG_PORT`
(`uvicorn.run(app, host=BIND, port=PORT, …)`; port 8700 by default). It has
no TLS of its own, no rate limiting and no lockout after failed token
attempts. It also sets no CORS headers, so a browser page on another origin
cannot read its responses.

Where it binds depends on where the configuration came from:

- `.env.example` and the launchd example set `IMSG_BIND=127.0.0.1`: the relay
  answers on the Mac's loopback only, and nothing else on the network can
  connect to the port. This is the setting for both deployments below.
- With `IMSG_BIND` absent the relay binds `0.0.0.0`, **every IPv4
  interface**. That is the built-in default, unchanged from before the key
  existed, and it is what you need only when the HTTPS front reaches the
  relay over the LAN.

The doctor's `listening on` row shows the address in force, with a hint when
it is every interface.

Expected deployment: the relay reached only through an HTTPS front that
terminates TLS and reaches the relay over the Mac's loopback:

- **Tailscale Serve**: HTTPS on the tailnet with a publicly trusted
  certificate; only devices on your tailnet can connect at all;
- **Cloudflare Tunnel + Cloudflare Access** with a service token: the tunnel
  daemon connects out, and Access rejects every request without the right
  `CF-Access-Client-Id` / `CF-Access-Client-Secret` pair before it reaches
  the relay.

The author's daily route is Cloudflare Tunnel + Access. The Tailscale Serve
route follows Tailscale's documentation (publicly trusted certificate,
WebSocket pass-through) but has not been exercised end to end with this app
by the author.

Do not port-forward 8700 on your router. Point the tunnel or `tailscale
serve` at `http://127.0.0.1:8700` (the address, not `localhost`: the relay
listens on IPv4 only) and keep `IMSG_BIND=127.0.0.1`. Nothing else then needs
to reach the port, and nothing else can. Confirm both halves:

```sh
# from another device on the same network (fill in the Mac's LAN address):
# must be refused or time out
curl -m 5 http://<mac-lan-address>:8700/health
# on the Mac: must print {"ok":true}
curl -s http://127.0.0.1:8700/health
```

If the first command prints `{"ok":true}`, the port is open to the LAN: the
relay is running with `0.0.0.0` (look at the `listening on` row; a key in the
LaunchAgent plist beats `.env`, and a plist edit needs `bootout` and
`bootstrap`, not a kickstart). Then check that the app's Test connection,
through the tunnel, still says Connected.

The author's own relay runs with the built-in `0.0.0.0`, because the
author's tunnel connects to the Mac's LAN address. The loopback bind was
checked in a local run (the port answered on `127.0.0.1` and refused
connections on the Mac's LAN address); it has not been run behind a tunnel
or Tailscale Serve by the author.

If you do bind `0.0.0.0`, port 8700 is open to your LAN, where the token and
the messages would travel in cleartext, unless you close it yourself. macOS's
built-in firewall works per application, not per port: System Settings →
Network → Firewall → turn it on → Options… → add the Python the relay runs
under and set it to **Block incoming connections** (the command-line tool is
`/usr/libexec/ApplicationFirewall/socketfilterfw`, with `--add <path>` and
`--blockapp <path>`). That blocks incoming connections for everything that
Python runs, not only the relay, and that includes an HTTPS front on another
machine, so it is an option only when the front runs on the Mac itself.
**The author has not tested the relay with this block in place**, so confirm
both halves with the two `curl` commands above, and that the app still
connects.

The Android app enforces its side of this: it refuses `http://` addresses
and certificates that are not in Android's system store.

Quick check of the HTTPS front from another machine:

```sh
curl -s https://relay.example.com/health            # {"ok":true}
curl -s -o /dev/null -w '%{http_code}\n' https://relay.example.com/threads   # 401
```

That is what a Tailscale Serve address shows from another device on your
tailnet. Behind Cloudflare Access, neither request reaches the relay without
the service-token headers: both show Access's own 403 or a redirect to its
login, which is what you want. Add `-H "CF-Access-Client-Id: <id>" -H
"CF-Access-Client-Secret: <secret>"` to both commands to see the relay's own
`{"ok":true}` and `401`.

### 6. SSRF guard on the relay's own outbound fetches

The only URLs the relay fetches because they appeared in a message are
`apple.news` links, which it resolves to the publisher article for a link
card (`link_enrich.py`; `APPLE_NEWS_PREVIEWS=0` turns this off entirely).
Every fetch (the `apple.news` page, the publisher page, each redirect hop
and each candidate image) goes through `check_url` / `GuardedFetcher`:

- `http` / `https` only, ports 80 / 443 / default only, no userinfo;
- the host must be a literal address or a plain DNS name (no `127.1`,
  `0x7f.1` or decimal tricks);
- the name is resolved first and **every** resolved address must be a
  globally routable unicast address (`is_public_address`): private,
  loopback, link-local, multicast, reserved, unspecified, shared/CGNAT and
  documentation ranges are refused, including IPv4 addresses embedded in
  IPv4-mapped, 6to4, Teredo and NAT64 IPv6 addresses;
- the connection goes to the **validated IP** with the hostname only in the
  `Host` header and TLS SNI, so a second DNS answer cannot redirect it;
- redirects are followed by hand (at most 5), re-checked at every hop, with
  a fresh client per hop so no cookie survives;
- bodies are capped, `Accept-Encoding: identity` is requested and a gzip
  body is inflated with a bound, so a decompression bomb cannot grow past the
  cap; every request (each redirect hop counts as one) is cut off after 10 s.

`tests/test_link_enrich.py` covers the guard. Nothing else in the relay
itself fetches a URL taken from message content. `/bp_asset` hands a
caller-supplied asset URL to Beeper Desktop, which is a different thing: see
control 8.

### 7. Read-only databases

`chat.db` is opened through a `mode=ro` SQLite URI
(`chatdb_adapter.configure(..., readonly_uri=True)` in `relay.py`), so the
relay never writes to Messages' database and never creates `-wal` / `-shm`
files beside it. Pins, archive state and read marks live in the relay's own
`relay_state.json` instead. The Beeper bridge database is opened the same way
(`beeper.py`).

### 8. Attachments are served by identifier, not by path (one exception)

`/attachment/{guid}` and `/thumbnail/{guid}` take an attachment identifier,
look it up in `chat.db` and serve that attachment's file or a cached
rendition of it: a JPEG for a HEIC photo, an M4A for a voice message, a Quick
Look PNG for `/thumbnail` (whose cache is checked before the database). A
request never names a filesystem path; the identifier is one URL path
segment, so it cannot contain a `/`. `/chat_icon/{guid}` reduces the
identifier to `[A-Za-z0-9_]` before using it as a cache filename.

The exception: with Beeper enabled, Google Messages attachments are fetched
through `/bp_asset?u=<asset URL>`. The relay accepts only the three URL
schemes Beeper Desktop's asset endpoint takes, `mxc://`, `localmxc://` and
`file://` (matched as written, lower case), and answers `400 unsupported
asset url` to anything else. It answers the same to a `file://` URL whose
path, percent-decoded once, has a `..` segment or a NUL byte. Any other
accepted URL goes to Beeper Desktop's local asset endpoint
(`beeper.asset_url`) as it is (`tests/test_bp_asset.py` pins all of this).
Beyond the `..` check the relay does not look at where a `file://` URL
points: what comes back is decided by Beeper Desktop, so a token holder can
ask it for any asset of those schemes that it is willing to serve, not only
the ones that appeared in a message.

What Beeper Desktop is willing to serve was measured once, on the author's
Mac on 2026-10-07 (Beeper Desktop 4.3.160 was the installed version): it
answered 403 for a plain `file://` path outside its own media store and 400
for an `https://` URL (that version's own check of the URL is the same three
schemes). On that evidence the relay is not an arbitrary-file reader. This
is an observation about one Beeper Desktop version, not a guarantee: apart
from the `..` and NUL refusal, the relay itself does not enforce it, and
another version may behave differently. Two things were **not** measured:
how Beeper Desktop treats a path that climbs out of its media store with
`..` (the relay refuses those itself, so that it does not depend on the
answer), and a `file://localhost/...` URL, which the relay passes on.

### 9. Upstream credentials

- `BB_PASSWORD` is sent to the BlueBubbles server as the `password` query
  parameter its API requires (`engines/bluebubbles.py`), over whatever
  `BB_URL` is: keep it on `localhost`.
- `BEEPER_TOKEN` and `HA_TOKEN` travel as `Authorization: Bearer` headers to
  `BEEPER_URL` / `HA_URL`.
- `FCM_CREDS` names a Firebase service-account JSON file on disk.
- `MAPKIT_TOKEN` is written into the `/map` page the relay serves, so every
  token holder can read it.
- If you register the FaceTime webhook, BlueBubbles stores the relay's **own**
  token in its webhook list, as part of the URL
  (`…/bb_event?token=<IMSG_TOKEN>`). Treat BlueBubbles' data as holding that
  secret too.

All of the relay's settings live in plaintext in the environment: the
LaunchAgent plist or the `.env` file. Keep those files `chmod 600`, keep
FileVault on, and never commit them (`.gitignore` already lists `.env`,
`fcm-key.json` and `.cf-imsg-creds`).

### 10. Feature flags are not access control

`FEATURE_FACETIME/MAP/TRANSLATE/VOICE` and the derived defaults
(`engines/features.py`) only change what `/health` advertises so the app can
hide buttons. **Every route stays mounted** regardless; a client with the
token can call `/ft_link` or `/locations` whether or not the flag is on
(they will simply fail with 501/503 when the backing service is not
configured).

### 11. The FaceTime auto-admit rig is off unless you build it

`ft-autoadmit.sh` and the `facetime/` helpers drive FaceTime's UI on the Mac
with screen captures and synthetic clicks. The relay launches them only when
`FT_AUTOADMIT` is not `0` **and** the Accessibility-granted helper app named
by `FT_ADMIT_APP` exists (`autoadmit_state()` in `relay.py`). That app is
git-ignored and never rebuilt, so a fresh checkout cannot run the rig by
accident; `relay.py --check` shows `off (helper app missing)`. The rig's
coordinates are specific to the author's display and it writes full-screen
captures to `shots/` while it runs.

### 12. An identifier from the client stays one path segment upstream

Three kinds of route put a client-supplied identifier into the path of a
request the relay makes with an upstream credential, and `httpx` resolves
`..` segments before it sends. Each identifier is confined to one segment:

- **FaceTime call id** (`/ft_answer?uuid=`, `/ft_decline?uuid=`): the routes
  answer `422` unless the id is letters, digits, `.`, `_` and `-` (BlueBubbles
  reports calls by UUID), and the engine percent-encodes the segment and
  refuses an id that is only dots (`engines/bluebubbles.py`). Before this,
  `uuid=../../message/text` made the relay send `POST
  /api/v1/message/text?password=<BB_PASSWORD>`: a token holder could reach
  any BlueBubbles endpoint that accepts a POST without a body.
- **Beeper chat id** (the part after `bp:` in a chat guid, on `/send`,
  `/thread/{chat_guid}/messages`, `/search?chat=` and `/read`): an id that is
  empty, only dots, or contains `/`, `\`, `?`, `#`, `%`, whitespace or a
  control character is refused and no request is made
  (`beeper.chat_path_id`). Before this, `bp:../../v1/accounts#` reached other
  endpoints of Beeper Desktop's API with the Beeper token.

- **Message guid** (`/unsend`): BlueBubbles' unsend endpoint takes the guid
  in its path (`/api/v1/message/<guid>/unsend`). The route only gets that
  far with a guid it has just found in `chat.db`, in the chat that was
  named, and the engine percent-encodes the segment and refuses one that is
  empty or only dots, as for a call id. An error answer from upstream is
  not passed on at all (the route answers `502` in its own words), and one
  that quotes the request URL is logged without the server password (in
  every spelling a URL can give it: on the wire it is percent-encoded),
  the server address and the message guid.

`tests/test_facetime_routes.py`, `tests/test_beeper_paths.py` and
`tests/test_edit_unsend_engines.py` check all three against a transport that
records the path as `httpx` puts it on the wire.
Nothing else in the relay builds an upstream path from client input:
`/chat_icon/{guid}` percent-encodes the chat guid for BlueBubbles (and asks
nothing for a "guid" that is only dots), and every other value travels in a
JSON body or a query parameter.

### 13. Changing a sent message: checked first, confirmed afterwards, and no shell

`POST /edit` and `POST /unsend` are the two routes that alter something
already delivered. What stands between a request and that:

- **Checks before any engine is asked** (`_changeable` in `relay.py`, one
  read-only query): the guid must exist in the chat that was named (an
  unknown guid and a guid from another chat get the same `404`) and be a
  message, not another row of that table (a tapback or a group event, the
  owner's own included: the same `404`); the message must be the owner's
  own (`403`), an iMessage (`409`), not unsent already (`409`), and inside
  Apple's window by the database's own date: 2 minutes for an unsend, 15
  for an edit, and not dated more than a minute ahead of the clock (a
  message that has not gone out; `409`). The route cannot be used to touch
  somebody else's message, an old message, or a guid the client made up.
- **Edits run one at a time, and are checked again when their turn comes.**
  The tool takes seconds, so edits queue. An edit that waited is read from
  `chat.db` a second time before the tool is started: one whose 15 minutes
  ended meanwhile is refused, one whose text is already there (the same
  request sent twice) is answered without running anything, and one that
  would be a sixth edit is refused where the history can be read. At most
  three edits wait, each for at most 20 seconds (`409` beyond that), so a
  token holder cannot park a row of requests that drive Messages.app one
  after another. An unsend is a single short call and does not queue.
- **No shell, and no borrowed environment.** An edit runs Beeper's
  `imessage-cli` (`engines/imessage_cli.py`) as an argument list through
  `subprocess.Popen`, never a command line: the chat guid, the message guid
  and the text are one argument each, whatever quotes, `$(...)`, line breaks
  or leading hyphens they contain. stdin is closed. The environment is
  `HOME`, `PATH`, `LANG` and `TMPDIR`; the relay's token and its upstream
  credentials are not passed on. The binary is the one found at startup
  (`IMESSAGE_CLI`, else Homebrew's two locations, else the PATH) and is
  run by absolute path. On a timeout the tool's own process is killed and
  no other.
- **Arguments the tool would misread are refused.** The tool (0.24.2) has no
  end-of-options marker and takes an argument that is exactly one of its
  options (`--json`, `--format=...`, `-h`) as that option wherever it
  stands; its parser rejects an argument behind three or more hyphens; it
  also reads `latest` and the like as "the newest message" and a bare phone
  number as "find the chat". So the engine requires a message id shaped
  like a guid and not like one of those aliases, a full chat guid, and a
  text that has neither the shape of an option nor three leading hyphens;
  anything else is refused before the tool starts. The routes add their own
  conditions: both ids were just read from `chat.db`, and the text carries
  no control character other than tab and line feed (the tool enters it
  into Messages through Accessibility, where a key code is not text) and
  something visible.
- **The tool is not started where it would only ask for permission.**
  Without the Accessibility grant `imessage-cli` does not fail: going by
  its source (0.24.2; not tried here) it opens the system prompt and a
  window of its own and waits up to two minutes. The engine first
  asks macOS whether its own process is trusted (`AXIsProcessTrusted`,
  which never prompts) and refuses the edit with a fixed phrase when it is
  not, so a missing grant cannot be turned into permission windows on the
  Mac by sending edits.
- **The tool's data directory is the relay's own.** `<data dir>/imessage-cli`
  is handed to the tool only when it is a real directory owned by the
  relay's user, not a symbolic link, and its mode is tightened to 0700
  first. What the tool keeps there has not been examined.
- **Nothing the tool prints is kept.** It echoes its arguments. Its standard
  output goes to an unnamed temporary file, is searched for the `ok edit`
  line and is gone when the call returns; standard error is discarded. A
  failure is reported and logged as a fixed phrase (`imessage-cli timed
  out`, `imessage-cli exited 1`, `imessage-cli reported an error`), never
  the exception text, which on a timeout would be the whole command.
- **Neither an engine's "ok" nor its failure is trusted.** BlueBubbles' edit
  call and the tool's `undo-send` both report success and change nothing on
  macOS 27. After any reported success the relay reads the row again (up to
  8 seconds) and answers `502 the Mac did not apply the change` unless the
  database shows it. After a failure that may have reached Messages (the
  tool was started; BlueBubbles gave no answer or a 5xx) it reads the row
  for 2 seconds and answers with the change if it is there. The tool's own
  success line proves little by itself: it is matched exactly, on standard
  output only, and a text cannot forge it because 0.24.2 prints its
  arguments as one line of JSON; a version that echoed them raw could be
  made to print it, and the database reading is what would catch that.

Three things this does not hide. While an edit runs (about three seconds)
the new text is a command-line argument of a process on the Mac, visible to
anything that can list that user's processes, and the tool's echo of it sits
in an unnamed temporary file in the relay user's temporary directory. And
`imessage-cli` works by driving Messages.app through the Accessibility
permission of the Python that runs the relay: that grant lets that Python
control any app on the Mac, not only Messages. Without the tool installed,
or with `IMESSAGE_CLI=0`, nothing of this is in play and `/edit` answers
`501`. (`SEND_ENGINES` does not switch the edit engine off: it orders the
engines that send.)

The engine's rules were read off one version of the tool, 0.24.2, and a
`brew upgrade` replaces the binary behind the same path. `brew pin
imessage-cli` keeps the version; the doctor row shows which one is
installed and says when it is another.

`tests/test_edit_unsend_engines.py` and `tests/test_edit_unsend_routes.py`
pin all of the above against a fake tool and a scripted BlueBubbles.

## What the token unlocks

Be clear about what a leaked `IMSG_TOKEN` means. Anyone holding it, and able
to reach the relay, can:

- list every thread and read every message and attachment (`/threads`,
  `/thread/{chat_guid}/messages`, `/attachment/{guid}`, `/search`);
- send text and files, react, create new chats, mark chats read, all as you;
- change what you already sent: replace the text of any iMessage you sent in
  the last 15 minutes (up to Apple's five edits per message) and unsend any
  you sent in the last 2 minutes (`/edit`, `/unsend`, control 13). Not older
  messages, and never somebody else's. Each edit also drives Messages.app's
  window on the Mac for a few seconds, one edit at a time;
- read the contact map (`/contacts` gives a count and a ten-entry sample;
  `/contacts/search` and `/contacts/lookup` answer for any name or number),
  your own identities (`/health`) and, if configured, family positions
  (`/locations`);
- obtain your MapKit JS token, which `/map` embeds in the page it serves;
- when Beeper is enabled: read and send in **every chat Beeper Desktop
  holds**, not only Google Messages. The thread list and the live watcher
  show the Google Messages account alone (`BEEPER_GM_ACCOUNT`), but
  `/thread/bp:<n>/messages`, `/search?chat=bp:<n>` and `/send` hand the chat
  number to Beeper Desktop without checking which account it belongs to, and
  those numbers are small integers. If other networks are connected in that
  Beeper Desktop, the relay's token reaches them;
- ask Beeper Desktop for any `mxc://`, `localmxc://` or `file://` asset it
  will serve (`/bp_asset`, control 8), when Beeper is enabled;
- answer or decline FaceTime calls and mint FaceTime links;
- register **their own** phone for push notifications (`/register_push`), so
  message previews reach them even after they lose network access to the
  relay;
- post fake events to `/bb_event` (a spoofed incoming FaceTime ring).

There are no per-user accounts, scopes or audit trail beyond the logs.

Generate the token with `openssl rand -hex 32` (64 letters and digits). Avoid
symbols, because the token is written in places that do not treat it as plain
text: `&` and `<` make the LaunchAgent plist invalid XML; in `.env`, a value
that starts with a quote, contains `${…}` or has a `#` after a space is not
read literally; `&`, `#`, `+` and `%` change the value inside the BlueBubbles
webhook URL (`…/bb_event?token=…`), so the relay sees a different token there
and FaceTime rings stop arriving; `"` and `\` break the `/map` page, which
writes the token into a script string. The app additionally refuses anything
outside printable ASCII. Put Cloudflare Access or a tailnet in front so the
token is the second lock, not the only one.

### Rotating the token

`launchctl kickstart -k` is **not** enough when the token lives in the
LaunchAgent plist. launchd reads a plist only when the job is bootstrapped; a
kickstart restarts the process but does not re-read the plist's
`EnvironmentVariables`, so the relay would come back with the old token. (An
edit to `.env` needs only a restart, because the relay reads `.env` every
time it starts.) The procedure below does a full bootout/bootstrap, which is
correct for both the plist and `.env`.

1. Stop the relay. A plain kill is not enough, because `KeepAlive` restarts
   it:

   ```sh
   launchctl bootout gui/$UID/org.selfbubbles.relay
   ```

2. Change `IMSG_TOKEN` where it is defined: the LaunchAgent plist
   (`~/Library/LaunchAgents/org.selfbubbles.relay.plist`) or `.env`. A key
   present in the plist always beats `.env`, so if it is in both, change the
   plist.
3. Clear the push registrations. The entries in `relay_state.json` →
   `push_tokens` are opaque FCM registration tokens, so you cannot tell your
   phone's from a stranger's: empty the list. Your own phone registers again
   when you save the new token in the app (step 6), and at every start of
   the app after that.

   ```sh
   cd /path/to/selfbubbles-relay
   venv/bin/python -c "import json,pathlib; p=pathlib.Path('relay_state.json'); s=json.loads(p.read_text()); s['push_tokens']=[]; p.write_text(json.dumps(s))"
   rm -f relay_state.bak   # still holds the old list, and is read if relay_state.json fails to parse
   ```

   (If you moved the state file with `IMSG_STATE`, use that path.)
4. Start the relay again; this is the step that makes launchd read the
   edited plist:

   ```sh
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/org.selfbubbles.relay.plist
   ```

5. Prove the old token is dead, on the Mac:

   ```sh
   curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Imsg-Token: <old token>' http://127.0.0.1:8700/threads
   ```

   It must print `401`. `000` means the relay has not finished starting:
   wait a few seconds and repeat. If it prints `200`, the relay is still
   running with the old value: look for a second definition (plist and
   `.env`).
6. Enter the new token in the app (Settings → Relay → Token → Test connection
   → Save); the app reconnects without a restart. A value typed there
   overrides one baked into the app at build time; update `secrets.properties`
   before the next build if you use it.
7. If BlueBubbles' FaceTime webhook is registered, update its URL
   (`…/bb_event?token=<new>`) in the BlueBubbles server settings.
8. If a Cloudflare Access service token was also exposed, rotate it in the
   Cloudflare dashboard and update the pair in the app.

If you run the relay by hand instead of through launchd, stop it, make the
same edits and start it again.

## What is not protected

- **The Mac itself.** The relay runs as your macOS user with Full Disk
  Access. Any process or person with that account can read `chat.db`, the
  plist / `.env` with every credential, `relay_state.json`, and the caches,
  which contain message attachments. The relay adds nothing on top of macOS
  here: use FileVault, a strong login password and a locked screen.
- **The services the relay talks to.** BlueBubbles, Beeper Desktop, Home
  Assistant, Ollama / MarianMT and Firebase each see what the relay sends
  them (message text for translation, previews for push, attachments for
  sending). They are trusted components, not something the relay sandboxes.
- **Message previews in push notifications.** With `FCM_CREDS` set and a
  phone registered, each incoming message that is not a tapback and not in
  an archived chat produces a Firebase data message carrying the chat
  identifier (for a one-to-one chat that is the other person's phone number
  or email address), the chat name, the sender's contact name (or the raw
  number / address when the contact is unknown), up to 300 characters of
  text, the message's row number and guid, and, when the message has a
  picture, the relay-relative path of the first image (`/attachment/<guid>`
  or `/bp_asset?u=…`; no hostname, no token). A FaceTime ring carries the
  call id, the caller's address and name. All of it travels over TLS but is
  readable by Google: it is not end-to-end encrypted. Leave `FCM_CREDS`
  empty to run WebSocket-only.
- **The LAN segment, if you open the port to it.** The listener is plain
  HTTP; with the relay bound to `0.0.0.0`, a sniffer on the same network as a
  client talking to port 8700 directly sees the token and the messages. That
  is why the two documented deployments terminate TLS in front, and why the
  example files bind the relay to loopback (control 5).
- **Token guessing.** Nothing throttles failed attempts, so anyone who can
  send requests to the relay can keep trying tokens. (Guesses can no longer
  be checked offline: the `/health?nonce=` answer that allowed it is gone,
  control 2.) The only defence is the token's length and randomness: a short
  or guessable token is a real risk, the 64-character output of `openssl
  rand -hex 32` is not.
- **Anything about the Private API.** BlueBubbles' Private API needs System
  Integrity Protection disabled on the Mac. That is BlueBubbles' trade-off,
  documented by them; the relay works without it (text and files go through
  AppleScript), losing tapbacks, replies, new chats and FaceTime.
- **The author's own history.** The relay was written for one household and
  has been used daily since July 2026, with an AI coding assistant doing much
  of the typing. It has had no independent security review. Read the code
  before you expose your messages through it.
