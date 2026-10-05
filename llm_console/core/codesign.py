# -*- coding: utf-8 -*-
"""llm_console.core.codesign — 把「本程序 exe 自己的签名」列入本机可信名单

本程序的 exe 是**自签名**的（证书与私钥由维护者自己生成，没买付费 CA）。没把这张证书
列入信任的机器上，Windows 会把它当"未知发布者"，SmartScreen 第一次会拦一下。这个模块
只做一件事：读出**当前 exe 自己的签名证书**，写进**当前用户**的「受信任的根证书颁发机构」。

⚠ 三条边界（都是有意为之，别顺手放宽）：
  · 只写 **CurrentUser** 存储 —— **不需要管理员权限**，也只对这台机器的这个 Windows 用户生效；
  · 别人下载后仍然要**自己**执行一次同样的操作（信任是逐机逐用户的事，替不了）；
  · **信了这张证书 = 这台机器会信所有用它签的东西** —— 所以界面上必须先让用户确认
    「软件是从 GitHub 直接下载的」（危险点在这一句，不在实现里）。

零界面依赖（不 import tkinter）；所有外部动作都经 `runner` 注入，自检里换掉就不碰真 certutil。
"""

import json
import os
import subprocess

from . import selfupdate

# 读签名 / 列信任状态 / 写入信任都走本机系统自带的 PowerShell 与 certutil（都随 Windows 分发）
_TIMEOUT = 60
_UNSIGNED = "NOSIGN"       # 子进程约定：这个文件没有签名


def self_exe():
    """当前进程自己的 exe 路径；源码运行时是 None（与自替换**同一处**判据）。"""
    return selfupdate.cur_exe_path()


def _powershell():
    return "powershell.exe" if os.name == "nt" else "powershell"


def _real_run(script):
    """跑一段 PowerShell → `(returncode, stdout文本)`。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=_TIMEOUT, creationflags=flags)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def _run(script, runner=None):
    return (runner or _real_run)(script)


def _q(path):
    """塞进 PowerShell 单引号字符串：内部的 ' 要写两遍。"""
    return str(path).replace("'", "''")


def _read_script(path):
    return (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$s=Get-AuthenticodeSignature -LiteralPath '%s';"
        "if(-not $s.SignerCertificate){Write-Output '%s';exit 0};"
        "$c=$s.SignerCertificate;"
        "$o=[ordered]@{subject=$c.Subject;thumbprint=$c.Thumbprint;"
        "not_after=$c.NotAfter.ToString('yyyy-MM-dd');"
        "trusted=(Test-Path ('Cert:\\CurrentUser\\Root\\'+$c.Thumbprint))};"
        "ConvertTo-Json -Compress -InputObject $o"
    ) % (_q(path), _UNSIGNED)


def _trust_script(path):
    return (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$s=Get-AuthenticodeSignature -LiteralPath '%s';"
        "if(-not $s.SignerCertificate){Write-Output '%s';exit 3};"
        "$c=$s.SignerCertificate;"
        "$f=Join-Path $env:TEMP ('llmchat-codesign-'+[guid]::NewGuid().ToString('N')+'.cer');"
        "Export-Certificate -Cert $c -FilePath $f -Force | Out-Null;"
        "$o=certutil -user -addstore -f Root $f 2>&1;"
        "Remove-Item $f -Force -ErrorAction SilentlyContinue;"
        "if($LASTEXITCODE -ne 0){Write-Output $o;exit $LASTEXITCODE};"
        "Write-Output 'TRUSTED'"
    ) % (_q(path), _UNSIGNED)


def read_cert(exe=None, runner=None):
    """读 exe 的签名证书 → dict；**没有签名**或读不出来 → None。

    字段：`subject`（证书主体）、`thumbprint`（指纹）、`not_after`（到期日）、
    `trusted`（是否已在本机「受信任的根」里）。
    """
    path = exe or self_exe()
    if not path or not os.path.isfile(path):
        return None
    try:
        code, out = _run(_read_script(path), runner)
    except Exception:
        return None
    out = (out or "").strip()
    if code != 0 or not out or out == _UNSIGNED:
        return None
    try:
        info = json.loads(out)
    except Exception:
        return None
    if not isinstance(info, dict) or not info.get("thumbprint"):
        return None
    info["trusted"] = bool(info.get("trusted"))
    return info


def trust(exe=None, runner=None):
    """把 exe 的签名证书写进**当前用户**的「受信任的根」→ `(True, "")` / `(False, 原因)`。

    免管理员（`certutil -user`）；证书只从 exe 自己的签名里取，不认调用方递进来的文件。
    """
    path = exe or self_exe()
    if not path or not os.path.isfile(path):
        return False, "拿不到本程序自己的 exe（源码运行时没有可签名的文件）。"
    try:
        code, out = _run(_trust_script(path), runner)
    except Exception as e:                                     # 系统工具没跑起来
        return False, "调用系统工具失败：%s" % e
    out = (out or "").strip()
    if code == 0 and out.endswith("TRUSTED"):
        return True, ""
    if out == _UNSIGNED:
        return False, "本程序这份 exe 没有签名，没有可列入的证书。"
    return False, (out or "系统工具返回了错误（退出码 %s）。" % code)
