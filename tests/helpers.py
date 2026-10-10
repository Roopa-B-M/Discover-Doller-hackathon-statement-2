import json
import os

import seed_loader

REAL_SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seed.json")


def build_db(tmp_path, shifts, certs=None, name="test.db"):
    """Create an isolated DB with staff P (owner), C (claimer), Q (other), M (manager).

    shifts: list of (id, start, end, required_cert_or_None, owner_id)
    certs : {staff_id: [certs]}
    """
    certs = certs or {}
    staff = [{"id": i, "name": n, "role": r, "certifications": certs.get(i, [])}
             for i, n, r in [("P", "Pat", "STAFF"), ("C", "Cal", "STAFF"),
                             ("Q", "Quinn", "STAFF"), ("M", "Mia", "MANAGER")]]
    data = {"staff": staff, "shifts": [
        {"id": i, "start": s, "end": e, "required_certification": c, "assigned_to": o}
        for i, s, e, c, o in shifts]}
    seed = tmp_path / f"{name}.seed.json"
    seed.write_text(json.dumps(data))
    db = str(tmp_path / name)
    seed_loader.initialize(db, str(seed))
    return db


def seeded_db(tmp_path, name="real.db"):
    db = str(tmp_path / name)
    seed_loader.initialize(db, REAL_SEED)
    return db
