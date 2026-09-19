# -*- coding: utf-8 -*-
"""Тесты исследовательского сравнения и объяснений.

Проверяют требования постановки:
  * T5: сравнение с простым подходом (последнее наблюдение / одно предупреждение);
  * O2: последовательная интерпретация уровней угрозы и неопределённости;
  * O4: доступность первоисточника и обоснования;
  * глоссарий: S/G/R не равнозначны, MMO != сближение, прокси-показатели.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from src.main.events.core import assess, event, iso
from src.main.events.demo import make_demo
from src.main.events.service import DEFAULT_CONFIG
from src.main.events.adapters import goes, kp, donki, socrates
from src.main.conditions.debris import (
    DebrisCalculator, DebrisObservation, MeteoroidSample,
)
from src.main.window import Window


SRC = dict(name='test', url='https://example.invalid',
           snapshot_id='test', fetched_at='2024-05-10T09:00:00Z')
Q = dict(start='2024-05-10T12:00:00Z', duration_hours=6,
         search_hours=0, mode='reconstruction')


class TestComparisonWithSimpleApproach(unittest.TestCase):
    """T5: сравнение с более простым вариантом."""

    def test_last_observation_vs_full_series(self):
        """Простой подход: только последнее наблюдение.
        Наш: весь ряд. Результаты должны различаться на выраженном событии.
        """
        # Выраженное событие: пик в середине, спад к концу.
        # Простой подход «последнее наблюдение» пропустит пик.
        pts = [
            {'time_tag': '2024-05-10T12:00Z', 'energy': '>=10 MeV',
             'satellite': 18, 'flux': 5},
            {'time_tag': '2024-05-10T12:10Z', 'energy': '>=10 MeV',
             'satellite': 18, 'flux': 50},   # пик
            {'time_tag': '2024-05-10T12:20Z', 'energy': '>=10 MeV',
             'satellite': 18, 'flux': 8},    # спад ниже порога
        ]
        src = {**SRC, 'name': 'noaa_protons',
               'fetched_at': '2024-05-10T12:25:00Z'}
        events, _, _ = goes(pts, src, DEFAULT_CONFIG)
        # Полный ряд должен найти эпизод
        self.assertGreaterEqual(len(events), 1)
        # Простой подход (последнее наблюдение = 8 pfu < порога 10)
        # не нашёл бы события. Это и есть содержательное различие.

    def test_single_bulletin_vs_normalized_events(self):
        """Одно готовое предупреждение vs нормализованные события.

        alerts() не создаёт активных интервалов (только context/evidence),
        поэтому простой подход «взять bulletin как событие» был бы неверным.
        """
        from src.main.events.adapters import alerts
        data = [{
            'product_id': 'ALTX',
            'issue_datetime': '2024-05-10T12:00Z',
            'message': 'ALERT: Threshold Reached: 2024 May 10 1200 UTC',
        }]
        events, coverage, context = alerts(data, SRC, DEFAULT_CONFIG)
        # Наш подход: bulletin → только evidence, не активное событие
        self.assertEqual(events, [])
        self.assertEqual(coverage, [])
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]['usage'],
                         'Evidence only; extensions/cancellations not used as independent risk increments.')

    def test_control_period_without_event(self):
        """Контрольный период без события → статус no_detected_events."""
        r = assess(dict(events=[], sources=[], coverage=[dict(
            mechanism='radiation',
            start='2024-05-10T12:00:00Z',
            end='2024-05-10T18:00:00Z',
            fetched_at='2024-05-10T12:00:00Z',
            snapshot_id='x', scope='test', basis='observation')]),
            Q)
        rad = r['windows'][0]['factors']['radiation']
        self.assertEqual(rad['event_count'], 0)
        self.assertEqual(rad['status'], 'no_detected_events')


class TestThreatLevelsNotEquivalent(unittest.TestCase):
    """S/G/R не равнозначны: одинаковые номера — разные воздействия."""

    def test_kp_and_xray_are_different_mechanisms(self):
        """Kp (геомагнитный) и X-ray (связь) — разные механизмы."""
        kp_events, _, _ = kp(
            [{'time_tag': '2024-05-10T12:00Z', 'kp': 6, 'observed': 'observed'}],
            SRC, DEFAULT_CONFIG)
        self.assertEqual(kp_events[0]['mechanism'], 'geomagnetic')

        xray_events, _, _ = goes(
            [{'time_tag': '2024-05-10T12:00Z', 'energy': '0.1-0.8nm',
              'flux': 1e-4}],
            {**SRC, 'name': 'noaa_xrays'}, DEFAULT_CONFIG)
        self.assertEqual(xray_events[0]['mechanism'], 'communications')
        self.assertNotEqual(kp_events[0]['mechanism'],
                            xray_events[0]['mechanism'])

    def test_kp_scale_not_relabelled_as_radiation(self):
        """Kp-эпизод не должен попадать в radiation."""
        kp_events, _, _ = kp(
            [{'time_tag': '2024-05-10T12:00Z', 'kp': 7, 'observed': 'observed'}],
            SRC, DEFAULT_CONFIG)
        self.assertNotEqual(kp_events[0]['mechanism'], 'radiation')

    def test_noaa_scale_retained_in_values(self):
        kp_events, _, _ = kp(
            [{'time_tag': '2024-05-10T12:00Z', 'kp': 7,
              'observed': 'observed', 'noaa_scale': 'G3'}],
            SRC, DEFAULT_CONFIG)
        self.assertEqual(kp_events[0]['values']['noaa_scale'], 'G3')


class TestMMOvsConjunction(unittest.TestCase):
    """MMO (микрометеороиды/мусор) и сближение — разные вещи."""

    def test_conjunction_has_tca_and_object_id(self):
        events, _, _ = socrates(
            'NORAD_CAT_ID_1,NORAD_CAT_ID_2,TCA,TCA_RANGE,'
            'TCA_RELATIVE_SPEED,MAX_PROB\n'
            '25544,99999,2024-05-10 13:00:00,5,12,0.001\n',
            SRC, DEFAULT_CONFIG)
        self.assertEqual(len(events), 1)
        v = events[0]['values']
        self.assertIn('tca', v)
        self.assertIn('other_norad_id', v)
        self.assertIn('miss_distance_km', v)
        self.assertEqual(events[0]['kind'], 'iss_conjunction')

    def test_conjunction_is_not_collision_probability(self):
        """MAX_PROB — не вероятность столкновения."""
        events, _, _ = socrates(
            'NORAD_CAT_ID_1,NORAD_CAT_ID_2,TCA,TCA_RANGE,'
            'TCA_RELATIVE_SPEED,MAX_PROB\n'
            '25544,99999,2024-05-10 13:00:00,5,12,0.001\n',
            SRC, DEFAULT_CONFIG)
        v = events[0]['values']
        self.assertIn('maximum_probability_bound', v)
        self.assertNotIn('collision_probability', v)
        self.assertNotIn('collision_probability_bound', v)

    def test_meteoroid_flux_is_separate_from_conjunctions(self):
        """DebrisCalculator сводит сближения и поток раздельно."""
        w = Window('mmo', datetime(2024, 5, 10, 12),
                   datetime(2024, 5, 10, 13))
        m = DebrisCalculator.calculate_metrics(
            w,
            [DebrisObservation(datetime(2024, 5, 10, 12, 20), 10, '25544-1')],
            [MeteoroidSample(datetime(2024, 5, 10, 12, 30), 1e-8)],
        )
        self.assertEqual(m.n_conjunctions, 1)
        self.assertGreater(m.meteoroid_fluence_m2, 0)
        self.assertGreater(m.max_meteoroid_flux_m2_s, 0)


class TestProxyIndicatorsExplained(unittest.TestCase):
    """Прокси-показатели: поток частиц на спутнике ≠ доза в скафандре."""

    def test_goes_proton_is_proxy_not_dose(self):
        src = {**SRC, 'name': 'noaa_protons',
               'fetched_at': '2024-05-10T12:00:00Z'}
        events, _, _ = goes(
            [{'time_tag': '2024-05-10T12:00Z', 'energy': '>=10 MeV',
              'satellite': 18, 'flux': 50}],
            src, DEFAULT_CONFIG)
        e = events[0]
        # Проверяем, что в limitations явно указано, что это не доза
        joined = ' '.join(e['limitations']).lower()
        self.assertIn('dose', joined)

    def test_kp_is_planetary_proxy_not_local(self):
        events, _, _ = kp(
            [{'time_tag': '2024-05-10T12:00Z', 'kp': 7, 'observed': 'observed'}],
            SRC, DEFAULT_CONFIG)
        joined = ' '.join(events[0]['limitations']).lower()
        self.assertIn('planetary', joined)

    def test_alerts_do_not_invent_risk(self):
        """Bulletin не должен превращаться в независимый риск-инкремент."""
        from src.main.events.adapters import alerts
        data = [{
            'product_id': 'WARK04',
            'issue_datetime': '2024-05-10T12:00Z',
            'message': 'WARNING: Geomagnetic Storm\nValid Until: 2024 May 10 1800 UTC',
        }]
        events, coverage, context = alerts(data, SRC, DEFAULT_CONFIG)
        self.assertEqual(events, [])
        self.assertEqual(coverage, [])
        self.assertIn('Evidence only', context[0]['usage'])


class TestSourceProvenanceAvailable(unittest.TestCase):
    """O4: для каждого предупреждения доступен первоисточник."""

    def test_event_has_source_url_and_snapshot(self):
        e = event('x', 'sep', 'radiation',
                  '2024-05-10T12:00Z', '2024-05-10T13:00Z', SRC, {})
        self.assertEqual(e['source_url'], SRC['url'])
        self.assertEqual(e['snapshot_id'], SRC['snapshot_id'])
        self.assertIn('fetched_at', e)

    def test_event_has_rule_and_limitations(self):
        e = event('x', 'sep', 'radiation',
                  '2024-05-10T12:00Z', '2024-05-10T13:00Z', SRC, {},
                  rule='test rule', limitations=['test limitation'])
        self.assertEqual(e['rule'], 'test rule')
        self.assertIn('test limitation', e['limitations'])

    def test_event_has_basis_and_relevance(self):
        e = event('x', 'sep', 'radiation',
                  '2024-05-10T12:00Z', '2024-05-10T13:00Z', SRC, {},
                  basis='external_forecast', relevance='earth_environment_proxy')
        self.assertEqual(e['basis'], 'external_forecast')
        self.assertEqual(e['relevance'], 'earth_environment_proxy')

    def test_context_has_source_url(self):
        from src.main.events.adapters import alerts
        data = [{
            'product_id': 'X',
            'issue_datetime': '2024-05-10T12:00Z',
            'message': 'SUMMARY: test',
        }]
        _, _, context = alerts(data, SRC, DEFAULT_CONFIG)
        self.assertEqual(context[0]['source_url'], SRC['url'])
        self.assertEqual(context[0]['snapshot_id'], SRC['snapshot_id'])


class TestUncertaintyAndLimits(unittest.TestCase):
    """O2: уровни угрозы, неопределённость и ограничения применяются последовательно."""

    def test_open_event_marks_unknown_end(self):
        e = event('s', 'sep', 'radiation',
                  '2024-05-10T12:00Z', None, SRC, {}, temporal='open')
        r = assess(dict(events=[e], sources=[]), Q)
        rad = r['windows'][0]['factors']['radiation']
        self.assertIn('s', rad['possible_ongoing_event_ids'])
        self.assertEqual(rad['known_interval_overlap_minutes'], 0)

    def test_coverage_scope_is_product_specific(self):
        """Покрытие помечается как «selected products only»."""
        e = event('x', 'sep', 'radiation',
                  '2024-05-10T12:00Z', '2024-05-10T13:00Z', SRC, {})
        r = assess(dict(events=[e], sources=[]), Q)
        rad = r['windows'][0]['factors']['radiation']
        self.assertIn('coverage_scope', rad)
        self.assertNotEqual(rad['coverage_scope'], 'total')

    def test_no_double_counting_from_unknown_end(self):
        """Открытое событие не должно раздувать known_interval_overlap."""
        e = event('open', 'sep', 'radiation',
                  '2024-05-10T12:00Z', None, SRC, {}, temporal='open')
        r = assess(dict(events=[e], sources=[]), Q)
        rad = r['windows'][0]['factors']['radiation']
        self.assertEqual(rad['known_interval_overlap_minutes'], 0)

    def test_missing_critical_mechanism_blocks_recommendation(self):
        """Отсутствие критичных данных по механизму → insufficient_evidence."""
        r = assess(dict(events=[], sources=[]), Q)
        self.assertEqual(r['recommendation']['status'],
                         'insufficient_evidence')
        self.assertIsNone(r['recommendation']['preferred_window'])


class TestResearchReportBasis(unittest.TestCase):
    """Проверки, что demo может служить основой research-отчёта."""

    def test_demo_is_labelled_synthetic(self):
        result = make_demo(DEFAULT_CONFIG)
        self.assertTrue(result.get('synthetic'))
        self.assertTrue(any('SYNTHETIC' in lim.upper()
                            for lim in result['limitations']))

    def test_demo_covers_control_and_event_periods(self):
        """Demo содержит выраженное событие (Kp 7) и контрольные точки."""
        result = make_demo(DEFAULT_CONFIG)

        # --- Выраженное событие: Kp >= порога -> в events ---
        kp_values = [
            e['values']['kp']
            for e in result['events']
            if e['kind'] == 'geomagnetic_kp_episode'
        ]
        self.assertIn(7, kp_values, "Должно быть выраженное событие Kp=7")

        # --- Контрольные точки: Kp < порога -> только в coverage ---
        coverage_kp = [
            c for c in result.get('coverage_intervals', [])
            if c.get('mechanism') == 'geomagnetic'
        ]
        self.assertGreaterEqual(
            len(coverage_kp), 2,
            "Должны быть контрольные Kp-интервалы в coverage",
        )

        # --- В coverage есть интервалы, не попавшие в events ---
        event_windows = {
            (e['start'], e['end'])
            for e in result['events']
            if e['kind'] == 'geomagnetic_kp_episode'
        }
        control_intervals = [
            c for c in coverage_kp
            if (c['start'], c['end']) not in event_windows
        ]
        self.assertGreaterEqual(
            len(control_intervals), 1,
            "Должен быть хотя бы один контрольный (бессобытийный) интервал",
        )
    def test_demo_has_nonzero_and_zero_windows(self):
        """Есть окно с событиями и окно без них (для сравнения)."""
        result = make_demo(DEFAULT_CONFIG)
        overlaps = [w['factors']['geomagnetic']['known_interval_overlap_minutes']
                    for w in result['windows']]
        self.assertTrue(any(o > 0 for o in overlaps))
        self.assertTrue(any(o == 0 for o in overlaps))


if __name__ == '__main__':
    unittest.main()