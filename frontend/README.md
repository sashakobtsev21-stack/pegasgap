# Frontend — toursearch dashboard

React 18 + Vite 8 + Tailwind 3 + framer-motion + lucide-react.
Glassmorphism SaaS-дашборд: поиск, история, мультипоиск, биллинг, тесты, админка.

## Quick start

```bash
cd frontend
npm install
npm run dev              # vite dev-server :5173 (HMR), прокси /api → :8000
npm run build            # → dist/ (раздаётся бэком через /app)
npm test                 # vitest
npm run build:analyze    # → dist/stats.html (treemap размеров пакетов)
node scripts/bundle-report.mjs   # текстовый отчёт топ-пакетов
```

Бэк должен быть запущен (`toursearch web` в корневом venv) — `npm run dev`
прокси'ит API-вызовы на `:8000`. Без бэка отрисуется только лендинг гостя.

## Структура

```
src/
├── App.jsx               корневой роутер (hash-роуты #/history, #/run/N, ...)
├── components/
│   ├── AppShell.jsx      шапка + левая навигация (Glassmorphism)
│   ├── ErrorBoundary.jsx ловит throw из ленивых страниц → fallback
│   ├── SearchForm.jsx    форма параметров поиска (большой контейнер)
│   ├── SearchTerminal.jsx правая колонка: прогресс + лог SSE
│   ├── LiveViews.jsx     левая колонка: live-кадры площадок
│   ├── NotificationsBell.jsx значок уведомлений (поллинг 30с)
│   └── ui/
│       ├── DatePicker.jsx   кастомный календарь (a11y: roving tabindex, Esc, focus trap)
│       ├── Field.jsx + Select  лейбл + кастомный Select (aria-activedescendant)
│       └── GlassCard.jsx
├── pages/
│   ├── LandingPage.jsx   гость на корне (продающая стартовая)
│   ├── LoginPage.jsx
│   ├── SearchPage.jsx    3-колоночный экран поиска (live / форма / терминал)
│   ├── ResultsPage.jsx   результат прогона #/run/<id>
│   ├── HistoryPage.jsx   список прогонов
│   ├── BatchPage.jsx     мультипоиск (создание)
│   ├── JobsPage.jsx + JobPage.jsx   мультипоиски (список + один)
│   ├── BillingPage.jsx   тарифы, чекаут, история платежей
│   ├── TestsPage.jsx     панель автотестов (admin)
│   └── AdminUsersPage.jsx (admin)
└── lib/
    ├── api.js            apiFetch — единая обёртка над fetch (timeout/retry/CSRF/abort)
    ├── auth.jsx          AuthProvider (контекст user, login/logout/refresh)
    ├── router.js         minimal hash-router (useHashRoute, navigate, matchRun)
    ├── format.js         providerLabel, formatPrice, formatDate
    ├── animations.js     fadeUp/slideIn (framer-motion variants)
    ├── searchEvents.js   helpers для SSE-ивентов
    ├── constants.js      хардкод-список площадок (fallback к /api/refdata)
    ├── resultStore.js + repeatStore.js   in-memory state между страницами
    └── *.test.js         vitest
```

## Routing

Hash-роутер (без зависимостей): `App.jsx` слушает `useHashRoute()` и переключает
страницы. Внутренние ссылки — `<a href="#/history">` или `navigate("/history")`.

Добавить новую страницу:
1. Создать `pages/MyPage.jsx`.
2. В `App.jsx` импортировать `const MyPage = lazy(() => import("./pages/MyPage.jsx"))`.
3. Добавить ветку в роутере: `else if (route.startsWith("/mypage")) page = <MyPage />;`
4. Если нужен пункт меню — `components/AppShell.jsx`.

## API-клиент (`lib/api.js`)

```jsx
import { apiFetch } from "./lib/api.js";
const r = await apiFetch("/api/runs", { retry: false });   // polling — без retry
const data = await r.json();
```

Что делает `apiFetch`:
* `credentials: "include"` — cookie-сессия едет сама на тот же origin;
* на unsafe-методах подставляет `X-CSRF-Token` из cookie `ts_csrf`;
* `timeoutMs` (default 30с) — AbortController для зависших запросов;
* retry на 429/502/503/504 для GET/HEAD (для POST — `retry: true` явно);
* на 401 (кроме /api/login) дёргает `onUnauthorized` → AuthProvider сбрасывает user.

## Состояние и auth

Auth-state в Context (`AuthProvider`). Любой компонент:

```jsx
import { useAuth } from "./lib/auth.jsx";
function MyComp() {
  const { user, login, logout, can, refresh } = useAuth();
  if (!user) return null;
  if (!can("users.manage")) return <div>Нет прав</div>;
  ...
}
```

`refresh` использует single-flight (per-context useRef) — параллельные вызовы шарят один промис.

## Анализ бандла

```bash
npm run build:analyze         # → dist/stats.html (treemap)
node scripts/bundle-report.mjs   # текстовый топ пакетов
```

Текущий профиль: framer-motion ~89 KB gzip (38.9%), app ~69 KB, react-dom ~47 KB.
Главный кандидат на оптимизацию — framer-motion LazyMotion (см. CLAUDE.md → backlog).

## A11y

* `DatePicker.jsx` — roving tabindex, Esc, focus trap, aria-current/selected (WCAG 2.1).
* `Field.jsx` Select — `aria-activedescendant` + `aria-controls`, role=combobox.
* Модалки — `role="dialog"` + `aria-modal=true`.
* `SearchTerminal` лог — `aria-live="polite"`.
* `ErrorBoundary` — поверх Suspense, не белый экран на throw.

## Тесты

`vitest` для pure-функций (`lib/*.test.js`). Логические тесты state-машины — да;
компонентов с DOM — нет (нет @testing-library/react). Для DOM-проверок — ручная
проверка в `npm run dev`.
