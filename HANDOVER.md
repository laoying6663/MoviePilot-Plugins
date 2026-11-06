# DynamicWeChatSelfUse v4.0.0 —— 交接文档

> 面向接手继续开发的开发者。读完本文即可独立开工，不需要回看原始对话。
> 文档对应代码版本：`plugin_version = "4.0.0"`，插件 ID `DynamicWeChatSelfUse`。
>
> **路径变更说明**：v4.0 整合后，原先的 `plugins.v3/wecomnotify/HANDOVER.md`
> 已随独立插件目录一并移除，重写后的交接文档移到仓库根目录 `HANDOVER.md`。

---

## 1. 状态快照

| 项目 | 状态 |
| --- | --- |
| 仓库 | `laoying6663/MoviePilot-Plugins`（个人 fork，插件市场数据源） |
| 分支 | PR [#1](https://github.com/laoying6663/MoviePilot-Plugins/pull/1) 已 **merged / closed**（merge commit `4ce76d1d78e5c0cfa66be0611fac9fedc14dd254`）；`main` 已包含 v4.0.0 |
| 插件 ID | **`DynamicWeChatSelfUse`**，目录 `plugins.v3/dynamicwechatselfuse/` |
| 版本 | **4.0.0**（`plugin_version` 与 `package.v3.json` 一致） |
| 需求变更 | PR #1 原本是「新增独立 `WecomNotify` 插件」。用户确认真实目标是**把企微通知能力并入已安装的 `DynamicWeChatSelfUse`**，并保留其动态可信 IP / CookieCloud 保活 / 扫码等全部旧功能。独立 `WecomNotify` 不再发布 |
| 配置兼容性 | 插件 ID 与 `plugin_config_prefix = "dynamicwechatselfuse_"` **未改变**，数据目录 `/config/plugins/DynamicWeChatSelfUse/` 原样保留，升级不丢原有应用ID、Cookie 与通知渠道配置 |
| 离线自测 | `pytest tests/test_wecomnotify.py` → **80 passed**；`python tests/test_wecomnotify.py` → **通过 80 / 80**（0 FAIL，退出码 0） |
| 静态检查 | `py_compile` 通过、`pyflakes` 零告警（整目录含存量 `helper.py` + 测试文件） |
| 真实宿主基线 | MoviePilot v3.2.4 / Python 3.14.7 / Docker（fnOS NAS，主机名 `moviepilot-v3`）。升级前 `plugin.runtime.status`：`ready=true`、generation 74、pending 0、failed 0 |
| 真实宿主部署 | PR #1 合并后 `plugin.install`（`force=true`）从 **3.2.0 → 4.0.0** 成功，`restart_required=false`；generation 74 → 83，全程 `ready=true` / pending 0 / failed 0 |
| 配置兼容性实证 | 升级前后逐字段 SHA256 比对：原有 **13 个配置字段一个未丢、值全部 SAME**；新增 11 个企微字段；命令 2 → 4 条且无重复 |
| 宿主验收 | 第 5.2 节 **1–5 条已完成**（详见第 5.3 节）；第 6–9 条真机企业微信联调**未做** |
| **未验证** | 真机企业微信联调（第 5.2 节 6–9 条）；第 3 条因宿主 MCP 无日志读取入口，只做到「DEBUG 级重载零异常 + 静态导入零违例」，未逐行核对日志文本 |

### 已交付文件

| 文件 | 职责 |
| --- | --- |
| `plugins.v3/dynamicwechatselfuse/__init__.py` | 宿主插件类：动态可信 IP / CookieCloud / 扫码 / 验证码（存量）+ v4.0 合并后的契约方法、事件转发、表单、页面、命令、API、配置落库、停止逻辑 |
| `plugins.v3/dynamicwechatselfuse/wecom_notify.py` | 新增的 `WecomNotifyMixin`：通知推送、回调验签与解密、远程命令、菜单、配置表单、详情页、状态 API。**不含任何 `@eventmanager.register`** |
| `plugins.v3/dynamicwechatselfuse/client.py` | 企业微信 API 客户端：token 缓存、2048 字节分块、菜单、素材下载 |
| `plugins.v3/dynamicwechatselfuse/crypt.py` | 回调加解密：SHA1 签名 + AES-256-CBC。**零宿主依赖**，可脱离宿主单测 |
| `plugins.v3/dynamicwechatselfuse/helper.py` | 存量：CookieCloud、第三方通知发送器、IP 解析、配置持久化 |
| `tests/test_wecomnotify.py` | 离线自测，pytest / 独立脚本双模；已迁移到 `dynamicwechatselfuse` 包并补充合并契约的回归用例 |
| `package.v3.json` | 仅 `DynamicWeChatSelfUse`(4.0.0) 与 `ParkingBot` 两条；`WecomNotify` 条目已删除 |
| `README.md` | 用户文档：两套能力、配置字段、命令、回调 URL、升级说明 |
| `HANDOVER.md` | 本文件 |

### 已删除

- `plugins.v3/wecomnotify/` **整个目录**（`__init__.py` / `client.py` / `crypt.py` / `README.md` / `HANDOVER.md`）。
  代码已迁入 `dynamicwechatselfuse/`，保留独立目录会造成重复实现与市场条目冲突。

### 尚未实现（明确不在本次范围）

- **企业微信智能机器人长连接**（`dmPolicy=open` / `groupPolicy=disabled`）。走智能机器人 WebSocket
  长连接，与自建应用推送是两套完全不同的 API。宿主内置实现在 `app/modules/wechat/wechatbot.py`。
- 图片 / 文件消息的素材下载回传，语音消息的 AMR 转码。

---

## 2. 权威数据源

### 2.1 宿主开发合同（改动前必读）

| 内容 | 位置 |
| --- | --- |
| 插件开发总合同（V3） | `jxxghp/MoviePilot-Plugins` → `docs/Plugin_Development.md` |
| 通知渠道扩展 FAQ | 同上 → `docs/faq/01-extend-notification-channel.md` |
| 仓库与发布指南 | 同上 → `docs/Repository_Guide.md` |
| Wiki 入口 | <https://wiki.movie-pilot.org/zh/plugindev> |
| 通知渠道配置说明 | <https://wiki.movie-pilot.org/zh/notification> |

### 2.2 宿主源码（分支 `v3`，即 `jxxghp/MoviePilot`）

| 路径 | 为什么重要 |
| --- | --- |
| `app/schemas/types.py` | `NotificationChannel` / `MessageType` / `EventType` 三个枚举的权威定义。配置表单的「类型」下拉直接来自 `MessageType` |
| `app/sdk/plugin/base.py` | `_PluginBase` 契约。确认 `self.eventmanager`、`self.chain`、`self.systemmessage`、`update_config()`、`get_data_path()` 的存在与签名 |
| `app/sdk/network.py` | 插件可用的 HTTP 出口：`RequestUtils`（同步）、`AsyncRequestUtils`（异步）、`UrlUtils` |
| `app/api/endpoints/message.py` | 宿主内置回调端点 `/api/v1/message/`，插件回调是它的插件版复刻 |
| `app/modules/wechat/__init__.py` | 宿主内置企业微信模块：消息解析、管理员校验、菜单注册 |
| `app/modules/wechat/wechat.py` | 宿主内置客户端：token 缓存、发送分块、菜单构建 |
| `app/adapters/external/wechat.py` | `WXBizMsgCrypt`（腾讯官方算法）。**插件不能导入它**，`crypt.py` 是按公开算法重写的独立实现 |

### 2.3 本仓库约定（与官方指南不同，以本仓库为准）

- **测试目录是平铺的**：`tests/test_*.py`，不是官方指南的 `tests/v3/<plugin_id>/`
- **测试是独立脚本风格**：自带 `PASS`/`FAIL` 计数，`sys.exit(1 if FAIL else 0)`
- **既有测试会在模块导入期执行副作用**（含 `sys.exit`），因此**不要**对整个 `tests/` 目录跑 `pytest`，只跑单个文件
- 既有测试通过 `exec(compile(src, ...))` 复用其它文件的桩，这是本仓库的惯用手法
- 仓库根存在硬编码本地路径（作者本地工作副本），属既有技术债，新增测试**不要**沿用
- **新增代码只用 `app.sdk.*` 稳定出口**；但 `DynamicWeChatSelfUse` 是存量插件，已存在的
  `app.adapters.external.cookiecloud`、`app.schemas.types`、`httpx2`、`pytz`、`cloakbrowser`
  依赖属于**存量例外**，见第 6 节红线 3

---

## 3. 机制说明

### 3.0 整体结构（v4.0 合并架构）

```
DynamicWeChatSelfUse(WecomNotifyMixin, _PluginBase)
  ├─ 存量能力（__init__.py）：动态可信 IP / Cookie 持久化 / CookieCloud / 扫码 / 验证码
  └─ 并入能力（wecom_notify.py mixin）：通知推送 / 回调 / 远程命令 / 菜单 / 状态面板
```

- **MRO 顺序**：`WecomNotifyMixin` 在 `_PluginBase` 之前，宿主类自己定义的方法优先。
- **事件注册只在宿主类**：mixin 里一个 `@eventmanager.register` 都没有。宿主类显式转发
  `NoticeMessage` → `WecomNotifyMixin.notice_message(self, event)`、
  `PluginAction` → `WecomNotifyMixin.plugin_action(self, event)`。
  若把装饰器留在 mixin 里，继承后宿主类会重复注册同一处理器。
- **插件 ID 由类名推导**：回调路径、`add_plugin_once_job` 的 job 前缀、状态面板路径都用
  `self.__class__.__name__`，因此合并后回调地址自动变为
  `/api/v1/plugin/DynamicWeChatSelfUse/wecom`（不再是 `WecomNotify`）。
- **配置合并**：`get_form()` 把 `_wecom_form()` 的表单节点接在旧表单后面，
  用 `_drop_shared_form_fields()` 递归剔除通知表单里重复的 `enabled` / `onlyonce`，
  默认模型也先 `pop` 这两个键再 `update`，保证只有一个开关、一个「立即运行一次」。
- **命令合并**：`get_command()` = 旧命令 `/push_qr`、`/wxcode` + `_wecom_commands()`
  里排除这两条后的 `/wecom_status`、`/wecom_test`。`/help` 与菜单都从这张表派生，
  新增命令只改一处。菜单只收 `category` 非空的命令，所以一级分组是「企业微信通知」。
- **页面合并**：`get_page()` = 旧二维码/Cookie 面板 `base_content + self._wecom_page()`。
- **API**：`get_api()` 直接返回 `self._wecom_api()`，注册 `/wecom`(GET 验证 / POST 消息，
  `auth: none`) 与 `/status`(GET，`auth: bear`)。

### 3.1 生命周期顺序（**最容易踩坏的一段**）

```
init_plugin(config)
  ├─ 读旧配置 → _enabled / _cron / _input_id_list / ...
  ├─ stop_service()                    ← 内部会 _stop_wecom()，把 _client 置 None
  │    └─ 若返回 False（后台任务没退干净）：
  │         _load_wecom_config(config) ← 只填字段、不建客户端、不联网
  │         __update_config()          ← 整体覆盖，必须先把企微凭据读回，否则抹成空
  │         return
  ├─ _init_wecom(config)               ← 必须在 stop_service() 之后！
  │    ├─ _load_wecom_config(config)
  │    ├─ 重建 WecomClient（每次重建，避免用旧代理/旧密钥）
  │    └─ 已启用且凭据完整 → get_access_token() 校验 + _sync_menus()
  ├─ _load_persisted_cookie() / _load_cookie_checked_at()
  ├─ 配置检查（未填应用ID 则 __update_config() 后 return）
  ├─ _start_background_loops() + _handle_once_tasks()
  └─ __update_config()
```

`_init_wecom()` 放在 `stop_service()` **之前**会立刻被 `_stop_wecom()` 释放，客户端永远为 None。
`_load_wecom_config()` 与 `_init_wecom()` 拆开，就是为了让提前 return 分支能安全地只补字段。

### 3.2 配置落库（`update_config()` 是整体覆盖）

`__update_config()` 必须显式写回**所有**字段，包括 `name` / `types` / `corpid` / `agentid` /
`secret` / `proxy` / `token` / `encoding_aes_key` / `userid` / `admins` / `create_menu`。
漏一个，用户每次保存配置或重载插件都会把那一项抹成默认值。
`admins` 落库为逗号分隔字符串，`_load_wecom_config()` 同时兼容字符串与列表。

### 3.3 `onlyonce` 的双语义

旧语义是「立即跑一次动态可信 IP 检查」；v4.0 里 `_handle_once_tasks()` 在同一个开关下
额外调用 `schedule_wecom_test_once()` 排一次企微通知连通性测试。
`_client.is_configured()` 为假时直接跳过，避免只开 IP 保活的老用户看到无意义的失败日志。

### 3.4 推送链路（宿主 → 企业微信）

```
宿主 post_message()
  └─ EventType.NoticeMessage 广播
       └─ 宿主类 wecom_notice_message() → WecomNotifyMixin.notice_message()
            ├─ 按 self._types 过滤消息类型
            ├─ 接收人：通知自带 userid 优先，否则用配置的 userid（空 = @all）
            └─ client.send_msg() → asyncio.to_thread → cgi-bin/message/send
```

插件**不走** `post_message()` 回复（会回到宿主渠道造成回环），而是自己调企微 API。
发送走 `asyncio.to_thread`，因为用的是同步 `RequestUtils`。

### 3.5 回调链路（企业微信 → 宿主）

```
企业微信 GET  /api/v1/plugin/DynamicWeChatSelfUse/wecom?msg_signature=&...&echostr=
  └─ wecom_verify()：SHA1 签名 → AES 解密 echostr → 校验内嵌企业ID → 返回明文

企业微信 POST /api/v1/plugin/DynamicWeChatSelfUse/wecom?msg_signature=&...
  └─ wecom_message()：验签 → AES 解密 → 取明文 XML → parse_xml_message()
       ├─ 文本 / 菜单点击 → 命令；语音 → 识别结果；图片/文件 → 回「暂不支持」
       ├─ 斜杠命令 → 管理员白名单 → _dispatch_command() → EventType.PluginAction
       ├─ 非命令消息 → EventType.UserMessage 广播（旧插件的验证码 talk 也监听它）
       └─ 始终返回 "success"（企业微信要求 5 秒内响应）
```

- 回调接口 `auth: "none"`，安全性由企业微信 SHA1 签名承担，**不要改成需要登录**。
- 消息处理包在 `asyncio.wait_for(..., timeout=20)` 里，超时不拖垮回调。
- `_dispatch_command()` 用 `self.get_command()` 作为唯一命令源；发 `/push_qr` 会落到
  旧插件的 `push_qr_code_event`，发 `/wxcode` 落到 `dynamicwechatselfuse_event`。

### 3.6 加解密结构（`crypt.py`）

```
明文 = random(16) + struct.pack(">I", len(msg)) + msg + receiveid
密文 = AES-256-CBC(key=EncodingAESKey 解出的 32 字节, iv=key[:16], PKCS7 补位到 32 倍数)
密文传输 = base64(密文)
签名 = SHA1("".join(sorted([token, timestamp, nonce, encrypt])))
```

回调报文外层是 `<xml><Encrypt><![CDATA[...]]></Encrypt></xml>`。

---

## 4. 标准流程

### 4.1 环境准备

```bash
# 本机没有 git（本项目因此全程走 GitHub App connector），用任何 git 客户端均可
git clone https://github.com/laoying6663/MoviePilot-Plugins.git
cd MoviePilot-Plugins
git checkout feat/wecom-notify

# 离线自测依赖装到独立目录，运行时用 PYTHONPATH 引入，不污染宿主环境
pip install --target ./_pylibs pytest pyflakes pycryptodome
```

- 出网走 `http_proxy` 环境变量指定的代理（**端口会变，用 `env | grep -i proxy` 查，不要写死**）。
- `codeload.github.com` 可用 `curl --noproxy '*'` 直连下载源码包。
- PowerShell 下调用原生命令传 JSON 参数会被剥掉内层双引号，改用 `--args-file <文件路径>`。

### 4.2 改代码 → 自测 → 提交

```bash
REPO=.  # 仓库根

# 1) 语法（整目录，含存量 helper.py）
python -m py_compile plugins.v3/dynamicwechatselfuse/*.py

# 2) 静态检查：零告警
PYTHONPATH=./_pylibs python -m pyflakes plugins.v3/dynamicwechatselfuse/*.py tests/test_wecomnotify.py

# 3) 离线自测（两种跑法都必须过）
PYTHONPATH=./_pylibs pytest tests/test_wecomnotify.py -q
PYTHONPATH=./_pylibs python tests/test_wecomnotify.py

# 4) 版本一致性
#    plugins.v3/dynamicwechatselfuse/__init__.py 的 plugin_version
#    必须等于 package.v3.json 的 DynamicWeChatSelfUse.version
python -c "import json;print(json.load(open('package.v3.json',encoding='utf-8'))['DynamicWeChatSelfUse']['version'])"
```

提交信息沿用仓库风格：

```
feat: <一句话说明>

<正文：改了什么、为什么、怎么验证的>
```

### 4.3 提 PR

分支命名 `feat/<主题>` 或 `fix/<主题>`，base 固定 `main`。
PR 正文写清：背景（并入旧插件而非新增独立插件）、改动、测试结果、**未包含的能力**、
以及第 5.2 节真机验收的完成情况。

---

## 5. 判据与验收标准

### 5.1 提交前必须全绿

| 判据 | 判据落地在哪 | 不满足的后果 |
| --- | --- | --- |
| `pytest tests/test_wecomnotify.py` 全绿 | pytest 退出码 0 | 回归 |
| `python tests/test_wecomnotify.py` 报 `通过 N / N` 且 N > 0 | 脚本退出码 0 | 假绿 |
| `py_compile` 通过 | 退出码 0 | 宿主加载失败 |
| `pyflakes` 零输出（整目录 + 测试文件） | 退出码 0 | 未定义名 / 未用导入 |
| `plugin_version` == `package.v3.json` 的 `DynamicWeChatSelfUse.version` | 人工核对 + 测试断言 | 市场页版本错乱 |
| `package.v3.json` 只有 `DynamicWeChatSelfUse` / `ParkingBot` 两条，其它条目未被改动 | `git diff` | 别人的插件被误改 |
| 插件 ID / `plugin_config_prefix` 未改变 | 人工核对 + 测试断言 | **用户配置与 Cookie 全部丢失** |
| `__update_config()` 写回全部企微字段 | 测试断言 | **每次保存/重载抹掉凭据** |
| 没有真实凭据进仓库 | `git diff` 全文搜 Secret / Token 值 | **安全事故** |

> ⚠️ 独立脚本模式下，如果一个用例都没跑到，脚本必须**判失败**而不是报「通过 0 / 0」。
> 这条已内置（见 `test_wecomnotify.py` 的 `_run_standalone`），改动它时不要移除。

### 5.2 真实宿主验收（1–5 条已完成，6–9 条待真机配合）

在真实 MoviePilot V3 宿主上逐条确认。第 1–5 条可由宿主 MCP / 文件系统完成，
第 6–9 条需要用户配合企业微信真机操作：

1. 插件市场 / 插件源能发现并更新到 `DynamicWeChatSelfUse` **4.0.0**；索引里不再有 `WecomNotify`
2. 插件启动 / 禁用 / 重载都不残留后台资源（重点看 `_client`、后台任务、scheduler once job）
3. `DEBUG=true` 时**没有**旧导入兼容警告（存量非 `app.sdk.*` 依赖见红线 3 的例外清单）
4. API、服务、命令只注册一次：`/wecom`(GET/POST)、`/status`(GET) 各一次；
   `get_command()` 返回 4 条且无重复（另有 `/help` 由分发器特殊处理，不在注册表内）；
   宿主类 5 个事件处理器（3 个 `PluginAction` / 1 个 `NoticeMessage` / 1 个 `UserMessage`）均只注册一次
5. 配置页两组字段渲染正常且与 README 一致；**原有应用ID / Cookie / 通知渠道配置未丢失**
6. 填入真实企业微信参数后，`/wecom_test` 能收到消息
7. 企业微信「接收消息」页保存 `/api/v1/plugin/DynamicWeChatSelfUse/wecom` 成功（回调验签通过）
8. 在企业微信里发 `/help`、`/wecom_status`、`/push_qr`，能收到回复
9. 企业微信应用菜单出现「企业微信通知」分组

> 每次推完版本都要提醒用户 **强制刷新（Ctrl+F5）+ 重载插件**，否则大半「功能没生效」
> 其实是在报浏览器 / 插件实例缓存。

### 5.3 v4.0.0 真实宿主验收记录

宿主：MoviePilot v3.2.4 / `moviepilot-v3` / Docker（fnOS NAS）。PR #1 合并到 `main` 后，
通过宿主 MCP 完成升级与第 1–5 条验收。

| 条目 | 结果 | 实测证据 |
| --- | --- | --- |
| 1 市场发现 v4.0.0 / 索引无 `WecomNotify` | ✅ | `plugin.market`（`force=true`）返回 `plugin_version=4.0.0`、`has_update=true`、`update_candidate.version=4.0.0`；查询 `WecomNotify` 返回 **0 条**；`plugin.install` 升级成功，`plugin_version` 3.2.0 → 4.0.0 |
| 2 启停重载不残留 | ✅ | 禁用 → 命令 4 → **0**；启用 → 4；重载 → 4。`scheduler.list` 总数全程 **31 不变**；`plugin.runtime.status` 全程 `ready=true` / pending 0 / failed 0 / `restart_required=[]` |
| 3 DEBUG 无旧导入兼容警告 | ⚠️ 部分 | 设 DEBUG 后重载：`failed=0`、命令仍 4 条，日志等级已恢复 INFO；新增 `client.py` / `crypt.py` / `wecom_notify.py` 静态扫描 **0 处** `app.adapters.*` / `app.core.*` 等禁用导入。**宿主 MCP 白名单没有日志读取入口，未能逐行核对运行时日志文本** |
| 4 API / 服务 / 命令单次注册 | ✅ | `plugin.capabilities` 4 条命令唯一；`slash.list` 全宿主 23 条命令**零重复**；`actions` / `services` 均为 0（符合设计）；HTTP 匿名探测 `/wecom` 与 `/status` 均返回 401 而非 404（路由已注册），旧 `WecomNotify/wecom` 返回 **404**（无残留） |
| 5 配置页两组字段 / 旧配置未丢 | ✅ | `plugin.config.get` 表单 22 个 `model` **22 unique 无重复**，`enabled` / `onlyonce` 各 1 个，旧组与企微组字段齐全；升级前后 `model` 逐字段 SHA256 比对：原有 **13 个字段一个未丢、值全部 SAME** |

**升级前基线**：v3.2.0、`state=true` / `runtime_status=active`、generation 74、13 个配置字段、
命令 `/push_qr` `/wxcode`。
**升级后**：v4.0.0、generation 76 → 83、24 个配置字段（新增 11 个企微字段）、
命令 `/push_qr` `/wxcode` `/wecom_status` `/wecom_test`。

> ⚠️ **宿主全局 apikey 拦截（影响第 7 条）**：匿名 GET
> `/api/v1/plugin/DynamicWeChatSelfUse/wecom` 返回 `401 apikey 校验不通过`；附加企微验签参数
> 与 `?apikey=` 均被同一中间件拦下。插件已按红线 10 声明 `auth: "none"`，但该校验发生在
> 宿主全局层、先于插件路由。企业微信后台保存回调 URL 时需按宿主规则附带有效 apikey 或在
> 宿主侧放行该路径，并在真机第 7 条重点验证。

> 第 6–9 条（`/wecom_test` 收消息、回调 URL 保存、企微内命令回复、应用菜单）仍需用户配合
> 企业微信真机操作，**尚未执行**。

---

## 6. 红线清单

违反其中任何一条都会造成实际损失：

1. **绝不把真实凭据提交进仓库。** 本次开发过程中，用户截图里带有真实的企业ID /
   应用Secret / Token / EncodingAESKey / 管理员白名单。仓库里只允许出现占位符与示例值，
   配置一律走插件配置页。*另：这些凭据已在对话与截图中明文出现，建议在企业微信后台轮换一次。*
2. **插件不能注册新的 `NotificationChannel`。** 那是宿主的封闭枚举，改不了。
   不要试图 monkey-patch `app.schemas.types`，也不要 import `app.domain.*` 去改枚举。
3. **新增代码只用 `app.sdk.*` 稳定出口。** 禁止 `app.adapters.*`、`app.application.*`、
   `app.domain.*`、`app.foundation.*`、`app.runtime.*`、`app.core.*`、`app.helper.*`、
   `app.utils.*`。
   **存量例外（仅限 `DynamicWeChatSelfUse` 已有代码，新增代码不得沿用）**：
   `app.adapters.external.cookiecloud`（CookieCloud 保活）、`app.schemas.types`（枚举，
   V3 中仍是 canonical 路径）、`httpx2`、`pytz`、`cloakbrowser`。
   新并入的 `client.py` / `crypt.py` / `wecom_notify.py` 已全部按 `app.sdk.*` 实现，
   `crypt.py` 更是零宿主依赖。
4. **测试不进 `plugins.v3/`。** 否则市场同步会把测试一起复制到运行目录。
5. **不自建 `BackgroundScheduler`。** 一次性任务用
   `app.sdk.scheduler.add_plugin_once_job()`；周期任务用 `get_service()`。
6. **导入期不启任务、不联网、不连数据库。** 这些只能放在 `init_plugin()` 里。
7. **`init_plugin()` 必须可重复调用**，`stop_service()` 必须可重复且安全释放。
   `_init_wecom()` 必须在 `stop_service()` 之后调用。
8. **运行数据不写插件源码目录。** 用 `save_data()` / `get_data_path()`。
9. **不自建哈希表 / 全局单例存可变状态**，虚拟分身会互相污染。
10. **回调接口保持 `auth: "none"`**，靠企业微信签名做鉴权。改成需要登录会让
    企业微信回调直接失败。
11. **插件 ID 与配置前缀不得改变**：必须是 `DynamicWeChatSelfUse` /
    `dynamicwechatselfuse_`。改了就等于换了一个插件，用户已有的配置、Cookie、
    通知渠道会全部失联（v3.2.0 已经因为改 ID 让用户重扫过一次码）。
12. **`__update_config()` 是整体覆盖语义，必须写回全部字段。** 每新增一个配置字段，
    都要同步在 `__update_config()` 里加一行，并补一条回归测试。
13. **事件装饰器只能写在宿主类上。** mixin 里出现 `@eventmanager.register` 会导致
    继承后重复注册；新增事件一律通过宿主类显式转发。

---

## 7. 踩坑档案（按症状索引）

### 7.1 长文本切分报 `IndexError: index out of range`

**症状**：`client.py` 的 `_split_content()` 在切长行时崩。
**根因**：回退到字符边界的循环写成了无条件执行，当 `end == line_length`（本行放得下）时
`encoded[end]` 越界。
**修法**：边界回退只在 `end < line_length` 时做：

```python
end = start + max_bytes
if end >= line_length:
    end = line_length
else:
    while end > start and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    if end == start:
        end = start + 1
        while end < line_length and (encoded[end] & 0xC0) == 0x80:
            end += 1
```

**回归点**：`test_split_content_respects_byte_limit`、`test_split_content_does_not_corrupt_multibyte_chars`

---

### 7.2 创建菜单报 `KeyError: 'agentid'`

**症状**：`create_menus()` 一调用就抛 `KeyError: 'agentid'`。
**根因**：菜单 URL 模板含 `{access_token}` 和 `{agentid}` 两个占位符，但 `_post()` 只填了前者。
**修法**：`create_menus()` 里先填 agentid，把 `{access_token}` 留给 `_post()`：

```python
url_template = self._create_menu_url.format(
    access_token="{access_token}", agentid=self._agentid
)
return self._post(url_template, {"button": buttons[:MAX_TOP_BUTTONS]})
```

**回归点**：`test_create_menus_posts_with_agentid`

---

### 7.3 EncodingAESKey 填错时抛出 `binascii.Error`

**症状**：回调接口返回 500 而不是可读错误。
**根因**：`base64.b64decode` 抛标准库异常，`_build_crypt()` 只捕获 `WXBizMsgCryptError`。
**修法**：`aes_key_from_encoding()` 把解码异常统一包成 `WXBizMsgCryptError`。
**通用教训**：对外暴露的校验函数，异常类型必须收敛成一种，否则调用方的 `except` 会漏。
**回归点**：`test_invalid_encoding_aes_key_raises_our_error`

---

### 7.4 独立脚本跑出「通过 0 / 0」却退出 0

**症状**：`python tests/test_wecomnotify.py` 打印 `通过 0 / 0`，退出码 0，看着像全绿。
**根因**：在函数内调用 `dir()` 得到的是局部变量名，不是模块全局。
**修法**：`dir(sys.modules[__name__])`，并加一条「零用例即判失败」的闸。
**通用教训**：自测脚本报告「全绿」但计数为 0，是最危险的假绿。

---

### 7.5 宿主报「插件加载失败」，本地单测全绿

**症状**：真机安装后插件加载失败，插件自己的日志文件根本不生成。
**根因**：模块导入期就炸了（典型是漏 `from app.sdk.plugin import _PluginBase`）。
本地单测只 import 解耦模块，从没 import 过 `__init__.py`，所以完全隐形。
**排查**：看宿主主日志的 traceback，line 号指向哪个 import。
**修法**：加 AST 未定义名检查；或构造完整桩后 import 宿主类做冒烟测试。

---

### 7.6 代码推上去了，宿主页面还是旧的

**症状**：插件市场看不到新版本，或插件页内容没变。
**根因**：浏览器缓存了插件页渲染结果，或插件实例里留着旧快照。
**修法**：**强制刷新（Ctrl+F5）+ 重载插件**。

---

### 7.7 PowerShell 下 connector 调用报「参数必须是合法 JSON」

**根因**：PowerShell 调用原生命令时会剥掉内层双引号。
**修法**：改用 `--args-file <文件路径>`。

---

### 7.8 本机没有 git 命令 / 出网代理不通

**症状**：`git clone` 报「无法将 git 识别为 cmdlet」；`curl` 连 GitHub 超时。
**绕法**：全程用 GitHub App connector（`github__*` MCP）读写分支与 PR；
出网走 `http_proxy` 指定的代理（端口会变，先 `env | grep -i proxy`）；
`codeload.github.com` 可 `--noproxy '*'` 直连。

---

### 7.9 【v4.0】保存一次配置后，企业微信凭据全没了

**症状**：填好企业ID / Secret / Token，保存或重载插件后配置页变空，通知发不出去。
**根因**：`update_config()` 是**整体覆盖**，不是增量合并。合并时若 `__update_config()`
只写旧插件字段，新增的企微字段每次都会被抹掉。
**修法**：`__update_config()` 显式写回全部企微字段；新增字段时同步补测试。
**回归点**：`test_update_config_preserves_all_wecom_fields`

---

### 7.10 【v4.0】`_client` 一直是 None，通知永远发不出去

**症状**：配置都填对了，但 `/wecom_test` 报「插件未启用」，面板显示「未配置」。
**根因**：`init_plugin()` 内部会调 `stop_service()`，而 `stop_service()` 里的
`_stop_wecom()` 会把 `_client` 置 None。若 `_init_wecom()` 写在 `stop_service()` **之前**，
刚建好的客户端会被立刻释放。
**修法**：`_init_wecom()` 必须放在 `stop_service()` 之后。
**回归点**：`test_init_wecom_after_stop_service`

---

### 7.11 【v4.0】`NoticeMessage` 事件被处理两次 / 命令注册重复

**症状**：一条通知收到两条企微消息，或命令列表里出现重复项。
**根因**：把 `@eventmanager.register` 留在 mixin 里，宿主类继承后装饰器在多实例 /
多代类加载时重复注册；或 `get_command()` 把两套命令直接相加而没有去重。
**修法**：mixin 内零装饰器，宿主类显式转发；`get_command()` 合并时排除已存在的
`/push_qr`、`/wxcode`。
**回归点**：`test_mixin_has_no_event_registration`、`test_commands_are_unique`

---

### 7.12 【v4.0】后台任务没退干净时重载，企微凭据被抹成空

**症状**：偶发，重载后凭据丢失，且日志有「仍有后台任务未退出，跳过启动新后台循环」。
**根因**：`init_plugin()` 有一条提前 return 分支，它同样调用 `__update_config()` 整体覆盖，
但走不到下面的 `_init_wecom()`，企微字段停留在类属性默认值。
**修法**：拆出 `_load_wecom_config()`（只填字段、不建客户端、不联网），
在提前 return 分支里先调用它再 `__update_config()`。
**回归点**：`test_early_return_branch_loads_wecom_config`

---

### 7.13 【v4.0】老用户点「立即运行一次」看到企微测试失败日志

**症状**：只开 IP 保活、没填企微凭据，每次点「立即运行一次」都报测试消息发送失败。
**根因**：`onlyonce` 合并后同时触发企微连通性测试，而 `_client` 即使凭据不全也会被创建。
**修法**：`schedule_wecom_test_once()` 先检查 `self._client.is_configured()`，不满足则跳过。
**回归点**：`test_skip_wecom_test_when_not_configured`

---

### 7.14 【v4.0】企业微信后台保存回调 URL 一直失败

**症状**：回调地址从 `WecomNotify` 改成 `DynamicWeChatSelfUse` 之前，验签能通过；
合并后照抄旧 URL 就失败。
**根因**：回调路径由 `self.__class__.__name__` 动态推导，插件类名变了路径也变。
**修法**：URL 必须写成 `/api/v1/plugin/DynamicWeChatSelfUse/wecom`，
并在 README 与详情页同时给出。
**回归点**：`test_callback_path_uses_host_class_name`

---

### 7.15 【v4.0】配置页出现两个「启用」开关、两个「立即运行一次」

**症状**：表单里重复字段，保存时互相覆盖。
**根因**：旧表单和 `_wecom_form()` 都定义了 `enabled` / `onlyonce`。
**修法**：`_drop_shared_form_fields()` 递归剔除通知表单里的这两个节点，
默认模型也先 `pop` 再 `update`。
**回归点**：`test_form_has_no_duplicate_shared_models`

---

## 8. 收尾清单

接手人完成开发后，逐条打勾：

- [ ] 改动只在 `plugins.v3/dynamicwechatselfuse/`、`tests/test_wecomnotify.py`、
      `package.v3.json`、`README.md`、`HANDOVER.md`；`plugins.v3/wecomnotify/` 已删除
- [ ] `plugin_version` 与 `package.v3.json` 的 `DynamicWeChatSelfUse.version` 一致（4.0.0），
      且 `history` 顶部有 v4.0.0 记录
- [ ] `package.v3.json` 只有两条插件条目，`ParkingBot` 未被改动
- [ ] 插件 ID / 配置前缀 / 数据目录未改变，升级不丢配置
- [ ] `py_compile` 通过、`pyflakes` 零告警（整目录 + 测试文件）
- [ ] `pytest tests/test_wecomnotify.py` 全绿，且 `python tests/test_wecomnotify.py`
      报 `通过 N / N`（N > 0）
- [ ] `git diff` 里没有任何真实凭据
- [ ] 新增逻辑都补了对应测试
- [x] 真实宿主完成第 5.2 节的 **1–5 条**（详见第 5.3 节）
- [ ] 第 6–9 条真机企业微信联调（需用户配合）
- [ ] README 与实际行为一致（配置字段、命令、回调路径）
- [ ] PR 正文写明：改了什么、为什么并入旧插件、测试结果、仍未覆盖的能力
- [ ] 提醒用户在企业微信后台轮换此前明文出现过的凭据
- [ ] 合并后 `git checkout main && git pull`，确认 `package.v3.json` 里插件条目正常

---

## 9. 后续可做的方向（按性价比排序）

1. **真机联调**：用真实企业微信应用跑通「推送 → 回调 → 命令 → 菜单」全链路
   （第 5.2 节 6–9 条），重点先解决第 5.3 节记录的宿主全局 apikey 拦截。
2. **补第 3 条运行时日志核对**：宿主 MCP 无日志读取入口，需通过宿主文件系统 / 日志页面
   确认 DEBUG 下确实没有旧导入兼容警告。
3. **存量依赖迁移**：把 `app.adapters.external.cookiecloud` 等存量例外逐步收敛到
   `app.sdk.*`，让整个插件都符合红线 3。需先确认 SDK 是否提供等价出口。
4. **智能机器人长连接**：宿主实现在 `app/modules/wechat/wechatbot.py`，
   需要常驻 WebSocket 后台任务与重连。注意宿主把它单独放在 `wechatbot.py` 而非
   `wechat.py`，就是因为两套 API 差异大。
5. **补图片 / 文件消息处理**：目前只回「暂不支持」，企业微信允许用 `media/get`
   下载素材再回传（`client.py` 已实现 `download_media()`）。
6. **补语音消息**：企业微信语音需要转 AMR，宿主内置实现用 `ffmpeg` 转码
   （见 `app/modules/wechat/wechat.py` 的 `_convert_voice_to_amr`），宿主环境有
   ffmpeg 才能做。

---

## 10. 附：本地临时文件（可丢弃）

开发过程中的中间产物，**不属于交付物**，接手人可直接忽略：

```
_merge_step1.py / _merge_step2.py   # 机械迁移脚本（复制 client/crypt、转换 mixin、合并契约方法）
_pylibs/                            # 临时装的 pytest / pyflakes / pycryptodome
__pycache__/ .pytest_cache/         # 运行产物
mp_api_schema.json                  # MoviePilot MCP 的 operation_id 契约快照
```

仓库里的代码才是权威来源。
