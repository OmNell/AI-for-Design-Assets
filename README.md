# NAS 设计资产工作台

公司内网设计资产工作台。当前完成 WP00～WP11。上传人可以分片上传项目文件夹，查看 AI 生成的标签和套装关系，逐项核对后提交初审。两名审核人依次审核，终审通过后启动入库，全部正式商品向量通过校验后发布。

## 开发环境

- Python 3.12。
- Node.js **22.12 或以上的 22 LTS**（也可使用符合依赖要求的较新 LTS），npm 10 或以上。Vitest 5 要求 Node 22.12+，架构文档原先的 Node 20 已记入 `docs/QUESTIONS.md`。
- SQLite 随 Python 提供，无需额外安装数据库或外部服务。
- 公司内网真实 NAS 不影响骨架启动及默认测试。

从仓库根目录执行。Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
.\.venv\Scripts\python.exe -m pip install -r scripts/probe/requirements-dev.txt
Copy-Item backend/config.example.yaml config.yaml
npm --prefix frontend ci
npm --prefix frontend run build
.\.venv\Scripts\python.exe -m app
```

macOS / Linux：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e './backend[test]'
.venv/bin/python -m pip install -r scripts/probe/requirements-dev.txt
cp backend/config.example.yaml config.yaml
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python -m app
```

已有 `config.yaml` 时保留现有文件，不重复复制。浏览器访问 <http://localhost:8080>，未登录时应进入中文登录页；使用下方示例假账号登录后，可看到角色导航、服务版本和运行状态。启动命令读取配置中的 `server.host` 和 `server.port`。默认监听 `0.0.0.0:8080`，仅本机开发时可改成 `127.0.0.1`。

可单独热更新前端：另开终端执行 `npm --prefix frontend run dev`，访问终端输出的 Vite 地址（默认 5173）；Vite 将 `/api` 请求代理给本机 8080 后端。

## 配置与持久化

- 默认读取当前工作目录的 `config.yaml`；`NASWB_CONFIG` 可指定其他文件。
- 优先级：环境变量 > YAML > 默认值。例如 `NASWB_MODE=dev`、`NASWB_SERVER__PORT=8081`；嵌套字段使用双下划线。
- 结构按架构 §12.1，增加 `mode: dev|prod` 与 `bootstrap_admin`；bootstrap_admin 在该账号首次成功登录并创建影子用户时追加 admin；不会反复覆盖管理员后来调整的角色。缺失文件、未知字段或非法值会阻止启动。
- `paths.data_dir` 默认 `./data`；相对路径以配置文件所在目录为准。日志也位于该目录下的 `logs/`，每天滚动，保留 30 个归档。
- 首次启动自动生成 `data/secret.key`、通过 Alembic 创建 `data/app.db`。SQLite 启用 WAL、`busy_timeout=5000` 和外键检查。每次启动执行 `upgrade head`，重复启动不会重建已有表或密钥。
- 首个迁移包含 `users`、`sessions`、`settings`、`oplog`、`jobs`，WP04 的 `0002_upload` 增加 `projects`、`batches`、`batch_files`、`preflight_acks`。`projects` 本包仅查重，不在上传时创建正式项目。`oplog` 数据库触发器禁止 UPDATE/DELETE。
- WP05 的 `0003_thumbnails` 增加 batch_files.thumb_status/thumb_error 和 jobs 状态/调度时间索引；启动自动迁移。
- WP06 的 `0004_ai` 增加 tag_fields、batch_products、company_industry_map、ai_calls、ai_alerts，并初始化业务文档9.5的11个自由文本字段。
- 密钥 POSIX 权限为 `0600`；Windows 使用受保护 DACL，仅当前服务身份、管理员组及 SYSTEM 可访问。应部署在支持权限控制的本地文件系统，权限设置失败会中止启动。Windows ACL 分支尚待在实际 Windows 环境验证。
- 密钥与数据库应一并备份；已有密钥无效时会报错，不自动覆盖。删除 `data/` 并重新启动会创建全新数据库与密钥，旧密钥加密的内容无法用新密钥解密。
- 普通 NAS 密码不写配置；`nas.system_password_enc` 和 `ai.api_key_enc` 只放密文。prod 登录时使用真实 DSM 和本人 SMB；只有显式使用系统会话才解密系统账号密码，不连接百炼。

加密工具的使用（Python 代码，不要把真实凭据写入源码、命令历史或日志）：

```python
from app.config import load_settings
from app.services.crypto import SecretCipher

cipher = SecretCipher(load_settings().paths.data_dir)
# cipher.encrypt(内存中的字符串) -> 密文字符串
# cipher.decrypt(密文字符串) -> 内存中的明文
```

业务服务可用 `with database.session() as session:` 获取事务，再调用 `OplogService(session).write(...)`。`user` 为用户 ID 或 `None`，`object_id` 为字符串或 `None`，`before/after` 为可 JSON 序列化对象。日志写入只 flush，由调用方事务统一提交/回滚；登录、注销和角色变更会追加操作日志，不记录密码或原始令牌。

## 测试与构建

仓库根目录，一条命令执行全部 Python 测试（包含 WP00）：

```sh
python -m pytest -q
```

这里的 `python` 指已激活 `.venv` 的解释器；也可使用上面的完整解释器路径。默认排除 `nas` / `bailian` 标记测试。本包所有新增测试使用 dev 模式与本地临时目录，不访问外部服务。

一条命令执行前端测试与构建：

```sh
npm --prefix frontend run check
```

输出为 `backend/app/static/`，已忽略 Git；部署或新检出后必须先构建。前端锁文件随仓库提交，使用 `npm ci` 复现。

手动迁移（默认启动也会自动执行）：

```sh
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
```

检查后端：

```sh
curl http://localhost:8080/api/health
```

成功示例：`{"version":"0.1.0","mode":"dev","database_available":true}`。数据库不可用时返回 503；所有 API 错误采用 `{code,message,detail}`，不返回内部异常、配置或请求体。非 `/api` 页面路由回退 `index.html`，未知 API 和缺失 `/assets/` 资源返回结构化 404，前端未构建时首页为 404，health 仍可用。

重建验收：停止后端，将开发环境的 `data/` 移至临时备份位置或删除，再启动；应自动生成新数据库及新密钥，health 恢复正常。此操作仅针对可丢弃的开发数据。

## 前端兼容性

`liquid-glass-react@1.1.1` 的 npm 发布包将 peerDependencies 写为 React >=19，但[上游修复提交](https://github.com/rdev/liquid-glass-react/commit/fff31be69fc674ebb71070a0f3e45659d66a5560)已改为 >=18。项目保留 React/ReactDOM 18.3.1，针对固定版本使用 npm overrides 指向根依赖，不修改库源码，不再使用全局 legacy-peer-deps。干净 `npm ci` 与 `npm ls react react-dom liquid-glass-react` 均可成功。

玻璃效果统一经 `src/components/glass/GlassSurface.tsx` 封装：Chrome/Edge 使用库的真实 SVG 位移、色散和鼠标高光，装饰层与正文分开，避免文字变形与点击被遮挡。ResizeObserver 让滤镜随容器尺寸更新，正文节点保持不变。

- full：导航、主要按钮、卡片及次要条目均使用实时滤镜。
- lite：导航、主要按钮、卡片保留玻璃效果；次要条目使用 Tailwind 半透明样式，不挂载 SVG 滤镜。
- 右上角可切换，偏好存于 localStorage，刷新保持。Safari/Firefox 默认 lite，其 SVG 背景位移支持不完整，使用 CSS 毛玻璃；不把这些浏览器的折射效果宣称为与 Chrome 相同。

本地验证：`npm --prefix frontend run check` 包含真实库的 React 18 DOM 集成测试，验证滤镜绑定、唯一 ID、模式切换、正文保留、尺寸变化及兼容回退。DOM 测试不能代替像素渲染；已另外在本机 Chrome 检查完整/轻量画面及刷新持久化。Safari/Firefox 和 Edge 的实际画面仍需现场验收。

技术文档见 `docs/`，进度及手动验收记录见 `docs/PROGRESS.md`；NAS 探路详见 `scripts/probe/README.md`。

## WP02 · NAS 文件接口与会话池

本节介绍 WP02 文件模块；WP03 登录接口见下方，应用启动仍不会自动登录 NAS。已有开发环境先执行 `python -m pip install -e "./backend[test]"` 更新依赖。首次复制示例配置可直接使用假账号；已有配置请补充下面的新字段，不覆盖真实配置。

```yaml
nas:
  # 保留原有 host/share/root_path/temp_path/backup_path 等字段
  idle_minutes: 30
  timeout_s: 30
  dsm_ca_file: null
paths:
  data_dir: ./data
  dev_nas_dir: ./devdata/nas
dev_users:
  - username: demo-uploader
    password: demo-uploader-only  # 假密码，严禁在 dev_users 填真实 NAS 凭据
    permissions: {formal: read, temp: write, backup: none}
  - username: demo-final
    password: demo-final-only
    permissions: {formal: write, temp: write, backup: none}
```

权限键为 `formal`（正式）、`temp`（临时）、`backup`（备份）；值为 `read`（只读）、`write`（读写）、`none`（无权限），缺项默认无权限。三个区域根由 `nas.root_path/temp_path/backup_path` 配置，必须不重叠。`paths.dev_nas_dir` 和 `nas.dsm_ca_file` 的相对路径仍以配置文件目录为准。

调用示例（在激活的 Python 环境中）：

```python
from app.config import load_settings
from app.services.nas import Region
from app.services.nas.factory import create_auth_backend, create_paths, create_session_pool

config = load_settings()
auth = create_auth_backend(config)
assert auth.verify("demo-uploader", "demo-uploader-only")
paths = create_paths(config)
with create_session_pool(config) as pool:
    fs = pool.open(1, "demo-uploader", "demo-uploader-only")
    path = paths.join(Region.TEMP, "开发演示.bin")
    with fs.open_write(path) as stream:
        stream.write(b"abc")
    with fs.open_write(path, append=True) as stream:
        stream.write(b"def")
    with pool.get(1).open_read(path, offset=2) as stream:
        assert stream.read() == b"cdef"
    fs.delete(path)
    pool.close(1)
```

接口约定：

- `list(path) -> list[str]` 返回排序后的直接子项名；`stat(path) -> NasStat(size,is_dir,mtime)`，mtime 为 Unix 时间；`exists` 仅在不存在时返回 False，权限和断网异常仍抛出。
- 所有运行时路径均为共享文件夹内的相对路径，通过 `create_paths(...).join(Region, ...)` / `safe_join` 构造；拒绝绝对路径、盘符、UNC、`..`、空片段及非法字符。配置根允许文档中的一个前导 `/`，不代表运行时允许绝对路径。
- `open_read(path, offset=0)`、`open_write(path, append=False)` 返回需关闭的二进制流，推荐 `with`；普通写会截断旧文件，追加写保留旧内容；不自动创建父目录。
- `mkdir(path, recursive=False)` 需显式开启递归，目标已存在报 `NasAlreadyExists`；`rename` 使用同共享文件夹的服务端改名，不降级为复制；`delete(path, recursive=False)` 可删文件或空目录，非空目录需显式递归。
- 禁止删除/改名三个区域根；LocalFs 拒绝符号链接/联接点，SmbFs 拒绝路径上的重解析点。不接受 DFS、其他服务器/共享文件夹路径；LocalFs 是受控开发模拟器，不作为多用户本机操作系统沙箱。
- 统一异常为 `NasPermissionDenied`、`NasNotFound`、`NasAlreadyExists`、`NasUnavailable`；错误消息为中文，不包含原始凭据或上游请求对象。
- `SessionPool.open/get/close/system` 线程安全；取不存在或空闲过期的用户会话抛 `NasSessionExpired`，要求重新登录。池有后台回收线程，默认每 30 秒扫一次，`get` 也会同步检查。`get` 和实际文件操作均刷新活动时间；打开的文件流阻止空闲回收，使用完必须关闭。应用结束调用 `shutdown()`，或使用上下文管理器。
- 每个 SmbFs 以本人凭据、私有缓存建立独立 SMB 连接；使用 NTLM 并要求签名。每次登录独立建连接，会话成立后释放普通用户密码引用。DSM SID 不能替代 SMB 密码。
- SMB 断线后返回会话失效，保存上次确认的偏移。重新登录后校对尺寸与分片边界再继续。已发出的变更和读写不自动重放。
- `DsmAuthBackend.verify` 发现接口后 POST 登录，成功立即 POST 注销，SID/密码不保留到实例。身份拒绝返回 False；网络、协议或注销失败抛 `AuthUnavailable`。没有 OTP / Secure SignIn 交互和自动认证重试；HTTPS 默认校验证书，自签名用 `dsm_ca_file` 指定受信 CA。
- `system()` 采用独立实例：dev 使用固定权限（正式只读、备份读写、临时无权限）；prod 解密 `nas.system_password_enc` 后建立系统账号连接，同时保留上述权限上限，实际 NAS ACL 仍须正确配置。普通用户文件操作从不回退系统账号。

### 内网真实 NAS 测试

默认 `python -m pytest -q` 排除 nas/bailian；即使只指定 `backend/tests` 也使用根测试配置。真实测试与本地实现共用 `backend/tests/nas/test_contract.py` 中的同一套参数化用例，另有两个账号的 DSM 登录/立即注销测试。

先由管理员创建**专用测试共享文件夹内的三个空区域目录**，例如 `WP02测试/正式`、`WP02测试/临时`、`WP02测试/备份`。不要指向公司正式资产目录。账号 A 对正式区只读、临时区读写，B 对正式区及临时区读写；两账号对测试目录需有列目录/遍历权限。备份区不参与写测试。测试将在正式测试区创建 UUID 子目录、写入并删除测试文件，因此 B 必须是被授权的终审测试账号。NAS 实际 ACL 是 prod 的最终权限执行点。

Windows PowerShell（当前进程环境变量，不写配置/源码）：

```powershell
$env:NAS_TEST_HOST = "192.168.0.177"
$env:NAS_TEST_SHARE = "专用测试共享"
$env:NAS_TEST_FORMAL = "WP02测试/正式"
$env:NAS_TEST_TEMP = "WP02测试/临时"
$env:NAS_TEST_BACKUP = "WP02测试/备份"
$env:NAS_TEST_USER_A = "测试上传人"
$env:NAS_TEST_USER_B = "测试终审"
$env:NAS_TEST_PASSWORD_A = [Net.NetworkCredential]::new("", (Read-Host "账号 A 密码" -AsSecureString)).Password
$env:NAS_TEST_PASSWORD_B = [Net.NetworkCredential]::new("", (Read-Host "账号 B 密码" -AsSecureString)).Password
$env:NAS_TEST_DSM_PORT = "5001"
$env:NAS_TEST_DSM_HTTPS = "true"
# 自签名证书时设置受信 CA，证书名称须与 HOST 匹配：
# $env:NAS_TEST_CA_FILE = "C:\certs\nas-ca.pem"
python -m pytest backend/tests/nas -m nas -q --tb=short
Remove-Item Env:NAS_TEST_PASSWORD_A, Env:NAS_TEST_PASSWORD_B
```

macOS/Linux（bash；zsh 用 `read -s 'NAS_TEST_PASSWORD_A?账号 A 密码：'` 等同义语法）：

```bash
export NAS_TEST_HOST=192.168.0.177
export NAS_TEST_SHARE='专用测试共享'
export NAS_TEST_FORMAL='WP02测试/正式'
export NAS_TEST_TEMP='WP02测试/临时'
export NAS_TEST_BACKUP='WP02测试/备份'
export NAS_TEST_USER_A='测试上传人'
export NAS_TEST_USER_B='测试终审'
read -r -s -p '账号 A 密码：' NAS_TEST_PASSWORD_A
read -r -s -p '账号 B 密码：' NAS_TEST_PASSWORD_B
export NAS_TEST_PASSWORD_A NAS_TEST_PASSWORD_B
export NAS_TEST_DSM_PORT=5001
export NAS_TEST_DSM_HTTPS=true
# export NAS_TEST_CA_FILE=/path/to/nas-ca.pem
python -m pytest backend/tests/nas -m nas -q --tb=short
unset NAS_TEST_PASSWORD_A NAS_TEST_PASSWORD_B
```

显式选择 nas 后缺少必要环境变量会报错，不会跳过并假装通过。不要加 `--showlocals`、输出完整环境或开启协议调试日志；测试报告仅记通过/失败、中文错误与现场权限。每个用例只清理本次创建的 UUID 子目录；网络断开或权限错误可能残留，请按前缀确认后人工清理，不能删除测试父目录。

现场验收需确认：全部 nas 用例通过；A/B 同时连接时 ACL 分别生效；A 不能把文件移入正式测试区、B 能用本人会话完成移动。也请继续填写 WP00 的 `scripts/probe/RESULT.md`（20MiB 与 rename <1 秒的性能验收属于探路脚本）。本次开发未连接真实 NAS，不能用本地测试结果替代内网验收。

实现参考：[smbprotocol 1.16.1 官方连接缓存实现](https://github.com/jborean93/smbprotocol/blob/v1.16.1/src/smbclient/_pool.py)、[DSM 官方登录 API 指南](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Os/DSM/All/enu/DSM_Login_Web_API_Guide_enu.pdf)。

## WP03 · 登录、角色与界面框架

更新依赖后重新构建、重启后端：

```sh
python -m pip install -e './backend[test]'
npm --prefix frontend ci
npm --prefix frontend run check
python -m app
```

首次使用复制最新 `backend/config.example.yaml`。已有配置请仅合并 dev_users 和 bootstrap_admin，不覆盖真实 NAS 配置；切勿在 dev_users 填写真实密码。示例为：

| 假账号 | 假密码 | 首次登录的工作台角色 |
|---|---|---|
| demo-admin | demo-admin-only | 上传人 + 管理员（示例 bootstrap_admin） |
| demo-uploader | demo-uploader-only | 上传人 |
| demo-final | demo-final-only | 上传人；其模拟 NAS 正式区可写，但仍需管理员授予终审角色 |

默认所有新 NAS 账号为 uploader。bootstrap_admin 必须与用户名精确匹配，仅在首次创建影子用户时追加 admin；后续登录不会自动恢复被撤销的管理员角色。prod 部署前将示例 bootstrap_admin 改为真实项目负责人的 NAS 账号。角色表仅列出已成功登录过的用户，不从 NAS 拉取整个公司目录。

- `/api/auth/login`：POST JSON `{username,password}`，验证后建立本人 NAS 会话、创建/读取用户并签发 256 位随机令牌；响应只有用户信息。数据库仅保存令牌 SHA-256，浏览器使用 HttpOnly、SameSite=Lax、Path=/ Cookie，有效期来自 `server.session_hours`；HTTPS 请求启用 Secure，架构中的内网 HTTP 保持可用。
- `/api/auth/me`：GET，返回 `id/username/display_name/roles/enabled`；所有已登录接口检查有效令牌、启用状态及 NAS 会话。未知/过期令牌返回 401；NAS 会话失效返回 `nas_session_expired`，页面提示“NAS 连接已超时，请重新登录”。过期 NAS 会话对应的旧令牌被作废，重新登录不能让这些旧令牌恢复有效。
- `/api/auth/logout`：POST，仅作废本次登录令牌并关闭对应 NAS 连接。同账号其他设备的登录与连接继续有效。应用进程重启后，旧 Cookie 因 NAS 会话不存在而要求重新登录。
- `/api/admin/roles`：GET 全部已登记用户，PUT JSON `{user_id,roles,enabled}` 更新一人；仅 admin 可用。角色仅四种、可多选：uploader、first_reviewer、final_reviewer、admin。初审/终审继承上传能力，admin 不自动继承审核权限。停用用户即撤销其全部令牌并关闭 NAS 会话；已登录请求每次重新检查数据库角色，授予角色后重新登录即可刷新导航。
- 管理员允许编辑自己的角色/启用状态，未增加文档没有规定的“最后一个管理员”业务限制；操作前请保留另一位可用管理员。bootstrap_admin 不会在每次登录时重新加权，重新启用需由其他管理员操作。
- 工作台角色不能授予 NAS ACL。dev 模式从 dev_users 模拟权限，prod 仍由本人 SMB 连接和群晖 ACL 实际执行文件权限。
- 上传及首页我的批次已在 WP04 实现；待初审、待终审、项目、搜索、设置仍为建设中占位。管理页可维护角色。组件样例 `/components` 仅 dev 模式显示，prod 直接访问跳回首页。
- 五种玻璃组件统一位于 `src/components/glass/`：Card、Button、Panel、Modal、Nav；GlassProvider/useGlassMode 提供全局两档。full 全部实时，lite 主要壳保留玻璃、卡片/次要按钮使用半透明和模糊。60 卡片网格仅供开发比较流畅度，没有宣称达到某个 FPS 指标。
- 退出或任一 API 返回 401 时清除内存用户及角色缓存，守卫跳转登录；令牌、用户名密码不写 localStorage（只保存视觉偏好）。表单提交后清空密码字段；SMB 建连后释放普通用户密码。生产环境 DSM 和百炼使用 HTTPS。工作台内网 HTTP 登录页提示口令及 Cookie 明文传输风险，写接口拒绝跨站浏览器请求。

手动验证：

1. 浏览器打开 <http://localhost:8080>，使用 demo-uploader 登录一次再退出；用错误密码重试应提示失败。退出后访问 `/projects` 或 `/admin/roles` 应进入登录页。
2. 用 demo-admin 登录，管理 → 角色表，给 demo-uploader 勾选初审并保存；对方重新登录后应出现“待初审”。普通账号直接请求 GET/PUT `/api/admin/roles` 返回 403。
3. 在组件样例页打开弹窗（Escape/关闭按钮可关闭）、切换 full/lite、滚动 60 张卡片并刷新，检查偏好保持与文字/按钮清晰可用。
4. 测试令牌和 NAS 超时的自动化用例位于 `backend/tests/integration/test_auth_roles.py`；默认测试无真实 NAS 依赖。
5. 内网 prod 验证：配置真实 NAS 主机、共享及区域根、受信 CA 与 bootstrap_admin，`mode: prod` 后重启；在登录页输入本人真实 NAS 凭据，应完成 DSM 验证和本人 SMB 建连。本包未在真实 NAS 上执行此项，不能把 dev 测试视作通过。

## WP04 · 文件夹预检与分片上传

沿用上面的安装、测试、构建和启动命令，无新依赖。重启会自动应用最新 Alembic 迁移；升级前备份现有数据库与密钥。默认配置为：

```yaml
upload:
  chunk_mb: 8
  parallel_files: 3
  fingerprint_algorithm: sha256
```

前端从批次响应读取分片字节数和并行文件数；主图优先，其余按文件大小递增，每个文件内部顺序发送。网络/服务暂时错误自动重试三次；409 按响应 `detail.bytes_received` 对齐。每次只读取当前分片，不把整个 2GB 文件装入内存。

浏览器操作：

1. 用拥有上传能力的 dev 账号登录，进入“上传”，先选是否中标，再拖入一个 `编号_企业_项目名` 文件夹。只能有一个顶层文件夹；Chrome/Edge 的“选择文件夹”优先使用目录选择 API，保留空目录，其他浏览器回退 `webkitdirectory`。回退无法枚举空目录，页面会提示改用拖入。
2. 左侧树和右侧问题清单按红/黄/灰显示拦截、提醒、忽略。首次入库至少需要一张商品主图，支持 jpg/jpeg/png（扩展名不区分大小写）；套装/单品必须平铺，其他目录及文件原样保留。Thumbs.db、.DS_Store、desktop.ini、`~$` 文件不上传；空文件和空目录可上传。每类提醒都需勾选“我已知晓”，后端会再次校验并记录 `preflight_acks`。
3. 第一段恰为大写 `H` 时，必须填写项目实际所属年份，创建时原子发放 `H{年份}-{四位流水}`；页面和临时区使用新名称，本地目录无需改名。已存在的正式项目编号显示“该项目已入库，补充功能尚未开放”，不能创建补充批次。
4. 核对编号、企业、是否中标、文件数、总大小后上传。显示按字节统计的总进度、平均速度、预计剩余时间及逐文件状态。同账号已有活动上传时，后续批次排队；保持排队页打开会自动轮询并按顺序开始。
5. 暂停、关闭页面或网络中断后，从首页“继续上传”重新拖入原文件夹，先比对路径、大小和浏览器修改时间（毫秒）。已完成且未变化的文件跳过，未完成的按服务端偏移续传。新增、缺失、变动及目录变化会先展示；明确确认差异后，缺失文件删除、变动文件从零重传，其他文件保留。
6. 全部文件完成进入“已暂存”。“确认，开始 AI 处理”启动打标。发起人可以在终审通过前填原因并确认终止，记录为“已终止”并释放批次锁，保留临时文件和历史。

文件仅写入 `nas.temp_path/{batch_no}/{项目文件夹名}/{相对路径}`；实际根由配置和后端 ID 拼接，不接收浏览器完整 NAS 路径。所有写入、重读、截断修复和删除均通过上传人 NasFs，不使用系统账号或正式区。

主要接口：

| 接口 | 用途 |
|---|---|
| POST `/api/batches/precheck` | `{folder_name,files}`；清单项为 `{rel_path,size,mtime,is_dir}`，目录可省略 size/mtime；返回解析、编号状态、问题和路径前缀字符数 |
| POST `/api/batches` | 上述清单加 `kind: ingest`、`won`、`acks` 规则编号数组、`historical_year`（历史 H 批次必填）；返回批次及会话绑定的上传令牌 |
| PUT `/api/batches/{id}/files/{file_id}/chunks/{index}` | `application/octet-stream` 原始字节，index 从 0 开始；同分片重复提交先核对已存内容，不重复追加 |
| GET `/api/batches/{id}/upload-state` | 服务端偏移、文件状态、排队及批次状态 |
| POST `/api/batches/{id}/files/{file_id}/complete` | 校验大小、计算指纹，全部完成后已暂存 |
| POST `/api/batches/{id}/resume` | 清单加 `acks`、可选 `accept_changes: true`；有差异时先仅返回差异，接受后重新签发令牌 |
| POST `/api/batches/{id}/pause`、`/cancel`、`/confirm` | 暂停、填写 `{reason}` 后终止并保留临时目录、确认进入 AI 状态 |
| GET `/api/batches`、`/api/batches/{id}` | 本人列表及详情 |

上传令牌通过 `X-Upload-Token` 传递，数据库只存哈希。分片、完成、状态和暂停均要求同一有效登录会话。已开始的分片可写到安全检查点，退出、到期或掉线后不得开启新分片。重新登录并核对清单后，从上次确认偏移继续。

页面离开时尽力发送暂停；浏览器强退无法保证通知，后台每 5 秒检查，连续 60 秒没有上传/轮询请求则记“上传中断”并释放会话保留。服务停止和启动也会记录中断，包括排队批次；进度、清单、已完成指纹仍在库中。超慢网络或后台标签页冻结也可能触发中断，重新登录并续传即可。单进程部署；不要启动多个 Uvicorn worker 共享上传目录。

所有上传文件均计算完整 SHA-256。服务重启后从 NAS 流式补算丢失的增量状态。旧摘要保留算法标记，继续或确认未结束批次时由本人补算，同时保留原值。NAS 已写但数据库未提交的尾部会在续传时按确认偏移校正。

验收建议：在 dev 模式准备一个含中文、`05 详情页/空目录`、Thumbs.db、主图及 2GB 资料文件的项目，故意缺少 `04授权文件`。核对提醒知晓后可上传、空目录存在、垃圾文件不存在，抽查三个文件的完整 SHA-256；再测试半途关闭浏览器、重新登录/拖入续传，以及终止后临时目录保留、同编号可以重新发起。分别测试非三段名称、套装/单品子目录、系统保留名会拦截。`H_企业_项目名` 应获新编号。本机已验证 2GB 浏览器上传和三个完整摘要一致；真实 NAS 的 ACL、SMB 掉线后重新登录及性能仍需公司内网验收，不能用 dev 速度代替。

## WP05 · 后台队列与三档主图缓存

先更新 Python 依赖（新增约定栈内的 Pillow），然后执行全量测试、前端 check 并重启。启动自动应用最新 Alembic 迁移。已有 config.yaml 可不修改，默认 4 个后台工作线程；要调整可增加：

```yaml
jobs:
  workers: 4
```

`services/jobs` 使用 jobs 表与条件 UPDATE 抢占，处理函数在注册表登记。每类并发上限独立：缩略图 2、AI 类默认 3、入库/入库后操作/巡检/备份各 1。默认失败重试为 AI 类 3 次、其他 1 次，即最多执行 4/2 次；损坏图片、权限拒绝和会话缺失等明确不能重试的失败直接记录原因。定时任务到 scheduled_at 后执行。`enqueue(..., session=...)` 可与业务更新使用同一数据库事务，未提交或回滚的文件完成不会留下可执行任务。本包仅登记 thumb 处理器，未提供 AI 调用或其他业务任务。

主图 complete 与 thumb 入队同事务完成，重复 complete 不重复入队。任务参数只记录批次/文件 ID 和源版本摘要，读取路径从数据库重新拼接；执行时从 SessionPool 获取任务绑定的上传人本人登录连接，活动执行保留连接直至完成，不使用系统身份。主图范围是套装/单品下平铺的 jpg/jpeg/png；资料夹内图片不生成缓存。

Pillow 直接读取本人 NasFs 流，按 EXIF 方向转正，保留比例生成以下 JPEG：

| size 参数 | 本地文件 | 最长边上限 |
|---|---|---|
| 300 | `data/thumbs/batch/{batch_id}/{batch_file_id}/300.jpg` | 300px 缩略图 |
| 1600 | 同目录 `1600.jpg` | 1600px 预览图 |
| 1024 | 同目录 `1024.jpg` | 1024px 送审图 |

小于目标的小图不放大；透明图合成白底，灰度/CMYK 转 RGB，动画 PNG 取首帧。三档都移除 EXIF、ICC、XMP、原注释等元数据；本包不把任何图像发送外部。原件不在本地另存，临时目录只含派生 JPEG。核对主图完整指纹后发布三档，任务 done 后才允许访问；损坏图片在 batch_files.thumb_error 与 jobs.error 记录中文原因，不改变文件上传 done 或批次已暂存。超出 Pillow 安全像素上限的图片也会安全失败，不能以此宣称支持任意大像素源图。

- `GET /api/batches/{id}/thumbs/{batch_file_id}/{size}`：返回 image/jpeg；仅批次 owner、该批次指定且仍具相应角色的初审/终审、管理员可访问。验证有效工作台 Cookie 与启用状态；本地缓存读取不需要仍有 NAS 连接，不接受上传令牌。使用 private/no-store，防止浏览器复用到其他账号。
- 未生成时 404，`detail: {retryable: true}`；永久失败 404，`detail: {retryable: false, reason: "图片损坏或无法解码"}`。无对应主图、文件不属该批次或批次已终止也为不可重试 404。非法 size 返回 422，越权 403，未登录 401。
- `GET /api/admin/jobs`：管理员查看 ID、类型、状态、attempts、操作人 ID、错误原因和时间，不暴露任务 payload。`POST /api/admin/jobs/{id}/retry` 仅允许失败任务重新排队，清零本轮 attempts；仍使用原 run_as_user_id，不能借管理员 NAS 连接。更改与执行状态均追加 oplog。
- 上传进度页在每个已完成的主图行显示 300px 缩略图，生成中每秒重试；损坏显示原因。图片请求 401 只提示重新登录查看，不会删除已接收的分片。取消、文件变化与卸载会中止图片请求并释放 Blob URL；终止后后台任务也不能重新发布旧缓存。

**重启恢复需要本人身份：**running 的 thumb/AI 类恢复 queued；其他有副作用的任务标 failed 等待人工处理。缩略图仍必须读 NAS，重启后不能从磁盘找回用户密码，执行取不到连接会失败并明确记录“操作人会话已失效”。上传人重新登录后，这一原因的缩略图任务会自动重排并继续；其他失败需管理员重试。恢复/重试不影响已暂存批次，也不改变原 run_as_user_id。正常停止等待正在执行的任务完成，未开始的任务保留；强退留下的派生临时目录可在停止服务后清理 `data/thumbs/thumb-*`，不要清理原始 NAS 文件。

本地验收：`python -m pytest backend/tests/integration/test_thumbnails_jobs.py -q -s`，包含 20 张 2000×1000 主图上传后 10 秒内三档可访问、EXIF 方向与清除、坏图隔离、404 标记/授权/管理员重试、取消竞态，以及真正子进程执行中 kill 后启动并由本人重新登录恢复。该用例使用 dev/LocalFs，不代表真实 NAS 性能；内网需用专用测试项目重复，并检查三档尺寸、访问权限和断线后的本人会话恢复。

## WP06 · AI 打标与自由文本字段

没有新依赖。更新代码后执行全部 pytest、前端 check，再重启；启动自动应用最新 Alembic 迁移。dev 模式永远使用 FakeAiClient，不调用外部模型；prod 使用已保存的百炼兼容配置。设置页保存的配置优先于 config.yaml，生效于后续请求，在途请求继续使用原配置。

- 管理员进入“设置”，填写接口地址、API Key、识图/文本/向量模型、并发数、超时、重试和停滞分钟数。密钥留空保留旧值；写入 settings 的只有 Fernet 密文，读取只返回 api_key_set，表单提交后清空密钥输入。接口地址不接受内嵌凭据/查询参数；服务不跟随重定向。
- “测试连接”使用已保存配置，分别发一次最小识图、文本、向量请求，逐项显示成功/错误原文（凭据、图片、提示词脱敏）。向量只用于这一次连接测试，不写入向量库或进行向量业务。生产调用协议参考[百炼视觉兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-compatible-with-openai)、[百炼向量兼容接口](https://www.alibabacloud.com/help/en/model-studio/embedding-interfaces-compatible-with-openai)；模型和地区地址由管理员按实际账号填写。
- 管理→标签字段固定 11 项，只能更改填写说明和顺序。PUT 提交完整版本与各字段的 `key/hint/sort_order`，实际变更后递增版本。版本冲突返回 409。旧扩展字段和旧标签仍可追溯，不参与新打标。
- 字段为 category/material/color/style/festival/audience/usage_scene/size/craft/material_name/structure。明确无节日则留空。打标运行使用填写说明快照，新运行才读取最新配置。

上传完成后点“确认，开始 AI 处理”会进入 `/batches/{id}` 只读详情：先从企业标准名/别名对照表给出行业/企业位置建议，查不到或存在歧义就“位置待定”，不调用模型猜位置。对照表本包只建表与查找，没有管理/导入页面，生产映射维护留后续工作包；开发测试通过数据库种子验证命中。推荐路径为正式区内的中标状态/行业/企业相对目录，不执行建目录或入库。

所有单品先处理，全部结束后才派发套装；一次套装调用同时打标签与推荐关系。单品清单只含临时序号、名称、标签；额外元数据/停用字段不进入提示词。生产客户端不发送源文件名；已知编号、企业及别名、上传人账号、NAS配置会从候选文字/说明中去除，外发图像只取本地无EXIF且长边≤1024的JPEG。损坏或尚未恢复的送审图不能借系统身份读取NAS，也不会改用原图发送。

模板位于 backend/app/prompts/item_tagging.txt、set_tagging.txt，每次按固定字段逐项列出填写说明，要求仅输出JSON。解析保留 AI 原文。未知字段值置空并标记，不生成检索词项。多出的字段丢弃；terms 按顿号、逗号、分号、空格、斜线拆分，去空白和重复，不统一同义词。标黄只有字段 uncertain、一图 multi_object、套装 empty_relation 三种；不做词表、标签矛盾或取值检查。关系序号不在本批单品清单中的丢弃，合法关系存 batch_product_id/source=ai，relation_confirmed 保持false，需后续人工核对。

部分失败仍进“待核对”，全部失败或无主图进“AI 处理失败”。详情每秒更新完成数/失败数、位置、只读标签与关系，不含编辑。处理中或全部失败时可整批重试，进入核对后仅当前责任人可重试；新运行代际使旧在途结果失效，未开始的旧任务取消。发起人可填写原因并终止，保留临时文件并取消未执行的 AI 任务，已发请求的结果不再写入。主图视觉成功必须匹配当前完整 SHA-256，状态文字或人工标签不能替代。管理员不能借重试替换原上传人的NAS身份。

每次HTTP尝试（包括重试）记录模型、用途、毫秒耗时和token用量到应用日志及 ai_calls，不保存图片、提示词、源文件名或密钥。网络重试由客户端统一控制，默认额外3次，避免与队列层叠乘；JSON解析失败另外按同一预算重新请求。429/5xx/超时可重试，401等明确失败不盲目重试；业务调用不保存可能回显输入的厂商错误正文。每5秒检查无进展时间，超过阈值为本批次本次运行新增一条 ai_alerts，管理员在设置页查看；后续进展不会删除历史异常。

| 接口 | 权限/用途 |
|---|---|
| GET/PUT `/api/settings/ai` | 管理员读取/保存；PUT的api_key可省略或空串保留 |
| POST `/api/settings/ai/test` | 管理员，返回 `{results:[{kind,model,success,error}]}` |
| GET/PUT `/api/admin/tag-fields` | 管理员，完整版本化字段清单 |
| GET `/api/admin/ai-alerts` | 管理员停滞异常列表 |
| POST `/api/batches/{id}/retry-ai` | 处理中或 AI 失败时由责任人或管理员重跑，待核对时限责任人 |
| GET `/api/batches/{id}` | 本人/责任人/指定审核人/管理员，只读进度和商品结果 |

Fake触发词可用于主图文件名：`无法判断`/`uncertain`、`一图多品`/`multi`、`空关系`/`empty_relation`（套装）、`调用失败`/`fail`。普通名称返回确定的11字段与自由描述，多值terms可直接观察；名字只在假模型内存里用于选择场景。

默认自动化全部在 dev/MockTransport 跑，不需要百炼凭据。真实百炼验收另有两项 `bailian` 用例，必须由人在公司内网显式运行：先准备本机已获准发送的10张真实主图样本，不要指向NAS共享或映射盘；通过环境变量提供以下值，不将真实Key写入config示例、shell历史或报告：

- BAILIAN_TEST_API_KEY（推荐用Python getpass隐藏输入后置入当前进程环境）。
- BAILIAN_TEST_MODEL_VISION、BAILIAN_TEST_MODEL_TEXT、BAILIAN_TEST_MODEL_EMBED。
- 可选 BAILIAN_TEST_BASE_URL，默认百炼北京兼容地址；地区须与账号模型匹配。
- BAILIAN_TEST_IMAGES：本机10张样本目录。

```sh
python -m pytest backend/tests/bailian -m bailian -q -s --tb=short
```

缺环境变量会明确失败，不会假装通过。测试先用Pillow生成无EXIF的1024 JPEG再发，报告仅临时序号和festival/usage_scene值；不要加 `--showlocals`。人工抽看节日与使用场景是否区分，需调整的说明记入 QUESTIONS.md，再通过字段管理更新。显式测试会调用真实服务并产生实际用量；本次开发没有执行真实调用。

## WP07 · 上传人核对

从首页的「去核对」进入三栏工作区。编辑会自动保存。每个字段单独选择「确认具体值」或「确认无法判断（留空）」。套装单独确认所选成员或无关联单品。一图多品需要显式处理。页面顶部列出剩余事项。提交后重开页面可以查看本轮冻结的版本和备注。

批次接口均位于 `/api/batches/{id}` 下。写入者须为具有上传权限的当前责任人，状态须为「待核对」或「已驳回」。

| 方法与相对路径 | 行为 |
|---|---|
| GET /products | 返回商品、字段、关系、文件清单、待处理计数；提交后读取冻结版本 |
| PATCH /products/{pid} | 保存标签或描述草稿，按字段记录明确确认，处理一图多品 |
| POST /products/bulk | 对选择的商品批量改值，改动的字段回到待确认 |
| PUT /products/{pid}/relations | 保存关系，明确传 confirmed_members 或 confirmed_empty |
| POST /products/{pid}/retry-ai | 单图重新识别，保留人工改值 |
| POST /products、DELETE /products/{pid} | 追加主图、移至批次回收区；相关套装需复核 |
| POST /submit | 校验当前文件与草稿，原子保存本轮版本，进入待初审 |

`GET /api/tag-values` 提供当前有权查看批次的词项提示。正式商品库由 WP09 接入。管理员可通过 `PUT /api/admin/batches/{id}/owner` 在可编辑阶段交接责任人。追加主图的 multipart 大小上限由 `upload.append_max_mb` 控制，默认 50 MiB。更新后请按安装章节同步 Python 依赖。

## WP08 两级审核

上传人从核对页提交后，初审和终审在各自的待办队列领取批次，核对同一轮快照。每级通过前须勾完六项。驳回时填写总体意见，图片意见随该轮审核保存。责任人从首页打开已驳回批次，修改并重新提交后产生新轮次。

| 接口 | 用途 |
| --- | --- |
| `GET /api/review/queue?stage=first` 或 `final` | 对应阶段队列及等待时长 |
| `GET /api/review/{batch_id}` | 冻结版本、审批历史、领取状态和本次操作使用的 `version` |
| `POST /api/review/{batch_id}/claim`、`/release` | 领取、释放该阶段的审核 |
| `PUT /api/review/{batch_id}/pins`、`/path` | 暂存本轮意见、终审核定目录 |
| `POST /api/review/{batch_id}/pass`、`/reject` | 提交六项清单或总体驳回意见。驳回可同时提交最终钉点 |
| `GET/PUT /api/admin/company-map`、`GET /api/catalog/tree` | 管理员维护行业、标准名与别名，登录用户读取目录树 |
| `GET /api/todos` | 当前账号各类待办数和对应批次 |

审核写请求均携带最近一次读到的 `X-Review-Version`。发生版本冲突返回 409，重新读取后再核对。已有决定的审核记录、钉点与提交快照由 SQLite 触发器保护。

终审通过后，入库任务从「入库中」开始。供入库任务读取的数据包括同轮 `snapshot_id`、文件 SHA-256、终审 `reviewer_id` 和 `execution_session_id`、核定的 `batches.final_path` 及 `review_rounds.path_change.after` 中的企业和中标状态。该路径相对于配置的正式区域根目录。设备会话失效时需要终审本人重新认证。

## WP09 正式入库

在 `server.public_base_url` 配置公司 IT 分配的固定内网域名与端口。生产模式使用管理员已配置的百炼向量模型与密钥。供应商若在同一模型名下发布新版本，可用 `ai.model_embed_version` 标记版本。项目在当前正式模型下通过完整向量校验后发布，已有正式模型时改模型须经 WP11 的索引迁移。

终审用本人 NAS 账号核定目录并通过。审核页和批次详情页显示九步进度。入库异常时，原终审重新登录后在审核页点击「继续入库」。系统依照快照、文件 SHA-256、发号记录、正式档案和已有向量继续。若 SMB 拒绝同卷 rename，需由管理员核查 NAS 布局和权限。

| 接口 | 用途 |
| --- | --- |
| `GET /api/review/{batch_id}/ingest` | 相关提交人、审核人和管理员查看步骤、文件数和向量进度 |
| `POST /api/review/{batch_id}/resume-ingest` | 原终审使用当前设备的有效 NAS 会话继续入库 |
| `GET /l/{link_id}` | 以访问者 NAS 权限解析项目、商品或文件的稳定身份，返回 JSON |

升级生产前，应停服务并使用 SQLite 一致性快照备份 `app.db`，另备份现用 `config.yaml` 与 `secret.key`。可先用隔离副本运行应用迁移到 `0016_catalog`，检查原有 ID 和 `PRAGMA foreign_key_check`。如升级后尚未开始新入库，可停服务恢复升级前的备份。已开始新入库时，应一并保留新版 SQLite 和 NAS 档案，避免回滚后丢掉已预分配的永久链接。

## WP10 项目浏览、预览和下载

登录后，从「项目」按编号、名称或企业及别名查找，可用行业、中标状态和排序筛选。项目页展示单品、套装及五个资料夹，商品页展示 11 个已确认标签、主图和关系。永久链接使用 `server.public_base_url`，登录后打开原项目、当前商品或原文件身份。作废文件保留历史状态，已物理删除的文件显示状态说明。

- `GET /api/projects`、`GET /api/projects/{id}`、`GET /api/projects/{id}/files?category=…`、`GET /api/products/{id}` 提供正式目录视图。
- `GET /api/files/{id}/preview` 支持 JPEG、PNG，经当前设备的 NAS 会话鉴权。原图可单独打开，其他格式可下载。缩略图接口为 `GET /api/thumbs/{product_id}/{size}`。
- 下载使用 `GET /api/files/{id}/download`，支持单段、开放尾端和后缀 Range；范围无效返回 416 并带文件大小。发送前从同一 NAS 读句柄流式核验完整 SHA-256。文件大时需等待核验，服务端保持固定大小缓冲。
- 批量入口 `POST /api/files/download-many` 逐项返回通过校验的下载地址或失败原因。页面按顺序触发浏览器下载，显示服务端发送结果。首次批量操作时，浏览器可能提示允许此站点自动下载多个文件。
- `GET /api/l/{link_id}` 返回既有链接解析结构。旧 `/l/{link_id}` JSON 调用保留兼容，浏览器导航进入工作台页面。
- 检测到内容变化时，页面警告后可授权查看当前 NAS 内容。唯一身份确认后的物理路径校正、内容变化或无法唯一定位的文件会进入管理员待办。

迁移头为 `0017_asset_access`。升级生产前沿用 WP09 的停机与一致性快照备份流程，核验原项目、商品、文件与链接 ID 和 SQLite 外键后再恢复服务。

## WP11 商品与项目检索

搜索页支持商品编号、立项编号和自然语言，可按行业、企业或中标状态筛选。精确编号只查 SQLite。自然语言检索将确认字段的关键词召回与正式向量召回合并，可取消某个查询条件后重搜。解释、向量或排序不可用时，页面显示原因，并返回已核实的可访问结果。

- `POST /api/search` 查询，`POST /api/search/click` 记录点击。结果使用 WP10 页面和固定内网永久链接。
- 管理员在 AI 设置页查看正式、候选和可回滚索引。先构建并检查覆盖率，再切换。切换与回滚都会核对当前有效商品的模型身份、维度、文本哈希和覆盖率。
- 既有 SQLite 向量 BLOB 是数据源。常规安装包含 NumPy。若平台支持，可运行 `python -m pip install -e "./backend[vector]"` 启用 sqlite-vec，服务会优先使用已加载扩展。
- 首次正式入库使用已配置的初始向量模型。正式代次一旦建立，由索引指针决定当前查询和新项目的入库模型。升级时应使用供应商能够同时调用的确切模型 ID。自填版本用于识别代次，供应商实际接收的仍是模型 ID。
- 查询理解缓存包含查询文本、字段说明与版本、文本模型及 `ai.model_text_version`。企业标识、账号、NAS 路径和已知凭据在外发文本中剔除。

开发考卷可通过 `python -m pytest backend/tests/integration/test_hybrid_search.py::test_search_exam_demo -s` 重现。业务方填写真实题库后，使用 `python scripts/run_exam.py --url http://固定内网入口:8080 --user 验收账号 --exam 真实题库.yaml` 运行。脚本从终端读取密码，输出召回、Top 3、Top 10、平均/P95 耗时、调用和 token 统计。若提供每百万输入及输出 token 单价，则输出估算费用。

Alembic 头为 `0018_search`。生产升级前沿用一致性数据库快照流程，保留原向量及正式模型指针。
