import unittest
from unittest.mock import patch

import brain
import care_agent


class SmartTelemetryTests(unittest.TestCase):
    @patch("care_agent.shutil.which", return_value=None)
    def test_missing_smartctl_has_actionable_note(self, _which):
        self.assertEqual(
            care_agent.telemetry_smart(),
            {"devices": [], "note": "smartctl not installed"},
        )

    @patch("care_agent._run", return_value=("", 0))
    @patch("care_agent.shutil.which", return_value="/usr/sbin/smartctl")
    def test_no_capable_drives_has_distinct_note(self, _which, _run):
        self.assertEqual(
            care_agent.telemetry_smart(),
            {"devices": [], "note": "no SMART-capable drives"},
        )

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
            "smart": {"devices": [], "note": "smartctl not installed"},
        }

    def test_plain_report_explains_missing_tool_without_banned_terms(self):
        bullet = brain.plain_bullets({"family-computer": self.telemetry})[0]
        self.assertIn("tool isn't installed", bullet)
        self.assertEqual(brain.check_dictionary(bullet), [])

if __name__ == "__main__":
    unittest.main()
