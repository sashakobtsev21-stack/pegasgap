import { useEffect, useState } from "react";
import { m } from "framer-motion";
import {
  AlertTriangle, ArrowLeft, CheckCircle2, HelpCircle, Info, Loader2, Star,
} from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { formatDate, formatPrice } from "../lib/format.js";
import { getJson } from "../lib/api.js";

/**
 * GapsPage — находки одного прогона (#/run/{id}).
 *
 * Порядок на экране повторяет порядок разбора: сначала повод не верить прогону, потом
 * контекст, потом сами находки, и только в конце — то, что в находки не попало. Читатель
 * должен узнать о недостоверности раньше, чем прочтёт цифры, а не сноской под таблицей.
 */
const KIND_TONE = {
  full: "border-rose-400/30 bg-rose-500/15 text-rose-200",
  not_responding: "border-amber-400/30 bg-amber-500/15 text-amber-200",
  hotel: "border-brand/30 bg-brand/15 text-brand-soft",
  price: "border-ocean/30 bg-ocean/15 text-ocean",
  reverse: "border-white/10 bg-white/[0.05] text-muted",
};

const DIAGNOSIS_TONE = {
  not_in_catalog: "text-rose-300",
  not_linked: "text-amber-300",
  linked_no_offer: "text-emerald-300",
  in_catalog_unchecked: "text-muted",
  uncertain: "text-muted",
};

export default function GapsPage({ runId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    getJson(`/api/runs/${runId}`)
      .then((j) => alive && setData(j))
      .catch((e) => alive && setError(String(e.message || e)));
    return () => { alive = false; };
  }, [runId]);

  if (error) return <Center icon={AlertTriangle} tone="err" text={`Не удалось загрузить прогон #${runId}: ${error}`} back />;
  if (!data) return <Center icon={Loader2} spin text={`Загружаю прогон #${runId}…`} />;

  const p = data.params;
  const hotels = p.search_mode === "hotels";

  return (
    <m.div variants={staggerContainer} initial="hidden" animate="show"
           className="mx-auto max-w-6xl space-y-5">
      <GlassCard variants={fadeUp} className="p-5">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h2 className="text-xl font-extrabold tracking-tight text-white">
              {p.departure_city} → {p.destination_country}
              <span className="ml-2 rounded-full bg-brand/20 px-2.5 py-0.5 align-middle text-xs font-semibold text-brand-soft">
                {hotels ? "Отели" : "Туры"}
              </span>
            </h2>
            <p className="text-xs text-muted">
              {formatDate(p.date_from)}–{formatDate(p.date_to)}, {p.nights_min}–{p.nights_max} ноч.,
              {" "}{p.adults} взр. · оператор {data.operator} · прогон #{data.run_id}
            </p>
          </div>
          <a href="#/" className="ml-auto flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-sm font-semibold text-muted transition-colors hover:text-ink">
            <ArrowLeft className="size-4" /> Новая проверка
          </a>
        </div>

        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          <Stat label="Турвизор" value={`${data.reference_status} · ${data.reference_hotels} отелей`} />
          <Stat label="наша выдача" value={data.checked_status} />
          <Stat label="сопоставлено" value={data.matched_hotels} />
          {data.price_offset_pct != null && (
            <Stat label="сдвиг цен" value={`${data.price_offset_pct > 0 ? "+" : ""}${data.price_offset_pct}%`} />
          )}
        </div>
      </GlassCard>

      {!data.trustworthy && (
        <GlassCard variants={fadeUp} className="border-rose-400/30 p-5">
          <h3 className="mb-2 flex items-center gap-2 text-base font-bold text-rose-200">
            <AlertTriangle className="size-4" /> Прогон недостоверен — находки использовать нельзя
          </h3>
          <ul className="space-y-1 text-sm text-rose-200/90">
            {data.problems.map((x, i) => <li key={i}>• {x}</li>)}
          </ul>
        </GlassCard>
      )}

      {data.notes?.length > 0 && (
        <div className="space-y-1.5">
          {data.notes.map((x, i) => (
            <p key={i} className="flex items-start gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-xs text-muted">
              <Info className="mt-0.5 size-3.5 shrink-0" /> <span>{x}</span>
            </p>
          ))}
        </div>
      )}

      <GlassCard variants={fadeUp} className="p-5">
        {data.gaps.length === 0 ? (
          <p className="flex items-center gap-2 py-6 text-sm text-emerald-300">
            <CheckCircle2 className="size-5" />
            Расхождений не найдено — всё, что Турвизор показывает по оператору, есть и у нас.
          </p>
        ) : (
          <>
            <h3 className="mb-3 text-base font-bold text-white">
              Находки · {data.gaps.length}
            </h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-sm">
                <thead>
                  <tr className="border-b border-white/10 text-left text-xs uppercase tracking-wider text-muted">
                    <th className="py-2 pr-3 font-semibold">Класс</th>
                    <th className="py-2 pr-3 font-semibold">Отель</th>
                    <th className="py-2 pr-3 font-semibold">Причина</th>
                    <th className="py-2 pr-3 text-right font-semibold">Эталон</th>
                    <th className="py-2 pr-3 text-right font-semibold">Наша</th>
                    <th className="py-2 pr-3 text-right font-semibold">Δ</th>
                    <th className="py-2 font-semibold">Комментарий</th>
                  </tr>
                </thead>
                <tbody>
                  {data.gaps.map((g, i) => (
                    <tr key={i} className="border-b border-white/5 align-top">
                      <td className="py-2 pr-3">
                        <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-semibold ${KIND_TONE[g.kind] || KIND_TONE.reverse}`}>
                          {g.kind_title}
                        </span>
                      </td>
                      <td className="py-2 pr-3 font-medium text-ink">
                        {g.hotel_name}
                        {g.stars ? <Stars n={g.stars} /> : null}
                      </td>
                      <td className={`py-2 pr-3 whitespace-nowrap ${DIAGNOSIS_TONE[g.diagnosis] || "text-muted"}`}>
                        {g.diagnosis_title || "—"}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums text-muted">
                        {g.reference_price != null ? formatPrice(g.reference_price) : "—"}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums text-muted">
                        {g.checked_price != null ? formatPrice(g.checked_price) : "—"}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums font-semibold text-ink">
                        {g.diff_pct != null ? `${g.diff_pct > 0 ? "+" : ""}${g.diff_pct.toFixed(1)}%` : "—"}
                      </td>
                      <td className="py-2 text-xs text-muted">{g.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Что делать — один раз на причину, а не в каждой строке. */}
            {data.actions?.length > 0 && (
              <div className="mt-4 space-y-1 border-t border-white/5 pt-3 text-xs text-muted">
                {data.actions.map((a, i) => (
                  <p key={i}>
                    <b className="text-ink">{a.title} · {a.count}</b> — {a.action}
                  </p>
                ))}
              </div>
            )}
          </>
        )}
      </GlassCard>

      {data.unmatched?.length > 0 && (
        <GlassCard variants={fadeUp} className="p-5">
          <h3 className="mb-2 flex items-center gap-2 text-sm font-bold text-amber-200">
            <HelpCircle className="size-4" />
            Требуют проверки · {data.unmatched.length}
          </h3>
          <p className="mb-2 text-xs text-muted">
            Отели сопоставлены неуверенно. В находки не включены намеренно: выдуманный
            пропуск дороже пропущенного.
          </p>
          <ul className="space-y-1 text-sm text-muted">
            {data.unmatched.map((x, i) => <li key={i}>• {x}</li>)}
          </ul>
        </GlassCard>
      )}
    </m.div>
  );
}

function Stat({ label, value }) {
  return (
    <span className="rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1">
      <span className="text-muted">{label}: </span>
      <span className="font-semibold text-ink">{value}</span>
    </span>
  );
}

function Stars({ n }) {
  return (
    <span className="ml-1.5 inline-flex items-center gap-0.5 align-middle text-amber-300">
      {Array.from({ length: n }).map((_, i) => <Star key={i} className="size-3 fill-current" />)}
    </span>
  );
}

function Center({ icon: Icon, text, spin = false, tone, back = false }) {
  return (
    <div className="mx-auto max-w-2xl">
      <GlassCard className="p-10">
        <div className={`flex flex-col items-center gap-3 text-center ${tone === "err" ? "text-rose-300" : "text-muted"}`}>
          <Icon className={`size-8 ${spin ? "animate-spin" : ""}`} />
          <p className="text-sm">{text}</p>
          {back && (
            <a href="#/" className="mt-2 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-sm font-semibold text-ink hover:bg-white/[0.07]">
              ← К проверке
            </a>
          )}
        </div>
      </GlassCard>
    </div>
  );
}
