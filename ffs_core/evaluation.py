"""Deterministic feature-flag evaluation engine.

Evaluation order for a given flag + user context:

  1. If the flag is globally disabled -> always return the "off" result
     (``False``), regardless of any targeting rules.
  2. Otherwise, walk the flag's targeting rules in ascending ``priority``
     order (lower number = evaluated first). The first rule that *matches*
     wins and its configured ``result_value`` is returned immediately.
       - ``user_id_list``       : explicit allow/deny list of user ids.
       - ``percentage_rollout`` : deterministic hash-based bucketing.
       - ``attribute_match``    : small comparison DSL over user attributes.
  3. If no rule matches, fall through to the flag's ``default_value``.

Percentage rollout determinism
-------------------------------
The rollout bucket for a (flag_key, user_id) pair is derived from an MD5
hash of ``"{flag_key}:{user_id}"``. Hashing (rather than ``random``) means
the same user always lands in the same bucket for the same flag, on every
node, in every process, forever -- with no state that needs to be stored or
synchronized. Changing the flag_key re-shuffles the population, which is
the standard, intentional behavior of hash-bucketed rollouts.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

# Rule types, in the order the spec describes them. Actual evaluation order
# is driven by each rule's explicit ``priority`` field (ascending), but by
# convention seed/demo data assigns priorities in this order.
USER_ID_LIST = "user_id_list"
PERCENTAGE_ROLLOUT = "percentage_rollout"
ATTRIBUTE_MATCH = "attribute_match"

VALID_RULE_TYPES = {USER_ID_LIST, PERCENTAGE_ROLLOUT, ATTRIBUTE_MATCH}

_SUPPORTED_OPERATORS = {"==", "!=", "in", "not_in", ">", ">=", "<", "<="}


def deterministic_bucket(flag_key: str, user_id: str) -> int:
    """Return a stable integer in [0, 100) for (flag_key, user_id).

    Deterministic: calling this any number of times, in any process, for the
    same inputs always returns the same value (no randomness, no clock).
    """
    digest = hashlib.md5(f"{flag_key}:{user_id}".encode("utf-8")).hexdigest()
    # Use the first 8 hex chars (32 bits) as an unsigned integer.
    return int(digest[:8], 16) % 100


def _compare(attr_value: Any, operator: str, target: Any) -> bool:
    if operator not in _SUPPORTED_OPERATORS:
        raise ValueError(f"Unsupported operator: {operator!r}")
    if attr_value is None:
        return False
    try:
        if operator == "==":
            return attr_value == target
        if operator == "!=":
            return attr_value != target
        if operator == "in":
            return attr_value in target
        if operator == "not_in":
            return attr_value not in target
        if operator == ">":
            return attr_value > target
        if operator == ">=":
            return attr_value >= target
        if operator == "<":
            return attr_value < target
        if operator == "<=":
            return attr_value <= target
    except TypeError:
        return False
    return False  # pragma: no cover - unreachable, kept for safety


def _rule_matches(
    rule: Dict[str, Any],
    flag_key: str,
    user_id: Optional[str],
    attributes: Dict[str, Any],
):
    """Return (matched: bool, value: Any) for a single targeting rule."""
    rule_type = rule["rule_type"]
    config = rule.get("rule_config") or {}

    if rule_type == USER_ID_LIST:
        user_ids = set(config.get("user_ids", []))
        if user_id is not None and user_id in user_ids:
            return True, config.get("result_value")
        return False, None

    if rule_type == PERCENTAGE_ROLLOUT:
        if user_id is None:
            return False, None
        percentage = config.get("percentage", 0)
        bucket = deterministic_bucket(flag_key, user_id)
        if bucket < percentage:
            return True, config.get("result_value")
        return False, None

    if rule_type == ATTRIBUTE_MATCH:
        attribute = config.get("attribute")
        operator = config.get("operator", "==")
        target = config.get("value")
        attr_value = attributes.get(attribute)
        if _compare(attr_value, operator, target):
            return True, config.get("result_value")
        return False, None

    raise ValueError(f"Unknown rule_type: {rule_type!r}")


def evaluate_flag(
    flag: Dict[str, Any],
    user_id: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate ``flag`` for a given user context.

    ``flag`` is a plain dict shaped like::

        {
          "key": "new-checkout",
          "enabled": True,
          "default_value": False,
          "rules": [
             {"id": 1, "rule_type": "user_id_list", "priority": 0,
              "rule_config": {"user_ids": ["u1"], "result_value": True}},
             ...
          ],
        }

    Returns a dict: ``{"flag_key", "value", "reason", "rule_id"?}``.
    """
    attributes = attributes or {}

    if not flag.get("enabled", True):
        return {"flag_key": flag["key"], "value": False, "reason": "flag_disabled"}

    rules: List[Dict[str, Any]] = sorted(
        flag.get("rules", []), key=lambda r: r.get("priority", 0)
    )

    for rule in rules:
        matched, value = _rule_matches(rule, flag["key"], user_id, attributes)
        if matched:
            return {
                "flag_key": flag["key"],
                "value": value,
                "reason": f"rule_match:{rule['rule_type']}",
                "rule_id": rule.get("id"),
            }

    return {
        "flag_key": flag["key"],
        "value": flag.get("default_value"),
        "reason": "default",
    }
