"""
针对 plugins.v2/dynamicwechat 补丁的离线逻辑自测（async 版本）。
打桩隔离 MoviePilot / playwright / aiohttp / 网络，只验证纯逻辑分支。
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import types
from unittest.mock import MagicMock

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "dynamicwechat-v2-fix")


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Logger:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Settings:
    VERSION_FLAG = "v2"
    TZ = "Asia/Shanghai"
    NGINX_PORT = 3000
    COOKIECLOUD_KEY = "k"
    COOKIECLOUD_PASSWORD = "p"
    COOKIECLOUD_ENABLE_LOCAL = True


class _EventType:
    PluginAction = "PluginAction"
    UserMessage = "UserMessage"


class _NotificationType:
    Plugin = "Plugin"


class _MessageChannel:
    Wechat = "Wechat"


class _EventManager:
    def register(self, *a, **k):
        return lambda f: f


class _Timeout(Exception):
    pass


class _Ctx:
    def __init__(self, fn):
        self.fn = fn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _AiohttpResp:
    def __init__(self, data=b"qr"):
        self.data = data

    async def read(self):
        return self.data


class _AiohttpSession:
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        _AiohttpSession.calls.append((url, kw))
        return _Ctx(self._resp)

    async def _resp(self):
        return _AiohttpResp()


class _ClientTimeout:
    def __init__(self, total=None):
        self.total = total


class _Aiohttp:
    ClientSession = _AiohttpSession
    ClientTimeout = _ClientTimeout


_stub("requests", get=None, post=None, RequestException=Exception)
_stub("aiohttp", ClientSession=_AiohttpSession, ClientTimeout=_ClientTimeout)
_stub("playwright")
_stub("playwright.async_api", async_playwright=None)
_stub("cloakbrowser", launch_context_async=None)
_stub("Crypto", Random=None)
_stub("Crypto.Cipher", AES=None)
_stub("app")
_stub("app.core")
_stub("app.core.config", settings=_Settings())
_stub("app.core.event", eventmanager=_EventManager(), Event=object)
_stub("app.log", logger=_Logger())
_stub("app.plugins", _PluginBase=object)
_stub("app.schemas")
_stub("app.schemas.types", EventType=_EventType(), NotificationType=_NotificationType(),
      MessageChannel=_MessageChannel())
_stub("app.modules")
_stub("app.modules.wechat", WeChat=object)
_stub("app.plugins.dynamicwechat")
_stub("app.helper")
_stub("app.helper.cookiecloud", CookieCloudHelper=MagicMock())

import importlib.util
import shutil

tmpdir = tempfile.mkdtemp()

# v2 用相对导入 `from .helper import ...`，必须以「包」的方式加载
PKG_PARENT = os.path.join(tmpdir, "pkgs")
PKG_NAME = "dwv2plugin"
PKG_DIR = os.path.join(PKG_PARENT, PKG_NAME)
os.makedirs(PKG_DIR, exist_ok=True)
for f in ("__init__.py", "helper.py"):
    shutil.copy(os.path.join(SRC, f), os.path.join(PKG_DIR, f))
sys.path.insert(0, PKG_PARENT)

helper = importlib.import_module(f"{PKG_NAME}.helper")
PyCookieCloud = helper.PyCookieCloud

main = importlib.import_module(PKG_NAME)
DynamicWeChat = main.DynamicWeChat

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


class FakeSender:
    init_success = True
    other_channel = []
    titles = []

    def send(self, title, content=None, image=None, force_send=False, **kw):
        FakeSender.titles.append(title)
        return None

    def reset_limit(self):
        pass


class FakePage:
    url = "https://work.weixin.qq.com/wework_admin/frame"

    def __init__(self, login_ok=False):
        self.login_ok = login_ok

    async def goto(self, url, **kw):
        self.url = url
        return MagicMock()

    async def wait_for_selector(self, *a, **k):
        if self.login_ok:
            return MagicMock()
        raise _Timeout("没有验证码面板")


class FakeContext:
    def __init__(self, cookies=None, login_ok=False):
        self._cookies = cookies or [{"name": "wwrt", "value": "v1", "domain": ".work.weixin.qq.com", "path": "/"}]
        self.login_ok = login_ok
        self.added = []
        self.closed = False
        self.page = FakePage(login_ok)

    async def add_cookies(self, c):
        self.added.append(list(c))

    async def cookies(self, *a):
        return list(self._cookies)

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


def new_plugin(settings_path, **attrs):
    p = DynamicWeChat.__new__(DynamicWeChat)
    p._settings_file_path = settings_path
    p._file_lock = threading.Lock()
    p._notify_lock = threading.Lock()
    p._qr_lock = threading.Lock()
    p._saved_cookie = None
    p._cookie_valid = True
    p._cookie_invalid_notified = False
    p._fail_count = 0
    p._fail_threshold = 3
    p._cookie_store_key = "_cookie"
    p._is_special_upload = False
    p._cookie_lifetime = 0
    p._use_cookiecloud = False
    p._cookie_header = ""
    p._my_send = None
    p._await_ip = False
    p._wechat_available = True
    p._stopping = False
    p._ip_changed = True
    p._future_timestamp = 0
    p._verification_code = None
    p._wechatUrl = "https://work.weixin.qq.com/wework_admin/loginpage_wx?from=myhome"
    p._cookiecloud = MagicMock()
    p.systemmessage = MagicMock()
    for k, v in attrs.items():
        setattr(p, k, v)
    return p


async def acquire(ok=True):
    return ok


tmpdir = tempfile.mkdtemp()
sf = os.path.join(tmpdir, "settings.json")

DEAD = [{"name": "wwrt", "value": "dead", "domain": ".work.weixin.qq.com", "path": "/"}]


async def main_async():
    print("\n=== 1. settings.json 读写健壮性 ===")
    check("文件不存在时返回空 dict", PyCookieCloud.read_settings(sf) == {})
    check("save_cookie_lifetime 成功", PyCookieCloud.save_cookie_lifetime(sf, 600) is True)
    check("load_cookie_lifetime 读回 600", PyCookieCloud.load_cookie_lifetime(sf) == 600)
    PyCookieCloud.increase_cookie_lifetime(sf, 600)
    check("increase 累加到 1200", PyCookieCloud.load_cookie_lifetime(sf) == 1200)
    PyCookieCloud.write_settings(sf, {"note": "中文说明"})
    check("UTF-8 存中文", "中文说明".encode("utf-8") in open(sf, "rb").read())
    with open(sf, "w", encoding="utf-8") as f:
        f.write("{ broken")
    check("JSON 损坏返回 None", PyCookieCloud.read_settings(sf) is None)
    with open(sf, "w", encoding="gbk") as f:
        f.write(json.dumps({"ips": ["1.1.1.1"], "note": "旧"}, ensure_ascii=False))
    check("GBK 文件可兜底读回",
          PyCookieCloud.read_settings(sf).get("note") == "旧",
          repr(PyCookieCloud.read_settings(sf)))

    print("\n=== 2. 并发读-改-写不丢更新 ===")
    sfc = os.path.join(tmpdir, "c.json")
    PyCookieCloud.write_settings(sfc, {})

    def hammer(field, value):
        for _ in range(6):
            PyCookieCloud.update_settings(sfc, lambda d: d.__setitem__(field, value))

    ts = []
    for field, val in [("_cookie", [{"n": "a"}]), ("ips", ["1.1.1.1"]),
                       ("_cookie_lifetime", 600), ("url_ip", ["2.2.2.2"])]:
        for _ in range(10):
            t = threading.Thread(target=hammer, args=(field, val))
            ts.append(t); t.start()
    for t in ts:
        t.join()
    final = json.load(open(sfc, encoding="utf-8"))
    check("4 个字段全部保留", len(final) == 4, json.dumps(final, ensure_ascii=False))
    check("无残留 .tmp",
          not [f for f in os.listdir(tmpdir) if f.endswith(".tmp")],
          str([f for f in os.listdir(tmpdir) if f.endswith(".tmp")]))

    print("\n=== 3. parse_cookie_header 多域名 ===")
    p = new_plugin(sf)
    r = p.parse_cookie_header("a=1;b=2;", "work.weixin.qq.com")
    check("domain 可参数化", r[0]["domain"] == "work.weixin.qq.com", str(r))
    check("尾随分号不崩", len(r) == 2)

    print("\n=== 4. 手工 cookie_header 在关闭 CookieCloud 时可用（自我锁死回归）===")
    sfh = os.path.join(tmpdir, "h.json")
    ph = new_plugin(sfh, _use_cookiecloud=False, _cookie_header="wwrt=manual;wwrt.defense=7")
    c = await ph.get_cookie_async()
    check("关闭 CookieCloud 时能解析手工 header",
          c is not None and c[0]["value"] == "manual", str(c))
    check("结果缓存到 _saved_cookie", ph._saved_cookie == c)

    sfh2 = os.path.join(tmpdir, "h2.json")
    ph2 = new_plugin(sfh2, _use_cookiecloud=False, _cookie_header="wwrt=manual2")
    c2 = await ph2.get_cookie_async(skip_local=True)
    check("skip_local=True 绕过缓存重新解析", c2 is not None and c2[0]["value"] == "manual2")

    print("\n=== 5. 落盘与重启恢复 ===")
    sfp = os.path.join(tmpdir, "p.json")
    pp = new_plugin(sfp)
    check("_persist_cookie 成功", await pp._persist_cookie(DEAD) is True)
    stored = json.load(open(sfp, encoding="utf-8"))
    check("cookie 落到 _cookie 字段", stored.get("_cookie") == DEAD)
    pp2 = new_plugin(sfp)          # 模拟重启：内存全空
    check("重启后 _saved_cookie 为空", pp2._saved_cookie is None)
    pp2._load_persisted_cookie()
    check("_load_persisted_cookie 恢复成功", pp2._saved_cookie == DEAD)
    check("恢复后 _cookie_valid 为 True", pp2._cookie_valid is True)

    print("\n=== 6. refresh_cookie 退避（单轮失败不清空、不通知）===")
    FakeSender.titles.clear()
    sfr = os.path.join(tmpdir, "r.json")
    pr = new_plugin(sfr, _saved_cookie=list(DEAD), _my_send=FakeSender())
    await pr._persist_cookie(DEAD)
    ctx = FakeContext()
    pr._launch_browser_context_with_retry = lambda headless=True: _ready(ctx)
    pr.check_login_status = lambda page, task: _ready(False)
    await pr.refresh_cookie()
    check("第 1 轮失败不清空 _saved_cookie", pr._saved_cookie == DEAD)
    check("第 1 轮失败不清空落盘副本",
          json.load(open(sfr, encoding="utf-8")).get("_cookie") == DEAD)
    check("第 1 轮失败不判定 _cookie_valid=False", pr._cookie_valid is True)
    check("第 1 轮不发失效通知", "cookie已失效,请及时更新" not in FakeSender.titles)
    check("失败计数累加为 1", pr._fail_count == 1, str(pr._fail_count))

    await pr.refresh_cookie()
    check("第 2 轮仍不清空", json.load(open(sfr, encoding="utf-8")).get("_cookie") == DEAD)
    check("失败计数累加为 2", pr._fail_count == 2, str(pr._fail_count))

    await pr.refresh_cookie()
    check("第 3 轮才判定失效", pr._cookie_valid is False)
    check("失效后清空内存 cookie", pr._saved_cookie is None)
    check("失效后清空落盘 cookie",
          "_cookie" not in json.load(open(sfr, encoding="utf-8")))
    check("整轮只发 1 次失效通知",
          FakeSender.titles.count("cookie已失效,请及时更新") == 1,
          str(FakeSender.titles))

    print("\n=== 7. 复核通过 → 回写轮换后的 Cookie 并落盘 ===")
    sfs = os.path.join(tmpdir, "s.json")
    rotated = [{"name": "wwrt", "value": "rotated", "domain": ".work.weixin.qq.com", "path": "/"},
               {"name": "extra", "value": "x", "domain": "qq.com", "path": "/"}]
    ps = new_plugin(sfs, _saved_cookie=list(DEAD), _my_send=FakeSender())
    await ps._persist_cookie(DEAD)
    ps._launch_browser_context_with_retry = lambda headless=True: _ready(FakeContext(rotated))
    ps.check_login_status = lambda page, task: _ready(True)
    await ps.refresh_cookie()
    check("判定有效", ps._cookie_valid is True)
    check("回写企微轮换后的 Cookie（含多域名）", ps._saved_cookie == rotated, str(ps._saved_cookie))
    check("最新 Cookie 已落盘",
          json.load(open(sfs, encoding="utf-8")).get("_cookie") == rotated)
    check("失败计数清零", ps._fail_count == 0)

    print("\n=== 8. 异常不清空 Cookie（原实现会误报失效）===")
    sfe = os_plugin = os.path.join(tmpdir, "e.json")
    pe = new_plugin(sfe, _saved_cookie=list(DEAD), _my_send=FakeSender())
    await pe._persist_cookie(DEAD)
    FakeSender.titles.clear()

    async def _boom(headless=True):
        raise RuntimeError("浏览器启动失败")

    pe._launch_browser_context_with_retry = _boom
    await pe.refresh_cookie()
    check("浏览器异常后 _saved_cookie 保留", pe._saved_cookie == DEAD)
    check("浏览器异常后落盘副本保留",
          json.load(open(sfe, encoding="utf-8")).get("_cookie") == DEAD)
    check("浏览器异常不发失效通知", len(FakeSender.titles) == 0, str(FakeSender.titles))
    check("浏览器异常不误置 _cookie_valid=False", pe._cookie_valid is True)

    print("\n=== 9. 手工 header 让插件能自行恢复 ===")
    sfi = os.path.join(tmpdir, "i.json")
    pi = new_plugin(sfi, _use_cookiecloud=False, _cookie_header="wwrt=manualX", _my_send=FakeSender())
    FakeSender.titles.clear()
    pi._launch_browser_context_with_retry = lambda headless=True: _ready(FakeContext())
    pi.check_login_status = lambda page, task: _ready(False)
    for _ in range(3):
        await pi.refresh_cookie()
    check("三轮后判定失效", pi._cookie_valid is False)
    check("手工 header 未被清除", pi._cookie_header == "wwrt=manualX")
    # CookieCloud 关闭 + 手工 header 仍在，下一轮应能重新取回
    pi.check_login_status = lambda page, task: _ready(True)
    await pi.refresh_cookie()
    check("下一轮从手工 header 重新取回并恢复",
          pi._cookie_valid is True and pi._saved_cookie is not None,
          f"valid={pi._cookie_valid} saved={pi._saved_cookie}")

    print("\n=== 10. 无可用 Cookie 时的退避 ===")
    sfn = os.path.join(tmpdir, "n.json")
    pn = new_plugin(sfn, _use_cookiecloud=False, _cookie_header="", _my_send=FakeSender())
    pn._launch_browser_context_with_retry = lambda headless=True: _ready(FakeContext())
    pn.check_login_status = lambda page, task: _ready(False)
    FakeSender.titles.clear()
    await pn.refresh_cookie()
    check("第 1 轮无 cookie 不判定失效", pn._cookie_valid is True)
    check("第 1 轮无 cookie 不通知", len(FakeSender.titles) == 0)


async def _ready(v):
    if isinstance(v, BaseException):
        raise v
    return v


asyncio.run(main_async())

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)