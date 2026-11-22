# agent_bridge — VideoMatrix Agent 接入层

让任意 Agent(Claude Desktop / Cursor / Codex / LangChain / 自研)完整驱动 VideoMatrix。

**一套工具定义,三种协议暴露**:MCP stdio · HTTP JSON · CLI。
纯标准库实现(仅需 Python 3.8+),**不需要 `pip install` 任何东西**。

---

## 与 `agent_adapter/` 的关系

仓库里已有 [`agent_adapter/`](../agent_adapter/README.md),这个目录是它的**功能超集**,两者互补:

| | `agent_adapter/`(官方) | `agent_bridge/`(本目录) |
|---|---|---|
| 工具数 | 6 | **33** |
| 结构混剪工作台 | ✗ | **✓ 完整 27 个端点** |
| 传统混剪 | ✓ | ✓ |
| 依赖 | `pip install mcp[cli]==2.0.0` | **零依赖,纯标准库** |
| 协议 | 仅 MCP | **MCP + HTTP + CLI** |
| MCP 注册名 | `videomatrix` | `videomatrix-bridge`(**不冲突,可共存**) |
| 默认行为 | 半自动(只填参数) | 显式调用,全部可控 |

**可以两个都装。** 注册名不同,客户端会同时列出两套工具:

```
videomatrix         → agent_adapter 的 6 个工具(半自动填参数)
videomatrix-bridge  → agent_bridge 的 33 个工具(全功能控制)
```

只想用一个的话推荐 `agent_bridge` —— 它覆盖了 `agent_adapter` 的全部能力(传统混剪),
并额外支持结构混剪工作台。

---

## 它是怎么接上的

```
Agent
 ├─ MCP stdio ──────┐
 ├─ HTTP /call ─────┤
 └─ CLI run ────────┤
                    ▼
           agent_bridge(33 个 vm_* 工具)
                    │  自动发现端口
                    ▼
        http://127.0.0.1:<动态端口>/api
                    │
        VideoMatrix 后端 (FastAPI + 内置 ffmpeg)
                    ▼
             混剪-%m%d-N.mp4
```

桌面程序启动时会拉起本地后端,并把端口写进
`%APPDATA%\VideoMatrix-Structured-2.8.4.1\desktop\desktop-runtime.json`。
agent_bridge 读这个文件拿端口,再用 `/api/health` 校验 instance 一致。

**不改软件任何文件,不注入进程,只走软件自己的 HTTP 接口。**

---

## 安装

### Windows(双击)

双击 `agent_bridge\install.bat`,会依次:

```
[1] 找 Python         → 自动探测 py 或 python
[2] 检查环境          → 确认能导入、33 个工具就绪
[3] 注册到客户端      → 列出检测到的客户端让你选
```

### 命令行

```cmd
cd agent_bridge

python -m vmagent.install check         :: 只检查环境,不写配置
python -m vmagent.install claude        :: 注册到 Claude Desktop
python -m vmagent.install cursor        :: 注册到 Cursor
python -m vmagent.install codex         :: 注册到 Codex CLI
python -m vmagent.install --dry-run     :: 演练,不落盘
python -m vmagent.install print         :: 只打印配置片段
```

### 装完验证

```cmd
python -m vmagent.install doctor
```

期望看到:

```
  [OK  ] Python 版本       3.12.x (需要 >= 3.8)
  [OK  ] 工具注册            33 个
  [OK  ] VideoMatrix 后端   http://127.0.0.1:52341 (v2.8.4.1)
  [OK  ] 结构混剪能力       可用
```

然后对 Agent 说「调用 vm_discover」,返回 `flavor: structured` 即成功。

---

## 33 个工具

### 连接(4)
| 工具 | 作用 |
|---|---|
| `vm_discover` | 连接后端,返回地址/版本/是否支持结构混剪/工具清单 |
| `vm_health` | 轻量存活检查 |
| `vm_openapi` | 导出后端 OpenAPI(发现工具集之外的新端点) |
| `vm_api_call` | 通用逃生舱,直接打任意端点 |

### 结构混剪 · 读(5)
| 工具 | 作用 |
|---|---|
| `vm_project_list` | 列出全部项目 |
| `vm_project_get` | 读单个项目完整状态 |
| `vm_project_summary` | **Agent 友好摘要**:各类别片段数、已核查/待核查 |
| `vm_source_list` | 列出素材(id / 路径 / 时长) |
| `vm_clip_list` | 列出片段,可按类别或核查状态过滤 |

### 结构混剪 · 写(11)
| 工具 | 作用 |
|---|---|
| `vm_project_create` | 新建项目 |
| `vm_product_create` | 新建产品(素材隔离单位) |
| `vm_folder_create` | 新建素材文件夹 |
| `vm_category_labels` | 读写片段类别名(A-G / T) |
| `vm_import_sources` | 导入素材(整目录递归 / 逐个) |
| `vm_clip_add` | **打片段**:类别 + 入点 + 出点 + 核查标记 |
| `vm_clip_update` | 改时间码 / 备注 / 核查状态 |
| `vm_clip_delete` | 删片段 [destructive] |
| `vm_formula_save` | 保存组合公式 |
| `vm_plan_create` | **生成组合清单**(拿 plan_id) |
| `vm_test_plan_create` | 开头测试 / 镜头测试清单 |

### 导出(6)
| 工具 | 作用 |
|---|---|
| `vm_render` | **按清单导出成片** |
| `vm_render_status` | 查导出进度 |
| `vm_render_stop` | 停单个 [destructive] |
| `vm_render_stop_all` | 停全部 [destructive] |
| `vm_simple_render` | 简易渲染 |
| `vm_simple_render_status` | 查简易渲染状态 |

### 传统混剪(7)

`vm_legacy_preflight` · `vm_legacy_render` · `vm_task_list` · `vm_task_status` · `vm_task_logs` · `vm_task_stop` [destructive] · `vm_task_resume`

---

## 典型 Agent 流程

```
1. vm_discover              → 确认软件在跑
2. vm_project_list          → 选项目(或 vm_project_create 新建)
3. vm_import_sources        → 导入素材,拿到 source_id 列表
4. vm_clip_add × N          → 打片段:A=开头, B-G=正文, T=待测开头
5. vm_project_summary       → 确认各类别数量够不够
6. vm_plan_create           → 生成组合清单,拿 plan_id
7. vm_render                → 导出,拿 render_id
8. vm_render_status         → 轮询直到 completed
```

**关键门禁**:`vm_clip_add` 默认 `cut_status=pending`,这种片段**不会**进入组合。
要让它参与,必须 `approved: true`(等价于人工核查过切点)。这是软件本身的门禁。

想看完整演示:

```cmd
python tests/demo_agent_flow.py
```

---

## 不用 MCP 也能接(HTTP)

```cmd
start-http.bat 8770
```

```python
import requests
BASE = 'http://127.0.0.1:8770'

# 给 LLM 做 function calling
tools = requests.get(BASE + '/openai.json').json()['tools']

# 调用工具
r = requests.post(BASE + '/call', json={
    'name': 'vm_discover',
    'arguments': {},
})
print(r.json())
```

设 `VMAGENT_TOKEN` 可开启 Bearer 鉴权。

---

## 命令行直接用

```cmd
python -m vmagent doctor
python -m vmagent run vm_project_list
python -m vmagent run vm_project_summary project_id=p1
python -m vmagent call vm_clip_add "{\"project_id\":\"p1\",\"source_id\":\"s1\",\"category\":\"A\",\"start\":0,\"end\":3.2}"
```

---

## 设计要点

**单一事实源** —— 33 个工具只在 `vmagent/tools.py` 定义一次。
MCP、HTTP、CLI 三个协议层都从 `REGISTRY` 自动生成,新增工具后三种入口同时可用。

**三协议响应同构** —— 工具直接透传后端响应并补 `ok` 字段。
同一个工具在 CLI、HTTP、MCP 下返回完全相同的 JSON。

**端口动态发现** —— 不写死 8765。实际端口由 OS 分配,四级回退:
1. `VMAGENT_BASE` / `VIDEOMATRIX_API_URL` 环境变量
2. `desktop-runtime.json`(APPDATA / LOCALAPPDATA / 仓库根)
3. instance 一致性校验(防连到已退出的旧实例)
4. 兜底 8765

**错误可读** —— FastAPI 的 422 校验错误压成一行。

**本地前置校验** —— 组合公式在本地就查(A 开头、只用 A-F、≤16 位),省一次往返。

---

## 自测

```cmd
python tests/run_conformance.py
```

起模拟后端,跑 78 项断言:
- 工具注册表完整性(schema 合法、命名规范、破坏性标注)
- CLI 10 项 / HTTP 10 项 / MCP 16 项(NDJSON + Content-Length 双分帧)
- 端到端业务流 26 项(建项目 → 素材 → 片段 → 清单 → 渲染 → 停止)
- 传统混剪流 7 项 / 错误路径 5 项

---

## 目录

```
agent_bridge/
├── README.md                  本文件
├── CHANGELOG.md
├── install.bat / .sh          一键安装
├── start-mcp.sh / .bat        启动 MCP 服务
├── start-http.sh / .bat       启动 HTTP 服务
├── vmagent/
│   ├── core.py                端口发现 + HTTP 传输
│   ├── tools.py               33 个工具注册表(单一事实源)
│   ├── mcp_server.py          MCP stdio(零依赖 JSON-RPC)
│   ├── http_server.py         HTTP/JSON 服务
│   ├── cli.py                 命令行入口
│   └── install.py             注册到 Claude/Cursor/Codex
└── tests/
    ├── mock_videomatrix.py    模拟后端
    ├── run_conformance.py     78 项一致性自测
    └── demo_agent_flow.py     Agent 视角全流程演示
```

---

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `VMAGENT_BASE` | 显式指定后端地址,跳过自动发现 | 自动 |
| `VIDEOMATRIX_API_URL` | 同上(兼容 `agent_adapter` 命名) | — |
| `VMAGENT_TIMEOUT` | 请求超时秒数 | 60 |
| `VMAGENT_TOKEN` | HTTP 服务 Bearer token | 空(不鉴权) |
| `VMAGENT_CACHE_TTL` | 后端连接缓存秒数 | 5 |
| `VMAGENT_CONFIG_ROOT` | 重定向客户端配置路径(测试用) | 系统默认 |

---

## 安全边界

| 风险 | 现状 | 建议 |
|---|---|---|
| 后端无鉴权 | VideoMatrix 本身不校验请求 | HTTP 服务默认只绑 127.0.0.1;可设 `VMAGENT_TOKEN` |
| `vm_api_call` 可打任意端点 | 逃生舱设计 | 给不可信 Agent 用时移除或加白名单 |
| 破坏性操作 | 4 个工具标了 `destructive: true` | 支持该标记的客户端会自动确认 |
| 任意路径访问 | Agent 可指定本机任意素材路径 | 在 Agent 侧加路径白名单 |

---

## 已知限制

1. **协议层已验证,Windows 真机渲染未实测**。模拟后端按 2.8.4.1 路由契约实现,
   78 项覆盖协议正确性,但真实素材渲染、ffmpeg 参数、GPU 探测需在 Windows 上确认。
2. **结构化响应字段来自逆向推断**。`structured_service.py` 未逐函数反编译,
   字段名以 OpenAPI 与前端调用为准。遇到不符先用 `vm_openapi` 拉真实 schema。
3. **结构混剪是 2.8.4.1 定制版特性**。开源仓库的 backend 没有 `/api/structured`,
   用开源版构建时只有 `vm_legacy_*` 和 `vm_task_*` 可用(工具会自动提示)。
