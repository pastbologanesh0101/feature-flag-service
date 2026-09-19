"""Integration tests for the Flask HTTP API, using an in-memory SQLite DB."""

import unittest

from app import create_app


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["ffs_db_conn"].close()

    def create_flag(self, key, name=None, enabled=True, default_value=False):
        resp = self.client.post(
            "/flags",
            json={
                "key": key,
                "name": name or key,
                "enabled": enabled,
                "default_value": default_value,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def add_rule(self, key, rule_type, rule_config, priority=0):
        resp = self.client.post(
            f"/flags/{key}/rules",
            json={"rule_type": rule_type, "rule_config": rule_config, "priority": priority},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_create_and_get_flag(self):
        self.create_flag("checkout-v2", default_value=False)
        resp = self.client.get("/flags/checkout-v2")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["key"], "checkout-v2")
        self.assertEqual(body["enabled"], True)
        self.assertEqual(body["rules"], [])

    def test_duplicate_flag_key_is_rejected(self):
        self.create_flag("dup-flag")
        resp = self.client.post(
            "/flags", json={"key": "dup-flag", "name": "Dup", "default_value": False}
        )
        self.assertEqual(resp.status_code, 409)

    def test_update_and_delete_flag(self):
        self.create_flag("temp-flag", enabled=True)
        resp = self.client.put("/flags/temp-flag", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["enabled"])

        resp = self.client.delete("/flags/temp-flag")
        self.assertEqual(resp.status_code, 204)
        resp = self.client.get("/flags/temp-flag")
        self.assertEqual(resp.status_code, 404)

    def test_targeting_rule_crud_and_effect_on_evaluation(self):
        self.create_flag("beta-feature", default_value=False)
        rule = self.add_rule(
            "beta-feature",
            "user_id_list",
            {"user_ids": ["alice"], "result_value": True},
            priority=0,
        )

        result = self.client.post(
            "/evaluate", json={"flag_key": "beta-feature", "user_id": "alice"}
        ).get_json()
        self.assertEqual(result["value"], True)

        # Update the rule to a different user list.
        resp = self.client.put(
            f"/rules/{rule['id']}",
            json={"rule_config": {"user_ids": ["bob"], "result_value": True}},
        )
        self.assertEqual(resp.status_code, 200)

        result = self.client.post(
            "/evaluate", json={"flag_key": "beta-feature", "user_id": "alice"}
        ).get_json()
        self.assertEqual(result["value"], False)  # alice no longer in the list

        resp = self.client.delete(f"/rules/{rule['id']}")
        self.assertEqual(resp.status_code, 204)
        resp = self.client.get("/flags/beta-feature/rules")
        self.assertEqual(resp.get_json(), [])

    def test_evaluate_endpoint_basic(self):
        self.create_flag("simple-flag", default_value=True)
        resp = self.client.post("/evaluate", json={"flag_key": "simple-flag", "user_id": "u1"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["value"], True)
        self.assertEqual(body["reason"], "default")

    def test_evaluate_disabled_flag_via_api(self):
        self.create_flag("off-flag", enabled=False, default_value=True)
        resp = self.client.post("/evaluate", json={"flag_key": "off-flag", "user_id": "u1"})
        self.assertEqual(resp.get_json()["value"], False)

    def test_evaluate_unknown_flag_returns_404(self):
        resp = self.client.post("/evaluate", json={"flag_key": "does-not-exist"})
        self.assertEqual(resp.status_code, 404)

    def test_bulk_evaluate_endpoint_returns_all_requested_flags(self):
        self.create_flag("flag-a", default_value=True)
        self.create_flag("flag-b", default_value=False)
        self.add_rule(
            "flag-b",
            "user_id_list",
            {"user_ids": ["u1"], "result_value": True},
        )

        resp = self.client.post(
            "/evaluate/bulk",
            json={"flag_keys": ["flag-a", "flag-b"], "user_id": "u1"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["flag-a"]["value"], True)
        self.assertEqual(body["flag-b"]["value"], True)

    def test_bulk_evaluate_defaults_to_all_flags_when_keys_omitted(self):
        self.create_flag("only-flag", default_value=True)
        resp = self.client.post("/evaluate/bulk", json={"user_id": "u1"})
        body = resp.get_json()
        self.assertIn("only-flag", body)
        self.assertEqual(body["only-flag"]["value"], True)

    def test_list_flags_embeds_rules_for_sdk_sync(self):
        self.create_flag("sync-flag", default_value=False)
        self.add_rule(
            "sync-flag",
            "percentage_rollout",
            {"percentage": 30, "result_value": True},
        )
        resp = self.client.get("/flags")
        body = resp.get_json()
        self.assertEqual(len(body), 1)
        self.assertEqual(len(body[0]["rules"]), 1)
        self.assertEqual(body[0]["rules"][0]["rule_type"], "percentage_rollout")


if __name__ == "__main__":
    unittest.main()
