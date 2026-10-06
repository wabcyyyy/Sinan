(() => {
  const buttons = Array.from(document.querySelectorAll(".filter-btn"));
  const tools = Array.from(document.querySelectorAll(".tool"));

  if (!buttons.length || !tools.length) return;

  function applyFilter(key) {
    tools.forEach((tool) => {
      const cats = (tool.dataset.cats || "").split(/\s+/);
      const show = key === "all" || cats.includes(key);
      tool.classList.toggle("is-hidden", !show);
    });

    buttons.forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.filter === key));
    });
  }

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      applyFilter(btn.dataset.filter || "all");
    });
  });
})();
