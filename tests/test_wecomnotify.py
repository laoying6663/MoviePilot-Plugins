"""DynamicWeChatSelfUse v5.0.0 离线自测。

v5.0.0 把企业微信自建应用那一整套能力（通知推送、`/wecom` 回调、远程命令、菜单）下线，
包内只剩 `__init__.py` + `helper.py`。因此本文件也做了大幅精简：

    删除  crypt.py / client.py / wecom_notify.py 的全部用例（SHA1、AES、token 缓存、菜单裁剪……）
    保留  保活 / 扫码 / 短信验证码 / 索引与文档契约

两种运行方式都支持：

    pytest tests/test_wecomnotify.py      # 与仓库其它测试一起被 pytest 收集
    python tests/test_wecomnotify.py       # 仓库既有的独立脚本风格，自带 PASS/FAIL 计数

不依赖宿主 SDK、不发起任何网络请求：所有断言都建立在源码 AST 与轻量函数提取之上。
"""

import ast
import asyncio
import copy
import html as _html
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# 仓库根目录：tests/test_wecomnotify.py -> 上一级即仓库根
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "dynamicwechatselfuse"
HOST = PLUGIN_DIR / "__init__.py"


# ========================================================================
# AST 辅助
# ========================================================================


def _host_source():
    return HOST.read_text(encoding="utf-8")


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


def _method_names(cls_node):
    return [
        n.name
        for n in cls_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _called_attrs(node):
    """收集方法体内所有 self.xxx() / obj.xxx() 调用，避免把 docstring 当成调用。"""
    return [
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]


def _form_models(fn_node):
    """静态收集表单结构里所有 ``'model': 'xxx'`` 字面量。"""
    found = set()
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "model"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found.add(value.value)
    return found


def _module_constant(name):
    """从 __init__.py 里取模块级字符串常量（不导入重型宿主依赖）。"""
    tree = ast.parse(_host_source())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"模块常量 {name} 不存在")


class _NullLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _extract_function(fn_name, extra=None):
    """从 __init__.py 里摘出单个方法，编译成模块级函数以便轻量调用。

    去掉装饰器，避免 @eventmanager.register / @staticmethod 的副作用。
    """
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    node = copy.deepcopy(_func_node(cls_node, fn_name))
    node.decorator_list = []
    wrapper = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(wrapper)

    from typing import Any, Dict, List, Optional, Tuple

    namespace = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Tuple": Tuple,
        "asyncio": asyncio,
        "logger": _NullLogger(),
        "Request": object,
        "EventType": _EVENT_TYPE,
        "_html_escape": _html.escape,
        "_VERIFY_CODE_PAGE_HTML": _module_constant("_VERIFY_CODE_PAGE_HTML"),
        "_VERIFY_CODE_SUBMIT_JS": _module_constant("_VERIFY_CODE_SUBMIT_JS"),
        "_VERIFY_CODE_POLL_JS": _module_constant("_VERIFY_CODE_POLL_JS"),
    }
    namespace.update(extra or {})
    exec(compile(wrapper, f"<{fn_name}>", "exec"), namespace)
    return namespace[fn_name]


class _EventType:
    """仅用于 get_command() 里的 EventType.PluginAction 求值。"""

    PluginAction = "PluginAction"
    NoticeMessage = "NoticeMessage"
    UserMessage = "UserMessage"


_EVENT_TYPE = _EventType()


class DynamicWeChatSelfUse:
    """测试用宿主替身：只承载被提取方法要读写的运行时属性。"""

    _qr_running = False
    _verification_code = None
    _last_code = ""
    _verify_code_required = False
    _verify_code_message = ""

    def update_config(self, config):
        self.saved_config = config


# ========================================================================
# 一、包结构与索引契约（v5.0.0 删减）
# ========================================================================

WECOM_MODULES = ("wecom_notify.py", "client.py", "crypt.py")

# v5.0.0 已从配置模型移除的 11 个企微字段
REMOVED_WECOM_FIELDS = {
    "name", "types", "corpid", "agentid", "secret", "proxy",
    "token", "encoding_aes_key", "userid", "admins", "create_menu",
}

# 保活相关字段（必须原样保留）
KEEPALIVE_FIELDS = {
    "enabled", "onlyonce", "cron", "refresh_cron", "notification_token",
    "notify_channels", "wechat_userid", "await_ip", "forced_update",
    "local_scan", "input_id_list", "cookie_header", "use_cookiecloud",
}

# cookie_header 是老的高级兜底字段：留在落库模型里，但配置页没有对应控件
FORM_MODELS = KEEPALIVE_FIELDS - {"cookie_header"}


def test_plugin_version_matches_index():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    version = _class_assign(cls_node, "plugin_version")
    index = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    assert version == "5.0.0"
    assert version == index["DynamicWeChatSelfUse"]["version"]


def test_index_history_has_v500_on_top():
    index = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    history = index["DynamicWeChatSelfUse"]["history"]
    assert next(iter(history)) == "v5.0.0", "新版本必须在 history 顶部"
    # 历史不能被清掉
    for old in ("v4.0.2", "v4.0.1", "v4.0.0", "v3.2.0"):
        assert old in history


def test_plugin_id_and_config_prefix_unchanged():
    """改了 ID / 前缀就等于换插件，用户配置与 Cookie 会全部失联。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    assert cls_node.name == "DynamicWeChatSelfUse"
    assert _class_assign(cls_node, "plugin_config_prefix") == "dynamicwechatselfuse_"


def test_index_has_no_wecomnotify_entry():
    index = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    assert "WecomNotify" not in index
    assert set(index) == {"DynamicWeChatSelfUse", "ParkingBot"}


def test_index_parkingbot_untouched():
    """重写 package.v3.json 时最容易被整篇覆盖，必须确认别人的条目还在。"""
    index = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    bot = index["ParkingBot"]
    assert bot["name"] and bot["version"]
    assert isinstance(bot.get("history"), dict) and bot["history"]


def test_wecomnotify_directory_removed():
    assert not (REPO_ROOT / "plugins.v3" / "wecomnotify").exists()


def test_wecom_modules_removed():
    """v5.0.0 应把三个企微模块整体删掉，而不是留着不用。"""
    for name in WECOM_MODULES:
        assert not (PLUGIN_DIR / name).exists(), f"{name} 应已删除"


def test_plugin_dir_only_two_modules():
    py_files = sorted(p.name for p in PLUGIN_DIR.glob("*.py"))
    assert py_files == ["__init__.py", "helper.py"], py_files


# ========================================================================
# 二、宿主类契约（不再有 mixin / 转发 / 回调解密）
# ========================================================================


def test_host_class_has_no_mixin():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    bases = [ast.unparse(b) for b in cls_node.bases]
    assert bases == ["_PluginBase"], bases


def test_host_module_has_no_wecom_import():
    body = ast.unparse(ast.parse(_host_source()))
    for token in ("wecom_notify", "WecomNotifyMixin", "WXBizMsgCrypt", "WecomClient"):
        assert token not in body, token


def test_host_has_no_wecom_methods():
    names = _method_names(_class_node(_host_source(), "DynamicWeChatSelfUse"))
    for gone in (
        "_init_wecom", "_stop_wecom", "_load_wecom_config", "_wecom_form",
        "_wecom_page", "_wecom_api", "wecom_verify", "wecom_message",
        "wecom_status", "wecom_notice_message", "wecom_plugin_action",
        "schedule_wecom_test_once", "_drop_shared_form_fields",
    ):
        assert gone not in names, f"{gone} 应已删除"


def test_host_registers_events_explicitly():
    """删掉企微转发后，宿主类只应保留 3 个事件处理器，且不再监听 NoticeMessage。"""
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
    assert set(names) == {
        "dynamicwechatselfuse_event",
        "push_qr_code_event",
        "talk",
    }, names
    assert not any("NoticeMessage" in text for _, text in registered)
    assert sum("PluginAction" in text for _, text in registered) == 2
    assert sum("UserMessage" in text for _, text in registered) == 1


def test_init_plugin_has_no_wecom_calls():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    called = _called_attrs(_func_node(cls_node, "init_plugin"))
    assert "_init_wecom" not in called
    assert "_load_wecom_config" not in called


def test_stop_service_has_no_stop_wecom():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    called = _called_attrs(_func_node(cls_node, "stop_service"))
    assert "_stop_wecom" not in called


def test_handle_once_tasks_has_no_wecom_test():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    called = _called_attrs(_func_node(cls_node, "_handle_once_tasks"))
    assert "schedule_wecom_test_once" not in called


def test_get_form_has_no_wecom_models():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    models = _form_models(_func_node(cls_node, "get_form"))
    assert not (models & REMOVED_WECOM_FIELDS), sorted(models & REMOVED_WECOM_FIELDS)
    # 保活字段一个都不能少
    assert FORM_MODELS <= models, sorted(FORM_MODELS - models)


def test_get_form_model_has_no_wecom_keys():
    fn = _extract_function("get_form")

    class _FormHost(DynamicWeChatSelfUse):
        # 有第三方通道时 await_ip 才会渲染；这里给个真值以便断言字段全集
        _my_send = SimpleNamespace(other_channel=True)

        def _mp_channel_options(self):
            return []

    _conf, model = fn(_FormHost())
    # 默认模型仍保留 cookie_header（落库用），只是配置页没有对应控件
    assert set(model) == KEEPALIVE_FIELDS


def test_get_form_no_longer_merges_mixin():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    body = ast.unparse(_func_node(cls_node, "get_form"))
    assert "_wecom_form" not in body
    assert "_drop_shared_form_fields" not in body


def test_update_config_writes_all_keepalive_fields():
    """__update_config() 是整体覆盖语义：少写一个键，保存时就把用户配置抹掉。"""
    fn = _extract_function("__update_config")
    host = DynamicWeChatSelfUse()
    for field in KEEPALIVE_FIELDS:
        setattr(host, "_" + field, None)
    host._manual_token = ""
    host._notify_channels = []
    host._wechat_userid = ""
    host._refresh_cron = ""
    host._input_id_list = ""
    host._cookie_header = ""
    host._cron = ""

    fn(host)
    saved = host.saved_config
    assert set(saved) == KEEPALIVE_FIELDS, sorted(set(saved) ^ KEEPALIVE_FIELDS)


def test_update_config_persists_manual_token_not_composed():
    """落库必须是用户原始输入，否则每保存一次就把 token 叠加一遍。"""
    fn = _extract_function("__update_config")
    host = DynamicWeChatSelfUse()
    for field in KEEPALIVE_FIELDS:
        setattr(host, "_" + field, None)
    host._manual_token = "SCTxxxx"
    host._notification_token = "Wechat||SCTxxxx"
    host._notify_channels = ["Wechat"]
    host._wechat_userid = ""
    host._refresh_cron = ""
    host._input_id_list = ""
    host._cookie_header = ""
    host._cron = ""
    fn(host)
    assert host.saved_config["notification_token"] == "SCTxxxx"
    assert host.saved_config["notify_channels"] == ["Wechat"]


def test_get_page_has_no_wecom_panel():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    body = ast.unparse(_func_node(cls_node, "get_page"))
    assert "_wecom_page" not in body
    assert "_verify_code_card" in body  # 验证码卡片仍要挂


def test_get_command_only_two():
    fn = _extract_function("get_command")
    cmds = fn()
    assert [c["cmd"] for c in cmds] == ["/push_qr", "/wxcode"]
    assert len({c["cmd"] for c in cmds}) == 2


def test_get_command_has_no_wecom_commands():
    fn = _extract_function("get_command")
    cmds = {c["cmd"] for c in fn()}
    assert "/wecom_status" not in cmds
    assert "/wecom_test" not in cmds


def test_get_api_only_verify_endpoints():
    fn = _extract_function("get_api")

    class _Host:
        api_verify_code = staticmethod(lambda request: None)
        api_verify_state = staticmethod(lambda: None)

    apis = fn(_Host())
    pairs = [(a["path"], tuple(a["methods"])) for a in apis]
    assert len(pairs) == len(set(pairs)), "API 端点不能重复注册"
    assert {a["path"] for a in apis} == {"/verify_code", "/verify_state"}
    assert not any(a["path"] == "/wecom" for a in apis)
    assert not any(a["path"] == "/status" for a in apis)


def test_all_api_endpoints_require_auth():
    """v5.0.0 不再有匿名回调端点，全部端点都必须是 bear。"""
    fn = _extract_function("get_api")

    class _Host:
        api_verify_code = staticmethod(lambda request: None)
        api_verify_state = staticmethod(lambda: None)

    for api in fn(_Host()):
        assert api["auth"] == "bear", api["path"]


def test_plugin_desc_mentions_builtin_channel():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    desc = _class_assign(cls_node, "plugin_desc")
    assert "MoviePilot 内置" in desc or "内置的微信通知" in desc
    # 不能还宣称支持回调与远程命令
    for gone in ("回调验签", "远程命令", "控制菜单"):
        assert gone not in desc, gone


# ========================================================================
# 三、短信验证码（v4.0.1 / v4.0.2 能力，v5.0.0 保留）
# ========================================================================


def test_verify_code_api_registered():
    """get_api() 里 /verify_code 必须是 bear + POST。"""
    fn = _extract_function("get_api")

    class _Host:
        api_verify_code = staticmethod(lambda request: None)
        api_verify_state = staticmethod(lambda: None)

    apis = fn(_Host())
    verify = [a for a in apis if a["path"] == "/verify_code"]
    assert len(verify) == 1
    assert verify[0]["auth"] == "bear"
    assert verify[0]["methods"] == ["POST"]
    assert verify[0]["endpoint"] is not None


def test_verify_code_page_has_input():
    """详情页必须有可输入的 input、提交按钮与正确的 API 路径。"""
    fn = _extract_function("_verify_code_card")
    host = DynamicWeChatSelfUse()
    host._qr_running = True
    host._verification_code = None
    host._verify_code_required = True
    host._verify_code_message = ""
    card = fn(host)
    assert len(card) == 1 and card[0]["component"] == "div"
    page = card[0]["html"]
    assert 'id="dwcsu_vcode"' in page
    assert "提交验证码" in page
    assert "plugin/DynamicWeChatSelfUse/verify_code" in page
    assert "window.MoviePilotAPI" in page
    assert "企业微信需要短信验证" in page
    assert "display:block" in page  # 需要验证码时必须可见
    # 内联 JS 要放进 onclick="..."，不能含双引号
    js = _module_constant("_VERIFY_CODE_SUBMIT_JS")
    assert '"' not in js
    assert "__PID__" in js and "verify_code?code=" in js
    assert "localStorage" in js  # 老版本前端的 fetch 回退


def test_verify_code_page_shows_received_state():
    fn = _extract_function("_verify_code_card")
    host = DynamicWeChatSelfUse()
    host._qr_running = True
    host._verification_code = "123456"
    host._verify_code_required = True
    host._verify_code_message = ""
    assert "已收到验证码" in fn(host)[0]["html"]


def test_verify_code_card_hidden_when_not_required():
    """没扫码 / 没到验证码步骤时，卡片必须是 display:none，不能常年占位。"""
    fn = _extract_function("_verify_code_card")
    host = DynamicWeChatSelfUse()
    host._qr_running = False
    host._verification_code = None
    host._verify_code_required = False
    host._verify_code_message = ""
    page = fn(host)[0]["html"]
    assert 'id="dwcsu_wrap"' in page
    assert "display:none" in page
    assert "display:block" not in page
    # 轮询钩子必须在，否则卡片永远没机会自动出现
    assert "verify_state" in page


def test_verify_code_card_html_is_well_formed():
    """两种 display 状态下标签都必须完整配对，否则 v-html 会吞掉后面的节点。"""
    fn = _extract_function("_verify_code_card")
    for required in (True, False):
        host = DynamicWeChatSelfUse()
        host._qr_running = required
        host._verification_code = None
        host._verify_code_required = required
        host._verify_code_message = "x"
        page = fn(host)[0]["html"]
        assert page.count("<div") == page.count("</div>"), page
        assert '<button' in page and "</button>" in page
        assert '<img' in page and "/>" in page


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


def test_submit_verify_code_marks_required():
    """提交成功后要把「需要验证码」置位，否则卡片会在登录完成前提前收起。"""
    fn = _extract_function("_submit_verify_code")

    class _Host:
        _qr_running = True
        _verification_code = None
        _last_code = ""
        _verify_code_required = False
        _verify_code_message = ""

    host = _Host()
    ok, _msg = fn(host, "123456")
    assert ok
    assert host._verify_code_required is True
    assert host._verify_code_message


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
        for forbidden in ("init_plugin", "update_config", "save_data"):
            assert forbidden not in called, f"{name} 不应调用 {forbidden}"

    # 验证码明文不能进日志
    body = ast.unparse(_func_node(cls_node, "_submit_verify_code"))
    assert "logger.info" in body
    assert "{digits}" not in body
    assert "f'" not in body and 'f"' not in body


def test_verify_state_api_registered():
    """新增的 /verify_state 必须是 bear + GET，且不能与既有端点重复。"""
    fn = _extract_function("get_api")

    class _Host:
        api_verify_code = staticmethod(lambda request: None)
        api_verify_state = staticmethod(lambda: None)

    apis = fn(_Host())
    state = [a for a in apis if a["path"] == "/verify_state"]
    assert len(state) == 1
    assert state[0]["auth"] == "bear"
    assert state[0]["methods"] == ["GET"]
    assert state[0]["endpoint"] is not None


def test_verify_state_endpoint_reports_flags():
    fn = _extract_function("api_verify_state")
    host = DynamicWeChatSelfUse()
    host._verify_code_required = True
    host._qr_running = True
    host._verification_code = None
    host._verify_code_message = "请输入手机收到的 6 位验证码"
    data = asyncio.run(fn(host))
    assert data["success"] is True
    assert data["data"]["need_code"] is True
    assert data["data"]["qr_running"] is True
    assert data["data"]["code_received"] is False
    assert data["data"]["message"]

    host._verify_code_required = False
    assert asyncio.run(fn(host))["data"]["need_code"] is False


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
        assert ("self._last_code = ''" in body or 'self._last_code = ""' in body), name
        assert "self._qr_running = True" in body, name


def test_verify_code_required_lifecycle():
    """状态位只在真正的验证码阶段为真，且所有退出路径都要复位。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    for name in ("check_login_status", "local_scanning", "_push_qr_code_async",
                 "init_plugin", "stop_service"):
        body = ast.unparse(_func_node(cls_node, name))
        assert "self._verify_code_required = False" in body, name

    # 置 True 的地方只能是「真的检测到验证码面板」和「提交成功」两处
    setters = []
    for node in cls_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "self._verify_code_required = True" in ast.unparse(node):
            setters.append(node.name)
    assert set(setters) == {"check_login_status", "_submit_verify_code"}, setters


def test_verify_code_poll_js_is_safe():
    """轮询脚本要能放进 onerror="..."，且必须请求 verify_state。"""
    js = _module_constant("_VERIFY_CODE_POLL_JS")
    assert '"' not in js
    assert "__PID__" in js and "verify_state" in js
    assert "setInterval" in js
    assert "localStorage" in js  # 老版本前端的 fetch 回退
    assert "need_code" in js
    assert "dwcsu_wrap" in js
    assert "focus()" in js


# ========================================================================
# 四、保活能力不回归
# ========================================================================


def test_keepalive_classes_still_present():
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    names = _method_names(cls_node)
    for kept in (
        "init_plugin", "stop_service", "check", "forced_change", "local_scanning",
        "check_login_status", "write_wan2_ip", "_push_qr_code_async", "mask_ip",
        "get_form", "get_page", "get_state", "get_command", "get_api",
    ):
        assert kept in names, f"{kept} 丢失"


def test_mask_ip_keeps_head_and_tail():
    fn = _extract_function("mask_ip")
    # 1.2.3.4 -> 1.*.*.4：保留首尾、中间按原长度打码
    assert fn("1.2.3.4") == "1.*.*.4"
    assert fn("not-an-ip") == "not-an-ip"


def test_manual_cookie_fallback_kept():
    """CookieCloud 不可用时要能用 cookie_header 兜底，这条老逻辑不能丢。"""
    body = ast.unparse(_class_node(_host_source(), "DynamicWeChatSelfUse"))
    assert "_cookie_header" in body
    assert "use_cookiecloud" in body


def test_await_ip_needs_other_channel():
    """没有第三方通道时 await_ip 无意义，init_plugin 里会强制关闭。"""
    cls_node = _class_node(_host_source(), "DynamicWeChatSelfUse")
    body = ast.unparse(_func_node(cls_node, "init_plugin"))
    assert "self._await_ip = False" in body


# ========================================================================
# 五、文档与索引一致性
# ========================================================================


def test_readme_has_no_callback_url():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "/api/v1/plugin/DynamicWeChatSelfUse/wecom" not in readme
    assert "/api/v1/plugin/WecomNotify/wecom" not in readme


def test_readme_documents_verify_code_page():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "短信验证码" in readme
    assert "提交验证码" in readme


def test_readme_points_to_builtin_channel():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "内置" in readme and "通知" in readme
    assert "5.0.0" in readme


def test_readme_command_table_only_two():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "/push_qr" in readme and "/wxcode" in readme
    # 命令表里不应再把已删命令当可用能力列出
    table_lines = [ln for ln in readme.splitlines() if ln.startswith("| `/")]
    joined = "\n".join(table_lines)
    assert "/wecom_status" not in joined
    assert "/wecom_test" not in joined


def test_handover_keeps_redlines_and_pitfalls():
    handover = (REPO_ROOT / "HANDOVER.md").read_text(encoding="utf-8")
    assert "## 6. 红线清单" in handover
    assert "## 7. 踩坑档案" in handover
    assert "7.16" in handover
    assert "PageRender" in handover


def test_handover_documents_v500_removal():
    handover = (REPO_ROOT / "HANDOVER.md").read_text(encoding="utf-8")
    assert "7.17" in handover
    assert "v5.0.0" in handover
    # 必须说明「删字段 == 删能力」这个判断，否则后人会以为字段是冗余的
    assert "能收不能回" in handover or "主动调" in handover


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
