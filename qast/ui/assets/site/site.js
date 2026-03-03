(function () {
  const THEME_KEY = "qast-theme";
  const root = document.documentElement;
  const savedTheme = localStorage.getItem(THEME_KEY);
  root.setAttribute("data-theme", savedTheme === "light" ? "light" : "dark");

  const toggle = document.getElementById("theme-toggle");
  const count = document.getElementById("github-stars-count");

  function refreshToggleLabel() {
    if (!toggle) return;
    const isDark = root.getAttribute("data-theme") !== "light";
    toggle.textContent = isDark ? "Light mode" : "Dark mode";
  }

  refreshToggleLabel();

  if (toggle) {
    toggle.addEventListener("click", function () {
      const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      localStorage.setItem(THEME_KEY, next);
      refreshToggleLabel();
    });
  }

  fetch("https://api.github.com/repos/richlegrand/qast", {
    headers: { Accept: "application/vnd.github+json" },
  })
    .then(function (res) {
      if (!res.ok) throw new Error("github request failed");
      return res.json();
    })
    .then(function (data) {
      if (!count) return;
      if (typeof data.stargazers_count === "number") {
        count.textContent = String(data.stargazers_count);
      }
    })
    .catch(function () {
      if (count) count.textContent = "87";
    });
})();
