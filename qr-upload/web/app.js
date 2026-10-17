"use strict";

const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
const filesInput = document.getElementById("files");
const send = document.getElementById("send");
const cancel = document.getElementById("cancel");
const queue = document.getElementById("queue");
const result = document.getElementById("result");
const progress = document.getElementById("progress");
const connection = document.getElementById("connection");
let pending = [];
let active = null;
let canceled = false;

async function connect() {
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
    connection.textContent = "链接无效";
    result.textContent = "请重新扫描设备上的二维码。";
    return;
  }
  try {
    const response = await fetch("/status", {
      headers: { Authorization: "Bearer " + token }, cache: "no-store"
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message);
    connection.textContent = data.message;
    filesInput.disabled = false;
  } catch (error) {
    connection.textContent = "未连接";
    result.textContent = error.message || "无法连接设备，请检查 Wi-Fi。";
  }
}

filesInput.addEventListener("change", () => {
  queue.replaceChildren();
  result.textContent = "";
  pending = Array.from(filesInput.files).map(file => {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = file.name;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = (file.size / 1000000).toFixed(1) + " MB";
    const state = document.createElement("span");
    state.className = "state";
    const valid = /\.(pdf|epub)$/i.test(file.name) && file.size > 0 && file.size <= 100000000;
    state.textContent = valid ? "待发送" : "仅支持不超过 100 MB 的非空 PDF/EPUB";
    row.dataset.state = valid ? "pending" : "error";
    row.append(name, size, state);
    queue.append(row);
    return { file, row, state, valid, done: false };
  });
  send.disabled = !pending.length || pending.some(item => !item.valid);
});

function upload(item) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    active = xhr;
    xhr.open("POST", "/upload");
    xhr.setRequestHeader("Authorization", "Bearer " + token);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(item.file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 6 * 60 * 1000;
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) progress.value = 100 * event.loaded / event.total;
    };
    xhr.upload.onload = () => { item.state.textContent = "等待系统导入"; };
    xhr.onload = () => {
      let data;
      try { data = JSON.parse(xhr.responseText); }
      catch (_) { reject(new Error("无法确认导入结果，请先检查设备资料库。")); return; }
      if (xhr.status === 200) resolve(data.message);
      else reject(new Error(data.message || "上传失败，请先检查设备资料库。"));
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error("连接中断，请先检查设备资料库，避免重复上传。"));
    xhr.onabort = () => reject(new Error("已取消传输；若已进入导入阶段，请先检查设备资料库。"));
    item.state.textContent = "发送中";
    progress.value = 0;
    xhr.send(item.file);
  });
}

send.addEventListener("click", async () => {
  send.disabled = true;
  filesInput.disabled = true;
  cancel.disabled = false;
  progress.hidden = false;
  result.textContent = "";
  canceled = false;
  let complete = true;
  for (const item of pending) {
    if (canceled) { complete = false; break; }
    if (item.done) continue;
    try {
      item.state.textContent = await upload(item);
      item.row.dataset.state = "done";
      item.done = true;
    } catch (error) {
      item.row.dataset.state = "error";
      item.state.textContent = error.message;
      result.textContent = "传输已暂停。确认设备上的文档后，重新选择需要发送的文件。";
      complete = false;
      break;
    } finally {
      active = null;
    }
  }
  if (complete) result.textContent = "系统已接受全部文档，请在设备资料库查看。";
  cancel.disabled = true;
  filesInput.disabled = false;
  filesInput.value = "";
  progress.hidden = true;
});

cancel.addEventListener("click", () => { canceled = true; if (active) active.abort(); });
connect();
