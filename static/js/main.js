/* 音乐播放器前端入口
 * 模块划分：utils / modal / 列表与编辑 / 底部卡片 / 播放器 / 歌词
 */
"use strict";

/* ==================== 工具 ==================== */
const $ = (sel) => document.querySelector(sel);

const api = async (url, options = {}) => {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
};

const fmtTime = (sec) => {
  if (!isFinite(sec)) return "0:00";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
};

const fmtSize = (bytes) => {
  bytes = Number(bytes);
  if (!isFinite(bytes)) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i ? 1 : 0)} ${units[i]}`;
};

const esc = (s) => {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
};

/* ==================== 弹窗 ==================== */
const Modal = {
  open(html, onMount, extraClass = "") {
    this.close();
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = `<div class="modal${extraClass ? " " + extraClass : ""}">${html}</div>`;
    mask.addEventListener("click", (e) => { if (e.target === mask) this.close(); });
    document.getElementById("modal-root").appendChild(mask);
    if (onMount) onMount(mask.firstElementChild);
  },
  close() {
    const root = document.getElementById("modal-root");
    root.innerHTML = "";
  },
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") Modal.close();
  /* 空格键播放/暂停：输入框、下拉框内不拦截，弹窗打开时忽略 */
  if (e.code === "Space") {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (document.querySelector("#modal-root .modal")) return;
    if (!player.current) return;
    e.preventDefault();
    player.togglePlay();
  }
});

/* 弹窗输入框按回车直接确认（textarea 用 Ctrl+回车，普通回车留给换行） */
function bindEnter(input, confirmBtn, withCtrl = false) {
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (!withCtrl || e.ctrlKey)) {
      e.preventDefault();
      confirmBtn.click();
    }
  });
}

/* ==================== 全局状态 ==================== */
const state = {
  songs: [],
  mounted: false,
  editing: false,
  bg: "",
  sort: localStorage.getItem("mp_sort") || "default",   // default | alpha | rating
};

/* ==================== 歌曲列表 ==================== */
async function loadSongs() {
  try {
    const data = await api("/api/songs");
    state.songs = data.songs;
    state.mounted = data.mounted;
    if (!state.mounted) {
      $("#empty-hint").hidden = false;
      $("#empty-hint").textContent = "优盘未挂载，请在下方「优盘管理」卡片中挂载。";
    } else {
      $("#empty-hint").hidden = state.songs.length > 0;
    }
    renderList();
  } catch (e) {
    $("#empty-hint").hidden = false;
    $("#empty-hint").textContent = "加载失败：" + e.message;
  }
}

function songTitle(s) { return s.title || s.filename; }

/* 排序后的歌曲视图：首字母按中文拼音习惯（localeCompare），星级降序、同星按名称 */
function sortedSongs() {
  const list = [...state.songs];
  if (state.sort === "alpha") {
    list.sort((a, b) => songTitle(a).localeCompare(songTitle(b), "zh-Hans-CN"));
  } else if (state.sort === "rating") {
    list.sort((a, b) => (b.rating || 0) - (a.rating || 0)
      || songTitle(a).localeCompare(songTitle(b), "zh-Hans-CN"));
  }
  return list;
}

function renderList() {
  const ul = $("#song-list");
  ul.innerHTML = "";
  // 加权随机的抽中概率 = 单曲权重 / 全库总权重（与 buildQueue 的权重口径一致）
  const weightOf = (s) => Math.min(5, Math.max(1, s.rating || 2));
  const totalWeight = state.songs.reduce((sum, s) => sum + weightOf(s), 0) || 1;
  const fmtProb = (w) => {
    const p = w / totalWeight * 100;
    if (p >= 10) return `${Math.round(p)}%`;
    if (p >= 1) return `${p.toFixed(1)}%`;
    return `${p.toFixed(2)}%`;
  };
  sortedSongs().forEach((s, i) => {
    const li = document.createElement("li");
    li.className = "song-item" + (player.current && player.current.path === s.path ? " playing" : "");
    li.dataset.sid = s.path;

    const starsOn = Math.max(1, s.rating || 0);
    const starsHtml = Array.from({ length: 5 }, (_, k) =>
      `<span class="${k < starsOn ? "on" : ""}" data-star="${k + 1}">★</span>`).join("");

    li.innerHTML = `
      <span class="idx">${i + 1}</span>
      ${s.cover ? `<img class="cover-thumb" src="${esc(s.cover)}" alt="" loading="lazy" decoding="async">` : `<span class="cover-thumb"></span>`}
      <div class="song-info">
        <div class="song-name">${esc(songTitle(s))}</div>
        <div class="song-sub">${esc(s.filename)}</div>
      </div>
      <span class="prob" title="加权随机抽中概率">${fmtProb(weightOf(s))}</span>
      <span class="stars ${state.editing ? "clickable" : ""}">${starsHtml}</span>
      <span class="song-edit">
        <button class="mini-btn" data-act="rename">改名</button>
        <button class="mini-btn" data-act="cover">封面</button>
        <button class="mini-btn" data-act="lyric">歌词</button>
        <button class="mini-btn danger" data-act="delete">删除</button>
      </span>`;

    // 点行播放；编辑模式下点星星改评级
    li.addEventListener("click", (e) => {
      const star = e.target.closest("[data-star]");
      if (star && state.editing) {
        setRating(s, Number(star.dataset.star));
        return;
      }
      if (e.target.closest(".song-edit") || (star && state.editing)) return;
      player.playSong(s.path);
    });
    li.querySelectorAll("[data-act]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const act = btn.dataset.act;
        if (act === "rename") editRename(s);
        if (act === "cover") editCover(s);
        if (act === "lyric") LyricStudio.open(s);
        if (act === "delete") editDelete(s);
      });
    });
    ul.appendChild(li);
  });
}

async function setRating(song, rating) {
  try {
    const updated = await api(`/api/songs/${encodeURIComponent(song.path)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating }),
    });
    Object.assign(song, updated);
    renderList();
  } catch (e) { alert(e.message); }
}

function editRename(song) {
  Modal.open(`
    <h3>修改名称</h3>
    <input type="text" id="rn-input" value="${esc(song.title || song.filename)}">
    <div class="modal-actions">
      <button class="ghost-btn" data-x="cancel">取消</button>
      <button class="primary-btn" data-x="ok">保存</button>
    </div>`, (m) => {
    const input = m.querySelector("#rn-input");
    const okBtn = m.querySelector("[data-x=ok]");
    bindEnter(input, okBtn);
    input.focus(); input.select();
    m.querySelector("[data-x=cancel]").onclick = () => Modal.close();
    okBtn.onclick = async () => {
      const title = input.value.trim();
      if (!title) return;
      try {
        Object.assign(song, await api(`/api/songs/${encodeURIComponent(song.path)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        }));
        Modal.close(); renderList();
      } catch (e) { alert(e.message); }
    };
  });
}

function editCover(song) {
  Modal.open(`
    <h3>连接封面</h3>
    <div class="row">
      <input type="text" id="cv-url" placeholder="粘贴图片 URL">
      <button class="primary-btn" data-x="url">用 URL</button>
    </div>
    <div class="row">
      <button class="ghost-btn" data-x="pick">上传本地图</button>
      <button class="ghost-btn" data-x="clear">清除封面</button>
    </div>
    <input type="file" id="cv-file" accept="image/*" hidden>`, (m) => {
    const fileInput = m.querySelector("#cv-file");
    const apply = (body, isJson) => api(`/api/songs/${encodeURIComponent(song.path)}/cover`, {
      method: "POST",
      ...(isJson ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
                 : { body }),
    }).then((updated) => { Object.assign(song, updated); Modal.close(); renderList(); })
      .catch((e) => alert(e.message));

    m.querySelector("[data-x=url]").onclick = () => {
      const url = m.querySelector("#cv-url").value.trim();
      if (url) apply({ url }, true);
    };
    bindEnter(m.querySelector("#cv-url"), m.querySelector("[data-x=url]"));
    m.querySelector("[data-x=pick]").onclick = () => fileInput.click();
    fileInput.onchange = () => {
      const f = fileInput.files[0];
      if (f) { const fd = new FormData(); fd.append("file", f); apply(fd, false); }
    };
    m.querySelector("[data-x=clear]").onclick = () => apply({ url: "" }, true);
  });
}

/* ==================== 歌词工坊 ==================== */
/* 一条链路取代原来的「歌词 / 识别 / 补词」三个按钮：
 *   打开即列出现有歌词的每一行（时间 + 文字），用户在基础上改；
 *   「自动补词」= 网站找词 + 必剪识别时间轴 + AI 逐行对齐纠错；
 *   网站没词时自动退回识别时间轴，列出来给人改，不用从零写。 */

const fmtTs = (sec) => {
  const v = Math.max(0, Number(sec) || 0);
  const m = Math.floor(v / 60);
  return `${String(m).padStart(2, "0")}:${(v - m * 60).toFixed(2).padStart(5, "0")}`;
};

const parseTs = (str) => {
  const s = String(str || "").trim().replace(/^\[|\]$/g, "");
  const m = s.match(/^(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?$/);
  if (m) return Number(m[1]) * 60 + Number(m[2]) + (m[3] ? Number("0." + m[3]) : 0);
  const n = Number(s);
  return s !== "" && isFinite(n) ? n : NaN;
};

const LyricStudio = {
  song: null,
  meta: [],

  open(song) {
    this.song = song;
    this.meta = [];
    Modal.open(`
      <h3 class="ls-title">歌词 · ${esc(songTitle(song))}</h3>
      <p id="ls-status" class="ls-status">读取中…</p>
      <div class="ls-tools">
        <button class="mini-btn" data-x="auto">自动补词</button>
        <button class="mini-btn" data-x="asr">重新识别时间轴</button>
        <button class="mini-btn" data-x="pick">上传 .lrc</button>
        <span class="ls-spacer"></span>
        <button class="mini-btn" data-x="shift" data-v="-0.5" title="全部时间轴提前 0.5 秒">-0.5s</button>
        <button class="mini-btn" data-x="shift" data-v="0.5" title="全部时间轴延后 0.5 秒">+0.5s</button>
      </div>
      <div id="ls-lines" class="ls-lines"></div>
      <div class="ls-foot">
        <button class="mini-btn" data-x="add">＋ 加一行</button>
        <span id="ls-count" class="hint-text"></span>
        <span class="ls-spacer"></span>
        <button class="primary-btn" data-x="save">保存</button>
      </div>
      <input type="file" id="ls-file" accept=".lrc,text/plain" hidden>`,
    (m) => this.bind(m), "wide");
  },

  bind(m) {
    const fileInput = m.querySelector("#ls-file");
    m.querySelectorAll("[data-x]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const act = btn.dataset.x;
        if (act === "auto") this.auto(m);
        if (act === "asr") this.asr(m);
        if (act === "pick") fileInput.click();
        if (act === "shift") this.shift(Number(btn.dataset.v));
        if (act === "add") this.addRow();
        if (act === "save") this.save(m);
      });
    });
    fileInput.addEventListener("change", () => {
      const f = fileInput.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        const lines = String(reader.result || "").split(/\r?\n/).map((line) => {
          const mm = line.match(/^\s*\[(\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?)\]\s*(.*)$/);
          if (!mm || !mm[2].trim()) return null;
          const t = parseTs(mm[1]);
          return isFinite(t) ? { t, text: mm[2].trim() } : null;
        }).filter(Boolean);
        if (!lines.length) { alert("这个文件里没有带时间戳的歌词行"); return; }
        this.renderRows(lines);
        this.setStatus(`已载入 ${lines.length} 行，记得点保存`, "ok");
      };
      reader.readAsText(f, "utf-8");
    });
    this.load(m);
  },

  async load(m) {
    try {
      const d = await api(`/api/songs/${encodeURIComponent(this.song.path)}/lyric`);
      this.meta = d.meta || [];
      this.renderRows(d.lines || []);
      const metaNote = this.meta.length ? `（已保留 ${this.meta.length} 行制作信息）` : "";
      this.setStatus(d.lines.length
        ? `现有 ${d.lines.length} 行${metaNote}，改完点保存`
        : `还没有歌词行${metaNote}，可以「自动补词」或手动加行`);
    } catch (e) {
      this.setStatus("读取失败：" + e.message, "error");
    }
  },

  renderRows(lines) {
    const box = document.querySelector("#ls-lines");
    if (!box) return;
    box.innerHTML = "";
    if (!lines.length) {
      box.innerHTML = `<p class="ls-empty">还没有歌词行。点「自动补词」让网站找词 + AI 对齐；网站没有的话会自动退回识别时间轴，再手动改文字。</p>`;
    }
    lines.forEach((item) => box.appendChild(this.makeRow(item)));
    this.updateCount();
    document.querySelectorAll("#ls-lines .ls-row").forEach((row, i) => {
      const idx = row.querySelector(".ls-idx");
      if (idx) idx.textContent = i + 1;
    });
  },

  makeRow(item) {
    const div = document.createElement("div");
    div.className = "ls-row";
    div.innerHTML = `
      <span class="ls-idx"></span>
      <button class="ls-play" title="从这句开始播放">▶</button>
      <input class="ls-ts" value="${fmtTs(item.t)}" inputmode="decimal">
      <input class="ls-txt" value="${esc(item.text || "")}" placeholder="歌词文字">
      <button class="ls-del" title="删除这一行">✕</button>`;
    div.querySelector(".ls-play").addEventListener("click", () => this.playLine(item.t));
    div.querySelector(".ls-del").addEventListener("click", () => {
      div.remove();
      this.reindex();
    });
    div.querySelectorAll("input").forEach((inp) =>
      inp.addEventListener("input", () => this.updateCount()));
    return div;
  },

  addRow() {
    const box = document.querySelector("#ls-lines");
    if (!box) return;
    const empty = box.querySelector(".ls-empty");
    if (empty) empty.remove();
    const rows = box.querySelectorAll(".ls-row");
    const last = rows.length ? parseTs(rows[rows.length - 1].querySelector(".ls-ts").value) : -5;
    const t = isFinite(last) ? last + 5 : 0;
    const row = this.makeRow({ t, text: "" });
    box.appendChild(row);
    this.reindex();
    row.querySelector(".ls-txt").focus();
  },

  reindex() {
    document.querySelectorAll("#ls-lines .ls-row").forEach((row, i) => {
      const idx = row.querySelector(".ls-idx");
      if (idx) idx.textContent = i + 1;
    });
    this.updateCount();
  },

  updateCount() {
    const el = document.querySelector("#ls-count");
    if (!el) return;
    const rows = document.querySelectorAll("#ls-lines .ls-row");
    const filled = [...rows].filter((r) => r.querySelector(".ls-txt").value.trim()).length;
    el.textContent = `${filled} 行`;
  },

  collect() {
    const rows = [...document.querySelectorAll("#ls-lines .ls-row")];
    const lines = [], bad = [];
    rows.forEach((row, i) => {
      const text = row.querySelector(".ls-txt").value.trim();
      if (!text) return;
      const t = parseTs(row.querySelector(".ls-ts").value);
      if (!isFinite(t)) { bad.push(i + 1); return; }
      lines.push({ t, text });
    });
    lines.sort((a, b) => a.t - b.t);
    return { lines, bad };
  },

  setStatus(text, kind = "") {
    const el = document.querySelector("#ls-status");
    if (!el) return;
    el.textContent = text;
    el.className = "ls-status" + (kind ? " " + kind : "");
  },

  setBusy(busy, text) {
    const m = document.querySelector("#modal-root .modal");
    if (!m) return;
    m.querySelectorAll(".ls-tools button").forEach((b) => { b.disabled = busy; });
    if (busy && text) this.setStatus(text, "busy");
  },

  async auto(m) {
    this.setBusy(true, "网站上找词 + AI 对齐中…首次需要识别时间轴，约 1~3 分钟，请不要关闭窗口");
    try {
      const r = await api(`/api/songs/${encodeURIComponent(this.song.path)}/lyric/auto`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      this.meta = r.meta || this.meta;
      this.renderRows(r.lines || []);
      this.setStatus(r.note || "完成", r.status === "aligned" ? "ok" : "warn");
      player.refreshLyricsIfCurrent(this.song.path);
      if (r.status === "aligned") LyricHub.refreshIfOpen();
    } catch (e) {
      this.setStatus("自动补词失败：" + e.message, "error");
    }
    this.setBusy(false);
  },

  async asr(m) {
    if (!confirm("重新识别会用语音识别结果替换下面列表的文字（不会自动保存，保存前可以改）。继续？")) return;
    this.setBusy(true, "语音识别中…约 1~3 分钟，请不要关闭窗口");
    try {
      const r = await api(`/api/songs/${encodeURIComponent(this.song.path)}/lyric/asr`, { method: "POST" });
      this.renderRows(r.lines || []);
      this.setStatus(`识别出 ${(r.lines || []).length} 行，文字可能有错别字，核对后再保存`, "warn");
    } catch (e) {
      this.setStatus("识别失败：" + e.message, "error");
    }
    this.setBusy(false);
  },

  shift(delta) {
    document.querySelectorAll("#ls-lines .ls-row").forEach((row) => {
      const inp = row.querySelector(".ls-ts");
      const t = parseTs(inp.value);
      if (isFinite(t)) inp.value = fmtTs(Math.max(0, t + delta));
    });
    this.setStatus(`全部时间已${delta > 0 ? "延后" : "提前"} ${Math.abs(delta)} 秒（记得保存）`, "warn");
  },

  playLine(t) {
    const seek = () => {
      try {
        player.audio.currentTime = Math.max(0, t - 0.15);
        player.audio.play().catch(() => {});
      } catch (e) { /* 音频还没就绪就忽略 */ }
    };
    if (player.current && player.current.path === this.song.path) { seek(); return; }
    player.playSong(this.song.path);
    const onReady = () => { player.audio.removeEventListener("loadedmetadata", onReady); seek(); };
    player.audio.addEventListener("loadedmetadata", onReady);
    setTimeout(() => player.audio.removeEventListener("loadedmetadata", onReady), 8000);
  },

  async save(m) {
    const { lines, bad } = this.collect();
    if (bad.length) { alert(`第 ${bad.join("、")} 行的时间格式不对，请写成 00:12.34`); return; }
    if (!lines.length) { alert("还没有歌词内容"); return; }
    try {
      const r = await api(`/api/songs/${encodeURIComponent(this.song.path)}/lyric`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines, meta: this.meta }),
      });
      this.setStatus(`已保存 ${r.lines} 行 → ${r.saved_as}`, "ok");
      player.refreshLyricsIfCurrent(this.song.path);
      loadSongs();
      LyricHub.refreshIfOpen();
    } catch (e) {
      alert(e.message);
    }
  },
};

/* 站内「缺词列表」：替代原来的独立补词页 */
const LyricHub = {
  open() {
    const el = $("#lyric-hub");
    if (!el) return;
    el.classList.remove("hidden");
    document.body.classList.add("overlay-open");
    this.load();
  },

  close() {
    $("#lyric-hub").classList.add("hidden");
    document.body.classList.remove("overlay-open");
  },

  refreshIfOpen() {
    if ($("#lyric-hub") && !$("#lyric-hub").classList.contains("hidden")) this.load();
  },

  async load() {
    const ul = $("#hub-list");
    const tip = $("#hub-count");
    ul.innerHTML = `<li class="net-loading">加载中…</li>`;
    try {
      const d = await api("/api/lyrfix/needy");
      const songs = d.songs || [];
      tip.textContent = songs.length ? `缺词 ${songs.length} 首` : "";
      $("#hub-empty").hidden = songs.length > 0;
      ul.innerHTML = "";
      songs.forEach((s) => {
        const li = document.createElement("li");
        li.className = "net-song hub-item";
        li.innerHTML = `
          <div class="net-info">
            <div class="net-name">${esc(s.title)}</div>
            <div class="net-artist">${s.has_lrc ? "只有制作信息" : "还没有歌词文件"} · ${esc(s.path)}</div>
          </div>
          <button class="mini-btn">编辑</button>`;
        li.querySelector("button").addEventListener("click", () => {
          const song = state.songs.find((x) => x.path === s.path);
          if (!song) { alert("这首歌已不在列表里，请刷新页面"); return; }
          LyricStudio.open(song);
        });
        ul.appendChild(li);
      });
    } catch (e) {
      ul.innerHTML = "";
      tip.textContent = "加载失败：" + e.message;
    }
  },
};

/* 一键补词：批量跑「网站有词」的歌（不跑 ASR，纯音乐自动跳过） */
function batchLyrfix() {
  api("/api/lyrfix/batch", { method: "POST" })
    .then((r) => {
      if (r.total === 0) { alert(r.msg || "没有需要补词的歌（或都已处理）"); return; }
      Modal.open(`
        <h3>批量补词（${r.total} 首）</h3>
        <p id="bf-progress" class="bf-progress">0 / ${r.total}</p>
        <p id="bf-current" class="bf-current"></p>
        <ul id="bf-result" class="bf-list"></ul>
        <div class="modal-actions">
          <button class="ghost-btn" data-x="stop">停止</button>
          <button class="primary-btn" data-x="close" hidden>关闭</button>
        </div>`, (m) => {
        const timer = setInterval(async () => {
          let st;
          try { st = await api("/api/lyrfix/status"); } catch { return; }
          m.querySelector("#bf-progress").textContent = `${st.done} / ${st.total}`;
          m.querySelector("#bf-current").textContent = st.current || "";
          if (st.running) return;
          clearInterval(timer);
          const icon = { updated: "✓", skipped: "–", error: "✗" };
          m.querySelector("#bf-result").innerHTML = (st.results || []).map((x) =>
            `<li>${icon[x.action] || "?"} ${esc(x.song)}${x.reason ? ` · ${esc(x.reason)}`
              : x.lines ? ` · ${x.lines} 行` : ""}</li>`).join("");
          m.querySelector("[data-x=stop]").hidden = true;
          const closeBtn = m.querySelector("[data-x=close]");
          closeBtn.hidden = false;
          closeBtn.onclick = () => { Modal.close(); loadSongs(); LyricHub.refreshIfOpen(); };
          loadSongs();
        }, 2000);
        m.querySelector("[data-x=stop]").onclick = async () => {
          try { await api("/api/lyrfix/stop", { method: "POST" }); } catch { /* 已结束也无所谓 */ }
        };
      });
    })
    .catch((e) => alert(e.message));
}

function editDelete(song) {
  if (!confirm(`确定删除「${songTitle(song)}」？\n文件将从优盘中移除，无法恢复！`)) return;
  api(`/api/songs/${encodeURIComponent(song.path)}`, { method: "DELETE" })
    .then(() => loadSongs())
    .catch((e) => alert(e.message));
}

/* ==================== 底部功能卡片（可扩展） ==================== */
/* 新功能只需往这里 push 一张卡片定义即可，不需要改其他代码 */
const PANELS = [
  {
    id: "upload",
    title: "上传音乐",
    desc: "从电脑通过局域网上传歌曲到优盘",
    actions: [{ label: "打开上传页", primary: true, onClick: () => window.open("/upload") }],
  },
  {
    id: "lyrfix",
    title: "歌词工坊",
    desc: "缺词清单：自动补词、逐行手动修改",
    actions: [{ label: "打开工坊", primary: true, onClick: () => LyricHub.open() }],
  },
  {
    id: "usb",
    title: "优盘管理",
    desc: "挂载 / 安全弹出优盘，保护数据",
    renderUsb: true,
  },
];

function renderPanels() {
  const root = $("#panels");
  root.innerHTML = "";
  PANELS.forEach((p) => {
    const card = document.createElement("div");
    card.className = "panel-card";
    card.id = `panel-${p.id}`;
    card.innerHTML = `<h3>${esc(p.title)}</h3><p>${esc(p.desc)}</p>`;
    if (p.renderUsb) {
      const actions = document.createElement("div");
      actions.className = "usb-area";
      actions.innerHTML = `<p style="margin:0">加载中…</p>`;
      card.appendChild(actions);
      refreshUsbPanel(actions);
    } else {
      const actions = document.createElement("div");
      actions.className = "panel-actions";
      p.actions.forEach((a) => {
        const btn = document.createElement("button");
        btn.className = a.primary ? "primary-btn" : "ghost-btn";
        btn.textContent = a.label;
        btn.onclick = a.onClick;
        actions.appendChild(btn);
      });
      card.appendChild(actions);
    }
    root.appendChild(card);
  });
}

async function refreshUsbPanel(area, force = false) {
  try {
    const data = await api("/api/usb/status");
    // 状态没变化就不重建 DOM，避免定时刷新打断正在点击的按钮
    const sig = JSON.stringify(data);
    if (!force && area.dataset.sig === sig) return;
    area.dataset.sig = sig;
    const parts = data.partitions;
    if (!parts.length) {
      area.innerHTML = `<p style="margin:0">没有检测到可移动磁盘。插入优盘后点击刷新。</p>`;
    } else {
      area.innerHTML = parts.map((p) => {
        const badge = p.protected ? `<span class="badge lock">其他项目占用</span>`
          : p.mounted_here ? `<span class="badge ok">已挂载</span>`
          : p.mountpoints.length ? `<span class="badge off">已挂载别处</span>`
          : `<span class="badge off">未挂载</span>`;
        const btn = p.can_mount
          ? `<button class="mini-btn" data-mount="${esc(p.dev)}">挂载</button>`
          : p.mounted_here ? `<button class="mini-btn danger" data-eject="1">安全弹出</button>` : "";
        // 剩余容量：只在挂载到 /mnt/music 的分区上可读（独立整行，避免挤压）
        const usageHtml = p.usage ? (() => {
          const pct = Math.round(p.usage.used / p.usage.total * 100);
          return `<div class="usb-usage">
            <span class="usb-bar"><i style="width:${pct}%"></i></span>
            剩 ${fmtSize(p.usage.free)} / 共 ${fmtSize(p.usage.total)}
          </div>`;
        })() : "";
        return `<div class="usb-dev">
          <div class="usb-dev-line"><b>${esc(p.dev)}</b> ${fmtSize(p.size_bytes)} ${esc(p.fstype)}</div>
          <span class="usb-side">${badge} ${btn}</span>
          ${usageHtml}
        </div>`;
      }).join("");
    }
    area.insertAdjacentHTML("beforeend",
      `<div class="panel-actions" style="margin-top:8px">
        <button class="ghost-btn" data-usb-refresh>刷新状态</button>
        <button class="ghost-btn" data-lrc-adopt>配对歌词</button>
        <button class="ghost-btn" data-lrc-clean>清理孤儿歌词</button>
      </div>`);

    area.querySelector("[data-usb-refresh]").onclick = () => refreshUsbPanel(area, true);
    area.querySelector("[data-lrc-adopt]").onclick = adoptOrphanLrc;
    area.querySelector("[data-lrc-clean]").onclick = cleanOrphanLrc;
    area.querySelectorAll("[data-mount]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true; b.textContent = "挂载中…";
        try {
          const r = await api("/api/usb/mount", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dev: b.dataset.mount }),
          });
          alert(r.msg); refreshUsbPanel(area); loadSongs();
        } catch (e) { alert(e.message); refreshUsbPanel(area); }
      };
    });
    const ejectBtn = area.querySelector("[data-eject]");
    if (ejectBtn) ejectBtn.onclick = async () => {
      if (!confirm("确定安全弹出优盘？")) return;
      ejectBtn.disabled = true; ejectBtn.textContent = "弹出中…";
      try {
        const r = await api("/api/usb/eject", { method: "POST" });
        alert(r.msg); refreshUsbPanel(area); loadSongs();
      } catch (e) { alert(e.message); refreshUsbPanel(area); }
    };
  } catch (e) {
    area.innerHTML = `<p style="margin:0">状态获取失败：${esc(e.message)}</p>`;
  }
}

/* 配对歌词：把「歌手-歌名」顺序颠倒等配不上的 .lrc 改名成与音频同名 */
async function adoptOrphanLrc() {
  try {
    const data = await api("/api/lrc/orphans");
    if (!data.adoptable.length) {
      alert(data.orphans.length
        ? `没有可自动配对的歌词（另有 ${data.orphans.length} 个真孤儿，可用「清理孤儿歌词」处理）。`
        : "所有歌词都已正确配对。");
      return;
    }
    const list = data.adoptable.map((p) => `${p.lrc}  →  ${p.audio}`).join("\n");
    if (!confirm(`发现 ${data.adoptable.length} 个歌词文件与音频命名不一致，将自动改名配对：\n\n${list}\n\n确定执行？`)) return;
    const r = await api("/api/lrc/adopt", { method: "POST" });
    alert(`已配对 ${r.count} 个歌词文件，这些歌现在可以显示歌词了。`);
    loadSongs();
  } catch (e) { alert(e.message); }
}

/* 清理孤儿歌词：真孤儿 + 与正版内容完全相同的重复副本才会删，内容不同的副本保留 */
async function cleanOrphanLrc() {
  try {
    const data = await api("/api/lrc/orphans");
    if (data.adoptable.length) {
      alert(`发现 ${data.adoptable.length} 个只是命名不一致的歌词（歌还在），\n请先用「配对歌词」处理，避免误删。`);
      return;
    }
    const dupSame = (data.duplicates || []).filter((d) => d.same_content);
    const dupDiff = (data.duplicates || []).filter((d) => !d.same_content);
    if (!data.orphans.length && !dupSame.length) {
      alert(dupDiff.length
        ? `没有可清理项。另有 ${dupDiff.length} 个重复副本内容与正主不同，已保留。`
        : "很干净，没有发现孤儿歌词。");
      return;
    }
    const lines = [
      ...dupSame.map((d) => `[重复副本] ${d.path}`),
      ...data.orphans.map((o) => `[无音频] ${o.path}`),
    ];
    const preview = lines.length > 12 ? lines.slice(0, 12).join("\n") + `\n…等共 ${lines.length} 个` : lines.join("\n");
    if (!confirm(`以下歌词将被删除（内容不同的重复副本会保留）：\n\n${preview}\n\n确定？`)) return;
    const r = await api("/api/lrc/cleanup", { method: "POST" });
    const extra = (r.kept_diff && r.kept_diff.length)
      ? `\n另有 ${r.kept_diff.length} 个内容不同的重复副本已保留。` : "";
    alert(`已删除 ${r.count} 个文件。${extra}`);
    loadSongs();
  } catch (e) { alert(e.message); }
}

/* 优盘卡片定时自动刷新：插拔优盘无需手动刷新页面 */
let usbRefreshing = false;
setInterval(() => {
  if (usbRefreshing) return;
  const area = document.querySelector("#panel-usb .usb-area");
  if (!area) return;
  usbRefreshing = true;
  Promise.resolve(refreshUsbPanel(area)).finally(() => { usbRefreshing = false; });
}, 20000);


/* ==================== 背景设置 ==================== */

/* 播放页全屏：桌面走标准 Fullscreen API，iPad Safari 走 webkit 前缀 */
function toggleFullscreen() {
  const el = document.getElementById("player");
  const doc = document;
  const current = doc.fullscreenElement || doc.webkitFullscreenElement;
  if (current) {
    (doc.exitFullscreen || doc.webkitExitFullscreen).call(doc);
    return;
  }
  const req = el.requestFullscreen
    ? () => el.requestFullscreen()
    : el.webkitRequestFullscreen
      ? () => el.webkitRequestFullscreen()
      : null;
  if (!req) { alert("此浏览器不支持网页内全屏，请更新系统或换用 Safari/Chrome。"); return; }
  const p = req.call(el);
  if (p && p.catch) p.catch(() => alert("浏览器拒绝了全屏请求，请再试一次。"));
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.bg = data.bg;
  document.documentElement.style.setProperty("--bg-image", `url("${state.bg}")`);
  document.documentElement.style.setProperty("--home-bg-image", `url("${data.home_bg}")`);
}

function openBgSettings() {
  Modal.open(`
    <h3>背景设置</h3>
    <div class="row">
      <select id="bg-target" class="sort-select" style="flex:0 0 auto">
        <option value="player">播放页</option>
        <option value="home">主页</option>
      </select>
      <input type="text" id="bg-url" placeholder="粘贴图片 URL">
      <button class="primary-btn" data-x="url">用 URL</button>
    </div>
    <div class="row">
      <button class="ghost-btn" data-x="pick">上传本地图</button>
      <button class="ghost-btn" data-x="reset">恢复默认</button>
    </div>
    <input type="file" id="bg-file" accept="image/*" hidden>`, (m) => {
    const fileInput = m.querySelector("#bg-file");
    const targetSel = m.querySelector("#bg-target");
    const apply = (body, isJson) => api("/api/settings/bg", {
      method: "POST",
      ...(isJson ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
                 : { body }),
    }).then((d) => {
      const isHome = targetSel.value === "home";
      if (isHome) {
        document.documentElement.style.setProperty("--home-bg-image", `url("${d.bg}")`);
      } else {
        state.bg = d.bg;
        document.documentElement.style.setProperty("--bg-image", `url("${state.bg}")`);
      }
      Modal.close();
    }).catch((e) => alert(e.message));

    m.querySelector("[data-x=url]").onclick = () => {
      const url = m.querySelector("#bg-url").value.trim();
      if (url) apply({ url, target: targetSel.value }, true);
    };
    bindEnter(m.querySelector("#bg-url"), m.querySelector("[data-x=url]"));
    m.querySelector("[data-x=pick]").onclick = () => fileInput.click();
    fileInput.onchange = () => {
      const f = fileInput.files[0];
      if (f) {
        const fd = new FormData();
        fd.append("file", f);
        fd.append("target", targetSel.value);
        apply(fd, false);
      }
    };
    m.querySelector("[data-x=reset]").onclick = () => apply({ reset: true, target: targetSel.value }, true);
  });
}

/* ==================== 歌词 ==================== */
const Lyrics = {
  lines: [],        // [{time, text}]
  currentIndex: -1,

  parse(lrcText) {
    if (typeof lrcText !== "string") return [];
    // 兼容 JSON 歌词（整体数组 / 每行一个对象）转成标准 LRC 行；
    // 注意同一文件可能是混合的：前半是 JSONL credits，后半是标准 [mm:ss.xx] 歌词，
    // 所以必须逐行判断，不能整文件二选一（否则后半段会被整段丢掉）。
    const chunks = [];
    const pushJson = (obj) => {
      const list = Array.isArray(obj) ? obj : [obj];
      list.forEach((l) => {
        if (!l || typeof l !== "object" || l.t === undefined) return;
        const sec = (Number(l.t) || 0) / 1000;
        const m = Math.floor(sec / 60);
        const s = (sec % 60).toFixed(2).padStart(5, "0");
        chunks.push(`[${m}:${s}]${(l.c || []).map((c) => (c && c.tx) || "").join("")}`);
      });
    };
    const body = lrcText.trim();
    let whole = null;
    if (body.startsWith("[") && body.endsWith("]")) {
      try {
        const j = JSON.parse(body);
        if (Array.isArray(j) && j.some((x) => x && typeof x === "object" && x.t !== undefined)) whole = j;
      } catch { /* 不是整体 JSON，走逐行 */ }
    }
    if (whole) {
      pushJson(whole);
    } else {
      lrcText.split(/\r?\n/).forEach((raw) => {
        const line = raw.trim();
        if (!line) return;
        if (line.startsWith("{")) {
          try { pushJson(JSON.parse(line)); } catch { /* 跳过坏行 */ }
        } else if (line.startsWith("[")) {
          chunks.push(line);
        }
      });
    }
    const out = [];
    const re = /\[(\d{1,2}):(\d{1,2}(?:\.\d{1,3})?)\]/g;
    chunks.join("\n").split(/\r?\n/).forEach((raw) => {
      const text = raw.replace(re, "").trim();
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(raw)) !== null) {
        out.push({ time: Number(m[1]) * 60 + Number(m[2]), text });
      }
    });
    out.sort((a, b) => a.time - b.time);
    return out;
  },

  async loadFor(sid) {
    this.reset();
    try {
      const data = await api(`/api/lyrics/${encodeURIComponent(sid)}`);
      this.lines = data.lyrics ? this.parse(data.lyrics) : [];
    } catch { this.lines = []; }
    this.render();
  },

  render() {
    const box = $("#lyrics");
    box.innerHTML = "";
    if (!this.lines.length) {
      box.innerHTML = `<div class="lyric-line dim">暂无歌词，可在编辑模式中连接字幕</div>`;
      return;
    }
    this.lines.forEach((l) => {
      const d = document.createElement("div");
      d.className = "lyric-line";
      d.textContent = l.text || "…";
      box.appendChild(d);
    });
    this.currentIndex = -1;
  },

  seek(currentTime) {
    if (!this.lines.length) return;
    let idx = -1;
    for (let i = 0; i < this.lines.length; i++) {
      if (this.lines[i].time <= currentTime + 0.2) idx = i; else break;
    }
    if (idx === this.currentIndex) return;
    this.currentIndex = idx;
    const nodes = $("#lyrics").children;
    for (let i = 0; i < nodes.length; i++) nodes[i].classList.toggle("active", i === idx);
    if (idx >= 0) {
      const node = nodes[idx];
      const box = $("#lyrics");
      const offset = node.offsetTop + node.offsetHeight / 2;
      box.style.transform = `translateY(${-offset}px)`;
    }
  },

  reset() {
    this.lines = [];
    this.currentIndex = -1;
    $("#lyrics").style.transform = "translateY(0)";
  },
};

/* ==================== 播放器 ==================== */
const player = {
  audio: $("#audio"),
  queue: [],
  qIndex: -1,
  current: null,
  loopOne: false,     // 单曲循环开关

  /* 加权随机队列：评级即重复次数（未评级按 2 次），喜欢的自然多放几遍 */
  buildQueue() {
    const entries = [];
    state.songs.forEach((s, i) => {
      const w = Math.min(5, Math.max(1, s.rating || 2));
      for (let k = 0; k < w; k++) entries.push(i);
    });
    for (let i = entries.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [entries[i], entries[j]] = [entries[j], entries[i]];
    }
    return entries;
  },

  playSong(sid) {
    const song = state.songs.find((s) => s.path === sid);
    if (!song) return;
    // 全量加权队列：所选歌曲排到队首，其余重复次数保留（实现「喜欢的多放几遍」）
    this.queue = this.buildQueue();
    const firstIdx = this.queue.findIndex((i) => state.songs[i].path === sid);
    if (firstIdx > 0) {
      const [item] = this.queue.splice(firstIdx, 1);
      this.queue.unshift(item);
    }
    this.qIndex = 0;
    this.load(song);
  },

  load(song) {
    this.current = song;
    this.audio.src = `/media/${song.path.split("/").map(encodeURIComponent).join("/")}`;
    this.audio.play().catch(() => {});
    setPlayerTitle(songTitle(song));
    const stars = "★".repeat(Math.max(1, song.rating || 0)) + "☆".repeat(5 - Math.max(1, song.rating || 0));
    $("#pl-rating").textContent = stars;
    // 封面
    const img = $("#disc-img"), fallback = $("#disc-fallback");
    if (song.cover) {
      img.src = song.cover; img.classList.remove("hidden"); fallback.classList.add("hidden");
      img.onerror = () => { img.classList.add("hidden"); fallback.classList.remove("hidden"); };
    } else {
      img.classList.add("hidden"); fallback.classList.remove("hidden");
    }
    Lyrics.loadFor(song.path);
    document.querySelectorAll(".song-item").forEach((li) =>
      li.classList.toggle("playing", li.dataset.sid === song.path));
    NowBar.sync();
  },

  next(opts = {}) {
    // 自动切歌（ended/看门狗）带 1.5s 防抖，避免双路径触发连跳两首；手动点击不受限
    if (opts.auto) {
      const now = Date.now();
      if (now - this._lastAuto < 1500) return;
      this._lastAuto = now;
    }
    if (!state.songs.length) return;
    // 单曲循环：自动切歌（播完/看门狗）时重播当前曲目；手动 ⏭ 不受影响
    if (opts.auto && this.loopOne && this.current) { this.load(this.current); return; }
    if (!this.queue.length) this.queue = this.buildQueue();
    this.qIndex += 1;
    if (this.qIndex >= this.queue.length) {          // 队列播完，重新洗牌
      this.queue = this.buildQueue();
      this.qIndex = 0;
    }
    this.load(state.songs[this.queue[this.qIndex]]);
  },

  prev() {
    if (!this.queue.length) return;
    this.qIndex = Math.max(0, this.qIndex - 1);
    this.load(state.songs[this.queue[this.qIndex]]);
  },

  togglePlay() {
    if (!this.current) {                       // 什么都没播就从随机开始
      if (state.songs.length) this.next();
      return;
    }
    if (this.audio.paused) this.audio.play().catch(() => {});
    else this.audio.pause();
  },

  refreshLyricsIfCurrent(sid) {
    if (this.current && this.current.path === sid) Lyrics.loadFor(sid);
  },
};

/* 标题字体：纯英文/数字 → 花体；只要含中文（含中英混杂）→ 创粗黑 */
const CJK_RE = /[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff]/;
function setPlayerTitle(title) {
  const el = $("#pl-title");
  el.textContent = title;
  el.classList.toggle("font-en", !CJK_RE.test(title));
  el.classList.toggle("font-cn", CJK_RE.test(title));
}

/* 常驻迷你控制条：返回列表后也能暂停/切歌/回到沉浸页 */
const NowBar = {
  sync() {
    const bar = $("#nowbar");
    const overlayHidden = $("#player").classList.contains("hidden");
    const visible = !!player.current && overlayHidden;
    bar.classList.toggle("hidden", !visible);
    document.body.classList.toggle("has-nowbar", visible);
    if (player.current) $("#nb-title").textContent = songTitle(player.current);
    $("#nb-play").textContent = player.audio.paused ? "▶" : "⏸";
  },
};

function initPlayerUI() {
  let seeking = false;
  const audio = player.audio;

  $("#btn-play").onclick = () => player.togglePlay();
  $("#btn-next").onclick = () => player.next();
  $("#btn-prev").onclick = () => player.prev();
  $("#btn-loop").onclick = () => {
    player.loopOne = !player.loopOne;
    $("#btn-loop").classList.toggle("active", player.loopOne);
  };
  $("#btn-back").onclick = () => {
    $("#player").classList.add("hidden");
    document.body.classList.remove("player-open");
    NowBar.sync();
  };
  $("#btn-bg").onclick = openBgSettings;

  $("#btn-fs").onclick = toggleFullscreen;
  ["fullscreenchange", "webkitfullscreenchange"].forEach((ev) =>
    document.addEventListener(ev, () => {
      const fs = document.fullscreenElement || document.webkitFullscreenElement;
      $("#btn-fs").textContent = fs ? "⛌" : "⛶";
    }));

  // 迷你控制条
  $("#nb-play").onclick = () => player.togglePlay();
  $("#nb-next").onclick = () => player.next();
  $("#nb-prev").onclick = () => player.prev();
  $("#nb-expand").onclick = () => {
    $("#player").classList.remove("hidden");
    document.body.classList.add("player-open");
    NowBar.sync();
    if (player.wake) player.wake();
  };

  $("#btn-edit").onclick = () => {
    state.editing = !state.editing;
    document.body.classList.toggle("editing", state.editing);
    $("#btn-edit").classList.toggle("active", state.editing);
    $("#btn-lyrfix-batch").hidden = !state.editing;
    renderList();
  };

  $("#btn-lyrfix-batch").onclick = batchLyrfix;

  $("#hub-close").onclick = () => LyricHub.close();
  $("#hub-batch").onclick = batchLyrfix;

  // 排序：记忆上次选择
  const sortSel = $("#sort-select");
  sortSel.value = state.sort;
  sortSel.onchange = () => {
    state.sort = sortSel.value;
    localStorage.setItem("mp_sort", state.sort);
    renderList();
  };

  audio.addEventListener("play", () => {
    $("#btn-play").textContent = "⏸";
    $("#disc").classList.add("spinning");
    NowBar.sync();
  });
  audio.addEventListener("pause", () => {
    $("#btn-play").textContent = "▶";
    $("#disc").classList.remove("spinning");
    NowBar.sync();
  });
  audio.addEventListener("ended", () => player.next({ auto: true }));
  audio.addEventListener("timeupdate", () => {
    if (!seeking) {
      $("#progress").value = audio.currentTime / (audio.duration || 1) * 1000;
      $("#time-cur").textContent = fmtTime(audio.currentTime);
    }
    Lyrics.seek(audio.currentTime);
    // 看门狗：个别环境 ended 事件不可靠，接近末尾时主动切歌
    if (audio.duration > 0 && audio.currentTime >= audio.duration - 0.2 && !audio.paused) {
      player.next({ auto: true });
    }
  });
  // 媒体加载出错时自动跳下一首（连续失败 3 次为止，避免优盘拔出后死循环）
  let errStreak = 0;
  audio.addEventListener("error", () => {
    if (!player.current) return;
    if (++errStreak <= 3) setTimeout(() => player.next(), 300);
  });
  audio.addEventListener("playing", () => { errStreak = 0; });
  audio.addEventListener("loadedmetadata", () => {
    $("#time-total").textContent = fmtTime(audio.duration);
  });

  const bar = $("#progress");
  bar.addEventListener("input", () => { seeking = true; });
  bar.addEventListener("change", () => {
    if (audio.duration) audio.currentTime = bar.value / 1000 * audio.duration;
    seeking = false;
  });

  // 免打扰：播放页 5 秒无操作后淡出上下控制区，任意操作唤醒
  const playerEl = $("#player");
  let zenTimer = null;
  const wake = () => {
    playerEl.classList.remove("zen");
    clearTimeout(zenTimer);
    zenTimer = setTimeout(() => playerEl.classList.add("zen"), 5000);
  };
  ["mousemove", "mousedown", "touchstart", "keydown"].forEach((ev) =>
    playerEl.addEventListener(ev, wake));
  player.wake = wake;   // 打开播放页时调用，重新计时

  // 打开播放页：点歌曲列表任意歌时展示沉浸页
  document.getElementById("song-list").addEventListener("click", (e) => {
    if (e.target.closest(".song-edit") || state.editing) return;
    if (e.target.closest(".song-item")) {
      $("#player").classList.remove("hidden");
      document.body.classList.add("player-open");
      NowBar.sync();
      wake();
    }
  });
}

/* ==================== 启动 ==================== */
initPlayerUI();
renderPanels();
loadSettings();
loadSongs();
