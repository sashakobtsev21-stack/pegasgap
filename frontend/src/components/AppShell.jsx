import { m } from "framer-motion";
import { History, Radar, Search } from "lucide-react";
import { fadeUp } from "../lib/animations.js";

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
  { label: "Обход", href: "#/sweep", icon: Radar, match: (p) => p.startsWith("/sweep") },
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
                Pegas Gap
              </span>
              <span className="hidden text-xs text-muted sm:inline">
                где у оператора есть туры на Турвизоре и нет у нас
              </span>
            </div>
          </a>
          <nav className="ml-auto flex items-center gap-1 text-sm font-semibold text-muted">
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
      </m.header>

      <main className="mx-auto max-w-[1800px] px-4 py-6 md:px-6">{children}</main>
    </div>
  );
}
