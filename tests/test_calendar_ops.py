"""Calendar & DST boundaries (§18.24) and backup/restore."""

from datetime import date, datetime, timezone

import pytest

from equity_monitor.data import calendar as cal
from equity_monitor.ledger.csv_import import import_csv
from equity_monitor.ledger.store import create_account, create_portfolio
from equity_monitor.ops import backup, restore
from equity_monitor.app import open_app


def test_2026_nyse_holidays():
    h = set(cal.holidays(2026))
    expected = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}
    assert h == expected


def test_observed_rules_and_special_closures():
    assert cal.is_session(date(2021, 12, 31))               # New Year's 2022 on Saturday: not observed
    assert not cal.is_session(date(2025, 1, 9))              # Carter national day of mourning
    assert not cal.is_session(date(2023, 1, 2))              # New Year's Sunday -> Monday
    assert cal.is_session(date(2021, 6, 18))                 # Juneteenth not a market holiday before 2022
    assert not cal.is_session(date(2022, 6, 20))             # Juneteenth 2022 Sunday -> Monday


def test_early_closes():
    assert cal.is_early_close(date(2026, 11, 27))
    assert cal.is_early_close(date(2026, 12, 24))
    assert cal.is_early_close(date(2025, 7, 3))
    assert not cal.is_early_close(date(2026, 7, 2))          # July 4 2026 is Saturday; Jul 3 is the holiday
    assert cal.session_close_utc(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


def test_dst_session_close_times():
    # EDT (UTC-4) before 2026-11-01; EST (UTC-5) after
    assert cal.session_close_utc(date(2026, 10, 30)) == datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
    assert cal.session_close_utc(date(2026, 11, 2)) == datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
    # spring forward 2026-03-08
    assert cal.session_close_utc(date(2026, 3, 6)) == datetime(2026, 3, 6, 21, 0, tzinfo=timezone.utc)
    assert cal.session_close_utc(date(2026, 3, 9)) == datetime(2026, 3, 9, 20, 0, tzinfo=timezone.utc)


def test_latest_completed_session_respects_lag_and_holidays():
    # Friday 2026-07-03 is a holiday; at 17:00 NY on Monday 07-06, data lag not elapsed -> Thursday 07-02
    now = datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc)
    assert cal.latest_completed_session(now, 120) == date(2026, 7, 2)
    assert cal.latest_completed_session(datetime(2026, 7, 6, 22, 0, tzinfo=timezone.utc), 120) == date(2026, 7, 6)
    # right after the DST change: 2026-11-02 close is 21:00 UTC
    assert cal.latest_completed_session(datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc), 60) == date(2026, 11, 2)
    assert cal.latest_completed_session(datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc), 60) == date(2026, 10, 30)


def test_backup_restore_roundtrip(tmp_path, clock):
    home = tmp_path / "home"
    app = open_app(home, clock=clock)
    pf = create_portfolio(app, "main", "ACTUAL")
    a = create_account(app, pf, "acct")
    import_csv(app, a, text="date,type,amount\n2026-01-05,DEPOSIT,100\n")
    (app.raw_dir / "x").mkdir(parents=True)
    (app.raw_dir / "x" / "f.json").write_text("{}")
    arc = backup(app, tmp_path / "bk")
    app.conn.close()
    with pytest.raises(FileExistsError):
        restore(arc, home)
    new_home = tmp_path / "restored"
    info = restore(arc, new_home)
    assert info["integrity"] == "ok"
    app2 = open_app(new_home, clock=clock)
    assert app2.conn.execute("SELECT COUNT(*) FROM ledger_event").fetchone()[0] == 1
    assert (new_home / "raw" / "x" / "f.json").exists()
