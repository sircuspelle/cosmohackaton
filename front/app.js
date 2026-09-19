const state = {
  result: null,
  selected: 0,
};

const demoResult = {
  query: {
    start: "2024-05-10T12:00:00Z",
    duration_hours: 6,
    mode: "reconstruction",
  },
  generated_at: new Date().toISOString(),
  windows: [],
  events: [],
  sources: [],
  limitations: [
    "Результат не загрузился. Проверьте доступность result.json.",
  ],
  recommendation: {
    status: "insufficient_evidence",
    reason: "Нет результата расчёта.",
  },
};

async function loadResult() {
  try {
    const response = await fetch("../result.json", {
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error("result.json unavailable");
    }

    state.result = await response.json();

    document.querySelector("#sourceState").textContent =
      "Результат загружен";
  } catch (error) {
    state.result = demoResult;

    document.querySelector("#sourceState").textContent =
      "Нет файла результата";
  }

  render(state.result);
}

function formatDate(value) {
  if (!value) {
    return "—";
  }

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
    events_require_review: "Нужна проверка",
    events_and_missing_data: "События и пробелы",
    no_detected_events: "События не обнаружены",
    insufficient_data: "Недостаточно данных",
  };

  return statuses[status] || status || "Неизвестно";
}

function factorCoverage(factor) {
  return Math.round((factor?.coverage_fraction || 0) * 100);
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

  const recommendation = document.querySelector(
    "#recommendationContent"
  );

  recommendation.className = "";

  recommendation.innerHTML = `
    <h2>${statusText(result.recommendation?.status)}</h2>
    <p>
      ${result.recommendation?.reason ||
    "Результат готов к проверке."
    }
    </p>
    ${preferred
      ? `
          <div class="recommendation-time">
            ${formatDate(preferred.start)}
            —
            ${formatDate(preferred.end)}
          </div>
        `
      : ""
    }
  `;

  document.querySelector("#windowsGrid").innerHTML =
    windows.length
      ? windows
        .map((window, index) => {
          const factors = Object.entries(window.factors || {});

          const hasEvents = factors.some(
            ([, factor]) => (factor.event_count || 0) > 0
          );

          const incomplete = factors.some(
            ([, factor]) =>
              factor.status?.includes("missing") ||
              factor.status === "insufficient_data"
          );

          const pill = incomplete
            ? ["Проверить", "risk-review"]
            : hasEvents
              ? ["Есть события", "risk-review"]
              : ["Данных достаточно", "risk-good"];

          return `
              <article
                class="window-card ${index === state.selected ? "selected" : ""
            }"
                data-index="${index}"
              >
                <div class="window-top">
                  <span class="window-index">
                    ОКНО ${String(index + 1).padStart(2, "0")}
                  </span>

                  <span class="risk-pill ${pill[1]}">
                    ${pill[0]}
                  </span>
                </div>

                <div class="window-time">
                  ${formatDate(window.start)}
                  —
                  ${formatDate(window.end)}
                </div>

                ${factors
              .slice(0, 3)
              .map(
                ([name, factor]) => `
                      <div class="factor-mini">
                        <span>${mechanismName(name)}</span>

                        <div class="bar">
                          <i
                            style="
                              width: ${Math.max(
                  5,
                  factorCoverage(factor)
                )}%
                            "
                          ></i>
                        </div>

                        <span>${factorCoverage(factor)}%</span>
                      </div>
                    `
              )
              .join("")}
              </article>
            `;
        })
        .join("")
      : `
        <div class="window-card">
          <strong>Нет доступных окон</strong>
          <p class="muted">
            Проверьте входные данные и архивы.
          </p>
        </div>
      `;

  document
    .querySelectorAll(".window-card[data-index]")
    .forEach((card) => {
      card.addEventListener("click", () => {
        state.selected = Number(card.dataset.index);
        render(result);
      });
    });

  const selected = windows[state.selected] || windows[0];
  const factors = selected?.factors || {};

  document.querySelector("#factorList").innerHTML =
    Object.entries(factors)
      .map(
        ([name, factor]) => `
          <div class="factor-row">
            <div>
              <div class="factor-name">
                ${mechanismName(name)}
              </div>

              <div class="factor-meta">
                ${factor.event_count || 0} событий ·
                ${factor.known_interval_overlap_minutes || 0
          } мин перекрытия
              </div>
            </div>

            <div class="status-label">
              ${statusText(factor.status)}
              <br />
              ${factorCoverage(factor)}% покрытия
            </div>
          </div>
        `
      )
      .join("") ||
    `
      <p class="muted">
        Факторы не переданы.
      </p>
    `;

  document.querySelector("#sourceList").innerHTML =
    (result.sources || [])
      .map(
        (source) => `
          <div class="source-row">
            <div>
              <div class="source-name">
                ${source.name || "Источник"}
              </div>

              <div class="source-meta">
                ${source.fetched_at
            ? formatDate(source.fetched_at)
            : "время неизвестно"
          }
              </div>
            </div>

            <div class="status-label">
              ${source.status || "без статуса"}
              <br />
              ${source.event_count ?? 0} событий
            </div>
          </div>
        `
      )
      .join("") ||
    `
      <p class="muted">
        Источники не переданы.
      </p>
    `;

  document.querySelector("#limitationsList").innerHTML =
    (result.limitations || [])
      .map((item) => `<li>${item}</li>`)
      .join("") ||
    "<li>Ограничения не указаны.</li>";
}

document
  .querySelector("#reloadButton")
  .addEventListener("click", loadResult);

document
  .querySelector("#assessButton")
  .addEventListener("click", () => {
    const start = document.querySelector("#startInput").value;
    const duration = document.querySelector("#durationInput").value;
    const mode = document.querySelector("#modeInput").value;

    const query = {
      start: start
        ? new Date(start).toISOString()
        : new Date().toISOString(),
      duration_hours: Number(duration),
      search_hours: 6,
      step_minutes: 60,
      mode,
    };

    document.querySelector("#sourceState").textContent =
      "Расчёт выполняется…";

    fetch(
      window.EVA_API_URL ||
      "http://127.0.0.1:8000/assess",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(query),
      }
    )
      .then((response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        return response.json();
      })
      .then((result) => {
        state.selected = 0;
        state.result = result;

        document.querySelector("#sourceState").textContent =
          "Расчёт завершён";

        render(result);
      })
      .catch(() => {
        document.querySelector("#sourceState").textContent =
          "API недоступен: показан последний результат";
      });
  });

loadResult();
