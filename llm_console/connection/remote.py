# -*- coding: utf-8 -*-
"""llm_console.connection.remote — 远程访问服务（2026-10-11 一期，99 K1）。

**远程层是唯一对外监听面**：8080（llama-server）/ 8081（代理）永远 loopback，
本模块是唯一绑定非回环地址的服务（一期绑定 0.0.0.0，局域网直连场景）。

安全三件套（服务端硬行为，不靠开关拼，缺一不开）：
  1. **设备配对**：手机输 6 位一次性配对码（5 分钟有效）→ 电脑端弹窗确认 →
     下发长效 token（Cookie）。本机只存 token 的 SHA-256（secrets.json，
     哈希不可逆推，secrets.json 外流也冒充不了设备）。**限次按码不按 IP**
     （2026-10-12 改，坑 231）：同一个码被试错满 `PAIR_TRIES_MAX` 次即作废、
     要电脑端刷新重出 —— 按来源 IP 连坐会让校园网 NAT 同出口的人互相牵连。
  2. **HTTPS 自签**（默认开）：证书在 `core/remote_cert.py`；PowerShell 不可用
     时回退明文 HTTP 并说明（不开死开关，文档劝走隧道）。
  3. **审计**：`remote_audit.jsonl`（JSONL 追加 + 超 1MB 轮转保尾 500 条），
     只记元数据（时间 / IP / 动作 / 成败 / 耗时），不含密钥与请求体。

鉴权口径：`127.0.0.1` / `::1` 豁免（本机浏览器无感）；其余来源一律要求
已配对设备的 Cookie，未配对只能看到配对页与 /health。

端点白名单（一期生图三件；二期加上传与能力查询）：`/health`（免鉴权）、
`/`（Web 单页）、`/api/pair`、`/api/image`、`/api/task`、`/api/cancel`、
`/api/artifact`、`/api/upload`、`/api/caps`。**绝不暴露**文件系统、
配置与密钥相关任何端点（坑 209 的教训）。

二期上传面（2026-10-11，W 拍板：附件 / 模式 / 强度 / 遮罩进网页端）是独立的
安全面，服务端硬行为：单文件 20MB 上限、**魔数嗅探判真实格式**（扩展名不是
证据，坑 209⑧ 纪律）、落盘名 = 随机 hex（客户端文件名永不进路径）、登记
"哪台设备传的"（跨设备引用拒收）、TTL + 总量上限惰性清理。
"""

import hashlib
import datetime as _datetime
import ipaddress
import shutil
import http.cookies
import http.server
import json
import os
import secrets as _pysecrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse

from ..core import config, secrets
from ..core.config import APP_DIR, atomic_write_json
from ..core import remote_cert

AUDIT_PATH = os.path.join(APP_DIR, "remote_audit.jsonl")
AUDIT_ROTATE_BYTES = 1024 * 1024
AUDIT_KEEP = 500

PAIR_TTL_S = 300          # 配对码有效期（W 拍板：5 分钟）
# 同一个配对码被试错这么多次 → **该码作废**（W 2026-10-12 定"按码限次"）：
# 旧的"按来源 IP 连坐"在校园网 NAT 下会一人撞码、同出口所有人被封 5 分钟。
# 防撞强度：每码最多 5 次尝试 + 5 分钟一次性 + 电脑端弹窗确认，三道闸。
PAIR_TRIES_MAX = 5
# W 2026-10-12 逐字批准的两句原话（拟稿：手机端 B / 设置页 A）
PAIR_BURNED_MSG = "配对码已被试错作废——在电脑端刷新配对码再试。"
PAIR_BURNED_PAGE = "上一码被连续试错，已作废——点「刷新配对码」重出"
PAIR_BAD_MSG = "配对码不对或已过期，请在电脑端刷新后重试。"

# TLS 握手超时（秒）：握手在工作线程里做（不再卡 accept 循环，见 _TLSWorkerMixin），
# 但也要给个上限——半开连接最多占本连接一个线程这么久。
HANDSHAKE_TIMEOUT_S = 10

COOKIE_NAME = "remote_token"

# ---- 二期上传面（参考图 / 遮罩）----
UPLOAD_DIR = os.path.join(APP_DIR, "remote_uploads")
UPLOAD_MAX_BYTES = 20 * 1024 * 1024   # 单文件上限（W 2026-10-11 拍板 20MB）
UPLOAD_TTL_S = 2 * 3600               # 传上来多久没用就清（惰性，不起线程）
UPLOAD_MAX_FILES = 20                 # 登记总量上限（超了逐最旧）
UPLOAD_KINDS = ("ref", "mask")

# 内嵌 Web 单页（一期：配对 + 远程文生图 + 进度 + 产物回显；二期 2026-10-11：
# 附件上传 + 带图两档 + 重绘强度 + 涂抹遮罩，W 拍板并入本轮）。
# 为什么内嵌成源码常量而不是外部文件：单文件 exe 里 __file__ 指向临时解包目录
# （坑 62 同族），带文件就得再算 _MEIPASS —— 纯文本常量最省事，也不用进
# 打包钩子。页面无外部 CDN、无构建链（校园网/离线可用），手机浏览器优先。
# __AUTHED__ 由 handler 按配对状态替换（1 = 已配对/本机，直接进主页）。
# raw 字符串：JS 里的反斜杠（如 join("\n")）不能被 Python 先吃一道。
# 控件的标签 / 置灰原因 / 档表全部来自 /api/caps（core 现算，与 GUI 同源），
# 页面里不写死第二份判据；用户可见文案一律 W 2026-10-11 批准清单内或 core 复用。
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
        min-height:0;padding:6px}
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
<h2>远程生图</h2>
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
</div>
<div id="maskui" style="display:none">
<div id="cvarea"><div id="cvwrap"><canvas id="cvbase"></canvas><canvas id="cvover"></canvas></div></div>
<div id="maskbar">
<p class="hint">涂过的地方交给模型重绘，没涂的地方保持不变；一点不涂 = 不用遮罩。</p>
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
function show(which){
  $("pair").style.display = which === "pair" ? "block" : "none";
  $("home").style.display = which === "home" ? "block" : "none";
  if (which !== "mask") $("maskui").style.display = "none";
}
if (AUTHED) { show("home"); loadCaps(); } else show("pair");
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
      renderTiles(); syncZones();
    };
    d.onclick = function(){ openMask(rf); };
    box.appendChild(d);
  });
  syncZones();
}

/* ---- 涂抹遮罩（全屏页：浏览器里只有图片 + 底下功能条，W 2026-10-12 定；
   双层画布：底图画一次不再动、红色叠加层整层重画——与 GUI maskedit 同一条
   "盖一层红色，别实时合成"口径。笔画坐标与笔宽**归一化**存（0~1），渲染时
   乘当前画布尺寸：转屏 / 窗口缩放 / 手机键盘弹出时原地重铺、笔画不丢；
   画布两维适配视口（只按宽度算会被 body 的 max-width 裁掉，坑 219）。---- */
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
  function posN(e){
    var over = $("cvover"), r = over.getBoundingClientRect();
    return [(e.clientX - r.left) / (r.width || 1),
            (e.clientY - r.top) / (r.height || 1)];
  }
  $("cvover").addEventListener("pointerdown", function(e){
    e.preventDefault();
    if (this.setPointerCapture && e.pointerId !== undefined) {
      try { this.setPointerCapture(e.pointerId); } catch (x) {}
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
    if (!drawing) return;
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
     会从上一笔的终点连一条飞线过来。*/
  window.addEventListener("pointerup", function(){ drawing = false; });
  window.addEventListener("pointercancel", function(){ drawing = false; });
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
  var body = {prompt:p};
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

# 本地地址显示缓存（设置页"手机怎么连"用；IP 变了重开设置页会现算）
_ip_cache = {"ts": 0.0, "ips": []}


def local_ips():
    """本机可达的地址列表（设置页显示"手机访问哪个地址"）：IPv4 局域网地址
    + **全球 IPv6**（2xxx，教育网/手机流量直连的候选），不含回环 / 链路本地
    （fe80）/ 唯一本地（fc00::/7）/ 基准测试段（198.18/15，常见于代理 TUN
    的 fake-ip）——那些出了这台机器都没有意义。

    IPv4 用 UDP connect 探主地址（不发包）；IPv4 全清单与全球 IPv6 走
    getaddrinfo（**v6 靠它**，gethostbyname_ex 只吐 v4）。结果缓存 10 秒
    —— 设置页渲染与轮询会反复调。
    """
    now = time.time()
    if now - _ip_cache["ts"] < 10 and _ip_cache["ips"]:
        return list(_ip_cache["ips"])
    cand = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            cand.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for fam, _t, _p, _cn, sa in socket.getaddrinfo(
                socket.gethostname(), None):
            if fam not in (socket.AF_INET, socket.AF_INET6):
                continue
            cand.append(str(sa[0]))
    except Exception:
        pass

    def _ok(ip):
        """过滤没意义的地址：回环 / 链路本地 / 基准段（代理 TUN 的 fake-ip
        常在 198.18/15）/ 唯一本地 v6（fc00::/7）——出了这台机器都不可达。"""
        if ":" in ip:
            low = ip.lower().split("%")[0]
            return low.startswith(("2", "3")) and not low.startswith("2001:db8")
        return not ip.startswith(("127.", "169.254.", "198.18.", "198.19."))

    ips = sorted({i.split("%")[0] for i in cand if _ok(i)})
    if not ips:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127."):
                    ips.append(ip)
        except Exception:
            pass
    _ip_cache.update(ts=now, ips=ips)
    return list(ips)


def _is_loopback(ip):
    return str(ip) in ("127.0.0.1", "::1")


# 跨源那道防线（2026-10-12）：回环是**豁免鉴权**的，于是"本机浏览器里的一个恶意
# 网页"天然拥有全部远程接口 —— 跨源 fetch 打 http://127.0.0.1:<port>/api/image，
# 源地址就是回环，`_auth_ok()` 直接放行（实测 text/plain 的简单请求不回预检，
# 200 起了真任务）。两条规矩堵它：Host 白名单（顺带堵 DNS rebinding：外部域名
# 解析到 127.0.0.1 时请求头里的 Host 是那个域名）+ POST 只收非"简单请求"的
# Content-Type（跨源发这两种类型浏览器必先发 OPTIONS 预检，而我们从不回 CORS 头）。
_HOST_ALLOW = None


def _host_allowed(raw):
    """Host 头可不可信：IP 字面量 / localhost / 本机自己的主机名。"""
    h = str(raw or "").strip().lower()
    if h.startswith("["):                       # [::1]:8443
        h = h.split("]", 1)[0][1:]
    elif h.count(":") == 1:                     # 1.2.3.4:8443 / name:8443
        h = h.split(":", 1)[0]
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True                             # 按 IP 访问，Host 就是这个 IP
    except Exception:
        pass
    if h in ("localhost", "localhost.localdomain"):
        return True
    global _HOST_ALLOW
    if _HOST_ALLOW is None:
        own = set()
        for fn in (socket.gethostname, socket.getfqdn):
            try:
                n = str(fn() or "").strip().lower()
            except Exception:
                n = ""
            if n:
                own.add(n)
                own.add(n.split(".", 1)[0])
        _HOST_ALLOW = own
    return h in _HOST_ALLOW


# POST 只收这两种（都不是 CORS 的"简单请求"类型 ⇒ 跨源必先预检 ⇒ 被浏览器拦下）
_POST_CTYPES = ("application/json", "application/octet-stream")
CROSS_ORIGIN_MSG = ("这个请求的来路不对，已拒绝：请从 设置 → 远程访问 给的网址"
                    "重新打开这一页。")


def ip_label(ip):
    """给 local_ips 里的地址定性（纯函数，设置页按它排序与标注）：

    `tailscale` = 100.64/10（Tailscale 的 CGNAT 段）或 fd7a:115c:a1e0::/48
    （Tailscale 的 v6 ULA）——装了 Tailscale 后手机跨网访问的首选；
    `v6` = 全球单播；`v4` = 其余 IPv4。"""
    if ":" in str(ip):
        return "tailscale" if str(ip).lower().startswith(
            "fd7a:115c:a1e0") else "v6"
    try:
        a = [int(x) for x in str(ip).split(".")]
        if a[0] == 100 and 64 <= a[1] <= 127:
            return "tailscale"
    except Exception:
        pass
    return "v4"


def tailscale_installed():
    """本机有没有装 Tailscale 客户端（PATH + 两个常见安装位置；只找不启动
    ——穿透工具一律外部程序，99 K1 既定纪律）。"""
    if shutil.which("tailscale"):
        return True
    for p in (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                           "Tailscale", "tailscale.exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                           "Tailscale", "tailscale.exe")):
        try:
            if os.path.isfile(p):
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# 审计（JSONL 追加；轮转 = 保尾部整份**纯文本**重写，见 audit() 里那段为什么不用
# atomic_write_json）
# ---------------------------------------------------------------------------

_audit_lock = threading.Lock()


def audit(action, ip, ok, detail=""):
    """记一条审计（只记元数据；调用方保证 detail 不含密钥 / 请求体）。"""
    line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "action": action, "ip": str(ip), "ok": bool(ok),
                       "detail": str(detail)[:200]},
                      ensure_ascii=False)
    try:
        with _audit_lock:
            rotate = (os.path.isfile(AUDIT_PATH)
                      and os.path.getsize(AUDIT_PATH) > AUDIT_ROTATE_BYTES)
            if rotate:
                try:
                    with open(AUDIT_PATH, "r", encoding="utf-8") as f:
                        tail = f.readlines()[-AUDIT_KEEP:]
                    # **纯文本写回**，不能用 atomic_write_json：这份文件是 JSONL
                    # （一行一条），而 atomic_write_json 写的是 `indent=2` 的 JSON
                    # 数组 —— 轮转一次之后整个文件就不是 JSONL 了，read_audit 逐行
                    # loads 拿到的是**字符串**，设置页「查看审计…」的 r.get("t") 当场
                    # AttributeError（保留的 500 条也全读不出来），再轮转一次转义还
                    # 指数增长。原子性靠 .keep + os.replace 自己保证。
                    with open(AUDIT_PATH + ".keep", "w", encoding="utf-8") as f:
                        f.writelines(ln if ln.endswith("\n") else ln + "\n"
                                     for ln in tail)
                    os.replace(AUDIT_PATH + ".keep", AUDIT_PATH)
                except Exception:
                    pass
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass                      # 审计写失败不拦业务；诊断页可见目录权限问题


def read_audit(limit=200):
    """读最近 limit 条审计（设置页"查看"用；文件按行倒序取尾）。"""
    try:
        with _audit_lock:
            with open(AUDIT_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def clear_audit():
    try:
        with _audit_lock:
            with open(AUDIT_PATH, "w", encoding="utf-8") as f:
                f.write("")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 上传登记（参考图 / 遮罩）：内存登记 + 落盘随机名 + 惰性清理
# ---------------------------------------------------------------------------

_uploads_lock = threading.Lock()
_uploads = {}          # name -> {"device_id", "kind", "ts", "path"}


def _sniff_image_ext(raw):
    """魔数 → 真实格式扩展名（认不出来 = 空串）。**只信字节魔数，不信客户端
    的扩展名与 Content-Type**（坑 209⑧ 纪律）。四种 = GUI 附件过滤器同一套
    （ui/chat._IMG_FILTER：png / jpg / jpeg / webp / bmp）。"""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:2] == b"BM":
        return ".bmp"
    return ""


def _sweep_uploads():
    """惰性清理（持锁调）：过 TTL 的、超出总量上限的（逐最旧），连登记带文件。"""
    now = time.time()
    gone = [n for n, u in _uploads.items() if now - u["ts"] > UPLOAD_TTL_S]
    if len(_uploads) - len(gone) > UPLOAD_MAX_FILES:
        rest = sorted((u["ts"], n) for n, u in _uploads.items() if n not in gone)
        gone += [n for _t, n in rest[:len(_uploads) - UPLOAD_MAX_FILES]]
    for n in gone:
        u = _uploads.pop(n, None)
        if u:
            try:
                os.remove(u["path"])
            except Exception:
                pass


def _mask_to_gray_png(raw):
    """遮罩落盘归一：任何输入 → **L 模式 PNG**（坑 218）。

    网页 canvas 导出的是 RGBA（笔画边缘带抗锯齿灰阶），而 GUI maskedit 落盘
    的遮罩是 L 模式 PNG——引擎 `--mask` 只在后者上真机实测过（坑 214 数据），
    不引入第二种口径。Pillow 就是既有运行期依赖（01 §1）；解不开 = 坏图拒收。
    抗锯齿灰阶**保留**（convert("L") 不二值化）：软边缘对引擎是混合权重，
    比硬边更贴近"涂过的地方重绘"的语义。参考图不归一——GUI 同样原样交文件。
    """
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            buf = io.BytesIO()
            im.convert("L").save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        return None


def upload_store(device_id, kind, raw):
    """存一份上传 → `(登记名, 错误文案)`。错误非空 = 没存。

    落盘名 = 随机 hex + **魔数定出来的**扩展名；客户端给的任何名字都不进路径
    （坑 209⑤ 同族）。目录在程序目录 `remote_uploads/`，与台账 / 审计同级。
    遮罩（kind="mask"）落盘前经 `_mask_to_gray_png` 归一成 L 模式 PNG。
    """
    if kind not in UPLOAD_KINDS:
        return "", "上传类型不对。"
    if not raw:
        return "", "这个文件是空的，换一张。"
    if len(raw) > UPLOAD_MAX_BYTES:
        return "", ("这张图 %.1fMB，超过 %dMB 上限——先压小一点。"
                    % (len(raw) / 1048576.0, UPLOAD_MAX_BYTES // 1048576))
    ext = _sniff_image_ext(raw)
    if not ext:
        return "", "这个文件不是支持的图片（认 PNG / JPG / WEBP / BMP）。"
    if kind == "mask":
        norm = _mask_to_gray_png(raw)
        if norm is None:
            return "", "这个文件不是支持的图片（认 PNG / JPG / WEBP / BMP）。"
        raw, ext = norm, ".png"
    with _uploads_lock:
        _sweep_uploads()
        if len(_uploads) >= UPLOAD_MAX_FILES:
            return "", "附件太多了，删几张再传。"
        name = _pysecrets.token_hex(8) + ext
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            path = os.path.join(UPLOAD_DIR, name)
            with open(path, "wb") as f:
                f.write(raw)
        except Exception as e:
            # 原话（含本机绝对路径与用户名）只进本机审计，不回给手机端（坑 209⑥）
            audit("upload", device_id, False, "save failed: %s: %s"
                  % (type(e).__name__, e))
            return "", "附件存不下来：电脑端的磁盘或权限有问题，请在电脑端看一下。"
        _uploads[name] = {"device_id": device_id, "kind": kind,
                          "ts": time.time(), "path": path}
    audit("upload", device_id, True, "%s %dB" % (kind, len(raw)))
    return name, ""


def upload_paths(device_id, names, kind):
    """登记名列表 → 本地路径列表。任何一张对不上（过期 / 不是这台设备的 /
    类型不对 / 文件没了）都整单拒绝："附件已过期，重新选一次。"

    设备判据与产物取回同口径：loopback（device_id == "local"）全可见。
    """
    out = []
    with _uploads_lock:
        _sweep_uploads()
        for n in names:
            u = _uploads.get(str(n or ""))
            if (not u or u["kind"] != kind
                    or (device_id != "local" and u["device_id"] != device_id)
                    or not os.path.isfile(u["path"])):
                return [], "附件已过期，重新选一次。"
            out.append(u["path"])
    return out, ""


def uploads_reset():
    """清空登记与目录（服务重启 / 测试用；文件尽力删，删不掉不留脏登记）。"""
    with _uploads_lock:
        for u in list(_uploads.values()):
            try:
                os.remove(u["path"])
            except Exception:
                pass
        _uploads.clear()


def _sweep_orphan_uploads():
    """服务启动时清孤儿文件：登记是内存态，重启即空 —— 目录里比 TTL 还老的
    文件必然没人再引用（新文件留着：可能是并行实例刚收的，宁可放过）。"""
    try:
        now = time.time()
        for n in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, n)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > UPLOAD_TTL_S:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass                      # 目录还不存在 = 没孤儿，正常路径


class _RemoteHandler(http.server.BaseHTTPRequestHandler):
    """远程访问端点。路由按方法表分发（批次 3 在 _ROUTE_POST 里加生图三件）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                      # 静默（审计自己记，标准库日志不要）

    # ---- 工具 ----
    def _json(self, code, obj, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass                  # 客户端断开无处可说（R-46 同款纪律）

    def _client_ip(self):
        ip = str(self.client_address[0])
        if ip.startswith("::ffff:"):       # 双栈下 IPv4 连接是 v4-mapped 形式，
            ip = ip[7:]                    # 归一回 IPv4，回环豁免 / 审计 / 白名单判据才对（坑 K3）
        return ip

    def _device(self):
        """Cookie 里的设备（没配对 / token 不对 = None）。

        **一个请求内只查一次**（`_dev_reset` 在 do_GET / do_POST 开头重置）：
        `_auth_ok()` 与 `_caller_id()` 都会问，而 `find_remote_device_by_hash`
        是整份 secrets.json 读盘 + 遍历 —— 手机端 1.5s 一轮的任务轮询下这是持续的
        无谓 IO。缓存**绝不跨请求**：handler 实例在 keep-alive 连接上是复用的，
        跨请求缓存会让"吊销设备"在那条连接上继续有效。
        """
        if getattr(self, "_dev_done", False):
            return self._dev_val
        self._dev_done = True
        dev = None
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = c.get(COOKIE_NAME)
            if morsel:
                h = hashlib.sha256(morsel.value.encode("utf-8")).hexdigest()
                dev = secrets.find_remote_device_by_hash(h)
                if dev is not None:
                    self.server.touch_seen(dev["id"])
        except Exception:
            dev = None
        self._dev_val = dev
        return dev

    def _dev_reset(self):
        self._dev_done = False
        self._dev_val = None

    def _auth_ok(self):
        """127.0.0.1 豁免；其余必须已配对。"""
        if _is_loopback(self._client_ip()):
            return True
        return self._device() is not None

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _cross_origin(self, is_post=False):
        """跨源防线（见 `_host_allowed` 那段为什么必须有）→ "" 放行 / 短原因进审计。"""
        if not _host_allowed(self.headers.get("Host", "")):
            return "bad-host"
        if is_post:
            ct = str(self.headers.get("Content-Type", "") or "")
            if ct.split(";")[0].strip().lower() not in _POST_CTYPES:
                return "bad-ctype"
        return ""

    # ---- GET ----
    def do_GET(self):
        t0 = time.time()
        self._dev_reset()
        path = self.path.split("?")[0].rstrip("/") or "/"
        bad = self._cross_origin()
        if bad:
            self.server.audit_req("get", self._client_ip(), False,
                                  "%s %s" % (path, bad))
            return self._json(403, {"error": CROSS_ORIGIN_MSG})
        if path == "/health":
            return self._json(200, {"ok": True, "service": "llm-chat-remote",
                                    "https": bool(self.server.https_on)})
        if path == "/":
            ok = self._auth_ok()
            self.server.audit_req("page", self._client_ip(), ok)
            if ok:
                return self._page_home()
            return self._page_pair()
        if not self._auth_ok():
            self.server.audit_req("get", self._client_ip(), False, path)
            return self._json(401, {"error": "未配对设备，请先在网页上完成配对。"})
        self.server.audit_req("get", self._client_ip(), True, path)
        fn = self.server.route_get.get(path)
        if fn:
            return fn(self)
        return self._json(404, {"error": "没有这个端点: %s" % path})

    # ---- POST ----
    def do_POST(self):
        t0 = time.time()
        self._dev_reset()
        path = self.path.split("?")[0].rstrip("/")
        bad = self._cross_origin(is_post=True)
        if bad:
            # 类型不对就**不去读那个 body**（长度由对方说了算，读它等于把内存交出去）；
            # 但 keep-alive 下"不读又不关"会让这条连接上的后续请求全错位，所以直接关。
            self.close_connection = True
            self.server.audit_req("post", self._client_ip(), False,
                                  "%s %s" % (path, bad))
            return self._json(403, {"error": CROSS_ORIGIN_MSG})
        if path == "/api/pair":
            return self._do_pair()
        if not self._auth_ok():
            self.server.audit_req("post", self._client_ip(), False, path)
            return self._json(401, {"error": "未配对设备，请先在网页上完成配对。"})
        self.server.audit_req("post", self._client_ip(), True, path)
        fn = self.server.route_post.get(path)
        if fn:
            return fn(self)
        return self._json(404, {"error": "没有这个端点: %s" % path})

    # ---- 配对（唯一的免鉴权 POST；按码限次在 server 侧，见 RemoteServer.pair_allowed）----
    def _do_pair(self):
        ip = self._client_ip()
        payload = self._read_json() or {}
        verdict, msg = self.server.pair_allowed(ip)
        if not verdict:
            self.server.audit_req("pair", ip, False, "blocked")
            return self._json(429, {"error": msg})
        code = str(payload.get("code", "")).strip()
        name = str(payload.get("name", "")).strip()[:20]
        if not self.server.take_pair_code(code):
            # 匹配即作废在这一次持锁里做完（take_pair_code）：分成"先查再作废"两步时，
            # 同一个码的并发请求可以都查到匹配 —— 一次性码就不一次性了。
            burned = self.server.pair_missed(ip)
            self.server.audit_req("pair", ip, False,
                                  "bad code" + ("; burned" if burned else ""))
            if burned:
                # 这一码试错满额刚作废：说清去哪儿重出（W 2026-10-12 逐字批准）
                return self._json(429, {"error": PAIR_BURNED_MSG})
            return self._json(403, {"error": PAIR_BAD_MSG})
        token = _pysecrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        dev_name = name or ("浏览器设备 " + str(ip))
        if not self.server.confirm_pair({"ip": ip, "name": dev_name,
                                         "agent": str(self.headers.get(
                                             "User-Agent", ""))[:80]}):
            self.server.audit_req("pair", ip, False, "denied on PC")
            return self._json(403, {"error": "电脑端拒绝了这次配对。"})
        did = secrets.add_remote_device(dev_name, token_hash,
                                        time.strftime("%Y-%m-%d %H:%M"))
        self.server.audit_req("pair", ip, True, "device " + did)
        # HTTPS 在跑时补 `Secure`：这个 token 是长期凭证（一年），少了这个标志，
        # 之后降级成 HTTP 用时它会明文过网（同一部手机、同一个 Wi-Fi 就够嗅了）。
        cookie = ("%s=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax%s"
                  % (COOKIE_NAME, token,
                     "; Secure" if getattr(self.server, "https_on", False) else ""))
        return self._json(200, {"ok": True, "device_id": did, "name": dev_name},
                          extra_headers=[("Set-Cookie", cookie)])

    # ---- 页面 ----
    def _html(self, code, text):
        data = text.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _page(self, authed):
        self._html(200, _PAGE_HTML.replace("__AUTHED__", "1" if authed else "0"))

    def _page_pair(self):
        self._page(False)

    def _page_home(self):
        self._page(True)


class _TLSWorkerMixin:
    """TLS 握手放在**本连接的请求线程**里做，不占 accept 循环（16 §26 第 5 条）。

    常规做法是把监听 socket 包成 SSL（`ctx.wrap_socket(监听socket)`）——代价是
    每个新连接的握手都发生在 `accept()` 里，也就是 `serve_forever` 的**唯一循环
    线程**：一个弱网手机 / 半开连接 / 扫端口的一次连接不完成握手，所有新连接一起
    干等，等多久由对方决定（原来还没有超时）。本机是 IPv6 直连暴露面，扫端口是
    常态，所以今天把它换掉：accept 出的是明文 socket，握手在本连接自己的工作线程
    里做，另加一个握手超时兜底；握手失败静默丢弃（与改前同语义：不 handle_error、
    不打堆栈）。

    实现 = 在 stdlib 的 `process_request_thread` 前面加一步 wrap（其余照抄）。
    `wrap_socket` 内部会 `sock.detach()`（fd 归属转给 SSLSocket，CPython 原话
    "Now SSLSocket is responsible for closing the file descriptor"），所以 finally
    里 `shutdown_request` 关的就是包装后的那个；握手失败时 CPython 已自行关掉它，
    这里 `close()` 只是兜底（detach 后的原对象再 close 是空操作）。
    """

    ssl_ctx = None

    def process_request_thread(self, request, client_address):
        if self.ssl_ctx is not None:
            try:
                request.settimeout(HANDSHAKE_TIMEOUT_S)
                request = self.ssl_ctx.wrap_socket(request, server_side=True)
                request.settimeout(None)      # 建连后的读写口径与改前一致（无超时）
            except Exception:
                try:
                    request.close()
                except Exception:
                    pass
                return
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class _V6MixServer(_TLSWorkerMixin, http.server.ThreadingHTTPServer):
    """双栈监听（AF_INET6 + IPV6_V6ONLY=0）：一个端口同时接 IPv6 与 IPv4
    （v4 走 v4-mapped 地址）。校园网手机直连走**全球 IPv6**（教育网 2001:da8
    常见），只绑 AF_INET 时 v6 包根本到不了 —— 这是 2026-10-11 本机诊断出的
    真缺口。Windows 建套接字默认 V6ONLY=1，必须显式关；系统没有 v6 栈时
    _make_httpd 回退 AF_INET。"""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6,
                                   socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        http.server.HTTPServer.server_bind(self)


class _PlainServer(_TLSWorkerMixin, http.server.ThreadingHTTPServer):
    """本机没有可用 IPv6 栈时的 IPv4 回退（TLS 握手同样在工作线程里做）。"""


def _make_httpd(port, handler):
    """优先双栈；本机没有可用 IPv6 栈（bind 失败）才退回 IPv4。"""
    if socket.has_ipv6:
        try:
            return _V6MixServer(("", port), handler)
        except OSError:
            pass
    return _PlainServer(("", port), handler)


class RemoteServer:
    """远程访问服务：手机/平板浏览器 → 本机。

    只依赖 cfg 与注入的回调（不含 GUI 状态）：
      confirm_pair(info) — 配对确认（GUI 弹窗由 App 侧经 _ui_q 编排，坑 54）
      note_fn(msg)       — 给输出栏的话（人话）
    生图任务管理（RemoteJobs）在批次 3 经 `httpd` 挂钩进 handler，同 proxy 先例。
    """

    def __init__(self, cfg, note_fn=None, confirm_pair=None):
        self.cfg = cfg
        self.note = note_fn or (lambda msg: None)
        self.confirm_pair = confirm_pair or (lambda info: False)
        self.httpd = None
        self.thread = None
        self.https_on = False
        self.cert_fp = ""
        self.jobs = None              # RemoteJobs（App 侧装配；批次 3）
        # 配对状态（含按码限次），全程由 _pair_lock 保护
        self._pair_lock = threading.Lock()
        self._pair_code = ""
        self._pair_deadline = 0.0
        self._pair_tries = 0           # 当前码已被试错几次（满 PAIR_TRIES_MAX 作废）
        self._pair_burned = False      # 上一码被试错作废（新码一出即清）
        # 设备 last_seen 回写节流（内存态；重启即重来，无妨）
        self._seen_ts = {}
        # 路由表（批次 3 由 App 侧填充：/api/image / /api/task / /api/artifact）
        self.route_get = {}
        self.route_post = {}

    # ---- 生命周期 ----

    def start(self):
        port = int(self.cfg.get("remote_port", 8443) or 8443)
        want_https = bool(self.cfg.get("remote_https", True))
        httpd = _make_httpd(port, _RemoteHandler)
        httpd.daemon_threads = True
        httpd.cfg = self.cfg
        httpd.remote = self
        httpd.https_on = False
        httpd.touch_seen = self._touch_seen
        httpd.audit_req = self._audit_req
        httpd.confirm_pair = self.confirm_pair
        httpd.pair_allowed = self.pair_allowed
        httpd.pair_missed = self.pair_missed
        httpd.pair_code_matches = self.pair_code_matches
        httpd.consume_pair_code = self.consume_pair_code
        httpd.take_pair_code = self.take_pair_code
        httpd.route_get = self.route_get
        httpd.route_post = self.route_post
        if self.jobs is not None:
            self.route_post["/api/image"] = self._h_image
            self.route_post["/api/cancel"] = self._h_cancel
            self.route_get["/api/task"] = self._h_task
            self.route_get["/api/artifact"] = self._h_artifact
            # 二期：附件上传（参考图 / 遮罩）+ 能力查询（网页控件的判据来源）
            self.route_post["/api/upload"] = self._h_upload
            self.route_get["/api/caps"] = self._h_caps
            _sweep_orphan_uploads()
        if want_https:
            try:
                cert = remote_cert.ensure_cert()
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert["cert"], cert["key"])
                # 只把 ctx 挂给服务器对象，由每个请求线程自己握手（_TLSWorkerMixin）：
                # 在这里 wrap 监听 socket 的话，握手会占住 accept 循环（16 §26 第 5 条）。
                httpd.ssl_ctx = ctx
                httpd.https_on = True
                self.cert_fp = cert["fingerprint"]
                # 证书生成不再发主页提醒（W 2026-10-11：主页只留一条"远程服务
                # 已启动"）；指纹在 设置 → 远程访问 的「网址 ****」里可见。
            except remote_cert.RemoteCertError as e:
                self.note("HTTPS 未能启用：%s 明文 HTTP 已回退开启（仅建议局域网内使用）。" % e)
        scheme = "https" if httpd.https_on else "http"
        self.httpd = httpd
        self.https_on = httpd.https_on
        # poll_interval 0.05：关窗路径跟 proxy 同一条实测（默认 0.5 会拖慢退出）
        self.thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()
        # 启动提醒只由 App.__init__ 发一条（不带网址，W 2026-10-11 二轮）；这里
        # 若再发就是主页两条，且设置页「拨开关即生效」路径同样会触发。
        return True

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self.https_on = False

    def running(self):
        return self.httpd is not None

    def scheme(self):
        return "https" if self.https_on else "http"

    # ---- 配对码 ----

    def new_pair_code(self):
        """生成新的 6 位一次性配对码（覆盖旧的；设置页"刷新"按钮调它）。

        同时清零按码试错计数与"上一码已作废"标记 —— 刷新出一个新码，
        手机端与设置页的两句作废话术都该收掉。
        """
        code = "%06d" % _pysecrets.randbelow(1000000)
        with self._pair_lock:
            self._pair_code = code
            self._pair_deadline = time.time() + PAIR_TTL_S
            self._pair_tries = 0
            self._pair_burned = False
        return code

    def pair_code_left(self):
        """当前码还剩多少秒（0 = 没有 / 已过期）。"""
        with self._pair_lock:
            return max(0, int(self._pair_deadline - time.time())) \
                if self._pair_code else 0

    def pair_code_matches(self, code):
        with self._pair_lock:
            return (bool(self._pair_code) and self._pair_code == str(code).strip()
                    and time.time() < self._pair_deadline)

    def consume_pair_code(self):
        with self._pair_lock:
            self._pair_code = ""
            self._pair_deadline = 0.0

    def take_pair_code(self, code):
        """配对码**匹配即作废**，一次持锁里做完（原子 CAS）→ 是否匹配成功。

        `_do_pair` 原来是先 `pair_code_matches()` 再 `consume_pair_code()`，两次
        分别拿锁：同一个码的两个并发请求可以都判匹配、都拿到 token，"6 位一次性码"
        就名存实亡。作废仍在"电脑端确认"之前（拒绝也要重拿新码，防重放），语义不变。
        """
        code = str(code or "").strip()
        with self._pair_lock:
            if not (self._pair_code and self._pair_code == code
                    and time.time() < self._pair_deadline):
                return False
            self._pair_code = ""
            self._pair_deadline = 0.0
            return True

    def pair_allowed(self, ip):
        """此刻能不能试配对 → (能不能, 被拒时的原话)。

        按码限次（16 §26 第 7 条，W 2026-10-12 拍板）：判据不看来源 IP ——
        旧的"按 IP 连坐"在校园网 NAT 下会让同出口的人互相牵连（一个人撞码，
        整层楼的人 5 分钟里填对了也进不来）。现在只看**当前码**还有没有试错
        额度：码被试错作废后（`_pair_burned`）要等电脑端刷新新码。
        loopback 永放行：本机浏览器本来就不需要配对。
        """
        if _is_loopback(ip):
            return True, ""
        with self._pair_lock:
            if self._pair_burned and not self._pair_code:
                return False, PAIR_BURNED_MSG
            return True, ""

    def pair_missed(self, ip):
        """记一次"没对上"→ 这一码是否**刚被作废**（手机端与设置页各有一句原话）。

        只对"当前有码"的失败计数：没码 / 码已过期的失败没什么可烧的，不计数
        （也顺带让"码过期后才来试"的人不受任何影响）。loopback 不计数 ——
        本机自检的错码用例不该把用户手里真在用的码烧掉。
        """
        if _is_loopback(ip):
            return False
        now = time.time()
        with self._pair_lock:
            if not (self._pair_code and now < self._pair_deadline):
                return False
            self._pair_tries += 1
            if self._pair_tries >= PAIR_TRIES_MAX:
                self._pair_code = ""
                self._pair_deadline = 0.0
                self._pair_burned = True
                return True
            return False

    def pair_burned(self):
        """上一码是不是被试错作废了（设置页配对码行据此显示原话）。"""
        with self._pair_lock:
            return bool(self._pair_burned and not self._pair_code)

    # ---- 其他 ----

    def _touch_seen(self, dev_id):
        """last_seen 回写节流（60 秒一次；写 secrets.json 不便宜）。"""
        now = time.time()
        if now - self._seen_ts.get(dev_id, 0) < 60:
            return
        self._seen_ts[dev_id] = now
        secrets.touch_remote_device(dev_id, time.strftime("%Y-%m-%d %H:%M"))

    def _audit_req(self, action, ip, ok, detail=""):
        audit(action, ip, ok, detail)

    def devices(self):
        return secrets.remote_devices()

    def revoke(self, dev_id):
        ok = secrets.remove_remote_device(dev_id)
        if ok:
            audit("revoke", "", True, str(dev_id))
        return ok

    # ---- 生图端点（路由在 start() 里挂；jobs 为 None 时不挂 = 一期开关齐了才开）----

    def _caller_id(self, h):
        dev = h._device()
        return dev["id"] if dev else "local"

    def _h_image(self, h):
        payload = h._read_json() or {}
        refs = payload.get("refs")
        refs = [str(x) for x in refs][:UPLOAD_MAX_FILES] \
            if isinstance(refs, list) else []
        tid, err = self.jobs.submit(
            str(payload.get("prompt", "")), self._caller_id(h),
            refs=refs,
            ref_mode=str(payload.get("ref_mode", "") or ""),
            strength_tier=str(payload.get("strength_tier", "") or ""),
            mask=str(payload.get("mask", "") or ""))
        if err:
            busy = ("忙" in err or "进行" in err)
            return h._json(409 if busy else 400, {"error": err})
        return h._json(200, {"ok": True, "task_id": tid})

    def _h_upload(self, h):
        """附件上传（参考图 / 遮罩）：原始字节体（免 base64 膨胀），
        `?kind=ref|mask`。体积先按 Content-Length 拦一道，读进来后再按魔数
        验真实格式——两道都是服务端硬行为（16 §4 第 2 条的"独立安全面"）。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        kind = (qs.get("kind") or ["ref"])[0]
        try:
            n = int(h.headers.get("Content-Length", 0) or 0)
        except Exception:
            n = 0
        if n <= 0:
            return h._json(400, {"error": "这个文件是空的，换一张。"})
        if n > UPLOAD_MAX_BYTES:
            # 不读这份超大体：回 413 并直接关连接（读了也是白读，keep-alive
            # 的流同步交给"下一条连接"去建立）
            h.close_connection = True
            self._audit_req("upload", h._client_ip(), False, "oversize")
            return h._json(413, {"error": (
                "这张图 %.1fMB，超过 %dMB 上限——先压小一点。"
                % (n / 1048576.0, UPLOAD_MAX_BYTES // 1048576))})
        try:
            raw = h.rfile.read(n)
        except Exception:
            raw = b""
        name, err = upload_store(self._caller_id(h), kind, raw)
        if err:
            self._audit_req("upload", h._client_ip(), False, kind)
            code = 413 if "上限" in err else 400
            return h._json(code, {"error": err})
        return h._json(200, {"ok": True, "name": name})

    def _h_caps(self, h):
        """网页控件的判据来源：带图两档（ok / why / 标签）、默认档、强度档表、
        上传限制。全部由 core 现算（capability / media / sdprofile），与 GUI
        同源——网页不另写一份字面量（10 §5.0 的"界面照实显示"纪律）。"""
        from ..core import capability, media, providers
        from ..core.config import cfg_lock
        from ..core.media import resolve_img_files, resolve_img_model_path
        with cfg_lock():
            snap = dict(self.cfg)
        # 远程只走本地链路：GUI 停在云端模型上时 model_provider 不是 local，
        # resolve_modes 会拐进云端分支——判的就不是本机 sd-cli 的档了。
        snap["model_provider"] = providers.LOCAL
        family = ""
        try:
            diffusion = resolve_img_model_path(snap)
            if diffusion:
                family = str(resolve_img_files(snap, diffusion).get("family") or "")
        except Exception:
            family = ""
        modes = capability.resolve_modes(snap, family or None)
        tiers = [[t, media.TIER_LABEL[t], media.strength_value(snap, t)]
                 for t in media.STRENGTH_TIERS]
        # 刷新恢复（16 §26 第 6 条）：轮询状态本来只活在网页内存里，手机刷新 =
        # 进度看不见、「停止」按不了、再提交吃 409。把"这台设备正在跑的任务"
        # 随 caps 带给页面，由页面自己恢复进度条与停止按钮；归属判据与
        # /api/task 走同一条（`jobs.status`）——别的设备还是看不到。
        run = None
        jobs = getattr(self, "jobs", None)
        if jobs is not None:
            rt = jobs.running_task()
            if rt:
                st = jobs.status(rt["id"], self._caller_id(h))
                if st and st.get("state") == "running":
                    run = st
        return h._json(200, {
            "modes": {m: {"ok": bool(modes.get(m, {}).get("ok")),
                          "why": str(modes.get(m, {}).get("why") or ""),
                          "label": capability.MODE_LABEL.get(m, m),
                          "note": capability.MODE_NOTE.get(m, "")}
                      for m in capability.MODES},
            "default_mode": capability.default_ref_mode(snap, family or None),
            "strength_auto": bool(snap.get("img_strength_auto", True)),
            "tiers": tiers,
            "running": run,
            "upload": {"max_bytes": UPLOAD_MAX_BYTES,
                       "formats": "PNG / JPG / WEBP / BMP"},
        })

    def _h_task(self, h):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        tid = (qs.get("id") or [""])[0]
        st = self.jobs.status(tid, self._caller_id(h))
        if st is None:
            return h._json(404, {"error": "没有这个任务。"})
        return h._json(200, st)

    def _h_cancel(self, h):
        payload = h._read_json() or {}
        ok = self.jobs.cancel(str(payload.get("id", "")) or None,
                              self._caller_id(h))
        return h._json(200 if ok else 404, {"ok": ok} if ok
                       else {"error": "没有可取消的远程任务。"})

    def _h_artifact(self, h):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        tid = (qs.get("task") or [""])[0]
        name = (qs.get("name") or [""])[0]
        p = self.jobs.artifact_path(tid, self._caller_id(h), name)
        if not p:
            return h._json(404, {"error": "产物不存在（只开放当前会话产生的文件）。"})
        ctype = "image/png" if p.lower().endswith(".png") else \
            "application/octet-stream"
        try:
            with open(p, "rb") as f:
                data = f.read()
        except Exception:
            return h._json(404, {"error": "产物读不出来（可能被移动或删除）。"})
        try:
            h.send_response(200)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Content-Disposition",
                          "attachment; filename=\"%s\"" % name)
            h.end_headers()
            h.wfile.write(data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 远程生图任务管理器（一期：文生图 prompt-only；二期 2026-10-11：参考图 /
# 带图两档 / 重绘强度 / 遮罩，判据逐字复用 GUI 链路，W 拍板并入）
# ---------------------------------------------------------------------------

_LEDGER_LOCK = threading.RLock()   # 可重入：_ledger_add/_update 持锁调 _ledger_save（坑 12 教训）
LEDGER_PATH = os.path.join(APP_DIR, "remote_jobs.json")
LEDGER_KEEP = 200          # 台账保尾条数（防止无限涨）


def _ledger_load():
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        tasks = data.get("tasks") if isinstance(data, dict) else None
        return tasks if isinstance(tasks, list) else []
    except Exception:
        return []


def _ledger_save(tasks):
    with _LEDGER_LOCK:
        atomic_write_json(LEDGER_PATH, {"tasks": tasks[-LEDGER_KEEP:]})


def _ledger_add(entry):
    with _LEDGER_LOCK:
        tasks = _ledger_load()
        tasks.append(entry)
        _ledger_save(tasks)


def _ledger_update(task_id, **kw):
    with _LEDGER_LOCK:
        tasks = _ledger_load()
        for t in tasks:
            if t.get("id") == task_id:
                t.update(kw)
        _ledger_save(tasks)


class RemoteJobs:
    """远程生图的单槽任务管理器：提交 / 进度 / 取消 / 产物取回。

    **与 GUI 的互斥是双向的**（现查结论，见 15 §1 2026-10-11 行）：
      · 提交前问 `local_busy()`（App._any_busy：GUI 的生成 / 服务操作进行中就拒）；
      · App._any_busy 反向纳入 `self.busy()`（远程任务在跑，GUI 也被拦住）——
        两份 sd-cli 并发抢 8GB 显存 + 单槽共享状态互相覆盖，都比"排队"糟。
    预检与命令组装**逐字复用 GUI 链路的判据**（resolve_img_model_path /
    resolve_img_files / missing_slots / build_img_cmd），产物名加微秒与任务短号
    防同秒碰撞。"必闪退"档（modelreq 3 级）远程**直接拒绝**——本地 GUI 有人弹窗
    确认，远程没人可问。
    """

    def __init__(self, cfg, local_busy=None, note_fn=None):
        self.cfg = cfg
        self._local_busy = local_busy or (lambda: False)
        self.note = note_fn or (lambda msg: None)
        self._lock = threading.RLock()
        self._proc = None
        self._stop_flag = None
        self._task = None            # 运行中任务的内存态（进度 / tail）

    # ---- 状态 ----

    def busy(self):
        # **不抢锁**：`submit` 全程持 `_lock`（预检 + 拼命令 + Popen），而电脑端主线程
        # 经 `App._any_busy()` 问的正是这个函数 —— 抢同一把锁就变成"手机一提交、
        # 电脑界面卡到 Popen 返回"（磁盘慢时是秒级）。读一个引用是原子的，这里要的
        # 只是个提示位；真正串行化的占槽判定仍在 submit 的锁内。
        return self._proc is not None

    def running_task(self):
        with self._lock:
            return dict(self._task) if self._task else None

    # ---- 提交 ----

    def submit(self, prompt, device_id, refs=None, ref_mode="",
               strength_tier="", mask=""):
        """提交一个生图任务 → (task_id, 错误文案)。错误非空 = 没提交。

        二期入参（2026-10-11，W 拍板）：`refs` = 已上传附件的登记名列表、
        `ref_mode` = 带图两档（edit / subject，空 = 该模型默认档）、
        `strength_tier` = 重绘强度档（空 = 交给提示词自动分档）、
        `mask` = 遮罩的登记名（只跟底图重绘档走）。预检顺序与判据**逐字复用
        GUI 链路**（ui/image_gen._start_chat_image：resolve_modes → 手选档
        校验 → default_ref_mode → plan_ref_images → edit_strength），回显行
        与命令行同源（同一条"说出去的强度就是引擎收到的强度"纪律）。
        """
        prompt = str(prompt or "").strip()
        if not prompt:
            return None, "提示词不能为空。"
        if len(prompt) > 4000:
            return None, "提示词太长（上限 4000 字）。"
        with self._lock:
            if self._proc is not None:
                return None, "已有一个远程生图在进行（本机同一时间只跑一个）。"
            if self._local_busy():
                return None, "本机正在忙（生成 / 生视频 / 服务操作），稍后再试。"
            from ..core.config import cfg_lock
            from ..core import capability, modelreq, providers, sdprofile
            from ..core.media import (build_img_cmd, edit_strength, img_out_dir,
                                      mask_hint, plan_ref_images,
                                      resolve_img_files, resolve_img_model_path)
            with cfg_lock():
                snap = dict(self.cfg)          # 提交点做一次快照（坑 194 口径）
            # 远程只走本地链路：GUI 停在云端模型上时 resolve_modes 会拐进云端
            # 分支（判成服务商的档）——快照里钉回 local，判据才是本机 sd-cli 的。
            snap["model_provider"] = providers.LOCAL
            sd = str(snap.get("sd_dir", "") or "")
            cli = os.path.join(sd, "sd-cli.exe")
            if not os.path.isfile(cli):
                # 不把引擎的绝对路径回给手机端（坑 209⑥：那是本机用户名与目录结构）；
                # 路径进审计，手机那头只留"去哪儿修"。
                audit("image.submit", device_id, False, "no sd-cli: %s" % (cli or "(空)"))
                return None, ("电脑端还没配好生图引擎：在 设置 → 本地模型 → "
                              "生图（sd.cpp） 里指向 sd.cpp 目录。")
            diffusion = resolve_img_model_path(snap)
            if not diffusion:
                return None, ("未找到生图模型：电脑端还没配好扩散权重。")
            files = resolve_img_files(snap, diffusion)
            missing = sdprofile.missing_slots(files["family"], files)
            if missing:
                return None, ("这个生图模型（识别为 %s）缺配套文件：%s——在电脑端"
                              "设置里补齐。" % (sdprofile.label_of(files["family"]),
                                               "、".join(missing)))
            try:
                need = modelreq.assess(snap, diffusion, "image", files=files)
                lvl = modelreq.grade(need, snap.get("vram_gb") or 0,
                                     snap.get("ram_gb") or 0) if need else 0
            except Exception:
                lvl = 0
            if lvl >= modelreq.LEVEL_FATAL:
                return None, ("这台机器带不动这个模型（%s）——远程没人能按"
                              "「仍要继续」，请在电脑端确认后再来。"
                              % modelreq.LEVEL_TEXT[lvl])
            # ---- 二期：附件 → 带图两档 → 强度 → 遮罩（判据与 GUI 逐字同源）----
            ref_paths, err = upload_paths(device_id, refs or [], "ref")
            if err:
                return None, err
            mask_path = ""
            if str(mask or ""):
                mp, err = upload_paths(device_id, [mask], "mask")
                if err:
                    return None, err
                mask_path = mp[0] if mp else ""
            modes = capability.resolve_modes(snap, files["family"])
            mode = str(ref_mode or "").strip()
            if ref_paths and mode and not modes.get(mode, {}).get("ok"):
                return None, str(modes[mode]["why"])   # 不支持的档就地拦，一个字都不丢
            mode = mode or capability.default_ref_mode(snap, files["family"])
            used, dropped, drop_note = plan_ref_images(ref_paths, mode)
            if mode and mode != capability.MODE_EDIT:
                mask_path = ""           # 主体参考档（-r）不吃遮罩（10 §5.0.4）
            if not used:
                mode = ""                # 纯文生图不带模式（GUI 同口径）
                mask_path = ""
            st_value = None              # 底图重绘档才算强度；None = build_img_cmd 自己判
            echo = []
            if used:
                echo.append("[%s] 参考图 %d 张%s"
                            % (capability.MODE_LABEL.get(mode, "带图"), len(used),
                               "" if not drop_note else "（%s）" % drop_note))
                if mode == capability.MODE_EDIT:
                    # 回显与命令行同源：这里算一次，数值显式交给 build_img_cmd
                    _t, st_value, st_why = edit_strength(
                        snap, prompt, tier=str(strength_tier or ""))
                    # 小改档 + 没带遮罩 → 半句指路（判据在 media.mask_hint，与 GUI 同源）
                    _hint = mask_hint(_t, auto=not str(strength_tier or "").strip(),
                                      has_mask=bool(mask_path))
                    echo.append("重绘强度 %s（%s）%s%s"
                                % (st_value, st_why,
                                   "｜遮罩：只重绘涂抹区" if mask_path else "",
                                   _hint))
            try:
                steps = max(1, int(snap.get("img_steps", 12) or 12))
            except Exception:
                steps = 12
            size = str(snap.get("img_size", "1024x1024"))
            try:
                cfg_scale = float(snap.get("img_cfg", 2.5) or 2.5)
            except Exception:
                cfg_scale = 2.5
            try:
                seed = int(snap.get("img_seed", -1))
            except Exception:
                seed = -1
            outdir = img_out_dir(snap)
            try:
                os.makedirs(outdir, exist_ok=True)
            except Exception as e:
                audit("image.submit", device_id, False, "outdir failed: %s (%s: %s)"
                      % (outdir, type(e).__name__, e))
                return None, ("电脑端的产物目录建不出来：检查那个目录的磁盘与权限"
                              "（位置在 设置 → 云端模型 → 生图 / 生视频 的「修改产物位置」）。")
            tid = _pysecrets.token_hex(4)
            # 微秒防同秒碰撞：**不能**走 time.strftime("%f") —— Windows 的 C 运行时
            # strftime 不认 %f（glibc 扩展），当场 ValueError；datetime 是 Python
            # 自己实现的，各平台都行。
            out = os.path.join(outdir, "img_r%s_%s_%s.png" % (
                tid, time.strftime("%Y%m%d_%H%M%S"),
                _datetime.datetime.now().strftime("%f")))
            log_path = config.media_log_path(snap, out)
            cmd = build_img_cmd(snap, prompt, out, steps, size, diffusion,
                                cfg_scale, seed, files=files,
                                ref_mode=mode, ref_images=used,
                                strength=st_value, mask=mask_path)
            try:
                with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
                    lf.write("=== argv（远程 %s）===\n" % device_id)
                    for i, a in enumerate(cmd):
                        lf.write("[%2d] %s\n" % (i, a))
                    lf.write("=== cwd=%s ｜ out=%s ｜ 提交于 %s ===\n\n"
                             % (sd, out, time.strftime("%Y-%m-%d %H:%M:%S")))
            except Exception:
                log_path = ""
            flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace",
                                        creationflags=flags if os.name == "nt" else 0,
                                        cwd=sd)
            except Exception as e:
                return None, "引擎启动失败：%s" % e
            self._proc = proc
            self._stop_flag = threading.Event()
            self._task = {"id": tid, "device_id": device_id,
                          "prompt": prompt[:200], "state": "running",
                          "progress": None, "tail": [], "files": [],
                          "error": "", "out": out, "t0": time.time(),
                          "echo": echo}
            _ledger_add({"id": tid, "device_id": device_id,
                         "name": os.path.basename(out), "path": out,
                         "done": False, "ok": False,
                         "submitted": time.strftime("%Y-%m-%d %H:%M:%S")})
            threading.Thread(target=self._reader, daemon=True,
                             name="remote-img").start()
            audit("image.submit", device_id, True, tid)
            return tid, ""

    # ---- 执行（无 GUI 版 reader，判据与 ui/image_gen 同源）----

    def _reader(self):
        import re
        proc = self._proc
        task = self._task
        stop = self._stop_flag
        prog_rx = re.compile(r"(\d+)/(\d+) - ")
        tail = []
        log = config.media_log_path(self.cfg, task["out"])
        try:
            for line in proc.stdout:
                line = (line or "").rstrip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-40]                       # 保尾 40 行（失败原因在这）
                m = prog_rx.search(line)
                if m:
                    task["progress"] = [int(m.group(1)), int(m.group(2))]
                if log:
                    try:
                        with open(log, "a", encoding="utf-8",
                                  errors="replace") as lf:
                            lf.write(line + "\n")
                    except Exception:
                        pass
                if stop.is_set():
                    proc.kill()
                    break
            rc = proc.wait()
        except Exception as e:
            rc = -1
            task["error"] = "读取引擎输出失败：%s" % e
        with self._lock:
            ok = (rc == 0 and os.path.isfile(task["out"])
                  and not stop.is_set())
            task["state"] = "done" if ok else (
                "cancelled" if stop.is_set() else "failed")
            if not ok and rc != 0 and not task["error"]:
                task["error"] = "引擎退出码 %s；末尾输出：%s" % (
                    rc, " ｜ ".join(tail[-3:]))
            task["tail"] = tail[-6:]
            task["files"] = ([os.path.basename(task["out"])]
                             if os.path.isfile(task["out"]) else [])
            self._proc = None
            _ledger_update(task["id"], done=True, ok=ok,
                           files=task["files"])
        audit("image.exit", task.get("device_id", "?"), ok,
              "rc=%s" % rc)

    def cancel(self, task_id=None, device_id=None):
        with self._lock:
            if self._proc is None:
                return False
            if (task_id and self._task and self._task["id"] != task_id):
                return False
            if (device_id and device_id != "local" and self._task
                    and self._task.get("device_id") != device_id):
                return False
            proc = self._proc
            if self._stop_flag:
                self._stop_flag.set()
        # **当场 kill**，不能只置 stop_flag 等 reader 去杀：reader 的 `proc.kill()`
        # 写在 `for line in proc.stdout` 循环里，引擎不吐字（加载十几 GB 权重那几十秒、
        # 或卡住）就一直等不到下一行 —— 「停止」按了没反应，单槽永久 busy，
        # 电脑端也被 `_any_busy` 一起拦死。GUI 那条链路（_cancel_chat_image）就是直接杀的。
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        audit("image.cancel", device_id or "?", True, task_id or "")
        return True

    # ---- 查询 / 取回 ----

    def _history_ok(self):
        """历史产物开关（cfg["remote_history"]，默认关）：开启后取回判据从
        "本设备自己的任务"放宽到台账全量（16 §4 第 3 条预留的口径——只放宽
        判据，台账形状与"白名单落点"都不动）。"""
        try:
            return bool(self.cfg.get("remote_history", False))
        except Exception:
            return False

    def status(self, task_id, device_id):
        hist = self._history_ok()
        with self._lock:
            if self._task and self._task["id"] == task_id:
                if (device_id != "local" and not hist
                        and self._task["device_id"] != device_id):
                    return None
                t = dict(self._task)
            else:
                for t2 in _ledger_load():
                    if t2.get("id") == task_id:
                        if (device_id != "local" and not hist
                                and t2.get("device_id") != device_id):
                            return None
                        t = {"id": task_id,
                             "state": "done" if t2.get("ok") else
                                      ("failed" if t2.get("done") else "failed"),
                             "progress": None, "tail": [],
                             "files": t2.get("files") or [],
                             "error": "" if t2.get("ok") else "已结束（详见电脑端 media_logs）",
                             "device_id": t2.get("device_id")}
                        break
                else:
                    return None
        return {"id": t["id"], "state": t.get("state", "failed"),
                "progress": t.get("progress"), "tail": t.get("tail") or [],
                "files": t.get("files") or [], "error": t.get("error", ""),
                "echo": t.get("echo") or []}

    def artifact_path(self, task_id, device_id, name):
        """产物取回的白名单判据：任务属于该设备（loopback 全可见）+ 文件名是
        纯 basename + 路径就在台账登记的那份（台账是我们写的，路径逃逸在源头
        就不存在；这里再做一层双层校验，坑 209 纪律）。"""
        if not name or os.path.basename(name) != name or name in (".", ".."):
            return None
        hist = self._history_ok()
        with self._lock:
            if self._task and self._task["id"] == task_id:
                if (device_id != "local" and not hist
                        and self._task["device_id"] != device_id):
                    return None
                for f in self._task["files"]:
                    if f == name:
                        p = os.path.join(os.path.dirname(self._task["out"]), name)
                        return p if os.path.isfile(p) else None
                return None
        for t in _ledger_load():
            if t.get("id") == task_id and t.get("done") and t.get("ok"):
                if device_id != "local" and not hist \
                        and t.get("device_id") != device_id:
                    return None
                if name in (t.get("files") or []):
                    p = os.path.join(os.path.dirname(str(t.get("path", ""))), name)
                    base = os.path.realpath(os.path.dirname(str(t.get("path", ""))))
                    real = os.path.realpath(p)
                    if real == p and os.path.dirname(real) == base \
                            and os.path.isfile(p):
                        return p
                    return None
        return None

