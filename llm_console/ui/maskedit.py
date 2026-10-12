# -*- coding: utf-8 -*-
"""llm_console.ui.maskedit — 附件瓷砖的「涂抹遮罩」二级页面（Mixin）

**为什么有这一页**：`--mask` 是本地唯一能"只改想改的那块、其余基本逐像素不动"的通路
（2026-10-11 真机实测：遮罩外平均像素差 2.56/255、93.6% 的像素差 ≤8，见坑 214）。
遮罩有两条来源 —— 自动检测（本机没有检测件，且上游只支持检测类权重，圈不到"只圈衣物"）
或**用户自己涂**。W 2026-10-11 选后者：点附件栏的图片打开这一页涂。

三条约定：

  · 只在**本地 + 底图重绘档**可用；其它档点了不打开，在附件条状态行说一句为什么
    （不弹窗、不加悬停提示 —— W 点名"不用做鼠标移上去的提醒"）。
  · 遮罩**会话内临时**：涂完写一个 PNG 到系统临时目录，路径挂在 `self._masks[图片路径]`。
    关掉程序就没了，也不往用户的图片目录 / 产物目录写任何东西。
  · **白色 = 要重绘**（引擎 `--mask` 的语义，实测过）；一点没涂 = 等于没有遮罩
    （所以"清空 + 确定"就是撤销遮罩）。
"""

import os
import tempfile
import tkinter as tk
from tkinter import ttk

from . import imgdecode, theme, widgets


class MaskEditMixin:
    """App 的「涂抹遮罩」二级页面职责（Mixin）；self._xxx 在运行时经 MRO 解析。"""

    MASK_VIEW_PX = 640        # 画布上图片的最长边（显示用，遮罩仍按原图分辨率存）
    MASK_BRUSH_MIN = 4
    MASK_BRUSH_MAX = 160

    def _mask_can_paint(self, image_path=""):
        """这一档能不能涂遮罩 → `(能不能, 不能时的原因)`（原因写进附件条状态行）。"""
        from ..core import capability, providers
        if providers.is_cloud(self.cfg):
            return False, "遮罩只对本地生图生效：云端生图的接口没有这个入参。"
        if str(self.cfg.get("model_kind") or "") != "image":
            return False, "遮罩只在生图模型的「底图重绘」档用得上。"
        if self._effective_ref_mode() != capability.MODE_EDIT:
            return False, "当前是「主体参考」档，遮罩用不上——切回「底图重绘」再点图片涂抹。"
        # 底图重绘一次只认第一张（`media.plan_ref_images`），涂在其余几张上的遮罩
        # 发送时根本不会被读到 —— 与其让用户白涂一遍再静默丢掉，点这里就说清。
        imgs = list(getattr(self, "_attached_images", None) or [])
        if image_path and len(imgs) > 1 and imgs[0] != image_path:
            return False, "底图重绘一次只认第一张图：这张不会送去重绘，遮罩用不上。"
        return True, ""

    def _mask_cleanup(self):
        """删掉本会话涂出来的那些遮罩（关窗时调；不删就一次次留在 %TEMP% 里）。"""
        d, self._mask_tmpdir = self._mask_tmpdir, ""
        self._masks = {}
        if not d:
            return
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    def _mask_path_of(self, image_path):
        """这张图这次任务要带的遮罩 PNG（没涂过 = 空串）。"""
        return str((self._masks or {}).get(image_path) or "")

    @staticmethod
    def _mask_ring_color(erasing):
        """笔刷范围圈的颜色：涂 = 红，擦 = 蓝（一眼知道现在按下去是加还是减）。"""
        return "#2f7dff" if erasing else "#e8453c"

    def _mask_save_path(self, image_path):
        """遮罩的落点：系统临时目录（会话内临时，不碰用户的图片与产物目录）。"""
        if not self._mask_tmpdir:
            self._mask_tmpdir = tempfile.mkdtemp(prefix="llm_console_mask_")
        stem = os.path.splitext(os.path.basename(image_path))[0] or "mask"
        keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)[:40]
        return os.path.join(self._mask_tmpdir, "%s_%08x.png"
                            % (keep, (abs(hash(os.path.abspath(image_path))) & 0xFFFFFFFF)))

    def _open_mask_editor(self, image_path):
        """打开涂抹页（单实例）。返回窗口或 None。"""
        ok, why = self._mask_can_paint(image_path)
        if not ok:
            # 说一句为什么就够了：不弹窗、不悬停（W 2026-10-11）
            self._attach_note_var.set(why)
            self.attach_note.grid()
            return None
        if not (image_path and os.path.isfile(image_path)):
            return None
        win = getattr(self, "_mask_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    if getattr(self, "_mask_img", "") == image_path:
                        win.lift()
                        return win
                    # 换了一张图：旧窗关掉（没点确定的那部分不落地，与「取消」同口径）。
                    # 单实例是为了不出现"两个窗口同时涂，不知道哪个算数"。
                    win.destroy()
            except Exception:
                pass
            self._mask_win = None
        self._mask_img = image_path
        try:
            from PIL import Image, ImageDraw, ImageOps
        except Exception:
            self._attach_note_var.set("这台机器上没有 Pillow，用不了涂抹遮罩。")
            self.attach_note.grid()
            return None

        src = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
        ow, oh = src.size
        sc = min(1.0, self.MASK_VIEW_PX / float(max(ow, oh) or 1))
        dw, dh = max(1, int(round(ow * sc))), max(1, int(round(oh * sc)))
        disp = src.resize((dw, dh), Image.LANCZOS) if sc < 1.0 else src.copy()
        mask = Image.new("L", (ow, oh), 0)
        old = self._mask_path_of(image_path)
        if old and os.path.isfile(old):
            try:
                with Image.open(old) as m:
                    mask = m.convert("L").resize((ow, oh), Image.NEAREST)
            except Exception:
                mask = Image.new("L", (ow, oh), 0)

        win = tk.Toplevel(self.root)
        win.withdraw()                      # 摆正了再显示（否则左上角闪一下，坑 161）
        win.title("涂抹要重绘的区域 · %s" % os.path.basename(image_path))
        theme.style_toplevel(win)
        win.transient(self.root)
        self._mask_win = win

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 10))
        # 顶栏那句要按**画布宽度**折行：不折的话它自己的请求宽能顶出窗口右界
        # （坑 113 同族——长中文标签按字符估算宽度必偏）。
        ttk.Label(body, text="涂过的地方交给模型重绘，没涂的地方保持不变；一点不涂 = 不用遮罩。",
                  style="Dim.TLabel", font=("Microsoft YaHei UI", 9),
                  wraplength=dw).pack(anchor="w")

        cv = tk.Canvas(body, width=dw, height=dh, highlightthickness=1,
                       highlightbackground=theme.c("border"), background=theme.c("bg"))
        # 退出 ttkbootstrap 的自动重涂 + 自己盖戳：`theme_use` 的 walk 会把经典
        # Canvas 的底色涂对、却顺手把 `highlightthickness` 清零（那圈 1px 描边没了）。
        # 底色交给 `_tint` 戳由 retint 重涂，几何就谁也碰不到了。
        cv._tb_no_autostyle = True
        theme.tint(cv, bg="bg")
        cv.pack(pady=(6, 8))
        holder = {"photo": None, "over": None}
        # 两份遮罩：`mask` 原图分辨率（落盘、喂引擎），`small` 显示分辨率（预览）。
        # 两份各画一次比"原图那份 resize 下来"便宜得多（4032px 上 resize 是大头）。
        small = mask.resize((dw, dh), Image.NEAREST)
        red = Image.new("RGB", (dw, dh), (255, 70, 70))

        # 画布分两层（W 2026-10-11 定：盖一层红色，别实时合成）：
        #   ① 底图 —— 建好之后**一次都不再上传**；
        #   ② 红色叠加层（RGBA，涂过的地方才有 alpha）—— 只在抬笔 / 清空 / 开页时更新。
        # 所以拖动期间既没有合成、也没有重传底图，只有一条跟着鼠标的线（见 on_move）。
        holder["photo"] = imgdecode.photo_of(disp)
        cid = cv.create_image(0, 0, anchor="nw", image=holder["photo"])
        oid = cv.create_image(0, 0, anchor="nw")

        def refresh_overlay():
            """更新红色叠加层。**只在抬笔 / 清空 / 开页时调**（拖动期间一次都不调）。

            原来是每个 motion 事件做一次"全图合成 + PNG 编码 + 新建 PhotoImage"，
            真机 4032x3024 底图上实测 15.5 ms/事件、200 个事件 3.1 秒，手一拖就卡。
            """
            lay = red.convert("RGBA")
            lay.putalpha(small.point(lambda v: 150 if v > 127 else 0))
            ph = imgdecode.photo_of(lay, fast=True)
            if ph is None:
                return
            holder["over"] = ph             # 持引用防 GC（同 _remember_photo 的教训）
            cv.itemconfigure(oid, image=ph)

        # 抬笔不立刻重画叠加层：`after_idle` 要等事件队列排空才跑 —— 连着快速涂好几笔时，
        # 这一批抬笔合并成**一次**全层重画（单次约 20 ms，主要在 itemconfigure 让 Tk 重画整层）。
        # 一笔一次的画面感不变（笔迹本身在拖动时就画在画布上了）。
        pending = {"id": None}

        def do_refresh():
            pending["id"] = None
            refresh_overlay()

        def cancel_pending():
            if pending["id"] is not None:
                try:
                    win.after_cancel(pending["id"])
                except Exception:
                    pass
                pending["id"] = None

        def schedule_overlay():
            if pending["id"] is not None:
                return
            try:
                pending["id"] = win.after_idle(do_refresh)
            except Exception:
                pending["id"] = None
                refresh_overlay()

        refresh_overlay()

        # 窗口销毁时取消挂起的重画：`_open_mask_editor` 换图那条路径是直接
        # `win.destroy()` 的（走不到 close()），after_idle 会在画布没了之后跑一次，
        # 抛一个没人接的 TclError。`<Destroy>` 对子控件也派发，只认窗口自己那次。
        def _on_win_destroy(e):
            if getattr(e, "widget", None) is win:
                cancel_pending()

        win.bind("<Destroy>", _on_win_destroy, add="+")

        bar = ttk.Frame(body)
        bar.pack(fill="x")
        ttk.Label(bar, text="笔刷", font=("Microsoft YaHei UI", 9)).pack(side="left")
        brush = tk.IntVar(value=40)
        ttk.Scale(bar, from_=self.MASK_BRUSH_MIN, to=self.MASK_BRUSH_MAX,
                  orient="horizontal", length=180, variable=brush).pack(side="left", padx=(4, 10))
        erase = tk.BooleanVar(value=False)
        theme.check(bar, text="橡皮擦", variable=erase).pack(side="left", padx=(0, 10))

        def clear():
            cancel_pending()
            mask.paste(0, (0, 0, ow, oh))
            small.paste(0, (0, 0, dw, dh))
            refresh_overlay()

        theme.button(bar, text="清空", command=clear).pack(side="left")

        # ---- 笔刷范围提示：跟着鼠标的一个圈（红=涂，蓝=擦），大小就是笔刷粗细 ----
        ring = {"item": None, "at": (0, 0)}

        def show_ring(x, y):
            r = max(2, brush.get() // 2)
            ring["at"] = (x, y)
            if ring["item"] is None:
                ring["item"] = cv.create_oval(x - r, y - r, x + r, y + r,
                                              outline=self._mask_ring_color(erase.get()),
                                              width=2)
            else:
                cv.coords(ring["item"], x - r, y - r, x + r, y + r)
                cv.itemconfigure(ring["item"], state="normal")
            cv.tag_raise(ring["item"])

        def hide_ring():
            if ring["item"] is not None:
                cv.itemconfigure(ring["item"], state="hidden")

        def ring_follow(*_a):
            """笔刷粗细 / 涂擦切换后，圈的大小与颜色要跟着变（不然提示是错的）。"""
            if ring["item"] is not None:
                x, y = ring["at"]
                r = max(2, brush.get() // 2)
                cv.coords(ring["item"], x - r, y - r, x + r, y + r)
                cv.itemconfigure(ring["item"], outline=self._mask_ring_color(erase.get()))

        brush.trace_add("write", ring_follow)
        erase.trace_add("write", ring_follow)
        cv.bind("<Motion>", lambda e: show_ring(e.x, e.y))
        cv.bind("<Leave>", lambda _e: hide_ring())

        def to_view(x, y):
            return (max(0, min(dw - 1, int(x))), max(0, min(dh - 1, int(y))))

        def to_orig(p):
            return (max(0, min(ow - 1, int(p[0] / (sc or 1.0)))),
                    max(0, min(oh - 1, int(p[1] / (sc or 1.0)))))

        def dot(im, p, w, val):
            r = max(1, w // 2)
            ImageDraw.Draw(im).ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=val)

        # 拖动中的"预览"是一条 Tk 线（`coords` 追加点）：零 PIL、零 PhotoImage，
        # 一次事件只有一次画布重画，所以手感不吃底图大小。
        live = {"pts": [], "item": None}

        def on_press(e):
            p = to_view(e.x, e.y)
            live["pts"] = [p]
            live["item"] = cv.create_line(
                p[0], p[1], p[0], p[1], capstyle="round", joinstyle="round",
                width=max(1, brush.get()),
                fill=self._mask_ring_color(erase.get()),
                stipple="gray50" if erase.get() else "")
            show_ring(e.x, e.y)

        def on_move(e):
            show_ring(e.x, e.y)                 # 圈跟着走（O(1)，与合成无关）
            if not live["pts"]:
                return
            live["pts"].append(to_view(e.x, e.y))
            cv.coords(live["item"], *[v for p in live["pts"] for v in p])

        def on_release(_e):
            pts, item = live["pts"], live["item"]
            live["pts"], live["item"] = [], None
            if item is not None:
                cv.delete(item)             # 预览线撤掉，换成合成后的真结果
            if not pts:
                return
            w_d = max(1, brush.get())
            w_o = max(1, int(round(brush.get() / (sc or 1.0))))
            val = 0 if erase.get() else 255
            op = [to_orig(p) for p in pts]
            if len(pts) == 1:               # 点一下也要看得见
                dot(small, pts[0], w_d, val)
                dot(mask, op[0], w_o, val)
            else:
                ImageDraw.Draw(small).line(pts, fill=val, width=w_d, joint="curve")
                ImageDraw.Draw(mask).line(op, fill=val, width=w_o, joint="curve")
                for i in (0, -1):           # 两端补圆头（PIL 的 line 不画端点圆）
                    dot(small, pts[i], w_d, val)
                    dot(mask, op[i], w_o, val)
            schedule_overlay()              # 合批：连笔只重画一次叠加层

        cv.bind("<Button-1>", on_press)
        cv.bind("<B1-Motion>", on_move)
        cv.bind("<ButtonRelease-1>", on_release)

        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(10, 0))
        note = tk.StringVar(value="")
        ttk.Label(foot, textvariable=note, style="Dim.TLabel",
                  font=("Microsoft YaHei UI", 9)).pack(side="left")

        def close():
            cancel_pending()
            self._mask_win = None
            holder["photo"] = None
            try:
                win.destroy()
            except Exception:
                pass

        def commit():
            if mask.getbbox() is None:
                self._masks.pop(image_path, None)       # 没涂 = 撤销
            else:
                out = self._mask_save_path(image_path)
                try:
                    mask.save(out)
                except Exception as e:
                    note.set("遮罩存不下来：%s" % e)
                    return
                self._masks[image_path] = out
            close()
            self._refresh_attach_note()

        theme.button(foot, text="取消", command=close).pack(side="right")
        theme.button(foot, text="确定", command=commit, bootstyle="primary").pack(
            side="right", padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        # 尺寸**量出来再交给 center_on**：控件建完的 req* 才是准的，手估一个 h+130
        # 会把底部的按钮那排裁掉半截（DPI 越高越容易被裁，坑 161 的另一半）。
        win.update_idletasks()
        w0 = max(win.winfo_reqwidth(), dw + 40)
        h0 = max(win.winfo_reqheight(), dh + 110)
        win.minsize(w0, h0)
        widgets.center_on(win, self.root, size=(w0, h0))
        return win
