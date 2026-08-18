import { describe, expect, it } from "vitest";
import { describeDifferences, groupFindings, plural } from "./grouping.js";

const f = (over = {}) => ({
  id: Math.random(), operator: "Pegas Touristik", departure_city: "Москва",
  country: "Египет", hotel_name: "SUNRISE SERANO CLUB RESORT", kind: "price",
  search_mode: "hotels", date_from: "2026-12-16", date_to: "2026-12-23",
  params: { nights_min: 7, nights_max: 7, adults: 2, children_ages: [] },
  reviewed: false, ...over,
});

describe("свод повторяющихся находок", () => {
  it("живой случай: четыре строки читались как дубликаты", () => {
    // Два окна вылета на две длительности — тот же отель, те же +34.4%.
    const findings = [
      f({ params: { nights_min: 10, nights_max: 10, adults: 2, children_ages: [] } }),
      f(),
      f({ date_from: "2026-11-16", date_to: "2026-11-23",
          params: { nights_min: 10, nights_max: 10, adults: 2, children_ages: [] } }),
      f({ date_from: "2026-11-16", date_to: "2026-11-23" }),
    ];
    const groups = groupFindings(findings);
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(4);
    expect(groups[0].differences).toBe("2 окна вылета · 7 и 10 ноч.");
  });

  it("совпадающее не упоминается", () => {
    // Различается только длительность — про режим и состав писать нечего.
    expect(describeDifferences([
      f(), f({ params: { nights_min: 10, nights_max: 10, adults: 2, children_ages: [] } }),
    ])).toBe("7 и 10 ноч.");
  });

  it("разные отели не сливаются", () => {
    expect(groupFindings([f(), f({ hotel_name: "NOVOTEL MARSA ALAM" })])).toHaveLength(2);
  });

  it("разные операторы не сливаются, даже если отель тот же", () => {
    expect(groupFindings([f(), f({ operator: "Coral Travel" })])).toHaveLength(2);
  });

  it("одинаковые параметры — это перепроверки, а не различие", () => {
    expect(describeDifferences([f(), f()])).toBe("2 перепроверки того же кейса");
  });

  it("группа разобрана, только когда разобраны все варианты", () => {
    const [g] = groupFindings([f({ reviewed: true }), f({ reviewed: false })]);
    expect(g.reviewed).toBe(false);
  });

  it("одиночная находка различий не имеет", () => {
    expect(groupFindings([f()])[0].differences).toBe("");
  });
});

describe("склонение после числительного", () => {
  it("считает как по-русски", () => {
    const cases = [[1, "случай"], [2, "случая"], [4, "случая"], [5, "случаев"],
                   [11, "случаев"], [14, "случаев"], [21, "случай"], [22, "случая"],
                   [25, "случаев"], [30, "случаев"], [101, "случай"], [112, "случаев"]];
    for (const [n, want] of cases) {
      expect(plural(n, "случай", "случая", "случаев")).toBe(want);
    }
  });

  it("окна вылета склоняются в самой подписи", () => {
    const g = (from, to) => ({
      operator: "P", departure_city: "Москва", country: "Египет", hotel_name: "H",
      kind: "price", search_mode: "hotels", date_from: from, date_to: to,
      params: { nights_min: 7, nights_max: 7, adults: 2, children_ages: [] },
    });
    expect(describeDifferences([g("2026-09-01", "2026-09-08"), g("2026-10-01", "2026-10-08")]))
      .toBe("2 окна вылета");
    const five = ["09-01", "10-01", "11-01", "12-01", "12-20"]
      .map((d) => g(`2026-${d}`, `2026-${d}`));
    expect(describeDifferences(five)).toBe("5 окон вылета");
  });
});
