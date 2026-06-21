/* The Brain — live advisor chat widget.
 *
 * A self-contained floating bubble + popup that streams a conversation with the armed
 * Brain over POST /chat (SSE). Themes off the macro design-system CSS vars (theme.css) so
 * dark/light + EN/ZH just work; static labels use the .l-en/.l-zh spans theme.css toggles.
 * No build step, no external libs. Mounts only on the dashboard (body.page-mm).
 */
(function () {
  "use strict";
  if (window.__brainChat) return;            // guard against double-init
  window.__brainChat = true;
  if (!document.body || !document.body.classList.contains("page-mm")) return;

  var CONV_KEY = "bc_conversation_id";
  var convId = null;
  try { convId = localStorage.getItem(CONV_KEY) || null; } catch (e) {}

  // --- friendly labels for the Brain's tool calls (chips) ---------------------
  var TOOL = {
    get_regime:        ["🌐", "reading the macro regime", "读取宏观状态"],
    get_daily_briefing:["🗒️", "pulling the daily briefing", "拉取每日简报"],
    get_decision_matrix:["🧮", "running the decision matrix", "运行决策矩阵"],
    get_ticker_package:["🔬", "deep-diving", "深度分析"],
    get_fundamentals:  ["📑", "reading fundamentals", "读取基本面"],
    get_options:       ["📐", "reading options (GEX)", "读取期权 (GEX)"],
    get_anticipation:  ["🔮", "the forward signal", "前瞻信号"],
    get_divergences:   ["🔀", "checking divergences", "检查背离"],
    get_intelligence:  ["📡", "reading intelligence", "读取情报"],
    get_altdata:       ["🏛️", "checking alt-data flow", "检查另类数据"],
    get_news:          ["📰", "scanning news", "扫描新闻"],
    get_standouts:     ["⭐", "the buy board", "买入榜"],
    get_themes:        ["🧭", "the theme universe", "主题宇宙"],
    get_portfolio:     ["📁", "reading the book", "读取持仓"],
    get_quote:         ["💵", "live quote", "实时报价"],
    get_intake_candidates:["📥", "the candidate queue", "候选队列"],
    read_signal:       ["📊", "reading", "读取"],
    save_research_note:["📝", "saving a research note", "保存研究笔记"],
    propose_thesis:    ["📌", "staging a thesis", "提交论点"],
    recommend_action:  ["✅", "staging a recommendation", "提交建议"],
    flag_emerging_theme:["🔥", "flagging a theme", "标记主题"],
    WebSearch:         ["🔎", "searching the web", "搜索网络"],
    WebFetch:          ["🌍", "fetching a page", "抓取页面"]
  };
  function toolChip(name, args) {
    var raw = (name || "").replace(/^mcp__bot__/, "");
    var m = TOOL[raw] || ["🔧", raw, raw];
    var hint = "";
    if (args) {
      hint = args.ticker || args.subject || args.name || args.symbol || args.query || "";
      if (!hint && args.path) hint = String(args.path).split("/").pop();
      if (!hint && args.url) { try { hint = new URL(args.url).hostname; } catch (e) {} }
    }
    return { icon: m[0], en: m[1] + (hint ? " " + hint : ""), zh: m[2] + (hint ? " " + hint : "") };
  }

  // --- tiny safe markdown (escape first, then a few inline forms) --------------
  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;";
    });
  }
  function fmt(text) {
    var lines = esc(text).split("\n");
    var out = [], inList = false;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var li = ln.match(/^\s*[-*]\s+(.*)$/);
      if (li) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + inl(li[1]) + "</li>");
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        var h = ln.match(/^\s*#{1,4}\s+(.*)$/);
        if (h) out.push("<div class='bc-h'>" + inl(h[1]) + "</div>");
        else if (ln.trim() === "") out.push("<br>");
        else out.push("<div>" + inl(ln) + "</div>");
      }
    }
    if (inList) out.push("</ul>");
    return out.join("");
  }
  function inl(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  // --- styles -----------------------------------------------------------------
  var css = ""
    + "#bc-bubble{position:fixed;right:22px;bottom:22px;width:56px;height:56px;border-radius:50%;"
    + "background:var(--info);color:#fff;border:none;cursor:pointer;font-size:25px;line-height:1;"
    + "box-shadow:0 6px 22px rgba(0,0,0,.35);z-index:9998;display:flex;align-items:center;justify-content:center;"
    + "transition:transform .18s cubic-bezier(.34,1.5,.5,1),box-shadow .2s;}"
    + "#bc-bubble:hover{transform:scale(1.08);box-shadow:0 8px 28px rgba(0,0,0,.45);}"
    + "#bc-bubble .dot{position:absolute;top:11px;right:11px;width:9px;height:9px;border-radius:50%;"
    + "background:var(--up,#3fbf7f);box-shadow:0 0 0 2px var(--info);}"
    + "#bc-panel{position:fixed;right:22px;bottom:22px;width:min(390px,calc(100vw - 28px));"
    + "height:min(640px,82vh);background:color-mix(in srgb,var(--panel) 88%,transparent);"
    + "backdrop-filter:blur(14px) saturate(1.2);-webkit-backdrop-filter:blur(14px) saturate(1.2);"
    + "border:1px solid var(--line);border-radius:18px;box-shadow:0 18px 50px rgba(0,0,0,.5);"
    + "z-index:9999;display:none;flex-direction:column;overflow:hidden;"
    + "opacity:0;transform:translateY(14px) scale(.98);transition:opacity .2s,transform .22s cubic-bezier(.34,1.4,.5,1);}"
    + "#bc-panel.open{display:flex;opacity:1;transform:none;}"
    + "#bc-head{display:flex;align-items:center;gap:10px;padding:13px 14px;border-bottom:1px solid var(--line);"
    + "background:color-mix(in srgb,var(--panel2) 70%,transparent);}"
    + "#bc-head .av{width:32px;height:32px;border-radius:50%;background:var(--info);display:flex;align-items:center;"
    + "justify-content:center;font-size:17px;flex:none;}"
    + "#bc-head .ttl{font-weight:700;font-size:14px;color:var(--text);line-height:1.15;}"
    + "#bc-head .sub{font-size:10.5px;color:var(--muted);}"
    + "#bc-head .paper{font-size:9px;font-weight:800;letter-spacing:.06em;color:var(--warn,#d8a23a);"
    + "border:1px solid var(--warn,#d8a23a);border-radius:4px;padding:1px 4px;margin-left:4px;}"
    + "#bc-head .sp{flex:1;}"
    + "#bc-head button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:4px 6px;border-radius:6px;}"
    + "#bc-head button:hover{background:var(--panel2);color:var(--text);}"
    + "#bc-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:11px;}"
    + ".bc-row{display:flex;flex-direction:column;max-width:100%;}"
    + ".bc-row.user{align-items:flex-end;}"
    + ".bc-b{padding:9px 12px;border-radius:13px;font-size:13px;line-height:1.5;max-width:88%;word-wrap:break-word;}"
    + ".bc-row.user .bc-b{background:var(--info);color:#fff;border-bottom-right-radius:4px;}"
    + ".bc-row.brain .bc-b{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-bottom-left-radius:4px;}"
    + ".bc-b .bc-h{font-weight:700;margin:3px 0;} .bc-b ul{margin:4px 0;padding-left:18px;} .bc-b li{margin:2px 0;}"
    + ".bc-b code{font-family:\"SF Mono\",ui-monospace,Menlo,monospace;font-size:11.5px;background:var(--panel);padding:1px 4px;border-radius:4px;}"
    + ".bc-chips{display:flex;flex-direction:column;gap:4px;margin:1px 0;}"
    + ".bc-chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);"
    + "background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:3px 9px;align-self:flex-start;}"
    + ".bc-chip.run .ic{animation:bc-spin 1s linear infinite;display:inline-block;}"
    + ".bc-chip.done{opacity:.7;} .bc-chip.done .ic{filter:grayscale(.3);}"
    + "@keyframes bc-spin{to{transform:rotate(360deg);}}"
    + ".bc-typing{display:inline-flex;gap:3px;padding:8px 12px;background:var(--panel2);border:1px solid var(--line);border-radius:13px;align-self:flex-start;}"
    + ".bc-typing i{width:5px;height:5px;border-radius:50%;background:var(--muted);animation:bc-bob 1s infinite;}"
    + ".bc-typing i:nth-child(2){animation-delay:.15s;} .bc-typing i:nth-child(3){animation-delay:.3s;}"
    + "@keyframes bc-bob{0%,60%,100%{opacity:.3;transform:translateY(0);}30%{opacity:1;transform:translateY(-3px);}}"
    + "#bc-quick{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 6px;}"
    + "#bc-quick button{font-size:11px;color:var(--link);background:none;border:1px solid var(--line);"
    + "border-radius:999px;padding:4px 10px;cursor:pointer;} #bc-quick button:hover{background:var(--panel2);}"
    + "#bc-foot{border-top:1px solid var(--line);padding:9px 11px;}"
    + "#bc-inrow{display:flex;align-items:flex-end;gap:8px;}"
    + "#bc-in{flex:1;resize:none;max-height:120px;background:var(--panel2);color:var(--text);border:1px solid var(--line);"
    + "border-radius:11px;padding:9px 11px;font:13px/1.45 Inter,-apple-system,sans-serif;outline:none;}"
    + "#bc-in:focus{border-color:var(--info);}"
    + "#bc-send{background:var(--info);color:#fff;border:none;border-radius:10px;width:38px;height:38px;flex:none;cursor:pointer;font-size:16px;}"
    + "#bc-send:disabled{opacity:.45;cursor:default;}"
    + "#bc-disc{text-align:center;font-size:9.5px;color:var(--muted);margin-top:6px;}";

  var style = document.createElement("style");
  style.id = "bc-style";
  style.textContent = css;
  document.head.appendChild(style);

  // --- DOM ---------------------------------------------------------------------
  function L(en, zh) { return "<span class='l-en'>" + en + "</span><span class='l-zh'>" + zh + "</span>"; }

  var bubble = document.createElement("button");
  bubble.id = "bc-bubble";
  bubble.setAttribute("aria-label", "Chat with the Brain");
  bubble.innerHTML = "🧠<span class='dot'></span>";

  var panel = document.createElement("div");
  panel.id = "bc-panel";
  panel.setAttribute("role", "dialog");
  panel.innerHTML =
      "<div id='bc-head'>"
    +   "<div class='av'>🧠</div>"
    +   "<div><div class='ttl'>" + L("The Brain", "大脑") + "<span class='paper'>PAPER</span></div>"
    +   "<div class='sub'>" + L("Autonomous portfolio advisor", "自主投资顾问") + "</div></div>"
    +   "<div class='sp'></div>"
    +   "<button id='bc-new' title='New chat' aria-label='New chat'>⟳</button>"
    +   "<button id='bc-close' title='Close' aria-label='Close'>✕</button>"
    + "</div>"
    + "<div id='bc-msgs'></div>"
    + "<div id='bc-quick'></div>"
    + "<div id='bc-foot'>"
    +   "<div id='bc-inrow'>"
    +     "<textarea id='bc-in' rows='1' placeholder='Ask the Brain…'></textarea>"
    +     "<button id='bc-send' aria-label='Send'>➤</button>"
    +   "</div>"
    +   "<div id='bc-disc'>" + L("Advisory · paper-only · never executes trades", "仅供参考 · 模拟盘 · 从不实际交易") + "</div>"
    + "</div>";

  document.body.appendChild(bubble);
  document.body.appendChild(panel);

  var msgs = panel.querySelector("#bc-msgs");
  var input = panel.querySelector("#bc-in");
  var sendBtn = panel.querySelector("#bc-send");
  var quick = panel.querySelector("#bc-quick");
  var busy = false;

  var QUICK = [
    ["What's the setup?", "现在的市场状态？", "What's the macro setup right now?"],
    ["Should we add NVDA?", "该加仓 NVDA 吗？", "Should we add NVDA? Walk me through the decision matrix."],
    ["Review the book", "检视持仓", "Review the current paper book — anything to trim or add?"],
    ["What changed today?", "今天有何变化？", "What changed today? Give me the daily briefing."]
  ];
  function renderQuick() {
    quick.innerHTML = "";
    QUICK.forEach(function (q) {
      var b = document.createElement("button");
      b.innerHTML = L(q[0], q[1]);
      b.onclick = function () { if (!busy) send(q[2]); };
      quick.appendChild(b);
    });
  }

  function zh() { return document.documentElement.getAttribute("data-lang") === "zh"; }

  function addRow(who) {
    var row = document.createElement("div");
    row.className = "bc-row " + who;
    msgs.appendChild(row);
    return row;
  }
  function scrollDown() { msgs.scrollTop = msgs.scrollHeight; }

  function greet() {
    msgs.innerHTML = "";
    var row = addRow("brain");
    var b = document.createElement("div");
    b.className = "bc-b";
    b.innerHTML = fmt(zh()
      ? "你好 — 我是**大脑**，你的投资组合顾问。问我宏观状态、任意个股，或检视持仓。我会给出判断并把想法提交到你的审核队列；我从不实际交易。"
      : "Hi — I'm **the Brain**, your portfolio advisor. Ask me about the macro setup, any name, or the book. I give a verdict and stage ideas to your review queue — I never execute trades.");
    row.appendChild(b);
    renderQuick();
    scrollDown();
  }

  function renderTurn(t) {
    if (t.role === "user") {
      var ub = document.createElement("div"); ub.className = "bc-b"; ub.textContent = t.content;
      addRow("user").appendChild(ub);
      return;
    }
    var row = addRow("brain");
    if (t.tools && t.tools.length) {
      var chips = document.createElement("div"); chips.className = "bc-chips";
      t.tools.forEach(function (tl) {
        var c = toolChip(tl.name, tl.args);
        var chip = document.createElement("div"); chip.className = "bc-chip done";
        chip.innerHTML = "<span class='ic'>" + c.icon + "</span>" + L(c.en, c.zh);
        chips.appendChild(chip);
      });
      row.appendChild(chips);
    }
    if (t.content) {
      var b = document.createElement("div"); b.className = "bc-b"; b.innerHTML = fmt(t.content);
      row.appendChild(b);
    }
  }

  function rehydrate() {
    if (!convId) { greet(); return; }
    fetch("/chat/history?conversation_id=" + encodeURIComponent(convId))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var turns = (d && d.turns) || [];
        if (!turns.length) { greet(); return; }
        msgs.innerHTML = "";
        turns.forEach(renderTurn);
        renderQuick();
        scrollDown();
      })
      .catch(function () { greet(); });
  }

  // --- streaming send ----------------------------------------------------------
  function send(text) {
    text = (text || input.value || "").trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true; input.value = ""; autoGrow();
    quick.innerHTML = "";

    var urow = addRow("user");
    var ub = document.createElement("div"); ub.className = "bc-b"; ub.textContent = text;
    urow.appendChild(ub);

    var brow = addRow("brain");
    var typing = document.createElement("div");
    typing.className = "bc-typing"; typing.innerHTML = "<i></i><i></i><i></i>";
    brow.appendChild(typing);
    scrollDown();

    var bodyEl = null, chipsEl = null, lastChip = null, acc = "";
    function ensureBody() {
      if (typing.parentNode) typing.remove();
      if (!bodyEl) { bodyEl = document.createElement("div"); bodyEl.className = "bc-b"; brow.appendChild(bodyEl); }
      return bodyEl;
    }
    function ensureChips() {
      if (!chipsEl) {
        chipsEl = document.createElement("div"); chipsEl.className = "bc-chips";
        brow.insertBefore(chipsEl, bodyEl || null);
      }
      return chipsEl;
    }

    function onEvent(ev) {
      if (ev.type === "start") {
        if (ev.conversation_id) { convId = ev.conversation_id; try { localStorage.setItem(CONV_KEY, convId); } catch (e) {} }
      } else if (ev.type === "text") {
        acc += ev.text;
        ensureBody().innerHTML = fmt(acc);
        scrollDown();
      } else if (ev.type === "tool") {
        if (lastChip) lastChip.classList.remove("run"), lastChip.classList.add("done");
        var c = toolChip(ev.name, ev.args);
        var chip = document.createElement("div"); chip.className = "bc-chip run";
        chip.innerHTML = "<span class='ic'>" + c.icon + "</span>" + L(c.en, c.zh);
        ensureChips().appendChild(chip);
        lastChip = chip; scrollDown();
      } else if (ev.type === "tool_result") {
        if (lastChip) { lastChip.classList.remove("run"); lastChip.classList.add("done"); }
      } else if (ev.type === "done") {
        if (lastChip) { lastChip.classList.remove("run"); lastChip.classList.add("done"); }
        if (!acc) ensureBody().innerHTML = fmt(zh() ? "_(没有返回内容)_" : "_(no response)_");
        finish();
      } else if (ev.type === "error") {
        ensureBody().innerHTML = "<span style='color:var(--down,#e06d6d)'>"
          + esc((zh() ? "出错了：" : "Error: ") + (ev.error || "unknown")) + "</span>";
        finish();
      }
    }
    function finish() { busy = false; sendBtn.disabled = false; scrollDown(); }

    fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: convId })
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
      (function pump() {
        return reader.read().then(function (r) {
          if (r.done) { if (busy) finish(); return; }
          buf += dec.decode(r.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            var frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
            var line = frame.split("\n").find(function (l) { return l.indexOf("data:") === 0; });
            if (!line) continue;
            try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) {}
          }
          return pump();
        });
      })();
    }).catch(function (e) {
      onEvent({ type: "error", error: String(e && e.message || e) });
    });
  }

  // --- input behaviour ---------------------------------------------------------
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  sendBtn.onclick = function () { send(); };

  function open() {
    panel.classList.add("open"); bubble.style.display = "none";
    if (!msgs.children.length) rehydrate();
    setTimeout(function () { input.focus(); }, 80);
  }
  function close() { panel.classList.remove("open"); bubble.style.display = "flex"; }
  bubble.onclick = open;
  panel.querySelector("#bc-close").onclick = close;
  panel.querySelector("#bc-new").onclick = function () {
    convId = null; try { localStorage.removeItem(CONV_KEY); } catch (e) {}
    greet();
  };
})();
