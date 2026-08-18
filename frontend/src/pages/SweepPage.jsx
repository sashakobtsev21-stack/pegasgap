import { useEffect, useRef, useState } from "react";
import { m } from "framer-motion";
import {
  AlertTriangle, CheckCircle2, ChevronDown, ListChecks, Loader2, Play, Radar,
} from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { getJson, postJson } from "../lib/api.js";
import { formatDate } from "../lib/format.js";

/**
 * SweepPage — обход всей матрицы направлений из scenarios.yaml.
 *
 * Обход идёт в фоне на сервере, страница только опрашивает состояние: прогон матрицы
 * длится минуты, и держать его на открытой вкладке было бы ошибкой — закрыл браузер,
 * потерял ночной обход.
 */
const POLL_MS = 2000;

export default function SweepPage() {
  const [matrix, setMatrix] = useState(null);
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  // Список свёрнут по умолчанию: он нужен, чтобы свериться, а не чтобы жить на экране.
  const [showList, setShowList] = useState(false);
  const timer = useRef(null);

  useEffect(() => {
    getJson("/api/sweep/matrix").then(setMatrix).catch(() => {});
    poll();
    return () => clearTimeout(timer.current);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function poll() {
    getJson("/api/sweep")
      .then((s) => {
        setState(s);
        // Опрашиваем, только пока обход идёт: законченный обход не меняется, и
        // фоновый пуллинг простаивающей вкладки — чистый шум в логах.
        if (s.running) timer.current = setTimeout(poll, POLL_MS);
      })
      .catch((e) => setError(String(e.message || e)));
  }

  async function start() {
    setError(null);
    try {
      await postJson("/api/sweep", {}, { timeoutMs: 30_000 });
      poll();
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  const hasHotels = matrix?.modes?.includes("hotels");
  const running = state?.running;
  const done = state?.done ?? 0;
  const total = state?.total ?? matrix?.total ?? 0;
  const pct = total ? Math.round((done / total) * 100) : 0;

  return (
    <div className="mx-auto max-w-4xl">
      <m.div variants={staggerContainer} initial="hidden" animate="show" className="space-y-4">
        <GlassCard variants={fadeUp} className="p-5">
          <div className="flex flex-wrap items-center gap-3">
            <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand to-ocean shadow-glow">
              <Radar className="size-5 text-white" />
            </span>
            <div className="min-w-0">
              <h1 className="text-xl font-extrabold tracking-tight text-white">Обход матрицы</h1>
              <p className="text-xs text-muted">
                Проверяет разом все направления из списка — то, что вешают на ночное расписание
              </p>
            </div>
            <button
              onClick={start}
              disabled={running}
              className="ml-auto flex items-center gap-2 rounded-xl bg-gradient-to-r from-brand to-ocean px-4 py-2.5 text-sm font-bold text-white shadow-glow transition-opacity hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {running
                ? <><Loader2 className="size-4 animate-spin" /> Идёт обход…</>
                : <><Play className="size-4" /> Запустить</>}
            </button>
          </div>

          {/* Состав матрицы — фактом, а не одной строкой с перечислением: до запуска это
              единственное, что отвечает на вопрос «а что именно сейчас проверится». */}
          {matrix ? (
            <div className="mt-4 grid gap-2 sm:grid-cols-3">
              <Spec label="Сценариев" value={matrix.total} />
              <Spec label="Направления" value={matrix.countries.join(", ")} />
              <Spec label="Города вылета" value={matrix.departure_cities.join(", ")} />
            </div>
          ) : (
            <p className="mt-4 text-xs text-muted">Загружаю матрицу…</p>
          )}

          <p className="mt-3 text-xs text-muted">
            Список правится в файле <code className="text-ink">scenarios.yaml</code> в корне
            проекта. Окна дат там заданы смещением от дня запуска, поэтому конкретные даты
            ниже — на сегодня.
            {hasHotels && (
              <> Режим «Отели» читается браузером и идёт заметно дольше туров —
              обход с ним занимает минуты, а не секунды.</>
            )}
          </p>
        </GlassCard>

        {/* Что именно проверится — списком. Абстракция «12 сценариев» не отвечает на этот
            вопрос, а даты по конфигу вообще не прочитать: там смещения, не числа. */}
        {matrix?.scenarios?.length > 0 && (
          <GlassCard variants={fadeUp} className="p-5">
            <button
              type="button"
              onClick={() => setShowList((v) => !v)}
              className="flex w-full items-center gap-2 text-left text-sm font-bold text-white"
            >
              <ListChecks className="size-4 text-brand-soft" />
              Что будет проверено · {matrix.scenarios.length}
              <ChevronDown
                className={`ml-auto size-4 text-muted transition-transform ${showList ? "rotate-180" : ""}`}
              />
            </button>

            {showList && (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[560px] text-sm">
                  <thead>
                    <tr className="border-b border-white/10 text-left text-xs uppercase tracking-wider text-muted">
                      <th className="py-2 pr-3 font-semibold">#</th>
                      <th className="py-2 pr-3 font-semibold">Направление</th>
                      <th className="py-2 pr-3 font-semibold">Режим</th>
                      <th className="py-2 pr-3 font-semibold">Окно вылета</th>
                      <th className="py-2 font-semibold">Ночей</th>
                    </tr>
                  </thead>
                  <tbody>
                    {matrix.scenarios.map((s, i) => (
                      <tr key={i} className="border-b border-white/5">
                        <td className="py-1.5 pr-3 tabular-nums text-muted">{i + 1}</td>
                        <td className="py-1.5 pr-3 text-ink">
                          {s.departure_city} → {s.country}
                        </td>
                        <td className="py-1.5 pr-3 text-muted">
                          {s.mode === "hotels" ? "отели (без перелёта)" : "туры (с перелётом)"}
                        </td>
                        <td className="py-1.5 pr-3 tabular-nums text-muted">
                          {formatDate(s.date_from)} – {formatDate(s.date_to)}
                        </td>
                        <td className="py-1.5 tabular-nums text-muted">{s.nights}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </GlassCard>
        )}

        {error && (
          <GlassCard variants={fadeUp} className="p-5 text-sm text-rose-300">{error}</GlassCard>
        )}

        {state && !state.running && !state.finished_at && (
          <GlassCard variants={fadeUp} className="p-8">
            <p className="text-center text-sm text-muted">
              Обход ещё не запускался. Результаты появятся здесь, а находки — на экране
              «История».
            </p>
          </GlassCard>
        )}

        {state && (state.running || state.finished_at) && (
          <GlassCard variants={fadeUp} className="p-5">
            <div className="mb-2 flex items-baseline justify-between text-sm">
              <span className="font-semibold text-ink">
                {running ? "Выполняется" : "Завершён"} · {done} из {total}
              </span>
              <span className="tabular-nums text-muted">{pct}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-white/10">
              <div
                className="h-full rounded-full bg-gradient-to-r from-brand to-ocean transition-[width] duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>

            {state.results?.length > 0 && (
              <ul className="mt-4 divide-y divide-white/5">
                {state.results.map((r) => (
                  <li key={r.run_id ?? `${r.country}-${r.mode}`}>
                    <a
                      href={r.run_id ? `#/run/${r.run_id}` : undefined}
                      className="flex items-center gap-3 py-2 text-sm transition-colors hover:bg-white/[0.03]"
                    >
                      <span className="min-w-0 flex-1 truncate text-ink">
                        {r.departure_city} → {r.country}
                        <span className="ml-1.5 text-[11px] text-muted">
                          {r.mode === "hotels" ? "отели" : "туры"}
                        </span>
                      </span>
                      {r.trustworthy === false ? (
                        <span className="flex items-center gap-1 text-xs text-rose-300">
                          <AlertTriangle className="size-3.5" /> недостоверен
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-xs text-muted">
                          <CheckCircle2 className="size-3.5 text-emerald-400/70" />
                          {r.gaps} наход.
                        </span>
                      )}
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </GlassCard>
        )}
      </m.div>
    </div>
  );
}

function Spec({ label, value }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div className="truncate text-sm font-semibold text-ink" title={String(value)}>{value}</div>
    </div>
  );
}
