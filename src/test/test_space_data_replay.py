import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.main.adapter import SpaceDataAdapter


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
