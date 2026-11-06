"""DynamicWeChatSelfUse v4.0.1 离线自测。

文件名沿用 PR #1，实际测试的是合并后的 ``plugins.v3/dynamicwechatselfuse`` 包
（原独立插件 ``plugins.v3/wecomnotify`` 已并入并删除）。

两种运行方式都支持：

    pytest tests/test_wecomnotify.py      # 与仓库其它测试一起被 pytest 收集
    python tests/test_wecomnotify.py       # 仓库既有的独立脚本风格，自带 PASS/FAIL 计数

覆盖三类逻辑：
    1. 企业微信回调加解密（SHA1 签名、AES-CBC、XML 解析）——不依赖宿主
    2. API 客户端（长文本切分、代理拼接、access_token 缓存、菜单裁剪）——用桩替换宿主 SDK
    3. v4.0 合并契约（版本/索引、事件注册、命令与 API 唯一性、配置不丢、
       生命周期顺序、表单去重、回调路径）——AST 静态检查 + mixin 运行时桩

不发起任何真实网络请求。
"""

import ast
import asyncio
import base64
import html as _html
import copy
import datetime
import enum
import hashlib
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

# 仓库根目录：tests/test_wecomnotify.py -> 上一级即仓库根
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "dynamicwechatselfuse"
PKG_NAME = "_mptest_dws"


# ========================================================================
# 宿主 SDK 桩
# ========================================================================


class _StubResponse:
    def __init__(self, payload=None, status_code=200, content=b"", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.reason = "stub"
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("stub response without payload")
        return self._payload


class _StubRequestUtils:
    """记录调用并按队列返回预置响应。"""

    responses: list = []
    calls: list = []

    def __init__(self, timeout=None, content_type=None, **kwargs):
        self.timeout = timeout
        self.content_type = content_type

    def get_res(self, url, **kwargs):
        type(self).calls.append(("get_res", url))
        return type(self).responses.pop(0) if type(self).responses else None

    def get(self, url, **kwargs):
        type(self).calls.append(("get", url))
        return type(self).responses.pop(0) if type(self).responses else None

    def post(self, url=None, data=None, **kwargs):
        type(self).calls.append(("post", url))
        return type(self).responses.pop(0) if type(self).responses else None


class _StubUrlUtils:
    @staticmethod
    def adapt_request_url(base, path):
        return f"{base.rstrip('/')}/{path.lstrip('/')}"


class _NullLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _StubEvent:
    def __init__(self, event_data=None):
        self.event_data = event_data or {}


class _StubEventManager:
    def __init__(self):
        self.sent = []

    def send_event(self, etype, data):
        self.sent.append((etype, data))

    @staticmethod
    def register(_etype):
        def deco(fn):
            return fn
        return deco


class _StubScheduler:
    def __init__(self):
        self.jobs = []

    def add_plugin_once_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))


class _StubMessageType(enum.Enum):
    MovieStarted = "MovieStarted"
    DownloadAdded = "DownloadAdded"
    SubscribeAdded = "SubscribeAdded"
    Message = "Message"


class _StubPluginBase:
    def update_config(self, config):
        self.saved_config = config

    def get_data_path(self):
        return REPO_ROOT / "_tmp_data"


def _install_stubs():
    """离线自测始终使用桩，不依赖宿主是否已安装 app.sdk.*。"""
    app_module = types.ModuleType("app")
    app_module.__path__ = []
    sdk_module = types.ModuleType("app.sdk")
    sdk_module.__path__ = []
    schemas_module = types.ModuleType("app.schemas")
    schemas_module.__path__ = []

    types_mod = types.ModuleType("app.schemas.types")
    types_mod.EventType = types.SimpleNamespace(
        NoticeMessage="NoticeMessage",
        PluginAction="PluginAction",
        UserMessage="UserMessage",
    )
    types_mod.MessageType = _StubMessageType
    types_mod.NotificationChannel = types.SimpleNamespace(Wechat="Wechat")

    logging_mod = types.ModuleType("app.sdk.logging")
    logging_mod.logger = _NullLogger()

    network_mod = types.ModuleType("app.sdk.network")
    network_mod.RequestUtils = _StubRequestUtils
    network_mod.UrlUtils = _StubUrlUtils

    config_mod = types.ModuleType("app.sdk.config")
    config_mod.settings = types.SimpleNamespace(NGINX_PORT=3000, VERSION_FLAG="v3")

    events_mod = types.ModuleType("app.sdk.events")
    events_mod.Event = _StubEvent
    events_mod.eventmanager = _StubEventManager()

    scheduler_mod = types.ModuleType("app.sdk.scheduler")
    scheduler = _StubScheduler()
    scheduler_mod.add_plugin_once_job = scheduler.add_plugin_once_job
    scheduler_mod.jobs = scheduler.jobs

    plugin_mod = types.ModuleType("app.sdk.plugin")
    plugin_mod._PluginBase = _StubPluginBase

    fastapi_mod = types.ModuleType("fastapi")

    class _Request:
        pass

    fastapi_mod.Request = _Request

    starlette_mod = types.ModuleType("starlette")
    starlette_mod.__path__ = []
    responses_mod = types.ModuleType("starlette.responses")

    class _PlainTextResponse:
        def __init__(self, content="", **kwargs):
            self.content = content

    responses_mod.PlainTextResponse = _PlainTextResponse
    starlette_mod.responses = responses_mod

    sys.modules.update(
        {
            "app": app_module,
            "app.sdk": sdk_module,
            "app.schemas": schemas_module,
            "app.schemas.types": types_mod,
            "app.sdk.logging": logging_mod,
            "app.sdk.network": network_mod,
            "app.sdk.config": config_mod,
            "app.sdk.events": events_mod,
            "app.sdk.scheduler": scheduler_mod,
            "app.sdk.plugin": plugin_mod,
            "fastapi": fastapi_mod,
            "starlette": starlette_mod,
            "starlette.responses": responses_mod,
        }
    )
    app_module.sdk = sdk_module
    app_module.schemas = schemas_module
    for name, mod in (
        ("logging", logging_mod),
        ("network", network_mod),
        ("config", config_mod),
        ("events", events_mod),
        ("scheduler", scheduler_mod),
        ("plugin", plugin_mod),
    ):
        setattr(sdk_module, name, mod)
    schemas_module.types = types_mod


def _build_package():
    """构造虚拟包，按生产模块名加载 crypt / client / wecom_notify。

    相对导入 ``from .client import ...`` 依赖 spec 的包名，所以必须走
    ``<PKG_NAME>.<submodule>`` 而不是裸文件名。
    """
    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG_NAME] = pkg

    loaded = {}
    for name in ("crypt", "client", "wecom_notify"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG_NAME}.{name}", PLUGIN_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PKG_NAME}.{name}"] = module
        setattr(pkg, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


# 加解密用例需要 pycryptodome；没有它时 crypt.py 连导入都做不到
pytest.importorskip("Crypto.Cipher.AES", reason="缺少 pycryptodome，跳过加解密测试")

_install_stubs()
_MODULES = _build_package()
crypt = _MODULES["crypt"]
client = _MODULES["client"]
wecom_mod = _MODULES["wecom_notify"]
WecomNotifyMixin = wecom_mod.WecomNotifyMixin


def _requests():
    return sys.modules["app.sdk.network"].RequestUtils


def _scheduler():
    return sys.modules["app.sdk.scheduler"]


def _reset_stub_state():
    RequestUtils = _requests()
    RequestUtils.calls = []
    RequestUtils.responses = []
    _scheduler().jobs.clear()


@pytest.fixture(autouse=True)
def _autouse_reset():
    _reset_stub_state()


def _make_client(proxy=None):
    return client.WecomClient("corpid", "secret", "1000004", proxy=proxy)


def _valid_token(obj, token="TOKEN"):
    obj._access_token = token
    obj._expires_at = datetime.datetime.now() + datetime.timedelta(hours=1)


# ========================================================================
# AST / 宿主方法提取辅助（避免 import 整个 __init__.py 的重型宿主依赖）
# ========================================================================


def _host_source():
    return (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")


def _mixin_source():
    return (PLUGIN_DIR / "wecom_notify.py").read_text(encoding="utf-8")


def _class_node(source, name):
    tree = ast.parse(source)
    return next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name
    )


def _func_node(cls_node, name):
    return next(
        n
        for n in cls_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _class_assign(node, target):
    for item in node.body:
        if not isinstance(item, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == target for t in item.targets):
            return ast.literal_eval(item.value)
    raise AssertionError(f"类属性 {target} 不存在")


def _extract_function(fn_name, from_mixin=False, extra=None):
    """从源码里摘出单个方法，编译成模块级函数以便轻量调用。

    去掉装饰器，避免 @eventmanager.register / @staticmethod 的副作用。
    exec 之后回填一个同类别名，使 ``DynamicWeChatSelfUse._drop_shared_form_fields``
    这类递归调用能解析到本函数。
    """
    source = _mixin_source() if from_mixin else _host_source()
    cls_name = "WecomNotifyMixin" if from_mixin else "DynamicWeChatSelfUse"
    cls_node = _class_node(source, cls_name)
    node = copy.deepcopy(_func_node(cls_node, fn_name))
    node.decorator_list = []
    wrapper = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(wrapper)

    class _Alias:
        pass

    from typing import Any, Dict, List, Optional, Tuple

    namespace = dict(vars(wecom_mod))
    namespace.update(
        {
            "WecomNotifyMixin": WecomNotifyMixin,
            "DynamicWeChatSelfUse": _Alias,
            "Any": Any,
            "Dict": Dict,
            "List": List,
            "Optional": Optional,
            "Tuple": Tuple,
        }
    )
    namespace.update(extra or {})
    exec(compile(wrapper, f"<{fn_name}>", "exec"), namespace)
    fn = namespace[fn_name]
    _Alias._drop_shared_form_fields = staticmethod(fn)
    return fn


def _module_constant(name):
    """从 __init__.py 里取模块级字符串常量（不导入重型宿主依赖）。"""
    tree = ast.parse(_host_source())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"模块常量 {name} 不存在")


def _called_attrs(node):
    """收集方法体内所有 self.xxx() 调用，避免把 docstring 当成调用。"""
    return [
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]


def _collect_models(nodes):
    """递归收集表单结构里所有 props.model。"""
    found = set()
    if isinstance(nodes, dict):
        props = nodes.get("props")
        if isinstance(props, dict) and props.get("model"):
            found.add(str(props["model"]))
        for value in nodes.values():
            found |= _collect_models(value)
    elif isinstance(nodes, (list, tuple)):
        for value in nodes:
            found |= _collect_models(value)
    return found


class DynamicWeChatSelfUse(WecomNotifyMixin):
    """测试用宿主替身，类名必须是真实插件 ID（回调路径由类名推导）。"""

    # __update_config() 会读到的存量字段
    _cron = None
    _refresh_cron = ""
    _manual_token = ""
    _notify_channels = []
    _wechat_userid = ""
    _await_ip = False
    _forced_update = False
    _local_scan = False
    _input_id_list = ""
    _cookie_header = ""
    _use_cookiecloud = True

    eventmanager = _StubEventManager()

    def update_config(self, config):
        self.saved_config = config


# ========================================================================
# 一、回调加解密（crypt.py）
# ========================================================================

# 签名时间戳沿用腾讯公开示例；AESKey 由 32 字节随机材料生成合法的 43 位值
TOKEN = "QDG6eK"
AESKEY = base64.b64encode(b"0" * 32).decode("ascii").rstrip("=")
CORPID = "wx5823bf96d3bd56c7"
TIMESTAMP = "1409659813"
NONCE = "1372623149"

SAMPLE_XML = (
    "<xml><ToUserName><![CDATA[toUser]]></ToUserName>"
    "<FromUserName><![CDATA[fromUser]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[this is a test]]></Content>"
    "<MsgId>1234567890123456</MsgId>"
    "<AgentID>1</AgentID></xml>"
)


def test_aes_key_roundtrip_length():
    assert len(crypt.aes_key_from_encoding(AESKEY)) == 32


@pytest.mark.parametrize(
    "content",
    [
        SAMPLE_XML,
        "<xml><Content><![CDATA[]]></Content></xml>",
        "<xml><Content><![CDATA[" + ("测" * 5000) + "]]></Content></xml>",
        "<xml><Content><![CDATA[a & b < c > d \" e]]></Content></xml>",
    ],
    ids=["ascii", "empty", "long-cjk", "special-chars"],
)
def test_encrypt_decrypt_roundtrip(content):
    encrypted = crypt.encrypt_message(content, CORPID, AESKEY)
    assert crypt.decrypt_message(encrypted, CORPID, AESKEY) == content


def test_decrypt_rejects_wrong_corpid():
    encrypted = crypt.encrypt_message(SAMPLE_XML, CORPID, AESKEY)
    with pytest.raises(crypt.WXBizMsgCryptError):
        crypt.decrypt_message(encrypted, "another_corpid", AESKEY)


@pytest.mark.parametrize("bad_key", ["", "   ", "!!!notbase64!!!", "AAAA"])
def test_invalid_encoding_aes_key_raises_our_error(bad_key):
    """非法密钥必须抛 WXBizMsgCryptError，否则会逃出调用方的捕获范围。"""
    with pytest.raises(crypt.WXBizMsgCryptError):
        crypt.aes_key_from_encoding(bad_key)


def test_signature_matches_manual_sha1():
    expected = hashlib.sha1(
        "".join(sorted([TOKEN, TIMESTAMP, NONCE, "payload"])).encode("utf-8")
    ).hexdigest()
    assert crypt.get_signature(TOKEN, TIMESTAMP, NONCE, "payload") == expected


def test_verify_url_accepts_valid_signature():
    obj = crypt.WXBizMsgCrypt(TOKEN, AESKEY, CORPID)
    echostr = crypt.encrypt_message("echo-payload", CORPID, AESKEY)
    signature = crypt.get_signature(TOKEN, TIMESTAMP, NONCE, echostr)
    ret, reply = obj.verify_url(signature, TIMESTAMP, NONCE, echostr)
    assert ret == crypt.WXBizMsgCrypt_OK
    assert reply == "echo-payload"


def test_verify_url_rejects_bad_signature():
    obj = crypt.WXBizMsgCrypt(TOKEN, AESKEY, CORPID)
    echostr = crypt.encrypt_message("echo-payload", CORPID, AESKEY)
    ret, _ = obj.verify_url("deadbeef", TIMESTAMP, NONCE, echostr)
    assert ret == crypt.WXBizMsgCrypt_ValidateSignature_Error


def test_decrypt_msg_full_flow():
    obj = crypt.WXBizMsgCrypt(TOKEN, AESKEY, CORPID)
    encrypted = crypt.encrypt_message(SAMPLE_XML, CORPID, AESKEY)
    post_data = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    signature = crypt.get_signature(TOKEN, TIMESTAMP, NONCE, encrypted)
    ret, content = obj.decrypt_msg(post_data, signature, TIMESTAMP, NONCE)
    assert ret == crypt.WXBizMsgCrypt_OK
    assert content == SAMPLE_XML


def test_decrypt_msg_rejects_missing_encrypt_node():
    obj = crypt.WXBizMsgCrypt(TOKEN, AESKEY, CORPID)
    ret, _ = obj.decrypt_msg(
        "<xml><ToUserName>x</ToUserName></xml>", "sig", TIMESTAMP, NONCE
    )
    assert ret == crypt.WXBizMsgCrypt_ParseXml_Error


def test_encrypt_msg_roundtrip():
    """被动回复加密后的密文应能被标准流程解回原文。"""
    obj = crypt.WXBizMsgCrypt(TOKEN, AESKEY, CORPID)
    reply = "<xml><Content><![CDATA[回复内容]]></Content></xml>"
    ret, encrypted_xml = obj.encrypt_msg(reply, NONCE, TIMESTAMP)
    assert ret == crypt.WXBizMsgCrypt_OK

    match = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>", encrypted_xml, re.S)
    assert match, "回复 XML 必须包含 Encrypt 节点"
    signature = crypt.get_signature(TOKEN, TIMESTAMP, NONCE, match.group(1))
    ret, plain = obj.decrypt_msg(encrypted_xml, signature, TIMESTAMP, NONCE)
    assert ret == crypt.WXBizMsgCrypt_OK
    assert plain == reply


def test_parse_xml_message_fields():
    info = crypt.parse_xml_message(SAMPLE_XML)
    assert info["FromUserName"] == "fromUser"
    assert info["MsgType"] == "text"
    assert info["Content"] == "this is a test"


def test_parse_xml_message_handles_garbage():
    assert crypt.parse_xml_message("not xml at all") == {}


# ========================================================================
# 二、API 客户端（client.py）
# ========================================================================


@pytest.mark.parametrize(
    "content,limit",
    [
        ("短文本", 2048),
        ("中文" * 2000, 2048),
        ("a" * 5000, 2048),
        ("x\n" * 5000, 2048),
        ("中" * 5000, 100),
    ],
    ids=["short", "cjk-multiline", "long-ascii-line", "many-lines", "tiny-limit-cjk"],
)
def test_split_content_respects_byte_limit(content, limit):
    chunks = client.WecomClient._split_content(content, limit)
    assert chunks, "切分结果不应为空"
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= limit


def test_split_content_does_not_corrupt_multibyte_chars():
    chunks = client.WecomClient._split_content("中" * 5000, 100)
    assert "".join(chunks) == "中" * 5000


def test_split_content_keeps_all_lines():
    source = "\n".join(f"第{i}行内容测试" for i in range(400))
    chunks = client.WecomClient._split_content(source, 300)
    assert "\n".join(chunks) == "\n".join(c for c in source.split("\n") if c.strip())


def test_default_api_base():
    assert _make_client().proxy == "https://qyapi.weixin.qq.com"


def test_custom_proxy_applied():
    obj = _make_client(proxy="http://192.168.55.96:3100/")
    assert obj._send_msg_url.startswith(
        "http://192.168.55.96:3100/cgi-bin/message/send"
    )


def test_proxy_trailing_slash_not_duplicated():
    obj = _make_client(proxy="http://192.168.55.96:3100")
    assert "//cgi-bin" not in obj._token_url


@pytest.mark.parametrize(
    "args",
    [("c", "s", "1"), ("c", "s", ""), ("c", "", "1"), ("", "s", "1")],
)
def test_is_configured(args):
    expected = bool(args[0] and args[1] and args[2])
    assert client.WecomClient(*args).is_configured() == expected


def test_token_is_cached():
    obj = _make_client()
    _requests().responses = [
        _StubResponse({"errcode": 0, "access_token": "TK1", "expires_in": 7200})
    ]
    assert obj.get_access_token() == "TK1"
    _requests().responses = []
    assert obj.get_access_token() == "TK1"
    assert len(_requests().calls) == 1, "缓存命中时不应再次请求"


def test_token_force_refresh():
    obj = _make_client()
    _requests().responses = [
        _StubResponse({"errcode": 0, "access_token": "TK1", "expires_in": 7200})
    ]
    obj.get_access_token()
    _requests().responses = [
        _StubResponse({"errcode": 0, "access_token": "TK2", "expires_in": 7200})
    ]
    assert obj.get_access_token(force=True) == "TK2"


def test_token_refreshes_when_expired():
    obj = _make_client()
    obj._access_token = "OLD"
    obj._expires_at = datetime.datetime.now() - datetime.timedelta(minutes=1)
    _requests().responses = [
        _StubResponse({"errcode": 0, "access_token": "NEW", "expires_in": 7200})
    ]
    assert obj.get_access_token() == "NEW"


def test_token_error_returns_none():
    obj = _make_client()
    _requests().responses = [
        _StubResponse({"errcode": 40013, "errmsg": "invalid corpid"})
    ]
    assert obj.get_access_token() is None


def test_create_menus_posts_with_agentid():
    obj = _make_client()
    _valid_token(obj)
    _requests().responses = [_StubResponse({"errcode": 0})]
    obj.create_menus({"/a": {"desc": "命令A", "category": "组1"}})
    posted = [url for method, url in _requests().calls if method == "post"]
    assert posted and "menu/create" in posted[0]
    assert "agentid=1000004" in posted[0]


def test_create_menus_skips_commands_without_category():
    obj = _make_client()
    _valid_token(obj)
    obj.create_menus({"/a": {"desc": "无分类"}})
    assert not [c for c in _requests().calls if c[0] == "post"]


def test_create_menus_with_empty_commands_sends_nothing():
    obj = _make_client()
    _valid_token(obj)
    assert obj.create_menus({}) is None
    assert not [c for c in _requests().calls if c[0] == "post"]


def test_download_media_returns_bytes():
    obj = _make_client()
    _valid_token(obj)
    _requests().responses = [
        _StubResponse(content=b"\x89PNG\r\n\x1a\n", headers={"Content-Type": "image/png"})
    ]
    assert obj.download_media("MID") == b"\x89PNG\r\n\x1a\n"


def test_download_media_rejects_json_error_body():
    """企业微信失败时返回 JSON 错误体，不能当成素材内容。"""
    obj = _make_client()
    _valid_token(obj)
    _requests().responses = [
        _StubResponse({"errcode": 40001}, headers={"Content-Type": "application/json"})
    ]
    assert obj.download_media("MID") is None


def test_download_media_with_empty_id():
    assert _make_client().download_media("") is None


# ========================================================================
# 三、v4.0 合并契约：索引 / 结构 / 红线
# ========================================================================


def test_plugin_version_matches_index():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    version = _class_assign(cls_node, "plugin_version")
    index = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    assert version == "4.0.1"
    assert version == index["DynamicWeChatSelfUse"]["version"]
    assert "v4.0.1" in index["DynamicWeChatSelfUse"]["history"]
    assert "v4.0.0" in index["DynamicWeChatSelfUse"]["history"]


def test_plugin_id_and_config_prefix_unchanged():
    """改了 ID / 前缀就等于换插件，用户配置与 Cookie 会全部失联。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    assert cls_node.name == "DynamicWeChatSelfUse"
    assert _class_assign(cls_node, "plugin_config_prefix") == "dynamicwechatselfuse_"


def test_index_has_no_wecomnotify_entry():
    index = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    assert "WecomNotify" not in index
    assert set(index) == {"DynamicWeChatSelfUse", "ParkingBot"}


def test_wecomnotify_directory_removed():
    assert not (REPO_ROOT / "plugins.v3" / "wecomnotify").exists()


def test_host_class_inherits_mixin():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    bases = [ast.unparse(b) for b in cls_node.bases]
    assert "WecomNotifyMixin" in bases
    assert "_PluginBase" in bases


def test_mixin_has_no_event_registration():
    """装饰器留在 mixin 里会导致宿主类继承后重复注册。"""
    tree = ast.parse(_mixin_source())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            assert "register" not in ast.unparse(deco), (
                f"mixin 方法 {node.name} 不应带 @eventmanager.register"
            )


def test_host_registers_events_explicitly():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    registered = []
    for node in cls_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            text = ast.unparse(deco)
            if "eventmanager.register" in text:
                registered.append((node.name, text))
    names = [name for name, _ in registered]
    assert len(names) == len(set(names)), "事件方法不能重复注册"
    assert "wecom_notice_message" in names
    assert "wecom_plugin_action" in names
    assert any("NoticeMessage" in text for _, text in registered)
    assert any("PluginAction" in text for _, text in registered)

    notice = _func_node(cls_node, "wecom_notice_message")
    assert "WecomNotifyMixin.notice_message(self, event)" in ast.unparse(notice)
    action = _func_node(cls_node, "wecom_plugin_action")
    assert "WecomNotifyMixin.plugin_action(self, event)" in ast.unparse(action)


def test_init_wecom_after_stop_service():
    """_init_wecom() 放在 stop_service() 之前会被立刻释放。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    init_node = _func_node(cls_node, "init_plugin")
    calls = [
        (n.lineno, n.func.attr)
        for n in ast.walk(init_node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    stop_lines = [ln for ln, attr in calls if attr == "stop_service"]
    init_lines = [ln for ln, attr in calls if attr == "_init_wecom"]
    assert stop_lines and init_lines
    assert min(init_lines) > max(stop_lines)


def test_early_return_branch_loads_wecom_config():
    """后台任务没退干净的提前 return 分支也必须先把企微配置读回。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    init_node = _func_node(cls_node, "init_plugin")
    branch = next(
        n
        for n in init_node.body
        if isinstance(n, ast.If) and "stop_service" in ast.unparse(n.test)
    )
    body = "\n".join(ast.unparse(stmt) for stmt in branch.body)
    assert "_load_wecom_config" in body
    assert "__update_config" in body


def test_get_api_delegates_to_mixin():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    fn = _func_node(cls_node, "get_api")
    assert "self._wecom_api()" in ast.unparse(fn)


def test_get_form_merges_both_models():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    fn = _func_node(cls_node, "get_form")
    body = ast.unparse(fn)
    assert "self._wecom_form()" in body
    assert "self._drop_shared_form_fields" in body
    assert "wecom_model.pop" in body
    assert "legacy_model.update(wecom_model)" in body


def test_get_page_appends_wecom_panel():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    fn = _func_node(cls_node, "get_page")
    assert "base_content + self._wecom_page()" in ast.unparse(fn)


def test_readme_documents_v4_callback_url():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "/api/v1/plugin/DynamicWeChatSelfUse/wecom" in readme
    assert "/api/v1/plugin/WecomNotify/wecom" not in readme


def test_readme_documents_verify_code_page():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "短信验证码" in readme
    assert "提交验证码" in readme
    assert "不会触发保存配置" in readme


def test_handover_keeps_redlines_and_pitfalls():
    handover = (REPO_ROOT / "HANDOVER.md").read_text(encoding="utf-8")
    assert "## 6. 红线清单" in handover
    assert "## 7. 踩坑档案" in handover
    assert "5.2 真实宿主验收" in handover
    assert "7.16" in handover
    assert "PageRender" in handover


# ========================================================================
# 四、v4.0 合并契约：运行时
# ========================================================================

WECOM_FORM_MODELS = {
    "enabled", "onlyonce", "name", "types", "corpid", "agentid", "secret",
    "proxy", "token", "encoding_aes_key", "userid", "admins", "create_menu",
}

UPDATE_CONFIG_KEYS = {
    "enabled", "onlyonce", "cron", "refresh_cron", "notification_token",
    "notify_channels", "wechat_userid", "await_ip", "forced_update",
    "local_scan", "input_id_list", "cookie_header", "use_cookiecloud",
    "name", "types", "corpid", "agentid", "secret", "proxy", "token",
    "encoding_aes_key", "userid", "admins", "create_menu",
}


def _sample_wecom_config():
    return {
        "enabled": True,
        "onlyonce": False,
        "name": "测试渠道",
        "types": ["MovieStarted", "Message"],
        "corpid": "corp_demo",
        "agentid": "1000004",
        "secret": "secret_demo",
        "proxy": "http://127.0.0.1:9",
        "token": "TOKEN",
        "encoding_aes_key": AESKEY,
        "userid": "zhangsan",
        "admins": "alice,bob，carol",
        "create_menu": False,
    }


def test_update_config_writes_all_wecom_fields():
    """update_config 是整体覆盖，漏一个字段就会在保存/重载时抹掉凭据。"""
    fn = _extract_function("__update_config")
    host = DynamicWeChatSelfUse()
    host._load_wecom_config(_sample_wecom_config())
    fn(host)
    saved = host.saved_config
    assert UPDATE_CONFIG_KEYS.issubset(saved.keys())
    assert saved["corpid"] == "corp_demo"
    assert saved["secret"] == "secret_demo"
    assert saved["encoding_aes_key"] == AESKEY
    assert saved["admins"] == "alice,bob,carol"
    assert saved["create_menu"] is False
    assert saved["notification_token"] == ""


def test_load_wecom_config_reads_all_fields():
    host = DynamicWeChatSelfUse()
    host._load_wecom_config(_sample_wecom_config())
    assert host._corpid == "corp_demo"
    assert host._agentid == "1000004"
    assert host._secret == "secret_demo"
    assert host._userid == "zhangsan"
    assert host._admins == ["alice", "bob", "carol"]
    assert host._types == ["MovieStarted", "Message"]
    assert host._create_menu is False
    assert host._name == "测试渠道"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("false", False), ("False", False), ("0", False), ("no", False),
        ("", False), (" off ", False), (None, False), (False, False),
        ("true", True), ("1", True), ("yes", True), ("ON", True),
        (True, True), (1, True),
    ],
)
def test_as_bool_rejects_false_strings(value, expected):
    assert WecomNotifyMixin._as_bool(value) is expected


def test_commands_are_unique():
    fn = _extract_function("get_command")
    commands = fn()
    cmds = [item["cmd"] for item in commands]
    assert len(cmds) == len(set(cmds)), "命令不能重复注册"
    assert set(cmds) == {"/push_qr", "/wxcode", "/wecom_status", "/wecom_test"}
    assert len([i for i in commands if i.get("category")]) == 2


def test_api_paths_unique_and_anonymous():
    host = DynamicWeChatSelfUse()
    host._init_wecom({})
    apis = host._wecom_api()
    pairs = [(a["path"], tuple(a["methods"])) for a in apis]
    assert len(pairs) == len(set(pairs)), "API 端点不能重复注册"
    callbacks = [a for a in apis if a["path"] == "/wecom"]
    assert len(callbacks) == 2
    assert {m for a in callbacks for m in a["methods"]} == {"GET", "POST"}
    assert all(a["auth"] == "none" for a in callbacks)
    status = [a for a in apis if a["path"] == "/status"]
    assert len(status) == 1 and status[0]["auth"] == "bear"


def test_callback_path_uses_host_class_name():
    host = DynamicWeChatSelfUse()
    host._init_wecom({"enabled": True})
    page = json.dumps(host._wecom_page(), ensure_ascii=False)
    assert "/api/v1/plugin/DynamicWeChatSelfUse/wecom" in page
    assert "WecomNotify/wecom" not in page


def test_wecom_form_has_expected_models():
    host = DynamicWeChatSelfUse()
    conf, model = host._wecom_form()
    assert WECOM_FORM_MODELS.issubset(_collect_models(conf))
    assert WECOM_FORM_MODELS.issubset(set(model))


def test_form_drops_shared_models():
    fn = _extract_function("_drop_shared_form_fields")
    host = DynamicWeChatSelfUse()
    conf, _ = host._wecom_form()
    cleaned = fn(conf)
    models = _collect_models(cleaned)
    assert "enabled" not in models
    assert "onlyonce" not in models
    assert "corpid" in models
    assert "create_menu" in models


def test_init_and_stop_wecom_repeatable():
    host = DynamicWeChatSelfUse()
    for _ in range(3):
        host._init_wecom({"corpid": "c", "secret": "s", "agentid": "1"})
        assert host._client is not None
        host._stop_wecom()
        assert host._client is None
        assert host._last_send == ""


def test_skip_wecom_test_when_not_configured():
    _scheduler().jobs.clear()
    host = DynamicWeChatSelfUse()
    host._init_wecom({})
    host.schedule_wecom_test_once()
    assert not _scheduler().jobs


def test_schedule_wecom_test_when_configured():
    _scheduler().jobs.clear()
    host = DynamicWeChatSelfUse()
    host._init_wecom({"corpid": "c", "secret": "s", "agentid": "1"})
    host.schedule_wecom_test_once()
    assert len(_scheduler().jobs) == 1
    args, kwargs = _scheduler().jobs[0]
    assert args[0] == "DynamicWeChatSelfUse"
    assert args[1] == "wecom_test_once"
    assert kwargs.get("delay_seconds") == 3


# ========================================================================
# 四、v4.0.1 详情页短信验证码入口
# ========================================================================


def test_verify_code_api_registered():
    """get_api() 合并后不能出现重复路径，新端点必须是 bear + POST。"""
    fn = _extract_function("get_api")
    inner = DynamicWeChatSelfUse()
    inner._init_wecom({})

    class _Wrapper:
        api_verify_code = staticmethod(lambda request: None)

        def __init__(self, host):
            self._host = host

        def _wecom_api(self):
            return self._host._wecom_api()

    apis = fn(_Wrapper(inner))
    pairs = [(a["path"], tuple(a["methods"])) for a in apis]
    assert len(pairs) == len(set(pairs)), "API 端点不能重复注册"
    verify = [a for a in apis if a["path"] == "/verify_code"]
    assert len(verify) == 1
    assert verify[0]["auth"] == "bear"
    assert verify[0]["methods"] == ["POST"]
    assert verify[0]["endpoint"] is not None


def test_verify_code_page_has_input():
    """详情页必须有可输入的 input、提交按钮与正确的 API 路径。"""
    fn = _extract_function(
        "_verify_code_card",
        extra={
            "_VERIFY_CODE_PAGE_HTML": _module_constant("_VERIFY_CODE_PAGE_HTML"),
            "_VERIFY_CODE_SUBMIT_JS": _module_constant("_VERIFY_CODE_SUBMIT_JS"),
            "_html_escape": _html.escape,
        },
    )
    host = DynamicWeChatSelfUse()
    host._qr_running = True
    host._verification_code = None
    host._verify_code_message = ""
    card = fn(host)
    assert len(card) == 1 and card[0]["component"] == "div"
    page = card[0]["html"]
    assert 'id="dwcsu_vcode"' in page
    assert "提交验证码" in page
    assert "plugin/DynamicWeChatSelfUse/verify_code" in page
    assert "window.MoviePilotAPI" in page
    assert "扫码任务进行中" in page
    # 内联 JS 要放进 onclick="..."，不能含双引号
    js = _module_constant("_VERIFY_CODE_SUBMIT_JS")
    assert '"' not in js
    assert "__PID__" in js and "verify_code?code=" in js
    assert "localStorage" in js  # 老版本前端的 fetch 回退


def test_verify_code_page_shows_received_state():
    fn = _extract_function(
        "_verify_code_card",
        extra={
            "_VERIFY_CODE_PAGE_HTML": _module_constant("_VERIFY_CODE_PAGE_HTML"),
            "_VERIFY_CODE_SUBMIT_JS": _module_constant("_VERIFY_CODE_SUBMIT_JS"),
            "_html_escape": _html.escape,
        },
    )
    host = DynamicWeChatSelfUse()
    host._qr_running = True
    host._verification_code = "123456"
    host._verify_code_message = ""
    assert "已收到验证码" in fn(host)[0]["html"]


def test_submit_verify_code_validates_and_sets_state():
    fn = _extract_function("_submit_verify_code")

    class _Host:
        _qr_running = True
        _verification_code = None
        _last_code = ""

    host = _Host()
    ok, msg = fn(host, "12 34-56")
    assert ok and "已提交" in msg
    assert host._verification_code == "123456" and host._last_code == "123456"

    ok, msg = fn(host, "123456")
    assert not ok and "已提交过" in msg  # 去重

    host2 = _Host()
    host2._qr_running = False
    ok, msg = fn(host2, "987654")
    assert not ok and "没有等待验证码" in msg

    host3 = _Host()
    ok, msg = fn(host3, "12345")
    assert not ok and "6 位数字" in msg


def test_verify_code_endpoint_accepts_query_and_body():
    """端点同时兼容 query 参数与 JSON body，且返回标准信封。"""
    fn = _extract_function("api_verify_code")
    submit = _extract_function("_submit_verify_code")

    class _Host:
        _qr_running = True
        _verification_code = None
        _last_code = ""
        _verify_code_message = ""

        def _submit_verify_code(self, code):
            return submit(self, code)

    class _Req:
        def __init__(self, query=None, payload=None):
            self.query_params = query or {}
            self._payload = payload

        async def json(self):
            if self._payload is None:
                raise ValueError("no body")
            return self._payload

    h1 = _Host()
    r1 = asyncio.run(fn(h1, _Req(query={"code": "123456"})))
    assert r1["success"] is True and h1._verification_code == "123456"
    assert h1._verify_code_message == r1["message"]

    h2 = _Host()
    r2 = asyncio.run(fn(h2, _Req(payload={"code": "654321"})))
    assert r2["success"] is True and h2._verification_code == "654321"

    h3 = _Host()
    r3 = asyncio.run(fn(h3, _Req(query={"code": "12x"})))
    assert r3["success"] is False and "6 位数字" in r3["message"]


def test_verify_code_endpoint_never_persists():
    """端点绝不能触发配置保存/重载，否则会杀掉正在等码的扫码任务。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    for name in ("api_verify_code", "_submit_verify_code"):
        called = _called_attrs(_func_node(cls_node, name))
        for forbidden in ("init_plugin", "update_config", "__update_config", "save_data"):
            assert forbidden not in called, f"{name} 不应调用 {forbidden}"

    # 验证码明文不能进日志
    body = ast.unparse(_func_node(cls_node, "_submit_verify_code"))
    assert "logger.info" in body
    assert "{digits}" not in body
    assert "f'" not in body and 'f"' not in body


def test_verify_code_wait_is_polling():
    """旧版固定干等 30 秒，现在必须是轮询且收到即刻继续。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    body = ast.unparse(_func_node(cls_node, "check_login_status"))
    assert "VERIFY_CODE_WAIT_SECONDS" in body
    assert "VERIFY_CODE_POLL_INTERVAL" in body
    assert "range(30)" not in body
    assert "if self._verification_code:" in body


def test_new_qr_task_clears_stale_code():
    """新一轮扫码必须清空上一轮残留验证码，避免直接填入旧码。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    for name in ("local_scanning", "_push_qr_code_async"):
        body = ast.unparse(_func_node(cls_node, name))
        assert "self._verification_code = None" in body, name
        # ast.unparse 会把源码里的 "" 统一渲染成 ''
        assert ("self._last_code = ''" in body or 'self._last_code = ""' in body), name
        assert "self._qr_running = True" in body, name


# ========================================================================
# 独立脚本入口
# ========================================================================


def _run_standalone():
    """按仓库既有风格输出 PASS/FAIL 计数，并以退出码表示结果。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    passed, failed = [], []
    # 注意：dir() 在函数内返回的是局部名，必须显式取模块命名空间，
    # 否则一个用例都跑不到却会报「通过 0 / 0」并以 0 退出（假绿）。
    module_globals = sys.modules[__name__]
    for name in sorted(dir(module_globals)):
        if not name.startswith("test_"):
            continue
        func = getattr(module_globals, name)
        if not callable(func):
            continue

        # 展开 parametrize：pytest 会把参数集挂在 pytestmark 上，
        # 独立脚本模式下要自己展开，否则这类用例会悄悄不执行。
        cases = [{}]
        for mark in getattr(func, "pytestmark", []) or []:
            if getattr(mark, "name", "") != "parametrize":
                continue
            argnames = mark.args[0]
            argvalues = mark.args[1]
            if isinstance(argnames, str):
                argnames = [n.strip() for n in argnames.split(",") if n.strip()]
            expanded = []
            for values in argvalues:
                if len(argnames) == 1:
                    values = (values,)
                expanded.append(dict(zip(argnames, values)))
            cases = expanded

        for kwargs in cases:
            label = name if not kwargs else f"{name}[{list(kwargs.values())}]"
            try:
                _reset_stub_state()
                func(**kwargs)
                passed.append(label)
                print(f"  PASS  {label}")
            except Exception as err:
                failed.append((label, err))
                print(f"  FAIL  {label}  [{type(err).__name__}: {err}]")

    total = len(passed) + len(failed)
    print("=" * 62)
    print(f"  通过 {len(passed)} / {total}")
    if failed:
        print("  失败项：")
        for name, err in failed:
            print(f"    - {name}: {type(err).__name__}: {err}")
    if total == 0:
        # 一个用例都没跑到时必须判失败：宁可报错，也不能报「全绿」
        print("  错误：没有任何用例被执行，判定为失败")
        print("=" * 62)
        return 1
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
