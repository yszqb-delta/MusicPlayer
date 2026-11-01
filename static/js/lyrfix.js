/* 手动补词页：列出缺词的歌，粘贴/上传 LRC 保存，或转自动补词 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
};

const api = async (url, options = {}) => {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
};

async function loadNeedy() {
  const ul = $("#needy-list");
  ul.innerHTML = "";
  let songs;
  try {
    songs = (await api("/api/lyrfix/needy")).songs;
  } catch (e) {
    $("#empty-hint");
    document.querySelector("main p").textContent = "加载失败：" + e.message;
    return;
  }
  $("#needy-count").textContent = songs.length ? `缺词 ${songs.length} 首` : "";
  $("#all-done").hidden = songs.length > 0;
  songs.forEach((s) => ul.appendChild(renderItem(s)));
}

function renderItem(s) {
  const li = document.createElement("li");
  li.className = "song-item";
  li.innerHTML = `
    <div class="song-info">
      <div class="song-name">${esc(s.title)}</div>
      <div class="song-sub">${s.has_lrc ? "已有文件但只有制作信息" : "还没有歌词文件"} · ${esc(s.path)}</div>
    </div>
    <span class="song-edit">
      <button class="mini-btn" data-act="expand">补词</button>
      <button class="mini-btn" data-act="auto">试试自动补词</button>
    </span>
    <div class="lyrfix-form" hidden>
      <textarea class="lrc-input" rows="8" placeholder="粘贴 LRC 歌词（支持带时间戳或纯文本）"></textarea>
      <div class="row" style="display:flex;gap:10px;margin-top:8px">
        <button class="mini-btn" data-act="pick">上传 .lrc 文件</button>
        <button class="primary-btn" data-act="save">保存歌词</button>
        <input type="file" accept=".lrc,text/plain" hidden>
      </div>
    </div>`;
  const form = li.querySelector(".lyrfix-form");
  const ta = li.querySelector("textarea");
  const fileInput = li.querySelector("input[type=file]");

  li.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.act;
      if (act === "expand") {
        form.hidden = !form.hidden;
        if (!form.hidden) ta.focus();
        return;
      }
      if (act === "pick") { fileInput.click(); return; }
      if (act === "save") {
        const text = ta.value.trim();
        if (!text) { alert("先粘贴或上传歌词内容"); return; }
        try {
          await api(`/api/songs/${encodeURIComponent(s.path)}/lyrics`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
          });
          li.remove();
          bumpCount(-1);
          alert("已保存：" + s.title);
        } catch (e) { alert(e.message); }
        return;
      }
      if (act === "auto") {
        btn.disabled = true;
        btn.textContent = "补词中…";
        try {
          await api(`/api/songs/${encodeURIComponent(s.path)}/lyrfix`, { method: "POST" });
        } catch (e) {
          btn.disabled = false; btn.textContent = "试试自动补词";
          alert(e.message); return;
        }
        const timer = setInterval(async () => {
          let st;
          try { st = await api("/api/lyrfix/status"); } catch { return; }
          if (st.running) return;
          clearInterval(timer);
          btn.disabled = false;
          btn.textContent = "试试自动补词";
          const r = (st.results && st.results[0]) || null;
          if (r && r.action === "updated") {
            li.remove();
            bumpCount(-1);
            alert(`已自动补上 ${r.lines} 行（来源：${r.source || "?"}）`);
          } else if (r) {
            alert("没有补上：" + (r.reason || "未知原因") + "\n可以手动粘贴歌词。");
          }
        }, 2000);
      }
    });
  });
  fileInput.addEventListener("change", () => {
    const f = fileInput.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => { ta.value = reader.result; };
    reader.readAsText(f, "utf-8");
  });
  return li;
}

function bumpCount(delta) {
  const el = $("#needy-count");
  const m = el.textContent.match(/\d+/);
  if (!m) return;
  const n = Number(m[0]) + delta;
  el.textContent = n > 0 ? `缺词 ${n} 首` : "";
  $("#all-done").hidden = n > 0;
}

$("#btn-reload").onclick = loadNeedy;
loadNeedy();
