# ComfyUI Director Desk（AI 视频导演台）

本地 ComfyUI 之上的视频生产编排层：**脚本 → 分镜 → 逐镜生成 → 拼接 + 字幕 → 成片**。
全程本地运行，无需外部 API（可选接入 DeepSeek / Qwen-VL / Neo4j / Ollama）。

> **当前版本：V1.2** — 素材匹配升级为本机 Ollama bge-m3 向量语义匹配（离线）、本地 AI 拆镜、多项目管理、Wan2.2 生视频支持。
>
> V1.1 — Neo4j + MySQL 联合素材库（图算法检索）、素材上传、生成产物不入库、favicon/日志等体验优化。

## 架构（三层）

```
智能层   DSH Skill「comfyui-workflow-builder」   白话文 → 工作流 / 优化 / 导入（cli.py 六子命令）
编排层   AI 导演台 web/app.py（端口 9081）       脚本→分镜→逐镜生成→拼接；素材库；角色；知识图谱
引擎层   ComfyUI 0.34.0（端口 8188）             MiniMax H3 / Wan2.2 5B / Wan2.2 14B 三套管线
```

## 功能特性

- **输入层**：脚本粘贴 / docx 上传 / AI 写剧本（DeepSeek，可选外网）、本地 AI 拆镜（Ollama qwen3:8b，离线）、手动新增镜头、追加模式
- **增强层**：首帧上传、多图参考（H3 AddGuide）、角色固定（描述注入 + 参考图 + 角色出场）、配音上传、自动匹配素材图（本机 Ollama bge-m3 向量语义，离线）
- **控制层**：全局模型选择、速度档位（极速/均衡/标准/高清）、景别/运镜/时长/步数/种子/负向词、分辨率比例
- **项目层**：多项目管理（首页 home.html 项目选择 + 按项目隔离 state 数据）
- **数据层**：自动保存（0.8s 防抖）、素材库（`assets\` 只收图片素材，上传/打标签，生成产物不入库）、素材检索
  - 素材存储：MySQL `comfyui_assets`（事实/自增 id/使用信息/匹配流水）+ Neo4j（按 id 建 Asset-Tag-FITS 图，图算法检索）；素材匹配用 Ollama bge-m3 向量余弦相似度，任一依赖不可用自动回退本地 JSON/规则打分
- **输出层**：逐镜预览、批量生成、拼接 + drawtext 字幕、镜头间过渡补帧、工作流导出（按模型分文件）

## 模型管线

| 能力 | MiniMax H3 | Wan2.2 5B TI2V | Wan2.2 14B | Wan2.2 S2V |
|---|---|---|---|---|
| 音频 | ✅ 原生音画 | ❌ 无声（配音文件） | ❌ 无声（配音文件） | ✅ 语音驱动（人声→画面） |
| 帧率 / 5 秒 | 24fps / 124 帧 | 16fps / 81 帧 | 16fps / 81 帧 | 16fps / 约 77 帧/块 |
| 参考图 | 首帧 + 末帧 + 多图 AddGuide | 单张首帧 | 单张首帧（I2V）/ 无（T2V） | 单张首帧 |
| 档位步数 | 4 / 12 / 25 / 32 | 10 / 20 / 30 / 40 | 10 / 20 / 20 / 30 | 6 / 10 / 10 / 15 |
| 体积 | ~25GB（INT8 + nvfp4） | ~19GB | ~57GB（4×fp8） | ~57GB（fp8） |
| 定位 | 有配音的成片 | 快速出片 | 最高画质 | 语音生视频（口型/动作随人声） |

## 快速开始

前置：Windows + NVIDIA GPU，ComfyUI 0.34.0 已装好模型（diffusion 82GB / text_encoders 21GB / vae 7GB），
Python 3.10 环境（torch 2.11+cu130，含 `imageio-ffmpeg`）。

```bat
:: 1. 启动 ComfyUI（log 重定向）
D:\ProgramData\anaconda3\envs\clora\python.exe main.py --disable-pinned-memory --use-sage-attention > comfyui.log 2>&1

:: 2. 启动导播台
D:\ProgramData\anaconda3\envs\clora\python.exe web\app.py > director.log 2>&1
```

打开 http://127.0.0.1:9081。可选功能需把 `web/.env.example` 复制为 `web/.env` 并填入密钥
（AI 写剧本 DeepSeek、素材打标签 Qwen-VL、知识图谱 Neo4j bolt://localhost:7688）。

本地 AI 拆镜 + 素材语义匹配走本机 Ollama（`http://127.0.0.1:11434`）：`qwen3:8b` 拆镜、`bge-m3` 向量，
模型缺失时自动回退格式规则/打分，功能不中断；内网机器带 Ollama 见 `migration/迁移_Ollama本地AI.md`。

## 目录结构

```
├── web/                       导演台本体（纯标准库后端 + 原生 JS 前端）
│   ├── app.py                 后端（http.server，零第三方依赖）
│   ├── _mcp_http.py           modao MCP 客户端（脚本辅助，可选）
│   ├── script_graph.json      景别/运镜知识图谱数据
│   └── static/                home.html（项目选择）+ index.html + app.js + 本地 vendor（Tailwind/Iconify/Cytoscape）
├── workflows/                 四套管线的 ComfyUI 工作流（H3 原生配音/多图参考、Wan2.2 5B/14B 文生/图生、Wan2.2 S2V 语音生视频）
├── migration/                 内网迁移：环境安装 + 启动 + 增量打包 + Ollama 离线打包脚本（不含 clora_env.tar.gz 大包）
├── 技术架构.md                系统全景解析（改系统前先看）
├── 迭代手册.md                恢复现场 / 日常改动 / 模型管理 / 内网迁移
└── stop_comfyui.bat           停止快捷脚本
```

## 数据与密钥

本仓库**不含**以下内容（均在 `.gitignore`）：

- `web/.env`（API 密钥）、`web/state.json`（镜头/角色数据）、`web/assets_tags.json`（素材标签）
- `models/`、`output/`、`input/`（模型与成片素材）
- `migration/clora_env.tar.gz`、`incremental_*.zip`（迁移大包，用 `migration/make_incremental.bat` 重新生成）

## 技术栈

- 后端：Python 3.10 纯标准库（`http.server` 多线程），零第三方依赖
- 前端：原生 JS + Tailwind（本地 vendor），无框架无构建，全本地化内网可用
- 视频：ComfyUI 0.34.0（含 comfy_api/comfy_api_nodes 定制扩展、KJNodes、MiniMax-H3-Turbo、MiniMaxH3-Director）
- 拼接：imageio-ffmpeg 本地二进制 + drawtext 字幕
- 可选：DeepSeek（剧本）、Qwen-VL（素材标签）、Neo4j（本地知识图谱）
- 本地 AI：Ollama（`qwen3:8b` 拆镜 + `bge-m3` 素材向量匹配），全离线，缺失自动回退

## 维护

- 迭代流程、常用命令、环境打包、模型管理见 `迭代手册.md`
- 工作流知识沉淀在全局 skill `comfyui-workflow-builder` 的 `references/workflow-format.md`
- 上传 GitHub：本地 checkout 为本仓库根目录，把 `D:\app\comfyui` 下更新后的文件复制过来提交即可
