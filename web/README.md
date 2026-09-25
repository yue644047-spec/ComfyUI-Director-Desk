# script-to-video Web 端

把视频脚本（分镜表 / 纯文字故事 / 一句话文案 / docx）转成 MiniMax H3 生成的视频。
你只做两件事：**输入脚本 + 逐镜确认**，其余自动完成。

## 流程

1. 粘贴脚本（或上传 docx）→ 点「解析优化」
2. 系统把脚本拆成分镜 + 优化提示词（可手动改）
3. 逐镜点「生成」→ 后台提交 ComfyUI 生成（可反复「重新生成」直到满意）；打开「全局设置 → 上一镜尾帧续接」（默认关）后，第 2 镜起自动抽上一镜成片的尾帧当本镜首帧，动作接着上一镜演（手动上传的首帧优先）
   - 每镜可点「文生图定帧」：按该镜提示词用 SDXL 出 4 张静帧，挑一张作本镜首帧——提示词里的元素（人物、道具、场景）先在静帧里定住，视频生成就不会跑偏；点缩略图可放大成「定帧大图」（←/→ 翻图、Esc 关闭、点遮罩关闭，弹窗里也能「选用为首帧」）；选中后该镜按「图+文」生成，合成时的过渡桥也会平滑落到这张图
4. 全部满意后点「合成成片」→ 拼接 + 叠字幕 → 预览/下载（「全局设置 → 过渡补帧」默认开会给每两镜之间插一段 H3 过渡片，关掉就是硬切；镜头已用尾帧续接时建议关）

## 前置条件

- ComfyUI 已运行在 `http://127.0.0.1:8188`（MiniMax H3 模型已下载，见 skill 的 references/workflow.md）
- 参考图/自动匹配素材（抽主体作参考）：`diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors` + `background_removal/birefnet.safetensors`（模型已随包内置）
- clora 环境：`D:\ProgramData\anaconda3\envs\clora\python.exe`（torch 2.11+cu130）
- 自定义节点 ComfyUI-KJNodes（提供 Sage 补丁节点）
- `imageio-ffmpeg`（pip install imageio-ffmpeg，拼接用）
- 自动匹配素材图：Ollama 运行在 `http://127.0.0.1:11434` 且有 bge-m3 模型（向量相似度）；Ollama 缺失时自动回退规则打分，功能不中断
- 分镜表（.xlsx/.xls/.docx/Markdown 表格）：按行列逐行解析，一行一个镜头，识别 时间/时长（0-5s、5秒、5、00:05，各种连字符/全角写法都认）、画面（画面/描述/内容/提示词列）、台词/文案/对白/旁白/独白/配音/解说、字幕列、音效/BGM 列；无表头或列名另类时按内容猜列，避免出现空镜头
- 旁白/配音：`（旁白）`、`配音：`、`（无旁白）` 这类标签与占位符会剥掉再进提示词（占位符整格忽略）；台词列是旁白/配音时，原话同时作为该镜字幕，成片贴底显示
- 字幕渲染：白字黑边、字号 26（848x480 成片画布，约画面高 5%）、水平居中、贴底 24px，超过 22 字自动折行往上叠；合成时用「字幕」开关整体关闭
- AI 拆分镜（纯文字/故事）：导入面板可选「自动（本地 Ollama 优先）/ 本地 Ollama / DeepSeek」。本地 Ollama 有对话模型（默认 qwen3:8b）时离线拆镜并补景别/光线描述；DeepSeek 需在「设置」里填 API Key（走 OpenAI 兼容接口）。引擎不可用或输出不可解析时自动回退格式规则拆分，不中断
- 分镜表配图：上传的 docx/xlsx 分镜表里内嵌的图片（或脚本里写到的本地图片路径、Markdown 图片语法）会自动落进 `input/frames` 并挂成对应镜头的首帧，解析完可直接「图+文」生成
- 文生图定帧：`models/checkpoints/RealVisXL_V5.0_fp16.safetensors` + `models/vae/RealVisXL_V5.0_vae.safetensors`（约 6.8GB，SDXL 写实模型）；提示词英译走 Ollama 对话模型，无对话模型时回退 DeepSeek API

## 启动

双击 `run.bat`，或：

```
D:\app\comfyui\python_embeded\python.exe web\app.py
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
| OLLAMA_KEEP_ALIVE | 30m | 拆镜后 Ollama 模型驻留时长 |
| AI_SPLIT_MAX_CHARS | 6000 | 送进模型拆镜的脚本最大字符数 |
| AI_SPLIT_TIMEOUT | 900 | 本地 Ollama 拆镜超时（秒） |
| DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL | 空 / deepseek-chat / https://api.deepseek.com | 选 DeepSeek 拆镜时的默认凭据（前端「设置」里填的优先） |

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
├── run.bat
└── README.md
```
