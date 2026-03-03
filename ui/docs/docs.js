(function () {
  var THEME_KEY = "qast-theme";
  var root = document.documentElement;
  var saved = localStorage.getItem(THEME_KEY);
  root.setAttribute("data-theme", saved === "light" ? "light" : "dark");

  var toggle = document.getElementById("theme-toggle");

  function refreshLabel() {
    if (!toggle) return;
    var isDark = root.getAttribute("data-theme") !== "light";
    toggle.textContent = isDark ? "Light mode" : "Dark mode";
  }
  refreshLabel();

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      localStorage.setItem(THEME_KEY, next);
      refreshLabel();
    });
  }

  // Highlight current page in sidebar
  var path = window.location.pathname;
  var links = document.querySelectorAll("aside a");
  links.forEach(function (a) {
    var href = a.getAttribute("href");
    if (href === path || (path.endsWith("/") && href === path.slice(0, -1))) {
      a.classList.add("active");
    }
  });
})();
