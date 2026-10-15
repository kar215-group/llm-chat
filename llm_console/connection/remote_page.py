# -*- coding: utf-8 -*-
"""llm_console.connection.remote_page — 远程访问的内嵌 Web 单页（2026-10-12 模块化）。

为什么内嵌成源码常量而不是外部文件：单文件 exe 里 `__file__` 指向临时解包目录
（坑 62 同族），带文件就得再算 `_MEIPASS` —— 纯文本常量最省事，也不用进打包钩子。
页面无外部 CDN、无构建链（校园网 / 离线可用），手机浏览器优先。

`__AUTHED__` 由 handler 按配对状态替换（1 = 已配对 / 本机，直接进主页）。
raw 字符串：JS 里的反斜杠（如 join("\n")）不能被 Python 先吃一道。
**控件的标签 / 置灰原因 / 档表全部来自 /api/caps（core 现算，与 GUI 同源），
页面里不写死第二份判据**；用户可见文案一律 W 批准清单内或 core 复用。
"""

_PAGE_HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Chat 远程</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;
     max-width:30em;margin:0 auto;padding:1.2em;background:#f4f4f6;color:#222}
h2{font-size:1.15em;margin:.2em 0 .6em}
textarea{width:100%;box-sizing:border-box;height:6em;font-size:1em;
         border:1px solid #ccc;border-radius:8px;padding:.5em}
button{font-size:1em;padding:.45em 1.1em;border:0;border-radius:8px;
       background:#4a5fc1;color:#fff}
button:disabled{background:#aaa}
#code{font-size:1.5em;letter-spacing:.35em;width:7em;text-align:center}
.msg{color:#b33;min-height:1.2em;font-size:.9em}
#bar{background:#ddd;border-radius:6px;height:10px;overflow:hidden;display:none}
#bar>div{background:#4a5fc1;height:100%;width:0}
img.art{max-width:100%;border-radius:8px;margin-top:.6em}
.hint{color:#777;font-size:.85em}
a.dl{display:inline-block;margin-top:.4em}
#tiles{display:flex;flex-wrap:wrap;gap:.5em;margin:.4em 0}
.tile{position:relative;width:72px;height:72px;border:1px solid #ccc;
      border-radius:8px;overflow:hidden;background:#fff}
.tile img{width:100%;height:100%;object-fit:cover}
.tile .x{position:absolute;top:2px;right:2px;width:22px;height:22px;
         line-height:20px;text-align:center;border-radius:11px;padding:0;
         background:rgba(0,0,0,.55);color:#fff;font-size:14px}
.tile .mk{position:absolute;left:0;right:0;bottom:0;text-align:center;
          background:rgba(0,0,0,.55);color:#fff;font-size:11px;line-height:1.5}
/* 页签：生图 / 聊天 两个区互斥显示（手机屏小，堆一起要滚半天）*/
#tabs{display:flex;gap:.5em;margin:0 0 .8em}
#tabs button{flex:1;background:#e6e6ee;color:#333}
#tabs button.on{background:#4a5fc1;color:#fff}
#msgs{margin:.2em 0 .6em}
.b{max-width:86%;padding:.5em .7em;border-radius:10px;margin:.35em 0;
   white-space:pre-wrap;word-break:break-word;font-size:.95em}
.bu{background:#4a5fc1;color:#fff;margin-left:auto}
.ba{background:#fff;border:1px solid #ddd}
.bc{background:#ececf1;color:#666;font-size:.85em;border:0}
/* 之前的成果（P1-6）：一格一缩略图，点开就是原图那条 /api/artifact */
#hist{display:flex;flex-wrap:wrap;gap:.5em;margin:.4em 0}
.tile a{display:block;width:100%;height:100%}
.tile a img{display:block}
#modezone label{margin-right:1.2em}
#modezone input:disabled+span{color:#aaa}
#modewhy{white-space:pre-line}
#echo{white-space:pre-line}
/* 涂抹页 = 全屏（W 2026-10-12 定：浏览器里只有图片和底下功能按钮）。
   画布两维适配视口——只按宽度算会被 body 的 max-width 裁掉（1:1 图最先
   穿帮，坑 219）；笔画坐标归一化存储，转屏/缩放原地重铺不丢笔画。*/
#maskui{position:fixed;left:0;top:0;right:0;bottom:0;background:#f4f4f6;
        z-index:10;flex-direction:column;
        user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;
        touch-action:manipulation}
#cvarea{flex:1;display:flex;align-items:center;justify-content:center;
        min-height:0;padding:6px;overflow:hidden}
#cvwrap{position:relative;line-height:0;border:1px solid #ccc;
        border-radius:8px;overflow:hidden}
#cvover{position:absolute;left:0;top:0;touch-action:none}
#maskbar{background:#ececf1;border-top:1px solid #d5d5da;
         padding:.45em .8em;
         padding-bottom:calc(.45em + env(safe-area-inset-bottom))}
#maskbar .hint{margin:0 0 .3em}
#maskbar .mrow{display:flex;align-items:center;gap:.6em;flex-wrap:wrap;
               margin-top:.35em}
#maskbar .msg{flex:1;min-width:8em}
#brush{flex:1;min-width:6em;max-width:16em}
</style></head><body>
<div id="pair" style="display:none">
<h2>连接到电脑</h2>
<p>输入电脑端「设置 → 远程访问」里显示的 6 位配对码：</p>
<p><input id="code" maxlength="6" inputmode="numeric" pattern="[0-9]{6}"></p>
<p><button id="pairbtn">配对</button></p>
<p class="msg" id="pairmsg"></p>
<p class="hint">配对需要电脑端确认；配对一次后这台设备会一直可用，直到在电脑端被吊销。</p>
</div>
<div id="home" style="display:none">
<div id="tabs"><button id="tab_img" type="button">生图</button>
  <button id="tab_chat" type="button">聊天</button></div>
<div id="imgzone">
<h2>远程生图 <span class="hint" style="font-size:.8em">×</span><input
   id="cnt" type="number" min="1" max="8" value="1" inputmode="numeric"
   style="width:2.4em;font-size:.8em;padding:.15em .25em;border:1px solid #ccc;
   border-radius:6px;vertical-align:middle"></h2>
<textarea id="prompt" placeholder="描述想要的画面…"></textarea>
<p><button id="pick" type="button">📎 参考图</button>
   <span class="hint" id="uphint">支持 PNG / JPG / WEBP / BMP，单张不超过 20MB</span></p>
<input type="file" id="file" accept="image/png,image/jpeg,image/webp,image/bmp"
       multiple style="display:none">
<div id="tiles"></div>
<p class="hint" id="refnote"></p>
<div id="modezone">
<p class="hint" style="margin:.2em 0">带图方式</p>
<label><input type="radio" name="rmode" id="rd_edit" value="edit">
       <span id="rl_edit">底图重绘</span></label>
<label><input type="radio" name="rmode" id="rd_subject" value="subject">
       <span id="rl_subject">主体参考</span></label>
<p class="hint" id="modewhy"></p>
<p id="tierrow" style="display:none">重绘强度 <select id="tier"></select></p>
</div>
<p><button id="go">开始生成</button> <button id="stop" style="display:none">停止</button></p>
<p class="msg" id="msg"></p>
<p class="hint" id="echo"></p>
<div id="bar"><div></div></div>
<p class="hint" id="tail"></p>
<div id="art"></div>
<div id="histzone" style="display:none">
<p class="hint" style="margin:.9em 0 .2em">之前的成果</p>
<p class="hint" id="histnote" style="margin:0 0 .2em"></p>
<p class="hint" id="histempty" style="display:none">这台设备还没有可取回的产物</p>
<div id="hist" style="display:none"></div>
</div>
</div>
<div id="chatzone" style="display:none">
<h2>和电脑上的模型聊</h2>
<p class="hint" id="chatwhy"></p>
<div id="msgs"></div>
<textarea id="ct" placeholder="想跟它说点什么…"></textarea>
<p><button id="csend">发送</button> <button id="cstop" type="button" style="display:none">停止</button>
   <button id="cclear" type="button">新对话</button></p>
<p class="msg" id="cmsg"></p>
</div>
</div>
<div id="maskui" style="display:none">
<div id="cvarea"><div id="cvwrap"><canvas id="cvbase"></canvas><canvas id="cvover"></canvas></div></div>
<div id="maskbar">
<p class="hint">涂过的地方交给模型重绘，没涂的地方保持不变；一点不涂 = 不用遮罩。</p>
<p class="hint">双指捏合缩放图片，双指拖动移动画面。</p>
<div class="mrow">笔刷 <input type="range" id="brush" min="4" max="160" value="40">
  <label><input type="checkbox" id="erase">橡皮擦</label></div>
<div class="mrow"><button id="mclear" type="button">清空</button>
  <button id="mcancel" type="button">取消</button>
  <button id="mok" type="button">确定</button>
  <span class="msg" id="maskmsg"></span></div>
</div>
</div>
<script>
var AUTHED = "__AUTHED__" === "1";
var $ = function(id){return document.getElementById(id)};
var timer = null, curTask = null;
var CAPS = null;                 // /api/caps 的现算结果（标签/置灰原因/档表）
var REFS = [];                   // [{name: 服务端登记名, url: 本地缩略图}]
var MASKS = {};                  // 参考图登记名 -> 遮罩登记名
var MODE = "", TIER = "";        // 带图方式 / 重绘强度档（"" = 自动）
var maskRef = null, strokes = [], maskNat = [0, 0];
var TAB = "img", CHAT = {sa: 0, st: 0, timer: null, loaded: false};
function setTab(t){
  TAB = t;
  $("imgzone").style.display = t === "img" ? "block" : "none";
  $("chatzone").style.display = t === "chat" ? "block" : "none";
  $("tab_img").className = t === "img" ? "on" : "";
  $("tab_chat").className = t === "chat" ? "on" : "";
  if (t === "chat" && !CHAT.loaded) loadChat();
  if (t === "chat") syncChat();
}
$("tab_img").onclick = function(){ setTab("img"); };
$("tab_chat").onclick = function(){ setTab("chat"); };
function bubble(cls, text){
  var d = document.createElement("div");
  d.className = "b " + cls; d.textContent = text;
  $("msgs").appendChild(d); return d;
}
function syncChat(){
  var c = (CAPS && CAPS.chat) || {};
  $("chatwhy").textContent = c.ok ? ("当前在服务：" + (c.model || "本地模型"))
                                  : (c.why || "没能从电脑端取到服务状态。");
  $("csend").disabled = !c.ok || CHAT.timer !== null;
}
function loadChat(){
  CHAT.loaded = true;
  fetch("/api/chat?sa=0&st=0&hist=1")
    .then(function(r){ return r.json(); })
    .then(function(j){
      $("msgs").innerHTML = "";
      (j.history || []).forEach(function(m){
        bubble(m.role === "user" ? "bu" : "ba", m.content);
      });
      if (j.state === "running") resumeChat(j);
      syncChat();
    })
    .catch(function(){});
}
function resumeChat(j){
  var node = bubble("ba", "");
  CHAT.node = node; CHAT.sa = j.sa; CHAT.st = j.st;
  if (j.thinking) { CHAT.rn = bubble("bc", j.thinking); }
  node.textContent = j.answer || "";
  $("cstop").style.display = ""; $("csend").disabled = true;
  CHAT.timer = setInterval(tickChat, 800);
}
function tickChat(){
  fetch("/api/chat?sa=" + CHAT.sa + "&st=" + CHAT.st)
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (j.thinking) {
        if (!CHAT.rn) CHAT.rn = bubble("bc", "");
        CHAT.rn.textContent += j.thinking;
      }
      if (j.answer && CHAT.node) CHAT.node.textContent += j.answer;
      CHAT.sa = j.sa; CHAT.st = j.st;
      if (j.state !== "running") {
        clearInterval(CHAT.timer); CHAT.timer = null;
        $("cstop").style.display = "none";
        if (j.state === "failed") $("cmsg").textContent = j.error || "出错了。";
        else if (j.state === "incomplete")
          $("cmsg").textContent = "回答可能不完整（连接提前断了）。";
        else if (j.state === "cancelled") $("cmsg").textContent = "已停止。";
        else $("cmsg").textContent = "";
        CHAT.node = null; CHAT.rn = null;
        syncChat();
      }
    })
    .catch(function(){});
}
$("csend").onclick = function(){
  var t = $("ct").value.trim();
  if (!t) { $("cmsg").textContent = "先打点什么。"; return; }
  $("cmsg").textContent = ""; $("ct").value = "";
  fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text:t})})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(o){
      if (!o.ok) { $("cmsg").textContent = o.j.error || "发送失败。"; syncChat(); return; }
      bubble("bu", t);
      CHAT.sa = 0; CHAT.st = 0;
      resumeChat({sa: 0, st: 0, answer: "", thinking: ""});
      syncChat();
    })
    .catch(function(){ $("cmsg").textContent = "网络错误，再试一次。"; });
};
$("cstop").onclick = function(){
  fetch("/api/chat/stop", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:"{}"}).catch(function(){});
};
$("cclear").onclick = function(){
  fetch("/api/chat/clear", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:"{}"})
    .then(function(r){ return r.json(); })
    .then(function(){ $("msgs").innerHTML = ""; $("cmsg").textContent = "";
                       CHAT.sa = 0; CHAT.st = 0; })
    .catch(function(){});
};

function show(which){
  $("pair").style.display = which === "pair" ? "block" : "none";
  $("home").style.display = which === "home" ? "block" : "none";
  if (which !== "mask") $("maskui").style.display = "none";
}
if (AUTHED) { show("home"); setTab("img"); loadCaps(); } else show("pair");
$("pairbtn").onclick = function(){
  var code = $("code").value.trim();
  if (!/^[0-9]{6}$/.test(code)) { $("pairmsg").textContent = "请输 6 位数字。"; return; }
  $("pairbtn").disabled = true;
  fetch("/api/pair", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code:code, name:"手机"})})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(o){
      if (o.ok) { location.reload(); }
      else { $("pairmsg").textContent = o.j.error || "配对失败。"; }
    })
    .catch(function(){ $("pairmsg").textContent = "网络错误，再试一次。"; })
    .finally(function(){ $("pairbtn").disabled = false; });
};

/* ---- 能力（模式两档 / 强度档表 / 上传限制，全部服务端 core 现算）---- */
function loadCaps(){
  fetch("/api/caps")
    .then(function(r){ return r.json(); })
    .then(function(j){
      CAPS = j;
      var whys = [];
      ["edit", "subject"].forEach(function(m){
        var info = (j.modes || {})[m] || {};
        var rd = $(m === "edit" ? "rd_edit" : "rd_subject");
        var rl = $(m === "edit" ? "rl_edit" : "rl_subject");
        if (info.label) rl.textContent = info.label;
        rd.disabled = !info.ok;
        rd.title = info.ok ? ((info.label || "") + "（" + (info.note || "") + "）")
                           : (info.why || "");
        if (!info.ok && info.why) whys.push((info.label || m) + "：" + info.why);
      });
      $("modewhy").textContent = whys.join("\n");
      var want = MODE || j.default_mode || "edit";
      if (!(j.modes || {})[want] || !(j.modes || {})[want].ok)
        want = j.default_mode || "edit";
      MODE = want;
      $(want === "subject" ? "rd_subject" : "rd_edit").checked = true;
      var sel = $("tier");
      sel.innerHTML = "";
      [["", "自动"]].concat(j.tiers || []).forEach(function(t){
        var o = document.createElement("option");
        o.value = t[0]; o.textContent = t[1];
        sel.appendChild(o);
      });
      sel.value = TIER;
      if (sel.value !== TIER) { TIER = ""; sel.value = ""; }
      var up = j.upload || {};
      $("uphint").textContent = "支持 " + (up.formats || "PNG / JPG / WEBP / BMP")
        + "，单张不超过 " + Math.round((up.max_bytes || 20971520) / 1048576) + "MB";
      syncZones();
      if (j.running) resumeTask(j.running);   /* 刷新后恢复运行中任务 */
      loadHistory();  /* 之前的成果（每次刷能力清单重拉一次） */
      syncChat();     /* 聊天区判据同源（能不能聊 / 在服务的是谁） */
    })
    .catch(function(){
      /* 拉不到能力清单不许静默：档位区会停在默认状态、强度下拉是空的，
         用户看不出"这是没取到"还是"电脑端就不支持"。提交时服务端仍会逐条校验。*/
      $("modewhy").textContent = "没能从电脑端取到能力清单（连接断了？）："
        + "这一页先按默认档走，提交时电脑端会再校验一次。";
    });
}
function syncZones(){
  /* 「重绘强度」只在底图重绘档出现（主体参考不传 --strength，GUI 同款显隐）*/
  $("tierrow").style.display = (MODE === "edit") ? "" : "none";
  var n = "";
  if (MODE === "edit" && REFS.length > 1)
    n = "底图重绘一次只认一张，多出来的没送进去";
  $("refnote").textContent = n;
}
/* ---- 之前的成果（P1-6）---- */
/* 台账里的成功产物清单。只取 task/name，路径不出电脑；
   能看到谁的，判据在服务端 jobs._may_see 一处，与取回完全同条。 */
function loadHistory(){
  fetch("/api/history?limit=12")
    .then(function(r){ return r.json(); })
    .then(function(j){
      var items = (j && j.items) || [];
      var box = $("hist");
      box.innerHTML = "";
      $("histzone").style.display = "block";
      /* 开关开着就不多说；关着才说明边界在哪（判据来自 /api/caps）*/
      $("histnote").textContent = (CAPS && CAPS.history_all) ? ""
        : "只列你自己提交过的（电脑端可放开取回全部）";
      if (!items.length) { $("hist").style.display = "none";
                           $("histempty").style.display = ""; return; }
      $("histempty").style.display = "none";
      box.style.display = "flex";
      items.forEach(function(it){
        var u = "/api/artifact?task=" + encodeURIComponent(it.task)
                + "&name=" + encodeURIComponent(it.name);
        var d = document.createElement("div"); d.className = "tile";
        var a = document.createElement("a"); a.href = u; a.target = "_blank";
        var im = new Image(); im.src = u; im.alt = it.name;
        /* 渲染兜底（不是判据：能不能取回的判据在服务端 /api/history 一处）：
           列出来到加载之间文件刚被删掉时，别留一格碎图 */
        im.onerror = function(){ d.remove(); };
        a.appendChild(im); d.appendChild(a); box.appendChild(d);
      });
    })
    .catch(function(){});
}

$("rd_edit").onchange = function(){ MODE = "edit"; syncZones(); };
$("rd_subject").onchange = function(){ MODE = "subject"; syncZones(); };
$("tier").onchange = function(){ TIER = this.value; };

/* ---- 附件上传 ---- */
$("pick").onclick = function(){ $("file").click(); };
$("file").onchange = function(){
  /* FileList 是**活的**：先拷贝出 File 再清 value（清 value = 允许重选同一张图，
     但会把同一个 FileList 当场清空——顺序反了真实选图会静默不上传，坑 217）*/
  var fs = Array.prototype.slice.call(this.files || []);
  this.value = "";
  for (var i = 0; i < fs.length; i++) uploadFile(fs[i], "ref", onUploaded);
};
function uploadFile(f, kind, done){
  var maxb = ((CAPS || {}).upload || {}).max_bytes || 20971520;
  if (!f.size) { $("msg").textContent = "这个文件是空的，换一张。"; return; }
  if (f.size > maxb) {
    $("msg").textContent = "这张图 " + (f.size / 1048576).toFixed(1) + "MB，超过 "
      + Math.round(maxb / 1048576) + "MB 上限——先压小一点。";
    return;
  }
  $("msg").textContent = "正在上传附件…";
  fetch("/api/upload?kind=" + kind, {method:"POST",
      headers:{"Content-Type":"application/octet-stream"}, body:f})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(o){
      if (!o.ok) { $("msg").textContent = o.j.error || "上传失败。"; return; }
      $("msg").textContent = "";
      done(f, o.j.name);
    })
    .catch(function(){ $("msg").textContent = "网络错误，再试一次。"; });
}
function onUploaded(f, name){
  REFS.push({name:name, url:URL.createObjectURL(f)});
  renderTiles();
}
function renderTiles(){
  var box = $("tiles");
  box.innerHTML = "";
  REFS.forEach(function(rf, idx){
    var d = document.createElement("div");
    d.className = "tile";
    var im = document.createElement("img");
    im.src = rf.url;
    d.appendChild(im);
    if (MASKS[rf.name]) {
      var mk = document.createElement("span");
      mk.className = "mk"; mk.textContent = "已涂遮罩";
      d.appendChild(mk);
    }
    var x = document.createElement("button");
    x.className = "x"; x.textContent = "×"; x.type = "button";
    x.onclick = function(ev){
      ev.stopPropagation();
      REFS.splice(idx, 1);
      delete MASKS[rf.name];
      /* 瓷砖移除即释放本地 Blob URL，反复选图不泄漏浏览器内存 */
      try { URL.revokeObjectURL(rf.url); } catch(e){}
      renderTiles(); syncZones();
    };
    d.appendChild(x);            /* 2026-10-13 补：原先创建了却忘了挂上 DOM —— 按钮
                                    从 beta2 起就"存在但看不见"，删除入口实际不可用 */
    d.onclick = function(){ openMask(rf); };
    box.appendChild(d);
  });
  syncZones();
}

/* ---- 涂抹遮罩（全屏页：浏览器里只有图片 + 底下功能条，W 2026-10-12 定；
   双层画布：底图画一次不再动、红色叠加层整层重画——与 GUI maskedit 同一条
   "盖一层红色，别实时合成"口径。笔画坐标与笔宽**归一化**存（0~1），渲染时
   乘当前画布尺寸：转屏 / 窗口缩放 / 手机键盘弹出时原地重铺、笔画不丢；
   画布两维适配视口（只按宽度算会被 body 的 max-width 裁掉，坑 219）。
   双指手势（2026-10-12，W 点名"照手机修图的路子来"）：捏合改缩放、双指
   拖动改平移，视图经 cvwrap 的 CSS transform 叠加在适配尺寸之上。---- */
var maskImg = null;
function maskStroke(c, s, W, H, color){
  c.globalCompositeOperation = s.er ? "destination-out" : "source-over";
  c.strokeStyle = c.fillStyle = color;
  c.lineWidth = Math.max(1, s.w * W);
  c.lineCap = "round"; c.lineJoin = "round";
  c.beginPath();
  c.moveTo(s.pts[0][0] * W, s.pts[0][1] * H);
  for (var i = 1; i < s.pts.length; i++)
    c.lineTo(s.pts[i][0] * W, s.pts[i][1] * H);
  c.stroke();
  if (s.pts.length === 1) {                 /* 单点 = 一个圆点 */
    c.beginPath();
    c.arc(s.pts[0][0] * W, s.pts[0][1] * H, c.lineWidth / 2, 0, 7);
    c.fill();
  }
}
var MASK_RED = "rgba(255,70,70,0.55)";
/* 视图变换：叠在"适配视口"的底图之上（transform-origin 默认为中心）。
   z = 倍率（1 = 刚适配好，双指捏合改它，封顶 MAXZ）；x/y = 屏幕像素平移
   （双指拖动改它，夹紧到图片任何一边不过视口）。笔画仍按**全图** 0~1 归一化
   存：getBoundingClientRect 带回变换后的几何，屏幕坐标除回去照样落在全图上，
   缩放 / 平移不扭曲已涂笔画、导出也不用知道当前视图。*/
var view = {z: 1, x: 0, y: 0};
var MAXZ = 8;
function applyView(){
  $("cvwrap").style.transform =
      (view.z === 1 && !view.x && !view.y) ? ""
      : "translate(" + view.x + "px," + view.y + "px) scale(" + view.z + ")";
}
function clampView(){
  var over = $("cvover");
  var barH = $("maskbar").offsetHeight || 0;
  var mx = Math.max(0, over.width * view.z / 2 - window.innerWidth / 2);
  var my = Math.max(0, over.height * view.z / 2
                        - Math.max(80, window.innerHeight - barH) / 2);
  view.x = Math.max(-mx, Math.min(mx, view.x));
  view.y = Math.max(-my, Math.min(my, view.y));
}
function maskRedraw(){
  var over = $("cvover"), c = over.getContext("2d");
  c.clearRect(0, 0, over.width, over.height);
  strokes.forEach(function(s){
    maskStroke(c, s, over.width, over.height,
               s.er ? "rgba(0,0,0,1)" : MASK_RED);
  });
}
function fitMask(){
  if (!maskImg) return;
  var natW = maskImg.naturalWidth || 1, natH = maskImg.naturalHeight || 1;
  var barH = $("maskbar").offsetHeight || 0;
  var availW = Math.max(80, window.innerWidth - 14);
  var availH = Math.max(80, window.innerHeight - barH - 14);
  var sc = Math.min(availW / natW, availH / natH, 2);   /* 小图最多放大 2 倍 */
  var dw = Math.max(1, Math.round(natW * sc)), dh = Math.max(1, Math.round(natH * sc));
  var base = $("cvbase"), over = $("cvover");
  base.width = dw; base.height = dh; over.width = dw; over.height = dh;
  base.getContext("2d").drawImage(maskImg, 0, 0, dw, dh);
  maskNat = [natW, natH];
  clampView(); applyView();          /* 转屏 / 缩放后按新尺寸重夹紧、重贴变换 */
  maskRedraw();
}
function openMask(rf){
  if (MODE !== "edit") {
    $("msg").textContent =
      "当前是「主体参考」档，遮罩用不上——切回「底图重绘」再点图片涂抹。";
    return;
  }
  if (REFS.length && REFS[0] !== rf) {
    $("msg").textContent = "底图重绘一次只认一张，多出来的没送进去";
    return;
  }
  var info = ((CAPS || {}).modes || {}).edit || {};
  if (CAPS && info.ok === false) { $("msg").textContent = info.why || ""; return; }
  maskRef = rf; strokes = [];
  $("maskmsg").textContent = "";
  var img = new Image();
  img.onload = function(){
    maskImg = img;
    view.z = 1; view.x = 0; view.y = 0;      /* 每次进涂抹页都从"适配好"开始 */
    $("home").style.display = "none";
    $("maskui").style.display = "flex";
    document.body.style.overflow = "hidden";       /* 全屏页期间锁页面滚动 */
    document.title = "涂抹要重绘的区域";            /* GUI 涂抹页同款标题 */
    fitMask();
  };
  img.src = rf.url;
}
(function(){
  var drawing = false, last = null;
  var live = {};              /* cvover 上按着的指针 pointerId -> {x,y} */
  var gest = null;            /* 双指手势快照：起始间距 / 中点 / 起始视图 */
  function posN(e){
    var over = $("cvover"), r = over.getBoundingClientRect();
    return [(e.clientX - r.left) / (r.width || 1),
            (e.clientY - r.top) / (r.height || 1)];
  }
  function gestStart(){
    var ids = Object.keys(live);
    if (ids.length < 2) return;
    var a = live[ids[0]], b = live[ids[1]];
    gest = {d: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)),
            mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2,
            z: view.z, x: view.x, y: view.y};
  }
  function gestMove(){
    if (!gest) return;
    var ids = Object.keys(live);
    if (ids.length < 2) return;
    var a = live[ids[0]], b = live[ids[1]];
    var d = Math.max(1, Math.hypot(a.x - b.x, a.y - b.y));
    view.z = Math.max(1, Math.min(MAXZ, gest.z * d / gest.d));
    view.x = gest.x + (a.x + b.x) / 2 - gest.mx;
    view.y = gest.y + (a.y + b.y) / 2 - gest.my;
    clampView(); applyView();
  }
  $("cvover").addEventListener("pointerdown", function(e){
    e.preventDefault();
    if (this.setPointerCapture && e.pointerId !== undefined) {
      try { this.setPointerCapture(e.pointerId); } catch (x) {}
    }
    live[e.pointerId] = {x: e.clientX, y: e.clientY};
    if (Object.keys(live).length >= 2) {
      /* 第二根手指落下 = 双指手势开始，手里那笔就此收笔：只有一个点的
         是落指误留的点，撤掉；已拖出线的保留。*/
      if (drawing && strokes.length
          && strokes[strokes.length - 1].pts.length === 1) {
        strokes.pop(); maskRedraw();
      }
      drawing = false;
      gestStart();
      return;
    }
    var over = $("cvover");
    drawing = true; last = posN(e);
    var s = {pts:[last],
             w:(parseInt($("brush").value, 10) || 40) / (over.width || 1),
             er:$("erase").checked};
    strokes.push(s);
    maskStroke(over.getContext("2d"), s, over.width, over.height,
               s.er ? "rgba(0,0,0,1)" : MASK_RED);
  });
  $("cvover").addEventListener("pointermove", function(e){
    if (live[e.pointerId]) live[e.pointerId] = {x: e.clientX, y: e.clientY};
    if (gest) { e.preventDefault(); gestMove(); return; }
    /* 只认正在涂改的那根指针：触摸涂改时鼠标悬停的 move 不该拐进笔画 */
    if (!drawing || !live[e.pointerId]) return;
    e.preventDefault();
    var over = $("cvover"), c = over.getContext("2d");
    var p = posN(e);
    var s = strokes[strokes.length - 1];
    s.pts.push(p);
    /* 拖动中只补这一段线，不整层重画（坑 216 同一条"事件要轻"纪律）*/
    c.globalCompositeOperation = s.er ? "destination-out" : "source-over";
    c.strokeStyle = s.er ? "rgba(0,0,0,1)" : MASK_RED;
    c.lineWidth = Math.max(1, s.w * over.width);
    c.lineCap = "round"; c.lineJoin = "round";
    c.beginPath();
    c.moveTo(last[0] * over.width, last[1] * over.height);
    c.lineTo(p[0] * over.width, p[1] * over.height);
    c.stroke();
    last = p;
  });
  /* pointercancel 也要收笔：手机上系统手势（下拉通知栏 / 返回滑动 / 手掌误触）
     打断触摸时浏览器发的是 cancel 而不是 up，只留 drawing=true 的话下一次落笔
     会从上一笔的终点连一条飞线过来。双指手势同理：按着的指针少于两根就落幕，
     剩下那根手指不接续涂改（抬手后再落笔才算新一笔）。*/
  function endPtr(e){
    delete live[e.pointerId];
    if (gest && Object.keys(live).length < 2) gest = null;
    if (drawing && !Object.keys(live).length) drawing = false;
  }
  window.addEventListener("pointerup", endPtr);
  window.addEventListener("pointercancel", endPtr);
  window.addEventListener("resize", function(){
    if ($("maskui").style.display === "flex") fitMask();
  });
})();
$("mclear").onclick = function(){
  strokes = [];
  var c = $("cvover").getContext("2d");
  c.clearRect(0, 0, $("cvover").width, $("cvover").height);
};
$("mcancel").onclick = function(){ closeMask(); };
function closeMask(){
  $("maskui").style.display = "none";
  $("home").style.display = "block";
  document.body.style.overflow = "";
  document.title = "LLM Chat 远程";
  maskImg = null; maskRef = null; strokes = [];
  view.z = 1; view.x = 0; view.y = 0; applyView();
}
$("mok").onclick = function(){
  if (!maskRef) { closeMask(); return; }
  /* 按**原图分辨率**重放笔画导出（引擎按原图对齐，给显示尺寸会错位——GUI
     maskedit 同款口径）；白 = 要重绘。一点不涂 + 确定 = 撤销遮罩。
     单边封顶 4096：手机浏览器画布有面积上限（iOS 约 16.7MP），全景原图
     直接建会**静默失败**；普通照片（≤4032）不受影响仍是原分辨率，缩放的
     只是遮罩、长宽比不变（引擎侧遮罩本来就要缩到 latent，对齐不受影响）。*/
  var natW = maskNat[0] || 1, natH = maskNat[1] || 1;
  var eSc = Math.min(1, 4096 / Math.max(natW, natH));
  var exW = Math.max(1, Math.round(natW * eSc));
  var exH = Math.max(1, Math.round(natH * eSc));
  var ex = document.createElement("canvas");
  ex.width = exW; ex.height = exH;
  var c = ex.getContext("2d");
  strokes.forEach(function(s){ maskStroke(c, s, exW, exH, "#fff"); });
  var d = c.getImageData(0, 0, exW, exH).data, painted = false;
  for (var i = 3; i < d.length; i += 16) { if (d[i] > 8) { painted = true; break; } }
  if (!painted) {
    delete MASKS[maskRef.name];
    renderTiles(); closeMask();
    return;
  }
  var fin = document.createElement("canvas");
  fin.width = exW; fin.height = exH;
  var fc = fin.getContext("2d");
  fc.fillStyle = "#000"; fc.fillRect(0, 0, exW, exH);
  fc.drawImage(ex, 0, 0);
  $("maskmsg").textContent = "正在保存遮罩…";
  var ref = maskRef;
  var send = function(blob){
    uploadFile2(blob, function(name){
      MASKS[ref.name] = name;
      renderTiles(); closeMask();
    });
  };
  if (fin.toBlob) fin.toBlob(send, "image/png");
  else send(dataURLtoBlob(fin.toDataURL("image/png")));
};
function uploadFile2(blob, done){
  fetch("/api/upload?kind=mask", {method:"POST",
      headers:{"Content-Type":"application/octet-stream"}, body:blob})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(o){
      if (!o.ok) { $("maskmsg").textContent = o.j.error || "遮罩没存上。"; return; }
      $("maskmsg").textContent = "";
      done(o.j.name);
    })
    .catch(function(){ $("maskmsg").textContent = "网络错误，再试一次。"; });
}
function dataURLtoBlob(u){
  var ps = u.split(","), mime = (ps[0].match(/:(.*?);/) || [,"image/png"])[1];
  var bin = atob(ps[1]), arr = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], {type:mime});
}

/* ---- 提交 / 进度 ---- */
function setBusy(on){
  $("go").disabled = on; $("stop").style.display = on ? "" : "none";
  $("bar").style.display = on ? "block" : "none";
  $("pick").disabled = on;
}
function poll(id){
  curTask = id;
  timer = setInterval(function(){ tickTask(id); }, 1500);
}
function tickTask(id){
  fetch("/api/task?id=" + id)
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (j.progress) {
        $("bar").firstChild.style.width =
          Math.round(100 * j.progress[0] / Math.max(1, j.progress[1])) + "%";
        $("msg").textContent = "生成中 " + j.progress[0] + " / " + j.progress[1];
      } else if (j.state === "running") {
        $("msg").textContent = "生成中…（首图要加载权重，可能先等一会儿）";
      }
      if (j.echo && j.echo.length) $("echo").textContent = j.echo.join("\n");
      $("tail").textContent = (j.tail || []).slice(-1).join(" ");
      if (j.state !== "running") {
        clearInterval(timer); timer = null; setBusy(false);
        loadCaps();               /* 电脑端可能换了模型：档位判据刷新一次 */
        if (j.state === "done" && (j.files || []).length) {
          $("msg").textContent = "";
          $("tail").textContent = "";
          $("art").innerHTML = "";
          var url = "/api/artifact?task=" + id + "&name=" +
                    encodeURIComponent(j.files[0]);
          var im = new Image();
          im.className = "art"; im.src = url;
          var a = document.createElement("a");
          a.className = "dl"; a.href = url; a.download = j.files[0];
          a.textContent = "下载原图";
          $("art").appendChild(im);
          $("art").appendChild(document.createElement("br"));
          $("art").appendChild(a);
          /* 一次多张（×n）：第一张内嵌大图照旧，其余列下载链接（W 2026-10-13 批准）*/
          if (j.files.length > 1) {
            var line = document.createElement("div");
            line.className = "hint";
            line.textContent = "另 " + (j.files.length - 1) + " 张：";
            for (var k = 1; k < j.files.length; k++) {
              var a2 = document.createElement("a");
              a2.href = "/api/artifact?task=" + id + "&name=" +
                        encodeURIComponent(j.files[k]);
              a2.download = j.files[k];
              a2.textContent = j.files[k];
              line.appendChild(a2);
              if (k < j.files.length - 1)
                line.appendChild(document.createTextNode(" · "));
            }
            $("art").appendChild(line);
          }
        } else if (j.state === "cancelled") {
          $("msg").textContent = "已停止。";
        } else {
          $("msg").textContent = j.error || "生成失败。";
        }
      }
    })
    .catch(function(){});
}
/* 刷新恢复（16 §26 第 6 条）：任务状态本来只活在页面内存里，手机刷新一次就
   "丢"——进度看不见、「停止」按不了、再提交吃 409。caps 现在带本设备运行中
   的任务，加载时把这几件恢复出来；之后的进度继续走 poll 轮询。 */
function resumeTask(t){
  var p = t.progress;
  $("echo").textContent = (t.echo || []).join("\n");
  $("tail").textContent = (t.tail || []).slice(-1).join(" ");
  if (p) {
    $("bar").firstChild.style.width =
      Math.round(100 * p[0] / Math.max(1, p[1])) + "%";
  }
  $("msg").textContent = p ? ("生成中 " + p[0] + " / " + p[1])
                           : "生成中…（刷新后已恢复）";
  setBusy(true);
  poll(t.id);
}
$("go").onclick = function(){
  var p = $("prompt").value.trim();
  if (!p) { $("msg").textContent = "先写点提示词。"; return; }
  $("msg").textContent = "提交中…"; $("art").innerHTML = "";
  $("echo").textContent = "";
  /* 一次出几张（标题旁的小控件）：夹到 1..8 并把生效值回写框；服务端仍会再夹一次 */
  var cnt = parseInt($("cnt").value, 10);
  if (!(cnt >= 1)) cnt = 1;
  if (cnt > 8) cnt = 8;
  $("cnt").value = cnt;
  var body = {prompt:p, count:cnt};
  if (REFS.length) {
    body.refs = REFS.map(function(x){ return x.name; });
    body.ref_mode = MODE;
    if (MODE === "edit") {
      body.strength_tier = TIER;
      if (REFS[0] && MASKS[REFS[0].name]) body.mask = MASKS[REFS[0].name];
    }
  }
  fetch("/api/image", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(o){
      if (!o.ok) { $("msg").textContent = o.j.error || "提交失败。"; return; }
      $("msg").textContent = "已提交，等电脑端开跑…";
      setBusy(true); poll(o.j.task_id);
    })
    .catch(function(){ $("msg").textContent = "网络错误，再试一次。"; });
};
$("stop").onclick = function(){
  if (!curTask) return;
  fetch("/api/cancel", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:curTask})});
};
</script>
</body></html>
"""


def render(authed):
    """按配对状态出整页（`__AUTHED__` 是唯一一处替换）。"""
    return _PAGE_HTML.replace("__AUTHED__", "1" if authed else "0")
