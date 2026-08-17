// Переиспользуемые варианты анимаций Framer Motion.
// Держим их в одном месте, чтобы тайминги/пружины были консистентны по всему UI.

// Мягкая «пружина» — основной переход для появления и hover-эффектов.
export const spring = { type: "spring", stiffness: 320, damping: 28, mass: 0.7 };

// Контейнер со ступенчатым (stagger) появлением детей.
export const staggerContainer = {
  hidden: { opacity: 0 },
  show: {
    opacity: 1,
    transition: { staggerChildren: 0.06, delayChildren: 0.08 },
  },
};

// Элемент, всплывающий снизу — для полей формы, карточек, заголовков.
export const fadeUp = {
  hidden: { opacity: 0, y: 14, filter: "blur(6px)" },
  show: { opacity: 1, y: 0, filter: "blur(0px)", transition: spring },
};

// Появление колонок дашборда слева/справа.
export const slideIn = (from = "left") => ({
  hidden: { opacity: 0, x: from === "left" ? -28 : 28 },
  show: { opacity: 1, x: 0, transition: { ...spring, damping: 24 } },
});

// Строка лога терминала: «вылетает» слева с лёгким блюром.
export const logLine = {
  hidden: { opacity: 0, x: -12, filter: "blur(4px)" },
  show: { opacity: 1, x: 0, filter: "blur(0px)", transition: { duration: 0.28, ease: "easeOut" } },
  exit: { opacity: 0, transition: { duration: 0.15 } },
};
