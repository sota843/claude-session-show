"""Usage snapshot for the tray gauge.

Two data sources:

* ``oauth``   (default, OFFICIAL) — calls the same private endpoint Claude Code's
              ``/usage`` command uses: ``GET https://api.anthropic.com/api/oauth/usage``.
              Returns real ``five_hour`` / ``seven_day`` utilisation percentages and
              reset times. Authenticated with the local OAuth access token from
              ``~/.claude/.credentials.json``. No budget guessing required.
* ``ccusage`` (estimate, fallback) — aggregates ``~/.claude/projects/**/*.jsonl``
              token counts via the ``ccusage`` CLI into 5-hour blocks / weeks and
              divides by a configured budget. Used automatically if the OAuth call
              fails (e.g. expired token) when ``fallback_to_ccusage`` is true.

The OAuth endpoint is undocumented and aggressively rate-limited; poll no faster
than ~180s and always send the ``User-Agent: claude-code/<version>`` header.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import appdata
import config as config_mod

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# OAuth token refresh — the same public client Claude Code uses. When the local
# accessToken is expired (or about to expire) we exchange the long-lived
# refreshToken for a fresh accessToken, without launching Claude Code.
#
# Verified against the installed Claude Code 2.1.247 bundle:
#   POST {TOKEN_URL}  Content-Type: application/json
#   {grant_type, refresh_token, client_id, scope}
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Token exchange uses Claude Code's Axios headers, NOT the claude-code/<version>
# UA required by the usage API. On this machine the usage headers produced 429
# on every exchange; the Axios headers succeeded with the same credentials.
# Keep these separate from the configurable usage User-Agent.
OAUTH_TOKEN_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "axios/1.9.0",
}
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code",
                "user:mcp_servers", "user:file_upload"]
# Refresh a bit before the hard expiry so a request never rides an expired token.
_REFRESH_SKEW_MS = 120_000

# The token endpoint is itself aggressively rate-limited (a single bad request
# can come back 429), so a failed refresh must NEVER be retried on the poll
# interval - that is how the tray used to talk itself into a permanent 429 and
# end up needing Claude Code to hand it a token. Failures back off on a ladder
# persisted in state.json; meanwhile every poll re-reads credentials.json, so if
# Claude Code (or another tray instance) refreshes, we pick that token up free.
_BACKOFF_HTTP_SEC = [300, 900, 1800, 3600]     # rejected / rate-limited by the server
_BACKOFF_NETWORK_SEC = [60, 120, 300, 600]     # offline, e.g. right after login

# Cross-process mutex so two refreshers never burn the same single-use refresh
# token. Claude Code keeps its own lock (~/.claude/.oauth_refresh.lock); we use a
# separate one and re-read credentials under it rather than touching theirs.
_LOCK_DIR = os.path.expanduser("~/.claude/.tray_oauth_refresh.lock")
_LOCK_STALE_SEC = 120

# Under pythonw (no console), shelling out to a .cmd like npx/ccusage would spawn
# a visible cmd window on every call. CREATE_NO_WINDOW suppresses it. The flag is
# Windows-only, so fall back to 0 elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Gauge:
    used: float          # for oauth: utilisation 0..100; for ccusage: token count
    budget: float        # for oauth: 100; for ccusage: token budget
    reset: Optional[datetime]  # local-tz aware

    @property
    def pct(self) -> Optional[float]:
        if self.budget and self.budget > 0:
            return max(0.0, min(1.0, self.used / self.budget))
        return None


@dataclass
class UsageSnapshot:
    session: Optional[Gauge]
    weekly: Optional[Gauge]
    error: Optional[str]
    fetched_at: datetime
    source: str = ""
    note: str = ""  # extra detail for tooltip (e.g. plan, opus weekly)

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_iso_to_local(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


# ---------------------------------------------------------------------------
# Source 1: official OAuth usage endpoint
# ---------------------------------------------------------------------------
_UA_CACHE: Optional[str] = None


def _user_agent(cfg: Dict[str, Any]) -> str:
    global _UA_CACHE
    override = cfg.get("user_agent")
    if override:
        return override
    if _UA_CACHE:
        return _UA_CACHE
    version = "2.1.0"
    exe = shutil.which("claude")
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True,
                                 text=True, timeout=15,
                                 creationflags=_NO_WINDOW).stdout
            m = re.search(r"(\d+\.\d+\.\d+)", out)
            if m:
                version = m.group(1)
        except Exception:  # noqa: BLE001
            pass
    _UA_CACHE = f"claude-code/{version}"
    return _UA_CACHE


def _read_oauth_creds() -> Dict[str, Any]:
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("claudeAiOauth", data)


def _write_oauth_creds(updates: Dict[str, Any]) -> None:
    """Merge ``updates`` into the OAuth block of credentials.json atomically,
    preserving the file's structure and any other keys."""
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    block = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    block.update(updates)
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, CREDENTIALS_PATH)


def _token_is_usable(creds: Dict[str, Any], skew_ms: int = _REFRESH_SKEW_MS) -> bool:
    if not creds.get("accessToken"):
        return False
    expires_at = creds.get("expiresAt")
    return not expires_at or time.time() * 1000 < float(expires_at) - skew_ms


# ----- refresh backoff (persisted in state.json) ----------------------------
def _refresh_state() -> Dict[str, Any]:
    st = appdata.load_state().get("refresh")
    return st if isinstance(st, dict) else {}


def refresh_blocked_for(state: Optional[Dict[str, Any]] = None) -> float:
    """Seconds left on the refresh backoff window (0 when a retry is allowed)."""
    st = _refresh_state() if state is None else state
    return max(0.0, float(st.get("next_attempt_at", 0) or 0) - time.time())


def _note_refresh_failure(reason: str, ladder: List[int],
                          retry_after: Optional[float] = None) -> None:
    fails = int(_refresh_state().get("failures", 0)) + 1
    delay = float(ladder[min(fails, len(ladder)) - 1])
    if retry_after:
        delay = max(delay, retry_after)
    appdata.update_state(refresh={
        "failures": fails,
        "next_attempt_at": time.time() + delay,
        "last_error": reason[:300],
        "last_failed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    appdata.log(f"token refresh failed ({reason[:200]}); "
                f"next attempt in ~{round(delay / 60)}min")


def _note_refresh_success() -> None:
    appdata.update_state(refresh={
        "failures": 0,
        "next_attempt_at": 0,
        "last_error": None,
        "last_success_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })


# ----- cross-process lock ---------------------------------------------------
def _acquire_lock(timeout: float = 15.0) -> bool:
    """mkdir-based mutex; a lock older than _LOCK_STALE_SEC is treated as dead."""
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(_LOCK_DIR)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_LOCK_DIR) > _LOCK_STALE_SEC:
                    appdata.log("clearing stale refresh lock")
                    os.rmdir(_LOCK_DIR)
                    continue
            except OSError:
                pass
        except OSError as exc:  # noqa: BLE001 - unwritable ~/.claude: don't block
            appdata.log(f"refresh lock unavailable ({exc}); continuing without it")
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _release_lock() -> None:
    try:
        os.rmdir(_LOCK_DIR)
    except OSError:
        pass


def _refresh_oauth_token(creds: Dict[str, Any], cfg: Dict[str, Any],
                         force: bool = False) -> Dict[str, Any]:
    """Exchange the refreshToken for a fresh accessToken and persist it.

    Returns the updated creds dict. Raises on failure (expired/invalid refresh
    token, network error, rate limit) so the caller can fall back. Failures arm
    a backoff window; ``force`` ignores it (manual re-auth from the menu)."""
    blocked = 0.0 if force else refresh_blocked_for()
    if blocked:
        st = _refresh_state()
        raise RuntimeError(
            f"refresh backing off {round(blocked / 60)}min after "
            f"{st.get('failures')} failure(s): {st.get('last_error') or ''}")

    got_lock = _acquire_lock()
    try:
        # Someone else (Claude Code, another tray) may have refreshed while we
        # waited. Re-read before spending our single-use refresh token.
        try:
            on_disk = _read_oauth_creds()
        except (OSError, json.JSONDecodeError):
            on_disk = {}
        if on_disk.get("accessToken") != creds.get("accessToken") and _token_is_usable(on_disk):
            appdata.log("credentials were refreshed by another process; reusing that token")
            return on_disk
        if on_disk.get("refreshToken"):
            creds = on_disk
        if not got_lock:
            raise RuntimeError("another process is refreshing the token; retrying next poll")

        return _do_refresh(creds, cfg)
    finally:
        if got_lock:
            _release_lock()


def _do_refresh(creds: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = creds.get("refreshToken")
    if not refresh_token:
        raise RuntimeError("no refreshToken in credentials (run Claude Code once)")

    rte = creds.get("refreshTokenExpiresAt")
    if rte and time.time() * 1000 > float(rte):
        raise RuntimeError("refreshToken expired (run Claude Code once to re-auth)")

    scopes = creds.get("scopes") or OAUTH_SCOPES
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
        "scope": " ".join(scopes),
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers=OAUTH_TOKEN_HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.load(resp)
    except urllib.error.HTTPError as exc:
        retry_after: Optional[float] = None
        try:
            retry_after = float(exc.headers.get("Retry-After") or 0) or None
        except (TypeError, ValueError):
            retry_after = None
        if exc.code == 429:
            detail = "HTTP 429 rate limited by the token endpoint"
        elif exc.code in (400, 401, 403):
            detail = f"HTTP {exc.code} refresh token rejected - run `claude` once to re-authenticate"
        else:
            detail = f"HTTP {exc.code}"
        _note_refresh_failure(detail, _BACKOFF_HTTP_SEC, retry_after)
        raise RuntimeError(f"token refresh: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Typically "no network yet" a few seconds after login: retry sooner.
        _note_refresh_failure(f"network: {exc}", _BACKOFF_NETWORK_SEC)
        raise RuntimeError(f"token refresh: network error ({exc})") from exc

    now_ms = int(time.time() * 1000)
    updates: Dict[str, Any] = {
        "accessToken": tok["access_token"],
        "expiresAt": now_ms + int(tok.get("expires_in", 0)) * 1000,
    }
    if tok.get("refresh_token"):
        updates["refreshToken"] = tok["refresh_token"]
    if tok.get("refresh_token_expires_in"):
        updates["refreshTokenExpiresAt"] = now_ms + int(tok["refresh_token_expires_in"]) * 1000
    _write_oauth_creds(updates)
    _note_refresh_success()
    appdata.log("oauth token refreshed; valid for "
                f"{round(int(tok.get('expires_in', 0)) / 3600, 1)}h")

    merged = dict(creds)
    merged.update(updates)
    return merged


def _window_gauge(window: Optional[Dict[str, Any]]) -> Optional[Gauge]:
    if not window or window.get("utilization") is None:
        return None
    reset = None
    if window.get("resets_at"):
        try:
            reset = _parse_iso_to_local(window["resets_at"])
        except ValueError:
            reset = None
    return Gauge(used=float(window["utilization"]), budget=100.0, reset=reset)


def _oauth_snapshot(cfg: Dict[str, Any], now: datetime) -> UsageSnapshot:
    creds = _read_oauth_creds()

    # Auto-refresh when the accessToken is missing, expired, or about to expire,
    # so the tray no longer needs Claude Code to be running to stay authorised.
    if not _token_is_usable(creds) and cfg.get("auto_refresh", True):
        try:
            creds = _refresh_oauth_token(creds, cfg)
        except Exception:  # noqa: BLE001
            # Inside the pre-expiry skew the current token still works: serve
            # this poll with it rather than failing while the backoff runs.
            if not _token_is_usable(creds, skew_ms=0):
                raise

    token = creds.get("accessToken")
    if not token:
        raise RuntimeError("no accessToken in credentials")
    if not _token_is_usable(creds, skew_ms=0):
        raise RuntimeError("oauth token expired (run Claude Code to refresh)")

    def _get_usage(bearer: str) -> Dict[str, Any]:
        req = urllib.request.Request(
            OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {bearer}",
                "anthropic-beta": OAUTH_BETA,
                "User-Agent": _user_agent(cfg),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    try:
        data = _get_usage(token)
    except urllib.error.HTTPError as exc:
        # Token rejected despite passing the local expiry check (clock skew or
        # server-side revocation): refresh once and retry.
        if exc.code == 401 and cfg.get("auto_refresh", True):
            creds = _refresh_oauth_token(creds, cfg)
            data = _get_usage(creds["accessToken"])
        else:
            raise

    session = _window_gauge(data.get("five_hour"))
    weekly = _window_gauge(data.get("seven_day"))

    note_parts = []
    sub = creds.get("subscriptionType")
    if sub:
        note_parts.append(str(sub))
    opus = _window_gauge(data.get("seven_day_opus"))
    if opus is not None:
        note_parts.append(f"Opus wk {round(opus.pct * 100)}%")
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("utilization") is not None:
        note_parts.append(f"extra {round(float(extra['utilization']))}%")

    return UsageSnapshot(session=session, weekly=weekly, error=None,
                         fetched_at=now, source="oauth", note=" · ".join(note_parts))


# ---------------------------------------------------------------------------
# Source 2: ccusage estimate (fallback)
# ---------------------------------------------------------------------------
def _run_ccusage(cfg: Dict[str, Any], subcommand: str) -> Dict[str, Any]:
    base = list(cfg["ccusage_cmd"])
    resolved = shutil.which(base[0])
    if resolved:
        base[0] = resolved
    elif not base[0].lower().endswith((".exe", ".cmd", ".bat")):
        raise FileNotFoundError(base[0])

    cmd = base + [subcommand, "--json"] + list(cfg.get("ccusage_extra_args", []))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          shell=False, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ccusage {subcommand} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def _ccusage_session_gauge(cfg: Dict[str, Any]) -> Gauge:
    blocks: List[Dict[str, Any]] = _run_ccusage(cfg, "blocks").get("blocks", [])
    active = next((b for b in blocks if b.get("isActive")), None)
    finished = [b for b in blocks if not b.get("isActive") and not b.get("isGap")]

    used = int(active["totalTokens"]) if active else 0
    reset = _parse_iso_to_local(active["endTime"]) if active and active.get("endTime") else None

    if cfg.get("budget_mode") == "fixed":
        budget = float(config_mod.fixed_budgets(cfg)["session"])
    else:
        peak = max((int(b.get("totalTokens", 0)) for b in finished), default=0)
        budget = float(max(peak, used, 1))
    return Gauge(used=used, budget=budget, reset=reset)


def _ccusage_weekly_gauge(cfg: Dict[str, Any]) -> Gauge:
    weeks: List[Dict[str, Any]] = _run_ccusage(cfg, "weekly").get("weekly", [])
    if not weeks:
        return Gauge(used=0, budget=1.0, reset=None)
    weeks_sorted = sorted(weeks, key=lambda w: w.get("period", ""))
    current, history = weeks_sorted[-1], weeks_sorted[:-1]

    used = int(current.get("totalTokens", 0))
    reset: Optional[datetime] = None
    if current.get("period"):
        try:
            reset = (datetime.strptime(current["period"], "%Y-%m-%d") + timedelta(days=7)).astimezone()
        except ValueError:
            reset = None

    if cfg.get("budget_mode") == "fixed":
        budget = float(config_mod.fixed_budgets(cfg)["weekly"])
    else:
        peak = max((int(w.get("totalTokens", 0)) for w in history), default=0)
        budget = float(max(peak, used, 1))
    return Gauge(used=used, budget=budget, reset=reset)


def _ccusage_snapshot(cfg: Dict[str, Any], now: datetime) -> UsageSnapshot:
    return UsageSnapshot(
        session=_ccusage_session_gauge(cfg),
        weekly=_ccusage_weekly_gauge(cfg),
        error=None, fetched_at=now, source="ccusage", note="estimate",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def fetch_snapshot(cfg: Dict[str, Any]) -> UsageSnapshot:
    """Fetch a snapshot. Never raises; failures land in ``.error``."""
    now = datetime.now().astimezone()
    source = cfg.get("source", "oauth")

    if source == "oauth":
        try:
            return _oauth_snapshot(cfg, now)
        except Exception as oauth_exc:  # noqa: BLE001
            if not cfg.get("fallback_to_ccusage", True):
                return UsageSnapshot(None, None, f"oauth: {oauth_exc}", now, source="oauth")
            try:
                snap = _ccusage_snapshot(cfg, now)
                snap.note = f"estimate (oauth failed: {type(oauth_exc).__name__})"
                return snap
            except Exception as cc_exc:  # noqa: BLE001
                return UsageSnapshot(None, None, f"oauth: {oauth_exc}; ccusage: {cc_exc}", now)

    try:
        return _ccusage_snapshot(cfg, now)
    except FileNotFoundError as exc:
        return UsageSnapshot(None, None, f"ccusage not found: {exc}", now)
    except Exception as exc:  # noqa: BLE001
        return UsageSnapshot(None, None, f"{type(exc).__name__}: {exc}", now)


# ---------------------------------------------------------------------------
# Snapshot persistence — the last good reading survives a restart, so the tray
# can show yesterday's value (clearly marked stale) while it re-authenticates
# instead of a blank "?" ring.
# ---------------------------------------------------------------------------
def _gauge_to_dict(g: Optional[Gauge]) -> Optional[Dict[str, Any]]:
    if g is None:
        return None
    return {"used": g.used, "budget": g.budget,
            "reset": g.reset.isoformat() if g.reset else None}


def _gauge_from_dict(d: Optional[Dict[str, Any]]) -> Optional[Gauge]:
    if not d:
        return None
    reset = None
    if d.get("reset"):
        try:
            reset = datetime.fromisoformat(d["reset"])
        except ValueError:
            reset = None
    return Gauge(used=float(d["used"]), budget=float(d["budget"]), reset=reset)


def snapshot_to_dict(snap: UsageSnapshot) -> Dict[str, Any]:
    return {
        "session": _gauge_to_dict(snap.session),
        "weekly": _gauge_to_dict(snap.weekly),
        "fetched_at": snap.fetched_at.isoformat(),
        "source": snap.source,
        "note": snap.note,
    }


def snapshot_from_dict(d: Optional[Dict[str, Any]]) -> Optional[UsageSnapshot]:
    if not isinstance(d, dict) or not d.get("fetched_at"):
        return None
    try:
        fetched_at = datetime.fromisoformat(d["fetched_at"])
    except ValueError:
        return None
    return UsageSnapshot(
        session=_gauge_from_dict(d.get("session")),
        weekly=_gauge_from_dict(d.get("weekly")),
        error=None, fetched_at=fetched_at,
        source=d.get("source", ""), note=d.get("note", ""),
    )


def _fmt_pct(g: Optional[Gauge]) -> str:
    if g is None or g.pct is None:
        return "??%"
    return f"{round(g.pct * 100)}%"


def _fmt_reset(g: Optional[Gauge]) -> str:
    if g is None or g.reset is None:
        return "--"
    return g.reset.strftime("%a %H:%M")


def tooltip_text(snap: UsageSnapshot) -> str:
    if not snap.ok:
        return f"unavailable\n{snap.error}"
    s, w = snap.session, snap.weekly
    lines = [
        f"5h:  {_fmt_pct(s)}  (reset {_fmt_reset(s)})",
        f"Week: {_fmt_pct(w)}  (reset {_fmt_reset(w)})",
    ]
    tag = snap.note or snap.source
    if tag:
        lines.append(tag)
    return "\n".join(lines)


def diagnose() -> None:
    """Print why the tray can (or cannot) authenticate right now."""
    print(f"credentials : {CREDENTIALS_PATH}")
    try:
        creds = _read_oauth_creds()
    except Exception as exc:  # noqa: BLE001
        print(f"  UNREADABLE: {exc}")
        return
    exp = creds.get("expiresAt")
    if exp:
        left = (float(exp) / 1000) - time.time()
        print(f"  accessToken expires : {datetime.fromtimestamp(float(exp) / 1000)} "
              f"({round(left / 60)}min left)")
    print(f"  refreshToken        : {'present' if creds.get('refreshToken') else 'MISSING'}")
    print(f"  plan                : {creds.get('subscriptionType')}")
    st = _refresh_state()
    blocked = refresh_blocked_for(st)
    print(f"refresh state: failures={st.get('failures', 0)} "
          f"backoff={'-' if not blocked else str(round(blocked / 60)) + 'min'}")
    if st.get("last_error"):
        print(f"  last error   : {st['last_error']}")
    if st.get("last_success_at"):
        print(f"  last success : {st['last_success_at']}")
    print(f"log file     : {appdata.LOG_PATH}")
    print(f"user agent   : {_user_agent(config_mod.load_config())}")


if __name__ == "__main__":
    import sys

    cfg = config_mod.load_config()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "diag":
        diagnose()
        raise SystemExit(0)
    if arg == "refresh":
        # Force a token refresh now, ignoring the backoff window.
        try:
            _refresh_oauth_token(_read_oauth_creds(), cfg, force=True)
            print("refresh OK")
        except Exception as exc:  # noqa: BLE001
            print(f"refresh FAILED: {exc}")
            raise SystemExit(1)
        raise SystemExit(0)

    snap = fetch_snapshot(cfg)
    print(f"[source={snap.source}]")
    print(tooltip_text(snap))
    if snap.ok:
        for label, g in (("session", snap.session), ("weekly", snap.weekly)):
            if g is None:
                print(f"  {label}: n/a")
                continue
            pct = None if g.pct is None else round(g.pct * 100, 1)
            print(f"  {label}: used={g.used} budget={int(g.budget)} pct={pct} reset={g.reset}")
