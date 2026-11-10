// Easter egg: dois cliques (ou dois toques) na logo da clínica mostram quem idealizou o sistema.
(function () {
  function show() {
    if (document.getElementById("egg")) return;
    var o = document.createElement("div");
    o.id = "egg";
    o.setAttribute("role", "dialog");
    o.setAttribute("aria-label", "Créditos");
    o.innerHTML =
      '<div class="egg-card"><img src="/static/arthur.jpg" alt="Foto de Arthur" width="180" height="180">' +
      '<p class="egg-name">Arthur 👑</p><p class="egg-txt">Idealizou, pensou e planejou este sistema.</p>' +
      '<p class="egg-close">Toque para fechar</p></div>';
    o.addEventListener("click", function () { o.remove(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { o.remove(); document.removeEventListener("keydown", esc); }
    });
    document.body.appendChild(o);
  }
  var last = 0;
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-egg]")) return;
    var now = Date.now();
    if (now - last < 450) { show(); last = 0; } else { last = now; }
  });
})();
