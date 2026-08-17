// GlassCard — обёртка стилей. Базовая регрессия: рендерится с children + className.
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { LazyMotion, domAnimation } from "framer-motion";
import GlassCard from "./GlassCard.jsx";

// LazyMotion обязательна для m.div внутри (strict mode из App.jsx).
const wrap = (ui) => <LazyMotion features={domAnimation}>{ui}</LazyMotion>;

describe("GlassCard", () => {
  it("рендерит children", () => {
    render(wrap(<GlassCard>привет</GlassCard>));
    expect(screen.getByText("привет")).toBeInTheDocument();
  });

  it("прокидывает className на внешний контейнер", () => {
    render(wrap(<GlassCard className="kustom-cls">тест</GlassCard>));
    expect(screen.getByText("тест").closest(".kustom-cls")).toBeInTheDocument();
  });
});
