# -*- coding: utf-8 -*-
"""llm_console.connection.remote_wfd — 无线直连组（Wi-Fi Direct GO）控制器。

2026-10-13（99 K16；评估与真机实测见工作区《设备直连-评估与方案.md》）：
「仅设备直连」档的电脑侧链路。Windows 侧的实现 = 一个**自主组主**
（`WiFiDirectAdvertisementPublisher` + `IsAutonomousGroupOwnerEnabled` +
`LegacySettings` 传统 SSID/密码）：手机**像连普通 Wi-Fi 一样加入**（不需要
装任何 App），链路**不共享任何上游**（手机不会把本机当代理——实测：全部
网卡 IPv4 转发 Disabled、直连网卡无 DNS、无 ICS 共享），组主地址固定
（真机三次实测 192.168.137.1，Windows 的 ICS 默认子网）。

分工（同 `core/remote_cert.py` 的 PS 桥纪律：内嵌脚本 + EncodedCommand、
零管理员、失败人话）：

  PowerShell（**长驻子进程**）
    记电台原状态 → 开电台 → 起组 → 看护（每 0.4 秒查一次：STOP 文件出现 /
    父进程没了）→ 收尾（`Stop()` + 电台还原）。
  Python
    `start_go()` 起进程并等组主地址就位；`stop_go()` 写 STOP 文件让脚本
    自己收尾（能还原电台）；`wait=False` = 关窗路径的快速收组（不等，
    后台散净，见函数 docstring）。**应用崩了也不残留**：脚本看护父进程
    PID，父进程一没就自己收工。

打桩点（自检纪律，坑 34）：`_popen`（真起进程）与 `remote_net.wfd_go_addr`
（地址就位判据）——自检不起真进程、不开真电台。
"""

import base64
import os
import subprocess
import time

from ..core.config import APP_DIR
from . import remote_net
from .. import texts

# 直连组的组名 / 密码（ASCII；WPA2 口径——本机驱动 P2P Sae 不支持，实测）。
# 文案待 W 逐字过目；将来要用户可改就加 cfg 键（wfd_ssid / wfd_pass）喂进
# `start_go(ssid=..., passphrase=...)`（签名已就位）。
GO_SSID = "LLM-Chat-Direct"
GO_PASS = "llmchat-direct"

# STOP 标记与组状态都落在程序目录（与 remote_cert/ 同款就地）。
WFD_DIR = os.path.join(APP_DIR, "remote_wfd")

# 起组后等组主地址就位的最长秒数（实测约 3~5 秒；慢机器放宽到 25）。
GO_READY_TIMEOUT = 25.0


class WfdGoError(Exception):
    """直连组起不来；文案已是人话，可直接给用户看（不套类型前缀）。"""


# PowerShell 脚本（纯 ASCII；值全走环境变量传——SSID/密码里出现引号也不会有
# 注入面）。收尾 = Stop() + 电台还原（只在"本进程把它打开过"时还原）。
_PS = r"""
$ErrorActionPreference = 'Stop'
$stopFile = $env:LLM_WFD_STOP
$parentPid = 0
try { $parentPid = [int]$env:LLM_WFD_PARENT } catch { $parentPid = 0 }
$ssid = $env:LLM_WFD_SSID; if (-not $ssid) { $ssid = 'LLM-Chat-Direct' }
$pass = $env:LLM_WFD_PASS; if (-not $pass) { $pass = 'llmchat-direct' }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($t, $rt) {
  $asTask = $asTaskGeneric.MakeGenericMethod($rt)
  $netTask = $asTask.Invoke($null, @($t))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
$null = [Windows.Devices.Radios.Radio, Windows.Devices.Radios, ContentType=WindowsRuntime]
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$radio = $radios | Where-Object { $_.Kind -eq [Windows.Devices.Radios.RadioKind]::WiFi } | Select-Object -First 1
$radioWasOn = $false
if ($radio) {
  if ($radio.State -eq [Windows.Devices.Radios.RadioState]::On) { $radioWasOn = $true }
  else { $null = Await ($radio.SetStateAsync([Windows.Devices.Radios.RadioState]::On)) ([Windows.Devices.Radios.RadioAccessStatus]) }
}
$pub = $null
try {
  $null = [Windows.Devices.WiFiDirect.WiFiDirectAdvertisementPublisher, Windows.Devices.WiFiDirect, ContentType=WindowsRuntime]
  $null = [Windows.Security.Credentials.PasswordCredential, Windows.Security.Credentials, ContentType=WindowsRuntime]
  $pub = New-Object Windows.Devices.WiFiDirect.WiFiDirectAdvertisementPublisher
  $adv = $pub.Advertisement
  $adv.IsAutonomousGroupOwnerEnabled = $true
  $legacy = $adv.LegacySettings
  $legacy.Ssid = $ssid
  $legacy.Passphrase = (New-Object Windows.Security.Credentials.PasswordCredential -ArgumentList 'llm-chat-wfd','go',$pass)
  $legacy.IsEnabled = $true
  $pub.Start()
  if ($pub.Status -ne 'Started') { throw ('publisher status: ' + $pub.Status) }
} catch {
  if ($pub -and $pub.Status -eq 'Started') { try { $pub.Stop() } catch {} }
  if ($radio -and -not $radioWasOn) {
    try { $null = Await ($radio.SetStateAsync([Windows.Devices.Radios.RadioState]::Off)) ([Windows.Devices.Radios.RadioAccessStatus]) } catch {}
  }
  throw
}
while ($true) {
  Start-Sleep -Milliseconds 400
  if ($stopFile -and (Test-Path -LiteralPath $stopFile)) { break }
  if ($parentPid -gt 0 -and -not (Get-Process -Id $parentPid -ErrorAction SilentlyContinue)) { break }
}
if ($pub.Status -eq 'Started') { try { $pub.Stop() } catch {} }
if ($radio -and -not $radioWasOn) {
  try { $null = Await ($radio.SetStateAsync([Windows.Devices.Radios.RadioState]::Off)) ([Windows.Devices.Radios.RadioAccessStatus]) } catch {}
}
"""

_proc = None                      # 长驻 PowerShell 子进程（None = 没起过 / 已收）


def _spawn_flags():
    """独立出来只为打桩（自检不真起进程）。"""
    return 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def _popen(cmd, env):
    """真起进程；独立出来只为打桩（坑 34 纪律）。"""
    return subprocess.Popen(cmd, env=env, creationflags=_spawn_flags(),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def stop_file():
    """STOP 标记的落点（脚本看护它；上轮没收工的标记由 start_go 清）。"""
    return os.path.join(WFD_DIR, "go.stop")


def running():
    return _proc is not None and _proc.poll() is None


def start_go(ssid=None, passphrase=None, timeout=GO_READY_TIMEOUT):
    """起直连组并等组主地址就位；已在跑 = 直接成功。失败抛 WfdGoError。"""
    global _proc
    if running():
        return True
    if os.name != "nt":
        raise WfdGoError(texts.RWFD_A0)
    try:
        os.makedirs(WFD_DIR, exist_ok=True)
        if os.path.exists(stop_file()):
            os.remove(stop_file())     # 上一轮的收工标记先清掉，别让新组秒退
    except Exception:
        pass
    env = dict(os.environ)
    env["LLM_WFD_STOP"] = stop_file()
    env["LLM_WFD_PARENT"] = str(os.getpid())
    env["LLM_WFD_SSID"] = str(ssid or GO_SSID)
    env["LLM_WFD_PASS"] = str(passphrase or GO_PASS)
    cmd = ["powershell", "-NoProfile", "-NonInteractive",
           "-EncodedCommand",
           base64.b64encode(_PS.encode("utf-16le")).decode("ascii")]
    try:
        _proc = _popen(cmd, env)
    except FileNotFoundError:
        _proc = None
        raise WfdGoError(texts.RWFD_A0_2)
    except Exception as e:
        _proc = None
        raise WfdGoError(texts.RWFD_CAT % e)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _proc.poll() is not None:
            _proc = None
            raise WfdGoError(texts.RWFD_A0_3)
        if remote_net.wfd_go_addr():
            return True
        time.sleep(0.5)
    stop_go()
    raise WfdGoError(texts.RWFD_CAT_2
                     % int(timeout))


def settle_stale(timeout=3.0):
    """等一个**不是本进程起的**直连组散掉（上一个实例关窗时的"快速收组"
    要 1~2 秒才清干净）。没有地址 = 立即返回。返回是否已散掉。

    为什么需要：关窗路径不等收尾（见 `stop_go(wait=False)`），于是"关了
    秒重开"的窗口里地址还在——直接绑上去就是绑一个正在死的地址（服务
    起来了又立刻没）。这里等它散，之后按"没链路"的正常流程重起新组。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not remote_net.wfd_go_addr():
            return True
        time.sleep(0.2)
    return not remote_net.wfd_go_addr()


def stop_go(timeout=8.0, wait=True):
    """停直连组：写 STOP 文件让脚本自己收尾（Stop + 电台还原）。

    `wait=True`（默认）等它退干净——自检与真机探针的确定性路径用它（等过
    了才好下断言）。`wait=False` = **UI 路径（关窗 / 设置页关服务）的快速
    收组**：写完标记立刻返回（实测 0.06s，原来同步等是 4s），脚本 0.4 秒内
    看到标记、约一两秒内自行散干净；界面不等它。立刻重开由 `settle_stale`
    兜住（关窗提速轮 2026-10-13）。`wait=False` 不兜底 kill——硬杀会跳过
    电台还原，宁可让它自己收。

    没起过 / 已收 = 空操作。
    """
    global _proc
    if _proc is None:
        return
    try:
        with open(stop_file(), "w", encoding="ascii") as f:
            f.write("stop\n")
    except Exception:
        pass
    if not wait:
        _proc = None
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _proc.poll() is not None:
            break
        time.sleep(0.1)
    if _proc is not None and _proc.poll() is None:
        # 兜底：硬杀（这一刀下去电台还原做不了，但组会随进程消亡）
        try:
            _proc.kill()
        except Exception:
            pass
    _proc = None
