# NAS 设计资产工作台 · 开发约定

## 项目是什么
公司内网的设计资产管理工作台：业务人员用浏览器把整理好的项目文件夹上传到群晖 NAS，
AI 给商品主图打标签，经上传人核对、初审、终审后移入正式目录，之后可以搜索和下载。
完整设计在 docs/ 下四份文档里，它们是唯一的需求来源。

## 文档优先级
1. docs/02-入库流程.md —— 业务规则以它为准
2. docs/01-命名规范.md —— 目录、命名、预检规则以它为准
3. docs/03-技术架构.md —— 技术实现以它为准（第 18 节已被 docs 之外的分阶段提示词取代）
4. docs/04-检索方案.md —— 检索思路；其中与 1～3 冲突的旧内容（NAS 只读、一图一 TXT、用型号做编号）一律作废
文档之间冲突时按上面顺序取舍，并把冲突点记到 docs/QUESTIONS.md。

## 工作方式
- 每次会话只做一个工作包（WP）。只做该包「要做」里的事，「不要做」里的事一律不碰。
- 开始前先读 docs/PROGRESS.md 了解已完成的内容，再读提示词指定的文档章节。
- 不修改 docs/ 下的四份设计文档。发现文档有疑问或缺口：写进 docs/QUESTIONS.md，
  按最保守、最容易改的方式实现，不要停下来等待，也不要自行发明业务规则。
- 完成后：跑通全部自动化测试和前端构建；更新 docs/PROGRESS.md（本包完成了什么、
  有哪些已知限制、怎么手动验收）；提交 git，提交信息格式「WPxx: 一句话说明」。

## 技术栈（不要更换）
后端：Python 3.12、FastAPI、Uvicorn、SQLAlchemy 2.x、Alembic、SQLite（WAL）、
      smbprotocol、httpx、Pillow、APScheduler、pydantic-settings、cryptography、pytest
前端：React 18、TypeScript、Vite、Tailwind CSS、liquid-glass-react、react-router、
      TanStack Query、zustand、react-hook-form、vitest
不引入 Redis、Celery、Docker、其他数据库、其他 UI 组件库。需要新依赖时在 PROGRESS.md 里说明理由。

## 目录结构
按 docs/03-技术架构.md 第 15 节。后端模块放 backend/app/services/<模块名>/，
路由放 backend/app/api/，前端页面放 frontend/src/pages/。

## 三种运行模式（config.yaml 的 mode 字段）
- dev：登录用配置文件里的假账号；NAS 用本地文件夹模拟（LocalFs，按角色模拟读写权限）；
       AI 用固定返回的假模型。所有自动化测试跑在 dev 模式，不需要任何外部系统。
- prod：真实群晖 DSM 登录、真实 SMB、真实百炼。
- 需要真实 NAS 的测试标记 @pytest.mark.nas，需要真实百炼的标记 @pytest.mark.bailian，
  默认跳过，由人在公司内网手动运行。

## 硬性规则
- 一切 NAS 文件操作必须通过 NasFs 接口，用「当前操作人本人」的会话执行；
  不允许用系统账号替用户写文件。系统账号只用于巡检（只读）和备份。
- 正式目录 /设计资产/ 只有终审的会话能写。代码里不要绕过。
- NAS 密码不落盘、不进日志、不进数据库。唯一落盘的凭据是系统账号密码和百炼 API Key，必须加密。
- 所有 NAS 路径由后端根据 ID 拼接，接口不接受前端传来的完整路径。
- 业务操作日志 oplog 只追加：代码里不写 UPDATE/DELETE，并用数据库触发器禁止。
- 发给外部模型的只有主图的缩小图（长边 1024、去 EXIF）和标签文字；
  不发 NAS 路径、账号、立项编号、企业名、资料夹里的任何文件。
- 每个改变状态的接口都要写 oplog，并做角色检查。

## 代码风格
- 标识符用英文；界面文字、错误提示、日志内容、注释用中文。
- 后端：类型标注齐全；接口的请求和响应用 Pydantic 模型；业务逻辑在 services，路由里不写业务。
- 前端：函数组件 + hooks；接口调用统一放 src/api；不用 any。
- 状态机的状态名、表名、字段名严格使用 docs/03-技术架构.md 第 6 节里的名字。
- 每个服务模块要有单元测试；每个接口至少一条成功和一条失败的集成测试。

## 界面风格
- 液态玻璃：统一通过 src/components/glass/ 里的封装组件使用 liquid-glass-react，页面里不直接引库。
- 全局视觉模式两档：full（所有玻璃组件启用实时滤镜）/ lite（只有导航、卡片容器、弹窗、
  主要按钮用玻璃，列表项和缩略图用 Tailwind 半透明样式）。存 localStorage，右上角一键切换；
  Safari 和 Firefox 默认 lite。
- 中文界面，桌面浏览器优先（Chrome / Edge），不做移动端适配。