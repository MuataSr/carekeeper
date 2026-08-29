"""Shared fixtures for the CareKeeper test suite.

The suite pins the locked design rules: the plain-language dictionary
gate, honesty-over-reassurance, consent-gated fixes, hash-chained
audits, and the regression fixes from the Aug 29 review session.
"""
import os
import sys

# make the repo root importable regardless of how pytest is invoked
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture
def make_tele():
    """Build a standard healthy telemetry dict; override any section.

    Usage: make_tele(patches={"pending": None}) or make_tele(disk={...}).
    """
    def _make(**overrides):
        tele = {
            "disk": {"worst_pct": 50},
            "smart": {"devices": [{"dev": "/dev/sda", "health": "ok",
                                   "reallocated_sectors": 0, "temp_c": 38}]},
            "backups": {"results": [{"path": "/home/x/backups", "present": True,
                                     "empty": False, "newest_age_hours": 2,
                                     "stale": False}]},
            "patches": {"pending": 0},
            "load": {"load1": 0.5, "uptime_hours": 100,
                     "ratio_vs_cores": 0.2, "state": "ok"},
        }
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(tele.get(k), dict):
                tele[k].update(v)
            else:
                tele[k] = v
        return tele
    return _make


@pytest.fixture
def tmp_cfg(tmp_path):
    """Minimal cfg pointing at tmp state/audit/backup dirs (no real FS)."""
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    state.mkdir()
    backups.mkdir()
    return {
        "device_id": "test-dev",
        "tier": "full",
        "audit": {"db": str(tmp_path / "audit.db")},
        "state_dir": str(state),
        "backup_dir": str(backups),
        "brain": {"url": "http://127.0.0.1:9/v1", "model": "test",
                  "timeout_s": 5},
        "telemetry": {"backups": [], "backup_max_age_hours": 48,
                      "disk_warn_pct": 85, "disk_crit_pct": 92,
                      "load_warn": 1.5, "load_crit": 3.0},
        "actions": {"enabled": ["rotate-logs"]},
    }
