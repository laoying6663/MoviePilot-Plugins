# -*- coding: utf-8 -*-
"""
反向验证 v1.50.0：停车优先停进高场景加成的车位。

沿用 v1.49.0 定下的规矩：
* 判据是**点名的那条断言**变红 + rc != 0 + 没有 Traceback；
  只有「删掉 state 异常归位」那种天然会崩的注入才走 crash 判据。
* 注入必须**只改变想测的那一个维度**。第一版把「就地跳过换车位重试」
  一起注掉了，连带把「名额用满 3 个不同主人」也打红 —— 那是注入的副作用，
  不是被测语义，分不清就等于没测。
* 验收值（_PARK_MAX_PER_OWNER = 2）必须钉字面量。这轮特意先钉了才注入：
  不钉的话下面所有行为断言都拿常量现算，改成 99 照样全绿。
"""
import io
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")


def _find_root():
    """向上找含 plugins.v3/parkingbot/__init__.py 的目录。

    ★别写死绝对路径（原来写的是 D:\\minimax\\parkingbot）：脚本要能原样搬进
    仓库的 tests/ 下、也能在交接副本里跑。写死就等于把可移植性换成一条
    「只能在某台机器上跑」的假便利。
    与 _verify_170.py 的同名函数保持一致。
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


ROOT = _find_root()
PLUGIN = os.path.join(ROOT, "plugins.v3", "parkingbot", "__init__.py")
# 被测脚本在本地布局里是项目根、在仓库布局里是 tests/ 下，两处都找。
# 只认项目根的话，仓库里克隆下来直接找不到被测脚本。
_try = [os.path.join(ROOT, "_verify_170.py"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "_verify_170.py")]
TARGET = next((p for p in _try if os.path.isfile(p)), _try[0])

assert os.path.basename(os.path.abspath(__file__)) != "_verify_170.py", \
    "harness 不能和被测脚本同名"
ORIGINAL = io.open(PLUGIN, encoding="utf-8").read()

CAP_SKIP = (
    '                if per_owner.get(cand_owner, 0) >= self._PARK_MAX_PER_OWNER:\n'
    '                    blocked_owners.add(cand_owner)\n'
    '                    blocked_count += 1\n'
    '                    continue\n')
RETRY = ('                spot, owner_key = cand, cand_owner\n'
         '                break\n')

CASES = [
    # want 要跟着**实际会红的那条**走。原先写的是「优先停进加成最高的车位」，
    # 但纯随机时第一辆车恰好落在 5% 位上是会发生的（约 1/3），
    # 判据就漏了。改成必然红的那条。
    # 这三条的锚点随 v1.51.0 改过：候选池拆成 private_targets / charity_targets，
    # 排序只作用在私家那部分（爱心车位不产金币，场景加成对它无意义）。
    # 注入片段必须跟着当前原文走，否则只会「定位失败」——那不算抓到。
    # ★want 必须挑**确定性**的那条断言。
    # 「停够车位后按加成从高到低铺开」和「第一个用满的仍是加成最高的那个主人」
    # 都是**概率性**的：上限改成每家 1 辆之后只剩 3 个主人，
    # 没有排序时第一个落点纯随机，通过率分别只有 1/6 和 1/3 ——
    # 同一份代码上一轮 OK、下一轮红。概率性断言不能当 want。
    # 「固定 12 个随机种子」那条是确定的：种子固定后两种实现的结果都不飘。
    ("完全退回纯随机（不按加成排序）", [
        ("        private_targets.sort(\n"
         "            key=lambda s: owner_bonus.get(str(s.get(\"ownerId\")), 0), reverse=True)\n",
         ""),
    ], "固定 12 个随机种子", "fail"),

    # want 只能挑**确定性**的那条。「优先停进加成最高的车位」是概率性的：
    # 排序一去掉，第一辆车落在 5% 档上的概率约 1/3，会 flaky。
    # 「整体按加成从高到低铺开」任何随机种子下都必然红，才是能当判据的。
    # 这条是**概率性**的：注掉排序后仍保留 shuffle，顺序随机，4 个车位里
    # 恰好按加成降序的概率约 1/24。判据要挑确定性的那条（源码层）。
    ("排序键改成车位自己（加成挂在车位上，取不到）", [
        ('        private_targets.sort(\n'
         '            key=lambda s: owner_bonus.get(str(s.get("ownerId")), 0), reverse=True)\n',
         '        private_targets.sort(key=lambda s: 0, reverse=True)\n'),
    ], "排序键是车位主人的加成", "fail"),

    ("忘了先 shuffle（档内变成固定顺序，失去随机分散）", [
        ("        random.shuffle(private_targets)\n", ""),
    ], "同一加成档内的落点不是固定顺序", "fail"),

    # ★注入片段和 want 都必须跟着源码/断言名的**当前原文**走。
    # v1.52.0 把 _PARK_MAX_PER_OWNER 从 2 改成 1、断言名也跟着改了之后，
    # 这两条同时过期：注入片段找不到（定位失败，一条都没测），
    # want 指向已改名的断言（真抓到 4 条却判成没抓到）。
    ("每个主人的上限形同虚设（改成 99）", [
        ("    _PARK_MAX_PER_OWNER = 1\n", "    _PARK_MAX_PER_OWNER = 99\n"),
    ], "每个车位主人最多停 1 辆", "fail"),

    ("上限不生效（不统计每个主人停了几辆）", [
        (CAP_SKIP, "                pass\n"),
    ], "任何车位主人都没超过 1 辆", "fail"),

    # 这一条要单独盯：就地跳过重试被改成「撞上限就换下一辆车」，
    # 会连带让「名额用满 3 个不同主人」也变红 —— 那正是本条要抓的语义。
    ("撞上限时丢掉这辆车（不换车位重试）", [
        (CAP_SKIP + RETRY,
         "                spot, owner_key = cand, cand_owner\n"
         "                break\n"),
        ("            blocked_count = 0\n"
         "            while targets:\n",
         "            blocked_count = 0\n"
         "            for _cand in targets[:1]:\n"),
        ("                cand = targets.pop(0)\n",
         "                cand = targets.pop(0)\n"),
    ], "名额用满 3 个不同主人", "fail"),

    ("加成表读错字段（读成 price）", [
        ('            out[str(bg_id)] = ParkingBot._to_int(item.get("bonusPercent"), 0,\n'
         '                                                  minimum=0)\n',
         '            out[str(bg_id)] = ParkingBot._to_int(item.get("price"), 0,\n'
         '                                                  minimum=0)\n'),
    ], "场景表能读出加成百分比", "fail"),

    # want 必须跟着**实际会红的那条**走。原来写的「users 是数字键时也能查到」
    # 抓不到 —— 那个夹具里 dict 键 9 和资料 id 9 恰好一致，
    # 删掉 id 兜底后 str(uid) 那条路照样命中。真正能区分的是
    # 「键和资料 id 不一致」那个新夹具。
    ("只认字符串键的 users（键≠资料 id 的玩家加成全丢）", [
        ('            out[str(uid)] = pct\n            pid = profile.get("id")\n'
         '            if pid is not None:\n'
         '                out.setdefault(str(pid), pct)\n',
         '            out[str(uid)] = pct\n'),
    ], "键和资料 id 不一致时", "fail"),

    # 上一版我写的就是这个：classmethod 读 cls._game_data，而 _game_data 是
    # 实例属性，类上永远是 None → 场景表恒空 → 加成恒 0，功能静默失效。
    ("退回到 classmethod 读 cls._game_data", [
        ("    @staticmethod\n    def _bg_bonus_map(game_data: Optional[dict]) -> Dict[str, int]:",
         "    @classmethod\n    def _bg_bonus_map(cls, game_data: Optional[dict] = None) -> Dict[str, int]:"),
        ("        for item in (game_data or {}).get(\"parkingBackgrounds\") or []:",
         "        game_data = cls._game_data\n"
         "        for item in (game_data or {}).get(\"parkingBackgrounds\") or []:"),
    ], "场景表能读出加成百分比", "fail"),

    ("爱心车位不再排除", [
        ('            and spot.get("spotType") != "charity"\n', ""),
    ], "爱心车位仍然被排除", "fail"),

    # 原来直接删掉那一行，列表推导式会变成
    #   [spot for spot in parking_spots and not spot.get(...) and ...]
    # —— for 的可迭代对象被吞掉、`if` 子句消失，变成 UnboundLocalError 崩在
    # 一个跟本用例无关的旧测试里。注入要**只改语义、不改语法结构**：
    # 把 ownerId != user_id 换成恒真的 is not None，
    # 效果一样是「不再排除自己的车位」，但推导式仍然合法。
    ("自己的车位也不再排除", [
        ('            if spot.get("ownerId") != user_id\n',
         '            if spot.get("ownerId") is not None\n'),
    ], "只剩自己的车位时一辆也不停", "fail"),

    # 这条只能靠源码层断言抓：行为测试都是直接调 _park_idle_cars，
    # 绕过 run_once，删掉调用点的 state 参数它们照样全绿。
    ("不传 state 进去（加成表永远为空）", [
        ("            max(0, self._max_cars - still_parked),\n"
         "            state,\n        )\n",
         "            max(0, self._max_cars - still_parked),\n        )\n"),
    ], "run_once 必须把 state 传给 _park_idle_cars", "fail"),

    # --- 「我的车位」标题右端的状态标 ---
    ("没加成时也硬画一个「收益+0%」", [
        ("        if pct <= 0:\n            return []\n", ""),
    ], "加成 0 / 缺失 / 负数都不画", "fail"),

    ("状态标不顶到右端（贴在标题后面）", [
        ('        badge["props"]["style"]["marginLeft"] = "auto"\n', ""),
    ], "状态标顶到标题行右端", "fail"),

    ("_card 不再渲染 badges", [
        ("                    *(badges or []),\n", ""),
    ], "_card 的 badges 确实挂在标题那一行里", "fail"),

    # want 必须跟**实际会红的那条**走。原先写的是渲染层的端到端，
    # 但那条是手搓 _page_cache 塞进去的、绕过写入路径，
    # 删掉 _record_round 里的字段它照样绿。真正会红的是新加的「真跑一轮」。
    ("快照不再写 spot_bonus_pct", [
        ('                "spot_bonus_pct": self._my_bonus_pct(\n'
         '                    state, self._game_data, user_id),\n',
         ""),
    ], "真跑一轮记录后", "fail"),

    ("车位卡片忘了传 badges", [
        ("                accent=_CYAN,\n"
         "                badges=self._spot_bonus_badges(snapshot)), howto_card]",
         "                accent=_CYAN), howto_card]"),
    ], "端到端：真渲染一页", "fail"),

    # ★这条就是「用户看不到 收益+5%」的真实成因。
    # 自己的场景在 state.php **顶层 currentBg**，不在 users[我] 里
    # （游戏前端 loadFromServer / applyBg 就是这么分的）。
    # 退回从 users[我] 查 → 恒为 0 → 标永远不显示，而且一声不吭。
    # 这条断言只有当夹具也照着**真实结构**造（users[我] 没有 currentBg）才抓得住 ——
    # v1.50.0 第一版的夹具把 currentBg 塞进了 users[我]，测了等于没测。
    ("退回从 users[我] 里查自己的场景", [
        ("        bg_id = str(st.get(\"currentBg\") or \"\")\n", ""),
        ("        if bg_id:\n"
         "            return cls._bg_bonus_map(game_data).get(bg_id, 0)\n"
         "        if user_id is not None:\n",
         "        if user_id is not None:\n"),
    ], "自己的加成走顶层 currentBg", "fail"),

    # --- 通知相对时间的时区（v1.50.2）---
    # 实测：notifications.php 的 created_at 是 UTC（本地 22:56 那一轮下发 14:56），
    # 而 state.php 的 parkedTime 是本地时间。把 UTC 当本地 = 整块偏 8 小时。
    ("把通知时间当本地时间（就是这次的 bug）", [
        ("        if not cls._has_tz_offset(text):\n"
         "            when = when.replace(tzinfo=pytz.utc).astimezone(\n"
         "                cls._tz()).replace(tzinfo=None)\n",
         ""),
    ], "通知时间按 UTC 解释", "fail"),

    # 二次换算：原始串自带偏移时再按 UTC 解释一次，又偏回 8 小时
    ("自带偏移的时间串被二次换算", [
        ("        if not cls._has_tz_offset(text):\n", "        if True:\n"),
    ], "不二次换算", "fail"),

    ("偏移识别一律当成有偏移（无偏移的就不换算了）", [
        ("        if t.endswith((\"Z\", \"z\")):\n"
         "            return True\n"
         "        return bool(re.search(r\"[+-]\\d{2}:?\\d{2}$\", t))\n",
         "        return True\n"),
    ], "偏移识别覆盖", "fail"),

    # 改坏 _parse_time 会连累车库的「已停 N 分钟」（parkedTime 确实是本地的）
    ("_parse_time 也改成按 UTC 解释", [
        ("        if moment.tzinfo is None:\n"
         "            # 无偏移量的字符串按游戏本地时间理解，直接与 _now_naive() 比较\n"
         "            return moment\n",
         "        if moment.tzinfo is None:\n"
         "            return moment.replace(tzinfo=pytz.utc).astimezone(\n"
         "                cls._tz()).replace(tzinfo=None)\n"),
    ], "_parse_time 仍把无偏移串当本地", "fail"),

    # --- 停入时刻改用游戏自己的绝对时间戳（v1.50.3）---
    # 注意：v1.50.4 给 _parked_at 加了量纲守卫，函数体多了一行 moment = None，
    # 这里的注入片段必须跟着当前原文走，否则会「定位失败」——那不是抓到了。
    ("退回只读 parkedTime 字符串（不用绝对时间戳）", [
        ("        stamp = cls._to_int(holder.get(\"parkedTimestamp\"), 0, minimum=0)\n"
         "        if stamp > 0:\n"
         "            moment = None\n",
         "        stamp = cls._to_int(holder.get(\"parkedTimestamp\"), 0, minimum=0)\n"
         "        if False:\n"
         "            moment = None\n"),
    ], "优先用 parkedTimestamp", "fail"),

    ("两个字段不一致时用了 parkedTime", [
        ("        stamp = cls._to_int(holder.get(\"parkedTimestamp\"), 0, minimum=0)\n"
         "        if stamp > 0:\n",
         "        stamp = 0\n        if False:\n"),
    ], "以 parkedTimestamp 为准", "fail"),

    # 去掉兜住异常的那几个类型后，恶意时间戳会直接抛出去打断整页。
    # 这条的抓点是**崩溃**（和 v1.49.0「删掉 state 异常归位」同类），
    # 崩溃不算判据里的「抓到」，所以单开 crash 判据并点名异常签名。
    ("parkedTimestamp 脏数据时不兜住（会打断整页）", [
        ("            except (OverflowError, OSError, ValueError) as err:",
         "            except ValueError as err:"),
    ], "OverflowError", "crash"),

    # 有一处漏改回字符串读取 —— 「4 处统一走 _parked_at」那条要抓住
    ("车位卡片漏改，仍直接读 parkedTime", [
        ("        parked_at = cls._parked_at(spot)\n"
         "        mins = max(0, int((now - parked_at).total_seconds() / 60)) if parked_at else 0\n",
         "        parked_at = cls._parse_time(spot.get(\"parkedTime\"))\n"
         "        mins = max(0, int((now - parked_at).total_seconds() / 60)) if parked_at else 0\n"),
    ], "不再直接读 parkedTime", "fail"),

    # --- F1：残缺 _page_cache 抹掉 daily / history（v1.50.4）---
    ("残缺缓存又被当成「有缓存」（数据抹掉的那个判定）", [
        ("            _cached_daily = (self._page_cache or {}).get(\"daily\")\n"
         "            if isinstance(_cached_daily, dict):\n",
         "            _cached_daily = (self._page_cache or {}).get(\"daily\")\n"
         "            if self._page_cache is not None:\n"),
        ("            _cached_h = (self._page_cache or {}).get(\"history\")\n"
         "            if isinstance(_cached_h, list):\n",
         "            _cached_h = (self._page_cache or {}).get(\"history\")\n"
         "            if self._page_cache is not None:\n"),
    ], "按键判断 daily", "fail"),

    ("record_history=False 分支又改回判缓存对象", [
        ("                if not isinstance((cache or {}).get(\"history\"), list):",
         "                if cache is None:"),
    ], "分支也按键判断", "fail"),

    ("_page_data 又整体回落成「缓存非空就全信」", [
        ("        if not isinstance(history, list):\n"
         "                history = stored_h if isinstance(stored_h, list) else []\n",
         "        if not isinstance(history, list):\n"
         "                history = []\n"),
    ], "残缺缓存也能把统计/历史从库里捞回来", "fail"),

    # --- F2：gameSettings / users 不是字典 ---
    # 注入点必须是 run_once **主路径**上那处。早先打在了 _recompute 里
    # （只有买卖后才走），auto_buy/auto_trade 都关的那轮根本进不去 → 注了个寂寞。
    # 而且 8 空格那行是 12 空格那行的**子串**，光靠缩进不唯一，得带上
    # 它前面那行注释当锚点。
    ("gameSettings 又不判类型（整轮 run_once 会抛）", [
        ("        settings_map = self._settings_of(state)\n"
         "        # 游戏允许的最短停车时长，低于它的挪车请求会被服务端拒绝\n",
         "        settings_map = state.get(\"gameSettings\") or {}\n"
         "        # 游戏允许的最短停车时长，低于它的挪车请求会被服务端拒绝\n"),
    ], "run_once 照常跑完", "fail"),

    # --- SSO 诊断日志（v1.50.5）---
    ("跳转链又退回只在成功路径上打（中途断链看不到）", [
        ("        finally:\n"
         "            # 跳转链必须在这里打", "        if True:\n            # 跳转链必须在这里打"),
    ], "跳转链改在 finally 里打", "fail"),

    # want 要跟着**实际会红的那条**走。去掉 try 之后真正变红的是功能断言
    # （异常真的掀到调用方了），源码断言只是顺带失效。
    ("probe 又不兜异常（会掀翻 run_once）", [
        ("        try:\n"
         "            state = await probe.get_json(f\"{self.API_BASE}/state.php\")\n"
         "        except Exception as err:\n"
         "            probe_err = f\"{type(err).__name__}: {err}\"\n",
         "        state = await probe.get_json(f\"{self.API_BASE}/state.php\")\n"),
    ], "都不会把异常掀给调用方", "fail"),

    ("错因又退回 (state or {}).get（登录页 HTML 会 AttributeError）", [
        ("            reason = (state.get(\"error\") if isinstance(state, dict) else \"\") \\\n"
         "                or probe_err or self._probe_reason(state)\n",
         "            reason = (state or {}).get(\"error\") or \"游戏接口无响应\"\n"),
    ], "不再用 (state or {}).get", "fail"),

    ("六种失败形态又共用一句话", [
        ("        if isinstance(state, str):\n"
         "            head = re.sub(", "        if False:\n            head = re.sub("),
    ], "六种失败形态的描述互不相同", "fail"),

    ("主站断链时又不设 _sso_error（只报笼统的「未取到登录 Cookie」）", [
        ("                    self._sso_error = f\"主站 SSO 第 {len(chain) + 1} 跳无响应（{url}）\"\n", ""),
    ], "七种失败形态的错误文案互不相同", "fail"),

    ("Cookie 取值被原样打进日志", [
        ("        logger.info(\"[不可躺自动抢车位] 本次带上的 Cookie 名（不回显取值）：%s\"\n"
         "                    % (\", \".join(sorted(jar)) or \"（一个都没有）\"))\n",
         "        logger.info(f\"[不可躺自动抢车位] Cookie：{cookie}\")\n"),
    ], "不回显取值", "fail"),

    # --- 优先停爱心车位赚经验（v1.51.0）---
    ("爱心车位又无条件排除（开关不起作用）", [
        ("        want_charity = self._prefer_charity and got < cap",
         "        want_charity = False"),
    ], "开着且经验没满时优先停爱心车位", "fail"),

    ("不看经验上限，满额后还往爱心位停（白停一整轮）", [
        ("        want_charity = self._prefer_charity and got < cap",
         "        want_charity = self._prefer_charity"),
    ], "经验满了就不再往爱心车位停", "fail"),

    ("上限写死不读 game_data（自定义上限永远不生效）", [
        ("        cap = self._exp_config(self._game_data).get(\"charity_park\", 50)",
         "        cap = 50"),
    ], "自定义上限能生效", "fail"),

    ("爱心挪车把经验当成金币记进收益", [
        ("            exp = self._to_int(result.get(\"charityExpContributed\"), 0, minimum=0)",
         "            exp = self._to_int(result.get(\"earned\"), 0, minimum=0)"),
    ], "不能出现 earned", "fail"),

    ("爱心挪车被游戏拒绝时车丢了", [
        ("            if not result or not result.get(\"success\"):\n"
         "                logger.warning(\n"
         "                    f\"[不可躺自动抢车位] 爱心挪车失败：{(result or {}).get('error') or '未知原因'}\")\n"
         "                still.append(car)\n",
         "            if not result or not result.get(\"success\"):\n"
         "                logger.warning(\n"
         "                    f\"[不可躺自动抢车位] 爱心挪车失败：{(result or {}).get('error') or '未知原因'}\")\n"),
    ], "挪车被游戏拒绝时车留在原位", "fail"),

    # --- 设为爱心后页面立刻刷新（乐观更新）---
    # 乐观更新排在 _record_round 之前 → 整份旧快照会把它盖回「私家」，
    # 页面上看起来跟没点一样。这里真的把两段换位置。
    ("乐观更新又排在 _record_round 之前（会被旧快照盖回去）", [
        ("        if fresh and fresh.get(\"success\"):\n"
         "            await self._record_round(client, fresh, fresh.get(\"userId\"),\n"
         "                                     0, 0, 0, record_history=False)\n"
         "        # 再按本次操作乐观更新 —— 顺序反了会被上面那份仍旧是旧值的快照盖回去。\n"
         "        if await self._patch_spot_charity(raw_spot_id, action == \"set\"):\n",
         "        if await self._patch_spot_charity(raw_spot_id, action == \"set\"):\n"
         "            pass\n"
         "        if fresh and fresh.get(\"success\"):\n"
         "            await self._record_round(client, fresh, fresh.get(\"userId\"),\n"
         "                                     0, 0, 0, record_history=False)\n"),
    ], "排在 _record_round **之后**", "fail"),

    # 要整块替换掉：只把 `if ...:` 换成 `if False:` 的话，
    # 下一行的 `await self._patch_spot_charity(...)` 会变成悬空 await → SyntaxError，
    # 那是「崩」不是「判据变红」，不算抓到。
    ("乐观更新整个去掉（页面停在旧状态）", [
        ("        if await self._patch_spot_charity(raw_spot_id, action == \"set\"):\n"
         "            logger.warning(\n"
         "                \"[不可躺自动抢车位] 切换后 state.php 立刻读回来的车位类型与本次操作不符\"\n"
         "                \"（服务端写入延迟），已按本次操作显示；下一轮会以真实状态为准。\")\n",
         "        pass\n"),
    ], "确实调用了乐观更新", "fail"),

    # --- 经验值面板（v1.51.1）---
    ("dailyExp 键尾又用本地日期（凌晨整片落空）", [
        ("        today = datetime.now(pytz.utc).strftime(\"%Y-%m-%d\")",
         "        today = cls._now_naive().strftime(\"%Y-%m-%d\")"),
    ], "用 UTC 日期拼键", "fail"),

    ("今天没拿过的又显示「—/上限」（游戏显示的是 0）", [
        ("            got = cls._to_int(daily.get(key), 0) if daily else None",
         "            got = cls._to_int(daily.get(key), 0) if key in daily else None"),
    ], "显示 0（和游戏一致", "fail"),

    # --- 车市：只看可买 + 性价比排序（v1.52.0）---
    # `if self._shop_afford:` 在 _build_shop 和 _shop_filter_row 里各有一处，
    # 光靠这一行不唯一，要带上前面那行过滤的注释。
    ("「只看可买」不再过滤（开关形同虚设）", [
        ("            # 「只看可买」：等级够得着**且**现金买得起，两条一起才算。\n"
         "            # 分开做两个开关反而更难用 —— 用户真正要的是「点进去就能买」的那一批。\n"
         "            if self._shop_afford:",
         "            if False:"),
    ], "砍掉买不起的", "fail"),

    ("「只看可买」只判价格不判等级（买得起但开不了的混进来）", [
        ("                        if self._to_int(c.get(\"level\"), 1, minimum=1) <= level\n"
         "                        and self._to_int(c.get(\"price\"), 0) <= cash]",
         "                        if self._to_int(c.get(\"price\"), 0) <= cash]"),
    ], "也砍掉等级不够的", "fail"),

    # want 要跟着**实际会红的那条**走：删掉调用后，排序键函数本身仍然正确，
    # 真正变红的是两条端到端断言（页面上的车型顺序不对）。
    ("不再按性价比排序（退回车型表原序）", [
        ("            visible = sorted(visible, key=self._shop_sort_key(self._shop_sort))\n", ""),
    ], "页面上的车型顺序就是性价比顺序", "fail"),

    # 原来那次注入只把元组第一项换成常量 0，车 3 仍然排在最后 ——
    # 等于没注入。这里真的换回当年那版「分母兜成 1」的写法，
    # 0 元车会算出 5/1=5 的比值冲到榜首。
    ("价格 0 的车又排到榜首（分母兜成 1 导致比值爆表）", [
        ("        return lambda c: (\n"
         "            0 if cls._to_int(c.get(\"price\"), 0) > 0 else 1,\n"
         "            -(cls._to_int(c.get(\"earn\"), 0)\n"
         "              / cls._to_int(c.get(\"price\"), 0)) if cls._to_int(c.get(\"price\"), 0) > 0 else 0,\n"
         "            cls._to_int(c.get(\"price\"), 0))",
         "        return lambda c: (\n"
         "            -(cls._to_int(c.get(\"earn\"), 0)\n"
         "              / max(1, cls._to_int(c.get(\"price\"), 0))),\n"
         "            cls._to_int(c.get(\"price\"), 0))"),
    ], "价格 0 的车排到最后", "fail"),

    ("未知排序档位照单全收（给一个乱七八糟的顺序）", [
        ("        if key not in dict(self._SHOP_SORTS):",
         "        if False:"),
    ], "未知排序被拒绝", "fail"),

    ("筛选开关被持久化（用户要求每次打开恢复全部）", [
        ("        self._shop_afford = False\n        self._shop_sort = \"ratio\"\n"
         "        self._title_open = bool(saved_view.get(\"title_open\"))",
         "        self._shop_afford = bool(saved_view.get(\"shop_afford\"))\n"
         "        self._shop_sort = str(saved_view.get(\"shop_sort\") or \"ratio\")\n"
         "        self._title_open = bool(saved_view.get(\"title_open\"))"),
    ], "两个开关都不进存档", "fail"),

    # --- 车库车位类型 / 车主名字（v1.52.0）---
    # want 要指向**注入后真的会红的那条断言**。
    # 这里注入只改 `charity` 那一行，而快照里的 spotType 是另一行独立算出来的，
    # 所以「从车位反查 spotType」这条断言压根不会红 —— 挂着它当 want 会把
    # 「抓到了」误判成「没抓到」。
    ("车位类型又只看 car.status（服务端不标就认不出爱心车位）", [
        ("                    charity = _spot_type == \"charity\" or status == \"charity\"",
         "                    charity = status == \"charity\""),
    ], "即使 car.status 没标 charity", "fail"),

    ("爱心车的金币收益不再强制为 0（会凭空显示一笔金币收入）", [
        ("                    \"gain\": (0 if charity or status != \"parked\"\n"
         "                             else min(int(mins * rate), max_earn)),",
         "                    \"gain\": (min(int(mins * rate), max_earn)\n"
         "                             if status in (\"parked\", \"charity\") else 0),"),
    ], "金币收益必须为 0", "fail"),

    ("车主名字又不去 users 里查（显示成「车主未知」）", [
        ("                    \"ownerName\": (\n"
         "                        names_by_id.get(str(car.get(\"parkedAt\")))\n"
         "                        or names_by_id.get(spot_owner.get(\n"
         "                            str(car.get(\"parkedSpotId\")), \"\")) or \"\"\n"
         "                    ) if status not in (\"idle\", None, \"\") else \"\",",
         "                    \"ownerName\": \"\",\n"
         "                    \"ownerName2\": (\n"
         "                        names_by_id.get(str(car.get(\"parkedAt\"))) or \"\"),"),
    ], "车主名字从 users 按 parkedAt 查出来", "fail"),

    ("不再统计空闲爱心车位数（开关没生效时看不出原因）", [
        ("                \"free_charity_spots\": sum(", "                \"_x\": sum("),
    ], "空闲爱心车位数", "fail"),

    # --- 自动刷「购车 / 以旧换新」每日经验（v1.52.0）---
    # 每条只改一个维度。want 一律指向**注入后真的会红的那条断言**。
    ("经验满了还继续买和换（白花钱）", [
        ("        if not need_buy and not need_trade:", "        if False:"),
    ], "两项经验都满时一个请求都不发", "fail"),

    # want 指向「两项都满时返回 (False, False)」—— 它才真正卡住区分点。
    # 原来挂的那条喂的是 tradeIn=30（本来就没满），错误实现算出来也一样。
    ("不按经验上限判断，缺不缺一律当成缺", [
        ("        got = cls._exp_daily(state, user_id)\n"
         "        return (got.get(\"buy\", 0) < cls._to_int(caps.get(\"buy\"), 0),\n"
         "                got.get(\"tradeIn\", 0) < cls._to_int(caps.get(\"tradeIn\"), 0))",
         "        got = cls._exp_daily(state, user_id)\n"
         "        return (got.get(\"buy\", 0) < cls._to_int(caps.get(\"buy\"), 0),\n"
         "                got.get(\"tradeIn\", 0) < cls._to_int(caps.get(\"tradeIn\"), 0) or True)"),
    ], "两项都满时返回 (False, False)", "fail"),

    ("回收挑最贵的空闲车", [
        ("        candidates.sort(key=lambda pair: pair[0])",
         "        candidates.sort(key=lambda pair: pair[0], reverse=True)"),
    ], "回收挑的是最便宜的空闲车", "fail"),

    ("不看空闲状态就挑车回收（停着的车也敢动）", [
        ("            if car.get(\"status\") != \"idle\" or car.get(\"carId\") == 0:",
         "            if car.get(\"carId\") == 0:"),
    ], "没有空闲车可回收时整轮跳过", "fail"),

    # ★want 必须用**新加那条**断言的名字。旧的「限时车（carId=0）不拿去回收」
    # 在真实夹具下区分不出（限时车压根不在车型表，两种写法结果一样），
    # 真正变红的是「即使出现在车型表里」那条。名字对不上 -> 误判没抓到。
    ("限时车也拿去回收（卖掉就再也买不回来）", [
        ("            if car.get(\"status\") != \"idle\" or car.get(\"carId\") == 0:",
         "            if car.get(\"status\") != \"idle\":"),
    ], "限时车即使出现在车型表里也不拿去回收", "fail"),

    ("买最贵的车而不是最便宜的", [
        ("        rows.sort(key=lambda d: cls._to_int(d.get(\"price\"), 0, minimum=0))",
         "        rows.sort(key=lambda d: cls._to_int(d.get(\"price\"), 0, minimum=0),\n"
         "                  reverse=True)"),
    ], "买的是等级内最便宜的那辆", "fail"),

    ("车型不按等级过滤（买自己开不了的车）", [
        ("            if cls._to_int(definition.get(\"levelReq\"), 1, minimum=1) > level:",
         "            if False:"),
    ], "_exp_farm_cars 会按等级滤掉买不起的", "fail"),

    ("循环末尾不回收换来的车（工位不回还，越刷车越少）", [
        ("            client, user_id, before_ids, hi_def.get(\"id\"), \"换新\")",
         "            client, user_id, before_ids, lo_def.get(\"id\"), \"换新\")"),
    ], "第二轮起不再腾位置", "fail"),

    # ★真机 v1.52.0 第一次开就撞上：buy 已写成功，但紧接着那一次 state.php
    # 还没带上新车，定位落空 -> 每轮「回收+买」净亏 160 金币的死循环。
    # 这几条盯的就是「重试」和「把匹配数说出来」两件事。
    ("认不出新车只试一次就放弃（真机上就是这个，直接变成漏钱循环）", [
        ("    _EXP_FARM_FIND_TRIES = 3", "    _EXP_FARM_FIND_TRIES = 1"),
    ], "state.php 慢一拍时仍能把这一轮走完", "fail"),

    ("重试次数被改成 1（钉死的验收值必须跟着变红）", [
        ("    _EXP_FARM_FIND_TRIES = 3", "    _EXP_FARM_FIND_TRIES = 1"),
    ], "重试次数钉死 3 次", "fail"),

    ("认不出新车时日志不说实际看到了几辆（下次只能靠猜）", [
        ("匹配数 {seen}", "若干"),
    ], "日志要写明**实际看到了几辆**", "fail"),

    # --- 刷经验期间不许自动买车/换新碰车（v1.52.2）---
    # ★真机烧钱记录（2026-10-17）：两个开关同开，一天 diff=340500=3×113500
    # （113500+800=114300=奥迪A8 的价）、recycled=3×91440，净损失 68580，
    # 而设计里一轮只该净亏 400。守卫是这次唯一的防线。
    # ★注入只能用 `pass` 顶替，**不能整块删空** —— 删空会留下一个没有体的
    # `if`，直接 IndentationError，测试脚本在第一个断言前就崩掉，
    # 判据看到的是「崩=True 没抓到」，而真因是注入本身写坏了。
    ("刷经验期间自动换新不再让路（会把刚买的便宜车换成贵车）", [
        ("            # ★见 _exp_farm_active 的注释：刷经验期间换新会把刚买的便宜车\n"
         "            # 换成贵车，实测一轮净支出从 400 变成 22860。必须整轮跳过。\n"
         "            logger.info(\n"
         "                \"[不可躺自动抢车位] 本轮跳过自动以旧换新：正在刷购车/以旧换新经验，\"\n"
         "                \"换新会挑走刚买的那辆便宜车（经验满了之后自动恢复）\")\n"
         "            return \"\"\n",
         "            pass\n"),
    ], "自动以旧换新整轮跳过", "fail"),

    ("刷经验期间自动买车不再让路（会占掉刚腾出来的工位）", [
        ("            # ★刷经验循环末尾会把空位还回来，这一轮「有空位就买收益最高的」\n"
         "            # 会正好买成 A8，把工位占掉，下一轮刷经验就得再回收一辆。\n"
         "            # 实测和自动换新是同一个坑（见 _exp_farm_active）。\n"
         "            logger.info(\n"
         "                \"[不可躺自动抢车位] 本轮跳过自动买车：正在刷购车/以旧换新经验，\"\n"
         "                \"买车会占掉刚腾出来的工位（经验满了之后自动恢复）\")\n"
         "            return \"\"\n",
         "            pass\n"),
    ], "自动买车也跳过", "fail"),

    ("守卫只看开关开没开、不看经验满没满（满额后自动换新被永久废掉）", [
        ("        need_buy, need_trade = self._exp_farm_need(state, user_id, self._game_data)\n"
         "        return bool(need_buy or need_trade)",
         "        return True"),
    ], "经验满之后自动换新自动恢复", "fail"),

    # --- 时间口径统一北京时间（v1.53.1）---
    ("界面上的重置时刻被去掉（用户不知道「今日」什么时候结束）", [
        ("        prog.append(cls._text(\n"
         "            f\"每日经验在 {ParkingBot._fmt_reset_clock(_rst)}（北京时间）重置\",",
         "        prog.append(cls._text(\"\","),
    ], "重置时刻写进界面上", "fail"),

    ("重置时刻写成 8:00 而不是 08:00（半截时间戳看着像坏了）", [
        ('        return "%02d:00" % (hour if 0 <= hour <= 23 else 0)',
         '        return "%d:00" % (hour if 0 <= hour <= 23 else 0)'),
    ], "重置时刻格式化会补零", "fail"),

    # ★注入里必须用 `datetime.timedelta` —— 插件是 `from datetime import datetime`，
    # 根本没有 `dt` 这个名字，写错就是 NameError 把脚本带崩，判据看到的是
    # 「崩=True 没抓到」，跟「注入生效但没被判据抓住」完全是两回事。
    # ★注入只能用源码里真实存在的名字。插件只有 `from datetime import datetime`
    # —— `datetime` 是**类**、没有 `.timedelta` 属性，timedelta 压根没导入。
    # 写 `datetime.timedelta(...)` 会 AttributeError 把脚本带崩，判据看到的是
    # 「崩=True 没抓到」，跟「注入生效但没被判据抓住」完全是两回事。
    # 这里改用 v1.51.0 那个真实的老 bug 写法：直接拿本地日期拼键。
    ("_exp_daily 改用本地日期拼键（v1.51.0 修掉的老 bug 复活）", [
        ("        today = datetime.now(pytz.utc).strftime(\"%Y-%m-%d\")",
         "        today = cls._now_naive().strftime(\"%Y-%m-%d\")"),
    ], "仍按 UTC 取日期", "fail"),

    ("每日重置时刻从 8 改成别的（与游戏 UTC 日界脱钩）", [
        ("    _EXP_DAY_RESET_HOUR = 8", "    _EXP_DAY_RESET_HOUR = 0"),
    ], "每日重置时刻按北京时间记", "fail"),

    ("回收拿回的钱不记账", [
        # v1.54.0 起 `_shop_recycled += back` 后面多了一行金币流水，
        # 注入片段必须把那一行一起带上，否则只是「定位失败」而不是抓到。
        ("        self._shop_recycled += back\n"
         "        self._coin_event(\"recycle\", back, hi_def.get(\"name\"))\n"
         "        note = \"", "        note = \""),
    ], "回收拿回的钱记成", "fail"),

    ("回收收入不并进当日收益（当日净收凭空少一截）", [
        ("            bucket[\"earned\"] = self._to_int(bucket.get(\"earned\"), 0) + self._shop_recycled\n",
         ""),
    ], "回收收入并进当日收益", "fail"),

    ("刷经验之后不刷新状态（拿旧车辆 ID 继续停车）", [
        ("                note = await self._exp_farm_step(client, state, cars_map, user_id)\n"
         "                if note:\n"
         "                    shop_note = f\"{shop_note}；{note}\" if shop_note else note\n"
         "                    state_is_current = False\n"
         "                    if not await _refresh_state():\n"
         "                        stale_after_shop = True\n"
         "                        logger.warning(\n"
         "                            \"[不可躺自动抢车位] 刷经验后刷新状态失败，本轮跳过停车，\"\n"
         "                            \"避免用过期的车辆 ID 继续操作\")",
         "                note = await self._exp_farm_step(client, state, cars_map, user_id)\n"
         "                if note:\n"
         "                    shop_note = f\"{shop_note}；{note}\" if shop_note else note\n"
         "                    state_is_current = False"),
    ], "刷完必须刷新状态", "fail"),

    # --- 每家停车上限 2 -> 1 + 回收率读真值（v1.52.0）---
    ("每家又能停 2 辆了（回到收紧前的行为）", [
        ("    _PARK_MAX_PER_OWNER = 1", "    _PARK_MAX_PER_OWNER = 2"),
    ], "每个车位主人最多停 1 辆", "fail"),

    ("回收率只读 gameRules（忽略 gameSettings 的覆盖，拿不到真实值）", [
        ("        cfg = cls._settings_of(state) if isinstance(state, dict) else {}\n"
         "        raw = cls._to_int((cfg or {}).get(\"RECYCLE_RATE\"), -1)\n"
         "        if raw > 0:\n"
         "            return min(100, raw)\n",
         "        cfg = cls._settings_of(state) if isinstance(state, dict) else {}\n"
         "        raw = -1\n"
         "        if raw > 0:\n"
         "            return min(100, raw)\n"),
    ], "gameSettings.RECYCLE_RATE 优先于 gameRules", "fail"),

    ("回收率优先级写反了（gameRules 压过 gameSettings）", [
        ("        cfg = cls._settings_of(state) if isinstance(state, dict) else {}\n"
         "        raw = cls._to_int((cfg or {}).get(\"RECYCLE_RATE\"), -1)\n"
         "        if raw > 0:\n"
         "            return min(100, raw)\n"
         "        rules = (game_data or {}).get(\"gameRules\") if isinstance(game_data, dict) else None\n"
         "        rules = rules if isinstance(rules, dict) else {}\n"
         "        return min(100, cls._to_int(rules.get(\"RECYCLE_RATE\"), 80, minimum=0))",
         "        rules = (game_data or {}).get(\"gameRules\") if isinstance(game_data, dict) else None\n"
         "        rules = rules if isinstance(rules, dict) else {}\n"
         "        first = min(100, cls._to_int(rules.get(\"RECYCLE_RATE\"), 80, minimum=0))\n"
         "        cfg = cls._settings_of(state) if isinstance(state, dict) else {}\n"
         "        raw = cls._to_int((cfg or {}).get(\"RECYCLE_RATE\"), -1)\n"
         "        return first if first > 0 else min(100, raw)"),
    ], "gameSettings.RECYCLE_RATE 优先于 gameRules", "fail"),

    ("回收率兜底照抄 JS 的 50（那是前端兜底，不是服务端值）", [
        ("        return min(100, cls._to_int(rules.get(\"RECYCLE_RATE\"), 80, minimum=0))",
         "        return min(100, cls._to_int(rules.get(\"RECYCLE_RATE\"), 50, minimum=0))"),
    ], "两边都没有时兜底 80", "fail"),

    ("free_private_owners 数成车位个数（同一家 3 个车位会算成 3 个主人）", [
        ("                \"free_private_owners\": len({\n"
         "                    str(s.get(\"ownerId\")) for s in all_spots\n"
         "                    if not s.get(\"carInSpot\")\n"
         "                    and s.get(\"spotType\") != \"charity\"\n"
         "                    and str(s.get(\"ownerId\")) != str(user_id)}),",
         "                \"free_private_owners\": sum(\n"
         "                    1 for s in all_spots\n"
         "                    if not s.get(\"carInSpot\")\n"
         "                    and s.get(\"spotType\") != \"charity\"\n"
         "                    and str(s.get(\"ownerId\")) != str(user_id)),"),
    ], "free_private_owners 数的是**不同的主人**", "fail"),

    ("文案里又把回收率写死成 80%", [
        ("                                                 \"花费：单轮净支出 = 新车价 x (1 - 回收率)。\"",
         "                                                 \"花费：单轮净支出 = 新车价 x (1 - 回收率)，\"\n"
         "                                                 \"回收率 80% 时约亏新车价的 20%。\""),
    ], "文案里不写死回收率 80%", "fail"),

    # v1.52.0 修复：被上限挡完的日志拿「已被 pop 空的 targets」算，候选数/车主数恒 0。
    # 注入只改日志里两个数字的来源（真实收集 -> 空 targets），维度单一。
    ("上限挡完日志拿已清空的 targets 算（候选数/车主数恒 0）", [
        ('但剩余 {blocked_count} 个候选车位都归 ',
         '但候选车位只剩 {len(targets)} 个、且都归 '),
        ('{len(blocked_owners)} 个已停满',
         '{len(targets)} 个已停满'),
    ], "被上限挡住时日志报真实候选数", "fail"),

    # ---- v1.53.0 个性化（场景 / 皮皮秀）----
    # ★currentBg 在 state **顶层**、qqShowId 在 **users[me]** 里 —— 两个位置
    # 历史都踩过反的（v1.50.2 就是 currentBg 读错了位置）。这两条各改一个源。
    ("个性化：currentBg 读成 users[me]（那儿根本没有这个字段）", [
        ('                "current_bg": str(state.get("currentBg") or ""),',
         '                "current_bg": str(mine.get("currentBg") or ""),'),
    ], "个性化：currentBg 读 state **顶层**", "fail"),

    ("个性化：qqShowId 读成 state 顶层（那儿没有这个字段）", [
        ('                "qqshow_id": self._to_int(mine.get("qqShowId"), 0),',
         '                "qqshow_id": self._to_int(state.get("qqShowId"), 0),'),
    ], "个性化：qqShowId 读 users[me]", "fail"),

    ("个性化：场景购买不打 buy_bg.php（假装成功、不扣钱）", [
        ('            result = await client.post_json(\n'
         '                f"{self.API_BASE}/buy_bg.php", json={"bgId": bg_id})',
         '            result = {"success": True}'),
    ], "场景购买走 buy_bg.php", "fail"),

    ("个性化：saveState 少传 ownedQqShows（全量覆盖会把它清空）", [
        ('            "ownedBgs": fresh.get("ownedBgs") or [],\n'
         '            "ownedQqShows": fresh.get("ownedQqShows") or [],',
         '            "ownedBgs": fresh.get("ownedBgs") or [],'),
    ], "POST state.php 必须带全 7 个字段", "fail"),

    ("个性化：未购买的皮皮秀也能直接穿", [
        ('            if price > 0 and qs_id not in owned:\n'
         '                return _result({"success": False, "error": "尚未购买这个皮皮秀"})\n',
         ''),
    ], "未购买的皮皮秀不能直接切换", "fail"),

    ("个性化：皮皮秀渲染不按性别过滤（会列出异性款）", [
        ('                if gender and q_gender not in (gender, "U"):\n'
         '                    continue\n',
         ''),
    ], "个性化·皮皮秀：只列男性款", "fail"),

    # ---- v1.54.0 金币流水 + 分类锁定换新 ----
    ("分类锁定形同虚设（候选不按锁定过滤）", [
        ('                _cat = str(definition.get("category") or "")\n'
         '                _lock = (self._trade_locks or {}).get(_cat)\n'
         '                if _lock is not None and str(definition.get("id")) != str(_lock):\n'
         '                    continue\n',
         '                _cat = str(definition.get("category") or "")\n'
         '                _lock = (self._trade_locks or {}).get(_cat)\n'),
    ], "锁定「代步车=桑塔纳」后只换桑塔纳", "fail"),

    ("金币流水不落盘（_coin_events 收走了但没进 bucket）", [
        ('            bucket["log"] = _log[-200:]\n', '            pass\n'),
    ], "流水落进当天 daily", "fail"),

    ("零元收支也刷一条流水", [
        ('        if not delta:\n            return\n',
         '        if False:\n            return\n'),
    ], "金额为 0 的不记", "fail"),

    ("锁定接口只能加不能取消", [
        ('        if str(locks.get(category)) == car_key:\n',
         '        if False:\n'),
    ], "同款再点一次 = 取消该分类锁定", "fail"),
]

ok = True
for name, edits, want, mode in CASES:
    text = ORIGINAL
    bad = None
    for old, new in edits:
        if text.count(old) != 1:
            bad = "片段出现 %d 次：%r" % (text.count(old), old[:60])
            break
        text = text.replace(old, new, 1)
    if bad:
        print("★定位失败：%s -> %s" % (name, bad), flush=True)
        ok = False
        continue
    io.open(PLUGIN, "w", encoding="utf-8", newline="").write(text)
    try:
        r = subprocess.run([sys.executable, TARGET], capture_output=True,
                           text=True, cwd=ROOT, encoding="utf-8",
                           errors="replace", timeout=900)
        out = (r.stdout or "") + (r.stderr or "")
        crashed = "Traceback" in out
        fails = [ln.strip() for ln in out.splitlines() if "   FAIL" in ln]
        if mode == "crash":
            caught = (r.returncode != 0) and crashed and want in out
        else:
            targeted = [ln for ln in fails if want in ln]
            caught = (r.returncode != 0) and bool(targeted) and not crashed
        print("%-40s rc=%-4s FAIL=%-3d 崩=%-5s -> %s"
              % (name[:40], r.returncode, len(fails), crashed,
                 "抓到了" if caught else "★没抓到★"), flush=True)
        for ln in fails[:4]:
            print("       %s" % ln[:92], flush=True)
        # ★崩了就把 traceback 打出来。「崩=True -> 没抓到」只说明**脚本**挂了，
        # 挂在哪一行才是唯一能定位问题的信息 —— 不打出来就只能靠猜，
        # 而猜错的代价是把一条真抓到（或真没抓到）的用例判反。
        if crashed:
            tail = out.rstrip().splitlines()[-14:]
            print("       ---- traceback ----", flush=True)
            for ln in tail:
                print("       %s" % ln[:92], flush=True)
        if not caught:
            ok = False
    finally:
        io.open(PLUGIN, "w", encoding="utf-8", newline="").write(ORIGINAL)

same = io.open(PLUGIN, encoding="utf-8").read() == ORIGINAL
print("\n还原校验 :", "一致" if same else "★不一致★")
ok = ok and same

r = subprocess.run([sys.executable, TARGET], capture_output=True, text=True,
                   cwd=ROOT, encoding="utf-8", errors="replace", timeout=900)
out = (r.stdout or "") + (r.stderr or "")
tail = [ln for ln in out.splitlines() if "通过" in ln]
print("还原后   : rc=%s FAIL=%d traceback=%s  %s"
      % (r.returncode, out.count("   FAIL"), "Traceback" in out,
         tail[-1].strip() if tail else ""))
if r.returncode != 0 or "Traceback" in out or out.count("   FAIL"):
    ok = False

print("\nREV150-OK" if ok else "\nREV150-FAIL")
sys.exit(0 if ok else 1)
