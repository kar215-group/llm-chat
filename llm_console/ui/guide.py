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
        [texts.GUIDE_HELP_1, texts.GUIDE_HELP_2, texts.GUIDE_HELP_3])
    return out


def settings_steps():
    """设置页引导的步骤表（2026-10-18 W 需求：主引导走完自动接这一段）。

    与 `steps()` 的两点不同：
      · `target` 全部留空、由 `page` / `section` / `key` 代替 —— 目标不是主窗控件，
        而是设置窗里懒建的区块标题。SpotlightGuide 每换一步先回调 on_step(i)，
        App 那边 `nav.select(key)`（选中即建整页区块）、再把这一步的 target 填成
        `app._settings_heads[(page, section)]`（`_settings_heads` 是 open_settings
        挂到 App 上的区块标题表）。查不到就保持 None —— 遮罩对 None 目标不挖洞，
        面板照常居中，不会指错地方。
      · 讲**某一项**而不是整页时，带 `anchor=(page, 名字)`：目标改从
        `app._settings_anchors` 取"这一行的标签 / 控件 / 摘要三格"，遮罩圈整行
        （坑 302：提示词优化那一步原来圈的是页头「通用」，说明面板正好压住下拉框）。
      · `key` 用导航叶子 key（两种用户模式同名：files / eng / c_prov / remote /
        general / about），`eng` 走 nav_hide 但照旧定位得到（输出栏「去配置引擎」
        就是先例）。
      · 每一步还会带 `nav_target`（App 的 on_step 现取 `SideNav.row_of(key)`）：
        SpotlightGuide 在左栏那一行画一块"选中态"高亮，告诉用户这个功能在导航栏
        哪儿。`eng` 没有自己的行，交回替身行「模型文件与引擎」（同一页，坑 304）。
      · `panel="right"`：说明面板优先放到**洞右边那片没有字的空处**（窗口右边缘），
        右边缘被洞占着、余量不足 240px 时才退回洞下方（W 2026-10-18：别盖住正文）。
    覆盖范围（W 定）：模型文件与引擎 / 服务商与密钥 / 远程访问 / 通用（只讲提示词
    优化）/ 关于与诊断；高级模式独有的页不进引导 —— 高级用户重看也会接这段，
    多逛一遍无妨。
    """
    return [
        {"page": "files", "section": "files", "key": "files", "target": None,
         "panel": "right",
         "title": texts.GUIDE_S_MDL_TITLE,
         "body": [texts.GUIDE_S_MDL_1, texts.GUIDE_S_MDL_2, texts.GUIDE_S_MDL_3]},
        {"page": "files", "section": "eng", "key": "eng", "target": None,
         "panel": "right",
         "title": texts.GUIDE_S_ENG_TITLE,
         "body": [texts.GUIDE_S_ENG_1, texts.GUIDE_S_ENG_2, texts.GUIDE_S_ENG_3]},
        {"page": "cloud_p", "section": "prov", "key": "c_prov", "target": None,
         "panel": "right",
         "title": texts.GUIDE_S_PRV_TITLE,
         "body": [texts.GUIDE_S_PRV_1, texts.GUIDE_S_PRV_2, texts.GUIDE_S_PRV_3]},
        {"page": "remote", "section": "remote", "key": "remote", "target": None,
         "panel": "right",
         "title": texts.GUIDE_S_RMT_TITLE,
         "body": [texts.GUIDE_S_RMT_1, texts.GUIDE_S_RMT_2, texts.GUIDE_S_RMT_3]},
        # 这一步讲的是「通用」页里的**提示词优化那一行**（下拉框），不是整页
        {"page": "general", "section": "general", "key": "general", "target": None,
         "panel": "right", "anchor": ("general", "prompt_opt"),
         "title": texts.GUIDE_S_GEN_TITLE,
         "body": [texts.GUIDE_S_GEN_1, texts.GUIDE_S_GEN_2, texts.GUIDE_S_GEN_3]},
        {"page": "about", "section": "about", "key": "about", "target": None,
         "panel": "right",
         "title": texts.GUIDE_S_ABOUT_TITLE,
         "body": [texts.GUIDE_S_ABOUT_1, texts.GUIDE_S_ABOUT_2, texts.GUIDE_S_ABOUT_3]},
    ]
