import json
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ffs_core.evaluation import evaluate_flag

from .db import get_db, get_lock

bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# Row <-> dict helpers
# ---------------------------------------------------------------------------

def _rule_row_to_dict(row):
    return {
        "id": row["id"],
        "flag_id": row["flag_id"],
        "rule_type": row["rule_type"],
        "rule_config": json.loads(row["rule_config"]),
        "priority": row["priority"],
    }


def _flag_row_to_dict(row, rules=None):
    return {
        "id": row["id"],
        "key": row["key"],
        "name": row["name"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "default_value": json.loads(row["default_value"]),
        "rules": rules if rules is not None else [],
    }


def _load_rules(db, flag_id):
    rows = db.execute(
        "SELECT * FROM targeting_rules WHERE flag_id = ? ORDER BY priority ASC",
        (flag_id,),
    ).fetchall()
    return [_rule_row_to_dict(r) for r in rows]


def _load_flag_by_key(db, key):
    row = db.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return _flag_row_to_dict(row, _load_rules(db, row["id"]))


def _log_evaluation(db, flag_id, user_id, result):
    db.execute(
        "INSERT INTO evaluation_log (flag_id, user_id, result, evaluated_at) "
        "VALUES (?, ?, ?, ?)",
        (
            flag_id,
            user_id,
            json.dumps(result["value"]),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


# ---------------------------------------------------------------------------
# Flag CRUD
# ---------------------------------------------------------------------------

@bp.get("/flags")
def list_flags():
    """List all flags with their targeting rules embedded.

    This is the endpoint the client SDK polls in local-cache mode: one
    call fetches every flag config needed to evaluate locally.
    """
    db = get_db()
    rows = db.execute("SELECT * FROM flags ORDER BY id ASC").fetchall()
    return jsonify([_flag_row_to_dict(r, _load_rules(db, r["id"])) for r in rows])


@bp.post("/flags")
def create_flag():
    payload = request.get_json(force=True) or {}
    key = payload.get("key")
    name = payload.get("name")
    if not key or not name:
        return jsonify({"error": "key and name are required"}), 400

    db = get_db()
    with get_lock():
        existing = db.execute("SELECT id FROM flags WHERE key = ?", (key,)).fetchone()
        if existing:
            return jsonify({"error": f"flag with key '{key}' already exists"}), 409

        db.execute(
            "INSERT INTO flags (key, name, description, enabled, default_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                key,
                name,
                payload.get("description", ""),
                1 if payload.get("enabled", True) else 0,
                json.dumps(payload.get("default_value", False)),
            ),
        )
        db.commit()

    return jsonify(_load_flag_by_key(db, key)), 201


@bp.get("/flags/<key>")
def get_flag(key):
    db = get_db()
    flag = _load_flag_by_key(db, key)
    if flag is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(flag)


@bp.put("/flags/<key>")
def update_flag(key):
    payload = request.get_json(force=True) or {}
    db = get_db()
    with get_lock():
        row = db.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404

        name = payload.get("name", row["name"])
        description = payload.get("description", row["description"])
        enabled = payload.get("enabled", bool(row["enabled"]))
        default_value = payload.get(
            "default_value", json.loads(row["default_value"])
        )

        db.execute(
            "UPDATE flags SET name = ?, description = ?, enabled = ?, "
            "default_value = ? WHERE key = ?",
            (name, description, 1 if enabled else 0, json.dumps(default_value), key),
        )
        db.commit()

    return jsonify(_load_flag_by_key(db, key))


@bp.delete("/flags/<key>")
def delete_flag(key):
    db = get_db()
    with get_lock():
        row = db.execute("SELECT id FROM flags WHERE key = ?", (key,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        db.execute("DELETE FROM targeting_rules WHERE flag_id = ?", (row["id"],))
        db.execute("DELETE FROM flags WHERE id = ?", (row["id"],))
        db.commit()
    return "", 204


# ---------------------------------------------------------------------------
# Targeting rule CRUD
# ---------------------------------------------------------------------------

@bp.get("/flags/<key>/rules")
def list_rules(key):
    db = get_db()
    row = db.execute("SELECT id FROM flags WHERE key = ?", (key,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_load_rules(db, row["id"]))


@bp.post("/flags/<key>/rules")
def create_rule(key):
    payload = request.get_json(force=True) or {}
    rule_type = payload.get("rule_type")
    if rule_type not in {"user_id_list", "percentage_rollout", "attribute_match"}:
        return jsonify({"error": "invalid rule_type"}), 400

    db = get_db()
    with get_lock():
        row = db.execute("SELECT id FROM flags WHERE key = ?", (key,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404

        cur = db.execute(
            "INSERT INTO targeting_rules (flag_id, rule_type, rule_config, priority) "
            "VALUES (?, ?, ?, ?)",
            (
                row["id"],
                rule_type,
                json.dumps(payload.get("rule_config", {})),
                payload.get("priority", 0),
            ),
        )
        db.commit()
        rule_id = cur.lastrowid

    rule_row = db.execute(
        "SELECT * FROM targeting_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    return jsonify(_rule_row_to_dict(rule_row)), 201


@bp.put("/rules/<int:rule_id>")
def update_rule(rule_id):
    payload = request.get_json(force=True) or {}
    db = get_db()
    with get_lock():
        row = db.execute(
            "SELECT * FROM targeting_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404

        rule_type = payload.get("rule_type", row["rule_type"])
        rule_config = payload.get("rule_config", json.loads(row["rule_config"]))
        priority = payload.get("priority", row["priority"])

        db.execute(
            "UPDATE targeting_rules SET rule_type = ?, rule_config = ?, "
            "priority = ? WHERE id = ?",
            (rule_type, json.dumps(rule_config), priority, rule_id),
        )
        db.commit()

    row = db.execute("SELECT * FROM targeting_rules WHERE id = ?", (rule_id,)).fetchone()
    return jsonify(_rule_row_to_dict(row))


@bp.delete("/rules/<int:rule_id>")
def delete_rule(rule_id):
    db = get_db()
    with get_lock():
        row = db.execute(
            "SELECT id FROM targeting_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        db.execute("DELETE FROM targeting_rules WHERE id = ?", (rule_id,))
        db.commit()
    return "", 204


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@bp.post("/evaluate")
def evaluate():
    payload = request.get_json(force=True) or {}
    flag_key = payload.get("flag_key")
    user_id = payload.get("user_id")
    attributes = payload.get("attributes", {})

    if not flag_key:
        return jsonify({"error": "flag_key is required"}), 400

    db = get_db()
    flag = _load_flag_by_key(db, flag_key)
    if flag is None:
        return jsonify({"error": f"flag '{flag_key}' not found"}), 404

    result = evaluate_flag(flag, user_id=user_id, attributes=attributes)

    with get_lock():
        _log_evaluation(db, flag["id"], user_id, result)
        db.commit()

    return jsonify(result)


@bp.post("/evaluate/bulk")
def evaluate_bulk():
    """Evaluate multiple flags for one user context in a single call.

    Body: {"flag_keys": [...], "user_id": "...", "attributes": {...}}
    If ``flag_keys`` is omitted, every known flag is evaluated -- this is
    exactly the "SDK startup" use case where a client wants everything
    relevant to a user in one round trip.
    """
    payload = request.get_json(force=True) or {}
    user_id = payload.get("user_id")
    attributes = payload.get("attributes", {})
    flag_keys = payload.get("flag_keys")

    db = get_db()

    if flag_keys is None:
        rows = db.execute("SELECT key FROM flags").fetchall()
        flag_keys = [r["key"] for r in rows]

    results = {}
    with get_lock():
        for flag_key in flag_keys:
            flag = _load_flag_by_key(db, flag_key)
            if flag is None:
                results[flag_key] = {
                    "flag_key": flag_key,
                    "value": None,
                    "reason": "flag_not_found",
                }
                continue
            result = evaluate_flag(flag, user_id=user_id, attributes=attributes)
            _log_evaluation(db, flag["id"], user_id, result)
            results[flag_key] = result
        db.commit()

    return jsonify(results)
