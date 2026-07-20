"""Config diagnostics and repair — the `doctor` tool.

Reads ``~/.aqua/config.json`` as raw JSON (not via ``Config.from_dict``) so it
can inspect and repair the file verbatim, including corrupt configs the
tolerant load path can only warn about.

Diagnoses three kinds of drift: orphan tool keys, entries matching the
shipped default, and unknown top-level keys. ``doctor`` is the only code
path that rewrites the file.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from .features import SHIPPED_DEFAULTS_ENABLED_TOOLS
from .storage import KNOWN_CONFIG_KEYS, Storage
from .tools import TOOLS

logger = logging.getLogger(__name__)

# Configs are a few KB; refuse anything wildly larger to avoid OOM on a corrupt file.
_MAX_CONFIG_BYTES = 5_000_000


def _is_prunable_default(name: str, value: Any) -> bool:
    """True if ``name=value`` is a known tool whose bool value equals its default.

    Uses the same default lookup as ``features.is_tool_enabled`` (``.get(name, True)``)
    so doctor's notion of "matches the default" can never drift from the value the
    runtime actually applies for an absent key. Pruning is sound only while a shipped
    default never changes for an *existing* tool: an entry equal to today's default is
    dropped, so a later default flip would silently change the effective value.
    """
    return (
        name in TOOLS
        and isinstance(value, bool)
        and value == SHIPPED_DEFAULTS_ENABLED_TOOLS.get(name, True)
    )


def run_doctor(storage: Storage | None = None, fix: bool = False) -> dict[str, Any]:
    """Diagnose (and with ``fix=True`` repair) the AQUA config file.

    Returns ``{config_path, healthy, fix_applied, findings, summary}``;
    ``healthy`` reflects the state *after* any repair.
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
        size = path.stat().st_size
        if size > _MAX_CONFIG_BYTES:
            raise ValueError(f"config file is implausibly large ({size} bytes)")
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

    # --- Unknown top-level keys (ignored at load; doctor removes them from disk) ---
    unknown_top = {k for k in raw if k not in KNOWN_CONFIG_KEYS}
    for key in sorted(unknown_top):
        findings.append({
            "type": "unknown_top_level_key",
            "key": key,
            "detail": f"Unknown top-level key {key!r} (ignored at load).",
            "action": "remove",
        })

    # --- enabled_tools analysis ---
    enabled = raw.get("enabled_tools")
    remove_tool_keys: set[str] = set()
    if isinstance(enabled, dict):
        for key, value in enabled.items():
            if key not in TOOLS:
                remove_tool_keys.add(key)
                # Show the value so --fix never *silently* drops a deliberate
                # `False` (a tool from a newer version this binary can't see
                # yet would fall back to its default once that version knows it).
                detail = (
                    f"{key!r}={value!r} is not a known tool (source of the "
                    "startup 'Unknown tool in enabled_tools' warning)."
                )
                findings.append({
                    "type": "orphan_tool",
                    "key": key,
                    "detail": detail,
                    "action": "remove",
                })
            elif _is_prunable_default(key, value):
                remove_tool_keys.add(key)
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
        # Apply exactly what was diagnosed above — no extra removals.
        cleaned: dict[str, Any] = {
            k: v for k, v in raw.items() if k not in unknown_top
        }
        if isinstance(enabled, dict):
            sparse = {k: v for k, v in enabled.items() if k not in remove_tool_keys}
            if sparse:
                cleaned["enabled_tools"] = sparse
            else:
                cleaned.pop("enabled_tools", None)
        storage.save_raw_config(cleaned)
        report["fix_applied"] = True
        # Only the (untouched) manual findings can remain after a repair.
        report["healthy"] = not manual
        counts = Counter(f["type"] for f in removable)
        repaired = (
            f"Repaired {path}: removed {counts['orphan_tool']} orphan tool key(s), "
            f"pruned {counts['matches_default']} default-matching entry(ies), "
            f"removed {counts['unknown_top_level_key']} unknown top-level key(s)."
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
