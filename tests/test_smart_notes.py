import unittest
from unittest.mock import patch

import brain
import care_agent
import dashboard
import pytest


class SmartTelemetryTests(unittest.TestCase):
    @patch("care_agent.shutil.which", return_value="/usr/sbin/smartctl")
    @patch(
        "care_agent._run",
        side_effect=[("/dev/sda -d sat", 0), ("SMART overall-health: PASSED", 0), ("", 0)],
    )
    def test_available_smartctl_still_reports_drive_health(self, _run, _which):
        result = care_agent.telemetry_smart()
        self.assertEqual(result["devices"][0]["health"], "ok")
        self.assertNotIn("note", result)


class SmartPresentationTests(unittest.TestCase):
    def setUp(self):
        self.telemetry = {
            "disk": {"worst_pct": 20},
            "patches": {"pending": 0},
            "backups": {"results": []},
            "smart": {"devices": [], "note": "the drive health check tool isn't installed"},
        }

    def test_plain_report_explains_missing_tool_without_banned_terms(self):
        bullet = brain.plain_bullets({"family-computer": self.telemetry})[0]
        self.assertIn("tool isn't installed", bullet)
        self.assertEqual(brain.check_dictionary(bullet), [])

if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("render", [dashboard.status_of, dashboard.row_text])
@pytest.mark.parametrize("note", [
    "the drive health check tool isn't installed",
    "the drive health check couldn't run",
    "the drive health tool found no drives to check",
])
def test_dashboard_explains_unavailable_drive_check(make_tele, render, note):
    tele = make_tele(smart={"devices": [], "note": note})
    label, color = render(tele)
    assert label == note
    assert color == dashboard.GRAY
    assert brain.check_dictionary(label) == []


@pytest.mark.parametrize("render", [dashboard.status_of, dashboard.row_text])
def test_dashboard_problems_outrank_unavailable_drive_check(make_tele, render):
    tele = make_tele(
        smart={"devices": [], "note": "the drive health check couldn't run"},
        patches={"pending": 1},
    )
    label, color = render(tele)
    assert "1 update ready" in label
    assert color == dashboard.AMBER
