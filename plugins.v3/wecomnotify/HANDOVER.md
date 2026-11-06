# 企业微信通知插件 —— 交接文档

> 面向接手继续开发的开发者。读完本文即可独立开工，不需要回看原始对话。
> 文档对应代码版本：`plugin_version = "1.0.0"`。

---

## 1. 状态快照

| 项目 | 状态 |
| --- | --- |
| 仓库 | `laoying6663/MoviePilot-Plugins`（个人 fork，插件市场数据源） |
| 分支 | `feat/wecom-notify`，HEAD `737d6be033ef02e28081c43b2c8c3d5b703516b7` |
| PR | [#1 feat: 新增企业微信通知插件](https://github.com/laoying6663/MoviePilot-Plugins/pull/1)，base `main`，状态 open |
| 插件 ID | `WecomNotify`，目录 `plugins.v3/wecomnotify/` |
| 离线自测 | `pytest tests/test_wecomnotify.py` → 42 passed；`python tests/test_wecomnotify.py` → 42/42 |
| 静态检查 | `pyflakes` 零告警 |
| **未验证** | **真实 MoviePilot 宿主加载验证**（未做）、**真机企业微信联调**（未做） |

### 已交付文件

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins.v3/wecomnotify/__init__.py` | 784 | 主类：生命周期、通知事件、回调 API、远程命令、配置表单、详情页 |
| `plugins.v3/wecomnotify/client.py` | 332 | 企业微信 API 客户端：token 缓存、消息发送、菜单、素材下载 |
| `plugins.v3/wecomnotify/crypt.py` | 189 | 回调加解密：SHA1 签名 + AES-CBC。**零宿主依赖**，可脱离宿主单测 |
| `plugins.v3/wecomnotify/README.md` | 50 | 插件说明与配置指引 |
| `tests/test_wecomnotify.py` | 370 | 离线自测，42 条用例，pytest / 独立脚本双模 |
| `package.v3.json` | — | 新增 `WecomNotify` 索引条目（其余条目原样保留） |

### 尚未实现（明确不在本次范围）

- **企业微信智能机器人长连接**（`dmPolicy=open` / `groupPolicy=disabled`）。走的是智能机器人
  WebSocket 长连接，与自建应用推送是两套完全不同的 API。宿主内置实现在
  `app/modules/wechat/wechatbot.py`，可作参考。

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
| `app/sdk/plugin/base.py` | `_PluginBase` 契约。确认了 `self.eventmanager`、`self.chain`、`self.systemmessage`、`update_config()`、`get_data_path()` 的存在与签名 |
| `app/sdk/network.py` | 插件可用的 HTTP 出口：`RequestUtils`（同步）、`AsyncRequestUtils`（异步）、`UrlUtils` |
| `app/api/endpoints/message.py` | 宿主内置回调端点 `/api/v1/message/`，本插件的回调是它的插件版复刻 |
| `app/modules/wechat/__init__.py` | 宿主内置企业微信模块：消息解析、管理员校验、菜单注册 |
| `app/modules/wechat/wechat.py` | 宿主内置客户端：token 缓存、发送分块、菜单构建 |
| `app/adapters/external/wechat.py` | `WXBizMsgCrypt`（腾讯官方算法）。**插件不能导入它**，`crypt.py` 是按公开算法重写的独立实现 |

### 2.3 本仓库约定（与官方指南不同，以本仓库为准）

- **测试目录是平铺的**：`tests/test_*.py`，不是官方指南的 `tests/v3/<plugin_id>/`
- **测试是独立脚本风格**：自带 `PASS`/`FAIL` 计数，`sys.exit(1 if FAIL else 0)`
- **既有测试会在模块导入期执行副作用**（含 `sys.exit`），因此**不要**对整个 `tests/` 目录跑
  `pytest`，只跑单个文件
- 既有测试通过 `exec(compile(src, ...))` 复用其它文件的桩，这是本仓库的惯用手法
- 仓库根存在硬编码本地路径 `B = r"D:\minimax\dynamicwechat-backup"`（作者本地工作副本），
  属于既有技术债，新增测试**不要**沿用

---

## 3. 机制说明

### 3.1 推送链路（宿主 → 企业微信）

```
宿主 post_message()
  └─ EventType.NoticeMessage 广播
       └─ WecomNotify.notice_message()          # __init__.py
            ├─ 按 self._types 过滤消息类型
            ├─ 确定接收人：通知自带 userid 优先，否则用配置的 userid（空 = @all）
            └─ client.send_msg()  →  asyncio.to_thread
                 └─ 企业微信 cgi-bin/message/send
```

要点：
- 插件**不走** `post_message()` 回复，否则会回到宿主渠道造成回环
- `self._client` 在 `init_plugin()` 中重建，避免配置变更后继续用旧的代理/密钥
- 发送走 `asyncio.to_thread`，因为用的是同步 `RequestUtils`

### 3.2 回调链路（企业微信 → 宿主）

```
企业微信  ──GET──▶  /api/v1/plugin/WecomNotify/wecom?msg_signature=&timestamp=&nonce=&echostr=
                     └─ WecomNotify.wecom_verify()
                          ├─ 签名校验：SHA1(token,timestamp,nonce,echostr) == msg_signature
                          ├─ AES 解密 echostr，校验内嵌企业ID
                          └─ 返回解密后的明文（企业微信据此判定 URL 有效）

企业微信  ──POST─▶  /api/v1/plugin/WecomNotify/wecom?msg_signature=&timestamp=&nonce=
                     └─ WecomNotify.wecom_message()
                          ├─ DecryptMsg：验签 → AES 解密 → 校验企业ID → 取明文 XML
                          ├─ parse_xml_message() 解析
                          ├─ 文本/菜单点击 → 命令；语音 → 识别结果；图片/文件 → 回执不支持
                          ├─ 斜杠命令 → 管理员白名单校验 → _dispatch_command()
                          ├─ 非命令消息 → EventType.UserMessage 广播
                          └─ 始终返回 "success"（企业微信要求 5 秒内响应）
```

要点：
- 回调接口 `auth: "none"`（匿名），安全性由企业微信的 SHA1 签名承担——攻击者算不出签名。
  这是**有意为之**，不要改成需要登录
- 消息处理包在 `asyncio.wait_for(..., timeout=20)` 里，超时不拖垮回调
- 回调先解密再处理，返回体固定是 `success`

### 3.3 加解密结构（`crypt.py`）

```
明文 = random(16) + struct.pack(">I", len(msg)) + msg + receiveid
密文 = AES-256-CBC(key=EncodingAESKey解出的32字节, iv=key[:16], PKCS7补位到32字节倍数)
密文传输 = base64(密文)
签名 = SHA1("".join(sorted([token, timestamp, nonce, encrypt])))
```

回调报文外层是 `<xml><Encrypt><![CDATA[...]]></Encrypt></xml>`。

### 3.4 远程命令分发

```
_dispatch_command(cmd, userid)
  ├─ /help 或 /帮助  → 回列本插件命令
  ├─ 命中 get_command() 注册表 → 发 EventType.PluginAction（含 userid / args）
  │     └─ plugin_action() 处理 wecom_status / wecom_test，用企业微信 API 直接回复
  └─ 未命中 → 回「未找到命令」
```

`get_command()` 是**唯一**的命令来源，`/help` 与菜单构建都从它派生，新增命令只改这一处。

### 3.5 菜单创建

`client.create_menus()` 按 `category` 分组：一级菜单最多 3 个、每个二级最多 5 个，
沿用宿主内置渠道的裁剪规则。`get_command()` 里没有 `category` 的命令会被跳过。

---

## 4. 标准流程

### 4.1 环境准备

```bash
# 本机没有 git（本项目就是因此全程走 GitHub App connector），用任何 git 客户端均可
git clone https://github.com/laoying6663/MoviePilot-Plugins.git
cd MoviePilot-Plugins
git checkout feat/wecom-notify

# 宿主依赖（插件共享宿主环境，不要另建虚拟环境）
# ../MoviePilot/.venv/bin/python -m pip install pytest pycryptodome
```

Windows PowerShell 下若直接用 `mcode-tools connector call --args '{...}'`，
**双引号会被原生参数解析吃掉**。必须用 `--args-file <文件路径>`。

### 4.2 改代码 → 自测 → 提交

```bash
# 1) 语法
python -m py_compile plugins.v3/wecomnotify/*.py

# 2) 静态检查
python -m pyflakes plugins.v3/wecomnotify/*.py tests/test_wecomnotify.py

# 3) 离线自测（两种跑法都必须过）
pytest tests/test_wecomnotify.py -q
python tests/test_wecomnotify.py

# 4) 版本一致性
#    plugins.v3/wecomnotify/__init__.py 的 plugin_version
#    必须等于 package.v3.json 里 WecomNotify.version
```

提交信息沿用仓库风格：

```
feat: <一句话说明>

<正文：改了什么、为什么、怎么验证的>
```

### 4.3 提 PR

分支命名 `feat/<主题>` 或 `fix/<主题>`，base 固定 `main`。
PR 正文写清：背景、改动、测试结果、**未包含的能力**。

---

## 5. 判据与验收标准

### 5.1 提交前必须全绿

| 判据 | 判据落地在哪 | 不满足的后果 |
| --- | --- | --- |
| `pytest tests/test_wecomnotify.py` 全绿 | pytest 退出码 0 | 回归 |
| `python tests/test_wecomnotify.py` 报 `通过 N / N` 且 N > 0 | 脚本退出码 0 | 假绿 |
| `pyflakes` 零输出 | 退出码 0 | 未定义名/未用导入 |
| `plugin_version` == `package.v3.json` 的 `version` | 人工核对 | 市场页版本错乱 |
| `package.v3.json` 其余条目未被改动 | `git diff` 只应有新增 | 别人的插件被误改 |
| 没有真实凭据进仓库 | `git diff` 全文搜 Secret/Token 值 | **安全事故** |

> ⚠️ 独立脚本模式下，如果一个用例都没跑到，脚本必须**判失败**而不是报「通过 0 / 0」。
> 这条已经内置（见 `test_wecomnotify.py` 的 `_run_standalone`），改动它时不要移除。

### 5.2 真实宿主验收（本插件尚未完成，交给接手人）

在真实 MoviePilot V3 宿主上逐条确认：

1. 插件市场能发现并安装 `WecomNotify`
2. 插件启动 / 禁用 / 重载都不残留后台资源
3. `DEBUG=true` 时**没有**旧导入兼容警告
4. API、服务、命令只注册一次
5. 配置页渲染正常，字段与 README 一致
6. 填入真实企业微信参数后，`/wecom_test` 能收到消息
7. 企业微信「接收消息」页保存 URL 成功（即回调验签通过）
8. 在企业微信里发 `/help`、`/wecom_status`，能收到回复
9. 企业微信应用菜单出现「企业微信通知」分组

---

## 6. 红线清单

违反其中任何一条都会造成实际损失：

1. **绝不把真实凭据提交进仓库。** 本次开发过程中，用户截图里带有真实的
   企业ID / 应用Secret / Token / EncodingAESKey / 管理员白名单。
   仓库里只允许出现占位符与示例值。配置一律走插件配置页。
   *另：这些凭据已在对话与截图中明文出现，建议在企业微信后台轮换一次。*
2. **插件不能注册新的 `NotificationChannel`。** 那是宿主的封闭枚举，改不了。
   不要试图 monkey-patch `app.schemas.types`，也不要 import `app.domain.*` 去改枚举。
3. **只用 `app.sdk.*` 稳定出口。** 禁止 `app.adapters.*`、`app.application.*`、
   `app.domain.*`、`app.foundation.*`、`app.runtime.*`、`app.core.*`、
   `app.helper.*`、`app.utils.*`。本插件已按此实现（`crypt.py` 更是零宿主依赖）。
4. **测试不进 `plugins.v3/`。** 否则市场同步会把测试一起复制到运行目录。
5. **不自建 `BackgroundScheduler`。** 一次性任务用
   `app.sdk.scheduler.add_plugin_once_job()`；周期任务用 `get_service()`。
6. **导入期不启任务、不联网、不连数据库。** 这些只能放在 `init_plugin()` 里。
7. **`init_plugin()` 必须可重复调用**，`stop_service()` 必须可重复且安全释放。
8. **运行数据不写插件源码目录。** 用 `save_data()` / `get_data_path()`。
9. **不自建哈希表/全局单例存可变状态**，虚拟分身会互相污染。
10. **回调接口保持 `auth: "none"`**，靠企业微信签名做鉴权。改成需要登录会让
    企业微信回调直接失败。

---

## 7. 踩坑档案（按症状索引）

### 7.1 长文本切分报 `IndexError: index out of range`

**症状**：`client.py` 的 `_split_content()` 在切长行时崩。

**根因**：回退到字符边界的循环写成了无条件执行。当 `end == line_length`（本行放得下）
时 `encoded[end]` 越界。

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

**根因**：菜单 URL 模板含两个占位符 `{access_token}` 和 `{agentid}`，
但 `_post()` 只填了 `access_token`。

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

**症状**：用户把 EncodingAESKey 填错，回调接口返回 500 而不是可读错误。

**根因**：`base64.b64decode` 抛的是标准库自己的异常，`_build_crypt()` 只捕获
`WXBizMsgCryptError`，异常逃出去了。

**修法**：`aes_key_from_encoding()` 把解码异常统一包成 `WXBizMsgCryptError`。
**通用教训**：对外暴露的校验函数，异常类型必须收敛成一种，否则调用方的
`except` 会漏。

**回归点**：`test_invalid_encoding_aes_key_raises_our_error`

---

### 7.4 独立脚本跑出「通过 0 / 0」却退出 0

**症状**：`python tests/test_wecomnotify.py` 打印 `通过 0 / 0`，退出码 0，看着像全绿。

**根因**：在函数内调用 `dir()` 得到的是**局部变量名**，不是模块全局，
所以一个用例都没收集到。

**修法**：`dir(sys.modules[__name__])`，并加一条「零用例即判失败」的闸。
**通用教训**：自测脚本报告「全绿」但计数为 0，是最危险的假绿——
它比报错更糟，因为它让人以为这一层被测到了。

---

### 7.5 宿主报「插件加载失败」，本地单测全绿

**症状**：真机安装后插件加载失败，插件自己的日志文件根本不生成。

**根因**：模块导入期就炸了（典型是漏 `from app.sdk.plugin import _PluginBase`）。
本地单测只 import 解耦模块，从没 import 过 `__init__.py`，所以完全隐形。

**排查**：看宿主主日志的 traceback，line 号指向哪个 import。
**修法**：加一条 AST 未定义名检查；或直接 import `__init__.py` 做冒烟测试。

---

### 7.6 代码推上去了，宿主页面还是旧的

**症状**：插件市场看不到新版本，或插件页内容没变。

**根因**：浏览器缓存了插件页渲染结果，或插件实例里留着旧快照。

**修法**：**强制刷新（Ctrl+F5）+ 重载插件**。
**通用教训**：每次推完插件版本都要提醒用户做这两步，否则大半「功能没生效」的报障
其实是在报缓存。

---

### 7.7 PowerShell 下 connector 调用报「参数必须是合法 JSON」

**症状**：`mcode-tools connector call ... --args '{"owner":"x"}'` 报 JSON 非法。

**根因**：PowerShell 调用原生命令时会剥掉内层双引号。

**修法**：改用 `--args-file <文件路径>`。

---

### 7.8 本机没有 git 命令

**症状**：`git clone` 报「无法将 git 识别为 cmdlet」。

**根因**：该机器未安装 git。

**绕法**：全程用 GitHub App connector（`mcode-tools connector call connector__github__*`）：
`get_file_contents` 读、`create_branch` 建分支、`push_files` 推、
`create_pull_request` 开 PR、`list_pull_requests` 回读。

---

## 8. 收尾清单

接手人完成开发后，逐条打勾：

- [ ] 改动只在 `plugins.v3/wecomnotify/` 与 `tests/test_wecomnotify.py`，
      `package.v3.json` 仅新增/修改 `WecomNotify` 条目
- [ ] `plugin_version` 与 `package.v3.json` 的 `version` 一致，且 `history` 顶部有当前版本记录
- [ ] `pytest tests/test_wecomnotify.py` 全绿，且 `python tests/test_wecomnotify.py`
      报 `通过 N / N`（N > 0）
- [ ] `pyflakes` 零告警
- [ ] `git diff` 里没有任何真实凭据
- [ ] 新增逻辑都补了对应测试
- [ ] 真实宿主完成第 5.2 节的 9 条验收
- [ ] README 与实际行为一致（配置字段、命令、回调路径）
- [ ] PR 正文写明：改了什么、测试结果、仍未覆盖的能力
- [ ] 合并后 `git checkout main && git pull`，确认 `package.v3.json` 里插件条目正常

---

## 9. 后续可做的方向（按性价比排序）

1. **补真实宿主验收**（第 5.2 节）。目前最大的未知项就是它。
2. **真机联调**：用真实企业微信应用跑通「推送 → 回调 → 命令 → 菜单」全链路。
3. **智能机器人长连接**：宿主实现在 `app/modules/wechat/wechatbot.py`，
   需要常驻 WebSocket 后台任务与重连，参考 `app/modules/wechat/wechatbot.py`
   的生命周期写法。注意宿主把它单独放在 `wechatbot.py` 而非 `wechat.py`，
   就是因为两套 API 差异大。
4. **补图片/文件消息处理**：目前收到图片和文件只回「暂不支持」。
   企业微信允许用 `media/get` 下载素材再回传，值得补。
5. **补语音消息**：企业微信语音需要转 AMR，宿主内置实现用 `ffmpeg` 转码
   （见 `app/modules/wechat/wechat.py` 的 `_convert_voice_to_amr`），
   宿主环境有 ffmpeg 才能做。

---

## 10. 附：本地临时文件（可丢弃）

开发过程中的中间产物，**不属于交付物**，接手人可直接忽略：

```
D:\minimax\.agent-tmp\wecomnotify\     # 插件源码的工作副本（已推送到仓库）
D:\minimax\.agent-tmp\tests\           # 测试文件工作副本（已推送到仓库）
D:\minimax\.agent-tmp\stage\           # 按仓库结构搭的本地镜像，用于跑测试
D:\minimax\.agent-tmp\pylibs\          # 临时装的 pytest / pyflakes / pycryptodome
D:\minimax\.agent-tmp\*.json           # connector 参数文件
```

仓库里的代码才是权威来源。
