"""Tests for the feature_flag_client SDK, in local-cache mode.

These tests use a fake `requests`-like session so no real network calls or
live server are involved, and so the exact fetch count can be verified.
"""

import unittest
from unittest.mock import MagicMock

from feature_flag_client import FeatureFlagClient


def _fake_response(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.side_effect = None
    return resp


SAMPLE_FLAGS = [
    {
        "id": 1,
        "key": "allow-flag",
        "enabled": True,
        "default_value": False,
        "rules": [
            {
                "id": 1,
                "rule_type": "user_id_list",
                "priority": 0,
                "rule_config": {"user_ids": ["alice"], "result_value": True},
            }
        ],
    },
    {
        "id": 2,
        "key": "rollout-flag",
        "enabled": True,
        "default_value": False,
        "rules": [
            {
                "id": 2,
                "rule_type": "percentage_rollout",
                "priority": 0,
                "rule_config": {"percentage": 100, "result_value": True},
            }
        ],
    },
]


class LocalCacheSdkTests(unittest.TestCase):
    def _make_client(self):
        session = MagicMock()
        session.get.return_value = _fake_response(SAMPLE_FLAGS)
        client = FeatureFlagClient(
            "http://example.invalid",
            local_cache=True,
            poll_interval=None,  # disable background polling for a deterministic test
            session=session,
        )
        return client, session

    def test_initial_sync_happens_exactly_once_on_construction(self):
        client, session = self._make_client()
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(client.fetch_count, 1)

    def test_evaluate_after_sync_requires_no_further_network_calls(self):
        client, session = self._make_client()

        for _ in range(25):
            client.evaluate("allow-flag", user_id="alice")
            client.evaluate("allow-flag", user_id="carol")
            client.evaluate("rollout-flag", user_id="bob")

        # Only the single constructor-time sync should have hit the network.
        self.assertEqual(session.get.call_count, 1)
        session.post.assert_not_called()

    def test_local_evaluation_matches_expected_rule_outcome(self):
        client, _ = self._make_client()
        self.assertEqual(client.evaluate("allow-flag", user_id="alice")["value"], True)
        self.assertEqual(client.evaluate("allow-flag", user_id="carol")["value"], False)
        # 100% rollout -> always true, deterministically.
        self.assertEqual(client.evaluate("rollout-flag", user_id="anyone")["value"], True)

    def test_evaluate_bulk_local_mode_returns_all_flags_without_network_calls(self):
        client, session = self._make_client()
        results = client.evaluate_bulk(user_id="alice")
        self.assertEqual(results["allow-flag"]["value"], True)
        self.assertEqual(results["rollout-flag"]["value"], True)
        self.assertEqual(session.get.call_count, 1)

    def test_unknown_flag_key_returns_not_found_locally(self):
        client, _ = self._make_client()
        result = client.evaluate("no-such-flag", user_id="alice")
        self.assertEqual(result["reason"], "flag_not_found")

    def test_manual_sync_refreshes_cache_and_increments_fetch_count(self):
        client, session = self._make_client()
        updated_flags = [dict(SAMPLE_FLAGS[0], enabled=False)]
        session.get.return_value = _fake_response(updated_flags)

        client.sync()

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(client.fetch_count, 2)
        # Flag is now disabled -> always off, regardless of its rules.
        self.assertEqual(client.evaluate("allow-flag", user_id="alice")["value"], False)


class RemoteModeSdkTests(unittest.TestCase):
    def test_remote_mode_posts_to_evaluate_endpoint(self):
        session = MagicMock()
        session.post.return_value = _fake_response(
            {"flag_key": "some-flag", "value": True, "reason": "default"}
        )
        client = FeatureFlagClient(
            "http://example.invalid", local_cache=False, session=session
        )
        result = client.evaluate("some-flag", user_id="u1")
        self.assertEqual(result["value"], True)
        session.post.assert_called_once()
        session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
