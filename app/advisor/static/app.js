// 设置页：换平台时把扣点率填成该平台的默认值（只影响默认值，店主仍可改）；实时显示保本价分母
function pct(name) {
  const el = document.querySelector(`input[name='${name}']`);
  const v = parseFloat(el && el.value);
  return isNaN(v) ? NaN : v / 100;
}
function refreshDenominator() {
  const out = document.getElementById("denominator");
  if (!out) return;
  const d = 1 - pct("扣点率") - pct("退货率") * pct("单次退货净损失占比");
  out.textContent = isNaN(d) ? "—" : d.toFixed(4);
}
document.addEventListener("input", refreshDenominator);
document.addEventListener("DOMContentLoaded", refreshDenominator);
document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.name === "平台" && el.dataset.rates) {
    const rates = JSON.parse(el.dataset.rates);
    const box = document.querySelector("input[name='扣点率']");
    if (box && el.value in rates) box.value = +(rates[el.value] * 100).toFixed(4);
    refreshDenominator();
  }
  if (el.matches("select[data-autosubmit]")) el.form.submit();
  // 导入页：选了文件后在框里显示文件名
  if (el.type === "file") {
    const drop = el.closest(".drop");
    if (drop) {
      const f = drop.querySelector(".file");
      const name = el.files[0] ? el.files[0].name : "";
      f.textContent = name || "未选择文件";
      f.classList.toggle("empty", !name);
      drop.classList.toggle("has", !!name);
    }
  }
});
// 提交后给出进度文字，防止重复点
document.addEventListener("submit", (e) => {
  const f = e.target;
  if (f.dataset.busy) {
    const b = f.querySelector("button[type=submit]");
    if (b) { b.disabled = true; b.textContent = f.dataset.busy; }
  }
});
