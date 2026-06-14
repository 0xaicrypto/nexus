"""
Regression: desktop auth state must live in sessionStorage, not
localStorage.

User-stated requirement (2026-06-14):

    登陆之后，关闭desktop，应该首先自动登出，然后下次重新打开的时候
    需要重新登陆.

Implementation: ``packages/desktop-v2/src/store.ts`` and ``api-client.ts``
were migrated from localStorage → sessionStorage for the two auth keys
(``nexus.auth.token`` + ``nexus.auth.user_id``). sessionStorage is
wiped when the webview is destroyed (window close / app quit) — so
the next launch has no token + no cached user_id → LoginView shows
and the medic re-enters their name. Minimise / focus changes don't
destroy the webview, so an active session survives those.

This test is a source-level guard. It reads the two TS files and
asserts:
  1. The auth keys are NEVER written through localStorage.
  2. They ARE written through sessionStorage somewhere.

Without (1) a regression silently reintroduces the "stays logged in
across restarts" bug; without (2) we'd accidentally drop auth
persistence entirely (single in-memory session, blanks every
navigation — also broken).

These checks are deliberately permissive about other localStorage
keys (theme, displayName, hidden patients) — those legitimately stay
in localStorage. We only constrain the auth-related keys.
"""
from __future__ import annotations

import pathlib
import re

DESKTOP_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "desktop-v2" / "src"
)


def _strip_comments(text: str) -> str:
    """Remove ``//`` line comments and ``/* … */`` block comments so
    the bug-history docstrings in our store.ts don't false-positive
    on the regression check.
    """
    # Block comments first.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Line comments — be careful not to eat the // inside a string.
    out_lines = []
    for line in text.splitlines():
        # Crude: find the first // not preceded by an opening quote on
        # the same line. Good enough for our codebase style.
        idx = line.find("//")
        if idx >= 0:
            # Drop the // and everything after.
            line = line[:idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def _read(name: str) -> str:
    path = DESKTOP_SRC / name
    assert path.exists(), f"expected file missing: {path}"
    return _strip_comments(path.read_text())


def test_store_token_uses_sessionStorage_not_localStorage():
    """``setToken`` + ``readStoredToken`` MUST go through
    sessionStorage. A regression that flips this back to localStorage
    silently re-introduces the cross-restart login bug."""
    src = _read("store.ts")

    # Find every reference to TOKEN_KEY usage on a storage call.
    # Each site MUST be ``sessionStorage.{getItem,setItem,removeItem}(TOKEN_KEY)``.
    bad = re.findall(
        r"localStorage\.\w+\(\s*TOKEN_KEY\s*\)",
        src,
    )
    assert not bad, (
        "store.ts uses localStorage for the JWT token — that survives "
        "across app restarts and breaks the 'close = logout' UX. Use "
        "sessionStorage. Offending refs: " + repr(bad)
    )
    # Confirm sessionStorage IS used somewhere with TOKEN_KEY.
    good = re.findall(
        r"sessionStorage\.\w+\(\s*TOKEN_KEY\s*\)",
        src,
    )
    assert good, (
        "store.ts no longer writes the token to sessionStorage — that "
        "drops auth persistence entirely (blank token on every "
        "navigation). Expected at least one sessionStorage.* call "
        "referencing TOKEN_KEY."
    )


def test_store_token_literal_key_not_in_localStorage():
    """Belt-and-suspenders: the literal ``'nexus.auth.token'`` must
    NEVER be written through localStorage — JWT is auth state.
    The user_id is a different concern (see
    test_api_client_user_id_uses_localStorage below): it's the
    medic's identifier, not auth, and IS persisted across launches
    so the medic's data is reachable after re-login."""
    for fname in ("store.ts", "lib/api-client.ts"):
        src = _read(fname)
        bad = re.findall(
            r"localStorage\.\w+\(\s*['\"]nexus\.auth\.token['\"]",
            src,
        )
        assert not bad, (
            f"{fname} writes the JWT through localStorage. "
            "Auth state must live in sessionStorage so closing the "
            "window logs the user out. Offending refs: " + repr(bad)
        )


def test_api_client_user_id_uses_localStorage():
    """The cached user_id MUST live in localStorage so the medic's
    patients / memory / sessions are still reachable after a
    close-and-reopen cycle.

    History (2026-06-14): user_id used to live in sessionStorage
    alongside the JWT. Closing the window minted a fresh user_id on
    next sign-in, and the medic saw an empty Patient list + empty
    Memory tab even though the DB still had their data — it was
    just bound to the old user_id they no longer had a handle to.
    The fix: user_id is identity (persistent), JWT is auth
    (session-scoped)."""
    src = _read("lib/api-client.ts")

    # The three helpers all wrap STORAGE_KEY_USER_ID — locate them by
    # the function names instead of by the variable, so this test
    # doesn't break if the constant is renamed.
    for fn in ("readUserId", "writeUserId", "clearUserId"):
        body_match = re.search(
            rf"function {fn}\([^)]*\)\s*[^{{]*\{{(?P<body>.*?)\n\}}",
            src, re.DOTALL,
        )
        assert body_match, f"{fn} not found in api-client.ts"
        body = body_match.group("body")
        assert "localStorage" in body, (
            f"{fn} doesn't use localStorage. The cached user_id is "
            "identity, not auth — putting it in sessionStorage wipes "
            "it on window close and the medic loses their data "
            "binding."
        )
        assert "sessionStorage" not in body, (
            f"{fn} still references sessionStorage. user_id must "
            "persist across launches (sessionStorage wipes on close); "
            "auth tier handled separately via the JWT."
        )
