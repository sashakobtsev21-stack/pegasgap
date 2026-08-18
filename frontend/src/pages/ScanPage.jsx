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
const MODES = [
  { id: "single", label: "Одно направление", icon: Search },
  { id: "full", label: "Полная по топу", icon: ListChecks },
];

export default function ScanPage() {
  const [mode, setMode] = useState("single");
  return (
    <m.div variants={staggerContainer} initial="hidden" animate="show"
           className="mx-auto max-w-3xl space-y-4">
      <GlassCard variants={fadeUp} className="p-2" overflow="visible">
        <div className="flex gap-1">
          {MODES.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setMode(id)}
              className={[
                "flex flex-1 items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold transition-colors",
                mode === id
                  ? "bg-gradient-to-r from-brand to-ocean text-white shadow-glow"
                  : "text-muted hover:bg-white/5 hover:text-ink",
              ].join(" ")}
            >
              <Icon className="size-4" /> {label}
            </button>
          ))}
        </div>
      </GlassCard>

      {mode === "single" ? <SingleScan /> : <FullScan />}
    </m.div>
  );
}

/** Точечная проверка — разбор конкретной жалобы: параметры на входе, ответ сразу. */
function SingleScan() {
  const [refdata, setRefdata] = useState(null);
  const [form, setForm] = useState({
    country: "Турция",
    departure: "Москва",
    date_from: isoInDays(30),
    date_to: isoInDays(37),
    nights: 7,
    adults: 2,
    mode: "tours",
    operator: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    getJson("/api/refdata").then((j) => alive && setRefdata(j)).catch(() => {});
    return () => { alive = false; };
  }, []);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const countries = refdata?.countries?.length ? refdata.countries : COUNTRIES;
  const cities = refdata?.departure_cities?.length ? refdata.departure_cities : DEPARTURE_CITIES;
  const operators = refdata?.operators?.length ? refdata.operators : ["Pegas Touristik"];
  // Пустое значение в форме = «первый из конфига», решает бэк. Как только справочник
  // приехал, подставляем явно — иначе в поле висел бы пустой выбор.
  const operator = form.operator || operators[0];

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await postJson("/api/scan", {
        ...form, operator,
        nights: Number(form.nights), adults: Number(form.adults),
      });
      navigate(`/run/${run_id}`);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <GlassCard variants={fadeUp} className="p-6" overflow="visible">
      <div className="mb-5 flex items-center gap-3">
        <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand to-brand-deep shadow-glow">
          <Search className="size-5 text-white" />
        </span>
        <div>
          <h1 className="text-xl font-extrabold tracking-tight text-white">
            Проверить направление
          </h1>
          <p className="text-xs text-muted">
            Результат сразу, на этой же странице
          </p>
        </div>
      </div>

      <form onSubmit={submit} className="space-y-4">
        <Field label="Туроператор" icon={Building2} className="min-w-0">
          <Select icon value={operator} onChange={set("operator")}>
            {operators.map((o) => <option key={o} value={o}>{o}</option>)}
          </Select>
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Страна" icon={Globe2} className="min-w-0">
            <Select icon searchable value={form.country} onChange={set("country")}>
              {countries.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
          <Field label="Город вылета" icon={PlaneTakeoff} className="min-w-0">
            <Select icon searchable value={form.departure} onChange={set("departure")}>
              {cities.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Вылет с" icon={CalendarDays} className="min-w-0">
            <DatePicker icon value={form.date_from} min={isoInDays(0)}
                        onChange={set("date_from")} />
          </Field>
          <Field label="Вылет по" icon={CalendarDays} className="min-w-0">
            <DatePicker icon value={form.date_to} min={form.date_from}
                        onChange={set("date_to")} />
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Ночей" icon={Moon} className="min-w-0">
            <Input icon type="number" min={1} max={30} value={form.nights}
                   onChange={set("nights")} />
          </Field>
          <Field label="Взрослых" icon={Users} className="min-w-0">
            <Input icon type="number" min={1} max={6} value={form.adults}
                   onChange={set("adults")} />
          </Field>
          <Field label="Режим" className="min-w-0">
            <Select value={form.mode} onChange={set("mode")}>
              <option value="tours">Туры (с перелётом)</option>
              <option value="hotels">Отели (без перелёта)</option>
            </Select>
          </Field>
        </div>

        {error && (
          <p className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
            {error}
          </p>
        )}

        <m.button
          variants={fadeUp}
          type="submit"
          disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand to-ocean py-3 text-sm font-bold text-white shadow-glow transition-opacity hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy
            ? <><Loader2 className="size-4 animate-spin" /> Идёт проверка — обе площадки параллельно…</>
            : <><Search className="size-4" /> Проверить</>}
        </m.button>
      </form>
    </GlassCard>
  );
}

/** Полная проверка — обход очереди топ-направлений; идёт часами в фоне. */
function FullScan() {
  const { state: liveState } = useEventStream();
  const [worker, setWorker] = useState(null);
  const [queue, setQueue] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState(null);

  const reload = () => {
    getJson("/api/worker").then(setWorker).catch(() => {});
    getJson("/api/queue?limit=500").then(setQueue).catch(() => {});
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
        setNote(`Очередь сверена с конфигом: ${r.seeded} актуальных`
                + (r.retired ? `, ${r.retired} отключено` : ""));
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
            Полная проверка по топу
          </h1>
          <p className="text-xs text-muted">
            Обход очереди направлений с наибольшим объёмом у оператора. Идёт в фоне —
            закрытая вкладка его не прервёт.
          </p>
        </div>
      </div>

      <div className="mb-4 grid gap-2 sm:grid-cols-3">
        <Stat label="Кейсов в очереди" value={stats.total ?? "—"} />
        <Stat label="Проверено" value={stats.checked ?? "—"} />
        <Stat label="Осталось" value={stats.pending ?? "—"} />
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => act("seed")}
          disabled={busy || running}
          title="Привести очередь в соответствие scenarios.yaml"
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
          {running ? "Остановить проверку" : "Запустить полную проверку"}
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

      {queue?.cases?.length > 0 && (
        <details className="mt-4">
          <summary className="cursor-pointer text-sm font-semibold text-muted hover:text-ink">
            Что в очереди · {queue.cases.length}
          </summary>
          <div className="mt-2 max-h-72 overflow-y-auto">
            <table className="w-full text-sm">
              <tbody>
                {queue.cases.map((c) => (
                  <tr key={c.id} className="border-b border-white/5">
                    <td className="py-1.5 pr-3 text-ink">
                      <span className="text-[11px] text-muted">{c.operator}</span>{" "}
                      {c.departure_city} → {c.country}
                    </td>
                    <td className="py-1.5 pr-3 text-[11px] text-muted">
                      {formatDate(c.date_from)}–{formatDate(c.date_to)} ·{" "}
                      {c.search_mode === "hotels" ? "отели" : "туры"}
                    </td>
                    <td className="py-1.5 text-right text-[11px] text-muted">
                      {c.last_checked ? `проверен, находок ${c.gaps_found}` : "не проверялся"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
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
