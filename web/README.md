# script-to-video Web 端

把视频脚本（分镜表 / 纯文字故事 / 一句话文案 / docx）转成 MiniMax H3 生成的视频。
你只做两件事：**输入脚本 + 逐镜确认**，其余自动完成。

## 流程

1. 粘贴脚本（或上传 docx）→ 点「解析优化」
2. 系统把脚本拆成分镜 + 优化提示词（可手动改）
3. 逐镜点「生成」→ 后台提交 ComfyUI 生成（可反复「重新生成」直到满意）；打开「全局设置 → 上一镜尾帧续接」（默认关）后，第 2 镜起自动抽上一镜成片的尾帧当本镜首帧，动作接着上一镜演（手动上传的首帧优先）
4. 全部满意后点「合成成片」→ 拼接 + 叠字幕 → 预览/下载（「全局设置 → 过渡补帧」默认开会给每两镜之间插一段 H3 过渡片，关掉就是硬切；镜头已用尾帧续接时建议关）

## 前置条件

- ComfyUI 已运行在 `http://127.0.0.1:8188`（MiniMax H3 模型已下载，见 skill 的 references/workflow.md）
- 参考图/自动匹配素材（抽主体作参考）：`diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors` + `background_removal/birefnet.safetensors`
- clora 环境：`D:\ProgramData\anaconda3\envs\clora\python.exe`（torch 2.11+cu130）
- 自定义节点 ComfyUI-KJNodes（提供 Sage 补丁节点）
- `imageio-ffmpeg`（pip install imageio-ffmpeg，拼接用）
- 自动匹配素材图：Ollama 运行在 `http://127.0.0.1:11434` 且有 bge-m3 模型（向量相似度）；Ollama 缺失时自动回退规则打分，功能不中断
- 本地 AI 拆分镜：同一台 Ollama 有对话模型（默认 qwen3:8b）时，「解析分镜」优先用它拆镜并补景别/光线描述；没有对话模型或调用失败时自动回退格式规则拆分，不中断

## 启动

```
D:\ProgramData\anaconda3\envs\clora\python.exe web\app.py
```

然后浏览器打开 **http://127.0.0.1:9081**

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| COMFY_URL | http://127.0.0.1:8188 | ComfyUI 地址 |
| OUTPUT_DIR | D:\app\comfyui\output\video | 成片输出目录 |
| FONT | C:/Windows/Fonts/simhei.ttf | 字幕字体 |
| PORT | 9081 | Web 端口 |
| OLLAMA_URL | http://127.0.0.1:11434 | Ollama 地址（bge-m3 向量相似度） |
| BGE_MODEL | bge-m3:latest | 相似度用的 embedding 模型 |
| BGE_COS_MIN | 0.45 | 纯向量兜底的最低余弦相似度 |
| OLLAMA_CHAT_MODEL | qwen3:8b | 本地 AI 拆分镜用的对话模型；该模型不存在时自动取 Ollama 里第一个非向量模型 |

## 迁移到别的机器

1. 整个 `web/` 目录复制过去即可（标准库后端 + 可选 jieba，环境包已含）。
2. 改环境变量（COMFY_URL / OUTPUT_DIR / FONT），或改 `app.py` 顶部的默认值。
3. 那台机器需满足「前置条件」（ComfyUI + clora + H3 模型 + Ollama bge-m3）。
4. 若拼接报 ffmpeg 错误：`pip install imageio-ffmpeg`。

## 目录结构

```
web/
├── app.py              后端（纯标准库）
├── static/
│   ├── index.html      前端页面
│   └── app.js          前端逻辑
├── requirements.txt
└── README.md
```
