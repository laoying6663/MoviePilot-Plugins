# -*- coding: utf-8 -*-
"""
远端内容级核对（直读 GitHub Contents API，不经过 raw 的 CDN）。

raw.githubusercontent.com 的边缘缓存有时好几分钟才刷新，会把已经推成功的
文件显示成旧版 —— 实测连等 3 分钟仍是旧的。连接器的 get_file_contents 走的是
api.github.com，没有这层缓存，拿到的是仓库里的真实内容。
"""
import ast
import io
import json
import os
import re
import subprocess
import time
import sys

sys.stdout.reconfigure(encoding="utf-8")


def _find_root():
    """向上找含 plugins.v3/parkingbot/__init__.py 的目录。

    ★原来写死 "D:/minimax/parkingbot/"，脚本一进仓库 tests/ 就废掉。
    与 _verify_170.py / _rev150_harness.py 用同一套推导，三份脚本放一起。
    """
    import os
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(d, "plugins.v3", "parkingbot", "__init__.py")):
            return d + "/"
        parent = os.path.dirname(d)
        if parent == d:
            raise SystemExit(
                "找不到 plugins.v3/parkingbot/__init__.py —— "
                "请把本脚本放在项目目录内（或它的某个子目录里）")
        d = parent


ROOT = _find_root()
FILES = [("plugins.v3/parkingbot/__init__.py", "plugins.v3/parkingbot/__init__.py"),
         ("plugins.v3/parkingbot/README.md", "plugins.v3/parkingbot/README.md"),
         ("package.v3.json", "package.v3.json")]

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"   {'PASS' if cond else 'FAIL'}  {label}" + (f" -> {extra}" if extra else ""))


_FETCH_MEMO = {}


def src_seg(src, name):
    """按**行首**匹配取方法体，返回 (函数名, 方法体, 到下一个方法的整段)。

    ★不能写成 `code[a:code.find("    def ", a+10)]`：8 空格缩进的**嵌套**
    def 里也含有 "    def " 这个子串，切片会在嵌套函数处提前截断，
    拿到的「方法体」缺了后半截 —— 判据于是去一个不完整的片段里找东西，
    永远找不到，红着还不给任何信息（这个坑本项目已经踩过好几次）。
    """
    lines = src.split("\n")
    bnd = ("    async def ", "    def ", "    @classmethod", "    @staticmethod")
    s = next(i for i, ln in enumerate(lines)
             if ln.startswith("    async def %s(" % name)
             or ln.startswith("    def %s(" % name))
    e = next((i for i in range(s + 1, len(lines)) if lines[i].startswith(bnd)),
             len(lines))
    return name, "\n".join(lines[s:e]), "\n".join(lines[s:e])


def fetch(repo):
    """
    连接器返回两个元素：第 0 个是状态说明，真正的文件内容在 [1]["resource"]["text"]。

    带缓存 + 重试：这个连接器偶发返回空（stdout 不是 JSON，或 resource.text 为空）。
    同一文件原来要被取好几次（逐字节比对一次、后面每段断言又各取一次），
    任何一次抖动都会让后面那几条断言凭空变红 —— 那是工具假失败，不是内容有问题。
    取一次存下来，后面全部复用；真取不到就重试几次，再取不到才算内容不对。
    """
    if repo in _FETCH_MEMO:
        return _FETCH_MEMO[repo]
    last = None
    for attempt in range(4):
        last = _fetch_once(repo)
        if last:
            break
        time.sleep(2 + attempt * 2)
    _FETCH_MEMO[repo] = last
    return last


def _fetch_once(repo):
    io.open("_vr_s.json", "w", encoding="utf-8").write(json.dumps(
        {"owner": "laoying6663", "repo": "MoviePilot-Plugins", "path": repo, "ref": "main"}))
    out = subprocess.run(
        "mcode-tools.cmd connector call connector__github__get_file_contents --args-file _vr_s.json",
        capture_output=True, text=True, shell=True, encoding="utf-8",
        errors="replace", timeout=180).stdout
    try:
        parts = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(parts, list):
        return None
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = (part.get("resource") or {}).get("text")
        if isinstance(text, str) and text:
            return text
    return None


for repo, local in FILES:
    remote = fetch(repo)
    disk = io.open(ROOT + local, encoding="utf-8").read()
    check(f"{repo} 逐字节一致", remote == disk,
          "远端没取到" if remote is None else f"远端 {len(remote)} / 本地 {len(disk)} 字符")

code = fetch("plugins.v3/parkingbot/__init__.py") or ""


def strip_docs(src):
    """把 # 注释和 docstring 都摘掉，只留真代码。

    这个插件到处是解释性的注释和文档字符串，里面会**原样写出**被检查的标识符。
    已经因此栽了三次：注释里的 return_exceptions、_save_view 文档串里的
    async_save_data、_auto_ticket_one 文档串里的 _ticket_seen ——
    每一次都让「某东西不该存在 / 某调用不该发生」的断言被自己的说明满足。
    所以凡是否定性断言，一律用这里产出的剥离版。
    """
    import ast as _ast
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        f = body[0]
        if isinstance(f, _ast.Expr) and isinstance(f.value, _ast.Constant) \
                and isinstance(f.value.value, str):
            body.pop(0)
    return _ast.unparse(tree)


CODE = strip_docs(code)


def body_code(src):
    """把从原文切出来的方法片段（0 缩进）包成合法类再剥注释/docstring。

    _tk / _ed 这些切片是从全文里 find 出来的，起点就是 def 那一行，
    缩进是 0 —— 直接拼到 "class X:\\n" 后面会 IndentationError。
    """
    indented = "\n".join(("    " + ln) if ln.strip() else ln
                         for ln in src.split("\n"))
    return strip_docs("class X:\n" + indented).split("\n", 1)[1]

# 期望值一律从本地文件推导，别再硬编码版本号——每次升版都忘改是常事
_local_idx = json.loads(io.open(ROOT + "package.v3.json", encoding="utf-8").read())
EXPECT_VER = _local_idx["ParkingBot"]["version"]
EXPECT_HIST = len(_local_idx["ParkingBot"]["history"])
_disk_code = io.open(ROOT + "plugins.v3/parkingbot/__init__.py", encoding="utf-8").read()
EXPECT_IN_CODE = re.search(r'plugin_version\s*=\s*"([\d.]+)"', _disk_code).group(1)

print("\n--- 代码内容 ---")
check(f'代码 plugin_version = "{EXPECT_IN_CODE}"',
      f'plugin_version = "{EXPECT_IN_CODE}"' in code)
check("代码 plugin_version 与索引 version 一致", EXPECT_IN_CODE == EXPECT_VER,
      f"{EXPECT_IN_CODE} vs {EXPECT_VER}")
check("换新已拆成独立方法",
      "async def _auto_trade_one" in code and "async def _auto_buy_one" in code)
check("旧 _auto_shop 已删除", "def _auto_shop" not in code)
check("换新排在挪车之后",
      code.index("await self._unpark_due_cars") < code.index("await self._auto_trade_one"))
check("游戏图片地址已绝对化", "_GAME_BASE" in code and "def _asset_url" in code)

print("\n--- 历次修复都还在（防回退） ---")
check("手动操作不再把空历史写进缓存",
      'cache["snapshot"] = snapshot' in code and "stored_h" in code)
check("限时车不能手动换新",
      "def _is_tradeable_idle" in code and "不能用来以旧换新" in code)
check("停放上限不再被重复扣减", "if recomputed else moved" in code)
check("刷新异常按失败处理", "刷新游戏状态异常" in code)
check("userId 统一按字符串比较", code.count('str(c.get("userId")) == str(user_id)') >= 3)
check("只构造当前页签的区块", "def _dict_rows" in code and "elif tab ==" in code)
check("今日收益按设定时区算", "today = cls._now_naive().strftime" in code)
check("_to_int 支持浮点", "int(float(str(value).strip()))" in code)
check("称号与特卖并行拉取", "asyncio.gather(" in code)
check("切称号就地改快照", "def _patch_snapshot_title" in code)
check("称号有 10 分钟缓存", "_TITLES_TTL = 600.0" in code)
check("车型表异常降级", "读取车型表失败，本轮按无车型表继续" in code)
check("指令异常不再无声丢失", "指令执行异常" in code)
check("图片地址引号已转义", "safe_url" in code)
check("挪车接口也收 Pydantic 模型",
      "async def api_unpark(self, request: CarMoveRequest)" in code)

print("\n--- 本次两处修复 ---")
check("手动买车/换车/抢购都会自己记账",
      code.count("self._shop_spent +=") >= 2 and "self._shop_diff += diff" in code)
check("花费并入当天统计不再被 record_history 分支跳过",
      "这一段必须放在 record_history 分支之外" in code)
check("backgroundColor 里不再有渐变（插件页渲染器不认）",
      not [ln for ln in code.splitlines()
           if "linear-gradient" in ln and "backgroundColor" in ln],
      [ln.strip()[:70] for ln in code.splitlines()
       if "linear-gradient" in ln and "backgroundColor" in ln][:3])
# v1.53.1 把选中色从 #7FF6FF 提到 #8DF3FF（提高对比度）。
# ★这里必须跟着改，否则 UI 已经改了而判据还钉着旧色 —— 一条永远红的断言
# 会把「真回归」也一起淹没，验收时就得靠肉眼分辨哪条红是真问题。
check("选中页签是高亮浅色文字（底色不渲染也读得清）",
      '"#8DF3FF" if active' in code and '"#7FF6FF"' not in code)
check("选中胶囊是高亮浅色文字", '"#FFC2CE" if active' in code)
check("🪙 只可能出现在注释里", not [ln for ln in code.splitlines()
                               if "\U0001FA99" in ln and not ln.strip().startswith("#")])

pkg = json.loads(fetch("package.v3.json") or "{}")
pb = pkg.get("ParkingBot") or {}
hist = pb.get("history") or {}
print("\n--- 图标 ---")
# 市场页只认两条路：值以 http 开头走后端代理，否则拼 ./plugin_icon/<值> 去读
# nginx 的本机静态目录。本插件用第一条，所以必须是完整 URL，且两处写法要一致。
_disk_icon = re.search(r'plugin_icon = \((.*?)\)\n', _disk_code, re.S)
_disk_icon_raw = ("(" + _disk_icon.group(1) + ")") if _disk_icon else ""
_disk_icon_val = ast.literal_eval(_disk_icon_raw) if _disk_icon else ""
_idx_icon = pb.get("icon")
check("代码 plugin_icon 是完整 URL",
      _disk_icon_val.startswith("https://"), _disk_icon_val)
check("代码 plugin_icon 指向仓库 icons/Parking_A.png",
      _disk_icon_val.endswith("/MoviePilot-Plugins/main/icons/Parking_A.png"),
      _disk_icon_val)
check("索引 icon 与代码 plugin_icon 完全一致", _idx_icon == _disk_icon_val,
      "索引=%r 代码=%r" % (_idx_icon, _disk_icon_val))
check("icon 以 http 开头才会走后端代理（否则退化成读主机目录）",
      bool(_idx_icon) and _idx_icon.startswith("http"))
_rd = fetch("plugins.v3/parkingbot/README.md") or ""
check("README 写清了图标的两条取法（含前端 startsWith 判断）",
      "startsWith('http')" in _rd and "getProxyImageUrl" in _rd
      and "plugin_icon" in _rd)

print("\n--- 贴条 ---")
check("贴条接口已注册", '"path": "/ticket"' in code and "def api_ticket" in code)
check("贴条调游戏 ticket.php 并透传原始车位 ID",
      '"spotId": raw_spot_id' in code)
check("贴条五道校验都在后端",
      all(s in code for s in ("只能给自己的车位贴条", "爱心车位不能贴条",
                              "车位上没有车", "自己停的车不用贴条", "停放不足")))
check("罚款按分成比例算并显示", '"ticketFine"' in code and "TICKET_OWNER_PERCENT" in code)
check("时间不够时按钮不挂事件", '"ticketReady"' in code and "not-allowed" in code)
check("贴条收入并进当日收益", "self._shop_fined" in code)

print("\n--- 车库手动停车（v1.21.0）---")
check("停车接口已注册", "def api_park(self" in code)
check("停车收 Pydantic 模型",
      "async def api_park(self, request: ParkRequest)" in code and "class ParkRequest" in code)
check("停车会拒绝非空闲车", "只有空闲车辆能停出去" in code)
check("停车会拒绝非本账号的车", "车辆不存在或不属于当前账号" in code)
check("停车挑的是他人的空余私家车位",
      'not s.get("carInSpot")' in code
      and 's.get("spotType") != "charity"' in code
      and 'str(s.get("ownerId")) != str(user_id)' in code
      and "random.choice(spots)" in code)
check("无可停车位时后端也拦一道", "当前没有可停的他人空余私家车位" in code)
check("车辆/车位 ID 原样透传不做字符串化",
      '{"userCarId": car.get("id"), "spotId": spot.get("id")}' in code)
check("停车成功后会刷新快照", 'self._refresh_snapshot(client, "手动停车")' in code)
check("车库卡片有停车按钮", "def _park_btn" in code)
check("快照记录 free_spots", '"free_spots"' in code)
check("车库副标题显示已停 N/上限",
      "当前已停" in code and "手动停车不受这个上限限制" in code)

print("\n--- 车库手动挪车（v1.23.0）---")
check("挪车按钮渲染方法已就位", "def _unpark_btn(" in code)
check("车库卡片接的是挪车按钮，不是旧的指令文字",
      "ParkingBot._unpark_btn(number, can_unpark, button_text," in code
      and '_command_hint(\n                            f"/挪车' not in code)
check("get_page 构造了 unpark 接口路径",
      'unpark_api = f"plugin/{pid}/unpark"' in code
      and "park_api, unpark_api)]" in code)
check("挪车按钮可点态挂事件、灰态不挂",
      'if can:\n            btn["events"] = {"click": {"api": api, "method": "post",'
      in code)
check("挪车按钮绿底，与停车的白底区分", "_GREEN if can" in code)
check("副标题措辞已从「指令」改成「按钮」",
      "分钟的车不能挪走，按钮会标灰" in code
      and "分钟的车不能挪走，指令会标灰" not in code)

print("\n--- 满员自动关自动买车（v1.25.0）---")
check("有独立的关闭开关方法", "def _disable_auto_buy_if_full(" in code)
check("满员分支调用它（不再静默跳过）",
      "return self._disable_auto_buy_if_full(len(my_cars), max_cars)" in code
      and 'if len(my_cars) >= max_cars:\n            return ""' not in code)
check("update_config 是整体覆盖，必须先取回完整配置",
      "config = self.get_config() or {}" in code
      and 'config["auto_buy"] = False' in code)
check("写失败不伪装成已关闭", "关闭自动买车失败" in code)
check("只关不开：没有把 auto_buy 改回 True 的路径",
      not re.search(r'config\["auto_buy"\]\s*=\s*True', code))
check("关完会通知一次",
      "车辆已满" in code and 'self.post_message(title=self.plugin_name, text=note)' in code)

print("\n--- 挪车按钮序号解析（v1.26.0）---")
_i = code.find("async def api_unpark")
_j = code.find("def _park_btn", _i)
_unpark = code[_i:_j] if _i > 0 else ""
check("挪车接口有序号解析（按钮传的是车库序号）",
      "self._resolve_car_ref(token, ordered)" in _unpark)
check("挪车走统一的车辆排序口径",
      "self._ordered_my_cars(state, cars_map)" in _unpark)
check("挪车复用 _load_game_context，不另造一套鉴权",
      "await self._load_game_context()" in _unpark)
check("挪车不再把 token 直接当实例 ID 匹配",
      'str(c.get("id")) == target' not in _unpark and "target" not in _unpark)
check("挪车只在自己名下的车里挑（归属校验没放松）",
      "for c in ordered" in _unpark and 'str(c.get("userId")) == str(user_id)' not in _unpark)
# 按钮仍然传序号，和后端解析口径要对得上。
# 注意切片只能到 _unpark_btn 后面紧跟的下一个方法：_garage 之前已经插了
# _garage_toolbar / _age_text，直接切到 def _garage 会把按钮本体整个切掉。
_ui = code.find("def _unpark_btn")
_uj = code.find("\n    @staticmethod", _ui)
_unpark_fn = code[_ui:_uj if _uj > _ui else len(code)]
check("挪车按钮仍传 car_id（序号）",
      '"params": {"car_id": number}' in _unpark_fn, len(_unpark_fn))
check("挪车与停车用的是同一套解析函数",
      "_resolve_car_ref" in code[code.find("async def api_park"):
                                 code.find("async def api_unpark")])

print("\n--- 立即刷新状态（v1.27.0）---")
check("刷新请求模型存在", "class RefreshRequest(BaseModel):" in code)
check("刷新接口已注册",
      '"path": "/refresh_status"' in code
      and '"endpoint": self.api_refresh_status' in code
      and "async def api_refresh_status(" in code)
check("车库工具栏与数据新鲜度文案",
      "def _garage_toolbar(" in code and "def _age_text(" in code
      and "self._garage_toolbar(snapshot, refresh_api)" in code)
check("刷新是纯只读：不挪车/停车/买车/换车",
      all(x not in code[code.find("async def api_refresh_status"):
                        code.find("def _unpark_btn")]
          for x in ('"unpark.php"', '"park.php"', '"buy_car.php"', '"trade_in.php"')))
check("刷新不写执行历史噪声行",
      "record_history=False" in code[code.find("async def api_refresh_status"):
                                     code.find("def _unpark_btn")])

print("\n--- 停放上限与请求数（v1.28.0）---")
# recomputed 只允许两处赋值：_recompute 置 True、run_once 开头初始化置 False。
# 曾经把初始化写在使用之后，标志被抹回 False，still_parked 重复扣减，
# 上限 2 会被突破到 4 —— 只在「自动换新」开着时才触发。
check("recomputed 的 False 赋值只有初始化那一处",
      code.count("recomputed = False") == 1, code.count("recomputed = False"))
check("recomputed 的 True 赋值只有 _recompute 那一处",
      code.count("recomputed = True") == 1, code.count("recomputed = True"))
_ri = code.find("        recomputed = False")
_rn = code.find("nonlocal parked, idle, charity, min_duration, target_minutes, recomputed")
_ru = code.find("still_parked = len(parked) - (0 if recomputed else moved)")
check("recomputed 在被读之前初始化", -1 < _ri < _rn < _ru, (_ri, _rn, _ru))
check("末尾落快照改看 state 是否陈旧，而非「动没动车」",
      "if not state_is_current:" in code and "if new_parked or moved:" not in code)
# 每个「改动服务端」的动作之后都要把 state 标记为陈旧，否则末尾会拿
# 动作之前的快照落盘，页面把刚动的车显示成旧状态。
# 买车 / 换新 / 挪车 / 停车 / 自动贴条 / 刷购车换新经验 —— 六个动作各一处。
# ★数量跟着新增动作走：v1.52.0 加了刷经验那一处，改常量不改这里就会红。
check("六个改动服务端的动作后都把 state 标记为陈旧",
      code.count("state_is_current = False") == 6,
      code.count("state_is_current = False"))

print("\n--- 脏数据健壮性（v1.29.0）---")
# int(float("inf")) 抛 OverflowError 而不是 ValueError，而 json.loads 默认接受
# Infinity / 1e400 并解析成 inf —— 只捕 (TypeError, ValueError) 会整页 500。
check("没有漏捕 OverflowError 的 except",
      "except (TypeError, ValueError):" not in code)
check("_to_int 兜住 OverflowError",
      "            except (TypeError, ValueError, OverflowError):\n"
      "                return default" in code)
check("_fmt_num 兜住 OverflowError",
      "        except (TypeError, ValueError, OverflowError):" in code)
check("特卖列表过滤了非字典项",
      'flash = self._dict_rows(snapshot.get("flash"))' in code)
check("车库 mins 走 _to_int 而不是裸 int()",
      'minutes = ParkingBot._to_int(item.get("mins"), 0)' in code
      and 'minutes = int(item.get("mins") or 0)' not in code)
check("换车选车型时 catalog 过滤了非字典项",
      'next((c for c in self._dict_rows(snapshot.get("catalog"))' in code)
check("统计页对 hours 做了类型校验（非空字符串是真值，or {} 兜不住）",
      "        if not isinstance(hours, dict):\n            hours = {}" in code)

print("\n--- 并发优化（v1.30.0）---")
# 手动操作原先 5.5 秒起步，瓶颈全是串行的网络往返；游戏站单请求约 1.1 秒。
check("state.php 与车型表并行拉取（两者互不依赖）",
      "state, cars_map = await asyncio.gather(" in code)
check("特卖/称号预取挂在取到 state 之后",
      "def _start_market_prefetch(" in code
      and "self._start_market_prefetch(client, state)" in code)
check("_record_round 取用预取结果而非重新发请求",
      "def _take_market_prefetch(" in code
      # 三元组：列表/称号之后是未读通知数。写成二元组会解包失败，
      # 整轮记账直接抛 —— 比「多发一次请求」严重得多。
      and "flash, titles, notif_count = await self._take_market_prefetch(client, state)"
      in code)
check("预取复用已在途任务，不会重复发请求",
      "if task is not None and not task.done():" in code)
check("预取有异常兜底（否则并发变日志噪音）",
      "def _drain_prefetch(" in code
      and "task.add_done_callback(ParkingBot._drain_prefetch)" in code)
check("预取失败会退回现拉，不打挂落快照",
      "预取失败，改为现拉" in code)

print("\n--- 车型表缓存 TTL（v1.31.0）---")
# 这个缓存原先没有失效时间：进程不重启就永远拿不到新车。2026-10-14 游戏一次就从
# 4 个分类加到 9 个（新增传奇车，Lv.15 起、916.5K），缓存不失效时车市页看着像没更新。
check("车型表缓存有 1 小时 TTL", "    _CARS_TTL = 3600.0" in code)
check("取用时按时间戳判过期",
      "time.monotonic() - self._cars_cache_at < self._CARS_TTL" in code)
check("成功取回后刷新时间戳",
      "self._cars_cache_at = time.monotonic()" in code)
check("插件重载时缓存连同时间戳清空",
      "self._cars_cache = {}\n        self._cars_cache_at = 0.0" in code)
check("失败路径不写缓存（仍只在这一处赋值）",
      code.count("self._cars_cache = cars") == 1, code.count("self._cars_cache = cars"))

print("\n--- 自动贴条（v1.32.x）---")
_tk = code[code.find("async def _auto_ticket_one"):code.find("async def _auto_buy_one")]
# 剥掉注释/docstring 的版本，给否定性断言用（理由见 strip_docs 的说明）
_tkc = body_code(_tk)
check("自动贴条方法在（且不与配置字段同名）",
      "async def _auto_ticket_one(" in code
      and "def _auto_ticket(self" not in code)
check("配置项 auto_ticket / ticket_min_fine 都在",
      '"model": "auto_ticket"' in code
      and '"model": "ticket_min_fine"' in code)
check("类属性默认值兜底（未初始化实例不该 AttributeError）",
      "    _auto_ticket: bool = False" in code
      and "    _ticket_min_fine: int = 0" in code)
for _frag, _why in (('if spot.get("spotType") == "charity":', "爱心车位排除"),
                    ('if str(car.get("userId")) == str(user_id):', "自己车不贴"),
                    ("if mins < min_ticket:", "时长门槛"),
                    ("if fine < threshold:", "阈值门槛")):
    check("保护：%s" % _why, _frag in _tk)
# v1.48.0：去重整条规则去掉了，游戏自己就没有这个限制（见 _auto_ticket_one 的说明）。
# 判定只剩「停够时长」和「达到门槛」两条。
check("★去重判定已彻底移除（同一辆车还停着就照贴）",
      "_ticket_seen" not in _tkc and "_ticket_seen" not in CODE)
check("★不再有 _ticket_seen 的清理与落盘",
      "before_seen" not in CODE and "_persist_ticket_seen" not in CODE
      and "ticket_seen" not in CODE)
check("★判定仍然只有时长与门槛两条，没被误删",
      "if mins < min_ticket:" in _tkc and "if fine < threshold:" in _tkc)
check("自动贴条排在挪车之后、停车之前",
      code.index("await self._auto_ticket_one(")
      < code.index("new_parked = await self._park_idle_cars(")
      and code.index("earned_total, moved = await self._unpark_due_cars(")
      < code.index("await self._auto_ticket_one("))

print("\n--- 贴条收入分项与柱状图悬停（v1.33.0）---")
# 贴条罚款是「收入」，但统计页要能单独看见这一块，所以同一天要记两份：
# fined 单独存一份给展示用，earned 照常并入总收益。只并进 earned 的话，
# 页面上那笔钱和挪车收益混成一坨，用户永远看不出贴条到底赚了多少。
check("贴条罚款单独记进 fined",
      'bucket["fined"] = self._to_int(bucket.get("fined"), 0) + self._shop_fined' in code)
check("贴条罚款同时并进 earned（否则当日净收会算少）",
      'bucket["earned"] = self._to_int(bucket.get("earned"), 0) + self._shop_fined' in code)
_ri_f = code.find('bucket["fined"]')
check("累计值每轮清零（否则手动贴条会跨轮重复记账）",
      "self._shop_fined = 0" in code[_ri_f:_ri_f + 400])
check("统计页有贴条取数函数 day_fined", "def day_fined(" in code)
check("day_fined 对老存档缺字段按 0 算（不能抛）",
      'return self._to_int(b.get("fined"), 0)' in code)
check("当日/本月各有一个「其中贴条」",
      code.count('_stat("其中贴条"') == 2, code.count('_stat("其中贴条"'))
# 常量必须是顶格模块级定义：缩进到类里的话 _stats_page 取不到，NameError 整页 500
_tc = [ln for ln in code.splitlines() if ln.startswith("_TICKET_C")]
check("_TICKET_C 是模块级定义", len(_tc) == 1 and _tc[0].strip() == '_TICKET_C = "#FF7A9C"',
      _tc)
check("两处「其中贴条」都用了 _TICKET_C",
      code.count('"其中贴条", day_fined(current), _TICKET_C') == 1
      and code.count('"其中贴条", sum(day_fined(d) for d in month_days), _TICKET_C') == 1)
check("柱子带逐小时悬停文案", "— 收益 " in code and "— 无收益" in code)
check("柱子给了 cursor:help 暗示可悬停", '"cursor": "help"' in code)
check("README 写进了贴条分项与悬停说明",
      "其中贴条" in _rd and "悬停" in _rd)

print("\n--- UI 改版与爱心车位切换（v1.34.0）---")
# 新接口必须和游戏前端 Game.prototype.toggleCharitySpot 逐字对齐：
# 端点名 charity_spot.php、参数 {spotId, action}。端点名写错按钮就点不动。
check("爱心车位切换接口已注册",
      '"path": "/charity_spot"' in code and '"endpoint": self.api_charity_spot' in code)
check("请求体模型齐全",
      "class CharitySpotRequest(BaseModel):" in code
      and 'spot_id: Any = Field(' in code and 'action: str = Field(' in code)
check("调的是游戏真实端点 charity_spot.php",
      "charity_spot.php" in code and '{"spotId": raw_spot_id, "action": action}' in code)
# 服务端五道校验一道都不能少（页面按钮只是入口，不能只靠前端拦）
for _frag, _why in (('if str(spot.get("ownerId")) != str(user_id):', "只允许操作自己的车位"),
                    ('if spot.get("carInSpot"):', "车位上有车不可切换"),
                    ('if action not in ("set", "unset"):', "action 白名单"),
                    ('if not spot:', "车位存在性"),
                    ("await self._refresh_snapshot(client, ", "成功后刷新快照")):
    check("爱心车位校验：%s" % _why, _frag in code)
check("切换按钮只画在自己的空车位上",
      'if "charity" in spot and charity_api:' in code)
check("快照在提前 return 之前就带上 charity 字段",
      code.index('"charity": charity') < code.index("# 空车位提前 return")
      if "# 空车位提前 return" in code else True)

# UI 三件
# v1.53.2 把一级页签改成两字短文案（车位/车库/车市/数据/装扮），
# 5 个标签在窄屏才排得进单行。
check("页签文案已去掉 ①②③④ 编号",
      '("spots", "车位")' in code and '("garage", "车库")' in code
      and not any(x in code.split("_TABS = ")[1][:240] for x in "\u2460\u2461\u2462\u2463"))
check("两类车位卡都统一 minHeight 295px",
      code.count('"minHeight": "295px"') >= 2, code.count('"minHeight": "295px"'))
check("boxSizing 显式声明（宿主的 border-box reset 会改写 minHeight 含义）",
      code.count('"boxSizing": "border-box"') >= 2)
check("空闲卡「空闲」与按钮之间有下间距",
      '"marginBottom": "12px"' in code
      and '"marginBottom": "12px"' in code[code.find('ParkingBot._text("空闲"'):][:600])
# 每小时收益图：早前那版从「SVG 折线 + 渐变面积」换成了 24 根 div 竖柱，
# 理由写在 _hour_chart 的注释里（手机宽度下 6px 圆点 + 14px 间距会连成毛边，
# 斜率还得读者自己做除法）。断言要跟着改成盯竖柱那套结构。
_hc = src_seg(code, "_hour_chart")[1]
check("每小时收益已是 24 根竖柱（不再用 SVG 折线）",
      '"maxWidth": "9px"' in _hc
      and '— 收益 {ParkingBot._fmt_num(value)} 金币' in _hc
      and "<polyline" not in _hc,
      [_l.strip()[:60] for _l in _hc.split("\n")
       if "maxWidth" in _l or "收益 {" in _l][:3])
check("悬停三要件仍齐（title 文案逐字未改 / cursor:help / 24 条热区）",
      code.count('— 收益 {ParkingBot._fmt_num(value)} 金币') == 1
      and code.count('— 无收益') == 1 and '"cursor": "help"' in code)

print("\n--- 概览卡 / 指标块 / 卡片外壳 / 新鲜度角标（v1.35.0）---")
# 极光顶线。注意 overflow:hidden 全文两处（_table 单元格 + 概览卡），
# 按计数断言会被表格那处冒充通过，必须按注释锚点定位概览卡那一处。
check("概览卡有极光顶线（青→紫 90deg）",
      "linear-gradient(90deg,#22D3EE,#A78BFA)" in code)
check("顶线用负外边距顶到卡缘", '"margin": "-16px -16px 14px"' in code)
check("概览卡那处 overflow:hidden 有注释锚点（防止被 _table 的同值冒充）",
      '# 顶线用负外边距顶到卡片两缘' in code
      and '"overflow": "hidden",' in code[code.find("# 顶线用负外边距顶到卡片两缘"):][:120])
check("玩家名放大到 21px", '"fontSize": "21px"' in code)
check("概览卡四边等距 16px", '"padding": "16px"' in code)

# 新鲜度角标：这一版修掉的是「只会说分钟」的退化
_g = code[code.find("def _humanize_gap("):code.find("def _freshness_text(")]
check("有时长换算的共用函数", "def _humanize_gap(" in code)
check("换算按分钟/小时分档", "if minutes < 60:" in _g)
check("换算带零头分钟（★不再出现「1595 分钟前」）",
      'f"{hours} 小时 {rest} 分钟前"' in _g)
_fx = code[code.find("def _freshness_text("):code.find("def _garage_toolbar(")]
check("角标走共用换算", "ParkingBot._humanize_gap(minutes)" in _fx)
check("角标不再残留只报分钟的写法", 'f"{minutes} 分钟前更新"' not in _fx)
check("数据缺失/格式坏/时间在未来时角标都不渲染",
      _fx.count('return ""') >= 3, _fx.count('return ""'))
_at = code[code.find("def _age_text("):code.find("def _garage_toolbar(")]
check("车库工具条也走同一份换算（口径不该分叉）",
      "_humanize_gap(minutes)}更新" in _at)

# 元信息胶囊
check("有元信息胶囊构造方法",
      "def _meta_pill(" in code and "def _meta_row(" in code)
check("胶囊是 999px 药丸", '"borderRadius": "999px"' in code)

# 指标块
_s = code[code.find("def _stat("):code.find("def _card(")]
check("指标块有左侧语义色条", '"width": "3px"' in _s and '"height": "34px"' in _s)
check("色条跟着调用方的语义色", '"backgroundColor": color' in _s)
check("指标数值等宽（tabular-nums）", "tabular-nums" in _s)
# ★圆角被收口成常量 R_SM/R_MD/R_CARD/R_FULL 之后，字面量 "14px" 在
# _stat 里找不到了 —— 但**设计锁定值没变**，R_CARD 就是 "14px"。
# 所以这里既要查引用、也要查常量的值，只查字面量会把「收口」误判成「被改小」。
check("指标块圆角 14px 是设计稿锁定值",
      '"borderRadius": R_CARD' in _s
      and re.search(r'R_SM,\s*R_MD,\s*R_CARD,\s*R_FULL\s*=\s*'
                    r'"6px",\s*"10px",\s*"14px",\s*"999px"', code) is not None)
check("大紫底已绝迹", "rgba(167,139,250,0.14)" not in _s)

# 卡片外壳
_c = code[code.find("def _card("):code.find("def _meta_pill(")]
check("_card 有 accent 参数（老调用有默认值，不炸）", "accent: str = " in _c)
check("标题圆点用传入的 accent", '"backgroundColor": accent' in _c)
# ★标题语义标记从 8×8 圆点换成了 3×16 竖条（8×8 在 14px 圆角卡里几乎看不见，
# 且与 _stat 左侧色条不统一）。判据跟着改盯竖条，别再钉 8px。
check("卡片标题有语义色条（3×16 竖条，非旧 8px 圆点）",
      '"width": "3px"' in _c and '"height": "16px"' in _c
      and '"width": "8px"' not in _c)
check("卡片四边等距 16px（旧的不对称底距已绝迹）",
      '"padding": "16px"' in _c and "14px 14px 4px" not in code)

# 页签栏内嵌槽，且锁定色没被动
# ★_nav_row 已改成只把 _TABS 翻译成 (key,label,icon) 再委托 _segmented ——
# 样式全搬到 _segmented 了（一级/二级共用一套分段控件）。
# 断言还去 _nav_row 里找颜色，**永远找不到**，等于两条红着不给任何信息。
_n = src_seg(code, "_segmented")[1]
check("_nav_row 确实委托给了 _segmented（样式不在它这儿了）",
      "self._segmented(" in code[code.find("def _nav_row("):code.find("def get_page(")])
check("分段控件套了内嵌槽（选中实底 + 未选中透明）",
      "backgroundColor\": SEL_BG if active else \"transparent\"" in _n)
check("页签锁定配色没被动",
      '"color": "#8DF3FF" if active' in _n
      and 'rgba(34,211,238,0.72)' in _n
      and '"#7FF6FF"' not in code)

print("\n--- 按下反馈与大整数兜底（v1.36.0）---")
# 按下反馈：插件元素没有 class，宿主的样式表里就没有任何 :hover/:active 规则可选，
# 单独写 transition 是死的（transition 只在属性值变化时才动）。
# 所以只能靠 html 字段注入一段 <style>，并用 .pb-tap 前缀把作用域限死。
check("有按下反馈的样式常量", "_TAP_CSS = (" in code)
_css = code[code.find("_TAP_CSS = ("):code.find("_TAP_CSS = (") + 600]
check("hover 有反馈", ".pb-tap:hover" in _css and "brightness" in _css)
check("active 有按下缩放", ".pb-tap:active" in _css and "scale(" in _css)
check("有 transition 过渡", "transition:" in _css)
check("尊重 prefers-reduced-motion", "prefers-reduced-motion" in _css)
# 作用域：不能有全局选择器，否则会波及同一弹窗里的其他插件
check("CSS 里没有全局选择器（body/html/*）",
      not re.search(r"(^|[}\s])(body|html|\*)\s*[,{]", _css))
check("hover/active 走 filter/transform（GPU 合成，不触发重排）",
      "filter:" in _css and "transform:" in _css and "background" not in _css)
check("有递归标记方法", "def _mark_tappable(" in code)
_mt = code[code.find("def _mark_tappable("):code.find("def _mark_tappable(") + 1300]
check("有幂等守卫（pb-tap 不会挂两次）", '"pb-tap" not in cur' in _mt)
check("样式节点插在页面最前面", "([style_node] + nodes)" in _mt)

# _fmt_num：int(float("inf")) 那条 v1.29.0 修过，大整数真除是第二条路径，一直漏着
_fn = code[code.find("def _fmt_num("):code.find("def _fmt_num(") + 1900]
check("★缩放包了 OverflowError（大整数不再让整页白屏）",
      "except OverflowError:" in _fn)
check("兜底走整除（对大整数永远安全）",
      "mag // 1000000" in _fn and "mag // 1000" in _fn)
check("正常值的四舍五入路径原样保留",
      'f"{number / 1000000:.1f}M"' in _fn and 'f"{number / 1000:.1f}K"' in _fn)
check("K 分支门槛仍是 number >= 10000（负数不会被误缩写）",
      "if number >= 10000:" in _fn)

print("\n--- 贴条记账（v1.37.0：修「其中贴条」一直是 0）---")
# 用户报的现象是统计页贴条分项恒为 0。根因两条，都在记账侧：
#   A. 自动贴条把罚款累进局部 total，只拼进日志文案，从没加过 _shop_fined；
#   B. 手动贴条累加了 _shop_fined 却不立刻落盘，要等下次定时任务才写进 daily
#      （跨天会记错日期，中途重启则永久丢失）。
_LINES = code.split("\n")


def _method_body(name):
    s = next(i for i, ln in enumerate(_LINES)
             if ln.startswith("    async def %s(" % name))
    e = next((i for i in range(s + 1, len(_LINES))
              if _LINES[i].startswith("    async def ")
              or _LINES[i].startswith("    def ")), len(_LINES))
    return "\n".join(_LINES[s:e])


_auto = _method_body("_auto_ticket_one")
check("★自动贴条把罚款并进 _shop_fined（bug A 的修复）",
      "self._shop_fined += got" in _auto)
check("★罚款累加仍在「贴成功」分支内，且排在追加摘要之前",
      _auto.find("self._shop_fined += got") > _auto.find('if not (result and result.get("success"))')
      and _auto.find("self._shop_fined += got") < _auto.find("notes.append("),
      (_auto.find("self._shop_fined += got"), _auto.find("notes.append(")))
_ticket = _method_body("api_ticket")
check("★手动贴条累加后立刻落盘（bug B 的修复）",
      "self._shop_fined += fine" in _ticket and "_record_round(" in _ticket
      and _ticket.find("_record_round(") > _ticket.find("self._shop_fined += fine"))
check("手动贴条落盘不写执行历史噪声行", "record_history=False" in _ticket)
# 记账侧本身没被破坏
_rr = _method_body("_record_round")
check("统计页读 fined 的口径没变", 'def day_fined(' in code and 'b.get("fined"), 0' in code)
check("fined 同时并进 earned（当日净收口径不变）",
      'bucket["fined"] = self._to_int(bucket.get("fined"), 0) + self._shop_fined' in _rr
      and 'bucket["earned"] = self._to_int(bucket.get("earned"), 0) + self._shop_fined' in _rr)

print("\n--- ticket.php 返回字段兼容（v1.38.0）---")
# 游戏 2026-10 把 ticket.php 的返回从 fine 改成 ownerEarn（同一笔钱，只换了名字
# 和文案）。插件只读一个就会静默记成 0：贴条成功、不报错、统计恒为 0。
# 证据在游戏前端 JS 里两个版本并存：旧版 toast「罚款 N 金」红字读 data.fine，
# 新版 toast「获得 N 金币」金字读 data.ownerEarn。
for _fn, _expr in (("_auto_ticket_one", "self._to_int(raw_earn, fine, minimum=0)"),
                   ("api_ticket", "self._to_int(raw_earn, 0, minimum=0)")):
    _b = _method_body(_fn)
    check("%s 读新字段 ownerEarn（★本次修复）" % _fn,
          'result.get("ownerEarn")' in _b)
    check("%s 回退读旧字段 fine" % _fn, 'result.get("fine")' in _b)
    check("%s 新字段优先（先 ownerEarn 再 fine）" % _fn,
          _b.find('result.get("ownerEarn")') < _b.find('result.get("fine")'),
          (_b.find('result.get("ownerEarn")'), _b.find('result.get("fine")')))
    check("%s 用上了兼容后的 raw_earn" % _fn, _expr in _b)
    check("%s 没退回直接读单个字段" % _fn,
          'result.get("fine"), 0, minimum=0' not in _b
          and 'result.get("fine"), fine, minimum=0' not in _b)

print("\n--- 手动操作的往返次数（v1.39.0）---")
# 实测（单请求 1.1 秒）：手动贴条原先 8.85 秒 / 10 次往返，是挪车（1.12 秒）的八倍。
# 三条成因：没用并行封装、_refresh_snapshot 内部二次 _record_round、
# 拼通知文案时单独再拉一次车型表。
for _fn in ("api_ticket", "api_charity_spot"):
    _b = _method_body(_fn)
    _bc = "\n".join(x for x in _b.split("\n") if not x.strip().startswith("#"))
    check("%s 用并行封装 _load_game_context（★往返的根因）" % _fn,
          "_load_game_context()" in _bc)
    check("%s 不再自己串行拉 state.php" % _fn,
          "AsyncRequestUtils(cookies=cookie" not in _bc)
    check("%s 不走 _refresh_snapshot（它内部会二次 _record_round）" % _fn,
          "_refresh_snapshot(" not in _bc)
    check("%s 记账只做一次且不写执行历史" % _fn,
          _bc.count("_record_round(") == 1 and "record_history=False" in _bc,
          _bc.count("_record_round("))
_bt = "\n".join(x for x in _method_body("api_ticket").split("\n")
                if not x.strip().startswith("#"))
check("贴条文案复用第一跳的 cars_map（不再单独拉车型表）",
      "self._car_name(car, cars_map)" in _bt
      and "await self._load_cars_map(client)" not in _bt)
_gc = _method_body("_load_game_context")
check("并行封装里 state 与车型表仍是 gather 并行", "asyncio.gather(" in _gc)
check("并行封装会顺带启动市场预取（让后面的 _record_round 命中缓存）",
      "_start_market_prefetch(" in _gc)
check("_refresh_snapshot 保留给停车/买车/换车/称号/抢购",
      code.count("_refresh_snapshot(") >= 5,
      code.count("_refresh_snapshot("))
# 字段兼容没被这次性能改动碰掉
check("★贴条字段兼容仍在（ownerEarn / fine 都读）",
      'result.get("ownerEarn")' in _method_body("api_ticket")
      and 'result.get("fine")' in _method_body("api_ticket"))

print("\n--- 经验值展示（v1.40.0）---")
check("ExpDetailRequest 声明了 open 开关（没有它面板收不起来）",
      re.search(r"class ExpDetailRequest\b.*?open:\s*bool", code, re.S) is not None)
check("/exp_detail 路由已注册", '"path": "/exp_detail"' in code)
_ed = _method_body("api_exp_detail")
_edc = body_code(_ed)
check("★api_exp_detail 按 open 翻转，不是写死 True",
      '_exp_open = bool(getattr(request, "open", False))' in _ed)
# v1.48.0：经验值面板不再持久化，用户要求每次打开页面默认收起。
check("★经验值面板不落盘，且不从存档读回",
      "_save_view(" not in _edc
      and 'saved_view.get("exp_open")' not in code
      and re.search(r"^\s{8}self\._exp_open = False\s*$", code, re.M) is not None)
check("_exp_open 类属性默认 False（详情默认折叠、点开才展开）",
      re.search(r"^\s{4}_exp_open = False\s*$", code, re.M) is not None)
check("★百分比分母是本级跨度，不是下一级门槛（否则 73% 变 91%）",
      'pct = 100 if span <= 0 else round((exp - out["base"]) / span * 100)' in code)
check("等级表按 level 升序、滤掉非法行",
      'out.sort(key=lambda x: x["level"])' in code)
check("快照落盘 exp / levelTable / expConfig / dailyExp",
      all(('"%s":' % k) in code
          for k in ("exp", "levelTable", "expConfig", "dailyExp")))
check("面板带「收起」出口",
      'cls._action_btn("收起", api, {"open": False},' in code)
check("面板挂载受 _exp_open 控制，且挂在名字行之后",
      "if snapshot and self._exp_open:" in code
      and "head_lines.append(self._exp_panel(snapshot, _exp_api))" in code)
check("经验值格与详情面板共用同一个 api（各算一次会出现两个不同路径）",
      '_exp_api = f"plugin/{_pid_now}/exp_detail"' in code
      and "self._exp_bar(snapshot, _exp_api)" in code)
check("经验值格排进名字行，而不是浮在页面别处",
      "name_row.append(self._exp_bar(snapshot, _exp_api))" in code)

print("\n--- 经验值触发器与通知铃铛（v1.41.0）---")
# ★ v1.40.0 线上真 bug：等级格发 params:{}，api 读 request.open 取默认 False，
# 于是每次点击都把展开状态设成 False（本来就是），页面毫无变化 = 「点了没反应」。
# 当时只断言了 api 和 method，没断言 params，所以放过去了。
check("★等级格的 click 明确带 open=True",
      '"params": {"open": True}}' in code)
check("★铃铛的 click 明确带 open=True",
      'name_row.append(self._notif_bell(snapshot, _notif_api))' in code
      and '"content": inner,\n            "events": {"click": {"api": api, "method": "post",'
          '\n                                 "params": {"open": True}}},' in code)
check("铃铛排在经验值格之后（名字 → 等级 → 通知，和游戏顶栏同序）",
      code.find('self._exp_bar(snapshot, _exp_api)')
      < code.find('self._notif_bell(snapshot, _notif_api)'))
check("/notif_detail 路由注册并指向 api_notif_detail",
      '"path": "/notif_detail"' in code
      and '"endpoint": self.api_notif_detail' in code)
check("★通知取数只用 GET：未读数走 count=1，列表走 perPage+category",
      'notifications.php?count=1' in code
      and '?perPage={self._NOTIF_PAGE}&category=all' in code)
check("★清空通知插件不做：代码里没有 action=clear",
      '"clear"' not in re.sub(r"(?m)#.*$", "", code))
check("唯一那个写操作只有全部已读，且 action 名正确",
      '"action": "read_all"' in code
      and len(re.findall(r"post_json\(\s*f\"\{self\.API_BASE\}/notifications\.php",
                         re.sub(r"(?m)#.*$", "", code), re.S)) == 1)
check("★未读数取不到返回 None，不回落成 0（否则角标凭空消失）",
      '读取未读通知数失败（沿用上一次）' in code
      and "async def _load_notif_count" in code
      and "-> Optional[int]:" in code)
check("未读数挂进特卖/称号那个 gather，不额外多一次往返",
      "self._load_notif_count(client)" in code
      and "flash, titles, notif_count = await self._take_market_prefetch(client, state)"
      in code)
check("15 种 notify_type 都有中文名与配色",
      len(re.findall(r'"[a-z_]+":\s*\("[^"]+",\s*"#', code)) == 15,
      len(re.findall(r'"[a-z_]+":\s*\("[^"]+",\s*"#', code)))
check("快照落盘 notifUnread，且这轮取不到时沿用上一次",
      '"notifUnread": notif_unread,' in code
      and 'prev.get("notifUnread")' in code)
# v1.45.0 把守卫从 `if err_text:` 改成了 `if failed:`（失败判据要先算出来才能用），
# 所以这里按「守卫必须排在写快照之前」的意图断，不再硬编码守卫变量名。
# 用 find 不用 index：定位不到时 find 返回 -1，不会把整个核对脚本带崩。
_i_fail = code.find("        if failed:")
_i_write = code.find("self._write_notifications(rows, 0)")
check("全部已读失败必须提前返回，不动本地快照",
      _i_fail > 0 and _i_write > 0 and _i_fail < _i_write,
      (_i_fail, _i_write))
# ★ v1.45.0 修的回归：游戏成功响应是
#   {'success': True, 'message': '全部标记已读'}
# message 是成功提示不是错误。把它当失败判据 → 每次成功都判失败、快照一个字不写，
# 页面看着和「点了没反应」一模一样。
# 注释已剥掉，所以「注释里提到 message」不算违规，只有真的把 message 取出来当判据才算。
_code_nc = re.sub(r"(?m)#.*$", "", code)
check("★成功判据绝不把 message 当错误（v1.45.0 的真回归）",
      'result.get("message")' not in _code_nc
      and 'result["message"]' not in _code_nc,
      [ln.strip()[:60] for ln in _code_nc.splitlines()
       if "message" in ln and "get(" in ln][:3])
# 光断「代码里出现过 get("error")」不够：err_text 那一行本来就一直在，
# 哪怕 failed 完全不看 error，断言也照样过（反向验证实测踩到过）。
# 注意不能只用 re.search 取第一处 —— `failed = result is None` 在前面，
# 真正决定成败的是 isinstance 分支里那次赋值。要全部取出来逐个看。
_failed_lines = re.findall(r"(?m)^\s*failed\s*=\s*(.+)$", code)
check("★失败判据同时看 error 与 success is False",
      any("err_text" in x and 'result.get("success") is False' in x
          for x in _failed_lines)
      and 'err_text = str(result.get("error") or "")' in code,
      [x.strip() for x in _failed_lines])
check("★全部已读的真实响应一定写进日志（否则「点了没反应」无从查起）",
      "全部已读响应：" in code)
# ★ v1.44.0 修的真 bug：只 async_save_data 改库不够。页面渲染走 _page_data()，
# 它优先读进程内缓存 _page_cache —— 库里已经是 0 了，页面读的还是内存里那份 10。
check("★写通知时内存缓存与数据库一起更新（照 _patch_snapshot_title 的模式）",
      "self._page_cache = cache" in code
      and 'await self.async_save_data("snapshot", snapshot)' in code
      and "async def _write_notifications(" in code)
check("★全部已读强制清零未读数，不让「按列表现算」把 0 变回去",
      "await self._write_notifications(rows, 0)" in code)
check("通知面板带「全部已读」与「收起」两个按钮，且无未读时不给按",
      '"全部已读", api, {"open": True, "read_all": True}' in code
      and 'cls._action_btn("收起", api, {"open": False},' in code)
check("★_notif_open 类属性与 init_plugin 都是 False（每次打开默认收起）",
      re.search(r"^\s{4}_notif_open = False\s*$", code, re.M) is not None
      and 'saved_view.get("notif_open")' not in code
      and re.search(r"^\s{8}self\._notif_open = False\s*$", code, re.M) is not None)

print("\n--- 经验值数据源改为实测口径（v1.42.0）---")
# v1.40/v1.41 里的 levelTable / expConfig 是从 state.php 读的，字段名是猜的。
# 实测 game_data.php 之后确认：这两个字段只存在于 game_data.php，state.php 不返回；
# expConfig 的每日上限在 xxxDailyLimit 那一组，dailyExp 的键是复合的。
check("★levelTable / expConfig 读的是 game_data 缓存，不是 state",
      '"levelTable": self._exp_level_table(self._game_data or {}),' in code
      and '"expConfig": self._exp_config(self._game_data or {}),' in code)
check("★game_data 与车型表同一次请求拿回，一并存进缓存（不多发请求）",
      "self._game_data = data" in code and "_game_data: Optional[Dict[str, Any]] = None"
      in code)
check("★game_data 缓存随插件重载清空", "self._game_data = None" in code)
check("★每日上限取自 xxxDailyLimit（不是 park=15 这种单次经验）",
      'cls._to_int(cfg.get(key + "DailyLimit"), fallback, minimum=0)' in code)
check("★dailyExp 靠前缀/后缀剥离来拆，不靠猜分隔位置",
      'prefix = f"{user_id}_" if user_id is not None else ""' in code
      and 'if action not in known:' in code)
check("★charity_park 是独立动作，不与 park 混淆",
      '("charity_park", "爱心停车", 50),' in code
      and '("park", "停车", 100),' in code)
check("★7 个来源与游戏 showExpDetail 的 actions 数组逐条对齐",
      re.findall(r'\("(\w+)", "[^"]+", (\d+)\),', code)[:7]
      == [("park", "100"), ("ticket", "60"), ("report", "200"), ("buy", "150"),
          ("tradeIn", "200"), ("charity_park", "50"), ("charity", "200")],
      re.findall(r'\("(\w+)", "[^"]+", (\d+)\),', code)[:7])
check("expConfig 整个缺失时退回游戏默认值，恒 7 行不��行",
      "if not cfg:\n            cfg = cls._exp_config({})" in code)
check("★缺今日经验显示「—/上限」而不是把上限也吞掉",
      'f"—/{ParkingBot._fmt_num(limit)}" if got is None' in code)
check("进度条按百分比分三档色，与游戏一致",
      'def _exp_bar_color(cls, percent: int)' in code
      and '_EXP_BAR_FULL = "#e94560"' in code
      and '_EXP_BAR_HIGH = "#ff9800"' in code
      and '_EXP_BAR_LOW = "#4caf50"' in code)
check("★来源区在 for 循环外面（缩进错会让同一批行重复渲染 7 遍）",
      re.search(r"items\.append\(line\)\n\n        # 这一整块必须在 for", code) is not None)
check("通知列表请求带 category=all（游戏那边每次都带）",
      '?perPage={self._NOTIF_PAGE}&category=all' in code)
check("等级与 Lv.N 在同一个横排节点里（照抄游戏顶栏）",
      '"display": "inline-flex", "alignItems": "baseline",' in code
      and 'f"Lv.{pg[\'cur\']}"' in code)
check("等级格整体仍是竖排：等级一行、进度条一行",
      '"display": "inline-flex", "flexDirection": "column",' in code)

print("\n--- 索引内容 ---")
check(f"version = {EXPECT_VER}", pb.get("version") == EXPECT_VER, pb.get("version"))
check(f"changelog {EXPECT_HIST} 条", len(hist) == EXPECT_HIST, len(hist))
check("changelog 首条就是当前版本", next(iter(hist), "") == "v" + EXPECT_VER,
      next(iter(hist), ""))
for k in ("v" + EXPECT_VER, "v1.20.0", "v1.17.0", "v1.16.0"):
    check(f"{k} 说明存在", k in hist and len(hist[k]) > 5, hist.get(k))
check("每条说明都非空", all(len(v.strip()) >= 3 for v in hist.values()),
      [k for k, v in hist.items() if len(v.strip()) < 3])
check("ParkingBot 仍在索引", "ParkingBot" in pkg)

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)