// Глобальный setup для vitest: jest-dom matchers (.toBeInTheDocument и т.п.) +
// автоочистка DOM между тестами (afterEach unmount всё что отрендерили).
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
