import { Fragment, useCallback, useEffect, useState } from "react";
import { m } from "framer-motion";
import { CheckCircle2, ChevronDown, ChevronRight, Circle, ExternalLink, Loader2, Radar,
  Wifi, WifiOff } from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { formatDate, formatShortDateTime } from "../lib/format.js";
import { getJson, postJson } from "../lib/api.js";
import { useEventStream } from "../lib/stream.js";
import { groupFindings, plural } from "../lib/grouping.js";

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
  // Сколько строк тянем. Отчёт растёт быстрее, чем его разбирают: на живом обходе
  // счётчик показывал 1379 находок, а таблица молча обрывалась на пятистах — и это
  // читалось как «вот всё, что нашли». Порог поднимается кнопкой.
  const [limit, setLimit] = useState(500);
  const [open, setOpen] = useState(() => new Set());   // раскрытые группы

  const reload = useCallback(() => {
    getJson(`/api/findings?days=30&only_open=${onlyOpen}&limit=${limit}`)
      .then(setStored).catch(() => {});
    getJson("/api/worker").then(setWorker).catch(() => {});
  }, [onlyOpen, limit]);

  useEffect(reload, [reload]);
  // Новая находка в потоке — подтягиваем накопленное, чтобы у строки появился id и с ней
  // можно было работать (отметить разобранной).
  useEffect(() => { if (live.length) reload(); }, [live.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const running = liveState?.running ?? worker?.running ?? false;
  const queue = worker?.queue || stored?.summary?.queue || {};
  const summary = stored?.summary || {};

  /** Отметить всю группу: одна проблема разбирается один раз, а не по разу на вариант. */
  async function toggleGroup(group) {
    const next = !group.reviewed;
    const ids = new Set(group.items.map((f) => f.id));
    setStored((prev) => prev && {
      ...prev,
      findings: prev.findings.map((f) => (ids.has(f.id) ? { ...f, reviewed: next } : f)),
    });
    try {
      await Promise.all(group.items.map((f) =>
        postJson(`/api/findings/${f.id}/review?reviewed=${next}`, {})));
    } catch {
      reload();
    }
  }

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
          <Stat label="Осталось в очереди" value={queue.pending ?? 0} />
          <Stat label="Найдено за 30 дней" value={summary.total ?? 0} tone="brand" />
          {/* Раньше рядом стояло «не разобрано», повторявшее предыдущую цифру один в
              один, пока никто ничего не отметил. Прогресс разбора полезнее: он растёт. */}
          <Stat label="Разобрано за 30 дней" value={summary.reviewed ?? 0} of={summary.total}
                tone="amber" />
        </div>

        <p className="mt-2 text-[11px] text-muted">
          Кейсы считаются за всё время жизни очереди, находки — за последние 30 дней.
        </p>

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
            Чего нет на Слетать
            {stored ? (stored.findings.length < (summary.total ?? 0)
              ? ` · показано ${stored.findings.length} из ${summary.total}`
              : ` · ${stored.findings.length}`) : ""}
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
                  <th className="py-2 font-semibold">Цены</th>
                </tr>
              </thead>
              <tbody>
                {groupFindings(stored.findings).map((g) => (
                  <Fragment key={g.key}>
                    <tr className={`border-b border-white/5 align-top ${g.reviewed ? "opacity-45" : ""}`}>
                      <td className="py-2 pr-2">
                        <button
                          onClick={() => toggleGroup(g)}
                          title={g.reviewed ? "Снять отметку" : "Отметить разобранным"}
                          className="text-muted transition-colors hover:text-emerald-300"
                        >
                          {g.reviewed
                            ? <CheckCircle2 className="size-4 text-emerald-400" />
                            : <Circle className="size-4" />}
                        </button>
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap font-medium text-ink">
                        {g.head.operator}
                      </td>
                      <td className="py-2 pr-3">
                        <div className="font-medium text-ink">
                          {g.head.departure_city} → {g.head.country}
                        </div>
                        {/* Совпадающее описываем один раз, различия — отдельной строкой:
                            иначе четыре почти одинаковых ряда приходится сличать глазами,
                            и они читаются как дубликаты. */}
                        {g.count === 1 ? (
                          <div className="text-[11px] text-muted">
                            {formatDate(g.head.date_from)}–{formatDate(g.head.date_to)} ·{" "}
                            {nightsLabel(g.head)} ·{" "}
                            {g.head.search_mode === "hotels" ? "отели" : "туры"} ·{" "}
                            {formatShortDateTime(g.head.run_at)}
                          </div>
                        ) : (
                          <button
                            onClick={() => setOpen((prev) => {
                              const next = new Set(prev);
                              next.has(g.key) ? next.delete(g.key) : next.add(g.key);
                              return next;
                            })}
                            className="mt-0.5 flex items-center gap-1 text-[11px] text-brand-soft hover:underline"
                          >
                            {open.has(g.key)
                              ? <ChevronDown className="size-3" />
                              : <ChevronRight className="size-3" />}
                            {g.count} {plural(g.count, "случай", "случая", "случаев")} ·
                            различаются: {g.differences}
                          </button>
                        )}
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap text-muted">
                        {paxLabel(g.head)}
                      </td>
                      <td className="py-2 pr-3">
                        <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-semibold ${KIND_TONE[g.head.kind] || KIND_TONE.reverse}`}>
                          {g.head.kind_title}
                        </span>
                      </td>
                      <td className="py-2 pr-3 text-ink">
                        <div>{g.head.hotel_name}{g.head.stars ? ` ${g.head.stars}*` : ""}</div>
                        {/* Ссылка на тот же поиск: без неё находка проверяется только
                            повторением поиска руками по десятку полей формы. */}
                        {g.head.search_url && (
                          <a href={g.head.search_url} target="_blank" rel="noreferrer"
                             className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-brand-soft hover:underline">
                            <ExternalLink className="size-3" /> открыть поиск на Слетать
                          </a>
                        )}
                      </td>
                      <td className="py-2 whitespace-nowrap text-xs">
                        <Prices f={g.head} />
                      </td>
                    </tr>

                    {open.has(g.key) && g.items.map((f) => (
                      <tr key={f.id} className="border-b border-white/5 bg-white/[0.02] text-xs">
                        <td />
                        <td />
                        <td className="py-1.5 pr-3 text-muted">
                          {formatDate(f.date_from)}–{formatDate(f.date_to)} ·{" "}
                          {nightsLabel(f)} ·{" "}
                          {f.search_mode === "hotels" ? "отели" : "туры"}
                        </td>
                        <td className="py-1.5 pr-3 text-muted">{paxLabel(f)}</td>
                        <td />
                        <td className="py-1.5 pr-3 text-muted">
                          {f.search_url
                            ? <a href={f.search_url} target="_blank" rel="noreferrer"
                                 className="text-brand-soft hover:underline">открыть поиск</a>
                            : null}
                        </td>
                        <td className="py-1.5"><Prices f={f} /></td>
                      </tr>
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {stored && stored.findings.length < (summary.total ?? 0) && (
          <button
            onClick={() => setLimit((n) => n + 1000)}
            className="mt-3 w-full rounded-lg border border-white/10 bg-white/[0.04] py-2 text-sm font-semibold text-muted transition-colors hover:text-ink"
          >
            Показать ещё · скрыто {summary.total - stored.findings.length}
          </button>
        )}
      </GlassCard>
    </m.div>
  );
}

/**
 * Цены сторон как есть. Проценты сами по себе нечитаемы: «+34.4%» не говорит ни сколько
 * стоит тур, ни на какой площадке он дороже — а именно это и нужно, чтобы решить,
 * настоящее это расхождение или разная база цены.
 */
function Prices({ f }) {
  const money = (v) => (v == null ? "—" : `${Math.round(v).toLocaleString("ru-RU")}`);
  if (f.reference_price == null && f.checked_price == null) return <span className="text-muted">—</span>;
  const diff = f.reference_price && f.checked_price != null
    ? (f.checked_price - f.reference_price) / f.reference_price * 100
    : null;
  return (
    <div className="tabular-nums leading-tight">
      <div className="text-muted">
        Турвизор <b className="text-ink">{money(f.reference_price)}</b>
      </div>
      <div className="text-muted">
        Слетать{" "}
        {f.checked_price == null
          ? <b className="text-rose-300">нет</b>
          : <b className="text-ink">{money(f.checked_price)}</b>}
      </div>
      {diff != null && (
        <div className={diff > 0 ? "text-amber-300" : "text-emerald-300"}>
          {diff > 0 ? "у нас дороже" : "у нас дешевле"} на{" "}
          {money(Math.abs(f.checked_price - f.reference_price))} ({diff > 0 ? "+" : ""}
          {diff.toFixed(1)}%)
        </div>
      )}
      {/* На какой заезд пришлась НАША цена. В окне у отеля десяток заездов с разной
          ценой, и минимум с двух площадок легко приходится на разные даты — без этой
          строки «расхождение» читается как разница площадок, хотя это разница дат. */}
      {f.checked_checkin && (
        <div className="mt-0.5 text-[11px] text-muted">
          наш минимум: заезд {formatDate(f.checked_checkin)}
          {f.checked_meal ? ` · ${f.checked_meal}` : ""}
          {f.checked_room ? ` · ${f.checked_room}` : ""}
        </div>
      )}
    </div>
  );
}

const nightsLabel = (f) =>
  f.params?.nights_min === f.params?.nights_max
    ? `${f.params?.nights_min} ноч.`
    : `${f.params?.nights_min}–${f.params?.nights_max} ноч.`;

const paxLabel = (f) =>
  `${f.params?.adults} взр.` +
  (f.params?.children_ages?.length
    ? ` + ${f.params.children_ages.length} реб. (${f.params.children_ages.join(", ")})`
    : "");


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
