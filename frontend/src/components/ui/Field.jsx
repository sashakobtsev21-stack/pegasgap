import { Children, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { m } from "framer-motion";
import { fadeUp } from "../../lib/animations.js";

/**
 * Базовые стили инпута/селекта в стиле shadcn — тёмное стекло, кольцо фокуса бренда.
 * Иконка Lucide встраивается слева через absolute-позиционирование.
 */
const controlBase =
  "w-full rounded-xl border border-white/10 bg-white/[0.04] text-ink placeholder:text-muted/70 " +
  "text-sm outline-none transition-all duration-200 " +
  "focus:border-brand/60 focus:bg-white/[0.07] focus:ring-4 focus:ring-brand/20 " +
  "hover:border-white/20";

/**
 * Field — обёртка «лейбл + контрол» с иконкой и анимацией появления.
 * Каждое поле — motion-элемент c вариантом fadeUp, чтобы родитель мог
 * выстроить ступенчатое (stagger) появление всей формы.
 */
export function Field({ label, icon: Icon, htmlFor, children, className = "" }) {
  return (
    <m.div variants={fadeUp} className={className}>
      {label && (
        <label
          htmlFor={htmlFor}
          className="mb-1.5 block text-xs font-medium tracking-wide text-muted"
        >
          {label}
        </label>
      )}
      <div className="group relative">
        {Icon && (
          <Icon
            className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted transition-colors group-focus-within:text-brand-soft"
            strokeWidth={2}
          />
        )}
        {children}
      </div>
    </m.div>
  );
}

/** Текстовый / числовой инпут. icon=true добавляет левый отступ под иконку. */
export function Input({ icon = false, className = "", ...props }) {
  return (
    <input
      className={[controlBase, "py-2.5", icon ? "pl-9 pr-3" : "px-3", className].join(" ")}
      {...props}
    />
  );
}

/**
 * Кастомный селект в стиле сайта (нативный <select> нельзя стилизовать —
 * его выпадающий список рисует ОС). Принимает те же <option> детьми, что и
 * раньше, поэтому места вызова не меняются. Работает и контролируемо
 * (value+onChange), и неконтролируемо (defaultValue+name → скрытый input для
 * отправки формы). onChange получает событие вида { target: { value } }.
 *
 * searchable=true — для длинных списков (города/страны): опции сортируются по
 * алфавиту, а вверху списка появляется поле ввода с живым фильтром (как на
 * Sletat: печатаешь — список сразу сужается).
 */
export function Select({
  icon = false, className = "", children,
  value, defaultValue, name, onChange, searchable = false, ...rest
}) {
  const baseOptions = Children.toArray(children)
    .filter((c) => c && c.type === "option")
    .map((c) => {
      const v = c.props.value !== undefined ? c.props.value : c.props.children;
      return { value: String(v), label: String(c.props.children ?? v) };
    });
  // В режиме поиска показываем по алфавиту (ru-локаль).
  const options = searchable
    ? [...baseOptions].sort((a, b) => a.label.localeCompare(b.label, "ru"))
    : baseOptions;

  const isControlled = value !== undefined;
  const [internal, setInternal] = useState(
    defaultValue !== undefined ? String(defaultValue) : baseOptions[0]?.value ?? ""
  );
  const current = isControlled ? String(value) : internal;
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const [query, setQuery] = useState("");
  const [pos, setPos] = useState(null); // координаты выпадающего списка (портал)
  const ref = useRef(null);     // обёртка с кнопкой-триггером
  const popRef = useRef(null);  // панель опций (в портале на body)
  const searchRef = useRef(null);
  // a11y: уникальные id для listbox и каждой опции — нужны aria-activedescendant,
  // чтобы screen reader объявлял активную опцию при ArrowDown/Up (P1-b 2026-06).
  const listboxId = useId();
  const optionId = (idx) => `${listboxId}-opt-${idx}`;

  // Живой фильтр по подстроке (регистронезависимо), только в searchable.
  const q = query.trim().toLowerCase();
  const filtered = searchable && q
    ? options.filter((o) => o.label.toLowerCase().includes(q))
    : options;

  // Позиционируем панель по триггеру. Портал на body нужен, потому что каждый
  // Field — m.div со своим stacking context (transform/filter), внутри
  // которого z-index панели «заперт» и её перекрывают соседние поля.
  const reposition = () => {
    if (!ref.current) return;
    const r = ref.current.getBoundingClientRect();
    setPos({ top: r.bottom + window.scrollY + 6, left: r.left + window.scrollX, width: r.width });
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

  // При открытии: сброс фильтра, подсветка текущего, фокус в поле поиска.
  useEffect(() => {
    if (open) {
      setQuery("");
      const i = options.findIndex((o) => o.value === current);
      setActiveIdx(i < 0 ? 0 : i);
      if (searchable) setTimeout(() => searchRef.current?.focus(), 30);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // При наборе текста активная строка — первая из отфильтрованных.
  useEffect(() => {
    if (open && searchable) setActiveIdx(0);
  }, [q]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedLabel = options.find((o) => o.value === current)?.label ?? current;

  const choose = (v) => {
    if (!isControlled) setInternal(v);
    setOpen(false);
    setQuery("");
    onChange?.({ target: { value: v } });
  };

  const onKeyDown = (e) => {
    if (!open && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
      e.preventDefault(); setOpen(true); return;
    }
    if (!open) return;
    if (e.key === "Escape") { setOpen(false); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setActiveIdx((i) => Math.min(i + 1, filtered.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); const o = filtered[activeIdx]; if (o) choose(o.value); }
  };

  return (
    <div className="relative" ref={ref}>
      {name && <input type="hidden" name={name} value={current} readOnly />}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onKeyDown}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={open && filtered[activeIdx] ? optionId(activeIdx) : undefined}
        className={[
          controlBase, "flex items-center justify-between py-2.5",
          icon ? "pl-9 pr-3" : "px-3", className,
        ].join(" ")}
        {...rest}
      >
        <span className="truncate text-left">{selectedLabel}</span>
        <svg
          className={`ml-2 size-4 shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`}
          viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
        >
          <path d="m6 9 6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && pos &&
        createPortal(
          <m.div
            ref={popRef}
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ duration: 0.14, ease: "easeOut" }}
            style={{ position: "absolute", top: pos.top, left: pos.left, width: pos.width }}
            className="z-[60] overflow-hidden rounded-xl border border-white/10 bg-[#0b1026] p-1 shadow-[0_20px_50px_-12px_rgba(0,0,0,0.9)] ring-1 ring-black/40"
          >
            {searchable && (
              <div className="relative px-1 pb-1 pt-0.5">
                <svg className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" strokeLinecap="round" />
                </svg>
                <input
                  ref={searchRef}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={onKeyDown}
                  placeholder="Поиск…"
                  role="combobox"
                  aria-expanded={true}
                  aria-controls={listboxId}
                  aria-activedescendant={filtered[activeIdx] ? optionId(activeIdx) : undefined}
                  aria-autocomplete="list"
                  className="w-full rounded-lg border border-white/10 bg-white/[0.04] py-2 pl-8 pr-3 text-sm text-ink outline-none placeholder:text-muted/60 focus:border-brand/60 focus:ring-2 focus:ring-brand/20"
                />
              </div>
            )}
            <ul id={listboxId} role="listbox" className="max-h-60 overflow-y-auto">
              {filtered.length === 0 && (
                <li className="px-3 py-3 text-center text-sm text-muted">Ничего не найдено</li>
              )}
              {filtered.map((o, i) => {
                const selected = o.value === current;
                const active = i === activeIdx;
                return (
                  <li key={o.value} id={optionId(i)} role="option" aria-selected={selected}>
                    <button
                      type="button"
                      onClick={() => choose(o.value)}
                      onMouseEnter={() => setActiveIdx(i)}
                      className={[
                        "flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm transition-colors",
                        selected ? "bg-brand/25 text-white" : active ? "bg-white/10 text-ink" : "text-ink/90",
                      ].join(" ")}
                    >
                      <span className="truncate">{o.label}</span>
                      {selected && (
                        <svg className="size-3.5 shrink-0 text-brand-soft" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                          <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </m.div>,
          document.body
        )}
    </div>
  );
}
