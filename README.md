# NAS 设计资产工作台

公司内网设计资产工作台。当前完成 WP00 探路、WP01 骨架、WP02 NAS 基础模块和 WP03 登录/角色/界面框架；上传与批次仍为占位。

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
- 首个迁移仅包含 `users`、`sessions`、`settings`、`oplog`、`jobs` 及 Alembic 自身的版本表。`oplog` 数据库触发器禁止 UPDATE/DELETE。
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
- 每个 SmbFs 以本人凭据、私有缓存建立独立 SMB 连接；使用 NTLM 并要求签名。为了重连，密码只保留在活动实例的内存 SecretStr 中，不落盘/写日志，关闭或超时清除引用；Python 字符串不能保证物理内存擦除。DSM SID 不能替代 SMB 密码。
- 每次操作发现连接已断可先重连一次；安全的 `list/stat/exists/open_read` 建立句柄阶段遇到连接故障可重连重试一次。**已发送的变更、流内读写不自动重放**，否则可能重复追加或无法判断 rename 是否完成；活动流存在时不替换底层连接。关闭流后由上层核对大小/状态再按偏移恢复。
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
- `/api/auth/logout`：POST，仅作废本浏览器令牌；同账号多台电脑的令牌相互独立，共享按用户维护的 NAS 连接，最后一个有效令牌退出时关闭连接。应用进程重启后，旧 Cookie 因 NAS 会话不存在而要求重新登录。
- `/api/admin/roles`：GET 全部已登记用户，PUT JSON `{user_id,roles,enabled}` 更新一人；仅 admin 可用。角色仅四种、可多选：uploader、first_reviewer、final_reviewer、admin。初审/终审继承上传能力，admin 不自动继承审核权限。停用用户即撤销其全部令牌并关闭 NAS 会话；已登录请求每次重新检查数据库角色，授予角色后重新登录即可刷新导航。
- 管理员允许编辑自己的角色/启用状态，未增加文档没有规定的“最后一个管理员”业务限制；操作前请保留另一位可用管理员。bootstrap_admin 不会在每次登录时重新加权，重新启用需由其他管理员操作。
- 工作台角色不能授予 NAS ACL。dev 模式从 dev_users 模拟权限，prod 仍由本人 SMB 连接和群晖 ACL 实际执行文件权限。
- 上传、待初审、待终审、项目、搜索、设置为建设中占位；首页不伪造批次和待办数据。管理页可维护角色。组件样例 `/components` 仅 dev 模式显示，prod 直接访问跳回首页。
- 五种玻璃组件统一位于 `src/components/glass/`：Card、Button、Panel、Modal、Nav；GlassProvider/useGlassMode 提供全局两档。full 全部实时，lite 主要壳保留玻璃、卡片/次要按钮使用半透明和模糊。60 卡片网格仅供开发比较流畅度，没有宣称达到某个 FPS 指标。
- 退出或任一 API 返回 401 时清除内存用户及角色缓存，守卫跳转登录；令牌、用户名密码不写 localStorage（只保存视觉偏好）。表单提交后清空密码字段；活动 SMB 内存凭据生命周期仍沿用 WP02，不落盘或写日志。

手动验证：

1. 浏览器打开 <http://localhost:8080>，使用 demo-uploader 登录一次再退出；用错误密码重试应提示失败。退出后访问 `/projects` 或 `/admin/roles` 应进入登录页。
2. 用 demo-admin 登录，管理 → 角色表，给 demo-uploader 勾选初审并保存；对方重新登录后应出现“待初审”。普通账号直接请求 GET/PUT `/api/admin/roles` 返回 403。
3. 在组件样例页打开弹窗（Escape/关闭按钮可关闭）、切换 full/lite、滚动 60 张卡片并刷新，检查偏好保持与文字/按钮清晰可用。
4. 测试令牌和 NAS 超时的自动化用例位于 `backend/tests/integration/test_auth_roles.py`；默认测试无真实 NAS 依赖。
5. 内网 prod 验证：配置真实 NAS 主机、共享及区域根、受信 CA 与 bootstrap_admin，`mode: prod` 后重启；在登录页输入本人真实 NAS 凭据，应完成 DSM 验证和本人 SMB 建连。本包未在真实 NAS 上执行此项，不能把 dev 测试视作通过。
