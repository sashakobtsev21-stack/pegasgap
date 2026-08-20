import { useEffect, useState } from "react";
import { m } from "framer-motion";
import {
  Building2, CalendarDays, Globe2, ListChecks, ListPlus, Loader2, Moon, Pause, Play,
  PlaneTakeoff, Search, Users,
} from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { Field, Input, Select } from "../components/ui/Field.jsx";
import { DatePicker } from "../components/ui/DatePicker.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { getJson, postJson } from "../lib/api.js";
import { COUNTRIES, DEPARTURE_CITIES } from "../lib/constants.js";
import { formatDate } from "../lib/format.js";
import { navigate } from "../lib/router.js";
import { plural } from "../lib/grouping.js";
import { useEventStream } from "../lib/stream.js";

/** ISO-дата через N дней от сегодня — дефолты формы должны быть в будущем. */
function isoInDays(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

/**
 * ScanPage — пусковая страница: отсюда запускают проверки, здесь же видно, что запущено.
 *
 * Два способа, и они решают разные задачи. Точечная проверка — разбор конкретной жалобы:
 * задал параметры, получил ответ сразу. Полная — обход всей очереди топ-направлений, она
 * идёт часами в фоне, и следить за ней надо на «Мониторинге», а не здесь.
 *
 * Отчёт на эту страницу не выносим: смешивать «что запустить» и «что нашлось» — значит
 * получить экран, на котором не делается толком ни то, ни другое.
 */
/**
 * Единственный режим — регулярный обход. Точечная проверка одного направления убрана
 * из интерфейса по решению владельца: инструмент существует ради обхода, а разовые
 * вопросы разбираются по находкам мониторинга (обе ссылки прижаты к отелю) или командой
 * `pegasgap scan` из консоли.
 */
export default function ScanPage() {
  return (
    <m.div variants={staggerContainer} initial="hidden" animate="show"
           className="mx-auto max-w-3xl space-y-4">
      <FullScan />
    </m.div>
  );
}

/** Полная проверка — обход очереди топ-направлений; идёт часами в фоне. */
function FullScan() {
  const { state: liveState } = useEventStream();
  const [worker, setWorker] = useState(null);
  const [queue, setQueue] = useState(null);
  const [proxies, setProxies] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState(null);

  const reload = () => {
    getJson("/api/worker").then(setWorker).catch(() => {});
    getJson("/api/queue?limit=0").then(setQueue).catch(() => {});
    getJson("/api/proxies").then(setProxies).catch(() => {});
  };
  useEffect(reload, []);

  const running = liveState?.running ?? worker?.running ?? false;
  const stats = worker?.queue || queue?.stats || {};

  async function act(what) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      if (what === "seed") {
        const r = await postJson("/api/queue/seed", {}, { timeoutMs: 60_000 });
        setNote(`Очередь собрана: ${r.seeded} кейсов`);
      } else {
        await postJson(`/api/worker/${what}`, {}, { timeoutMs: 120_000 });
        if (what === "start") navigate("/monitor");
      }
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
      reload();
    }
  }

  return (
    <GlassCard variants={fadeUp} className="p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand to-ocean shadow-glow">
          <ListChecks className="size-5 text-white" />
        </span>
        <div className="min-w-0">
          <h1 className="text-xl font-extrabold tracking-tight text-white">
            Регулярный обход
          </h1>
          <p className="text-xs text-muted">
            Обход очереди направлений с наибольшим объёмом у оператора. Идёт в фоне —
            закрытая вкладка его не прервёт.
          </p>
        </div>
      </div>

      {/* Из чего складывается очередь — иначе непонятно, откуда взялись тысячи кейсов
          и что вообще проверяется. Считается по самой очереди, а не по конфигу: важно
          то, что реально засеяно. */}
      {queue?.dimensions && (
        <p className="mb-4 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2.5 text-xs leading-relaxed text-muted">
          Кейс — это один поиск: <b className="text-ink">оператор</b> +{" "}
          <b className="text-ink">город вылета</b> +{" "}
          <b className="text-ink">страна</b> +{" "}
          <b className="text-ink">окно дат вылета</b> +{" "}
          <b className="text-ink">длительность</b> +{" "}
          <b className="text-ink">режим</b> (с перелётом или без). Очередь — все их
          сочетания:
          {(queue.dimensions.by_mode || []).map((m) => (
            <span key={m.mode} className="mt-1 block pl-3">
              {m.mode === "hotels" ? "отели (без перелёта)" : "туры (с перелётом)"} —{" "}
              <b className="text-ink">{queue.dimensions.operators}</b> оператора ×{" "}
              <b className="text-ink">{m.cities}</b>{" "}
              {plural(m.cities, "город", "города", "городов")} вылета ×{" "}
              <b className="text-ink">{queue.dimensions.countries}</b> стран ×{" "}
              <b className="text-ink">{queue.dimensions.windows}</b>{" "}
              {plural(queue.dimensions.windows, "окно", "окна", "окон")} ×{" "}
              <b className="text-ink">{queue.dimensions.durations}</b>{" "}
              {plural(queue.dimensions.durations, "длительность", "длительности", "длительностей")}
              {" = "}<b className="text-ink">{m.cases}</b>
              {m.mode === "hotels"
                ? " — город здесь один: без перелёта он ни на что не влияет"
                : ""}
            </span>
          ))}
          <span className="mt-1 block">
            Состав задаётся в <code>scenarios.yaml</code>. Кнопка «Собрать очередь»
            перечитывает файл и приводит очередь в соответствие: новые сочетания
            добавляет, исчезнувшие отключает, историю проверок сохраняет.
          </span>
        </p>
      )}

      <div className="mb-4 grid gap-2 sm:grid-cols-3">
        <Stat label="Кейсов в очереди" value={stats.total ?? "—"} />
        <Stat label="Проверено" value={stats.checked ?? "—"} />
        <Stat label="Осталось" value={stats.pending ?? "—"} />
      </div>

      {/* Прокси — не украшение: без них обход встаёт на первом десятке кейсов, обе
          площадки режут по IP. Пустой пул надо видеть до запуска, а не по логам. */}
      <p className="mb-4 text-xs text-muted">
        {proxies?.total
          ? <>Прокси: <b className="text-ink">{proxies.available}</b> годных из {proxies.total}
              {proxies.cooling ? `, ${proxies.cooling} остывают` : ""}</>
          : <span className="text-amber-300">
              Прокси не настроены — обход встанет на первом десятке кейсов. Вставьте список
              в <code>proxies.txt</code> (формат — в <code>proxies.example.txt</code>).
            </span>}
      </p>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => act("seed")}
          disabled={busy || running}
          title="Перечитать scenarios.yaml и привести очередь в соответствие с ним: новые сочетания добавить, исчезнувшие отключить. История проверок сохраняется"
          className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2.5 text-sm font-semibold text-muted transition-colors hover:text-ink disabled:opacity-50"
        >
          <ListPlus className="size-4" /> Собрать очередь
        </button>
        <button
          onClick={() => act(running ? "stop" : "start")}
          disabled={busy}
          className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand to-ocean px-4 py-2.5 text-sm font-bold text-white shadow-glow transition-opacity hover:opacity-95 disabled:opacity-60"
        >
          {busy ? <Loader2 className="size-4 animate-spin" />
                : running ? <Pause className="size-4" /> : <Play className="size-4" />}
          {running ? "Остановить обход" : "Запустить обход"}
        </button>
      </div>

      {note && <p className="mt-3 text-sm text-emerald-300">{note}</p>}
      {error && <p className="mt-3 text-sm text-rose-300">{error}</p>}
      {running && (
        <p className="mt-3 text-xs text-muted">
          Идёт: {liveState?.current || worker?.current || "готовлюсь…"} · находки смотрите
          на вкладке «Мониторинг», подробности — в «Логах».
        </p>
      )}

      {/* Сводкой, а не списком: кейсов тысячи, и подряд идут почти одинаковые строки,
          различающиеся датой. Нужен объём работы по маршрутам, а не перечисление. */}
      {queue?.composition?.map((block) => (
        <details key={block.operator} className="mt-3">
          <summary className="cursor-pointer text-sm font-semibold text-muted hover:text-ink">
            {block.operator} · {block.cases} кейсов
            <span className="ml-2 text-[11px] font-normal">
              пройдено {block.checked}
            </span>
          </summary>
          <div className="mt-2 max-h-64 overflow-y-auto pl-3">
            {block.routes.map((r) => (
              <div key={r.route}
                   className="flex items-baseline justify-between border-b border-white/5 py-1 text-sm">
                <span className="text-ink">{r.route}</span>
                <span className="text-[11px] tabular-nums text-muted">
                  {r.cases} {plural(r.cases, "кейс", "кейса", "кейсов")}
                  {r.checked ? ` · пройдено ${r.checked}` : ""}
                </span>
              </div>
            ))}
          </div>
        </details>
      ))}

    </GlassCard>
  );
}

function Stat({ label, value }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div className="text-xl font-extrabold tabular-nums text-ink">{value}</div>
    </div>
  );
}
