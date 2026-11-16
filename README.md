# NAS 设计资产工作台

公司内网设计资产工作台。当前完成 WP00 探路和 WP01 可运行骨架；本包不包含登录、NAS 接入或上传。

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

已有 `config.yaml` 时保留现有文件，不重复复制。浏览器访问 <http://localhost:8080>，应看到中文占位页及服务版本、开发/生产模式、数据库状态。启动命令读取配置中的 `server.host` 和 `server.port`。默认监听 `0.0.0.0:8080`，仅本机开发时可改成 `127.0.0.1`。

可单独热更新前端：另开终端执行 `npm --prefix frontend run dev`，访问终端输出的 Vite 地址（默认 5173）；Vite 将 `/api` 请求代理给本机 8080 后端。

## 配置与持久化

- 默认读取当前工作目录的 `config.yaml`；`NASWB_CONFIG` 可指定其他文件。
- 优先级：环境变量 > YAML > 默认值。例如 `NASWB_MODE=dev`、`NASWB_SERVER__PORT=8081`；嵌套字段使用双下划线。
- 结构按架构 §12.1，增加 `mode: dev|prod` 与 `bootstrap_admin`；本包只加载管理员名称，不创建账号。缺失文件、未知字段或非法值会阻止启动。
- `paths.data_dir` 默认 `./data`；相对路径以配置文件所在目录为准。日志也位于该目录下的 `logs/`，每天滚动，保留 30 个归档。
- 首次启动自动生成 `data/secret.key`、通过 Alembic 创建 `data/app.db`。SQLite 启用 WAL、`busy_timeout=5000` 和外键检查。每次启动执行 `upgrade head`，重复启动不会重建已有表或密钥。
- 首个迁移仅包含 `users`、`sessions`、`settings`、`oplog`、`jobs` 及 Alembic 自身的版本表。`oplog` 数据库触发器禁止 UPDATE/DELETE。
- 密钥 POSIX 权限为 `0600`；Windows 使用受保护 DACL，仅当前服务身份、管理员组及 SYSTEM 可访问。应部署在支持权限控制的本地文件系统，权限设置失败会中止启动。Windows ACL 分支尚待在实际 Windows 环境验证。
- 密钥与数据库应一并备份；已有密钥无效时会报错，不自动覆盖。删除 `data/` 并重新启动会创建全新数据库与密钥，旧密钥加密的内容无法用新密钥解密。
- 普通 NAS 密码不写配置；`nas.system_password_enc` 和 `ai.api_key_enc` 只放密文。骨架仅加载这些字段，不连接 NAS 或百炼。

加密工具的使用（Python 代码，不要把真实凭据写入源码、命令历史或日志）：

```python
from app.config import load_settings
from app.services.crypto import SecretCipher

cipher = SecretCipher(load_settings().paths.data_dir)
# cipher.encrypt(内存中的字符串) -> 密文字符串
# cipher.decrypt(密文字符串) -> 内存中的明文
```

业务服务可用 `with database.session() as session:` 获取事务，再调用 `OplogService(session).write(...)`。`user` 为用户 ID 或 `None`，`object_id` 为字符串或 `None`，`before/after` 为可 JSON 序列化对象。日志写入只 flush，由调用方事务统一提交/回滚；本包无改变业务状态的 API。

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

`liquid-glass-react@1.1.1` 的 peerDependencies 要求 React/ReactDOM >=19，项目固定 React 18.3.1。为遵守技术栈，安装阶段通过项目级 `.npmrc` 的 `legacy-peer-deps=true` 保留 React 18，并记录 Q004。没有使用 `--force` 或升级 React。

当前首页经 `src/components/glass/` 统一封装，使用半透明样式；full/lite 偏好可切换、持久化，Safari/Firefox 默认 lite。**实时折射滤镜暂不启用，两档均使用兼容样式**，待后续兼容性处理。页面不直接导入玻璃库，不引入其他 UI 组件库。这个限制不影响 health 与骨架验收。

技术文档见 `docs/`，进度及手动验收记录见 `docs/PROGRESS.md`；NAS 探路详见 `scripts/probe/README.md`。
