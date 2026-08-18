import { useState } from "react";
import { m } from "framer-motion";
import { History, Loader2, Radar, ScrollText, Search, Square } from "lucide-react";
import { fadeUp } from "../lib/animations.js";
import { postJson } from "../lib/api.js";
import { useEventStream } from "../lib/stream.js";

/**
 * AppShell — каркас всех экранов: анимированный фон и липкая шапка с навигацией.
 * Вкладки не фильтруются правами — прав нет, инструмент внутренний.
 */
const NAV = [
  {
    label: "Проверка",
    href: "#/",
    icon: Search,
    match: (p) => p === "/" || p.startsWith("/run"),
  },
  {
    label: "Мониторинг",
    href: "#/monitor",
    icon: Radar,
    match: (p) => p.startsWith("/monitor") || p.startsWith("/sweep"),
  },
  { label: "Логи", href: "#/logs", icon: ScrollText, match: (p) => p.startsWith("/logs") },
  { label: "История", href: "#/history", icon: History, match: (p) => p.startsWith("/history") },
];

export default function AppShell({ route = "/", children }) {
  return (
    <div className="relative min-h-screen">
      <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
        <div className="absolute -left-40 -top-40 size-[42rem] animate-floatA rounded-full bg-brand/25 blur-[120px]" />
        <div className="absolute -right-40 top-1/4 size-[38rem] animate-floatB rounded-full bg-ocean/20 blur-[120px]" />
        <div className="absolute -bottom-48 left-1/3 size-[40rem] animate-floatA rounded-full bg-violet-500/15 blur-[120px]" />
      </div>

      <m.header
        variants={fadeUp}
        initial="hidden"
        animate="show"
        className="glass-surface sticky top-0 z-30 border-x-0 border-t-0"
      >
        <div className="mx-auto flex h-16 max-w-[1800px] items-center gap-3 px-5">
          <a href="#/" className="flex items-center gap-3">
            <span className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-brand to-ocean shadow-glow">
              <Radar className="size-5 text-white" />
            </span>
            <div className="flex items-baseline gap-2">
              <span className="bg-gradient-to-r from-brand-soft to-ocean bg-clip-text text-lg font-extrabold tracking-tight text-transparent">
                Gap Monitor
              </span>
              <span className="hidden text-xs text-muted sm:inline">
                где у операторов есть туры на Турвизоре и нет у нас
              </span>
            </div>
          </a>
          <div className="ml-auto flex items-center gap-1">
          <StopButton />
          <nav className="flex items-center gap-1 text-sm font-semibold text-muted">
            {NAV.map(({ label, href, icon: Icon, match }) => (
              <a
                key={label}
                href={href}
                className={[
                  "flex items-center gap-1.5 rounded-lg px-3 py-2 transition-colors hover:bg-white/5 hover:text-ink",
                  match(route) ? "bg-white/5 text-ink" : "",
                ].join(" ")}
              >
                <Icon className="size-4" />
                <span className="hidden sm:inline">{label}</span>
              </a>
            ))}
          </nav>
          </div>
        </div>
      </m.header>

      <main className="mx-auto max-w-[1800px] px-4 py-6 md:px-6">{children}</main>
    </div>
  );
}

/**
 * Остановка обхода из любого экрана — живёт в шапке, а не только на «Проверке».
 *
 * Смотреть на то, как обход идёт не туда, чаще всего приходится с «Логов» или
 * «Мониторинга», и уходить оттуда на другую вкладку ради одной кнопки — ровно та заминка,
 * когда успевает пройти ещё пяток ненужных кейсов. Кнопка появляется только во время
 * работы: постоянная «Стоп» при остановленном воркере — мусор в шапке.
 *
 * Показывается по состоянию из живого потока, так что остановка с другой вкладки или из
 * CLI уберёт её сама, без перезагрузки страницы.
 */
function StopButton() {
  const { state } = useEventStream();
  const [busy, setBusy] = useState(false);

  if (!state?.running) return null;

  async function stop() {
    setBusy(true);
    try {
      await postJson("/api/worker/stop", {}, { timeoutMs: 120_000 });
    } catch {
      // Молча: воркер мог остановиться сам, и ругаться в шапке не на что.
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={stop}
      disabled={busy}
      title="Остановить обход — текущий кейс доработает до конца"
      className="ml-auto mr-1 flex items-center gap-1.5 rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-sm font-semibold text-rose-200 transition-colors hover:bg-rose-500/20 disabled:opacity-60"
    >
      {busy ? <Loader2 className="size-4 animate-spin" /> : <Square className="size-3.5 fill-current" />}
      <span className="hidden sm:inline">{busy ? "Останавливаю…" : "Стоп"}</span>
    </button>
  );
}
