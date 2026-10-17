# -*- coding: utf-8 -*-
"""llm_console.core.codesign — 把「本程序 exe 自己的签名」列入本机可信名单

本程序的 exe 是**自签名**的（证书与私钥由维护者自己生成，没买付费 CA）。没把这张证书
列入信任的机器上，Windows 会把它当"未知发布者"，SmartScreen 第一次会拦一下。这个模块
只做一件事：读出**当前 exe 自己的签名证书**，写进**当前用户**的「受信任的根证书颁发机构」。

⚠ 边界（都是有意为之，别顺手放宽）：
  · 默认那条路只写 **CurrentUser**（`trust`）—— **不需要管理员权限**，也只对这台机器的这个
    Windows 用户生效；
  · **`trust_machine` 是另一回事**：写 `LocalMachine`（这台机器所有用户）、**要管理员**（走 UAC
    提权），只该由开发者选项里**带严重警告**的那个按钮调用；
  · 别人下载后仍然要**自己**执行一次同样的操作（信任是逐机逐用户的事，替不了）；
  · **信了这张证书 = 这台机器会信所有用它签的东西** —— 所以界面上必须先让用户确认
    「软件是从 GitHub 直接下载的」（危险点在这一句，不在实现里）。

零界面依赖（不 import tkinter）；所有外部动作都经 `runner` 注入，自检里换掉就不碰真 certutil。
"""

import json
import os
import re
import subprocess

from . import selfupdate
from .. import texts

# 读签名 / 列信任状态 / 写入信任都走本机系统自带的 PowerShell 与 certutil（都随 Windows 分发）
_TIMEOUT = 60
_UNSIGNED = "NOSIGN"       # 子进程约定：这个文件没有签名


def self_exe():
    """当前进程自己的 exe 路径；源码运行时是 None（与自替换**同一处**判据）。"""
    return selfupdate.cur_exe_path()


def _powershell():
    return "powershell.exe" if os.name == "nt" else "powershell"


def _real_run(script):
    """跑一段 PowerShell → `(returncode, stdout文本)`。

    stdout 为空时把 stderr 折进来（R-110）：执行策略 / 语法类故障只会往 stderr 写，
    原来丢掉之后界面只剩"系统工具没有任何输出"，等于零线索。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=_TIMEOUT, creationflags=flags)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if not out.strip() and err.strip():
        out = err
    return p.returncode, out


def _humanize_tool_output(out, code):
    """系统工具输出 → 可展示文本（R-110）：把 `C:\\Users\\<名>` 一类本机路径与用户名
    掩掉、截断到 6 行 / 400 字——certutil / PowerShell 报错会把它们原样倒出来。"""
    s = str(out or "").strip()
    if not s:
        return texts.CS_CAT % code
    s = re.sub(r"(?i)([a-z]:[\\/]users[\\/])[^\\/\s]+", r"\1…", s)
    lines = [ln for ln in s.splitlines() if ln.strip()][:6]
    s = "\n".join(lines)
    return s[:400] + ("…" if len(s) > 400 else "")


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
        "trusted=(Test-Path ('Cert:\\CurrentUser\\Root\\'+$c.Thumbprint));"
        "trusted_machine=(Test-Path ('Cert:\\LocalMachine\\Root\\'+$c.Thumbprint))};"
        "ConvertTo-Json -Compress -InputObject $o"
    ) % (_q(path), _UNSIGNED)


def _trust_machine_script(path):
    return (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$s=Get-AuthenticodeSignature -LiteralPath '%s';"
        "if(-not $s.SignerCertificate){Write-Output '%s';exit 3};"
        "$c=$s.SignerCertificate;"
        "$f=Join-Path $env:TEMP ('llmchat-codesign-'+[guid]::NewGuid().ToString('N')+'.cer');"
        "Export-Certificate -Cert $c -FilePath $f -Force | Out-Null;"
        # 机器级要管理员：把内层命令编成 Base64 交给提权进程，绕开命令行引号 / 空格的坑
        "$inner='certutil -addstore -f Root \"'+$f+'\"';"
        "$b64=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner));"
        "try{Start-Process -Verb RunAs -Wait -FilePath powershell "
        "-ArgumentList @('-NoProfile','-EncodedCommand',$b64)}"
        "catch{Remove-Item $f -Force -ErrorAction SilentlyContinue;Write-Output 'UAC-DENIED';exit 5};"
        "Remove-Item $f -Force -ErrorAction SilentlyContinue;"
        "if(Test-Path ('Cert:\\LocalMachine\\Root\\'+$c.Thumbprint))"
        "{Write-Output 'TRUSTED';exit 0};"
        "Write-Output 'NOT-INSTALLED';exit 6"
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


def read_cert_detail(exe=None, runner=None):
    """读 exe 的签名证书 → `(info 或 None, why)`。

    info 为 None 时 `why` 说明是哪一种"读不出来"（2026-10-06 补的区分，全静默的话
    企业机上 PowerShell 被禁的用户既收不到询问、也收不到"判断不了"）：
      · `why == ""`  —— 这份 exe 确实**没有签名**（不是故障，不用解释）；
      · 非空人话    —— **这台机器判断不了**（工具调不动 / 退出码异常 / 结果认不出来），
                        调用方应该把这句话说给用户，而不是装作什么都没发生。
    `read_cert()` 是它的窄包装（老契约：拿不到一律 None）。
    """
    path = exe or self_exe()
    if not path or not os.path.isfile(path):
        return None, ""
    try:
        code, out = _run(_read_script(path), runner)
    except Exception as e:                                     # 工具没跑起来
        return None, texts.CS_CAT_2 % e
    out = (out or "").strip()
    if not out:
        return None, texts.CS_L1
    if out == _UNSIGNED:
        return None, ""
    if code != 0:
        return None, texts.CS_CAT_3 % code
    try:
        info = json.loads(out)
    except Exception:
        return None, texts.CS_L1_2
    if not isinstance(info, dict) or not info.get("thumbprint"):
        return None, texts.CS_L1_3
    info["trusted"] = bool(info.get("trusted"))
    info["trusted_machine"] = bool(info.get("trusted_machine"))
    return info, ""


def read_cert(exe=None, runner=None):
    """读 exe 的签名证书 → dict；**没有签名**或读不出来 → None。

    字段：`subject`（证书主体）、`thumbprint`（指纹）、`not_after`（到期日）、
    `trusted`（是否已在本机「受信任的根」里）。想区分"没签名"与"机器判不了"，
    用 `read_cert_detail`。
    """
    return read_cert_detail(exe, runner)[0]


def trust(exe=None, runner=None):
    """把 exe 的签名证书写进**当前用户**的「受信任的根」→ `(True, "")` / `(False, 原因)`。

    免管理员（`certutil -user`）；证书只从 exe 自己的签名里取，不认调用方递进来的文件。
    """
    path = exe or self_exe()
    if not path or not os.path.isfile(path):
        return False, texts.CS_L1_4
    try:
        code, out = _run(_trust_script(path), runner)
    except Exception as e:                                     # 系统工具没跑起来
        return False, texts.CS_CAT_4 % e
    out = (out or "").strip()
    if code == 0 and out.endswith("TRUSTED"):
        return True, ""
    if out == _UNSIGNED:
        return False, texts.CS_L1_5
    return False, _humanize_tool_output(out, code)


def _untrust_script(path):
    """撤销**当前用户**的信任：`certutil -user -delstore Root <指纹>`（免管理员）。

    指纹从 exe 自己的签名里现取，不从调用方递进来 —— 与 trust() 同一条纪律：
    能删的必须正好是这张 exe 的证书，删错一张等于把别的软件的信任也撤了。
    删除后**回读一次**判成败（certutil 的退出码在不同 Windows 版本上不一致）。
    """
    return (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$s=Get-AuthenticodeSignature -LiteralPath '%s';"
        "if(-not $s.SignerCertificate){Write-Output '%s';exit 3};"
        "$t=$s.SignerCertificate.Thumbprint;"
        "$o=certutil -user -delstore Root $t 2>&1;"
        "if(Test-Path ('Cert:\\CurrentUser\\Root\\'+$t)){Write-Output $o;Write-Output 'STILL-TRUSTED';exit 4};"
        "Write-Output 'UNTRUSTED'"
    ) % (_q(path), _UNSIGNED)


def untrust(exe=None, runner=None):
    """把这张 exe 的签名证书从**当前用户**的受信任根里删掉 → `(True, "")` / `(False, 原因)`。

    与 `trust()` 对称（16 §19.3）：原来只有"加"没有"撤"，用户后悔只能自己开
    `certmgr.msc`。这里只动 CurrentUser（免管理员，与 trust 同一层级）；
    机器级那份是管理员写进去的，也只能由管理员在系统里删 —— 不在这里假装能撤。
    """
    path = exe or self_exe()
    if not path or not os.path.isfile(path):
        return False, texts.CS_L1_4
    try:
        code, out = _run(_untrust_script(path), runner)
    except Exception as e:
        return False, texts.CS_CAT_4 % e
    out = (out or "").strip()
    if code == 0 and out.endswith("UNTRUSTED"):
        return True, ""
    if out == _UNSIGNED:
        return False, texts.CS_L1_5
    if out.endswith("STILL-TRUSTED"):
        return False, texts.CS_L1_8
    return False, _humanize_tool_output(out, code)


def trust_machine(exe=None, runner=None):
    """把 exe 的签名证书写进**本机（所有用户）**的受信任根 → `(True, "")` / `(False, 原因)`。

    ⚠ 这一条**需要管理员**：脚本用 `Start-Process -Verb RunAs` 拉起提权进程，用户在 UAC 上点
    "否"就是放弃（回 `UAC-DENIED`）。成败**不看**提权进程的退出码（拿不到），而是回读
    `Cert:\\LocalMachine\\Root` 判断 —— 只该由开发者选项里带严重警告的那个按钮调用。
    """
    path = exe or self_exe()
    if not path or not os.path.isfile(path):
        return False, texts.CS_L1_4
    try:
        code, out = _run(_trust_machine_script(path), runner)
    except Exception as e:
        return False, texts.CS_CAT_4 % e
    out = (out or "").strip()
    if code == 0 and out.endswith("TRUSTED"):
        return True, ""
    if out == _UNSIGNED:
        return False, texts.CS_L1_5
    if out == "UAC-DENIED":
        return False, texts.CS_L1_6
    if out == "NOT-INSTALLED":
        return False, texts.CS_L1_7
    return False, _humanize_tool_output(out, code)
