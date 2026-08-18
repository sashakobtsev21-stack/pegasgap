import { useCallback, useEffect, useState } from "react";
import { m } from "framer-motion";
import { CheckCircle2, Circle, Loader2, Radar, Wifi, WifiOff } from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { formatDate, formatShortDateTime } from "../lib/format.js";
import { getJson, postJson } from "../lib/api.js";
import { useEventStream } from "../lib/stream.js";

/**
 * MonitorPage — отчёт, который наполняется по ходу проверки.
 *
 * Только отчёт: запускают проверки на «Проверке», разбирают подробности в «Логах».
 * Экран, на котором и пульт, и результат, плох тем, что во время долгого прогона
 * половина его занята кнопками, которые уже нажали.
 *
 * Живой поток, а не опрос: находки падают наверх списка по мере появления, накопленные
 * подтягиваются из базы — иначе до первой находки текущей сессии экран был бы пустым.
 */
const KIND_TONE = {
  full: "border-rose-400/30 bg-rose-500/15 text-rose-200",
  not_responding: "border-amber-400/30 bg-amber-500/15 text-amber-200",
  hotel: "border-brand/30 bg-brand/15 text-brand-soft",
  price: "border-ocean/30 bg-ocean/15 text-ocean",
  reverse: "border-white/10 bg-white/[0.05] text-muted",
};

export default function MonitorPage() {
  const { findings: live, state: liveState, connected } = useEventStream();
  const [stored, setStored] = useState(null);
  const [worker, setWorker] = useState(null);
  const [onlyOpen, setOnlyOpen] = useState(false);

  const reload = useCallback(() => {
    getJson(`/api/findings?days=30&only_open=${onlyOpen}`).then(setStored).catch(() => {});
    getJson("/api/worker").then(setWorker).catch(() => {});
  }, [onlyOpen]);

  useEffect(reload, [reload]);
  // Новая находка в потоке — подтягиваем накопленное, чтобы у строки появился id и с ней
  // можно было работать (отметить разобранной).
  useEffect(() => { if (live.length) reload(); }, [live.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const running = liveState?.running ?? worker?.running ?? false;
  const queue = worker?.queue || stored?.summary?.queue || {};
  const summary = stored?.summary || {};

  async function toggleReview(finding) {
    const next = !finding.reviewed;
    setStored((prev) => prev && {
      ...prev,
      findings: prev.findings.map((f) =>
        f.id === finding.id ? { ...f, reviewed: next } : f),
    });
    try {
      await postJson(`/api/findings/${finding.id}/review?reviewed=${next}`, {});
    } catch {
      reload();     // не приняли — возвращаем то, что на сервере
    }
  }

  return (
    <m.div variants={staggerContainer} initial="hidden" animate="show"
           className="mx-auto max-w-6xl space-y-4">
      <GlassCard variants={fadeUp} className="p-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand to-ocean shadow-glow">
            <Radar className={`size-5 text-white ${running ? "animate-pulse" : ""}`} />
          </span>
          <div className="min-w-0">
            <h1 className="text-xl font-extrabold tracking-tight text-white">Мониторинг</h1>
            <p className="truncate text-xs text-muted">
              {running
                ? (liveState?.current || worker?.current || "готовлюсь…")
                : "проверка не идёт — запустить можно на вкладке «Проверка»"}
            </p>
          </div>
          <span className={`ml-auto flex items-center gap-1.5 text-xs ${connected ? "text-emerald-300" : "text-muted"}`}>
            {connected ? <Wifi className="size-3.5" /> : <WifiOff className="size-3.5" />}
            {connected ? "поток live" : "поток оборван"}
          </span>
        </div>

        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Проверено кейсов" value={queue.checked ?? 0} of={queue.total} />
          <Stat label="Найдено несоответствий" value={summary.total ?? 0} tone="brand" />
          <Stat label="Не разобрано" value={summary.open ?? 0} tone="amber" />
          <Stat label="Осталось в очереди" value={queue.pending ?? 0} />
        </div>

        {(liveState?.errors ?? worker?.errors ?? 0) > 0 && (
          <p className="mt-3 text-xs text-amber-300">
            Сбоев на кейсах: {liveState?.errors ?? worker?.errors}
            {(liveState?.last_error || worker?.last_error) &&
              ` — последний: ${liveState?.last_error || worker?.last_error}`}
          </p>
        )}
      </GlassCard>

      <GlassCard variants={fadeUp} className="p-5">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <h2 className="text-base font-bold text-white">
            Чего нет на Слетать{stored ? ` · ${stored.findings.length}` : ""}
          </h2>
          <label className="ml-auto flex cursor-pointer items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={onlyOpen}
              onChange={(e) => setOnlyOpen(e.target.checked)}
              className="size-3.5 accent-brand"
            />
            только неразобранные
          </label>
        </div>

        {!stored ? (
          <div className="grid place-items-center py-10">
            <Loader2 className="size-6 animate-spin text-muted" />
          </div>
        ) : stored.findings.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted">
            Пока ничего не найдено. Запустите мониторинг — находки будут появляться здесь
            по ходу проверки.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-sm">
              <thead>
                <tr className="border-b border-white/10 text-left text-xs uppercase tracking-wider text-muted">
                  <th className="w-10 py-2 pr-2 font-semibold">✓</th>
                  <th className="py-2 pr-3 font-semibold">Оператор</th>
                  <th className="py-2 pr-3 font-semibold">Направление и даты</th>
                  <th className="py-2 pr-3 font-semibold">Туристы</th>
                  <th className="py-2 pr-3 font-semibold">Класс</th>
                  <th className="py-2 pr-3 font-semibold">Отель</th>
                  <th className="py-2 font-semibold">Причина</th>
                </tr>
              </thead>
              <tbody>
                {stored.findings.map((f) => (
                  <tr key={f.id}
                      className={`border-b border-white/5 align-top ${f.reviewed ? "opacity-45" : ""}`}>
                    <td className="py-2 pr-2">
                      <button
                        onClick={() => toggleReview(f)}
                        title={f.reviewed ? "Снять отметку" : "Отметить разобранным"}
                        className="text-muted transition-colors hover:text-emerald-300"
                      >
                        {f.reviewed
                          ? <CheckCircle2 className="size-4 text-emerald-400" />
                          : <Circle className="size-4" />}
                      </button>
                    </td>
                    <td className="py-2 pr-3 whitespace-nowrap font-medium text-ink">
                      {f.operator}
                    </td>
                    <td className="py-2 pr-3">
                      <div className="font-medium text-ink">
                        {f.departure_city} → {f.country}
                      </div>
                      <div className="text-[11px] text-muted">
                        {formatDate(f.date_from)}–{formatDate(f.date_to)} ·{" "}
                        {f.search_mode === "hotels" ? "отели" : "туры"} ·{" "}
                        {formatShortDateTime(f.run_at)}
                      </div>
                    </td>
                    <td className="py-2 pr-3 whitespace-nowrap text-muted">
                      {f.params?.adults} взр.
                      {f.params?.children_ages?.length
                        ? ` + ${f.params.children_ages.length} реб. (${f.params.children_ages.join(", ")})`
                        : ""}
                    </td>
                    <td className="py-2 pr-3">
                      <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-semibold ${KIND_TONE[f.kind] || KIND_TONE.reverse}`}>
                        {f.kind_title}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-ink">
                      {f.hotel_name}{f.stars ? ` ${f.stars}*` : ""}
                    </td>
                    <td className="py-2 text-xs text-muted">
                      {f.diagnosis_title ? <b className="text-ink">{f.diagnosis_title}. </b> : null}
                      {f.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>
    </m.div>
  );
}

function Stat({ label, value, of, tone }) {
  const colour = tone === "brand" ? "text-brand-soft"
    : tone === "amber" ? "text-amber-300" : "text-ink";
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2.5">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`text-2xl font-extrabold tabular-nums ${colour}`}>
        {value}
        {of != null && <span className="ml-1 text-sm font-semibold text-muted">/ {of}</span>}
      </div>
    </div>
  );
}
