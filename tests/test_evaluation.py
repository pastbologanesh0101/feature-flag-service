"""Unit tests for the pure evaluation engine (ffs_core.evaluation)."""

import unittest

from ffs_core.evaluation import deterministic_bucket, evaluate_flag


def make_flag(**overrides):
    flag = {
        "key": "test-flag",
        "enabled": True,
        "default_value": False,
        "rules": [],
    }
    flag.update(overrides)
    return flag


class DisabledFlagTests(unittest.TestCase):
    def test_disabled_flag_always_returns_off_regardless_of_rules(self):
        flag = make_flag(
            enabled=False,
            default_value=True,
            rules=[
                {
                    "rule_type": "user_id_list",
                    "priority": 0,
                    "rule_config": {"user_ids": ["alice"], "result_value": True},
                }
            ],
        )
        result = evaluate_flag(flag, user_id="alice", attributes={})
        self.assertEqual(result["value"], False)
        self.assertEqual(result["reason"], "flag_disabled")


class UserIdListRuleTests(unittest.TestCase):
    def test_allow_list_includes_listed_users(self):
        flag = make_flag(
            default_value=False,
            rules=[
                {
                    "rule_type": "user_id_list",
                    "priority": 0,
                    "rule_config": {
                        "mode": "allow",
                        "user_ids": ["alice", "bob"],
                        "result_value": True,
                    },
                }
            ],
        )
        self.assertEqual(evaluate_flag(flag, user_id="alice")["value"], True)
        self.assertEqual(evaluate_flag(flag, user_id="bob")["value"], True)
        # Not in the list -> falls through to default_value.
        result = evaluate_flag(flag, user_id="carol")
        self.assertEqual(result["value"], False)
        self.assertEqual(result["reason"], "default")

    def test_deny_list_excludes_listed_users(self):
        flag = make_flag(
            default_value=True,
            rules=[
                {
                    "rule_type": "user_id_list",
                    "priority": 0,
                    "rule_config": {
                        "mode": "deny",
                        "user_ids": ["mallory"],
                        "result_value": False,
                    },
                }
            ],
        )
        # Denied user gets false, even though default_value is true.
        self.assertEqual(evaluate_flag(flag, user_id="mallory")["value"], False)
        # Everyone else falls through to the (true) default.
        self.assertEqual(evaluate_flag(flag, user_id="anyone-else")["value"], True)


class PercentageRolloutTests(unittest.TestCase):
    def test_deterministic_bucket_is_stable_across_calls(self):
        b1 = deterministic_bucket("my-flag", "user-42")
        b2 = deterministic_bucket("my-flag", "user-42")
        b3 = deterministic_bucket("my-flag", "user-42")
        self.assertEqual(b1, b2)
        self.assertEqual(b2, b3)
        self.assertTrue(0 <= b1 < 100)

    def test_percentage_rollout_is_deterministic_across_repeated_evaluations(self):
        flag = make_flag(
            default_value=False,
            rules=[
                {
                    "rule_type": "percentage_rollout",
                    "priority": 0,
                    "rule_config": {"percentage": 50, "result_value": True},
                }
            ],
        )
        first = evaluate_flag(flag, user_id="user-777")["value"]
        for _ in range(50):
            self.assertEqual(evaluate_flag(flag, user_id="user-777")["value"], first)

    def test_percentage_rollout_statistical_distribution(self):
        flag = make_flag(
            default_value=False,
            rules=[
                {
                    "rule_type": "percentage_rollout",
                    "priority": 0,
                    "rule_config": {"percentage": 20, "result_value": True},
                }
            ],
        )
        n = 5000
        enabled_count = 0
        for i in range(n):
            user_id = f"synthetic-user-{i}"
            if evaluate_flag(flag, user_id=user_id)["value"] is True:
                enabled_count += 1

        pct = (enabled_count / n) * 100
        # 20% target, allow a few percentage points of tolerance for hash noise.
        self.assertGreater(pct, 16)
        self.assertLess(pct, 24)

    def test_percentage_rollout_bucket_is_keyed_by_flag_and_user(self):
        # The bucket must be a function of BOTH flag_key and user_id: the
        # same user_id can land in different buckets for different flags,
        # but repeated calls for the same pair are always identical.
        self.assertEqual(
            deterministic_bucket("flag-a", "same-user"),
            deterministic_bucket("flag-a", "same-user"),
        )
        # Across a batch of users, at least one flag_key/user_id pair should
        # produce a different bucket between two distinct flag keys -- i.e.
        # the flag key actually participates in the hash (not ignored).
        buckets_differ = any(
            deterministic_bucket("flag-a", f"user-{i}")
            != deterministic_bucket("flag-b", f"user-{i}")
            for i in range(100)
        )
        self.assertTrue(buckets_differ)


class AttributeMatchRuleTests(unittest.TestCase):
    def test_attribute_match_rule_matches_when_condition_true(self):
        flag = make_flag(
            default_value=False,
            rules=[
                {
                    "rule_type": "attribute_match",
                    "priority": 0,
                    "rule_config": {
                        "attribute": "country",
                        "operator": "==",
                        "value": "US",
                        "result_value": True,
                    },
                }
            ],
        )
        result = evaluate_flag(flag, user_id="u1", attributes={"country": "US"})
        self.assertEqual(result["value"], True)
        self.assertEqual(result["reason"], "rule_match:attribute_match")

    def test_attribute_match_rule_excludes_when_condition_false(self):
        flag = make_flag(
            default_value="off-variant",
            rules=[
                {
                    "rule_type": "attribute_match",
                    "priority": 0,
                    "rule_config": {
                        "attribute": "plan",
                        "operator": "==",
                        "value": "premium",
                        "result_value": "premium-variant",
                    },
                }
            ],
        )
        result = evaluate_flag(flag, user_id="u1", attributes={"plan": "free"})
        self.assertEqual(result["value"], "off-variant")
        self.assertEqual(result["reason"], "default")

    def test_attribute_match_supports_in_operator(self):
        flag = make_flag(
            default_value=False,
            rules=[
                {
                    "rule_type": "attribute_match",
                    "priority": 0,
                    "rule_config": {
                        "attribute": "country",
                        "operator": "in",
                        "value": ["US", "CA"],
                        "result_value": True,
                    },
                }
            ],
        )
        self.assertTrue(evaluate_flag(flag, attributes={"country": "CA"})["value"])
        self.assertFalse(evaluate_flag(flag, attributes={"country": "FR"})["value"])


class RulePriorityAndFallthroughTests(unittest.TestCase):
    def test_rule_priority_order_is_respected(self):
        # Two rules could both match the same user; the lower-priority
        # (evaluated first) rule must win.
        flag = make_flag(
            default_value=False,
            rules=[
                {
                    "rule_type": "attribute_match",
                    "priority": 10,
                    "rule_config": {
                        "attribute": "country",
                        "operator": "==",
                        "value": "US",
                        "result_value": "from-attribute-rule",
                    },
                },
                {
                    "rule_type": "user_id_list",
                    "priority": 0,
                    "rule_config": {
                        "user_ids": ["alice"],
                        "result_value": "from-user-id-rule",
                    },
                },
            ],
        )
        result = evaluate_flag(flag, user_id="alice", attributes={"country": "US"})
        self.assertEqual(result["value"], "from-user-id-rule")
        self.assertEqual(result["reason"], "rule_match:user_id_list")

    def test_no_matching_rule_falls_through_to_default_value(self):
        flag = make_flag(
            default_value="fallback-variant",
            rules=[
                {
                    "rule_type": "user_id_list",
                    "priority": 0,
                    "rule_config": {"user_ids": ["someone-else"], "result_value": True},
                },
                {
                    "rule_type": "attribute_match",
                    "priority": 10,
                    "rule_config": {
                        "attribute": "country",
                        "operator": "==",
                        "value": "DE",
                        "result_value": True,
                    },
                },
            ],
        )
        result = evaluate_flag(flag, user_id="nobody", attributes={"country": "US"})
        self.assertEqual(result["value"], "fallback-variant")
        self.assertEqual(result["reason"], "default")


if __name__ == "__main__":
    unittest.main()
