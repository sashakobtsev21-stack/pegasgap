// Обёртка над fetch: таймаут + ретраи на транзиентных ответах.
//
// Авторизации нет и не предполагается — инструмент внутренний, поднимается локально или
// на служебной машине. Поэтому здесь нет ни cookie-сессий, ни CSRF-токенов: они были бы
// украшением без модели угроз, которую защищают.
//
//  • timeoutMs — внутренний AbortController отменит зависший запрос; внешний opts.signal
//    комбинируется (отменится по любому из двух);
//  • ретраим 429/502/503/504 только на GET/HEAD, backoff 500мс·2^n либо Retry-After.
//    Мутирующие запросы автоматически не повторяем — риск запустить прогон дважды.

const SAFE = new Set(["GET", "HEAD"]);
const RETRY_STATUSES = new Set([429, 502, 503, 504]);
const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_RETRIES = 2;

function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    if (!signal) return;
    if (signal.aborted) { clearTimeout(t); reject(new DOMException("aborted", "AbortError")); return; }
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("aborted", "AbortError"));
    }, { once: true });
  });
}

export async function apiFetch(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const externalSignal = opts.signal;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const retryEnabled = opts.retry === undefined ? SAFE.has(method) : Boolean(opts.retry);

  let lastError;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(
      () => ctrl.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);
    const onExternalAbort = () => ctrl.abort();
    if (externalSignal) {
      if (externalSignal.aborted) ctrl.abort();
      else externalSignal.addEventListener("abort", onExternalAbort, { once: true });
    }
    try {
      const res = await fetch(url, { ...opts, method, signal: ctrl.signal });
      if (retryEnabled && RETRY_STATUSES.has(res.status) && attempt < MAX_RETRIES) {
        const retryAfter = parseInt(res.headers.get("Retry-After") || "", 10);
        await delay(Number.isNaN(retryAfter) ? 500 * 2 ** attempt : retryAfter * 1000,
                    externalSignal);
        continue;
      }
      return res;
    } catch (err) {
      lastError = err;
      if (!retryEnabled || attempt >= MAX_RETRIES
          || err.name === "AbortError" || err.name === "TimeoutError") throw err;
      await delay(500 * 2 ** attempt, externalSignal);
    } finally {
      clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort);
    }
  }
  throw lastError;
}

/** GET с разбором JSON. Бросает при не-2xx — вызывающий показывает ошибку пользователю. */
export async function getJson(url, opts = {}) {
  const res = await apiFetch(url, opts);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** POST JSON. Прогон долгий, поэтому таймаут по умолчанию щедрый. */
export async function postJson(url, body, opts = {}) {
  const res = await apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    timeoutMs: 600_000,
    ...opts,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch { /* тело не JSON — оставляем код статуса */ }
    throw new Error(detail);
  }
  return res.json();
}
