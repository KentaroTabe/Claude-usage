// ツールチップと十字線。値は表でも読めるので、これは補助。文字列は textContent で入れる(外部入力のため)。
(() => {
  const tip = document.getElementById("tooltip");
  if (!tip) return;
  const tipValue = tip.querySelector(".tip-value");
  const tipLabel = tip.querySelector(".tip-label");

  function show(value, label, x, y) {
    tipValue.textContent = value || "";
    tipLabel.textContent = label || "";
    tip.hidden = false;
    const pad = 12;
    const r = tip.getBoundingClientRect();
    let left = x + pad;
    let top = y + pad;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - pad;
    if (top + r.height > window.innerHeight - 8) top = y - r.height - pad;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  function hide() { tip.hidden = true; }

  document.querySelectorAll(".mark").forEach((m) => {
    m.addEventListener("pointermove", (e) => show(m.dataset.tipValue, m.dataset.tipLabel, e.clientX, e.clientY));
    m.addEventListener("pointerleave", hide);
    m.addEventListener("focus", () => {
      const r = m.getBoundingClientRect();
      show(m.dataset.tipValue, m.dataset.tipLabel, r.left + r.width / 2, r.top);
    });
    m.addEventListener("blur", hide);
  });

  document.querySelectorAll(".hover-target").forEach((target) => {
    const points = JSON.parse(target.dataset.points || "[]");
    const svg = target.ownerSVGElement;
    const line = svg.querySelector(".crosshair");
    const dot = svg.querySelector(".crosshair-dot");
    if (!points.length) return;
    target.addEventListener("pointermove", (e) => {
      const p = svg.createSVGPoint();
      p.x = e.clientX;
      p.y = e.clientY;
      const loc = p.matrixTransform(svg.getScreenCTM().inverse());
      let best = points[0];
      for (const pt of points) if (Math.abs(pt.x - loc.x) < Math.abs(best.x - loc.x)) best = pt;
      line.setAttribute("x1", best.x);
      line.setAttribute("x2", best.x);
      dot.setAttribute("cx", best.x);
      dot.setAttribute("cy", best.y);
      line.style.visibility = "visible";
      dot.style.visibility = "visible";
      show(best.v, best.t, e.clientX, e.clientY);
    });
    target.addEventListener("pointerleave", () => {
      line.style.visibility = "hidden";
      dot.style.visibility = "hidden";
      hide();
    });
  });
})();
