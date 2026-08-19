import { useEffect, useRef, useState } from "react";
import { m } from "framer-motion";
import { ScrollText, Wifi, WifiOff } from "lucide-react";
import GlassCard from "../components/ui/GlassCard.jsx";
import { fadeUp, staggerContainer } from "../lib/animations.js";
import { postJson } from "../lib/api.js";
import { useEventStream } from "../lib/stream.js";

/**
 * LogsPage — что происходит прямо сейчас.
 *
 * Отдельный экран, а не подвал мониторинга: когда процесс молчит десять минут, смотреть
 * надо именно сюда — ждёт он площадку, упёрся в квоту или завис. Автопрокрутка
 * отключается, как только пользователь сам отлистал вверх: иначе невозможно прочитать
 * то, что уехало.
 */
const LEVEL_TONE = {
  error: "text-rose-300",
  warn: "text-amber-300",
  found: "text-brand-soft",
  dim: "text-muted/70",
  info: "text-ink/90",
};

export default function LogsPage() {
  const { logs, connected, clearLogs } = useEventStream();
  const [follow, setFollow] = useState(true);
  const boxRef = useRef(null);

  useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [logs, follow]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    // «Почти внизу» вместо строгого равенства: дробные высоты строк не дают точного нуля.
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    setFollow(atBottom);
  };

  async function clearLog() {
    try {
      await postJson("/api/logs/clear", {});
    } finally {
      clearLogs();
    }
  }

  return (
    <m.div variants={staggerContainer} initial="hidden" animate="show"
           className="mx-auto max-w-5xl space-y-4">
      <GlassCard variants={fadeUp} className="p-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-brand to-ocean shadow-glow">
            <ScrollText className="size-5 text-white" />
          </span>
          <div>
            <h1 className="text-xl font-extrabold tracking-tight text-white">Логи</h1>
            <p className="text-xs text-muted">Что делает проверка в эту минуту</p>
          </div>
          <span className={`ml-auto flex items-center gap-1.5 text-xs ${connected ? "text-emerald-300" : "text-muted"}`}>
            {connected ? <Wifi className="size-3.5" /> : <WifiOff className="size-3.5" />}
            {connected ? "поток live" : "поток оборван"}
          </span>
          {!follow && (
            <button
              onClick={() => setFollow(true)}
              className="rounded-lg border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs font-semibold text-muted hover:text-ink"
            >
              ↓ К последним
            </button>
          )}
          {/* Лог живёт в памяти процесса, и до этой кнопки очистить его можно было
              только перезапуском сервера — то есть заодно погасив обход. */}
          <button
            onClick={clearLog}
            title="Забыть накопленные строки. На идущий обход не влияет"
            className="rounded-lg border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs font-semibold text-muted hover:text-ink"
          >
            Очистить
          </button>
        </div>
      </GlassCard>

      <GlassCard variants={fadeUp} className="p-0">
        <div
          ref={boxRef}
          onScroll={onScroll}
          className="max-h-[65vh] overflow-y-auto p-4 font-mono text-[12.5px] leading-relaxed"
        >
          {logs.length === 0 ? (
            <p className="py-8 text-center font-sans text-sm text-muted">
              Пока тихо. Запустите мониторинг — здесь пойдут строки о каждом кейсе.
            </p>
          ) : (
            logs.map((line, i) => (
              <div key={i} className="flex gap-3">
                <span className="shrink-0 text-muted/50">{line.at?.slice(11, 19)}</span>
                <span className={LEVEL_TONE[line.level] || LEVEL_TONE.info}>
                  {line.message}
                </span>
              </div>
            ))
          )}
        </div>
      </GlassCard>
    </m.div>
  );
}
