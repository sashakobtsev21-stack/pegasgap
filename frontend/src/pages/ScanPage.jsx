import { useEffect, useState } from "react";
import { m } from "framer-motion";
import { CalendarDays, Globe2, Loader2, Moon, PlaneTakeoff, Search, Users } from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { Field, Input, Select } from "../components/ui/Field.jsx";
import { DatePicker } from "../components/ui/DatePicker.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { getJson, postJson } from "../lib/api.js";
import { COUNTRIES, DEPARTURE_CITIES } from "../lib/constants.js";
import { navigate } from "../lib/router.js";

/** ISO-дата через N дней от сегодня — дефолты формы должны быть в будущем. */
function isoInDays(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

/**
 * ScanPage — проверка одного направления.
 *
 * Оператор в форме не выбирается: инструмент существует ради одного ТО, и селект с
 * единственным осмысленным значением добавлял бы лишний шаг. Имя оператора показано в
 * шапке карточки, чтобы не возникало сомнений, что именно проверяется.
 */
export default function ScanPage() {
  const [refdata, setRefdata] = useState(null);
  const [form, setForm] = useState({
    country: "Турция",
    departure: "Москва",
    date_from: isoInDays(30),
    date_to: isoInDays(37),
    nights: 7,
    adults: 2,
    mode: "tours",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    getJson("/api/refdata")
      .then((j) => alive && setRefdata(j))
      .catch(() => {});   // офлайн-фолбэк из constants.js — форма остаётся рабочей
    return () => { alive = false; };
  }, []);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const countries = refdata?.countries?.length ? refdata.countries : COUNTRIES;
  const cities = refdata?.departure_cities?.length ? refdata.departure_cities : DEPARTURE_CITIES;
  const operator = refdata?.operator || "Pegas Touristik";

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await postJson("/api/scan", {
        ...form,
        nights: Number(form.nights),
        adults: Number(form.adults),
      });
      navigate(`/run/${run_id}`);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <m.div
      variants={staggerContainer}
      initial="hidden"
      animate="show"
      className="mx-auto max-w-3xl space-y-5"
    >
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
              Оператор <b className="text-ink">{operator}</b> · сравниваем выдачу Турвизора
              с нашей и ищем, чего у нас нет
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="space-y-4">
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
          {busy && (
            <p className="text-center text-xs text-muted">
              Обычно занимает несколько секунд. В режиме «Отели» Турвизор читается браузером —
              там дольше, до минуты.
            </p>
          )}
        </form>
      </GlassCard>
    </m.div>
  );
}
