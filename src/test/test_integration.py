"""Интеграционные тесты: events ↔ conditions ↔ WindowComparator.

Проверяют, что модуль events подаёт данные через events_adapter
в WindowComparator, и что результаты conditions и events
корректно объединяются.
"""

import unittest
from datetime import datetime, timedelta, timezone

from src.main.core import assess
from src.main.demo import make_demo
from src.main.events.service import DEFAULT_CONFIG
from src.main.events_adapter import (
    EventsFactorResult,
    adapt_events_for_comparator,
    merge_factor_results,
)
from src.main.window import Window
from src.main.window_comparator import WindowComparator
from src.main.conditions.debris import (
    DebrisCalculator,
    DebrisObservation,
    MeteoroidSample,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

def _demo_assess():
    """Синтетический результат assess() из demo."""
    return make_demo(DEFAULT_CONFIG)


def _simple_assess(events=None, coverage=None):
    """Минимальный assess() с контролируемыми событиями."""
    from src.main.core import event as mk_event, iso

    src = dict(
        name='test', url='https://example.invalid',
        snapshot_id='test', fetched_at='2024-05-10T09:00:00Z',
    )
    q = dict(
        start='2024-05-10T12:00:00Z', duration_hours=6,
        search_hours=6, step_minutes=60, mode='reconstruction',
    )
    bundle = dict(
        events=events or [],
        sources=[src],
        coverage=coverage or [],
        context=[],
        limitations=[],
    )
    return assess(bundle, q)


def _make_windows():
    """Два окна одинаковой длительности для comparator."""
    a = Window('win_a', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 18))
    b = Window('win_b', datetime(2024, 5, 10, 13), datetime(2024, 5, 10, 19))
    return a, b


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestEventsAdapterStructure(unittest.TestCase):
    """adapt_events_for_comparator возвращает корректную структуру."""

    def test_converts_assess_result(self):
        result = _demo_assess()
        adapted = adapt_events_for_comparator(result)

        self.assertIsInstance(adapted, dict)
        self.assertGreater(len(adapted), 0)

        first_wid = next(iter(adapted))
        factors = adapted[first_wid]
        self.assertIsInstance(factors, dict)

        # Проверяем наличие хотя бы одного механизма из events
        known_mechanisms = {'radiation', 'geomagnetic', 'communications',
                           'tracked_debris', 'meteoroids'}
        self.assertTrue(known_mechanisms & set(factors.keys()))

        # Каждый фактор имеет нужные поля
        for name, fr in factors.items():
            self.assertIsInstance(fr, EventsFactorResult)
            self.assertGreaterEqual(fr.risk_score, 0.0)
            self.assertLessEqual(fr.risk_score, 100.0)
            self.assertGreaterEqual(fr.data_coverage_pct, 0.0)
            self.assertLessEqual(fr.data_coverage_pct, 100.0)
            self.assertIsInstance(fr.missing_data, list)


class TestComparatorAcceptsEvents(unittest.TestCase):
    """WindowComparator.compare() принимает данные из events_adapter."""

    def test_comparator_with_events_factors(self):
        a, b = _make_windows()

        # Синтетические events-факторы: окно a безопаснее
        factor_results = {
            'win_a': {
                'radiation': EventsFactorResult(risk_score=10.0, data_coverage_pct=100.0),
                'geomagnetic': EventsFactorResult(risk_score=5.0, data_coverage_pct=100.0),
            },
            'win_b': {
                'radiation': EventsFactorResult(risk_score=60.0, data_coverage_pct=100.0),
                'geomagnetic': EventsFactorResult(risk_score=40.0, data_coverage_pct=100.0),
            },
        }

        comparison = WindowComparator.compare([a, b], factor_results)

        self.assertEqual(comparison.preferred_window_id, 'win_a')
        self.assertEqual(len(comparison.assessments), 2)

        # Каждое окно должно иметь факторы
        for wa in comparison.assessments:
            self.assertGreater(len(wa.factors), 0)


class TestCombinedConditionsAndEvents(unittest.TestCase):
    """Полный цикл: conditions + events → comparator."""

    def test_combined_factors_in_comparator(self):
        a, b = _make_windows()

        # Генерируем достаточно сэмплов метеороидов для покрытия > 50%
        met_samples_a = [
            MeteoroidSample(datetime(2024, 5, 10, 12) + timedelta(minutes=m), 1e-9)
            for m in range(0, 360, 1)
        ]
        met_samples_b = [
            MeteoroidSample(datetime(2024, 5, 10, 13) + timedelta(minutes=m), 1e-9)
            for m in range(0, 360, 1)
        ]

        # --- Условия (conditions): debris ---
        debris_a = DebrisCalculator.calculate_metrics(
            a,
            [DebrisObservation(datetime(2024, 5, 10, 14), 50.0, 'OBJ-1')],
            met_samples_a,
        )
        debris_b = DebrisCalculator.calculate_metrics(
            b,
            [DebrisObservation(datetime(2024, 5, 10, 14), 5.0, 'OBJ-2')],
            met_samples_b,
        )

        conditions_factors = {
            'win_a': {'debris': debris_a},
            'win_b': {'debris': debris_b},
        }

        # --- События (events) ---
        events_factors = {
            'win_a': {
                'radiation': EventsFactorResult(risk_score=5.0, data_coverage_pct=100.0),
                'tracked_debris': EventsFactorResult(risk_score=10.0, data_coverage_pct=80.0),
            },
            'win_b': {
                'radiation': EventsFactorResult(risk_score=50.0, data_coverage_pct=100.0),
                'tracked_debris': EventsFactorResult(risk_score=60.0, data_coverage_pct=80.0),
            },
        }

        merged = merge_factor_results(conditions_factors, events_factors)

        # Проверяем объединение
        self.assertIn('debris', merged['win_a'])
        self.assertIn('radiation', merged['win_a'])
        self.assertIn('tracked_debris', merged['win_a'])

        comparison = WindowComparator.compare([a, b], merged)

        # win_a безопаснее по всем факторам
        self.assertEqual(comparison.preferred_window_id, 'win_a')

        # Все факторы учтены
        for wa in comparison.assessments:
            factor_names = {f.name for f in wa.factors}
            self.assertIn('debris', factor_names)
            self.assertIn('radiation', factor_names)
            self.assertIn('tracked_debris', factor_names)


class TestEventsAdapterEmptyWindows(unittest.TestCase):
    """Пустой результат events → корректные нули."""

    def test_no_events_gives_zero_risk(self):
        result = _simple_assess(events=[], coverage=[])
        adapted = adapt_events_for_comparator(result)

        for wid, factors in adapted.items():
            for mechanism, fr in factors.items():
                self.assertEqual(fr.risk_score, 0.0)
                self.assertEqual(fr.high_risk_minutes, 0.0)


class TestEventsAdapterMissingData(unittest.TestCase):
    """Статус insufficient_data → missing_data заполнен."""

    def test_insufficient_data_propagates(self):
        result = _simple_assess(events=[], coverage=[])
        adapted = adapt_events_for_comparator(result)

        first_wid = next(iter(adapted))
        factors = adapted[first_wid]

        # Без покрытия все механизмы должны иметь insufficient
        has_missing = any(
            len(fr.missing_data) > 0
            for fr in factors.values()
        )
        self.assertTrue(has_missing, 'Должен быть хотя бы один фактор с missing_data')


class TestWindowFromEventsQuery(unittest.TestCase):
    """Window создаётся из параметров events query."""

    def test_window_from_query_params(self):
        start = datetime(2024, 5, 10, 12, tzinfo=timezone.utc)
        duration_hours = 6
        end = start + timedelta(hours=duration_hours)

        w = Window('from_events', start, end)

        self.assertEqual(w.duration_minutes, 360.0)
        self.assertIsNone(w.start.tzinfo)  # нормализован в naive UTC


class TestDebrisFromEventsConjunctions(unittest.TestCase):
    """Сближения из events SOCRATES → DebrisObservation → DebrisCalculator."""

    def test_events_conjunctions_to_debris(self):
        # Симулируем events-данные о сближении
        tca = datetime(2024, 5, 10, 14, 30)
        miss_km = 8.0
        obj_id = '99999'

        # Конвертируем в формат conditions
        obs = DebrisObservation(
            time=tca,
            miss_distance_km=miss_km,
            object_id=obj_id,
            relative_speed_km_s=12.1,
            source='SOCRATES_via_events',
        )

        w = Window('conj_test', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 18))
        metrics = DebrisCalculator.calculate_metrics(w, [obs])

        self.assertEqual(metrics.n_conjunctions, 1)
        self.assertEqual(metrics.closest_distance_km, miss_km)
        self.assertEqual(metrics.closest_object_id, obj_id)
        self.assertGreater(metrics.risk_score, 0)


if __name__ == '__main__':
    unittest.main()
