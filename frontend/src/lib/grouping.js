import { formatDate } from "./format.js";

/**
 * Свод повторяющихся находок и разбор того, ЧЕМ они различаются.
 *
 * Одна и та же проблема отеля повторяется в каждом кейсе, где этот отель попал в выдачу:
 * на живом обходе `SUNRISE SERANO CLUB RESORT` дал четыре строки подряд — два окна вылета
 * на две длительности, — и читались они как дубликаты. Показать все различающие поля в
 * строке мало: глазами их всё равно надо сличать между строками, а при 3960 кейсах один
 * системный перекос по отелю размножится в десятки строк и утопит единичные находки.
 *
 * Поэтому строка одна на проблему, а варианты сворачиваются с явной подписью, что именно
 * в них разное: «2 окна · 7 и 10 ноч.». Совпадающее не упоминается вовсе — если у всех
 * вариантов один режим и один состав, писать про них нечего.
 */

/** Что считаем одной проблемой: этот отель у этого оператора на этом направлении. */
export function groupKey(f) {
  return [f.operator, f.departure_city, f.country, f.hotel_name, f.kind].join("|");
}

const nightsOf = (f) =>
  f.params?.nights_min === f.params?.nights_max
    ? `${f.params?.nights_min}`
    : `${f.params?.nights_min}–${f.params?.nights_max}`;

const paxOf = (f) =>
  `${f.params?.adults} взр.` +
  (f.params?.children_ages?.length ? ` + ${f.params.children_ages.length} реб.` : "");

const modeOf = (f) => (f.search_mode === "hotels" ? "отели" : "туры");
const windowOf = (f) => `${formatDate(f.date_from)}–${formatDate(f.date_to)}`;

/**
 * Русское склонение после числительного: 1 окно, 2 окна, 5 окон.
 *
 * Нужно потому, что счётчики здесь идут от двух до нескольких десятков, и «5 окна» с
 * «8 случая» лезут в глаза в каждой второй строке отчёта.
 */
export function plural(n, one, few, many) {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = n % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

/** Человеческое перечисление: «7 и 10», «7, 10 и 14». */
function listRu(values) {
  if (values.length <= 1) return values[0] ?? "";
  return `${values.slice(0, -1).join(", ")} и ${values[values.length - 1]}`;
}

/**
 * Чем различаются варианты внутри группы. Пусто — вариант один, различий нет.
 *
 * Окна дат перечисляем не поимённо: их бывает шесть, и строка «01.09–08.09, 17.09–24.09,
 * 02.10–09.10…» нечитаема. Точные даты видны при раскрытии.
 */
export function describeDifferences(items) {
  if (items.length < 2) return "";
  const parts = [];

  const windows = [...new Set(items.map(windowOf))];
  if (windows.length > 1) {
    parts.push(`${windows.length} ${plural(windows.length, "окно", "окна", "окон")} вылета`);
  }

  const nights = [...new Set(items.map(nightsOf))].sort((a, b) => parseInt(a) - parseInt(b));
  if (nights.length > 1) parts.push(`${listRu(nights)} ноч.`);

  const modes = [...new Set(items.map(modeOf))];
  if (modes.length > 1) parts.push(modes.join(" и "));

  const pax = [...new Set(items.map(paxOf))];
  if (pax.length > 1) parts.push(listRu(pax));

  // Все поля совпали, а строк несколько — значит один и тот же кейс проверялся повторно.
  // Это не различие параметров, а история перепроверок, и назвать её надо честно.
  if (!parts.length) {
    const word = plural(items.length, "перепроверка", "перепроверки", "перепроверок");
    return `${items.length} ${word} того же кейса`;
  }
  return parts.join(" · ");
}

/** Находки → группы, порядок сохраняется по первой встреченной находке. */
export function groupFindings(findings) {
  const map = new Map();
  for (const f of findings) {
    const key = groupKey(f);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(f);
  }
  return [...map.entries()].map(([key, items]) => ({
    key,
    items,
    head: items[0],
    count: items.length,
    // Группа разобрана, только когда разобраны все её варианты: иначе галка скрывала бы
    // неразобранные строки под собой.
    reviewed: items.every((f) => f.reviewed),
    differences: describeDifferences(items),
  }));
}
