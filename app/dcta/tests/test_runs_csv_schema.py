"""runs.csv must stay readable when FIELDNAMES grows.

csv.DictWriter only writes a header for a NEW file, so appending a wider row to a
runs.csv created under an older schema misaligns it silently: the surplus value
lands under DictReader's None key and every consumer — the eval harness, the
reported KPIs — reads shifted data without erroring. That is invisible until the
numbers are already in a report, so it gets a regression test.
"""
import csv

from dcta.utils.telemetry.metrics import FIELDNAMES, RunMetrics


def _write_old_schema(path, drop=("endpoint",), rows=2):
    """A runs.csv as an earlier build would have left it: same minus `drop`."""
    old = [f for f in FIELDNAMES if f not in drop]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=old)
        w.writeheader()
        for i in range(rows):
            w.writerow({k: f"old{i}" for k in old})
    return old


def test_append_to_older_csv_stays_aligned(tmp_path):
    p = tmp_path / "runs.csv"
    old = _write_old_schema(p)

    RunMetrics(customer_id="C001").write(p)

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert len(rows) == 3                       # 2 historical + 1 new
    for r in rows:
        assert None not in r, "surplus field leaked into DictReader's None key"
        assert set(r) == set(FIELDNAMES)

    # historical values survive the widening untouched
    assert all(rows[0][k] == "old0" for k in old)


def test_new_column_blank_on_historical_rows(tmp_path):
    """Blank means 'predates the column' — never guessed retroactively.

    Backfilling historical rows with today's endpoint would assert something the
    data does not support: those runs may have used a different backend entirely.
    """
    p = tmp_path / "runs.csv"
    _write_old_schema(p)
    RunMetrics(customer_id="C001").write(p)

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["endpoint"] == ""
    assert rows[-1]["endpoint"], "the new row must record its endpoint"


def test_migration_is_idempotent(tmp_path):
    """Cost is one header read per write; it must not rewrite every time."""
    p = tmp_path / "runs.csv"
    _write_old_schema(p)

    for i in range(3):
        RunMetrics(customer_id=f"C00{i}").write(p)

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert len(rows) == 5                       # 2 historical + 3 appended
    assert all(set(r) == set(FIELDNAMES) for r in rows)


def test_endpoint_distinguishes_backends():
    """`model` alone cannot: a single-model gateway reports its model as "default".

    Without this column two eval batches run against different backends are
    indistinguishable in the CSV, which silently breaks their comparability.
    """
    row = RunMetrics(model="default", endpoint="https://gw.internal/v1").to_row()

    assert row["model"] == "default"            # ambiguous on its own
    assert row["endpoint"] == "https://gw.internal/v1"


def test_fresh_file_gets_current_header(tmp_path):
    p = tmp_path / "runs.csv"
    RunMetrics(customer_id="C001").write(p)

    header = next(csv.reader(p.open(encoding="utf-8")))
    assert header == FIELDNAMES


def test_default_path_is_read_live(tmp_path, monkeypatch):
    """write() with no argument must follow the module constant at call time, or a
    test could never keep its rows out of the real file."""
    from dcta.utils.telemetry import metrics

    target = tmp_path / "elsewhere" / "runs.csv"
    monkeypatch.setattr(metrics, "RUNS_CSV", target)
    RunMetrics(customer_id="C001").write()

    assert target.is_file()
