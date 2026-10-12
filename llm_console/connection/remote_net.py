# -*- coding: utf-8 -*-
"""llm_console.connection.remote_net — 远程访问的**监听面**（2026-10-12 模块化）。

从 `connection/remote.py` 拆出来，本模块只管四件事：

  1. **怎么监听**：双栈优先（`V6MixServer`：AF_INET6 + V6ONLY=0，一个端口同时接
     v6 与 v4-mapped；本机没有 v6 栈才退 `PlainServer`），TLS 握手在**本连接的
     工作线程**里做（`TLSWorkerMixin`，坑 229），绑定范围可按 `remote_bind` 收窄；
  2. **一条连接能活多久、同时在飞几条**：空闲超时 + 并发上限——8443 是**唯一对外
     监听面**（8080/8081 永远 loopback），IPv6 直连时扫端口是常态，这两件事
     不能由对方决定；
  3. **本机有哪些地址**：`local_ips` / `ip_label` / `tailscale_installed`
     （设置页"手机连哪个地址"与穿透判据，穿透工具一律外部程序）；
  4. **这请求来路对不对**：`is_loopback`（豁免**鉴权**）与 `host_allowed` +
     `POST_CTYPES`（校验**来源**）是两回事——浏览器就坐在回环上（坑 227）。

铁律 1：connection 层不得 import tkinter。
"""

import http.server
import ipaddress
import os
import shutil
import socket
import threading
import time

from . import remote_audit

# TLS 握手超时（秒）：握手在工作线程里做（不再卡 accept 循环，见 TLSWorkerMixin），
# 但也要给个上限——半开连接最多占本连接一个线程这么久。
HANDSHAKE_TIMEOUT_S = 10

# 建连之后的**空闲**上限（秒）：handler 的 `timeout` 会在 `setup()` 里重新
# `settimeout`，所以这一条取代了原先"握手后一律 no timeout"的口径。为什么必须有：
# 本端口是唯一对外监听面，HTTP/1.1 又是长连接——不写字节的客户端原本可以
# **无限期**占着一个线程（慢速 / 半开 / 扫端口都是常态，占多久由对方说了算）。
IDLE_TIMEOUT_S = 30

# 同时在飞的连接上限：每条连接一个线程，线程不是免费的（栈 + 内核对象），
# 而"开一百个连接挂着不动"没有任何成本。拦在 `verify_request`（accept 之后、
# **建线程之前**），超了直接关——回不了 HTTP 响应，因为那时还没有协议层。
# 24 不是 12：桌面与手机浏览器每台都可占 6 条长连接，两部手机 + 本机浏览器
# 就够 12 条——闸门要拦的是"无上限"，不该把一家人正常用挡在门外。
MAX_CONCURRENT = 24


def normalize_ip(raw):
    """双栈监听下 IPv4 连接是 `::ffff:a.b.c.d` 形式，归一回 IPv4。
    回环豁免 / 审计 / 限速 / 绑定判据都靠它，口径必须只有一处（坑 K3）。"""
    ip = str(raw or "")
    return ip[7:] if ip.startswith("::ffff:") else ip


class ConnGateMixin:
    """并发闸门（P0-1）：占多少线程不能由对方决定。

    计数配平在 `process_request_thread` 的**进 / 出**上（一条连接一次进、一次出，
    握手失败提前返回也走同一条 finally）；`verify_request` 只在 accept 循环上读
    一个提示位做拦截 —— 允许 ±1 条的竞态窗口，这里拦的是"挂着不写字节的一堆连接"，
    不是精确配额。
    """

    max_concurrent = MAX_CONCURRENT

    def __init__(self, *args, **kw):
        self._live = 0
        self._live_lock = threading.Lock()
        super().__init__(*args, **kw)

    def verify_request(self, request, client_address):
        with self._live_lock:
            if self._live >= self.max_concurrent:
                remote_audit.audit(
                    "conn.limit", normalize_ip(client_address[0]), False,
                    "live=%d" % self._live)
                return False
        return True

    def _slot(self, delta):
        with self._live_lock:
            self._live = max(0, self._live + delta)


class TLSWorkerMixin(ConnGateMixin):
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
    这里走 `shutdown_request` 只是把计数归还并兜底关（detach 后的原对象再 close
    是空操作）。握手后**不再**把这条连接调回"无超时"：读写上限交给 handler 的
    `timeout`（见 `IDLE_TIMEOUT_S`）。
    """

    ssl_ctx = None

    def process_request_thread(self, request, client_address):
        self._slot(1)
        try:
            if self.ssl_ctx is not None:
                try:
                    request.settimeout(HANDSHAKE_TIMEOUT_S)
                    request = self.ssl_ctx.wrap_socket(request, server_side=True)
                except Exception:
                    self.shutdown_request(request)
                    return
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
        finally:
            self._slot(-1)


class V6MixServer(TLSWorkerMixin, http.server.ThreadingHTTPServer):
    """双栈监听（AF_INET6 + IPV6_V6ONLY=0）：一个端口同时接 IPv6 与 IPv4
    （v4 走 v4-mapped 地址）。校园网手机直连走**全球 IPv6**（教育网 2001:da8
    常见），只绑 AF_INET 时 v6 包根本到不了 —— 这是 2026-10-11 本机诊断出的
    真缺口。Windows 建套接字默认 V6ONLY=1，必须显式关；系统没有 v6 栈时
    make_httpd 回退 AF_INET。"""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6,
                                   socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        http.server.HTTPServer.server_bind(self)


class PlainServer(TLSWorkerMixin, http.server.ThreadingHTTPServer):
    """本机没有可用 IPv6 栈时的 IPv4 回退（TLS 握手同样在工作线程里做）。"""


def make_httpd(port, handler, bind=""):
    """起监听对象。`bind` 空 = 全部地址（默认，与一期行为一致）：优先双栈，
    本机没有可用 IPv6 栈（bind 失败）才退回 IPv4。

    指定了地址就按地址族选服务类（P0-5 的"对外开放范围"）：v6 字面量走
    `V6MixServer`（它会把 V6ONLY 关掉，绑到那一个 v6 地址上只接 v6），
    v4 字面量走 `PlainServer`（`"0.0.0.0"` = 全部 v4 网卡但**不收 v6**）。
    绑一个本机没有的地址在 Windows 上是 OSError（不会静默变成全网卡），
    由调用方按"回退 + 如实说"处理。
    """
    if ":" in str(bind or ""):
        return V6MixServer((bind, port), handler)
    if bind:
        return PlainServer((bind, port), handler)
    if socket.has_ipv6:
        try:
            return V6MixServer(("", port), handler)
        except OSError:
            pass
    return PlainServer(("", port), handler)


# ---- 对外开放范围（cfg["remote_bind"]，P0-5）----
BIND_ALL = "all"
BIND_TAILSCALE = "tailscale"
BIND_V4 = "v4"
# 设置页下拉的三项标签（W 2026-10-12 批准的文案）
BIND_LABELS = {BIND_ALL: "本机所有网卡（默认）",
               BIND_TAILSCALE: "仅 Tailscale",
               BIND_V4: "仅局域网 IPv4"}
BIND_FALLBACK_MSG = "没检测到 Tailscale 地址，已按本机所有网卡开放。"


def tailscale_addr(ips=None):
    """本机可绑的 Tailscale 地址（拿不到 = 空串）。

    `local_ips` 的 v6 过滤只放 2xxx/3xxx（全球单播），Tailscale 的
    fd7a:115c:a1e0 段本来就在名单外，所以这里实际只会命中 100.64/10 那个
    IPv4 地址——它也比 v6 稳（不随后缀漂）。
    """
    for ip in (local_ips() if ips is None else ips):
        if ip_label(ip) == "tailscale" and ":" not in ip:
            return ip
    return ""


def resolve_bind(cfg):
    """配置 → `(要绑的地址, 回退说明)`；bind 空串 = 全部网卡（现行为）。

    默认档 `all` 与一期逐字一致；`v4` 用 `"0.0.0.0"` 这个**显式 v4 字面量**表达
    "收全部 v4、不收 v6"；`tailscale` 只绑虚拟网卡那一个地址（把攻击面收到
    只有隧道能到的位置，99 K1：穿透工具一律外部程序，我们只给绑法与指引）。
    选了 Tailscale 但本机没有那个地址时**回退 all 并如实说明**——静默不监听
    会让用户以为远程是开着的，那比多开一个面更糟（坑 128）。
    """
    want = str((cfg or {}).get("remote_bind", BIND_ALL) or BIND_ALL)
    if want == BIND_V4:
        return "0.0.0.0", ""
    if want == BIND_TAILSCALE:
        ip = tailscale_addr()
        if ip:
            return ip, ""
        return "", BIND_FALLBACK_MSG
    return "", ""


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


def is_loopback(ip):
    return str(ip) in ("127.0.0.1", "::1")

# 跨源那道防线（2026-10-12）：回环是**豁免鉴权**的，于是"本机浏览器里的一个恶意
# 网页"天然拥有全部远程接口 —— 跨源 fetch 打 http://127.0.0.1:<port>/api/image，
# 源地址就是回环，`_auth_ok()` 直接放行（实测 text/plain 的简单请求不回预检，
# 200 起了真任务）。两条规矩堵它：Host 白名单（顺带堵 DNS rebinding：外部域名
# 解析到 127.0.0.1 时请求头里的 Host 是那个域名）+ POST 只收非"简单请求"的
# Content-Type（跨源发这两种类型浏览器必先发 OPTIONS 预检，而我们从不回 CORS 头）。
_HOST_ALLOW = None


def host_allowed(raw):
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
POST_CTYPES = ("application/json", "application/octet-stream")
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
