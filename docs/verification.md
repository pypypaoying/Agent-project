# Agent 核心与 Desktop 验收记录

本文件区分宿主合同测试、真实 Docker 执行和真实模型调用。模拟执行器不能证明容器隔离，API 调用成功也不能证明任务成果。

## 自动验收

- 宿主：CSV 自动识别/资源限制、重复表头与原始值、TXT/MD/PDF/BM25/引用、实际双 Agent 工具轨迹、交付项、预算、恢复去重、流式正文与执行进度、产物攻击输入/分页/全量导出/失败回滚、VS Code 正式入口。
- Docker：三类核心任务与独立手算值、列顺序/新类别、280 组和大整数、报错修正、网络/密钥/输入写入限制、无限循环/OOM、取消清理、符号链接/50 MiB 超限、stdout 截断/协议伪造与无模型重放。
- CI：Ubuntu/Windows × Python 3.11/3.12 运行含可选 Web 依赖的宿主测试；独立前端单元测试与构建；Ubuntu 构建锁定镜像并运行真实 Docker 测试、公开问卷演示及 Chromium 闭环。没有向 CI 提供模型密钥。

```powershell
uv run --frozen --extra web pytest -q -m "not docker"
# 先启动 Docker 并构建镜像：
$env:LABWEAVER_REQUIRE_DOCKER="1"
uv run --frozen --extra web pytest -q -m "docker and not live"
```

缺少 Docker 时本地 Docker 测试明确 skip；CI 设置 REQUIRE_DOCKER=1，缺失或构建失败必须 fail。VS Code 入口为 `src/labweaver/app.py`；本地聊天界面由 `labweaver serve --open` / `start-labweaver.cmd` 启动。测试集中在 `tests/` 与 `frontend/`，生成代码/日志/图片只在忽略的运行或应用数据目录中。

## Desktop P0/P1 验收

设计与验收矩阵见 [Desktop](desktop.md)。P0 基线 `982c401` 已完成调研，2026-10-11 复测宿主 342 passed、28 subtests passed；该轮排除 16 项 Docker 测试，没有将之前 D5 的 Docker/在线结果当作新界面的验收。受限 Windows 环境的测试临时目录应放在仓库可写 `.cache/`，避免系统 TEMP 的权限差异。

新增功能分别验收 API/数据库、受控工作进程、前端和真实 Docker 浏览器闭环。以下命令复现公开测试；在线测试单独显式启用，不向 CI 提供模型密钥。

```powershell
uv sync --locked --extra web
uv run --frozen --extra web pytest -q -m "not docker"
cd frontend
npm ci
npm test
npm run build
npx playwright install chromium
npm run test:e2e
cd ..
# 引擎和镜像就绪后，Docker 浏览器验收不得 skip：
$env:LABWEAVER_REQUIRE_DOCKER="1"
uv run --frozen --extra web pytest -q -m "docker and not live"
npm --prefix frontend run test:e2e
# 已配置本机模型接口时，单独执行公开合成数据的在线验收：
$env:LABWEAVER_LIVE="1"
uv run --frozen --extra web pytest -q -m live
```

Playwright 启动隔离的离线测试服务并使用公开合成数据。固定测试应用令牌只服务于本地自动测试，生产启动生成随机令牌。离线模型不是模拟容器：三类计算验收必须登记实际执行、CSV/指标、PNG/SVG，并与独立期望比较。

### P1 实测结果（2026-10-11）

| 验收层 | 本机实测 | 实际核对内容 |
|---|---|---|
| 全量宿主回归 | **429 passed、28 subtests passed**，耗时50.98秒；2 项 Windows 符号链接测试因系统错误1314跳过，23 项 Docker/在线测试排除 | 原核心回归、Web API/存储/权限、幂等、历史重建、保存事务、进程监督与取消、启动错误提示 |
| 真实 Windows Docker | 20 passed | 原有隔离/修正/资源/重放，以及 Web 三类实际分析与停止；没有宿主代码执行降级 |
| 前端 | 17 个 Vitest 测试通过，TypeScript/Vite 生产构建通过 | 授权、界面状态、SSE、输入法与真实接口适配 |
| Chromium 浏览器 | 5 个 Playwright 用例通过 | 上传、概览/SSE/历史/下载，实际格式澄清与继续，三类真实 Docker 计算 |
| 在线模型＋Web API＋Docker | 3 个公开合成案例通过 | 实际主/代码模型调用、真实 Python 执行、全量结果与独立期望 |
| 用户真实 CSV 图形界面验收 | **尚未完成，待用户自行选择文件并反馈** | 不把合成数据或 API 测试冒充用户真实文件验收 |
| 云端新工作流 | 已配置，结果以本轮发布后的 CI 为准 | 四个宿主环境、前端构建及真实 Docker/浏览器；不沿用此前提交的绿色结果 |

三类浏览器和在线案例检查了导出表格的实际数值：交叉表 A(East=6、West=3)、B(East=5、West=1)；日汇总5/4/3、峰谷差值2；清洗4→3行、填空1、去重1、双倍数量4/0/8及编号001/002/003。需要图表的任务取得实际 PNG/SVG 与绘图数据。中文标题、坐标轴、排序和窗口布局已经查看，产物保留在忽略目录。

进程传输测试替身只验证监督和事务合同，不构成隔离验收；Docker 与在线结果分开记录。保存失败或终态事件写入失败会回滚数据库完成状态，合法孤立导出重启后仅恢复登记并标记 interrupted，不自动恢复 Agent 节点。取消清理未确认返回 error。

收尾完整性校验改动后的相关4项真实 Docker 回归另行复核通过（4 passed、8 deselected，31.75秒）；这是重复验证，未累计为24项不同 Docker 测试。

用户验收可在界面选择自己的 CSV，发送一项有独立期望的任务，回复澄清，查看执行代码、图表并下载全量结果；随后重启服务再更改范围重新计算。用户私人输入与产物不进入 Git。执行成功与文件存在不自动证明全部业务口径和结论正确。

## D5 历史验收（2026-10-09）

- 本机最新宿主回归：342 passed，28 subtests passed，16 项 Docker 测试单独运行。包含 Docker context 优先级、固定已校验端点，以及显式阻止客户端代理配置将凭证注入容器。
- 本机 Windows 真实 Docker 最终回归：16 passed，342 项宿主测试在该轮排除；两类验收合计358项通过。包括报错修正、产物拒绝原因、隔离、资源终止、取消清理及重放。
- [此前 CI 验证提交 a8623c1](https://github.com/pypypaoying/LabWeaver/actions/runs/37923944631)：四个宿主环境全部通过；Ubuntu 真实 Docker 15 passed，并完成公开问卷的双 Agent 分析演示。该次镜像 ID 为 `sha256:b64639ca41847b6d0cade42faa643c1ad430018ad78018fa83dd127895b68354`。最新提交的 CI 状态以仓库 Verification 工作流为准。
- 核心数值已在真实容器中核对：交叉表 A(East=6/West=3)、B(East=5/West=1)；日汇总 5/4/3、峰谷差值2；清洗4→3行、去重1、填空1、保留001等编号。280组、大整数、错误修正、隔离、超时/OOM、取消、无模型重放均包含在 Docker 测试中。
- 已查看公开合成 CI 图表及本机真实在线图表的 PNG 与 SVG 渲染：中文标题/轴、分组和日期次序正常。没有将这些产物提交到库。
- 重启并启动 Docker 后，本机 Windows Linux 引擎可运行，`labweaver-python:0.2.0` 镜像已构建。在线执行使用不可变 ID `sha256:63b9b1835fb762a71866337bf30e1f2aa56ac264bc2ca9ddb125fc7c40a69057`，没有降级到宿主执行。

### 真实在线模型＋Windows Docker

三个案例均使用公开合成 CSV、现有在线模型接口和实际 Docker 执行；读取导出的完整 CSV/指标 JSON 与独立期望比较，而不是只检查 completed。

| 案例 | 主/代码模型调用 | 实际代码执行 | 验证结果 |
|---|---:|---:|---|
| 拆分＋交叉统计＋分组柱状图 | 4 / 2 | 1 | A: East=6/West=3；B: East=5/West=1；实际 PNG/SVG 与绘图数据 |
| 日期处理＋日汇总＋趋势图 | 5 / 2 | 1 | 日汇总5/4/3，峰值12/1=5，谷值12/3=3，差值2；完整日期升序表与图 |
| 去重＋填空＋派生数据 | 4 / 2 | 1 | 4→3行，去重1、填空1，双倍4/0/8，编号001/002/003保留；没有绘图 |

三次源 CSV 哈希均未改变，容器均已清理。日期案例的成功脚本已无模型重放，主/代码模型调用均为0，结果表 CSV 字节与原运行一致。

首次在线验收暴露了字段映射使用错误、可选依赖 `tabulate` 缺失、交付项类型不匹配、模型提前结束与输出格式问题。修复通用协议和有限反馈并加入回归后完成上表验收；明确“不绘图”的请求也不再被图表关键词误判。运行记录保留在忽略目录，不发布模型正文、生成代码与产物。三例通过只证明这些案例，不能据此保证任意分析任务的数值正确性。

## 验收解释

完成同时要求合法执行证据、通过校验的产物和所有记录的必需交付项；显式图表必须有真实图片。数值与口径仍用独立预期核对。引用仅证明实际检索片段与来源可追溯，不自动证明全文语义。

源码输入 SHA-256 与结果来源关联；源 CSV 不直接挂入可写容器。报告版本2不迁移旧日志，也不支持进程退出后 checkpoint 恢复。重放只重新执行保存的成功代码。
