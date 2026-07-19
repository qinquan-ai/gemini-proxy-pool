const STORAGE_KEY = "gemini-pool-theme";

function preferredTheme() {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(STORAGE_KEY, theme);
  const button = document.getElementById("theme-button");
  if (button) button.setAttribute("aria-label", theme === "dark" ? "切换到浅色主题" : "切换到深色主题");
}

export function initializeTheme() {
  setTheme(preferredTheme());
  document.getElementById("theme-button")?.addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
}
