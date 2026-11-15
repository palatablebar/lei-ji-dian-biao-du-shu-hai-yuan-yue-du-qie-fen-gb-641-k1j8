// 正式账页交互：发布关账版本、提交更正。所有请求走同源接口。
const state = { busy: false };

function el(id) {
  return document.getElementById(id);
}

function notice(message, isError = false) {
  const box = el("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = { error: "E_BAD_RESPONSE", message: "响应不是 JSON" };
  }
  if (!response.ok) {
    const code = payload && payload.error ? payload.error : response.status;
    const detail = payload && payload.message ? payload.message : "请求失败";
    throw new Error(code + " " + detail);
  }
  return payload;
}

async function publish() {
  if (state.busy) return;
  state.busy = true;
  try {
    const payload = await postJSON("/api/ledger/publish", {});
    const replay = payload.idempotent_replay ? " · 已存在同批次的关账版本" : "";
    notice("正式账版本 v" + payload.version.version_no + " 已发布" + replay);
    window.setTimeout(() => window.location.reload(), 500);
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    state.busy = false;
  }
}

async function submitCorrection(event) {
  event.preventDefault();
  if (state.busy) return;
  state.busy = true;
  const item = {
    meter_id: el("f-meter").value,
    register: el("f-register").value,
    read_at: el("f-readat").value.trim(),
    reading: el("f-reading").value.trim(),
    unit: el("f-unit").value.trim(),
  };
  const body = {
    items: [item],
    reason: el("f-reason").value.trim(),
    ticket: el("f-ticket").value.trim(),
    posted_month: el("f-posted").value.trim(),
  };
  try {
    const payload = await postJSON("/api/corrections", body);
    const cid = payload.correction.correction_id;
    if (payload.status === "noop") {
      notice("更正 " + cid + " 结果与当前版本一致，未产生新版本");
    } else {
      const replay = payload.idempotent_replay ? " · 重跑命中，未重复调账" : "";
      notice("更正 " + cid + " 已入账，版本 v" + payload.version.version_no + replay);
    }
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    state.busy = false;
  }
}

function bind() {
  const publishButton = el("publish-button");
  if (publishButton) publishButton.addEventListener("click", publish);
  const form = el("correction-form");
  if (form) form.addEventListener("submit", submitCorrection);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const box = el("notice");
      if (box) box.hidden = true;
    }
  });
}

bind();
