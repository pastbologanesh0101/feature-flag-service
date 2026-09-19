# Feature Flag Service

[![tests](https://github.com/pastbologanesh0101/feature-flag-service/actions/workflows/tests.yml/badge.svg)](https://github.com/pastbologanesh0101/feature-flag-service/actions/workflows/tests.yml)

A small, self-contained feature-flag management service (Flask + SQLite)
with a Python client SDK that supports **local-cache evaluation** — the
pattern real feature-flag SDKs (LaunchDarkly, Unleash, etc.) use to avoid a
network round trip on every single flag check.

## Contents

- `app/` — Flask app factory, SQLite persistence, HTTP API.
- `ffs_core/evaluation.py` — the evaluation engine. Framework-agnostic and
  shared, unmodified, between the server and the client SDK, so local and
  remote evaluation can never disagree.
- `feature_flag_client.py` — the client SDK (`FeatureFlagClient`).
- `tests/` — unit + integration tests (`unittest`).
- `run.py` — dev server entrypoint.

## Data model

- **flag**: `key`, `name`, `description`, `enabled` (bool), `default_value`
  (JSON — `true`/`false` for a simple boolean flag, or any JSON value for a
  multivariate/variant flag, e.g. `"treatment-b"` or `{"limit": 10}`).
- **targeting_rule**: `flag_id`, `rule_type`
  (`user_id_list` | `percentage_rollout` | `attribute_match`), `rule_config`
  (JSON, shape depends on `rule_type` — see DSL below), `priority` (int,
  lower runs first).
- **evaluation_log**: `flag_id`, `user_id`, `result`, `evaluated_at` — a
  simple append-only log written on every `/evaluate` and `/evaluate/bulk`
  call, useful for basic analytics ("how often was this flag on for whom").

## Evaluation order (the important part)

For a given flag and user context (`user_id` + an arbitrary `attributes`
dict), `ffs_core.evaluation.evaluate_flag()` runs:

1. **Kill switch first.** If the flag is globally `enabled = False`, the
   result is always `False`, unconditionally — no rule, however specific,
   can override a disabled flag.
2. **Targeting rules, in ascending `priority` order.** The first rule that
   *matches* wins and its `result_value` is returned immediately:
   - `user_id_list` — explicit allow/deny list. Matches if `user_id` is in
     `rule_config["user_ids"]`; the `result_value` you configure decides
     whether being on the list means "on" (allow-list pattern) or "off"
     (deny-list pattern). Not on the list ⇒ no match, keep going.
   - `percentage_rollout` — deterministic hash-based bucketing (see below).
     Matches if the user's bucket is `< rule_config["percentage"]`.
   - `attribute_match` — a small comparison DSL over `attributes` (see
     below). Matches if the configured comparison is true.
3. **Fall through to `default_value`** if no rule matched.

The spec's "(1) user id lists, (2) percentage rollout, (3) attribute match"
grouping is the *conventional* priority ordering — in this implementation
priority is an explicit integer per rule so you can interleave rule types
however a real rollout plan needs (e.g. an emergency deny-list rule with
`priority=-100` that beats everything).

## Percentage rollout determinism

```python
bucket = int(hashlib.md5(f"{flag_key}:{user_id}".encode()).hexdigest()[:8], 16) % 100
matched = bucket < percentage
```

This is **hash-based, not random**: the bucket for a given `(flag_key,
user_id)` pair is a pure function with no hidden state, no clock, and no
stored assignment table.

- Same user + same flag ⇒ same bucket, every time, in every process,
  forever. Re-evaluating a user 1000 times gives 1000 identical results
  (`tests/test_evaluation.py::PercentageRolloutTests::
  test_percentage_rollout_is_deterministic_across_repeated_evaluations`).
- Across a large population of distinct user ids, buckets are
  (approximately) uniformly distributed 0–99, so a 20% rollout enables the
  flag for ~20% of users (`test_percentage_rollout_statistical_distribution`
  checks 5,000 synthetic users land within 16–24%).
- Changing the `flag_key` re-shuffles the bucket for the same user — this is
  intentional: it's what stops rollouts of different flags from always
  picking the same "lucky" early-adopter cohort.

## Targeting rule DSL

`rule_config` is JSON and its shape depends on `rule_type`:

```jsonc
// user_id_list
{ "mode": "allow", "user_ids": ["alice", "bob"], "result_value": true }
{ "mode": "deny",  "user_ids": ["mallory"],       "result_value": false }

// percentage_rollout
{ "percentage": 20, "result_value": true }

// attribute_match
{ "attribute": "country", "operator": "==", "value": "US", "result_value": true }
```

Supported `operator`s for `attribute_match`: `==`, `!=`, `in`, `not_in`,
`>`, `>=`, `<`, `<=`. `attribute` is looked up in the caller-supplied
`attributes` dict (e.g. `{"country": "US", "plan": "premium"}`); a missing
attribute never matches.

## Running the server

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py          # http://localhost:5000
```

### API

| Method | Path                     | Purpose                                   |
|--------|--------------------------|--------------------------------------------|
| GET    | `/flags`                 | List all flags with rules embedded (also what the SDK polls) |
| POST   | `/flags`                 | Create a flag |
| GET    | `/flags/<key>`           | Get one flag with its rules |
| PUT    | `/flags/<key>`           | Update a flag |
| DELETE | `/flags/<key>`           | Delete a flag (and its rules) |
| GET    | `/flags/<key>/rules`     | List a flag's targeting rules |
| POST   | `/flags/<key>/rules`     | Add a targeting rule |
| PUT    | `/rules/<id>`            | Update a targeting rule |
| DELETE | `/rules/<id>`            | Delete a targeting rule |
| POST   | `/evaluate`              | `{flag_key, user_id, attributes}` → single result |
| POST   | `/evaluate/bulk`         | `{flag_keys?, user_id, attributes}` → `{flag_key: result}` for every flag (all flags if `flag_keys` omitted) |

Example:

```bash
curl -X POST localhost:5000/flags -H 'Content-Type: application/json' \
  -d '{"key": "new-checkout", "name": "New checkout", "default_value": false}'

curl -X POST localhost:5000/flags/new-checkout/rules -H 'Content-Type: application/json' \
  -d '{"rule_type": "percentage_rollout", "priority": 0, "rule_config": {"percentage": 20, "result_value": true}}'

curl -X POST localhost:5000/evaluate -H 'Content-Type: application/json' \
  -d '{"flag_key": "new-checkout", "user_id": "user-42", "attributes": {"country": "US"}}'
```

## Client SDK usage

```python
from feature_flag_client import FeatureFlagClient

# Local-cache mode (default): fetches ALL flag configs once via /flags,
# then evaluates every subsequent check locally, with zero network round
# trips, using the exact same evaluation engine the server uses. A
# background thread re-syncs every `poll_interval` seconds.
client = FeatureFlagClient("http://localhost:5000", poll_interval=30)

result = client.evaluate("new-checkout", user_id="user-42", attributes={"country": "US"})
print(result)  # {"flag_key": "new-checkout", "value": True, "reason": "rule_match:percentage_rollout", "rule_id": 1}

all_results = client.evaluate_bulk(user_id="user-42", attributes={"country": "US"})

client.stop()  # stop the background poller on shutdown

# Remote mode: one HTTP call per evaluation, always fresh.
remote_client = FeatureFlagClient("http://localhost:5000", local_cache=False)
remote_client.evaluate("new-checkout", user_id="user-42")
```

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -p "test_*.py" -v
```

`create_app(testing=True)` gives every test an isolated in-memory SQLite
database, so tests never touch a real file or leak state between runs.
CI (`.github/workflows/tests.yml`) runs the full suite on Python 3.11 and
3.12 on every push and pull request.

## License

MIT — see [LICENSE](LICENSE).
