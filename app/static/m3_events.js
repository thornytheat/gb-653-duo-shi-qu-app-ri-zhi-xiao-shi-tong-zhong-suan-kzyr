// M3 事件去重页面：真实批次导入、契约切换/回退、批次核对
const $ = (id) => document.getElementById(id);
const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  return { ok: res.ok, status: res.status, data };
}

function show(el, ok, data) {
  el.textContent = JSON.stringify(data, null, 2);
  el.style.borderLeftColor = ok ? "var(--accent)" : "var(--warn)";
}

$("btn-switch")?.addEventListener("click", async () => {
  const out = $("contract-result");
  const { ok, data } = await post("/api/events/contracts/switch", {
    service: $("c-service").value.trim(),
    kind: $("c-kind").value,
    expected_version: num($("c-expected").value),
    note: $("c-note").value,
  });
  show(out, ok, data);
  if (ok) setTimeout(() => location.reload(), 600);
});

$("btn-rollback")?.addEventListener("click", async () => {
  const out = $("contract-result");
  const { ok, data } = await post("/api/events/contracts/rollback", {
    service: $("c-service").value.trim(),
    target_version: num($("c-target").value),
    expected_version: num($("c-expected").value),
  });
  show(out, ok, data);
  if (ok) setTimeout(() => location.reload(), 600);
});

$("btn-import")?.addEventListener("click", async () => {
  const out = $("import-result");
  const payload = {
    service: $("b-service").value.trim(),
    text: $("b-text").value,
    expected_version: num($("b-expected").value),
    stop_after: num($("b-stop").value),
  };
  const id = $("b-id").value.trim();
  if (id) payload.batch_id = id;
  const { ok, data } = await post("/api/events/dedupe", payload);
  show(out, ok, data);
  if (ok && data.batch_id && !$("b-id").value) $("b-id").value = data.batch_id;
  if (ok) setTimeout(() => location.reload(), 900);
});

document.querySelectorAll(".batch-link").forEach((link) =>
  link.addEventListener("click", async (event) => {
    event.preventDefault();
    const res = await fetch(`/api/events/batches/${link.dataset.batch}`);
    const data = await res.json();
    show($("import-result"), res.ok, data);
  })
);
