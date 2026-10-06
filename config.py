"""Configuration loading/saving for the Claude usage tray app.

Reads ``config.json`` next to this file, deep-merging it onto DEFAULTS so a
partial / missing user file still yields a complete config. Also exposes
helpers to resolve the per-plan token budgets.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# NOTE: token budgets below are ROUGH placeholders. Anthropic does not expose
# the real subscription limits locally, and ccusage's totalTokens includes huge
# cache-read counts, so absolute numbers are plan/usage dependent. Either tune
# these in config.json, or use "budget_mode": "auto" to calibrate the gauge
# against your own historical peak block / week.
DEFAULTS: Dict[str, Any] = {
    # Data source:
    #   "oauth"   -> OFFICIAL %/reset from api.anthropic.com/api/oauth/usage
    #                (same data as Claude Code's /usage). No budget needed.
    #   "ccusage" -> local-log token ESTIMATE via ccusage.
    "source": "oauth",
    # If the oauth call fails (expired token, offline), fall back to the estimate.
    "fallback_to_ccusage": True,
    "auto_refresh": True,
    # User-Agent for the oauth endpoint; null = auto-detect from `claude --version`.
    # The endpoint REQUIRES a "claude-code/<version>" UA.
    "user_agent": None,
    # The oauth endpoint is aggressively rate-limited: keep >= 180s.
    "poll_interval_sec": 300,

    # How to invoke ccusage (estimate source). Override with ["ccusage"] if global.
    "ccusage_cmd": ["npx", "-y", "ccusage@latest"],
    "ccusage_extra_args": ["--offline"],

    # "fixed" -> budget = plans[plan].{session,weekly}_tokens  (stable; tune to taste)
    # "auto"  -> budget = max of your historical finished blocks / weeks
    #            (NOTE: the current week is usually its own peak, so weekly
    #             tends to peg near 100% in auto mode)
    "budget_mode": "fixed",
    "plan": "max20x",
    "plans": {
        "pro":    {"session_tokens":   5_000_000, "weekly_tokens":  40_000_000},
        "max5x":  {"session_tokens":  25_000_000, "weekly_tokens": 200_000_000},
        "max20x": {"session_tokens": 100_000_000, "weekly_tokens": 800_000_000},
    },

    "thresholds": {"warn": 0.60, "crit": 0.85},
    "colors": {
        "ok":      [60, 200, 90],
        "warn":    [235, 185, 20],
        "crit":    [225, 55, 55],
        "track":   [70, 70, 70],
        "unknown": [130, 130, 130],
    },
    "show_center_text": True,
    "icon_size": 64,

    # Wide battery-style meter embedded in the taskbar, left of the
    # notification area. offset_x nudges it (in 96-dpi px; negative = left).
    "taskbar_band": True,
    "taskbar_band_offset_x": 0,
    # Reset column: "remaining" (2h13m / 3d4h), "clock" (14:30 / 10/9) or "off".
    "taskbar_band_reset": "remaining",
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_config() -> Dict[str, Any]:
    """Return DEFAULTS deep-merged with config.json (if present)."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            return _deep_merge(DEFAULTS, user or {})
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {CONFIG_PATH}: {exc}; using defaults")
    return copy.deepcopy(DEFAULTS)


def fixed_budgets(cfg: Dict[str, Any]) -> Dict[str, int]:
    """Resolve the session/weekly token budgets for the configured plan."""
    plan = cfg.get("plan", "max20x")
    plans = cfg.get("plans", {})
    chosen = plans.get(plan) or DEFAULTS["plans"]["max20x"]
    return {
        "session": int(chosen["session_tokens"]),
        "weekly": int(chosen["weekly_tokens"]),
    }


def write_default_config() -> None:
    """Write a fully-populated config.json (used for first-run scaffolding)."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(DEFAULTS, fh, indent=2)


if __name__ == "__main__":
    import pprint
    pprint.pprint(load_config())
