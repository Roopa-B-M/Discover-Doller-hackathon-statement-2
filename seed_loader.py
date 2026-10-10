"""Idempotent seed loading. The seed is loaded exactly once per database."""
import json
import os

from database import BASE_DIR, get_connection, init_schema, now_str, safe_rollback

SEED_PATH = os.path.join(BASE_DIR, "data", "seed.json")
SEED_FLAG = "seed_initialized"


class SeedFileNotFoundError(FileNotFoundError):
    pass


def read_seed(seed_path=None):
    path = os.path.abspath(seed_path or os.environ.get("SHIFTSWAP_SEED") or SEED_PATH)
    if not os.path.isfile(path):
        raise SeedFileNotFoundError(
            f"Seed file not found. Copy the provided SHiftSwap_seed.json to exactly: {path}"
        )
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "staff" not in data or "shifts" not in data:
        raise ValueError(f"Seed file {path} must contain top-level 'staff' and 'shifts' arrays.")
    return data


def initialize(db_path=None, seed_path=None):
    """Create the schema and load the seed ONLY if the DB was never initialized.

    Returns True if the seed was loaded by this call, False if the DB already
    held saved state. The decision is based on a meta flag, never on whether
    listings/history happen to be empty.
    """
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        if _is_initialized(conn):
            return False
        data = read_seed(seed_path)  # raises a clear error if the file is missing
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _is_initialized(conn):  # another process won the race
                conn.execute("ROLLBACK")
                return False
            for s in data["staff"]:
                conn.execute("INSERT INTO staff(id, name, role) VALUES (?,?,?)",
                             (s["id"], s["name"], s["role"]))
                for cert in s.get("certifications") or []:
                    conn.execute(
                        "INSERT INTO staff_certifications(staff_id, certification) VALUES (?,?)",
                        (s["id"], cert))
            for sh in data["shifts"]:
                conn.execute(
                    'INSERT INTO shifts(id, start, "end", required_certification, assigned_to) '
                    "VALUES (?,?,?,?,?)",
                    (sh["id"], sh["start"], sh["end"], sh.get("required_certification"),
                     sh["assigned_to"]))
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (SEED_FLAG, now_str()))
            conn.execute("COMMIT")
            return True
        except BaseException:
            safe_rollback(conn)
            raise
    finally:
        conn.close()


def _is_initialized(conn):
    row = conn.execute("SELECT 1 FROM meta WHERE key = ?", (SEED_FLAG,)).fetchone()
    return row is not None
