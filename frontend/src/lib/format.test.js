import { describe, it, expect } from "vitest";
import { providerLabel, formatPrice, formatDate } from "./format.js";

const noSpace = (s) => s.replace(/\s/g, "");

describe("providerLabel", () => {
  it("level → Level Travel, остальные без изменений", () => {
    expect(providerLabel("level")).toBe("Level Travel");
    expect(providerLabel("sletat")).toBe("sletat");
    expect(providerLabel("travelata")).toBe("travelata");
    expect(providerLabel("ostrovok")).toBe("ostrovok");
  });
});

describe("formatPrice", () => {
  it("число → группировка + ₽", () => {
    expect(noSpace(formatPrice("80000"))).toBe("80000₽");
    expect(noSpace(formatPrice(1234567))).toBe("1234567₽");
  });
  it("валюта ≠ RUB → её код", () => {
    expect(noSpace(formatPrice(80000, "EUR"))).toBe("80000EUR");
  });
  it("пусто/невалид → прочерк или сам ввод", () => {
    expect(formatPrice(null)).toBe("—");
    expect(formatPrice(undefined)).toBe("—");
    expect(formatPrice("")).toBe("—");
  });
});

describe("formatDate", () => {
  it("валидная дата содержит месяц и год", () => {
    const out = formatDate("2026-06-15");
    expect(out).toContain("2026");
    expect(out).toContain("06");
  });
  it("невалидная дата возвращается как есть", () => {
    expect(formatDate("не-дата")).toBe("не-дата");
  });
});
