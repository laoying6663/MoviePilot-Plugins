"""企业微信通知插件离线自测。

两种运行方式都支持：

    pytest tests/test_wecomnotify.py      # 与仓库其它测试一起被 pytest 收集
    python tests/test_wecomnotify.py       # 仓库既有的独立脚本风格，自带 PASS/FAIL 计数

覆盖两类纯逻辑：
    1. 企业微信回调加解密（SHA1 签名、AES-CBC、XML 解析）——不依赖宿主
    2. API 客户端（长文本切分、代理拼接、access_token 缓存、菜单裁剪）——用桩替换宿主 SDK

不发起任何真实网络请求。
"""

import datetime
import hashlib
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

# 仓库根目录：tests/test_wecomnotify.py -> 上一级即仓库根
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "wecomnotify"


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


def _install_host_stubs():
    """宿主不在时，注入最小可用的 app.sdk 桩，让 client.py 能独立导入。"""
    import importlib

    # 用 importlib 而不是 import 语句：后者会把 app 绑定成函数局部名，
    # 导致后面注入桩时 UnboundLocalError。
    try:
        importlib.import_module("app.sdk.logging")
        importlib.import_module("app.sdk.network")

        return
    except ImportError:
        pass

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    sdk_module = types.ModuleType("app.sdk")
    sdk_module.__path__ = []
    logging_mod = types.ModuleType("app.sdk.logging")
    network_mod = types.ModuleType("app.sdk.network")

    class _NullLogger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    logging_mod.logger = _NullLogger()
    network_mod.RequestUtils = _StubRequestUtils
    network_mod.UrlUtils = _StubUrlUtils
    sys.modules.update(
        {
            "app": app_module,
            "app.sdk": sdk_module,
            "app.sdk.logging": logging_mod,
            "app.sdk.network": network_mod,
        }
    )


def _load_module(name, filename):
    """优先按生产路径 app.plugins.wecomnotify 导入，退回按文件路径加载。"""
    try:
        module = importlib.import_module(f"app.plugins.wecomnotify.{name}")
        return module
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location(
        f"wecomnotify_{name}", PLUGIN_DIR / filename
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"wecomnotify_{name}"] = module
    spec.loader.exec_module(module)
    return module


crypt = _load_module("crypt", "crypt.py")
_install_host_stubs()
client = _load_module("client", "client.py")


def _requests():
    return sys.modules["app.sdk.network"].RequestUtils


def _reset_stub_state():
    RequestUtils = _requests()
    RequestUtils.calls = []
    RequestUtils.responses = []


@pytest.fixture(autouse=True)
def _autouse_reset():
    _reset_stub_state()


def _make_client(proxy=None):
    return client.WecomClient("corpid", "secret", "1000004", proxy=proxy)


def _valid_token(obj, token="TOKEN"):
    obj._access_token = token
    obj._expires_at = datetime.datetime.now() + datetime.timedelta(hours=1)


# ========================================================================
# 一、回调加解密
# ========================================================================

pytest.importorskip("Crypto.Cipher.AES", reason="缺少 pycryptodome，跳过加解密测试")

# 腾讯官方示例中的测试向量
TOKEN = "QDG6eK"
AESKEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
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


@pytest.mark.parametrize("bad_key", ["", "   ", "!!!notbase64!!!", "c2hvcnQ="])
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
# 二、API 客户端
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
                print(f"  FAIL  {label}  [{err}]")

    total = len(passed) + len(failed)
    print("=" * 62)
    print(f"  通过 {len(passed)} / {total}")
    if failed:
        print("  失败项：")
        for name, err in failed:
            print(f"    - {name}: {err}")
    if total == 0:
        # 一个用例都没跑到时必须判失败：宁可报错，也不能报「全绿」
        print("  错误：没有任何用例被执行，判定为失败")
        print("=" * 62)
        return 1
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
