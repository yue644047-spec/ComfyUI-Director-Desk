# script-to-video Web 端

把视频脚本（分镜表 / 纯文字故事 / 一句话文案 / docx）转成 MiniMax H3 生成的视频。
你只做两件事：**输入脚本 + 逐镜确认**，其余自动完成。

## 流程

1. 粘贴脚本（或上传 docx）→ 点「解析优化」
2. 系统把脚本拆成分镜 + 优化提示词（可手动改）
3. 逐镜点「生成」→ 后台提交 ComfyUI 生成（可反复「重新生成」直到满意）
4. 全部满意后点「合成成片」→ 拼接 + 叠字幕 → 预览/下载

## 前置条件

- ComfyUI 已运行在 `http://127.0.0.1:8188`（MiniMax H3 模型已下载，见 skill 的 references/workflow.md）
- clora 环境：`D:\ProgramData\anaconda3\envs\clora\python.exe`（torch 2.11+cu130）
- 自定义节点 ComfyUI-KJNodes（提供 Sage 补丁节点）
- `imageio-ffmpeg`（pip install imageio-ffmpeg，拼接用）

## 启动

双击 `run.bat`，或：

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

## 迁移到别的机器

1. 整个 `web/` 目录复制过去即可（纯标准库后端，无需安装）。
2. 改环境变量（COMFY_URL / OUTPUT_DIR / FONT），或改 `app.py` 顶部的默认值。
3. 那台机器需满足「前置条件」（ComfyUI + clora + H3 模型）。
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
