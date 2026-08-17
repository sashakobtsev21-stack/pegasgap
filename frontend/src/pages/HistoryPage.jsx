import { useEffect, useState } from "react";
import { m } from "framer-motion";
import {
  AlertTriangle, CheckCircle2, ChevronRight, History as HistoryIcon, Hotel, Loader2, Plane,
} from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { formatDate, formatDateTime } from "../lib/format.js";
import { getJson } from "../lib/api.js";

/**
 * HistoryPage — прошлые прогоны и сводка находок за период (#/history).
 *
 * Сводка стоит над списком: она отвечает на вопрос «что вообще происходит», а список —
 * «где именно». Заходят обычно с первым вопросом.
 */
export default function HistoryPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [days, setDays] = useState(7);

  useEffect(() => {
    let alive = true;
    setData(null);
    getJson(`/api/history?days=${days}`)
      .then((j) => alive && setData(j))
      .catch((e) => alive && setError(String(e.message || e)));
    return () => { alive = false; };
  }, [days]);

  return (
    <div className="mx-auto max-w-5xl">
      <m.div variants={staggerContainer} initial="hidden" animate="show" className="space-y-4">
        <GlassCard variants={fadeUp} className="flex flex-wrap items-center gap-3 p-5">
          <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand to-ocean shadow-glow">
            <HistoryIcon className="size-5 text-white" />
          </span>
          <div>
            <h1 className="text-xl font-extrabold tracking-tight text-white">История</h1>
            <p className="text-xs text-muted">Что накопилось по находкам и прогонам</p>
          </div>
          <div className="ml-auto flex gap-1">
            {[1, 7, 30].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={[
                  "rounded-lg px-3 py-1.5 text-sm font-semibold transition-colors",
                  days === d ? "bg-white/10 text-ink" : "text-muted hover:bg-white/5 hover:text-ink",
                ].join(" ")}
              >
                {d === 1 ? "сутки" : `${d} дн.`}
              </button>
            ))}
          </div>
        </GlassCard>

        {error && (
          <GlassCard variants={fadeUp} className="p-6 text-sm text-rose-300">
            Не удалось загрузить историю: {error}
          </GlassCard>
        )}
        {!data && !error && (
          <GlassCard variants={fadeUp} className="grid place-items-center p-10">
            <Loader2 className="size-7 animate-spin text-muted" />
          </GlassCard>
        )}

        {data && (
          <>
            <GlassCard variants={fadeUp} className="p-5">
              <h2 className="mb-3 text-sm font-bold uppercase tracking-wider text-muted">
                Находок за период · достоверных прогонов: {data.trustworthy_runs}
              </h2>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {data.summary.map((s) => (
                  <div key={s.kind}
                       className="rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-sm font-semibold text-ink">{s.title}</span>
                      <span className={`text-lg font-extrabold tabular-nums ${s.count ? "text-brand-soft" : "text-muted/50"}`}>
                        {s.count}
                      </span>
                    </div>
                    <p className="mt-0.5 text-[11px] leading-snug text-muted">{s.hint}</p>
                  </div>
                ))}
              </div>
            </GlassCard>

            <GlassCard variants={fadeUp} className="p-5">
              <h2 className="mb-3 text-sm font-bold uppercase tracking-wider text-muted">
                Прогоны · {data.runs.length}
              </h2>
              {data.runs.length === 0 ? (
                <p className="py-4 text-sm text-muted">За период прогонов не было.</p>
              ) : (
                <ul className="divide-y divide-white/5">
                  {data.runs.map((r) => (
                    <li key={r.run_id}>
                      <a
                        href={`#/run/${r.run_id}`}
                        className="flex items-center gap-3 py-2.5 transition-colors hover:bg-white/[0.03]"
                      >
                        <span className="grid size-8 shrink-0 place-items-center rounded-lg border border-white/10 bg-white/[0.04] text-muted">
                          {r.search_mode === "hotels" ? <Hotel className="size-4" /> : <Plane className="size-4" />}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-semibold text-ink">
                            {r.departure_city} → {r.destination_country}
                          </div>
                          <div className="truncate text-[11px] text-muted">
                            {formatDate(r.date_from)}–{formatDate(r.date_to)} · {formatDateTime(r.run_at)}
                          </div>
                        </div>
                        {r.trustworthy ? (
                          <span className="flex items-center gap-1 text-xs text-muted">
                            <CheckCircle2 className="size-3.5 text-emerald-400/70" />
                            {r.gaps} наход.
                          </span>
                        ) : (
                          <span className="flex items-center gap-1 text-xs text-rose-300">
                            <AlertTriangle className="size-3.5" /> недостоверен
                          </span>
                        )}
                        <ChevronRight className="size-4 shrink-0 text-muted" />
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </GlassCard>

            {data.standing?.length > 0 && (
              <GlassCard variants={fadeUp} className="p-5">
                <h2 className="mb-1 text-sm font-bold uppercase tracking-wider text-muted">
                  Застарелые находки · {data.standing.length}
                </h2>
                <p className="mb-3 text-xs text-muted">
                  Повторяются из прогона в прогон — это системные дыры, а не свежие регрессии.
                </p>
                <ul className="space-y-1 text-sm">
                  {data.standing.map((s, i) => (
                    <li key={i} className="flex flex-wrap items-baseline gap-2">
                      <span className="text-ink">{s.gap_key}</span>
                      <span className="text-[11px] text-muted">
                        с {formatDate(s.first_seen)}, раз: {s.times_seen}
                      </span>
                    </li>
                  ))}
                </ul>
              </GlassCard>
            )}
          </>
        )}
      </m.div>
    </div>
  );
}
