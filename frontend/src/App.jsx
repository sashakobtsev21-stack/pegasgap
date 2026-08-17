import { lazy, Suspense } from "react";
import { LazyMotion, domAnimation } from "framer-motion";
import AppShell from "./components/AppShell.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import ScanPage from "./pages/ScanPage.jsx";
import { useHashRoute, matchRun } from "./lib/router.js";

// Не-входные экраны — лениво: отдельными чанками, чтобы стартовый бандл нёс только форму.
const GapsPage = lazy(() => import("./pages/GapsPage.jsx"));
const HistoryPage = lazy(() => import("./pages/HistoryPage.jsx"));
const SweepPage = lazy(() => import("./pages/SweepPage.jsx"));

/**
 * App — корень дашборда. Три экрана: проверка одного направления, регулярный обход и
 * история находок; переключаются hash-маршрутами внутри одного SPA.
 *
 * Гарда авторизации нет намеренно: инструмент внутренний и поднимается локально или на
 * служебной машине. Экран входа здесь был бы декорацией — он ничего не защищает, но
 * добавляет состояние, которое надо поддерживать.
 */
function PageFallback() {
  return (
    <div className="grid place-items-center py-24">
      <div className="size-7 animate-spin rounded-full border-2 border-white/20 border-t-brand" />
    </div>
  );
}

function AppInner() {
  const route = useHashRoute();
  const runId = matchRun(route);

  let page;
  if (runId != null) page = <GapsPage key={route} runId={runId} />;
  else if (route.startsWith("/sweep")) page = <SweepPage />;
  else if (route.startsWith("/history")) page = <HistoryPage />;
  else page = <ScanPage />;

  return (
    <AppShell route={route}>
      <ErrorBoundary resetKey={route}>
        <Suspense fallback={<PageFallback />}>{page}</Suspense>
      </ErrorBoundary>
    </AppShell>
  );
}

export default function App() {
  return (
    <LazyMotion features={domAnimation} strict>
      <AppInner />
    </LazyMotion>
  );
}
