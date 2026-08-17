import { describe, it, expect } from "vitest";
import { matchRun, matchJob } from "./router.js";

describe("matchRun", () => {
  it("/run/18 → 18", () => {
    expect(matchRun("/run/18")).toBe(18);
  });
  it("/run/0 → null (id из БД начинается с 1)", () => {
    expect(matchRun("/run/0")).toBeNull();
  });
  it("посторонние маршруты → null", () => {
    expect(matchRun("/")).toBeNull();
    expect(matchRun("/run/")).toBeNull();
    expect(matchRun("/run/abc")).toBeNull();
    expect(matchRun("/run/18/extra")).toBeNull();
  });
});

describe("matchJob", () => {
  it("/jobs/42 → 42", () => {
    expect(matchJob("/jobs/42")).toBe(42);
  });
  it("/jobs/0 → null", () => {
    expect(matchJob("/jobs/0")).toBeNull();
  });
  it("посторонние маршруты → null", () => {
    expect(matchJob("/")).toBeNull();
    expect(matchJob("/jobs/abc")).toBeNull();
  });
});
