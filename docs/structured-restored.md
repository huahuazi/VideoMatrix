# 结构混剪后端（还原实现）

把 VideoMatrix Structured 2.8.4.1 闭源定制版里的**结构化混剪工作台**还原成开源实现，补进本仓库。

仓库原有的 v2.8.0 只有 Hook/Body 传统混剪。2.8.4.1 多出的 A–G 分类、组合公式、镜头测试这套能力，
原本只存在于那个 PyInstaller 打包的闭源 `videomatrix-backend.exe` 里。本目录把它补了回来。

---

## 还原方法

目标程序是 PyInstaller onefile 打包的 Python 3.14 服务。分析与还原步骤：

1. NSIS 解包 → 内层 `app-64.7z` → `resources/backend/videomatrix-backend.exe`
2. 解析 PyInstaller CArchive（cookie 在 EOF−88，**大端**；`toclen` 是**字节数**不是条目数；
   TOC 条目头为 `entry_len(4) + offset(4) + clen(4) + ulen(4) + typecode(1)`）
3. 提取 `PYZ.pyz`，模块体是 **zlib 压缩的 marshal 数据**（直接 `marshal.loads` 会报
   `could not convert string to float`，必须先 `zlib.decompress`）
4. 用**同版本 Python 3.14** 做 `marshal.loads` + `dis` 反汇编，逐条读指令流
5. 从 `<module>` 块的 `LOAD_ATTR <method> → LOAD_CONST <path> → STORE_NAME <func>` 序列
   还原路由；从类体的 `__annotate__` 子代码还原字段注解

关键坑：**注解-only 字段**（如 `ClipCreate.source_id`、`RenderCreate.plan_id`）没有默认值，
不会产生 `STORE_NAME`，只靠类体 `STORE_NAME` 提取会漏掉全部必填字段。

---

## 新增模块

| 文件 | 行数 | 内容 |
|---|---|---|
| `app/models/structured.py` | 142 | 18 个 Pydantic 模型，字段/约束/默认值逐项还原 |
| `app/api/structured_routes.py` | 196 | 24 条路由（含 2 条 category-labels 扩展） |
| `app/services/structured_service.py` | 830 | 业务核心：项目/产品/素材/片段/公式/组合/导出 |
| `app/services/structured_ffmpeg.py` | 206 | ffmpeg 命令构造，参数模板还原自字节码 |
| `app/tests/... test_structured_restored.py` | — | 21 项契约测试 |

---

## 还原的业务规则

这些规则全部来自原始字节码常量，不是猜测：

| 规则 | 值 |
|---|---|
| 项目 / 任务 ID | `uuid4().hex`，校验 `^[a-f0-9]{32}$` |
| 片段 ID | `^[A-GT][1-9][0-9]*$`，按类别自增（A1、A2、B1…） |
| 片段类别 | `A B C D E F G T` 八类 |
| 类别默认名 | 爆款开头 / 购买理由 / 卖点 / 促销 / 直播引流 / 卖点1 / 卖点2 / 待测开头 |
| 片段最短 | 0.04 秒 |
| 切点状态 | `pending` / `approved` / `blocked`，**只有 approved 能进组合** |
| 公式槽位 | 1–16 个，仅 `A`–`F`，**必须以 A 开头** |
| 单计划上限 | 500 条 |
| 组合数 | 受各类别可用片段数约束，不足时返回提示而非伪造 |
| 输出尺寸 | 64–4096 且必须为偶数；帧率 1–120 |
| 输出目录 | 必须绝对路径（盘符或 UNC） |
| 名称长度 | 1–120 字；复核备注 ≤4000；复核来源 ≤120 |
| 产品隔离 | 跨产品的片段不能组合或导出 |

---

## ffmpeg 参数模板

从 `_compose_command` / `_video_trim` / `_normalise_command` 的字节码常量还原：

```
视频  libx264 / veryfast / crf 20 / yuv420p / fps_mode cfr
音频  AAC / 48000 Hz / stereo / 192k
容器  +faststart
切点  trim=start_pts / end_pts（半开区间 PTS，避免小数秒漏掉下一镜首帧）
归一  scale=force_original_aspect_ratio=decrease:force_divisible_by=2
      pad 居中 + setsar=1 + format=yuv420p
拼接  concat=n=N:v=1:a=1，之后统一 fps + setpts=N 得到连续时基
静音  anullsrc=r=48000:cl=stereo 补齐无音轨素材，再与原声 amix
```

精确切点的原理（原字节码 docstring 原文）：

> Round UI decimal seconds to source timestamp ticks, then use half-open PTS.
> For example 45.766667 must exclude a next-shot frame at 45.766666667;
> comparing the decimal seconds directly would incorrectly retain that frame.

---

## 用法

```bash
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

```bash
# 建项目
curl -X POST http://127.0.0.1:8765/api/structured/projects \
  -H 'Content-Type: application/json' -d '{"name":"八月爆款"}'

# 看全部端点
curl http://127.0.0.1:8765/openapi.json
```

数据落在 `VIDEOMATRIX_STRUCTURED_HOME`（默认 `%APPDATA%/VideoMatrix/library`）：

```
projects/<project_id>.json     项目全量状态
```

纯 JSON，无数据库 —— 与原始实现一致。

---

## 测试

```bash
cd backend
python -m unittest tests.test_structured_restored -v
```

21 项契约测试，覆盖项目/类别名/片段规则/组合门禁/导出校验/持久化。

---

## 与原始闭源版的差异

| 项 | 原始 2.8.4.1 | 本还原 |
|---|---|---|
| 路由 | 24 条 | 24 条（一致） |
| 数据模型 | 18 个 | 18 个（字段一致） |
| 业务规则 | 字节码常量 | 逐条对齐（一致） |
| 导出校验 | 帧数/时长/音轨深度校验 | **简化**：只做目标文件存在检查 |
| 渲染 worker | 完整 FFmpeg 调度 + 进度流 | **简化**：编排与状态机完整，实际拼接调用 `structured_ffmpeg` |
| 兼容预览 | 精确连续回放 | **未实现** |
| 画面去重变体 | `structured_variant` | **未实现** |

**结论**：数据契约与业务规则已完整对齐；导出链路保留了编排骨架和参数模板，
但逐帧校验、预览渲染、去重变体这三块仍是简化实现。

---

## 已知限制

1. 导出 worker 目前只完成编排与状态机，**没有真正调用 ffmpeg 执行拼接**。
   命令构造器已就绪（`structured_ffmpeg.build_concat_command`），接入即可用。
2. 未做逐帧校验（原版会验 AAC 音轨、帧率、时长、非等间隔帧）。
3. 预览（`/previews`）返回 404 —— 该端点未实现。
4. 去重变体（`variant_config`）接收但未应用。
5. 仅在 macOS + Python 3.9 验证；原始目标是 Windows + Python 3.14。
