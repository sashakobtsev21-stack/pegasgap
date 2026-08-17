import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { m, AnimatePresence } from "framer-motion";
import { ChevronLeft, ChevronRight } from "lucide-react";

/**
 * DatePicker — кастомный календарь в тёмном стеклянном стиле сайта.
 *
 * Нативный <input type="date"> рисует ОС-пикер (его нельзя стилизовать), поэтому
 * заменяем его собственным поповером: триггер-кнопка + анимированный календарь в
 * портале на body (тот же приём, что у Select — чтобы не «запираться» в stacking
 * context соседних motion-полей).
 *
 * Совместим по API с прежним <Input type="date">:
 *  - value (ISO "yyyy-mm-dd"), min, max — те же пропсы;
 *  - onChange получает событие вида { target: { value } };
 *  - name → скрытый input для отправки формы (FormData.get(name)).
 *
 * A11y (P1-a 2026-06):
 *  - role=dialog/aria-modal=true; focus trap по Tab/Shift+Tab;
 *  - roving tabindex по сетке дней; стрелки/Home/End/PageUp/PageDown/Enter/Space;
 *  - Esc закрывает + возвращает фокус на триггер;
 *  - при открытии фокус автоматически переходит на «активный» день
 *    (выбранный, либо сегодня, либо первый доступный).
 */

const MONTHS_NOM = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];
const MONTHS_SHORT = [
  "янв", "фев", "мар", "апр", "мая", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек",
];
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

// Анимация смены месяца: новый месяц въезжает со стороны направления листания.
const slideVariants = {
  enter: (d) => ({ opacity: 0, x: d > 0 ? 28 : -28 }),
  center: { opacity: 1, x: 0 },
  exit: (d) => ({ opacity: 0, x: d > 0 ? -28 : 28 }),
};

const pad = (n) => String(n).padStart(2, "0");
const toISO = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`; // m: 0-based
const todayISO = new Date().toISOString().slice(0, 10);

const controlBase =
  "w-full rounded-xl border border-white/10 bg-white/[0.04] text-ink " +
  "text-sm outline-none transition-all duration-200 " +
  "focus:border-brand/60 focus:bg-white/[0.07] focus:ring-4 focus:ring-brand/20 " +
  "hover:border-white/20";

/** Подпись для триггера: «5 июн 2026». */
function formatLabel(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  return `${d} ${MONTHS_SHORT[m - 1]} ${y}`;
}

/** Сдвинуть ISO-дату на N дней; вернуть тоже ISO. */
function shiftISO(iso, deltaDays) {
  const [y, m, d] = iso.split("-").map(Number);
  const next = new Date(y, m - 1, d + deltaDays);
  return toISO(next.getFullYear(), next.getMonth(), next.getDate());
}
/** Сдвинуть ISO-дату на N месяцев (день обрезается до последнего дня нового месяца). */
function shiftMonthsISO(iso, deltaMonths) {
  const [y, m, d] = iso.split("-").map(Number);
  const target = new Date(y, m - 1 + deltaMonths, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  return toISO(target.getFullYear(), target.getMonth(), Math.min(d, lastDay));
}
function dayOfWeekMon0(iso) {
  // Пн=0 .. Вс=6
  const [y, m, d] = iso.split("-").map(Number);
  return (new Date(y, m - 1, d).getDay() + 6) % 7;
}
function clampToRange(iso, min, max) {
  if (min && iso < firstISO(min)) return firstISO(min);
  if (max && iso > lastISO(max)) return lastISO(max);
  return iso;
}

export function DatePicker({ id, name, value, min, max, onChange, icon = false, className = "" }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const [view, setView] = useState(() => firstOfMonth(value || min || todayISO));
  const [dir, setDir] = useState(0); // направление смены месяца (для анимации)
  // Roving tabindex: текущая «фокусируемая» дата в сетке. Старт — выбранная,
  // иначе сегодня (если в диапазоне), иначе min.
  const [focusedISO, setFocusedISO] = useState(() =>
    clampToRange(value || todayISO, min, max));
  const ref = useRef(null);
  const popRef = useRef(null);
  const gridRef = useRef(null);
  const prevOpenRef = useRef(false);

  // Открываем календарь на месяце выбранной даты + синхронизируем focusedISO.
  useEffect(() => {
    if (open) {
      const start = clampToRange(value || todayISO, min, max);
      setFocusedISO(start);
      setView(firstOfMonth(start));
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // Focus management: при открытии → фокус на focused-cell; при закрытии → возврат на триггер.
  useEffect(() => {
    const justOpened = open && !prevOpenRef.current;
    const justClosed = !open && prevOpenRef.current;
    prevOpenRef.current = open;
    if (justOpened) {
      // Дать AnimatePresence отрендерить попап + анимацию
      const t = setTimeout(() => {
        const cell = gridRef.current?.querySelector(`[data-iso="${focusedISO}"]:not([disabled])`);
        (cell || gridRef.current?.querySelector("[data-iso]:not([disabled])"))?.focus();
      }, 60);
      return () => clearTimeout(t);
    }
    if (justClosed) {
      ref.current?.focus();
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // Когда стрелка увела на другой месяц — синхронизируем фокус с новой сеткой.
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      const cell = gridRef.current?.querySelector(`[data-iso="${focusedISO}"]:not([disabled])`);
      if (cell && document.activeElement !== cell) cell.focus();
    }, 60);
    return () => clearTimeout(t);
  }, [view, focusedISO, open]);

  const reposition = () => {
    if (!ref.current) return;
    const r = ref.current.getBoundingClientRect();
    const width = 296;
    // не вылезаем за правый край вьюпорта
    const left = Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - width - 12);
    setPos({ top: r.bottom + window.scrollY + 6, left: Math.max(window.scrollX + 8, left), width });
  };

  useLayoutEffect(() => {
    if (open) reposition();
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (ref.current?.contains(e.target) || popRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    const onReflow = () => reposition();
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("scroll", onReflow, true);
    window.addEventListener("resize", onReflow);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("scroll", onReflow, true);
      window.removeEventListener("resize", onReflow);
    };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const choose = (iso) => {
    setOpen(false);
    onChange?.({ target: { value: iso } });
  };

  // Можно ли уйти на пред./след. месяц (не за пределы min/max).
  const prevDisabled = min ? toISO(view.y, view.m, 1) <= firstISO(min) : false;
  const nextDisabled = max ? toISO(view.y, view.m + 1, 1) > lastISO(max) : false;

  const step = (delta) => {
    setDir(delta);
    setView((v) => {
      const d = new Date(v.y, v.m + delta, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });
  };

  /** Сдвинуть focusedISO; если уехали на другой месяц — листаем view; уважаем min/max. */
  const moveFocus = (deltaDays = 0, deltaMonths = 0) => {
    let next = focusedISO;
    if (deltaMonths) next = shiftMonthsISO(next, deltaMonths);
    if (deltaDays) next = shiftISO(next, deltaDays);
    next = clampToRange(next, min, max);
    if (next === focusedISO) return;
    const [ny, nm] = next.split("-").map(Number);
    if (ny !== view.y || nm - 1 !== view.m) {
      setDir(next > focusedISO ? 1 : -1);
      setView({ y: ny, m: nm - 1 });
    }
    setFocusedISO(next);
  };

  const onPopKeyDown = (e) => {
    // Esc — закрыть; всегда (даже когда фокус на nav-кнопке).
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      setOpen(false);
      return;
    }
    // Focus trap по Tab — внутри попапа цикл по фокусируемым элементам.
    if (e.key === "Tab") {
      const list = popRef.current?.querySelectorAll(
        'button:not([disabled]), [tabindex="0"]'
      );
      if (!list || list.length === 0) return;
      const first = list[0], last = list[list.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
      return;
    }
    // Клавиши сетки работают только когда фокус на ячейке-дне.
    if (!gridRef.current?.contains(document.activeElement)) return;
    switch (e.key) {
      case "ArrowLeft":  moveFocus(-1); e.preventDefault(); break;
      case "ArrowRight": moveFocus(1);  e.preventDefault(); break;
      case "ArrowUp":    moveFocus(-7); e.preventDefault(); break;
      case "ArrowDown":  moveFocus(7);  e.preventDefault(); break;
      case "Home":       moveFocus(-dayOfWeekMon0(focusedISO));     e.preventDefault(); break; // понедельник недели
      case "End":        moveFocus(6 - dayOfWeekMon0(focusedISO));  e.preventDefault(); break; // воскресенье недели
      case "PageUp":     moveFocus(0, e.shiftKey ? -12 : -1); e.preventDefault(); break;       // −1мес / −1год
      case "PageDown":   moveFocus(0, e.shiftKey ? 12 : 1);   e.preventDefault(); break;       // +1мес / +1год
      case "Enter":
      case " ":
        if (focusedISO) {
          const okMin = !min || focusedISO >= firstISO(min);
          const okMax = !max || focusedISO <= lastISO(max);
          if (okMin && okMax) { e.preventDefault(); choose(focusedISO); }
        }
        break;
      default: break;
    }
  };

  const cells = buildCells(view.y, view.m);

  return (
    <div className="relative" ref={ref}>
      {name && <input type="hidden" name={name} value={value || ""} readOnly />}
      <button
        id={id}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className={[
          controlBase, "flex items-center justify-between py-2.5",
          icon ? "pl-9 pr-3" : "px-3",
          value ? "text-ink" : "text-muted/70", className,
        ].join(" ")}
      >
        <span className="truncate text-left">{value ? formatLabel(value) : "выбрать дату"}</span>
        <svg className={`ml-2 size-4 shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`}
             viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="m6 9 6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && pos &&
        createPortal(
          <m.div
            ref={popRef}
            role="dialog"
            aria-modal="true"
            aria-label={`Календарь: ${MONTHS_NOM[view.m]} ${view.y}`}
            onKeyDown={onPopKeyDown}
            initial={{ opacity: 0, y: -8, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
            style={{ position: "absolute", top: pos.top, left: pos.left, width: pos.width }}
            className="z-[60] select-none rounded-2xl border border-white/10 bg-[#0b1026] p-3
                       shadow-[0_24px_60px_-12px_rgba(0,0,0,0.9)] ring-1 ring-black/40
                       bg-gradient-to-b from-white/[0.04] to-transparent"
          >
            {/* Шапка: месяц/год + навигация */}
            <div className="mb-2 flex items-center justify-between px-1">
              <NavBtn disabled={prevDisabled} onClick={() => step(-1)} aria-label="Предыдущий месяц">
                <ChevronLeft aria-hidden="true" className="size-4" />
              </NavBtn>
              <div className="text-sm font-bold text-white" aria-live="polite">
                {MONTHS_NOM[view.m]} <span className="text-muted">{view.y}</span>
              </div>
              <NavBtn disabled={nextDisabled} onClick={() => step(1)} aria-label="Следующий месяц">
                <ChevronRight aria-hidden="true" className="size-4" />
              </NavBtn>
            </div>

            {/* Дни недели */}
            <div className="mb-1 grid grid-cols-7 gap-1 px-0.5" role="row">
              {WEEKDAYS.map((w, i) => (
                <div
                  key={w}
                  role="columnheader"
                  aria-label={w}
                  className={`py-1 text-center text-[10px] font-semibold ${i >= 5 ? "text-rose-300/70" : "text-muted/70"}`}
                >
                  {w}
                </div>
              ))}
            </div>

            {/* Сетка дней с анимацией смены месяца */}
            <div ref={gridRef} role="grid" aria-label="Дни месяца" className="relative overflow-hidden">
              <AnimatePresence initial={false} mode="popLayout" custom={dir}>
                <m.div
                  key={`${view.y}-${view.m}`}
                  custom={dir}
                  variants={slideVariants}
                  initial="enter"
                  animate="center"
                  exit="exit"
                  transition={{ duration: 0.2, ease: "easeOut" }}
                  className="grid grid-cols-7 gap-1 px-0.5"
                  role="rowgroup"
                >
                  {cells.map((cell, i) => {
                    if (cell == null) return <div key={`e${i}`} role="gridcell" />;
                    const iso = toISO(view.y, view.m, cell);
                    const disabled = (min && iso < firstISO(min)) || (max && iso > lastISO(max));
                    const selected = iso === value;
                    const isFocused = iso === focusedISO;
                    const isToday = iso === todayISO;
                    const weekend = (i % 7) >= 5;
                    return (
                      <button
                        key={iso}
                        type="button"
                        role="gridcell"
                        data-iso={iso}
                        disabled={disabled}
                        tabIndex={isFocused && !disabled ? 0 : -1}
                        aria-selected={selected}
                        aria-current={isToday ? "date" : undefined}
                        aria-label={formatLabel(iso)}
                        onClick={() => choose(iso)}
                        onFocus={() => setFocusedISO(iso)}
                        className={[
                          "relative grid h-9 place-items-center rounded-lg text-sm font-medium transition-colors",
                          "focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand focus-visible:outline-offset-1",
                          disabled
                            ? "cursor-not-allowed text-muted/25"
                            : selected
                              ? "bg-gradient-to-br from-brand to-brand-deep text-white shadow-glow"
                              : weekend
                                ? "text-rose-200/90 hover:bg-white/10"
                                : "text-ink hover:bg-white/10",
                        ].join(" ")}
                      >
                        {cell}
                        {isToday && !selected && (
                          <span aria-hidden="true" className="absolute bottom-1 size-1 rounded-full bg-ocean" />
                        )}
                      </button>
                    );
                  })}
                </m.div>
              </AnimatePresence>
            </div>

            {/* Быстрый переход «Сегодня/ближайшая доступная» */}
            <div className="mt-2 flex justify-between px-1 pt-1">
              <button
                type="button"
                onClick={() => {
                  const earliest = min && todayISO < firstISO(min) ? firstISO(min) : todayISO;
                  setView(firstOfMonth(earliest));
                  setFocusedISO(clampToRange(earliest, min, max));
                }}
                className="rounded-lg px-2 py-1 text-[11px] font-semibold text-muted transition-colors hover:bg-white/10 hover:text-ink"
              >
                К ближайшей дате
              </button>
              {value && (
                <span className="rounded-lg px-2 py-1 text-[11px] font-semibold text-brand-soft">
                  {formatLabel(value)}
                </span>
              )}
            </div>
          </m.div>,
          document.body
        )}
    </div>
  );
}

/** Кнопка навигации по месяцам. */
function NavBtn({ disabled, onClick, children, ...rest }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={[
        "grid size-7 place-items-center rounded-lg border border-white/10 transition-colors",
        disabled ? "cursor-not-allowed text-muted/25" : "text-muted hover:border-brand/40 hover:bg-white/10 hover:text-ink",
      ].join(" ")}
      {...rest}
    >
      {children}
    </button>
  );
}

// --- helpers -----------------------------------------------------------------

function firstOfMonth(iso) {
  const [y, m] = (iso || todayISO).split("-").map(Number);
  return { y, m: m - 1 };
}

/** Сетка месяца, неделя с понедельника: ведущие null + числа 1..N. */
function buildCells(y, m) {
  const firstDow = new Date(y, m, 1).getDay();      // 0=Вс..6=Сб
  const offset = (firstDow + 6) % 7;                 // сдвиг до понедельника
  const days = new Date(y, m + 1, 0).getDate();
  const cells = Array(offset).fill(null);
  for (let d = 1; d <= days; d++) cells.push(d);
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

// min/max могут приходить как ISO — работаем с первыми 10 символами.
const firstISO = (s) => String(s).slice(0, 10);
const lastISO = (s) => String(s).slice(0, 10);
