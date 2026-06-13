(function () {
  const button = document.getElementById("themeToggle");
  const icon = document.getElementById("themeIcon");
  const root = document.documentElement;
  const saved = localStorage.getItem("ct-theme");
  if (saved) root.setAttribute("data-bs-theme", saved);

  function updateIcon() {
    if (!icon) return;
    icon.className = root.getAttribute("data-bs-theme") === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
  }

  updateIcon();
  if (button) {
    button.addEventListener("click", function () {
      const next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      localStorage.setItem("ct-theme", next);
      updateIcon();
    });
  }
})();
