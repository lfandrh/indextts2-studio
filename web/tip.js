/* 自绘悬浮提示
   ─────────────────────────────────────────────────────────────
   为什么需要这个文件：全站 60 多处提示都挂在 title 属性上，而 title 的浮层是
   浏览器自己画的（Windows 上是白底 + 1px 灰边 + 系统字体，没有圆角也没有阴影）。
   那层浮层不在 DOM 里，CSS 一个像素都碰不到，所以深色主题下它会像贴了张便签纸。

   这个文件**只换外观，不改事件**：什么时候弹、什么时候收，全部对齐 Chrome 原生
   title 的行为，避免出现「原生会弹的地方这里不弹」「点完还挂在那儿」这类差异。
   对齐的六条：
     1. 指针停在带 title 的元素上满 500ms 才弹；中途移走就取消计时，不弹。
     2. 移出元素立刻收（在元素内部换子节点不算移出）。
     3. 按下鼠标、滚动、缩放窗口、按 Esc、窗口失焦，立刻收。
     4. 键盘 Tab 聚焦不弹（原生也不弹，读屏仍能念到 title）。
     5. 点击后指针原地不动，不再弹 —— 按钮被重建时 Chromium 会补发 pointerover，
        原生 title 这种情况下是不弹的，所以按下坐标记下来，指针真的动了才恢复。
     6. 弹出来时把 title 摘掉（不摘会和原生浮层叠成两个框），收起时原样还回去；
        没还回去的话这处提示文字就永久消失了，读屏和二次悬停都拿不到。

   用事件委托而不是给每个元素绑监听：app.js 里有 20 余处渲染后才写 title
   （历史条目、预设卡、示例卡、默认值提示…），委托能自动覆盖，不用去改那些地方。
*/
(() => {
  "use strict";

  const DELAY = 500;      // 原生 title 的驻留时长；太短会在扫过成排按钮时一路乱闪
  const GAP = 9;          // 气泡与元素的间距（箭头占 5px，视觉留白约 4px）
  const EDGE = 8;         // 气泡到视口边缘的最小留白
  const ARROW_PAD = 14;   // 箭头中心距气泡圆角的最小距离

  let host = null;        // 当前关注的元素（可能还在计时，气泡未必已弹）
  let text = "";          // 从 title 摘下来的原文
  let timer = 0;
  let tip = null;
  let stripped = false;   // title 是否已被摘掉 —— 只有摘过才需要还
  let down = null;        // 最近一次按下鼠标的坐标，见文件头第 5 条

  function clearTimer() {
    if (timer) {
      clearTimeout(timer);
      timer = 0;
    }
  }

  function ensure() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.id = "tip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    document.body.appendChild(tip);
    return tip;
  }

  /* 定位：默认在元素下方居中；下方放不下就翻到上方。
     水平方向先试居中，居中会被视口边缘切掉时改成「以箭头为支点」对齐 ——
     直接把气泡夹进视口会让它离元素很远，看起来像没挂上去。 */
  function place() {
    const r = host.getBoundingClientRect();
    const b = tip.getBoundingClientRect();
    const cx = r.left + r.width / 2;

    let dir = "down";
    let top = r.bottom + GAP;
    if (top + b.height > window.innerHeight - EDGE) {
      const up = r.top - GAP - b.height;
      if (up >= EDGE) {
        dir = "up";
        top = up;
      } else {
        // 上下都放不下（元素几乎占满视口）：留在下方夹进视口，宁可压住元素也别出血
        top = Math.max(EDGE, Math.min(top, window.innerHeight - EDGE - b.height));
      }
    }

    const maxLeft = Math.max(EDGE, window.innerWidth - EDGE - b.width);
    let left = cx - b.width / 2;
    if (left < EDGE) left = cx - ARROW_PAD;
    else if (left > maxLeft) left = cx - (b.width - ARROW_PAD);
    left = Math.max(EDGE, Math.min(left, maxLeft));

    // 箭头跟着元素中心走；离圆角太近就干脆不画，免得箭头被圆角啃掉一半
    const ax = cx - left;
    const arrow = ax >= ARROW_PAD && ax <= b.width - ARROW_PAD;
    tip.dataset.dir = dir;
    tip.dataset.arrow = arrow ? "1" : "0";
    tip.style.top = `${Math.round(top)}px`;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.setProperty("--tip-arrow", `${Math.round(ax)}px`);
  }

  function show() {
    if (!host || !host.isConnected) {
      host = null;
      return;
    }
    ensure();
    tip.textContent = text;
    // 先摆在左上角再量尺寸：位置会参与 getBoundingClientRect，不能让上一次的坐标混进来
    tip.style.top = "0px";
    tip.style.left = "0px";
    tip.hidden = false;
    place();
    tip.classList.add("on");
    // 位置算完才摘 title：中途出意外至少原生提示还在
    host.removeAttribute("title");
    stripped = true;
  }

  function hide() {
    clearTimer();
    if (host) {
      // 只补回还没被重新赋值的那份：调用方可能在悬停期间又写了新 title
      if (stripped && host.isConnected && !host.hasAttribute("title")) {
        host.setAttribute("title", text);
      }
      host = null;
    }
    text = "";
    stripped = false;
    if (tip) {
      tip.classList.remove("on");
      tip.hidden = true;
    }
  }

  /* 开始关注一个元素；还没弹就先计时 */
  function watch(node) {
    hide();
    const raw = node.getAttribute("title");
    if (!raw) return;
    host = node;
    text = raw;
    timer = setTimeout(() => {
      timer = 0;
      show();
    }, DELAY);
  }

  document.addEventListener("pointerover", (e) => {
    // 点击后指针原地不动：元素被重建时浏览器会补发 pointerover，这里不接。
    // 注意顺序：指针移到新元素时 Chromium 先发 pointerover 再发 pointermove，
    // 所以不能只等 pointermove 来解禁 —— 要在这里比对距离，真的移开了就放行，
    // 否则「点一下旁边的按钮再滑到相邻按钮」会一直不弹。
    if (down) {
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) < 3) return;
      down = null;
    }
    const node = e.target instanceof Element ? e.target.closest("[title]") : null;
    if (!node) {
      if (host) hide();
      return;
    }
    if (node === host) return;
    watch(node);
  }, true);

  document.addEventListener("pointerout", (e) => {
    if (!host) return;
    const to = e.relatedTarget;
    // 在元素内部换子节点（图标、文字）不算离开
    if (to instanceof Node && host.contains(to)) return;
    hide();
  }, true);

  document.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch") down = { x: e.clientX, y: e.clientY };
    hide();
  }, true);

  document.addEventListener("pointermove", (e) => {
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) >= 3) down = null;
  }, true);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  }, true);

  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
  window.addEventListener("wheel", hide, { passive: true, capture: true });
  window.addEventListener("blur", hide);
  document.addEventListener("visibilitychange", hide);
})();
