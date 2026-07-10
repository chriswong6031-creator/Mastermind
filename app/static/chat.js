/* The Brain — live advisor chat widget (Intercom-grade).
 *
 * A floating launcher + glass popup that streams a conversation with the armed Brain over
 * POST /chat (SSE). It can run the full evaluate -> research-paper -> trade flow: tool chips
 * narrate the work, a "paper" event renders an Open-research-paper card, and a click opens
 * the paper in a slide-up sheet (also stored on the Research dashboard).
 *
 * Themes off the macro design-system CSS vars (theme.css) so dark/light + EN/中文 just work;
 * static labels use the .l-en/.l-zh spans theme.css toggles. No build step, no libs.
 */
(function () {
  "use strict";

  function pageReady() {
    return !!(document.body && document.body.classList.contains("page-mm"));
  }

  // Mount once. We latch the dedupe flag only AFTER a successful mount (or if a launcher is
  // already in the DOM) — never before the readiness check. So a run that evaluates during a
  // mid-edit / --reload gap, when <body class="page-mm"> isn't in place yet, bails *without*
  // permanently blocking the retry below. Returns true when terminal, false to "try again".
  function boot() {
    if (window.__brainChat || document.getElementById("bc-launch")) { window.__brainChat = true; return true; }
    if (!pageReady()) return false;
    window.__brainChat = true;
    mount();
    return true;
  }

  function mount() {
  var CONV_KEY = "bc_conversation_id";
  var convId = null;
  try { convId = localStorage.getItem(CONV_KEY) || null; } catch (e) {}

  // --- friendly labels for the Brain's tool calls (chips) ---------------------
  var TOOL = {
    evaluate_gate:     ["🧪", "running the preliminary gate", "运行初步筛查"],
    file_research_paper:["📝", "writing the research paper", "撰写研究报告"],
    execute_trade:     ["📈", "executing the paper trade", "执行模拟交易"],
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
    recommend_action:  ["✅", "staging a recommendation", "提交建议"],
    propose_thesis:    ["📌", "staging a thesis", "提交论点"],
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

  // --- tiny safe markdown -----------------------------------------------------
  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;";
    });
  }
  function inl(s) {
    return s.replace(/`([^`]+)`/g, "<code>$1</code>")
            .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
            .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  }
  function fmt(text) {
    var lines = esc(text).split("\n"), out = [], inList = false;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i], li = ln.match(/^\s*[-*]\s+(.*)$/);
      if (li) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + inl(li[1]) + "</li>");
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        var h = ln.match(/^\s*(#{1,4})\s+(.*)$/);
        if (h) out.push("<div class='bc-h h" + h[1].length + "'>" + inl(h[2]) + "</div>");
        else if (ln.trim() === "") out.push("<div class='bc-sp'></div>");
        else out.push("<div>" + inl(ln) + "</div>");
      }
    }
    if (inList) out.push("</ul>");
    return out.join("");
  }

  // --- styles -----------------------------------------------------------------
  var GRAD = "linear-gradient(135deg,#5b9bf0 0%,#7b6cf0 55%,#a45bf0 100%)";
  var css = ""
    // launcher
    + "#bc-launch{position:fixed;right:24px;bottom:24px;width:60px;height:60px;border-radius:50%;border:none;"
    + "cursor:pointer;z-index:2147483000;background:" + GRAD + ";box-shadow:0 10px 30px rgba(60,60,160,.45);"
    + "display:flex;align-items:center;justify-content:center;transition:transform .25s cubic-bezier(.34,1.56,.5,1),box-shadow .25s;}"
    + "#bc-launch:hover{transform:scale(1.07) translateY(-1px);box-shadow:0 14px 36px rgba(60,60,160,.55);}"
    + "#bc-launch:active{transform:scale(.96);}"
    + "#bc-launch .ic{font-size:26px;line-height:1;transition:transform .3s,opacity .2s;filter:drop-shadow(0 1px 1px rgba(0,0,0,.25));}"
    + "#bc-launch .dot{position:absolute;top:12px;right:12px;width:10px;height:10px;border-radius:50%;background:#3fe08a;box-shadow:0 0 0 2.5px #fff;}"
    + "body.bc-open #bc-launch{transform:scale(.9);opacity:0;pointer-events:none;}"
    // panel
    + "#bc-panel{position:fixed;right:24px;bottom:24px;width:min(404px,calc(100vw - 24px));height:min(680px,calc(100vh - 40px));"
    + "z-index:2147483001;display:flex;flex-direction:column;border-radius:22px;overflow:hidden;background:var(--panel);"
    + "border:1px solid var(--line);box-shadow:0 24px 70px rgba(10,12,30,.5),0 4px 14px rgba(10,12,30,.3);"
    + "transform-origin:bottom right;opacity:0;transform:translateY(22px) scale(.92);pointer-events:none;"
    + "transition:opacity .26s,transform .32s cubic-bezier(.22,1.2,.36,1);}"
    + "body.bc-open #bc-panel{opacity:1;transform:none;pointer-events:auto;}"
    // header (gradient)
    + "#bc-hd{position:relative;background:" + GRAD + ";color:#fff;padding:20px 18px 16px;flex:none;}"
    + "#bc-hd .glow{position:absolute;inset:0;background:radial-gradient(120% 90% at 85% -10%,rgba(255,255,255,.28),transparent 60%);pointer-events:none;}"
    + "#bc-hd .top{position:relative;display:flex;align-items:center;gap:11px;}"
    + "#bc-hd .av{width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,.2);backdrop-filter:blur(4px);"
    + "display:flex;align-items:center;justify-content:center;font-size:20px;border:1.5px solid rgba(255,255,255,.45);flex:none;}"
    + "#bc-hd .nm{font-weight:750;font-size:15.5px;letter-spacing:.01em;display:flex;align-items:center;gap:7px;}"
    + "#bc-hd .paper{font-size:9px;font-weight:800;letter-spacing:.07em;background:rgba(255,255,255,.22);padding:2px 6px;border-radius:5px;}"
    + "#bc-hd .st{font-size:11.5px;opacity:.92;display:flex;align-items:center;gap:5px;margin-top:2px;}"
    + "#bc-hd .st i{width:7px;height:7px;border-radius:50%;background:#3fe08a;box-shadow:0 0 0 2px rgba(63,224,138,.35);display:inline-block;}"
    + "#bc-hd .sp{flex:1;}"
    + "#bc-hd button{background:rgba(255,255,255,.16);border:none;color:#fff;width:30px;height:30px;border-radius:9px;cursor:pointer;font-size:14px;transition:background .15s;display:flex;align-items:center;justify-content:center;}"
    + "#bc-hd button:hover{background:rgba(255,255,255,.3);}"
    + "#bc-hd .hi{position:relative;margin-top:13px;font-size:17px;font-weight:700;line-height:1.3;}"
    + "#bc-hd .sub{position:relative;font-size:12px;opacity:.9;margin-top:3px;}"
    // body
    + "#bc-body{flex:1;overflow-y:auto;padding:16px 14px 8px;display:flex;flex-direction:column;gap:3px;background:var(--bg);}"
    + "#bc-body::-webkit-scrollbar{width:7px;}#bc-body::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px;}"
    + ".bc-row{display:flex;gap:8px;align-items:flex-end;margin-top:9px;animation:bc-rise .32s cubic-bezier(.22,1,.36,1) both;}"
    + ".bc-row.user{flex-direction:row-reverse;}"
    + "@keyframes bc-rise{from{opacity:0;transform:translateY(9px);}to{opacity:1;transform:none;}}"
    + ".bc-av{width:26px;height:26px;border-radius:50%;background:" + GRAD + ";display:flex;align-items:center;justify-content:center;font-size:14px;flex:none;box-shadow:0 2px 6px rgba(60,60,160,.35);}"
    + ".bc-row.user .bc-av{display:none;}"
    + ".bc-col{display:flex;flex-direction:column;gap:5px;max-width:80%;}"
    + ".bc-row.user .bc-col{align-items:flex-end;}"
    + ".bc-b{padding:10px 13px;font-size:13.5px;line-height:1.55;border-radius:16px;word-wrap:break-word;overflow-wrap:anywhere;}"
    + ".bc-row.brain .bc-b{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-bottom-left-radius:5px;}"
    + ".bc-row.user .bc-b{background:" + GRAD + ";color:#fff;border-bottom-right-radius:5px;box-shadow:0 3px 12px rgba(70,70,170,.32);}"
    + ".bc-b .bc-h{font-weight:750;margin:5px 0 2px;} .bc-b .bc-h.h1{font-size:15px;} .bc-b .bc-sp{height:7px;}"
    + ".bc-b ul{margin:5px 0;padding-left:18px;} .bc-b li{margin:2px 0;} .bc-b strong{font-weight:700;}"
    + ".bc-b code{font-family:\"SF Mono\",ui-monospace,Menlo,monospace;font-size:11.5px;background:var(--bg);padding:1px 5px;border-radius:5px;}"
    // tool chips
    + ".bc-chips{display:flex;flex-direction:column;gap:5px;}"
    + ".bc-chip{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;color:var(--muted);background:var(--panel2);"
    + "border:1px solid var(--line);border-radius:999px;padding:4px 11px;align-self:flex-start;}"
    + ".bc-chip .ic{font-size:12px;display:inline-block;}"
    + ".bc-chip.run .ic{animation:bc-spin 1.1s linear infinite;}"
    + ".bc-chip.run::after{content:'';width:5px;height:5px;border-radius:50%;background:var(--info);animation:bc-pulse 1s infinite;}"
    + ".bc-chip.done{opacity:.6;} .bc-chip.done .ic{filter:grayscale(.4);}"
    + "@keyframes bc-spin{to{transform:rotate(360deg);}}"
    + "@keyframes bc-pulse{0%,100%{opacity:.3;}50%{opacity:1;}}"
    // paper card
    + ".bc-paper{display:flex;align-items:center;gap:11px;width:100%;text-align:left;cursor:pointer;background:var(--panel2);"
    + "border:1px solid var(--line);border-left:3px solid var(--info);border-radius:13px;padding:11px 13px;transition:transform .15s,box-shadow .15s,border-color .15s;}"
    + ".bc-paper:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(10,12,30,.22);border-left-color:var(--link);}"
    + ".bc-paper.ok{border-left-color:#2bbf6e;} .bc-paper.no{border-left-color:#e0a13a;}"
    + ".bc-paper .pic{font-size:22px;flex:none;}"
    + ".bc-paper .pb{flex:1;min-width:0;} .bc-paper .pt{font-weight:700;font-size:13px;color:var(--text);}"
    + ".bc-paper .pm{font-size:11px;color:var(--muted);margin-top:2px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;}"
    + ".bc-badge{font-size:9.5px;font-weight:800;letter-spacing:.04em;padding:1px 6px;border-radius:5px;}"
    + ".bc-badge.ok{background:rgba(43,191,110,.16);color:#2bbf6e;} .bc-badge.no{background:rgba(224,161,58,.16);color:#d8951f;}"
    + ".bc-paper .go{font-size:20px;color:var(--muted);flex:none;}"
    // typing
    + ".bc-typing{display:inline-flex;gap:4px;padding:11px 14px;background:var(--panel2);border:1px solid var(--line);border-radius:16px;border-bottom-left-radius:5px;align-self:flex-start;}"
    + ".bc-typing i{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:bc-bob 1.1s infinite;}"
    + ".bc-typing i:nth-child(2){animation-delay:.16s;}.bc-typing i:nth-child(3){animation-delay:.32s;}"
    + "@keyframes bc-bob{0%,60%,100%{opacity:.3;transform:translateY(0);}30%{opacity:1;transform:translateY(-4px);}}"
    // quick replies
    + "#bc-quick{display:flex;flex-wrap:wrap;gap:7px;padding:4px 16px 10px;}"
    + "#bc-quick button{font-size:12px;color:var(--link);background:var(--panel);border:1px solid var(--info);border-radius:999px;"
    + "padding:6px 13px;cursor:pointer;font-weight:550;transition:background .15s,transform .12s;}"
    + "#bc-quick button:hover{background:var(--panel2);transform:translateY(-1px);}"
    // composer
    + "#bc-foot{flex:none;border-top:1px solid var(--line);background:var(--panel);padding:10px 12px 8px;}"
    + "#bc-inrow{display:flex;align-items:flex-end;gap:9px;background:var(--panel2);border:1.5px solid var(--line);border-radius:15px;padding:6px 6px 6px 13px;transition:border-color .18s;}"
    + "#bc-inrow:focus-within{border-color:var(--info);}"
    + "#bc-in{flex:1;resize:none;max-height:120px;background:transparent;color:var(--text);border:none;outline:none;padding:6px 0;font:13.5px/1.45 Inter,-apple-system,sans-serif;}"
    + "#bc-send{flex:none;width:36px;height:36px;border:none;border-radius:11px;background:" + GRAD + ";color:#fff;cursor:pointer;font-size:15px;transition:opacity .15s,transform .12s;display:flex;align-items:center;justify-content:center;}"
    + "#bc-send:disabled{opacity:.4;cursor:default;} #bc-send:not(:disabled):hover{transform:scale(1.06);}"
    + "#bc-disc{text-align:center;font-size:9.5px;color:var(--muted);margin-top:7px;}"
    // paper modal (slide-up sheet inside the panel)
    + "#bc-sheet{position:absolute;inset:0;z-index:5;background:var(--panel);display:flex;flex-direction:column;"
    + "transform:translateY(100%);transition:transform .34s cubic-bezier(.22,1,.36,1);}"
    + "#bc-sheet.open{transform:none;}"
    + "#bc-sheet .sh-hd{flex:none;background:" + GRAD + ";color:#fff;padding:15px 16px;}"
    + "#bc-sheet .sh-top{display:flex;align-items:center;gap:10px;}"
    + "#bc-sheet .sh-top button{background:rgba(255,255,255,.18);border:none;color:#fff;width:30px;height:30px;border-radius:9px;cursor:pointer;font-size:15px;}"
    + "#bc-sheet .sh-t{font-weight:750;font-size:15px;}"
    + "#bc-sheet .sh-scores{display:flex;gap:8px;margin-top:11px;flex-wrap:wrap;}"
    + "#bc-sheet .sc{background:rgba(255,255,255,.16);border-radius:9px;padding:5px 10px;font-size:11px;line-height:1.25;}"
    + "#bc-sheet .sc b{display:block;font-size:15px;font-weight:800;}"
    + "#bc-sheet .sh-body{flex:1;overflow-y:auto;padding:16px 18px 24px;font-size:13px;line-height:1.6;color:var(--text);}"
    + "#bc-sheet .sh-body .bc-h{font-weight:750;margin:14px 0 4px;color:var(--text);} #bc-sheet .sh-body .bc-h.h1{font-size:17px;} #bc-sheet .sh-body .bc-h.h2{font-size:14.5px;}"
    + "#bc-sheet .sh-body ul{padding-left:20px;} #bc-sheet .sh-body .bc-sp{height:8px;}"
    + "#bc-sheet .risks{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:11px 14px;margin-bottom:14px;}"
    + "#bc-sheet .risks .rt{font-weight:700;font-size:12px;color:var(--warn,#d8951f);margin-bottom:4px;}";

  var style = document.createElement("style");
  style.id = "bc-style"; style.textContent = css;
  document.head.appendChild(style);

  function L(en, zh) { return "<span class='l-en'>" + en + "</span><span class='l-zh'>" + zh + "</span>"; }
  function zh() { return document.documentElement.getAttribute("data-lang") === "zh"; }

  // --- DOM --------------------------------------------------------------------
  var launch = document.createElement("button");
  launch.id = "bc-launch"; launch.setAttribute("aria-label", "Chat with the Brain");
  launch.innerHTML = "<span class='ic'>🧠</span><span class='dot'></span>";

  var panel = document.createElement("div");
  panel.id = "bc-panel"; panel.setAttribute("role", "dialog");
  panel.innerHTML =
      "<div id='bc-hd'><div class='glow'></div>"
    +   "<div class='top'>"
    +     "<div class='av'>🧠</div>"
    +     "<div><div class='nm'>" + L("The Brain", "大脑") + "<span class='paper'>PAPER</span></div>"
    +       "<div class='st'><i></i>" + L("Active · advisor", "在线 · 顾问") + "</div></div>"
    +     "<div class='sp'></div>"
    +     "<button id='bc-new' title='New chat' aria-label='New chat'>↻</button>"
    +     "<button id='bc-min' title='Close' aria-label='Close'>✕</button>"
    +   "</div>"
    +   "<div class='hi'>" + L("Hi — I'm the Brain 👋", "你好 — 我是大脑 👋") + "</div>"
    +   "<div class='sub'>" + L("Your autonomous portfolio advisor", "你的自主投资顾问") + "</div>"
    + "</div>"
    + "<div id='bc-body'></div>"
    + "<div id='bc-quick'></div>"
    + "<div id='bc-foot'><div id='bc-inrow'>"
    +   "<textarea id='bc-in' rows='1' placeholder='Ask the Brain…'></textarea>"
    +   "<button id='bc-send' aria-label='Send'>↑</button>"
    + "</div><div id='bc-disc'>" + L("Paper trading · no real money · the Brain can add & cut", "模拟交易 · 无真实资金 · 大脑可加减仓") + "</div></div>"
    + "<div id='bc-sheet'><div class='sh-hd'><div class='sh-top'>"
    +   "<button id='bc-sheet-x' aria-label='Back'>←</button><div class='sh-t'></div></div>"
    +   "<div class='sh-scores'></div></div><div class='sh-body'></div></div>";

  document.body.appendChild(launch);
  document.body.appendChild(panel);

  var body = panel.querySelector("#bc-body");
  var input = panel.querySelector("#bc-in");
  var sendBtn = panel.querySelector("#bc-send");
  var quick = panel.querySelector("#bc-quick");
  var sheet = panel.querySelector("#bc-sheet");
  var busy = false;

  var QUICK = [
    ["What's the setup?", "现在的市场状态？", "What's the macro setup right now?"],
    ["Evaluate NVDA to add", "评估是否加仓 NVDA", "I'm pushing NVDA — run it through the gate, write the research paper, and decide whether to add it."],
    ["Review the book", "检视持仓", "Review the current paper book — anything to trim or exit?"],
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

  function addRow(who) {
    var row = document.createElement("div"); row.className = "bc-row " + who;
    if (who === "brain") {
      var av = document.createElement("div"); av.className = "bc-av"; av.textContent = "🧠";
      row.appendChild(av);
    }
    var col = document.createElement("div"); col.className = "bc-col";
    row.appendChild(col); body.appendChild(row);
    return col;
  }
  function bubble(col, html, isText) {
    var b = document.createElement("div"); b.className = "bc-b";
    if (isText) b.textContent = html; else b.innerHTML = html;
    col.appendChild(b); return b;
  }
  function scrollDown() { body.scrollTop = body.scrollHeight; }

  function paperCard(meta) {
    var ok = !!meta.confirmed;
    var btn = document.createElement("button");
    btn.className = "bc-paper " + (ok ? "ok" : "no");
    var verdict = ok ? L("CONFIRMED", "已确认") : L("NOT CONFIRMED", "未确认");
    var badge = "<span class='bc-badge " + (ok ? "ok" : "no") + "'>" + verdict + "</span>";
    var ci = (meta.combined != null) ? (L("Conviction", "信念指数") + " " + meta.combined + "/100") : "";
    btn.innerHTML = "<span class='pic'>📄</span><span class='pb'>"
      + "<span class='pt'>" + L("Research Report", "研究报告") + " · " + (meta.ticker || "") + "</span>"
      + "<span class='pm'>" + badge + ci + "</span></span><span class='go'>›</span>";
    btn.onclick = function () { openPaper(meta.paper_id); };
    return btn;
  }

  // --- paper sheet ------------------------------------------------------------
  function openPaper(id) {
    if (!id) return;
    var t = sheet.querySelector(".sh-t"), sc = sheet.querySelector(".sh-scores"), bd = sheet.querySelector(".sh-body");
    t.textContent = "…"; sc.innerHTML = ""; bd.innerHTML = "<div style='color:var(--muted)'>" + (zh() ? "加载研究报告…" : "Loading the research paper…") + "</div>";
    sheet.classList.add("open");
    fetch("/chat/paper?id=" + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(function (p) {
      if (!p || p.error) { bd.innerHTML = "<div style='color:var(--down,#e06d6d)'>" + (zh() ? "找不到该报告。" : "Paper not found.") + "</div>"; t.textContent = ""; return; }
      t.textContent = (p.ticker || "") + " — " + (zh() ? "研究报告" : "Research Report");
      var ok = !!p.confirmed;
      sc.innerHTML =
          "<div class='sc'><b>" + (p.combined != null ? p.combined : "–") + "</b>" + (zh() ? "信念指数" : "Conviction") + "</div>"
        + "<div class='sc'><b>" + (p.engine_score != null ? p.engine_score : "–") + "</b>" + (zh() ? "引擎分" : "Engine") + "</div>"
        + "<div class='sc'><b>" + (p.research_score != null ? p.research_score : "–") + "</b>" + (zh() ? "研究分" : "Research") + "</div>"
        + "<div class='sc'><b style='color:" + (ok ? "#bff5d8" : "#ffe0a8") + "'>" + (ok ? (zh() ? "确认" : "PASS") : (zh() ? "否决" : "HOLD")) + "</b>" + (p.viability || "") + "</div>";
      var html = "";
      if (p.key_risks && p.key_risks.length) {
        html += "<div class='risks'><div class='rt'>" + (zh() ? "关键风险" : "Key risks") + "</div><ul>"
          + p.key_risks.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul></div>";
      }
      html += fmt(p.report_md || p.summary || "");
      bd.innerHTML = html; bd.scrollTop = 0;
    }).catch(function () { bd.innerHTML = "<div style='color:var(--down,#e06d6d)'>" + (zh() ? "加载失败。" : "Failed to load.") + "</div>"; });
  }
  sheet.querySelector("#bc-sheet-x").onclick = function () { sheet.classList.remove("open"); };

  // --- greeting + rehydrate ---------------------------------------------------
  function greet() {
    body.innerHTML = "";
    var col = addRow("brain");
    bubble(col, fmt(zh()
      ? "问我宏观状态、任意个股，或检视持仓。把一个想加的标的发给我 — 我会先做初步筛查，通过后撰写完整研究报告，再决定是否加入模拟组合。我只做模拟交易，绝不动用真实资金。"
      : "Ask me about the macro setup, any name, or the book. Push me a ticker to add — I'll run the preliminary gate, write a full research paper if it clears, then decide whether to add it to the paper book. Paper trades only; never real money."));
    renderQuick(); scrollDown();
  }
  function renderTurn(t) {
    if (t.role === "user") { bubble(addRow("user"), t.content, true); return; }
    var col = addRow("brain");
    if (t.tools && t.tools.length) {
      var chips = document.createElement("div"); chips.className = "bc-chips";
      t.tools.forEach(function (tl) {
        var c = toolChip(tl.name, tl.args);
        var chip = document.createElement("div"); chip.className = "bc-chip done";
        chip.innerHTML = "<span class='ic'>" + c.icon + "</span>" + L(c.en, c.zh);
        chips.appendChild(chip);
      });
      col.appendChild(chips);
    }
    if (t.content) bubble(col, fmt(t.content));
    (t.papers || []).forEach(function (p) { col.appendChild(paperCard(p)); });
  }
  function rehydrate() {
    if (!convId) { greet(); return; }
    fetch("/chat/history?conversation_id=" + encodeURIComponent(convId))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var turns = (d && d.turns) || [];
        if (!turns.length) { greet(); return; }
        body.innerHTML = ""; turns.forEach(renderTurn); renderQuick(); scrollDown();
      }).catch(function () { greet(); });
  }

  // --- streaming send ---------------------------------------------------------
  function send(text) {
    text = (text || input.value || "").trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true; input.value = ""; autoGrow(); quick.innerHTML = "";

    bubble(addRow("user"), text, true);
    var col = addRow("brain");
    var typing = document.createElement("div"); typing.className = "bc-typing"; typing.innerHTML = "<i></i><i></i><i></i>";
    col.appendChild(typing); scrollDown();

    var bodyEl = null, chipsEl = null, lastChip = null, acc = "";
    function ensureBody() {
      if (typing.parentNode) typing.remove();
      if (!bodyEl) bodyEl = bubble(col, "");
      return bodyEl;
    }
    function ensureChips() {
      if (!chipsEl) { chipsEl = document.createElement("div"); chipsEl.className = "bc-chips"; col.insertBefore(chipsEl, bodyEl || null); }
      return chipsEl;
    }
    function settle() { if (lastChip) { lastChip.classList.remove("run"); lastChip.classList.add("done"); } }

    function onEvent(ev) {
      if (ev.type === "start") {
        if (ev.conversation_id) { convId = ev.conversation_id; try { localStorage.setItem(CONV_KEY, convId); } catch (e) {} }
      } else if (ev.type === "text") {
        acc += ev.text; ensureBody().innerHTML = fmt(acc); scrollDown();
      } else if (ev.type === "tool") {
        settle();
        var c = toolChip(ev.name, ev.args);
        var chip = document.createElement("div"); chip.className = "bc-chip run";
        chip.innerHTML = "<span class='ic'>" + c.icon + "</span>" + L(c.en, c.zh);
        ensureChips().appendChild(chip); lastChip = chip; scrollDown();
      } else if (ev.type === "tool_result") {
        settle();
      } else if (ev.type === "paper") {
        settle();
        if (typing.parentNode) typing.remove();
        col.appendChild(paperCard(ev)); scrollDown();
      } else if (ev.type === "done") {
        settle();
        if (!acc && !col.querySelector(".bc-paper")) ensureBody().innerHTML = fmt(zh() ? "_(没有返回内容)_" : "_(no response)_");
        finish();
      } else if (ev.type === "error") {
        settle();
        ensureBody().innerHTML = "<span style='color:var(--down,#e06d6d)'>" + esc((zh() ? "出错了：" : "Error: ") + (ev.error || "unknown")) + "</span>";
        finish();
      }
    }
    function finish() {
      busy = false; sendBtn.disabled = false;
      if (typing.parentNode) typing.remove();   // never leave the '...' spinner up (incl. content-free clean close)
      renderQuick();                             // restore the quick-reply chips cleared at send() (idempotent)
      scrollDown();
    }

    fetch("/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: convId }) })
      .then(function (resp) {
        if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
        var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
        // RETURN the pump promise so a mid-stream reader.read() rejection (a 524/proxy drop, VPS
        // hiccup, or crash after the 200 header) propagates to the .catch below -> onEvent(error) ->
        // finish(). Without the return, the pump chain was orphaned and the widget wedged forever.
        return (function pump() {
          return reader.read().then(function (r) {
            if (r.done) { if (busy) finish(); return; }
            buf += dec.decode(r.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              var frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
              var line = frame.split("\n").find(function (l) { return l.indexOf("data:") === 0; });
              if (line) { try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) {} }
            }
            return pump();
          });
        })();
      }).catch(function (e) { onEvent({ type: "error", error: String(e && e.message || e) }); });
  }

  // --- input + open/close -----------------------------------------------------
  function autoGrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  sendBtn.onclick = function () { send(); };

  function open() {
    document.body.classList.add("bc-open");
    if (!body.children.length) rehydrate();
    setTimeout(function () { input.focus(); }, 120);
  }
  function close() { document.body.classList.remove("bc-open"); sheet.classList.remove("open"); }
  launch.onclick = open;
  panel.querySelector("#bc-min").onclick = close;
  panel.querySelector("#bc-new").onclick = function () {
    convId = null; try { localStorage.removeItem(CONV_KEY); } catch (e) {}
    sheet.classList.remove("open"); greet();
  };
  }   // end mount()

  // Bootstrap: attempt immediately, then on DOMContentLoaded, then poll briefly. This covers the
  // windows where index.html is being edited under the --reload dev server and chat.js can
  // evaluate before <body class="page-mm"> is in place. boot() only latches __brainChat after a
  // real mount, so a premature bail never permanently strands the widget.
  if (!boot()) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot, { once: true });
    }
    var __bcTries = 0;
    var __bcIv = setInterval(function () {
      if (boot() || ++__bcTries > 40) clearInterval(__bcIv);   // ~40 × 150ms ≈ 6s
    }, 150);
  }
})();
