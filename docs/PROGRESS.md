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

## WP02 · NAS 文件接口与会话池

- 日期：2026-11-16。
- 开发与本地验证完成；**真实 NAS 人工验收尚未执行**。探路 RESULT.md 仍未填写，不把本地测试记成真实 NAS 验收。

### 已完成

- `backend/app/services/nas/fs.py` 定义 NasFs 协议与 NasStat，实现所有规定方法、流式异常转换和生命周期管理；支持追加、偏移读、递归建目录及显式递归删除。
- `LocalFs` 根据用户区域权限表执行只读/读写/无权限模拟；默认根 `./devdata/nas` 来自配置。`SmbFs` 使用本人凭据、独立连接缓存及 SMB 连接，不挂盘、不借用系统身份。
- 三个区域根均来自配置；安全拼接拒绝路径穿越、绝对路径、非法名称和区域外访问，禁止删除/改名区域根；拒绝链接/重解析点。重命名检查源与目标权限，只用 rename，不退化复制。
- 统一四类文件异常；SMB 按 NTSTATUS 优先分类，共享冲突不能误判为权限拒绝。错误不返回上游请求对象和认证材料。
- SessionPool 提供 open/get/close/system、后台空闲回收与 shutdown，支持上下文管理；用户会话过期抛 NasSessionExpired 并要求重新登录。实例操作和池操作分别上锁，活动流阻止空闲回收。
- FakeAuthBackend 从 dev_users 读取假账号、密码与权限；DsmAuthBackend 发现接口后 POST 登录，成功立即 POST 注销，不保留用户凭据或 SID，认证拒绝与服务不可用分别处理。
- 模式工厂提供验证后端、用户文件会话、系统文件会话及会话池；prod 系统账号从 Fernet 密文解密，dev 使用 LocalFs。系统权限固定为正式只读/备份读写/临时无权限，并仍受 NAS ACL 限制。
- 配置示例与 README 已补齐 dev_users、dev_nas_dir、idle_minutes、timeout_s、受信 CA，以及两个测试账号的环境变量运行方式。根 pytest 配置统一管理 nas/bailian 默认排除。
- 本包没有登录或上传 API，没有新数据库表/迁移，没有前端业务改动。工厂由后续调用者显式创建，启动 health 不会访问真实 NAS。

### 依赖与验证

- 运行依赖增加既定技术栈中的 `smbprotocol==1.16.1`、`httpx==0.28.1`（探路已使用），Fernet 复用 WP01。无栈外运行依赖。
- 全部 Python 测试：90 项通过、14 项真实 NAS 测试默认排除；其中 WP02 新增 49 项本地验证，覆盖 NasFs 契约、权限、路径、符号链接、系统权限、池打开/替换/获取/超时/后台关闭/并发获取、活动流保护、DSM 成功/拒绝/注销失败、SMB 隔离/重连上限/错误脱敏/不重复写入。
- 同一套文件契约参数化使用 LocalFs 与 SmbFs，smb 参数加 nas 标记；另有真实 DSM 双账号验证/注销用例。`pytest backend/tests/nas -m nas --collect-only -q` 确认可收集 14 项真实用例，未执行连接。
- `npm --prefix frontend run check`：13 项测试通过，TypeScript 与生产构建通过。Python 编译与依赖兼容检查通过。保留现有 Starlette/httpx TestClient 弃用提示，没有引入 httpx2。

### 已知限制与取舍

- 为满足本包“断线重连一次”，活动 SMB 实例仅在内存 SecretStr 保留本人密码，关闭/过期清除引用；不落盘或写日志，不使用 DSM SID 换 SMB 凭据。Python 无法保证字符串物理内存擦除；架构中的冲突已更新 Q003。
- 每次方法调用最多重连一次；已知断线可在执行前恢复，安全读请求可重试；已发送变更、流内 I/O 不重放，活动流存在时不替换连接，以免重复追加或移动结果不确定。上层恢复策略留给后续上传/入库工作包，详见 Q007。
- 目录删除默认非递归；显式递归过程不是事务，失败可能留下部分目录。LocalFs 是受控开发模拟器，不是本机多用户沙箱；本包不实现跨共享/DFS/IPv6、OTP/Secure SignIn 交互或工作台角色授权。
- 池关闭与流关闭由调用者负责。空闲回收默认每 30 秒扫一次，get 同步检查；打开的流不视为空闲，可避免长上传被回收，但忘记关闭会阻止回收。
- 真实群晖 DSM/SMB、现场 ACL 与跨目录 rename 均待内网验证；不宣称公司 NAS 已验证通过，也不自动改动生产配置或正式目录。

### 手动验收

1. 更新依赖 `python -m pip install -e './backend[test]'`；使用示例 dev 配置的两个假账号，通过 README 的工厂/会话池示例验证本地读写、追加及偏移读；全量运行 `python -m pytest -q`。
2. 管理员提前设置专用测试共享内三个区域：A 正式只读、临时读写；B 为授权终审测试账号，正式与临时读写。不得指向公司正式资产目录。
3. 按 README 用环境变量提供 NAS_TEST_HOST/SHARE/FORMAL/TEMP/BACKUP、USER_A/B、PASSWORD_A/B；DSM 端口/HTTPS 与 CA 按现场设置，密码隐藏输入且不写文件。
4. 在公司内网执行 `python -m pytest backend/tests/nas -m nas -q --tb=short`，全部 14 项应通过。验证两账号同时存在、A 拒绝写正式测试区而 B 本人可移动；DSM 验证后立即注销。
5. 检查本次 UUID 子目录已清理；若网络/权限导致残留，确认后人工清理。记录真实结果并更新探路 RESULT.md；20MiB 与 rename <1 秒性能验收仍按 WP00 脚本执行。

## WP03 · 登录、角色与界面框架

- 日期：2026-11-16。开发、本地自动化与 Chrome 验收完成；真实 NAS 的 prod 登录仍待公司内网手动验证。

### 已完成

- POST `/api/auth/login/logout`、GET `/api/auth/me`，使用 WP02 验证后端与本人 SessionPool。新账号默认 uploader，bootstrap_admin 仅首次创建影子用户时获得 admin；disabled 账号禁止登录。
- 256 位随机工作台令牌，数据库只存 SHA-256；HttpOnly + SameSite=Lax Cookie，时长来自配置，HTTPS 启用 Secure；密码不入数据库、日志、浏览器存储，响应不包含令牌。NAS 活动内存凭据沿用业务 §2.1 与 WP02，见 Q013。
- require_login/require_role，令牌过期、账号启用状态及 NAS 会话检查；NAS 过期统一 401/nas_session_expired，并作废该用户所有旧令牌；每次请求读取最新角色。
- GET/PUT `/api/admin/roles`，仅管理员访问，四种角色可多选并支持启用/停用；停用撤销用户全部令牌并关闭 NAS 会话。登录成功/失败、注销、会话失效、角色修改都追加 oplog，角色变更含前后值；无 oplog 更新删除。
- 同账号多个浏览器使用独立令牌，按用户共享 NAS 连接，最后一个有效令牌退出时关闭；防止一台注销影响其他电脑。应用生命周期创建/关闭池；重启后旧 Cookie 需重新登录。
- 五种 glass 封装齐全：GlassCard/Button/Panel/Modal/Nav；提供 GlassProvider/useGlassMode，兼容已有 useGlass 调用。full 使用真实 SVG 滤镜，lite 非壳条目/次按钮退化半透明和模糊；右上角切换、localStorage 持久化，Safari/Firefox 默认 lite 与 CSS 回退不变。
- 中文登录页、角色导航外壳、当前用户/退出、角色表页面、dev 独有组件样例和 60 卡片网格；原生 dialog 支持焦点约束和 Escape 关闭。API 401 清内存认证与角色缓存并跳转登录，NAS 超时展示指定提示。
- 首页不伪造批次/待办数据；上传、初审、终审、项目、搜索、设置均为建设中；未实现上传或批次业务。
- 示例增加 demo-admin 假账号与 bootstrap，README 补运行、角色表与 prod 手动验收。本机旧 dev 配置在缺少账号时已补齐示例假账号，保留其他配置；真实配置及运行数据均未提交。

### 依赖与验证

- 新增既定栈内 `react-router`（7.x，支持 React 18）、TanStack Query（5.x）、zustand（5.x）、react-hook-form（7.x），分别用于路由/守卫、请求缓存、内存认证状态和表单。无其他 UI 库或数据库。
- 全部 Python：102 项通过、14 项真实 NAS 用例默认排除；WP03 新增 10 项集成与 2 项服务单元测试。覆盖每个 API 的成功/失败、错误密码、Cookie/令牌哈希、过期、角色继承/拒绝、bootstrap 不反复提权、停用撤销、NAS 过期码、旧令牌不复活、多人注销和失败回滚。
- 前端 23 项测试通过（新增 10 项），覆盖导航权限、401 清理与超时提示、403 保留认证、网络中文提示、路由守卫、退出、dev/prod 样例可见性、60 卡片与弹窗。已有真实玻璃库集成测试保留。
- npm ci --strict-peer-deps 成功，npm audit 0；TypeScript 与生产构建成功。Python 编译检查通过。仍有已知 Starlette/httpx TestClient 弃用提示。
- Chrome 实测：demo-admin 登录成功，出现管理导航；角色表显示角色复选框；完整模式 60 卡片网格显示正常；切至轻量打开弹窗，Escape 关闭，刷新仍为轻量；退出回登录。角色授予与对方重新登录的导航变化由自动化覆盖。未使用真实 NAS 凭据，未实测 prod 登录。

### 已知限制与取舍

- 多浏览器共用每用户 NAS 连接，注销语义及文档差异见 Q010；角色、引导、巡检权限冲突见 Q011～Q014。角色表不更改 NAS ACL。
- 管理员可撤销自身权限或停用自己，本包未自行增加“最后一个管理员”限制；bootstrap 仅首次创建时生效，部署需保留可用管理员。
- 页面导航基于已获取角色；服务器每次检查最新角色，界面重新登录后更新，API 401 即清状态。未增加账号目录同步或显示名编辑功能。
- 同账号重新登录会重建共享 NAS 连接；本包无上传任务，因此不处理长任务跨登录恢复。上传不中断/任务续传需后续工作包结合活动句柄处理。
- 全部新逻辑为单进程部署设计，登录/角色修改使用进程锁和数据库事务；NAS 与数据库无法组成分布式事务，异常会作废/清理已打开连接。
- 组件网格供人工观察流畅度，未提供或宣称 FPS 性能基准。Edge/Safari/Firefox 现场视觉与 Windows 仍待验证。

### 手动验收

1. 安装更新依赖、构建、重启；从示例合并 dev_users 和 bootstrap_admin。dev 登录 demo-uploader/demo-uploader-only，错密码应失败，退出后打开任意受保护路径回登录。
2. demo-admin/demo-admin-only 首次登录获得管理角色；让另一账号先登录一次，在角色表给其勾选初审或终审并保存，对方重新登录后检查菜单。非管理员调用 GET/PUT roles 均应 403。
3. 角色停用后旧登录立即不可用，重新登录被拒绝；用仍有效的管理员恢复启用。
4. 打开 dev 组件样例，检查全部组件、弹窗与 60 张卡片；切换/刷新确认 full/lite 持久化；prod 下入口隐藏且直接访问跳首页。
5. 运行 `python -m pytest -q` 与 `npm --prefix frontend run check`。公司内网再配置 mode=prod、真实 NAS/CA 与负责人账号，通过浏览器使用本人 NAS 凭据验证 DSM + SMB 登录，不将真实密码写入示例或报告。
