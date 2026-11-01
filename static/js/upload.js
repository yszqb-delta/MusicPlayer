/* 上传页：拖拽 / 多选，逐文件显示进度 */
"use strict";

const $ = (sel) => document.querySelector(sel);

function addItem(name) {
  const li = document.createElement("li");
  li.className = "upload-item";
  li.innerHTML = `<div class="row-line"><span class="name"></span><span class="status">等待中</span></div><div class="bar"><i></i></div>`;
  li.querySelector(".name").textContent = name;
  $("#upload-list").appendChild(li);
  return li;
}

function uploadFile(file) {
  const li = addItem(file.name);
  const status = li.querySelector(".status");
  const barFill = li.querySelector(".bar i");

  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    const fd = new FormData();
    fd.append("files", file);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        const pct = Math.round(e.loaded / e.total * 100);
        barFill.style.width = pct + "%";
        status.textContent = pct + "%";
      }
    });
    xhr.addEventListener("load", () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* 忽略解析失败 */ }
      if (xhr.status === 200) {
        li.classList.add("done");
        status.textContent = data.saved.length ? "✓ 完成" : "✗ " + (data.failed[0] || {}).reason;
        if (!data.saved.length) li.classList.add("fail");
        else status.textContent = "✓ 已保存为 " + data.saved[0];
      } else {
        li.classList.add("fail");
        status.textContent = "✗ " + (data.error || `失败 (${xhr.status})`);
      }
      resolve();
    });
    xhr.addEventListener("error", () => {
      li.classList.add("fail");
      status.textContent = "✗ 网络错误";
      resolve();
    });
    xhr.open("POST", "/api/upload");
    xhr.send(fd);
  });
}

async function handleFiles(files) {
  $("#btn-pick").disabled = true;
  for (const f of files) await uploadFile(f);   // 串行上传，避免拖垮小服务器
  $("#btn-pick").disabled = false;
}

$("#btn-pick").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (e) => {
  handleFiles([...e.target.files]);
  e.target.value = "";
});

const zone = $("#drop-zone");
["dragenter", "dragover"].forEach((ev) =>
  zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("dragover"); }));
["dragleave", "drop"].forEach((ev) =>
  zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("dragover"); }));
zone.addEventListener("drop", (e) => handleFiles([...e.dataTransfer.files]));
