/**
 * 注入到 WorkBuddy 账号菜单的「切换账号」入口。
 *
 * 位置：账号菜单底部，与「退出登录」相邻；优先复用原生「切换账号」节点，
 * 保证字体、图标、hover 与客户端完全一致。
 * 交互：点开是一个贴着菜单弹出的账号浮层，点账号即切换（客户端会重启）。
 *
 * 约束：只操作 DOM，不读取页面里的任何令牌，不修改客户端文件。
 */
(() => {
  const SIG = window.__WBS_SIG__ || "";
  if (window.__WBS_PANEL__ && window.__WBS_PANEL__.sig === SIG) return;
  // 重新注入时先清掉旧版本的 DOM，避免旧按钮和旧浮层残留。
  try {
    document.getElementById("wbs-entry-btn")?.remove();
    document.getElementById("wbs-style")?.remove();
    document.querySelectorAll(".wbs-layer").forEach((node) => node.remove());
  } catch {
    /* ignore */
  }

  const CFG = Object.assign({ api: "", token: "" }, window.__WBS_CFG__ || {});
  const LOGOUT_TEXTS = ["退出登录", "退出登陆", "登出", "Sign out", "Log out"];
  const SWITCH_TEXTS = ["切换账号", "切换帐号", "Switch account", "Switch Account"];
  const BTN_ID = "wbs-entry-btn";
  const LOGIN_BTN_ID = "wbs-login-switch-btn";
  const PENDING_KEY = "wbs.pendingSwitch";
  const SEEN_KEY = "wbs.seenSwitchOps";
  const PENDING_MAX_AGE_MS = 5 * 60 * 1000;
  const RESULT_MAX_AGE_MS = 10 * 60 * 1000;
  const state = { profiles: [], runningId: "", busy: false, managing: false, pendingDelete: null };

  function newOpId() {
    try {
      if (window.crypto && crypto.randomUUID) return "op-" + crypto.randomUUID();
    } catch {
      /* ignore */
    }
    return "op-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function readPending() {
    try {
      const raw = localStorage.getItem(PENDING_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      return data && data.opId ? data : null;
    } catch {
      return null;
    }
  }

  function writePending(pending) {
    try {
      localStorage.setItem(PENDING_KEY, JSON.stringify(pending));
    } catch {
      /* ignore */
    }
  }

  function clearPending() {
    try {
      localStorage.removeItem(PENDING_KEY);
    } catch {
      /* ignore */
    }
  }

  // 已展示过的操作 ID。localStorage 按档案隔离，所以这只用于“同一档案
  // 不重复弹结果”；跨档案由后台 lastOperation 兜底，见 restoreLastResult。
  function readSeenOps() {
    try {
      const arr = JSON.parse(localStorage.getItem(SEEN_KEY) || "[]");
      return Array.isArray(arr) ? arr : [];
    } catch {
      return [];
    }
  }

  function markSeenOp(opId) {
    if (!opId) return;
    try {
      const arr = readSeenOps().filter((id) => id !== opId);
      arr.push(opId);
      localStorage.setItem(SEEN_KEY, JSON.stringify(arr.slice(-10)));
    } catch {
      /* ignore */
    }
  }

  function isSeenOp(opId) {
    return !!opId && readSeenOps().indexOf(opId) >= 0;
  }

  const SWAP_ICON =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h12l-3-3"/><path d="M20 16H8l3 3"/></svg>';

  const CSS = `
  #${BTN_ID}{position:relative}
  .wbs-layer{position:fixed;inset:0;z-index:2147483000;font-family:inherit;-webkit-font-smoothing:antialiased}
  .wbs-card{position:fixed;display:flex;flex-direction:column;background:#fff;border-radius:14px;
    border:1px solid #EBEDF3;box-shadow:0 18px 50px rgba(15,20,30,.18),0 2px 8px rgba(15,20,30,.06);
    overflow:hidden;animation:wbsIn .14s ease;font-size:13px;color:#1B1F27}
  @keyframes wbsIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
  .wbs-head{display:flex;align-items:flex-start;justify-content:space-between;padding:13px 14px 6px}
  .wbs-title{font-size:13.5px;font-weight:600;line-height:1.3}
  .wbs-sub{font-size:11px;color:#9299A6;margin-top:3px}
  .wbs-x{border:0;background:none;font-size:16px;line-height:1;color:#A2A9B5;cursor:pointer;padding:2px 4px;border-radius:6px}
  .wbs-x:hover{color:#1B1F27;background:#F2F4F8}
  .wbs-list{overflow:auto;padding:2px 7px 6px;max-height:44vh}
  .wbs-item{display:flex;align-items:center;gap:9px;padding:7px 8px;border-radius:9px;cursor:pointer;border:1px solid transparent}
  .wbs-item:hover{background:#F6F8FC;border-color:#EDEFF5}
  .wbs-item.is-running{cursor:default}
  .wbs-item.is-running:hover{background:transparent;border-color:transparent}
  .wbs-avatar{width:28px;height:28px;border-radius:8px;flex:none;display:grid;place-items:center;
    font-size:12px;font-weight:600;color:#fff;overflow:hidden}
  .wbs-avatar-img{width:100%;height:100%;object-fit:cover;display:block}
  .wbs-info{min-width:0;flex:1}
  .wbs-name{font-size:12.5px;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .wbs-meta{font-size:10.5px;color:#9299A6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
  .wbs-tag{font-size:9.5px;padding:2px 6px;border-radius:5px;background:#E9F7F1;color:#0E9F6E;flex:none}
  .wbs-tag.off{background:#F2F4F8;color:#98A0AC}
  .wbs-del{border:0;background:#FDEEEE;color:#D93A3A;font-size:10.5px;cursor:pointer;padding:4px 8px;border-radius:6px;flex:none}
  .wbs-del:hover{background:#FADDDD}
  .wbs-del.confirm{color:#fff;background:#D93A3A;font-size:10px;padding:4px 7px}
  .wbs-item.is-manage{cursor:default}
  .wbs-foot{padding:7px 9px 9px;border-top:1px solid #F2F4F8;display:flex;align-items:center;justify-content:space-between}
  .wbs-add{border:0;background:none;color:#2B6CF6;font-size:12px;cursor:pointer;padding:4px 6px;border-radius:6px}
  .wbs-add:hover{background:#EDF2FE}
  .wbs-manage{border:0;background:none;color:#8B929E;font-size:11px;cursor:pointer;padding:4px 6px;border-radius:6px}
  .wbs-manage:hover{color:#2B6CF6;background:#EDF2FE}
  .wbs-hint{font-size:10px;color:#A2A9B5}
  .wbs-empty{padding:16px 10px;text-align:center;font-size:11.5px;color:#9299A6}
  .wbs-row{display:flex;gap:6px;padding:2px 9px 10px}
  .wbs-input{flex:1;height:28px;border:1px solid #DDE1EA;border-radius:7px;padding:0 8px;font-size:12px;
    outline:none;color:#1B1F27;background:#fff}
  .wbs-input:focus{border-color:#2B6CF6;box-shadow:0 0 0 3px #EDF2FE}
  .wbs-ok{height:28px;padding:0 11px;border:0;border-radius:7px;background:#2B6CF6;color:#fff;font-size:12px;cursor:pointer}
  .wbs-ok:disabled{opacity:.5;cursor:not-allowed}
  .wbs-busy{display:flex;align-items:center;gap:8px;padding:18px 14px;font-size:12.5px}
  .wbs-busy .b{width:13px;height:13px;border:2px solid #E1E5EE;border-top-color:#2B6CF6;border-radius:50%;
    animation:wbsSpin .8s linear infinite;flex:none}
  @keyframes wbsSpin{to{transform:rotate(360deg)}}
  .wbs-note{padding:0 14px 12px;font-size:10.5px;color:#9299A6;line-height:1.5}
  .wbs-err{color:#B42626;font-size:11px;padding:0 14px 10px;line-height:1.5}
  .wbs-result-note{padding:2px 14px 12px;font-size:10.5px;color:#9299A6;line-height:1.5;word-break:break-all}
  .wbs-force{margin:0 14px 12px;height:28px;padding:0 12px;border:0;border-radius:7px;
    background:#D93A3A;color:#fff;font-size:12px;cursor:pointer;flex:none}
  .wbs-force:hover{background:#C03030}
  .wbs-title.ok{color:#0E9F6E}
  .wbs-title.bad{color:#B42626}
  .wbs-login-switch{border:0;background:none;color:#2B6CF6;font:inherit;font-size:12px;
    cursor:pointer;padding:2px 4px;border-radius:5px;display:inline-flex;align-items:center;gap:4px}
  .wbs-login-switch:hover{background:#EDF2FE}
  .wbs-layer.is-toast{inset:auto 0 0 auto;width:auto;pointer-events:none;padding:14px}
  .wbs-layer.is-toast .wbs-card{position:relative;left:auto;bottom:auto;
    box-shadow:0 12px 36px rgba(15,20,30,.22),0 2px 8px rgba(15,20,30,.08)}
  `;

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  function injectCss() {
    // 每次都覆盖，避免旧版本样式残留
    let style = document.getElementById("wbs-style");
    if (!style) {
      style = document.createElement("style");
      style.id = "wbs-style";
      (document.head || document.documentElement).appendChild(style);
    }
    if (style.textContent !== CSS) style.textContent = CSS;
  }

  // ------------------------------------------------------------- 定位入口
  function isVisible(node) {
    if (!node || !node.isConnected) return false;
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const style = getComputedStyle(node);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  }

  function findByText(texts) {
    const wanted = texts.map((t) => t.toLowerCase());
    const nodes = document.querySelectorAll("button,div,span,a,li,p");
    const hits = [];
    for (const node of nodes) {
      if (node.children.length > 0) continue;
      const text = (node.textContent || "").trim().toLowerCase();
      if (!text || !wanted.includes(text)) continue;
      if (node.closest("[class*='cr-tool'], .wbs-layer, #wbs-entry-btn, #wbs-login-switch-btn")) continue;
      const clickable = node.closest('button,[role="button"],a,[tabindex],.user-menu-item') || node;
      if (!isVisible(clickable)) continue;
      hits.push(clickable);
    }
    return hits;
  }

  function pickAnchor() {
    const switchHits = findByText(SWITCH_TEXTS);
    if (switchHits.length) return { node: switchHits[switchHits.length - 1], native: true };
    const logoutHits = findByText(LOGOUT_TEXTS);
    if (logoutHits.length) {
      const logout = logoutHits[logoutHits.length - 1];
      if (logout.parentElement) return { node: logout, native: false };
    }
    return null;
  }

  function decorate(source, native) {
    const btn = source.cloneNode(true);
    btn.id = BTN_ID;
    btn.setAttribute("data-wbs", "1");
    if (!native) {
      // 以「退出登录」为模板时，按原生结构重建图标 + 文案，并去掉退出项的红色样式
      btn.classList.remove("user-menu-item--logout");
      btn.innerHTML = "";
      const icon = el("span", "user-menu-item-icon");
      icon.innerHTML = SWAP_ICON;
      btn.appendChild(icon);
      btn.appendChild(el("span", "user-menu-item-label", "切换账号"));
    }
    btn.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        openPicker(btn.getBoundingClientRect());
      },
      true
    );
    return btn;
  }

  function ensureLoginFallback() {
    const view = document.querySelector(".login-view");
    if (!view) {
      document.getElementById(LOGIN_BTN_ID)?.remove();
      return false;
    }
    let btn = document.getElementById(LOGIN_BTN_ID);
    if (btn && btn.isConnected) {
      if (btn.dataset.wbsSig === SIG) return true;
      btn.remove();
    }
    btn = el("button", "wbs-login-switch");
    btn.id = LOGIN_BTN_ID;
    btn.type = "button";
    btn.dataset.wbsSig = SIG;
    btn.innerHTML = SWAP_ICON + "<span>切换账号</span>";
    btn.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        openPicker(btn.getBoundingClientRect());
      },
      true
    );
    const footer = view.querySelector(".login-view__footer");
    if (footer) footer.insertBefore(btn, footer.firstChild);
    else view.appendChild(btn);
    return true;
  }

  function ensure() {
    if (!document.body) return false;
    injectCss();
    const existing = document.getElementById(BTN_ID);
    if (existing && existing.isConnected) {
      if (existing.dataset.wbsSig === SIG) return isVisible(existing);
      existing.remove();
    }
    const anchor = pickAnchor();
    if (!anchor) {
      ensureLoginFallback();
      return false;
    }
    document.getElementById(LOGIN_BTN_ID)?.remove();
    const btn = decorate(anchor.node, anchor.native);
    btn.dataset.wbsSig = SIG;
    if (anchor.native) anchor.node.replaceWith(btn);
    else anchor.node.parentElement.insertBefore(btn, anchor.node);
    return true;
  }

  // ------------------------------------------------------------- 网络
  function apiUrl(path) {
    const base = (CFG.api || "").replace(/\/$/, "");
    const join = path.includes("?") ? "&" : "?";
    return `${base}${path}${join}t=${encodeURIComponent(CFG.token)}`;
  }

  async function call(path, body) {
    let res;
    try {
      res = await fetch(apiUrl(path), {
        method: "POST",
        headers: { "X-WBS-Token": CFG.token, "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      throw new Error("后台服务未运行：请双击「安装.bat」后重试");
    }
    let data = {};
    try {
      data = await res.json();
    } catch {
      throw new Error("服务响应异常");
    }
    if (!res.ok || data.ok === false) throw new Error(data.error || "操作失败");
    return data;
  }

  async function pull() {
    try {
      const res = await fetch(apiUrl("/api/state?sizes=0"), { headers: { "X-WBS-Token": CFG.token } });
      const data = await res.json();
      if (data && data.ok !== false && Array.isArray(data.profiles)) {
        state.profiles = data.profiles.map((p) => ({
          id: p.id,
          name: p.name,
          color: p.color,
          running: !!p.running,
          isDefault: !!p.isDefault,
          account: p.account,
          hasAvatar: !!p.hasAvatar,
          avatarVersion: p.avatarVersion || "",
        }));
        state.runningId = (data.app && data.app.profileId) || "";
      }
    } catch {
      /* 宿主会定期推送，拉不到就沿用 */
    }
  }

  // ------------------------------------------------------------- 浮层
  function closePopover() {
    const layer = document.querySelector(".wbs-layer");
    if (layer) layer.remove();
    state.pendingDelete = null;
    document.removeEventListener("keydown", onKey, true);
  }

  function onKey(event) {
    if (event.key === "Escape") closePopover();
  }

  function showCard(card, anchorRect, toast = false) {
    closePopover();
    const layer = el("div", "wbs-layer" + (toast ? " is-toast" : ""));
    if (!toast) {
      layer.addEventListener("mousedown", (event) => {
        if (!card.contains(event.target)) closePopover();
      });
    }
    layer.appendChild(card);
    document.body.appendChild(layer);
    document.addEventListener("keydown", onKey, true);

    const width = Math.min(330, window.innerWidth - 24);
    card.style.width = width + "px";
    if (toast) {
      // 重启后的结果提示是非阻塞的：不铺满全屏，也不拦截页面点击。
      return card;
    }
    card.style.left = "12px";
    card.style.bottom = "12px";

    // 入口在点击瞬间可能已被客户端收起，所以优先用点击时记下的位置
    let rect = anchorRect;
    if (!rect || !rect.width) {
      const anchor =
        document.querySelector(".user-menu") ||
        document.querySelector(".user-menu-item--logout") ||
        document.getElementById(BTN_ID);
      rect = anchor ? anchor.getBoundingClientRect() : null;
    }
    const height = card.getBoundingClientRect().height || 240;
    const viewportH = window.innerHeight;
    const viewportW = window.innerWidth;
    if (rect && rect.width) {
      let left = rect.right + 12;
      let bottom = viewportH - rect.bottom - 4;
      if (left + width > viewportW - 12) {
        left = Math.max(12, Math.min(rect.left, viewportW - width - 12));
        bottom = viewportH - rect.top + 8;
      }
      if (bottom + height > viewportH - 12) bottom = Math.max(12, viewportH - height - 12);
      if (bottom < 12) bottom = 12;
      card.style.left = Math.round(left) + "px";
      card.style.bottom = Math.round(bottom) + "px";
    }
    return card;
  }

  function buildCard() {
    const card = el("div", "wbs-card");
    const head = el("div", "wbs-head");
    const box = el("div");
    box.appendChild(el("div", "wbs-title", state.managing ? "管理账号" : "切换账号"));
    box.appendChild(
      el(
        "div",
        "wbs-sub",
        state.managing ? "删除只移出列表，不删除客户端数据" : "每个账号独立登录态，切换后自动重启"
      )
    );
    const close = el("button", "wbs-x", "×");
    close.onclick = closePopover;
    head.appendChild(box);
    head.appendChild(close);
    card.appendChild(head);

    const list = el("div", "wbs-list");
    if (!state.profiles.length) {
      list.appendChild(el("div", "wbs-empty", "还没有账号，点下面「添加账号」"));
    }
    for (const profile of state.profiles) {
      list.appendChild(buildItem(profile, state.managing));
    }
    card.appendChild(list);

    const foot = el("div", "wbs-foot");
    if (state.managing) {
      const done = el("button", "wbs-add", "完成");
      done.onclick = () => {
        state.managing = false;
        state.pendingDelete = null;
        render();
      };
      foot.appendChild(done);
      foot.appendChild(el("span", "wbs-hint", "删除只移出列表"));
    } else {
      const add = el("button", "wbs-add", "＋ 添加账号");
      add.onclick = async () => {
        if (add.disabled || state.busy) return;
        add.disabled = true;
        try {
          await call("/api/profiles", { name: `账号${state.profiles.length + 1}` });
          await pull();
          render();
        } catch (err) {
          add.disabled = false;
          showError(err.message);
        }
      };
      const manage = el("button", "wbs-manage", "管理");
      manage.onclick = () => {
        state.managing = true;
        state.pendingDelete = null;
        render();
      };
      foot.appendChild(add);
      foot.appendChild(manage);
    }
    card.appendChild(foot);
    return card;
  }

  function buildItem(profile, managing = false) {
    const item = el(
      "div",
      "wbs-item" + (profile.running ? " is-running" : "") + (managing ? " is-manage" : "")
    );
    const account = profile.account || null;
    const displayName = (account && account.nickname) || "未登录账号";
    const initial = account ? (displayName || "?").trim().slice(0, 1).toUpperCase() : "?";
    const avatar = el("div", "wbs-avatar");
    avatar.style.background = profile.color || "#2B6CF6";
    avatar.textContent = initial;
    if (profile.hasAvatar && account) {
      const img = el("img", "wbs-avatar-img");
      img.alt = displayName;
      img.src = apiUrl(
        `/api/avatar/${encodeURIComponent(profile.id)}?v=${encodeURIComponent(profile.avatarVersion || "0")}`
      );
      img.addEventListener(
        "error",
        () => {
          img.remove();
          avatar.textContent = initial;
        },
        { once: true }
      );
      avatar.textContent = "";
      avatar.appendChild(img);
    }
    const info = el("div", "wbs-info");
    info.appendChild(el("div", "wbs-name", displayName));
    let meta;
    if (profile.running) {
      meta = "当前运行中";
    } else if (account) {
      meta = account.type === "enterprise" ? "企业账号" : "个人账号";
    } else {
      meta = "点击后登录";
    }
    info.appendChild(el("div", "wbs-meta", meta));
    item.appendChild(avatar);
    item.appendChild(info);

    if (managing) {
      if (profile.isDefault) {
        item.appendChild(el("span", "wbs-tag off", "默认"));
      } else if (profile.running) {
        item.appendChild(el("span", "wbs-tag", "使用中"));
      } else {
        const del = el("button", "wbs-del", "删除");
        del.onclick = async (event) => {
          event.stopPropagation();
          if (state.pendingDelete !== profile.id) {
            state.pendingDelete = profile.id;
            del.classList.add("confirm");
            del.textContent = "确认";
            setTimeout(() => {
              if (state.pendingDelete === profile.id) {
                state.pendingDelete = null;
                render();
              }
            }, 3000);
            return;
          }
          try {
            await call(`/api/profiles/${profile.id}/delete`, { purge: false });
            state.pendingDelete = null;
            await pull();
            render();
          } catch (err) {
            showError(err.message);
          }
        };
        item.appendChild(del);
      }
      return item;
    }

    const tag = el(
      "span",
      "wbs-tag" + (profile.running || account ? "" : " off"),
      profile.running ? "运行中" : account && account.pro ? "PRO" : account ? "已登录" : "未登录"
    );
    item.appendChild(tag);
    if (!profile.running) item.onclick = () => doSwitch(profile);
    return item;
  }

  function showError(message) {
    const card = document.querySelector(".wbs-card");
    if (!card) return;
    let box = card.querySelector(".wbs-err");
    if (!box) {
      box = el("div", "wbs-err");
      card.appendChild(box);
    }
    box.textContent = message;
  }

  function doSwitch(profile, force = false) {
    if (state.busy) return;
    state.busy = true;
    const opId = newOpId();
    const label = (profile.account && profile.account.nickname) || "新账号";
    // 操作 ID 先落盘再发请求：页面会随客户端一起被关闭，重启注入后
    // 靠它查回这次切换的最终结果，也保证重复提交不会执行两次。
    writePending({ opId, profileId: profile.id, label, force: !!force, at: Date.now() });

    const card = el("div", "wbs-card");
    const busy = el("div", "wbs-busy");
    busy.appendChild(el("span", "b"));
    busy.appendChild(el("span", null, (force ? "正在强制切换到「" : "正在切换到「") + label + "」"));
    card.appendChild(busy);
    const note = el(
      "div",
      "wbs-note",
      force
        ? "将强制结束当前客户端并重启（可能中断正在进行的任务）。"
        : "正在保存状态并重启客户端，约需数秒。页面会随客户端关闭，重启后自动显示结果。"
    );
    card.appendChild(note);
    showCard(card);

    call("/api/switch", { id: profile.id, opId, force: !!force })
      .then(() => {
        note.textContent = "已受理，等待客户端重启…";
        // 页面若没被关闭（如关闭超时、预检失败），就地轮询并展示结果；
        // 页面随客户端关闭时由重启后的 boot 检查接管。
        checkPendingSwitch(500);
      })
      .catch((err) => {
        // 请求失败有两种可能：后台真没起，或页面正在被关闭。等一小会儿，
        // 页面还活着才说明是真实错误，可以展示并撤销待定操作。
        setTimeout(() => {
          if (!document.querySelector(".wbs-layer")) return;
          clearPending();
          state.busy = false;
          showError(err.message);
        }, 2500);
      });
  }

  // ------------------------------------------------------- 切换结果恢复查询
  function queryOperation(opId) {
    return fetch(apiUrl(`/api/switch-op?id=${encodeURIComponent(opId)}`), {
      headers: { "X-WBS-Token": CFG.token },
    }).then((res) => {
      if (res.status === 404) return null;
      if (!res.ok) throw new Error("查询切换结果失败");
      return res.json().then((data) => (data && data.operation ? data.operation : null));
    });
  }

  async function fetchLatestOperation() {
    try {
      const res = await fetch(apiUrl("/api/switch-op"), { headers: { "X-WBS-Token": CFG.token } });
      if (!res.ok) return null;
      const data = await res.json();
      return data && data.operation ? data.operation : null;
    } catch {
      return null;
    }
  }

  function checkPendingSwitch(delay = 400, toast = false) {
    let timer = null;
    const step = () => {
      const pending = readPending();
      if (!pending) return;
      queryOperation(pending.opId)
        .then((op) => {
          if (!op) {
            // 旧版本可能留下查不到的记录：清掉后改用后台最近一次操作兜底。
            clearPending();
            restoreLastResult().catch(() => {});
            return;
          }
          if (op.state === "running") {
            if (Date.now() - (pending.at || 0) > PENDING_MAX_AGE_MS) {
              clearPending();
              markSeenOp(op.id);
              showResultCard(pending, null, "切换仍在进行或已超时，请打开账号面板确认当前账号。", toast);
              return;
            }
            timer = setTimeout(step, 2000);
            return;
          }
          clearPending();
          markSeenOp(op.id);
          // 成功结果只对“当前就是目标账号”的页面有意义；如果用户后来又
          // 切了别的账号，旧的成功提示会误导，直接跳过。
          if (op.state === "success" && state.runningId && op.profile !== state.runningId) {
            return;
          }
          showResultCard(pending, op, "", toast);
        })
        .catch(() => {
          const again = readPending();
          if (again && again.opId === pending.opId) timer = setTimeout(step, 4000);
        });
    };
    timer = setTimeout(step, delay);
    return () => timer && clearTimeout(timer);
  }

  // 轮询一个操作直到结束；超时返回当前（可能仍是 running 的）记录。
  function watchOperation(opId, maxWaitMs) {
    return new Promise((resolve) => {
      const started = Date.now();
      const tick = () => {
        queryOperation(opId)
          .then((op) => {
            if (!op) return resolve(null);
            if (op.state !== "running") return resolve(op);
            if (Date.now() - started > maxWaitMs) return resolve(op);
            setTimeout(tick, 2000);
          })
          .catch(() => {
            if (Date.now() - started > maxWaitMs) return resolve(null);
            setTimeout(tick, 3000);
          });
      };
      tick();
    });
  }

  // 切换后客户端用的是新档案的独立数据目录，localStorage 读不到旧档案里
  // 记下的 opId。这里先处理本档案可能留下的 pending，再从后台取最近一次
  // 操作；两者都要考虑“操作还在进行”的情况，不能查一次就走。
  async function restoreLastResult() {
    await pull(); // 先拿到当前运行档案，才能判断结果是否属于这个页面
    const handled = new Set();

    const applyResult = (pending, op) => {
      if (!op || op.state === "running") return;
      markSeenOp(op.id);
      handled.add(op.id);
      // 成功结果只对“当前就是目标账号”的页面有意义；如果用户后来又切了
      // 别的账号，旧的成功提示会误导，直接跳过。
      if (op.state === "success" && state.runningId && op.profile !== state.runningId) {
        return;
      }
      showResultCard(pending, op, "", true);
    };

    // 1) 本档案自己的 pending（例如 A→B→A 回到 A 时留下的旧记录）。
    const pending = readPending();
    if (pending) {
      const op = await watchOperation(pending.opId, 60000);
      clearPending();
      if (op) {
        applyResult(
          {
            opId: op.id,
            profileId: op.profile,
            label: pending.label || op.targetName || "",
          },
          op
        );
      }
    }

    // 2) 后台最近一次操作（跨档案恢复的主路径）。即使 pending 不匹配，
    //    也要继续查，否则 A→B→A 这种往返会漏掉返回 A 的结果。
    const latest = await fetchLatestOperation();
    if (!latest || !latest.id || handled.has(latest.id) || isSeenOp(latest.id)) return;
    let op = latest;
    if (op.state === "running") {
      // 页面被注入时切换可能还在收尾，持续查询直到结束。
      op = await watchOperation(op.id, 90000);
      if (!op || op.state === "running") return;
    }
    const finishedAt = Number(op.finishedAt || op.updatedAt || 0);
    if (!finishedAt || Date.now() - finishedAt > RESULT_MAX_AGE_MS) return;
    const relevant = op.profile === state.runningId || op.rolledBack || op.state === "failed";
    if (!relevant) return;
    applyResult(
      { opId: op.id, profileId: op.profile, label: op.targetName || "" },
      op
    );
  }

  function showResultCard(pending, op, fallbackText, toast = false) {
    // 无论结果如何，这条切换已经结束，允许用户再次操作（例如强制切换）。
    state.busy = false;
    const success = op ? op.state === "success" : false;
    const failed = op ? op.state === "failed" : false;
    const label = (pending && pending.label) || (op && op.targetName) || "目标账号";
    const card = el("div", "wbs-card");
    const head = el("div", "wbs-head");
    const box = el("div");
    const title = el(
      "div",
      "wbs-title" + (success ? " ok" : failed ? " bad" : ""),
      success
        ? `已切换到「${label}」`
        : failed
        ? "切换失败"
        : "切换结果"
    );
    box.appendChild(title);
    const lines = [];
    if (op && op.rolledBack) lines.push("已自动恢复原档案");
    if (op && op.error) lines.push(op.error);
    if (fallbackText) lines.push(fallbackText);
    if (lines.length) box.appendChild(el("div", "wbs-sub", lines.join("；")));
    head.appendChild(box);
    const close = el("button", "wbs-x", "×");
    close.onclick = () => closePopover();
    head.appendChild(close);
    card.appendChild(head);

    // 关闭超时是用户可决策的失败：给出主动强制切换的入口。
    if (failed && op && op.error && op.error.indexOf("宽限时间内退出") >= 0) {
      const force = el("button", "wbs-force", "强制切换（可能中断任务）");
      force.onclick = () => {
        closePopover();
        const profile = state.profiles.find((p) => p.id === pending.profileId);
        if (profile) doSwitch(profile, true);
        else
          pull().then(() => {
            const found = state.profiles.find((p) => p.id === pending.profileId);
            if (found) doSwitch(found, true);
          });
      };
      card.appendChild(force);
    }
    showCard(card, null, toast);
    setTimeout(() => {
      const layer = document.querySelector(".wbs-layer");
      if (layer && layer.contains(card)) closePopover();
    }, 12000);
  }

  function render(rect) {
    showCard(buildCard(), rect);
  }

  function openPicker(rect) {
    state.managing = false;
    state.pendingDelete = null;
    render(rect);
    pull().then(() => {
      if (document.querySelector(".wbs-layer")) render(rect);
    });
  }

  // ------------------------------------------------------------- 自愈
  let timer = null;
  function scheduleEnsure(delay = 400) {
    // 已有排队的任务就不再重置，否则频繁的 DOM 变动会把定时器一直往后推
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      try {
        ensure();
      } catch {
        /* 静默 */
      }
    }, delay);
  }

  window.__WBS_PANEL__ = {
    version: 21,
    sig: SIG,
    ensure,
    setProfiles(list) {
      if (Array.isArray(list) && list.length) state.profiles = list;
      scheduleEnsure(0);
    },
    openPicker,
  };

  function boot() {
    [160, 600, 1600, 3200].forEach((ms) => scheduleEnsure(ms));
    // 客户端重启后：先看本档案待定记录，再从后台恢复跨档案的切换结果。
    try {
      restoreLastResult().catch(() => {});
    } catch {
      /* ignore */
    }
    try {
      new MutationObserver(() => {
        const entry = document.getElementById(BTN_ID);
        const login = document.getElementById(LOGIN_BTN_ID);
        const staleEntry = entry && entry.dataset.wbsSig !== SIG;
        const staleLogin = login && login.dataset.wbsSig !== SIG;
        if (staleEntry || staleLogin || (!entry && !login)) scheduleEnsure(420);
      }).observe(document.documentElement, { childList: true, subtree: true });
    } catch {
      /* ignore */
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
  window.addEventListener("popstate", () => scheduleEnsure(500));
})();
