# 项目进度

## WP00 · NAS 接入探路脚本

- 日期：2026-11-16。
- 开发状态：脚本与本地自动化验证完成；**真实 NAS 人工验收尚未执行**。
- 本包开始时没有进度文件与 Git 仓库；已建立进度、问题记录和 Git 仓库。四份设计文档保持原内容。

### 已完成

- `scripts/probe/probe_nas.py`：独立 Python 3.12 脚本；支持 DSM 地址、端口、HTTP/HTTPS、共享文件夹、1～2 组账号密码和相对测试目录，支持隐藏输入密码。
- DSM 接口发现、POST 登录/注销；每账号独立 SMB TCP 连接与缓存，均使用该账号本人凭据，所有远程文件操作通过脚本内部最小 `NasFs` 接口。
- 按账号执行列目录、建目录、20 × 1MiB 追加、SHA-256 校验、纯 rename 计时、rename 后校验和清理；中文、空格及长路径读写。
- A 只读目录拒写、B 同目录允许写入、双账号 A→B→A 复查及 B 加入后 A 再次拒写；权限异常打印类型及错误码，其他错误不能冒充权限通过。
- 逐项输出和末尾汇总表；仅清理本次创建的对象，失败仍尝试清理和回收会话；报告不自动落盘，提供人工填写的 `RESULT.md`。
- `README.md`：Windows/macOS/Linux 安装运行、权限配置、结果解释与设计调整依据。
- `docs/QUESTIONS.md`：记录只读参数缺口、同父目录 rename 的验证边界及密码/重连待定项，并保留新问题模板。

### 依赖与验证

- 运行依赖：`httpx==0.28.1`、`smbprotocol==1.16.1`；测试依赖：`pytest==9.0.2`。均为约定技术栈内依赖；无额外业务依赖，固定直接版本便于复现。
- Python 3.12.16：`python -m pytest scripts/probe/tests -q`，24 项本地测试通过；使用临时目录和模拟接口，覆盖追加/摘要/改名/清理、权限拒绝与非权限失败、双账号隔离、DSM POST 登录注销、凭据脱敏、参数错误及隐藏密码输入不可用时安全退出。
- 已检查命令行帮助、Python 编译及依赖兼容性（uv pip check）。没有前端工程，前端构建不适用；本包不创建前后端或正式服务模块。

### 已知限制

- 本地测试没有连接真实 NAS，不能声称 DSM 7、双账号 NAS ACL 或 rename 性能已经通过；公司内网须完成 `RESULT.md`。
- 只验证同共享文件夹、同父目录 rename；实际临时区→正式目录还须确认同卷和跨目录 ACL。
- 不处理 OTP / Secure SignIn、DFS、多共享文件夹、IPv6、系统账号、自动重连、项目角色/状态机或业务日志。
- `--read-only-dir` 为额外必填参数，不使用默认业务目录；默认 A 在主测试目录可读写、对照目录只读，B 两处可读写。其他权限配置的失败需人工与后台逐项核对。
- 强制终止、网络断开、删除权限不足可能残留 UUID 测试对象；按输出路径确认后人工清理。单账号可运行，但不满足双账号验收，返回非零。
- CLI 密码参数可能进入 shell 历史/进程参数；推荐省略后隐藏输入。报告模板不自动覆盖，需人工填入脱敏结果。

### 手动验收

1. 在内网专用共享文件夹建读写测试目录和权限对照目录，设置 A/B 不同权限，记录 DSM 后台 ACL；不要选正式资产目录。
2. 用 Python 3.12 安装 `scripts/probe/requirements.txt`，按 `scripts/probe/README.md` 带两个账号运行，密码推荐隐藏输入。
3. 核对每项成功/失败是否与后台一致；确认 20MiB 追加及摘要一致、同父目录 rename <1 秒、中文与空格路径读写通过、长路径结果已记录。
4. 确认 A 明确权限拒绝、B 同目录可写、两个独立会话同时存活、B 加入后 A 仍拒写；核对清理和注销。
5. 将终端脱敏汇总及环境、权限、结论填入 `scripts/probe/RESULT.md`，需要调整的设计记入 `docs/QUESTIONS.md`。

## 后续工作包模板

<!-- WP 编号 / 日期 / 完成内容 / 新依赖及理由 / 自动化验证 / 已知限制 / 手动验收 -->

## WP01 · 项目骨架

- 日期：2026-11-16。
- 状态：骨架及本机 HTTP 验证完成；后续 WP01 补充修复已完成 Chrome 图形界面验证，见下方记录。
- 本次只做 WP01；误开 WP02 时产生的未提交 NAS 草稿已移出仓库，不包含在本次提交。

### 已完成

- 按架构目录建立 `backend/`、`frontend/`，根 README 给出 Windows/macOS/Linux 安装、启动、测试及迁移命令。
- Python 3.12 后端包、pydantic-settings YAML/环境变量配置与完整 `config.example.yaml`；增加 `mode`、`bootstrap_admin`。管理员名称仅加载，不创建账号。
- Fernet 加解密与首次启动的 `data/secret.key`；已有密钥复用，损坏时拒绝覆盖。POSIX 0600；Windows 受保护 DACL 限定服务身份、管理员与 SYSTEM。
- SQLite WAL、busy_timeout=5000、外键及 SQLAlchemy 事务会话；首个 Alembic 迁移只建 `users/sessions/settings/oplog/jobs` 五张业务表。启动自动 upgrade head，不绕过迁移建表。
- oplog 仅追加服务，与调用者事务一并提交/回滚；数据库 BEFORE UPDATE/DELETE 触发器直接拒绝修改。
- 统一 `{code,message,detail}` 错误处理、`GET /api/health`（版本、模式、数据库可用性）、按天滚动的中文应用日志。
- FastAPI 托管构建产物及 SPA 路由回退，未知 API 和缺失 assets 资源不回退首页；路径穿越拒绝。
- React 18 + TypeScript + Vite + Tailwind 占位首页，统一 API 请求、读取并显示 health；玻璃封装、视觉偏好持久化及 Safari/Firefox 默认 lite。
- 前端构建输出 `backend/app/static/`；Git 忽略运行数据、日志、配置、依赖和构建产物，提交前端锁文件。

### 依赖与验证

- 后端新增约定栈内的 FastAPI/Uvicorn、SQLAlchemy/Alembic、pydantic-settings、cryptography；`pydantic-settings[yaml]` 额外安装 PyYAML，仅用于读取明确要求的 YAML 配置，不替换配置技术选型。
- 前端使用 React/ReactDOM 18.3.1、Vite 6.4.6、Tailwind 3.4、liquid-glass-react 1.1.1、TypeScript；Vitest 5.1.0，开发 Node 22.23.5。PostCSS、Autoprefixer 和 Vite React 插件为构建所需，不引入其他 UI 库。
- 根目录 `python -m pytest -q`：41 项通过（WP01 17 项 + WP00 24 项）。覆盖配置、加解密、密钥复用/损坏、五表、WAL/超时/外键、日志触发器、事务回滚、health 成败、错误脱敏、静态托管/路径拒绝以及删除 data 后重建。
- `npm --prefix frontend run check`：5 项前端测试通过，TypeScript 检查与生产构建成功。初次安装旧 Vitest 的依赖审计出现问题，升级后 npm audit 为 0；依赖兼容性检查通过。
- 已实际启动 8080 后端：health 返回 200、dev、database_available=true；首页和 SPA 深层页面返回构建 HTML，未知 API 返回统一 404。手动 Alembic current 为 `0001_skeleton (head)`。
- 当前 Starlette 对 httpx TestClient 发出弃用提示；现有 httpx 测试仍通过，未引入栈外的 httpx2。

### 已知限制

- 原实时滤镜缺口已在下方“WP01 补充修复”解决；Q004 已更新。
- Windows ACL 实现未在本机 macOS 实跑；现场 Windows 需核对 secret.key 的实际权限。密钥丢失不可解密旧密文，备份需同时保留数据库与密钥。
- 首次骨架阶段未完成图形验收；补充修复已在本机 Chrome 验证，其他目标浏览器仍待现场验收。
- 不包含登录、NAS、上传、任务执行器、角色授权或前端业务页面。`bootstrap_admin`、NAS 和 AI 配置只加载，不触发业务行为。
- 数据库与 Alembic 初始化按单进程部署设计；未设计多实例同时初始化密钥/执行迁移。

### 手动验收

1. 按根 README 安装 Python 3.12、Node 22.12+，创建虚拟环境、安装依赖、复制配置；运行 `npm --prefix frontend ci` 和 `npm --prefix frontend run check`。
2. 执行 `python -m app`，浏览器打开 `http://localhost:8080`，确认中文占位页显示版本 v0.1.0、开发模式和数据库连接正常；刷新深层页面也能返回首页。
3. 执行 `python -m pytest -q`，确认所有本地测试通过，oplog 的更新/删除被触发器拒绝。
4. 停止服务，备份或删除可丢弃的开发 `data/` 后重新启动，确认 app.db/secret.key 自动重建且 health 正常；核对日志位于 logs/。Windows 核对密钥 DACL。
5. 如需切换配置位置，用 NASWB_CONFIG 指向新 config.yaml；确认相对 data 路径以该文件目录为准。

## WP01 补充修复 · React 18 实时玻璃滤镜

- 日期：2026-11-16；按用户追加要求，仅解决实时玻璃及相关验证。
- 核对上游 peer 依赖修复提交，保留 React 18.3.1 和 liquid-glass-react 1.1.1；仅针对该固定版本以 npm overrides 对齐 React 依赖，移除全局 legacy-peer-deps，不再跳过整个依赖树的 peer 检查。
- 新增 GlassSurface 统一封装，真实挂载 SVG 位移/色散滤镜；导航、主按钮、卡片与次要状态条目均通过该封装。滤镜装饰层独立，文字与点击不受滤镜变形影响；禁用库默认的整体平移，避免偏位。
- full 全部启用实时滤镜，lite 保留主要容器滤镜、卸载次要条目的 SVG 并使用 Tailwind 半透明样式。保存偏好并保持内容节点；ResizeObserver 更新尺寸时只重建装饰层。
- Safari/Firefox 默认 lite，SVG 背景位移支持不完整时使用 CSS 毛玻璃；这属于浏览器能力回退，不宣称折射与 Chrome 一致。
- 新测试依赖 jsdom 26.1.0：仅为 Vitest 提供 React DOM 环境，测试实际库和 React 18 的挂载与交互，不替换 UI 库，不用于生产代码。
- `npm ci --strict-peer-deps`、依赖树检查和 npm audit（0 项）通过。前端 13 项测试通过（原有 5 项 + 8 项真实库集成测试），覆盖滤镜实际引用、唯一 ID、两档差异、持久化、内容保留、动态尺寸、鼠标重绘和浏览器回退。Python 全部 41 项仍通过；TypeScript 与生产构建通过。
- 已在本机 Chrome 实际打开生产构建，观察完整/轻量两种画面和次要条目的效果差异，验证切换、刷新保持，并恢复为完整模式。Edge、Safari、Firefox 实际画面尚未现场验证，自动化覆盖其回退判断。
- 手动验收：构建后打开 localhost:8080，在 Chrome/Edge 切换右上角完整/轻量；导航和卡片保持玻璃，三个状态条目仅完整档有折射边缘；刷新应保留选择。移动鼠标检查高光变化，缩放窗口检查边缘与容器对齐。Safari/Firefox 验证默认轻量和 CSS 回退。
