# 使用与检验

## Windows 本地聊天界面

浏览器应用需要先完成下面的 Docker 配置，模型可通过页面设置配置。首次安装额外需要 Node.js 构建前端；日常运行只使用 Python 服务和构建后的页面，不启动 Vite，也不自动安装 Docker 或分析依赖。

```powershell
uv sync --locked --extra web
cd frontend
npm ci
npm run build
cd ..
```

双击 `start-labweaver.cmd`。也可从仓库根目录执行：

```powershell
uv run --frozen --no-sync --extra web labweaver serve --open
```

启动会检查模型配置、数据目录和本地 Docker/镜像。页面的应用访问令牌由启动链接传入后从地址栏移除；它不是模型 API Key。无需把密钥粘贴到聊天框。服务仅绑定 `127.0.0.1`，不要配置公共监听地址。

1. 新建对话，选择或拖入一份 CSV，可加 TXT/Markdown/文字型 PDF。已有无附件、消息和任务的空草稿会复用，反复点击不会堆积空对话。上传后查看预览；图片、Excel、JSON、DOCX 尚不支持分析。
2. 输入任务并发送。首次发送冻结 CSV 和资料；需要更换输入时新建对话。输入法确认不会发送，Shift+Enter 换行。
3. 等待时在聊天区查看“执行过程”：工具调用、Python 执行与修正、检索、核验和保存均来自实际事件。展开右侧执行面板可查看完整计划、代码、错误及资料来源；出现澄清问题时在当前运行提交回复。单个步骤完成不等于全部任务完成。
4. 核验完成后查看表格/图表，下载完整 CSV、PNG/SVG、代码和报告。表格显示的是分页预览，完整结果没有 100 行限制。
5. 停止后等待取消与容器清理确认；关闭或刷新浏览器不会自动重跑或取消。全局只有一个活动任务，等待回复时也占用该位置。
6. 服务重启后重新打开历史，继续提交新任务。旧的未完成运行会显示 interrupted，不能回复旧问题以恢复节点；可按原任务重新执行。

基础历史、导入附件和生成成果位于应用数据目录，与 CLI 的 `runs/` 分开。旧对话的新运行重建有限历史背景，再从原始 CSV 重新计算。Windows 资源管理器复制的文件未必能直接粘贴到浏览器；失败时使用拖拽或文件选择器，粘贴路径不会读取磁盘。

2026-10-12 更新增大了常用文字和图标。已运行的服务需重启并刷新页面才能使用更新后的界面和路径修复；过去失败的任务不会自动重跑，可在历史中按原任务重新执行。

离线公开演示可使用 `labweaver serve --offline --open`，分析仍需要真实 Docker；确定性模型只支持有限示例。搜索、重命名/删除管理、工作空间、独立知识库与桌面安装包留待 P2–P4。详细架构见 [Desktop](desktop.md)。

## 模型选择与服务商配置

输入框右下角、发送按钮左侧是模型选择器，按服务商分组展示。新用户默认 DeepSeek；点击未配置的模型会进入配置页。每次任务和历史卡片记录实际选择的服务商与模型。切换模型会在下一项任务重建 Agent 运行时，并把已核验的最近历史作为背景；CSV 和资料绑定保持当前对话的输入。运行中或等待澄清时不能切换，回复继续使用原任务模型。

1. 点击左下角 **设置**，选择 DeepSeek，或点击 **添加兼容供应商**。
2. 填写服务商名称、API 根地址 **Base URL**、**API Key**；模型列表每行一个实际模型 ID，默认模型必须包含在列表里。地址应来自服务商 API 文档，不能填写聊天网页、密钥、查询参数或用户名。首版支持 OpenAI **Chat Completions** 兼容接口，不支持原生 Anthropic 协议。
3. 点击 **保存配置**。Key 留空保留已有凭据；勾选“保存时清除现有密钥”才会移除。返回页面后不会回显密钥。
4. 可点击 **获取模型列表**或 **测试连接**。两者只向保存的接口发送专用请求，不发送 CSV、任务或资料。连接成功和工具调用验证分别展示，不代表已经证明任意分析任务可完成。接口不支持列表或返回超过 50 个模型时，手动维护需要的模型即可。
5. 返回聊天，在右下角选择模型后发送任务。**设为应用默认**影响默认选择；重新打开历史仍显示该对话上次使用的模型。

DeepSeek 预置 Base URL 为 `https://api.deepseek.com`，模型为 `deepseek-flash`、`deepseek-v4-pro`，默认前者，按[官方 API 文档](https://api-docs.deepseek.com/api/create-chat-completion/)核对。当前 ChatOpenAI 适配器不会回传 DeepSeek 的 `reasoning_content`，因此仅对官方接口的这些模型禁用 thinking，确保连续工具调用兼容；其他兼容服务商不注入此参数。[DeepSeek 工具调用要求](https://api-docs.deepseek.com/guides/thinking_mode/)

Windows 新 Key 加密保存在本机应用数据目录，只能由当前 Windows 用户解密，不进入 SQLite、聊天、执行记录或容器。非 Windows 本轮仅在后端内存保存新 Key，重启服务后需重新填写；不会退回明文文件。设置页会说明实际保存方式。浏览器只在输入和保存请求中短暂持有新 Key，不保存到 localStorage/sessionStorage，保存后清空字段。

已有后端配置继续兼容：官方 DeepSeek 配置作为预置服务商的环境引用；其他配置显示为“现有本地配置”，公开地址和模型只读。修改环境引用的接口地址后不会把原环境 Key 发往新地址。需要修改只读配置时可以新增服务商；CLI 和 VS Code 入口继续使用原 `.env`/TOML 配置，不读取桌面服务商设置。离线演示锁定选择器并显示“离线演示”，不假装调用在线模型。

## Windows / VS Code

首次安装 Docker Desktop 与 WSL2、启用 Virtual Machine Platform/硬件虚拟化，按系统要求重启。确认 Docker Desktop Linux 引擎已运行，再在 VS Code 的 Terminal → Run Task 选择 **LabWeaver: Build analysis sandbox**。安装宿主依赖 `uv sync --locked` 后选择项目 `.venv` 解释器。

模型配置使用忽略的 `.env` 或 `labweaver.local.toml` 引用已有配置，例如：

```toml
[intake]
env_file = "D:/private/existing.env"
mode = "live"
analysis_max_tokens = 4096
execution_image = "labweaver-python:0.2.0"
execution_timeout = 60
```

模型/API 超时仍由 `LLM_TIMEOUT` 控制，容器执行超时单独配置。主 Agent 与代码 Agent 默认同一接口，代码 Agent 使用单独的输出 token 设置。镜像在构建时安装锁定依赖；分析过程中不联网安装。

打开 `src/labweaver/app.py`，Run Python File 或 F5。日常只输入 CSV 路径、可选资料路径（分号分隔）、任务；不要改程序里的 prompt 或资料路径。CSV 默认自动识别编码与分隔符，歧义时展示候选预览供选择。通过 TOML/CLI 仍可高级覆盖。

Agent 会显示短计划、真实代码执行与修正次数、流式回答。收到关键问题时输入确认，随后继续同一任务；完成后可修改范围、图型、标题或继续新任务。退出输入 `退出`。图表和全量结果保存在报告列出的路径，终端只预览部分行。

## 三类任务验收

可以将 `tests/fixtures/` 的合成 CSV 用于在线本机检验，也可以指定自己的数据。检验是否产生正确成果，不能只看 completed。

| CSV | 任务 | 独立期望 |
|---|---|---|
| composite.csv | 拆分组合编码，按类别/区域交叉统计数量，绘制分组柱状图 | A: East=6/West=3；B: East=5/West=1；完整交叉 CSV＋PNG/SVG |
| dates.csv | 解析日期，按日汇总数量，计算峰谷差值并绘制趋势图 | 12/1=5、12/2=4、12/3=3；差值=2；日期升序 |
| cleaning.csv | 删除完全重复行，再将数量空白填0，派生双倍数量并导出，报告前后变化 | 4→3行；去重1；填充1；双倍为4/0/8；编号001/002/003保留 |

打开 Markdown 与 PNG 检查中文、轴和图型；检查 CSV/指标 JSON 的实际值；打开每次 `.py` 与运行 JSON，核对 execute_python 工具 ID、退出码、修正错误和真实 image_id。源文件 SHA-256 应不变。

## 编程接口和重放

`create_session(csv_path, model, *, analysis_model=None, execution_config=None, material_paths=None, encoding="auto", delimiter="auto")` 返回当前进程的 `AgentSession`。`invoke(task)`、`resume(reply)` 返回报告，`cancel()` 终止容器并取消会话，`save(output_dir)` 导出最新报告并返回包含路径的字典。`run_intake()` 是单次包装，无法独自完成需回复的澄清。

```python
from labweaver.runtime.replay import replay_analysis
report = replay_analysis("runs/会话/任务/记录.json", "原始CSV路径")
print(report["status"], report["result_csv_paths"])
```

重放不调用模型，核对输入和代码 SHA-256，并使用记录中的不可变 Docker 镜像 ID；原镜像被删除时明确失败。它重新计算成功脚本，并非恢复已关闭的会话。表格/指标应与原运行一致，图像字节可因生成元数据不同而不同。

`--offline` 适合公开演示和工具流程验证，代码模型只认识有限的合成任务，不用于评价通用理解。离线统计仍真实执行容器；宿主合同测试的注入执行器只用于测试，不构成第二套生产路径。

## 常见问题

- `docker_unavailable/docker_not_ready`：安装并启动 Linux Docker 引擎。Windows 虚拟化未生效时完成重启，不能通过改用宿主 Python绕过。
- `sandbox_image_missing`：运行 VS Code 的镜像构建任务。生产执行不会自动拉取或构建镜像。
- `python_failed`：代码 Agent 根据真实 stderr 自动有限修正；连续失败则保留代码/错误，返回未完成。
- Windows 打包宿主中出现 `/input/analysis.py` 不存在：曾由 AppData/TEMP 路径重定向使 Docker 挂到空目录引起。本轮已在临时目录创建后解析真实路径；更新后重启服务再重新执行，不需要放开容器输入写权限。
- `save_failed`：成果保存或数据库登记未完成，真实代码执行成功也不能标记整个任务完成。本轮同时修复了 Windows 路径重定向造成的存储根目录不一致；其他磁盘、权限或登记错误仍需查看实际失败记录。
- `missing_deliverables`：任务交付项尚缺真实产物，不能用计划文字代替结果。
- `execution_timeout/memory_limit/output_limit/artifact_rejected`：资源或产物协议失败，查看保存的执行记录；不静默丢行。
- 关闭程序后不能 resume；仅可通过成功代码重放或创建新会话。
