# -*- coding: utf-8 -*-
"""llm_console.connection.proxy — OpenAI 兼容中转代理：本机 HTTP 服务、模型名解析、按需换载与 context 校验"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
import http.server

from ..core.config import api_headers, base_url, save_config
from ..core.models import display_name, resolve_model, scan_models
from ..core.params import ctx_for
from ..core.server import (_query_serving_ctx, _query_serving_model,
                           server_alive, server_process_alive, server_ready,
                           start_server, stop_server)


PROXY_WAIT_READY_S = 240          # 经代理拉起服务时等待就绪的上限（超时→503）

class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """OpenAI 兼容代理端点：转发到 llama-server（按需拉起/切换模型）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                      # 静默访问日志

    # ---- 工具 ----
    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        key = self.server.cfg.get("api_key") or ""
        if not key:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + key

    def _chunk(self, data):
        if not data:
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/health":
            return self._json(200, {"status": "ok", "proxy": True,
                                    "port": self.server.cfg.get("proxy_port")})
        if not self._auth_ok():
            return self._json(401, {"error": {"message": "invalid api key"}})
        if path == "/v1/models":
            _d, chat, _img = scan_models(self.server.cfg)
            data = []
            for p in chat:
                b = os.path.basename(p)
                # 返回更友好的 id（简称），同时保留长文件名作为兼容项：
                # 客户端手填任一形式（含 local 兜底）都可由 resolve_model 命中
                data.append({"id": display_name(self.server.cfg, p),
                             "object": "model", "owned_by": "local"})
                data.append({"id": b, "object": "model", "owned_by": "local"})
            data.append({"id": "local", "object": "model", "owned_by": "local"})
            seen, uniq = set(), []
            for m in data:
                if m["id"] not in seen:
                    seen.add(m["id"]); uniq.append(m)
            return self._json(200, {"object": "list", "data": uniq})
        return self._json(404, {"error": {"message": "not found: " + self.path}})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found: " + self.path}})
        if not self._auth_ok():
            return self._json(401, {"error": {"message": "invalid api key"}})
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception as e:
            return self._json(400, {"error": {"message": "bad json: %s" % e}})

        target = self.server.ensure_model(str(payload.get("model", "")))
        if target is None:
            return self._json(503, {"error": {"message":
                "本地模型未就绪（加载超时或失败），请稍后重发请求。"}})
        payload["model"] = os.path.basename(target)

        url = base_url(self.server.cfg) + "/v1/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers=api_headers(self.server.cfg),
                                     method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as e:
            # 透传上游错误详情（llama.cpp 会给出真实原因，如超上下文），
            # 并对常见错误附中文提示，避免只看到含糊的 "upstream error"
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            try:
                obj = json.loads(detail) if detail else {}
            except Exception:
                obj = {"error": {"message": detail[:2000]}}
            if not isinstance(obj, dict):
                obj = {"error": {"message": str(obj)[:2000]}}
            err = obj.setdefault("error", {})
            if "exceed" in detail and "context" in detail:
                err["hint"] = ("本地模型上下文不足：请在 GUI 设置 → 服务参数调大 context"
                               "（当前 %s），保存并重启服务。" % self.server.cfg.get("ctx"))
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(e.code)              # 透传上游状态码（不再包成 502）
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            return self._json(502, {"error": {"message": "upstream error: %s" % e}})

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for raw in resp:
                    self._chunk(raw)
                self._chunk(b"")
            except Exception:
                pass
            try:
                resp.close()
            except Exception:
                pass
        else:
            data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

class ProxyServer:
    """OpenAI 兼容中转服务：agent → 本代理 → llama-server。

    只依赖 cfg（不含 GUI 状态），可供 agent 应用稳定接入；
    请求的模型与当前已加载不一致时自动停止并重启服务。
    """

    def __init__(self, cfg, note_fn=None):
        self.cfg = cfg
        self.note = note_fn or (lambda msg: None)
        self.httpd = None
        self.thread = None
        self._lock = threading.Lock()

    def start(self):
        port = int(self.cfg.get("proxy_port", 8081) or 8081)
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                    _ProxyHandler)
        except Exception as e:
            self.note("代理启动失败（端口 %s）：%s" % (port, e))
            return False
        httpd.cfg = self.cfg
        httpd.ensure_model = self.ensure_model
        httpd.daemon_threads = True
        self.httpd = httpd
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        self.note("API 代理已启动：http://127.0.0.1:%d/v1" % port)
        return True

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None

    def running(self):
        return self.httpd is not None

    # ---- 模型解析 + 按需加载（串行化，避免并发请求重复拉起） ----
    def ensure_model(self, req_model):
        with self._lock:
            _d, chat, _img = scan_models(self.cfg)
            if not chat:
                return None
            target = resolve_model(self.cfg, req_model, chat=chat)
            if not target:
                last = str(self.cfg.get("proxy_last_model", ""))
                target = next((p for p in chat
                               if os.path.basename(p) == last), None) or chat[0]
            tb = os.path.basename(target)
            want_ctx = ctx_for(self.cfg, target, agent=True)
            # 快路径先做毫秒级本地进程检查：进程不在就无需（也避免）各等 ~2 秒
            # HTTP 超时——本函数全程持锁，这里曾让"服务未运行时的每个 agent 请求"
            # 白白阻塞 ~4 秒。与 _status_loop / 设置页的守卫是同一模式。
            if server_process_alive() and (
                    server_ready(self.cfg) and _query_serving_model(self.cfg) == tb
                    and (_query_serving_ctx(self.cfg) or want_ctx) == want_ctx):
                self.cfg["proxy_last_model"] = tb
                return target
            # 需要（重新）加载目标模型（或 context 与 agent 场景记录不符）
            if server_process_alive() and server_alive(self.cfg):
                self.note("agent 请求 %s（context %d）：停止当前服务并切换…" % (tb, want_ctx))
            else:
                self.note("agent 请求 %s（context %d）：正在启动服务…" % (tb, want_ctx))
            stop_server()
            time.sleep(1)
            self.cfg["model"] = target
            self.cfg["model_kind"] = "chat"
            save_config(self.cfg)
            try:
                start_server(self.cfg, want_ctx)
            except Exception as e:
                self.note("agent 请求启动服务失败：%s" % e)
                return None
            if not self._wait_ready():
                self.note("agent 请求 %s：等待超时（%ds），已返回 503"
                          % (tb, PROXY_WAIT_READY_S))
                return None
            self.cfg["proxy_last_model"] = tb
            save_config(self.cfg)
            self.note("agent 请求 %s：服务就绪。" % tb)
            return target

    def _wait_ready(self, timeout_s=PROXY_WAIT_READY_S):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if server_ready(self.cfg):
                return True
            if time.time() - t0 > 10 and not server_alive(self.cfg):
                return False          # 进程已退出（崩溃/参数错误）
            time.sleep(1.5)
        return False
