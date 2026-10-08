## What changed

<!-- What the change does and why. Link the issue if there is one. -->

## How it was tested

<!-- Run both commands and paste the last line of each. -->

```
python -m pytest -q tests
python -m tests.record_golden --check
```

- pytest summary line: <!-- e.g. "1004 passed, 3 warnings in 42.10s" -->
- golden check line: <!-- "source: relay; check ok" -->
- macOS and Python version it ran on:

## Checklist

- [ ] No secrets or personal data anywhere in the diff, the tests or this description: no token or password, no `.env` or `relay_state.json` content, no phone number, e-mail address, real name, host name, private IP address or message text. Fixtures use synthetic values only.
- [ ] Tests added or updated for the change; the suite still builds its own synthetic database and never opens a real `chat.db`.
- [ ] Docs updated where behaviour, configuration keys or log lines changed (`README.md`, `SECURITY.md`, `docs/`, `.env.example`, the launchd example).
- [ ] If `relay.py` changed: every HTTP response keeps its shape and `tests.record_golden --check` passes untouched, **or** the goldens were re-recorded (`python -m tests.record_golden`) and this description says which responses changed and why.
