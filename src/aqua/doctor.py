"""Config diagnostics and repair — the `doctor` tool.

Reads ``~/.aqua/config.json`` as raw JSON, never via ``Config.from_dict``
(which crashes on an unknown top-level key) — this lets doctor repair the
exact configs that would otherwise break config loading entirely.

Diagnoses (and with ``fix=True`` repairs) three kinds of drift: orphan tool
keys no longer in ``TOOLS``, entries matching the current shipped default
(prunable to keep the config sparse), and unknown top-level keys. Absent
keys use the shipped default (see ``features.is_tool_enabled``); ``doctor``
is the only code path that rewrites the file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import fields
from typing import Any

from .features import SHIPPED_DEFAULTS_ENABLED_TOOLS
from .storage import Config, Storage
from .tools import TOOLS

logger = logging.getLogger(__name__)

# Top-level keys the Config schema accepts. Anything else crashes from_dict.
_KNOWN_CONFIG_KEYS: frozenset[str] = frozenset(f.name for f in fields(Config))

# Configs are a few KB; refuse anything wildly larger to avoid OOM on a corrupt file.
_MAX_CONFIG_BYTES = 5_000_000


def _is_prunable_default(name: str, value: Any) -> bool:
    """True if ``name=value`` is a known tool whose bool value equals its default."""
    return (
        name in TOOLS
        and isinstance(value, bool)
        and value == SHIPPED_DEFAULTS_ENABLED_TOOLS.get(name)
    )


def run_doctor(storage: Storage | None = None, fix: bool = False) -> dict[str, Any]:
    """Diagnose (and with ``fix=True`` repair) the AQUA config file.

    Returns ``{config_path, healthy, fix_applied, findings, summary}``; each
    finding's ``action`` is "remove" (auto-fixable) or "manual". ``healthy``
    reflects the state *after* any repair.
    """
    if storage is None:
        storage = Storage()
    path = storage.config_path

    report: dict[str, Any] = {
        "config_path": str(path),
        "healthy": True,
        "fix_applied": False,
        "findings": [],
        "summary": "",
    }
    findings: list[dict[str, Any]] = report["findings"]

    if not path.exists():
        report["summary"] = (
            "No config file found; all tools use shipped defaults. Nothing to do."
        )
        return report

    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise ValueError(
                f"config file is implausibly large ({path.stat().st_size} bytes)"
            )
        with open(path) as f:
            # ValueError covers JSONDecodeError; RecursionError covers deeply-nested input.
            raw = json.load(f)
    except (OSError, ValueError, RecursionError) as exc:
        report["healthy"] = False
        findings.append({
            "type": "unreadable_config",
            "key": None,
            "detail": f"Could not read/parse {path}: {exc}",
            "action": "manual",
        })
        report["summary"] = (
            "Config file is unreadable or corrupt; fix or delete it by hand."
        )
        return report

    if not isinstance(raw, dict):
        report["healthy"] = False
        findings.append({
            "type": "invalid_config_root",
            "key": None,
            "detail": f"Config root must be a JSON object, got {type(raw).__name__}.",
            "action": "manual",
        })
        report["summary"] = "Config root is not an object; fix it by hand."
        return report

    # --- Unknown top-level keys (crash Config.from_dict) ---
    unknown_top = [k for k in raw if k not in _KNOWN_CONFIG_KEYS]
    for key in unknown_top:
        findings.append({
            "type": "unknown_top_level_key",
            "key": key,
            "detail": f"Unknown top-level key {key!r} (would crash config loading).",
            "action": "remove",
        })

    # --- enabled_tools analysis ---
    enabled = raw.get("enabled_tools")
    orphans: list[str] = []
    default_matches: list[str] = []
    if isinstance(enabled, dict):
        for key, value in enabled.items():
            if key not in TOOLS:
                orphans.append(key)
                findings.append({
                    "type": "orphan_tool",
                    "key": key,
                    "detail": (
                        f"{key!r} is not a known tool (source of the startup "
                        "'Unknown tool in enabled_tools' warning)."
                    ),
                    "action": "remove",
                })
            elif _is_prunable_default(key, value):
                default_matches.append(key)
                findings.append({
                    "type": "matches_default",
                    "key": key,
                    "detail": (
                        f"{key!r}={value} equals the shipped default; prunable to "
                        "keep the config sparse."
                    ),
                    "action": "remove",
                })
            # else: genuine override (bool != default) or non-bool value → leave it.
    elif enabled is not None:
        findings.append({
            "type": "invalid_enabled_tools",
            "key": "enabled_tools",
            "detail": "'enabled_tools' must be an object mapping tool name -> bool.",
            "action": "manual",
        })

    removable = [f for f in findings if f["action"] == "remove"]
    manual = [f for f in findings if f["action"] == "manual"]

    if fix and removable:
        cleaned: dict[str, Any] = {k: v for k, v in raw.items() if k in _KNOWN_CONFIG_KEYS}
        if isinstance(enabled, dict):
            sparse = {
                k: v
                for k, v in enabled.items()
                if k in TOOLS and not _is_prunable_default(k, v)
            }
            if sparse:
                cleaned["enabled_tools"] = sparse
            else:
                cleaned.pop("enabled_tools", None)
        storage.save_raw_config(cleaned)
        report["fix_applied"] = True
        # Only the (untouched) manual findings can remain after a repair.
        report["healthy"] = not manual
        repaired = (
            f"Repaired {path}: removed {len(orphans)} orphan tool key(s), "
            f"pruned {len(default_matches)} default-matching entry(ies), "
            f"removed {len(unknown_top)} unknown top-level key(s)."
        )
        report["summary"] = (
            f"{repaired} {len(manual)} issue(s) still need manual attention."
            if manual
            else repaired
        )
    elif not findings:
        report["healthy"] = True
        report["summary"] = "Config is healthy; nothing to fix."
    else:
        report["healthy"] = False
        if removable:
            report["summary"] = (
                f"Found {len(removable)} auto-fixable issue(s)"
                + (f" and {len(manual)} needing manual attention" if manual else "")
                + ". Run `aqua doctor --fix` to apply the fixable ones."
            )
        else:
            report["summary"] = (
                f"{len(manual)} issue(s) need manual attention; "
                "nothing can be auto-fixed."
            )

    return report
