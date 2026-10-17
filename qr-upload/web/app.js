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
    const cleanName = file.name.trim();
    const validName = cleanName.length > 0 && !cleanName.startsWith(".") &&
      new TextEncoder().encode(file.name).length <= 240 && !/[\\/:\p{Cc}\p{Cf}]/u.test(file.name);
    const valid = validName && /\.(pdf|epub)$/i.test(cleanName) && file.size > 0 && file.size <= 200000000;
    state.textContent = !validName ? "文件名过长或含不支持的字符，请重命名后重选" :
      valid ? "待发送" : "仅支持不超过 200 MB 的非空 PDF/EPUB；更大文件请使用 USB 传输";
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
    const started = performance.now();
    let loaded = 0;
    let changedAt = started;
    let sent = false;
    const showProgress = () => {
      const seconds = Math.floor((performance.now() - started) / 1000);
      if (sent) {
        item.state.textContent = `已发送，等待设备校验和系统导入 · ${seconds} 秒`;
      } else if (loaded === 0) {
        item.state.textContent = `正在读取文件或连接设备 · 已等待 ${seconds} 秒`;
      } else {
        const percent = Math.min(100, Math.floor(100 * loaded / item.file.size));
        const stalled = performance.now() - changedAt >= 8000;
        item.state.textContent = `${stalled ? "等待继续上传" : "发送中"} ${percent}% · ${(loaded / 1000000).toFixed(1)} / ${(item.file.size / 1000000).toFixed(1)} MB · ${seconds} 秒`;
      }
    };
    const timer = setInterval(showProgress, 1000);
    active = xhr;
    xhr.open("POST", "/upload");
    xhr.setRequestHeader("Authorization", "Bearer " + token);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(item.file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 6 * 60 * 1000;
    xhr.upload.onprogress = event => {
      if (event.loaded > loaded) changedAt = performance.now();
      loaded = event.loaded;
      if (event.lengthComputable) progress.value = 100 * event.loaded / event.total;
      showProgress();
    };
    xhr.upload.onload = () => {
      sent = true;
      progress.removeAttribute("value");
      showProgress();
    };
    xhr.onloadend = () => clearInterval(timer);
    xhr.onload = () => {
      let data;
      try { data = JSON.parse(xhr.responseText); }
      catch (_) { reject(new Error("无法确认导入结果，请先检查设备资料库。")); return; }
      if (xhr.status === 200) resolve(data.message);
      else reject(new Error(data.message || "上传失败，请先检查设备资料库。"));
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error("连接中断，请先检查设备资料库，避免重复上传。"));
    xhr.onabort = () => reject(new Error("已取消传输；若已进入导入阶段，请先检查设备资料库。"));
    showProgress();
    progress.removeAttribute("value");
    try { xhr.send(item.file); }
    catch (_) {
      clearInterval(timer);
      reject(new Error("浏览器无法读取所选文件，请确认该文件已完整保存在手机上。"));
    }
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
