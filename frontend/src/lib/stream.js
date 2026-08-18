import { useEffect, useRef, useState } from "react";

/**
 * Подписка на живой поток событий сервера (SSE).
 *
 * Одно соединение на страницу, переподключение при обрыве — процесс работает часами, и
 * вкладка, которая молча отвалилась через десять минут, хуже отсутствия живого потока:
 * она выглядит работающей.
 *
 * События копятся в кольцевом буфере: за сутки их набегают тысячи, а держать всё в
 * памяти вкладки незачем — история и так в базе.
 */
const MAX_LOGS = 500;
const MAX_FINDINGS = 300;
const RECONNECT_MS = 3000;

export function useEventStream() {
  const [logs, setLogs] = useState([]);
  const [findings, setFindings] = useState([]);
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef(null);

  useEffect(() => {
    let closed = false;
    let retry;

    const connect = () => {
      if (closed) return;
      const source = new EventSource("/api/stream");
      sourceRef.current = source;

      source.onopen = () => setConnected(true);
      source.onmessage = (e) => {
        let event;
        try {
          event = JSON.parse(e.data);
        } catch {
          return;             // пульс-комментарий или мусор — молча пропускаем
        }
        if (event.kind === "log") {
          setLogs((prev) => [...prev, event].slice(-MAX_LOGS));
        } else if (event.kind === "finding") {
          setFindings((prev) => [event, ...prev].slice(0, MAX_FINDINGS));
        } else if (event.kind === "state") {
          setState(event);
        }
      };
      source.onerror = () => {
        setConnected(false);
        source.close();
        // Переподключаемся сами: браузер это тоже умеет, но молча и с своими паузами,
        // а нам нужно показать разрыв пользователю.
        retry = setTimeout(connect, RECONNECT_MS);
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      sourceRef.current?.close();
    };
  }, []);

  return { logs, findings, state, connected, clearFindings: () => setFindings([]) };
}
