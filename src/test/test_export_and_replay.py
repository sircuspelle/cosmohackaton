# -*- coding: utf-8 -*-
"""Тесты выгрузки, воспроизводимости и исторического режима.

Проверяют требования постановки:
  * п.5: сохранение расчёта и машиночитаемая выгрузка;
  * T8: воспроизводимость сохранённого запроса;
  * T4: replay использует только сведения, доступные на момент прогноза;
  * O3: объяснение правил предпочтения окна.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.main.events.core import assess, event, iso, now
from src.main.events.demo import make_demo
from src.main.events.service import DEFAULT_CONFIG
from src.main.events.storage import Store
from src.main.events.app import main as app_main
from src.main.window_comparator import WindowComparator
from src.main.window import Window
from src.main.events_adapter import (
    EventsFactorResult,
    adapt_events_for_comparator,
)


SRC = dict(name='test', url='https://example.invalid',
           snapshot_id='test', fetched_at='2024-05-10T09:00:00Z')
Q = dict(start='2024-05-10T12:00:00Z', duration_hours=6,
         search_hours=6, step_minutes=60, mode='reconstruction')


class TestRunPersistence(unittest.TestCase):
    """T8: сохранённый прогон можно восстановить."""

    def test_save_and_load_run_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'a.db')
            result = make_demo(DEFAULT_CONFIG)
            rid = store.save_run(result)
            self.assertIsNotNone(rid)
            loaded = store.run(rid)
            self.assertIsNotNone(loaded)
            # Ключевые поля должны сохраниться
            for key in ('algorithm_version', 'query', 'windows',
                        'events', 'sources', 'recommendation'):
                self.assertIn(key, loaded)
            self.assertEqual(loaded['algorithm_version'],
                             result['algorithm_version'])

    def test_run_contains_query_parameters(self):
        """Выгрузка должна содержать параметры запроса (п.5)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'a.db')
            result = make_demo(DEFAULT_CONFIG)
            rid = store.save_run(result)
            loaded = store.run(rid)
            q = loaded['query']
            for key in ('start', 'duration_hours', 'search_hours',
                        'step_minutes', 'mode'):
                self.assertIn(key, q)

    def test_run_contains_source_versions(self):
        """В выгрузке должны быть версии/идентификаторы источников."""
        result = make_demo(DEFAULT_CONFIG)
        self.assertIn('sources', result)
        for src in result['sources']:
            # Идентификация источника — обязательна
            self.assertIn('name', src)
            self.assertIn('snapshot_id', src)
            self.assertIn('fetched_at', src)
            self.assertIn('url', src)
            # Статус — опционален: у синтетического demo его может не быть
            if 'status' in src:
                self.assertIsInstance(src['status'], str)
    def test_snapshot_evidence_stored_and_retrievable(self):
        """Сырой ответ источника сохраняется как snapshot (evidence)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'a.db')
            body = json.dumps([{'time_tag': '2024-05-10T12:00Z',
                                'energy': '>=10 MeV', 'flux': 15}])
            sid = store._save_snapshot(
                'https://example.invalid/protons',
                '2024-05-10T12:00:00Z', body, '{}')
            snap = store.snapshot(sid)
            self.assertIsNotNone(snap)
            self.assertEqual(snap['body'], body)

    def test_export_is_json_serializable(self):
        """Результат должен быть сериализуем в JSON без NaN."""
        result = make_demo(DEFAULT_CONFIG)
        # allow_nan=False выбросит ValueError, если есть NaN/Inf
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertIn('windows', text)
        self.assertIn('recommendation', text)


class TestReplayHonesty(unittest.TestCase):
    """T4: replay не маскирует поздние сведения под точный прогноз."""

    def test_asof_excludes_events_fetched_after_cutoff(self):
        e = event(
            'late', 'sep', 'radiation',
            '2024-05-10T12:00Z', '2024-05-10T14:00Z',
            {**SRC, 'fetched_at': '2026-09-18T10:00:00Z'},
            {}, published='2024-05-09T10:00Z',
        )
        r = assess(dict(events=[e], sources=[]),
                   {**Q, 'mode': 'as_of', 'as_of': '2024-05-10T11:00Z'})
        self.assertEqual(r['events'], [])
        self.assertEqual(r['exclusions_after_cutoff'], 1)

    def test_asof_keeps_event_fetched_before_cutoff(self):
        e = event(
            'early', 'sep', 'radiation',
            '2024-05-10T12:00Z', '2024-05-10T14:00Z',
            {**SRC, 'fetched_at': '2024-05-09T10:00:00Z'},
            {}, published='2024-05-09T09:00:00Z',
        )
        r = assess(dict(events=[e], sources=[]),
                   {**Q, 'mode': 'as_of', 'as_of': '2024-05-10T11:00Z'})
        self.assertEqual(len(r['events']), 1)
        self.assertEqual(r['exclusions_after_cutoff'], 0)

    def test_asof_keeps_forecast_known_at_cutoff(self):
        """Внешний прогноз, опубликованный до cutoff, должен участвовать."""
        e = event(
            'fc', 'cme_arrival_earth', 'geomagnetic',
            '2024-05-10T12:00Z', '2024-05-10T15:00Z',
            {**SRC, 'fetched_at': '2024-05-10T08:00:00Z'},
            {}, basis='external_forecast',
            published='2024-05-10T07:00:00Z',
        )
        r = assess(dict(events=[e], sources=[]),
                   {**Q, 'mode': 'as_of', 'as_of': '2024-05-10T11:00Z'})
        self.assertEqual(len(r['events']), 1)

    def test_asof_excludes_coverage_after_cutoff(self):
        """Coverage-интервалы, полученные после cutoff, не учитываются."""
        cov = [dict(
            mechanism='radiation',
            start='2024-05-10T12:00:00Z',
            end='2024-05-10T13:00:00Z',
            fetched_at='2026-09-18T10:00:00Z',
            snapshot_id='x',
            scope='test',
            basis='observation',
        )]
        r = assess(dict(events=[], sources=[], coverage=cov),
                   {**Q, 'mode': 'as_of', 'as_of': '2024-05-10T11:00Z'})
        rad = r['windows'][0]['factors']['radiation']
        self.assertEqual(rad['coverage_fraction'], 0.0)

    def test_reconstruction_mode_does_not_require_as_of(self):
        r = assess(dict(events=[], sources=[]), Q)
        self.assertIn('windows', r)
        self.assertEqual(r['query']['mode'], 'reconstruction')

    def test_replay_cli_recomputes_from_saved_inputs(self):
        """CLI replay восстанавливает результат из сохранённых normalized inputs."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'a.db'
            cfg = {**DEFAULT_CONFIG, 'database': str(db)}
            store = Store(db)
            result = make_demo(cfg)
            rid = store.save_run(result)

            # Эмулируем логику app.py replay
            loaded = store.run(rid)
            bundle = {k: loaded.get(k, [])
                      for k in ('events', 'sources', 'context', 'limitations')}
            bundle['coverage'] = loaded.get('coverage_intervals', [])
            recomputed = assess(bundle, loaded['query'])
            self.assertEqual(recomputed['algorithm_version'],
                             loaded['algorithm_version'])
            self.assertEqual(len(recomputed['windows']),
                             len(loaded['windows']))


class TestComparatorExplainsChoice(unittest.TestCase):
    """O3: правила предпочтения и объяснение отказа понятны."""

    def test_reason_mentions_minimum_when_choice_made(self):
        a = Window('a', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 18))
        b = Window('b', datetime(2024, 5, 10, 13), datetime(2024, 5, 10, 19))
        factors = {
            'a': {'radiation': EventsFactorResult(risk_score=10.0, data_coverage_pct=100.0)},
            'b': {'radiation': EventsFactorResult(risk_score=80.0, data_coverage_pct=100.0)},
        }
        cmp = WindowComparator.compare([a, b], factors)
        self.assertEqual(cmp.preferred_window_id, 'a')
        self.assertIn('Минимальный', cmp.reason)

    def test_reason_mentions_tie_when_close(self):
        a = Window('a', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 18))
        b = Window('b', datetime(2024, 5, 10, 13), datetime(2024, 5, 10, 19))
        factors = {
            'a': {'radiation': EventsFactorResult(risk_score=50.0, data_coverage_pct=100.0)},
            'b': {'radiation': EventsFactorResult(risk_score=51.0, data_coverage_pct=100.0)},
        }
        cmp = WindowComparator.compare([a, b], factors, tie_tolerance=2.0)
        self.assertIsNone(cmp.preferred_window_id)
        self.assertIn('порог', cmp.reason.lower())

    def test_insufficient_data_defers_choice(self):
        a = Window('a', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 18))
        b = Window('b', datetime(2024, 5, 10, 13), datetime(2024, 5, 10, 19))
        factors = {
            'a': {'radiation': EventsFactorResult(risk_score=None, data_coverage_pct=0.0,
                                                  missing_data=['нет данных'])},
            'b': {'radiation': EventsFactorResult(risk_score=None, data_coverage_pct=0.0,
                                                  missing_data=['нет данных'])},
        }
        cmp = WindowComparator.compare([a, b], factors)
        self.assertIsNone(cmp.preferred_window_id)
        self.assertIn('Недостаточно', cmp.reason)

    def test_shortening_shown_as_plan_change(self):
        """Сокращение ВКД — изменение плана, а не улучшение при прежних условиях.

        Проверяем, что comparator не смешивает окна разной длительности.
        """
        a = Window('a', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 18))
        b = Window('b', datetime(2024, 5, 10, 12), datetime(2024, 5, 10, 15))
        with self.assertRaises(ValueError):
            WindowComparator.compare([a, b], {})


class TestExportMatchesInterface(unittest.TestCase):
    """Сведения в выгрузке должны совпадать с интерфейсом."""

    def test_export_contains_recommendation_and_windows(self):
        result = make_demo(DEFAULT_CONFIG)
        self.assertIn('recommendation', result)
        self.assertIn('status', result['recommendation'])
        self.assertIn('preferred_window', result['recommendation'])
        self.assertIn('reason', result['recommendation'])
        self.assertGreater(len(result['windows']), 0)

    def test_export_contains_event_ids_referenced_from_windows(self):
        """ID событий в окне должны существовать в общем списке events."""
        result = make_demo(DEFAULT_CONFIG)
        all_ids = {e['id'] for e in result['events']}
        for w in result['windows']:
            for m, f in w['factors'].items():
                for eid in f.get('event_ids', []):
                    self.assertIn(eid, all_ids,
                                  f'{eid} из окна {w["start"]} не найден в events')

    def test_export_contains_limitations(self):
        result = make_demo(DEFAULT_CONFIG)
        self.assertIn('limitations', result)
        self.assertGreater(len(result['limitations']), 0)


class TestOfflineAndCacheReproducibility(unittest.TestCase):
    """T8: воспроизводимость через кэш без сети."""

    def test_offline_uses_cached_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'a.db')
            # Кладём снапшот вручную
            body = json.dumps([])
            store._save_snapshot('https://example.invalid/x',
                                 iso(now()), body, '{}')
            spec = {'url': 'https://example.invalid/x', 'ttl_seconds': 3600}
            r = store.fetch(spec, DEFAULT_CONFIG, offline=True)
            self.assertEqual(r['transport_status'], 'cached')
            self.assertEqual(r['body'], body)

    def test_offline_without_cache_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'a.db')
            spec = {'url': 'https://example.invalid/missing', 'ttl_seconds': 3600}
            with self.assertRaises(ValueError):
                store.fetch(spec, DEFAULT_CONFIG, offline=True)

    def test_cutoff_uses_only_older_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'a.db')
            store._save_snapshot('https://example.invalid/y',
                                 '2024-05-10T10:00:00Z', '[]', '{}')
            store._save_snapshot('https://example.invalid/y',
                                 '2024-05-10T14:00:00Z', '[]', '{}')
            # cutoff = 12:00 → должен вернуться только снапшот 10:00
            from src.main.events.core import dt
            got = store.last('https://example.invalid/y',
                             dt('2024-05-10T12:00:00Z'))
            self.assertEqual(got['fetched_at'], '2024-05-10T10:00:00Z')


if __name__ == '__main__':
    unittest.main()