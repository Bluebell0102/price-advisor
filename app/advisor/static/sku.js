// 单品页：试算滑块、采纳价校验、忽略弹层。只在本机请求 /sku/<编号>/estimate
(function () {
  const money = (v) => "¥" + Number(v).toFixed(2);
  const cell = (x) => {
    if (x["好坏"] === true) return `<span class="up">${x["变化"]}</span>`;
    if (x["好坏"] === false) return `<span class="down">${x["变化"]}</span>`;
    if (x["变化"] === "未计" || x["变化"] === "无访客数") return `<span class="unk">${x["变化"]}</span>`;
    return `<span class="muted">${x["变化"]}</span>`;
  };

  const el = document.getElementById("slider");
  if (el) {
    const out = document.getElementById("slider-price");
    const head = document.getElementById("try-head");
    const body = document.querySelector("#try-table tbody");
    let timer;
    el.addEventListener("input", () => {
      out.textContent = money(el.value);
      head.textContent = "试算 " + money(el.value);
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const r = await fetch(`/sku/${encodeURIComponent(el.dataset.sku)}/estimate?price=${el.value}`);
        if (!r.ok) return;
        const d = await r.json();
        body.innerHTML = d["行"].map((x) => `<tr><td>${x["指标"]}</td><td class="num">${x["当前"]}</td><td class="num">${x["建议"]}</td><td class="num">${cell(x)}</td></tr>`).join("");
      }, 60);
    });
  }

  // 采纳价：低于最低建议价不可提交；高于最高建议价可以提交，记为区间外采纳
  const form = document.getElementById("adopt-form");
  if (form) {
    const input = document.getElementById("adopt-price");
    const btn = document.getElementById("adopt-btn");
    const hint = document.getElementById("adopt-hint");
    const low = parseFloat(form.dataset.low), high = parseFloat(form.dataset.high);
    const base = hint.textContent;
    const check = () => {
      const v = parseFloat(String(input.value).replace("¥", ""));
      if (isNaN(v)) { btn.disabled = true; hint.textContent = "采纳价请填数字"; return; }
      if (v < low - 1e-9) { btn.disabled = true; hint.textContent = `低于最低建议价 ${money(low)}，不能采纳：最低建议价已含保本线`; return; }
      btn.disabled = false;
      hint.textContent = v > high + 1e-9 ? `高于最高建议价 ${money(high)}，可以采纳，会记为「区间外采纳」` : base;
    };
    input.addEventListener("input", check);
    check();
  }

  // 采纳或忽略提交后，居中弹出结果（弹层在最上层）
  const done = document.getElementById("done-dialog");
  if (done && !done.open) done.showModal();

  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open]");
    if (open) document.getElementById(open.dataset.open).showModal();
    const close = e.target.closest("[data-close]");
    if (close) close.closest("dialog").close();
  });
})();
