# Debug session: browser proxy navigation

Status: [OPEN]

## Symptom

Camoufox/Playwright fails during `page.goto` with `Page.goto: <unknown error>` when using an HTTP proxy for HTTPS pages. The console output also contains a full traceback and a Camoufox `LeakWarning`.

## Hypotheses

1. The URL passed to `page.goto` contains a literal backtick or other formatting character.
2. The configured HTTP proxy cannot establish HTTPS CONNECT through Camoufox.
3. The navigation error is logged too verbosely and obscures the actionable cause.
4. The `LeakWarning` is incidental and is not the navigation failure.

## Evidence

- `check_access.py` runtime reproduced navigation failure for both sources.
- Runtime URL contains no literal backticks: `https://ekb.docdoc.ru/doctor/Brant_Ekaterina` and `https://prodoctorov.ru/volgograd/vrach/1380841-nazarov`.
- Camoufox starts successfully with the configured proxy; failure occurs later in `page.goto` with `Page.goto: <unknown error>`.
- `LeakWarning` is emitted before navigation and is incidental; it recommends `geoip=True`.
- After logging change, ERROR output is one line per navigation failure; traceback is no longer printed at INFO level.
- Tests: `venv\\Scripts\\python.exe -m pytest tests/test_browser.py tests/test_http_client.py -q` → 80 passed.
