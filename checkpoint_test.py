"""Persistent, OFFLINE test of checkpoint/resume for numeric record IDs.

THIS SCRIPT MAKES NO NETWORK REQUESTS TO MCSO. It uses a synthetic provider
and stores only the progress state in local JSON or private Cloudflare R2.
It deliberately does not update the jail-booking archive or public website.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SEED = 1658164  # User's observed newest booking ID, unverified independently
DEFAULT_KEY = "multco/checkpoint-demo.json"  # Not the production archive.json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_state(seed: int) -> dict:
    if seed < 1:
        raise ValueError("Seed must be a positive number")
    return {
        "schema": 1,
        "test_mode": "OFFLINE_SIMULATION_ONLY",
        "seed_id": seed,
        "highest_confirmed_id": seed,
        "next_candidate": seed + 1,
        "consecutive_missing": 0,
        "run_count": 0,
        "last_checked_id": None,
        "last_run_utc": None,
        "confirmed_test_ids": [],
    }


def validate_state(state: dict, seed: int) -> None:
    if not isinstance(state, dict) or state.get("schema") != 1 or state.get("test_mode") != "OFFLINE_SIMULATION_ONLY":
        raise ValueError("Unexpected checkpoint format; refusing to overwrite it")
    if state.get("seed_id") != seed:
        raise ValueError("Existing checkpoint uses another seed; refusing to overwrite it")
    if (not isinstance(state.get("next_candidate"), int)
        or not isinstance(state.get("highest_confirmed_id"), int)
        or state["next_candidate"] <= seed
        or state["highest_confirmed_id"] < seed):
        raise ValueError("Invalid existing checkpoint; refusing to overwrite it")


class LocalStateStore:
    def __init__(self, file: str):
        self.path = Path(file)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class S3StateStore:
    def __init__(self, key: str = DEFAULT_KEY):
        missing = [k for k in (
            "ARCHIVE_BUCKET", "ARCHIVE_ENDPOINT_URL", "ARCHIVE_ACCESS_KEY_ID", "ARCHIVE_SECRET_ACCESS_KEY"
        ) if not os.getenv(k)]
        if missing:
            raise ValueError("Missing Cloudflare R2 GitHub secrets: " + ", ".join(missing))
        import boto3
        self.bucket = os.environ["ARCHIVE_BUCKET"]
        self.key = key
        self.s3 = boto3.client(
            "s3", endpoint_url=os.environ["ARCHIVE_ENDPOINT_URL"],
            aws_access_key_id=os.environ["ARCHIVE_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["ARCHIVE_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    def load(self) -> dict | None:
        from botocore.exceptions import ClientError
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.key)
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def save(self, state: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket, Key=self.key,
            Body=json.dumps(state, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )


def fake_status(candidate_id: int, run_number: int) -> str:
    """Synthetic records; never derived from a real person's booking.

    Run 1: 1658165 and 1658167 exist; then five misses.
    Run 2+: 1658170 becomes visible late, showing why we recheck gaps.
    """
    synthetic_found = {1658165, 1658167}
    if run_number >= 2:
        synthetic_found.add(1658170)
    return "FOUND" if candidate_id in synthetic_found else "NOT_FOUND"


def apply_result(state: dict, candidate_id: int, result: str, threshold: int) -> tuple[dict, bool]:
    """Pure transition. Return updated state and whether to stop this run.

    A transport error or CAPTCHA does NOT count as a missing booking ID.
    """
    if candidate_id != state["next_candidate"]:
        raise ValueError("Out-of-order candidate")
    if threshold < 1:
        raise ValueError("Invalid threshold")
    out = deepcopy(state)
    if result == "FOUND":
        out["highest_confirmed_id"] = max(out["highest_confirmed_id"], candidate_id)
        out["confirmed_test_ids"] = sorted(set(out.get("confirmed_test_ids", []) + [candidate_id]))
        out["consecutive_missing"] = 0
        out["next_candidate"] = candidate_id + 1
        stop = False
    elif result == "NOT_FOUND":
        out["consecutive_missing"] += 1
        out["next_candidate"] = candidate_id + 1
        stop = out["consecutive_missing"] >= threshold
        if stop:
            # Recheck the unconfirmed end range on the next run. Do not treat 5
            # missing IDs as a proven end of all records.
            out["next_candidate"] = out["highest_confirmed_id"] + 1
            out["consecutive_missing"] = 0
    elif result in ("ERROR", "BLOCKED"):
        # No advance: a failed request is not a confirmed missing ID.
        stop = True
    else:
        raise ValueError("Unknown result " + repr(result))
    out["last_checked_id"] = candidate_id
    return out, stop


def run_simulation(store, seed: int = DEFAULT_SEED, threshold: int = 5, max_requests: int = 100) -> dict:
    state = store.load()
    if state is None:
        state = new_state(seed)
        print(f"New PRIVATE test checkpoint: after ID {seed}")
    else:
        validate_state(state, seed)
        print(f"Resuming PRIVATE test checkpoint from ID {state['next_candidate']}")
    state["run_count"] += 1
    state["last_run_utc"] = utc_now()
    store.save(state)
    print(f"OFFLINE SIMULATION ONLY | run={state['run_count']} | stop_after_missing={threshold}")
    print("No MCSO requests; no real booking records; no changes to live website/archive")
    for _ in range(max_requests):
        candidate_id = state["next_candidate"]
        result = fake_status(candidate_id, state["run_count"])
        state, stop = apply_result(state, candidate_id, result, threshold)
        store.save(state)  # Durable per-candidate checkpoint for interrupted runs
        print(f"SIMULATED ID {candidate_id}: {result}")
        if stop:
            break
    else:
        print("Safety request ceiling reached; checkpoint saved")
    print(f"Checkpoint saved: highest_mock_found={state['highest_confirmed_id']} next_candidate={state['next_candidate']} runs={state['run_count']}")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("local", "s3"), default="local")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--missing-threshold", type=int, default=5)
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--file", default=".private/checkpoint-demo.json")
    args = parser.parse_args()
    if args.max_requests < 1 or args.max_requests > 1000 or args.missing_threshold < 1 or args.missing_threshold > args.max_requests:
        parser.error("Invalid request limit or missing threshold")
    store = S3StateStore() if args.backend == "s3" else LocalStateStore(args.file)
    run_simulation(store, args.seed, args.missing_threshold, args.max_requests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
