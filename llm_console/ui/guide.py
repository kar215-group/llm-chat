# -*- coding: utf-8 -*-
"""llm_console.ui.guide — 首次启动的遮罩引导步骤表（只有数据与文案，不画任何东西）。

画的事全在 `widgets.SpotlightGuide`；这里只回答"第几步指着谁、说什么"。

三条约定：
  · `target` 一律按 App 的**实例属性名**取（`getattr(app, name)`），不在这里存控件对象 ——
    界面改了名这里立刻取不到，而取不到必须由自检判红（引导默默指空处比没有引导更糟）。
  · 每步正文 ≤3 条、每条 ≤40 字：引导是路标不是手册，细节交给控件旁的 "?" 与 README。
  · 第 0 步（欢迎页）会带上诊断出的"还缺什么"：新设备上最容易卡住的不是"按钮在哪"，
    而是引擎和模型还没下载 —— 那两样都不随本程序分发。
"""

WELCOME = "welcome"


def steps(app, missing=None):
    """生成这一步要走的引导。`missing` = 诊断里 level=fail 的条目（可为空）。"""
    out = []
    # `paths` 那条是"两条路都不通"的**汇总**，不是一件可补的东西：混进清单里
    # 这一屏就会念出"· 能不能跑通"这种半句话（W 2026-10-02 验收看到的就是它）
    missing = [c for c in (missing or []) if c.get("id") != "paths"]

    if missing:
        # W 2026-10-02 验收：这一屏"看得懂但太啰嗦"。原来每条缺件后面还拖一句
        # `fix[:44]`（半句话被截断，读起来像坏掉的提示），而处置细节在「诊断」里都有。
        # 现在只报"缺什么"，把"下一步做什么"收成最后一行。
        body = ["这台机器上还缺 %d 样，先补上才跑得起来：" % len(missing)]
        body += ["· %s" % m["title"] for m in missing[:3]]
        if len(missing) > 3:
            body.append("· 另有 %d 项" % (len(missing) - 3))
        body.append("补齐只要一处：设置 → 本地模型 → 获取引擎。")
        title = "开始之前"
    else:
        body = ["这个工作台把本地大模型对话、生图生视频和给 agent 用的接口装进一个窗口。",
                "环境检查没发现阻塞问题，往下看怎么用。"]
        title = "欢迎使用 LLM Chat"
    out.append({"target": None, "title": title, "body": body})

    def add(attr, title, body, pad=6):
        w = None
        try:
            w = getattr(app, attr, None)
        except Exception:
            w = None
        out.append({"target": w, "target_name": attr, "title": title, "body": body,
                    "pad": pad})

    add("status_label", "先看这盏灯",
        ["○ 未运行 / ◐ 加载中 / ● 运行中 —— 灯说什么就是什么。",
         "首次加载大模型可能要 1~3 分钟，那期间输入框仍可以打字。"])

    add("model_btn", "点这里换模型",
        ["菜单里本地模型与云端模型混排，云端的带「（云）」后缀。",
         "换模型只是选一下，不会在这里花几秒去加载权重。",
         "选〔生图〕〔生视频〕的模型时不用启动服务，直接发提示词就出片。"])

    add("input", "在这儿说话",
        ["Enter 发送，Shift+Enter 换行。",
         "如果服务加载的不是你刚选的模型，它会自动换载再把这句话发出去，上下文不丢。"])

    add("attach_btn", "📎 挂图片或文件",
        ["图片要这个模型配了视觉投影器（mmproj）才收；文本文件本地云端都能发。",
         "被拦下时提示会说明原因，并给你一次「仍按支持图片处理」的出口。"])

    add("start_btn", "启动 / 停止服务",
        ["只有聊天需要启动服务；生图、生视频和所有云端模型都不经过它。",
         "显存不够时会先问你怎么办，不会偷偷排队把机器卡死。"])

    add("btn_settings", "出问题去哪里",
        ["设置 → 关于与诊断：重看这段引导；点「诊断」跑一键诊断、复制诊断报告。",
         "先跑一次诊断，比自己猜快得多。"])
    return out
