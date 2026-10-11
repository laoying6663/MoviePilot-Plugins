# dynamicwechat 插件保全包

这个目录是 `laoying6663/MoviePilot-Plugins` 仓库的**唯一完整副本**。
原仓库的 PR #2 与 PR #3 **从未合并**，仓库一旦删除，GitHub 上不会有任何备份。

保全时间：2026-10-11

---

## 目录结构

```text
dynamicwechat-backup/
├── plugins.v3/dynamicwechat/          # V3 专用实现 3.0.0 ← 你要用的
│   ├── __init__.py
│   └── helper.py
├── plugins.v2/dynamicwechat/          # V2 实现 2.1.9（V3 加载失败时的回退）
│   ├── __init__.py
│   ├── helper.py
│   └── src/UpdateHelp.py
├── package.v3.json                    # V3 索引（仅 DynamicWeChat = 3.0.0, system_version >=3.0.0）
├── package.v2.json                    # V2 索引（仅 DynamicWeChat = 2.1.9, "v3": false）
├── icons/Wecom_A.png                  # 插件图标（需手动上传，API 不支持二进制）
├── tests/                             # 离线自测（v3 65 项 / v2 43 项）
└── MANIFEST.json                      # 每个文件的 SHA256
```

> **没有 `package.json`**：该文件是 V1 默认索引，对应 `plugins/` 目录。本仓库不含
> V1 实现，保留它只会让索引指向一堆不存在的插件目录。

## 版本状态

| 目录 | 版本 | 说明 |
|---|---|---|
| `plugins.v3/dynamicwechat` | **3.0.0** | V3 专用实现，`app.sdk.*` + `httpx2` |
| `plugins.v2/dynamicwechat` | 2.1.9 | V2 实现，含同样的 Cookie 修复 |
| `plugins/`（V1） | — | **不含**。原仓库的 1.7.3 未纳入，1.7.4 的改动已随 PR #1 关闭丢弃 |

## 署名

- `author` / `plugin_author`：**RamenRa, laoying6663**
  上游插件作者是 RamenRa，本仓库为其 fork 并由 laoying6663 维护修复，署名保留双方。
- `author_url`：仍指向 `https://github.com/RamenRa/MoviePilot-Plugins`
  —— 插件面板里的「作者主页 / 全功能详细说明」链接以上游仓库文档为准。

## 重建仓库步骤

### 1. 建库

新建一个仓库（**不要 fork**），然后把本目录内容推上去：

```powershell
cd dynamicwechat-backup
git init
git add .
git commit -m "feat(dynamicwechat): V3 专用实现 3.0.0"
git remote add origin https://github.com/<你的账号>/<新仓库名>.git
git push -u origin main
```

> 本机目前**没有系统级 git**。如果还没装，可用 `D:\minimax\tools\flutter\bin\cache\mingit`（Flutter 自带），
> 或者安装 Git for Windows 后再执行上面的命令。

### 2. 校验完整性（可选但建议）

```powershell
python -c "import hashlib,json;m=json.load(open('MANIFEST.json',encoding='utf-8'));[print(('OK  ' if hashlib.sha256(open(f['path'],'rb').read()).hexdigest()==f['sha256'] else 'BAD '),f['path']) for f in m['files']]"
```

### 3. 在 MoviePilot 里换源

插件市场里把仓库地址改成新仓库 URL，然后刷新市场缓存。

## 重要提醒

**V3 那份（3.0.0）从未在真实 MoviePilot V3 宿主加载过。**

- `httpx2` 调用与 `app.sdk.*` 导入路径是**核对 V3 源码**确认存在的，但没有真机验证
- 迁移涉及的类改名是**互换命名**，很容易搞反：
  - 旧 `NotificationType.Plugin` → 新 `MessageType.Plugin`
  - 旧 `MessageChannel.Wechat` → 新 `NotificationChannel.Wechat`
- **如果 V3 版本加载失败**，把 `package.v3.json` 里的 `DynamicWeChat` 条目删掉、把
  `package.v2.json` / `package.json` 里的 `"v3": false` 去掉，宿主就会回退加载
  `plugins.v2/` 那份 2.1.9 —— 它经过更多验证，且 V3 通过兼容层本来就能加载。

**已验证的部分**：离线逻辑自测 v3 65/65、v2 43/43 全通过；索引契约按官方
`check_plugin_versions.py` 逻辑校验通过；V3 源码零遗留旧导入、零 `aiohttp`。
