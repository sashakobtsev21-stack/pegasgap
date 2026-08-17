import { Component } from "react";

/**
 * ErrorBoundary — последний рубеж: ловит throw из дочернего дерева (включая
 * лениво-загруженные страницы внутри Suspense). До этой компоненты любая
 * случайная ошибка (нестандартный ответ API, undefined-доступ при рендере)
 * валилась в белый экран без сообщения.
 *
 * Сбросить — кнопкой «Попробовать снова» (локальный сброс key) или ссылкой
 * «На главную» (полная перезагрузка). При смене props.resetKey (например,
 * при смене hash-маршрута) состояние тоже сбрасывается — пользователь
 * переходит на другую страницу и не остаётся залипшим в ошибке.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidUpdate(prevProps) {
    // Смена resetKey (например, новый route) → новая попытка отрендерить дочернее дерево.
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error, info) {
    // Браузерная диагностика; на сервер ошибки не шлём (нет endpoint'а и не хотим
    // случайно слить PII из стектрейса).
    if (typeof console !== "undefined" && console.error) {
      console.error("ErrorBoundary:", error, info?.componentStack);
    }
  }

  reset = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    const msg = String(this.state.error?.message || this.state.error || "неизвестная ошибка");
    return (
      <div className="grid min-h-screen place-items-center px-6">
        <div className="max-w-md rounded-2xl border border-white/10 bg-white/[0.04] p-6 text-center backdrop-blur">
          <div className="mb-3 text-2xl font-semibold text-white">Что-то сломалось</div>
          <div className="mb-4 text-sm text-muted">
            Произошла непредвиденная ошибка. Если повторяется — обновите страницу или
            вернитесь на главную; если воспроизводится стабильно, сообщите детали.
          </div>
          <details className="mb-4 rounded-lg bg-black/30 p-3 text-left text-xs text-rose-300">
            <summary className="cursor-pointer text-rose-200">Детали</summary>
            <pre className="mt-2 whitespace-pre-wrap break-words">{msg}</pre>
          </details>
          <div className="flex justify-center gap-2">
            <button
              type="button"
              onClick={this.reset}
              className="rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-sm text-white transition-colors hover:bg-white/[0.1]"
            >
              Попробовать снова
            </button>
            <a
              href="/app/"
              className="rounded-lg bg-brand px-4 py-2 text-sm text-white transition-colors hover:bg-brand/90"
            >
              На главную
            </a>
          </div>
        </div>
      </div>
    );
  }
}
