# -*- coding: utf-8 -*-
"""llm_console.core.remote_cert — 远程访问的 HTTPS 自签证书（2026-10-11，99 K1）。

为什么是 PowerShell + Python 拼：Python 标准库**没有** RSA 密钥生成，也没有能喂给
`ssl.SSLContext.load_cert_chain` 的 PKCS#12 读取器；而 Windows 自带的 PowerShell
（.NET）两样都有。分工：

  PowerShell（一次调用，零管理员、不进证书库）
    1. `[RSA]::Create(2048)` 造密钥对；
    2. `CertificateRequest...CreateSelfSigned` 出自签证书（SAN = localhost + 127.0.0.1）；
    3. 证书 DER（RawData）与 RSA 参数（ExportParameters）以 base64 JSON 打到 stdout。
  Python（纯标准库）
    4. 把参数转大端、DER 编码成 PKCS#1 RSAPrivateKey → `key.pem`；
    5. 证书 DER 包成 PEM → `cert.pem`；
    6. SHA-256 指纹回给调用方（设置页显示，供需要的人核对）。

失败一律抛 `RemoteCertError`，文案**人话**、不透 PowerShell 原文（坑 157 口径：
"机器判不了" ≠ 报错误原文；外部工具输出不进界面）。`runner` 可注入——自检不碰
真 PowerShell、也不产真实密钥。
"""

import base64
import hashlib
import json
import os
import subprocess

from .config import APP_DIR

CERT_DIR = os.path.join(APP_DIR, "remote_cert")
CERT_PEM = os.path.join(CERT_DIR, "cert.pem")
KEY_PEM = os.path.join(CERT_DIR, "key.pem")

# PowerShell 脚本：产出 cert DER + RSA 参数 JSON。**私钥材料只走 stdout**（被本进程
# 捕获后立刻落 key.pem），不落任何临时文件、不出现在命令行参数里。
_PS = r"""
$ErrorActionPreference = 'Stop'
$rsa = [System.Security.Cryptography.RSA]::Create(2048)
$req = New-Object System.Security.Cryptography.X509Certificates.CertificateRequest(
    'CN=llm-chat-remote', $rsa,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$cert = $req.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1),
                              [DateTimeOffset]::UtcNow.AddYears(10))
$p = $rsa.ExportParameters($true)
function B($b) { if ($b) { [Convert]::ToBase64String($b) } else { '' } }
@{ cert = [Convert]::ToBase64String($cert.RawData);
   n = B $p.Modulus; e = B $p.Exponent; d = B $p.D;
   p = B $p.P; q = B $p.Q; dp = B $p.DP; dq = B $p.DQ; qi = B $p.InverseQ } |
  ConvertTo-Json -Compress
"""


class RemoteCertError(Exception):
    """证书生成/加载失败；文案已是人话，可直接给用户看。"""


def _run_ps():
    """真实 runner：调用 PowerShell 出材料。独立出来只为打桩（坑 34 纪律）。"""
    cmd = ["powershell", "-NoProfile", "-NonInteractive",
           "-EncodedCommand", base64.b64encode(_PS.encode("utf-16le")).decode("ascii")]
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=flags if os.name == "nt" else 0)
    except FileNotFoundError:
        raise RemoteCertError("找不到 PowerShell，无法生成 HTTPS 证书；"
                              "可在设置里改用明文 HTTP（仅建议局域网内使用）。")
    except Exception as e:
        raise RemoteCertError("生成 HTTPS 证书失败：%s" % e)
    if p.returncode != 0:
        # 不透 PowerShell 原文（可能含策略/路径细节）；只分类
        raise RemoteCertError("本机 PowerShell 未能生成证书（可能被安全策略限制）；"
                              "可在设置里改用明文 HTTP（仅建议局域网内使用）。")
    return p.stdout.decode("utf-8", "replace").strip()


def _int(b64, reverse=True):
    """base64 字节 → 大端无符号整数的 DER INTEGER 载荷。

    `reverse` 按字节反转：.NET 的 RSAParameters **没有统一端序**——
    RSACryptoServiceProvider（CAPI）是小端、RSACng 是大端，`[RSA]::Create()`
    在不同机器/框架上可能落到不同实现。**不猜**：两种解释都算一遍，取能通过
    RSA 自洽性校验（_params_ok）的那个（坑 K2 三轮：猜小端导致 KEY_VALUES_
    MISMATCH——证书里的公钥和私钥数值对不上）。
    """
    if not b64:
        return b"\x00"
    raw = base64.b64decode(b64)
    if reverse:
        raw = raw[::-1]
    stripped = raw.lstrip(b"\x00") or b"\x00"
    if stripped[0] & 0x80:                # DER INTEGER：最高位为 1 要补 0x00 防负数
        stripped = b"\x00" + stripped
    return stripped


def _params_ok(v):
    """RSA 参数自洽性（OpenSSL 装载时做的同族检查，这里提前到 Python 侧）：
    n = p·q、e 合法、d < n、dp/dq 与 d 模上同余、qi 真是 q⁻¹ mod p。
    端序猜错时这些全部不成立——就是靠这一点自动判端序。"""
    try:
        n, e, d = v["n"], v["e"], v["d"]
        p, q = v["p"], v["q"]
        return (p > 2 and q > 2 and p != q and p * q == n
                and 1 < e < n and e % 2 == 1 and 0 < d < n
                and v["dp"] == d % (p - 1)
                and v["dq"] == d % (q - 1)
                and (v["qi"] * q) % p == 1)
    except Exception:
        return False


def _rsa_ints(params):
    """PowerShell 的参数 JSON → 自洽的大端整数 dict；两种端序都不自洽 = 报错。"""
    keys = ("n", "e", "d", "p", "q", "dp", "dq", "qi")
    for reverse in (True, False):
        v = {k: int.from_bytes(_int(params[k], reverse), "big")
             for k in keys}
        if _params_ok(v):
            return {k: _int(params[k], reverse) for k in keys}
    raise RemoteCertError("生成的密钥材料自洽性校验没过（这不常见）；"
                          "可在设置里改用明文 HTTP（仅建议局域网内使用）。")


def _der_len(n):
    if n < 0x80:
        return bytes([n])
    out = b""
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    return bytes([0x80 | len(out)]) + out


def _der(tag, payload):
    return bytes([tag]) + _der_len(len(payload)) + payload


def _pem(tag, der):
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (tag, "\n".join(lines), tag)


def _key_pem(ints):
    """自洽的 RSA 参数（_rsa_ints 的产物）→ PKCS#1 RSAPrivateKey PEM（RFC 8017）。

    结构 = **一个** SEQUENCE 里平铺 version(0) + 9 个 INTEGER —— 不是
    "INTEGER + 嵌套 SEQUENCE"（首版把 8 个整数包进了内层 SEQUENCE，
    OpenSSL 当场拒载：[SSL] PEM lib，坑 K2）。"""
    body = _der(0x02, b"\x00") + b"".join(
        _der(0x02, ints[k])
        for k in ("n", "e", "d", "p", "q", "dp", "dq", "qi"))
    return _pem("RSA PRIVATE KEY", _der(0x30, body))


def _fingerprint(der):
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def cert_state():
    """现有证书可不可用：(cert 存在, key 存在, 指纹或 "")。"""
    ok_c = os.path.isfile(CERT_PEM)
    ok_k = os.path.isfile(KEY_PEM)
    fp = ""
    if ok_c:
        try:
            with open(CERT_PEM, "rb") as f:
                txt = f.read().decode("ascii", "replace")
            b64 = "".join(l for l in txt.splitlines()
                          if not l.startswith("-----"))
            fp = _fingerprint(base64.b64decode(b64))
        except Exception:
            fp = ""
    return ok_c, ok_k, fp


def _loadable(cert_path, key_path):
    """真加载器验收：OpenSSL 能不能吃这一对（坑 K2 的教训——PEM 形状对
    不代表结构对，只有 `load_cert_chain` 说了算）。"""
    try:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        return True
    except Exception:
        return False


def ensure_cert(runner=None):
    """确保证书就位，返回 {cert, key, fingerprint, generated}。

    已有**且真加载器验收通过**的一对直接复用（generated=False）；缺失或
    加载不过（老版本编坏过的 key 也算）才重新生成——重新生成 = 指纹变，
    浏览器会再警告一次，所以能用就不重造。新生成后仍加载不过 → 人话报错，
    绝不让坏文件装作可用。
    """
    ok_c, ok_k, fp = cert_state()
    if ok_c and ok_k and fp and _loadable(CERT_PEM, KEY_PEM):
        return {"cert": CERT_PEM, "key": KEY_PEM, "fingerprint": fp,
                "generated": False}
    try:
        out = (runner or _run_ps)()
    except RemoteCertError:
        raise
    except Exception:
        raise RemoteCertError("生成 HTTPS 证书失败（本机安全软件或策略可能拦了"
                              " PowerShell）；可在设置里改用明文 HTTP（仅建议"
                              "局域网内使用）。")
    try:
        params = json.loads(out)
    except Exception:
        raise RemoteCertError("生成 HTTPS 证书时得到无法解析的结果，已放弃；"
                              "可在设置里改用明文 HTTP（仅建议局域网内使用）。")
    missing = [k for k in ("cert", "n", "e", "d", "p", "q", "dp", "dq", "qi")
               if not params.get(k)]
    if missing:
        raise RemoteCertError("生成 HTTPS 证书时缺少关键材料（%s），已放弃；"
                              "可在设置里改用明文 HTTP（仅建议局域网内使用）。"
                              % "、".join(missing[:3]))
    cert_der = base64.b64decode(params["cert"])
    key_text = _key_pem(_rsa_ints(params))
    cert_text = _pem("CERTIFICATE", cert_der)
    try:
        os.makedirs(CERT_DIR, exist_ok=True)
        with open(KEY_PEM, "w", encoding="ascii") as f:
            f.write(key_text)
        with open(CERT_PEM, "w", encoding="ascii") as f:
            f.write(cert_text)
        if os.name == "nt":
            # 尽力收紧 key 的 ACL（不校验结果：收紧失败不拦功能）。
            # ⚠ 用户名必须在 Python 这边展开：`subprocess.run` 不走 shell，
            # 字面量 "%USERNAME%:F" 会原样交给 icacls → "没有映射到账户名和安全
            # 标识符" → 整条命令失败，而 capture_output 把 stderr 吞了 ⇒ 私钥的
            # 权限**一次都没收紧过**（装在 Program Files 下时其他本机账户可读）。
            _me = str(os.environ.get("USERNAME") or "").strip()
            if _me:
                subprocess.run(["icacls", KEY_PEM, "/inheritance:r", "/grant:r",
                                "%s:F" % _me], capture_output=True)
    except Exception as e:
        raise RemoteCertError("证书文件写不进程序目录（%s）——检查目录权限后重试。" % e)
    # 生成路径不做 load_cert_chain 验收：真加载器要证书与密钥**一起**吃，
    # 而自检的夹具证书是假 DER（真加载必挂在证书上，坑 K2 二轮）。
    # 真材料下的加载验收在自检 [6]（真 PowerShell 生成 → 真加载断言）；
    # 复用路径的 _loadable 自愈管住"文件后来坏了/老版本编坏过"这两类。
    return {"cert": CERT_PEM, "key": KEY_PEM,
            "fingerprint": _fingerprint(cert_der), "generated": True}
