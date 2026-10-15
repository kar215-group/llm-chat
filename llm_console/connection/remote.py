# -*- coding: utf-8 -*-
"""llm_console.connection.remote — 远程访问服务（监听面外壳 + 门面，2026-10-11 一期，99 K1）。

**远程层是唯一对外监听面**：8080（llama-server）/ 8081（代理）永远 loopback，
只有这条链会绑定非回环地址。

模块划分（2026-10-12 拆分，判据与行为一字未动）：本文件只留 HTTP handler +
`RemoteServer`（生命周期 / 配对委派 / 端点路由）+ 门面，实现按职责住在同目录：

  remote_net      监听面：双栈、TLS 握手在工作线程、空闲超时与并发上限、本机地址
  remote_pairing  配对码状态机：一次性 + 按码限次，全部不变量由同一把锁保护
  remote_audit    审计：JSONL + 轮转；关键事件逐条、轮询类聚合
  remote_uploads  附件上传面：独立安全面（魔数 / 上限 / 随机名 / 设备绑定 / TTL）
  remote_ledger   任务台账：产物取回的唯一白名单落点
  remote_jobs     生图任务管理器：单槽、与 GUI 双向互斥、判据逐字复用 GUI 链
  remote_page     内嵌 Web 单页：控件判据全来自 /api/caps，页面不写第二份字面量

⚠ **打桩要钉定义处**（坑 34）：常量与落盘路径在上面那些模块里，门面只是别名。

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
import http.cookies
import http.server
import json
import os
import secrets as _pysecrets
import ssl
import threading
import time
import urllib.parse

from ..core import remote_cert, secrets
from .remote_audit import (AUDIT_KEEP, AUDIT_PATH, AUDIT_ROTATE_BYTES,
                           audit, audit_poll, clear_audit, poll_reset,
                           read_audit)
from . import remote_chat
from .remote_chat import RemoteChat
from .remote_chat import ready as chat_ready
from .remote_jobs import RemoteJobs
from . import remote_wfd
from .remote_net import (BIND_ALL, BIND_FALLBACK_MSG, BIND_LABELS, BIND_TAILSCALE,
                         BIND_V4, BIND_WFD, BIND_WFD_REFUSE_MSG,
                         CROSS_ORIGIN_MSG, HANDSHAKE_TIMEOUT_S, IDLE_TIMEOUT_S,
                         MAX_CONCURRENT, POST_CTYPES, host_allowed, ip_label,
                         is_loopback, local_ips, make_httpd, normalize_ip,
                         resolve_bind, tailscale_installed)
from .remote_page import _PAGE_HTML, render as _render_page
from .remote_pairing import (PAIR_BAD_MSG, PAIR_BURNED_MSG, PAIR_BURNED_PAGE,
                             PAIR_TRIES_MAX, PAIR_TTL_S, PairGate)
from .remote_uploads import (UPLOAD_DIR, UPLOAD_KINDS, UPLOAD_MAX_BYTES,
                             UPLOAD_MAX_FILES, UPLOAD_TTL_S, _sniff_image_ext,
                             _uploads, _uploads_lock, sweep_orphan_uploads,
                             upload_paths, upload_store, uploads_reset)

class RemoteStartError(Exception):
    """启动失败且文案已是人话（端口越界等）。`App.start_remote_async` 对这类
    直接展示 `str(e)`，不套异常类型前缀（坑 157 口径：机器错误不进界面）。"""


# 鉴权用的 Cookie 名（长效设备 token 就装在这里）；监听面本身不知道它
COOKIE_NAME = "remote_token"

# 直连组的组名 / 密码（设置页「网址」次级窗显示、复制给手机用；改字面
# 一律先过 W——铁律 14）。定义在 remote_wfd，脚本默认值与它是同一份来源。
WFD_SSID = remote_wfd.GO_SSID
WFD_PASS = remote_wfd.GO_PASS

# 心跳类端点：审计里按窗口聚合，不逐条落（P0-3，聚合口径见 remote_audit.audit_poll）
POLL_PATHS = ("/api/task", "/api/caps", "/api/chat")

# JSON 请求体上限（2026-10-12 评估轮 C2）：上传面单张 20MB 有闸门，JSON 端点
# 原先一个都没有——`_read_json` 按对方声明的 Content-Length 原样读进内存，
# 免鉴权的 `/api/pair` 也能被喂超大 body。1MB 对 prompt 4000 字 + refs 清单
# 足够（chat / prompt 两条上限都是 4000 字）。
JSON_MAX_BYTES = 1024 * 1024
# 超限回给端上的原话（W 2026-10-12 批准）：拒读 + 断连，与上传面 413 同款纪律
JSON_TOO_LARGE_MSG = "请求内容太大（上限 1MB），已拒绝。"

# `_read_json` 的超限哨兵：None = 解析失败（语义是"按空 body 走"），哨兵 =
# 这一条根本没读——调用方必须回 413 并 return，不能当空 body 继续往下走。
_OVERSIZE = object()


def _ms(h):
    """本机处理这条请求花掉的毫秒数（P0-4：原先 `t0` 算了没人用）。"""
    return max(0, int((time.time() - getattr(h, "_t0", time.time())) * 1000))


def _audit(h, action, ok, detail="", poll=False):
    """这条请求的审计落点。写成模块级函数：自检里用桩对象借真方法时，
    桩只带 `client_address` / `_client_ip` / `server`，方法挂在类上就找不到了。
    """
    h.server.audit_req(action, h._client_ip(), ok, detail,
                       ms=_ms(h), poll=poll)


class _RemoteHandler(http.server.BaseHTTPRequestHandler):
    """远程访问的 HTTP 层：路由、鉴权、跨源闸门、配对提交、页面出口。

    端点按方法表分发（路由表由 `RemoteServer.start()` 挂），本类只做"这条请求
    该不该答、用谁的身份答"；**业务全在 `RemoteJobs` / 上传面 / 台账里**。

    `timeout` 是 P0-1 的空闲上限：stdlib 在 `setup()` 里按它 `settimeout`，
    于是"连上不写字节"最多占这条连接这么久，keep-alive 空转也会自己断。
    """

    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT_S

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
        # 双栈下 IPv4 连接是 v4-mapped 形式，归一在 remote_net.normalize_ip 一处
        #（回环豁免 / 审计 / 限速判据都靠它，坑 K3）
        return normalize_ip(self.client_address[0])

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
        if is_loopback(self._client_ip()):
            return True
        return self._device() is not None

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except Exception:
            return None
        if n > JSON_MAX_BYTES:
            # 不读这份超大体（长度由对方说了算，读它等于把内存交出去）；
            # keep-alive 下"不读又不关"会让这条连接上的后续请求全错位，所以直接关。
            self.close_connection = True
            return _OVERSIZE
        if n < 0:
            # 负数长度同样不能放：BufferedReader.read(-1) 一直读到 EOF ——
            # 慢速喂字节可借空闲超时续命把连接永久挂住（凑满并发闸门即拒死服务），
            # 快速灌数则吞内存。/api/pair 免鉴权，这条是未授权可达面（坑 254）。
            # body 一字节都不读、直接关连接（不读不关会毒化 keep-alive 流）；
            # 返回 None 后各端点按自身的空请求逻辑回 400/404。
            self.close_connection = True
            return None
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _cross_origin(self, is_post=False):
        """跨源防线（见 `remote_net.host_allowed` 那段为什么必须有）→ "" 放行 / 短原因进审计。"""
        if not host_allowed(self.headers.get("Host", "")):
            return "bad-host"
        if is_post:
            ct = str(self.headers.get("Content-Type", "") or "")
            if ct.split(";")[0].strip().lower() not in POST_CTYPES:
                return "bad-ctype"
        return ""

    # ---- GET ----
    def do_GET(self):
        self._t0 = time.time()
        self._dev_reset()
        path = self.path.split("?")[0].rstrip("/") or "/"
        bad = self._cross_origin()
        if bad:
            _audit(self, "get", False, "%s %s" % (path, bad))
            return self._json(403, {"error": CROSS_ORIGIN_MSG})
        if path == "/health":
            return self._json(200, {"ok": True, "service": "llm-chat-remote",
                                    "https": bool(self.server.https_on)})
        if path == "/":
            ok = self._auth_ok()
            _audit(self, "page", ok)
            if ok:
                return self._page_home()
            return self._page_pair()
        if not self._auth_ok():
            _audit(self, "get", False, path)
            return self._json(401, {"error": "未配对设备，请先在网页上完成配对。"})
        fn = self.server.route_get.get(path)
        ok = True
        try:
            if fn:
                return fn(self)
            return self._json(404, {"error": "没有这个端点: %s" % path})
        except Exception:
            # 路由函数炸了：审计记失败——finally 里无条件记 True 会让台账出现
            # "成功"假阳性，而客户端实际收到的是 500（A3）
            ok = False
            raise
        finally:
            # 关键一条：审计在**派发之后**落，才量得到本机耗时；而心跳类端点
            # （进度 / 能力查询）走聚合，否则配对、吊销这些要看的行会被轮询冲走
            # （P0-3，判据在 remote_audit.audit_poll）
            _audit(self, "get", ok, path, poll=path in POLL_PATHS)

    # ---- POST ----
    def do_POST(self):
        self._t0 = time.time()
        self._dev_reset()
        path = self.path.split("?")[0].rstrip("/")
        bad = self._cross_origin(is_post=True)
        if bad:
            # 类型不对就**不去读那个 body**（长度由对方说了算，读它等于把内存交出去）；
            # 但 keep-alive 下"不读又不关"会让这条连接上的后续请求全错位，所以直接关。
            self.close_connection = True
            _audit(self, "post", False, "%s %s" % (path, bad))
            return self._json(403, {"error": CROSS_ORIGIN_MSG})
        if path == "/api/pair":
            return self._do_pair()
        if not self._auth_ok():
            # 没读 body 就应答：同一条 keep-alive 连接上的下一个请求会把 body
            # 残字节当请求行解析（与跨源 403 路径同款处理，见上面那段注释）
            self.close_connection = True
            _audit(self, "post", False, path)
            return self._json(401, {"error": "未配对设备，请先在网页上完成配对。"})
        fn = self.server.route_post.get(path)
        ok = True
        try:
            if fn:
                return fn(self)
            # 路由未命中 = 没读 body：不断连就会错位后续请求（A2）
            self.close_connection = True
            return self._json(404, {"error": "没有这个端点: %s" % path})
        except Exception:
            ok = False       # 同上：路由函数抛异常时审计记失败，不谎报成功（A3）
            raise
        finally:
            _audit(self, "post", ok, path)

    # ---- 配对（唯一的免鉴权 POST；按码限次在 server 侧，见 RemoteServer.pair_allowed）----
    def _do_pair(self):
        ip = self._client_ip()
        payload = self._read_json()
        if payload is _OVERSIZE:
            return self._json(413, {"error": JSON_TOO_LARGE_MSG})
        payload = payload or {}
        verdict, msg = self.server.pair_allowed(ip)
        if not verdict:
            _audit(self, "pair", False, "blocked")
            return self._json(429, {"error": msg})
        code = str(payload.get("code", "")).strip()
        name = str(payload.get("name", "")).strip()[:20]
        if not self.server.take_pair_code(code):
            # 匹配即作废在这一次持锁里做完（take_pair_code）：分成"先查再作废"两步时，
            # 同一个码的并发请求可以都查到匹配 —— 一次性码就不一次性了。
            burned = self.server.pair_missed(ip)
            _audit(self, "pair", False,
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
            _audit(self, "pair", False, "denied on PC")
            return self._json(403, {"error": "电脑端拒绝了这次配对。"})
        did = secrets.add_remote_device(dev_name, token_hash,
                                        time.strftime("%Y-%m-%d %H:%M"))
        _audit(self, "pair", True, "device " + did)
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
        self._html(200, _render_page(authed))

    def _page_pair(self):
        self._page(False)

    def _page_home(self):
        self._page(True)


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
        self.bind = ""                # 实际绑到的地址（"" = 全部网卡，现行为）
        self.bind_note = ""           # 收窄没收成时的回退说明（设置页状态行可见）
        self.bind_kind = ""           # 设备直连档的链路种类："wfd"（电脑直连组
                                      # ）/ "wifi"（手机热点）；其他档 = ""
        self.jobs = None              # RemoteJobs（App 侧装配；批次 3）
        self.chat = None              # RemoteChat（同上；没装配就没有聊天端点）
        # 配对码状态机（一次性 + 按码限次）整套不变量与它自己的锁在 remote_pairing
        self.pair_gate = PairGate()
        # 设备 last_seen 回写节流（内存态；重启即重来，无妨）
        self._seen_ts = {}
        # 路由表（批次 3 由 App 侧填充：/api/image / /api/task / /api/artifact）
        self.route_get = {}
        self.route_post = {}

    # ---- 生命周期 ----

    def start(self):
        # 重入保护（坑 271）：start 内有起组 / 绑端口 / 起服务线程，绕过
        # App.start_remote_async 二次调用会留下第二个 httpd（旧的不再被引用、
        # 端口却一直占着）。先 stop 再 start 是正常路径（settings 拨开关走它）。
        if getattr(self, "httpd", None) is not None or getattr(self, "_starting", False):
            raise RemoteStartError("远程服务已经在启动或运行中，先停止再启动")
        self._starting = True
        try:
            return self._start_inner()
        finally:
            self._starting = False

    def _start_inner(self):
        # 注意不能用 `cfg.get("remote_port", 8443) or 8443`：那个 `or` 会把
        # 手改成 0 的端口**静默变成 8443**（服务器听 8443、网址窗显示 :0，
        # 手机永远连不上）——正是要报出来的那一类。None / 空串才回落默认值。
        raw_port = self.cfg.get("remote_port", 8443)
        try:
            port = int(raw_port if raw_port not in (None, "") else 8443)
        except Exception:
            port = -1
        if not (1024 <= port <= 65535):
            # 设置页保存时有区间校验，但配置文件可以手改：0 / 越界 / 非数字
            # 原来会静默改听默认端口（界面还显示 :0）或绑失败后走 all 回退——
            # 两种都让用户以为远程开着（坑 128 同族）。人话报错，由
            # App.start_remote_async 直接展示（RemoteStartError 不套类型前缀）。
            raise RemoteStartError("端口应在1024~65535范围内")
        want_https = bool(self.cfg.get("remote_https", True))
        # 对外开放范围（P0-5）：默认 `all` = 全部网卡，与一期行为逐字一致；
        # `wfd` = 仅设备直连（2026-10-13，99 K16）
        want_bind = str(self.cfg.get("remote_bind", BIND_ALL) or BIND_ALL)
        self.bind_kind = ""
        if want_bind == BIND_WFD:
            # 设备直连档：现成链路优先（手机热点已接入 / 组已在跑）——没有就
            # 把电脑侧的无线直连组开起来（约几秒）。**拿不到直连地址就拒绝
            # 启动**：绝不回退 all（回退等于把公网口打开，K16 的硬要求）。
            bind, note = resolve_bind(self.cfg)
            if bind and not remote_wfd.running():
                # 地址在、组却不是本进程起的——多半是上一个实例关窗时的
                # 快速收组还没散净。等它散掉再按"没链路"重新起，别绑一个
                # 正在死的地址（2026-10-13 关窗提速轮）
                remote_wfd.settle_stale()
                bind, note = resolve_bind(self.cfg)
            if not bind:
                try:
                    remote_wfd.start_go()
                except remote_wfd.WfdGoError as e:
                    raise RemoteStartError(str(e))
                bind, note = resolve_bind(self.cfg)
                if not bind:
                    remote_wfd.stop_go()
                    raise RemoteStartError(BIND_WFD_REFUSE_MSG)
            self.bind_kind = "wfd" if remote_wfd.running() else "wifi"
        else:
            bind, note = resolve_bind(self.cfg)
        self.bind_note = note
        try:
            httpd = make_httpd(port, _RemoteHandler, bind)
        except OSError:
            if want_bind == BIND_WFD:
                # 直连档绑的是具体地址，绑不上基本就是端口被占——同样不回退
                remote_wfd.stop_go()
                raise RemoteStartError("端口 %d 被别的程序占用：换一个端口"
                                       "再拨一次开关" % port)
            # 选了 Tailscale 档、但那一刻那个地址没了（客户端刚断）：退回全部
            # 网卡并**如实说出来**——静默不监听会让用户以为远程开着（坑 128）
            httpd = make_httpd(port, _RemoteHandler, "")
            self.bind, self.bind_note = "", BIND_FALLBACK_MSG
            audit("bind.fallback", "", False, BIND_FALLBACK_MSG)
        else:
            self.bind = bind
            if want_bind == BIND_WFD:
                audit("wfd.bind", "", True, "bind=%s" % bind)
        poll_reset()                # 聚合窗口随服务重启清零
        # 上传登记是内存态、重启即空：目录里比 TTL 还老的文件必然没人再引用
        # （新文件留着：可能是并行实例刚收的）。**无条件清**——不挂在 jobs /
        # chat 的装配条件下（那两个在测试与复用路径里可能是 None）。
        sweep_orphan_uploads()
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
            # 历史产物清单（P1-6）：`remote_history` 这个开关从这一刻起才真有入口
            self.route_get["/api/history"] = self._h_history
        if self.chat is not None:
            # 文本聊天（99 K7）：只打本机已加载的文本模型，**不触发换载**
            self.route_post["/api/chat"] = self._h_chat
            self.route_post["/api/chat/stop"] = self._h_chat_stop
            self.route_post["/api/chat/clear"] = self._h_chat_clear
            self.route_get["/api/chat"] = self._h_chat_poll
        if want_https:
            try:
                cert = remote_cert.ensure_cert()
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert["cert"], cert["key"])
                # 只把 ctx 挂给服务器对象，由每个请求线程自己握手（TLSWorkerMixin）：
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

    def stop(self, quick=False):
        """停远程面。`quick=True`（关窗路径）：直连组**不等收尾**——写完
        STOP 就把控制权交回，组由脚本自己散净（关窗不再被那两秒拖住，
        2026-10-13 关窗提速轮；父进程看护兜底）。其他路径照旧等组退干净。"""
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self.https_on = False
        # 直连组跟着收（没开过 = 空操作）：组只在本档有，服务停了留一个
        # 开着的小组没有意义，还占着电台。
        try:
            remote_wfd.stop_go(wait=not quick)
        except Exception:
            pass
        self.bind_kind = ""

    def running(self):
        return self.httpd is not None

    def scheme(self):
        return "https" if self.https_on else "http"

    # ---- 配对码 ----

    # ---- 配对码（状态机在 remote_pairing.PairGate；这里只留既有公开 API 的委派）----

    def new_pair_code(self):
        return self.pair_gate.new_code()

    def pair_code_left(self):
        return self.pair_gate.code_left()

    def pair_code_matches(self, code):
        return self.pair_gate.matches(code)

    def consume_pair_code(self):
        self.pair_gate.consume()

    def take_pair_code(self, code):
        return self.pair_gate.take(code)

    def pair_allowed(self, ip):
        return self.pair_gate.allowed(ip)

    def pair_missed(self, ip):
        return self.pair_gate.missed(ip)

    def pair_burned(self):
        return self.pair_gate.burned()


    # ---- 其他 ----

    def _touch_seen(self, dev_id):
        """last_seen 回写节流（60 秒一次；写 secrets.json 不便宜）。"""
        now = time.time()
        if now - self._seen_ts.get(dev_id, 0) < 60:
            return
        self._seen_ts[dev_id] = now
        secrets.touch_remote_device(dev_id, time.strftime("%Y-%m-%d %H:%M"))

    def _audit_req(self, action, ip, ok, detail="", ms=None, poll=False):
        """落审计：心跳类走聚合，其余逐条（判据只在 `remote_audit` 那一处）。"""
        (audit_poll if poll else audit)(action, ip, ok, detail, ms)

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
        payload = h._read_json()
        if payload is _OVERSIZE:
            return h._json(413, {"error": JSON_TOO_LARGE_MSG})
        payload = payload or {}
        refs = payload.get("refs")
        refs = [str(x) for x in refs][:UPLOAD_MAX_FILES] \
            if isinstance(refs, list) else []
        tid, err = self.jobs.submit(
            str(payload.get("prompt", "")), self._caller_id(h),
            refs=refs,
            ref_mode=str(payload.get("ref_mode", "") or ""),
            strength_tier=str(payload.get("strength_tier", "") or ""),
            mask=str(payload.get("mask", "") or ""),
            # 一次出几张（网页「远程生图」旁的小控件；夹取在 submit 一处做，
            # 越界 / 非数字一律当 1 —— 引擎只对生图支持批量）
            count=payload.get("count", 1))
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
            self._audit_req("upload", h._client_ip(), False, "oversize",
                            ms=_ms(h))
            return h._json(413, {"error": (
                "这张图 %.1fMB，超过 %dMB 上限——先压小一点。"
                % (n / 1048576.0, UPLOAD_MAX_BYTES // 1048576))})
        try:
            raw = h.rfile.read(n)
        except Exception:
            raw = b""
        name, err = upload_store(self._caller_id(h), kind, raw)
        if err:
            self._audit_req("upload", h._client_ip(), False, kind,
                            ms=_ms(h))
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
        # 聊天就绪判据要问本机服务：一次 /health + 一次 /v1/models。
        # 服务没起时是连接拒绝（秒回），页面也只在进页与任务收尾时拉 caps
        chat_ok, chat_why, chat_model = chat_ready(snap)
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
            # 历史产物开关的状态（页面据此决定要不要说明边界，
            # 判据不在页面写第二份）
            "history_all": bool(snap.get("remote_history", False)),
            # 聊天控件判据同源：能不能聊 / 为什么不能 / 现在在服务的是谁
            "chat": {"ok": chat_ok, "why": chat_why, "model": chat_model},
            "upload": {"max_bytes": UPLOAD_MAX_BYTES,
                       "formats": "PNG / JPG / WEBP / BMP"},
        })

    def _h_history(self, h):
        """「之前的成果」：这台设备能取回的成功产物清单（P1-6）。

        只回 `task` / `name` / `submitted` 三样，**path 不出门**（台账里那份是
        本机绝对路径，坑 209⑥）；能不能看到别人的，判据在 `jobs._may_see` 一处，
        与 /api/task、/api/artifact 完全同一条。
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        try:
            limit = max(1, min(50, int((qs.get("limit") or ["20"])[0])))
        except Exception:
            limit = 20
        return h._json(200, {"items": self.jobs.history(self._caller_id(h),
                                                        limit)})

    # ---- 文本聊天（99 K7：只本地、不触发换载、轮询增量、会话 per-设备）----

    def _h_chat(self, h):
        payload = h._read_json()
        if payload is _OVERSIZE:
            return h._json(413, {"error": JSON_TOO_LARGE_MSG})
        payload = payload or {}
        ok, err = self.chat.send(self._caller_id(h),
                                 str(payload.get("text", "")))
        if err:
            # 内容问题 400；"现在不行"（忙 / 服务没起 / 加载的不是能聊的）409
            code = 400 if err in (remote_chat.EMPTY_MSG,
                                  remote_chat.TOO_LONG_MSG) else 409
            return h._json(code, {"error": err})
        return h._json(200, {"ok": ok})

    def _h_chat_poll(self, h):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)

        def _n(key):
            try:
                return max(0, int((qs.get(key) or ["0"])[0]))
            except Exception:
                return 0
        out = self.chat.poll(self._caller_id(h), _n("sa"), _n("st"),
                             (qs.get("hist") or [""])[0] == "1")
        return h._json(200, out)

    def _h_chat_stop(self, h):
        # 网页固定 POST body:"{}"：不读它，残留字节会被 keep-alive 连接上的
        # 下一条请求当请求行解析（与 401/404 路径同一条纪律，坑 258）
        if h._read_json() is None and h.close_connection:
            return h._json(400, {"error": "请求体读不了。"})
        if self.chat.cancel(self._caller_id(h)):
            return h._json(200, {"ok": True})
        return h._json(404, {"error": "没有可停止的远程会话。"})

    def _h_chat_clear(self, h):
        if h._read_json() is None and h.close_connection:
            return h._json(400, {"error": "请求体读不了。"})
        return h._json(200, {"ok": True,
                             "cleared": self.chat.clear(self._caller_id(h))})

    def _h_task(self, h):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        tid = (qs.get("id") or [""])[0]
        st = self.jobs.status(tid, self._caller_id(h))
        if st is None:
            return h._json(404, {"error": "没有这个任务。"})
        return h._json(200, st)

    def _h_cancel(self, h):
        payload = h._read_json()
        if payload is _OVERSIZE:
            return h._json(413, {"error": JSON_TOO_LARGE_MSG})
        payload = payload or {}
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
# 门面：`connection/remote.py` 是监听面 + 消费者唯一入口
# ---------------------------------------------------------------------------
# 界面层（`ui/settings.py` / `ui/app.py`）与自检都只认 `remote` 这一个模块名，
# 实现按职责住在同目录的 `remote_*.py` 里。⚠ **打桩纪律（坑 34）**：
# 要给常量/路径打桩必须钉**定义处**——
#   审计路径与阈值    → `connection.remote_audit`
#   上传目录与上限    → `connection.remote_uploads`
#   台账路径          → `connection.remote_ledger`
#   握手/空闲超时      → `connection.remote_net`
# 在门面别名上赋值不会影响真正读它的那个模块（函数对象是共享的，常量不是）。

