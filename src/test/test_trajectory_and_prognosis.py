# -*- coding: utf-8 -*-
"""Тесты траектории МКС и прогноза на 6 часов.

Проверяют требования постановки:
  * п.1: расчёт положения станции и использование его в анализе фактора;
  * п.2: хотя бы один механизм имеет расчёт/прогноз на ближайшие 6 часов;
  * O1: не менее двух разных механизмов воздействия;
  * п.2: observation / external_forecast / team_calculation различаются.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from src.main.core import assess, event, iso, MECHANISMS
from src.main.events.service import DEFAULT_CONFIG, collect
from src.main.events.storage import Store
from src.main.events.adapters import donki, kp, goes, socrates
from src.main.conditions.protons import (
    ProtonPoint,
    TrajectoryAwareProtonCalculator,
)
from src.main.conditions.illumination import IlluminationCalculator
from src.main.conditions.thermal import ThermalCalculator
from src.main.window import Window


SRC = dict(name='test', url='https://example.invalid',
           snapshot_id='test', fetched_at='2024-05-10T09:00:00Z')
Q = dict(start='2024-05-10T12:00:00Z', duration_hours=6,
         search_hours=6, step_minutes=60, mode='reconstruction')


class TestTrajectoryIsUsedInFactor(unittest.TestCase):
    """Траектория должна влиять на оценку, а не только отображаться."""

    def test_proton_flux_with_position_differs_from_flat(self):
        """Эффективный поток с координатами != сырому потоку.

        Это доказывает, что геометрия траектории реально участвует
        в расчёте (требование «расчёт траектории используется в анализе»).
        """
        # Полюс: transmission=1.0 → f_eff = f_raw
        # Экватор: transmission=0.05 → f_eff << f_raw
        pts = [
            ProtonPoint(
                time=datetime(2024, 5, 10, 12, 0),
                flux=100.0,
                lat_deg=80.0, lon_deg=0.0, alt_km=420.0,
                mag_lat_deg=80.0,
            ),
            ProtonPoint(
                time=datetime(2024, 5, 10, 12, 30),
                flux=100.0,
                lat_deg=0.0, lon_deg=0.0, alt_km=420.0,
                mag_lat_deg=0.0,
            ),
        ]
        w = Window('traj_test',
                   datetime(2024, 5, 10, 12),
                   datetime(2024, 5, 10, 13))
        m = TrajectoryAwareProtonCalculator.calculate_proton_metrics(w, pts)

        self.assertTrue(m.trajectory_aware)

        # Максимум eff может совпасть с raw (полюс), но интеграл — нет:
        # интеграл учитывает обе точки, одна из которых сильно ослаблена.
        self.assertLess(
            m.integral_flux_eff,
            m.integral_flux_raw,
            "Интеграл эффективного потока должен быть меньше сырого "
            "из-за ослабления на экваторе",
        )

        # Дополнительно: убедимся, что вес действительно различается
        from src.main.conditions.protons import GeomagneticModel
        w_pole = GeomagneticModel.weight(pts[0])
        w_eq = GeomagneticModel.weight(pts[1])
        self.assertGreater(w_pole, w_eq,
                           "Пропускание у полюса должно быть выше, чем у экватора")
        self.assertAlmostEqual(w_pole, 1.0, places=3)
        self.assertLess(w_eq, 0.5)
    def test_trajectory_not_available_falls_back_to_raw(self):
        """Без координат trajectory_aware = False, метрики считаются по сырому потоку."""
        pts = [
            ProtonPoint(time=datetime(2024, 5, 10, 12, 0), flux=50.0),
            ProtonPoint(time=datetime(2024, 5, 10, 12, 30), flux=60.0),
        ]
        w = Window('no_traj',
                   datetime(2024, 5, 10, 12),
                   datetime(2024, 5, 10, 13))
        m = TrajectoryAwareProtonCalculator.calculate_proton_metrics(w, pts)
        self.assertFalse(m.trajectory_aware)
        self.assertEqual(m.max_flux_eff, m.max_flux_raw)

    def test_tle_epoch_and_source_visible_in_record(self):
        """TLE-запись должна нести эпоху и источник (требование п.1)."""
        from src.main.adapter import TLERecord
        rec = TLERecord(
            name='ISS (ZARYA)',
            line1='1 25544U ...',
            line2='2 25544 ...',
            epoch=datetime(2024, 5, 10, 10, 0),
            norad_id=25544,
            source='celestrak',
            retrieved_at=datetime(2024, 5, 10, 10, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(rec.source, 'celestrak')
        self.assertEqual(rec.epoch.year, 2024)
        self.assertEqual(rec.norad_id, 25544)


class TestSixHourPrognosisExists(unittest.TestCase):
    """Хотя бы один механизм должен иметь прогноз/расчёт на 6 часов."""

    def test_proton_threshold_episode_within_6h(self):
        """GOES proton episode должен быть виден в 6-часовом окне."""
        src = {**SRC, 'name': 'noaa_protons',
               'fetched_at': '2024-05-10T12:00:00Z'}
        raw = [
            {'time_tag': f'2024-05-10T{h:02d}:{m:02d}Z',
             'satellite': 18, 'energy': '>=10 MeV', 'flux': f}
            for h, m, f in [
                (12, 0, 5), (12, 5, 15), (12, 10, 25),
                (12, 15, 30), (12, 20, 8), (12, 25, 5),
            ]
        ]
        events, coverage, _ = goes(raw, src, DEFAULT_CONFIG)
        self.assertGreaterEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e['mechanism'], 'radiation')
        self.assertEqual(e['kind'], 'proton_threshold_episode')
        # Событие начинается внутри 6-часового горизонта
        start = datetime.fromisoformat(e['start'].replace('Z', '+00:00'))
        horizon = datetime(2024, 5, 10, 12, tzinfo=timezone.utc) + timedelta(hours=6)
        self.assertLess(start, horizon)

    def test_enlil_forecast_is_external_forecast_basis(self):
        """WSA-Enlil — внешний прогноз, а не наблюдение."""
        raw = [{
            'simulationID': 'sim1',
            'estimatedShockArrivalTime': '2024-05-10T15:00Z',
            'estimatedDuration': 3,
            'modelCompletionTime': '2024-05-09T10:00Z',
            'cmeInputs': [{'cmeid': 'cme1'}],
        }]
        events, _, _ = donki(raw, SRC, 'WSAEnlilSimulations', DEFAULT_CONFIG)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['basis'], 'external_forecast')
        self.assertEqual(events[0]['mechanism'], 'geomagnetic')

    def test_team_calculated_basis_present(self):
        """GOES threshold segments помечаются team_calculation."""
        src = {**SRC, 'name': 'noaa_xrays',
               'fetched_at': '2024-05-10T12:00:00Z'}
        raw = [
            {'time_tag': '2024-05-10T12:00Z', 'energy': '0.1-0.8nm', 'flux': 1e-4},
            {'time_tag': '2024-05-10T12:01Z', 'energy': '0.1-0.8nm', 'flux': 1e-4},
        ]
        events, _, _ = goes(raw, src, DEFAULT_CONFIG)
        self.assertEqual(events[0]['basis'], 'team_calculation')


class TestAtLeastTwoMechanisms(unittest.TestCase):
    """O1: выбраны разные механизмы, а не показатели одного."""

    def test_two_distinct_mechanisms_in_bundle(self):
        """Собираем bundle с radiation и geomagnetic и проверяем,
        что это разные механизмы с разной физикой."""
        e_rad = event('r1', 'sep', 'radiation',
                      '2024-05-10T12:00Z', '2024-05-10T14:00Z', SRC, {})
        e_geo = event('g1', 'geomagnetic_storm', 'geomagnetic',
                      '2024-05-10T12:00Z', '2024-05-10T15:00Z', SRC, {})
        result = assess(dict(events=[e_rad, e_geo], sources=[]), Q)
        mechs_with_events = {
            m for m in MECHANISMS
            if result['windows'][0]['factors'][m]['event_count'] > 0
        }
        self.assertGreaterEqual(len(mechs_with_events), 2)
        self.assertIn('radiation', mechs_with_events)
        self.assertIn('geomagnetic', mechs_with_events)

    def test_multiple_indicators_of_one_mechanism_not_enough(self):
        """Два события radiation не должны считаться двумя механизмами."""
        e1 = event('r1', 'sep', 'radiation',
                   '2024-05-10T12:00Z', '2024-05-10T13:00Z', SRC, {})
        e2 = event('r2', 'rbe', 'radiation',
                   '2024-05-10T13:00Z', '2024-05-10T14:00Z', SRC, {})
        result = assess(dict(events=[e1, e2], sources=[]), Q)
        mechs = {m for m in MECHANISMS
                 if result['windows'][0]['factors'][m]['event_count'] > 0}
        self.assertEqual(mechs, {'radiation'})


class TestBasisSeparation(unittest.TestCase):
    """Наблюдение, внешний прогноз и расчёт команды различаются."""

    def test_bases_counted_separately(self):
        e_obs = event('obs1', 'flr', 'communications',
                      '2024-05-10T12:00Z', '2024-05-10T13:00Z',
                      SRC, {}, basis='observation')
        e_fc = event('fc1', 'cme_arrival', 'geomagnetic',
                     '2024-05-10T12:00Z', '2024-05-10T15:00Z',
                     SRC, {}, basis='external_forecast')
        e_calc = event('calc1', 'xray_threshold_episode', 'communications',
                       '2024-05-10T12:00Z', '2024-05-10T12:30Z',
                       SRC, {}, basis='team_calculation')
        result = assess(dict(events=[e_obs, e_fc, e_calc], sources=[]), Q)
        comm = result['windows'][0]['factors']['communications']
        geo = result['windows'][0]['factors']['geomagnetic']
        self.assertEqual(comm['observed_event_count'], 1)
        self.assertEqual(comm['team_calculated_event_count'], 1)
        self.assertEqual(geo['forecast_event_count'], 1)
        # Разделение по времени перекрытия
        self.assertGreater(comm['observation_overlap_minutes'], 0)
        self.assertGreater(comm['team_calculation_overlap_minutes'], 0)
        self.assertGreater(geo['external_forecast_overlap_minutes'], 0)


class TestWindowContainsTrajectoryMetrics(unittest.TestCase):
    """Окно ВКД должно позволять хранить и сравнивать траекторные метрики."""

    def test_window_accepts_6h_duration(self):
        w = Window('six_h',
                   datetime(2024, 5, 10, 12),
                   datetime(2024, 5, 10, 18))
        self.assertEqual(w.duration_minutes, 360.0)

    def test_window_rejects_more_than_8h(self):
        with self.assertRaises(ValueError):
            Window('too_long',
                   datetime(2024, 5, 10, 12),
                   datetime(2024, 5, 10, 21))

    def test_search_window_up_to_24h(self):
        # search_hours = 24 допустимо
        q = {**Q, 'search_hours': 24}
        from src.main.core import validate_query
        validate_query(q)  # не должно бросить

    def test_search_window_25h_rejected(self):
        from src.main.core import validate_query
        with self.assertRaises(ValueError):
            validate_query({**Q, 'search_hours': 25})


if __name__ == '__main__':
    unittest.main()