const state = {
  result: null,
  selected: 0,
};

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function mechanismName(name) {
  const names = {
    radiation: "Радиация",
    geomagnetic: "Геомагнитная обстановка",
    communications: "Связь",
    tracked_debris: "Сближения",
    meteoroids: "Микрометеороиды",
  };
  return names[name] || name;
}

function statusText(status) {
  const statuses = {
    review_candidates: "Кандидаты для проверки",
    insufficient_evidence: "Недостаточно данных",
    review_with_caveats: "Оценка с оговорками",
    events_require_review: "Нужна проверка",
    events_and_missing_data: "События и пробелы",
    no_detected_events: "События не обнаружены",
    insufficient_data: "Недостаточно данных",
    ok: "Норма",
  };
  return statuses[status] || status || "Неизвестно";
}

function factorScore(factor) {
  const events = factor?.event_count || 0;
  const overlap = factor?.known_interval_overlap_minutes || 0;
  if (events === 0 && overlap === 0) return { pct: 0, label: "Нет событий" };
  const score = Math.min(100, Math.round((overlap / 360) * 100));
  return { pct: score, label: `${events}соб. / ${overlap}мин` };
}

function render(result) {
  const windows = result.windows || [];
  const preferred = result.recommendation?.preferred_window;

  document.querySelector("#eventCount").textContent =
    result.events?.length || 0;

  document.querySelector("#sourceCount").textContent =
    `${result.sources?.length || 0} источников`;

  document.querySelector("#windowCount").textContent =
    `${windows.length} вариантов`;

  document.querySelector("#generatedAt").textContent =
    `Обновлено ${formatDate(result.generated_at)}`;

  document.querySelector("#coverageText").textContent =
    result.exclusions_after_cutoff
      ? `${result.exclusions_after_cutoff} исключено`
      : "Отсечки нет";

  const recommendation = document.querySelector("#recommendationContent");
  recommendation.className = "";
  recommendation.innerHTML = `
    <h2>${statusText(result.recommendation?.status)}</h2>
    <p>${result.recommendation?.reason || "Результат готов к проверке."}</p>
    ${preferred
      ? `<div class="recommendation-time">${formatDate(preferred.start)} — ${formatDate(preferred.end)}</div>`
      : ""
    }
  `;

  document.querySelector("#windowsGrid").innerHTML =
    windows.length
      ? windows
          .map((window, index) => {
            const factors = Object.entries(window.factors || {});
            const hasEvents = factors.some(([, f]) => (f.event_count || 0) > 0);
            const incomplete = factors.some(
              ([, f]) => f.status?.includes("missing") || f.status === "insufficient_data"
            );

            const pill = incomplete
              ? ["Проверить", "risk-review"]
              : hasEvents
                ? ["Есть события", "risk-review"]
                : ["Данных достаточно", "risk-good"];

            return `
              <article class="window-card ${index === state.selected ? "selected" : ""}" data-index="${index}">
                <div class="window-top">
                  <span class="window-index">ОКНО ${String(index + 1).padStart(2, "0")}</span>
                  <span class="risk-pill ${pill[1]}">${pill[0]}</span>
                </div>
                <div class="window-time">
                  ${formatDate(window.start)} — ${formatDate(window.end)}
                </div>
                ${factors.slice(0, 3).map(([name, factor]) => {
                  const s = factorScore(factor);
                  return `
                    <div class="factor-mini">
                      <span>${mechanismName(name)}</span>
                      <div class="bar"><i style="width: ${Math.max(5, s.pct)}%"></i></div>
                      <span>${s.pct}%</span>
                    </div>`;
                }).join("")}
              </article>`;
          })
          .join("")
      : `<div class="window-card"><strong>Нет доступных окон</strong><p class="muted">Проверьте входные данные.</p></div>`;

  document.querySelectorAll(".window-card[data-index]").forEach((card) => {
    card.addEventListener("click", () => {
      state.selected = Number(card.dataset.index);
      render(result);
    });
  });

  const selected = windows[state.selected] || windows[0];
  const factors = selected?.factors || {};

  document.querySelector("#factorList").innerHTML =
    Object.entries(factors)
      .map(([name, factor]) => {
        const s = factorScore(factor);
        return `
          <div class="factor-row">
            <div>
              <div class="factor-name">${mechanismName(name)}</div>
              <div class="factor-meta">
                ${factor.event_count || 0} событий · ${factor.known_interval_overlap_minutes || 0} мин перекрытия
              </div>
            </div>
            <div class="status-label">
              ${statusText(factor.status)}
              <br />${s.label}
            </div>
          </div>`;
      })
      .join("") || `<p class="muted">Факторы не переданы.</p>`;

  document.querySelector("#sourceList").innerHTML =
    (result.sources || [])
      .map(
        (source) => `
          <div class="source-row">
            <div>
              <div class="source-name">${source.name || "Источник"}</div>
              <div class="source-meta">
                ${source.fetched_at ? formatDate(source.fetched_at) : "время неизвестно"}
              </div>
            </div>
            <div class="status-label">
              ${source.status || "без статуса"}
              <br />${source.event_count ?? source.context_count ?? 0} событий
            </div>
          </div>`
      )
      .join("") || `<p class="muted">Источники не переданы.</p>`;

  document.querySelector("#limitationsList").innerHTML =
    (result.limitations || [])
      .map((item) => `<li>${item}</li>`)
      .join("") || "<li>Ограничения не указаны.</li>";
}

function setLoading() {
  document.querySelector("#recommendationContent").innerHTML =
    `<h2>Ожидание расчёта</h2><p>Введите параметры и нажмите «Оценить окна».</p>`;
  document.querySelector("#windowsGrid").innerHTML =
    `<div class="window-card"><p class="muted">Результаты появятся после расчёта.</p></div>`;
  document.querySelector("#factorList").innerHTML =
    `<p class="muted">Нет данных.</p>`;
  document.querySelector("#sourceList").innerHTML =
    `<p class="muted">Нет данных.</p>`;
  document.querySelector("#limitationsList").innerHTML =
    `<li>Ограничения не указаны.</li>`;
  document.querySelector("#eventCount").textContent = "—";
  document.querySelector("#sourceCount").textContent = "—";
  document.querySelector("#windowCount").textContent = "";
  document.querySelector("#generatedAt").textContent = "";
  document.querySelector("#coverageText").textContent = "";
}

function assess() {
  const start = document.querySelector("#startInput").value;
  const duration = document.querySelector("#durationInput").value;
  const mode = document.querySelector("#modeInput").value;

  const query = {
    start: start ? new Date(start).toISOString() : new Date().toISOString(),
    duration_hours: Number(duration),
    search_hours: 6,
    step_minutes: 60,
    mode,
    force_insufficient: document.querySelector("#forceInsufficient").checked,
  };

  const btn = document.querySelector("#assessButton");
  btn.disabled = true;
  btn.textContent = "Расчёт…";
  document.querySelector("#sourceState").textContent = "Расчёт выполняется…";

  const sections = ["#recommendationContent", "#windowsGrid", "#factorList", "#sourceList", "#limitationsList"];
  sections.forEach((sel) => {
    const el = document.querySelector(sel);
    el.classList.add("results-loading");
    if (sel === "#windowsGrid") {
      el.innerHTML = `<div class="loading-pulse"><span>●</span><span>●</span><span>●</span><span>Загрузка данных…</span></div>`;
    }
  });

  fetch("/assess", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(query),
  })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((result) => {
      state.selected = 0;
      state.result = result;
      document.querySelector("#sourceState").textContent = "Расчёт завершён";
      render(result);
    })
    .catch((err) => {
      document.querySelector("#sourceState").textContent =
        `Ошибка: ${err.message}`;
    })
    .finally(() => {
      btn.disabled = false;
      btn.innerHTML = "Оценить окна <span>→</span>";
      sections.forEach((sel) => document.querySelector(sel)?.classList.remove("results-loading"));
    });
}

document.querySelector("#assessButton").addEventListener("click", assess);
document.querySelector("#reloadButton").addEventListener("click", assess);

const modeDescriptions = {
  reconstruction: "Использует архивные данные DONKI и NOAA для анализа конкретного прошлого периода. Подходит для изучения прошлых солнечных событий и штормов.",
  live: "Анализирует данные в реальном времени из NOAA SWPC и DONKI. Дата автоматически устанавливается на текущую. Подходит для планирования ближайших выходов.",
  as_of: "Воссоздаёт обстановку на конкретный момент времени, используя только данные доступные до этого момента. Подходит для точного моделирования прошлых решений.",
};

function updateModeDescription() {
  const mode = document.querySelector("#modeInput").value;
  document.querySelector("#modeDescription").textContent = modeDescriptions[mode] || "";
}

document.querySelector("#modeInput").addEventListener("change", (e) => {
  updateModeDescription();
  const input = document.querySelector("#startInput");
  if (e.target.value === "live") {
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    input.value = now.toISOString().slice(0, 16);
  }
});

const startInput = document.querySelector("#startInput");
if (!startInput.value) {
  const now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  startInput.value = now.toISOString().slice(0, 16);
}

updateModeDescription();
setLoading();
