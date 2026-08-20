import { Fragment, useCallback, useEffect, useState } from "react";
import { m } from "framer-motion";
import { CheckCircle2, ChevronDown, ChevronRight, Circle, ExternalLink, Loader2, Radar,
  Wifi, WifiOff } from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { Select } from "../components/ui/Field.jsx";
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
  // Период фиксированный: срез по нему в отчёте не нужен, а место в ряду фильтров
  // занимал. Возраст находки при этом остаётся виден в строке — он и отделяет
  // устойчивую проблему от разовой ряби.
  const days = 30;
  const [showFailed, setShowFailed] = useState(false);
  // Срезы отчёта. Пустая строка — «все»; значения приходят из самих данных, а не из
  // конфига, иначе список предлагал бы фильтры, по которым отчёт пуст.
  const [pick, setPick] = useState({
    operator: "", departure_city: "", country: "", kind: "", diagnosis: "",
  });
  const [open, setOpen] = useState(() => new Set());   // раскрытые группы

  const reload = useCallback(() => {
    const q = new URLSearchParams({ days, only_open: onlyOpen, limit, ...pick });
    getJson(`/api/findings?${q}`).then(setStored).catch(() => {});
    getJson("/api/worker").then(setWorker).catch(() => {});
  }, [onlyOpen, limit, pick]);

  /** Обновить только счётчики — список при этом остаётся на месте. */
  const refreshSummary = useCallback(() => {
    const q = new URLSearchParams({ days, only_open: onlyOpen, limit: 1, ...pick });
    getJson(`/api/findings?${q}`)
      .then((fresh) => setStored((prev) => prev && { ...prev, summary: fresh.summary }))
      .catch(() => {});
  }, [onlyOpen, pick]);

  useEffect(reload, [reload]);
  // Новая находка в потоке — подтягиваем накопленное, чтобы у строки появился id и с ней
  // можно было работать (отметить разобранной).
  useEffect(() => { if (live.length) reload(); }, [live.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const running = liveState?.running ?? worker?.running ?? false;
  const queue = worker?.queue || stored?.summary?.queue || {};
  const summary = stored?.summary || {};
  const facets = stored?.facets || {};
  const groups = stored ? groupFindings(stored.findings) : [];

  /** Отметить всю группу: одна проблема разбирается один раз, а не по разу на вариант. */
  async function toggleGroup(group) {
    const next = !group.reviewed;
    const ids = new Set(group.items.map((f) => f.id));
    // Обновляем на месте и строки, И СЧЁТЧИК. Раньше правились только строки: галка
    // загоралась, а «разобрано» оставалось нулём до следующей перезагрузки — выглядело
    // так, будто отметка не сохранилась.
    setStored((prev) => prev && {
      ...prev,
      findings: prev.findings.map((f) => (ids.has(f.id) ? { ...f, reviewed: next } : f)),
      summary: {
        ...prev.summary,
        unique_reviewed: (prev.summary?.unique_reviewed ?? 0) + (next ? 1 : -1),
      },
    });
    try {
      // Один запрос на всю группу: сервер отмечает проблему, а не строки. Раньше здесь
      // шёл запрос на каждую строку, и отмечались только загруженные — остальные
      // (а их у проблемы бывает под полсотни) оставались неразобранными.
      await postJson(`/api/findings/${group.head.id}/review?reviewed=${next}`, {});
    } finally {
      // Перечитываем ТОЛЬКО сводку, а не список. Полная перезагрузка пересобирала таблицу,
      // отмеченная строка уезжала (выборка предпочитает неразобранные), и следующий клик
      // попадал уже в другую проблему. Счётчик при этом должен быть серверный: он считает
      // по всей базе, а не по загруженным пятистам строкам.
      refreshSummary();
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
          <Stat label="Проблем за 30 дней" value={summary.unique ?? 0} tone="brand" />
          {/* Раньше рядом стояло «не разобрано», повторявшее предыдущую цифру один в
              один, пока никто ничего не отметил. Прогресс разбора полезнее: он растёт. */}
          <Stat label="Разобрано" value={summary.unique_reviewed ?? 0} of={summary.unique}
                tone="amber" />
        </div>

        {/* Разрыв между числом проверок и числом находок сбивает с толку, если его не
            объяснить: одна проверка сравнивает сотню отелей, и каждый отсутствующий даёт
            строку, а потом повторяется в каждом окне дат. Поэтому показываем «проблемы»
            (отель + оператор + направление), а строки идут пояснением. */}
        <p className="mt-2 text-[11px] text-muted">
          Проблема — это отель у оператора на направлении; в отчёте она одна строка, даже
          если встретилась в нескольких датах. Всего таких повторов{" "}
          <b className="text-ink">{summary.total ?? 0}</b>. Кейсы считаются за всё время
          жизни очереди, находки — за выбранный период.
        </p>

        {/* Непроверенное держим на виду: прогон, которому нельзя верить, — это дыра в
            покрытии, и молчать о ней значит выдавать неполный отчёт за полный. */}
        {summary.failed_runs > 0 && (
          <div className="mt-3">
            <button
              onClick={() => setShowFailed((v) => !v)}
              className="text-xs font-semibold text-amber-300 hover:underline"
            >
              Не удалось проверить: {summary.failed_runs}{" "}
              {plural(summary.failed_runs, "прогон", "прогона", "прогонов")} —{" "}
              {showFailed ? "свернуть" : "показать причины"}
            </button>
            {showFailed && (
              <div className="mt-2 max-h-56 overflow-y-auto rounded-xl border border-white/10 bg-white/[0.03] p-2">
                {(stored?.failed || []).map((f) => (
                  <div key={f.run_id} className="border-b border-white/5 py-1 text-[11px]">
                    <span className="text-ink">
                      {f.operator}: {routeLabel(f)}
                    </span>{" "}
                    <span className="text-muted">
                      {f.search_mode === "hotels" ? "отели" : "туры"} ·{" "}
                      {formatShortDateTime(f.run_at)} — {f.problems.join("; ")}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

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
            {/* Считаем в ПРОБЛЕМАХ, как и карточка рядом. Раньше здесь стояли строки:
                «показано 500 из 36788» при том, что таблица показывала полторы сотни
                свёрнутых групп, а карточка — десять тысяч проблем. Три числа, три
                разные единицы, и ни одно не сходилось с соседним. */}
            Расхождения площадок
            {stored ? (groups.length < (summary.unique ?? 0)
              ? ` · показано ${groups.length} из ${summary.unique}`
              : ` · ${groups.length}`) : ""}
          </h2>
          <div className="ml-auto flex flex-wrap items-center gap-2 text-xs text-muted">
            <Pick label="оператор" value={pick.operator} allLabel="все"
                  onChange={(v) => setPick((p) => ({ ...p, operator: v }))}
                  options={(facets.operators || []).map((o) => [o, o])} />
            <Pick label="откуда" value={pick.departure_city} allLabel="все города"
                  onChange={(v) => setPick((p) => ({ ...p, departure_city: v }))}
                  options={(facets.departure_cities || []).map((o) => [o, o])} />
            <Pick label="куда" value={pick.country} allLabel="все страны"
                  onChange={(v) => setPick((p) => ({ ...p, country: v }))}
                  options={(facets.countries || []).map((o) => [o, o])} />
            <Pick label="класс" value={pick.kind} allLabel="все"
                  onChange={(v) => setPick((p) => ({ ...p, kind: v }))}
                  options={(facets.kinds || []).map((k) => [k, facets.kind_titles?.[k] || k])} />
            <Pick label="причина" value={pick.diagnosis} allLabel="любая"
                  onChange={(v) => setPick((p) => ({ ...p, diagnosis: v }))}
                  options={(facets.diagnoses || [])
                    .map((d) => [d, facets.diagnosis_titles?.[d] || d])} />
            <label className="flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={onlyOpen}
                onChange={(e) => setOnlyOpen(e.target.checked)}
                className="size-3.5 accent-brand"
              />
              только неразобранные
            </label>
          </div>
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
            <table className="w-full min-w-[1100px] table-fixed text-sm">
              <colgroup>
                <col className="w-10" />
                <col className="w-[9rem]" />
                <col className="w-[15rem]" />
                <col className="w-[7rem]" />
                <col className="w-[10rem]" />
                <col />
                <col className="w-[13rem]" />
              </colgroup>
              <thead>
                <tr className="border-b border-white/10 text-left text-xs uppercase tracking-wider text-muted">
                  <th className="w-10 py-2 pr-2 font-semibold">✓</th>
                  <th className="py-2 pr-3 font-semibold">Оператор</th>
                  <th className="py-2 pr-3 font-semibold">Направление и даты</th>
                  <th className="py-2 pr-3 font-semibold">Туристы</th>
                  <th className="py-2 pr-3 font-semibold">Класс</th>
                  <th className="py-2 pr-3 font-semibold">Отель</th>
                  <th className="py-2 pr-3 font-semibold">Причина</th>
                  <th className="py-2 font-semibold">Цены</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
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
                          {routeLabel(g.head)}
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
                        {/* Возраст отделяет устойчивую проблему от разовой ряби. */}
                        {g.head.times_seen > 1 && (
                          <div className="mt-1 text-[11px] text-muted">
                            держится {g.head.times_seen}{" "}
                            {plural(g.head.times_seen, "прогон", "прогона", "прогонов")}
                            {g.head.first_seen
                              ? `, с ${formatDate(g.head.first_seen.slice(0, 10))}` : ""}
                          </div>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-ink">
                        <div>{g.head.hotel_name}{g.head.stars ? ` ${g.head.stars}*` : ""}</div>
                        {/* Ссылка на тот же поиск: без неё находка проверяется только
                            повторением поиска руками по десятку полей формы. */}
                        <div className="mt-0.5 flex flex-col gap-0.5">
                          {g.head.search_url && (
                            <a href={g.head.search_url} target="_blank" rel="noreferrer"
                               className="inline-flex items-center gap-1 text-[11px] text-brand-soft hover:underline">
                              <ExternalLink className="size-3" /> на Слетать
                            </a>
                          )}
                          {g.head.reference_url && (
                            <a href={g.head.reference_url} target="_blank" rel="noreferrer"
                               className="inline-flex items-center gap-1 text-[11px] text-ocean hover:underline">
                              <ExternalLink className="size-3" /> на Турвизоре
                            </a>
                          )}
                        </div>
                      </td>
                      {/* Предполагаемая причина расхождения: вердикт разбора и его
                          подробности — под каким именем отель у другой стороны, что
                          именно не нашлось. Без этой колонки находка требует веры на
                          слово, а первый же спорный случай (Atlantis Royal) выглядел
                          ошибкой инструмента, будучи верным. */}
                      <td className="py-2 pr-3 max-w-[280px] text-xs">
                        {g.head.diagnosis_title && (
                          <div className="text-ink">{g.head.diagnosis_title}</div>
                        )}
                        {g.head.note && (
                          <div className="mt-0.5 text-[11px] leading-snug text-muted">
                            {g.head.note}
                          </div>
                        )}
                        {!g.head.diagnosis_title && !g.head.note && (
                          <span className="text-muted">—</span>
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
                          {f.search_url && (
                            <a href={f.search_url} target="_blank" rel="noreferrer"
                               className="mr-2 text-brand-soft hover:underline">Слетать</a>
                          )}
                          {f.reference_url && (
                            <a href={f.reference_url} target="_blank" rel="noreferrer"
                               className="text-ocean hover:underline">Турвизор</a>
                          )}
                        </td>
                        <td className="py-1.5 pr-3 text-[11px] text-muted">{f.note || ""}</td>
                        <td className="py-1.5"><Prices f={f} /></td>
                      </tr>
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {stored && groups.length < (summary.unique ?? 0) && (
          <button
            onClick={() => setLimit((n) => n + 1000)}
            className="mt-3 w-full rounded-lg border border-white/10 bg-white/[0.04] py-2 text-sm font-semibold text-muted transition-colors hover:text-ink"
          >
            Показать ещё · скрыто {summary.unique - groups.length}{" "}
            {plural(summary.unique - groups.length, "проблема", "проблемы", "проблем")}
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
          {/* Имя площадки, а не «у нас»: эталона больше нет, и дороже бывает любая
              сторона — на живых данных обратных случаев каждый восьмой. */}
          {diff > 0 ? "на Слетать дороже" : "на Турвизоре дороже"} на{" "}
          {money(Math.abs(f.checked_price - f.reference_price))} ({diff > 0 ? "+" : ""}
          {diff.toFixed(1)}%)
        </div>
      )}
      {/* Основа сравнения: заезд и питание общие, номер сверяется с карточкой тура. */}
      <Basis f={f} />
    </div>
  );
}

/**
 * Основа, на которой сравнивались цены: общий заезд, общее питание, номера сторон.
 * Питание идёт без пометки «у нас» — сравниваются только одинаковые коды, это общий
 * знаменатель. Номер витрины появляется после точечной сверки её карточки тура; пока
 * его нет, строка честно говорит «не сверен», а не делает вид, что сверила.
 */
function Basis({ f }) {
  const day = f.checked_checkin || f.reference_checkin;
  if (!day) return null;
  const parts = [`заезд ${formatDate(day)}`];
  if (f.checked_meal) parts.push(f.checked_meal);
  if (f.reference_room) {
    parts.push(`номер: «${f.checked_room || "?"}» ≈ «${f.reference_room}»`);
  } else if (f.checked_room) {
    parts.push(`у нас ${f.checked_room} · номер витрины не сверен`);
  }
  return <div className="mt-0.5 text-[11px] text-muted">{parts.join(" · ")}</div>;
}

const routeLabel = (f) =>
  (f.search_mode === "hotels" || f.params?.search_mode === "hotels")
    ? `${f.country} · без перелёта`
    : `${f.departure_city} → ${f.country}`;

const nightsLabel = (f) =>
  f.params?.nights_min === f.params?.nights_max
    ? `${f.params?.nights_min} ноч.`
    : `${f.params?.nights_min}–${f.params?.nights_max} ноч.`;

const paxLabel = (f) =>
  `${f.params?.adults} взр.` +
  (f.params?.children_ages?.length
    ? ` + ${f.params.children_ages.length} реб. (${f.params.children_ages.join(", ")})`
    : "");


/**
 * Срез отчёта. Отдельным компонентом, потому что их семь и каждый — метка плюс список.
 * Используем общий Select проекта: голый `<select>` в тёмной теме рисует пункты серым
 * по белому, и выбранного варианта в списке не разглядеть.
 */
function Pick({ label, value, onChange, options, allLabel }) {
  return (
    <label className="flex items-center gap-1.5">
      {label}
      <Select value={value} onChange={(e) => onChange(e.target.value)}
              className="min-w-[7rem]">
        {allLabel ? <option value="">{allLabel}</option> : null}
        {options.map(([v, title]) => <option key={v} value={v}>{title}</option>)}
      </Select>
    </label>
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
