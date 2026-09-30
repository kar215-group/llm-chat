# LLM Chat 工作台

一个**零第三方依赖**的桌面工作台：把本地 llama.cpp 对话、sd.cpp 生图 / 生视频、以及云端
OpenAI 兼容 API 收进同一个窗口，只用 Python 标准库 + Tkinter 写成，不需要 `pip install`
任何东西，也不依赖任何 Web 服务或后台守护进程。

> 本仓库**只包含工作台代码**。推理引擎、模型权重、云端密钥都不在仓库里，
> 需要按下面的《你需要自己准备的东西》各自获取。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 本地对话 | 调用 llama.cpp 的 `llama-server`，图形界面里选模型、启动/停止服务、看思考链与 token 用量；按模型分别记忆上下文长度与 GPU 层数 |
| 发图片给模型 | 支持看图的模型（本地＝配了视觉投影器 `mmproj`，云端＝你声明过或实测过）可以直接收图 |
| 发文本文件 | `.txt` / `.md` / 代码 / **.docx** 作为附件，按模型上下文预算自动折行截取；PDF 与老版 `.doc` 会明确拒绝并提示另存 |
| 生图 | 走 sd.cpp 引擎，文生图 + 参考图编辑，产物落盘并显示在对话流里 |
| 生视频 | 走 sd.cpp 的 MiniMax-H3 链路，文生视频 / 图生视频，输出 webm / avi / webp |
| 云端对话 | 内置 DeepSeek 与阿里云百炼 Token Plan，也可自填任意 OpenAI 兼容端点；密钥单独存放，不进配置文件 |
| 本机 API | 可把"当前选中的模型"暴露成一个 OpenAI 兼容的本机端点，给 agent 或其他软件直接调用 |

## 环境要求

- **Windows**（按 Windows 设计与实测；停止服务、显存探测等少量调用用了 `taskkill` / `nvidia-smi`，跨平台需自行适配）
- **Python 3.8+**，且带 Tkinter（python.org 的 Windows 安装包默认自带；部分精简版 Python 需要单独装 `tkinter`）
- 显存 / 内存 / 磁盘取决于你选的模型，与本项目无关

## 快速开始

1. **准备推理引擎**（本仓库不含）
   - 从 llama.cpp 的 Releases 下载对应平台的预编译包（CUDA 版还要把 `cudart` 包的 dll 放到同一目录），
     或者直接跑仓库里的脚本：
     ```bash
     bash tools/deploy_llamacpp.sh ./llama-engine
     ```
     脚本支持 `BUILD` / `FLAVOR` / `MIRROR` 环境变量换构建号、换 CPU/CUDA 版本、走 GitHub 加速镜像。
   - 打开工作台：**设置 → 服务参数**，把 `llama-server` 的路径填进去。
2. **放模型**（本仓库不含）
   - 默认约定：把 `.gguf` 放进**程序目录下的 `models/`**；换目录在 **设置 → 服务参数** 的
     `models_dir` 里改（生图 / 生视频权重各有自己的目录字段，分别在 **设置 → 生图** 与 **设置 → 生视频**）。
   - 工作台会扫描这些目录并把模型分进「文本模型 / 生图模型 / 生视频模型」三组菜单；
     **设置 → 模型管理** 用来补全每个模型的 GPU 层数、上下文与 `mmproj` 配对记录。
3. **启动**
   ```bash
   python llama_gui.py        # 带控制台，第一次跑推荐，报错直接看得见
   pythonw llama_gui.py       # 无控制台窗口
   ```
   在左上角选模型 → 点「启动服务」→ 顶栏状态变成 **● 运行中** 就能对话。云端模型不需要启动服务，选上即用。

## 你需要自己准备的东西

| 用途 | 从哪拿 | 拿什么 | 放哪 |
|---|---|---|---|
| 本地对话引擎 | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) 的 Releases | `llama-server` 及其同目录运行库（CUDA 版另需 `cudart` 包里的 dll） | 任意目录，**设置 → 服务参数** 指路 |
| 对话模型 | HuggingFace 等模型站上的 GGUF（Qwen、DeepSeek、GLM、Gemma…） | `*.gguf` | `models/` 或自定模型目录 |
| 让模型能看图 | 同上模型页面 | `mmproj-*.gguf` 视觉投影器 | 与主模型同目录，界面会自动配对并标注 |
| 生图引擎 | [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) 的 Releases | `sd-cli`（Windows 为 `sd-cli.exe`）+ dll | 单独目录，**设置 → 生图** 指路 |
| 生图权重 | Qwen-Image / SD1.5 / SDXL / Flux 等的 GGUF 或 safetensors | 模型文件 | 生图模型目录 |
| 生视频 | MiniMax-H3 视频链路的**三件套**：扩散主体 GGUF、文本编码器、视频 VAE | 三个文件都要，**VAE 最容易漏** | 生视频模型目录，**设置 → 生视频** 指路 |
| 云端对话 | DeepSeek / 阿里云百炼 控制台 | API Key | **设置 → 云端 API** 的密钥按钮（存进 `secrets.json`，不入库） |

## 界面与常用操作

- **顶栏**：模型选择菜单（本地与云端混排，云端条目带「（云）」后缀）、状态灯、启动/停止服务。
- **对话区**：`Enter` 发送、`Shift+Enter` 换行；生图中可随时「停止生成」；每轮结束显示 token 用量
  （思考模型会单列"其中思考 N"）。
- **📎 附件**：图片或文本文件。被拦下时提示会说明原因，并允许你"仍按支持图片处理"一次。
- **设置共 7 页**：生成参数 / 服务参数 / 生图 / 生视频 / API 连接 / 云端 API / 模型管理。
  每页各自保存，窗口最下方的「保存」是统一入口——当前页保存不通过时不会关窗。
- **云端选模型**：填好地址与密钥 → 测试连接 → 点「选择模型…」在弹窗里勾选要收进菜单的模型
  （清单来自接口的 `/models`，拉过一次就缓存，只有点「刷新清单」才重新请求；
  也可以手填模型名让服务端验证）。同一弹窗里能给每个模型声明「图片输入：自动判断 / 支持 / 不支持」，
  **改动即时生效**，不必点确定。

## 配置文件放在哪

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `gui_config.json` | 运行期配置（含你本机的路径） | **否**（`.gitignore` 已排除） |
| `secrets.json` | 只存云端 API Key | **否**（同上，任何备份也应排除） |
| 生成产物 | 图片落在 `<sd.cpp 目录>/output/`，视频落在 `<sd.cpp 目录>/video/`；生视频还会写一份同名 `.log`（引擎原始输出 + 完整命令行），失败时界面会直接给出日志路径 | 否 |

首次运行会自动生成默认配置；换机器时重装模型、重填路径即可，没有需要迁移的隐藏状态。

## 能力边界（先看这段，省得踩坑）

- **云端目前只做到文本对话**。云端生图 / 生视频在菜单里能看到、也能归类，但选中发送会被拦下并说明原因。
- **云端模型能不能收图，接口不会告诉你**。`/models` 不带能力字段，所以只能靠你在「选择模型」里声明，
  或点「验证图片输入」发一张 1×1 图实测一次；判不出来时宁可标"未确认"也不会替你判定"不支持"。
- **本地模型没配 `mmproj` 就收不了图**，这是引擎限制，不是界面问题。
- **对话与生图 / 生视频都要吃显存**，同时占用会先弹确认；取消即放弃，不会偷偷排队。
- **附件会留在对话历史里每一轮重发**。云端预算因此有上限（可在设置里按服务商声明真实上下文窗口），
  不是"能塞多塞"——超长上下文的成本是按每一轮计的。
- 所有云端请求带一个通用客户端标识（User-Agent），用于避免部分平台把自建客户端识别成异常流量。

## 目录结构

```
llama_gui.py                 启动入口（只有十几行）
llm_console/
  core/                      配置、模型扫描、GGUF 元数据、参数计算、看图能力判定、
                             文本附件预算、服务商注册表、密钥存取、引擎进程管理
  connection/                本地 SSE 流、云端 OpenAI 兼容流、本机 API 代理
  ui/                        主窗口 + 聊天 / 生图 / 生视频 / 模型管理 / 服务 / 设置 六个 Mixin
tools/deploy_llamacpp.sh     取 llama.cpp 引擎的脚本（参数化）
```

分层规矩：`core/` 与 `connection/` **不 import tkinter**，所有界面代码只在 `ui/`。
渲染层只吃统一的事件流，本地与云端共用同一套解析。

## 打包成 exe（可选）

不打包也能用（`python llama_gui.py` 就是完整程序）。想要一个双击即开的 `llm-chat.exe`：

```bash
python -m pip install "pyinstaller==6.21.0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name llm-chat \
  --distpath dist --workpath build/work --specpath build llama_gui.py
```

- 产物是**界面程序**（约 12MB），推理引擎与模型权重仍然要按上面的表格自己准备；
  推荐把 `llm-chat.exe` 直接放进 `llama-server.exe` 所在的那一层目录 —— 配置默认按
  "与 exe 同目录"来找引擎和 `models/`，`gui_config.json` / `secrets.json` 也生成在那儿。
- 自检：`llm-chat.exe --selfcheck`（退出码 0 = 打包路径与内置服务商都正常）、`--version`。
- exe **未做代码签名**，第一次运行 SmartScreen 可能拦一下：「更多信息」→「仍要运行」。

### 自动打包与发布（GitHub Actions）

仓库自带 `.github/workflows/build.yml`，两种触发：

| 怎么触发 | 结果 |
|---|---|
| **Actions → 打包 exe 并发布 → Run workflow** | 在 `windows-latest` 上打包并自检，产物 zip 作为 artifact 挂在这次 run 上（30 天有效），**不发 Release** |
| **`git tag v0.0.2beta && git push origin v0.0.2beta`** | 打包 + 自检 + 自动建 Release 并挂上 `llm-chat-<tag>-windows-x64.zip`；标签带 `-` 或 alpha/beta/rc/preview/dev 字样的自动标预发布；Release 已存在（比如你先在网页建了草稿）时只补传 zip，不会报错 |

要点：

- **不需要配任何 secret**：建 Release 用的是工作流自带的 `GITHUB_TOKEN`（`permissions: contents: write`）。
- **tag 去掉 `v` 后必须等于 `llm_console/core/config.py` 里的 `APP_VERSION`**，不一致流水线直接失败，
  免得 Release 标着新版本、里面装的是旧 exe。发新版时先改 `APP_VERSION` 再打 tag。
- 构建环境：`windows-latest` + **Python 3.12** + PyInstaller 6.21.0。
  别改成 3.14 —— GitHub 镜像里那份 hostedtoolcache 的 3.14.7 **没装全 Tcl**（只有 `tk9.0`
  的壳、没有 `init.tcl`），打出的 exe 启动即报 `Tcl data directory ... not found`。
  构建步骤会先定位 `init.tcl` / `tk.tcl`，找不到就当场失败，不会把坏包推上 Release。

## 第三方与许可

- 本仓库只是工作台代码，不含模型权重与引擎二进制。
- **llama.cpp**：MIT；其 CUDA 发行包随附的 libomp 为 Apache-2.0 with LLVM exception。
- **sd.cpp（stable-diffusion.cpp）**：见该项目仓库声明的许可。
- **模型权重**：各自遵循其在发布平台上的条款，商用前请逐个确认。
- **云端 API**：按各服务商计费策略产生费用，注意 token 成本。

本仓库当前**未附加开源许可证**（私有用途）。需要对外开源时再补 `LICENSE`。
