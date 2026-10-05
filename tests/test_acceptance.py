from __future__ import annotations

import unittest
from pathlib import Path

from commitment_control.acceptance import run as run_commitment
from cooperation_assurance.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation_count"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["input_sha256"]), 64)

    def test_commitment_control_acceptance(self) -> None:
        result = run_commitment(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["standby_winner"]["winner_resource_version_id"], "r-standby-1")
        self.assertEqual(
            result["standby_winner"]["offer_links"][0]["source_resource_version_id"],
            "r-quota-compliance",
        )
        self.assertTrue(result["audit"]["valid"])
        # 后续规则没有重排任何已核验交付与已形成阻断说明。
        self.assertTrue(any("需要重新承诺" in reason for reason in result["milestone_blocked_reasons"]))


if __name__ == "__main__":
    unittest.main()
