// Дымовой тест страницы: она должна отрисоваться на живой форме данных.
//
// Заведён после того, как в «Мониторинг» уехала опечатка `const nightsLabel = (f) =`
// вместо `=>`. Сборка её проглотила (синтаксис валидный: присваивание, а не стрелка), и
// падало только в браузере — «f is not defined» вместо всей страницы. Тесты на утилиты
// такое не ловят по устройству: ошибка была в модуле страницы, который никто не
// импортировал.
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../lib/stream.js", () => ({
  useEventStream: () => ({
    logs: [], findings: [], state: null, connected: true,
    clearFindings: () => {}, clearLogs: () => {},
  }),
}));

const finding = (extra = {}) => ({
  id: 1,
  run_id: 2,
  run_at: "2026-08-20T09:26:30",
  operator: "Pegas Touristik",
  departure_city: "Москва",
  country: "Турция",
  search_mode: "tours",
  date_from: "2026-09-03",
  date_to: "2026-09-10",
  params: {
    departure_city: "Москва", destination_country: "Турция",
    date_from: "2026-09-03", date_to: "2026-09-10",
    nights_min: 10, nights_max: 10, adults: 2, children_ages: [],
    search_mode: "tours", currency: "RUB", operators: ["Pegas Touristik"],
  },
  kind: "price",
  kind_title: "Цена расходится",
  hotel_name: "SUNPARK GARDEN",
  stars: 4,
  reference_price: 132999,
  checked_price: 154379,
  currency: "RUB",
  diagnosis: "unknown",
  diagnosis_title: "не проверялось",
  reviewed: false,
  first_seen: "2026-08-20T09:26:30",
  runs: 1,
  ...extra,
});

const payload = (findings) => ({
  findings,
  failed: [],
  summary: {
    total: findings.length, unique: findings.length, open: findings.length,
    reviewed: 0, unique_open: findings.length, unique_reviewed: 0,
    failed_runs: 0, queue: { total: 10, checked: 1, pending: 9 },
  },
  facets: {
    operators: ["Pegas Touristik"], departure_cities: ["Москва"],
    countries: ["Турция"], kinds: ["price"], diagnoses: [],
    kind_titles: { price: "Цена расходится" }, diagnosis_titles: {},
  },
});

let response = payload([finding()]);
vi.mock("../lib/api.js", () => ({
  getJson: () => Promise.resolve(response),
  postJson: () => Promise.resolve({}),
}));

const { default: MonitorPage } = await import("./MonitorPage.jsx");

describe("MonitorPage", () => {
  it("отрисовывает находку целиком, не падая", async () => {
    response = payload([finding()]);
    render(<MonitorPage />);
    expect(await screen.findByText(/SUNPARK GARDEN/)).toBeTruthy();
    // Подпись называет площадку, а не «у нас»: дороже бывает любая сторона.
    expect(await screen.findByText(/на Слетать дороже/)).toBeTruthy();
    expect(screen.getByText(/10 ноч\./)).toBeTruthy();
  });

  it("показывает основу сравнения и честно говорит о несверенном номере", async () => {
    response = payload([finding({
      reference_checkin: "2026-09-19", checked_checkin: "2026-09-19",
      checked_meal: "BB", checked_room: "Стандартный номер",
    })]);
    render(<MonitorPage />);
    expect(await screen.findByText(/заезд 19\.09\.2026 · BB/)).toBeTruthy();
    expect(screen.getByText(/номер витрины не сверен/)).toBeTruthy();
  });

  it("показывает сверенный номер витрины", async () => {
    response = payload([finding({
      reference_checkin: "2026-09-19", checked_checkin: "2026-09-19",
      checked_meal: "RO", checked_room: "Стандартный номер",
      reference_room: "стандарт 2 местный",
    })]);
    render(<MonitorPage />);
    expect(await screen.findByText(/«Стандартный номер» ≈ «стандарт 2 местный»/)).toBeTruthy();
  });

  it("называет Турвизор, когда дороже он", async () => {
    response = payload([finding({ reference_price: 200294, checked_price: 126790 })]);
    render(<MonitorPage />);
    expect(await screen.findByText(/на Турвизоре дороже/)).toBeTruthy();
  });
});
