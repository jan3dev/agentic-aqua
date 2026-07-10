"""Tests for the `doctor` config diagnostic/repair tool."""

import json
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from aqua.cli.doctor import doctor as doctor_cmd
from aqua.cli.main import AquaContext
from aqua.doctor import run_doctor
from aqua.features import SHIPPED_DEFAULTS_ENABLED_TOOLS, load_config_with_merge
from aqua.storage import Config, Storage
from aqua.tools import TOOLS


@pytest.fixture
def temp_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Storage(Path(tmpdir))


def _write_raw(storage: Storage, data) -> None:
    """Write raw JSON to config.json, bypassing Config coercion."""
    storage.config_path.write_text(json.dumps(data))


def _pick_default_true_tool() -> str:
    for name, default in SHIPPED_DEFAULTS_ENABLED_TOOLS.items():
        if default is True:
            return name
    raise AssertionError("no enabled-by-default tool found")


def _pick_default_false_tool() -> str:
    for name, default in SHIPPED_DEFAULTS_ENABLED_TOOLS.items():
        if default is False:
            return name
    raise AssertionError("no disabled-by-default tool found")


# --- No config / healthy ---------------------------------------------------


def test_no_config_file_is_healthy(temp_storage):
    assert not temp_storage.config_path.exists()
    report = run_doctor(temp_storage, fix=False)
    assert report["healthy"] is True
    assert report["findings"] == []
    assert report["fix_applied"] is False


def test_sparse_config_is_healthy(temp_storage):
    """A config with only genuine overrides has nothing to fix."""
    tool = _pick_default_true_tool()
    _write_raw(temp_storage, {"enabled_tools": {tool: False}})  # override (default True)
    report = run_doctor(temp_storage, fix=False)
    assert report["healthy"] is True


# --- Orphan keys -----------------------------------------------------------


def test_orphan_key_reported_and_removed(temp_storage):
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True, "bogus_tool": False}})
    diag = run_doctor(temp_storage, fix=False)
    assert diag["healthy"] is False
    orphans = {f["key"] for f in diag["findings"] if f["type"] == "orphan_tool"}
    assert orphans == {"depix_swap", "bogus_tool"}
    # Diagnose is a dry-run: file untouched.
    assert "depix_swap" in temp_storage.config_path.read_text()

    fix = run_doctor(temp_storage, fix=True)
    assert fix["fix_applied"] is True
    on_disk = json.loads(temp_storage.config_path.read_text())
    assert "depix_swap" not in on_disk.get("enabled_tools", {})
    assert "bogus_tool" not in on_disk.get("enabled_tools", {})


def test_startup_warning_gone_after_fix(temp_storage, caplog):
    """The whole point: after --fix, no more 'Unknown tool' startup warning."""
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})
    run_doctor(temp_storage, fix=True)
    with caplog.at_level(logging.WARNING, logger="aqua.features"):
        load_config_with_merge(temp_storage)
    assert not any("depix_swap" in rec.message for rec in caplog.records)


# --- Default-matching pruning ---------------------------------------------


def test_default_matching_pruned(temp_storage):
    true_tool = _pick_default_true_tool()
    false_tool = _pick_default_false_tool()
    _write_raw(temp_storage, {
        "enabled_tools": {
            true_tool: True,     # equals default → prune
            false_tool: False,   # equals default → prune
        }
    })
    diag = run_doctor(temp_storage, fix=False)
    matches = {f["key"] for f in diag["findings"] if f["type"] == "matches_default"}
    assert matches == {true_tool, false_tool}

    run_doctor(temp_storage, fix=True)
    on_disk = json.loads(temp_storage.config_path.read_text())
    # Both pruned; enabled_tools now empty → omitted entirely (fully sparse).
    assert "enabled_tools" not in on_disk


def test_healthy_true_after_successful_fix(temp_storage):
    """After --fix removes all removable drift, the report is healthy again."""
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})
    report = run_doctor(temp_storage, fix=True)
    assert report["fix_applied"] is True
    assert report["healthy"] is True


def test_fix_with_manual_and_removable_stays_unhealthy(temp_storage):
    """A removable key is fixed, but a coexisting manual issue keeps it unhealthy."""
    # 'version' is removable; non-dict enabled_tools is a manual finding.
    _write_raw(temp_storage, {"version": 5, "enabled_tools": "bad"})
    report = run_doctor(temp_storage, fix=True)
    assert report["fix_applied"] is True
    assert report["healthy"] is False  # manual invalid_enabled_tools remains
    on_disk = json.loads(temp_storage.config_path.read_text())
    assert "version" not in on_disk  # removable part applied
    assert "re-run" not in report["summary"].lower()  # no misleading re-run hint


def test_genuine_override_preserved(temp_storage):
    """A value differing from the shipped default is a real choice — keep it."""
    true_tool = _pick_default_true_tool()
    _write_raw(temp_storage, {"enabled_tools": {true_tool: False}})  # override
    report = run_doctor(temp_storage, fix=True)
    # Nothing to remove → healthy, no write.
    assert report["healthy"] is True
    on_disk = json.loads(temp_storage.config_path.read_text())
    assert on_disk["enabled_tools"][true_tool] is False


def test_non_bool_entry_left_untouched(temp_storage):
    """Per spec: doctor does not normalize non-bool values for known tools."""
    tool = _pick_default_true_tool()
    _write_raw(temp_storage, {"enabled_tools": {tool: "yes", "depix_swap": True}})
    run_doctor(temp_storage, fix=True)
    on_disk = json.loads(temp_storage.config_path.read_text())
    # Orphan removed, but the non-bool known-tool entry is preserved verbatim.
    assert "depix_swap" not in on_disk.get("enabled_tools", {})
    assert on_disk["enabled_tools"][tool] == "yes"


# --- Unknown top-level keys (the crash path) -------------------------------


def test_unknown_top_level_key_reported_and_removed(temp_storage):
    _write_raw(temp_storage, {"version": 5, "network": "mainnet", "enabled_tools": {}})
    diag = run_doctor(temp_storage, fix=False)
    assert any(f["type"] == "unknown_top_level_key" and f["key"] == "version"
               for f in diag["findings"])

    run_doctor(temp_storage, fix=True)
    on_disk = json.loads(temp_storage.config_path.read_text())
    assert "version" not in on_disk
    assert on_disk["network"] == "mainnet"  # known keys preserved


def test_unknown_top_level_key_does_not_crash_load(temp_storage, caplog):
    """A stray top-level key must NOT crash config loading (it is dropped in
    memory), and doctor --fix removes it from disk."""
    _write_raw(temp_storage, {"version": 5, "enabled_tools": {"depix_swap": True}})
    # The load path is now tolerant (it used to raise TypeError on `version`).
    with caplog.at_level(logging.WARNING, logger="aqua.storage"):
        config = temp_storage.load_config()
    assert isinstance(config, Config)
    assert any("version" in rec.message for rec in caplog.records)
    # Read-only startup leaves it on disk until doctor cleans it.
    assert "version" in json.loads(temp_storage.config_path.read_text())

    run_doctor(temp_storage, fix=True)
    on_disk = json.loads(temp_storage.config_path.read_text())
    assert "version" not in on_disk
    assert "depix_swap" not in on_disk.get("enabled_tools", {})


# --- Corrupt / invalid configs (manual) ------------------------------------


def test_corrupt_json_flagged_manual_not_touched(temp_storage):
    temp_storage.config_path.write_text("{not valid json")
    before = temp_storage.config_path.read_text()
    report = run_doctor(temp_storage, fix=True)
    assert report["healthy"] is False
    assert any(f["action"] == "manual" for f in report["findings"])
    assert report["fix_applied"] is False
    assert temp_storage.config_path.read_text() == before  # untouched


def test_non_object_root_flagged_manual(temp_storage):
    _write_raw(temp_storage, ["lw_send"])
    report = run_doctor(temp_storage, fix=True)
    assert report["healthy"] is False
    assert any(f["type"] == "invalid_config_root" for f in report["findings"])
    assert report["fix_applied"] is False


# --- Permissions -----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_fix_writes_0600(temp_storage):
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})
    run_doctor(temp_storage, fix=True)
    mode = stat.S_IMODE(os.stat(temp_storage.config_path).st_mode)
    assert mode == 0o600


# --- MCP tool wiring -------------------------------------------------------


def test_doctor_is_registered_mcp_tool():
    assert "doctor" in TOOLS


def test_mcp_doctor_tool_diagnose_and_fix(temp_storage, monkeypatch):
    """The MCP `doctor` tool honors fix=False (default) vs fix=True."""
    import aqua.storage as storage_mod

    monkeypatch.setattr(storage_mod, "DEFAULT_DIR", temp_storage.base_dir)
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})

    diagnose = TOOLS["doctor"]()  # fix defaults to False
    assert diagnose["fix_applied"] is False
    assert "depix_swap" in temp_storage.config_path.read_text()

    fixed = TOOLS["doctor"](fix=True)
    assert fixed["fix_applied"] is True
    assert "depix_swap" not in json.loads(
        temp_storage.config_path.read_text()
    ).get("enabled_tools", {})


def test_mcp_doctor_string_fix_false_does_not_write(temp_storage, monkeypatch):
    """A non-compliant client passing fix as the string 'false' must NOT mutate."""
    import aqua.storage as storage_mod

    monkeypatch.setattr(storage_mod, "DEFAULT_DIR", temp_storage.base_dir)
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})

    report = TOOLS["doctor"](fix="false")
    assert report["fix_applied"] is False
    assert "depix_swap" in temp_storage.config_path.read_text()

    # ...but the string 'true' is honored.
    report_true = TOOLS["doctor"](fix="true")
    assert report_true["fix_applied"] is True


# --- CLI command -----------------------------------------------------------


def _invoke_doctor(temp_storage, monkeypatch, args):
    import aqua.storage as storage_mod

    monkeypatch.setattr(storage_mod, "DEFAULT_DIR", temp_storage.base_dir)
    runner = CliRunner()
    return runner.invoke(doctor_cmd, args, obj=AquaContext(fmt="json"))


def test_cli_doctor_diagnose_exit_1_when_issues(temp_storage, monkeypatch):
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})
    result = _invoke_doctor(temp_storage, monkeypatch, [])
    assert result.exit_code == 1
    assert "depix_swap" in result.output
    # Dry-run: not fixed.
    assert "depix_swap" in temp_storage.config_path.read_text()


def test_cli_doctor_fix_exit_0(temp_storage, monkeypatch):
    _write_raw(temp_storage, {"enabled_tools": {"depix_swap": True}})
    result = _invoke_doctor(temp_storage, monkeypatch, ["--fix"])
    assert result.exit_code == 0
    assert "depix_swap" not in json.loads(
        temp_storage.config_path.read_text()
    ).get("enabled_tools", {})


def test_cli_doctor_healthy_exit_0(temp_storage, monkeypatch):
    result = _invoke_doctor(temp_storage, monkeypatch, [])
    assert result.exit_code == 0
