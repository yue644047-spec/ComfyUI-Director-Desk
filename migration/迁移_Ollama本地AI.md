# 内网迁移：本地 AI 拆分镜（Ollama）

> 2026-09-12 新增。导播台「解析分镜」现在会用本机 Ollama 对话模型拆镜，
> 无网环境下这条链路完全在本机跑，但**模型必须提前带到内网机**。

## 一、内网机需要什么

| 内容 | 源机器路径 | 内网机路径 | 大小 |
|---|---|---|---|
| Ollama 程序 | `%LOCALAPPDATA%\Programs\Ollama` | 同路径 | 约 2.8GB |
| 模型库 | `D:\model`（`OLLAMA_MODELS`） | `D:\model` | 全部 14GB / mini 6.5GB |
| 代码 | `migration\incremental_*.zip` | 解压覆盖 `D:\app\comfyui` | 约 30MB |

程序里已含 `ollama.exe` / `ollama app.exe` / `llama-server.exe` 与 CUDA 运行库，
整目录拷过去即可运行，不需要再下载安装包。当前版本 **0.33.3**。

模型库内容（源机器）：`qwen3:8b`、`qwen3:4b`、`qwen3-vl:8b`、`bge-m3:latest`；
导播台只用到 **qwen3:8b**（拆镜）和 **bge-m3**（素材向量匹配），其余可不带。

## 二、源机器打包

双击 `migration\make_ollama_pack.bat E:\ollama_pack`（目标目录换成移动硬盘），
或只带两个必需模型：

```
migration\make_ollama_pack.bat E:\ollama_pack mini
```

产出 `E:\ollama_pack\Programs\Ollama` 与 `E:\ollama_pack\model`。

## 三、内网机安装

1. 拷贝程序：`E:\ollama_pack\Programs\Ollama` → `%LOCALAPPDATA%\Programs\Ollama`
   （即 `C:\Users\<用户名>\AppData\Local\Programs\Ollama`）
2. 拷贝模型：`E:\ollama_pack\model` → `D:\model`
3. 设环境变量（新开 cmd 生效）：
   ```
   setx OLLAMA_MODELS D:\model
   ```
   或者打开 Ollama 托盘应用 → Settings → Model location 改成 `D:\model`。
4. 启动：双击 `启动全部.bat`（已内置：11434 没在监听时自动拉起 `ollama serve`），
   或手动 `"%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve`。
5. 验证：
   ```
   curl http://127.0.0.1:11434/api/tags     :: 应列出 qwen3:8b
   ollama run qwen3:8b "你好"               :: 能回答即正常
   ```
   导播台里勾选「本地 AI 解析分镜」解析一段脚本，提示「本地 AI 分镜」即打通。

## 四、不带 Ollama 也能用

导播台对缺失是容错的：解析分镜自动回退**格式规则拆分**（分镜表按列拆、纯文字按句拆），
素材自动匹配回退**规则打分**。只是纯文字剧本拆出来的镜头更粗、没有景别/光线描述。

## 五、注意事项

- **版本**：需要 0.9 以上（`think` / `format=json` 参数）；本次实测 0.33.3。旧版会让请求报错并静默回退规则拆分。
- **显存**：拆镜请求带 `keep_alive: 0`，解析完立即卸载模型，不会和 H3 生成抢显存；
  但**别在生成视频的同时解析分镜**（qwen3:8b 约 5GB 显存）。
- **CPU 也能跑**，只是慢很多（无可用 GPU 时 Ollama 自动退回 CPU）。
- **模型目录**：`OLLAMA_MODELS` 指到哪里，模型就放哪里；不要一边用 `D:\model`、一边又往
  `%USERPROFILE%\.ollama\models` 里拷。
- **不需要联网**：Ollama 启动时会尝试查更新/推荐模型，失败只写日志，不影响使用。
- 内网机空间紧张时，可以先全量拷过去再用 `ollama rm qwen3-vl:8b`、`ollama rm qwen3:4b` 回收空间。
