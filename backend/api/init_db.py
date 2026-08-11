"""
Creates every table defined in models.py against whatever DATABASE_URL
points at. Idempotent — SQLAlchemy's create_all() skips tables that
already exist, so this is safe to run more than once (e.g. after
adding a new model).

This is NOT a migration tool — it has no concept of altering an
existing table's columns. Fine for getting the schema stood up for
the first time; once there's real data in production, switch to
Alembic (or similar) for anything beyond adding a brand-new table.

Run once against the live Supabase DB before anything else in the
checklist:

    export DATABASE_URL=postgresql://...
    python api/init_db.py
"""
import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from database import engine   # noqa: E402
from models import Base        # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("init_db")

if __name__ == "__main__":
    log.info("Creating tables (existing ones are left untouched)...")
    Base.metadata.create_all(bind=engine)
    table_names = sorted(Base.metadata.tables.keys())
    log.info("Done. Tables now present: %s", ", ".join(table_names))
