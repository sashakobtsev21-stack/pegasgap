// Форматтеры для отображения цен и дат (единый стиль по всему дашборду).

// Отображаемые названия площадок (ключ провайдера → как показывать). Внутренний ключ
// остаётся прежним (URL/хранение/логи), меняется только подпись в интерфейсе.
const PROVIDER_LABELS = { level: "Level Travel" };

export function providerLabel(name) {
  return PROVIDER_LABELS[name] ?? name;
}

export function formatPrice(value, currency = "RUB") {
  if (value === null || value === undefined || value === "") return "—";
  const n = Math.round(Number(value));
  if (Number.isNaN(n)) return String(value);
  const sym = currency === "RUB" ? "₽" : currency;
  return n.toLocaleString("ru-RU").replace(/ /g, " ") + " " + sym;
}

export function formatDateTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function formatDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

// Короткая дата-время без года (для уведомлений/списка анализов).
export function formatShortDateTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
