# -*- coding: utf-8 -*-
"""llm_console.ui.guide — 首次启动的遮罩引导步骤表（只有数据与文案，不画任何东西）。

画的事全在 `widgets.SpotlightGuide`；这里只回答"第几步指着谁、说什么"。

三条约定：
  · `target` 一律按 App 的**实例属性名**取（`getattr(app, name)`），不在这里存控件对象 ——
    界面改了名这里立刻取不到，而取不到必须由自检判红（引导默默指空处比没有引导更糟）。
  · 每步正文 ≤3 条、每条 ≤40 字：引导是路标不是手册，细节交给控件旁的 "?" 与 README。
    **第一屏是唯一的例外**：那里是"三条路各缺什么"的清单，三行 + 一行指路（见下）。
  · 第 0 步（欢迎页）会带上诊断出的"还缺什么"：新设备上最容易卡住的不是"按钮在哪"，
    而是引擎和模型还没下载 —— 那两样都不随本程序分发。而本地有**三条**路可走
    （llama 对话 / sd 生图生视频 / 云端），缺哪条就点哪条，别去催用户下他不需要的引擎
    （坑 150；清单由 `core.diagnose.guide_missing` 生成，判据只在那儿一处）。
"""

from .. import texts

WELCOME = "welcome"


def steps(app, missing=None):
    """生成这一步要走的引导。`missing` = 诊断里那三条路的就绪情况（可为空）。"""
    out = []
    # `paths` 那条是"三条路通不通"的**汇总**，不是一件可补的东西：混进清单里
    # 这一屏就会念出"· 能不能跑通"这种半句话（那是 run_checks 的汇总项，guide 不用它）
    missing = [c for c in (missing or []) if c.get("id") != "paths"]

    if missing:
        # W 2026-10-05：这一屏逐条列出三条路（原来只列 llama 与云端，只想生图的用户被
        # 催去下对话引擎）。每行一句"就绪 / 缺什么"，末行给两处指路。
        body = [texts.GUIDE_WELCOME_PATHS]
        body += [texts.GUIDE_WELCOME_PATH_ITEM % m["title"] for m in missing[:3]]
        body.append(texts.GUIDE_WELCOME_GO_SETTINGS)
        title = texts.GUIDE_WELCOME_TITLE_MISSING
    else:
        body = [texts.GUIDE_WELCOME_OK_1, texts.GUIDE_WELCOME_OK_2]
        title = texts.GUIDE_WELCOME_TITLE_OK
    out.append({"target": None, "title": title, "body": body})

    def add(attr, title, body, pad=6):
        w = None
        try:
            w = getattr(app, attr, None)
        except Exception:
            w = None
        out.append({"target": w, "target_name": attr, "title": title, "body": body,
                    "pad": pad})

    add("status_label", texts.GUIDE_LAMP_TITLE,
        [texts.GUIDE_LAMP_1, texts.GUIDE_LAMP_2])

    add("model_btn", texts.GUIDE_MODEL_TITLE,
        [texts.GUIDE_MODEL_1, texts.GUIDE_MODEL_2, texts.GUIDE_MODEL_3])

    add("input", texts.GUIDE_INPUT_TITLE,
        [texts.GUIDE_INPUT_1, texts.GUIDE_INPUT_2])

    add("attach_btn", texts.GUIDE_ATTACH_TITLE,
        [texts.GUIDE_ATTACH_1, texts.GUIDE_ATTACH_2])

    add("start_btn", texts.GUIDE_START_TITLE,
        [texts.GUIDE_START_1, texts.GUIDE_START_2])

    add("btn_settings", texts.GUIDE_HELP_TITLE,
        [texts.GUIDE_HELP_1, texts.GUIDE_HELP_2])
    return out
