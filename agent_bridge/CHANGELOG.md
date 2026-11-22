# CHANGELOG

## v1.0.0 — 2026-11-22

首个版本。把 VideoMatrix 结构混剪 2.8.4.1 暴露成 Agent 可直连的三协议接入层。

### 新增

- **33 个工具**,分 5 组:connection(4) / structured-read(5) / structured-write(11) / render(6) / legacy(7)
- **MCP stdio 服务器**:零依赖实现的 JSON-RPC 2.0,支持 NDJSON 与 LSP Content-Length 双分帧,
  兼容 Claude Desktop / Cursor / Codex / 任意 MCP 客户端
- **HTTP/JSON 服务**:`GET /tools` / `POST /call` / `POST /tools/<name>` / `GET /openai.json` / `GET /openapi.json`,
  可选 Bearer token
- **CLI**:`discover` / `doctor` / `tools` / `schema` / `call` / `run` / `serve-mcp` / `serve-http`,
  子命令从工具注册表自动生成
- **一键安装器**:`python3 -m vmagent.install claude|cursor|codex|print|doctor`,
  写入前自动备份原配置
- **四级端口发现**:环境变量 → desktop-runtime.json(多候选) → instance 校验 → 8765 兜底
- **78 项一致性自测**:`tests/run_conformance.py`,含模拟后端 `tests/mock_videomatrix.py`

### 设计决策

- **单一事实源**:工具只在 `tools.py` 定义一次,三协议自动派生。新增工具不必改协议层。
- **三协议响应同构**:工具直接透传后端响应 + `ok` 字段,CLI/HTTP/MCP 返回完全一致。
- **零第三方依赖**:纯标准库,目标 Windows 机器无需 pip install。
- **本地前置校验**:组合公式在本地就校验(A 开头 / 只用 A-F / ≤16 位),省一次网络往返。
- **破坏性操作显式标注**:`destructive: true` 写进 schema,支持该标记的客户端自动要求确认。

### 验证

- 78/78 断言通过(工具注册表 / CLI / HTTP / MCP 双分帧 / 业务流 / 传统流 / 错误路径)
- 已验证错误路径:后端离线、未知工具、缺必填参数、未知参数、后端 404、公式非法

### 已知限制

- 只在 macOS 上用模拟后端验证协议层;Windows 真机渲染未实测
- `structured_service` 未逐函数反编译,响应字段以 OpenAPI 与前端调用为准
- VideoMatrix 后端本身无鉴权,vmagent 的隔离完全依赖回环绑定