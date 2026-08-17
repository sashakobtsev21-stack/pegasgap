/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Основной интерфейсный шрифт + моноширинный для терминала логов.
        sans: ['"Plus Jakarta Sans"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        // Глубокая «ночная» палитра + индиго-бренд + бирюза «океан/путешествия».
        ink: "#e7ecff", // основной текст на тёмном фоне
        muted: "#8a93b8", // приглушённый текст
        brand: {
          DEFAULT: "#7c83ff",
          deep: "#5a62f0",
          soft: "#a9adff",
        },
        ocean: "#38e0d8", // акцент «бирюза»
        sunset: "#ff9b7d", // тёплый акцент для статусов
        // Стеклянные подложки (используются через bg-glass / border-glass).
        glass: "rgba(255,255,255,0.05)",
      },
      boxShadow: {
        glass: "0 24px 60px -28px rgba(8,12,35,0.85)",
        glow: "0 0 0 1px rgba(124,131,255,0.35), 0 8px 30px -8px rgba(124,131,255,0.5)",
        "inset-terminal": "inset 0 2px 24px rgba(0,0,0,0.55)",
      },
      borderRadius: {
        xl2: "1.5rem",
      },
      keyframes: {
        floatA: {
          "0%,100%": { transform: "translate3d(0,0,0) scale(1)" },
          "50%": { transform: "translate3d(3%,-5%,0) scale(1.12)" },
        },
        floatB: {
          "0%,100%": { transform: "translate3d(0,0,0) scale(1.06)" },
          "50%": { transform: "translate3d(-4%,4%,0) scale(0.94)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        pulseDot: { "50%": { opacity: "0.3" } },
      },
      animation: {
        floatA: "floatA 22s ease-in-out infinite",
        floatB: "floatB 27s ease-in-out infinite",
        shimmer: "shimmer 2.2s linear infinite",
        pulseDot: "pulseDot 1.1s steps(2,start) infinite",
      },
    },
  },
  plugins: [],
};
