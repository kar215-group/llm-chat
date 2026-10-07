# -*- coding: utf-8 -*-
"""tools/check_structure.py — llm_console 结构检查器（2026-10-03 新增，run_all 第 13 套件）

判定全部走 AST / symtable 只读源码，**零 import 被测包**（G1/G2/G4 走 AST、[4] 走 symtable，
都只读源码文件）—— 包被改坏时本工具照样可跑，铁律 1 无从触碰。四段判据：

  [1] Mixin 方法名不重叠（坑 35）
      基类自动发现：`ui/app.py` 的 `class App(...)` 基类列表就是 MRO 的真源，新增 /
      删除 Mixin 自动被覆盖，从根上消除"检查清单腐烂"。任一方法名（含 `__init__`——
      Mixin 里定义 `__init__` 会被 App 的 MRO 静默跳过、从不执行，正是同族静默失效）
      出现在 ≥2 个类（含 App 自身）= 红。零白名单：共享的正确形态是"定义一处、他处
      调用"（`_append` 定义于 ChatMixin、被多条链调用——调用不是定义）。若未来真要
      两个类**有意**同名，加 ALLOWED_DUP 常量（一行一名字一理由）并登记 `99-待确认
      规则清单.md`。App MRO 之外的独立类（widgets.* 也有 `self._proc` 这类同名字段）
      不在类集合里，天然不判。

  [2] NAV_SPEC / SIMPLE_NAV_SPEC ↔ @section(page, sec) 键一致（坑 142 同族）
      2026-10-07 起设置页有两种用户模式，导航规格是**两份**：NAV_SPEC（高级用户模式）
      与 SIMPLE_NAV_SPEC（普通用户模式，默认）。两份都必须是纯字面量
      （`ast.literal_eval` 是原子求值：要么全量成功、要么
      抛错判红——机制上不存在"看起来是字面量、实际是调用、却被漏检"的中间态）。
      三向判据：① 两份叶子并集 − registry 差集非空 = 红（左栏点过去显示"还没接上构建
      函数"）；② registry − 并集差集非空 = 红（注册了但两种模式都到不了；只挂其中
      一份的区块——api/svc/cmedia 只在高级、cimg/cvid 只在普通——不算死区块）；
      ③ NAV 内部一致性：高级每叶子 page / section / title / help 四项齐全，普通模式
      help 允许为空（那一套不放 "?" 气泡，2026-10-07 W 定）、其余三项齐全、同一份内
      叶子 key 不重复、`nav_hide` 的替身 key
      必须是同一份里的有效叶子（替身改名则悬空）；同一 (page, sec) 键注册两次 = 红（静默覆盖）。
      ② 有一处**明账**豁免：NAV_HIDDEN（一行一键一理由）—— 给"确实到得了、但有意不走
      左栏"的隐藏页用（如开发者选项，入口在关于页版本号上）。豁免不是免检：豁免的键
      必须有模块级**字面量**导航项指向它，否则照旧判红。
      `@section` 的两个参数必须是常量（变量 / 调用 = 红）。literal_eval 失败本身 = 红，
      红文案三段式见实现——检查绝不静默跳过，这是防检查器自身腐烂的机制。

  [3] App 实例字段归属（写口径判红，读口径仅报告；"显眼不禁止"的落地）
      数据字段（`App.__init__` 字段 ∪ 出现过 Store 写的 `self.X`，方法名整体排除——
      那是 [1] 的领域）被 ≥2 个 Mixin **写** = 红；`App.__init__` 的初始化写是铁律 3
      的预期形状（App 是状态中心），不参与判红。跨 Mixin **读** 是这个架构的设计
      而不是违规，只在 `-v` 时列出。`__init__` 完备性（被写但不在 `__init__` 的字段）
      只入报告区不判红——漏初始化是响亮的 AttributeError，首次访问就现形。
      G4_WHITELIST 一行一字段一理由；本文件在 git 里，白名单变更随 diff 被 review。

  [4] 全包未绑定名字（坑 93 家族；原 D:\\tmp\\audit_unbound.py 判据本体并入）
      symtable 递归全部子作用域（模块顶层 / 类体 / 函数 / 方法 / lambda / 推导式）：
      引用了、但既不在本作用域绑定（赋值 / 参数 / import / for / with as / except as /
      global）、也不在模块顶层绑定、又不是 builtin —— 即运行期必炸的 NameError
      （0.0.2 唯一的 P0 就是子线程 StreamHandler 引用未导入名字，界面零痕迹）。
      模块隐式属性豁免见 MOD_IMPLICIT（语言定义的封闭集，永不需维护）。

不覆盖（先登记 `99` 再扩判据，别静默拓宽）：
  - [1]/[3]：monkey-patch 挂方法、类体外定义再挂进类、`setattr(self, ...)` 动态写、
    别名（`s = self; s.x = 1`）—— 项目均无此写法（`setattr(` 全项目仅 app.py 一处、
    对 self 动态清属性，属循环遍历不涉模块名）。
  - [2]：渲染期形状（左栏行数、控件数）不在本段，`test_settings_layout.py` 的断言
    继续兜（它抓"建出来的东西变形"，这里抓"注册对不上"）。
  - [4]：先用后赋的 UnboundLocalError（作用域内有绑定 → is_global 为假 → 不报）、
    函数内 import 前引用、跨模块 monkey-patch、`import *`（会漏报；项目禁用此写法）。

用法：
  python tools/check_structure.py        # 四段全跑；全绿 exit 0，任一红 exit 1
  python tools/check_structure.py -v     # 附：[3] 跨 Mixin 读清单（报告区，不判红）

挂进 `_selftest/run_all.py`（本地行为，run_all 不进仓库——见 `.gitignore:89-90`）：
  SUITES 追加 ("../tools/check_structure.py", "…")；点名跑
  `python _selftest/run_all.py check_structure`（run_all 按 basename 匹配）。
  run_all 摘要抓每套件**最后一行**，本工具约定最后一行 = "结果: N 红 / …"。
"""
import ast
import builtins
import symtable
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PKG = ROOT / "llm_console"
UI = PKG / "ui"

REDS = []          # (段号, 描述) —— 任意非空 → exit 1


def red(seg, msg):
    REDS.append((seg, msg))
    print("  FAIL  %s" % msg)


def ok(msg):
    print("  OK    %s" % msg)


# =========================================================================
# [1] Mixin 方法名不重叠（坑 35）
# =========================================================================
ALLOWED_DUP = {}   # 未来若真要"两个类有意同名靠 MRO 分派"：一行一名字一理由，并登记 99


def find_classes():
    """返回 (基类名列表, {类名: ClassDef}, {类名: 文件名})；类缺失记红、值为 None。"""
    tree = ast.parse((UI / "app.py").read_text(encoding="utf-8"))
    app_cls = next((n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "App"), None)
    if app_cls is None:
        red("[1]", "ui/app.py 里找不到 class App —— MRO 真源丢失，判据无法继续")
        return [], {}, {}
    bases = [getattr(b, "id", None) for b in app_cls.bases]
    bad = [b for b in bases if not b]
    if bad:
        red("[1]", "App 的基类列表里有非简单名基类（%r）——自动发现只认同包内直名" % bad)
    bases = [b for b in bases if b]
    classes = {"App": app_cls}
    where = {"App": "app.py"}
    for m in bases:
        classes.setdefault(m, None)
    for p in sorted(UI.glob("*.py")):
        t = ast.parse(p.read_text(encoding="utf-8"))
        for n in t.body:
            if isinstance(n, ast.ClassDef) and n.name in classes and classes[n.name] is None:
                classes[n.name] = n
                where[n.name] = p.name
    for m in bases:
        if classes.get(m) is None:
            red("[1]", "Mixin %r 在 ui/*.py 顶层定义里找不到（更名 / 移动后 02 §2 模块地图失联）" % m)
    return bases, classes, where


def direct_methods(cls_node):
    """类体**直接定义**的方法名（AST 只看类体一层，天然不含继承与嵌套函数）。"""
    return [n.name for n in cls_node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def check_g1():
    print("[1] Mixin 方法重名（自动发现基类，MRO 真源 = ui/app.py 的 class App）")
    bases, classes, where = find_classes()
    live = {k: v for k, v in classes.items() if v is not None}
    total = sum(len(direct_methods(v)) for v in live.values())
    if len(live) != len(classes):
        ok("（类集合不完整，可判部分：%d/%d 个类，共 %d 方法）" % (len(live), len(classes), total))
    else:
        ok("类集合完整：App + %d 个 Mixin，共 %d 个直接定义的方法" % (len(bases), total))
    owner = {}
    for cls, node in live.items():
        for m in direct_methods(node):
            owner.setdefault(m, []).append(cls)
    dup = {m: cs for m, cs in owner.items()
           if len(cs) >= 2 and m not in ALLOWED_DUP}
    for m in sorted(dup):
        red("[1]", "方法 %r 定义于多个类（MRO 静默取先者）：%s" %
            (m, " + ".join("%s(%s)" % (c, where[c]) for c in sorted(dup[m]))))
    if not dup:
        ok("零重名 —— 新增方法先想到的一步已经由本段代劳（坑 35）；"
           "自检脚本助手函数仍按坑 78 人工全包搜")
    return live, where


# =========================================================================
# [2] NAV_SPEC ↔ @section(page, sec) 键一致
# =========================================================================
def _flatten_nav(items, out):
    """复刻 settings._nav_leaves 的拍平语义：有 children 递归、无 children 是叶子。"""
    for it in items:
        kids = it.get("children")
        if kids:
            _flatten_nav(kids, out)
        else:
            out.append(it)


_NON_LITERAL = (ast.Call, ast.Name, ast.JoinedStr, ast.Attribute, ast.Starred,
                ast.IfExp, ast.Lambda, ast.Compare, ast.Await)

# [2]「② registry − NAV 叶子」那一半的**唯一**豁免口：一行一键一理由（同 ALLOWED_DUP 的规矩，
# 变更随 git diff 被 review，并登记 `99-待确认规则清单.md`）。
# 用它的前提是"这一页确实到得了，只是**有意**不走左栏"：
#   - 开发者选项（W 2026-10-04）：入口是"关于页那行版本号连点 5 次"，见 settings.DEV_NAV_ITEM。
#     它故意不进 NAV_SPEC 是有理由的：NAV_SPEC 描述的是"普通用户看得到的左栏"，
#     而且这一页不放 "?"（help 为空）—— 塞进去会连带打翻 [2] 自己的"四项齐全"判据
#     和 test_settings_layout 的 11 个叶子数断言。
# 光豁免会让"死区块"有地方躲，所以 check_g2 末尾会回头验一遍：豁免的键必须有
# **模块级字面量**导航项指向它（`DEV_NAV_ITEM` 那种），否则照旧判红。
NAV_HIDDEN = {
    ("dev", "dev"): "开发者选项：入口藏在关于页版本号上（连点 5 次），见 settings.DEV_NAV_ITEM",
}


def check_g2():
    print("[2] NAV_SPEC / SIMPLE_NAV_SPEC ↔ @section 键一致（导航到不了 / 接不上，都在这一段抓）")
    src = (UI / "settings.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # --- @section 常量参数注册集（搜全树：decorator 都是嵌套在 open_settings 里的用法）---
    reg_pairs = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.FunctionDef):
            continue
        for d in n.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                    and d.func.id == "section"):
                if len(d.args) != 2 or not all(isinstance(a, ast.Constant) for a in d.args):
                    red("[2]", "settings.py:%d @section 参数不是两个常量 —— 键必须能被静态读到" % d.lineno)
                    continue
                reg_pairs.append((d.args[0].value, d.args[1].value))
    # --- 两份规格的静态求值（原子：要么全成、要么判据过期判红）---
    # 2026-10-07 起设置页有**两种用户模式**：NAV_SPEC = 高级用户模式（原样），
    # SIMPLE_NAV_SPEC = 普通用户模式（默认；无 "?" ⇒ help 一律空串，且允许出现
    # 普通模式专有的区块键 cimg/cvid）。死区块判据按**两份的并集**算：
    # 只挂其中一份的区块（api/svc/cmedia 只在高级；cimg/cvid 只在普通）都不是死区块。
    def _spec_node(name):
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(
                    getattr(t, "id", None) == name for t in n.targets):
                return n.value
        return None

    specs = {}
    for name, help_required in (("NAV_SPEC", True), ("SIMPLE_NAV_SPEC", False)):
        node = _spec_node(name)
        if node is None:
            red("[2]", "settings.py 模块级找不到 %s 赋值 —— 判据失效，请更新本工具 [2] 段" % name)
            return
        try:
            specs[name] = (ast.literal_eval(node), help_required)
        except (ValueError, SyntaxError, TypeError):
            bad = next((x for x in ast.walk(node) if isinstance(x, _NON_LITERAL)), None)
            loc = ("settings.py:%d:%d — %s" % (bad.lineno, bad.col_offset, type(bad).__name__)
                   if bad is not None else "（未定位到具体节点）")
            red("[2]", "%s 不再是纯字面量（首处非法节点 %s）——判据过期：静态求值、双向差集、"
                       "键完整性本轮均未执行。出路二选一：(a) 改回纯字面量（导航规格是注册表数据，"
                       "代码不进数据）；(b) 结构确实要变（i18n / 路径计算）：先更新本工具 [2] 段的"
                       "求值方式，再改规格 —— 顺序勿反" % (name, loc))
            return

    all_leaves = {}          # name → leaves
    nav_keys = set()
    for name, (spec, _hr) in specs.items():
        leaves = []
        _flatten_nav(spec, leaves)
        all_leaves[name] = leaves
        nav_keys |= {(it.get("page"), it.get("section")) for it in leaves}
    reg_keys = set(reg_pairs)
    missing = sorted(nav_keys - reg_keys)
    dead = sorted(reg_keys - nav_keys - set(NAV_HIDDEN))
    if missing:
        red("[2]", "NAV 叶子未接 @section（点过去显示「还没接上构建函数」）：%s" % missing)
    if dead:
        red("[2]", "@section 注册了但两种模式的导航都到不了（死区块 / 漏挂规格）：%s" % dead)
    # 豁免项得真的"到得了"：模块级必须有个**字面量**导航项（如 DEV_NAV_ITEM）指向它。
    # 少了这一验，NAV_HIDDEN 就成了死区块的遮羞布 —— 写进去一行就再没人管它了。
    hid_ok = set()
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        try:
            val = ast.literal_eval(n.value)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(val, dict) and (val.get("page"), val.get("section")) in NAV_HIDDEN:
            hid_ok.add((val.get("page"), val.get("section")))
    for k in sorted(set(NAV_HIDDEN) - hid_ok):
        red("[2]", "NAV_HIDDEN 豁免的 %s 找不到模块级字面量导航项（如 DEV_NAV_ITEM）——"
                   "「隐藏」不能当「死区块」的遮羞布：要么把它接回去，要么说明为什么到得了" % (k,))
    for name, (spec, help_required) in specs.items():
        leaves = all_leaves[name]
        key_set = {it.get("key") for it in leaves}
        if len(key_set) != len(leaves):
            dup = sorted(k for k in key_set
                         if sum(1 for it in leaves if it.get("key") == k) > 1)
            red("[2]", "%s 里叶子 key 重复（SideNav 选中/跳转会认错行）：%s" % (name, dup))
        for it in leaves:
            needs = ("page", "section", "title", "help") if help_required \
                else ("page", "section", "title")
            for need in needs:
                if not str(it.get(need) or "").strip():
                    red("[2]", "%s 叶子 key=%r 缺 %-7s —— 四项不全（坑 142 同族的注册数据完整性）"
                        % (name, it.get("key"), need))
            if it.get("nav_hide") is not None and it.get("nav_hide") not in key_set:
                red("[2]", "%s 叶子 key=%r 的 nav_hide 指向不存在的替身 %r —— 替身改名后悬空"
                    % (name, it.get("key"), it.get("nav_hide")))
    if len(reg_pairs) != len(reg_keys):
        dup = sorted(k for k in reg_keys if reg_pairs.count(k) > 1)
        red("[2]", "同一 (page, section) 注册了两次（registry 后写覆盖先写）：%s" % dup)
    hid = sorted(set(NAV_HIDDEN) & reg_keys)
    ok("NAV 高级 %d 叶子 + 普通 %d 叶子（并集 %d 键）↔ @section %d 处注册，双向差集为空；"
       "四项齐全（普通模式 help 允许为空 = 不放 \"?\"）；nav_hide 替身有效%s"
       % (len(all_leaves["NAV_SPEC"]), len(all_leaves["SIMPLE_NAV_SPEC"]),
          len(nav_keys), len(reg_pairs),
          ("；隐藏页豁免 %d 处（%s，均有模块级字面量导航项兜着）"
           % (len(hid), ", ".join("%s/%s" % k for k in hid))) if hid else ""))


# =========================================================================
# [3] App 实例字段归属（G4：写口径判红，读口径仅报告）
# =========================================================================
G4_WHITELIST = {
    # 一行一字段一理由；变更随 git diff 被 review —— 这份清单本身就是"显眼"的实现。
    "_proc":       "chat 与 service 共享同一 llama-server 进程句柄（换载要停的就是它）；"
                   "chat.py:373-377 与 service.py:123-127 有同构重复，收敛方案挂账 16 §16",
    "_stop_flag":  "chat / img / vid 三链共用的停止旗标（配 _img_gen / _vid_gen 世代号防串）",
    "_cloud_tid":  "img / vid 共用当前云端任务 id（异步任务取消 / 取回要用）",
    "_cloud_pid":  "img / vid 共用云端任务归属 provider（取消时按它取密钥）",
}


def _self_attr_users(cls_node):
    """走全树（含方法内嵌套函数）：self.X 的写 / 读归属。返回 (写 attr 集, 读 attr 集)。"""
    writes, reads = set(), set()
    for n in ast.walk(cls_node):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "self"):
            (writes if isinstance(n.ctx, ast.Store) else reads).add(n.attr)
    return writes, reads


def check_g3(live, verbose):
    print("[3] App 实例字段归属（写口径判红：单 owner 或白名单；读口径仅报告）")
    if "App" not in live:
        red("[3]", "App 类缺失，本段跳过")
        return
    writes, reads = {}, {}
    for cls, node in live.items():
        writes[cls], reads[cls] = _self_attr_users(node)
    method_names = {m for node in live.values() for m in direct_methods(node)}
    init_node = next((n for n in live["App"].body
                      if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
    if init_node is None:
        red("[3]", "App 里找不到 __init__ —— 实例字段唯一初始化点（铁律 3）失联")
        return
    init_fields = _self_attr_users(init_node)[0]
    all_writes = set().union(*[writes[c] for c in live]) if live else set()
    data_fields = (init_fields | all_writes) - method_names   # 方法名是 [1] 的领域
    mixins = [c for c in live if c != "App"]
    bad = []
    shared = []
    for f in sorted(data_fields):
        ws = sorted(c for c in mixins if f in writes[c])
        if len(ws) >= 2:
            if f in G4_WHITELIST:
                shared.append("%s ← %s（白名单：%s）" % (f, "+".join(ws), G4_WHITELIST[f]))
            else:
                bad.append("%s ← %s（历史耦合或漏收敛；有意共享则加白名单 + 理由 + 通行 review）"
                           % (f, "+".join(ws)))
    for f in sorted(bad):
        red("[3]", "字段被多个 Mixin 写：" + f)
    ok("数据字段 %d 个（__init__ %d 个）；多 Mixin 写且进白名单 %d 个：%s"
       % (len(data_fields), len(init_fields), len(shared),
          "、".join(sorted(G4_WHITELIST)) or "（无）"))
    not_init = sorted(data_fields - init_fields)
    if not_init:
        print("  报告  被 Mixin 写、却不在 App.__init__ 的字段（不判红——漏初始化是响亮的 "
              "AttributeError，首次访问即现形）：%s" % "、".join(not_init))
    unused_wl = sorted(set(G4_WHITELIST) - {f for f in data_fields})
    if unused_wl:
        print("  报告  白名单条目已无多 Mixin 写，可退役（删掉让 diff 显眼）：%s" % "、".join(unused_wl))
    if verbose:
        cross = {f: sorted(c for c in mixins if f in reads[c])
                 for f in sorted(data_fields)
                 if sum(1 for c in mixins if f in reads[c]) >= 2}
        print("  报告  跨 Mixin 读（设计常态，仅列出）：")
        for f, rs in cross.items():
            print("        %-14s 读于 %s" % (f, "、".join(rs)))


# =========================================================================
# [4] 全包未绑定名字（坑 93 家族；原 D:\tmp\audit_unbound.py 判据并入 + 两处修正）
# =========================================================================
# 修正 1：模块隐式属性豁免 —— 解释器注入、从未被显式绑定（symtable 顶层不列），
#         语言定义的封闭集，不随项目演进增长，永不需维护。
MOD_IMPLICIT = {"__file__", "__name__", "__doc__", "__spec__",
                "__package__", "__loader__", "__cached__"}
_BUILTINS = set(dir(builtins))


def check_unbound():
    print("[4] 全包未绑定名字（symtable 递归；子线程 NameError 界面零痕迹，坑 93）")
    mod_count = 0
    problems = []
    for path in sorted(PKG.rglob("*.py")):
        mod_count += 1
        src = path.read_text(encoding="utf-8")
        try:
            st = symtable.symtable(src, str(path), "exec")
        except SyntaxError as e:
            problems.append((path.name, "<SYNTAX>", str(e)))
            continue
        top = {s.get_name() for s in st.get_symbols()} | _BUILTINS | MOD_IMPLICIT

        def walk(tbl, qual):
            for sym in tbl.get_symbols():
                # is_global：本作用域引用了、既不局部也非参数；is_assigned 为假 = 从未绑定
                if sym.is_global() and sym.is_referenced() and not sym.is_assigned():
                    name = sym.get_name()
                    if name not in top:
                        problems.append((path.name, qual, name))
            for child in tbl.get_children():
                walk(child, qual + "." + child.get_name())

        walk(st, path.stem)
    seen, out = set(), []
    for mod, qual, name in problems:
        if (mod, name) in seen:
            continue
        seen.add((mod, name))
        out.append((mod, qual, name))
    for mod, qual, name in out:
        red("[4]", "%s: %s 引用了未绑定名字 %r（运行期必炸的 NameError，先补绑定再看）"
            % (mod, qual, name))
    ok("扫 %d 个模块，未绑定名字 %d 个（豁免：builtins + 模块隐式属性 %d 字封闭集）"
       % (mod_count, len(out), len(MOD_IMPLICIT)))


# =========================================================================
# 汇总
# =========================================================================
def main():
    verbose = "-v" in sys.argv[1:]
    t0 = time.perf_counter()
    live, where = check_g1()
    check_g2()
    check_g3(live, verbose)
    check_unbound()
    dt = (time.perf_counter() - t0) * 1000
    print("-" * 72)
    if REDS:
        for seg, msg in REDS:
            print("红 %-4s %s" % (seg, msg[:80]))
    print("结果: %d 红 / 4 段判据（%.0fms）" % (len(REDS), dt))
    return 1 if REDS else 0


if __name__ == "__main__":
    sys.exit(main())
