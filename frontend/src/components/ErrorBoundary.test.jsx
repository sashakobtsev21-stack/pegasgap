// ErrorBoundary — рендерит fallback вместо упавшего ребёнка (не «белый экран»).
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ErrorBoundary from "./ErrorBoundary.jsx";

function Boom() {
  throw new Error("boom-message");
}

function Ok() {
  return <div>ok-content</div>;
}

describe("ErrorBoundary", () => {
  it("рендерит ребёнка пока нет ошибки", () => {
    render(
      <ErrorBoundary>
        <Ok />
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok-content")).toBeInTheDocument();
  });

  it("показывает fallback при throw из ребёнка", () => {
    // глушим React error log в этом тесте (ожидаемая ошибка)
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    // в fallback есть сообщение об ошибке
    expect(screen.getByText(/boom-message/i)).toBeInTheDocument();
    spy.mockRestore();
  });

  it("сбрасывает fallback при изменении resetKey", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(
      <ErrorBoundary resetKey="r1">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/boom-message/i)).toBeInTheDocument();
    // меняем resetKey + детей → ErrorBoundary должен попытаться отрендерить заново
    rerender(
      <ErrorBoundary resetKey="r2">
        <Ok />
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok-content")).toBeInTheDocument();
    spy.mockRestore();
  });
});
