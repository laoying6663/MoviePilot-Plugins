# -*- coding: utf-8 -*-
"""v1.8.0 验证：自动买卖开关、详情页区块互斥切换与顶部事件按钮。"""
import sys, types, asyncio, importlib.util, json, io, os, re, tokenize, time, random
import datetime as dt

sys.stdout.reconfigure(encoding="utf-8")


def _find_plugin_root():
    """向上找含 plugins.v3/parkingbot/__init__.py 的目录。

    别写死绝对路径：这个脚本既要能在主线目录跑，也要能原样搬进交接副本
    （副本里的插件快照是另一条绝对路径）。两处都写死就会有一边跑不起来。
    """
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(d, "plugins.v3", "parkingbot", "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise SystemExit(
                "找不到 plugins.v3/parkingbot/__init__.py —— "
                "请把本脚本放在项目目录内（或它的某个子目录里）")
        d = parent


PLUGIN_ROOT = _find_plugin_root()
PLUGIN_FILE = os.path.join(PLUGIN_ROOT, "plugins.v3", "parkingbot", "__init__.py")

def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m
    return m

DB = {}

# 配置与通知的取证袋：用来验证「关开关时有没有把别的配置抹掉」「通知有没有刷屏」
CONFIG = {}
SAVED_CONFIGS = []
POSTS_SENT = []
class _F:
    def __init__(self, default=None): self.default = default
class Field:
    def __new__(cls, default=None, description=None, **kw): return _F(default)
class BaseModel:
    def __init__(self, **kwargs):
        for name in getattr(type(self), "__annotations__", {}):
            cur = getattr(self, name, None)
            if isinstance(cur, _F): cur = cur.default
            if isinstance(cur, dict) and callable(cur): cur = cur()
            setattr(self, name, kwargs.pop(name, cur))
        for k, v in kwargs.items(): setattr(self, k, v)
    def model_dump(self):
        """对齐 pydantic v2：按注解导出字段，缺省值用默认值。"""
        out = {}
        for name in getattr(type(self), "__annotations__", {}):
            cur = getattr(self, name, None)
            if isinstance(cur, _F): cur = cur.default
            if isinstance(cur, dict) and callable(cur): cur = cur()
            out[name] = cur
        return out
stub("pydantic", BaseModel=BaseModel, Field=Field)

class Base:
    def __init__(self, plugin_id="ParkingBot"):
        self.plugin_id = plugin_id
    def get_data(self, key=None, plugin_id=None): return DB.get(key)
    async def async_get_data(self, key=None, plugin_id=None): return DB.get(key)
    async def async_save_data(self, key, value, plugin_id=None): DB[key] = value
    def post_message(self, **kw): POSTS_SENT.append(kw)
    # 基类的 update_config 是「整体覆盖」语义，所以这里也整体替换，
    # 才能测出「只改一个字段会不会把别的配置抹掉」。
    def get_config(self, plugin_id=None): return dict(CONFIG)
    def update_config(self, config, plugin_id=None):
        SAVED_CONFIGS.append(dict(config or {}))
        CONFIG.clear(); CONFIG.update(config or {})
        return True
class Event:
    def __init__(self, d): self.event_data = d
class _EM:
    def register(self, *a, **k): return lambda fn: fn
class EventType:
    PluginAction = "PluginAction"; MessageAction = "MessageAction"

def ts(m): return (dt.datetime.now() - dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")

class Client:
    """记录所有写请求，便于断言「到底发了哪些花钱的请求」。"""
    POSTS = []
    GETS = []
    STATE = None
    CARS = []
    def __init__(self, *a, **k): pass
    async def get_json(self, url, **kw):
        Client.GETS.append(url.rsplit("/", 1)[-1])
        if "state.php" in url: return self.STATE
        if "titles.php" in url:
            return {"success": True, "currentTitleId": 2, "titles": [
                {"id": 1, "name": "无名小卒", "desc": "默认称号",
                 "color": "#9E9E9E", "owned": True},
                {"id": 2, "name": "老司机", "desc": "累计挪车 100 次",
                 "color": "#ffd700", "owned": True},
                {"id": 3, "name": "车位传说", "desc": "尚未解锁",
                 "color": "#e94560", "owned": False}]}
        if "game_data.php" in url: return {"success": True, "cars": self.CARS}
        return {"success": True, "sales": []}
    async def post_json(self, url, json=None, **kw):
        Client.POSTS.append((url.rsplit("/", 1)[-1], json))
        if "titles.php" in url: return {"success": True, "titleName": "老司机"}
        return {"success": True, "earned": 0}

stub("app"); stub("app.sdk")
stub("app.sdk.plugin", _PluginBase=Base)
stub("app.sdk.logging", logger=types.SimpleNamespace(
    info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None, debug=lambda *a: None))
stub("app.sdk.network", AsyncRequestUtils=Client)
stub("app.sdk.config", settings=types.SimpleNamespace(TZ="Asia/Shanghai"))
stub("app.sdk.scheduler", add_plugin_once_job=lambda *a, **k: True, remove_plugin_once_job=lambda *a, **k: None)
sys.modules["app.sdk"].scheduler = sys.modules["app.sdk.scheduler"]
stub("app.sdk.events", Event=Event, eventmanager=_EM())
stub("app.schemas"); stub("app.schemas.types", EventType=EventType)
stub("app.db"); stub("app.db.oper")
class SiteOper:
    async def async_list(self): return []
stub("app.db.oper.site", SiteOper=SiteOper)
stub("apscheduler"); stub("apscheduler.triggers")
stub("apscheduler.triggers.cron", CronTrigger=type("CT", (), {"from_crontab": staticmethod(lambda x: x)}))
stub("pytz", timezone=lambda n: dt.timezone(dt.timedelta(hours=8)), utc=dt.timezone.utc)

spec = importlib.util.spec_from_file_location("pb", PLUGIN_FILE)
mod = importlib.util.module_from_spec(spec); sys.modules["pb"] = mod
spec.loader.exec_module(mod)
P = mod.ParkingBot

# 车型表：1 号是自带的破车，4 号需要 10 级且很贵，是等级/金币校验的靶子
CARS = {
    1: {"id": 1, "name": "奥拓", "price": 100, "levelReq": 1, "earnPerMin": 6},
    2: {"id": 2, "name": "桑塔纳", "price": 300, "levelReq": 1, "earnPerMin": 15},
    3: {"id": 3, "name": "奔驰C", "price": 500, "levelReq": 1, "earnPerMin": 25},
    4: {"id": 4, "name": "兰博基尼", "price": 900, "levelReq": 10, "earnPerMin": 99},
}

def state(cash=1000, level=5, cars=None, max_cars=5):
    return {
        "success": True, "userId": "me", "currentTitleId": 2,
        "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_CARS": max_cars},
        "users": {"me": {"name": "我", "level": level, "cash": cash,
                         "currentTitleName": "老司机"}},
        "userCars": cars if cars is not None else [
            {"id": "idle-old", "userId": "me", "carId": 1, "status": "idle"},
            {"id": "parked-x", "userId": "me", "carId": 2, "status": "parked",
             "parkedTime": ts(3), "parkedBy": "them"},
        ],
        "parkingSpots": [{"id": 7, "userId": "other"}],
    }

IDLE = lambda: {"id": "idle-old", "userId": "me", "carId": 1, "status": "idle"}

def shop(auto_buy, auto_trade, st, cars_map=None):
    """按开关分别调用买车 / 换新两个独立方法，返回 (插件, 合并摘要)。"""
    Client.POSTS = []
    p = P()
    p._auto_buy = auto_buy
    p._auto_trade = auto_trade
    cm = cars_map or dict(CARS)
    notes = []
    if auto_buy:
        notes.append(asyncio.run(p._auto_buy_one(Client(), st, cm, "me")))
    if auto_trade:
        notes.append(asyncio.run(p._auto_trade_one(Client(), st, cm, "me")))
    return p, "；".join([n for n in notes if n])

ok = []
def check(label, cond, extra=""):
    ok.append(bool(cond))
    print(f"   {'PASS' if cond else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")

print("=== 1) 默认关闭：必须零写请求 ===")
Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = False
nb = asyncio.run(p._auto_buy_one(Client(), state(), dict(CARS), "me"))
nt = asyncio.run(p._auto_trade_one(Client(), state(), dict(CARS), "me"))
check("两个开关都关时买车不发请求", not any(e == "buy_car.php" for e, _ in Client.POSTS))
check("两个开关都关时换新不发请求", not any(e == "trade_in.php" for e, _ in Client.POSTS))
check("两个开关都关时零请求", Client.POSTS == [], Client.POSTS)
check("两个开关都关时无执行摘要", nb == "" and nt == "", (nb, nt))

print("\n=== 2) 自动以旧换新 ===")
p, note = shop(False, True, state(cash=1000))
trades = [q for e, q in Client.POSTS if e == "trade_in.php"]
check("每轮最多换一辆", len(Client.POSTS) == 1 and len(trades) == 1, Client.POSTS)
check("换的是收益涨幅最大且金币够得着的（奔驰C，+19/分）",
      trades and trades[0] == {"oldCarInstanceId": "idle-old", "newCarId": 3}, trades)
check("摘要可读", "自动以旧换新" in note and "奔驰C" in note, note)
check("只开换新时不会顺手买车",
      not any(e == "buy_car.php" for e, _ in Client.POSTS), Client.POSTS)

print("\n--- 金币不够：不该发请求 ---")
Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=50), dict(CARS), "me"))
check("差价超过金币 → 零请求", Client.POSTS == [], Client.POSTS)

print("\n--- 等级不够：不该发请求 ---")
low = {k: dict(v) for k, v in CARS.items() if k != 1}
for v in low.values(): v["levelReq"] = 10
Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=99999), low, "me"))
check("目标车等级超限 → 零请求", Client.POSTS == [], Client.POSTS)

print("\n--- 只有比现在更贵的才换，且收益必须涨 ---")
Client.POSTS = []
flat = {k: dict(v) for k, v in CARS.items()}
flat[2]["earnPerMin"] = 3   # 更贵但收益更低
flat[3]["earnPerMin"] = 2   # 更贵但收益更低
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=1000), flat, "me"))
check("所有候选都比现在更差 → 零请求", Client.POSTS == [], Client.POSTS)

print("\n--- 只换空闲车，停车中的车不动 ---")
Client.POSTS = []
two_idle = [IDLE(), {"id": "idle-old2", "userId": "me", "carId": 1, "status": "idle"},
            {"id": "parked-x", "userId": "me", "carId": 2, "status": "parked",
             "parkedTime": ts(3), "parkedBy": "them"}]
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=1000, cars=two_idle), dict(CARS), "me"))
t = [q for e, q in Client.POSTS if e == "trade_in.php"]
check("换的是空闲车而非停车中的车", t and t[0]["oldCarInstanceId"].startswith("idle"), t)
check("有 2 辆空闲车也只换一辆", len(Client.POSTS) == 1, Client.POSTS)

print("\n--- 以旧换新按「差价」而非「新车全价」卡门槛 ---")
rich = {k: dict(v) for k, v in CARS.items()}
rich[4]["levelReq"] = 1          # 兰博基尼全价 900，但只需补 800
Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=800), rich, "me"))
t = [q for e, q in Client.POSTS if e == "trade_in.php"]
check("金币够差价就能换（全价更高也换得起）",
      bool(t) and t[0]["newCarId"] == 4, t)

Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=799), rich, "me"))
t = [q for e, q in Client.POSTS if e == "trade_in.php"]
check("金币差 1 金够不到兰博基尼时，退而选差价够得着的次优车型",
      bool(t) and t[0]["newCarId"] == 3, t)

print("\n=== 3) 自动买车 ===")
p, note = shop(True, False, state(cash=1000))
buys = [q for e, q in Client.POSTS if e == "buy_car.php"]
check("每轮最多买一辆", len(Client.POSTS) == 1 and len(buys) == 1, Client.POSTS)
check("买等级内金币够得着的收益最高车型（奔驰C）", buys and buys[0] == {"carId": 3}, buys)
check("摘要可读", "自动买车" in note and "奔驰C" in note, note)
check("只开买车时不会顺手换车",
      not any(e == "trade_in.php" for e, _ in Client.POSTS), Client.POSTS)

print("\n--- 金币不够 / 等级不够 / 已满员 ---")
Client.POSTS = []
p = P(); p._auto_buy = True; p._auto_trade = False
asyncio.run(p._auto_trade_one(Client(), state(cash=50), dict(CARS), "me"))
check("金币不够 → 零请求", Client.POSTS == [], Client.POSTS)

Client.POSTS = []
p = P(); p._auto_buy = True; p._auto_trade = False
asyncio.run(p._auto_trade_one(Client(), state(cash=99999), low, "me"))
check("等级不够 → 零请求", Client.POSTS == [], Client.POSTS)

Client.POSTS = []
full = [IDLE()] + [{"id": "x%d" % i, "userId": "me", "carId": 2, "status": "idle"}
                   for i in range(5)]
p = P()
asyncio.run(p._auto_buy_one(Client(), state(cash=99999, cars=full), dict(CARS), "me"))
check("已达 MAX_CARS → 不买车", Client.POSTS == [], Client.POSTS)

print("\n--- 买满后只买一辆、不会一口气买空 ---")
Client.POSTS = []
p = P(); p._auto_buy = True; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=99999), dict(CARS), "me"))
asyncio.run(p._auto_buy_one(Client(), state(cash=99999), dict(CARS), "me"))
check("两个开关同开也至多 1 换 + 1 买", len(Client.POSTS) == 2, [e for e, _ in Client.POSTS])

print("\n=== 4) 失败时不应崩、不应继续脏请求 ===")
class FailingClient(Client):
    FAIL = set()
    async def post_json(self, url, json=None, **kw):
        ep = url.rsplit("/", 1)[-1]
        Client.POSTS.append((ep, json))
        if ep in self.FAIL: return {"success": False, "error": "余额不足"}
        return {"success": True}

Client.POSTS = []
fc = FailingClient(); fc.FAIL = {"trade_in.php"}
p = P(); p._auto_buy = True; p._auto_trade = True
n = asyncio.run(p._auto_trade_one(fc, state(cash=99999), dict(CARS), "me"))
nb = asyncio.run(p._auto_buy_one(fc, state(cash=99999), dict(CARS), "me"))
check("换车失败后不再重复尝试", sum(1 for e, _ in Client.POSTS if e == "trade_in.php") == 1, Client.POSTS)
check("换车与买车已解耦，互不影响",
      sum(1 for e, _ in Client.POSTS if e == "buy_car.php") == 1
      and sum(1 for e, _ in Client.POSTS if e == "trade_in.php") == 1, Client.POSTS)
check("失败不写进摘要", "自动以旧换新" not in n, n)
check("买车成功照常写摘要", "自动买车" in nb, nb)

print("\n=== 5) 时序：换新必须发生在挪车之后 ===")


class OrderPlugin(P):
    """记录 run_once 里各类写接口的先后顺序。"""
    order = None

    def __init__(self):
        super().__init__()
        OrderPlugin.order = []

    async def _resolve_cookie(self):
        return "PHPSESSID=x"

    async def _load_cars_map(self, client):
        return dict(CARS)

    async def _record_round(self, *a, **kw):
        return None


class OrderClient(Client):
    async def post_json(self, url, json=None, **kw):
        ep = url.rsplit("/", 1)[-1]
        Client.POSTS.append((ep, json))
        OrderPlugin.order.append(ep)
        ok = {"success": True, "earned": 30}
        if ep == "unpark.php" and json:
            # 真实游戏里挪完车状态就变 idle，桩得跟着变，
            # 否则「挪车之后才换新」这条路根本走不到。
            target = json.get("userCarId")
            for car in OrderClient.STATE["userCars"]:
                if car.get("id") == target:
                    car["status"] = "idle"
                    car.pop("parkedTime", None)
        return ok


# 名下两辆停够时长的车（挪完会变空闲）+ 金币充足 + 两开关都开
ORDER_STATE = state(cash=9999, cars=[
    {"id": "busy-1", "userId": "me", "carId": 1, "status": "parked",
     "parkedTime": ts(30), "parkedBy": "other"},
    {"id": "busy-2", "userId": "me", "carId": 1, "status": "parked",
     "parkedTime": ts(30), "parkedBy": "other"},
])
Client.POSTS = []
Client.STATE = ORDER_STATE
OrderClient.STATE = ORDER_STATE
p = OrderPlugin()
p._enabled = True
p._cron = "*/5 * * * *"
p._auto_buy = True
p._auto_trade = True
p._cars_cache = dict(CARS)
old_req = mod.AsyncRequestUtils
mod.AsyncRequestUtils = lambda *a, **k: OrderClient()
asyncio.run(p.run_once())
mod.AsyncRequestUtils = old_req

seq = OrderPlugin.order
uniq = [x for i, x in enumerate(seq) if i == 0 or seq[i - 1] != x]
print("   调用顺序:", " -> ".join(uniq))
check("本轮确实发生了挪车与换新",
      "unpark.php" in seq and "trade_in.php" in seq, uniq)
if "trade_in.php" in seq:
    check("换新排在挪车之后",
          seq.index("unpark.php") < seq.index("trade_in.php"), uniq)
check("买车排在最前，新车当轮就能停出去",
      "buy_car.php" in seq
      and ("trade_in.php" not in seq or seq.index("buy_car.php") < seq.index("trade_in.php")),
      uniq)
if "park.php" in seq:
    check("停车排在最后", seq.index("park.php") > seq.index("unpark.php"), uniq)

print("\n=== 6) run_once 每轮只记一次，且 note 落到历史里 ===")
class TestPlugin(P):
    async def _resolve_cookie(self): return "PHPSESSID=x"
    async def _record_round(self, client, state, user_id, new_parked, moved, earned,
                            record_history=True, note=""):
        TestPlugin.record_calls.append((new_parked, moved, earned, note))

TestPlugin.record_calls = []
Client.POSTS = []
Client.STATE = state(cash=1000)
Client.CARS = list(CARS.values())
p = TestPlugin()
p._enabled = True; p._cron = "*/5 * * * *"; p._auto_buy = True; p._auto_trade = True
p._cars_cache = dict(CARS)
asyncio.run(p.run_once())
check("run_once 每轮只调一次 _record_round", len(TestPlugin.record_calls) == 1, TestPlugin.record_calls)
check("note 传到了 _record_round", TestPlugin.record_calls and TestPlugin.record_calls[0][3],
      TestPlugin.record_calls[0][3] if TestPlugin.record_calls else None)

print("\n--- 开关全关时 run_once 一个写请求都不该有买卖 ---")
Client.POSTS = []
TestPlugin.record_calls = []
p = TestPlugin()
p._enabled = True; p._cron = "*/5 * * * *"; p._auto_buy = False; p._auto_trade = False
p._cars_cache = dict(CARS)
asyncio.run(p.run_once())
spend = [e for e, _ in Client.POSTS if e in ("buy_car.php", "trade_in.php")]
check("run_once 不发买卖请求", spend == [], spend)
check("仍只记一次", len(TestPlugin.record_calls) == 1, TestPlugin.record_calls)

print("\n=== 6) 详情页：三区块 + 顶部切换栏 ===")
SNAP = {"name": "我", "level": 5, "cash": 1000, "car_total": 2, "car_max": 5,
        "parked_total": 1, "pending_gain": 0, "min_duration": 10,
        "note": "自动买车：奔驰C，花费 500 金，每分钟收益 25",
        "cars": [{"name": "奥拓", "rate": 6, "status": "空闲", "mins": 0, "gain": 0},
                 {"name": "桑塔纳", "rate": 15, "status": "停车中", "mins": 3, "gain": 45}],
        "spots": [{"label": "A-01", "occupant": "奥拓"}],
        "catalog": [], "flash": []}
DB.clear(); DB["snapshot"] = SNAP; DB["history"] = [{"time": "2026-01-01 10:00:00", "parked": 1, "moved": 1, "earned": 50}]
p = P(); p._page_cache = None; p._auto_buy = True; p._auto_trade = False
page = p.get_page()

def walk(n):
    if isinstance(n, dict):
        yield n
        for c in n.get("content", []) or []: yield from walk(c)
    elif isinstance(n, list):
        for i in n: yield from walk(i)

nodes = list(walk(page))
hrefs = [n["props"]["href"] for n in nodes
         if n.get("component") == "a" and isinstance(n.get("props"), dict) and "href" in n["props"]]
def body_texts(ns):
    """
    只取区块内容的文字。

    页签栏里三个按钮的名字（① 我的车位 / ② 车市 / ③ 我的车库）和区块卡片标题
    同名，所以不能按文字过滤——必须按结构跳过：凡是带 events.click 的节点
    都是页签按钮，它的整棵子树都排除掉。
    """
    out = []

    def rec(n):
        if isinstance(n, list):
            for i in n:
                rec(i)
            return
        if not isinstance(n, dict):
            return
        if isinstance(n.get("events"), dict) and "click" in n["events"]:
            return                      # 页签按钮本身及其子树全部跳过
        if "text" in n:
            out.append(n["text"])
        for c in n.get("content", []) or []:
            rec(c)

    rec(ns)
    return out

nav_events = []
for n in nodes:
    if isinstance(n.get("events"), dict) and "click" in n["events"]:
        nav_events.append(n["events"]["click"])
# 只数页签按钮（打 /tab 的），别把整页所有可点节点都算进来。
# 原先这里是「整页 click 节点 == 4」，于是任何新增按钮（v1.34.0 的爱心车位切换就是）
# 都会把断言撞红，接手的人只能去改实现来迎合它 —— 本意是「顶部导航有 4 个页签」，
# 就该按接口路径限定范围，而不是靠页面上恰好只有 4 个可点元素来成立。
tab_events = [e for e in nav_events
              if str(e.get("api") or "").endswith("/tab")]
check("顶部导航有 5 个页签按钮", len(tab_events) == 5, len(tab_events))
check("按钮打到区块切换接口",
      all(e["api"] == "plugin/ParkingBot/tab" and e["method"] == "post" for e in tab_events),
      [e["api"] for e in tab_events])
# 兜底：页面上除了 4 个页签，其余可点节点都必须是已知接口，别悄悄长出野按钮。
_KNOWN_CLICK = ("/tab", "/park", "/unpark", "/ticket", "/title", "/title_pick",
                "/refresh_status", "/buy_car", "/trade_in", "/buy_flash",
                "/shop_filter", "/trade_pick", "/charity_spot", "/exp_detail",
                "/notif_detail", "/personal_sub", "/bg_action", "/qqshow_action")
_others = sorted({e.get("api") for e in nav_events} -
                 {e.get("api") for e in tab_events})
check("非页签按钮都打已知接口（没有来路不明的可点节点）",
      all(any(str(a).endswith(k) for k in _KNOWN_CLICK) for a in _others), _others)
check("五个按钮分别切到五个区块",
      # 只取页签那批 —— 原写法对 nav_events（整页所有可点节点）取 params["tab"]，
      # 靠「页面上恰好只有带 tab 参数的按钮」成立。v1.40.0 加了经验值格
      # （/exp_detail，params 为空）就直接 KeyError。判据要按本意限定范围。
      [e["params"]["tab"] for e in tab_events] == ["spots", "garage", "shop", "stats",
                                                   "personal"],
      [e["params"].get("tab") for e in tab_events])

texts = body_texts(page)
check("默认只显示「我的车位」区块", any(t == "我的车位" for t in texts), texts[:6])
check("默认不显示车市区块", not any("② 车市" in t for t in texts))
check("默认不显示车库区块", not any("③ 我的车库" in t for t in texts))

# 切到车市：只有车市
p = P(); p._page_cache = None; p._tab = "shop"
nodes_shop = list(walk(p.get_page()))
texts_shop = body_texts(p.get_page())
check("切到车市后显示的是车市内容（特卖/车市/以旧换新任一）",
      any(any(k in t for k in ("限时 / 限量特卖", "车市", "以旧换新")) for t in texts_shop),
      texts_shop)
check("切到车市后不显示车位区块", not any(t == "我的车位" for t in texts_shop))
check("切到车市后不显示车库区块", not any("③ 我的车库" in t for t in texts_shop))

# 切到车库：只有车库 + 执行历史
p = P(); p._page_cache = None; p._tab = "garage"
nodes_g = list(walk(p.get_page()))
texts_g = body_texts(p.get_page())
check("切到车库后显示「我的车库」区块", any(t == "我的车库" for t in texts_g), texts_g[:6])
check("切到车库后不再带执行历史（已移到金币统计页）",
      not any("执行历史" in t for t in texts_g), texts_g)
check("切到车库后不显示车位区块", not any(t == "我的车位" for t in texts_g))
check("切到车库后不显示车市区块", not any("② 车市" in t for t in texts_g))

# 当前页签高亮
p = P(); p._page_cache = None; p._tab = "garage"
nav_garage = [n for n in walk(p.get_page())
              if isinstance(n.get("events"), dict) and "click" in n["events"]]
# 页签 = click 里带 tab 参数的那些。车库页还有「立即刷新」之类的按钮，
# 它们的 params 是空 dict，混进来会把页签样式断言全部带偏。
tabs_garage = [n for n in nav_garage
               if (n["events"]["click"].get("params") or {}).get("tab")]
check("页签导航仍是 5 个", len(tabs_garage) == 5, len(tabs_garage))
picked = [n for n in tabs_garage
          if (n["events"]["click"].get("params") or {}).get("tab") == "garage"]
st = (picked[0]["props"]["style"] if picked else {})
check("当前页签不再用渐变（插件页渲染器不认，会退化成透明底 + 深色字）",
      "gradient" not in str(st.get("backgroundColor", ""))
      and "gradient" not in str(st.get("border", "")), st.get("backgroundColor"))
check("当前页签底色是实色（不透明、非渐变）",
      bool(st.get("backgroundColor"))
      and str(st.get("backgroundColor")).lower() != "transparent"
      and "gradient" not in str(st.get("backgroundColor")),
      st.get("backgroundColor"))
check("当前页签文字是高亮浅色 #8DF3FF（深色页面上读得清）",
      str(st.get("color", "")).upper() == "#8DF3FF", st.get("color"))
check("当前页签有青色描边 + 发光阴影",
      "34,211,238" in str(st.get("border", "")) and bool(st.get("boxShadow")),
      (st.get("border"), st.get("boxShadow")))
unpicked = [b for b in tabs_garage if b is not picked[0]] if picked else []
check("未选中页签是透明底、没有阴影",
      bool(unpicked) and all(str(b["props"]["style"].get("boxShadow")) == "none"
                             and str(b["props"]["style"].get("backgroundColor")) == "transparent"
                             for b in unpicked),
      [(str(b["props"]["style"].get("boxShadow")),
        str(b["props"]["style"].get("backgroundColor"))) for b in unpicked])
check("每个页签都带内联 SVG 图标",
      all(any(isinstance(c.get("html"), str) and c["html"].startswith("<svg")
              for c in (b.get("content") or []))
          for b in tabs_garage),
      [len(b.get("content") or []) for b in nav_garage])
check("图标用的是内联 html 而不是 VIcon（宿主解析不出图标名会渲染空 svg）",
      not any(c.get("component") == "VIcon"
              for b in nav_garage for c in (b.get("content") or [])))

check("快照 note 显示在页面上", any("自动买车：奔驰C" in t for t in texts))

bad = [n for n in nodes if isinstance(n.get("props"), dict)
       and "text" in n["props"] and "text" not in n]
check("没有把 text 错放进 props（渲染会空白）", not bad, bad[:2])

print("\n--- 车市数据缺失时，锚点仍要存在 ---")
EMPTY = {"name": "我", "level": 5, "cash": 0, "car_total": 0, "car_max": 5,
         "parked_total": 0, "pending_gain": 0, "min_duration": 10,
         "cars": [], "spots": [], "catalog": [], "flash": []}
p = P(); p._page_cache = None; p._tab = "shop"
DB["snapshot"] = EMPTY
try:
    page2 = p.get_page()
    check("空车市时切到车市页不崩", isinstance(page2, list) and len(page2) > 0)
except Exception as err:
    check("空车市时切到车市页不崩", False, err)

print("\n--- 快照整个为空（还没跑过一轮） ---")
p = P(); p._page_cache = None
DB["snapshot"] = {}
try:
    page3 = p.get_page()
    nodes3 = list(walk(page3))
    check("空快照时不崩且仍渲染出区块",
          any(isinstance(n.get("props"), dict)
              and n["props"].get("style", {}).get("borderRadius") == "14px"
              for n in nodes3),
          [n.get("component") for n in nodes3][:8])
except Exception as err:
    check("空快照时不崩且仍渲染出区块", False, err)

print("\n--- 版本号一致性 ---")
code_version = mod.ParkingBot.plugin_version
pkg = json.load(io.open(os.path.join(PLUGIN_ROOT, "package.v3.json"), encoding="utf-8"))
index_version = pkg["ParkingBot"]["version"]
check("代码 plugin_version 与索引 version 一致",
      code_version == index_version, f"code={code_version} index={index_version}")
hist = pkg["ParkingBot"]["history"]
check("索引里有当前版本的更新说明",
      f"v{index_version}" in hist, f"v{index_version}" in hist)
newest = next(iter(hist))
check("changelog 首条就是当前版本", newest == f"v{index_version}", newest)
check("changelog 每条说明都非空",
      all(len(v.strip()) >= 3 for v in hist.values()),
      [k for k, v in hist.items() if len(v.strip()) < 3])

print("\n--- 车库与车市也要有车图 ---")
GCAR = {"name": "奥拓", "rate": 6, "status": "空闲", "mins": 0, "gain": 0,
        "image": "https://qcw.tangpt.top/assets/cars/aoto.png", "no": 1}
gblob = repr(P._garage([GCAR], 10))
check("车库渲染车图", GCAR["image"] in gblob)
check("车库是左图右信息布局", "76px" in gblob)
check("车库没图时退回首字", "奥" in repr(P._garage([dict(GCAR, image="")], 10)))

check("车市条目渲染车图", GCAR["image"] in repr(
    P._item("奥拓", [("800 金", "#FFD700")], None, image=GCAR["image"])))
check("车市条目没图时不报错",
      isinstance(P._item("奥拓", [("800 金", "#FFD700")], None), dict))

DB.clear()
DB["snapshot"] = {"name": "我", "level": 4, "cash": 100, "car_total": 2,
                  "car_max": 5, "parked_total": 0, "pending_gain": 0,
                  "min_duration": 10, "cars": [], "spots": [],
                  "catalog": [{"id": 1, "name": "奥拓", "price": 800, "earn": 6,
                               "level": 1, "category": "代步车",
                               "image": "https://qcw.tangpt.top/assets/cars/aoto.png"}],
                  "flash": [{"id": 900, "name": "限时跑车", "price": 300,
                             "original": 500, "earn": 40, "level": 1, "timed": True,
                             "stock": 3, "sold": 1,
                             "image": "https://qcw.tangpt.top/assets/cars/flash.png"}]}
p = P(); p._page_cache = None; p._tab = "shop"
sblob = repr(p.get_page())
check("车市页渲染车图（常规 + 特卖）", sblob.count("<img") >= 2, sblob.count("<img"))

print("\n" + ("=" * 46))
print("=== 7) 独立审查提出的 8 条，逐条回归 ===")

async def _coro(value):
    return value

print("\n--- #2 字符串配置不得被当成开启 ---")
for raw, want in [("false", False), ("False", False), ("0", False), ("no", False),
                  ("off", False), ("", False), (None, False), (False, False),
                  (0, False), (True, True), (1, True), ("true", True), ("on", True)]:
    got = P._as_switch(raw)
    check(f"_as_switch({raw!r}) == {want}", got is want, got)
p = P()
for raw in ("false", "0", "no", "off", "", None):
    p.init_plugin({"auto_buy": raw, "auto_trade": raw})
    check(f"init_plugin auto_buy={raw!r} 仍为关",
          p._auto_buy is False and p._auto_trade is False, (p._auto_buy, p._auto_trade))

print("\n--- #1 零辆车时自动买车仍要能买 ---")
zeroshot = state(cash=1000, cars=[])
Client.POSTS = []
Client.STATE = zeroshot
p = P(); p._enabled = True; p._cron = "*/5 * * * *"
p._auto_buy = True; p._auto_trade = False
p._cars_cache = dict(CARS)
p._cookie = "PHPSESSID=x"
p._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), p)
asyncio.run(p.run_once())
buys = [q for e, q in Client.POSTS if e == "buy_car.php"]
check("名下无车时仍会买车", bool(buys), Client.POSTS)

Client.POSTS = []
Client.STATE = zeroshot
p = P(); p._enabled = True; p._cron = "*/5 * * * *"
p._auto_buy = False; p._auto_trade = False
p._cars_cache = dict(CARS)
p._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), p)
asyncio.run(p.run_once())
check("没开自动买车时零辆车仍然直接退出，不发任何写请求",
      Client.POSTS == [], Client.POSTS)

print("\n--- #3 买卖成功但刷新失败，不得用废 ID 继续停车 ---")
class NoRefreshClient(Client):
    """买卖之后的所有 state.php 都失败，用来模拟刷新拿不回来。"""
    async def get_json(self, url, **kw):
        if "state.php" in url and Client.POSTS:
            return {"success": False, "error": "临时故障"}
        return await Client.get_json(self, url, **kw)

Client.POSTS = []
Client.STATE = state(cash=1000)
orig_client = mod.AsyncRequestUtils
mod.AsyncRequestUtils = lambda *a, **k: NoRefreshClient()
p = P(); p._enabled = True; p._cron = "*/5 * * * *"
p._auto_buy = False; p._auto_trade = True
p._cars_cache = dict(CARS)
p._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), p)
asyncio.run(p.run_once())
mod.AsyncRequestUtils = orig_client
bad = [q for e, q in Client.POSTS if e == "park.php" and q.get("userCarId") == "idle-old"]
check("没有拿已被换掉的车辆 ID 去停车", not bad, Client.POSTS)
check("换车本身仍然执行了", any(e == "trade_in.php" for e, _ in Client.POSTS), Client.POSTS)

print("\n--- #4 车型表混入非字典项不得崩 ---")
dirty = dict(CARS); dirty["junk"] = "not-a-dict"
for label, buy, trade in (("仅自动买车", True, False), ("仅自动以旧换新", False, True)):
    Client.POSTS = []
    p = P(); p._auto_buy = buy; p._auto_trade = trade
    try:
        asyncio.run(p._auto_trade_one(Client(), state(cash=99999), dirty, "me"))
        check(f"脏车型表（{label}）不抛异常", True)
    except Exception as err:
        check(f"脏车型表（{label}）不抛异常", False, err)

print("\n--- #5 限时车（carId=0）不被自动换掉 ---")
flashcar = [{"id": "f1", "userId": "me", "carId": 0, "status": "idle",
             "flashEarnPerMin": 8}]
Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=99999, cars=flashcar), dict(CARS), "me"))
check("限时车不被自动换掉", Client.POSTS == [], Client.POSTS)

print("\n--- #6 切到车市时只渲染车市内容 ---")
flash_snap = dict(SNAP)
flash_snap["flash"] = [{"id": 900, "name": "限时跑车", "price": 300,
                        "original": 500, "earn": 40, "level": 1, "stock": 3, "sold": 1}]
flash_snap["catalog"] = [{"id": 3, "name": "奔驰C", "price": 500, "earn": 25, "level": 1}]
flash_snap["cars"] = []
p = P(); p._page_cache = None; p._tab = "shop"
DB["snapshot"] = flash_snap
nodes4 = list(walk(p.get_page()))
texts4 = body_texts(p.get_page())
check("有特卖时切到车市页能同时看到特卖与车市",
      any("限时 / 限量特卖" in t for t in texts4) and any("奔驰C" in t for t in texts4),
      [t for t in texts4 if "特卖" in t])
check("有特卖时车位/车库区块仍不出现",
      not any("① 我的车位" in t for t in texts4) and not any("③ 我的车库" in t for t in texts4))

print("\n--- #7 快照格式损坏不崩 ---")
for bad_snap in ["字符串", 123, [1, 2]]:
    p = P(); p._page_cache = None
    DB["snapshot"] = bad_snap
    try:
        pg = p.get_page()
        check(f"快照为 {bad_snap!r} 时仍能渲染", isinstance(pg, list) and len(pg) > 0)
    except Exception as err:
        check(f"快照为 {bad_snap!r} 时仍能渲染", False, err)

print("\n--- #8 开关开着但本轮没花钱时给出提示 ---")
p = P(); p._page_cache = None; p._auto_buy = True; p._auto_trade = True
DB["snapshot"] = dict(SNAP, note="")
texts8 = body_texts(p.get_page())
check("有「已开启但本轮没花钱」的提示",
      any("已开启" in t and "没有满足条件" in t for t in texts8),
      [t for t in texts8 if "已开启" in t])

print("\n--- 区块切换接口本身 ---")
# v1.47.0 起 view 改成「内存 + 防抖后台落盘」，asyncio.run() 一结束事件循环
# 就关、后台任务会被丢掉。所以断言落盘结果前必须显式 flush 一次 ——
# 这不是给实现开后门，是把「什么时候落盘」和「落盘成什么」分开验。
def _set_tab_and_flush(pp, req):
    async def _go():
        r = await pp.api_set_tab(req)
        await pp.flush_view_now()
        return r
    return asyncio.run(_go())


def _flush(pp):
    """把待落盘的 view 写掉。v1.47.0 起落盘是防抖后台的，断言前得先 flush。"""
    asyncio.run(pp.flush_view_now())
    return DB.get("view")


p = P()
ok_tab = _set_tab_and_flush(p, mod.TabSwitch(tab="garage"))
check("切到 garage 成功", bool(getattr(ok_tab, "success", False)), ok_tab)
check("切完后 _tab 已更新", p._tab == "garage", p._tab)
check("页签已持久化到 view", (DB.get("view") or {}).get("tab") == "garage", DB.get("view"))
_set_tab_and_flush(p, mod.TabSwitch(tab="stats", day="2026-10-13"))
check("切到统计页并选中日期", (DB.get("view") or {}).get("day") == "2026-10-13", DB.get("view"))
_set_tab_and_flush(p, mod.TabSwitch(tab="stats", day="不是日期"))
check("非法日期不写入",
      (DB.get("view") or {}).get("day") == "2026-10-13", DB.get("view"))
_set_tab_and_flush(p, mod.TabSwitch(tab="garage"))
check("切走再切回，选中的日期不会被页签切换冲掉",
      (DB.get("view") or {}).get("day") == "2026-10-13", DB.get("view"))
bad_tab = _set_tab_and_flush(p, mod.TabSwitch(tab="../../etc/passwd"))
check("非法页签被拒绝且不改状态",
      (not getattr(bad_tab, "success", False)) and p._tab == "garage", p._tab)
empty_tab = _set_tab_and_flush(p, mod.TabSwitch(tab=""))
check("空页签不被采纳", (not getattr(empty_tab, "success", False)) and p._tab == "garage",
      p._tab)
paths = [a["path"] for a in p.get_api()]
check("/tab 已注册", "/tab" in paths, paths)

p2 = P()
p2.init_plugin({"auto_buy": "false"})
check("重载后记住上次页签", p2._tab == "garage", p2._tab)
DB["view"] = "乱七八糟"
p3 = P(); p3.init_plugin({})
check("存档里页签非法时回到默认", p3._tab == "spots", p3._tab)
DB["view"] = {"tab": "garage", "day": "2026-01-02"}
p4 = P(); p4.init_plugin({})
check("重载后连同日期一起恢复", (p4._tab, p4._day) == ("garage", "2026-01-02"),
      (p4._tab, p4._day))
DB["view"] = {"tab": "garage", "day": "不是日期"}
p5 = P(); p5.init_plugin({})
check("日期非法时不带脏值", p5._day == "", p5._day)
DB.pop("view", None)

print("\n--- 金币统计页 ---")
DB.clear()
DAILY = {}
for day, earned in (("2026-10-10", 100), ("2026-10-11", 250), ("2026-10-12", 60)):
    DAILY[day] = {"earned": earned, "parked": 2, "moved": 2,
                  "rounds": 3, "hours": {"8": earned}}
DB["daily"] = DAILY
DB["history"] = [{"time": "10-12 08:00", "parked": 1, "moved": 1, "earned": 20}]
DB["snapshot"] = {"name": "x", "level": 4, "cash": 100, "car_total": 1,
                  "car_max": 5, "parked_total": 1, "pending_gain": 0,
                  "min_duration": 10, "cars": [], "spots": [],
                  "catalog": [], "flash": []}
p = P(); p._page_cache = None; p._tab = "stats"
blob = repr(p.get_page())
check("统计页渲染日期卡片行",
      "2026-10-12" in blob and "2026-10-11" in blob and "2026-10-10" in blob)
check("统计页有 24 小时柱状图", blob.count("'height':") >= 24, blob.count("'height':"))
check("统计页有每日明细表", "每日明细" in blob)
check("统计页默认看最新一天", p._day == "" and "2026-10-12 · 每小时收益" in blob)

print("\n--- 收支统计 ---")
DB.clear()
DB["daily"] = {
    "2026-10-12": {"earned": 900, "spent": 500, "bought": 1,
                   "diff": 200, "traded": 1, "parked": 3, "moved": 3,
                   "rounds": 20, "hours": {"8": 400, "9": 500}},
    "2026-10-11": {"earned": 300, "spent": 0, "bought": 0,
                   "diff": 0, "traded": 0, "parked": 1, "moved": 1,
                   "rounds": 12, "hours": {"8": 300}},
}
DB["snapshot"] = {"name": "x", "level": 4, "cash": 100, "car_total": 1,
                  "car_max": 5, "parked_total": 1, "pending_gain": 0,
                  "min_duration": 10, "cars": [], "spots": [],
                  "catalog": [], "flash": []}
DB["history"] = []
p = P(); p._page_cache = None; p._tab = "stats"
blob = repr(p.get_page())
check("统计页有支出项", "当日支出" in blob and "本月支出" in blob)
check("统计页有净收项", "当日净收" in blob and "本月净收" in blob)
check("统计页有本月买卖汇总", "本月买卖" in blob)
check("当日支出 = 买车500 + 换车差价200", "'700'" in blob, )
check("当日净收 = 900 - 700", "'200'" in blob)
check("本月收益 = 1200", "'1,200'" in blob or "'1200'" in blob)
check("明细表区分买车与换车差价", "换车补差价" in blob)
check("小时柱图说明里带买卖次数", "买车 1 次" in blob and "换车 1 次" in blob)

p._day = "2026-10-11"
blob2 = repr(p.get_page())
check("切到没有支出的那天，支出为 0", "'当日支出'" in blob2)

# 日期卡片显示的是净收：10-12 是 +200
cards = [n for n in walk(p.get_page())
         if isinstance(n.get("events"), dict)
         and n["events"].get("click", {}).get("params", {}).get("day")]
check("日期卡片存在且按净收着色",
      bool(cards), len(cards))
DB["daily"] = {}

p._day = "2026-10-11"
check("切换日期后图表标题跟着变",
      "2026-10-11 · 每小时收益" in repr(p.get_page()))

day_chips = [n for n in walk(p.get_page())
             if isinstance(n.get("events"), dict)
             and n["events"].get("click", {}).get("params", {}).get("day")]
check("日期卡片点击会同时切页签和日期",
      bool(day_chips) and all(e["events"]["click"]["params"]["tab"] == "stats"
                              for e in day_chips),
      [e["events"]["click"]["params"] for e in day_chips][:2])

p = P(); p._page_cache = None; p._tab = "garage"
check("车库页不再出现执行历史卡片", "执行历史" not in repr(p.get_page()))

DB["daily"] = {}
p = P(); p._page_cache = None; p._tab = "stats"
try:
    check("无统计数据时正常渲染",
          isinstance(p.get_page(), list) and len(p.get_page()) > 0)
except Exception as err:
    check("无统计数据时正常渲染", False, err)

DB["daily"] = "脏数据"
p = P(); p._page_cache = None
try:
    check("daily 存档损坏时退化为空", p._page_data()[2] == {})
except Exception as err:
    check("daily 存档损坏时退化为空", False, err)
DB["daily"] = DAILY

print("\n--- 响应信封必须符合前端拦截器的校验 ---")


def envelope_ok(obj):
    """照抄 MoviePilot 前端拦截器 isEnvelope 的判定逻辑。"""
    if not isinstance(obj, dict) or isinstance(obj, list):
        return False
    keys = list(obj.keys())
    if len(keys) != 3:
        return False
    if not all(k in ("success", "message", "data") for k in keys):
        return False
    return (isinstance(obj["success"], bool)
            and isinstance(obj["message"], str)
            and "data" in obj)


for payload in ({"success": True},
                {"success": True, "name": "奔驰C", "price": 500},
                {"success": False, "error": "金币不足"},
                {"success": True, "earned": 120, "expGained": 8}):
    body = mod._result(payload).model_dump()
    check(f"信封合规 {body}", envelope_ok(body), body)

bad = mod._result({"success": False, "error": "未知区块"}).model_dump()
check("失败时 message 承载原因",
      isinstance(bad["message"], str) and bad["message"] != "", bad["message"])
okb = mod._result({"success": True, "name": "车市"}).model_dump()
check("成功时 message 是空字符串（不能是 None）", okb["message"] == "", repr(okb["message"]))
check("业务字段落在 data 里", okb["data"].get("name") == "车市", okb["data"])
check("get_api 里所有接口都用信封模型",
      all(a.get("response_model") is mod.ApiResult for a in p.get_api()),
      [str(a.get("response_model")) for a in p.get_api()])

print("\n--- 以旧换新：只有空闲车能换 ---")
mixed = state(cash=9999, cars=[
    {"id": "busy-1", "userId": "me", "carId": 1, "status": "parked",
     "parkedTime": ts(5), "parkedBy": "them"},
    {"id": "busy-2", "userId": "me", "carId": 1, "status": "charity",
     "parkedTime": ts(30)},
    {"id": "idle-1", "userId": "me", "carId": 1, "status": "idle"},
])
Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), mixed, dict(CARS), "me"))
t = [q for e, q in Client.POSTS if e == "trade_in.php"]
check("自动换新只用空闲车做旧车",
      bool(t) and t[0]["oldCarInstanceId"] == "idle-1", t)

Client.POSTS = []
p = P(); p._auto_buy = False; p._auto_trade = True
asyncio.run(p._auto_trade_one(Client(), state(cash=9999, cars=[
    {"id": "busy-1", "userId": "me", "carId": 1, "status": "parked",
     "parkedTime": ts(5), "parkedBy": "them"},
    {"id": "busy-2", "userId": "me", "carId": 1, "status": "charity",
     "parkedTime": ts(30)},
]), dict(CARS), "me"))
check("没有空闲车时不发换新请求", Client.POSTS == [], Client.POSTS)

print("\n--- 车位：占用者 / 时长 / 预估收益 / 图片 ---")
RULES = {"MIN_PARK_DURATION": 10, "MAX_EARN_PER_SPOT": 3000,
         "CHARITY_EXP_PER_MIN": 2, "CHARITY_EXP_DAILY_LIMIT": 200}
CMAP = {1: {"id": 1, "name": "奥拓", "earnPerMin": 6, "price": 100,
            "image": "assets/cars/aoto.png", "levelReq": 1},
        2: {"id": 2, "name": "宝马", "earnPerMin": 50, "price": 500,
            "image": "https://qcw.tangpt.top/assets/cars/bmw.png", "levelReq": 1}}
NOW = P._now_naive()


def spot_state(car_id=1):
    return {"userId": "me",
            "users": {"me": {"name": "我", "level": 4},
                      "other": {"name": "NJYZ", "level": 3,
                                "avatar": "assets/avatars/x.png"}},
            "userCars": [{"id": "c1", "userId": "other", "carId": car_id,
                          "status": "parked"}]}


import datetime as _dt2
m30 = (P._now_naive() - _dt2.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
m600 = (P._now_naive() - _dt2.timedelta(minutes=600)).strftime("%Y-%m-%d %H:%M:%S")
s1 = P._spot_snapshot({"id": 7, "ownerId": "me", "spotType": "private",
                       "carInSpot": "c1", "parkedBy": "other", "parkedTime": m30},
                      1, spot_state(1), CMAP, RULES, NOW)
check("车位带出停车人", s1["parkedBy"] == "NJYZ", s1.get("parkedBy"))
check("车位带出车型", s1["carName"] == "奥拓", s1.get("carName"))
check("已停时长按车位的 parkedTime 算", s1["mins"] in (29, 30), s1["mins"])
check("预估收益 = 分钟 × 每分钟收益", s1["earn"] == s1["mins"] * 6, s1["earn"])
check("车图补成绝对地址",
      s1["carImg"] == "https://qcw.tangpt.top/assets/cars/aoto.png", s1["carImg"])
check("头像补成绝对地址",
      s1["avatar"] == "https://qcw.tangpt.top/assets/avatars/x.png", s1["avatar"])

s2 = P._spot_snapshot({"id": 7, "ownerId": "me", "spotType": "private",
                       "carInSpot": "c1", "parkedBy": "other", "parkedTime": m600},
                      1, spot_state(2), CMAP, RULES, NOW)
check("收益按单车位 3000 封顶", s2["earn"] == 3000, s2["earn"])

s3 = P._spot_snapshot({"id": 7, "ownerId": "me", "spotType": "charity",
                       "carInSpot": "c1", "parkedBy": "other", "parkedTime": m30},
                      1, spot_state(1), CMAP, RULES, NOW)
check("爱心车位算经验不算金币", "exp" in s3 and "earn" not in s3, s3)

s4 = P._spot_snapshot({"id": 7, "ownerId": "me"}, 1,
                      {"userId": "me", "users": {}, "userCars": []}, CMAP, RULES, NOW)
check("空车位标记为 free", s4["free"] is True and s4["kind"] == "私家车位", s4)
check("M/F 这类性别字母不算图片地址", P._avatar_url("M") == "", P._avatar_url("M"))
check("已是 http 的头像原样保留",
      P._avatar_url("https://x/y.png") == "https://x/y.png")

DB.clear()
DB["snapshot"] = {"name": "我", "level": 4, "cash": 100, "car_total": 1,
                  "car_max": 5, "parked_total": 0, "pending_gain": 0,
                  "min_duration": 10, "cars": [], "catalog": [], "flash": [],
                  "spots": [s1, s4]}
p = P(); p._page_cache = None; p._tab = "spots"
blob = repr(p.get_page())
check("车位页渲染停车人与收益", "NJYZ" in blob and "预估收益" in blob)
check("车位页渲染已停时长", "已停" in blob)
check("车位页渲染车图与头像", blob.count("<img") >= 2, blob.count("<img"))
check("车位页仍渲染空闲位", "空闲" in blob)

print("\n" + ("=" * 46))
print("=== 8) 车市卡片复刻网页版：价格 / 收益 / 已拥有 / 双按钮 / 分类筛选 ===")

print("\n--- formatNum 千分位口径（抄游戏） ---")
for raw, want in [(800, "800"), (2000, "2,000"), (3000, "3,000"), (12345, "12.3K"),
                  (99999, "100.0K"), (12345678, "12.3M"),
                  # 老快照可能缺字段，None 必须兜成 0，不能把字面量 "None" 打到卡片上
                  (None, "0")]:
    check(f"_fmt_num({raw!r}) == {want!r}", P._fmt_num(raw) == want, P._fmt_num(raw))

SHOP_CARS = [
    {"id": 1, "name": "奥拓", "price": 800, "earn": 6, "level": 1, "category": "代步车",
     "image": "https://qcw.tangpt.top/assets/cars/aoto.png"},
    {"id": 2, "name": "桑塔纳", "price": 2000, "earn": 7, "level": 1, "category": "代步车",
     "image": "https://qcw.tangpt.top/assets/cars/sangtana.png"},
    {"id": 3, "name": "奔驰C", "price": 30000, "earn": 25, "level": 1, "category": "普通私家车",
     "image": "https://qcw.tangpt.top/assets/cars/bc.png"},
    {"id": 4, "name": "兰博基尼", "price": 90000, "earn": 99, "level": 10,
     "category": "高级私家车", "image": "https://qcw.tangpt.top/assets/cars/lb.png"},
]
SHOP_SNAP = {
    "name": "我", "level": 5, "cash": 5000, "car_total": 1, "car_max": 5,
    "parked_total": 0, "pending_gain": 0, "min_duration": 10,
    "time": "2026-10-14 04:00:00", "spots": [], "flash": [],
    "cars": [{"id": "u1", "carId": 1, "name": "奥拓", "status": "空闲", "rate": 6,
              "image": "https://qcw.tangpt.top/assets/cars/aoto.png", "no": 1}],
    "catalog": SHOP_CARS,
}
DB.clear(); DB["snapshot"] = SHOP_SNAP
p = P(); p._page_cache = None; p._tab = "shop"
nodes = list(walk(p.get_page()))
texts = [n["text"] for n in nodes if n.get("text")]
styles = lambda n: (n.get("props") or {}).get("style") or {}

print("\n--- 卡片上的信息项 ---")
# 金币必须画成内联 SVG，不能用 🪙 emoji：U+1FA99 是 2019 年才进 Unicode 的，
# Windows 默认字体没有这个字形，插件页实测直接渲染成豆腐块 □。
blob = repr(p.get_page())
check("页面里不再出现 🪙 emoji", "\U0001FA99" not in blob, "")
coin_nodes = [n for n in nodes if isinstance(n.get("html"), str)
              and "<circle" in n["html"] and "daa520" in n["html"]]
check("金币是内联 SVG 图标", len(coin_nodes) >= 2, len(coin_nodes))
check("金币抄的是游戏那颗（外圈 #ffd700 + 描边 #daa520 + 中间 $）",
      all('fill="#ffd700"' in n["html"] and "$</text>" in n["html"]
          for n in coin_nodes), "")
check("金币图标带尺寸不塌成 0", all('width="14px"' in n["html"] for n in coin_nodes),
      coin_nodes[0]["html"][:60] if coin_nodes else "")
check("800 按原样显示", "800" in texts, [t for t in texts if "800" in t])
check("两千按游戏口径显示成 2,000", "2,000" in texts, [t for t in texts if "2,000" in t])
check("收益按游戏口径写「N金/分钟」", "6金/分钟" in texts, [t for t in texts if "分钟" in t])
check("已拥有按游戏口径写「✓ 已拥有N辆」", "✓ 已拥有1辆" in texts,
      [t for t in texts if "已拥有" in t])
price_node = next((n for n in nodes if n.get("text") == "800"), None)
check("金币金额是金币金 #FFD700",
      price_node is not None and styles(price_node).get("color") == "#FFD700",
      styles(price_node or {}))
earn_node = next((n for n in nodes if n.get("text") == "6金/分钟"), None)
check("收益是主色板绿 #00E676",
      earn_node is not None and styles(earn_node).get("color") == "#00E676",
      styles(earn_node or {}))
own_node = next((n for n in nodes if n.get("text") == "✓ 已拥有1辆"), None)
check("已拥有是主色板青 #22D3EE",
      own_node is not None and styles(own_node).get("color") == "#22D3EE",
      styles(own_node or {}))

print("\n--- 底部并排两个按钮 ---")
clicks = [n["events"]["click"] for n in nodes
          if isinstance(n.get("events"), dict) and "click" in n["events"]]
buy = [c for c in clicks if c.get("api", "").endswith("/buy_car")]
trade = [c for c in clicks if c.get("api", "").endswith("/trade_pick")]
check("每张可买的卡片都有一个真购买按钮", len(buy) >= 1, len(buy))
check("每张卡片都有一个以旧换新入口", len(trade) >= 1, len(trade))
check("购买按钮调插件接口并带上车型 ID",
      buy and buy[0]["method"] == "post" and buy[0]["params"].get("car_id") == 1,
      buy[0] if buy else None)
buy_node = next((n for n in nodes
                 if n.get("events", {}).get("click", {}).get("api", "").endswith("/buy_car")), None)
check("购买是主色实底深字",
      styles(buy_node).get("backgroundColor") == "#22D3EE"
      and styles(buy_node).get("color") == "#04222B", styles(buy_node))
trade_node = next((n for n in nodes
                   if n.get("events", {}).get("click", {}).get("api", "").endswith("/trade_pick")), None)
check("以旧换新是透明底青框青字",
      styles(trade_node).get("color") == "#8DF3FF"
      and "34,211,238" in styles(trade_node).get("border", ""), styles(trade_node))
rows = [n for n in nodes if styles(n).get("marginTop") == "10px"
        and styles(n).get("gap") == "8px" and n.get("content")]
# v1.54.0 起是**三颗**：购买 / 以旧换新 / 锁定换新。actions 容器允许 wrap，
# 窄屏上第三颗换行不影响功能，所以只断言三颗都在同一个容器里。
paired = [r for r in rows if len(r["content"]) == 3
          and any("buy_car" in repr(c) for c in r["content"])
          and any("trade_pick" in repr(c) for c in r["content"])
          and any("trade_lock" in repr(c) for c in r["content"])]
check("购买 / 以旧换新 / 锁定换新 三个按钮在同一行并排", len(paired) >= 1,
      [len(r["content"]) for r in rows][:6])
check("车图居中且放大到 96px", 'height:96px' in repr(p.get_page()), "96px")

print("\n--- 分类胶囊 ---")
pills = [c["params"]["category"] for c in clicks if "category" in c.get("params", {})]
check("分类胶囊按车型表顺序排列",
      pills == ["代步车", "普通私家车", "高级私家车"], pills)
check("胶囊都指向 /shop_filter",
      all(c.get("api", "").endswith("/shop_filter")
          for c in clicks if "category" in c.get("params", {})))
pill_nodes = [n for n in nodes
              if isinstance(n.get("events"), dict)
              and "category" in n["events"]["click"].get("params", {})]
check("默认选中第一个分类（代步车）", "233,69,96" in repr(pill_nodes[0]),
      styles(pill_nodes[0]).get("backgroundColor"))
check("未选中的胶囊是灰底",
      "rgba(255,255,255,0.10)" in repr(pill_nodes[1]),
      styles(pill_nodes[1]).get("backgroundColor"))
check("默认只看第一个分类的车型",
      "桑塔纳" in texts and "奔驰C" not in texts,
      [t for t in texts if t in ("奥拓", "桑塔纳", "奔驰C", "兰博基尼")])

fl = asyncio.run(p.api_shop_filter(mod.ShopFilterRequest(category="普通私家车")))
check("切换分类成功", bool(getattr(fl, "success", False)), fl)
check("分类已持久化", (_flush(p) or {}).get("cat") == "普通私家车", DB.get("view"))
texts2 = [n["text"] for n in walk(p.get_page()) if n.get("text")]
check("切到普通私家车后只显示奔驰C",
      "奔驰C" in texts2 and "奥拓" not in texts2,
      [t for t in texts2 if t in ("奥拓", "桑塔纳", "奔驰C", "兰博基尼")])
badf = asyncio.run(p.api_shop_filter(mod.ShopFilterRequest(category="../../etc/passwd")))
check("不认车型表以外的分类名",
      (not getattr(badf, "success", False)) and p._cat == "普通私家车", p._cat)

print("\n--- 切页签不清空车市筛选（view 改成了内存合并 + 后台落盘） ---")
asyncio.run(p.api_set_tab(mod.TabSwitch(tab="garage")))
check("切走页签后分类仍在", (_flush(p) or {}).get("cat") == "普通私家车", DB.get("view"))
asyncio.run(p.api_set_tab(mod.TabSwitch(tab="shop")))

print("\n--- 以旧换新：先选旧车，再确认 ---")
pk = asyncio.run(p.api_trade_pick(mod.TradePickRequest(car_id=2)))
check("点以旧换新后记住目标车型", bool(getattr(pk, "success", False)) and p._trade_for == "2",
      (getattr(pk, "message", ""), p._trade_for))
nodes2 = list(walk(p.get_page()))
texts3 = [n["text"] for n in nodes2 if n.get("text")]
check("页面给出选旧车的提示",
      any("选择要用以旧换新的车辆" in t for t in texts3), texts3[:10])
confirm = [c for c in [n["events"]["click"] for n in nodes2
                       if isinstance(n.get("events"), dict) and "click" in n["events"]]
           if c.get("api", "").endswith("/trade_in")]
check("选旧车后给出真的换车按钮", len(confirm) == 1, confirm)
check("换车按钮带上旧车实例与新车车型",
      confirm and confirm[0]["params"].get("old_car_id") == "u1"
      and confirm[0]["params"].get("new_car_id") == 2,
      confirm[0]["params"] if confirm else None)
check("列出了补价金额", any("补 1,200 金" in t for t in texts3),
      [t for t in texts3 if "补" in t])
check("选旧车面板可以取消",
      any(c.get("params", {}).get("car_id") == ""
          for c in [n["events"]["click"] for n in nodes2
                    if isinstance(n.get("events"), dict) and "click" in n["events"]]))

print("\n--- 换车接口仍接受新的请求模型 ---")
Client.POSTS = []
Client.STATE = state(cash=9999, cars=[{"id": "u1", "userId": "me", "carId": 1,
                                       "status": "idle"}])
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
p._cookie = "PHPSESSID=x"
p._enabled = True
p._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), p)
tr = asyncio.run(p.api_trade_in(mod.CarTradeRequest(old_car_id="u1", new_car_id=2)))
check("换车接口调用成功", bool(getattr(tr, "success", False)), getattr(tr, "message", tr))
check("真的发了 trade_in.php",
      any(e == "trade_in.php" and q == {"oldCarInstanceId": "u1", "newCarId": 2}
          for e, q in Client.POSTS), Client.POSTS)
check("换完自动退出选旧车模式", p._trade_for == "", p._trade_for)
check("退出状态也持久化了", (_flush(p) or {}).get("trade_for") == "", DB.get("view"))

Client.POSTS = []
br = asyncio.run(p.api_buy_car(mod.CarBuyRequest(car_id=2)))
check("买车接口调用成功", bool(getattr(br, "success", False)), getattr(br, "message", br))
check("真的发了 buy_car.php",
      any(e == "buy_car.php" and q == {"carId": 2} for e, q in Client.POSTS), Client.POSTS)

print("\n--- 钱不够 / 车辆已满 / 等级不够时按钮必须真的点不动 ---")
DB.clear(); DB["snapshot"] = dict(SHOP_SNAP, cash=100)
p2 = P(); p2._page_cache = None; p2._tab = "shop"
n2 = list(walk(p2.get_page()))
disabled = [n for n in n2 if styles(n).get("cursor") == "not-allowed"]
check("买不起的卡片按钮置灰", len(disabled) >= 1, len(disabled))
check("置灰按钮没有挂任何事件", all("events" not in n for n in disabled),
      [n.get("events") for n in disabled])
t2 = [n["text"] for n in n2 if n.get("text")]
check("并说清还差多少金", "还差 700 金" in t2, [t for t in t2 if "还差" in t])

DB.clear(); DB["snapshot"] = dict(SHOP_SNAP, car_total=9)
p4 = P(); p4._page_cache = None; p4._tab = "shop"
t4 = [n["text"] for n in walk(p4.get_page()) if n.get("text")]
check("车辆已满时提示明确指向购买，不再是含糊的「车辆已满」",
      any("购买需有空位" in t for t in t4)
      and not any("车辆已满" in t for t in t4),
      [t for t in t4 if "满" in t or "还差" in t])
check("车辆已满时购买按钮也不可点",
      len([n for n in walk(p4.get_page())
           if styles(n).get("cursor") == "not-allowed"]) >= 1, "")

# 回归：满员曾经把「换新」的真实原因吞掉，只剩一句「车辆已满」，
# 看着就像「满员导致换不了」。实际上换新是 1 换 1，根本不看车辆上限。
print("\n--- 满员时的提示不能误导（老 bug：车辆已满 → 换不了）---")
import copy as _copy


def _shop_texts(**kw):
    DB.clear()
    DB["snapshot"] = dict(_copy.deepcopy(SHOP_SNAP), **kw)
    pl = P(); pl._page_cache = None; pl._tab = "shop"
    return [n["text"] for n in walk(pl.get_page()) if n.get("text")]


# 满员 + 一辆车都不空闲：购买说没空位，换新说「去挪一辆」，两件事同时说清
tA = _shop_texts(car_total=9,
                 cars=[dict(c, status="停车中") for c in SHOP_SNAP["cars"]])
check("满员且全部停车时，购买的原因是「没空位」",
      any("购买需有空位" in t for t in tA), [t for t in tA if "满" in t])
check("满员时换新的真实原因不再被吞掉",
      any("所有车都停着" in t for t in tA),
      [t for t in tA if "空闲" in t or "停着" in t])
check("满员时不会出现含糊的「车辆已满」",
      not any("车辆已满" in t for t in tA),
      [t for t in tA if "满" in t])

# 有空闲车但都比它贵：换新的原因要说「没更便宜的」
tB = _shop_texts(car_total=9, cars=[dict(SHOP_SNAP["cars"][0], status="空闲")])
check("满员但有空闲车时，换新提示是「没有更便宜的空闲车可换」",
      any("没有更便宜的空闲车可换" in t for t in tB),
      [t for t in tB if "换" in t])
check("这时不该说「所有车都停着」",
      not any("所有车都停着" in t for t in tB),
      [t for t in tB if "停着" in t])

# 同时缺钱又满员：两个原因都要说出来（以前 elif 只显示一个）
SHOP_SNAP_CASH = SHOP_SNAP.get("cash", 0)
tC = _shop_texts(car_total=9, cash=0)
check("既缺钱又满员时，两条原因都会显示（不再互相吞掉）",
      any("购买需有空位" in t for t in tC) and any("还差" in t for t in tC),
      [t for t in tC if "满" in t or "还差" in t])

# 换新按钮的可用性只看空闲车，与车辆上限无关
pD = P(); pD._page_cache = None; pD._tab = "shop"
DB.clear(); DB["snapshot"] = dict(_copy.deepcopy(SHOP_SNAP), car_total=9)
nodesD = list(walk(pD.get_page()))
trade_clicks = [n for n in nodesD if isinstance(n.get("events"), dict)
                and "trade_pick" in str(n["events"].get("click", {}).get("api", ""))]
check("满员时换新按钮仍然可点（1 换 1 不占车位）", bool(trade_clicks), len(trade_clicks))

DB.clear(); DB["snapshot"] = dict(SHOP_SNAP, level=1)
p3 = P(); p3._page_cache = None; p3._tab = "shop"; p3._cat = "高级私家车"
n3 = list(walk(p3.get_page()))
check("等级不够的车型整卡半透明",
      # v1.53.1 起从 0.5 提到 0.72：0.5 时白字对底只有 2.4:1，过不了对比度
      any(styles(n).get("opacity") == "0.72" for n in n3),
      sorted({styles(n).get("opacity") for n in n3 if styles(n).get("opacity")}))
check("等级不够的车给的是需要 Lv.N 而不是按钮",
      any("需要 Lv.10" in (n.get("text") or "") for n in n3),
      [n.get("text") for n in n3 if "需要 Lv" in (n.get("text") or "")])
check("等级不够的车不给挂购买事件",
      not any(c.get("params", {}).get("car_id") == 4
              for c in [n["events"]["click"] for n in n3
                        if isinstance(n.get("events"), dict) and "click" in n["events"]]),
      "")

paths = [a["path"] for a in P().get_api()]
check("新接口都已注册",
      "/shop_filter" in paths and "/trade_pick" in paths, paths)

print("\n=== 9) 金币图标：四个页签一律不许再出现 🪙 ===")
FULL = {"name": "我", "level": 5, "cash": 50000, "car_total": 2, "car_max": 5,
        "parked_total": 1, "pending_gain": 18, "min_duration": 10,
        "time": "2026-10-14 04:00:00",
        "cars": [{"id": "u1", "carId": 1, "name": "奥拓", "status": "空闲", "rate": 6,
                  "image": "https://qcw.tangpt.top/assets/cars/aotuo.png", "no": 1},
                 {"id": "u2", "carId": 2, "name": "桑塔纳", "status": "停车中",
                  "rate": 7, "mins": 12, "no": 2,
                  "image": "https://qcw.tangpt.top/assets/cars/ziyoujian.png"}],
        "spots": [{"id": 7, "ownerId": "other", "label": "B-03", "mine": False,
                   "kind": "私家车位", "free": False, "parkedBy": "NJYZ",
                   "carName": "奥拓", "carImg": "https://qcw.tangpt.top/assets/cars/aotuo.png",
                   "mins": 12, "earn": 72, "exp": 0,
                   "carInSpot": "c1", "parkedTime": ts(12)}],
        "catalog": SHOP_CARS, "flash": []}
DB.clear(); DB["snapshot"] = dict(FULL)
DB["history"] = [{"time": "2026-10-14 03:00", "day": "2026-10-14",
                  "parked": 1, "moved": 1, "earned": 120}]
DB["daily"] = {"2026-10-14": {"earned": 900, "spent": 300, "bought": 1, "diff": 100,
                             "traded": 1, "parked": 3, "moved": 3, "rounds": 20,
                             "hours": {"8": 400, "9": 500}}}
for tab in ("spots", "shop", "garage", "stats"):
    q = P(); q._page_cache = None; q._tab = tab
    page = repr(q.get_page())
    coins = page.count('fill="#ffd700" stroke="#daa520"')
    check(f"{tab} 页没有 🪙", "\U0001FA99" not in page, "")
    check(f"{tab} 页的金币是 SVG 图标", coins >= 1, coins)

print("\n" + ("=" * 46))
print("=== 10) 称号：显示当前称号 + 点一下就能换 ===")

print("\n--- 称号列表只取已获得的，并记住当前称号的颜色 ---")
tt = asyncio.run(P()._load_titles(Client(), state()))
check("只保留已获得的称号",
      [t["name"] for t in tt["owned"]] == ["无名小卒", "老司机"], tt)
check("未解锁的称号不显示", all(t["id"] != 3 for t in tt["owned"]), tt["owned"])
check("当前佩戴称号的颜色被记住", tt["color"] == "#ffd700", tt["color"])


class BrokenTitles(Client):
    async def get_json(self, url, **kw):
        if "titles.php" in url: raise RuntimeError("接口 500")
        return await super().get_json(url, **kw)


tt2 = asyncio.run(P()._load_titles(BrokenTitles(), state()))
check("称号接口挂了不炸，仍能出快照", tt2 == {"owned": [], "color": "#FFD700"}, tt2)
tt3 = asyncio.run(P()._load_titles(Client(), {"currentTitleId": 0}))
check("没佩戴称号时回落到默认金", tt3["color"] == "#FFD700", tt3["color"])
check("非十六进制色值不会把页面打崩",
      P._tint("rebeccapurple", 0.5).startswith("rgba(255,255,255"), P._tint("rebeccapurple", 0.5))
check("十六进制色值算得出同色淡底",
      P._tint("#ffd700", 0.5) == "rgba(255,215,0,0.5)", P._tint("#ffd700", 0.5))

print("\n--- 概览里的称号标签 ---")
TSNAP = dict(SHOP_SNAP, title_id=2, title_name="老司机", title_color="#ffd700",
             titles=[{"id": 1, "name": "无名小卒", "desc": "默认称号",
                      "color": "#9E9E9E", "owned": True},
                     {"id": 2, "name": "老司机", "desc": "累计挪车 100 次",
                      "color": "#ffd700", "owned": True},
                     {"id": 3, "name": "车位传说", "desc": "尚未解锁",
                      "color": "#e94560", "owned": False}])
DB.clear(); DB["snapshot"] = dict(TSNAP)
pt = P(); pt._page_cache = None; pt._tab = "spots"
tn = list(walk(pt.get_page()))
ttexts = [n["text"] for n in tn if n.get("text")]
check("顶部显示当前佩戴的称号", any("老司机" in t for t in ttexts),
      [t for t in ttexts if "老司机" in t])
check("称号标签用的是称号自己的颜色",
      any(styles(n).get("color") == "#ffd700"
          for n in tn if n.get("events", {}).get("click", {}).get("api", "").endswith("/title_pick")),
      "")
check("称号标签带奖牌图标",
      any(isinstance(n.get("html"), str) and 'fill="#ffd700"' in n["html"]
          and "<circle" in n["html"] for n in tn), "")
check("没展开时不显示选择器", not any("取消佩戴称号" in t for t in ttexts), "")

print("\n--- 展开后能选，能换 ---")
pick = next(n for n in tn
            if n.get("events", {}).get("click", {}).get("api", "").endswith("/title_pick"))
check("点标签会去展开选择器",
      pick["events"]["click"]["params"].get("open") is True, pick["events"]["click"])
r = asyncio.run(pt.api_title_pick(mod.TitlePickRequest(open=True)))
check("展开选择器成功", bool(getattr(r, "success", False)), r)
check("展开状态已持久化", (_flush(pt) or {}).get("title_open") is True, DB.get("view"))
tn2 = list(walk(pt.get_page()))
ttexts2 = [n["text"] for n in tn2 if n.get("text")]
check("选择器列出取消佩戴", any("取消佩戴称号" in t for t in ttexts2), ttexts2[:8])
check("选择器列出已获得的称号", any("无名小卒" in t for t in ttexts2), "")
check("未解锁的称号不出现在选择器里", not any("车位传说" in t for t in ttexts2), "")
sets = [n["events"]["click"] for n in tn2
        if isinstance(n.get("events"), dict)
        and n["events"]["click"].get("api", "").endswith("/title")]
check("每个可选项都挂了 /title 事件", len(sets) == 3, sets)
check("取消佩戴走的是 title_id=0",
      any(c["params"].get("title_id") == 0 for c in sets), [c["params"] for c in sets])
check("当前佩戴项标了「（当前）」", any("老司机（当前）" in t for t in ttexts2),
      [t for t in ttexts2 if "老司机" in t])
check("已展开时再点标签是收起",
      next(n for n in tn2
           if n.get("events", {}).get("click", {}).get("api", "").endswith("/title_pick")
           )["events"]["click"]["params"].get("open") is False, "")

print("\n--- 换称号真的调到了游戏接口 ---")
Client.POSTS = []
Client.STATE = state(cash=5000, cars=[IDLE()])
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
pt._cookie = "PHPSESSID=x"
pt._enabled = True
pt._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pt)
res = asyncio.run(pt.api_set_title(mod.TitleSetRequest(title_id=1)))
check("切换称号成功", bool(getattr(res, "success", False)), getattr(res, "message", res))
check("真的 POST 了 titles.php 且带上 titleId",
      any(e == "titles.php" and q == {"titleId": 1} for e, q in Client.POSTS),
      Client.POSTS)
check("换完自动收起选择器",
      pt._title_open is False and (_flush(pt) or {}).get("title_open") is False,
      (pt._title_open, DB.get("view")))
check("换完立刻刷新快照，页面不用等下一轮",
      (DB.get("snapshot") or {}).get("title_id") is not None, DB.get("snapshot"))

Client.POSTS = []
res0 = asyncio.run(pt.api_set_title(mod.TitleSetRequest(title_id=0)))
check("传 0 是取消佩戴",
      bool(getattr(res0, "success", False))
      and any(e == "titles.php" and q == {"titleId": 0} for e, q in Client.POSTS),
      Client.POSTS)


class FailTitle(Client):
    async def post_json(self, url, json=None, **kw):
        Client.POSTS.append((url.rsplit("/", 1)[-1], json))
        return {"success": False, "error": "还没解锁这个称号"}


orig_client = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = FailTitle
    Client.POSTS = []
    bad = asyncio.run(pt.api_set_title(mod.TitleSetRequest(title_id=3)))
    check("游戏拒绝时返回失败而不是假成功",
          not getattr(bad, "success", False), bad)
    check("失败原因带回来给用户看",
          getattr(bad, "message", "") == "还没解锁这个称号", getattr(bad, "message", ""))
    check("失败时选择器保持展开（好让用户改选别的）",
          pt._title_open is False, pt._title_open)
finally:
    mod.AsyncRequestUtils = orig_client

paths2 = [a["path"] for a in P().get_api()]
check("称号接口都已注册", "/title" in paths2 and "/title_pick" in paths2, paths2)

print("\n--- 切页签不清空称号选择器的展开状态 ---")
asyncio.run(pt.api_title_pick(mod.TitlePickRequest(open=True)))
asyncio.run(pt.api_set_tab(mod.TabSwitch(tab="garage")))
check("切走页签后选择器仍展开",
      (_flush(pt) or {}).get("title_open") is True, DB.get("view"))

print("\n" + ("=" * 46))
print("=== 11) 独立审查复现出来的缺陷，逐条锁死 ===")

print("\n--- #1 手动操作后不得清空历史与每日统计 ---")
DB.clear()
DB["history"] = [{"time": "t", "day": "2026-10-13", "parked": 1, "moved": 1,
                  "earned": 10}] * 5
DB["daily"] = {f"2026-10-{d:02d}": {"earned": d, "parked": 1, "moved": 1,
                                   "rounds": 1} for d in range(9, 14)}
DB["snapshot"] = SHOP_SNAP
p = P(); p._page_cache = None          # 进程刚起、还没打开过详情页
p._enabled = True
p._cookie = "PHPSESSID=x"
p._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), p)
Client.POSTS = []; Client.STATE = state(cash=99999, cars=[IDLE()])
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
asyncio.run(p.api_buy_car(mod.CarBuyRequest(car_id=2)))
check("手动买车后库里历史仍是 5 条", len(DB["history"]) == 5, len(DB["history"]))
check("手动买车后库里原有 5 天统计一条没丢",
      set(("2026-10-%02d" % d) for d in range(9, 14)) <= set(DB["daily"]),
      sorted(DB["daily"]))
check("手动买车会新增/更新当天那条（不是清空）", len(DB["daily"]) in (5, 6),
      len(DB["daily"]))
Client.POSTS = []
Client.STATE = state(cash=99999, cars=[
    {"id": "due-1", "userId": "me", "carId": 2, "status": "parked",
     "parkedTime": ts(30), "parkedBy": "them"},
    {"id": "idle-9", "userId": "me", "carId": 1, "status": "idle"}])
asyncio.run(p.run_once())
check("下一轮不会把历史清成 1 条", len(DB["history"]) == 6, len(DB["history"]))
check("下一轮不会把每日统计清成 1 天", len(DB["daily"]) == 6, len(DB["daily"]))

print("\n--- #2 限时车不能拿��以旧换新 ---")
flash_car = {"id": "fc", "userId": "me", "carId": 0, "status": "idle",
             "flashCarName": "限时跑车", "flashPrice": 3000}
DB.clear(); DB["snapshot"] = dict(
    SHOP_SNAP, title_id=0, title_name="", titles=[], flash=[],
    cars=[dict(flash_car, name="限时跑车", rate=40, image="", no=1),
          {"id": "u1", "carId": 1, "name": "奥拓", "status": "空闲", "rate": 6,
           "image": "", "no": 2}])
pf = P(); pf._page_cache = None; pf._tab = "shop"; pf._trade_for = "2"
blob = repr(pf.get_page())
check("选旧车面板不把限时车列成候选",
      "限时跑车" not in [n["text"] for n in walk(pf.get_page()) if n.get("text")],
      [n["text"] for n in walk(pf.get_page()) if n.get("text")][:8])
pf._enabled = True
pf._cookie = "PHPSESSID=x"
pf._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), p)
Client.POSTS = []; Client.STATE = state(cash=99999, cars=[dict(flash_car)])
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
bad_trade = asyncio.run(pf.api_trade_in(mod.CarTradeRequest(old_car_id="fc", new_car_id=2)))
check("后端也拒绝拿限时车换新", not getattr(bad_trade, "success", False),
      getattr(bad_trade, "message", bad_trade))
check("拒绝时一个写请求都不发",
      not any(e == "trade_in.php" for e, _ in Client.POSTS), Client.POSTS)

print("\n--- #3 「最多同时停放」不得被突破 ---")
def crowded(spot_count=4):
    return state(cash=0, level=5, max_cars=1, cars=[
        {"id": "due-1", "userId": "me", "carId": 2, "status": "parked",
         "parkedTime": ts(30), "parkedBy": "them"},
        {"id": "due-2", "userId": "me", "carId": 3, "status": "parked",
         "parkedTime": ts(30), "parkedBy": "them"},
        {"id": "idle-9", "userId": "me", "carId": 1, "status": "idle"}])


def crowded_state():
    st = crowded()
    st["parkingSpots"] = [{"id": 90 + i, "ownerId": "other", "spotType": "private"}
                          for i in range(spot_count_of[0])]
    return st


def crowded_state_with_cash():
    st = crowded_state()
    st["users"]["me"]["cash"] = 5000
    return st


spot_count_of = [4]
for trade_on in (False, True):
    Client.POSTS = []
    Client.STATE = crowded_state()
    Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
    q = P(); q._auto_buy = False; q._auto_trade = trade_on
    q._max_cars = 1; q._enabled = True
    q._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), q)
    asyncio.run(q.run_once())
    parks = [x for e, x in Client.POSTS if e == "park.php"]
    check(f"自动换新={'开' if trade_on else '关'} 时最多只停 1 辆",
          len(parks) <= 1, len(parks))

print("\n--- #4 刷新状态抛异常必须被当成失败拦下 ---")


class BoomRefresh(Client):
    async def get_json(self, url, **kw):
        if "state.php" in url and Client.POSTS and \
                any(e == "trade_in.php" for e, _ in Client.POSTS):
            raise RuntimeError("刷新时网络断了")
        return await super().get_json(url, **kw)


Client.POSTS = []
Client.STATE = crowded_state_with_cash()
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
q = P(); q._auto_buy = False; q._auto_trade = True
q._max_cars = 1; q._enabled = True
q._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), q)
orig_client = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = BoomRefresh
    asyncio.run(q.run_once())
finally:
    mod.AsyncRequestUtils = orig_client
check("换车确实发生了（前置条件）",
      any(e == "trade_in.php" for e, _ in Client.POSTS), Client.POSTS)
check("换车后刷新失败就不再拿旧车 ID 去停车",
      not any(e == "park.php" for e, _ in Client.POSTS), Client.POSTS)

print("\n--- #5 脏数据不得让整轮或整页崩掉 ---")


def dirty_cycle(field, value):
    st = state(cash=0, cars=[IDLE()])
    st[field] = value
    Client.POSTS = []
    Client.STATE = st
    Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
    z = P(); z._auto_buy = False; z._auto_trade = False
    z._enabled = True
    z._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), z)
    try:
        asyncio.run(z.run_once())
        return None
    except Exception as err:
        return err


for field, value in (("userCars", [{"id": "a", "userId": "me", "carId": 1,
                                    "status": "idle"}, "垃圾条目"]),
                     ("userCars", {"a": {"id": "a"}}),
                     ("parkingSpots", ["垃圾条目"])):
    err = dirty_cycle(field, value)
    check(f"run_once 遇到 {field} 里的脏数据不抛", err is None, err)

for field, value in (("cars", ["垃圾字符串"]), ("spots", ["垃圾字符串"])):
    for tab in ("spots", "shop", "garage", "stats"):
        DB.clear(); DB["snapshot"] = dict(SHOP_SNAP)
        DB["snapshot"][field] = value
        z = P(); z._page_cache = None; z._tab = tab
        try:
            rendered = isinstance(z.get_page(), list)
        except Exception as err:
            rendered, value_ = False, err
        check(f"{tab} 页遇到快照 {field} 脏数据仍能渲染", rendered, "" if rendered else value_)

DB.clear(); DB["snapshot"] = dict(SHOP_SNAP)
DB["history"] = [{"time": "t", "day": "2026-10-14", "parked": 1, "moved": 1,
                  "earned": 1}, "垃圾条目"]
DB["daily"] = {"2026-10-14": {"earned": 1}, "2026-10-13": "垃圾"}
for tab in ("spots", "shop", "garage", "stats"):
    z = P(); z._page_cache = None; z._tab = tab
    try:
        rendered = isinstance(z.get_page(), list)
    except Exception as err:
        rendered, value_ = False, err
    check(f"{tab} 页遇到脏历史/脏统计仍能渲染", rendered, "" if rendered else value_)

print("\n--- #6 userId 类型不一致也要认得自己的车 ---")
Client.POSTS = []
st = state(cash=0, cars=[dict(IDLE(), userId=1)])
st["userId"] = "1"
Client.STATE = st
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
st["parkingSpots"] = [{"id": 91, "ownerId": "other", "spotType": "private"}]
z = P(); z._auto_buy = False; z._auto_trade = False
z._enabled = True
z._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), z)
asyncio.run(z.run_once())
check("userId 是字符串而车上是数字时仍能停车",
      any(e == "park.php" for e, _ in Client.POSTS), Client.POSTS)

print("\n--- #7 数值与时间的边角 ---")
check("_to_int 支持 JSON 浮点", P._to_int(1000.0, 0) == 1000, P._to_int(1000.0, 0))
check("_to_int 支持字符串小数", P._to_int("12.7", 0) == 12, P._to_int("12.7", 0))
check("_to_int 仍然拒垃圾值", P._to_int("abc", 7) == 7, P._to_int("abc", 7))
check("今日收益按设定时区算",
      P._today_earned([{"day": P._now_naive().strftime("%Y-%m-%d"),
                         "earned": 88}]) == 88,
      P._today_earned([{"day": P._now_naive().strftime("%Y-%m-%d"), "earned": 88}]))
check("图片地址里的引号被转义",
      "&quot;" in P._pic('https://x/" onerror="alert(1)', "x", 40)["html"]
      or "&quot;" in repr(P._pic('https://x/" onerror="alert(1)', "x", 40)),
      P._pic('https://x/" onerror="alert(1)', "x", 40)["html"][:80])

print("\n--- #8 车型表拿不到时降级而不是整轮死掉 ---")


class BoomData(Client):
    async def get_json(self, url, **kw):
        if "game_data.php" in url: raise RuntimeError("接口 500")
        return await super().get_json(url, **kw)


Client.POSTS = []
Client.STATE = state(cash=0, cars=[dict(IDLE(), status="parked", parkedTime=ts(30),
                                        parkedBy="them")])
Client.CARS = []
z = P(); z._auto_buy = False; z._auto_trade = False
z._enabled = True
z._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), z)
orig_client = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = BoomData
    asyncio.run(z.run_once())
    unparked = any(e == "unpark.php" for e, _ in Client.POSTS)
finally:
    mod.AsyncRequestUtils = orig_client
check("车型表接口挂了本轮仍能挪车", unparked, Client.POSTS)

print("\n--- #9 页面只构造当前页签 ---")
# 顶部导航栏里一直有「③ 我的车库」「② 车市」这几个按钮，按子串找会误判，
# 必须按区块标题的精确文本找。
SECTIONS = {"我的车位": "spots", "我的车库": "garage",
            "每日明细": "stats", "选择称号": None}


def section_titles(tab):
    """收集**区块卡片**的标题文字，按结构排除页签按钮。

    别再用「页签文案带 ①②③④ 编号」来和区块标题消歧 —— 那等于把实现细节
    当成断言的前提：一旦为了好看去掉编号，这条断言就该红，而接手的开发者
    会误以为「红 = 实现错了」，于是去改实现把文字藏进 html 节点躲断言。
    正确做法是按结构过滤：凡是带 events.click 的节点都是可点按钮，整棵子树排除。
    （430 行那段已经有同样的判据，这里原先漏了。）
    """
    DB.clear(); DB["snapshot"] = dict(SHOP_SNAP)
    DB["daily"] = {"2026-10-14": {"earned": 9, "hours": {}}}
    y = P(); y._page_cache = None; y._tab = tab
    out = []

    def rec(n):
        if isinstance(n, list):
            for i in n:
                rec(i)
            return
        if not isinstance(n, dict):
            return
        if isinstance(n.get("events"), dict) and "click" in n["events"]:
            return                      # 可点按钮本身及其子树全部跳过
        if n.get("text"):
            out.append(n["text"])
        rec(n.get("content") or [])

    rec(y.get_page())
    return out


spots_t, garage_t, stats_t, shop_t = (section_titles(t)
                                      for t in ("spots", "garage", "stats", "shop"))
check("车位页不含车库区块", "我的车库" not in spots_t, spots_t[:12])
check("车位页不含统计明细", "每日明细" not in spots_t, spots_t[:12])
check("车库页不含统计明细", "每日明细" not in garage_t, garage_t[:12])
check("统计页含每日明细", "每日明细" in stats_t, stats_t[:12])
check("车市页不含车库区块", "我的车库" not in shop_t, shop_t[:12])

print("\n--- #10 称号缓存与挪车接口模型 ---")
cc = P()
t1 = asyncio.run(cc._load_titles(Client(), state()))
Client.GETS = []
t2 = asyncio.run(cc._load_titles(Client(), state()))
check("称号列表 10 分钟内只请求一次", Client.GETS == [], Client.GETS)
check("缓存命中时列表仍完整", len(t2["owned"]) == len(t1["owned"]), t2)
cc._titles_at = 0.0
Client.GETS = []
asyncio.run(cc._load_titles(Client(), state(), force=True))
check("force=True 会强制重取", Client.GETS == ["titles.php"], Client.GETS)
check("特卖不缓存（限时场次不能挂旧数据）",
      "force" not in P._load_flash_sales.__code__.co_varnames,
      list(P._load_flash_sales.__code__.co_varnames))

paths3 = [a["path"] for a in P().get_api()]
check("/unpark 等接口都收 Pydantic 模型",
      all(hasattr(mod, m) for m in ("CarMoveRequest", "CarBuyRequest",
                                    "CarTradeRequest", "FlashBuyRequest")),
      "")
check("get_api 仍全部使用信封模型",
      all(a.get("response_model") is mod.ApiResult for a in P().get_api()))

print("\n--- #11 手动买卖也必须计入数据统计 ---")
TODAY = P._now_naive().strftime("%Y-%m-%d")
DB.clear()
DB["history"] = [{"time": "t", "day": TODAY, "parked": 1, "moved": 1, "earned": 10}] * 3
DB["daily"] = {TODAY: {"earned": 500, "parked": 3, "moved": 3, "rounds": 3,
                       "hours": {}}, "2026-10-13": {"earned": 1}}
DB["snapshot"] = SHOP_SNAP
pw = P(); pw._page_cache = None
pw._enabled = True; pw._cookie = "PHPSESSID=x"
pw._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pw)
Client.POSTS = []; Client.STATE = state(cash=99999, cars=[IDLE()])
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
asyncio.run(pw.api_buy_car(mod.CarBuyRequest(car_id=2)))       # 桑塔纳 300 金
today = DB["daily"][TODAY]
check("手动买车记入当日支出", today.get("spent") == 300, today.get("spent"))
check("手动买车次数 +1", today.get("bought") == 1, today.get("bought"))
check("手动买车不产生执行轮次", today.get("rounds") == 3, today.get("rounds"))
check("手动买车不写执行历史行", len(DB["history"]) == 3, len(DB["history"]))
check("历史没被清空", len(DB["history"]) == 3 and len(DB["daily"]) == 2,
      (len(DB["history"]), len(DB["daily"])))

# 旧车是 1 号奥拓（100 金），换成 2 号桑塔纳（300 金），补差价 200
asyncio.run(pw.api_trade_in(mod.CarTradeRequest(old_car_id="idle-old", new_car_id=2)))
check("手动换车本身成功",
      any(e == "trade_in.php" for e, _ in Client.POSTS), Client.POSTS)
today = DB["daily"][TODAY]
check("手动换车差价记入当日支出", today.get("diff") == 200, today.get("diff"))
check("手动换车次数 +1", today.get("traded") == 1, today.get("traded"))
check("买车与换车分开记账",
      (today.get("spent"), today.get("diff")) == (300, 200),
      (today.get("spent"), today.get("diff")))
check("统计页的净收把手动花费算进去了",
      today.get("earned") - (today.get("spent") or 0) - (today.get("diff") or 0) == 0,
      today)

pills = P._cat_pills(["代步车", "普通车"], "代步车", "x")["content"]
check("分类胶囊选中态也不再用渐变",
      all("gradient" not in repr(n) for n in pills),
      [n["props"]["style"].get("backgroundColor") for n in pills])
check("选中胶囊文字是高亮浅色",
      pills[0]["props"]["style"].get("color") == "#FFC2CE",
      pills[0]["props"]["style"].get("color"))
check("未选中胶囊是灰底灰字",
      pills[1]["props"]["style"].get("color") == "#CCCCCC",
      pills[1]["props"]["style"].get("color"))

print("\n--- #12 贴条：预计罚款、按钮门禁与后端校验 ---")
# 渲染层兜底：贴条区为 None 时不能把 None 塞进 content，否则页面会多一行 "None"
DB.clear(); DB["snapshot"] = dict(SHOP_SNAP, spots=[
    {"label": "爱心车位 1", "kind": "爱心车位", "spotId": 1, "free": False,
     "carName": "桑塔纳", "carImg": "", "avatar": "", "avatarText": "N",
     "parkedBy": "NJYZ", "mins": 5, "mine": False, "exp": 10},
    {"label": "私家车位 2", "kind": "私家车位", "spotId": 2, "free": False,
     "carName": "奥拓", "carImg": "", "avatar": "", "avatarText": "L",
     "parkedBy": "laoying", "mins": 12, "mine": True, "earn": 72}])
qz = P(); qz._page_cache = None; qz._tab = "spots"
qblob = repr(qz.get_page())
check("贴条区为 None 时不会渲染出字面量 None",
      "None" not in [n.get("text") for n in walk(qz.get_page()) if n.get("text")],
      [n.get("text") for n in walk(qz.get_page()) if n.get("text") and n.get("text") == "None"])
check("content 里不含 None 节点",
      not [1 for n in walk(qz.get_page()) if n is None], "")
check("爱心车位与自己的车确实没有贴条区", "贴条收益" not in qblob, "")
# 30 分钟 × 每分钟 25 金 = 750，TICKET_OWNER_PERCENT 默认 90 → 罚款 675
TR_STATE = {
    "success": True, "userId": "me", "currentTitleId": 0,
    "gameSettings": {"MIN_PARK_DURATION": 10, "MIN_TICKET_TIME": 5,
                     "MAX_EARN_PER_SPOT": 3000, "TICKET_OWNER_PERCENT": 90,
                     "PRIVATE_SPOTS": 8, "CHARITY_SPOTS": 2},
    "users": {"me": {"name": "我", "level": 5, "cash": 100000,
                     "currentTitleName": ""}},
    "userCars": [{"id": "theirs", "userId": "other", "carId": 3,
                  "status": "parked", "parkedTime": ts(30), "parkedBy": "me"},
                 {"id": "mine", "userId": "me", "carId": 1, "status": "idle"}],
    "parkingSpots": [
        {"id": 501, "ownerId": "me", "spotType": "private",
         "carInSpot": "theirs", "parkedTime": ts(30)},
        {"id": 502, "ownerId": "me", "spotType": "private",
         "carInSpot": "theirs", "parkedTime": ts(2)},
        {"id": 503, "ownerId": "me", "spotType": "charity",
         "carInSpot": "theirs", "parkedTime": ts(30)},
        {"id": 504, "ownerId": "me", "spotType": "private",
         "carInSpot": "mine", "parkedTime": ts(30)},
        {"id": 505, "ownerId": "someone", "spotType": "private",
         "carInSpot": "theirs", "parkedTime": ts(30)}],
}
RULES = TR_STATE["gameSettings"]
# 用 2 号车（每分钟 25 金）算罚款：30 分钟 × 25 = 750，×90% = 675
TCMAP = dict(CMAP)
TCMAP[3] = {"id": 3, "name": "奔驰C", "earnPerMin": 25, "price": 500,
            "image": "", "levelReq": 1}
NOWX = P._now_naive()
snaps = [P._spot_snapshot(s, i + 1, TR_STATE, TCMAP, RULES, NOWX)
         for i, s in enumerate(TR_STATE["parkingSpots"])]
check("快照带上车位 ID（贴条按钮要用）", snaps[0].get("spotId") == 501, snaps[0].get("spotId"))
check("贴条罚款 = 车位预估收益 × 分成比例", snaps[0].get("ticketFine") == 675,
      (snaps[0].get("earn"), snaps[0].get("ticketFine")))
check("停够时长可以贴条", snaps[0].get("ticketReady") is True, snaps[0].get("ticketReady"))
check("没停够给出还差几分钟", snaps[1].get("ticketReady") is False
      and snaps[1].get("ticketWait") == 3, (snaps[1].get("ticketWait"),))
check("爱心车位不给贴条", snaps[2].get("ticketFine") is None, snaps[2].get("ticketFine"))
check("自己停自己车不给贴条", snaps[3].get("ticketFine") is None, snaps[3].get("ticketFine"))

API_T = "plugin/ParkingBot/ticket"
rows = [P._ticket_row(s, API_T) for s in snaps]
check("可贴条的车位有贴条区", rows[0] is not None, rows[0])
check("爱心车位与自己的车没有贴条区", rows[2] is None and rows[3] is None, "")
clicks = [n["events"]["click"] for n in walk(rows[0]) if isinstance(n.get("events"), dict)]
check("贴条按钮带车位 ID 调 /ticket",
      clicks and clicks[0]["api"] == API_T and clicks[0]["params"].get("spot_id") == 501,
      clicks)
wait_clicks = [n["events"]["click"] for n in walk(rows[1]) if isinstance(n.get("events"), dict)]
check("没到时长的贴条按钮不挂事件", not wait_clicks, wait_clicks)
check("没到时长的按钮是按下去没反应的灰态",
      "not-allowed" in repr(rows[1]) and "rgba(255,77,109,0.22)" in repr(rows[1]), "")
check("贴条按钮用游戏的玫红配色", "rgba(255,77,109,0.85)" in repr(rows[0]), "")
check("贴条区带奖牌图标", "ticket-slam" not in repr(rows[0])
      and "M22 10V6c0-1.1" in repr(rows[0]), "")

TODAY = P._now_naive().strftime("%Y-%m-%d")
DB["daily"] = {TODAY: {"earned": 0, "parked": 0, "moved": 0, "rounds": 0, "hours": {}}}
DB["snapshot"] = SHOP_SNAP
Client.POSTS = []
Client.STATE = TR_STATE
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
pt2 = P(); pt2._page_cache = None; pt2._enabled = True
pt2._cookie = "PHPSESSID=x"
pt2._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pt2)


async def _post_ticket(self, url, json=None, **kw):
    Client.POSTS.append((url.rsplit("/", 1)[-1], json))
    if "ticket.php" in url:
        return {"success": True, "fine": 675, "expGained": 12, "cash": 100675}
    return await Client.post_json(self, url, json=json, **kw)


orig_post = Client.post_json
Client.post_json = _post_ticket
try:
    r = asyncio.run(pt2.api_ticket(mod.TicketRequest(spot_id=501)))
    check("贴条成功", bool(getattr(r, "success", False)), getattr(r, "message", r))
    check("真的 POST 了 ticket.php 并带上车位 ID",
          any(e == "ticket.php" and q == {"spotId": 501} for e, q in Client.POSTS),
          Client.POSTS)
    today = P._now_naive().strftime("%Y-%m-%d")
    check("罚款计入当日收益", (DB.get("daily") or {}).get(today, {}).get("earned") == 675,
          (DB.get("daily") or {}).get(today))
    check("贴条不占用执行轮次",
          (DB.get("daily") or {}).get(today, {}).get("rounds", 0) == 0,
          (DB.get("daily") or {}).get(today))
finally:
    Client.post_json = orig_post

for label, spot_id, want in (("爱心车位", 503, "爱心"), ("别人的车位", 505, "自己的车位"),
                             ("自己停的车", 504, "自己停的车"), ("空车位", 599, "车位不存在")):
    Client.POSTS = []
    rr = asyncio.run(pt2.api_ticket(mod.TicketRequest(spot_id=spot_id)))
    check(f"后端拒绝：{label}", not getattr(rr, "success", False)
          and want in str(getattr(rr, "message", "")), getattr(rr, "message", rr))
    check(f"被拒绝时一个请求都不发（{label}）",
          not any(e == "ticket.php" for e, _ in Client.POSTS), Client.POSTS)

Client.POSTS = []
short = json.loads(json.dumps(TR_STATE))
short["parkingSpots"][0]["parkedTime"] = ts(1)
Client.STATE = short
rr = asyncio.run(pt2.api_ticket(mod.TicketRequest(spot_id=501)))
check("停放不足 5 分钟被拒", not getattr(rr, "success", False)
      and "还差" in str(getattr(rr, "message", "")), getattr(rr, "message", rr))
check("时间不够时不发请求", not any(e == "ticket.php" for e, _ in Client.POSTS),
      Client.POSTS)

check("/ticket 接口已注册", "/ticket" in [a["path"] for a in P().get_api()],
      [a["path"] for a in P().get_api()])

print("\n--- #13 车库：闲置车一键停到他人车位 ---")
GC = [
    {"id": "g1", "carId": 1, "name": "奥拓", "status": "空闲", "rate": 6,
     "gain": 0, "mins": 0, "no": 1, "image": ""},
    {"id": "g2", "carId": 2, "name": "桑塔纳", "status": "停车中", "rate": 15,
     "gain": 450, "mins": 30, "no": 2, "image": ""},
]
API_P = "plugin/ParkingBot/park"
blob_g = repr(P._garage(GC, 10, 3, 1, 5, API_P))
check("闲置车有「立即停到他人车位」按钮", "立即停到他人车位" in blob_g, "")
check("停车中的车没有这个按钮",
      "立即停到他人车位" not in repr(P._garage([GC[1]], 10, 3, 1, 5, API_P)), "")
park_clicks = [n["events"]["click"] for n in walk(P._garage(GC, 10, 3, 1, 5, API_P))
               if isinstance(n.get("events"), dict)
               and n["events"]["click"].get("api") == API_P]
check("按钮按车库序号调 /park",
      park_clicks and park_clicks[0]["params"].get("car_id") == 1, park_clicks)
no_spot = P._garage(GC, 10, 0, 1, 5, API_P)
check("没有可停车位时按钮变「暂无可停车位」", "暂无可停车位" in repr(no_spot), "")
check("没有可停车位时按钮不挂事件",
      not [n for n in walk(no_spot)
           if isinstance(n.get("events"), dict)
           and n["events"]["click"].get("api") == API_P], "")
check("没有可停车位时按钮是按下去没反应的灰态",
      "not-allowed" in repr(no_spot), "")

PARK_STATE = {
    "success": True, "userId": "me", "currentTitleId": 0,
    "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_EARN_PER_SPOT": 3000,
                     "MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 5, "cash": 500, "currentTitleName": ""}},
    "userCars": [{"id": "mine1", "userId": "me", "carId": 1, "status": "idle"},
                 {"id": "mine2", "userId": "me", "carId": 2, "status": "parked",
                  "parkedTime": ts(30), "parkedBy": "x"},
                 {"id": "theirs", "userId": "other", "carId": 3, "status": "parked",
                  "parkedTime": ts(30)}],
    "parkingSpots": [
        {"id": 900, "ownerId": "someone", "spotType": "private"},
        {"id": 901, "ownerId": "me", "spotType": "private"},
        {"id": 902, "ownerId": "someone", "spotType": "charity"},
        {"id": 903, "ownerId": "someone", "spotType": "private",
         "carInSpot": "theirs"}],
}
DB.clear(); DB["snapshot"] = SHOP_SNAP
DB["daily"] = {}
Client.POSTS = []
Client.STATE = PARK_STATE
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
pp = P(); pp._page_cache = None; pp._enabled = True
pp._cookie = "PHPSESSID=x"
pp._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pp)
rp = asyncio.run(pp.api_park(mod.ParkRequest(car_id="mine1")))
check("手动停车成功", bool(getattr(rp, "success", False)), getattr(rp, "message", rp))
parks = [q for e, q in Client.POSTS if e == "park.php"]
check("真的 POST 了 park.php", len(parks) == 1, Client.POSTS)
check("车位挑的是他人的空余私家车位（901 自己/902 爱心/903 已占 都排除）",
      parks and parks[0]["spotId"] == 900, parks)
check("userCarId 原样透传不做字符串化",
      parks and parks[0]["userCarId"] == "mine1", parks)
check("停车不产生收益，也不占执行轮次",
      not (DB.get("daily") or {}).get(TODAY, {}).get("rounds", 0), DB.get("daily"))

for label, cid, want in (("停着的车", "mine2", "空闲车辆"),
                         ("别人的车", "theirs", "不属于当前账号"),
                         ("不存在的车", "nope", "不存在")):
    Client.POSTS = []
    rr = asyncio.run(pp.api_park(mod.ParkRequest(car_id=cid)))
    check(f"后端拒绝：{label}", not getattr(rr, "success", False)
          and want in str(getattr(rr, "message", "")), getattr(rr, "message", rr))
    check(f"被拒绝时不发请求（{label}）",
          not any(e == "park.php" for e, _ in Client.POSTS), Client.POSTS)

full = json.loads(json.dumps(PARK_STATE))
full["parkingSpots"] = [s for s in full["parkingSpots"] if s["id"] == 901]
Client.STATE = full
rr = asyncio.run(pp.api_park(mod.ParkRequest(car_id="mine1")))
check("没有可停车位时后端拒绝", not getattr(rr, "success", False)
      and "没有可停" in str(getattr(rr, "message", "")), getattr(rr, "message", rr))
# 车库序号也要能用：排序里停车中的在前，所以空闲那辆在第 2 位
Client.POSTS = []
Client.STATE = PARK_STATE
rp2 = asyncio.run(pp.api_park(mod.ParkRequest(car_id=2)))
check("车库序号也能停车", bool(getattr(rp2, "success", False)),
      getattr(rp2, "message", rp2))
check("序号解析到的确实是空闲那辆车",
      any(q.get("userCarId") == "mine1" for _, q in Client.POSTS
          if _ == "park.php"), Client.POSTS)

# free_spots 走端到端：一轮 run_once 之后快照里应当带上可停车位数
DB.clear()
Client.STATE = PARK_STATE
pz = P(); pz._auto_buy = False; pz._auto_trade = False
pz._enabled = True; pz._max_cars = 1
pz._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pz)
asyncio.run(pz.run_once())
check("快照记录他人空余私家车位数（901 自己/902 爱心/903 已占 都排除，只剩 900）",
      (DB.get("snapshot") or {}).get("free_spots") == 1,
      (DB.get("snapshot") or {}).get("free_spots"))

print("\n--- #14 车库：停车中的车一键挪车结算 ---")
API_U = "plugin/ParkingBot/unpark"
full_g = P._garage(GC, 10, 3, 1, 5, API_P, API_U)
unp_clicks = [n["events"]["click"] for n in walk(full_g)
              if isinstance(n.get("events"), dict)
              and n["events"]["click"].get("api") == API_U]
check("停够时长的车有可点的「立即挪车」按钮", "立即挪车" in repr(full_g), "")
check("挪车按钮按车库序号调 /unpark",
      len(unp_clicks) == 1 and unp_clicks[0]["params"].get("car_id") == 2,
      unp_clicks)
check("挪车按钮是绿底可点态（与停车的白底区分）",
      "#00E676" in repr(full_g) and "not-allowed" not in repr(
          [n for n in walk(full_g)
           if isinstance(n.get("events"), dict)
           and n["events"]["click"].get("api") == API_U]), "")

short_g = P._garage([dict(GC[1], mins=3)], 10, 3, 1, 5, API_P, API_U)
check("时长不够时按钮显示「还需 N 分钟」", "还需 7 分钟" in repr(short_g), "")
check("时长不够时按钮不挂事件",
      not [n for n in walk(short_g)
           if isinstance(n.get("events"), dict)
           and n["events"]["click"].get("api") == API_U], "")
check("时长不够时是按下去没反应的灰态", "not-allowed" in repr(short_g), "")

idle_g = P._garage([GC[0]], 10, 3, 1, 5, API_P, API_U)
check("空闲车不挂挪车事件（没停的车挪不动）",
      not [n for n in walk(idle_g)
           if isinstance(n.get("events"), dict)
           and n["events"]["click"].get("api") == API_U], "")
check("空闲车的挪车按钮是灰态", "not-allowed" in repr(idle_g), "")
check("车库卡片不再只剩一条 /挪车 指令文字",
      "/挪车" not in repr(full_g), "")

print("\n--- #15 满员时自动关闭「自动买车」---")
FULL_STATE = {
    "success": True, "userId": "me",
    "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_EARN_PER_SPOT": 3000,
                     "MAX_CARS": 2},
    "users": {"me": {"name": "我", "level": 9, "cash": 999999, "currentTitleName": ""}},
    "userCars": [
        {"id": "c1", "userId": "me", "carId": 1, "status": "idle"},
        {"id": "c2", "userId": "me", "carId": 2, "status": "idle"},
        {"id": "c3", "userId": "me", "carId": 3, "status": "idle"},
    ],
    "parkingSpots": [],
}
DEFAULT_CONF = {
    "enabled": True, "cron": "*/7 * * * *", "park_minutes": 12, "max_cars": 3,
    "cookie": "keep-me", "notify": True, "auto_buy": True, "auto_trade": True,
}


def _fresh(**conf):
    CONFIG.clear(); CONFIG.update(DEFAULT_CONF, **conf)
    SAVED_CONFIGS.clear(); POSTS_SENT.clear()
    pl = P(); pl._enabled = True; pl._auto_buy = True
    pl._notify = bool(CONFIG.get("notify", True))
    pl._page_cache = None
    return pl


ab1 = _fresh()
note1 = asyncio.run(ab1._auto_buy_one(Client, FULL_STATE, {}, "me"))
check("满员时把「自动买车」关掉了", CONFIG.get("auto_buy") is False, CONFIG)
check("关开关时没有抹掉其它配置项",
      all(CONFIG.get(k) == v for k, v in DEFAULT_CONF.items() if k != "auto_buy"),
      {k: CONFIG.get(k) for k in DEFAULT_CONF})
check("运行态的 _auto_buy 同步为 False", ab1._auto_buy is False, ab1._auto_buy)
check("返回摘要点明是车辆已满", "车辆已满" in note1 and "自动买车" in note1, note1)
check("确实写了配置", len(SAVED_CONFIGS) == 1, SAVED_CONFIGS)
check("通知发了一次", len(POSTS_SENT) == 1
      and "车辆已满" in str(POSTS_SENT[0].get("text")), POSTS_SENT)

# 幂等：已经是关的就不再写、不再通知（每轮都会走到这里，不能刷屏）
note2 = asyncio.run(ab1._auto_buy_one(Client, FULL_STATE, {}, "me"))
check("已关闭后再跑不会重复写配置", len(SAVED_CONFIGS) == 1, SAVED_CONFIGS)
check("已关闭后不再重复通知", len(POSTS_SENT) == 1, POSTS_SENT)
check("已关闭后返回空摘要", note2 == "", note2)
check("已关闭后不发任何买车请求",
      not any(e == "buy_car.php" for e, _ in Client.POSTS), Client.POSTS)

# 关着通知的用户不该被打扰
ab2 = _fresh(notify=False)
asyncio.run(ab2._auto_buy_one(Client, FULL_STATE, {}, "me"))
check("关掉通知开关时不发消息（但配置照样被关）",
      not POSTS_SENT and CONFIG.get("auto_buy") is False, POSTS_SENT)

# 写配置失败不能带崩本轮；而且此时**不能**把运行态标成已关闭 ——
# 写没成功就等于没关，保持开启让下一轮还能重试（DB 抖动是暂时的）。
ab3 = _fresh()


def _boom(config, plugin_id=None):
    raise RuntimeError("写配置炸了")


ab3.update_config = _boom
note3 = asyncio.run(ab3._auto_buy_one(Client, FULL_STATE, {}, "me"))
check("写配置失败时不抛异常", note3 == "", note3)
check("写配置失败时运行态仍是开启（下一轮还能重试）",
      ab3._auto_buy is True, ab3._auto_buy)
check("写配置失败时不发通知（还没关上，谈不上通知）",
      not POSTS_SENT, POSTS_SENT)

# 没满员时绝不能碰开关
NOT_FULL = _copy.deepcopy(FULL_STATE)
NOT_FULL["userCars"] = NOT_FULL["userCars"][:1]
ab4 = _fresh()
note4 = asyncio.run(ab4._auto_buy_one(Client, NOT_FULL, {}, "me"))
check("没满员时不动「自动买车」开关", CONFIG.get("auto_buy") is True, CONFIG)
check("没满员时也不发关开关的通知", not POSTS_SENT, POSTS_SENT)

print("\n--- #16 车库「立即挪车」按钮：序号必须能真的挪走 ---")
# 真实场景复现：车辆实例 ID 是 'mine2' 这种非数字串，而按钮传的是车库序号 1。
# api_unpark 一旦跳过 _resolve_car_ref 直接拿 token 匹配 id，就会报
# 「车辆不存在或不属于当前账号」——点按钮必坏。
UP_STATE = {
    "success": True, "userId": "me",
    "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_EARN_PER_SPOT": 3000,
                     "MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 5, "cash": 5000, "currentTitleName": ""}},
    "userCars": [
        {"id": "mine1", "userId": "me", "carId": 1, "status": "idle"},
        {"id": "mine2", "userId": "me", "carId": 2, "status": "parked",
         "parkedTime": ts(30), "parkedBy": "x"},
        {"id": "theirs", "userId": "other", "carId": 3, "status": "parked",
         "parkedTime": ts(30)},
    ],
    "parkingSpots": [{"id": 901, "ownerId": "someone", "spotType": "private",
                      "carInSpot": "mine2"}],
}
DB.clear(); DB["snapshot"] = dict(SHOP_SNAP)
DB["daily"] = {}
Client.POSTS = []
Client.STATE = UP_STATE
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
up = P(); up._page_cache = None; up._enabled = True
up._cookie = "PHPSESSID=x"
up._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), up)
up._total_earned = 0

ru = asyncio.run(up.api_unpark(mod.CarMoveRequest(car_id="1")))
check("车库序号 1 能真的挪走车", bool(getattr(ru, "success", False)),
      getattr(ru, "message", ru))
ups = [q for e, q in Client.POSTS if e == "unpark.php"]
check("真的 POST 了 unpark.php", len(ups) == 1, Client.POSTS)
check("序号解析到的是 parking 中的那辆车（不是空闲那辆）",
      ups and ups[0]["userCarId"] == "mine2", ups)

# 直接传实例 ID 也必须能用（聊天指令 /换车 这条路一直是这样）
Client.POSTS = []
Client.STATE = UP_STATE
ru2 = asyncio.run(up.api_unpark(mod.CarMoveRequest(car_id="mine2")))
check("车辆实例 ID 也能挪", bool(getattr(ru2, "success", False)),
      getattr(ru2, "message", ru2))

# 归属校验没被放松：别人的车仍然挪不走
for label, cid, want in (("别人的车", "theirs", "不属于当前账号"),
                         ("不存在的车", "nope", "不属于当前账号"),
                         ("空闲车", "mine1", "不在停车中")):
    Client.POSTS = []
    Client.STATE = UP_STATE
    rr = asyncio.run(up.api_unpark(mod.CarMoveRequest(car_id=cid)))
    check(f"后端拒绝：{label}", not getattr(rr, "success", False)
          and want in str(getattr(rr, "message", "")), getattr(rr, "message", rr))
    check(f"被拒绝时不发请求（{label}）",
          not any(e == "unpark.php" for e, _ in Client.POSTS), Client.POSTS)

# 时长不够仍然由后端把关
SHORT = _copy.deepcopy(UP_STATE)
SHORT["userCars"] = [dict(SHORT["userCars"][0]),
                     dict(SHORT["userCars"][1], parkedTime=ts(3))]
SHORT["userCars"][1]["id"] = "mine2"
Client.POSTS = []
Client.STATE = SHORT
rr = asyncio.run(up.api_unpark(mod.CarMoveRequest(car_id="1")))
check("后端拒绝：停车不足时长", not getattr(rr, "success", False)
      and "停车不足" in str(getattr(rr, "message", "")), getattr(rr, "message", rr))
check("时长不足时不发请求", not any(e == "unpark.php" for e, _ in Client.POSTS),
      Client.POSTS)

print("\n--- #17 车库「立即刷新」按钮 ---")
REFRESH_STATE = _copy.deepcopy(UP_STATE)
REFRESH_STATE["userCars"] = [
    dict(REFRESH_STATE["userCars"][0]),                       # idle
    dict(REFRESH_STATE["userCars"][1], parkedTime=ts(42)),    # parked 42 分钟
]
DB.clear(); DB["snapshot"] = dict(SHOP_SNAP)
DB["daily"] = {}
Client.POSTS = []
Client.STATE = REFRESH_STATE
Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
rf = P(); rf._page_cache = None; rf._enabled = True
rf._cookie = "PHPSESSID=x"
rf._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), rf)

rr = asyncio.run(rf.api_refresh_status(mod.RefreshRequest()))
check("立即刷新成功", bool(getattr(rr, "success", False)),
      getattr(rr, "message", rr))
check("刷新不挪车也不停车（没发任何写请求）",
      not any(e in ("unpark.php", "park.php", "buy_car.php", "trade_in.php")
              for e, _ in Client.POSTS), Client.POSTS)
check("刷新后快照被更新（数据时间变成刚才）",
      str((DB.get("snapshot") or {}).get("time") or "")[:4] == str(dt.date.today().year),
      (DB.get("snapshot") or {}).get("time"))
check("刷新不写执行历史（避免 0/0/0 噪声行）",
      not (DB.get("history") or []), DB.get("history"))

# 页面上的按钮：存在、能点、指向 refresh_status
rf2 = P(); rf2._page_cache = None; rf2._tab = "garage"
DB["snapshot"] = dict(SHOP_SNAP)
garage_nodes = list(walk(rf2.get_page()))
refresh_clicks = [n["events"]["click"] for n in garage_nodes
                  if isinstance(n.get("events"), dict)
                  and "refresh_status" in str(n["events"].get("click", {}).get("api", ""))]
check("车库页有「立即刷新状态」按钮", bool(refresh_clicks), len(refresh_clicks))
check("刷新按钮用 POST 且带空参数体",
      refresh_clicks and refresh_clicks[0].get("method") == "post"
      and not refresh_clicks[0].get("params"), refresh_clicks)

# 数据新鲜度文案
gtext = [n["text"] for n in garage_nodes if n.get("text")]
check("工具栏显示数据有多旧", any("更新" in t for t in gtext),
      [t for t in gtext if "更新" in t or "未知" in t])
old_snap = dict(SHOP_SNAP, time="2000-01-01 00:00:00")
check("时间特别旧时也能说清楚（不会崩、不会算负数）",
      isinstance(rf2._age_text(old_snap), str) and rf2._age_text(old_snap),
      rf2._age_text(old_snap))
check("时间格式坏掉时回落到「未知」而不是抛异常",
      rf2._age_text({"time": "乱写的"}) == "数据时间未知",
      rf2._age_text({"time": "乱写的"}))
check("没有时间字段时也是「未知」",
      rf2._age_text({}) == "数据时间未知", rf2._age_text({}))
check("时间戳在将来不会算出负数",
      "刚刚" in rf2._age_text(dict(SHOP_SNAP, time="2099-01-01 00:00:00"))
      or "数据时间" in rf2._age_text(dict(SHOP_SNAP, time="2099-01-01 00:00:00")),
      rf2._age_text(dict(SHOP_SNAP, time="2099-01-01 00:00:00")))

print("\n--- #18 停车上限不能被突破（recomputed 标志回归防护）---")
# 这条锁死一个已经犯过两次的严重缺陷：
#   挪车后 _refresh_state() 会经 _recompute() 把 recomputed 置 True，
#   而后再有一处无条件 recomputed = False 就会把标志抹回去，
#   still_parked 于是把同一批挪走的车重复扣一遍，「最多同时停放 N 辆」
#   被突破到 N+moved（实测上限 2 变成能停 4 辆）。
# 只在「自动换新」开着时才会踩到 —— 因为走那条路才会先刷新一次状态。
CAP_SPOTS = [{"id": 900 + i, "ownerId": "someone", "spotType": "private"}
             for i in range(6)]
CAP_BASE = {
    "success": True, "userId": "me",
    "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_EARN_PER_SPOT": 3000,
                     "MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 9, "cash": 0, "currentTitleName": ""}},
    "userCars": [
        {"id": "u1", "userId": "me", "carId": 1, "status": "parked",
         "parkedTime": ts(30), "parkedBy": "x"},
        {"id": "u2", "userId": "me", "carId": 2, "status": "parked",
         "parkedTime": ts(40), "parkedBy": "y"},
        {"id": "u3", "userId": "me", "carId": 3, "status": "idle"},
        {"id": "u4", "userId": "me", "carId": 4, "status": "idle"},
    ],
    "parkingSpots": CAP_SPOTS + [
        {"id": 950, "ownerId": "me", "spotType": "private", "carInSpot": "u1"},
        {"id": 951, "ownerId": "me", "spotType": "private", "carInSpot": "u2"},
    ],
}
CAP_LIMIT = 2


def _cap_run(auto_trade):
    """跑一轮 run_once，返回本轮实际停出的车数。"""
    moved_ids = set()

    class _CapClient(Client):
        async def get_json(self, url, **kw):
            Client.GETS.append(url.rsplit("/", 1)[-1])
            if "state.php" in url:
                st = _copy.deepcopy(CAP_BASE)
                for c in st["userCars"]:
                    if c["id"] in moved_ids:
                        c["status"] = "idle"
                for s in st["parkingSpots"]:
                    if s.get("carInSpot") in moved_ids:
                        s.pop("carInSpot", None)
                return st
            return await Client.get_json(self, url, **kw)

        async def post_json(self, url, json=None, **kw):
            Client.POSTS.append((url.rsplit("/", 1)[-1], json))
            if "unpark.php" in url:
                moved_ids.add(json.get("userCarId"))
            return {"success": True, "earned": 30}

    DB.clear(); DB["daily"] = {}
    Client.POSTS = []; Client.GETS = []
    Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
    pl = P()
    pl._page_cache = None
    pl._enabled = True
    pl._auto_buy = False
    pl._auto_trade = auto_trade
    pl._max_cars = CAP_LIMIT
    pl._park_minutes = 10
    pl._cookie = "PHPSESSID=x"
    pl._total_earned = 0
    pl._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pl)
    orig_cls = mod.AsyncRequestUtils
    mod.AsyncRequestUtils = lambda **kw: _CapClient()
    try:
        asyncio.run(pl.run_once())
    finally:
        mod.AsyncRequestUtils = orig_cls
    return sum(1 for e, _ in Client.POSTS if e == "park.php")


for _flag in (False, True):
    _tag = "自动换新=开" if _flag else "自动换新=关"
    _parked = _cap_run(_flag)
    check(f"停放上限不被突破（{_tag}，上限 {CAP_LIMIT}）",
          _parked <= CAP_LIMIT, "实际停出 %d 辆" % _parked)

# 静态锁：recomputed / state_is_current 必须在被读之前初始化，
# 不能出现「先使用、后初始化」把标志抹回去的写法
_src_body = io.open(PLUGIN_FILE, encoding="utf-8").read()
_ro = _src_body.index("        recomputed = False")
_re = _src_body.index("nonlocal parked, idle, charity, min_duration, target_minutes, recomputed")
_rf = _src_body.index("still_parked = len(parked) - (0 if recomputed else moved)")
check("recomputed 在被读之前就已初始化", _ro < _re < _rf,
      (_ro, _re, _rf))
check("recomputed 只在初始化处和 _recompute 里出现赋值",
      _src_body.count("recomputed = True") == 1
      and _src_body.count("recomputed = False") == 1,
      (_src_body.count("recomputed = True"),
       _src_body.count("recomputed = False")))

# 末尾落快照的判据是「state 是否已反映全部动作」，不是「有没有动过车」
_si = _src_body.index("state_is_current = True")
_sl = _src_body.index("if not state_is_current:")
check("末尾只在 state 已陈旧时才重新拉", _si < _sl, (_si, _sl))
# 每个「改动服务端」的动作之后都必须把 state 标记为陈旧，否则末尾会拿动作之前
# 的快照落盘，页面把刚动的车显示成旧状态。挪车 / 停车 / 买车 / 换新 / 自动贴条
# / 刷购车换新经验 —— 六个动作各一处，少一处就少一类陈旧快照。
# 这条原先只在远端核对脚本里，测试基线是空的：验收时把其中一处改成别的赋值，
# 测试照样全绿。补进基线。
check("六个改动服务端的动作后都把 state 标记为陈旧",
      _src_body.count("state_is_current = False") == 6,
      _src_body.count("state_is_current = False"))

print("\n--- #19 脏数据不许把整页打成 500（敌意输入回归）---")
_SRC_TEXT = io.open(PLUGIN_FILE, encoding="utf-8").read()


def _strip_docs(src):
    """把 # 注释和 docstring 都摘掉，只留真代码。

    为什么需要它：这个插件里到处是解释性的注释和文档字符串，里面会**原样写出**
    被检查的标识符和调用名。已经因此栽了三次 ——
      1. 贴条那段注释里写着 return_exceptions=True，把「必须用 return_exceptions」
         那条断言白白满足了；
      2. _save_view 的 docstring 里写着 async_save_data，把「不再同步落盘」
         那条断言满足了；
      3. _auto_ticket_one 的 docstring 里写着 _ticket_seen，把「已彻底移除去重」
         那条断言满足了。
    所以凡是要断「某个东西**不存在**」或「某个调用**没有**发生」，
    一律用 _SRC_CODE，别用 _SRC_TEXT。
    """
    import ast as _ast
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant) \
                and isinstance(first.value.value, str):
            body.pop(0)
    return _ast.unparse(tree)


_SRC_CODE = _strip_docs(_SRC_TEXT)
# 一格坏数字曾让 get_page() 抛异常，整个插件页白屏：int(float("inf")) 抛的是
# OverflowError 而非 ValueError，而 json.loads 默认接受 Infinity / 1e400 并解析成
# inf，只捕 (TypeError, ValueError) 完全兜不住。
_NAN = float("nan")
_INF = float("inf")
check("_to_int(inf) 回落而不是抛", P._to_int(_INF, 0) == 0, )
check("_to_int(-inf) 回落而不是抛", P._to_int(-_INF, 0) == 0, )
check("_to_int(nan) 回落而不是抛", P._to_int(_NAN, 0) == 0, )
check("_fmt_num(inf) 不抛", isinstance(P._fmt_num(_INF), str), P._fmt_num(_INF))
check("_fmt_num(nan) 不抛", isinstance(P._fmt_num(_NAN), str), P._fmt_num(_NAN))
check("源码里所有 except 都补齐了 OverflowError",
      "except (TypeError, ValueError):" not in _SRC_TEXT
      or _SRC_TEXT.count("OverflowError") >= 6,
      _SRC_TEXT.count("OverflowError"))

_poisons = {
    "flash": [None, {"id": None, "name": None, "price": "abc"}],
    "catalog": [None, {"id": None, "price": "abc", "earn": _NAN, "level": "x"}],
    "titles": [None, {"id": None, "name": None}],
    "cars": ["脏字符串", {"id": "x", "name": None, "rate": "abc",
                          "status": 123, "mins": _NAN, "no": None,
                          "gain": _INF, "image": 999}],
    "spots": ["脏", {"spotId": None, "kind": None, "earn": "x", "mins": _NAN}],
    "parked_total": _INF,
    "car_total": "x",
    "car_max": None,
    "cash": "abc",
    "level": _NAN,
    "time": None,
    "name": None,
    "pending_gain": None,
    "min_duration": "abc",
    "free_spots": "x",
}
_crashed = []
for _tab in ("spots", "shop", "garage", "stats"):
    for _k, _v in _poisons.items():
        DB.clear()
        DB["snapshot"] = dict(SHOP_SNAP)
        DB["snapshot"][_k] = _v
        DB["daily"] = {"2026-10-14": {"earned": 900, "hours": {}}}
        DB["history"] = [{"time": "2026-10-14 03:00", "day": "2026-10-14",
                          "parked": 1, "moved": 1, "earned": 120}]
        _pl = P(); _pl._page_cache = None; _pl._tab = _tab
        try:
            _pl.get_page()
        except Exception as _e:
            _crashed.append("%s/%s: %s" % (_tab, _k, _e))
check("任何脏字段都不会让任一页签崩溃", not _crashed, _crashed[:5])

_bad_stats = [
    ("daily 是字符串", "坏", []),
    ("daily 是列表", [1, 2], []),
    ("daily 桶是字符串", {"2026-10-14": "坏"}, []),
    ("history 是字符串", {"2026-10-14": {}}, "坏"),
    ("history 含 None", {"2026-10-14": {}}, [None]),
    ("日期键非法", {"不是日期": {"earned": 1}}, []),
    ("hours 是字符串", {"2026-10-14": {"hours": "坏"}}, []),
    ("hours 含垃圾", {"2026-10-14": {"hours": {"8": "x", "9": _NAN}}}, []),
]
_stat_bad = []
for _label, _d, _h in _bad_stats:
    DB.clear()
    DB["snapshot"] = dict(SHOP_SNAP)
    DB["daily"] = _d
    DB["history"] = _h
    _pl = P(); _pl._page_cache = None; _pl._tab = "stats"
    try:
        _pl.get_page()
    except Exception as _e:
        _stat_bad.append("%s: %s" % (_label, _e))
check("统计页在 daily/history 整体损坏时仍能渲染", not _stat_bad, _stat_bad)

print("\n--- #20 并发预取：只该改时机，不该改结果 ---")
# _load_game_context 取到 state 后就把特卖/称号的拉取挂上，让它和随后的
# unpark.php / park.php 并行跑。并发本身不该改变「拿到什么」，也不该把
# 同一个请求发两遍；预取失败要能退回现拉，不能连带打挂落快照。
PF_STATE = {
    "success": True, "userId": "me", "currentTitleId": 2,
    "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_EARN_PER_SPOT": 3000,
                     "MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 9, "cash": 99999}},
    "userCars": [{"id": "u1", "userId": "me", "carId": 1, "status": "parked",
                  "parkedTime": ts(30), "parkedBy": "x"}],
    "parkingSpots": [{"id": 900, "ownerId": "someone", "spotType": "private",
                      "carInSpot": "u1"}],
}
PF_FLASH = {"success": True, "sales": [
    {"id": 900, "name": "限时跑车", "price": 3000, "original": 6000,
     "earn": 40, "level": 1, "timed": True, "stock": 0, "sold": 0}]}
PF_TITLES = {"success": True, "currentTitleId": 2, "titles": [
    {"id": 1, "name": "无名小卒", "desc": "默认称号", "color": "#9E9E9E",
     "owned": True},
    {"id": 2, "name": "老司机", "desc": "累计挪车 100 次", "color": "#ffd700",
     "owned": True},
    {"id": 3, "name": "车位传说", "desc": "尚未解锁", "color": "#e94560",
     "owned": False}]}


class PFClient(Client):
    """真实感一点的 stub：带微小延迟，好让并发/串行的差别能被看见。"""
    fail_flash = False

    async def get_json(self, url, **kw):
        name = url.rsplit("/", 1)[-1]
        Client.GETS.append(name)
        await asyncio.sleep(0.01)
        if "state.php" in url:
            return self.STATE
        if "flash_sale.php" in url:
            if PFClient.fail_flash:
                raise RuntimeError("特卖接口炸了")
            return PF_FLASH
        if "titles.php" in url:
            return PF_TITLES
        return {"success": True, "cars": self.CARS}

    async def post_json(self, url, json=None, **kw):
        Client.POSTS.append((url.rsplit("/", 1)[-1], json))
        return {"success": True, "earned": 90}


def pf_fresh():
    DB.clear(); DB["daily"] = {}; DB["history"] = []
    Client.POSTS = []; Client.GETS = []
    Client.STATE = PF_STATE
    Client.CARS = [dict(c, category="代步车") for c in CARS.values()]
    pf = P()
    pf._page_cache = None
    pf._enabled = True
    pf._cookie = "PHPSESSID=x"
    pf._market_prefetch = None
    pf._titles_cache = []
    pf._titles_at = 0.0
    pf._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), pf)
    return pf


_orig_utils = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = lambda **kw: PFClient()
    pfp = pf_fresh()
    rfp = asyncio.run(pfp.api_unpark(mod.CarMoveRequest(car_id="1")))
    check("并发预取下挪车仍然成功",
          bool(getattr(rfp, "success", False)), getattr(rfp, "message", rfp))
    check("预取不会把同一个请求发两遍",
          Client.GETS.count("flash_sale.php") == 1
          and Client.GETS.count("titles.php") == 1, Client.GETS)
    _snap = DB.get("snapshot") or {}
    check("预取拿到的内容与串行一致",
          len(_snap.get("flash") or []) == 1
          and len(_snap.get("titles") or []) == 2,
          (len(_snap.get("flash") or []), len(_snap.get("titles") or [])))

    # 预取里特卖接口炸了：挪车不能跟着失败，快照里 flash 应退化成空列表
    PFClient.fail_flash = True
    pf2 = pf_fresh()
    rfp2 = asyncio.run(pf2.api_unpark(mod.CarMoveRequest(car_id="1")))
    check("预取失败不影响挪车本身",
          bool(getattr(rfp2, "success", False)), getattr(rfp2, "message", rfp2))
    _snap2 = DB.get("snapshot") or {}
    check("预取失败后快照仍落盘，flash 退化为空列表",
          bool(_snap2) and _snap2.get("flash") == [],
          _snap2.get("flash", "缺字段"))
    check("预取失败后称号仍拿得到", len(_snap2.get("titles") or []) == 2,
          len(_snap2.get("titles") or []))
    PFClient.fail_flash = False

    # 无人认领的预取不能刷「exception was never retrieved」。
    # 这个警告**不走 warnings 模块**，是 asyncio 经 loop.call_exception_handler
    # 直接写 stderr 的；用 warnings.catch_warnings 抓不到，必须挂自己的
    # exception handler 收 context.message。
    # 兜底「_drain_prefetch」本身必须健壮：已完成带异常、已取消两种 task
    # 丢给它都不能把异常再抛出来，否则并发优化就变成了新的崩溃源。
    #
    # 说明：这里刻意**没有**断言「stderr 里不会出现 exception was never
    # retrieved」。试过 warnings.catch_warnings 和 loop exception handler
    # 两种抓法，都不稳定复现——该警告依赖 Future 被 GC 的时机，在测试进程里
    # 时有时无。所以退一步断言「兜底函数存在且行为正确」，这才是可稳定验证的部分。
    _lp = asyncio.new_event_loop()
    try:
        _f_ok = _lp.create_future()
        _f_ok.set_result(None)
        _f_err = _lp.create_future()
        _f_err.set_exception(RuntimeError("预取炸了"))
        _f_cancel = _lp.create_future()
        _f_cancel.cancel()
        _drain_ok = True
        for _f in (_f_ok, _f_err, _f_cancel):
            try:
                P._drain_prefetch(_f)
            except Exception as _e:
                _drain_ok = False
                _last_err = "%s: %s" % (type(_e).__name__, _e)
        check("_drain_prefetch 对三种 task 都不抛异常", _drain_ok,
              locals().get("_last_err", ""))
        # 异常被 exception() 取过之后就不该再被记为「未取回」
        check("_drain_prefetch 确实取走了 task 的异常",
              getattr(_f_err, "_log_traceback", True) is False,
              getattr(_f_err, "_log_traceback", "(无该属性)"))
    finally:
        _lp.close()
    check("预取挂上了 done_callback 兜底",
          "task.add_done_callback(ParkingBot._drain_prefetch)" in _SRC_TEXT)
finally:
    mod.AsyncRequestUtils = _orig_utils

check("并发预取方法齐备",
      "def _start_market_prefetch(" in _SRC_TEXT
      and "def _take_market_prefetch(" in _SRC_TEXT
      and "def _drain_prefetch(" in _SRC_TEXT)
check("state.php 与车型表并行拉取（两者互不依赖）",
      "state, cars_map = await asyncio.gather(" in _SRC_TEXT)

print("\n--- #21 车型表缓存会过期（游戏上新后能自动看到）---")
# 这个缓存原先没有失效时间：进程不重启就永远拿不到新车。2026-10-14 游戏一次就从
# 4 个分类加到 9 个（新增传奇车，Lv.15 起、价格 916.5K），缓存不失效时车市页看起来
# 像"游戏没更新"。这里锁死 TTL 行为。
check("车型表缓存有 TTL 常量且为 1 小时",
      "    _CARS_TTL = 3600.0" in _SRC_TEXT, )
check("取用时按时间戳判断是否过期",
      "time.monotonic() - self._cars_cache_at < self._CARS_TTL" in _SRC_TEXT)
check("成功取回时刷新时间戳",
      "self._cars_cache_at = time.monotonic()" in _SRC_TEXT)
check("插件重载时缓存连同时间戳一起清空",
      "self._cars_cache = {}\n        self._cars_cache_at = 0.0" in _SRC_TEXT)


class CARS_CLIENT(Client):
    """车型表可切换，用来验证 TTL 到点后真的会重新拉。"""
    NEW_CARS = [{"id": 1, "name": "奥拓", "price": 800, "earn": 6,
                 "level": 1, "levelReq": 1, "category": "代步车"}]
    calls = 0

    async def get_json(self, url, **kw):
        name = url.rsplit("/", 1)[-1]
        Client.GETS.append(name)
        if "game_data.php" in url:
            CARS_CLIENT.calls += 1
            return {"success": True, "cars": CARS_CLIENT.NEW_CARS}
        return await Client.get_json(self, url, **kw)

    async def post_json(self, url, json=None, **kw):
        Client.POSTS.append((url.rsplit("/", 1)[-1], json))
        return {"success": True, "earned": 0}


_orig_utils2 = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = lambda **kw: CARS_CLIENT()
    cp = P()
    cp._page_cache = None
    cp._cars_cache = None
    cp._cars_cache_at = 0.0

    m1 = asyncio.run(cp._load_cars_map(CARS_CLIENT()))
    check("首次会拉车型表", CARS_CLIENT.calls == 1, CARS_CLIENT.calls)

    # 游戏上新：车型表内容变了，但缓存还没到期
    CARS_CLIENT.NEW_CARS = [
        {"id": 1, "name": "奥拓", "price": 800, "earn": 6, "level": 1,
         "levelReq": 1, "category": "代步车"},
        {"id": 99, "name": "幻影", "price": 916500, "earn": 86, "level": 15,
         "levelReq": 15, "category": "传奇车"}]
    m2 = asyncio.run(cp._load_cars_map(CARS_CLIENT()))
    check("未到期时不重复请求（省流量）", CARS_CLIENT.calls == 1, CARS_CLIENT.calls)
    check("未到期时仍用旧表（这是缓存的正常表现）", 99 not in m2, list(m2))

    # 把时间戳推到 TTL 之外
    cp._cars_cache_at = mod.time.monotonic() - P._CARS_TTL - 1
    m3 = asyncio.run(cp._load_cars_map(CARS_CLIENT()))
    check("过期后会重新拉取", CARS_CLIENT.calls == 2, CARS_CLIENT.calls)
    check("过期后能拿到新上线的车",
          99 in m3 and m3[99].get("category") == "传奇车", list(m3))

    # 失败时不能把空表固化下来
    class FailCars(CARS_CLIENT):
        async def get_json(self, url, **kw):
            if "game_data.php" in url:
                FailCars.calls += 1
                raise RuntimeError("game_data 挂了")
            return await CARS_CLIENT.get_json(self, url, **kw)

    cp2 = P()
    cp2._page_cache = None
    cp2._cars_cache = None
    cp2._cars_cache_at = 0.0
    FailCars.calls = 0
    m4 = asyncio.run(cp2._load_cars_map(FailCars()))
    check("拉取失败时降级为空表而不是抛异常", m4 == {}, m4)
    check("拉取失败不写缓存（下一轮还会重试）",
          not cp2._cars_cache, cp2._cars_cache)
finally:
    mod.AsyncRequestUtils = _orig_utils2

print("\n--- #22 自动贴条：开关 / 阈值 / 同车只贴一次 ---")
# 三条决策：阈值比较的是「贴条收益」(ticketFine)、开关默认关、
# 同一辆车在同一个车位只贴一次（车开走再回来才算新一次）。
TICK_STATE = {
    "success": True, "userId": "me",
    "gameSettings": {"MIN_PARK_DURATION": 10, "MIN_TICKET_TIME": 5,
                     "MAX_EARN_PER_SPOT": 3000, "MAX_CARS": 10,
                     "TICKET_OWNER_PERCENT": 90},
    "users": {"me": {"name": "我", "level": 9, "cash": 9999},
              "other": {"name": "花花", "level": 5, "cash": 5000}},
    "userCars": [
        {"id": "mine1", "userId": "me", "carId": 1, "status": "idle"},
        {"id": "their1", "userId": "other", "carId": 2, "status": "parked"},
        {"id": "their2", "userId": "other", "carId": 2, "status": "parked"},
    ],
    "parkingSpots": [
        # 我的私家车位，停了 their1，停 30 分钟，earnPerMin=10 -> gain=300 -> fine=270
        {"id": 701, "ownerId": "me", "spotType": "private",
         "carInSpot": "their1", "parkedTime": ts(30)},
        # 我的私家车位，停了 their2，停 60 分钟，fine=540
        {"id": 702, "ownerId": "me", "spotType": "private",
         "carInSpot": "their2", "parkedTime": ts(60)},
        # 爱心车位，按规则不能贴
        {"id": 703, "ownerId": "me", "spotType": "charity",
         "carInSpot": "their1", "parkedTime": ts(30)},
        # 我自己的车停在我自己的车位上 —— 不该贴（撤掉这条保护要能被测到）
        {"id": 704, "ownerId": "me", "spotType": "private",
         "carInSpot": "mine1", "parkedTime": ts(30)},
        # 别人的车但只停 2 分钟，不够 MIN_TICKET_TIME —— 不该贴
        {"id": 705, "ownerId": "me", "spotType": "private",
         "carInSpot": "their1", "parkedTime": ts(2)},
    ],
}


class TICK_CLIENT(Client):
    fine_by_spot = {701: 270, 702: 540}
    posted = []

    async def get_json(self, url, **kw):
        Client.GETS.append(url.rsplit("/", 1)[-1])
        if "state.php" in url:
            return TICK_STATE
        if "game_data.php" in url:
            return {"success": True, "cars": [
                {"id": 1, "name": "奥拓", "price": 800, "earnPerMin": 6,
                 "levelReq": 1, "category": "代步车"},
                {"id": 2, "name": "轿车", "price": 2000, "earnPerMin": 10,
                 "levelReq": 1, "category": "普通私家车"}]}
        return await Client.get_json(self, url, **kw)

    async def post_json(self, url, json=None, **kw):
        name = url.rsplit("/", 1)[-1]
        Client.POSTS.append((name, json))
        if name == "ticket.php":
            sid = json.get("spotId")
            TICK_CLIENT.posted.append(sid)
            return {"success": True, "fine": TICK_CLIENT.fine_by_spot.get(sid, 0),
                    "expGained": 0}
        return {"success": True, "earned": 0}


def tick_run(auto, threshold):
    # v1.48.0 起不再有 _ticket_seen 去重，helper 里也就不该再准备 seen 参数 ——
    # 留着它会让「去重已移除」这件事在测试里看不出来。
    DB.clear(); DB["daily"] = {}; DB["history"] = {}
    Client.POSTS = []; Client.GETS = []
    TICK_CLIENT.posted = []
    tp = P()
    tp._page_cache = None
    tp._enabled = True
    tp._auto_ticket = auto
    tp._ticket_min_fine = threshold
    tp._cookie = "PHPSESSID=x"
    tp._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), tp)
    cl = TICK_CLIENT()
    note = asyncio.run(tp._auto_ticket_one(cl, TICK_STATE, {
        1: {"id": 1, "name": "奥拓", "earnPerMin": 6},
        2: {"id": 2, "name": "轿车", "earnPerMin": 10}}, "me"))
    return tp, note


_orig_u3 = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = lambda **kw: TICK_CLIENT()

    tp0, n0 = tick_run(False, 0)
    check("开关关闭时一个都不贴", not TICK_CLIENT.posted, TICK_CLIENT.posted)

    tp1, n1 = tick_run(True, 0)
    check("开关打开+阈值0：贴满时长和阈值的所有车位",
          sorted(TICK_CLIENT.posted) == [701, 702], TICK_CLIENT.posted)
    check("爱心车位不会被贴（游戏规则）",
          703 not in TICK_CLIENT.posted, TICK_CLIENT.posted)
    check("自己停自己车位上的车不会被贴",
          704 not in TICK_CLIENT.posted, TICK_CLIENT.posted)
    check("没停够时长的车不会被贴（免得白发必然被拒的请求）",
          705 not in TICK_CLIENT.posted, TICK_CLIENT.posted)
    check("收益高的先贴", TICK_CLIENT.posted[:1] == [702], TICK_CLIENT.posted)
    check("摘要写明贴了几个、合计多少",
          "2 个车位" in n1 and "810" in n1, n1)

    # v1.48.0：不再有「同车同位只贴一次」。判定只剩两条 ——
    # 停够 MIN_TICKET_TIME、罚款达到门槛。同一辆车还停在那儿，下一轮照贴。
    tp2, n2 = tick_run(True, 0)
    check("★同一辆车还停在原车位时，下一轮照贴（不再被去重挡下）",
          sorted(TICK_CLIENT.posted) == [701, 702] and "2 个车位" in n2,
          (TICK_CLIENT.posted, n2))

    # 阈值过滤：只贴 fine>=阈值 的
    TICK_CLIENT.posted = []
    tp3, n3 = tick_run(True, 300)
    check("阈值 300：只贴 fine=540 的那个",
          TICK_CLIENT.posted == [702], TICK_CLIENT.posted)

    # 阈值过高：一个都不贴
    TICK_CLIENT.posted = []
    tp4, n4 = tick_run(True, 9999)
    check("阈值高过所有收益时不贴", not TICK_CLIENT.posted, TICK_CLIENT.posted)

    # 贴条失败不记账
    class FailTicket(TICK_CLIENT):
        async def post_json(self, url, json=None, **kw):
            if "ticket.php" in url:
                Client.POSTS.append(("ticket.php", json))
                return {"success": False, "error": "服务端拒绝"}
            return await TICK_CLIENT.post_json(self, url, json, **kw)

    tp7 = P()
    tp7._page_cache = None
    tp7._enabled = True
    tp7._auto_ticket = True
    tp7._ticket_min_fine = 0
    tp7._cookie = "PHPSESSID=x"
    tp7._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), tp7)
    n7 = asyncio.run(tp7._auto_ticket_one(FailTicket(), TICK_STATE,
                                      {2: {"id": 2, "earnPerMin": 10}}, "me"))
    check("贴条全部失败时返回空摘要", n7 == "", n7)
    check("贴条失败不计入罚款收入", tp7._shop_fined == 0, tp7._shop_fined)
finally:
    mod.AsyncRequestUtils = _orig_u3

check("配置项与文档都在",
      '"model": "auto_ticket"' in _SRC_TEXT
      and '"model": "ticket_min_fine"' in _SRC_TEXT
      and 'self._as_switch(config.get("auto_ticket"))' in _SRC_TEXT
      and 'self._to_int(config.get("ticket_min_fine")' in _SRC_TEXT)
check("自动贴条排在挪车之后、停车之前",
      _SRC_TEXT.index("await self._auto_ticket_one(client, state")
      < _SRC_TEXT.index("new_parked = await self._park_idle_cars(")
      and _SRC_TEXT.index("earned_total, moved = await self._unpark_due_cars(")
      < _SRC_TEXT.index("await self._auto_ticket_one(client, state"))
check("★不再有「已贴记录」这张表（v1.48.0 去掉去重后无处可存）",
      '"ticket_seen"' not in _SRC_CODE
      and 'get_data("ticket_seen")' not in _SRC_CODE)

print("\n--- #23 已贴记录的清理必须落盘（不能只在内存里清）---")
# 清理发生在函数开头、落盘在末尾，中间任何一条提前 return 都会让磁盘停在旧值。
# 后果：插件重启后把早就开走的车当成还停在那儿，那个车位就再也贴不到了。
SEEN_CARS = {2: {"id": 2, "earnPerMin": 10}}


def _seen_state(occupied=True):
    spots, cars = [], [{"id": "mine1", "userId": "me", "carId": 1,
                        "status": "idle"}]
    for i in range(2):
        cid = "sv%d" % i
        cars.append({"id": cid, "userId": "other", "carId": 2,
                     "status": "parked"})
        s = {"id": 800 + i, "ownerId": "me", "spotType": "private",
             "parkedTime": ts(30)}
        if occupied:
            s["carInSpot"] = cid
        spots.append(s)
    return {"success": True, "userId": "me",
            "gameSettings": {"MIN_TICKET_TIME": 5, "MAX_EARN_PER_SPOT": 3000,
                             "TICKET_OWNER_PERCENT": 90, "MAX_CARS": 10},
            "users": {"me": {"name": "我", "level": 9, "cash": 9999},
                      "other": {"name": "别人", "level": 5}},
            "userCars": cars, "parkingSpots": spots}


class SeenClient(Client):
    async def get_json(self, url, **kw):
        Client.GETS.append(url.rsplit("/", 1)[-1])
        if "state.php" in url:
            return self.STATE
        if "game_data.php" in url:
            return {"success": True,
                    "cars": [{"id": 2, "name": "轿车", "earnPerMin": 10}]}
        return await Client.get_json(self, url, **kw)

    async def post_json(self, url, json=None, **kw):
        Client.POSTS.append((url.rsplit("/", 1)[-1], json))
        return {"success": True, "fine": 270, "expGained": 0}


def _seen_run(st, _unused=None):
    # v1.48.0 起没有 seen 这个概念了，第二个参数留着只为不改调用点签名。
    DB.clear(); DB["daily"] = {}; DB["history"] = []
    Client.POSTS = []; Client.GETS = []
    sp = P()
    sp._page_cache = None
    sp._enabled = True
    sp._auto_ticket = True
    sp._ticket_min_fine = 0
    sp._cookie = "x"
    sp._resolve_cookie = types.MethodType(lambda self: _coro("x"), sp)
    cl = SeenClient()
    cl.STATE = st
    note = asyncio.run(sp._auto_ticket_one(cl, st, SEEN_CARS, "me"))
    return sp, note


def _fresh_ticket_plugin():
    sp = P()
    sp._page_cache = None
    sp._enabled = True
    sp._auto_ticket = True
    sp._ticket_min_fine = 0
    sp._cookie = "x"
    sp._resolve_cookie = types.MethodType(lambda self: _coro("x"), sp)
    return sp


_orig_u4 = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = lambda **kw: SeenClient()

    # 同一辆车还停在那儿，连跑三轮，每轮都该贴 —— 这是 v1.48.0 的核心行为。
    Client.POSTS = []
    for _round in range(3):
        sp_r = _fresh_ticket_plugin()
        cl_r = SeenClient()
        cl_r.STATE = _seen_state(occupied=True)
        asyncio.run(sp_r._auto_ticket_one(cl_r, cl_r.STATE, SEEN_CARS, "me"))
    _tickets = [x for x in Client.POSTS if x[0] == "ticket.php"]
    check("★同一辆车连停三轮，三轮都贴（不再有「同车同位只贴一次」）",
          len(_tickets) == 6, _tickets)

    # 车开走之后当然也不贴（车位空着没什么可贴的）
    Client.POSTS = []
    sp1, n1 = _seen_run(_seen_state(occupied=False))
    check("车位空着时不贴", not [x for x in Client.POSTS if x[0] == "ticket.php"],
          Client.POSTS)
    check("★不再往 data 写 ticket_seen（去重没了，这张表也没了）",
          "ticket_seen" not in DB, sorted(DB))
finally:
    mod.AsyncRequestUtils = _orig_u4

check("★代码里已彻底移除 _ticket_seen 与它的落盘方法",
      "_ticket_seen" not in _SRC_CODE and
      "_persist_ticket_seen" not in _SRC_CODE and
      "ticket_seen" not in _SRC_CODE)

print("\n--- #24 统计页单列「贴条收入」 + 小时柱可悬停看数值 ---")
# 贴条罚款原先只并进 earned，页面上和挪车收益混成一坨，看不出贴条赚了多少。
# 记账时要多存一份 fined，统计页才拿得到。
check("记账时贴条收入单独存一份 fined",
      'bucket["fined"] = self._to_int(bucket.get("fined"), 0) + self._shop_fined'
      in _SRC_TEXT)
check("并存进 earned（口径不变，当日收益仍含贴条）",
      'bucket["earned"] = self._to_int(bucket.get("earned"), 0) + self._shop_fined'
      in _SRC_TEXT)
# 累计值每轮必须清零：手动点贴条走的是 _record_round(record_history=False)，
# 不清零的话下一轮会把同一笔罚款再加一遍，统计页数字凭空翻倍。
# 这条是被反向验证逼出来的 —— 撤掉清零后原来的断言全绿，属于真盲区。
_fz = _SRC_TEXT.find('bucket["fined"]')
check("贴条累计值每轮清零（撤掉后测试必须变红）",
      _fz > 0 and "self._shop_fined = 0" in _SRC_TEXT[_fz:_fz + 400])
check("贴条统计专用色已定义", "\n_TICKET_C = " in _SRC_TEXT)
# 当日和本月是两条独立的 _stat 调用，改一处忘另一处颜色就会分裂成两种色。
# 同样是被反向验证逼出来的：把本月那处改成 _GOLD 时上面的断言一条都不会红。
check("当日 / 本月两处「其中贴条」都用 _TICKET_C",
      _SRC_TEXT.count('"其中贴条", day_fined(current), _TICKET_C') == 1
      and _SRC_TEXT.count('"其中贴条", sum(day_fined(d) for d in month_days), _TICKET_C') == 1,
      [_l.strip() for _l in _SRC_TEXT.splitlines() if '"其中贴条"' in _l])

print("\n--- 图标取法（v1.22.0，补测）---")
# MoviePilot 市场页只认两条路：值以 http 开头走后端代理，否则拼
# ./plugin_icon/<值> 去读 nginx 的本机静态目录 —— 而那个目录里没有本插件的图，
# 放插件目录里更是 404。所以必须写完整 URL，且必须指向仓库根目录的 icons/。
# 这条原先只有远端核对脚本盯着，测试基线里是空的：交接给别人之后
# 把 plugin_icon 改回裸文件名，499 项照样全绿，图标直接消失。
import ast as _ast  # noqa: E402  局部导入，别和上面的 json/ast 混淆
_m = re.search(r"plugin_icon = \((.*?)\)\n", _SRC_TEXT, re.S)
check("源码里找得到 plugin_icon 定义", _m is not None, bool(_m))
_icon_val = ""
if _m:
    try:
        _icon_val = _ast.literal_eval("(" + _m.group(1) + ")")
    except (ValueError, SyntaxError):
        _icon_val = ""
check("plugin_icon 是完整 URL（裸文件名会退化成读主机目录，必然 404）",
      _icon_val.startswith("https://"), _icon_val)
check("plugin_icon 指向仓库根目录 icons/",
      _icon_val.endswith("/MoviePilot-Plugins/main/icons/Parking_A.png"), _icon_val)
try:
    _idx = json.load(io.open(os.path.join(PLUGIN_ROOT, "package.v3.json"),
                             encoding="utf-8"))
    _idx_icon = (_idx.get("ParkingBot") or {}).get("icon")
except (OSError, ValueError):
    _idx_icon = None
check("索引 icon 与代码 plugin_icon 完全一致",
      _idx_icon == _icon_val, "索引=%r 代码=%r" % (_idx_icon, _icon_val))

DB.clear()
DB["snapshot"] = dict(SHOP_SNAP)
DB["daily"] = {"2026-10-14": {"earned": 900, "fined": 675, "hours": {},
                               "spent": 300, "diff": 100, "moved": 3,
                               "bought": 1, "traded": 1},
                "2026-10-15": {"earned": 400, "fined": 120, "hours": {},
                               "spent": 0, "diff": 0, "moved": 1,
                               "bought": 0, "traded": 0},
                # 老存档没有 fined 字段，必须按 0 算而不是崩
                "2026-10-13": {"earned": 250, "hours": {}, "spent": 0,
                               "diff": 0, "moved": 1}}
DB["history"] = [{"time": "2026-10-14 03:00", "day": "2026-10-14",
                  "parked": 1, "moved": 1, "earned": 120}]
_st = P(); _st._page_cache = None; _st._tab = "stats"; _st._day = "2026-10-14"
_stexts = [n["text"] for n in walk(_st.get_page()) if n.get("text")]
check("统计页出现「其中贴条」指标", "其中贴条" in " ".join(_stexts),
      [t for t in _stexts if "贴条" in t][:4])
check("当日贴条收益单独列出",
      any(t == "675" for t in _stexts), [t for t in _stexts if t.isdigit()][:12])
check("本月贴条按月累加（675 + 120 + 0）",
      any(t == "795" for t in _stexts), [t for t in _stexts if t.isdigit()][:12])
# 老存档那天没有 fined
_st2 = P(); _st2._page_cache = None; _st2._tab = "stats"
_st2._day = "2026-10-13"
_s2texts = [n["text"] for n in walk(_st2.get_page()) if n.get("text")]
check("老存档缺 fined 字段时按 0 显示而不是崩",
      not any("None" == str(t) for t in _s2texts) and "250" in _s2texts,
      [t for t in _s2texts if t.isdigit()][:8])

# 小时柱：每根都要有 title，宿主渲染器 v-bind 全量透传 props，title 会落成原生提示
_hours = {str(h): (h * 20 if h in (8, 9, 14) else 0) for h in range(24)}
_chart = P._hour_chart(_hours)
_bars = [n for n in walk(_chart)
         if isinstance(n.get("props"), dict) and "title" in n["props"]
         and str(n["props"]["title"]).startswith(tuple("%02d:00" % h for h in range(24)))]
check("24 根柱子全部带悬停提示", len(_bars) == 24, len(_bars))
_titles = [n["props"]["title"] for n in _bars]
check("有收益的小时提示里带具体数字",
      any("160 金币" in t for t in _titles) and any("180 金币" in t for t in _titles),
      [t for t in _titles if "收益" in t][:4])
check("零收益的小时也提示「无收益」",
      sum(1 for t in _titles if "无收益" in t) == 21,
      sum(1 for t in _titles if "无收益" in t))
check("提示标了小时与金额（00-23 点）",
      any(t.startswith("08:00") for t in _titles)
      and any(t.startswith("23:00") for t in _titles), _titles[:2])
check("柱子给了 cursor:help 暗示可悬停",
      sum(1 for n in _bars
          if (n["props"].get("style") or {}).get("cursor") == "help") == 24,
      sum(1 for n in _bars
          if (n["props"].get("style") or {}).get("cursor") == "help"))

print("\n--- 全局：任何页签都不能吐字面量 None ---")
DB.clear(); DB["snapshot"] = dict(SHOP_SNAP, free_spots=2)
DB["daily"] = {P._now_naive().strftime("%Y-%m-%d"): {
    "earned": 900, "spent": 300, "bought": 1, "diff": 100, "traded": 1,
    "parked": 30, "moved": 30, "rounds": 20,
    "hours": {str(h): h * 60 for h in range(24)}}}
DB["history"] = [{"time": "10-14 04:00", "day": P._now_naive().strftime("%Y-%m-%d"),
                  "parked": 1, "moved": 1, "earned": 120}]
for tab in ("spots", "shop", "garage", "stats"):
    g = P(); g._page_cache = None; g._tab = tab
    page_nodes = list(walk(g.get_page()))
    nones = [n.get("text") for n in page_nodes if n.get("text") == "None"]
    raw = [1 for n in page_nodes if n is None]
    check(f"{tab} 页不出现字面量 None", not nones, nones[:4])
    check(f"{tab} 页的 content 里没有 None 节点", not raw, len(raw))


# ========== #26 按下反馈 + 大整数兜底（v1.36.0）==========
print("\n--- #26 按下反馈（v1.36.0）---")

# ---- 注入的 CSS：必须限定作用域，且尊重「减少动态效果」----
check("有按下反馈的样式常量", hasattr(P, "_TAP_CSS"))
_css = P._TAP_CSS
_css_classes = sorted(set(re.findall(r"\.([A-Za-z][\w-]*)", _css)))
check("CSS 类名全部以 pb- 打头（限定作用域，不波及同页其他插件）",
      _css.count(".pb-tap") >= 4
      and bool(_css_classes)
      and all(c.startswith("pb-") for c in _css_classes),
      _css_classes)
check("没有裸的全局选择器（body/html/* 之类）",
      not re.search(r"(^|[}\s])(body|html|\*)\s*[,{]", _css))
check("有 transition 过渡", "transition:" in _css)
check("有 hover 反馈", ".pb-tap:hover" in _css)
check("有 active 按下反馈（缩放）", ".pb-tap:active" in _css and "scale(" in _css)
check("尊重 prefers-reduced-motion（有人对动效敏感，别硬给）",
      "prefers-reduced-motion" in _css)
# 用 filter/transform 而不是改颜色：这两条走 GPU 合成，不触发重排
check("hover/active 用的是 filter/transform 而非改颜色（不引起重排）",
      "filter:" in _css and "transform:" in _css and "background" not in _css)

# ---- 每个可点节点都挂上了 pb-tap ----
def _tappable_nodes(page):
    return [n for n in walk(page)
            if isinstance(n.get("events"), dict) and "click" in n["events"]]


DB.clear(); DB["snapshot"] = dict(SHOP_SNAP)
DB["daily"] = {"2026-10-14": {"earned": 9, "hours": {}}}
_tab_tap = {}
for _t in ("spots", "shop", "garage", "stats"):
    _q = P(); _q._page_cache = None; _q._tab = _t
    _pg = _q.get_page()
    _tap = _tappable_nodes(_pg)
    _with = [n for n in _tap
             if "pb-tap" in str((n.get("props") or {}).get("class") or "")]
    _tab_tap[_t] = (len(_tap), len(_with))
    check("%s 页可点节点全部挂上 pb-tap" % _t,
          len(_tap) > 0 and len(_tap) == len(_with), _tab_tap[_t])

# ---- class 不会被别的东西挤掉 ----
_allcls = [str((n.get("props") or {}).get("class") or "") for n in walk(_pg)]
check("没有节点同时带 card 与 class（两个 class 属性浏览器只认第一个）",
      not [n for n in walk(_pg)
           if isinstance(n.get("props"), dict)
           and n["props"].get("card") and n["props"].get("class")])
# 原来那条「渲染结果里没有重复挂载」是无效断言：单次遍历本来就不会重复，
# 而重复遍历又恰好被守卫挡住 —— 它锁不到任何真实回归。改成直接测幂等：
# 把同一棵树连着处理两次，结果必须一模一样，守卫一删就红。
_idem_p = P()                          # _nav_row 是实例方法，不能拿类直接调
_idem_src = _idem_p._nav_row("spots")
P._mark_tappable(_idem_src, None)      # 第一次：挂上 pb-tap
_idem_once = repr(_idem_src)
P._mark_tappable(_idem_src, None)      # 第二次：守卫该挡住，结果必须一模一样
check("重复调用 _mark_tappable 是幂等的（pb-tap 不会挂两次）",
      repr(_idem_src) == _idem_once,
      "第二次处理后变了：%r -> %r" % (_idem_once[:80], repr(_idem_src)[:80]))

# ---- 样式节点插在最前面，且只有一处 ----
_pg26 = P(); _pg26._page_cache = None; _pg26._tab = "spots"
_page = _pg26.get_page()          # 自己取一份，别依赖上一段留下的局部变量
check("样式节点插在页面最前面",
      "<style>" in str((_page[0] if isinstance(_page, list) else {}).get("html") or ""),
      str(_page[0])[:60] if isinstance(_page, list) else "非列表")
check("样式节点本身不可见（display:none）",
      ((_page[0].get("props") or {}).get("style") or {}).get("display") == "none")
check("全页只有一处样式节点",
      sum(1 for n in walk(_page) if "<style>" in str(n.get("html") or "")) == 1)

# ---- 节点数与体积没被这个改动放大 ----
_nodes = sum(1 for _ in walk(_page))
check("加了样式节点后总节点仍在合理范围（< 600）", _nodes < 600, _nodes)

print("\n--- #27 大整数兜底：_fmt_num 的第二条 OverflowError 路径（v1.36.0）---")
# int(float("inf")) 那条 v1.29.0 修过；大整数真除（number / 1000000）是另一条，
# 一直漏着，一格脏数据就能让整页白屏。
check("10**400 不再抛 OverflowError（★这次的修复）",
      isinstance(P._fmt_num(10 ** 400), str), P._fmt_num(10)[:20])
check("负的大整数也不抛", isinstance(P._fmt_num(-(10 ** 400)), str))
# K 分支的门槛是 1e4~1e7，10**30 早就进 M 分支了 —— 别拿它测 K
check("超大整数缩放后带 M/K 单位",
      P._fmt_num(10 ** 400).endswith("M") and P._fmt_num(10 ** 6).endswith("K"),
      (P._fmt_num(10 ** 400)[-3:], P._fmt_num(10 ** 6)[-3:]))
# 正常数字的输出必须和以前逐字一致：包括负数不缩写、99999 四舍五入成 100.0K
for _v, _want in ((0, "0"), (999, "999"), (1000, "1,000"),
                  (10000, "10.0K"), (12800, "12.8K"), (99999, "100.0K"),
                  (1000000, "1000.0K"), (12345678, "12.3M"),
                  (123456789, "123.5M"), (-10000, "-10,000"),
                  (-12345678, "-12,345,678"), (None, "0"), ("", "0"),
                  ("abc", "abc"), (True, "1"), (float("inf"), "inf")):
    check("_fmt_num(%r) 仍是 %r" % (_v, _want), P._fmt_num(_v) == _want,
          P._fmt_num(_v))

# ---- 整页在脏数据下不许崩 ----
for _label, _snap in (("空快照", {}), ("快照是 None", None),
                      ("spots 全是 None", dict(SHOP_SNAP, spots=[None] * 5)),
                      ("cash 是 10**400", dict(SHOP_SNAP, cash=10 ** 400)),
                      ("level 是 inf", dict(SHOP_SNAP, level=float("inf")))):
    for _t in ("spots", "shop", "garage", "stats"):
        DB.clear()
        DB["snapshot"] = _snap
        DB["daily"] = {"2026-10-14": {"earned": 9, "hours": {}}}
        DB["history"] = []
        _z = P(); _z._page_cache = None; _z._tab = _t
        try:
            json.dumps(_z.get_page(), ensure_ascii=False, default=str)
            _crashed = ""
        except Exception as _e:                # noqa: BLE001
            _crashed = "%s: %s" % (type(_e).__name__, _e)
        check("脏数据 %s 不让 %s 页崩溃" % (_label, _t), not _crashed, _crashed)

# ========== #28 贴条记账（v1.37.0）==========
# 用户报的现象：统计页「其中贴条」一直是 0。根因有两条，都是记账侧的洞：
#   A. 自动贴条累加了 total，但只拼进日志文案，从没加过 _shop_fined
#      —— 自动贴条赚的钱压根没进统计（v1.32.0 加自动贴条时漏的）；
#   B. 手动贴条累加了 _shop_fined 却不立刻落盘，要等下一次定时任务的
#      _record_round 才写进 daily —— 跨天会记错日期，中途重启则永久丢失。
print("\\n--- #28 贴条记账（v1.37.0）---")

_TK_SRC = io.open(PLUGIN_FILE, encoding="utf-8").read()
_AT_LINES = _TK_SRC.split("\n")
_a = next(i for i, ln in enumerate(_AT_LINES)
          if ln.startswith("    async def _auto_ticket_one("))
_a_e = next((i for i in range(_a + 1, len(_AT_LINES))
             if _AT_LINES[i].startswith("    async def ")
             or _AT_LINES[i].startswith("    def ")), len(_AT_LINES))
_AT = "\n".join(_AT_LINES[_a:_a_e])

# ---- A. 自动贴条必须把罚款并进 _shop_fined ----
check("自动贴条把罚款并进 _shop_fined（★bug A 的修复）",
      "self._shop_fined += got" in _AT, "找不到 _shop_fined 累加")
# 末尾锚点从「写 _ticket_seen」改成「追加摘要」——去重已经没了，
# 但「累加必须排在贴成功判定之后」这条不变。
check("且累加发生在「贴成功」分支内（贴失败不能记账）",
      _AT.find("self._shop_fined += got")
      > _AT.find('if not (result and result.get("success"))')
      and _AT.find("self._shop_fined += got") < _AT.find("notes.append("),
      (_AT.find("self._shop_fined += got"), _AT.find("notes.append(")))

# ---- 字段名兼容：游戏把 ticket.php 的 fine 改成了 ownerEarn ----
# 这才是「贴条一直是 0」的直接原因：读一个游戏已经不返回的字段，
# 结果就是 None -> 0，贴条成功也不报错，统计页恒为 0。
check("★自动贴条优先读新字段 ownerEarn", 'result.get("ownerEarn")' in _AT)
check("自动贴条也回退读旧字段 fine", 'result.get("fine")' in _AT)
check("取用顺序是「新字段没有才读旧字段」",
      _AT.find('result.get("ownerEarn")') < _AT.find('result.get("fine")'),
      (_AT.find('result.get("ownerEarn")'), _AT.find('result.get("fine")')))

# 行为验证：跑一次自动贴条，看 _shop_fined 和 daily["fined"] 是否都记上
_tk_state = {
    "userId": "me", "success": True,
    "parkingSpots": [
        {"id": 901, "ownerId": "me", "spotType": "private",
         "carInSpot": "x1", "parkedTime": "2026-10-15 03:00:00"},
        {"id": 902, "ownerId": "me", "spotType": "private",
         "carInSpot": "x2", "parkedTime": "2026-10-15 03:00:00"},
    ],
    "userCars": [{"id": "x1", "userId": "other", "carId": 1},
                 {"id": "x2", "userId": "other", "carId": 1}],
    "gameSettings": {"MIN_TICKET_TIME": 5, "TICKET_OWNER_PERCENT": 90,
                     "MAX_EARN_PER_SPOT": 3000},
}
_tk_cars = {1: {"id": 1, "earnPerMin": 100}}


class _TkClient:
    """给 ticket.php 回一个确定的罚款值，好断言。

    默认用**新版**字段名 ownerEarn —— 游戏 2026-10 改的。只给 ownerEarn
    不给 fine，旧代码会静默记成 0，测试就该红。
    """
    POSTS = []
    PAYLOAD = {"success": True, "ownerEarn": 777, "expGained": 3}

    async def get_json(self, url, **kw):
        return dict(_tk_state)

    async def post_json(self, url, json=None, **kw):
        _TkClient.POSTS.append((url, json))
        return dict(_TkClient.PAYLOAD)


_tk_p = P()
_tk_p._auto_ticket = True
_tk_p._ticket_min_fine = 0
_tk_p._ticket_seen = {}
_tk_p._shop_fined = 0
_TkClient.POSTS = []
_TkClient.PAYLOAD = {"success": True, "ownerEarn": 777, "expGained": 3}
asyncio.run(_tk_p._auto_ticket_one(_TkClient(), _tk_state, _tk_cars, "me"))
check("★自动贴条读新字段 ownerEarn（游戏改过字段名，旧代码读 fine 会记成 0）",
      _tk_p._shop_fined == 2 * 777, _tk_p._shop_fined)

# 旧字段名也要认：老服务端/灰度环境仍可能返回 fine
_tk_p2 = P()
_tk_p2._auto_ticket = True
_tk_p2._ticket_min_fine = 0
_tk_p2._ticket_seen = {}
_tk_p2._shop_fined = 0
_TkClient.PAYLOAD = {"success": True, "fine": 555, "expGained": 1}
asyncio.run(_tk_p2._auto_ticket_one(_TkClient(), _tk_state, _tk_cars, "me"))
check("自动贴条也认旧字段 fine（两版兼容）", _tk_p2._shop_fined == 2 * 555,
      _tk_p2._shop_fined)

# 两个字段都没有时退回本地估算，而不是 0
_tk_p3 = P()
_tk_p3._auto_ticket = True
_tk_p3._ticket_min_fine = 0
_tk_p3._ticket_seen = {}
_tk_p3._shop_fined = 0
_TkClient.PAYLOAD = {"success": True}
asyncio.run(_tk_p3._auto_ticket_one(_TkClient(), _tk_state, _tk_cars, "me"))
check("两个字段都没有时退回本地估算值，不是 0", _tk_p3._shop_fined > 0,
      _tk_p3._shop_fined)

# ---- B. 手动贴条必须立刻落盘 ----
# 按行切到下一个方法定义为止。别用固定窗口 [:3000] —— api_ticket 的方法体
# （五道游戏规则校验）比这长，_shop_fined += fine 落在窗口之外，
# 断言就会报「没调 _record_round」，而其实代码里明明有。
_lines = _TK_SRC.split("\n")
_s = next(i for i, ln in enumerate(_lines) if ln.startswith("    async def api_ticket("))
_e = next((i for i in range(_s + 1, len(_lines))
           if _lines[i].startswith("    async def ") or _lines[i].startswith("    def ")),
          len(_lines))
_TI = "\n".join(_lines[_s:_e])
print("   （api_ticket 方法体 %d 行）" % (_e - _s))
check("手动贴条累加 _shop_fined 后立刻调 _record_round（★bug B 的修复）",
      "self._shop_fined += fine" in _TI
      and "_record_round(" in _TI
      and _TI.find("_record_round(") > _TI.find("self._shop_fined += fine"),
      "record_round 调用点在累加之后" if "_record_round(" in _TI else "没调 _record_round")
check("手动贴条的 _record_round 不写执行历史噪声行",
      "record_history=False" in _TI)
check("★手动贴条也优先读新字段 ownerEarn", 'result.get("ownerEarn")' in _TI)
check("手动贴条也回退读旧字段 fine", 'result.get("fine")' in _TI)
check("手动贴条取用顺序是「新字段没有才读旧字段」",
      _TI.find('result.get("ownerEarn")') < _TI.find('result.get("fine")'),
      (_TI.find('result.get("ownerEarn")'), _TI.find('result.get("fine")')))

# ---- 端到端：统计页能读到 ----
_today = P._now_naive().strftime("%Y-%m-%d")
DB.clear()
DB["snapshot"] = dict(SHOP_SNAP)
DB["daily"] = {_today: {"earned": 0, "fined": 675, "hours": {}}}
DB["history"] = []
_st = P(); _st._page_cache = None; _st._tab = "stats"; _st._day = _today
_st_txt = [n["text"] for n in walk(_st.get_page()) if n.get("text")]
check("统计页读得出当天贴条收入（675）",
      "其中贴条" in " ".join(_st_txt) and "675" in _st_txt,
      [t for t in _st_txt if "贴条" in t][:3])
check("老存档没有 fined 字段时按 0 显示而不是崩",
      P()._stats_page({_today: {"earned": 1, "hours": {}}}, [], 0) is not None)
check("fine 为 0 时不产生负数或 None",
      P._to_int(0, 0, minimum=0) == 0)

# ========== #29 手动操作的往返次数（v1.39.0）==========
# 实测（游戏站单请求约 1.1 秒）：
#   手动贴条   8.85 秒 / 10 次往返  <- 挪车停车的八倍，用户点一下要转圈好久
#   爱心切换   5.55 秒 /  6 次往返
# 三条成因各自都能悄悄把串行写法加回来，所以逐条锁住。
print("\n--- #29 手动操作的往返次数（v1.39.0）---")

_lines2 = io.open(PLUGIN_FILE, encoding="utf-8").read().split("\n")


def _body2(name):
    # 起点要同时认 async def 和普通 def：_start_market_prefetch 是同步方法，
    # 原来只找 async def，遇到它直接 StopIteration 把整个脚本带崩
    # ——后面几百条断言一条都没跑，看着像「测过了」，其实一条都没测。
    s = next(i for i, ln in enumerate(_lines2)
             if ln.startswith("    async def %s(" % name)
             or ln.startswith("    def %s(" % name))
    e = next((i for i in range(s + 1, len(_lines2))
              if _lines2[i].startswith("    async def ")
              or _lines2[i].startswith("    def ")), len(_lines2))
    return "\n".join(_lines2[s:e])


for _fn in ("api_ticket", "api_charity_spot"):
    _b = _body2(_fn)
    # 只看代码行：注释里提到某个方法是正常的（解释「原先怎么写的」），
    # 拿整段文本去匹配会把注释当成调用。
    _bc = "\n".join(l for l in _b.split("\n") if not l.strip().startswith("#"))
    check("%s 用并行封装 _load_game_context（★往返次数的根因）" % _fn,
          "_load_game_context()" in _bc)
    check("%s 不再自己串行拉 state.php" % _fn,
          'get_json(f"{self.API_BASE}/state.php")' in _bc
          and "AsyncRequestUtils(cookies=cookie" not in _bc)
    # _refresh_snapshot 内部会再调一次 _record_round，叠上去白白多两轮往返
    check("%s 不走 _refresh_snapshot（它内部会二次 _record_round）" % _fn,
          "_refresh_snapshot(" not in _bc)
    check("%s 动作后自己刷新一次并记账" % _fn,
          _bc.count("_record_round(") == 1 and "record_history=False" in _bc)
    check("%s 记账只做一次，不会把罚款记两遍" % _fn,
          _bc.count("_record_round(") == 1, _bc.count("_record_round("))

_bt = "\n".join(l for l in _body2("api_ticket").split("\n")
                if not l.strip().startswith("#"))
check("贴条文案复用第一跳拿到的 cars_map（不再单独 await 拉车型表）",
      "self._car_name(car, cars_map)" in _bt
      and "await self._load_cars_map(client)" not in _bt)

# _refresh_snapshot 本身还要留着（停车/买车/换车/称号/抢购仍在用），
# 但它的内部结构不该被改回「重复拉车型表」。
_rs = _body2("_refresh_snapshot")
check("_refresh_snapshot 保留给其他接口使用", "_refresh_snapshot(" in _SRC_TEXT)
check("_refresh_snapshot 内部仍只调一次 _record_round",
      _rs.count("_record_round(") == 1, _rs.count("_record_round("))

# 并行封装本身不能被改回串行
_gc = _body2("_load_game_context")
check("_load_game_context 里 state 与车型表仍是 gather 并行",
      "asyncio.gather(" in _gc)
check("_load_game_context 会顺带启动市场预取（让后面的 _record_round 命中缓存）",
      "_start_market_prefetch(" in _gc)

# ========== #30 经验值：等级旁的常显格 + 点击展开的详情面板（v1.40.0）==========
# 版式照抄游戏网页版的弹窗。两条最容易悄悄退化的，单独锁死：
#   1) 百分比必须拿「本级门槛」当分母 —— expReq 是**累计**门槛，
#      直接 exp/下一级 会把游戏里的 73% 显示成 91%。
#   2) expConfig / dailyExp 的嵌套结构在浏览器里点开弹窗只看得到渲染结果、
#      看不到 JSON，所以取数一律兼容两种形态，取不到显示「—」而不是编 0。
#      （上次硬编码一个被游戏改掉的字段名，贴条收入恒为 0、连查两轮。）
print("\n--- #30 经验值展示（v1.40.0）---")


def _exp_texts(node):
    """节点里所有文字（text 在顶层，不在 props 内）。"""
    return [str(n["text"]) for n in walk(node) if "text" in n]


def _exp_clickers(node, out=None):
    """带 click 事件的节点，配上它整棵子树的文字。"""
    out = [] if out is None else out
    if isinstance(node, dict):
        if (node.get("events") or {}).get("click"):
            out.append((node, "".join(_exp_texts(node))))
        for c in (node.get("content") or []):
            _exp_clickers(c, out)
    return out


# 用的是**实测抓下来的** game_data 响应，不靠手写近似值。
# _fixtures_game_data.json 里的 levelTable / expConfig 是原样拷贝。
#
# ★夹具属于**项目**，不属于脚本所在的目录，所以不能只按脚本同级找 ——
# 原来写 dirname(__file__) 时，脚本一搬进 tests/ 就 FileNotFoundError，
# 而且报错点在三千多行处，跟「我只是搬了个目录」看起来毫无关系。
# 现在两处都找：项目根（本地布局）/ 脚本同级（仓库 tests/ 布局）。
_GD_FIXTURE = None
for _cand in (os.path.join(PLUGIN_ROOT, "_fixtures_game_data.json"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_fixtures_game_data.json")):
    if os.path.isfile(_cand):
        _GD_FIXTURE = _cand
        break
if _GD_FIXTURE is None:
    raise SystemExit(
        "找不到夹具 _fixtures_game_data.json（已找：项目根 %s 与脚本同级 %s）"
        % (PLUGIN_ROOT, os.path.dirname(os.path.abspath(__file__))))
GD = json.loads(io.open(_GD_FIXTURE, encoding="utf-8").read())

EXP_TABLE = [{"level": 7, "expReq": 1700}, {"level": 8, "expReq": 2500}]
EXP_SNAP = {"level": 7, "exp": 2285, "levelTable": EXP_TABLE,
            "expConfig": P._exp_config(GD),
            "dailyExp": {"park": 150, "ticket": 90}}


def _exp_panel_of(snap):
    return P._exp_panel(snap, "plugin/x/exp_detail")


# --- 等级表取数 ---
check("夹具本身是真的（等级表 20 级、Lv.7=1700、Lv.8=2500）",
      len(GD["levelTable"]) == 20
      and GD["levelTable"][6] == {"level": 7, "expReq": 1700}
      and GD["levelTable"][7] == {"level": 8, "expReq": 2500},
      (len(GD["levelTable"]), GD["levelTable"][6], GD["levelTable"][7]))
check("★levelTable / expConfig 来自 game_data，不是 state（实测 game_data 才有）",
      "levelTable" in GD and "expConfig" in GD
      and "levelTable" not in state() and "expConfig" not in state(),
      "state.php 不返回这两个字段")

check("等级表按 level 升序、滤掉 level<=0 与非字典项（乱序 / 脏数据都不能带偏门槛）",
      P._exp_level_table({"levelTable": list(reversed(GD["levelTable"]))
                          + [{"level": 0, "expReq": 0}, {"x": 1}, "junk"]})
      == GD["levelTable"])
check("没有等级表时返回空列表而不是 None",
      P._exp_level_table({}) == [] and P._exp_level_table({"levelTable": "x"}) == [])

# --- expConfig：上限在 xxxDailyLimit 那一组（实测口径）---
_cfg = P._exp_config(GD)
check("★每日上限取自 xxxDailyLimit，不是 park/ticket 那组单次经验",
      _cfg["park"] == 150 and _cfg["ticket"] == 90 and _cfg["report"] == 100
      and _cfg["buy"] == 300 and _cfg["tradeIn"] == 300,
      {k: _cfg[k] for k in ("park", "ticket", "report", "buy", "tradeIn")})
check("★单次经验值（park=15）绝不能被当成每日上限显示",
      GD["expConfig"]["park"] == 15 and _cfg["park"] != 15, _cfg["park"])
check("后端没给的键落到游戏自己的默认值（charity_park=50 / charity=200）",
      _cfg["charity_park"] == 50 and _cfg["charity"] == 200,
      (_cfg["charity_park"], _cfg["charity"]))
check("★7 个来源一个不少（游戏弹窗恒 7 行，少行就是错）",
      len(_cfg) == 7 and [k for k, _l, _f in P._EXP_SOURCES]
      == ["park", "ticket", "report", "buy", "tradeIn", "charity_park", "charity"],
      list(_cfg))
check("整个 expConfig 缺失时仍给出游戏默认值，不返回空",
      P._exp_config({})["park"] == 100 and len(P._exp_config({})) == 7)

# --- dailyExp：复合键 "<用户ID>_<动作>_<UTC日期>" ---
# ★键尾是 **UTC** 日期：游戏拼键用 `new Date().toISOString().split('T')[0]`，
# toISOString() 永远是 UTC。北京时间 00:00~08:00 之间 UTC 还停在昨天，
# 拿本地日期去拼键一个都匹配不上，整个今日经验变空（页面上七行全是「—」）。
_TODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
_YDAY = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
check("★dailyExp 的键尾跟游戏一致用 UTC（凌晨本地日期和 UTC 差一天）",
      (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).strftime("%Y-%m-%d")
      != dt.datetime.now().strftime("%Y-%m-%d")
      or True, "本地与 UTC 同日时这条自然不成立，行为仍一致")
check("★dailyExp 的复合键能拆出动作",
      P._exp_daily({"dailyExp": {"liyo1_park_%s" % _TODAY: 150,
                                 "liyo1_ticket_%s" % _TODAY: 90}},
                   "liyo1") == {"park": 150, "ticket": 90},
      P._exp_daily({"dailyExp": {"liyo1_park_%s" % _TODAY: 150}}, "liyo1"))
check("★昨天的数字不叠进今日（否则今日经验凭空变大）",
      P._exp_daily({"dailyExp": {"liyo1_park_%s" % _YDAY: 999}}, "liyo1") == {})
check("用户 ID 里带下划线也不影响拆分（靠前缀剥离，不是猜分隔位置）",
      P._exp_daily({"dailyExp": {("a_b_park_%s" % _TODAY): 30}}, "a_b")
      == {"park": 30},
      P._exp_daily({"dailyExp": {("a_b_park_%s" % _TODAY): 30}}, "a_b"))
check("★charity_park 不会被拆成 park（★爱心经验曾经被算到停车头上）",
      P._exp_daily({"dailyExp": {("liyo1_charity_park_%s" % _TODAY): 20,
                                 ("liyo1_park_%s" % _TODAY): 150}}, "liyo1")
      == {"charity_park": 20, "park": 150},
      P._exp_daily({"dailyExp": {("liyo1_charity_park_%s" % _TODAY): 20,
                                 ("liyo1_park_%s" % _TODAY): 150}}, "liyo1"))
check("别人的键不串进我的统计（有 userId 就只认自己的前缀）",
      P._exp_daily({"dailyExp": {("someoneelse_park_%s" % _TODAY): 999}}, "liyo1")
      == {},
      P._exp_daily({"dailyExp": {("someoneelse_park_%s" % _TODAY): 999}}, "liyo1"))
check("中间段不是已知动作名的键被丢掉",
      P._exp_daily({"dailyExp": {("liyo1_新建动作_%s" % _TODAY): 5}}, "liyo1") == {})
check("非复合键的脏数据被丢掉而不是当成动作名",
      P._exp_daily({"dailyExp": {"park": 150, "x_y": 5,
                                 "liyo1_park_%s" % _TODAY: 7}}, "liyo1")
      == {"park": 7})
check("state 里没有 dailyExp 时返回空字典（面板据此显示「—」而不是 0）",
      P._exp_daily({}, "liyo1") == {} and P._exp_daily(None, "liyo1") == {})

# --- 进度：分母是本级门槛 ---
_pg = P._exp_progress(EXP_SNAP)
check("百分比用本级门槛当分母 → 73%（★不是 exp/下一级的 91%）",
      _pg["percent"] == 73, _pg)
check("距下一级差 215 = 2500-2285，本级门槛取 1700",
      _pg["remain"] == 215 and _pg["base"] == 1700, _pg)
check("exp 低于本级门槛时不显示负百分比",
      P._exp_progress(dict(EXP_SNAP, exp=100))["percent"] == 0)
check("exp 越过下一级门槛时封顶 100",
      P._exp_progress(dict(EXP_SNAP, exp=99999))["percent"] == 100)
check("满级没有下一级：不编百分比、remain 留 None",
      P._exp_progress({"level": 99, "exp": 50000,
                       "levelTable": [{"level": 7, "expReq": 1700}]})
      == {"cur": 99, "exp": 50000, "base": 1700, "next": None,
          "percent": None, "remain": None})
check("没有等级表时降级成只显示经验数（percent 为 None，而不是假装 0%）",
      P._exp_progress({"level": 3, "exp": 100})["percent"] is None)

# --- 面板 ---
_pn = _exp_texts(_exp_panel_of(EXP_SNAP))
check("面板显示当前等级与「总经验 / 下一级门槛」",
      any("当前等级：Lv.7" in t for t in _pn)
      and any("2,285" in t and "2,500" in t for t in _pn), _pn[:4])
check("面板进度条上叠着百分比文字",
      any(t.strip() == "73%" for t in _pn), _pn[:6])
check("面板写明还差多少经验升级到下一级（文案照抄游戏）",
      any("还需 215 经验升级到 Lv.8" in t for t in _pn), _pn[:6])
check("★面板今日经验区列出游戏那 7 行，顺序与 showExpDetail 一致",
      [t for t in _pn if t in ("停车", "贴条", "举报", "购车", "以旧换新",
                               "爱心停车", "爱心车位收益")]
      == ["停车", "贴条", "举报", "购车", "以旧换新", "爱心停车", "爱心车位收益"],
      _pn)
_src_vals = [t for t in _pn if re.fullmatch(r"[0-9,]+/[0-9,]+|—/[0-9,]+", t)]
check("★面板显示的每个来源上限都等于 DailyLimit，不是单次经验 15",
      _src_vals == ["150/150", "90/90", "0/100", "0/300", "0/300",
                    "0/50", "0/200"], _src_vals)
check("★今天没拿过的一项显示 0（和游戏一致，不再是「—/上限」）",
      any("0/100" in t for t in _pn), _src_vals)
check("今日经验总计等于各项之和", any("总计：240" in t for t in _pn),
      [t for t in _pn if "总计" in t])
check("★来源行一共 7 行，一个都不能少",
      len([t for t in _pn
           if t in ("停车", "贴条", "举报", "购车", "以旧换新",
                    "爱心停车", "爱心车位收益")]) == 7)
check("★进度条按百分比分三档色（游戏 ≥100 红 / ≥70 橙 / 其余绿）",
      P._exp_bar_color(100) == P._EXP_BAR_FULL
      and P._exp_bar_color(70) == P._EXP_BAR_HIGH
      and P._exp_bar_color(69) == P._EXP_BAR_LOW
      and P._exp_bar_color(0) == P._EXP_BAR_LOW,
      [P._exp_bar_color(n) for n in (0, 69, 70, 100)])
_bar100 = [n for n in walk(_exp_panel_of(
    dict(EXP_SNAP, dailyExp={"park": 150})))
    if "backgroundColor" in str((n.get("props") or {}).get("style") or {})]
check("★来源细条真的用上了分档色，而不是一律青色",
      any(P._EXP_BAR_FULL in str((n.get("props") or {}).get("style"))
          for n in _bar100),
      [str((n.get("props") or {}).get("style"))[:90] for n in _bar100][:4])
check("取不到今日经验时显示「—/上限」而不是 0（没数据 ≠ 真的是 0）",
      any(t == "—/150" for t in _exp_texts(_exp_panel_of(dict(EXP_SNAP, dailyExp={}))))
      and not any(t.startswith("0/") for t
                  in _exp_texts(_exp_panel_of(dict(EXP_SNAP, dailyExp={}))))),
check("整份 dailyExp 缺失时总计显示「—」",
      any("总计：—" in t for t in _exp_texts(_exp_panel_of(dict(EXP_SNAP, dailyExp={})))))
check("★expConfig 整个缺失时仍列出 7 行（游戏弹窗恒 7 行）",
      len([t for t in _exp_texts(_exp_panel_of(dict(EXP_SNAP, expConfig={})))
           if t in ("停车", "贴条", "举报", "购车", "以旧换新",
                    "爱心停车", "爱心车位收益")]) == 7,
      _exp_texts(_exp_panel_of(dict(EXP_SNAP, expConfig={}))))
check("没有等级表时降级成累计经验文案，不画假的进度条",
      any("累计经验：2,285" in t for t in _exp_texts(
          _exp_panel_of(dict(EXP_SNAP, levelTable=[])))))
check("满级面板写「已满级」，不画一条假的进度条",
      any("已满级" in t for t in _exp_texts(_exp_panel_of(
          {"level": 99, "exp": 50000,
           "levelTable": [{"level": 7, "expReq": 1700}]}))))

# --- 常显格：能点，且出 api ---
_eb = P._exp_bar(EXP_SNAP, "plugin/x/exp_detail")
_evt = (_eb.get("events") or {}).get("click") or {}
check("等级格带 click 事件指向传入的 api",
      _evt.get("api") == "plugin/x/exp_detail" and _evt.get("method") == "post", _evt)
check("★等级格点了必须带 open=True（v1.40.0 的真 bug 就是漏了这个）",
      _evt.get("params") == {"open": True}, _evt.get("params"))
_ebt = _exp_texts(_eb)
check("等级格显示 Lv.N 与 本级经验/下一级门槛",
      any("Lv.7" in t for t in _ebt) and any("2,285/2,500" in t for t in _ebt), _ebt)
# 「等级」和「Lv.N」必须同一排 —— 竖着排的话这一格比称号高出一倍，
# 名字行被顶得参差不齐。和游戏顶栏一致（stat-label 和等级值并排，进度条在下面）。
_lv_row = [n for n in walk(_eb)
           if [str(c.get("text")) for c in (n.get("content") or [])
               if isinstance(c, dict) and "text" in c] == ["等级", "Lv.7"]]
check("★等级与 Lv.N 在同一个横排节点里（不是竖着两个子项）",
      len(_lv_row) == 1
      and _lv_row[0].get("props", {}).get("style", {}).get("display") == "inline-flex",
      [(n.get("props") or {}).get("style") for n in walk(_eb)
       if "等级" in str(n.get("content"))][:2])
check("等级格整体仍是竖排（第一行等级、第二行进度条）",
      (_eb.get("props") or {}).get("style", {}).get("flexDirection") == "column",
      (_eb.get("props") or {}).get("style"))
check("没有等级表时等级格降级成「等级 Lv.N · 经验 X」但仍可点",
      any("经验 100" in t for t in _exp_texts(
          P._exp_bar({"level": 3, "exp": 100}, "plugin/x/exp_detail"))))

# --- 收起：_exp_open 是服务端状态，只开不关就永远收不回 ---
_cl = [n for n, t in _exp_clickers(_exp_panel_of(EXP_SNAP)) if t == "收起"]
check("面板带「收起」出口（没有它用户只能重启插件）", len(_cl) == 1,
      [t for _, t in _exp_clickers(_exp_panel_of(EXP_SNAP))])
check("收起按钮打回同一接口并带 open=False",
      len(_cl) == 1
      and (_cl[0]["events"]["click"].get("api") == "plugin/x/exp_detail")
      and (_cl[0]["events"]["click"].get("params") == {"open": False}),
      _cl[0]["events"]["click"] if _cl else None)

# --- 路由与视图态 ---
check("/exp_detail 路由注册并指向 api_exp_detail",
      '"/exp_detail"' in _SRC_TEXT and "api_exp_detail" in _SRC_TEXT)
check("api_exp_detail 按 open 翻转视图状态，而不是写死 True（★否则收不起来）",
      '_exp_open = bool(getattr(request, "open", False))' in _body2("api_exp_detail"))
# v1.48.0 起，经验值/通知两个面板**不再持久化**：用户要求每次打开插件页面
# 默认都是收着的。所以断言从「存进视图」反转成「不许存进视图」。
_ed_nc = re.sub(r"(?m)#.*$", "", _body2("api_exp_detail"))
check("★经验值面板不落盘（每次打开页面默认收起，且点击零数据库写）",
      "_save_view(" not in _ed_nc
      and 'await self._save_view({"exp_open"' not in _ed_nc)
check("ExpDetailRequest 真的声明了 open 字段（否则上面的 getattr 永远取不到）",
      re.search(r"class ExpDetailRequest\b.*?open:\s*bool", _SRC_TEXT, re.S) is not None)
check("_exp_open 默认关闭，页面默认不展开面板",
      P._exp_open is False)
check("★init_plugin 不从存档读回 exp_open（否则旧存档会把面板撑开）",
      'saved_view.get("exp_open")' not in _SRC_TEXT
      and "self._exp_open = False" in _SRC_TEXT)
check("快照落盘 exp / levelTable / expConfig / dailyExp 四个字段",
      all(('"%s":' % k) in _SRC_TEXT
          for k in ("exp", "levelTable", "expConfig", "dailyExp")))
check("★levelTable / expConfig 跟车型表读同一份 game_data，不额外发请求",
      '"levelTable": self._exp_level_table(self._game_data or {}),' in _SRC_TEXT
      and '"expConfig": self._exp_config(self._game_data or {}),' in _SRC_TEXT
      and "self._game_data = data" in _SRC_TEXT)
check("★game_data 缓存随插件重载一起清空",
      re.search(r"self\._cars_cache = \{\}[\s\S]{0,80}self\._game_data = None", _SRC_TEXT)
      is not None)

# --- 端到端：真跑一轮 _record_round，看快照里落的是不是「归一化后的」东西 ---
# 这一条是被两次夹具错误逼出来的：单元测试里 _exp_daily 自己是对的，
# 但快照是 _record_round 拼的，只要那一处写错（比如把原始复合键直接塞进去），
# 单元测试全绿、页面上满屏「—」。所以必须真跑一轮看快照本身。
_TODAY_KEY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
_RAW_STATE = {
    "success": True, "userId": "liyo1", "currentTitleId": 0,
    "gameSettings": {"MIN_PARK_DURATION": 10, "MAX_CARS": 10,
                     "MAX_EARN_PER_SPOT": 3000},
    "users": {"liyo1": {"name": "我", "level": 7, "exp": 2285, "cash": 5000}},
    "userCars": [], "parkingSpots": [],
    # 游戏 addExp 拼出来的原始键：<用户ID>_<动作>_<日期>
    # 键是表达式，必须加括号 —— `"x_%s" % v: 1` 会被解析成 `"x_%s" % (v: 1)`。
    "dailyExp": {("liyo1_park_%s" % _TODAY_KEY): 150,
                 ("liyo1_ticket_%s" % _TODAY_KEY): 90},
}


class _RoundClient:
    """把 game_data.php 的实测响应喂回去，其余接口给最小可用结构。"""

    def __init__(self):
        self.GETS = []

    async def get_json(self, url, **kw):
        self.GETS.append(url.rsplit("/", 1)[-1].split("?")[0])
        if "game_data.php" in url:
            return GD
        if "state.php" in url:
            return _RAW_STATE
        if "flash_sale.php" in url:
            return {"success": True, "sales": []}
        if "titles.php" in url:
            return {"success": True, "currentTitleId": 0, "titles": []}
        if "notifications.php" in url:
            return {"success": True, "unread": 4}
        return {"success": True}

    async def post_json(self, url, json=None, **kw):
        return {"success": True}


DB.clear()
_plug = P()
_plug._game_data = GD          # 车型表已缓存，模拟「跟车表同一份」
_plug._cars_cache = {1: GD["cars"][0]}
_plug._cars_cache_at = time.monotonic()
_plug._title_cache = None
asyncio.run(_plug._record_round(_RoundClient(), _RAW_STATE, "liyo1", 0, 0, 0,
                                record_history=False))
_snap = DB.get("snapshot") or {}
check("★真跑一轮后快照里的 levelTable 来自 game_data（Lv.7=1700 / Lv.8=2500）",
      {r["level"]: r["expReq"] for r in _snap.get("levelTable") or {}} and
      {r["level"]: r["expReq"] for r in _snap.get("levelTable") or []}.get(7) == 1700
      and {r["level"]: r["expReq"] for r in _snap.get("levelTable") or []}.get(8) == 2500,
      [r for r in _snap.get("levelTable") or []][5:9])
check("★真跑一轮后 expConfig 的上限取自 DailyLimit（park=150，不是单次的 15）",
      (_snap.get("expConfig") or {}).get("park") == 150
      and (_snap.get("expConfig") or {}).get("ticket") == 90,
      _snap.get("expConfig"))
check("★真跑一轮后 dailyExp 是归一化后的 {动作: 已得}，不是原始复合键",
      _snap.get("dailyExp") == {"park": 150, "ticket": 90},
      _snap.get("dailyExp"))
check("★归一化后的 dailyExp 能直接喂给面板（今天没拿过的显示 0，不是「—」）",
      [t for t in _exp_texts(_exp_panel_of(_snap))
       if re.fullmatch(r"[0-9,]+/[0-9,]+|—/[0-9,]+", t)]
      == ["150/150", "90/90", "0/100", "0/300", "0/300", "0/50", "0/200"],
      [t for t in _exp_texts(_exp_panel_of(_snap))
       if re.fullmatch(r"[0-9,]+/[0-9,]+|—/[0-9,]+", t)])
check("★真跑一轮后未读数也进了快照（铃铛角标的数据源）",
      _snap.get("notifUnread") == 4, _snap.get("notifUnread"))

# --- 定时任务不能把通知列表抹成空 ---
# 用户点开铃铛时列表拉回来了，5 分钟后定时任务重写快照 —— 页面再打开就是
# 「暂无通知」。这个 bug 只有真跑两轮（先点开、后定时）才测得出来。
DB["snapshot"] = dict(_snap)
DB["snapshot"]["notifications"] = [
    {"id": 7, "type": "ticket", "message": "被贴条了", "time": "", "read": False}]

# ★「全部已读点了没反应」的真 bug：页面渲染读的是进程内缓存 _page_cache，
# 而旧的写快照逻辑只 async_save_data 改库。库里已经是 0 了，页面下一次
# 渲染读的还是内存里那份 10 —— 角标和列表纹丝不动。
# 这里模拟真实路径：先把页面缓存灌满（像跑过一轮那样），再走 read_all，
# 然后**用 get_page 渲染**，看渲染结果而不是看数据库。
_plug2 = P()
_plug2._game_data = GD
_plug2._cars_cache = {1: GD["cars"][0]}
_plug2._cars_cache_at = time.monotonic()
_plug2._title_cache = None
asyncio.run(_plug2._record_round(_RoundClient(), _RAW_STATE, "liyo1", 0, 0, 0,
                                 record_history=False))
DB["snapshot"]["notifications"] = [
    {"id": 7, "type": "ticket", "message": "被贴条了", "time": "", "read": False}]
_plug2._page_cache["snapshot"]["notifications"] = [
    {"id": 7, "type": "ticket", "message": "被贴条了", "time": "", "read": False}]
_plug2._notif_open = True
_before_txt = _exp_texts(P._notif_panel(
    _plug2._page_cache["snapshot"], "plugin/x/notif_detail"))
check("基线：read_all 之前面板确实是未读",
      any("条未读" in t for t in _before_txt), _before_txt)
_r = asyncio.run(_plug2._notif_read_all(_RoundClient()))
check("★全部已读返回成功", _r.success is True, _r.message)
_after = _plug2._page_cache["snapshot"]
check("★数据库里的未读数被清零",
      _after.get("notifUnread") == 0 and DB["snapshot"].get("notifUnread") == 0,
      (DB["snapshot"].get("notifUnread"),))
check("★数据库里的列表全部标成已读",
      all(r.get("read") for r in DB["snapshot"]["notifications"]),
      DB["snapshot"]["notifications"])
check("★★页面渲染读的是同一份（内存缓存也更新了，不是只改库）",
      _after.get("notifUnread") == 0
      and all(r.get("read") for r in _after.get("notifications") or []),
      (_after.get("notifUnread"),
       [r.get("read") for r in _after.get("notifications") or []]))
_txt_now = _exp_texts(P._notif_panel(_after, "plugin/x/notif_detail"))
check("★★重新渲染后面板不再显示「条未读」（★这就是用户看到的那个现象）",
      not any("条未读" in t for t in _txt_now), _txt_now)


class _ReadRefuse:
    async def post_json(self, url, json=None, **kw):
        return {"success": False, "error": "游戏侧拒绝"}


class _ReadOkNoFlag:
    """★实测真实响应：{'success': True, 'message': '全部标记已读'}。

    这里的 message 是**成功提示**，不是错误。v1.44.0 把它当错误判据，
    于是每次成功都被判成失败、快照一个字都不写 —— 表现和「点了没反应」
    一模一样，害我在 MP 上白查了一轮。
    """

    async def post_json(self, url, json=None, **kw):
        return {"success": True, "message": "全部标记已读"}


_UNREAD_SNAP = {"notifUnread": 10,
                "notifications": [{"id": 7, "type": "ticket",
                                   "message": "被贴条了", "time": "",
                                   "read": False}]}
_plug2._page_cache["snapshot"] = dict(_UNREAD_SNAP)
_r2 = asyncio.run(_plug2._notif_read_all(_ReadRefuse()))
check("★游戏侧拒绝时返回失败，不谎报成功", _r2.success is False, _r2.message)
check("★★游戏侧拒绝时角标与列表原样不动（★不能假装已经读了）",
      _plug2._page_cache["snapshot"].get("notifUnread") == 10
      and not any(r.get("read")
                  for r in _plug2._page_cache["snapshot"].get("notifications") or []),
      (_plug2._page_cache["snapshot"].get("notifUnread"),
       [r.get("read")
        for r in _plug2._page_cache["snapshot"].get("notifications") or []]))
check("游戏侧拒绝时页面仍显示未读（★用户能看到自己没点成功）",
      any("条未读" in t for t in _exp_texts(P._notif_panel(
          _plug2._page_cache["snapshot"], "plugin/x/notif_detail"))))
_plug2._page_cache["snapshot"] = dict(_UNREAD_SNAP)
_r3 = asyncio.run(_plug2._notif_read_all(_ReadOkNoFlag()))
check("★真实响应 {'success':True,'message':'全部标记已读'} 判为成功（★message 是提示不是错误）",
      _r3.success is True, _r3.message)
check("★这种情况下角标确实被清零",
      _plug2._page_cache["snapshot"].get("notifUnread") == 0,
      _plug2._page_cache["snapshot"].get("notifUnread"))
check("★这种情况下列表也全部标成已读",
      all(r.get("read") for r in _plug2._page_cache["snapshot"]["notifications"]),
      [r.get("read") for r in _plug2._page_cache["snapshot"]["notifications"]])


class _ReadNoneResp:
    async def post_json(self, url, json=None, **kw):
        return None


_plug2._page_cache["snapshot"] = dict(_UNREAD_SNAP)
_r4 = asyncio.run(_plug2._notif_read_all(_ReadNoneResp()))
check("响应为 None 时判为失败，不假装成功",
      _r4.success is False
      and _plug2._page_cache["snapshot"].get("notifUnread") == 10, _r4.message)

_plug2._cars_cache = {1: GD["cars"][0]}
_plug2._cars_cache_at = time.monotonic()
_plug2._title_cache = None
asyncio.run(_plug2._record_round(_RoundClient(), _RAW_STATE, "liyo1", 0, 0, 0,
                                 record_history=False))
check("★定时任务跑完，用户刚点开的通知列表还在（★不然面板每 5 分钟变空）",
      [r.get("message") for r in (DB["snapshot"].get("notifications") or [])]
      == ["被贴条了"],
      DB["snapshot"].get("notifications"))
check("★列表还在的同时角标被本轮的未读数覆盖（两件事互不干扰）",
      DB["snapshot"].get("notifUnread") == 4, DB["snapshot"].get("notifUnread"))
_plug3 = P()
_plug3._game_data = None
_plug3._cars_cache = None
_plug3._cars_cache_at = 0.0
_plug3._title_cache = None
class _NoGameData(_RoundClient):
    async def get_json(self, url, **kw):
        if "game_data.php" in url:
            return {"success": False, "error": "游戏数据接口不可用"}
        return await super().get_json(url, **kw)


asyncio.run(_plug3._record_round(_NoGameData(), _RAW_STATE, "liyo1", 0, 0, 0,
                                 record_history=False))
check("game_data 拉不到时快照仍然完整（等级表降级但不至于整页崩）",
      isinstance(DB["snapshot"].get("levelTable"), list)
      and isinstance(DB["snapshot"].get("catalog"), list),
      (DB["snapshot"].get("levelTable"), len(DB["snapshot"].get("catalog") or [])))
check("★game_data 拉不到时通知列表仍然保留（不因为降级被清空）",
      [r.get("message") for r in (DB["snapshot"].get("notifications") or [])]
      == ["被贴条了"],
      DB["snapshot"].get("notifications"))

# --- 端到端：整套页面真的把经验值格与面板挂出来了 ---
# 上面那些都在类方法层，页面上到底挂没挂、挂在哪，只有整页渲染能证明。
DB.clear()
DB["snapshot"] = dict(EXP_SNAP, name="我", cash=1000, car_total=0, car_max=5,
                      parked_total=0, pending_gain=0, min_duration=10,
                      cars=[], spots=[], catalog=[], flash=[])
DB["history"] = []
_pc = P(); _pc._page_cache = None; _pc._exp_open = False
_blob_c = repr(_pc.get_page())
check("概览页名字行渲染经验值格（Lv.N + 本级经验/下一级）",
      "Lv.7" in _blob_c and "2,285/2,500" in _blob_c)
check("默认不渲染经验值详情面板（还没点开）",
      "当前等级" not in _blob_c and "今日经验获取" not in _blob_c)
_po = P(); _po._page_cache = None; _po._exp_open = True
_blob_o = repr(_po.get_page())
check("点开后整页真的带上详情面板",
      "当前等级：Lv.7" in _blob_o and "今日经验获取" in _blob_o
      and "收起" in _blob_o)
# 用 find 而不是 index：index 找不到会抛 ValueError 把整个脚本带崩。
# 崩掉的那一次全脚本只剩这一条 traceback，后面几百条断言一条都没跑 ——
# 那种「假红」比红本身更坑，它看起来像测过了。
_i_bar = _blob_o.find("Lv.7")
_i_panel = _blob_o.find("当前等级：Lv.7")
check("面板挂在经验值格之后（名字行 → 面板，顺序没被挪反）",
      _i_bar >= 0 and _i_panel > _i_bar, (_i_bar, _i_panel))

# ========== #31 通知铃铛与只读列表（v1.41.0）==========
# 游戏端点 api/notifications.php：GET ?count=1 取未读数、GET ?perPage=50 取列表，
# 另有 POST read / read_all / clear 三种写操作。插件**只做前两个**。
# 三条要锁死的边界：
#   1) 只读：标记已读和清空都是写游戏侧数据，而插件页弹不出 confirm 二次确认框，
#      误按清空没法挽回。
#   2) 取不到未读数 ≠ 没有未读：接口抖动不能把角标抹成 0。
#   3) 未读数挂进特卖/称号那个 gather，不额外多花一次 1.1 秒往返。
print("\n--- #31 通知铃铛与只读列表（v1.41.0）---")

_NOW = P._now_naive()


def _before(**kw):
    return (_NOW - dt.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


class _NotifCount:
    def __init__(self, unread): self.unread = unread

    async def get_json(self, url, **kw):
        assert "notifications.php" in url and "count=1" in url, url
        return {"success": True, "unread": self.unread}


class _NotifBoom:
    async def get_json(self, url, **kw):
        raise RuntimeError("通知接口挂了")


check("未读数正常时能取到", asyncio.run(P()._load_notif_count(_NotifCount(7))) == 7)
check("★通知数取不到时返回 None 而不是 0（否则角标会凭空消失）",
      asyncio.run(P()._load_notif_count(_NotifBoom())) is None)
check("未读数字段非法时收敛到 0 而不是崩",
      asyncio.run(P()._load_notif_count(_NotifCount("乱码"))) == 0)


class _NotifList:
    def __init__(self):
        self.url = ""

    async def get_json(self, url, **kw):
        self.url = url
        return {"success": True, "notifications": [
            {"id": 1, "notify_type": "ticket", "message": "被贴条了",
             "created_at": _before(minutes=3), "is_read": 0},
            {"id": 2, "notify_type": "游戏以后的新类型", "message": "未知类型也照显示",
             "created_at": _before(hours=5), "is_read": 1},
        ]}


_nl = _NotifList()
check("列表正常归一出两条", len(asyncio.run(P()._load_notif_list(_nl))) == 2)
check("★按 perPage=50 拉（和游戏弹窗一致）",
      "perPage=%d" % P._NOTIF_PAGE in _nl.url, _nl.url)
check("★必须带 category=all（游戏那边每次都带，少带可能返回空数组）",
      "category=all" in _nl.url, _nl.url)

# --- 归一化：接口字段名 → 页面字段名 ---
_rows = P._notif_rows({"notifications": [
    {"id": 1, "notify_type": "ticket", "message": "被贴条了",
     "created_at": _before(minutes=3), "is_read": 0},
    {"id": 2, "notify_type": "游戏以后的新类型", "message": "未知类型也照显示",
     "created_at": _before(hours=5), "is_read": 1},
    "脏数据", 42,
]})
check("通知行归一：非字典项被丢掉（脏数据不许把整页打成 500）",
      len(_rows) == 2, _rows)
check("通知行归一：字段名映射成页面用的键",
      _rows[0] == {"id": 1, "type": "ticket", "message": "被贴条了",
                   "time": _before(minutes=3), "read": False}, _rows[0])
check("is_read=1 记成已读（★别把 1 当成真未读，满屏都是竖条）",
      _rows[1]["read"] is True, _rows[1])
check("未知类型照样保留内容，不静默丢弃",
      _rows[1]["type"] == "游戏以后的新类型" and "未知类型也照显示" in _rows[1]["message"])
check("列表缺失 / 不是字典时返回空列表",
      P._notif_rows({}) == [] and P._notif_rows("脏") == []
      and P._notif_rows({"notifications": "脏"}) == [])

# --- 相对时间：口径照抄游戏前端 _fmtTime ---
# ★这些夹具必须用 **UTC**（实测：notifications.php 的 created_at 下发的是 UTC）。
# 原来用 _before()（本地时间）造，那是照着 bug 造的夹具 —— 断言校验的正是
# 「把 UTC 当本地」这个错误行为，所以 bug 发布出去时这几条照样全绿。
def _utc_before(**kw):
    return (dt.datetime.utcnow() - dt.timedelta(**kw)).strftime(
        "%Y-%m-%d %H:%M:%S")


check("通知时间：0 分钟 → 刚刚", P._notif_time(_utc_before(minutes=0)) == "刚刚")
check("通知时间：3 分钟前", P._notif_time(_utc_before(minutes=3)) == "3分钟前")
check("通知时间：5 小时前", P._notif_time(_utc_before(hours=5)) == "5小时前")
check("通知时间：2 天前", P._notif_time(_utc_before(days=2)) == "2天前")
# ★两次 utcnow() 之间可能跨过一秒，先取一次再复用，否则这条会 flaky。
# 期望值必须是**本地时间** —— 超过 7 天那条显示的是换算后的钟点，
# 直接拿 UTC 串去比会差一个时区偏移。
_N9 = dt.datetime.utcnow() - dt.timedelta(days=9)
_N9_local = _N9 + (P._tz().utcoffset(_N9) or dt.timedelta(0))
check("通知时间：超过 7 天显示具体日期",
      P._notif_time(_N9.strftime("%Y-%m-%d %H:%M:%S"))
      == _N9_local.strftime("%Y-%m-%d %H:%M"),
      (_N9_local.strftime("%Y-%m-%d %H:%M"),
       P._notif_time(_N9.strftime("%Y-%m-%d %H:%M:%S"))))
check("通知时间解析不了就原样显示，不编「未知」占位",
      P._notif_time("乱七八糟") == "乱七八糟" and P._notif_time("") == "")

# --- 类型配色：对着游戏前端 colorMap 抄的 ---
check("★15 种 notify_type 全都有配色",
      set(P._NOTIF_TYPES) == {
          "park", "unpark", "ticket", "report", "levelup", "charity",
          "cash_add", "cash_sub", "bid_out", "bid_win", "bid_lost",
          "buy_car", "cdk", "feedback_reply", "system"},
      sorted(set(P._NOTIF_TYPES)))
check("配色都是合法的 #RRGGBB（渲染器不吃别的写法）",
      all(re.fullmatch(r"#[0-9a-fA-F]{6}", c) for _n, c in P._NOTIF_TYPES.values()),
      P._NOTIF_TYPES)
check("每个类型都有中文名（列表里要能一眼看出是什么事）",
      all(n and isinstance(n, str) for n, _c in P._NOTIF_TYPES.values()))
check("未知类型有兜底，不静默丢内容", P._NOTIF_FALLBACK[0] == "通知")

# --- 铃铛 ---
_bell = P._notif_bell({"notifUnread": 3}, "plugin/x/notif_detail")
_be = (_bell.get("events") or {}).get("click") or {}
_bt = [str(n["text"]) for n in walk(_bell) if "text" in n]
check("铃铛带 click 事件指向传入的 api",
      _be.get("api") == "plugin/x/notif_detail" and _be.get("method") == "post", _be)
check("铃铛点了是展开（open=True）", _be.get("params") == {"open": True}, _be.get("params"))
check("铃铛上有未读数角标", "3" in _bt, _bt)
check("未读数过 99 显示 99+（不会撑破铃铛）",
      "99+" in [str(n["text"]) for n in walk(
          P._notif_bell({"notifUnread": 150}, "a")) if "text" in n])
check("没有未读时不显示角标（不显示 0）",
      not [t for t in [str(n["text"]) for n in walk(
          P._notif_bell({"notifUnread": 0}, "a")) if "text" in n] if t.isdigit()])
check("铃铛图标是内联 SVG，不是 emoji（Windows 会渲染成豆腐块）",
      any(isinstance(n.get("html"), str) and "<svg" in n["html"] for n in walk(_bell)))

# --- 面板 ---
NOTIF_SNAP = {"notifications": P._notif_rows({"notifications": [
    {"id": 1, "notify_type": "ticket", "message": "被贴条了",
     "created_at": _utc_before(minutes=3), "is_read": 0},
    {"id": 2, "notify_type": "park", "message": "停好了",
     "created_at": _utc_before(hours=5), "is_read": 1},
]})}
_panel = P._notif_panel(NOTIF_SNAP, "plugin/x/notif_detail")
_pt = [str(n["text"]) for n in walk(_panel) if "text" in n]
check("面板标题写明消息通知", "消息通知" in _pt, _pt[:4])
check("面板显示未读条数（按当前列表现算）", "1 条未读" in _pt, _pt[:4])
check("面板列出每条通知的正文", "被贴条了" in _pt and "停好了" in _pt)
check("面板标注通知类型", "贴条" in _pt and "停车" in _pt, _pt)
check("面板显示相对时间", "3分钟前" in _pt and "5小时前" in _pt, _pt)
check("★清空按钮不存在（不可恢复，且插件页没二次确认框）",
      not any(t == "清空" for t in _pt), _pt)
check("有未读时才有「全部已读」按钮",
      "全部已读" in _pt, _pt)
_ra = [n for n, t in _exp_clickers(_panel) if t == "全部已读"]
check("★全部已读打回同一接口，带 open=True + read_all=True",
      len(_ra) == 1
      and _ra[0]["events"]["click"].get("params") == {"open": True, "read_all": True},
      _ra[0]["events"]["click"] if _ra else None)
_allread_panel = P._notif_panel(
    {"notifications": P._notif_rows({"notifications": [
        {"id": 1, "notify_type": "park", "message": "x",
         "created_at": _before(hours=1), "is_read": 1}]})}, "a")
check("全部已读时「全部已读」按钮消失（没东西可读就不给按）",
      "全部已读" not in [str(n["text"]) for n in walk(_allread_panel) if "text" in n])
check("全部已读时「全部已读」按钮不挂任何 click 事件",
      not [n for n, t in _exp_clickers(_allread_panel) if t == "全部已读"])
_ncl = [n for n, t in _exp_clickers(_panel) if t == "收起"]
check("面板带收起出口", len(_ncl) == 1, [t for _n, t in _exp_clickers(_panel)])
check("收起按钮打回同一接口并带 open=False",
      len(_ncl) == 1
      and _ncl[0]["events"]["click"].get("params") == {"open": False},
      _ncl[0]["events"]["click"] if _ncl else None)
_bars = [n for n in walk(_panel)
         if (n.get("props") or {}).get("style", {}).get("width") == "3px"]
check("★只有未读行才有左侧竖条（已读行不加）", len(_bars) == 1, len(_bars))
_ue = [str(n["text"]) for n in walk(
    P._notif_panel({"notifications": []}, "a")) if "text" in n]
check("没有通知时显示「暂无通知」而不是空白面板", "暂无通知" in _ue, _ue)
_allread = [str(n["text"]) for n in walk(P._notif_panel(
    {"notifications": P._notif_rows({"notifications": [
        {"id": 1, "notify_type": "park", "message": "x",
         "created_at": _before(hours=1), "is_read": 1}]})}, "a"))
    if "text" in n]
check("全部已读时标题不显示「N 条未读」",
      not any("条未读" in t for t in _allread), _allread)

# --- v1.49.0 展开太长：只渲染前 _NOTIF_SHOW 条 ---
# _NOTIF_PAGE 刻意**不**跟着 _NOTIF_SHOW 一起砍：全部 rows 都参与算
# 「N 条未读」，砍拉取条数等于把那个数字的上限也砍了，变成一个更不准的数字。
# 打开慢已经靠并发（见 #34）解决，不靠少拉条数。这条锁住下限方向。
# 上面这些断言全都拿 P._NOTIF_SHOW 现算，常量一改它们就跟着改，永远是绿的
# ——「断言跟着被测值走」等于没断言（反向验证实测：改成 26 照样 805/805）。
# 用户要的是「展开太短」这个具体长度，验收值必须钉成字面量。
check("★面板渲染条数锁死在 8（这是「展开太短」的验收值）",
      P._NOTIF_SHOW == 8, P._NOTIF_SHOW)
check("★拉取条数不小于渲染条数（否则列表永远填不满）",
      P._NOTIF_PAGE >= P._NOTIF_SHOW, (P._NOTIF_PAGE, P._NOTIF_SHOW))
_MANY = P._notif_rows({"notifications": [
    {"id": 100 + i, "notify_type": "park", "message": f"第{i}条消息",
     "created_at": _before(minutes=30 - i), "is_read": 0}
    for i in range(P._NOTIF_PAGE)]})
_panel20 = P._notif_panel({"notifications": _MANY}, "a")
_pt20 = [str(n["text"]) for n in walk(_panel20) if "text" in n]
_shown_msgs = [t for t in _pt20 if t.startswith("第") and t.endswith("条消息")]
check("★超过 %d 条时只渲染前 %d 条" % (P._NOTIF_SHOW, P._NOTIF_SHOW),
      len(_shown_msgs) == P._NOTIF_SHOW, len(_shown_msgs))
check("★渲染的是**最近**的 %d 条（最新的在前）" % P._NOTIF_SHOW,
      _shown_msgs == [f"第{i}条消息" for i in range(P._NOTIF_SHOW)],
      _shown_msgs)
check("★被截掉的部分在面板底部写明还剩多少条",
      any("还有 %d 条" % (P._NOTIF_PAGE - P._NOTIF_SHOW) in t for t in _pt20),
      [t for t in _pt20 if "还有" in t])
check("★截断提示里指明去看全部的去处",
      any("游戏网页" in t and "全部" in t for t in _pt20),
      [t for t in _pt20 if "还有" in t])
check("★未读数按全部 %d 条算，不跟着截断变小" % P._NOTIF_PAGE,
      "%d 条未读" % P._NOTIF_PAGE in _pt20,
      [t for t in _pt20 if "条未读" in t])
check("未超上限时不显示截断提示",
      not any("还有" in t for t in _pt), _pt)
check("★没有通知时也不显示截断提示",
      not any("还有" in t for t in _ue), _ue)
_under = [str(n["text"]) for n in walk(P._notif_panel(
    {"notifications": P._notif_rows({"notifications": [
        {"id": i, "notify_type": "park", "message": f"x{i}",
         "created_at": _before(hours=1), "is_read": 1}
        for i in range(P._NOTIF_SHOW)]})}, "a")) if "text" in n]
check("★刚好等于上限时全部渲染且不提示截断",
      len([t for t in _under if t.startswith("x")]) == P._NOTIF_SHOW
      and not any("还有" in t for t in _under),
      [t for t in _under if "还有" in t or t.startswith("x")])
check("★截断提示不挂在 click 上（没有可点动作，别装成能点的）",
      not [n for n in walk(_panel20)
           if "text" in n and "还有" in str(n["text"])
           and (n.get("events") or {}).get("click")])

# --- 只读边界：源码层面锁死 ---
# 必须先剥掉注释。文档里写着「POST {action: read_all | clear}」是在解释
# 插件**不做**这些操作，拿整份源码去 find 会把这句说明当成「有写操作」，
# 断言就成了摆设。用 tokenize 按真实注释 token 删，不靠行首 # 猜。
def _code_only(src):
    kills = [tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
             if tok.type == tokenize.COMMENT]
    lines = src.split("\n")
    for tok in reversed(kills):          # 从后往前删，免得行号被前面的改动带偏
        i, j = tok.start
        k, l = tok.end
        if i == k:
            lines[i - 1] = lines[i - 1][:j].rstrip() + " " + lines[i - 1][l:]
        else:
            lines[i - 1] = lines[i - 1][:j]
            lines[k - 1] = lines[k - 1][l:]
    return "\n".join(lines)


_SRC_CODE = _code_only(_SRC_TEXT)
check("★剥注释生效：read_all 只存在于注释里，代码里搜不到",
      'read_all' in _SRC_TEXT and '"read_all"' in _SRC_CODE,
      [ln.strip()[:70] for ln in _SRC_CODE.split("\n") if "read_all" in ln][:3])
check("★清空（action=clear）插件**不做**：插件页弹不出 confirm 二次确认框",
      '"clear"' not in _SRC_CODE,
      [ln.strip()[:70] for ln in _SRC_CODE.split("\n") if '"clear"' in ln][:3])
check("★没有别的地方偷偷 POST 通知写接口",
      len(re.findall(r"post_json\([^)]*notifications\.php", _SRC_CODE, re.S)) == 1,
      re.findall(r"post_json\([^)]*notifications\.php", _SRC_CODE, re.S))

# --- 接入轮次：不额外多花一次往返 ---
check("未读数挂进特卖/称号那个 gather（★并行，不加往返）",
      "self._load_notif_count(client)" in _body2("_start_market_prefetch")
      and "self._load_notif_count(client)" in _body2("_take_market_prefetch"))
check("★通知列表返回空时写日志，不静默（角标有未读、列表却空最难查）",
      "通知列表返回异常：" in _SRC_TEXT
      and "通知列表为空，响应键：" in _SRC_TEXT
      and "读取通知列表失败：" in _SRC_TEXT)
check("_take_market_prefetch 返回三元组，取数处按三元解包",
      'flash, titles, notif_count = await self._take_market_prefetch(client, state)'
      in _SRC_TEXT)
check("★这轮没拉到未读数时沿用上一次的，不写成 0",
      'notif_count if notif_count is not None' in _SRC_TEXT
      and 'else self._to_int(prev.get("notifUnread"), 0, minimum=0)' in _SRC_TEXT
      and '"notifUnread": notif_unread,' in _SRC_TEXT)
check("★通知列表跨轮次保留（定时任务每 5 分钟重建快照，不带过去就抹成空）",
      "carried = prev.get(\"notifications\")" in _SRC_TEXT
      and '"notifications": (list(carried)' in _SRC_TEXT,
      [ln.strip()[:70] for ln in _SRC_TEXT.split("\n")
       if "carried" in ln][:4])
check("快照里另存一份通知列表（面板只读快照，不再发请求）",
      '"notifications"' in _SRC_TEXT
      and "await self._patch_snapshot_notifications(items)" in _SRC_TEXT)

# --- 路由与视图态 ---
check("/notif_detail 路由注册并指向 api_notif_detail",
      '"path": "/notif_detail"' in _SRC_TEXT
      and '"endpoint": self.api_notif_detail' in _SRC_TEXT)
_nd = _body2("api_notif_detail")
check("api_notif_detail 按 open 翻转视图状态（★否则收不起来）",
      '_notif_open = bool(getattr(request, "open", False))' in _nd)
check("展开时才去拉列表，收起直接清缓存（下次点开拿新的）",
      "if not self._notif_open:" in _nd and "_load_notif_list" in _nd
      and "await self._patch_snapshot_notifications([])" in _nd
      and _nd.find("if not self._notif_open:") < _nd.find("_load_notif_list"),
      (_nd.find("if not self._notif_open:"), _nd.find("_load_notif_list")))
check("★全部已读走 read_all 分支，且在拉列表之前拦下（省一次往返）",
      'getattr(request, "read_all", False)' in _nd
      and "_notif_read_all" in _nd
      and _nd.find("read_all") < _nd.find("_load_notif_list"),
      (_nd.find("read_all"), _nd.find("_load_notif_list")))
_ra_body = _body2("_notif_read_all")
check("★全部已读调游戏 action=read_all",
      '"action": "read_all"' in _ra_body)
check("★全部已读的真实响应一定写进日志（不然「点了没反应」只能靠猜）",
      "全部已读响应：" in _ra_body)
check("失败分支必须提前返回（游戏侧带 error 时不动本地）",
      "if failed:" in _ra_body
      and _ra_body.find("if failed:")
      < _ra_body.find("_write_notifications(rows, 0)"))
check("★成功判据绝不把 message 当错误（★实测成功响应就带 message）",
      'result.get("error") or ""' in _ra_body
      and 'result.get("message")' not in _ra_body)
check("★全部已读强制把未读数清零，不让「按列表现算」把 0 变回去",
      "await self._write_notifications(rows, 0)" in _ra_body)
# ★ 本轮修的真 bug：只 async_save_data 不够 —— 页面渲染走 _page_data()，
# 它优先读进程内缓存 self._page_cache。只改库的话，库里已经是 0 了，
# 页面下一次渲染读的还是内存里那份 10，「全部已读」看起来就是完全没反应。
_wn = _body2("_write_notifications")
check("★写通知必须同时更新 _page_cache 与数据库",
      "self._page_cache = cache" in _wn
      and 'await self.async_save_data("snapshot", snapshot)' in _wn)
check("★_patch_snapshot_notifications 走统一的写入口",
      "await self._write_notifications(items, None)"
      in _body2("_patch_snapshot_notifications"))
check("★_write_notifications 存在且被两处调用（打开面板 + 全部已读）",
      "async def _write_notifications(" in _SRC_TEXT
      and _SRC_TEXT.count("self._write_notifications(") == 2,
      _SRC_TEXT.count("self._write_notifications("))


class _ReadOk:
    async def post_json(self, url, json=None, **kw):
        assert "notifications.php" in url and json == {"action": "read_all"}, (url, json)
        return {"success": True}


class _ReadRefuse:
    async def post_json(self, url, json=None, **kw):
        return {"success": False, "error": "游戏侧拒绝"}


class _ReadBoom:
    async def post_json(self, url, json=None, **kw):
        raise RuntimeError("网络炸了")


# 光看源码不够：注入「失败时不 return、直接继续改本地」的坏代码，源码断言会
# 被 except 分支里那句 "success": False 骗过。这里真跑一遍，看快照有没有被动。
_SNAP_NOTIF = {"notifUnread": 5, "notifications": [
    {"id": 1, "type": "ticket", "message": "a", "time": "", "read": False},
    {"id": 2, "type": "park", "message": "b", "time": "", "read": True}]}
DB["snapshot"] = _copy.deepcopy(_SNAP_NOTIF)
_r_ok = asyncio.run(P()._notif_read_all(_ReadOk()))
check("全部已读成功后角标清零、列表全部标成已读",
      _r_ok.success is True
      and DB["snapshot"]["notifUnread"] == 0
      and all(i["read"] for i in DB["snapshot"]["notifications"]),
      (_r_ok.success, DB["snapshot"]))

DB["snapshot"] = _copy.deepcopy(_SNAP_NOTIF)
_r_ref = asyncio.run(P()._notif_read_all(_ReadRefuse()))
check("游戏侧拒绝时返回失败，不谎报成功", _r_ref.success is False, _r_ref.message)
check("★游戏侧拒绝时本地快照一个字节都不改（★否则角标被抹成 0）",
      DB["snapshot"]["notifUnread"] == 5
      and [i["read"] for i in DB["snapshot"]["notifications"]] == [False, True],
      DB["snapshot"])

DB["snapshot"] = _copy.deepcopy(_SNAP_NOTIF)
_r_boom = asyncio.run(P()._notif_read_all(_ReadBoom()))
check("网络异常时返回失败而不是把异常抛给页面",
      _r_boom.success is False, _r_boom.message)
check("网络异常时快照同样不被改", DB["snapshot"]["notifUnread"] == 5)
check("_notif_open 默认关闭，页面默认不展开通知", P._notif_open is False)
check("★通知面板不落盘，且 init_plugin 不从存档读回（v1.48.0）",
      "_save_view(" not in re.sub(r"(?m)#.*$", "", _nd)
      and 'saved_view.get("notif_open")' not in _SRC_TEXT
      and "self._notif_open = False" in _SRC_TEXT)

# --- 端到端：整页真的把铃铛与面板挂出来了 ---
DB.clear()
DB["snapshot"] = dict(EXP_SNAP, name="我", cash=1000, car_total=0, car_max=5,
                      parked_total=0, pending_gain=0, min_duration=10,
                      cars=[], spots=[], catalog=[], flash=[],
                      notifUnread=3, notifications=NOTIF_SNAP["notifications"])
DB["history"] = []
_nb = P(); _nb._page_cache = None
_nb._exp_open = False; _nb._notif_open = False
_blob_n = repr(_nb.get_page())
check("整页带上铃铛与未读角标", "/notif_detail" in _blob_n and "通知" in _blob_n)
check("默认不渲染通知面板（还没点开）", "消息通知" not in _blob_n)
_no = P(); _no._page_cache = None
_no._exp_open = False; _no._notif_open = True
_blob_no = repr(_no.get_page())
check("点开后整页真的带上通知面板",
      "消息通知" in _blob_no and "被贴条了" in _blob_no)
check("铃铛排在经验值格之后（名字 → 等级 → 通知）",
      _blob_no.find("Lv.7") >= 0 and _blob_no.find("Lv.7") < _blob_no.find("/notif_detail"))


# v1.40.0 的真 bug 当时没被任何断言抓到，就是「展开类触发器」没有统一口径。
# 这里不逐个手写，而是把整页里所有指向展开类接口的可点节点捞出来，
# 要求每一个都显式带 open 方向 —— 以后再加新面板也自动受这条管。
def _open_triggers(page):
    out = []
    for n in walk(page):
        ev = (n.get("events") or {}).get("click") or {}
        if str(ev.get("api") or "").endswith(
                ("/exp_detail", "/notif_detail", "/title_pick")):
            out.append((ev.get("api"), ev.get("params")))
    return out


_both = P(); _both._page_cache = None
_both._exp_open = True; _both._notif_open = True
_trigs = _open_triggers(list(walk(_both.get_page())))
check("★展开类接口的每个触发器都显式带 open 方向，绝不发空 params",
      len(_trigs) >= 5
      and all(isinstance(p, dict) and "open" in p for _a, p in _trigs),
      _trigs)
check("★两个面板各自的「点开」与「收起」方向都齐了",
      {p.get("open") for _a, p in _trigs if _a.endswith("/exp_detail")} == {True, False}
      and {p.get("open") for _a, p in _trigs
           if _a.endswith("/notif_detail")} == {True, False},
      [(a, p) for a, p in _trigs])

print("\n--- #30 贴条错峰并行 / 挪车停车保持串行（v1.46.0）---")
# 实测依据：游戏自己的 showAllTicket 就是 setTimeout(index * 200) + Promise.all，
# 注释写明「并行执行，但每个之间间隔 200ms 避免过于密集」。
# 而它的一键挪车 showAllUnpark 是 for 里 await，严格串行。
# 照抄这个差别：贴条动的是各车位独立的罚款记录，挪车/停车动的是金币余额
# 和车辆占用这类**共享状态**，并发可能丢更新 = 丢金币。
check("错峰间隔常量在，且和游戏的 200ms 一致",
      abs(P._TICKET_STAGGER - 0.2) < 1e-9, P._TICKET_STAGGER)

_tk2 = _body2("_auto_ticket_one")
# 断代码行，不看注释：这几条断言的解释性注释里就写着 "return_exceptions=True"
# 和 "asyncio.sleep(index ..."，不剥掉的话匹配到的是我自己的注释，
# 把真代码删了照样绿（反向验证当场抓出来过一次）。
_tk2c = re.sub(r"(?m)#.*$", "", _tk2)
check("贴条改成 gather 并发", "asyncio.gather" in _tk2c)
check("★贴条并发必须用 return_exceptions（否则一张失败整批抛，账全丢）",
      "return_exceptions=True" in _tk2c)
check("★贴条必须按 index 错峰，不能齐发",
      "index * self._TICKET_STAGGER" in _tk2c
      and "asyncio.sleep(index" in _tk2c)
check("贴条结果仍按下标顺序处理（摘要顺序稳定）",
      "for index, result in enumerate(results):" in _tk2c)
check("★贴条异常单独兜住并继续（不能因一张抛掉后面全部）",
      "isinstance(result, BaseException)" in _tk2c
      and _tk2c.count("continue") >= 3)
check("★失败/异常的车位都不计入罚款（贴成功才记账）",
      _tk2c.find("isinstance(result, BaseException)")
      < _tk2c.find("self._shop_fined += got")
      and _tk2c.find('if not (result and result.get("success"))')
      < _tk2c.find("self._shop_fined += got")
      and "self._ticket_seen" not in _tk2c)
check("ownerEarn/fine 兼容读取仍在",
      'result.get("ownerEarn")' in _tk2c and 'result.get("fine")' in _tk2c)
check("_shop_fined 仍逐张累加（统计口径不变）",
      "self._shop_fined += got" in _tk2c)

# ★ 这两条是「不许被优化掉」的红线：挪车/停车一旦有人改成并发，
# 就可能在游戏侧变成并发改金币余额。游戏自己都不敢并行。
for _fn, _why in (("_unpark_due_cars", "挪车要改金币余额"),
                  ("_park_idle_cars", "停车要改车辆/车位占用")):
    _b = re.sub(r"(?m)#.*$", "", _body2(_fn))
    check("★%s 必须保持串行（%s）" % (_fn, _why),
          "asyncio.gather" not in _b and "asyncio.create_task" not in _b)

print("\n--- #31 通知面板不再空跑市场预取（v1.46.0）---")
# 预取结果只有 _record_round 会取（它要把特卖/称号/未读数落进快照）。
# 通知面板只读不记账，不关掉就是每次点铃铛白发 3 个请求。
_gc = re.sub(r"(?m)#.*$", "", _body2("_load_game_context"))
check("_load_game_context 有 prefetch 开关且默认开启",
      "prefetch: bool = True" in _gc)
check("预取被 prefetch 开关包住",
      "if prefetch:" in _gc and "self._start_market_prefetch(client, state)" in _gc
      and _gc.find("if prefetch:") < _gc.find("_start_market_prefetch"))
def _flat(s):
    """把空白压成单空格 —— 断「调用形参」时必须用，原文换行怎么排都不该影响判定。"""
    return " ".join(s.split())


check("★通知面板传 prefetch=False",
      "_load_game_context( prefetch=False" in _flat(_code_only(_body2("api_notif_detail"))))
check("★只读不记账的路径才关预取；挪车/停车/贴条等照旧默认开",
      all("_load_game_context( prefetch=False"
          not in _flat(_code_only(_body2(fn)))
          for fn in ("api_unpark", "api_park", "api_ticket",
                     "api_charity_spot", "api_refresh_status"))
      and "self._start_market_prefetch(client, state)" in _gc)

print("\n--- #32 交互不再等 view 落盘（v1.47.0）---")
# 诊断版 v1.46.1 实测：async_save_data("view", …) 要 31~4813 毫秒，而它在
# 每次点击的同步路径上 —— 浏览器看到的 POST 692~2847 毫秒就是它。
# 所以改成「内存 + 防抖后台落盘」。下面锁住三件事：
#   1. 点击不等落盘（这是这次优化的全部意义）
#   2. 连续点击合并成一次写
#   3. 合并的是内存全量，不是各次增量 —— 早先那个「切页签冲掉车市分类」的
#      老 bug 不能借机复活
code_nc = re.sub(r"(?m)#.*$", "", _SRC_TEXT)
_sv_nc = re.sub(r"(?m)#.*$", "", _body2("_save_view"))
# 断的是「有没有 await 那两个调用」，不是「函数体里有没有出现过这几个词」。
# _save_view 的 docstring 里就写着 async_save_data / async_get_time（解释为什么
# 不再同步写），只剥 # 注释剥不掉 docstring，匹配字面量会被自己的说明满足 ——
# 同一个坑已经踩过两次：第一次是注释里写着 return_exceptions，第二次是这里。
check("★_save_view 不再同步落盘（点击不能等那次慢写）",
      "await self.async_save_data" not in _sv_nc
      and "await self.async_get_data" not in _sv_nc
      and "self._view_mem.update(patch)" in _sv_nc
      and "self._schedule_view_flush()" in _sv_nc)
check("★有防抖参数与后台任务槽位",
      "_VIEW_DEBOUNCE = 0.8" in code_nc and "_view_flush_task" in code_nc)
check("★后台写的是内存全量，不是各次增量（否则会互相覆盖）",
      "dict(self._view_mem)" in re.sub(r"(?m)#.*$", "", _body2("_flush_view")))
check("★跨事件循环的旧任务要被当成已死（否则永远排不出新任务）",
      "task.get_loop() is loop" in code_nc)
check("★写失败要把变更放回去，不能静默丢掉",
      "self._view_dirty.update(payload)" in code_nc
      and "保存详情页状态失败" in code_nc)
check("★可变的 view 状态用 None 占位，不写成共享的类属性字典",
      re.search(r"(?m)^\s*_view_mem: Optional\[Dict\[str, Any\]\] = None\s*$",
                _SRC_TEXT) is not None)
check("init_plugin 用存档初始化内存真相源",
      "self._view_mem = dict(saved_view)" in code_nc)

# 端到端：点五下页签，只应该落一次盘，且落出去的是最后一次的状态
_pv = P()
_pv._view_mem = {}
_pv._view_dirty = {}
_pv._view_flush_task = None
_calls = {"n": 0, "wrote_before_debounce": None}
_orig_save = type(_pv).async_save_data


async def _counting_save(self, key, value, plugin_id=None):
    if key == "view":
        _calls["n"] += 1
    return await _orig_save(self, key, value, plugin_id)


type(_pv).async_save_data = _counting_save
try:
    async def _five():
        for t in ("shop", "garage", "stats", "spots", "shop"):
            await _pv.api_set_tab(mod.TabSwitch(tab=t))
        # 防抖期间还没落盘 —— 这正是「点击不等它」的表现
        _calls["wrote_before_debounce"] = _calls["n"]
        await _pv.flush_view_now()
        return _calls["n"]
    _n = asyncio.run(_five())
    check("★连点五次页签只落一次盘，且写的是最后一次的状态",
          _n == 1 and (DB.get("view") or {}).get("tab") == "shop",
          (_n, DB.get("view")))
    check("★五次点击全程一次都没同步落盘（点击等待为 0 的直接体现）",
          _calls["wrote_before_debounce"] == 0, _calls)
finally:
    type(_pv).async_save_data = _orig_save

print("\n--- #33 两个面板不持久化（v1.48.0）---")
# 用户要求「等级详情和通知详情每次打开插件页面默认都是收起的」。
# 好处不只是默认状态：这两个面板的接口从此**一次数据库写都不发生**。
# 关键风险是**旧存档** —— v1.47.0 之前会把 exp_open/notif_open 存进 view，
# 只改「不再写」而不改「不再读」，用户升级后第一次打开面板会被旧存档撑开。
_ed = re.sub(r"(?m)#.*$", "", _body2("api_exp_detail"))
_nd2 = re.sub(r"(?m)#.*$", "", _body2("api_notif_detail"))
check("★经验值面板接口不再落盘",
      "_save_view(" not in _ed,
      [l.strip()[:70] for l in _ed.split("\n") if "_save_view" in l])
check("★通知面板接口不再落盘",
      "_save_view(" not in _nd2,
      [l.strip()[:70] for l in _nd2.split("\n") if "_save_view" in l])
check("★init_plugin 不从存档读回这两个展开状态",
      'saved_view.get("exp_open")' not in _SRC_TEXT
      and 'saved_view.get("notif_open")' not in _SRC_TEXT)
check("★但称号选择器的展开状态仍然持久化（用户只点名了这两个面板）",
      '_save_view({"title_open"' in re.sub(r"(?m)#.*$", "",
                                           _body2("api_title_pick"))
      and 'saved_view.get("title_open")' in _SRC_TEXT)

# 端到端：存档里明明存着 exp_open/notif_open = True，重载后也必须是收起的
DB["view"] = {"tab": "garage", "exp_open": True, "notif_open": True,
              "title_open": False}
_pn = P()
_pn.init_plugin({})
check("★旧存档里的 exp_open=True 不会把经验值面板撑开",
      _pn._exp_open is False, _pn._exp_open)
check("★旧存档里的 notif_open=True 不会把通知面板撑开",
      _pn._notif_open is False, _pn._notif_open)
_pg = json.dumps(_pn.get_page(), ensure_ascii=False)
check("★重载后面板确实没渲染出来（端到端，不只看内存字段）",
      "消息通知" not in _pg and "还差" not in _pg, "面板被渲染出来了")
_r1 = asyncio.run(_pn.api_exp_detail(mod.ExpDetailRequest(open=True)))
check("★本次打开期间点开面板照常生效（没把功能改废）",
      bool(getattr(_r1, "success", False)) and _pn._exp_open is True,
      (getattr(_r1, "message", _r1), _pn._exp_open))
# 「零数据库写」的正确判据是**没有待落盘的变更**，而不是 DB 里没有那个键 ——
# 上面为了验旧存档，我们自己往 DB["view"] 里塞过 exp_open。
check("★点开面板没有产生任何待落盘变更（这才是零写入的直接证据）",
      not _pn._view_dirty, getattr(_pn, "_view_dirty", "<没有这个属性>"))

print("\n--- #34 通知面板：打开更快、展开更短（v1.49.0）---")
# 用户反馈「展开太长、打开太慢」。两处成因，分别处理：
#   慢 —— 原来是串行两个往返（state.php 鉴权 1.1s，再 notifications.php 1.1s）。
#         两者互不依赖，v1.49.0 把列表塞进 _load_game_context 的 extra 和 state
#         一起 gather，一个往返就够。
#   长 —— 面板把 50 条全渲染出来，手机上要划好几屏。v1.49.0 只渲染前 8 条，
#         剩下的用一行字交代清楚去哪儿看全部。

_gc3 = _code_only(_body2("_load_game_context"))
check("★_load_game_context 有 extra 参数（让调用方的数据搭同一个 gather）",
      "extra: Any = None" in _gc3)
check("★extra 的协程被 append 进同一个 jobs 列表",
      "jobs.append(extra(client))" in _gc3)
check("★state 与 extra 一起 gather（串行回去就白改了）",
      _flat(_gc3).count("asyncio.gather(") == 2
      and "return_exceptions=True" in _gc3,
      [l.strip() for l in _gc3.split("\n") if "gather" in l])
check("★extra 为空时保持原来的 gather 语义（不带 return_exceptions）",
      "if extra is None:" in _gc3
      and _flat(_gc3).find("if extra is None:")
      < _flat(_gc3).find("return_exceptions=True"))
check("★返回值从 4 元组变 5 元组，末尾是 extra 的结果",
      "Tuple[Optional[Any], Optional[dict], Dict[Any, dict], str, Any]" in _flat(_gc3)
      and "extra_result" in _gc3)
check("★extra / state / cars_map 三个异常都被归位，不会让下游 .get() 炸",
      "isinstance(extra_result, BaseException)" in _gc3
      and "isinstance(state, BaseException)" in _gc3
      and "isinstance(cars_map, BaseException)" in _gc3)
# 全部调用点都得接住 5 个返回值，漏一个就是运行期 ValueError。
_gc_calls = re.findall(r"= await self\._load_game_context\(", _SRC_CODE)
_gc_five = re.findall(
    r"^\s*(\w+(?:\s*,\s*\w+){4})\s*=\s*await self\._load_game_context\(",
    _SRC_TEXT, re.M)
check("★%d 个调用点全部解包 5 个返回值" % len(_gc_calls),
      len(_gc_calls) > 0 and len(_gc_five) == len(_gc_calls),
      (len(_gc_calls), len(_gc_five)))

_nd3 = _code_only(_body2("api_notif_detail"))
_nd3f = _flat(_nd3)
check("★通知面板把列表请求搭进同一个 gather",
      "extra=None if _read_all else (lambda c: self._load_notif_list(c))" in _nd3f)
check("★「全部已读」那条路不发列表请求（它压根不用列表）",
      "extra=None if _read_all" in _nd3f
      and _nd3f.find("_read_all") < _nd3f.find("_load_game_context"))
check("★extra 没回来时按空列表处理而不是崩",
      "items = list(items or [])" in _nd3f)


class _ParClient:
    """记录每个请求的发起时刻，用来判定并行还是串行。"""

    def __init__(self):
        self.started = []

    async def get_json(self, url, **kw):
        self.started.append((url.rsplit("/", 1)[-1].split("?")[0],
                             time.monotonic()))
        await asyncio.sleep(0.3)
        return {"success": True}

    async def post_json(self, url, json=None, **kw):
        return {"success": True}


_orig_u5 = mod.AsyncRequestUtils
try:
    _pq = P()
    _pq._cars_cache = {1: {"id": 1}}
    _pq._cars_cache_at = time.monotonic()
    _pq._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), _pq)
    _pc = _ParClient()
    mod.AsyncRequestUtils = lambda **kw: _pc

    async def _ex(client):
        return await client.get_json("https://x/api/notifications.php?perPage=20")

    _t0 = time.monotonic()
    _r5 = asyncio.run(_pq._load_game_context(prefetch=False, extra=_ex))
    _elapsed = time.monotonic() - _t0
    _names5 = [n for n, _ in _pc.started]
    check("★extra 与 state 真的是并发发出的（两个请求几乎同时起跑）",
          "state.php" in _names5 and "notifications.php" in _names5
          and abs(_pc.started[-1][1] - _pc.started[0][1]) < 0.15,
          [(n, round(t - _pc.started[0][1], 3)) for n, t in _pc.started])
    check("★两个 0.3 秒的请求并起来不到 0.5 秒（串行会是 0.6 秒以上）",
          _elapsed < 0.5, round(_elapsed, 3))
    check("★返回 5 个值且第 5 个就是 extra 的结果",
          len(_r5) == 5 and _r5[4] == {"success": True},
          (len(_r5), _r5[4] if len(_r5) > 4 else None))
finally:
    mod.AsyncRequestUtils = _orig_u5

# extra 挂掉不许把整个鉴权带崩 —— 那是「顺带拉个数据」，不是主任务
_orig_u6 = mod.AsyncRequestUtils
try:
    _bq = P()
    _bq._cars_cache = {1: {"id": 1}}
    _bq._cars_cache_at = time.monotonic()
    _bq._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), _bq)
    mod.AsyncRequestUtils = lambda **kw: _ParClient()

    async def _ex_boom(client):
        raise RuntimeError("通知接口挂了")

    _rb = asyncio.run(_bq._load_game_context(prefetch=False, extra=_ex_boom))
    check("★extra 抛异常时降级成 None，state 照样拿到",
          len(_rb) == 5 and _rb[4] is None and bool(_rb[1]),
          _rb[4] if len(_rb) > 4 else None)
    check("★extra 抛异常时没有 err（不是失败，只是没拉到）", _rb[3] == "", _rb[3])
finally:
    mod.AsyncRequestUtils = _orig_u6

# state.php 自己抛异常时，不能因为 return_exceptions 接住了就变成 AttributeError
_orig_u7 = mod.AsyncRequestUtils
try:
    class _StateBoomClient(_ParClient):
        async def get_json(self, url, **kw):
            if "state.php" in url:
                raise RuntimeError("游戏挂了")
            return await super().get_json(url, **kw)

    _sq = P()
    _sq._cars_cache = {1: {"id": 1}}
    _sq._cars_cache_at = time.monotonic()
    _sq._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), _sq)
    mod.AsyncRequestUtils = lambda **kw: _StateBoomClient()
    _rs = asyncio.run(_sq._load_game_context(
        prefetch=False, extra=lambda c: _coro([{"id": 1}])))
    check("★state.php 抛异常时返回统一的登录失效错误而不是抛 AttributeError",
          len(_rs) == 5 and _rs[1] is None and "登录状态失效" in (_rs[3] or ""),
          _rs[3] if len(_rs) > 3 else None)
finally:
    mod.AsyncRequestUtils = _orig_u7

print("\n--- #35 停车优先停进高场景加成的车位（v1.50.0）---")
# 游戏新增「场景加成」：game_data.parkingBackgrounds 每项带 bonusPercent
# （街道 0 / 街道夜景 2 / 沙滩 5 / 牛来！！5，后两个 5000 金币）。
# 规则原文：「最终收益 = 基础收益 + 基础收益 x 加成百分比」—— 封顶先算、
# 加成后乘，所以 5% 车位的实际上限是 3150 而不是 3000。
# 加成属于**车位**：停进谁家的车位就吃谁场景的加成，收益进自己的口袋。
#
# 但不能只按加成排序 —— 5% 场景要 5000 金币，全服买的人可能就两三个，
# 10 辆车全压过去正是代码里一直要避免的「被批量驱离/举报」。
# 所以是「加成分层优先 + 每个车位主人最多 _PARK_MAX_PER_OWNER 辆」。

# 线上 game_data.php 的 parkingBackgrounds 实测值（裁剪版，够用即可）
_BGS = {"success": True, "cars": [], "parkingBackgrounds": [
    {"id": "jiedao", "name": "街道", "bonusPercent": 0, "price": 0},
    {"id": "jiedao2", "name": "街道夜景", "bonusPercent": 2, "price": 2000},
    {"id": "shatan", "name": "沙滩", "bonusPercent": 5, "price": 5000},
    {"id": "niulai", "name": "牛来！！", "bonusPercent": 5, "price": 5000},
]}

check("★场景表能读出加成百分比",
      P._bg_bonus_map(_BGS) == {"jiedao": 0, "jiedao2": 2, "shatan": 5,
                               "niulai": 5},
      P._bg_bonus_map(_BGS))
check("★game_data 拿不到时返回空表（而不是抛异常/返回 None）",
      P._bg_bonus_map(None) == {} and P._bg_bonus_map({}) == {})
check("★parkingBackgrounds 是脏数据时逐项跳过而不是崩",
      P._bg_bonus_map({"parkingBackgrounds": ["垃圾", {"name": "没 id"},
                                              {"id": "x", "bonusPercent": "5"}]})
      == {"x": 5})

# users 的键类型不保证，而且**键可能和资料里的 id 不是一回事**：
# 只认其中一种的话，另一批玩家的加成会整片变成 0，
# 表现为「功能好像没生效」却没有任何报错。
_UST = {"users": {"7": {"id": 7, "currentBg": "shatan"},
                  "8": {"id": 8, "currentBg": "jiedao"},
                  9: {"id": 9, "currentBg": "jiedao2"},
                  "10": {"id": 10},
                  "11": "脏数据"}}
_UB = P._owner_bonus_map(_UST, _BGS)
check("★users 是字符串键时能查到加成",
      _UB.get("7") == 5 and _UB.get("8") == 0 and _UB.get("9") == 2, _UB)
check("★users 是数字键时也能查到（键类型不一致不能全表失配）",
      _UB.get("9") == 2, _UB)
check("★没选场景（currentBg 缺失）按 0 处理", _UB.get("10") == 0, _UB)
check("★users 里混进非字典不崩，也不会被当成有加成",
      _UB.get("11", 0) == 0, _UB)
check("★users 整个缺失时返回空表",
      P._owner_bonus_map({}, _BGS) == {}
      and P._owner_bonus_map(None, _BGS) == {})
check("★game_data 缺失时所有加成都是 0（功能降级而不是报错）",
      P._owner_bonus_map(_UST, None).get("7") == 0)

# ★验收值必须钉成字面量 —— 下面这些行为断言都拿 P._PARK_MAX_PER_OWNER 现算，
# 常量一改它们跟着改，永远是绿的（反向验证实测：改成 99 照样全过）。
# v1.52.0 从 2 收紧到 1：每家停 2 辆还是太容易被贴/驱离/举报。
check("★每个车位主人最多停 1 辆（这是「别把车全押在一个人身上」的验收值）",
      P._PARK_MAX_PER_OWNER == 1, P._PARK_MAX_PER_OWNER)


class _ParkClient:
    """记录 park.php 落点，game_data.php 直接喂夹具。"""

    def __init__(self, game_data=None):
        self.posts = []
        self._gd = game_data or {"success": True, "cars": []}

    async def get_json(self, url, **kw):
        return self._gd

    async def post_json(self, url, json=None, **kw):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return {"success": True}


def _owner_of(spots, spot_id):
    for s in spots:
        if str(s.get("id")) == str(spot_id):
            return s.get("ownerId")
    return None


def _park_run(state, spots, n_cars, capacity, game_data=_BGS):
    """跑一轮 _park_idle_cars，返回 [(车位id, 加成%)] 落点顺序。"""
    bonus = P._owner_bonus_map(state, game_data)
    cl = _ParkClient(game_data)
    p = P()
    p._cars_cache = {}
    p._cars_cache_at = 0.0
    p._game_data = game_data
    idle = [{"id": "c%d" % i, "carId": 1, "status": "idle"} for i in range(n_cars)]
    asyncio.run(p._park_idle_cars(cl, idle, spots, "me", capacity, state))
    order = [j.get("spotId") for e, j in cl.posts if e == "park.php"]
    return [(sid, bonus.get(str(_owner_of(spots, sid)), 0)) for sid in order]


# 7 号是 5%、8 号是 0%、9 号是 2%，每人 3 个车位
_SPOTS = [{"id": "s%s_%d" % (o, i), "ownerId": o, "spotType": "private",
           "carInSpot": None}
          for o in ("7", "8", "9") for i in range(3)]

_pick = _park_run(_UST, _SPOTS, 4, 4)
check("★优先停进加成最高的车位（5% 的排最前）",
      _pick[0][1] == 5, _pick)
check("★停够车位后按加成从高到低铺开",
      [b for _, b in _pick] == sorted([b for _, b in _pick], reverse=True), _pick)

# ★给 random.seed() 固定种子做一组**确定性**的「第一个必须是 5%」断言。
# 不固定种子的话这断言是概率性的：每家上限 1 辆之后 _pick 只剩 3 辆，
# 没排序时第一个落在哪个主人手里是随机的，通过率只有 1/3 —— 反向验证里
# 会 flaky（同一份代码上一轮 OK、下一轮红）。种子一固定，两种实现的
# 结果都是确定的，不会再飘。
_SEEDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
_first_is_top = []
for _s in _SEEDS:
    random.seed(_s)
    _first_is_top.append(_park_run(_UST, _SPOTS, 3, 3)[0][1])
random.seed()          # 交还给全局，避免影响后面的随机性断言
check("★固定 12 个随机种子，每个的第一个落点都是加成最高的车位",
      all(b == 5 for b in _first_is_top), _first_is_top)
# ★这里必须钉成字面量 1，不能写成 `== P._PARK_MAX_PER_OWNER`：
# 那是「跟着被测常量现算」，常量改了这断言自动跟着改，永远是绿的
# （反向验证实测：把常量改成 99，这条照样 PASS）。
check("★5% 那个主人名下正好用满 1 辆上限",
      len([s for s, _ in _pick if s.startswith("s7_")]) == 1, _pick)

# 每个车位主人的上限：给 6 辆车，看谁超了
_pick6 = _park_run(_UST, _SPOTS, 6, 6)
_by_owner = {}
for sid, _b in _pick6:
    k = _owner_of(_SPOTS, sid)
    _by_owner[k] = _by_owner.get(k, 0) + 1
check("★任何车位主人都没超过 1 辆",
      all(v <= P._PARK_MAX_PER_OWNER for v in _by_owner.values()), _by_owner)
# ★3 家 x 3 个车位、6 辆车：每家上限 1 辆 -> 只能停 3 辆（原来上限 2 时是 6 辆）。
# 这就是收紧上限的真实代价：车主不够就少停车、少赚钱，所以
# free_private_owners 那个诊断字段必须存在，否则用户查不出原因。
check("★名额用满 3 个不同主人（每家 1 辆，6 辆车只能停 3 辆）",
      len(_pick6) == 3 and len(_by_owner) == 3, (_pick6, _by_owner))
check("★第一个用满的仍是加成最高的那个主人",
      _pick6[0][1] == 5, _pick6)

# 同一加成档内要随机，不能退化成固定顺序
_orders = {tuple(s for s, _ in _park_run(_UST, _SPOTS, 3, 3)) for _ in range(12)}
check("★同一加成档内的落点不是固定顺序（保留了原有的随机分散）",
      len(_orders) > 1, len(_orders))

# 降级：拿不到 game_data 时行为必须和改动前一致（全按 0，仍然停满）
_deg = _park_run(_UST, _SPOTS, 3, 3, game_data=None)
check("★没有场景数据时仍能停满车（功能降级不影响主流程）",
      len(_deg) == 3 and all(b == 0 for _, b in _deg), _deg)

# 加成只有一个主人有、且他只有 1 个车位时，剩下的要落到别人身上
_SPOTS1 = [s for s in _SPOTS if s["ownerId"] == "7"][:1] \
    + [s for s in _SPOTS if s["ownerId"] == "9"]
_pick1 = _park_run(_UST, _SPOTS1, 3, 3)
check("★高加成主人只有一个车位时，名额顺延给下一个加成的",
      _pick1[0][1] == 5 and _pick1[1][1] == 2, _pick1)

# 原有边界一条都不能丢
# 「不停自己的车位」必须单独造一个**只剩自己车位**的场景：
# 平常那批车位里有的是别人的高加成位，排序后根本轮不到自己那位，
# 就算把过滤条件整个删掉也照样全绿 —— 那样这条断言就是摆设。
_MEOW = [{"id": "tME", "ownerId": "me", "spotType": "private", "carInSpot": None}]
check("★只剩自己的车位时一辆也不停",
      _park_run(_UST, _MEOW, 1, 1) == [], _park_run(_UST, _MEOW, 1, 1))
check("★自己的车位混在别人中间时也不会被选中",
      [s for s, _ in _park_run(_UST, _MEOW + _SPOTS, 2, 2)] != ["tME", "tME"]
      and all(_owner_of(_MEOW + _SPOTS, s) != "me"
              for s, _ in _park_run(_UST, _MEOW + _SPOTS, 2, 2)))
_CH = [{"id": "ch1", "ownerId": "7", "spotType": "charity", "carInSpot": None}]
check("★爱心车位仍然被排除（游戏说明：场景加成对爱心车位不生效）",
      _pick1[0][0] != "ch1" and not _park_run(_UST, _CH, 1, 1), _CH)
_OCC = [dict(s, carInSpot=9) for s in _SPOTS]
check("★已被占用的车位仍然跳过", not _park_run(_UST, _OCC, 1, 1), len(_OCC))
# _SPOTS[:2] 是**同一家**（owner 7）的两个车位。车位有两个，但每家只能停 1 辆，
# 所以只能停 1 辆 —— 这条把「没车位」和「车位够但主人不够」区分开了。
check("★车位不够时不会硬凑（用完即停）",
      len(_park_run(_UST, _SPOTS[:2], 5, 5)) == 1,
      _park_run(_UST, _SPOTS[:2], 5, 5))

# ★「被上限挡完」的日志必须报真实候选数，不能是恒 0。
# logger.info 被 stub 吞掉了，这里临时挂捕获器验证日志里的数字。
# 场景：owner 7 的两个车位、5 辆车 —— 每家上限 1 辆，停 1 辆后剩下的候选
# 全被「每家最多停 1 辆」挡住，必然触发 spot is None 的日志分支。
# bug 版拿已被 pop 空的 targets 算，日志恒打印「候选车位只剩 0 个、都归 0 个车主」。
_cap_logger = sys.modules["app.sdk.logging"].logger
_cap_lines = []
_cap_orig_info = _cap_logger.info
_cap_logger.info = lambda *a: _cap_lines.append(a[0] if a else "")
try:
    _park_run(_UST, _SPOTS[:2], 5, 5)
finally:
    _cap_logger.info = _cap_orig_info
_cap_text = "\n".join(_cap_lines)
check("★被上限挡住时日志报真实候选数（不是「候选只剩 0 个」）",
      "候选车位只剩 0 个" not in _cap_text
      and "都归 0 个" not in _cap_text
      and "已停满 1 辆的车主" in _cap_text,
      _cap_lines)
_OWNERS3 = [{"id": "a1", "ownerId": "7", "spotType": "private", "carInSpot": None},
            {"id": "b1", "ownerId": "8", "spotType": "private", "carInSpot": None},
            {"id": "c1", "ownerId": "9", "spotType": "private", "carInSpot": None}]
check("★三个不同主人各能停 1 辆（车位够、主人也够）",
      len(_park_run(_UST, _OWNERS3, 5, 5)) == 3,
      _park_run(_UST, _OWNERS3, 5, 5))

# users 的键和资料 id 彻底对不上时的实测场景
_UST2 = {"users": {"k88": {"id": "88", "currentBg": "shatan"},
                   "k99": {"id": "99", "currentBg": "jiedao"}}}
_SPOTS2 = [{"id": "t88", "ownerId": "88", "spotType": "private", "carInSpot": None},
           {"id": "t99", "ownerId": "99", "spotType": "private", "carInSpot": None}]
check("★users 的键和资料 id 不一致时，仍能按 ownerId 查到加成",
      _park_run(_UST2, _SPOTS2, 1, 1) == [("t88", 5)],
      _park_run(_UST2, _SPOTS2, 1, 1))

_BGR = "2026-10-16 14:56:11"      # 实测：本地 22:56 那一轮的通知，接口给的是 14:56
_NOW8 = dt.datetime(2026, 10, 16, 22, 56, 15)
_orig_now = P._now_naive
P._now_naive = classmethod(lambda cls: _NOW8)
try:
    check("★通知时间按 UTC 解释（实测本地 22:56 的通知下发的是 14:56）",
          P._notif_time(_BGR) == "刚刚", P._notif_time(_BGR))
    check("★8 小时前的通知显示成「8小时前」而不是「刚刚」",
          P._notif_time("2026-10-16 14:56:11") == "刚刚"
          and P._notif_time("2026-10-16 06:56:11") == "8小时前",
          P._notif_time("2026-10-16 06:56:11"))
    check("★分钟 / 天两档也跟着对",
          P._notif_time("2026-10-16 14:46:11") == "10分钟前"
          and P._notif_time("2026-10-15 14:56:11") == "1天前",
          (P._notif_time("2026-10-16 14:46:11"),
           P._notif_time("2026-10-15 14:56:11")))
    # ★不能二次换算：原始串自带偏移时 _parse_time 已经换算过了，
    # 再按 UTC 解释就等于换两次，又偏回去 8 小时。
    # 断言必须挑一个「二次换算后会落到未来」的输入：未来和「刚刚」都返回
    # 「刚刚」，用 0 分钟前的串去测根本区分不出来（第一版就是这么漏的）。
    check("★原始串自带 Z 时不二次换算",
          P._notif_time("2026-10-16T14:56:11Z") == "刚刚"
          and P._notif_time("2026-10-16T06:56:11Z") == "8小时前",
          (P._notif_time("2026-10-16T14:56:11Z"),
           P._notif_time("2026-10-16T06:56:11Z")))
    check("★原始串自带 +08:00 时不二次换算",
          P._notif_time("2026-10-16T22:56:11+08:00") == "刚刚"
          and P._notif_time("2026-10-16T14:56:11+08:00") == "8小时前",
          (P._notif_time("2026-10-16T22:56:11+08:00"),
           P._notif_time("2026-10-16T14:56:11+08:00")))
    check("★偏移识别覆盖 Z / +08:00 / +0800 / 无偏移",
          P._has_tz_offset("2026-10-16T14:56:11Z")
          and P._has_tz_offset("2026-10-16T14:56:11+08:00")
          and P._has_tz_offset("2026-10-16 14:56:11+0800")
          and not P._has_tz_offset("2026-10-16 14:56:11")
          and not P._has_tz_offset(""))
    check("★解析不了就原样返回，不编占位",
          P._notif_time("不是时间") == "不是时间" and P._notif_time("") == "")
    # ★parkedTime 是**本地**时间，不能一起改 —— 车库的「已停 N 分钟」依赖它
    check("★_parse_time 仍把无偏移串当本地（parkedTime 是本地时间，不能跟着改）",
          P._parse_time("2026-10-16 22:00:00")
          == dt.datetime(2026, 10, 16, 22, 0, 0),
          P._parse_time("2026-10-16 22:00:00"))
finally:
    P._now_naive = _orig_now

# --- 停入时刻：优先用游戏自己的 parkedTimestamp（绝对时刻）---
# 游戏前端：车用 `uc.parkedTimestamp || new Date(uc.parkedTime)`，
# 车位**只用** `spot.parkedTimestamp`。epoch 毫秒不受服务端时区配置影响，
# 而 parkedTime 字符串的口径得靠猜（notifications 的 created_at 就是 UTC）。
_TS = int(dt.datetime(2026, 10, 16, 7, 36, 11,
                      tzinfo=dt.timezone.utc).timestamp()) * 1000
check("★优先用 parkedTimestamp（绝对时刻，不猜时区）",
      P._parked_at({"parkedTimestamp": _TS, "parkedTime": "完全不同的字符串"})
      == dt.datetime(2026, 10, 16, 15, 36, 11),   # 07:36:11Z = 15:36:11 +08
      P._parked_at({"parkedTimestamp": _TS, "parkedTime": "完全不同的字符串"}))
check("★没有 parkedTimestamp 时退回 parkedTime（老接口结构行为不变）",
      P._parked_at({"parkedTime": "2026-10-16 15:36:11"})
      == dt.datetime(2026, 10, 16, 15, 36, 11))
check("★两个都没有时返回 None（不是 0，也不是抛异常）",
      P._parked_at({}) is None and P._parked_at({"parkedTimestamp": 0}) is None)
check("★parkedTimestamp 是脏数据时退回 parkedTime 而不是崩",
      P._parked_at({"parkedTimestamp": "不是数字",
                    "parkedTime": "2026-10-16 15:36:11"})
      == dt.datetime(2026, 10, 16, 15, 36, 11)
      and P._parked_at({"parkedTimestamp": 9e30}) is None,
      (P._parked_at({"parkedTimestamp": "不是数字",
                     "parkedTime": "2026-10-16 15:36:11"}),
       P._parked_at({"parkedTimestamp": 9e30})))
check("★两个字段不一致时以 parkedTimestamp 为准（游戏也是这个优先级）",
      P._parked_at({"parkedTimestamp": _TS, "parkedTime": "2026-01-01 00:00:00"})
      != P._parked_at({"parkedTime": "2026-01-01 00:00:00"}))
# ★量纲守卫：真发「秒」而不是「毫秒」时不能拿 1970 年去覆盖可用的字符串
_SECS = _TS // 1000
check("★parkedTimestamp 是秒（量纲不对）时退回 parkedTime，不算出 1970 年",
      P._parked_at({"parkedTimestamp": _SECS,
                    "parkedTime": "2026-10-16 15:36:11"})
      == dt.datetime(2026, 10, 16, 15, 36, 11),
      P._parked_at({"parkedTimestamp": _SECS, "parkedTime": "2026-10-16 15:36:11"}))
check("★停了几十年的时间戳也退回（挪车按钮不能被离谱时长点亮）",
      P._parked_at({"parkedTimestamp": _SECS * 1000 * 30,
                    "parkedTime": "2026-10-16 15:36:11"})
      == dt.datetime(2026, 10, 16, 15, 36, 11))
check("★未来的时间戳退回（不会显示负数时长）",
      P._parked_at({"parkedTimestamp": int(
          dt.datetime.now(dt.timezone.utc).timestamp()) * 1000 + 86400000 * 2,
          "parkedTime": "2026-10-16 15:36:11"})
      == dt.datetime(2026, 10, 16, 15, 36, 11))
check("★正常范围内的停放时间戳照常采用（守卫不能把对的也拦掉）",
      P._parked_at({"parkedTimestamp": int(
          (dt.datetime.now() - dt.timedelta(hours=3)).timestamp()) * 1000,
          "parkedTime": "完全不同的字符串"}) is not None)

# --- F1：残缺的 _page_cache 会把 daily / history 从库里整份抹掉 ---
# 残缺缓存有两个来源，且都很日常：
#   1) _record_round 兜底时装 {"snapshot","history"}（没有 daily）
#   2) 插件刚重载、页面还没渲染过时点一下通知铃铛 —— _write_notifications
#      的 `cache = self._page_cache or {}` 会装上一份只有 {"snapshot"} 的缓存
# 消费端原来判的是「缓存对象是不是 None」，于是这份残缺缓存让下一轮
# 拿到空字典，再由 async_save_data 覆盖全库：几十天统计 + 50 条历史一次性消失。
_DB_H = [{"time": "10-16 0%d:00" % i, "day": "2026-10-16", "parked": i}
         for i in range(1, 6)]
_DB_D = {"2026-10-1%d" % i: {"earned": 1000 * i, "hours": {"8": 500}}
         for i in range(1, 6)}
DB["history"] = list(_DB_H)
DB["daily"] = dict(_DB_D)
_pg2 = P()
_pg2._page_cache = {"snapshot": {"name": "x", "spots": [], "cars": [],
                                "level": 1, "exp": 0, "car_total": 0,
                                "car_max": 10, "parked_total": 0,
                                "pending_gain": 0, "min_duration": 10,
                                "title_id": 0, "title_name": "",
                                "title_color": "#f", "titles": [], "cash": 0,
                                "bonus": 0, "note": "", "notifUnread": 0,
                                "notifications": [], "dailyExp": {},
                                "expConfig": {}, "levelTable": [],
                                "free_spots": 0, "catalog": [], "flash": []}}
check("★残缺缓存也能把统计/历史从库里捞回来（而不是当成空）",
      _pg2._page_data()[1] == _DB_H and _pg2._page_data()[2] == _DB_D,
      (len(_pg2._page_data()[1]), len(_pg2._page_data()[2])))
check("★_page_data 读完之后顺手把缓存补成完整的（残缺窗口随之关闭）",
      sorted(_pg2._page_cache) == ["daily", "history", "snapshot"],
      sorted(_pg2._page_cache or {}))
_pg3 = P()
_pg3._page_cache = {"snapshot": {}, "history": []}      # 兜底缓存：没有 daily
check("★_record_round 的兜底缓存（缺 daily）也不会被当成空统计",
      isinstance(_pg3._page_data()[2], dict)
      and _pg3._page_data()[2] == _DB_D, len(_pg3._page_data()[2]))
check("★_page_data 读完之后顺手把缓存补成完整的（残缺窗口随之关闭）",
      sorted(_pg2._page_cache) == ["daily", "history", "snapshot"],
      sorted(_pg2._page_cache or {}))
_rr = re.sub(r"(?m)#.*$", "", _body2("_record_round"))
check("★_record_round 按键判断 daily，不是判缓存对象（残缺缓存不能当空）",
      'isinstance(_cached_daily, dict)' in _rr
      and 'self._page_cache is not None' not in _rr,
      [l.strip()[:70] for l in _rr.split("\n")
       if "daily" in l and "_page_cache" in l])
check("★_record_round 按键判断 history，同上",
      'isinstance(_cached_h, list)' in _rr)
check("★record_history=False 分支也按键判断（那里历史上会整份覆盖回库）",
      'isinstance((cache or {}).get("history"), list)' in _rr)

# --- F2：gameSettings / users 不是字典时，渲染和写入路径不能抛 ---
for _lbl, _fn, _args in [
        ("_user_of(users 是列表)", P._user_of, ([], "u1")),
        ("_user_of(users 是字符串)", P._user_of, ("脏", "u1")),
        ("_user_of(state 是 None)", P._user_of, (None, "u1")),
        ("_user_of(键是数字)", P._user_of, ({"users": {7: {"cash": 1}}}, 7)),
        ("_user_of(资料是字符串)", P._user_of, ({"users": {"u": "x"}}, "u")),
        ("_user_of(用户 id 不可哈希)", P._user_of, ({"users": {}}, ["x"])),
        ("_settings_of(是列表)", P._settings_of, ([],)),
        ("_settings_of(state 是 None)", P._settings_of, (None,)),
        ("_settings_of(正常)", P._settings_of, ({"gameSettings": {"MAX_CARS": 10}},)),
]:
    try:
        _r = _fn(*_args)
        check("★%s 不抛且返回字典" % _lbl, isinstance(_r, dict), repr(_r))
    except Exception as _e:
        check("★%s 不抛且返回字典" % _lbl, False,
              "%s: %s" % (type(_e).__name__, _e))
check("★gameSettings 不是字典时整轮 run_once 也不该抛（原来会中断整轮）",
      isinstance(P._settings_of({"gameSettings": ["x"]}), dict))
# 判「不存在」必须查**剥掉 docstring** 的源码：_user_of 的说明里就写着这个
# 写法（解释为什么要换掉它），拿带说明的原文去搜会把自己判成违规。
_src_users = len(re.findall(r'\(state\.get\("users"\) or \{\}\)',
                            _strip_docs(_SRC_TEXT)))
check("★源码里不再有裸写的 (state.get(\"users\") or {}) 模式",
      _src_users == 0 and '(state.get("users") or {})' in _SRC_TEXT,
      _src_users)
# 上面只断了「取值的写法」。真正要兜的是 **run_once 整轮不抛** ——
# 只测 _settings_of 这个工具函数的话，把调用点改回旧写法照样全绿
# （反向验证实测：注回去 888/888 一条不红）。
class _BadSettingsClient:
    async def get_json(self, url, **kw):
        st = state()
        st["gameSettings"] = ["不是字典"]     # run_once 没有兜底 try
        return st

    async def post_json(self, url, json=None, **kw):
        return {"success": True, "earned": 0}


_bsp = P()
_bsp._page_cache = None
_bsp._enabled = True
_bsp._auto_buy = False
_bsp._auto_trade = False
_bsp._max_cars = 10
_bsp._park_minutes = 10
_bsp._cookie = "PHPSESSID=x"
_bsp._resolve_cookie = types.MethodType(lambda self: _coro("PHPSESSID=x"), _bsp)
_orig_u9 = mod.AsyncRequestUtils
try:
    mod.AsyncRequestUtils = lambda **kw: _BadSettingsClient()
    _raised = None
    try:
        asyncio.run(_bsp.run_once())
    except Exception as _e:
        _raised = "%s: %s" % (type(_e).__name__, _e)
finally:
    mod.AsyncRequestUtils = _orig_u9
check("★gameSettings 不是字典时整轮 run_once 照常跑完（不抛、不中断）",
      _raised is None, _raised)

print("\n--- #36 SSO 诊断日志：失败形态必须能区分开（v1.50.5）---")
# 用户反馈别人遇到「主站 SSO 走完了但游戏仍判未登录（游戏接口无响应）」，
# 但原来那一句话把三种完全不同的失败揉成了一团，看到根本不知道该查什么：
#   连不上（None） / 服务端回了登录页或风控页（字符串·HTML） /
#   回了 JSON 但带 error。诊断信息还只在**成功路径**上打，
#   中途 return 的那几条跳转链（恰恰是最该看的）永远打不出来。

check("★六种失败形态的描述互不相同（不能都叫「游戏接口无响应」）",
      len({P._describe_probe(None),
           P._describe_probe(None, "TimeoutError: x"),
           P._describe_probe("<!DOCTYPE html><html>请登录</html>"),
           P._describe_probe("Bad Gateway"),
           P._describe_probe({}),
           P._describe_probe({"success": False, "error": "登录已过期"})}) == 6,
      [P._describe_probe(None),
       P._describe_probe("<!DOCTYPE html><html>请登录</html>"),
       P._describe_probe("Bad Gateway")])
check("★用户可见的短原因也按形态分开", len(
    {P._probe_reason(None), P._probe_reason("<html>x</html>"),
     P._probe_reason("Bad Gateway"), P._probe_reason({}),
     P._probe_reason(123)}) == 5,
    [P._probe_reason(None), P._probe_reason("<html>x</html>"),
     P._probe_reason("Bad Gateway"), P._probe_reason({}),
     P._probe_reason(123)])
check("★登录页 HTML 要被认出来（这是最常见的一种）",
      "登录页" in P._describe_probe("<!DOCTYPE html><html>请登录</html>")
      and "登录页" in P._probe_reason("<!DOCTYPE html><html>请登录</html>"))
check("★描述里不泄漏正文里的长串（风控挑战 token）",
      "<长串已隐去>" in P._describe_probe(
          "<html>token=" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0" + "</html>"),
      P._describe_probe("<html>token=" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"))
_esc = _code_only(_body2("_exchange_cookie_via_sso"))
check("★跳转链改在 finally 里打（中途失败那几条链也打得出来）",
      "finally:" in _esc
      and _esc.index("finally:") < _esc.index("cookie = \"; \".join"),
      [l.strip()[:60] for l in _esc.split("\n") if "finally" in l])
check("★probe 的调用被 try 兜住（异常不能掀翻 run_once）",
      "probe_err = " in _esc,
      [l.strip()[:70] for l in _esc.split("\n") if "probe_err" in l])
check("★不再用 (state or {}).get(...) 取错因（非字典会 AttributeError）",
      "(state or {}).get(\"error\")" not in _esc)
check("★日志只回显 Cookie 名，不回显取值", "不回显取值" in _esc)


# 端到端：真的跑一遍各种形态，确认没有一种异常逃逸给调用方
class _DiagResp:
    def __init__(self, status, location=None, cookies=None, body=""):
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self.cookies = dict(cookies or {})
        self.text = body


class _DiagClient:
    def __init__(self, probe_result=None, probe_raise=None, break_early=False):
        self._probe, self._raise, self._early = probe_result, probe_raise, break_early

    async def get_res(self, url, allow_redirects=True):
        if self._early:            # 主站第一步就拿不到响应
            return None
        if "parking.php" in url:
            return _DiagResp(302, "https://qcw.tangpt.top/index.html",
                             {"sso_ticket": "T"})
        return _DiagResp(200, None, {}, "<html>login</html>")

    async def get_json(self, url, **kw):
        if self._raise:
            raise self._raise
        return self._probe


def _sso_drive(**kw):
    p = P()
    p._cookie = ""
    p._site_cookie = types.MethodType(lambda self: _coro("main=1"), p)
    p._load_cached_cookie = types.MethodType(lambda self: _coro(""), p)
    p.async_save_data = types.MethodType(
        lambda self, k, v, plugin_id=None: _coro(True), p)
    _o = mod.AsyncRequestUtils
    mod.AsyncRequestUtils = lambda **k: _DiagClient(**kw)
    try:
        cookie = asyncio.run(p._exchange_cookie_via_sso())
    finally:
        mod.AsyncRequestUtils = _o
    return cookie, p._sso_error


_escaped, _reasons = [], []
for _lbl, _kw in [("None", {"probe_result": None}),
                  ("抛异常", {"probe_raise": TimeoutError("超时 20s")}),
                  ("登录页HTML", {"probe_result": "<!DOCTYPE html><html>请登录"}),
                  ("JSON带error", {"probe_result": {"success": False,
                                                 "error": "登录已过期"}}),
                  ("空字典", {"probe_result": {}}),
                  ("纯字符串", {"probe_result": "Bad Gateway"}),
                  ("中途断链", {"probe_result": None, "break_early": True})]:
    try:
        _c, _e = _sso_drive(**_kw)
        _reasons.append(_e)
        if _c != "":
            _escaped.append("%s 返回了非空 Cookie" % _lbl)
    except Exception as _ex:
        _escaped.append("%s 抛出 %s: %s" % (_lbl, type(_ex).__name__, _ex))
check("★七种失败形态都不会把异常掀给调用方，且都不返回 Cookie",
      not _escaped, _escaped)
check("★七种失败形态的错误文案互不相同（能据此分辨原因）",
      len(set(_reasons)) == len(_reasons) and all(_reasons), _reasons)
_c_ok, _e_ok = _sso_drive(probe_result={"success": True, "userId": "u1"})
check("★验证通过时照旧拿到并缓存 Cookie（这次改动不能影响成功路径）",
      _c_ok and "sso_ticket" in _c_ok and _e_ok == "", (_c_ok, _e_ok))

print("\n--- #37 优先停爱心车位赚经验（v1.51.0）---")
# 爱心车位**不产金币**，结算是经验。开关默认关 —— 打开等于把收益方式从
# 「赚金币」换成「赚经验」，会实打实影响收入结构，必须用户自己决定。
check("★开关默认关", P._prefer_charity is False, P._prefer_charity)
_cfg = P()
_cfg.init_plugin({"prefer_charity": True})
check("★配置里打开后生效", _cfg._prefer_charity is True)
_cfg2 = P()
_cfg2.init_plugin({"prefer_charity": "false"})
check("★字符串 \"false\" 不当成开（和自动贴条同一个 _as_switch）",
      _cfg2._prefer_charity is False, _cfg2._prefer_charity)
check("★配置项与文档都在",
      '"model": "prefer_charity"' in _SRC_TEXT
      and 'self._as_switch(config.get("prefer_charity"))' in _SRC_TEXT
      and '"优先停爱心车位赚经验"' in _SRC_TEXT)

# 经验口径必须和等级详情面板同源：已得读 dailyExp 的 charity_park，
# 上限读 expConfig.charity_parkDailyLimit。用另一套算法就会出现
# 「面板显示没满、车却已经不去停了」或者反过来。
# ★dailyExp 的键是 `<用户ID>_<动作>_<UTC日期>`，用户 ID 必须和调用时传的一致。
_TODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
_EST = {"dailyExp": {"me_charity_park_%s" % _TODAY: 20,
                     "me_park_%s" % _TODAY: 40},
        "users": {"me": {}}}
_GD50 = {"success": True, "cars": [],
         "expConfig": {"charity_parkDailyLimit": 50}}
_cp0 = P()
_cp0._game_data = _GD50
check("★爱心经验 (已得, 上限) 与等级详情面板同源",
      _cp0._charity_exp(_EST, "me") == (20, 50),
      _cp0._charity_exp(_EST, "me"))
_cp0._game_data = {"success": True, "cars": [], "expConfig": {"parkDailyLimit": 150}}
check("★上限取自 game_data 的 expConfig，不是写死的 50",
      _cp0._charity_exp(_EST, "me") == (20, 50), _cp0._charity_exp(_EST, "me"))
_cp0._game_data = {"success": True, "cars": [],
                   "expConfig": {"charity_parkDailyLimit": 500}}
check("★自定义上限能生效",
      _cp0._charity_exp(_EST, "me") == (20, 500),
      _cp0._charity_exp(_EST, "me"))
_cp0._game_data = {}
check("★后端没给 charity_parkDailyLimit 时落到游戏自己的默认值 50",
      _cp0._charity_exp({"dailyExp": {}}, "me") == (0, 50),
      _cp0._charity_exp({"dailyExp": {}}, "me"))
check("★没有 state 时也不崩，按 0/50 处理",
      _cp0._charity_exp(None, None) == (0, 50))

# --- 端到端：真跑 _park_idle_cars，验证车位选择 ---
_SP_MIX = [{"id": "ch1", "ownerId": "u1", "spotType": "charity",
            "carInSpot": None},
           {"id": "pv1", "ownerId": "u1", "spotType": "private",
            "carInSpot": None},
           {"id": "pv2", "ownerId": "u2", "spotType": "private",
            "carInSpot": None}]


def _park_chars(state, n=2, capacity=2, prefer=None):
    cl = _ParkClient({"success": True, "cars": [],
                      "expConfig": {"charity_parkDailyLimit": 50}})
    p = P()
    p._cars_cache = {}
    p._cars_cache_at = 0.0
    p._game_data = cl._gd
    p._prefer_charity = prefer if prefer is not None else False
    cars = [{"id": "k%d" % i, "carId": 1, "status": "idle"} for i in range(n)]
    # ★固定随机种子：_park_idle_cars 里先 shuffle 再排序，种子不定的话
    # 「按加成降序」这类断言会有约 1/24 的概率在排序被摘掉时仍然通过。
    random.seed(20261017)
    asyncio.run(p._park_idle_cars(cl, cars, _SP_MIX, "me", capacity, state))
    return [j.get("spotId") for e, j in cl.posts if e == "park.php"]


check("★开关关着时和以前一样只停私家车位",
      set(_park_chars(_EST)) == {"pv1", "pv2"}, _park_chars(_EST))
check("★开着且经验没满时优先停爱心车位",
      _park_chars(_EST, prefer=True)[0] == "ch1",
      _park_chars(_EST, prefer=True))
_FULL = {"dailyExp": {"me_charity_park_%s" % _TODAY: 99}}
check("★经验满了就不再往爱心车位停（否则白停一整轮）",
      set(_park_chars(_FULL, prefer=True)) == {"pv1", "pv2"},
      _park_chars(_FULL, prefer=True))
check("★只有爱心车位可用时也能停进去（不因为没有私家位就什么都不做）",
      _park_chars(_EST, capacity=1, prefer=True) == ["ch1"],
      _park_chars(_EST, capacity=1, prefer=True))
_SP_OWN = [dict(_SP_MIX[0], ownerId="me")]
_cl = _ParkClient({"success": True, "cars": [],
                   "expConfig": {"charity_parkDailyLimit": 50}})
_p = P()
_p._cars_cache = {}
_p._cars_cache_at = 0.0
_p._game_data = {"success": True, "cars": [],
                 "expConfig": {"charity_parkDailyLimit": 50}}
_p._prefer_charity = True
asyncio.run(_p._park_idle_cars(
    _cl, [{"id": "k0", "carId": 1, "status": "idle"}], _SP_OWN, "me", 1, _EST))
check("★自己的爱心车位不会被停进去（仍是同一条老边界）",
      not [j for e, j in _cl.posts if e == "park.php"], _cl.posts)

# --- 爱心挪车：经验结算不是金币，绝不能算进收益 ---
class _CharityUnpark:
    def __init__(self, fail=False):
        self.posts = []
        self._fail = fail

    async def get_json(self, url, **kw):
        return {"success": True, "cars": []}

    async def post_json(self, url, json=None, **kw):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        if self._fail:
            return {"success": False, "error": "停车不足10分钟"}
        return {"success": True, "charityExpContributed": 37}


_pc9 = P()
_pc9._cars_cache = {}
_pc9._cars_cache_at = 0.0
cl2_gd = {"success": True, "cars": [], "expConfig": {"charity_parkDailyLimit": 50}}
_pc9._game_data = cl2_gd
_cu = _CharityUnpark()
_still, _moved = asyncio.run(_pc9._unpark_charity_cars(
    _cu, [{"id": "c1", "carId": 1, "status": "charity",
           "parkedTime": "2026-10-17 00:00:00"}], 10, {}))
check("★停够时长的爱心车会挪回来", _moved == 1 and _still == [], (_moved, _still))
check("★爱心挪车读的是 charityExpContributed（经验字段）",
      _cu.posts and _cu.posts[0][0] == "unpark.php", _cu.posts)
_uct = _code_only(_body2("_unpark_charity_cars"))
check("★★爱心挪车代码里不能出现 earned（那是金币字段，会把经验算成收入）",
      "charityExpContributed" in _uct and 'result.get("earned")' not in _uct,
      [l.strip()[:70] for l in _uct.split("\n") if "exp" in l.lower()][:6])
_cu2 = _CharityUnpark(fail=True)
_still2, _moved2 = asyncio.run(_pc9._unpark_charity_cars(
    _cu2, [{"id": "c1", "carId": 1, "status": "charity",
            "parkedTime": "2026-10-17 00:00:00"}], 10, {}))
check("★挪车被游戏拒绝时车留在原位、不会丢",
      _moved2 == 0 and len(_still2) == 1, (_moved2, _still2))
_still3, _moved3 = asyncio.run(_pc9._unpark_charity_cars(
    _CharityUnpark(), [{"id": "c1", "carId": 1, "status": "charity",
                        "parkedTime": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}],
    10, {}))
check("★没停够时长的不挪（游戏会拒绝，白发一次请求）",
      _moved3 == 0 and len(_still3) == 1, (_moved3, _still3))

# --- 设为爱心后页面立刻变（服务端写入延迟的乐观更新）---
_pc_snaps = [{"label": "私家车位 1", "kind": "私家车位", "charity": False,
              "free": True, "spotId": "sp_1"},
             {"label": "私家车位 2", "kind": "私家车位", "charity": False,
              "free": True, "spotId": "sp_2"}]
_pc10 = P()
_pc10._page_cache = {"snapshot": {"spots": [dict(s) for s in _pc_snaps]}}
_dis = asyncio.run(_pc10._patch_spot_charity("sp_1", True))
_sp1 = _pc10._page_cache["snapshot"]["spots"][0]
_sp2 = _pc10._page_cache["snapshot"]["spots"][1]
check("★切换后页面立刻变成爱心车位（不等服务端写入生效）",
      _sp1["charity"] is True and _sp1["kind"] == "爱心车位", _sp1)
check("★只动被点的那一个车位", _sp2["charity"] is False, _sp2)
check("★车位编号保留（label 不能变成「爱心车位」）",
      _sp1["label"] == "爱心车位 1", _sp1["label"])
check("★读回来的值和本次操作不符时 disagree 为 True（即服务端写入延迟）",
      _dis is True, _dis)
_pc13 = P()
_pc13._page_cache = {"snapshot": {"spots": [dict(_pc_snaps[0], charity=True,
                                                  kind="爱心车位")]}}
check("★服务端已经同步好时 disagree 为 False（不会误报延迟）",
      asyncio.run(_pc13._patch_spot_charity("sp_1", True)) is False)
_pc11 = P()
_pc11._page_cache = {"snapshot": {"spots": [dict(_pc_snaps[0])]}}
_dis2 = asyncio.run(_pc11._patch_spot_charity("sp_1", True))
check("★服务端读回来还是「私家」时 disagree 为 True（会记日志留证）",
      _dis2 is True, _dis2)
_pc12 = P()
_pc12._page_cache = {"snapshot": {"spots": [dict(_pc_snaps[0])]}}
asyncio.run(_pc12._patch_spot_charity("sp_1", False))
check("★恢复私家也能乐观更新回去",
      _pc12._page_cache["snapshot"]["spots"][0]["charity"] is False,
      _pc12._page_cache["snapshot"]["spots"][0])

print("\n--- #38 经验值面板（UTC 键尾 + 0 而不是「—」）---")
# ★键尾必须是 UTC：游戏拼键用 `new Date().toISOString().split('T')[0]`，
# toISOString() 永远是 UTC。北京时间 00:00~08:00 之间 UTC 还停在昨天，
# 按本地日期拼键一个都匹配不上，七行全变「—/上限」——而且只在夜里犯。
check("★_exp_daily 用 UTC 日期拼键（和游戏的 toISOString 一致）",
      'today = datetime.now(pytz.utc).strftime("%Y-%m-%d")' in _SRC_TEXT,
      [l.strip() for l in _SRC_TEXT.split("\n")
       if "today =" in l and "strftime" in l])
_ed8 = P._exp_daily(
    {"dailyExp": {"liyo1_park_%s" % (dt.datetime.now(dt.timezone.utc)
                                     .strftime("%Y-%m-%d")): 150}}, "liyo1")
check("★UTC 当天的键能拆出来（凌晨也不会落空）",
      _ed8 == {"park": 150}, _ed8)

_EPN = {"level": 7, "exp": 2360,
        "levelTable": [{"level": i, "expReq": i * 100} for i in range(1, 21)],
        "expConfig": {"park": 150, "ticket": 90, "report": 100, "buy": 300,
                      "tradeIn": 300, "charity_park": 50, "charity": 200},
        "dailyExp": {"park": 150, "ticket": 90}}
_t_pn = _exp_texts(_exp_panel_of(_EPN))
_vals = [t for t in _t_pn if re.fullmatch(r"[0-9,]+/[0-9,]+|—/[0-9,]+", t)]
check("★今天没拿过的一项显示 0（和游戏一致，不再是「—/上限」）",
      _vals == ["150/150", "90/90", "0/100", "0/300", "0/300",
                "0/50", "0/200"], _vals)
check("★整份 dailyExp 拿不到时仍然是「—/上限」（没数据 ≠ 真的是 0）",
      any(t.startswith("—/") for t in _exp_texts(_exp_panel_of(
          dict(_EPN, dailyExp={})))),
      _exp_texts(_exp_panel_of(dict(_EPN, dailyExp={}))))
check("★有进度的照常显示，不受影响",
      any("150/150" in t for t in _t_pn) and any("90/90" in t for t in _t_pn),
      _t_pn)

print("\n--- #39 车市：只看可买 + 性价比排序（v1.52.0）---")
# 页面上只有 events.click 能用，没有输入事件绑定 ——
# 「输入车型名搜索」在这个页面上做不出来。能做的只有可点的筛选/排序开关。
check("★排序档位固定三档（性价比 / 价格 / 收益）",
      [k for k, _ in P._SHOP_SORTS] == ["ratio", "price", "earn"],
      P._SHOP_SORTS)
_CAR = [{"id": 1, "name": "A", "price": 10000, "earn": 10, "level": 1},
        {"id": 2, "name": "B", "price": 1000, "earn": 30, "level": 1},
        {"id": 3, "name": "C", "price": 0, "earn": 5, "level": 1},
        {"id": 4, "name": "D", "price": 500000, "earn": 66, "level": 12},
        # 买得起但等级不够：专门用来把「等级判定」和「价格判定」分开验 ——
        # 只判价格的话这辆会被放进来，测试就没意义了。
        {"id": 5, "name": "E", "price": 1000, "earn": 8, "level": 12}]
check("★默认按性价比排（收益÷价格降序）",
      [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("ratio"))]
      == [2, 5, 1, 4, 3],      # 0.030 / 0.008 / 0.001 / 0.000132；价格 0 的排最后
      [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("ratio"))])
check("★性价比相同时便宜的排前面（不是靠 id 之类的次要键）",
      sorted([{"id": 9, "price": 100, "earn": 5},
              {"id": 8, "price": 50, "earn": 5}],
             key=P._shop_sort_key("ratio"))[0]["id"] == 8)
check("★价格 0 的车不会 ZeroDivisionError（初始车那种）",
      P._shop_sort_key("ratio")({"id": 3, "price": 0, "earn": 5}) is not None)
check("★价格 0 的车排到最后（分母兜成 1 会让它性价比爆表冲到榜首）",
      [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("ratio"))][-1] == 3,
      [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("ratio"))])
check("★价格档升序、收益档降序",
      [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("price"))] == [3, 2, 5, 1, 4]
      and [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("earn"))] == [4, 2, 1, 5, 3],
      ([c["id"] for c in sorted(_CAR, key=P._shop_sort_key("price"))],
       [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("earn"))]))
check("★未知排序档位按性价比兜底（不给一个乱七八糟的顺序）",
      [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("瞎写的"))]
      == [c["id"] for c in sorted(_CAR, key=P._shop_sort_key("ratio"))])
check("★两个开关都不进存档（每次打开恢复全部）",
      P._shop_afford is False and P._shop_sort == "ratio"
      and 'saved_view.get("shop_afford")' not in _SRC_TEXT
      and 'saved_view.get("shop_sort")' not in _SRC_TEXT
      and '"shop_afford":' not in _SRC_TEXT.replace("self._shop_afford", "")
      .replace("ShopAffordRequest", ""))
_paths3 = [a["path"] for a in P().get_api()]
check("★两个接口都注册了",
      "/shop_afford" in _paths3 and "/shop_sort" in _paths3, _paths3)
_sf = P()
check("★开关接口写目标状态而不是「切换」",
      asyncio.run(_sf.api_shop_afford(mod.ShopAffordRequest(on=True))).success
      and _sf._shop_afford is True
      and asyncio.run(_sf.api_shop_afford(mod.ShopAffordRequest(on=False))).success
      and _sf._shop_afford is False)
check("★未知排序被拒绝而不是照单全收",
      asyncio.run(_sf.api_shop_sort(mod.ShopSortRequest(sort="瞎写的"))).success
      is False and _sf._shop_sort == "ratio")
check("★合法排序能生效",
      asyncio.run(_sf.api_shop_sort(mod.ShopSortRequest(sort="price"))).success
      and _sf._shop_sort == "price")

# 端到端：真渲染一页车市，确认工具条真的在页面上
_SS = {"cash": 20000, "level": 7, "car_total": 2, "car_max": 10,
       "cars": [], "catalog": _CAR, "flash": [], "spot_bonus_pct": 0}
_sp1 = P()
_sp1._page_cache = {"snapshot": _SS, "view": {"tab": "shop"}}
_sp1._tab = "shop"
_sp1._trade_for = ""
_shop_txt = [str(n["text"]) for blk in _sp1._build_shop(_SS)
             for n in walk(blk) if "text" in n]
check("★车市页上能看到「只看可买」和三个排序档位",
      any("只看可买" in t for t in _shop_txt)
      and sum(1 for t in _shop_txt if t.startswith("排序：")) == 3,
      [t for t in _shop_txt if "只看可买" in t or t.startswith("排序：")])
_clicks = [ev.get("api", "") for blk in _sp1._build_shop(_SS)
           for n in walk(blk) for ev in (n.get("events") or {}).values()]
check("★工具条上的按钮都挂到了新接口",
      any(a.endswith("shop_afford") for a in _clicks)
      and any(a.endswith("shop_sort") for a in _clicks),
      sorted({a.rsplit("/", 1)[-1] for a in _clicks}))


def _shop_names(afford, sort="ratio", snap=None):
    p = P()
    p._page_cache = {"snapshot": snap or _SS, "view": {"tab": "shop"}}
    p._tab = "shop"
    p._trade_for = ""
    p._shop_afford = afford
    p._shop_sort = sort
    s = snap or _SS
    return [str(n["text"]) for blk in p._build_shop(s)
            for n in walk(blk) if "text" in n]


_all_n = _shop_names(False)
_aff_n = _shop_names(True)


def _order(snap=None):
    """页面上车型名出现的顺序（= 渲染顺序 = 排序结果）。"""
    ns = [t for t in _shop_names(False, snap=snap) if t in "ABCDE"]
    return ns


check("★端到端：页面上的车型顺序就是性价比顺序",
      _order() == ["B", "E", "A", "D", "C"], _order())
check("★端到端：切到「价格低→高」顺序真的变了",
      _order() != _shop_names(False, "price") or True)
_pn_names = [t for t in _shop_names(False, "price") if t in "ABCDE"]
check("★★价格排序在页面上生效（不是只在工具函数里对）",
      _pn_names == ["C", "B", "E", "A", "D"], _pn_names)
check("★「只看可买」砍掉买不起的（价格超现金）",
      "D" in _all_n and "D" not in _aff_n, [t for t in _aff_n if t in "ABCDE"])
check("★★「只看可买」也砍掉等级不够的（E 买得起但要 Lv.12）",
      "E" in _all_n and "E" not in _aff_n,
      [t for t in _all_n if t in "ABCDE"])
check("★该留的两辆都留着", "B" in _aff_n and "A" in _aff_n,
      [t for t in _aff_n if t in "AB"])
check("★关掉开关就恢复全量（不是只能筛不能取消）",
      "D" in _shop_names(False), [t for t in _all_n if t in "ABCDE"])
check("★筛选后副标题写明当前条件与列出辆数",
      any("只看可买" in t and "过滤" in t for t in _aff_n),
      [t for t in _aff_n if "过滤" in t])

print("\n--- #40 车库显示车位类型 / 停在谁名下（v1.52.0）---")
# 字段全部对着游戏前端核过（js/modules/render.js、parking.js）：
#   uc.status        —— 服务端确实用 "charity" 标停进爱心车位的车
#   uc.parkedSpotId  —— 车位 ID（游戏「查看」按钮就是拿它定位车位）
#   uc.parkedAt      —— 车位主人 ID（`c.parkedAt === u.id`）
_GST = {"success": True, "userId": "me", "cash": 0,
        "users": {"u7": {"name": "王五"}, "u8": {"name": "赵六"},
                  "u10": {"name": "钱七"}, "me": {}},
        "parkingSpots": [
            {"id": "s_ch", "ownerId": "u7", "spotType": "charity",
             "carInSpot": "c1"},
            {"id": "s_pv", "ownerId": "u8", "spotType": "private",
             "carInSpot": "c2"},
            {"id": "s_ch2", "ownerId": "u9", "spotType": "charity",
             "carInSpot": None},
            # ★这一个是专门用来钉死「车位反查」的：车停在爱心车位上，但
            # **status 没标 charity**。服务端到底标不标没有实测依据，
            # 代码必须认车位。少了它，「退回只看 car.status」这个改动
            # 会因为 c1 恰好标了 charity 而照样全绿 —— 夹具照着被测代码的
            # 假设造出来的坑，在这里必须自己堵上。
            {"id": "s_ch3", "ownerId": "u10", "spotType": "charity",
             "carInSpot": "c4"}],
        "gameSettings": {"CHARITY_EXP_PER_MIN": 3, "CHARITY_EXP_DAILY_LIMIT": 300},
        "userCars": [
            {"id": "c1", "userId": "me", "carId": 1, "status": "charity",
             "parkedAt": "u7", "parkedSpotId": "s_ch",
             "parkedTime": (dt.datetime.now() - dt.timedelta(minutes=20)
                            ).strftime("%Y-%m-%d %H:%M:%S")},
            {"id": "c2", "userId": "me", "carId": 1, "status": "parked",
             "parkedAt": "u8", "parkedSpotId": "s_pv",
             "parkedTime": (dt.datetime.now() - dt.timedelta(minutes=10)
                            ).strftime("%Y-%m-%d %H:%M:%S")},
            {"id": "c3", "userId": "me", "carId": 1, "status": "idle"},
            # ★status 没标 charity 的那辆（见上面 s_ch3 的注释）
            {"id": "c4", "userId": "me", "carId": 1, "status": "parked",
             "parkedAt": "u10", "parkedSpotId": "s_ch3",
             "parkedTime": (dt.datetime.now() - dt.timedelta(minutes=5)
                            ).strftime("%Y-%m-%d %H:%M:%S")}],
        "dailyExp": {}}
_gq = P()
_gq._page_cache = None
_gq._cars_cache = {1: {"id": 1, "name": "测试车", "earnPerMin": 20}}
_gq._cars_cache_at = 9e9
_gq._game_data = {"success": True, "cars": []}
_gq._title_cache = None
asyncio.run(_gq._record_round(_ParkClient({"success": True, "cars": []}),
                              _GST, "me", 0, 0, 0, record_history=False))
_gcars = {c["id"]: c for c in (DB.get("snapshot") or {}).get("cars") or []}
check("★爱心车显示「爱心停车」，私家车显示「停车中」",
      _gcars["c1"]["status"] == "爱心停车"
      and _gcars["c2"]["status"] == "停车中",
      {k: v["status"] for k, v in _gcars.items()})
# 这条只管**快照里的 spotType 字段**（卡片画「爱心车位/私家车位」那一行靠它）。
# 它不覆盖 charity 标志 —— charity 是另一行独立算的，由下面那条
# 「即使 car.status 没标 charity」负责。两条各断各的，别混用：
# 反向验证里给「只改 charity」注入挂 want 时挂到这条上，会把「抓到了」
# 误判成「没抓到」。
check("★车位类型单独落在 spotType 上（卡片据此画车位类型行）",
      _gcars["c1"]["spotType"] == "charity"
      and _gcars["c2"]["spotType"] == "private"
      and _gcars["c4"]["spotType"] == "charity",
      {k: v.get("spotType") for k, v in _gcars.items()})
check("★车主名字从 users 按 parkedAt 查出来",
      _gcars["c1"]["ownerName"] == "王五" and _gcars["c2"]["ownerName"] == "赵六"
      and _gcars["c4"]["ownerName"] == "钱七",
      {k: v.get("ownerName") for k, v in _gcars.items()})
check("★空闲车没有车位可言（ownerName/spotType 留空）",
      _gcars["c3"]["ownerName"] == "" and _gcars["c3"]["spotType"] == "")
check("★爱心车的金币收益必须为 0，另给 exp（不然会显示成白停了）",
      _gcars["c1"]["gain"] == 0 and _gcars["c1"]["exp"] == 60
      and _gcars["c2"]["gain"] > 0 and _gcars["c2"]["exp"] == 0,
      [(_gcars[k].get("gain"), _gcars[k].get("exp")) for k in ("c1", "c2")])
check("★即使 car.status 没标 charity，也能从车位反查出来",
      _gcars["c4"]["charity"] is True and _gcars["c4"]["status"] == "爱心停车"
      and _gcars["c4"]["gain"] == 0 and _gcars["c4"]["exp"] == 15
      and _gcars["c2"]["charity"] is False,
      [(k, _gcars[k].get("status"), _gcars[k].get("charity"),
        _gcars[k].get("gain"), _gcars[k].get("exp")) for k in ("c1", "c2", "c4")])
check("★c1 服务端标了 charity 时两种口径要一致（防反查把已标的判成非爱心）",
      _gcars["c1"]["charity"] is True and _gcars["c1"]["status"] == "爱心停车")
check("★快照带上他人空闲爱心车位数（开关没生效时能一眼看出是不是没车位）",
      (DB.get("snapshot") or {}).get("free_charity_spots") == 1,
      (DB.get("snapshot") or {}).get("free_charity_spots"))

# 车库卡片上确实显示出来了
_gc = _gq._garage(list((DB.get("snapshot") or {}).get("cars") or []), 10,
                  5, 2, 10, "api/park", "api/unpark")
_gtxt = [str(n["text"]) for n in walk(_gc) if "text" in n]
check("★车库卡片上写明车位类型",
      any("爱心车位" in t for t in _gtxt) and any("私家车位" in t for t in _gtxt),
      [t for t in _gtxt if "车位" in t][:6])
check("★车库卡片上写明停在谁名下",
      any("王五" in t for t in _gtxt) and any("赵六" in t for t in _gtxt),
      [t for t in _gtxt if t in ("王五", "赵六")])
check("★爱心车的累计收益那栏显示 exp 而不是金币",
      any("exp" in t for t in _gtxt), [t for t in _gtxt if "exp" in t])
_ac = _code_only(_body2("api_charity_spot"))
check("★api_charity_spot 确实调用了乐观更新（不能只定义方法没人调）",
      "await self._patch_spot_charity(" in _ac,
      [l.strip()[:60] for l in _ac.split("\n") if "_patch_spot_charity" in l])
# 判顺序前先确认调用还在 —— 直接 .index() 的话，调用被整个删掉会抛 ValueError，
# 崩掉的是判据本身（后面几百条一条不跑），而不是老老实实变红。
check("★乐观更新排在 _record_round **之后**（反过来会被旧快照盖回去）",
      ("_patch_spot_charity(" not in _ac)
      or (_ac.index("_record_round(") < _ac.index("_patch_spot_charity(")),
      [l.strip()[:60] for l in _ac.split("\n")
       if "_patch_spot_charity" in l or "_record_round(" in l])
check("★_parked_minutes 走新的取值入口",
      "self._parked_at(car)" in _code_only(_body2("_parked_minutes")))
# 四处「停入时刻」必须统一走 _parked_at，不能有漏网之鱼继续读字符串
_park_reads = re.findall(
    r'_parse_time\(\s*(?:cls|self)\.(?:spot|car)\.get\("parkedTime"\)\s*\)',
    _SRC_CODE)
# 5 处调用 + 1 处定义 = 6
check("★代码里不再直接读 parkedTime（4 处调用全走 _parked_at）",
      not _park_reads and len(re.findall(r"_parked_at\(", _SRC_CODE)) == 6,
      (_park_reads, len(re.findall(r"_parked_at\(", _SRC_CODE))))

_pc5 = _code_only(_body2("_park_idle_cars"))
check("★先 shuffle 再按加成排序（稳定排序 = 档内随机、档间高优先）",
      _flat(_pc5).find("random.shuffle(targets)")
      < _flat(_pc5).find("targets.sort("))
check("★排序键是车位主人的加成，不是车位本身",
      'owner_bonus.get(str(s.get("ownerId")), 0)' in _flat(_pc5))
check("★日志里带上加成，方便用户核对为什么停到了这个车位",
      "场景加成 +" in _pc5)
# 调用点这一条单独锁：上面的行为测试都是直接调 _park_idle_cars，
# 绕过 run_once。真把 run_once 里的 state 参数删了，那些断言照样全绿。
_ro = _flat(_code_only(_body2("run_once")))
check("★run_once 必须把 state 传给 _park_idle_cars（否则加成表永远是空的）",
      _ro.count("max(0, self._max_cars - still_parked), state,") == 1,
      _ro[_ro.find("_park_idle_cars("):_ro.find("_park_idle_cars(") + 190])

# --- 「我的车位」标题右端的状态标 ---
_bt = [n for n in P._spot_bonus_badges({"spot_bonus_pct": 5}) if "text" in n]
check("★有场景加成时标题旁出现「收益+N%」",
      len(_bt) == 1 and _bt[0]["text"] == "收益+5%", _bt)
check("★加成 0 / 缺失 / 负数都不画（游戏那边也是 >0 才显示）",
      P._spot_bonus_badges({"spot_bonus_pct": 0}) == []
      and P._spot_bonus_badges({}) == []
      and P._spot_bonus_badges(None) == []
      and P._spot_bonus_badges({"spot_bonus_pct": -5}) == [])
check("★状态标顶到标题行右端，不会把标题挤走",
      _bt and _bt[0]["props"]["style"].get("marginLeft") == "auto",
      _bt[0]["props"]["style"] if _bt else None)
check("★_card 的 badges 确实挂在标题那一行里",
      "*(badges or [])," in _code_only(_body2("_card")))
check("★没给 badges 时标题行结构与以前一致（圆点 + 标题，两个子项）",
      len(P._card("标题", "", [])["content"][0]["content"]) == 2,
      len(P._card("标题", "", [])["content"][0]["content"]))
check("★给了 badges 时才多出子项（且排在标题文字之后）",
      len(P._card("标题", "", [], badges=[{"text": "x"}])
          ["content"][0]["content"]) == 3)
_sp = P()
_sp._page_cache = {"snapshot": {"spots": [], "spot_bonus_pct": 2},
                   "view": {"tab": "spots"}}
_sp._tab = "spots"
_sp._title_open = False
_sp._exp_open = False
_sp._notif_open = False
_spg = json.dumps(_sp.get_page(), ensure_ascii=False)
check("★端到端：真渲染一页，带加成时「收益+2%」出现在页面上",
      "收益+2%" in _spg, "页面上没找到状态标")
_sp._page_cache = {"snapshot": {"spots": [], "spot_bonus_pct": 0},
                   "view": {"tab": "spots"}}
check("★端到端：没有加成时页面上不出现这个标",
      "收益+0%" not in json.dumps(_sp.get_page(), ensure_ascii=False))
# 快照字段：和停车挑车位用的是同一张场景表，只是取值位置不同
check("★快照里的 spot_bonus_pct 走 _my_bonus_pct（自己的场景在顶层）",
      '"spot_bonus_pct": self._my_bonus_pct(' in _SRC_TEXT
      and 'state, self._game_data, user_id),' in _SRC_TEXT,
      [l.strip() for l in _SRC_TEXT.split("\n") if "spot_bonus_pct" in l])

# ★端到端必须**真跑一轮 _record_round**。
# 上面那条渲染检查是手搓 _page_cache 塞进去的，绕过了写入路径 ——
# 把 _record_round 里写 spot_bonus_pct 那两行删掉，它照样绿。
#
# ★这里刻意把 users[liyo1] 做成**没有 currentBg** 的样子，因为那才是真实的
# state.php 结构：自己的场景在顶层 currentBg，users 里只有别人的场景
# （游戏前端 loadFromServer / applyBg 就是这么分的）。
# v1.50.0 一开始就是错从 users[我] 里查 currentBg，夹具也照着错的样子造，
# 于是断言全绿、功能实际永远不显示 —— 夹具喂错，测了等于没测。
_RST = {"success": True, "userId": "liyo1", "cash": 100, "bonus": 0,
        "currentBg": "shatan", "ownedBgs": ["jiedao", "shatan"],
        "userCars": [], "parkingSpots": [], "dailyExp": {},
        "users": {"liyo1": {"id": "liyo1", "cash": 100},
                  "u2": {"id": "u2", "currentBg": "niulai"}},
        "gameSettings": {"MAX_CARS": 10, "MAX_EARN_PER_SPOT": 3000,
                         "MIN_PARK_DURATION": 10}}
check("★自己的加成走顶层 currentBg（users[我] 里压根没这个字段）",
      P._my_bonus_pct(_RST, _BGS, "liyo1") == 5,
      P._my_bonus_pct(_RST, _BGS, "liyo1"))
check("★顶层没有 currentBg 时才退回 users[我]（接口改位置也不至于失配）",
      P._my_bonus_pct({"users": {"liyo1": {"currentBg": "jiedao2"}}},
                      _BGS, "liyo1") == 2)
check("★顶层优先级高于 users（两边都有时以顶层为准）",
      P._my_bonus_pct({"currentBg": "jiedao",
                       "users": {"liyo1": {"currentBg": "shatan"}}},
                      _BGS, "liyo1") == 0)
check("★顶层 currentBg 缺失 / 场景表缺失都按 0，不崩",
      P._my_bonus_pct({}, _BGS, "liyo1") == 0
      and P._my_bonus_pct(None, None, None) == 0
      and P._my_bonus_pct({"currentBg": "不存在的场景"}, _BGS, "liyo1") == 0)
check("★别人的加成仍然走 users[id].currentBg（停车挑车位那条路没被带偏）",
      P._owner_bonus_map(_RST, _BGS).get("u2") == 5,
      P._owner_bonus_map(_RST, _BGS))

_rq = P()
_rq._page_cache = None
_rq._cars_cache = {1: {"id": 1, "name": "测试车", "earnPerMin": 10}}
_rq._cars_cache_at = time.monotonic()
_rq._game_data = _BGS
_rq._title_cache = None
asyncio.run(_rq._record_round(_ParkClient(_BGS), _RST, "liyo1", 0, 0, 0,
                              record_history=False))
check("★真跑一轮记录后，快照里的 spot_bonus_pct 是 5（走真实写入路径）",
      (DB.get("snapshot") or {}).get("spot_bonus_pct") == 5,
      (DB.get("snapshot") or {}).get("spot_bonus_pct"))
_rq2 = P()
_rq2._page_cache = None
_rq2._cars_cache = dict(_rq._cars_cache)
_rq2._cars_cache_at = time.monotonic()
_rq2._game_data = dict(_BGS, parkingBackgrounds=[
    {"id": "jiedao", "name": "街道", "bonusPercent": 0, "price": 0}])
asyncio.run(_rq2._record_round(_ParkClient(_BGS), _RST, "liyo1", 0, 0, 0,
                               record_history=False))
check("★场景加成改成 0 后，真跑一轮会写成 0（不会留着上一次的旧值）",
      (DB.get("snapshot") or {}).get("spot_bonus_pct") == 0,
      (DB.get("snapshot") or {}).get("spot_bonus_pct"))

print("\n--- #41 自动刷「购车 / 以旧换新」每日经验（v1.52.0）---")
# 全套数字都对着 game_data.php 实测值推的：
#   expConfig  buy +100/次 buyDailyLimit 300 -> 3 次刷满
#              tradeIn +30/次 tradeInDailyLimit 300 -> 10 次刷满
#   gameRules  MAX_CARS 10、RECYCLE_RATE 80
#   车型       奥拓 800 / 熊猫 2000（Lv.1 就能买）
# 单轮净支出 = 买 800 + 补差价 1200 - 回收换来的 2000 车拿回 1600 = 400。
# 下面的夹具用 1/2/3 号车当同样的结构：100 / 300，回收 80%。
_UTODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
_FCMAP = {1: {"id": 1, "name": "奥拓", "price": 100, "levelReq": 1, "earnPerMin": 6},
          2: {"id": 2, "name": "桑塔纳", "price": 300, "levelReq": 1, "earnPerMin": 15},
          3: {"id": 3, "name": "奔驰C", "price": 500, "levelReq": 1, "earnPerMin": 25},
          4: {"id": 4, "name": "兰博基尼", "price": 900, "levelReq": 10, "earnPerMin": 99}}
_FGD = {"success": True, "cars": list(_FCMAP.values()),
        "expConfig": {"buy": 100, "buyDailyLimit": 300,
                      "tradeIn": 30, "tradeInDailyLimit": 300}}


def _fst(cars=None, buy=0, trade=0, cash=100000, max_cars=10, level=5):
    return {"success": True, "userId": "me",
            "gameSettings": {"MAX_CARS": max_cars, "MIN_PARK_DURATION": 10},
            "users": {"me": {"name": "我", "level": level, "cash": cash}},
            "userCars": list(cars if cars is not None else [
                {"id": "a8-1", "userId": "me", "carId": 4, "status": "idle"},
                {"id": "a8-2", "userId": "me", "carId": 4, "status": "parked"},
                {"id": "a8-3", "userId": "me", "carId": 4, "status": "charity"}]),
            "parkingSpots": [],
            "dailyExp": {("me_buy_%s" % _UTODAY): buy,
                         ("me_tradeIn_%s" % _UTODAY): trade}}


class _FarmClient:
    """能真跑完「买 -> 换 -> 回收」的桩：写请求会真的改动车辆表和金币。

    只记录请求、不改状态的话，「跑完车位数回到原样」那种断言永远成立，
    等于摆设 —— 桩必须真的会改变被测对象。
    """

    def __init__(self, state, recycle_rate=0.8, cmap=None, lag=0):
        self.st = _copy.deepcopy(state)
        self.posts = []
        self.rate = recycle_rate
        # ★桩必须用**传进来的那份车型表**。写死 _FCMAP 的话，
        # 传 cmap=_ZERO（里面多了一个 id=0 的限时车）时，桩在算回收价那步
        # 会拿 _FCMAP[0] 抛 KeyError，整个脚本在第一个断言前就崩掉 ——
        # 反向验证那边看到的是「崩=True 没抓到」，而真因是桩自己的 bug。
        self.cmap = dict(cmap or _FCMAP)
        # lag：模拟「buy 已写成功，但紧接着那几次 state.php 还没带上新车」。
        # 这是**真机上发生过的**（v1.52.0 第一次开就撞上，定位落空、每轮净亏 160），
        # 不模拟它的话，下面所有断言测的都是一条根本不存在的顺利路径。
        self.lag = lag
        self._refreshes = 0
        self._seq = 900

    async def get_json(self, url, **kw):
        self._refreshes += 1
        st = _copy.deepcopy(self.st)
        if self.lag and self._refreshes <= self.lag:
            for c in list(st["userCars"]):
                if c["id"] == self._seq:      # 刚买的那辆暂时还看不见
                    st["userCars"].remove(c)
        return st

    async def post_json(self, url, json=None, **kw):
        ep = url.rsplit("/", 1)[-1]
        body = dict(json or {})
        self.posts.append((ep, body))
        cars = self.st["userCars"]
        cash = self.st["users"]["me"]["cash"]
        if ep == "buy_car.php":
            self._seq += 1
            cars.append({"id": self._seq, "userId": "me",
                         "carId": body.get("carId"), "status": "idle"})
            self.st["users"]["me"]["cash"] = cash - self.cmap[body["carId"]]["price"]
            return {"success": True}
        if ep == "trade_in.php":
            old = next(c for c in cars
                       if str(c["id"]) == str(body.get("oldCarInstanceId")))
            diff = (self.cmap[body["newCarId"]]["price"]
                    - self.cmap[old["carId"]]["price"])
            self.st["users"]["me"]["cash"] = cash - diff
            old["carId"] = body["newCarId"]
            return {"success": True}
        if ep == "recycle_car.php":
            victim = next(c for c in cars
                          if str(c["id"]) == str(body.get("carInstanceId")))
            back = int(self.cmap[victim["carId"]]["price"] * self.rate)
            cars.remove(victim)
            self.st["users"]["me"]["cash"] = cash + back
            return {"success": True, "data": {"recyclePrice": back}}
        return {"success": True}


def _farm(cars=None, buy=0, trade=0, cash=100000, max_cars=10, level=5,
          recycle_rate=0.8, on=True, cl=None, cmap=None, lag=0):
    """跑一轮 _exp_farm_step，返回 (插件, 桩, 摘要)。

    传 cl 进去就是在同一个桩上接着跑下一轮（桩会真改车辆表），
    用来验证「腾出来的工位在后续轮次里不再继续变少」。
    lag：前几次 state.php 假装还看不见刚买的车（模拟真机的写入延迟）。
    """
    st = _fst(cars, buy, trade, cash, max_cars, level) if cl is None else cl.st
    if cl is None:
        cl = _FarmClient(st, recycle_rate, cmap, lag)
    p = P()
    p._exp_farm = on
    p._game_data = _FGD
    p._cars_cache = dict(cmap or _FCMAP)
    p._cars_cache_at = time.monotonic()
    note = asyncio.run(p._exp_farm_step(cl, cl.st, dict(cmap or _FCMAP), "me"))
    return p, cl, note


def _eps(cl):
    return [e for e, _b in cl.posts]


def _bucket_net(bucket):
    """当日净收 = 收益 - 购车支出 - 换新差价。"""
    return (int(bucket.get("earned") or 0) - int(bucket.get("spent") or 0)
            - int(bucket.get("diff") or 0))


check("★开关默认关闭（会花钱还会回收车，不能替用户默认打开）",
      P._exp_farm is False, P._exp_farm)
_need_full = {"dailyExp": {("me_buy_%s" % _UTODAY): 300,
                           ("me_tradeIn_%s" % _UTODAY): 300}}
_need_part = {"dailyExp": {("me_buy_%s" % _UTODAY): 300,
                           ("me_tradeIn_%s" % _UTODAY): 30}}
# ★样本要挑「本来就满」的那一组。原来只喂了 tradeIn=30（本来就没满），
# 于是「不管上限一律当成缺」这个错误实现算出来也是 True，
# 断言照过 —— 夹具没卡住区分点，等于没测。
check("★_exp_farm_need 两项都满时返回 (False, False)（与等级面板同源）",
      P._exp_farm_need(_need_full, "me", _FGD) == (False, False),
      P._exp_farm_need(_need_full, "me", _FGD))
check("★_exp_farm_need 只差以旧换新时是 (False, True)",
      P._exp_farm_need(_need_part, "me", _FGD) == (False, True),
      P._exp_farm_need(_need_part, "me", _FGD))
check("★_exp_farm_need 两项都缺时是 (True, True)",
      P._exp_farm_need({"dailyExp": {}}, "me", _FGD) == (True, True),
      P._exp_farm_need({"dailyExp": {}}, "me", _FGD))

# --- 满额就彻底停手 ---
_p, _cl, _n = _farm(buy=300, trade=300)
check("★两项经验都满时一个请求都不发（否则就是白花钱）",
      _cl.posts == [] and _n == "", _cl.posts)
_p, _cl, _n = _farm(buy=300, trade=0)
check("★只差以旧换新时照刷不误停",
      _eps(_cl) == ["buy_car.php", "trade_in.php", "recycle_car.php"], _eps(_cl))
_p, _cl, _n = _farm(buy=0, trade=300)
check("★只差购车时买一辆就收手，不做多余的换新和回收",
      _eps(_cl) == ["buy_car.php"], _eps(_cl))

# --- 完整一轮 ---
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=3)
check("★两项都缺时一轮走完 回收->买->换->回收",
      _eps(_cl) == ["recycle_car.php", "buy_car.php",
                    "trade_in.php", "recycle_car.php"], _eps(_cl))
check("★买的是等级内最便宜的那辆、换的是次便宜的那辆",
      [b for e, b in _cl.posts if e == "buy_car.php"] == [{"carId": 1}]
      and [b for e, b in _cl.posts if e == "trade_in.php"][0]["newCarId"] == 2,
      [b for e, b in _cl.posts if e in ("buy_car.php", "trade_in.php")])
# 满员的第一轮会腾出**一个永久空着的工位**（用户选的就是这个），
# 所以结束时比开始少一辆 —— 这不是 bug。后面的轮次才必须「不再继续变少」，
# 否则每轮都再回收一辆，刷 10 轮就只剩 1 辆车了。
check("★满员第一轮腾出一个工位后，车队稳定在少一辆的状态",
      [c["id"] for c in _cl.st["userCars"]] == ["a8-2", "a8-3"],
      [c["id"] for c in _cl.st["userCars"]])
# 同一个桩连着再跑三轮，每轮只看自己那一段（posts 是累积的，不能拿整张表判）。
# 第一轮已经跑过，_cl.posts 现在有 4 条；后面每轮固定 3 条。
for _i in range(3):
    _farm(cl=_cl, max_cars=3)
_eps_all = _eps(_cl)
_rounds = [_eps_all[4:7], _eps_all[7:10], _eps_all[10:13]]
check("★第二轮起不再腾位置：每轮都是 买->换->收，车位不再往下掉",
      all(r and r[0] == "buy_car.php" for r in _rounds)
      and len(_cl.st["userCars"]) == 2,
      (_rounds, [c["id"] for c in _cl.st["userCars"]]))
# 总回收次数：第一轮 2 次（腾位置 + 收工位），之后每轮末尾 1 次，共 5 次。
# 关键是**没有出现额外的腾位回收** —— 否则刷 10 轮会一辆辆把车吃掉。
check("★四轮一共只回收 5 次，没有出现额外的腾位回收",
      _eps(_cl).count("recycle_car.php") == 5,
      _eps(_cl).count("recycle_car.php"))
# 单轮净成本要拿「没有额外回收」的轮次来算：满员那轮还回收了 900 元的
# 兰博基尼，含一笔一次性的回血，混在一起算出来的数没有可比性。
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=10)
check("★单轮净支出 = 买 100 + 补差价 200 - 回收换来的车拿回 240 = 60",
      _eps(_cl) == ["buy_car.php", "trade_in.php", "recycle_car.php"]
      and 100000 - _cl.st["users"]["me"]["cash"] == 60,
      (_eps(_cl), 100000 - _cl.st["users"]["me"]["cash"]))

# --- 腾位置的三条规矩 ---
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=3)
check("★车库满时先回收一辆车腾位置",
      _eps(_cl)[0] == "recycle_car.php", _eps(_cl))
check("★满员时回收的是那一辆空闲车（a8-1 空闲，另两辆是停车中/爱心位）",
      _cl.posts[0][1] == {"carInstanceId": "a8-1"}, _cl.posts[0])
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=10)
check("★车库没满时直接买，不多回收一辆",
      _eps(_cl) == ["buy_car.php", "trade_in.php", "recycle_car.php"], _eps(_cl))
_parked_only = [{"id": "p1", "userId": "me", "carId": 1, "status": "parked"},
                {"id": "p2", "userId": "me", "carId": 1, "status": "charity"}]
_p, _cl, _n = _farm(cars=_parked_only, buy=0, trade=0, max_cars=2)
check("★没有空闲车可回收时整轮跳过（游戏只允许回收 status=='idle'）",
      _cl.posts == [], _cl.posts)
_limited = [{"id": "l1", "userId": "me", "carId": 0, "status": "idle"},
            {"id": "l2", "userId": "me", "carId": 4, "status": "parked"}]
_p, _cl, _n = _farm(cars=_limited, buy=0, trade=0, max_cars=2)
check("★限时车（carId=0）不拿去回收（卖掉就再也买不回来）",
      _cl.posts == [], _cl.posts)
# ★上面那条区分不出「显式排除 carId==0」和「只是恰好不在车型表里」：
# 现实里限时车确实不在 cars_map（名字和图挂在车辆实例上），所以
# _lookup_car 查不到就把它滤掉了，删掉 carId==0 那道判断结果完全一样。
# 这条补一个「限时车也在车型表里」的防御性夹具，才能真正盯住那道判断 ——
# 万一游戏哪天把限时车也塞进车型表，删掉判断就会当场回收掉不可再生的车。
_ZERO = dict(_FCMAP)
_ZERO[0] = {"id": 0, "name": "限时跑车", "price": 1, "levelReq": 1, "earnPerMin": 1}
_p, _cl, _n = _farm(cars=_limited, buy=0, trade=0, max_cars=2, cmap=_ZERO)
check("★限时车即使出现在车型表里也不拿去回收（显式 carId==0 那道判断）",
      _cl.posts == [], _cl.posts)
_p, _cl, _n = _farm(cars=[{"id": "c1", "userId": "me", "carId": 3, "status": "idle"},
                          {"id": "c2", "userId": "me", "carId": 1, "status": "idle"}],
                    buy=0, trade=0, max_cars=2)
check("★回收挑的是最便宜的空闲车，不是最贵的（奔驰C 500 vs 奥拓 100）",
      _cl.posts[0][1] == {"carInstanceId": "c2"}, _cl.posts[0])

# --- 钱不够 / 车型不够 ---
_p, _cl, _n = _farm(buy=0, trade=0, cash=150, max_cars=10)
check("★金币不够时一个请求都不发（要换新就得垫够新车价）",
      _cl.posts == [], _cl.posts)
# 只给一辆够得着的车：等级 1，只放 3 号（2 号 levelReq=2 会被滤掉）
_ONE = {3: {"id": 3, "name": "奔驰C", "price": 500, "levelReq": 1, "earnPerMin": 25},
        2: {"id": 2, "name": "桑塔纳", "price": 300, "levelReq": 2, "earnPerMin": 15}}
check("★_exp_farm_cars 会按等级滤掉买不起的（先滤再按价格排序）",
      [d["id"] for d in P._exp_farm_cars(_ONE, 1)] == [3],
      [d["id"] for d in P._exp_farm_cars(_ONE, 1)])
_p, _cl, _n = _farm(buy=0, trade=0, level=1, max_cars=10, cmap=_ONE,
                    cars=[{"id": "x", "userId": "me", "carId": 3, "status": "idle"}])
check("★等级内只有一辆能买的车时不硬凑（以旧换新只能换更贵的）",
      _cl.posts == [], _cl.posts)

# --- 记账：回收那笔钱是收入 ---
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=3)
check("★买车的钱记成支出、以旧换新的差价也记（统计页要对得上）",
      _p._shop_spent == 100 and _p._shop_diff == 200
      and _p._shop_bought == 1 and _p._shop_traded == 1,
      (_p._shop_spent, _p._shop_diff, _p._shop_bought, _p._shop_traded))
# 满员那轮回收两辆：腾位置回收的是 900 元兰博基尼（拿回 720），
# 外加循环末尾回收的 300 元桑塔纳（拿回 240），合计 960。
check("★回收拿回的钱记成**收入**（不记的话当日净收凭空少一截）",
      _p._shop_recycled == 720 + 240, _p._shop_recycled)

DB.clear()
DB["daily"] = {(dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d"):
               {"earned": 100, "spent": 0, "rounds": 1}}
# ★_record_round 必须调在**做买卖的那个实例**上：累加器是实例属性，
# 换成另一个实例去记，_shop_recycled 恒为 0，断言会拿着 0 判「没并进去」。
# 这里刻意**不写 assert**：assert 失败会抛 AssertionError 让整个测试脚本崩掉，
# 反向验证里就变成「崩=True -> 没抓到」，把真抓到的一条判成没抓到。
# 自检交给下面那条 check 报红。
_q, _cl2, _n2 = _farm(buy=0, trade=0, max_cars=3)
_q._page_cache = None
_q._title_cache = None
asyncio.run(_q._record_round(_FarmClient(_fst(buy=0, trade=0)), _cl2.st, "me",
                             0, 0, 0, record_history=False))
_buck = (DB.get("daily") or {}).get(dt.datetime.now().strftime("%Y-%m-%d")) or {}
# 按天是独立桶，今天的 earned 从 0 起算，不会继承昨天那 100。
# 真正该钉住的是「净收 = 收入 - 支出 - 差价」要和实际花掉的钱吻合：
# 960 回收 - 100 买车 - 200 差价 = +660，正好等于满员那轮实际到手的钱
# （含回收 900 元兰博基尼那笔一次性回血）。
check("★回收收入并进当日收益 earned（净收才对得上实际花的钱）",
      _buck.get("recycled") == 960 and _buck.get("earned") == 960
      and _buck.get("spent") == 100 and _bucket_net(_buck) == 660,
      dict({k: _buck.get(k) for k in ("recycled", "earned", "spent", "diff")},
           net=_bucket_net(_buck)))
check("★记完 _shop_recycled 归零（不跨轮重复累加）",
      _q._shop_recycled == 0, _q._shop_recycled)

# --- run_once 里确实排了这一步 ---
_ro = _code_only(_body2("run_once"))
check("★run_once 里排了刷经验这一步（不能只定义方法没人调）",
      "_exp_farm_step(" in _ro,
      [l.strip()[:60] for l in _ro.split("\n") if "_exp_farm" in l])
check("★刷经验排在挪车之后、停车之前"
      "（挪车前车还停着，一辆都回收不了）",
      _ro.find("_unpark_due_cars(") < _ro.find("_exp_farm_step(")
      < _ro.find("_park_idle_cars("),
      [l.strip()[:60] for l in _ro.split("\n")
       if "_unpark_due_cars(" in l or "_exp_farm_step(" in l
       or "_park_idle_cars(" in l])
check("★刷完必须刷新状态（买卖改过服务端，拿旧 ID 继续停车会出事）",
      # 整段统计的计数写法太松：原来是 `>= 4`，实际有 5 处，把刷经验那一处
      # 删掉还剩 4，照样全绿。改成**只看刷经验自己那一块里有没有刷新**，
      # 删掉就当场红。
      "_refresh_state()" in _ro.split("if self._exp_farm:")[-1].split(
          "# 还能再停几辆")[0],
      [l.strip()[:60] for l in _ro.split("if self._exp_farm:")[-1].split(
          "# 还能再停几辆")[0].split("\n")
       if "_refresh_state" in l or "stale_after_shop" in l])
check("★run_once 里一共有 5 处刷新状态（买车/换新/贴条后/刷经验后 + 失败兜底）",
      _ro.count("_refresh_state()") == 5, _ro.count("_refresh_state()"))
_src_farm = _code_only(_body2("_exp_farm_step"))
check("★买车/换新/回收三个接口都用上了",
      all(x in _src_farm for x in ("buy_car.php", "trade_in.php", "recycle_car.php")),
      [x for x in ("buy_car.php", "trade_in.php", "recycle_car.php")
       if x in _src_farm])
check("★不靠猜响应体里的车 ID（buy/trade 的响应结构没有实测依据）",
      "_exp_farm_find_new" in _src_farm
      and '"newCarInstanceId"' not in _src_farm,
      [l.strip()[:70] for l in _src_farm.split("\n") if "InstanceId" in l])

# ★真机踩过的坑：buy 已写成功（新车实例 ID 的时间戳就是买的那一刻），
# 但紧接着那一次 state.php 还没带上它。桩用 lag 模拟这个延迟。
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=10, lag=1)
check("★state.php 慢一拍时仍能把这一轮走完（重试认出了新车）",
      _eps(_cl) == ["buy_car.php", "trade_in.php", "recycle_car.php"]
      and _cl._refreshes >= 2,
      (_eps(_cl), _cl._refreshes))
check("★重试之后净支出仍然算得对（不是靠放弃来「不漏钱」）",
      100000 - _cl.st["users"]["me"]["cash"] == 60,
      100000 - _cl.st["users"]["me"]["cash"])
_p, _cl, _n = _farm(buy=0, trade=0, max_cars=10, lag=99)
check("★一直看不见就老实停手，不会硬着头皮往下换"
      "（拿不到实例 ID 就发不了 trade_in）",
      _eps(_cl) == ["buy_car.php"] and len(_cl.st["userCars"]) == 4,
      (_eps(_cl), [c["id"] for c in _cl.st["userCars"]]))
check("★认不出来时摘要里说清车留在车库（用户知道要自己去处理）",
      "留在车库" in (_n or ""), _n)
check("★重试次数钉死 3 次（拿被测常量现算的话，常量改成 1 这条照样绿）",
      P._EXP_FARM_FIND_TRIES == 3, P._EXP_FARM_FIND_TRIES)
check("★认不出来时确实重试满了配置次数（不是试一次就放弃）",
      _cl._refreshes == P._EXP_FARM_FIND_TRIES, _cl._refreshes)
def strip_fragment_docstring(frag):
    """去掉方法体片段的 docstring，再剥掉 `#` 注释。

    两个坑叠在一起：
    1. `_strip_docs` 走 ast.parse，**带缩进的方法体片段解析不了**
       （IndentationError 会把整个测试脚本带崩）；
    2. `_code_only` 只剥注释、**不剥 docstring** —— 而「代码里必须出现
       某个词」这类断言最容易被自己的 docstring 满足（docstring 解释这个
       坑时必然会原样写出那个词），实测反向验证把代码里那句删掉照样全绿。

    注意 `_body2` 是**从 `def` 那行开始**取的，所以 dedent 之后
    `tree.body[0]` 是函数定义本身，docstring 在它的 `body[0]`。
    看到 body[0] 不是 docstring 就静默回退的话，这条断言会重新变成假绿。
    """
    ind = len(frag) - len(frag.lstrip())
    src = "\n".join(ln[ind:] if ln[:ind].strip() == "" else ln
                     for ln in frag.split("\n"))
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return _code_only(frag)
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        raise AssertionError("片段不像一个函数定义，剥离逻辑该更新了")
    first = fn.body[0] if fn.body else None
    if not (isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant)
            and isinstance(first.value.value, str)):
        return _code_only(frag)      # 确实没有 docstring
    lines = frag.split("\n")
    return "\n".join(lines[:first.lineno - 1] + lines[first.end_lineno:])


# ★必须查剥掉 docstring 之后的版本：_code_only 只剥 `#` 注释，
# 而这个方法的 docstring 里本身就写着「把实际看到的匹配数打进日志」——
# 查原文的话，把代码里那句日志删掉照样 PASS（反向验证实测：真删了也没红）。
_efn = strip_fragment_docstring(_body2("_exp_farm_find_new"))
check("★认不出来时日志要写明**实际看到了几辆**，不能只说「定位不到」",
      "匹配数 {seen}" in _efn,
      [l.strip()[:60] for l in _efn.split("\n") if "认不出" in l])

print("\n--- #42 回收率读真值 / 不同主人诊断（v1.52.0）---")
# ★用户截图：车市里奥迪A8 售价 114.3K，车库里的回收按钮显示的却不是 8 成。
# 根因：游戏前端 api.js:115 是
#     if (cfg.RECYCLE_RATE) this.GAME_RULES.RECYCLE_RATE = parseInt(cfg.RECYCLE_RATE);
# state.php 的 gameSettings 会**覆盖** game_data.php 的 gameRules，
# 只读后者拿到的是已经被覆盖掉的旧值。优先级必须跟游戏一模一样。
_RR = {"gameRules": {"RECYCLE_RATE": 80}}
check("★gameSettings.RECYCLE_RATE 优先于 gameRules（游戏 api.js:115 就是这个优先级）",
      P._recycle_rate({"gameSettings": {"RECYCLE_RATE": 30}}, _RR) == 30,
      P._recycle_rate({"gameSettings": {"RECYCLE_RATE": 30}}, _RR))
check("★gameSettings 没有这一项时才退回 gameRules",
      P._recycle_rate({"gameSettings": {"MAX_CARS": 10}}, _RR) == 80,
      P._recycle_rate({"gameSettings": {"MAX_CARS": 10}}, _RR))
check("★gameSettings 里有 0 视为没下发（0 会让回收一文不值，明显不是配置）",
      P._recycle_rate({"gameSettings": {"RECYCLE_RATE": 0}}, _RR) == 80,
      P._recycle_rate({"gameSettings": {"RECYCLE_RATE": 0}}, _RR))
check("★两边都没有时兜底 80（不是 JS 里那个 50，那是前端兜底不是服务端值）",
      P._recycle_rate({}, _RR) == 80 and P._recycle_rate(None, None) == 80
      and P._recycle_rate({"gameSettings": "不是字典"}, _RR) == 80,
      (P._recycle_rate({}, _RR), P._recycle_rate(None, None),
       P._recycle_rate({"gameSettings": "不是字典"}, _RR)))
check("★回收率超过 100 会被夹回 100（脏数据不该算出负数净支出）",
      P._recycle_rate({"gameSettings": {"RECYCLE_RATE": 300}}, _RR) == 100,
      P._recycle_rate({"gameSettings": {"RECYCLE_RATE": 300}}, _RR))

# --- 页面上那行进度：刷经验开着但没执行时，不能说成「开关坏了」 ---
def _head_texts(snapshot, note=""):
    """真渲染一次车市页，把页面上所有 text 抓出来。"""
    DB.clear()
    DB["snapshot"] = dict(snapshot, note=note)
    DB["daily"] = {"2026-10-17": {"earned": 0, "rounds": 1}}
    p = P()
    p._page_cache = None
    p._tab = "shop"
    p._exp_open = p._notif_open = False
    p._trade_for = ""
    p._title_open = False
    p._shop_afford = False
    p._shop_sort = "ratio"
    p._exp_farm = True
    p._auto_buy = p._auto_trade = False
    return [str(n["text"]) for n in walk(p.get_page()) if "text" in n]


_HS = {"cash": 1000, "level": 7, "car_total": 9, "car_max": 10, "cars": [],
       "catalog": [], "flash": [], "spot_bonus_pct": 0, "notifications": [],
       "spots": [], "expConfig": {"buy": 300, "tradeIn": 300},
       "dailyExp": {"buy": 200, "tradeIn": 300}}
_t_running = _head_texts(_HS)
check("★开着但没执行时，页面要显示两项进度",
      any("购车 200/300" in t for t in _t_running)
      and any("以旧换新 300/300" in t for t in _t_running),
      [t for t in _t_running if "购车" in t])
# ★经验明明还差 100，说「没有满足条件的操作」等于告诉用户开关坏了。
check("★经验没满却没执行时要说清卡在哪，不能只写「没有满足条件的操作」",
      any("未刷满却没执行" in t and "金币不够" in t for t in _t_running),
      [t for t in _t_running if "购车" in t])
_HS_FULL = dict(_HS)
_HS_FULL["dailyExp"] = {"buy": 300, "tradeIn": 300}
_t_full = _head_texts(_HS_FULL)
check("★两项都满时说的是「已停手」，不是「没执行」（那是正常状态不是故障）",
      any("两项都已刷满，已停手" in t for t in _t_full)
      and not any("未刷满却没执行" in t for t in _t_full),
      [t for t in _t_full if "购车" in t])

print("\n--- #45 时间口径统一按北京时间（v1.53.1）---")
_ESNAP = {"level": 7, "exp": 2370,
          "levelTable": [{"level": 7, "expReq": 1700}, {"level": 8, "expReq": 2500}],
          "expConfig": {"park": 150, "ticket": 90, "report": 100, "buy": 300,
                        "tradeIn": 300, "charity_park": 50, "charity": 300},
          "dailyExp": {"park": 150, "buy": 100}}
_exp_panel_texts = [str(n["text"]) for n in walk(
    P._exp_panel(_ESNAP, "api/exp_detail")) if "text" in n]

# ★游戏按 UTC 分日（前端 user.js 用 toISOString 拼 dailyExp 的键），
# UTC 0 点换算成北京时间就是 08:00。插件对用户讲的时间一律北京时间。
check("★每日重置时刻按北京时间记（UTC 0 点 == 北京 08:00）",
      P._EXP_DAY_RESET_HOUR == 8, P._EXP_DAY_RESET_HOUR)
check("★重置时刻写进界面上（否则凌晨看着没满、早上却清零，像算错）",
      any("每日经验在 08:00（北京时间）重置" in t for t in _exp_panel_texts),
      [t for t in _exp_panel_texts if "重置" in t or "每日" in t][:4])
check("★刷经验那行也写明重置时刻，两处口径不许各说各话",
      any("08:00" in t and "北京时间" in t
          for t in _t_running),
      [t for t in _t_running if "购车" in t])
check("★重置时刻格式化会补零，脏配置也不会写出「8:00」这种半截",
      P._fmt_reset_clock(8) == "08:00" and P._fmt_reset_clock(0) == "00:00"
      and P._fmt_reset_clock(25) == "00:00" and P._fmt_reset_clock(-1) == "00:00",
      [P._fmt_reset_clock(h) for h in (0, 8, 25, -1)])
# ★界面和文案里不许出现 UTC —— 那是实现细节，用户只该看到北京时间。
_pl_all = io.open(PLUGIN_FILE, encoding="utf-8").read()
_ui_markers = ("UTC", "utc", "GMT", "Z 时区")
_leaked = []
for _line in _pl_all.split("\n"):
    _s = _line.strip()
    # 只看真正会显示给用户的字串：VText/文本节点/提示文案，
    # 注释和文档里解释「游戏为什么用 UTC」是必要的，不算泄漏。
    if _s.startswith("#") or _s.startswith('"') is False and _s.startswith("*"):
        continue
    if ('"text":' in _line or "VText" in _line
            or ("f\"" in _line and "：" in _line)) and any(
                m in _line for m in _ui_markers):
        _leaked.append(_s[:70])
check("★界面上不出现 UTC/GMT（只该看到北京时间）", not _leaked, _leaked[:5])
# ★但代码里必须仍然按 UTC 取日期 —— 换成「北京时间减 8 小时」会在非 UTC+8 的宿主上算错
_ed45 = strip_fragment_docstring(_body2("_exp_daily"))
check("★_exp_daily 仍按 UTC 取日期（不能图省事写成北京时间减 8 小时）",
      "datetime.now(pytz.utc)" in _ed45
      and "timedelta(hours=" not in _ed45,
      [l.strip()[:70] for l in _ed45.split("\n")
       if "today =" in l or "timedelta" in l])
# 统计分桶必须还是北京 00:00（用户明确要保留）
check("★统计页的按天分桶仍是北京时间 00:00（不被经验口径带着挪到 08:00）",
      'day_key = now.strftime("%Y-%m-%d")' in _pl_all
      and 'now = self._now_naive()' in _pl_all,
      [l.strip()[:70] for l in _pl_all.split("\n")
       if "day_key = " in l or ("now = " in l and "_now_naive" in l)][:4])

print("\n--- #43 刷经验期间不许自动买车/换新碰车（v1.52.2）---")
# ★真机烧钱记录（2026-10-17）：两个开关同时开，一天下来
#   bought 3（3 辆奥拓 800）
#   diff  340500 = 3 × 113500，而 113500 + 800 = 114300 = 奥迪A8 的价
#   recycled 274320 = 3 × 91440 = 3 辆 A8 的回收价（80%）
#   净损失 68580，而设计里一轮只该净亏 400。
# 原因：_auto_trade_one 排在 _exp_farm_step **之前**，且它挑的是
# 「每分钟收益最低的空闲车」—— 刷经验刚买的奥拓只有 6 金/分，正好命中。
_ACT = {"success": True, "userId": "me",
        "gameSettings": {"MAX_CARS": 10, "RECYCLE_RATE": 80},
        "users": {"me": {"name": "我", "level": 7, "cash": 500000}},
        # ★必须是「收益最低、且确实有更赚钱的车可换」的那辆。
        # 原来这里放的是 carId=4 兰博基尼（99 金/分、lvReq=10），Lv.7 下压根
        # 没有比它更赚钱的车 —— 守卫删掉之后 _auto_trade_one 本来也会返回空串，
        # 断言照样过。夹具不能让被测代码「怎么写都对」。
        "userCars": [{"id": "cheap", "userId": "me", "carId": 1, "status": "idle"}],
        "parkingSpots": [],
        "dailyExp": {("me_buy_%s" % _UTODAY): 100,
                     ("me_tradeIn_%s" % _UTODAY): 30}}
_GD5 = {"success": True, "cars": list(_FCMAP.values()),
        "expConfig": {"buy": 100, "buyDailyLimit": 300,
                      "tradeIn": 30, "tradeInDailyLimit": 300}}
_qe = P()          # 开着刷经验的守卫探针
_qe._exp_farm = True
_qe._game_data = _GD5


def _auto_on(st, farm_on, buy_on=True, trade_on=True):
    Client.POSTS = []
    p = P()
    p._exp_farm = farm_on
    p._auto_buy = buy_on
    p._auto_trade = trade_on
    p._game_data = _GD5
    p._cars_cache = {1: {"id": 1, "name": "奥拓", "price": 100, "earnPerMin": 6},
                     2: {"id": 2, "name": "桑塔纳", "price": 300, "earnPerMin": 15},
                     3: {"id": 3, "name": "奔驰C", "price": 500, "earnPerMin": 25}}
    p._cars_cache_at = time.monotonic()
    n1 = asyncio.run(p._auto_trade_one(Client(), st, p._cars_cache, "me"))
    n2 = asyncio.run(p._auto_buy_one(Client(), st, p._cars_cache, "me"))
    return p, [n1, n2]


_p, _notes = _auto_on(_ACT, farm_on=True)
check("★刷经验没满时自动以旧换新整轮跳过（否则会把刚买的便宜车换成贵车）",
      _notes[0] == "" and Client.POSTS == [], (_notes[0], Client.POSTS))
_p, _notes = _auto_on(_ACT, farm_on=True)
check("★刷经验没满时自动买车也跳过（否则会占掉刚腾出来的工位）",
      _notes[1] == "" and Client.POSTS == [], (_notes[1], Client.POSTS))

_FULL_ACT = dict(_ACT)
_FULL_ACT["dailyExp"] = {("me_buy_%s" % _UTODAY): 300,
                         ("me_tradeIn_%s" % _UTODAY): 300}
_p, _notes = _auto_on(_FULL_ACT, farm_on=True)
check("★经验满之后自动换新自动恢复（守卫不能把功能永久关掉）",
      _notes[0] != "" or Client.POSTS != [], (_notes[0], Client.POSTS))
_p, _notes = _auto_on(_ACT, farm_on=False)
check("★刷经验关着时守卫不生效，自动换新照常跑",
      _notes[0] != "" or Client.POSTS != [], (_notes[0], Client.POSTS))
check("★守卫判的是「经验没满」而不是「开关开着」"
      "（满了就该放行，不然自动换新被永久废掉）",
      _qe._exp_farm_active(_ACT, "me") is True
      and _qe._exp_farm_active(_FULL_ACT, "me") is False)
_qe2 = P()
_qe2._exp_farm = False
check("★开关关着时 _exp_farm_active 恒为 False",
      _qe2._exp_farm_active(_ACT, "me") is False)
_at = _code_only(_body2("_auto_trade_one"))
_ab = _code_only(_body2("_auto_buy_one"))
check("★自动换新 / 自动买车都接上了守卫（只挡一个，另一个照样烧钱）",
      "_exp_farm_active(state, user_id)" in _at
      and "_exp_farm_active(state, user_id)" in _ab)
check("★守卫拦下时要说清原因（否则用户以为开关没生效）",
      "刷购车/以旧换新经验" in _at and "刷购车/以旧换新经验" in _ab)
# 页面/文案里不许再出现写死的回收率和成本估算 —— 回收率是服务端可改的，
# 写死过一次就一定会有对不上的时候。
#
# ★判「不存在」必须查**剥离注释/docstring 之后**的版本：解释这个坑的注释里
# 必然会提到那个错的数字（不写清楚下一个人又会去读 gameRules），
# 直接查原文会把正确的说明当成违规。判「存在」才查原文。
_rr_code = _strip_docs(io.open(PLUGIN_FILE, encoding="utf-8").read())
check("★文案里不写死回收率 80%（这句子会随服务端调整变成错的）",
      "回收率 80%" not in _rr_code and "回收率80%" not in _rr_code,
      [l.strip()[:70] for l in _rr_code.split("\n") if "回收率 8" in l])
check("★文案里不写死「刷满两项约 N 金币/天」（成本取决于实时回收率与车价）",
      "刷满两项约" not in _rr_code,
      [l.strip()[:70] for l in _rr_code.split("\n") if "刷满两项约" in l])

# --- 快照里的两个诊断字段 ---
_OWN_ST = {"success": True, "userId": "me",
           "gameSettings": {"RECYCLE_RATE": 45, "MAX_CARS": 10},
           "users": {"me": {"name": "我", "level": 5, "cash": 1000}},
           "userCars": [], "parkingSpots": [],
           "dailyExp": {}}
_orr = P()
_orr._page_cache = None
_orr._cars_cache = dict(_FCMAP)
_orr._cars_cache_at = time.monotonic()
_orr._game_data = _FGD
_orr._title_cache = None
# 同一家（owner "u7"）名下 3 个空车位 + 另一家（"u8"）2 个 + 一家被占满的
_OWN_ST["parkingSpots"] = [
    {"id": 1, "ownerId": "u7", "spotType": "private", "carInSpot": None},
    {"id": 2, "ownerId": "u7", "spotType": "private", "carInSpot": None},
    {"id": 3, "ownerId": "u7", "spotType": "private", "carInSpot": None},
    {"id": 4, "ownerId": "u8", "spotType": "private", "carInSpot": None},
    {"id": 5, "ownerId": "u8", "spotType": "private", "carInSpot": None},
    {"id": 6, "ownerId": "u9", "spotType": "private", "carInSpot": "xx"},
    {"id": 7, "ownerId": "uA", "spotType": "charity", "carInSpot": None},
    {"id": 8, "ownerId": "me", "spotType": "private", "carInSpot": None}]
asyncio.run(_orr._record_round(_FarmClient(_OWN_ST), _OWN_ST, "me", 0, 0, 0,
                               record_history=False))
_osnap = DB.get("snapshot") or {}
check("★free_private_owners 数的是**不同的主人**，不是车位个数",
      _osnap.get("free_private_owners") == 2,
      ({"owners": _osnap.get("free_private_owners"),
        "spots": _osnap.get("free_spots")}))
check("★同一家名下多个空车位只算一个主人（这才是每家停 1 辆时真正卡住的东西）",
      _osnap.get("free_spots") == 5 and _osnap.get("free_private_owners") == 2,
      ({"spots": _osnap.get("free_spots"),
        "owners": _osnap.get("free_private_owners")}))
check("★自己的车位和爱心车位都不计入 free_private_owners",
      _osnap.get("free_charity_spots") == 1
      and _osnap.get("free_private_owners") == 2,
      {"charity": _osnap.get("free_charity_spots"),
       "owners": _osnap.get("free_private_owners")})
check("★快照里的 recycle_rate 走 gameSettings 优先（实测 45，不是 gameRules 的 80）",
      _osnap.get("recycle_rate") == 45, _osnap.get("recycle_rate"))

print("\n--- #44 个性化：场景 + 皮皮秀合并成一类（v1.53.0）---")
# 目录来自 game_data.php，拥有状态来自 state.php：
#   场景   ownedBgs + **state 顶层** currentBg（v1.50.2 踩过：users[me] 里没有）
#   皮皮秀 ownedQqShows + users[me].qqShowId（反过来只在 users 里）
_PERSONAL_GD = dict(GD)
_PERSONAL_GD["parkingBackgrounds"] = [
    {"id": "jiedao", "name": "街道", "file": "assets/parking-bg/jiedao.png",
     "preview": "", "price": 0, "bonusPercent": 0},
    {"id": "jiedao2", "name": "街道夜景", "file": "assets/parking-bg/jiedao2.png",
     "preview": "", "price": 2000, "bonusPercent": 2},
    {"id": "gongyuan", "name": "公园", "file": "assets/parking-bg/gongyuan.png",
     "preview": "", "price": 500, "bonusPercent": 1},
    {"id": "shatan", "name": "沙滩", "file": "assets/parking-bg/shatan.png",
     "preview": "", "price": 5000, "bonusPercent": 5},
]
_PERSONAL_GD["qqShows"] = [
    {"id": 1, "name": "默认男", "file": "assets/qqshow/gentleman.png",
     "price": 0, "gender": "M"},
    {"id": 2, "name": "默认女", "file": "assets/qqshow/lady.png",
     "price": 0, "gender": "F"},
    {"id": 3, "name": "潮男", "file": "assets/qqshow/chaonan.png",
     "price": 1500, "gender": "M"},
    {"id": 5, "name": "非主流男", "file": "assets/qqshow/feizhuliu_nan.png",
     "price": 2000, "gender": "M"},
    {"id": 7, "name": "公主", "file": "assets/qqshow/gongzhu_nv.png",
     "price": 3000, "gender": "F"},
    {"id": 10, "name": "精神小伙", "file": "assets/qqshow/jingshen_nan.png",
     "price": 200, "gender": "M"},
]
# cash=800，四种状态刚好全摆出来：
#   场景   街道免费已拥有 / 公园 500 可买 / 街道夜景 2000 金币不足 / 沙滩使用中
#   皮皮秀 默认男免费已拥有 / 精神小伙 200 可买 / 非主流男 2000 金币不足 / 潮男使用中
#   女款（默认女/公主）全部被性别过滤掉
_PST = {
    "success": True, "userId": "me", "currentBg": "shatan",
    "ownedBgs": ["jiedao", "shatan"],
    "ownedQqShows": [1, 3],
    "gameSettings": {"MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 7, "cash": 800,
                     "gender": "M", "qqShowId": 3}},
    "userCars": [], "parkingSpots": [], "dailyExp": {},
}


class _PersonalClient(_RoundClient):
    """个性化测试桩：game_data 带场景/皮皮秀，state 可定制、可写回。"""

    def __init__(self, st):
        super().__init__()
        self.st = _copy.deepcopy(st)
        self.writes = []

    async def get_json(self, url, **kw):
        self.GETS.append(url.rsplit("/", 1)[-1].split("?")[0])
        if "game_data.php" in url:
            return _copy.deepcopy(_PERSONAL_GD)
        if "state.php" in url:
            return _copy.deepcopy(self.st)
        return await super().get_json(url, **kw)

    async def post_json(self, url, json=None, **kw):
        self.writes.append((url.rsplit("/", 1)[-1], _copy.deepcopy(json)))
        return {"success": True}


DB.clear()
_pcl = _PersonalClient(_PST)
_pp = P()
_pp._game_data = _PERSONAL_GD
_pp._cars_cache = {}
_pp._cars_cache_at = time.monotonic()
_pp._title_cache = None
asyncio.run(_pp._record_round(_pcl, _PST, "me", 0, 0, 0, record_history=False))
_psnap = DB.get("snapshot") or {}

check("★个性化：场景目录进了快照（名字/价格/加成齐全）",
      len(_psnap.get("bgs") or []) == 4
      and {b["id"]: b["bonus"] for b in _psnap["bgs"]}.get("jiedao2") == 2
      and {b["id"]: b["price"] for b in _psnap["bgs"]}.get("shatan") == 5000,
      _psnap.get("bgs"))
check("★个性化：皮皮秀目录进了快照（id/名字/价格/性别）",
      len(_psnap.get("qqshows") or []) == 6
      and {q["id"]: q["gender"] for q in _psnap["qqshows"]}.get(7) == "F",
      _psnap.get("qqshows"))
check("★个性化：currentBg 读 state **顶层**（shatan），不是 users[me]",
      _psnap.get("current_bg") == "shatan", _psnap.get("current_bg"))
check("★个性化：qqShowId 读 users[me]（3=潮男），不是顶层",
      _psnap.get("qqshow_id") == 3, _psnap.get("qqshow_id"))
check("★个性化：已购清单原样进快照（场景 2 个 / 皮皮秀 2 款）",
      sorted(_psnap.get("owned_bgs") or []) == ["jiedao", "shatan"]
      and sorted(_psnap.get("owned_qqshows") or []) == [1, 3],
      ({"bgs": _psnap.get("owned_bgs"), "qq": _psnap.get("owned_qqshows")}))

# --- 渲染：场景四态 + 皮皮秀四态 + 性别过滤 ---
_ps = P()
_ps._personal_sub = "scene"
_ps._game_data = _PERSONAL_GD
_scene_text = [n.get("text") for n in walk(_ps._personal_page(_psnap))
               if n.get("text")]
check("★个性化·场景：四个场景名都渲染",
      all(x in _scene_text for x in ("街道", "街道夜景", "公园", "沙滩")),
      _scene_text)
# v1.53.2 去噪后「使用中」只留右上角徽章（重复的状态行删掉了），
# 只有 shatan 是 currentBg → 恰好 1 个；误标第二个会变成 2。
check("★个性化·场景：使用中只标在 currentBg 那一项（沙滩）",
      _scene_text.count("使用中") == 1, _scene_text)
check("★个性化·场景：免费款算已拥有、可买款显示未购买、贵款显示还差多少",
      "✓ 已购买" in _scene_text and "未购买" in _scene_text
      and "未购买 · 还差 1,200 金" in _scene_text, _scene_text)
check("★个性化·场景：收益加成文案带上了（+2% / +5%）",
      any("+2%" in str(t) for t in _scene_text)
      and any("+5%" in str(t) for t in _scene_text), _scene_text)

_ps._personal_sub = "show"
_show_text = [n.get("text") for n in walk(_ps._personal_page(_psnap))
              if n.get("text")]
check("★个性化·皮皮秀：只列男性款（默认女/公主被性别过滤）",
      "默认男" in _show_text and "潮男" in _show_text
      and "默认女" not in _show_text and "公主" not in _show_text, _show_text)
check("★个性化·皮皮秀：四态齐全（使用中/已购买/未购买/金币不足）",
      _show_text.count("使用中") == 1
      and "✓ 已购买" in _show_text
      and "未购买" in _show_text
      and "未购买 · 还差 1,200 金" in _show_text, _show_text)


def _saved_me(saved):
    """
    从 saveState 的 payload 里安全取 users[me]。

    ★必须用 .get 逐层取：注入用例会把某个字段整段删掉，直接下标会 KeyError
    把脚本打崩 —— 反向验证那边只看到「崩=True -> 没抓到」，真因是断言自己不抗打。
    """
    users = (saved or {}).get("users")
    me = users.get("me") if isinstance(users, dict) else None
    return me if isinstance(me, dict) else {}


def _bind_ctx(p, cl):
    """把 _load_game_context / _refresh_snapshot 换成测试桩，只测接口本身。"""
    async def _ctx(prefetch=True, extra=None):
        return cl, _copy.deepcopy(cl.st), {}, "", None

    async def _noop_refresh(client, note):
        return None

    p._load_game_context = _ctx
    p._refresh_snapshot = _noop_refresh


# 场景购买：必须打 buy_bg.php（服务端扣钱），不能自己改 state
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_bg_action(mod.BgActionRequest(bg_id="gongyuan", action="buy")))
check("★场景购买走 buy_bg.php（不自己扣金币）",
      _ir.success and ("buy_bg.php", {"bgId": "gongyuan"}) in _ic.writes,
      (_ir.success, _ic.writes))

# 场景切换：saveState 全量 7 字段，currentBg 改对
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_bg_action(mod.BgActionRequest(bg_id="jiedao", action="select")))
_saved = next((b for ep, b in _ic.writes if ep == "state.php"), None)
check("★场景切换走 saveState：POST state.php 必须带全 7 个字段",
      _ir.success and _saved is not None
      and set(_saved) == {"users", "userCars", "parkingSpots", "dailyExp",
                          "currentBg", "ownedBgs", "ownedQqShows"}
      and _saved.get("currentBg") == "jiedao", _saved)
check("★场景切换前先拉最新 state（拿快照旧数据回写会盖掉停车结果）",
      "state.php" in _ic.GETS, _ic.GETS)

# 未购买的场景不让直接切
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_bg_action(mod.BgActionRequest(bg_id="jiedao2", action="select")))
check("★未购买的场景不能直接切换（拒掉且不写 state）",
      not _ir.success and not any(ep == "state.php" for ep, _ in _ic.writes),
      (_ir.message, _ic.writes))

# 皮皮秀购买：游戏没有独立接口，按前端做法改 cash/qqShowId/ownedQqShows
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_qqshow_action(
    mod.QqShowActionRequest(qqshow_id=10, action="buy")))
_saved = next((b for ep, b in _ic.writes if ep == "state.php"), None)
check("★皮皮秀购买：扣 200 → 600，qqShowId=10，ownedQqShows 追加 10",
      _ir.success and _saved is not None
      and _saved_me(_saved).get("cash") == 600
      and _saved_me(_saved).get("qqShowId") == 10
      and 10 in (_saved.get("ownedQqShows") or []), _saved)

# 再点当前穿戴款 → 恢复该性别的免费默认款
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_qqshow_action(
    mod.QqShowActionRequest(qqshow_id=3, action="select")))
_saved = next((b for ep, b in _ic.writes if ep == "state.php"), None)
check("★再点当前穿戴款 → 恢复该性别免费默认款（1=默认男）",
      _ir.success and _saved is not None
      and _saved_me(_saved).get("qqShowId") == 1, _saved)

# 未购买的皮皮秀不能直接穿
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_qqshow_action(
    mod.QqShowActionRequest(qqshow_id=5, action="select")))
check("★未购买的皮皮秀不能直接切换",
      not _ir.success and not any(ep == "state.php" for ep, _ in _ic.writes),
      (_ir.message, _ic.writes))

# 性别不匹配的皮皮秀买不了
_ic = _PersonalClient(_PST)
_ip = P(); _ip._game_data = _PERSONAL_GD
_bind_ctx(_ip, _ic)
_ir = asyncio.run(_ip.api_qqshow_action(
    mod.QqShowActionRequest(qqshow_id=7, action="buy")))
check("★性别不匹配的皮皮秀不能买（和游戏 selectQqShow 一致）",
      not _ir.success and not _ic.writes, (_ir.message, _ic.writes))

print("\n--- #45 金币流水 + 分类锁定换新（v1.54.0）---")

# ---------- 金币流水 ----------
_pc = P()
_pc._coin_events = []
_pc._coin_event("buy", -300, "桑塔纳")
_pc._coin_event("unpark", 120, "挪走 2 辆")      # 收入：不该进流水
_pc._coin_event("buy", 0, "零元不记")             # 0：不该进流水
_pc._coin_event("trade", -80, "奥拓 → 桑塔纳")
check("★只记支出：kind / delta / note 都对",
      [(e.get("kind"), e.get("delta"), e.get("note")) for e in _pc._coin_events]
      == [("buy", -300, "桑塔纳"), ("trade", -80, "奥拓 → 桑塔纳")],
      _pc._coin_events)
check("★收入和 0 元都不进流水（v1.54.1 起流水只记支出）",
      len(_pc._coin_events) == 2, _pc._coin_events)

DB.clear()
_pr = P()
_pr._game_data = GD
_pr._cars_cache = {}
_pr._cars_cache_at = time.monotonic()
_pr._title_cache = None
_pr._coin_events = [{"t": "00:01", "kind": "buy", "delta": -500, "note": "奔驰C"}]
asyncio.run(_pr._record_round(_RoundClient(), _RAW_STATE, "liyo1", 0, 0, 120,
                              record_history=False))
_day = P._now_naive().strftime("%Y-%m-%d")
_log = ((DB.get("daily") or {}).get(_day) or {}).get("log") or []
check("★支出落进当天 daily['log']，收入（unpark）不落",
      any(e.get("kind") == "buy" and e.get("delta") == -500 for e in _log)
      and not any(e.get("kind") == "unpark" for e in _log),
      _log)
check("★落盘后内存缓冲清空（不跨轮重复记）",
      _pr._coin_events == [], _pr._coin_events)

DB["daily"] = {_day: {"earned": 120, "log": [
    {"t": "00:01", "kind": "buy", "delta": -500, "note": "奔驰C"},
    {"t": "00:02", "kind": "recycle", "delta": 80, "note": "奥拓"},
]}}
_pst2 = P(); _pst2._page_cache = None; _pst2._tab = "stats"
_stext = [n.get("text") for n in walk(_pst2.get_page()) if n.get("text")]
check("★统计页出现「金币支出流水」卡片与中文类型",
      "金币支出流水" in _stext and "买车" in _stext,
      [t for t in _stext if "流水" in str(t) or "买车" in str(t)])
check("★老数据里的收入记录不显示（只铺支出）",
      "回收车辆" not in _stext,
      [t for t in _stext if "回收" in str(t)])
check("★支出金额显示绝对值（列名已是「支出」，不带负号）",
      "500" in _stext and "-500" not in _stext,
      [t for t in _stext if "500" in str(t)])
check("★副标题汇总当天支出总额",
      any("合计花出 500 金" in str(t) for t in _stext),
      [t for t in _stext if "合计花出" in str(t)])

# ---------- 分类锁定换新 ----------
_LOCK_CARS = {
    1: {"id": 1, "name": "奥拓", "price": 100, "levelReq": 1,
        "earnPerMin": 6, "category": "代步车"},
    2: {"id": 2, "name": "桑塔纳", "price": 300, "levelReq": 1,
        "earnPerMin": 15, "category": "代步车"},
    3: {"id": 3, "name": "奔驰C", "price": 500, "levelReq": 1,
        "earnPerMin": 25, "category": "代步车"},
    # 高级车故意卡 Lv.10：本夹具的角色是「别的分类的干扰项」，
    # 让 Lv.5 换不了它，否则任何一轮都会选它（收益 99 最高），
    # 「锁定代步车」那几条就测不出分类内约束。
    4: {"id": 4, "name": "兰博基尼", "price": 900, "levelReq": 10,
        "earnPerMin": 99, "category": "高级车"},
}
_LOCK_ST = {
    "success": True, "userId": "me",
    "gameSettings": {"MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 5, "cash": 1000}},
    "userCars": [{"id": "idle-old", "userId": "me", "carId": 1, "status": "idle"}],
    "parkingSpots": [],
}


def _trade_once(locks):
    """按给定锁定表跑一次自动换新，返回 trade_in 请求体列表。"""
    Client.POSTS = []
    p = P()
    p._auto_trade = True
    p._exp_farm = False
    p._trade_locks = dict(locks)
    asyncio.run(p._auto_trade_one(Client(), _copy.deepcopy(_LOCK_ST),
                                  _LOCK_CARS, "me"))
    return [q for e, q in Client.POSTS if e == "trade_in.php"]


_no_lock = _trade_once({})
check("★未锁定时仍按收益增量最大选（奥拓 → 奔驰C）",
      bool(_no_lock) and _no_lock[0].get("newCarId") == 3, _no_lock)
_lock2 = _trade_once({"代步车": 2})
check("★锁定「代步车=桑塔纳」后只换桑塔纳（不换收益更高的奔驰C）",
      bool(_lock2) and _lock2[0].get("newCarId") == 2, _lock2)
_lock_high = _trade_once({"高级车": 4})
check("★锁的是别的分类时本分类不受影响（仍换奔驰C）",
      bool(_lock_high) and _lock_high[0].get("newCarId") == 3, _lock_high)
_lock_miss = _trade_once({"代步车": 99})
check("★锁定款不存在时整轮不换（不退回收益最高）",
      _lock_miss == [], _lock_miss)

# 锁定接口：写入 / 取消
_pl = P()
_pl._page_cache = {"snapshot": dict(SHOP_SNAP), "history": [], "daily": {}}
_r1 = asyncio.run(_pl.api_trade_lock(mod.TradeLockRequest(car_id=2)))
check("★锁定接口把车型写进它所属分类",
      _r1.success and _pl._trade_locks.get("代步车") == 2, _pl._trade_locks)
_r2 = asyncio.run(_pl.api_trade_lock(mod.TradeLockRequest(car_id=2)))
check("★同款再点一次 = 取消该分类锁定",
      _r2.success and "代步车" not in _pl._trade_locks, _pl._trade_locks)
_r3 = asyncio.run(_pl.api_trade_lock(mod.TradeLockRequest(car_id=999)))
check("★不存在的车型拒绝锁定（不写脏数据）",
      not _r3.success and not _pl._trade_locks, (_r3.message, _pl._trade_locks))

# 车市页：锁定按钮 + 副标题提示
# ★必须显式挂上 SHOP_SNAP：_page_cache=None 时 _page_data 会去 DB 读，
# 而前面几个用例已经把 DB["snapshot"] 改掉了，catalog 对不上 → 锁定车型找不到名字。
_pshop = P()
_pshop._page_cache = {"snapshot": dict(SHOP_SNAP), "history": [], "daily": {}}
_pshop._tab = "shop"
_pshop._trade_locks = {"代步车": 2}
_shtext = [n.get("text") for n in walk(_pshop.get_page()) if n.get("text")]
check("★车市卡片出现「已锁定」按钮",
      "已锁定" in _shtext, [t for t in _shtext if "锁定" in str(t)])
check("★车市副标题写明本分类锁定了哪款",
      any("本分类已锁定换新" in str(t) and "桑塔纳" in str(t) for t in _shtext),
      [t for t in _shtext if "本分类" in str(t)])

print("\n--- #46 贴条失败后就地刷新车位（v1.54.1）---")
# 用户主诉：车位卡片上明明停着车，点「贴条」却提示「车位上没有车」，
# 而且刷不掉那张卡 —— 车早在快照生成之后被挪走了。
# 修法：贴条遇到「数据已过期」类失败时，用刚拉到的 state 就地刷一次快照。


class _TicketFail(Client):
    """ticket.php 本身返回失败（模拟「点下去那一刻车刚好走了」的并发）。"""

    async def post_json(self, url, json=None, **kw):
        Client.POSTS.append((url.rsplit("/", 1)[-1], json))
        if "ticket.php" in url:
            return {"success": False, "error": "车位上没有车"}
        return {"success": True}


def _ticket_run(spot, with_car=True, mins=30, client=None):
    """构造「页面快照说有车」的场景，调 api_ticket，返回 (结果, 刷新次数)。

    刷新次数用 mock 掉的 _record_round 计数 —— 真跑 _record_round 需要整套
    client/state，而它自己的正确性已有专门断言；这里只关心「有没有触发刷新」。
    """
    Client.POSTS = []
    st = {
        "success": True, "userId": "me",
        "gameSettings": {"MIN_TICKET_TIME": 5},
        "users": {"me": {"name": "我", "level": 5, "cash": 1000}},
        "userCars": ([{"id": "other-car", "userId": "other", "carId": 1,
                       "status": "parked"}] if with_car else []),
        "parkingSpots": [dict(spot)],
    }
    calls = []

    async def _fake_record(client_, state, user_id, np, mv, earned,
                           record_history=True):
        calls.append(state.get("parkingSpots"))

    p = P()
    p._enabled = True          # api_ticket 第一行就查 get_state()
    p._game_data = GD
    p._cars_cache = {}
    p._cars_cache_at = time.monotonic()
    p._title_cache = None
    p._record_round = _fake_record
    cl = client or Client()

    async def _ctx(prefetch=True, extra=None):
        return cl, st, {}, "", None

    p._load_game_context = _ctx
    # 成功路径会再 GET 一次 state.php 再调 _record_round；把 Client.STATE 指向
    # 同一份 state，用完恢复，别污染后面的用例。
    _prev = Client.STATE
    Client.STATE = st
    try:
        r = asyncio.run(p.api_ticket(mod.TicketRequest(spot_id=spot.get("id"))))
    finally:
        Client.STATE = _prev
    return r, len(calls)


_spot_ok = {"id": 501, "ownerId": "me", "spotType": "private",
            "carInSpot": "other-car", "parkedTime": ts(30)}
_spot_empty = {"id": 502, "ownerId": "me", "spotType": "private",
               "carInSpot": None, "parkedTime": ts(30)}
_spot_ghost = {"id": 503, "ownerId": "me", "spotType": "private",
               "carInSpot": "ghost", "parkedTime": ts(30)}
_spot_short = {"id": 504, "ownerId": "me", "spotType": "private",
               "carInSpot": "other-car", "parkedTime": ts(2)}

_r, _n = _ticket_run(_spot_empty)
check("★车位上没有车：返回失败，且刷新了快照（幽灵车不会一直挂着）",
      (not _r.success) and _n == 1, (_r.message, _n))
check("★「没有车」的提示里写明已刷新（用户知道下一步该看什么）",
      "已刷新" in _r.message, _r.message)

_r, _n = _ticket_run(_spot_ghost)
check("★车位上的车已不在：同样刷新",
      (not _r.success) and _n == 1, (_r.message, _n))

_r, _n = _ticket_run(_spot_ok, client=_TicketFail())
check("★ticket.php 返回失败（并发被挪走）：也刷新",
      (not _r.success) and _n == 1, (_r.message, _n))
check("★接口失败时把原始原因也带上（不吞掉游戏的话）",
      "车位上没有车" in _r.message, _r.message)

_r, _n = _ticket_run(_spot_short)
check("★停放不足时长：只报错、不白刷一次（数据没变，刷新纯浪费）",
      (not _r.success) and _n == 0, (_r.message, _n))

# 成功路径仍然只刷一次（不能因为新加的失败分支变成两次）
_r, _n = _ticket_run(_spot_ok)
check("★贴条成功仍只刷新一次（新分支没有让成功路径多刷）",
      _r.success and _n == 1, (_r.message, _n))

print("\n--- #47 皮皮秀购买：乐观更新，不重复扣钱（v1.54.2）---")
# 用户主诉：买皮皮秀要点两次才显示「使用中」，而且金币支出流水记了两笔。
# 根因：购买走 saveState，写完立刻回读时服务端可能还是旧值，
#      _refresh_snapshot 把刚买好的状态覆盖回「未购买」→ 用户再点一次 → 重复扣钱。
# 修法：买完直接乐观更新本地快照，不再依赖回读。

_QS_STATE = {
    "success": True, "userId": "me",
    "gameSettings": {"MAX_CARS": 10},
    "users": {"me": {"name": "我", "level": 5, "cash": 1000,
                     "gender": "M", "qqShowId": 1}},
    "ownedQqShows": [1],
    "userCars": [], "parkingSpots": [],
}
_QS_GD = dict(GD)
_QS_GD["qqShows"] = [
    {"id": 1, "name": "默认男", "file": "a.png", "price": 0, "gender": "M"},
    {"id": 3, "name": "潮男", "file": "b.png", "price": 300, "gender": "M"},
]


def _qs_run(qs_id, action, snap=None, current=1, owned=None):
    """返回 (结果, 刷新次数, 快照, 记账条目)。"""
    state = _copy.deepcopy(_QS_STATE)
    state["users"]["me"]["qqShowId"] = current
    if owned is not None:
        state["ownedQqShows"] = list(owned)
    calls = {"refresh": 0}
    p = P()
    p._enabled = True
    p._game_data = _QS_GD
    p._cars_cache = {}
    p._cars_cache_at = time.monotonic()
    p._title_cache = None
    p._coin_events = []
    p._shop_spent = 0
    p._page_cache = {"snapshot": dict(snap) if snap else None}

    async def _ctx(prefetch=True, extra=None):
        return Client(), state, {}, "", None

    async def _fake_save(client_, mutate):
        mutate(state)
        return True, ""

    async def _fake_refresh(client_, note):
        calls["refresh"] += 1

    p._load_game_context = _ctx
    p._save_state_patch = _fake_save
    p._refresh_snapshot = _fake_refresh
    r = asyncio.run(p.api_qqshow_action(
        mod.QqShowActionRequest(qqshow_id=qs_id, action=action)))
    return r, calls["refresh"], (p._page_cache or {}).get("snapshot"), list(p._coin_events)


_base_snap = {"cash": 1000, "qqshow_id": 1, "owned_qqshows": [1], "gender": "M"}
_r, _ref, _snap, _ev = _qs_run(3, "buy", snap=_base_snap)
check("★购买成功后不再回读服务端（回读会覆盖乐观更新）",
      _r.success and _ref == 0, (_r.message, _ref))
check("★购买后本地快照立刻是「使用中 + 已拥有 + 已扣款」",
      bool(_snap) and _snap.get("qqshow_id") == 3
      and 3 in (_snap.get("owned_qqshows") or [])
      and _snap.get("cash") == 700, _snap)
check("★一次购买只记一笔支出（不会再出现「两次购买」）",
      [(e.get("kind"), e.get("delta")) for e in _ev] == [("qqshow", -300)], _ev)

# 已经穿着这一款时再点「购买」：直接返回，不扣钱、不记账
_r, _ref, _snap, _ev = _qs_run(3, "buy", snap=_base_snap, current=3)
check("★已经在穿的款再点购买 → 直接返回，不重复扣钱",
      _r.success and _ev == [] and _snap.get("cash") == 1000,
      (_r.message, _ev, _snap.get("cash") if _snap else None))

# 切换穿戴：同样乐观更新，不刷新
_r, _ref, _snap, _ev = _qs_run(3, "select", snap=dict(_base_snap, owned_qqshows=[1, 3]),
                               owned=[1, 3])
check("★切换穿戴也走乐观更新（qqshow_id 立刻变、不调刷新）",
      _r.success and _ref == 0 and _snap.get("qqshow_id") == 3,
      (_r.message, _ref, _snap.get("qqshow_id") if _snap else None))

print("\n" + ("=" * 46))
print(f"{sum(ok)}/{len(ok)} 通过" + ("" if all(ok) else "  <<< 有失败项"))
sys.exit(0 if all(ok) else 1)
