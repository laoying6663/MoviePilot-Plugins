"""
通知渠道勾选功能的离线自测（V3）。
重点验证：向后兼容（老配置不被改写）与多渠道路由正确。
"""
import os
import sys
import tempfile
import threading
import shutil
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

B = r"D:\minimax\dynamicwechat-backup"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ---- 复用 v3 测试里的桩 ----
import importlib.util

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "test_dynamicwechat_v3.py"), encoding="utf-8").read()
src = src.split("print(\"\\n=== 11")[0]      # 去掉后半段，避免重复执行
ns = {"__name__": "_stub", "__file__": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "test_dynamicwechat_v3.py")}
exec(compile(src, "stubs", "exec"), ns)

helper = ns["helper"]
DynamicWeChat = ns["DynamicWeChat"]
MySender = helper.MySender
import sys as _s
NotificationChannel = _s.modules["app.schemas.types"].NotificationChannel
_MsgType = _s.modules["app.schemas.types"].MessageType

print("\n=== 1. 渠道识别（向后兼容）===")
check("旧写法 WeChat,WangFeng 仍识别为 MoviePilot 渠道",
      MySender._detect_channel("WeChat,WangFeng") == "MoviePilot:wechat",
      MySender._detect_channel("WeChat,WangFeng"))
check("裸 WeChat 识别为 MoviePilot 渠道",
      MySender._detect_channel("WeChat") == "MoviePilot:wechat")
check("Telegram 识别为 MoviePilot 渠道",
      MySender._detect_channel("Telegram") == "MoviePilot:telegram")
check("SCT 开头仍是 Server酱",
      MySender._detect_channel("SCT123") == "ServerChan")
check("含 iyuu 仍是 IYUU",
      MySender._detect_channel("iyuu_xxx") == "IYUU")
check("其余仍是 PushPlus",
      MySender._detect_channel("abcdef") == "PushPlus")
check("空 token 不崩溃", MySender._detect_channel("") == "PushPlus")

print("\n=== 2. Token 解析（||Q 与 || 分隔）===")
s = MySender("WeChat,WangFeng||SCT123||Q")
check("||Q 被识别为静默标志", s.quiet_flag is True)
check("||Q 后的渠道被正确剥离", s.tokens == ["WeChat,WangFeng", "SCT123"], str(s.tokens))
check("渠道识别正确", s.channels == ["MoviePilot:wechat", "ServerChan"], str(s.channels))
check("MoviePilot 渠道不计入 other_channel",
      [c for c, _ in s.other_channel] == ["ServerChan"], str(s.other_channel))

s2 = MySender("Wechat||Telegram||SCT1")
check("多 MoviePilot 渠道 + 第三方可共存", s2.channels == ["MoviePilot:wechat", "MoviePilot:telegram", "ServerChan"],
      str(s2.channels))

print("\n=== 3. post_message 路由 ===")
calls = []


def fake_post(**kw):
    calls.append(kw)


s3 = MySender("Wechat,user123", func=fake_post)
err = s3._try_send("标题", "内容", None, "MoviePilot:wechat", token="Wechat,user123")
check("发送到指定渠道成功", err is None, str(err))
check("channel 传的是枚举成员",
      calls and calls[0]["channel"] is NotificationChannel.Wechat, str(calls[:1]))
check("mtype 传的是 MessageType.Plugin", calls and calls[0]["mtype"] is _MsgType.Plugin)
check("userid 后缀被单独解析", calls and calls[0].get("userid") == "user123", str(calls[:1]))
check("title/text 正确传递", calls and calls[0]["title"] == "标题" and calls[0]["text"] == "内容")

calls.clear()
s4 = MySender("Telegram", func=fake_post)
s4._try_send("t", "c", None, "MoviePilot:telegram", token="Telegram")
check("非微信渠道也能投递", calls and calls[0]["channel"] is NotificationChannel.Telegram, str(calls[:1]))
check("未指定 userid 时不传该参数", calls and "userid" not in calls[0])

calls.clear()
s5 = MySender("NotAChannel", func=fake_post)
err5 = s5._try_send("t", "c", None, "MoviePilot:notachannel", token="NotAChannel")
check("非法渠道名给出可读错误", err5 and "未知" in err5, str(err5))

s6 = MySender("Wechat")
err6 = s6._try_send("t", "c", None, "MoviePilot:wechat", token="Wechat")
check("无宿主能力时提示而非静默", err6 and "宿主" in err6, str(err6))

print("\n=== 4. 组合逻辑（关键：老配置不能被改写）===")
C = DynamicWeChat._compose_notification_token

check("未勾选渠道时原样返回（老配置不受影响）",
      C([], "WeChat,WangFeng||SCT123") == "WeChat,WangFeng||SCT123",
      C([], "WeChat,WangFeng||SCT123"))
check("None 渠道列表同样原样返回",
      C(None, "SCT123") == "SCT123")
check("勾选一个渠道 + 无第三方",
      C(["Wechat"], "") == "Wechat", C(["Wechat"], ""))
check("勾选渠道 + 第三方 Token 以 || 连接",
      C(["Wechat"], "SCT123") == "Wechat||SCT123", C(["Wechat"], "SCT123"))
check("多渠道按勾选顺序拼接",
      C(["Wechat", "Telegram"], "SCT1") == "Wechat||Telegram||SCT1",
      C(["Wechat", "Telegram"], "SCT1"))
check("||Q 结尾被保留",
      C(["Wechat"], "SCT1||Q") == "Wechat||SCT1||Q", C(["Wechat"], "SCT1||Q"))
check("空白项被过滤",
      C(["Wechat", "", "  "], "SCT1") == "Wechat||SCT1")
check("组合结果可被 MySender 正确解析",
      MySender(C(["Wechat", "Telegram"], "SCT1")).channels ==
      ["MoviePilot:wechat", "MoviePilot:telegram", "ServerChan"])

print("\n=== 5. 表单选项 ===")
p = DynamicWeChat.__new__(DynamicWeChat)
opts = p._mp_channel_options()
check("返回可勾选渠道列表", isinstance(opts, list) and len(opts) > 0, str(len(opts)))
check("选项含 title/value", all("title" in o and "value" in o for o in opts))
check("value 用枚举名（ASCII 稳定）",
      all(o["value"] == c.name for o, c in zip(opts, NotificationChannel)))
check("title 含中文显示名", any("微信" in o["title"] for o in opts), str(opts[:2]))

p._notification_token = "WeChat,WangFeng||SCT1"
found = p._current_mp_channels()
check("能从旧 token 回显已勾选渠道", found == ["Wechat"], str(found))
p._notification_token = "SCT1||Q"
check("纯第三方 token 不回显渠道", p._current_mp_channels() == [], str(p._current_mp_channels()))
check("映射回枚举名（大小写不敏感）",
      DynamicWeChat._map_channel_name("wechat") == "Wechat")

print("\n=== 6. init_plugin 向后兼容（老配置必须原样保留）===")


class Cfg(DynamicWeChat):
    """只提供 init_plugin 用到的最小环境，避免拉起浏览器。"""
    VERSION_FLAG = "v2"

    def __init__(self, config):
        self.get_data_path = lambda: __import__("pathlib").Path(tmpdir)
        self.get_state = lambda: False
        self.get_form = lambda: ({}, {})
        self.update_config = lambda *a, **k: None
        self.post_message = lambda **k: None
        self.systemmessage = type("SM", (), {"put": staticmethod(lambda *a: None)})()
        self.stop_service = lambda: True
        self._bg_tasks = None
        self._bg_threads = None
        self._file_lock = threading.Lock()
        self._qr_lock = threading.Lock()
        self._notify_lock = threading.Lock()
        self._homepage_logged = False
        self.version = "v2"
        try:
            self.init_plugin(config)
        except Exception as e:  # 只关心结果，不让环境噪声掩盖断言
            self._init_err = e


tmpdir = tempfile.mkdtemp()

# 老配置：没有 notify_channels 键
old = {"enabled": True, "notification_token": "WeChat,WangFeng||SCT123",
       "input_id_list": "1000002", "cron": "*/10 * * * *"}
p_old = Cfg(dict(old))
check("老配置：token 一字未改", p_old._notification_token == "WeChat,WangFeng||SCT123",
      p_old._notification_token)
check("老配置：渠道被回解析出来用于回显", p_old._notify_channels == ["Wechat"],
      str(p_old._notify_channels))
check("老配置：WeChat userid 未丢失（未参与重组）",
      p_old._my_send.tokens[0] == "WeChat,WangFeng", str(p_old._my_send.tokens))

# 新配置：勾选了渠道
new = {"enabled": True, "notify_channels": ["Wechat", "Telegram"],
       "notification_token": "SCT123", "input_id_list": "1000002", "cron": "*/10 * * * *"}
p_new = Cfg(dict(new))
check("新配置：勾选渠道与第三方合并",
      p_new._notification_token == "Wechat||Telegram||SCT123", p_new._notification_token)
check("新配置：渠道识别正确",
      p_new._my_send.channels == ["MoviePilot:wechat", "MoviePilot:telegram", "ServerChan"],
      str(p_new._my_send.channels))

# 新配置：明确清空勾选
clr = {"enabled": True, "notify_channels": [], "notification_token": "SCT123",
       "input_id_list": "1000002", "cron": "*/10 * * * *"}
p_clr = Cfg(dict(clr))
check("明确清空勾选后只剩第三方",
      p_clr._notification_token == "SCT123", p_clr._notification_token)

# 老配置里的 WeChat 仍走 MoviePilot 渠道，不会被当成 PushPlus
check("老配置的 WeChat 不会误判为 PushPlus",
      p_old._my_send.channels[0] == "MoviePilot:wechat", str(p_old._my_send.channels))

print("\n=== 7. 表单结构合法 ===")
src_txt = open(os.path.join(B, "plugins.v3", "dynamicwechat", "__init__.py"), encoding="utf-8").read()
check("含 VSelect 多选定义", "'component': 'VSelect'" in src_txt)
check("multiple 为 True", "'multiple': True" in src_txt)
check("模型为 notify_channels", "'model': 'notify_channels'" in src_txt)
check("userid 字段存在", "'model': 'wechat_userid'" in src_txt)
check("默认值含 notify_channels", '"notify_channels": []' in src_txt)
check("持久化含 notify_channels", '"notify_channels": self._notify_channels' in src_txt)

# 校验 get_form 返回结构里确实含该组件（用假实例调用）
class FakeP(DynamicWeChat):
    def __init__(self):
        self._notification_token = "Wechat||SCT1"
        self._use_cookiecloud = True
        self._local_scan = False
        self._my_send = None
        self._enabled = True
        self._await_ip = False
        self._cron = "*/10 * * * *"
        self._onlyonce = False
        self._input_id_list = "1000002"
        self._cookie_header = ""

fp = FakeP()
form = fp.get_form()
blob = json.dumps(form, ensure_ascii=False)
check("get_form() 可正常执行并含 VSelect", "VSelect" in blob)
check("get_form() 含 wechat_userid", "wechat_userid" in blob)
check("get_form() 含 notify_channels", "notify_channels" in blob)

print("\n=== 8. 最近验证时间戳与面板文案 ===")
import asyncio as _a
import time
import json as _json

PyCookieCloud = helper.PyCookieCloud
pT = DynamicWeChat.__new__(DynamicWeChat)
pT._settings_file_path = sfT = os.path.join(tmpdir, "ts.json")
pT._file_lock = threading.Lock()
pT._cookie_checked_at = 0
pT._cookie_lifetime = 0
pT._cookie_valid = True
pT._enabled = True
pT._is_special_upload = True
PyCookieCloud.write_settings(sfT, {})
_a.run(pT._mark_cookie_verified())
check("校验成功后写入时间戳", pT._cookie_checked_at > 0, str(pT._cookie_checked_at))
check("时间戳落盘", (_json.load(open(sfT, encoding="utf-8")) or {}).get("_cookie_checked_at", 0) > 0)

# 获得时刻：扫码时刷新，且把累加器清零
pT._cookie_lifetime = 999
_a.run(pT._mark_cookie_acquired())
check("扫码后记录获得时刻", pT._cookie_acquired_at > 0, str(pT._cookie_acquired_at))
check("扫码后累加器清零", pT._cookie_lifetime == 0, str(pT._cookie_lifetime))
d0 = _json.load(open(sfT, encoding="utf-8"))
check("获得时刻已落盘", d0.get("_cookie_acquired_at", 0) > 0)
check("落盘同时把 _cookie_lifetime 清零", d0.get("_cookie_lifetime", 1) == 0)

pT2 = DynamicWeChat.__new__(DynamicWeChat)
pT2._settings_file_path = sfT
pT2._cookie_checked_at = 0
pT2._cookie_acquired_at = 0
pT2._load_cookie_checked_at()
check("重启后恢复两个时间戳",
      pT2._cookie_checked_at == pT._cookie_checked_at and pT2._cookie_acquired_at == pT._cookie_acquired_at)

src3b = open(os.path.join(B, "plugins.v3", "dynamicwechat", "__init__.py"), encoding="utf-8").read()
check("面板文案为「Cookie 已使用」", "Cookie 已使用" in src3b)
check("掉线时显示「已掉线」", "Cookie 已掉线，请重新扫码" in src3b)
check("面板按时间戳实时计算（每分钟都会走）",
      "int(time.time()) - self._cookie_acquired_at" in src3b)
check("已引入 time 模块", "\nimport time\n" in src3b)
check("面板含「最近验证」时间戳", "最近验证" in src3b)
check("从未成功时给出明确提示", "尚未完成首轮校验" in src3b)
check("失效时清掉获得时刻", "'_cookie_acquired_at'] = 0" in src3b)
check("时间戳组件已接入页面内容", "checked_at_component if checked_at_component" in src3b)
check("续期成功后才打时间戳", "await self._mark_cookie_verified()" in src3b)

# 掉线时「已使用」必须冻结，不能继续涨
pZ = DynamicWeChat.__new__(DynamicWeChat)
pZ._is_special_upload = True
pZ._enabled = True
pZ._cookie_valid = False
pZ._cookie_acquired_at = int(time.time()) - 3600
pZ._cookie_lifetime = 0
pZ._cookie_checked_at = 0
pZ._qr_code_image = None
pZ._future_timestamp = 0
pZ._settings_file_path = sfT
pZ._file_lock = threading.Lock()
pg_txt = json.dumps(pZ.get_page(), ensure_ascii=False, default=str)
check("掉线时页面文案为「已掉线」", "Cookie 已掉线，请重新扫码" in pg_txt)
check("掉线时不再显示「已使用」", "Cookie 已使用" not in pg_txt)

# 在线时显示已使用，且按真实经过时间算
pO = DynamicWeChat.__new__(DynamicWeChat)
pO._is_special_upload = True
pO._enabled = True
pO._cookie_valid = True
pO._cookie_acquired_at = int(time.time()) - 3661
pO._cookie_lifetime = 0
pO._cookie_checked_at = int(time.time()) - 60
pO._qr_code_image = None
pO._future_timestamp = 0
pO._settings_file_path = sfT
pO._file_lock = threading.Lock()
ok_txt = json.dumps(pO.get_page(), ensure_ascii=False, default=str)
check("在线时显示「已使用」", "Cookie 已使用" in ok_txt)
check("已使用时长按真实经过时间计算（不是累加器）", "1小时1分钟" in ok_txt, ok_txt[:200])
check("在线时展示最近验证时间", "最近验证" in ok_txt)

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)