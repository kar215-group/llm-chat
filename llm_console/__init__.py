# -*- coding: utf-8 -*-
"""llm_console — 本地 LLM 工作站（llama.cpp + sd.cpp + OpenAI 兼容中转）。

三层结构：
  core/        业务逻辑，不依赖 tkinter：配置、GGUF 解析、模型库、参数推算、服务、生成命令
  connection/  对外连接：SSE 流式对话、OpenAI 兼容代理
  ui/          tkinter 界面：主窗口外壳 + 6 个职责 Mixin + 独立对话框

依赖方向单向向下（ui → connection/core，connection → core，core 内部 config→gguf→models→params→server→media），
不存在反向引用。程序入口仍是仓库根目录的 llama_gui.py（桌面快捷方式指向它）。
"""

__version__ = "30.0"
