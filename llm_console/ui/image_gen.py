# -*- coding: utf-8 -*-
"""llm_console.ui.image_gen — 界面 Mixin：聊天流内生图链路（进度行、结果内嵌、取消）"""

import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

from .. import texts
from ..core import capability, cloudjobs, config, providers, sdprofile
from . import theme
from ..core.media import (batch_count_of, batch_out_paths, build_img_cmd,
                          edit_strength, img_out_dir, mask_hint,
                          plan_ref_images, resolve_img_files,
                          resolve_img_model_path, salvage_products)
from ..connection import cloud_media
from . import imgdecode


class ImageGenMixin:
    """App 的聊天流内生图职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    # ---- 聊天流内生图（方案 A：页面形态不变，输出内嵌对话流）----
    def _start_chat_image(self, prompt, ref_img=None, ref_imgs=None, ref_mode="",
                          doc=None):
        """带图两档都从这里进：`ref_mode` 空 = 按当前模型的默认档。

        `ref_imgs` 是多图列表；旧的单张 `ref_img` 仍能用（内部并入列表）。
        `doc` = 文本附件（set_file_attachment 的成品块）：回显后并进提示词再发
        （W 2026-10-07 裁决，合并与 📄 行见 chat._doc_merge）。
        """
        # 云端生图走服务商原生接口（二期），不启动本地 sd-cli：两套链路在入口就分开，
        # 免得拿云端模型名去喂本地引擎（那是 v32 拦下的那个错）。
        if providers.is_cloud(self.cfg):
            return self._start_cloud_image(prompt, ref_imgs=ref_imgs or ([ref_img] if ref_img else []),
                                           ref_mode=ref_mode, doc=doc)
        sd = self.cfg.get("sd_dir", "")
        cli = os.path.join(sd, "sd-cli.exe")
        if not os.path.isfile(cli):
            self._append(texts.IMG_NO_ENGINE % (cli or texts.MEDIA_NO_DIR), "error")
            return
        # 扩散模型一律走统一出口（绝对路径）：**菜单里选中的那个就是用的那个**
        # （2026-10-16 收口：默认生图模型 / 扫描第一个两条旧兜底已删——坏选中报错让重选，
        #  不再静默换模型；resolve 返回空串即"当前选中文件不在了"，见 media.resolve_img_model_path）。
        diffusion = resolve_img_model_path(self.cfg)
        if not diffusion:
            self._append(texts.IMG_NO_MODEL, "error")
            return
        # 配套文件由模型族决定（Qwen-Image 要 LLM+VAE，Flux 要 clip_l+t5xxl+VAE，SDXL
        # 单文件就够了），所以按 sdprofile 的 require 预检；缺件就地报错，别把不存在
        # 的路径交给引擎再吃一次"退出码 1"（生图链路没有失败回显，黑盒更难查）。
        files = resolve_img_files(self.cfg, diffusion)
        # 人工指定的配套件没找到：逐条点名（R-20——曾经静默换成自动发现的文件，
        # 用错 VAE/编码器只会表现为出图糊/风格错，用户查不到这项配置头上）
        for _s, _w in (files.get("manual_missing") or []):
            self._append(texts.MEDIA_MANUAL_MISSING % (sdprofile.slot_name(_s), _w), "error")
        missing = sdprofile.missing_slots(files["family"], files)
        if missing:
            self._append(texts.IMG_MISSING_SLOTS
                         % (sdprofile.label_of(files["family"]), "、".join(missing)), "error")
            return
        # 带图两档：**在 Popen 之前**定档并判这一族走不走得通。认不出族 / 这一族没这条通路
        # 就地报错，一个字都不丢 —— 别把命令拼好再让引擎吃一次"退出码 1"。
        refs = [p for p in (list(ref_imgs or []) + ([ref_img] if ref_img else []))
                if p and os.path.isfile(p)]
        modes = capability.resolve_modes(self.cfg, files["family"])
        mode = str(ref_mode or "").strip()
        if refs and mode and not modes.get(mode, {}).get("ok"):
            self._append(texts.IMG_MODE_WHY % modes[mode]["why"], "error")
            return
        mode = mode or capability.default_ref_mode(self.cfg, files["family"])
        used, dropped, drop_note = plan_ref_images(refs, mode)
        st_value = None              # 底图重绘档才算强度；None = 交给 build_img_cmd 自己判
        # 用户涂的遮罩（白 = 要重绘）：只跟底图重绘档的第一张图走（那一档只认一张）。
        mask_path = ""
        if used and mode == capability.MODE_EDIT:
            _mp = self._mask_path_of(used[0])
            mask_path = _mp if (_mp and os.path.isfile(_mp)) else ""
        if not refs and mode:
            mode = ""                      # 纯文生图不带模式，省得回显与日志里出现无意义的档位
        # "必闪退"档启动确认（2026-10-06）：摆在消费输入框之前，点否一个字都不丢（坑 133）
        if not self._confirm_fatal_perf(diffusion, "image", files=files):
            return
        try:
            steps = max(1, int(self.cfg.get("img_steps", 20) or 20))
        except Exception:
            steps = 20
        size = str(self.cfg.get("img_size", "1024x1024"))
        try:
            cfg_scale = float(self.cfg.get("img_cfg", 2.5) or 2.5)
        except Exception:
            cfg_scale = 2.5
        # 种子跟着设置页的 img_seed 走（-1 = 随机）。不用 `or -1` 兜底：0 是合法种子，
        # 会被 falsy 判断吞成随机（与云端生图路径同一教训）。
        try:
            seed = int(self.cfg.get("img_seed", -1))
        except Exception:
            seed = -1
        outdir = img_out_dir(self.cfg)   # <产物文件夹>/本地/image（2026-10-06 起）
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, time.strftime("img_%Y%m%d_%H%M%S") + ".png")
        # 多次生成（×n，W 2026-10-13）：只在本地生图链路生效（云端在上面已分流 ——
        # n 次请求 = n 次真实计费，"省一次加载"在云端也不成立）。归一与夹取都在
        # App._normalize_count → media.batch_count_of 一处；归一的生效值回写框。
        try:
            count = max(1, int(self._normalize_count() or 1))
        except Exception:
            count = 1
        # 期望产物：n=1 = [out]（逐字不变）；n>1 = 主干_1.._n（与引擎命名同源）
        expected = batch_out_paths(out, count)

        # 预检都过了才消费输入框与附件：清空摆在调用方（send_message）的话，
        # "没找到引擎 / 缺配套件"这类失败会把提示词与参考图一起吃掉（坑 133）
        self.input.delete("1.0", "end")
        self.clear_attachment()
        # 消费掉输入框后立即重算星星显隐（R-89）：原来要靠 _render_status 的
        # 3 秒兜底才消失，期间点它没反应，看着像坏按钮
        self._opt_star_refresh()
        self._append(texts.MEDIA_USER_PREFIX + prompt + "\n", "user")
        prompt = self._doc_merge(prompt, doc)
        for p in used:
            self._append_image(p, max_w=320)
        if used:
            # 措辞一律问 capability（模式名只有那一处定义），不自己写"图生图"
            self._append(texts.IMG_REF_LINE
                         % (capability.MODE_LABEL.get(mode, texts.IMG_REF_GENERIC), len(used),
                            "" if not drop_note else texts.IMG_DROP_PAREN % drop_note), "meta")
            if mode == capability.MODE_EDIT:
                # 强度在这里算一次、拼命令行时原样交出去（`strength=`）：
                # 回显说 0.7 而引擎收到 0.9 是这类 bug 最难查的一种。
                # 原来这里读 `cfg["img_strength"]` 且兜底写 0.6，与命令行侧的 0.9 对不上。
                _st_tier, st_value, st_why = edit_strength(
                    self.cfg, prompt, tier=self._img_strength_tier)
                # 自动判到"小改"档而这次没带遮罩：给一句指路（判据在 media.mask_hint，
                # W 2026-10-12 拍板；手机网页的回显问的是同一处）
                _hint = mask_hint(_st_tier, auto=not str(
                    self._img_strength_tier or "").strip(),
                    has_mask=bool(mask_path))
                self._append(texts.IMG_STRENGTH
                             % (st_value, st_why,
                                texts.IMG_MASK_TAG if mask_path else "",
                                _hint), "meta")
        self._img_gen += 1
        mygen = self._img_gen
        mark = "imgprog%d" % mygen
        self.chat.mark_set(mark, "end-1c")
        self.chat.mark_gravity(mark, "left")   # 进度文字长在 mark 右侧，mark 钉在行首（gravity=right 会导致堆叠）
        self._img_mark = mark
        self._img_busy = True
        self._img_out = out
        # 每次任务重新开始：末尾缓冲、日志与"这一次是不是云端"都要归零
        self._img_tail = []
        self._img_extra_paths = []
        self._img_expected = expected        # 批量产出枚举（退出判据与"同批"行都读它）
        self._img_batch_i = 0                # 引擎还没报批号
        self._img_batch_n = count
        self._img_log = config.media_log_path(self.cfg, out)   # media_logs，不在产物目录（W 2026-10-11）
        self._cloud_img = False
        self._t0 = time.time()
        self._saw_decode = False
        self._stop_flag = threading.Event()
        self._set_busy_ui(True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            # build_cmd 挪进 try（2026-10-11 排查报告）：busy 已置位后到线程启动前
            # 任何一步抛错都必须复位，否则发送/切模型永久被"任务进行中"拦住
            cmd = build_img_cmd(self.cfg, prompt, out, steps, size, diffusion, cfg_scale,
                                seed, ref_mode=mode, ref_images=used, files=files,
                                strength=st_value, mask=mask_path, batch=count)
            # 完整引擎日志（命令行 + 引擎原话 + 退出码），与生视频链路同形：界面只回显末几行，
            # "引擎说完成了文件在哪"这类问题只能靠这份文件回答（`10` §2 一直是这么写的，
            # 但生图这条链路的落盘此前根本没接上 —— 2026-10-11 补）。
            try:
                with open(self._img_log, "w", encoding="utf-8", errors="replace") as lf:
                    lf.write("=== argv ===\n")
                    for i, a in enumerate(cmd):
                        lf.write("[%2d] %s\n" % (i, a))
                    lf.write("=== cwd=%s ｜ out=%s ｜ 提交于 %s ===\n\n"
                             % (sd, out, time.strftime("%Y-%m-%d %H:%M:%S")))
                    lf.flush()
            except Exception:
                self._img_log = ""
            self._img_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=flags, cwd=sd)
        except Exception as e:
            self._img_busy = False
            self._img_proc = None
            self._set_busy_ui(False)
            self._append(texts.IMG_START_FAIL % e, "error")
            return
        # 日志路径走参数而不是 self._img_log：取消后立刻重开一次任务时，
        # 旧线程会把自己的输出追加进新任务的日志里（与生视频同一处理）
        threading.Thread(target=self._img_reader,
                         args=(self._img_proc, mygen, self._img_log, self._t0,
                               expected),
                         daemon=True).start()

    # ---- 云端生图（二期：服务商原生接口，按 providers.media_api 分派）----
    def _start_cloud_image(self, prompt, ref_img=None, ref_imgs=None, ref_mode="",
                           doc=None):
        provider = providers.current_provider(self.cfg)
        sp = providers.split_cloud_id(self.cfg.get("model", ""))
        model = sp[1] if sp else ""
        # 预检在提交前做完：云端一次请求就是真金白银/套餐额度，别把"缺密钥"这种
        # 问题发给服务端再解析它的 401（与本地链路"Popen 前预检"是同一条纪律）
        err = cloud_media.check(self.cfg, provider, model, "image")
        if err:
            self._append(texts.IMG_CLOUD_ERR % err, "error")
            return
        outdir = config.cloud_media_dir(self.cfg, "image")
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self._append(texts.MEDIA_CLOUD_DIR_FAIL % (texts.IMG_CLOUD_TAG, e), "error")
            return
        out = os.path.join(outdir, time.strftime("img_%Y%m%d_%H%M%S") + ".png")
        # 云端 ×n（C2-3）：只有能力档案允许 n>1 的家会显示控件；这里再归一一次，
        # 防止用户没失焦就直接按发送。固定 1 张的家 max=1，count 恒 1。
        try:
            count = max(1, int(self._normalize_count() or 1))
        except Exception:
            count = 1
        # 预检（check + 建目录）都过了才消费输入框与附件（坑 133，同本地链路）
        self.input.delete("1.0", "end")
        self.clear_attachment()
        self._opt_star_refresh()      # 同 _start_chat_image（R-89）
        self._append(texts.MEDIA_USER_PREFIX + prompt + "\n", "user")
        prompt = self._doc_merge(prompt, doc)
        # 参考图在发送前已由 chat.send_message 过一遍 capability + ref_images_error（含档位
        # 校验），这里只负责显示回对话流并交给 worker（缩略图与本地生图同一套形态）。
        refs = [p for p in (list(ref_imgs or []) + ([ref_img] if ref_img else []))
                if p and os.path.isfile(p)]
        mode = ""                       # 纯文生图不带档位，省得回显与日志里出现无意义的它
        used, drop_note = [], ""
        if refs:
            # 档位：选了且这家支持就用，否则回这家默认档（各家默认档不同，别替它决定）
            mode = str(ref_mode or "").strip()
            if not (mode and providers.supports_ref_mode(provider, mode)):
                mode = providers.ref_image_mode(provider)
            # 云端也走同一份取舍（2026-10-19 云端对齐轮）：底图重绘只送第一张，
            # 主体参考全送（张数上限仍由 ref_images_error 在发送前拦）。原来云端
            # 把 3 张全塞进 content[]，而附件条状态行按本地规则说"只认一张"——
            # 说的和发的对不上（坑 101/102 同族）。
            used, _dropped, drop_note = plan_ref_images(refs, mode)
            for p in used:
                self._append_image(p, max_w=320)
            _name = os.path.basename(used[0])
            if len(used) > 1:
                _name += texts.IMG_ETC
            if drop_note:
                _name += "，" + drop_note
            self._append(texts.IMG_CLOUD_REF_LINE
                         % (capability.MODE_LABEL.get(mode, texts.IMG_REF_GENERIC_CLOUD),
                            len(used), _name), "meta")
        # 费用就近打在对话流里，而且必须打在**提交之前**：生图不弹二次确认（同步、
        # 单次几分钱，每次都问只会让人不看内容按回车），但那不等于不告诉。
        # 单价按模型查，没填就明说"以账单为准"，不编数字。
        self._append(texts.IMG_CLOUD_MODEL_LINE
                     % (model or texts.IMG_UNSELECTED,
                        providers.media_price_note(provider, providers.KIND_IMAGE,
                                                   0, model, count=count)), "meta")
        # 档案说这家没有负向词（MiniMax / 方舟）时，界面上那格已置灰；老配置里
        # 还留着的值不该静默消失，提交前明说一次"已忽略"（C2-1：填了不生效要说话）。
        # 智谱/华为是"未核实"（None）而不是"没有"：这一档同样不发送，说法必须分开
        # （把"不知道"说成"没有"是 11 §7 ⑦ 明令禁止的）。
        _pf = providers.profile_for(provider, model)
        _neg = str(self.cfg.get("cloud_img_negative", "") or "").strip()
        _neg_cap = _pf.get("img_negative")
        if _neg and _neg_cap is False:
            self._append(texts.IMG_CLOUD_IGNORE_NEG, "meta")
        elif _neg and _neg_cap is None:
            self._append("[%s] %s\n" % (texts.IMG_CLOUD_TAG, texts.SET_CMEDIA_N_10),
                         "meta")
        gen, q, stop = self._cloud_begin("image")
        self._cloud_pid = str(provider["id"])
        self._img_out = out
        self._img_log = config.media_log_path(self.cfg, out)
        self._update_img_progress_line(texts.IMG_CLOUD_RUNNING
                                       % (providers.short_of(model),
                                          provider.get("name") or ""))
        threading.Thread(target=self._cloud_image_worker,
                         args=(provider, model, prompt, out, gen, q, stop),
                         kwargs={"refs": used, "ref_mode": mode, "n": count},
                         daemon=True).start()

    def _cloud_image_worker(self, provider, model, prompt, dest, gen, q, stop_flag,
                            tid="", refs=None, ref_mode="", n=1):
        """子线程：只往队列里投事件，绝不碰控件（坑 11/54 的铁律）。

        `tid` 非空 = 从台账取回旧任务，**不再提交一次**（重复提交就是重复扣钱）。
        """
        cur = {"tid": str(tid or "")}      # 当前任务的 task_id（提交后才拿得到）

        def persist(status, raw=None):
            if cur["tid"]:
                cloudjobs.update_job(cur["tid"], status=str(status))

        def note_tid(t):
            """一拿到 task_id 就落台账 —— **在轮询之前**。

            异步化的生图（服务端回了 task_id）原来要等整轮轮询结束、worker 收尾时才
            写台账，中间这段（可能几十分钟）只要断电 / 强杀，这条结果就再也取不回来
            （产物地址只活 24 小时，开发机 23:30 断电）。生视频那条链路一直是提交即登记，
            这里补齐同一条纪律（坑 136）。
            """
            t = str(t or "").strip()
            if not t or cur["tid"]:
                return
            cur["tid"] = t
            if not cloudjobs.get_job(t):
                cloudjobs.add_job(t, "image", provider["id"],
                                  provider.get("name", ""), model, prompt, dest)
            if gen == self._img_gen:
                # 本轮还在等：写回共享字段，主线程「停止生成」读 self._cloud_tid 才能
                # 对 PENDING 窗口内的任务发起真取消（生视频链路同款 worker 写回，见
                # video_gen 的 _cloud_video_worker）。不写回的话，已提交的任务点停止
                # 会被当成"还没提交出去"——文案与事实不符，还错过唯一取消得掉的窗口。
                self._cloud_tid = t
            else:
                # 本轮已被取消/被新任务替换（取消那刻 tid 还没回来，cancel 发不出去）：
                # 任务号到手立刻补发真取消（R-02——写回前必须比对世代，否则旧 worker
                # 会把新任务刚清空的 _cloud_tid 覆盖掉，下一次取消打错对象）。
                self._late_cancel("image", provider, t, model)

        try:
            # 云端自己的种子（2026-10-19 B）：原来偷偷读本地 `img_seed`，设置页没有
            # 云端入口、也说不清哪家的 seed 真的发出去了。不用 or：0 是合法种子。
            seed = int(self.cfg.get("cloud_img_seed", -1))
            if providers.profile_for(provider, model).get("img_seed") is False:
                seed = -1              # 这家请求体没有 seed：别让它看起来"设了会生效"
        except Exception:
            seed = -1

        def emit(e):
            # 事件带上世代号再入队：_poll 对过期轮的残留直接丢弃——取消后迟到的
            # progress 会 delete(mark,"end") 抹掉"已取消"甚至后续聊天内容（R-13）。
            try:
                q.put((e[0], e[1], gen))
            except Exception:
                q.put(e)

        try:
            # 日志路径按产物算、当参数传（不读 self._img_log）：这条 worker 可能属于
            # 已被取消/替换的旧轮，读共享字段会把自己的输出写进新任务的日志里。
            log_path = config.media_log_path(self.cfg, dest, "cloud")
            if cur["tid"]:
                # 取回旧任务：不再提交一次，只查 + 下载
                res = cloud_media.wait_task(self.cfg, provider, cur["tid"], dest, kind="image",
                                            emit=emit, stop_flag=stop_flag,
                                            persist=persist, model=model, log_path=log_path)
            else:
                res = cloud_media.generate_image(
                    self.cfg, provider, model, prompt, dest, emit=emit,
                    stop_flag=stop_flag,
                    negative=str(self.cfg.get("cloud_img_negative", "") or ""),
                    size=str(self.cfg.get("cloud_img_size", "") or ""), seed=seed,
                    ref_images=list(refs or []),
                    ref_mode=str(ref_mode or ""),
                    n=int(n or 1),
                    log_path=log_path,
                    on_task_id=note_tid, persist=persist)
            paths = list(res.get("paths") or [])
            # 真实落地路径可能与服务端给的扩展名一致而与我们的默认值不同（.png vs .jpg），
            # 所以把主图路径回写：_handle_img_exit 是拿 os.path.isfile(_img_out) 判成功的。
            # 这里只写普通属性、不碰控件，读它的是主线程的 exit 处理（在事件入队之后）。
            # 世代守卫：已取消/被替换的旧轮**不得**再写共享字段——那会覆盖新任务刚在
            # _cloud_begin 里清空/重置的状态（R-02）。
            if gen == self._img_gen:
                if paths:
                    self._img_out = paths[0]
                # 多张产物：主图走原有的内嵌回显，其余的只列路径（一次塞四张进聊天区会把界面撑爆）
                self._img_extra_paths = paths[1:]
            new_tid = cur["tid"] or str(res.get("task_id") or "")
            if new_tid:
                note_tid(new_tid)              # 兜底：上面没走到（同步路径直接给 url）就不补
                if paths:
                    cloudjobs.mark_done(new_tid, paths=paths, urls=res.get("urls"),
                                        seconds=res.get("seconds", 0))
                else:
                    # 保留"还在等 / 超时"这类真实状态（取回时按它决定显示"取回"还是"继续等"），
                    # 只把真出错的那种折成 unknown
                    st = str(res.get("status") or "")
                    cloudjobs.update_job(
                        new_tid, status=st if st in cloudjobs.OPEN_STATUS else "unknown",
                        error=str(res.get("error") or "")[:600])
            elif res.get("urls"):
                # 同步生图（服务端当场回 URL）下载失败：**URL 是唯一还活着的线索**
                # （24h）。原来它只留在 .log 里，重启后没有任何入口能拿回来（16 §24.2）。
                # 记成一条合成台账（task_id 前缀 url-）：取回时走"按 URL 重下"，
                # 不查服务端、更不重新提交（重复提交 = 重复扣钱）。
                jid = cloudjobs.url_job_id(res.get("urls"))
                if jid:
                    if not cloudjobs.get_job(jid):
                        cloudjobs.add_job(jid, "image", provider["id"],
                                          provider.get("name", ""), model, prompt, dest)
                    cloudjobs.update_job(
                        jid, urls=list(res.get("urls") or []), status="succeeded",
                        error=str(res.get("error") or "")[:600])
                    # 子线程绝不碰控件（坑 11/54）：这句话走队列回主线程
                    q.put(("line", texts.IMG_CLOUD_URL_KEPT % jid[:10], gen))
            if not paths:
                q.put(("line", "[ERROR] %s" % (res.get("error") or texts.IMG_CLOUD_FAILED), gen))
            q.put(("exit", 0 if paths else 1, gen))
        except Exception as e:
            # 兜底（2026-10-11 排查报告）：worker 体内任何未捕获异常都不许吞掉 exit
            # 事件——没有它主线程不复位 _img_busy，发送/切模型会永久被"任务进行中"拦住
            try:
                q.put(("line", "[ERROR] %s" % e, gen))
            except Exception:
                pass
            q.put(("exit", 1, gen))

    def _cloud_url_worker(self, job, dest, gen, q, stop_flag):
        """按台账里存的产物地址重下（**不查服务端、不重新提交**，16 §24.2）。

        同步生图的收尾形状与异步任务不同：服务端当场回 URL，没有 task_id 可查。
        所以这条 worker 只做"下载 + 落账 + 投 exit"，事件与 `_cloud_image_worker`
        完全同形，主线程的 `_handle_img_exit` 不用知道自己在收哪种结果。
        """
        urls = [u for u in (job.get("urls") or []) if str(u or "").strip()]
        try:
            res = cloud_media.download_urls(
                urls, dest, emit=lambda e: q.put((e[0], e[1], gen)),
                stop_flag=stop_flag)
            paths = list(res.get("paths") or [])
            if gen == self._img_gen:
                if paths:
                    self._img_out = paths[0]
                    self._img_extra_paths = paths[1:]
            if paths:
                cloudjobs.mark_done(job.get("task_id"), paths=paths, urls=urls)
            else:
                q.put(("line", "[ERROR] %s"
                       % (res.get("error") or texts.IMG_CLOUD_FAILED), gen))
            q.put(("exit", 0 if paths else 1, gen))
        except Exception as e:
            try:
                q.put(("line", "[ERROR] %s" % e, gen))
            except Exception:
                pass
            q.put(("exit", 1, gen))

    def _img_reader(self, proc, gen, log_path="", t0=None, expected=None):
        # 完整引擎输出落盘（与 _vid_reader 同形）：内存里的 _img_tail 只有 12 行，
        # 够回答"为什么失败"，不够回答"引擎说自己成功了却没写出文件"。
        log = None
        try:
            # 行缓冲：应用被强杀时也要留下"引擎最后说了什么"
            log = open(log_path, "a", encoding="utf-8", errors="replace",
                       buffering=1) if log_path else None
        except Exception:
            log = None
        try:
            for line in proc.stdout:
                if self._closing:
                    break
                if log is not None:
                    try:
                        log.write(line if line.endswith("\n") else line + "\n")
                    except Exception:
                        pass
                # 与 error/exit 同款带世代号（坑 264）：取消后立刻重发时，旧进程
                # 缓冲里迟到的行会被 app._poll 的 stale 守卫整条丢掉，不进新任务
                # 的进度行与 _img_tail
                self._img_q.put(("line", line.rstrip(), gen))
        except Exception:
            pass
        try:
            rc = proc.wait()
        except Exception:
            rc = -1
        # 系统码页把中文路径读歪时（引擎 MinGW 窄 argv = UTF-8 × 文件 API 按 ACP
        # 解释，判据见 core/media.salvage_products），产物会落在乱码镜像目录里 ——
        # 这里是"搬回来"的位置：**必须在 ("exit", …) 入队之前**，主线程是拿
        # os.path.isfile(expected) 判成功的，搬晚了会报成"引擎没有交付文件"。
        try:
            moved = salvage_products(self.cfg, expected, rc=rc)
        except Exception:
            moved = []
        if log is not None:
            try:
                for _src, _dst in moved:
                    log.write("=== 产物不在指定位置，已从乱码镜像目录搬回：%s → %s ===\n"
                              % (_src, _dst))
                log.write("=== 退出码 %s ｜ 用时 %.1fs ===\n"
                          % (rc, time.time() - (t0 or time.time())))
                log.close()
            except Exception:
                pass
        self._img_q.put(("exit", rc, gen))

    def _update_img_progress_line(self, text):
        mark = getattr(self, "_img_mark", None)
        if not mark:
            return
        self.chat.configure(state="normal")
        try:
            self.chat.delete(mark, "end")
            self.chat.insert(mark, "\n" + text, "meta")
        except Exception:
            pass
        self.chat.configure(state="disabled")

    def _handle_img_line(self, line):
        # 滚动保留输出末尾：进度行是 delete(mark,"end") 整行清除的，引擎/服务端的报错
        # 会跟着一起被删掉，界面只剩"退出码 1"（坑 40，生视频链路已经吃过一次）。
        if line.strip():
            self._img_tail.append(line.strip()[:220])
            if len(self._img_tail) > 12:
                del self._img_tail[0]
        if self._stop_flag is not None and self._stop_flag.is_set():
            return
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        low = line.lower()
        mb = re.search(r"generating image: (\d+)/(\d+) - seed ", line)
        if mb:
            # 批量（-b n>1）时每张开头这一行（源码里的 LOG_INFO："generating image:
            # i/n - seed s"）：记批号并把「第 i/n 张」先报出来。n=1 时这一行与
            # 改造前一样原样跳过（文案逐字不变）。
            self._img_batch_i = int(mb.group(1))
            self._img_batch_n = int(mb.group(2))
            if self._img_batch_n > 1:
                self._update_img_progress_line(
                    texts.IMG_PROG_BATCH
                    % (self._img_batch_i, self._img_batch_n, elapsed))
            return
        if "generating image" in line:
            return      # 采样开始行（其它形状）：没有进度数字可报，原样跳过
        m = re.search(r"(\d+)/(\d+) - ", line)
        steps = max(1, int(self.cfg.get("img_steps", 20) or 20))
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if (not getattr(self, "_saw_decode", False)
                    and total == steps and cur <= total):
                # 只认与设定步数一致的进度（区分模型加载段与 VAE 分块）
                if (getattr(self, "_img_batch_n", 1) or 1) > 1:
                    # 批量：带批号（W 2026-10-13 逐字批准）
                    self._update_img_progress_line(
                        texts.IMG_PROG_BATCH_STEPS
                        % (max(1, self._img_batch_i), self._img_batch_n,
                           cur, total, elapsed))
                else:
                    self._update_img_progress_line(
                        texts.IMG_PROG_STEPS % (cur, total, elapsed))
            return
        if "decoding" in line and "latent" in line:
            self._saw_decode = True
            _n = getattr(self, "_img_batch_n", 1) or 1
            self._update_img_progress_line(
                (texts.IMG_DECODE_BATCH % (_n, elapsed)) if _n > 1
                else (texts.IMG_DECODE % elapsed))
        elif "error" in low or "failed" in low or "out of memory" in low:
            # 报错当场固化在进度行上，别等它被进度行一起删掉
            self._update_img_progress_line("🎨 %s" % line.strip()[:160])

    def _handle_img_exit(self, rc, gen):
        if gen != self._img_gen:
            return                      # 过期任务（已被取消并替换），忽略其回调
        elapsed = int(time.time() - self._t0) if getattr(self, "_t0", None) else 0
        # 这一轮是不是云端，必须在 _cloud_reset 之前读走：那个函数会把标记清掉，
        # 读晚了失败文案就会把云端的结果说成"引擎没有交付文件"（生视频那侧顺序是对的）
        cloud = bool(getattr(self, "_cloud_img", False))
        self._img_busy = False
        self._img_proc = None
        self._cloud_reset("image")
        self._set_busy_ui(False)
        out = getattr(self, "_img_out", "")
        # 批量（×n）判据：按 `_img_expected` 逐条核（n=1 时它就等于 [out]，与改造前
        # 逐字等价；云端不设 expected，走 worker 现场回写的 _img_out 旧判据）。
        # 成功 = rc==0 且**至少有一张**落地；第一张当主图、其余走"同批还有"那行
        # （与云端同款回显，W 2026-10-13 选定）。
        exp = [p for p in (getattr(self, "_img_expected", []) or []) if p]
        found = [p for p in exp if os.path.isfile(p)]
        if not exp:
            found = [out] if (out and os.path.isfile(out)) else []
        cancelled = self._stop_flag is not None and self._stop_flag.is_set()
        # 清除进度行（mark 到末尾；行结构由进度文字的前导换行维持）
        mark = getattr(self, "_img_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
        self._img_mark = None           # 收尾即封死 mark（同 _cancel_chat_image；R-13）
        if cancelled:
            self._append(texts.IMG_CANCELLED, "meta")
            self._append("────────────────\n", "meta")
            return
        if rc == 0 and found:
            # 主图 = 第一张落地产物；其余进"同批还有 N 张"（n=1 时 extras 为空，
            # 与改造前逐字一致）
            self._img_out, self._img_extra_paths = found[0], found[1:]
            out = found[0]
            # 关键：回显必须处于 normal 状态——disabled 的 Text 会静默丢弃
            # image_create/insert/window_create（此前图片不显示的根因）。
            # 布局：提示词行 → 进度行（原位替换）→ 图片独立成行，按钮在图片右侧同行
            self.chat.configure(state="normal")
            try:
                # 缩略图单独一层：imgdecode.photo 不抛（Pillow 优先，Tk 8.6 画不出
                # JPEG / WEBP / BMP、或 Pillow 缺席时回 None）——那只是"没有预览"，
                # **已保存 + 两个打开按钮照给**（坑 132）
                thumb = None
                img = imgdecode.photo(out, 500)
                if img is not None:
                    thumb = self._remember_photo(out, img)     # 持引用防 GC（按路径为键）
                self.chat.insert("end", "\n", "meta")      # 图片前补换行：独立成行
                if thumb is not None:
                    self.chat.image_create("end", image=thumb)
                    self.chat.insert("end", "  ", "meta")      # 图片与按钮的间距
                btn = theme.button(self.chat, text=texts.IMG_OPEN_ORIG,
                                 command=lambda p=out: os.startfile(p))
                self.chat.window_create("end", window=btn)
                self.chat.insert("end", "  ", "meta")
                btn2 = theme.button(self.chat, text=texts.MEDIA_OPEN_FOLDER,
                                  command=lambda p=out: self._open_containing(p))
                self.chat.window_create("end", window=btn2)
                self.chat.insert("end", texts.IMG_SAVED % (out, elapsed), "meta")
                extra = [p for p in (getattr(self, "_img_extra_paths", []) or [])
                         if p and os.path.isfile(p)]
                if extra:
                    self.chat.insert("end", texts.IMG_BATCH_MORE
                                     % (len(extra), "、".join(os.path.basename(p)
                                                              for p in extra)), "meta")
                self.chat.insert("end", "────────────────\n", "meta")
                self.chat.see("end")
            except Exception as e:
                self._append(texts.IMG_ECHO_FAIL % (e, out), "error")
            self.chat.configure(state="disabled")
        else:
            # 与生视频同一套失败回显：本地是"退出码 + 引擎末几行"，云端是"服务端原话"
            src = texts.MEDIA_SRC_CLOUD if cloud else texts.MEDIA_SRC_ENGINE
            # 前导换行：进度行是 delete(mark,"end") 整行删掉的，连它自带的那个换行一起没了，
            # 不加回来这行就会粘在上一行尾巴上
            # 云端没有进程，worker 那个 exit 1 只是"没拿到产物"的内部信号；照
            # 本地口径写"退出码 1"会让人去查一个根本不存在的引擎进程（2026-10-19）。
            _why = (texts.IMG_NO_FILE % src) if cloud else (
                texts.MEDIA_RC % rc if rc else texts.IMG_NO_FILE % src)
            self._append(texts.IMG_FAIL % _why, "error")
            tail = [x for x in (getattr(self, "_img_tail", []) or []) if x][-6:]
            self._append(texts.MEDIA_LAST_OUTPUT % src + "\n  ".join(tail) + "\n" if tail
                         else texts.IMG_NO_OUTPUT, "error")
            log = getattr(self, "_img_log", "")
            if log and os.path.isfile(log):
                self._append(texts.MEDIA_LOG_PATH % log, "meta")
            self._append("────────────────\n", "meta")

    def _cancel_chat_image(self):
        """立即中止当前生图任务：杀进程、世代递增（旧回调失效）、清理进度行。

        返回 True = 这是云端任务，取消的结果**已由 _cloud_cancel_task 如实说过**，
        调用方（stop_generate）就别再补一句"已取消生成"——云端任务在 RUNNING 时
        根本取消不掉，那样说会把用户骗过去（文档 §12.3 ⑥）。
        """
        cloud = bool(getattr(self, "_cloud_img", False))
        tid = getattr(self, "_cloud_tid", "")
        if self._img_proc is not None:
            try:
                self._img_proc.kill()
            except Exception:
                pass
        # 置停止标志：让仍在队列里的进度行事件被 _handle_img_line 丢弃。
        # 否则它们会继续用旧 mark 重画进度行——而 delete(mark,"end") 会连带
        # 抹掉刚写入的"已取消生成"提示，并在界面留下一条永远不消失的进度。
        if self._stop_flag is not None:
            self._stop_flag.set()
        self._img_gen += 1
        self._img_busy = False
        self._img_proc = None
        self._cloud_img = False
        self._cloud_wait_on = False
        self._cloud_tid = ""      # 清掉这一轮的 tid：留着会让下一次云端任务误取消它
        mark = getattr(self, "_img_mark", None)
        if mark:
            self.chat.configure(state="normal")
            try:
                self.chat.delete(mark, "end")
            except Exception:
                pass
            self.chat.configure(state="disabled")
        # mark 置 None 天然封死迟到进度行：_update_img_progress_line 开头 if not mark
        # 就 return——原来只删内容不清引用，残留 progress 事件会把"已取消"连分隔线
        # 一起抹掉、换上一行永不消失的假进度，mark 卡在对话流中段时后续聊天内容
        # 会被整段删掉（R-13）
        self._img_mark = None
        if cloud:
            return self._cloud_cancel_task(tid, "image")
        return False
