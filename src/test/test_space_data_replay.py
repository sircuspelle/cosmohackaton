from datetime import datetime, timezone
from unittest.mock import Mock

from src.main.adapter import SpaceDataAdapter
from src.main.replay_archive import known_at, weather_rows


def test_replay_protons_and_kp_are_cut_off():
    a = SpaceDataAdapter()
    as_of = datetime(2024, 6, 15, 12, tzinfo=timezone.utc)
    protons = a.fetch_protons(as_of=as_of)
    kp = a.fetch_kp(as_of=as_of)
    assert protons and all(p.time <= as_of.replace(tzinfo=None) for p in protons)
    assert kp and all(row["time_tag"] <= "2024-06-15T12:00:00Z" for row in kp)


def test_replay_prefers_celestrak_gp_history():
    c = Mock()
    c.get_gp_history.return_value = [{
        "OBJECT_NAME": "ISS (ZARYA)", "NORAD_CAT_ID": 25544,
        "EPOCH": "2024-05-10T10:00:00Z",
        "TLE_LINE1": "1 25544U 98067A   24131.41666667  .0001  00000-0  10000-3 0  9999",
        "TLE_LINE2": "2 25544  51.6400  10.0000 0005000  20.0000  30.0000 15.50000000123456",
        "available_at": "2024-05-10T11:00:00Z",
    }]
    a = SpaceDataAdapter(celestrak=c)
    tle = a.fetch_iss_tle(datetime(2024, 5, 10, 12, tzinfo=timezone.utc))
    assert tle is not None
    assert tle.source == "celestrak_gp_history"
    c.get_gp_history.assert_called_once()


def test_replay_context_exposes_limitations():
    ctx = SpaceDataAdapter().build_context(datetime(2024, 5, 10, tzinfo=timezone.utc))
    assert ctx.is_historical
    assert ctx.limitations


def test_reconstruction_returns_closed_interval_from_as_of_to_end(tmp_path):
    archive = tmp_path / "archive_protons_may_june2024.json"
    archive.write_text(
        """[
          {"time_tag":"2024-05-10T09:00:00Z","energy":">=10 MeV","flux":1},
          {"time_tag":"2024-05-10T12:00:00Z","energy":">=10 MeV","flux":2},
          {"time_tag":"2024-05-10T15:00:00Z","energy":">=10 MeV","flux":3},
          {"time_tag":"2024-05-10T18:00:00Z","energy":">=10 MeV","flux":4}
        ]""",
        encoding="utf-8",
    )

    rows = weather_rows(
        tmp_path,
        "protons",
        datetime(2024, 5, 10, 12, tzinfo=timezone.utc),
        mode="reconstruction",
        end=datetime(2024, 5, 10, 15, tzinfo=timezone.utc),
    )

    assert [row["flux"] for row in rows] == [2, 3]


def test_weather_rows_rejects_dates_outside_archive_period(tmp_path):
    assert weather_rows(
        tmp_path,
        "protons",
        datetime(2024, 4, 30, 23, 59, tzinfo=timezone.utc),
    ) == []
    assert weather_rows(
        tmp_path,
        "protons",
        datetime(2024, 7, 1, tzinfo=timezone.utc),
    ) == []


def test_known_at_requires_explicit_availability_timestamp():
    cutoff = datetime(2024, 5, 10, 12, tzinfo=timezone.utc)

    assert not known_at({"time_tag": "2024-05-10T09:00:00Z"}, cutoff)
    assert known_at(
        {"available_at": "2024-05-10T11:59:00Z"},
        cutoff,
    )
