"""
Regression test for the NGX bulletin parser — run this after any change
to ngx_scraper.py's parsing logic, before trusting it against a live
bulletin. No pytest dependency; plain asserts so it runs anywhere:

    python ingestion/test_ngx_parser.py

Fixtures are real text pulled from the 20-04-2026 NGX Daily Official
List (Equities), covering the edge cases that broke earlier parser
attempts:
- same-line vs. 2-line vs. 3-line wrapped company names
- numbers concatenated with no separator (e.g. "19.423.00")
- a date glued directly onto the preceding price with no space (AVAIF)
- duplicate ticker rows (PRESCO appears twice with different Div Sc)
- rows with no trailing dividend/EPS/P.E. data at all
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ngx_scraper as ns  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_sample_fixture():
    raw = (FIXTURES_DIR / "ngx_sample_20260420.txt").read_text()
    rows = ns.parse_pdf_text(raw)
    by_symbol = {r.symbol: r for r in rows}

    assert len(rows) == 10, f"expected 10 rows, got {len(rows)}"

    # Same-line name+numbers case
    assert by_symbol["ELLAHLAKES"].name == "ELLAH LAKES PLC."
    assert by_symbol["ELLAHLAKES"].last_price == 10.35
    assert by_symbol["ELLAHLAKES"].volume == 150

    # 2-line wrapped name, with a concatenated-number field later in
    # the row (doesn't affect these fields since they come before it)
    assert by_symbol["FTNCOCOA"].name == "FTN COCOA PROCESSORS PLC"
    assert by_symbol["FTNCOCOA"].volume == 3880

    # Huge volume number (2 billion) — makes sure comma-stripping and
    # int() conversion handle NGX's largest realistic values
    assert by_symbol["UNITYBNK"].volume == 2_000_000_000

    print("test_sample_fixture: PASS")


def test_full_fixture_edge_cases():
    raw = (FIXTURES_DIR / "ngx_full_20260420.txt").read_text()
    rows = ns.parse_pdf_text(raw)
    by_symbol = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)

    assert len(rows) == 19, f"expected 19 rows, got {len(rows)}"

    # Duplicate ticker (PRESCO appears twice in the source with
    # different Div Sc values) — both should parse, not collide
    assert len(by_symbol["PRESCO"]) == 2

    # The AVAIF/CNIF pair — AVAIF's date is glued to the preceding
    # price with no space ("1,000,000.0019/02/26"). This is the case
    # that used to swallow CNIF's entire row when it broke.
    avaif = by_symbol["AVAIF"][0]
    assert avaif.business_done_date == "19/02/26", avaif.business_done_date
    assert avaif.volume == 9, avaif.volume
    assert avaif.last_price == 1_000_000.0, avaif.last_price

    cnif = by_symbol["CNIF"][0]
    assert cnif.name == "CORONATION INFRASTRUCTURE FUND"
    assert cnif.business_done_date == "20/04/26"
    assert cnif.volume == 10
    assert cnif.last_price == 110.0

    # 3-line wrapped name
    assert by_symbol["NIDF"][0].name == "CHAPEL HILL DENHAM NIG. INFRAS DEBT FUND"

    print("test_full_fixture_edge_cases: PASS")


def test_prior_close_and_change_pct():
    """Not a bulletin-parsing test — verifies upsert_rows() correctly
    computes change_pct from our OWN stored price history, since the
    bulletin itself doesn't reliably give us a trustworthy prior-close
    field (see upsert_rows docstring)."""
    from datetime import date
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import database, models

    test_engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(test_engine)
    original_session_local = database.SessionLocal
    database.SessionLocal = sessionmaker(bind=test_engine)

    try:
        day1, day2 = date(2026, 4, 20), date(2026, 4, 21)
        row_day1 = ns.ParsedRow(symbol="GTCO", name="GUARANTY TRUST HOLDING",
                                 business_done_date="20/04/26", volume=1000, last_price=80.00)
        row_day2 = ns.ParsedRow(symbol="GTCO", name="GUARANTY TRUST HOLDING",
                                 business_done_date="21/04/26", volume=1200, last_price=82.40)

        ns.upsert_rows(day1, [row_day1])
        ns.upsert_rows(day2, [row_day2])

        with database.get_session() as db:
            rows = db.query(models.PriceDaily).order_by(models.PriceDaily.trade_date).all()
            # Extract while the session is still open — get_session()
            # closes (and expires attributes on) exit, same as any
            # SQLAlchemy session, so values must be read before that.
            day1_prev, day1_change = rows[0].prev_close, rows[0].change_pct
            day2_prev, day2_change = rows[1].prev_close, rows[1].change_pct

        assert day1_prev is None, "first-ever row should have no prior close to compare against"
        assert day1_change is None

        assert float(day2_prev) == 80.00
        assert float(day2_change) == 3.0, day2_change

        print("test_prior_close_and_change_pct: PASS")
    finally:
        database.SessionLocal = original_session_local


if __name__ == "__main__":
    test_sample_fixture()
    test_full_fixture_edge_cases()
    test_prior_close_and_change_pct()
    print("\nAll tests passed.")
