"""Pure event/window calculations. Python 3.10+, no third-party dependencies."""

import math
from collections import Counter
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
VERSION = "1.0.0"
MECHANISMS = (
    "radiation",
    "geomagnetic",
    "communications",
    "tracked_debris",
    "meteoroids",
)


def dt(value, provider=False):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        if not provider:
            raise ValueError("Timestamp must contain Z or a UTC offset")
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def iso(value):
    return dt(value).isoformat().replace("+00:00", "Z")


def now():
    return datetime.now(UTC)


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite number")
    return result


def event(
    eid,
    kind,
    mechanism,
    start,
    end,
    source,
    record,
    *,
    basis="observation",
    relevance="earth_environment_proxy",
    temporal="interval",
    published=None,
    values=None,
    related=None,
    rule="",
    limitations=None,
):
    start = dt(start, provider=True)
    end = dt(end, provider=True) if end else None
    if end and end < start:
        raise ValueError("Event end precedes start")
    return dict(
        id=str(eid),
        kind=kind,
        mechanism=mechanism,
        start=iso(start),
        end=iso(end) if end else None,
        temporal=temporal,
        basis=basis,
        relevance=relevance,
        published_at=iso(dt(published, provider=True)) if published else None,
        source=source["name"],
        source_url=record.get("link") or source["url"],
        snapshot_id=source["snapshot_id"],
        fetched_at=source["fetched_at"],
        record_version=record.get("versionId"),
        related_ids=related or [],
        values=values or {},
        rule=rule,
        limitations=limitations or [],
        confidence="proxy_only" if relevance != "iss_conjunction" else "screening_only",
    )


def union_minutes(intervals):
    merged = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return round(sum((b - a).total_seconds() / 60 for a, b in merged), 6)


def latest_events(events):
    """One version per provider/event ID; prefer latest declared publication."""
    latest = {}
    for e in events:
        key = (e["source"], e["id"])
        rank = (
            e.get("published_at") or "",
            e.get("record_version") or 0,
            e["fetched_at"],
        )
        old = latest.get(key)
        if old is None or rank > old[0]:
            latest[key] = (rank, e)
    return [x[1] for x in latest.values()]


def group_count(events):
    """
    Считает число независимых групп событий.

    Два события попадают в одну группу, если они связаны через
    общий related_id (прямо или транзитивно), даже если сам related_id
    не входит в `events` (например, context_only CME).

    Гарантии:
      * Транзитивность: A→X, B→X,Y, C→Y ⇒ A, B, C в одной группе.
      * Отсутствующий related_ids не ломает группировку.
      * None, пустые строки и дубликаты в related_ids игнорируются.
      * Каждое событие учитывается ровно один раз.
    """
    # Union-Find по всем узлам (события + related_ids).
    parent = {}

    def find(x):
        # Итеративный path compression (без рекурсии — избегаем
        # RecursionError на длинных цепочках).
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def _clean_ids(values):
        """Убирает None, пустые строки, дубликаты. Сохраняет порядок."""
        if not values:
            return []
        seen = set()
        out = []
        for v in values:
            if not isinstance(v, str):
                continue
            v = v.strip()
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out

    # Первый проход: регистрируем все узлы и объединяем.
    event_ids = []
    for e in events:
        eid = e.get("id")
        if not isinstance(eid, str) or not eid:
            # Событие без id не может быть в группе — пропускаем,
            # но это не должно происходить в норме.
            continue
        event_ids.append(eid)
        find(eid)  # регистрируем узел

        related = _clean_ids(e.get("related_ids"))
        for other in related:
            union(eid, other)

    if not event_ids:
        return 0

    # Второй проход: считаем уникальные корни среди id событий.
    roots = {find(eid) for eid in event_ids}
    return len(roots)


def validate_query(q):
    start = dt(q["start"])
    duration = number(q.get("duration_hours", 6))
    search = number(q.get("search_hours", 0))
    step = number(q.get("step_minutes", 60))
    if not 1 <= duration <= 8 or not 0 <= search <= 24 or not 15 <= step <= 1440:
        raise ValueError(
            "duration_hours: 1..8; search_hours: 0..24; step_minutes: 15..1440"
        )
    mode = q.get("mode", "live")
    if mode not in ("live", "reconstruction", "as_of"):
        raise ValueError("mode: live, reconstruction, as_of")
    cutoff = dt(q["as_of"]) if mode == "as_of" else None
    if cutoff and cutoff > start:
        raise ValueError("as_of must not be later than window start")
    if mode == "live" and start < now() - timedelta(hours=1):
        raise ValueError("Use reconstruction for historical windows")
    return start, duration, search, step, mode, cutoff


def check_requirements(window, requirements=None):
    """Проверяет ограничения плана на одном окне. Отсутствие данных считается unknown."""
    requirements = requirements or {}
    factors = window.get("factors", {})
    checks = []
    for mechanism, limit_key in [
        ("radiation", "max_radiation_overlap_minutes"),
        ("geomagnetic", "max_geomagnetic_overlap_minutes"),
        ("communications", "max_communications_overlap_minutes"),
        ("tracked_debris", "max_tracked_debris_overlap_minutes"),
    ]:
        if limit_key not in requirements:
            continue
        f = factors.get(mechanism, {})
        value = f.get("known_interval_overlap_minutes")
        checks.append(
            {
                "requirement": limit_key,
                "mechanism": mechanism,
                "limit": requirements[limit_key],
                "value": value,
                "status": "unknown"
                if value is None
                else ("pass" if value <= requirements[limit_key] else "fail"),
            }
        )
    min_cov = requirements.get("min_data_coverage_fraction")
    if min_cov is not None:
        vals = [f.get("coverage_fraction") for f in factors.values()]
        value = min(vals) if vals else None
        checks.append(
            {
                "requirement": "min_data_coverage_fraction",
                "limit": min_cov,
                "value": value,
                "status": "unknown"
                if value is None
                else ("pass" if value >= min_cov else "fail"),
            }
        )

    if not checks:
        # Требования не заданы — это не "pass", это "не проверялось".
        return {
            "passed": False,
            "status": "not_checked",
            "checks": [],
            "reason": "No requirements were provided for this window.",
        }

    statuses = {x["status"] for x in checks}
    if "unknown" in statuses:
        status = "unknown"
    elif "fail" in statuses:
        status = "fail"
    else:
        status = "pass"
    return {
        "passed": status == "pass",
        "status": status,
        "checks": checks,
    }


# core.py — заменить select_best_window()

def select_best_window(windows, requirements=None, allow_unchecked=False):
    """
    Ранжирует окна по burden и покрытию.

    preferred_window_index возвращается только если:
      * требования заданы и хотя бы одно окно проходит, ИЛИ
      * allow_unchecked=True и есть окно с известным score.

    Иначе preferred_window_index=None, а ranked содержит всю диагностику.
    """
    requirements = requirements or {}
    req_provided = bool(requirements)
    ranked = []
    for i, w in enumerate(windows):
        req = check_requirements(w, requirements)
        burden = sum(
            f.get("known_interval_overlap_minutes", 0)
            for f in w.get("factors", {}).values()
        )
        coverage = min(
            (f.get("coverage_fraction", 0) for f in w.get("factors", {}).values()),
            default=0,
        )
        incomplete = any(
            f.get("status") == "insufficient_data"
            or f.get("possible_ongoing_event_ids")
            for f in w.get("factors", {}).values()
        )
        ranked.append({
            "window_index": i,
            "requirements": req,
            "burden_minutes": burden,
            "coverage_fraction": coverage,
            "incomplete": incomplete,
        })

    # Сортировка: pass > unknown > fail, затем burden, затем coverage
    def _key(x):
        s = x["requirements"]["status"]
        rank = {"pass": 0, "unknown": 1, "not_checked": 1, "fail": 2}.get(s, 3)
        return (rank, x["burden_minutes"], -x["coverage_fraction"], x["window_index"])

    ranked.sort(key=_key)

    # Условия для выдачи рекомендации
    if not ranked:
        return {
            "preferred_window_index": None,
            "reason": "Нет окон для сравнения.",
            "ranked": [],
            "recommendation_status": "no_windows",
        }

    best = ranked[0]

    if req_provided:
        if best["requirements"]["status"] == "pass":
            return {
                "preferred_window_index": best["window_index"],
                "reason": "Минимальный burden среди окон, прошедших требования.",
                "ranked": ranked,
                "recommendation_status": "recommended",
            }
        if best["requirements"]["status"] == "unknown":
            return {
                "preferred_window_index": None,
                "reason": "Требования заданы, но данные неполные: статус unknown.",
                "ranked": ranked,
                "recommendation_status": "insufficient_evidence",
            }
        return {
            "preferred_window_index": None,
            "reason": "Ни одно окно не проходит заданные требования.",
            "ranked": ranked,
            "recommendation_status": "no_eligible_window",
        }

    # Требования не заданы
    if best["incomplete"]:
        return {
            "preferred_window_index": None,
            "reason": "Требования не заданы, данные неполные: рекомендация невозможна.",
            "ranked": ranked,
            "recommendation_status": "insufficient_evidence",
        }
    if allow_unchecked:
        return {
            "preferred_window_index": best["window_index"],
            "reason": "Требования не заданы; выбран минимальный burden (allow_unchecked).",
            "ranked": ranked,
            "recommendation_status": "ranked_without_requirements",
        }
    return {
        "preferred_window_index": None,
        "reason": "Требования не заданы — рекомендация не выдаётся.",
        "ranked": ranked,
        "recommendation_status": "not_checked",
    }


def assess(bundle, q):
    """
    Считает метрики по окнам и формирует рекомендацию.

    Гарантии:
      * recommendation.status и recommendation.preferred_window согласованы:
        preferred_window не выдаётся при insufficient_evidence.
      * requirements.status = "not_checked", если требования не заданы.
      * Возраст данных разделён: snapshot_age / event_age / data_lag.
      * coverage и known_overlap считаются только по доступным данным.
    """
    start, duration, search, step, mode, cutoff = validate_query(q)

    # ------------------------------------------------------------------
    # 1. Нормализация событий
    # ------------------------------------------------------------------
    events = latest_events(bundle["events"])

    excluded = 0
    excluded_unknown_publication = 0

    if cutoff:
        kept = []
        for e in events:
            if dt(e["fetched_at"]) > cutoff:
                excluded += 1
                continue
            if not e.get("published_at"):
                if (
                    e.get("basis") == "external_forecast"
                    and dt(e["fetched_at"]) <= cutoff
                ):
                    e = {**e, "published_at": e["fetched_at"]}
                else:
                    excluded += 1
                    excluded_unknown_publication += 1
                    continue
            if dt(e["published_at"]) > cutoff:
                excluded += 1
                continue
            kept.append(e)
        events = kept

    # ------------------------------------------------------------------
    # 2. Окна
    # ------------------------------------------------------------------
    windows = []
    n_windows = int(search * 60 // step) + 1
    now_utc = now()

    for i in range(n_windows):
        a = start + timedelta(minutes=i * step)
        b = a + timedelta(hours=duration)
        factors = {}

        for mechanism in MECHANISMS:
            chosen, intervals, unknown = [], [], []

            for e in events:
                if e["mechanism"] != mechanism:
                    continue
                if e["relevance"] == "context_only":
                    continue

                s = dt(e["start"])
                t = dt(e["end"]) if e["end"] else None

                if e["temporal"] == "instant":
                    hit = a <= s < b
                elif t:
                    hit = s < b and t > a
                else:
                    hit = s < b

                if not hit:
                    continue

                chosen.append(e)
                if t:
                    intervals.append((max(a, s), min(b, t)))
                elif e["temporal"] != "instant":
                    unknown.append(e["id"])

            # ----------------------------------------------------------
            # Coverage: только по интервалам, известным на cutoff
            # ----------------------------------------------------------
            coverage_intervals = []
            for c in bundle.get("coverage", []):
                if c["mechanism"] != mechanism:
                    continue
                if cutoff and dt(c["fetched_at"]) > cutoff:
                    continue
                coverage_intervals.append(
                    (max(a, dt(c["start"])), min(b, dt(c["end"])))
                )

            coverage_minutes = union_minutes(coverage_intervals)
            coverage_fraction = (
                coverage_minutes / (duration * 60) if duration > 0 else 0.0
            )
            known_minutes = union_minutes(intervals)

            # ----------------------------------------------------------
            # Возраст данных: три раздельные метрики
            # ----------------------------------------------------------
            if chosen:
                snap_ages = [
                    max(0.0, (now_utc - dt(e["fetched_at"])).total_seconds() / 60)
                    for e in chosen
                ]
                event_ages = [
                    max(0.0, (now_utc - dt(e["start"])).total_seconds() / 60)
                    for e in chosen
                ]
                lags = [
                    max(
                        0.0,
                        (dt(e["fetched_at"]) - dt(e["published_at"])).total_seconds()
                        / 60,
                    )
                    for e in chosen
                    if e.get("published_at")
                ]
                snapshot_age = round(max(snap_ages), 1)
                event_age = round(max(event_ages), 1)
                data_lag = round(max(lags), 1) if lags else None
            else:
                snapshot_age = None
                event_age = None
                data_lag = None

            # ----------------------------------------------------------
            # Статус механизма
            # ----------------------------------------------------------
            if coverage_fraction >= 0.9999:
                mech_status = (
                    "events_require_review" if chosen else "no_detected_events"
                )
            else:
                mech_status = (
                    "events_and_missing_data" if chosen else "insufficient_data"
                )

            factors[mechanism] = dict(
                event_count=len(chosen),
                linked_group_count=group_count(chosen),
                event_types=dict(Counter(e["kind"] for e in chosen)),
                known_interval_overlap_minutes=known_minutes,
                possible_ongoing_event_ids=unknown,
                observed_event_count=sum(e["basis"] == "observation" for e in chosen),
                forecast_event_count=sum(
                    e["basis"] == "external_forecast" for e in chosen
                ),
                team_calculated_event_count=sum(
                    e["basis"] == "team_calculation" for e in chosen
                ),
                coverage_fraction=round(min(coverage_fraction, 1), 4),
                coverage_scope="selected_products_only_not_total_hazard_coverage",
                snapshot_age_minutes=snapshot_age,
                event_age_minutes=event_age,
                data_lag_minutes=data_lag,
                next_event_in_minutes=min(
                    (
                        (dt(e["start"]) - a).total_seconds() / 60
                        for e in events
                        if e["mechanism"] == mechanism
                        and e["relevance"] != "context_only"
                        and dt(e["start"]) >= a
                    ),
                    default=None,
                ),
                status=mech_status,
                event_ids=[e["id"] for e in chosen],
            )

        # ------------------------------------------------------------------
        # Разбивка перекрытия по basis
        # ------------------------------------------------------------------
        for mechanism in MECHANISMS:
            f = factors[mechanism]
            for basis in ("observation", "external_forecast", "team_calculation"):
                f[f"{basis}_overlap_minutes"] = union_minutes(
                    [
                        (max(a, dt(e["start"])), min(b, dt(e["end"])))
                        for e in events
                        if e["id"] in f["event_ids"]
                        and e["basis"] == basis
                        and e["end"]
                    ]
                )

        # ------------------------------------------------------------------
        # Debris-специфика: min miss distance, max speed
        # ------------------------------------------------------------------
        debris = [
            e for e in events
            if e["id"] in factors["tracked_debris"]["event_ids"]
        ]
        factors["tracked_debris"]["minimum_miss_distance_km"] = min(
            (e["values"].get("miss_distance_km") for e in debris),
            default=None,
        )
        factors["tracked_debris"]["maximum_relative_speed_km_s"] = max(
            (e["values"].get("relative_speed_km_s") for e in debris),
            default=None,
        )

        windows.append(
            dict(
                start=iso(a),
                end=iso(b),
                duration_hours=duration,
                factors=factors,
            )
        )

    # ------------------------------------------------------------------
    # 3. Парето-фронтир по burden
    # ------------------------------------------------------------------
    vectors = [
        tuple(
            w["factors"][m]["known_interval_overlap_minutes"] for m in MECHANISMS
        )
        for w in windows
    ]
    frontier = [
        i
        for i, v in enumerate(vectors)
        if not any(
            all(x <= y for x, y in zip(other, v))
            and any(x < y for x, y in zip(other, v))
            for other in vectors
        )
    ]

    # ------------------------------------------------------------------
    # 4. Достаточность данных
    # ------------------------------------------------------------------
    # Достаточно, если по КАЖДОМУ механизму есть хотя бы одно из:
    #   * хотя бы одно событие в окне
    #   * coverage_fraction >= 0.5 (данные покрывают половину окна)
    # Полное покрытие (1.0) не требуется — реальные источники редко дают 100%.
    sufficient = all(
        any(
            w["factors"][m]["event_count"] > 0
            or w["factors"][m]["coverage_fraction"] >= 0.5
            for w in windows
        )
        for m in MECHANISMS
    )

    # ------------------------------------------------------------------
    # 5. Выбор окна
    # ------------------------------------------------------------------
    if not events:
        selection = {
            "preferred_window_index": None,
            "reason": "нет событий и недостаточно данных для выбора",
            "ranked": [],
            "recommendation_status": "no_events",
        }
    else:
        selection = select_best_window(windows, q.get("requirements"))

    # ------------------------------------------------------------------
    # 6. Согласование recommendation
    # ------------------------------------------------------------------
    # Правило:
    #   * insufficient_evidence   → preferred_window = None
    #   * review_candidates       → preferred_window = выбранное окно
    #   * not_checked / no_eligible_window / no_events
    #                             → preferred_window = None
    if not sufficient:
        rec_status = "insufficient_evidence"
        preferred_for_output = None
        missing = [
            m for m in MECHANISMS
            if not any(
                w["factors"][m]["event_count"] > 0 or w["factors"][m]["coverage_fraction"] >= 0.5
                for w in windows
            )
        ]
        rec_reason = (
            f"Недостаточно данных по механизмам: {', '.join(missing)}. "
            "Расчёт выполнит оценку, но точность ограничена."
            if missing
            else "Данные загружены. Рекомендация основана на доступных источниках."
        )
    elif selection["recommendation_status"] == "recommended":
        rec_status = "review_candidates"
        preferred_for_output = selection["preferred_window_index"]
        rec_reason = (
            "Candidates ranked by event burden under provided requirements."
        )
    else:
        rec_status = selection["recommendation_status"]
        preferred_for_output = None
        rec_reason = selection.get("reason", "Рекомендация не выдаётся.")

    preferred_window = (
        windows[preferred_for_output]
        if preferred_for_output is not None
        else None
    )

    # ------------------------------------------------------------------
    # 7. Результат
    # ------------------------------------------------------------------
    return dict(
        algorithm_version=VERSION,
        query=q,
        generated_at=iso(now_utc),
        windows=windows,
        events=events,
        context=bundle.get("context", []),
        sources=bundle["sources"],
        exclusions_after_cutoff=excluded,
        excluded_unknown_publication=excluded_unknown_publication,
        event_burden_frontier_indices=frontier,
        recommendation=dict(
            status=rec_status,
            preferred_window=preferred_window,
            window_selection=selection,
            reason=rec_reason,
        ),
        limitations=bundle.get("limitations", []),
    )
