# -*- coding: utf-8 -*-
"""不可躺自动抢车位 - MoviePilot V3 插件。

自动把车辆停到他人私家车位上赚金币，停够设定时长后自动挪走结算收益。
登录身份复用 MoviePilot「站点管理」中 tangpt.top 站点已保存的 Cookie，
不重复维护第二份登录态。
"""

import random
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.db.oper.site import SiteOper
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils
from app.sdk.plugin import _PluginBase


class ParkingBot(_PluginBase):
    """自动停车 + 自动挪车。"""

    plugin_name = "不可躺自动抢车位"
    plugin_desc = ("自动把车停到他人私家车位赚金币，停够设定时长后自动挪车结算。"
                   "登录身份复用「站点管理」里 tangpt.top 站点的 Cookie。")
    plugin_icon = "Parking_A.jpg"
    plugin_version = "1.4.2"
    plugin_author = "laoying6663"
    author_url = "https://github.com/laoying6663/MoviePilot-Plugins"
    plugin_config_prefix = "parkingbot_"
    plugin_order = 48
    auth_level = 1

    # 游戏接口根地址
    API_BASE = "https://qcw.tangpt.top/api"
    # 「站点管理」中用于定位目标站点的域名关键字
    SITE_KEYWORD = "tangpt.top"
    # 主站 SSO 入口。用游戏登录页「从主站登录」按钮指向的地址。
    # 注意别用 tp-open-connect/sso/generate —— 那是游戏「退出登录」时跳转的
    # 解绑入口，用它能跑通但拿不到游戏会话，只会被 state.php 判为未登录。
    SSO_URL = "https://www.tangpt.top/parking.php"
    # 游戏 Cookie 缓存有效期（秒）。票据是一次性的，缓存后不必每个周期都去主站打接口。
    COOKIE_TTL = 3600
    # 一次性任务的 job id，停用时据此取消
    ONCE_JOB_ID = "parkingbot_run_once"

    _default_cron = "*/5 * * * *"
    _default_park_minutes = 10
    _default_max_cars = 1

    # ---------- 配置项 ----------
    _enabled = False
    _cron = _default_cron
    _park_minutes = _default_park_minutes
    _max_cars = _default_max_cars
    _cookie = ""
    _notify = True

    # ---------- 运行态（仅内存，用于详情页展示） ----------
    _last_run = "尚未运行"
    _last_run_ts = 0
    _total_earned = 0
    _auth_warned = False
    # 最近一次 SSO 失败的原因。SSO 失败时游戏 Cookie 取不到，外层若只报
    # 「缺少登录 Cookie」会把排查方向带偏，这里把真实原因带出去。
    _sso_error = ""
    _cars_cache: Optional[Dict[int, dict]] = None
    # 详情页数据缓存（快照 + 历史）。
    # 每轮执行结束后在内存里同步刷新，页面渲染与插件重载都直接读它，
    # 不再每次访问数据库 —— 保存配置会触发插件重载，重载又会重新收集
    # 页面/仪表盘投影，每次都查库正是保存转圈变慢的原因。
    # 只在进程冷启动（从未执行过）时回落到一次数据库读取。
    _page_cache: Optional[Dict[str, Any]] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """
        读取插件配置并重建运行态。
        勾选「立即执行一次」时交给宿主调度器延迟触发，避免插件自建调度线程。
        """
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cron = self._normalize_cron(config.get("cron")) or self._default_cron
        self._park_minutes = self._to_int(config.get("park_minutes"),
                                          self._default_park_minutes, minimum=10)
        self._max_cars = self._to_int(config.get("max_cars"),
                                      self._default_max_cars, minimum=1, maximum=10)
        self._cookie = str(config.get("cookie") or "").strip()
        self._notify = bool(config.get("notify", True))
        self._auth_warned = False
        self._cars_cache = {}

        if config.get("onlyonce"):
            self._schedule_once(delay_seconds=3)
            config["onlyonce"] = False
            self.update_config(config)

    # ================================================================
    # 生命周期与调度
    # ================================================================

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """按配置周期注册定时服务，每轮先挪车再停车。"""
        if not self.get_state():
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(
                f"[不可躺自动抢车位] 执行周期表达式非法：{self._cron}，错误：{err}；"
                "应为 5 段标准 cron，例如 */5 * * * *（留空则用默认每 5 分钟）"
            )
            return []
        return [
            {
                "id": "ParkingBot.Run",
                "name": "不可躺自动抢车位定时执行",
                "trigger": trigger,
                "func": self.run_once,
                "kwargs": {},
            }
        ]

    def stop_service(self) -> None:
        """停用时取消尚未执行的一次性任务；周期服务由宿主按插件 ID 统一清理。"""
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if remove_once:
            try:
                remove_once(self.__class__.__name__, self.ONCE_JOB_ID)
            except Exception as err:
                logger.debug(f"[不可躺自动抢车位] 取消一次性任务失败：{err}")

    def _schedule_once(self, delay_seconds: float = 3) -> None:
        """把「立即执行一次」交给宿主调度器，调度器未就绪时静默跳过。"""
        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if not add_once:
            return
        try:
            add_once(
                self.__class__.__name__,
                self.ONCE_JOB_ID,
                self.run_once,
                "不可躺自动抢车位立即执行一次",
                delay_seconds=delay_seconds,
            )
        except Exception as err:
            logger.error(f"[不可躺自动抢车位] 追加一次性任务失败：{err}")

    # ================================================================
    # 核心逻辑
    # ================================================================

    async def run_once(self) -> None:
        """执行一轮自动挪车 + 自动停车。"""
        if not self.get_state():
            return

        cookie = await self._resolve_cookie()
        if not cookie:
            detail = self._sso_error or "未取到登录 Cookie"
            self._set_result(detail)
            self._notify_once(
                f"{detail}。请到插件日志看「主站 SSO 跳转链」那几行定位断点；"
                "若跳转链正常但游戏仍判未登录，可在本插件配置里手动填写 qcw.tangpt.top 的 Cookie。"
            )
            return

        client = AsyncRequestUtils(cookies=cookie, timeout=20)
        state = await client.get_json(f"{self.API_BASE}/state.php")
        if not state or not state.get("success"):
            reason = (state or {}).get("error") or "游戏接口无响应"
            # 缓存的游戏会话可能已失效，清掉让下一轮重新走主站 SSO 换取
            await self._drop_cached_cookie()
            self._set_result(f"获取状态失败：{reason}")
            self._notify_once(
                f"登录状态失效：{reason}。请确认「站点管理」中 tangpt.top 的 Cookie 仍然有效；"
                "若主站已掉线需重新登录，或在插件配置里手动填写游戏 Cookie。"
            )
            return

        # 登录已恢复，复位告警标记，后续真出异常才能再次提醒
        self._auth_warned = False

        user_id = state.get("userId")
        settings_map = state.get("gameSettings") or {}
        # 游戏允许的最短停车时长，低于它的挪车请求会被服务端拒绝
        min_duration = self._to_int(settings_map.get("MIN_PARK_DURATION"), 10, minimum=1)
        target_minutes = max(self._park_minutes, min_duration)

        my_cars = [c for c in (state.get("userCars") or []) if c.get("userId") == user_id]
        if not my_cars:
            self._set_result("账号下没有车辆")
            logger.warning("[不可躺自动抢车位] 该账号名下没有任何车辆，无法执行自动停车")
            return

        # 状态分三类：idle 空闲、charity 爱心停车、其余为普通停车。
        # 爱心车位是玩家主动做的公益停车，结算的是经验而非金币，
        # 既不纳入自动挪车，也不该被就地重新停到别人车位上去。
        parked = [c for c in my_cars if c.get("status") not in ("idle", "charity")]
        idle = [c for c in my_cars if c.get("status") == "idle"]
        charity = [c for c in my_cars if c.get("status") == "charity"]
        if charity:
            logger.debug(f"[不可躺自动抢车位] {len(charity)} 辆车停在爱心车位，不参与自动挪车")

        earned_total, moved = await self._unpark_due_cars(client, parked, idle, target_minutes)
        new_parked = await self._park_idle_cars(
            client, idle, state.get("parkingSpots") or [], user_id,
            max(0, self._max_cars - (len(parked) - moved)),
        )
        self._summarize(new_parked, moved, earned_total)

        # 本轮动过车就重新取一次状态：上面那份 state 是动作之前的快照，
        # 直接拿去落快照会让页面把刚停的车显示成「空闲」、按钮置灰，
        # 要等下一轮才对上。动作数为 0 时沿用原状态，省一次请求。
        snapshot_state = state
        if new_parked or moved:
            fresh = await client.get_json(f"{self.API_BASE}/state.php")
            if fresh and fresh.get("success"):
                snapshot_state = fresh
        await self._record_round(
            client, snapshot_state, snapshot_state.get("userId") or user_id,
            new_parked, moved, earned_total,
        )

    async def _unpark_due_cars(
        self,
        client: AsyncRequestUtils,
        parked: List[dict],
        idle: List[dict],
        target_minutes: int,
    ) -> Tuple[int, int]:
        """
        挪走所有停够时长的车辆，并把释放出的车辆放回空闲列表供本轮复用。

        :return: (本轮收益, 本轮挪车数)
        """
        earned_total, moved = 0, 0
        for car in list(parked):
            minutes = self._parked_minutes(car)
            if minutes < target_minutes:
                logger.debug(f"[不可躺自动抢车位] 车辆 {car.get('id')} 已停 {minutes:.0f} 分钟，未到挪车时间")
                continue
            result = await self._post(client, "unpark.php", {"userCarId": car.get("id")})
            if not result or not result.get("success"):
                logger.warning(f"[不可躺自动抢车位] 挪车失败：{(result or {}).get('error') or '未知原因'}")
                continue
            earned_total += self._to_int(result.get("earned"), 0)
            moved += 1
            idle.append(car)
        return earned_total, moved

    async def _park_idle_cars(
        self,
        client: AsyncRequestUtils,
        idle: List[dict],
        parking_spots: List[dict],
        user_id: Any,
        capacity: int,
    ) -> int:
        """
        把空闲车辆停到他人空闲的私家车位。

        车辆按每分钟收益从高到低出队，优先用收益高的车；目标车位在候选集中随机挑选，
        避免所有车辆长期压在同一个车位主人身上而被针对。
        """
        if capacity <= 0 or not idle:
            return 0

        targets = [
            spot for spot in parking_spots
            if spot.get("ownerId") != user_id
            and not spot.get("carInSpot")
            and spot.get("spotType") != "charity"
        ]
        if not targets:
            logger.info("[不可躺自动抢车位] 当前没有可停的他人空余私家车位")
            return 0
        random.shuffle(targets)

        cars_map = await self._load_cars_map(client)
        idle.sort(key=lambda c: self._earn_per_min(c, cars_map), reverse=True)

        new_parked = 0
        for car in idle:
            if new_parked >= capacity or not targets:
                break
            spot = targets.pop(0)
            result = await self._post(client, "park.php", {
                "userCarId": car.get("id"),
                "spotId": spot.get("id"),
            })
            if not result or not result.get("success"):
                logger.warning(f"[不可躺自动抢车位] 停车失败：{(result or {}).get('error') or '未知原因'}")
                continue
            new_parked += 1
            logger.info(
                f"[不可躺自动抢车位] 车辆 {car.get('id')} 已停入车位 {spot.get('id')}，"
                f"收益 {self._earn_per_min(car, cars_map)} 金/分钟"
            )
        return new_parked

    async def _post(self, client: AsyncRequestUtils, endpoint: str, payload: dict) -> Optional[dict]:
        """调用游戏写接口，统一返回解析后的 JSON（失败返回 None）。"""
        return await client.post_json(f"{self.API_BASE}/{endpoint}", json=payload)

    async def _load_cars_map(self, client: AsyncRequestUtils) -> Dict[Any, dict]:
        """
        拉取车型定义表并按进程缓存，用于挑选每分钟收益最高的车辆。
        车型表是静态数据，首次成功后不再重复请求。
        """
        if self._cars_cache:
            return self._cars_cache
        data = await client.get_json(f"{self.API_BASE}/game_data.php")
        if data and data.get("success"):
            cars: Dict[Any, dict] = {}
            for item in (data.get("cars") or []):
                if not isinstance(item, dict):
                    continue
                car_id = item.get("id")
                if car_id is None:
                    continue
                # 车型 id 不保证是整数，直接 int() 遇到 "supercar"、"1.0" 这类
                # 值会抛 ValueError。而本方法处在 run_once 的关键路径上且
                # run_once 没有兜底 try，一个异常就会让整轮停车挪车全部中断。
                try:
                    cars[int(car_id)] = item
                except (TypeError, ValueError):
                    cars[str(car_id)] = item
            self._cars_cache = cars
        return self._cars_cache or {}

    @staticmethod
    def _lookup_car(cars_map: Dict[Any, dict], car_id: Any) -> Optional[dict]:
        """按车辆实例上的 carId 找车型定义，数字/字符串两种键都试一次。"""
        if car_id is None:
            return None
        if car_id in cars_map:
            return cars_map[car_id]
        try:
            return cars_map.get(int(car_id))
        except (TypeError, ValueError):
            return cars_map.get(str(car_id))

    # ================================================================
    # 辅助方法
    # ================================================================

    async def _resolve_cookie(self) -> str:
        """
        取得游戏所需的登录 Cookie。

        优先级：配置中手填的 Cookie > 上次 SSO 换到且未过期的缓存 > 现走一次主站 SSO。
        """
        if self._cookie:
            return self._cookie
        cached = await self._load_cached_cookie()
        if cached:
            return cached
        return await self._exchange_cookie_via_sso()

    async def _load_cached_cookie(self) -> str:
        """读取缓存的游戏会话 Cookie，未记录或已过期返回空串。"""
        try:
            state = await self.async_get_data("game_cookie") or {}
            # 缓存是历史遗留格式时可能是字符串，直接 .get() 会抛 AttributeError
            # 并逃逸出 _resolve_cookie，整轮执行直接挂掉。
            if not isinstance(state, dict):
                logger.warning("[不可躺自动抢车位] 游戏 Cookie 缓存格式异常，已忽略并重新换取")
                return ""
            cookie = str(state.get("cookie") or "")
            age = datetime.now().timestamp() - float(state.get("at") or 0)
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] 读取 Cookie 缓存失败：{err}")
            return ""
        return cookie if cookie and age < self.COOKIE_TTL else ""

    async def _drop_cached_cookie(self) -> None:
        """确认 Cookie 失效后清掉缓存，让下一轮重新走 SSO 换取。"""
        try:
            await self.async_save_data("game_cookie", {})
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] 清除 Cookie 缓存失败：{err}")

    async def _exchange_cookie_via_sso(self) -> str:
        """
        用主站 Cookie 走一次 SSO，换取游戏站会话 Cookie。

        游戏站是独立子域，主站的登录 Cookie 不足以通过 /api/state.php 鉴权，
        必须先访问主站的 parking.php 完成 SSO，由它把游戏会话 Cookie 下发下来。

        两个关键点：
        1. 逐跳手动跟随跳转并自行收集 Set-Cookie。交给 httpx 自动跟随时，
           会话 Cookie 留在内部 client 上，调用方取不出来。
        2. 拿到结果后必须回游戏验证一次再缓存。旧实现无论拿到什么 Cookie
           都记「已通过主站 SSO 获取游戏会话 Cookie」，Cookie 实际无效时
           日志会误导排查方向，而且坏 Cookie 还会被缓存住反复使用。
        """
        site_cookie = await self._site_cookie()
        if not site_cookie:
            self._sso_error = "「站点管理」中没有 tangpt.top 的 Cookie"
            logger.error(f"[不可躺自动抢车位] {self._sso_error}，无法进行 SSO")
            return ""
        self._sso_error = ""

        jar = self._parse_cookie_header(site_cookie)
        client = AsyncRequestUtils(cookies=jar, timeout=20)
        url = self.SSO_URL
        chain: List[str] = []
        try:
            for _ in range(6):  # 跳转链通常 2~3 跳，留足冗余
                resp = await client.get_res(url, allow_redirects=False)
                if resp is None:
                    logger.error(f"[不可躺自动抢车位] 主站 SSO 请求失败：{url}")
                    return ""
                fresh = []
                for name, value in resp.cookies.items():
                    jar[name] = value
                    fresh.append(name)
                location = resp.headers.get("location")
                chain.append(
                    f"{resp.status_code} {url} | 跳转:{location or '无'} | 新增Cookie:{','.join(fresh) or '无'}"
                )
                if resp.status_code not in (301, 302, 303, 307, 308):
                    # 有些站点用 meta refresh / JS 跳转而不是 302，正文里再捞一次
                    if "qcw." not in url:
                        location = self._scrape_redirect(resp, url)
                        if not location:
                            break
                    else:
                        break
                if not location:
                    break
                url = urljoin(url, location)
        except Exception as err:
            logger.error(f"[不可躺自动抢车位] 主站 SSO 换取 Cookie 异常：{err}")
            return ""

        cookie = "; ".join(f"{name}={value}" for name, value in jar.items())
        logger.info("[不可躺自动抢车位] 主站 SSO 跳转链：" + " ‖ ".join(chain))

        # 回游戏验证：只有真的能过 state.php 鉴权才算换到
        probe = AsyncRequestUtils(cookies=self._parse_cookie_header(cookie), timeout=20)
        state = await probe.get_json(f"{self.API_BASE}/state.php")
        if not (state and state.get("success")):
            reason = (state or {}).get("error") or "游戏接口无响应"
            self._sso_error = f"主站 SSO 走完了但游戏仍判未登录（{reason}）"
            logger.error(
                f"[不可躺自动抢车位] {self._sso_error}；"
                "已获取的 Cookie 不会缓存。跳转链见上一行 INFO。"
            )
            return ""

        try:
            await self.async_save_data(
                "game_cookie",
                {"cookie": cookie, "at": datetime.now().timestamp()},
            )
        except Exception as err:
            logger.warning(f"[不可躺自动抢车位] Cookie 缓存落盘失败：{err}")
        logger.info("[不可躺自动抢车位] 已通过主站 SSO 获取游戏会话 Cookie，并已缓存")
        return cookie

    @staticmethod
    def _scrape_redirect(resp: Any, base_url: str) -> str:
        """从非 3xx 响应正文里找跳转地址（meta refresh / JS location）。"""
        try:
            body = resp.text or ""
        except Exception:
            return ""
        patterns = (
            r"""url\s*=\s*['"]?([^'"\s>]+)""",
            r"""location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
        )
        for pattern in patterns:
            match = re.search(pattern, body, re.I)
            if match and "tangpt.top" in match.group(1):
                return match.group(1)
        return ""

    async def _site_cookie(self) -> str:
        """
        读取 MoviePilot「站点管理」中 tangpt.top 站点已保存的 Cookie。

        用子串而非精确域名匹配，是因为站点在库里的 key 可能是 tangpt.top，
        也可能是 www.tangpt.top。
        """
        try:
            oper = SiteOper()
            # 站点查询走异步入口，避免在事件循环里执行同步数据库调用
            sites = await oper.async_list() if hasattr(oper, "async_list") else oper.list()
            for site in sites:
                domain = str(getattr(site, "domain", "") or "")
                url = str(getattr(site, "url", "") or "")
                if self.SITE_KEYWORD in domain or self.SITE_KEYWORD in url:
                    return str(getattr(site, "cookie", "") or "").strip()
        except Exception as err:
            logger.error(f"[不可躺自动抢车位] 读取「站点管理」Cookie 失败：{err}")
        return ""

    @staticmethod
    def _parse_cookie_header(header: str) -> Dict[str, str]:
        """把 Cookie 请求头字符串拆成字典，只取第一个等号前的键名。"""
        jar: Dict[str, str] = {}
        for part in str(header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                jar[name] = value
        return jar

    def _parked_minutes(self, car: dict) -> float:
        """
        计算车辆已停放的分钟数。

        时间无法解析时返回 0 —— 宁可多停一轮，也不发出必然被服务端拒绝的挪车请求。
        """
        moment = self._parse_time(car.get("parkedTime"))
        if moment is None:
            return 0.0
        return max(0.0, (self._now_naive() - moment).total_seconds() / 60)

    @staticmethod
    def _tz():
        """取 MoviePilot 配置时区，取不到时兜底 UTC。"""
        try:
            return pytz.timezone(settings.TZ) if settings.TZ else pytz.utc
        except Exception:
            return pytz.utc

    @classmethod
    def _now_naive(cls) -> datetime:
        """按 MoviePilot 配置时区取当前时间，去掉时区信息以便与游戏本地时间比较。"""
        return datetime.now(cls._tz()).replace(tzinfo=None)

    @classmethod
    def _parse_time(cls, value: Any) -> Optional[datetime]:
        """
        解析游戏返回的时间字段，兼容 ISO 与「YYYY-MM-DD HH:MM:SS」两种写法。

        带时区偏移的输入必须换算到 MoviePilot 配置时区后再去掉 tzinfo，
        不能只是剥掉偏移：否则同一时刻 `...T19:37:20+08:00` 会被算成 30 分钟，
        而 `...T11:37:20Z` 会被算成 510 分钟，差出 8 小时。
        """
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        moment: Optional[datetime] = None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        if moment is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    moment = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if moment is None:
            return None
        if moment.tzinfo is None:
            # 无偏移量的字符串按游戏本地时间理解，直接与 _now_naive() 比较
            return moment
        # 有偏移量的先换算到 MoviePilot 时区，再抹掉 tzinfo 与 _now_naive() 对齐
        return moment.astimezone(cls._tz()).replace(tzinfo=None)

    @classmethod
    def _earn_per_min(cls, car: dict, cars_map: Dict[Any, dict]) -> int:
        """返回车辆每分钟收益。限时车型（carId 为 0）的收益直接存在车辆实例上。"""
        if car.get("carId") == 0:
            return cls._to_int(car.get("flashEarnPerMin"), 5)
        definition = cls._lookup_car(cars_map, car.get("carId"))
        return cls._to_int(definition.get("earnPerMin"), 0) if definition else 0

    @staticmethod
    def _normalize_cron(value: Any) -> str:
        """
        规整 cron 表达式。

        配置表单保存时可能把步进写法拆开（*/5 → * /5），多出一个字段会让
        CronTrigger 报 "Wrong number of fields"。这里压掉分隔符两侧的空白，
        顺带收敛连续空格，让 5 段表达式能稳定解析。
        """
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            return ""
        return re.sub(r"\s*([/,-])\s*", r"\1", text).strip()

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
        """把配置值安全转成整数；非法值回落到默认值，越界值收敛到边界。"""
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        if number < minimum:
            return minimum
        if maximum is not None and number > maximum:
            return maximum
        return number

    def _summarize(self, new_parked: int, moved: int, earned_total: int) -> None:
        """汇总本轮动作，写入运行态并记日志。"""
        parts = []
        if new_parked:
            parts.append(f"新停放 {new_parked} 辆")
        if moved:
            parts.append(f"挪走 {moved} 辆")
        text = "，".join(parts) if parts else "无操作（车辆均未到挪车时间）"
        if earned_total:
            text += f"，本轮收益 {earned_total} 金币"
        self._total_earned += earned_total
        self._set_result(text)
        logger.info(f"[不可躺自动抢车位] {text}")

    async def _record_round(
        self,
        client: AsyncRequestUtils,
        state: dict,
        user_id: Any,
        new_parked: int,
        moved: int,
        earned: int,
    ) -> None:
        """
        记录本轮结果：账户快照 + 执行历史，供插件详情页展示。

        快照保存的是已经算好的展示字段，不再依赖游戏接口 —— 页面渲染走同步读，
        不会每次打开详情页都去打一次游戏。
        """
        try:
            cars_map = await self._load_cars_map(client)
            rules = state.get("gameSettings") or {}
            max_earn = self._to_int(rules.get("MAX_EARN_PER_SPOT"), 3000, minimum=1)
            min_duration = self._to_int(rules.get("MIN_PARK_DURATION"), 10, minimum=1)
            mine = (state.get("users") or {}).get(user_id) or {}
            my_cars = [c for c in (state.get("userCars") or []) if c.get("userId") == user_id]
            my_spots = [s for s in (state.get("parkingSpots") or []) if s.get("ownerId") == user_id]
            # 展示用时间戳与时长计算统一走 MoviePilot 时区，避免两者漂移
            now = self._now_naive()

            cars = []
            for car in my_cars:
                parked_at = self._parse_time(car.get("parkedTime"))
                mins = max(0, (self._now_naive() - parked_at).total_seconds() / 60) if parked_at else 0
                rate = self._earn_per_min(car, cars_map)
                status = car.get("status")
                cars.append({
                    "id": car.get("id"),
                    "name": self._car_name(car, cars_map),
                    "rate": rate,
                    "status": "空闲" if status == "idle" else ("爱心停车" if status == "charity" else "停车中"),
                    "mins": round(mins),
                    # 收益按「已停时长 × 每分钟收益」估算，单车位封顶。
                    # 爱心车位结算的是经验不是金币，不能算进待结算金币。
                    "gain": min(int(mins * rate), max_earn) if status == "parked" else 0,
                })
            # 停车中的排前面，同状态按收益从高到低，方便一眼看出哪辆最值钱
            cars.sort(key=lambda item: (item["status"] == "空闲", -item["rate"]))

            spots = []
            for index, spot in enumerate(my_spots, start=1):
                kind = "爱心车位" if spot.get("spotType") == "charity" else "私家车位"
                spots.append({"label": f"{kind} {index}", "occupant": self._spot_occupant(spot, state, cars_map)})

            snapshot = {
                "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "name": mine.get("name") or "未命名",
                "level": self._to_int(mine.get("level"), 1, minimum=1),
                "cash": self._to_int(mine.get("cash"), 0),
                "bonus": self._to_int(mine.get("bonus"), 0),
                "car_total": len(my_cars),
                "car_max": self._to_int(rules.get("MAX_CARS"), 10, minimum=1),
                "parked_total": sum(1 for c in cars if c["status"] != "空闲"),
                "pending_gain": sum(item["gain"] for item in cars),
                "min_duration": min_duration,
                "cars": cars,
                "spots": spots,
            }
            await self.async_save_data("snapshot", snapshot)

            # 历史在内存里维护一份，避免每轮都读库。但插件重载会把模块重新执行、
            # 类属性复位，此时 _page_cache 为 None；若不先从数据库把历史捞回来，
            # 第一轮就会用「空历史 + 新增一条」覆盖掉落盘的 50 条记录。
            if self._page_cache is not None:
                history = list(self._page_cache.get("history") or [])
            else:
                stored = await self.async_get_data("history") or []
                history = list(stored) if isinstance(stored, list) else []
            history.insert(0, {
                "time": now.strftime("%m-%d %H:%M"),
                "day": now.strftime("%Y-%m-%d"),
                "parked": new_parked,
                "moved": moved,
                "earned": earned,
            })
            history = history[:50]
            await self.async_save_data("history", history)
            # 同步刷新内存缓存：页面渲染与之后的插件重载都直接读它
            self._page_cache = {"snapshot": snapshot, "history": history}
        except Exception as err:
            logger.warning(f"[不可躺自动抢车位] 记录本轮结果失败：{err}")
            # 兜底给缓存一个确定值。否则 _page_cache 一直是 None，
            # get_page / get_dashboard 每次渲染都会回落成同步查库 ——
            # 正是当初加缓存想避免的开销。
            if self._page_cache is None:
                self._page_cache = {"snapshot": {}, "history": []}

    @classmethod
    def _car_name(cls, car: dict, cars_map: Dict[Any, dict]) -> str:
        """取车辆显示名，限时车型直接取实例上的名称。"""
        if car.get("carId") == 0:
            return str(car.get("flashCarName") or "限时车型")
        definition = cls._lookup_car(cars_map, car.get("carId"))
        return str(definition.get("name") or "未知车型") if definition else "未知车型"

    @staticmethod
    def _spot_occupant(spot: dict, state: dict, cars_map: Dict[int, dict]) -> str:
        """返回车位占用者描述，空闲车位给出可否停车提示。"""
        car_id = spot.get("carInSpot")
        if not car_id:
            return "空闲"
        owner_id = spot.get("parkedBy")
        cars = state.get("userCars") or []
        car = next((c for c in cars if c.get("id") == car_id), None)
        if not car:
            return "已被占用"
        owner = (state.get("users") or {}).get(owner_id) or {}
        who = owner.get("name") or "他人"
        if owner_id == spot.get("ownerId"):
            return "我的车辆"
        return f"{who} 的 {ParkingBot._car_name(car, cars_map)}"

    def _set_result(self, text: str) -> None:
        """记录最近一轮结果，供详情页展示。"""
        self._last_run = text
        self._last_run_ts = int(datetime.now().timestamp())

    def _notify_once(self, message: str) -> None:
        """登录态异常只在连续失败期间提醒一次，避免每轮刷屏。"""
        logger.error(f"[不可躺自动抢车位] {message}")
        if self._auth_warned or not self._notify:
            return
        self._auth_warned = True
        try:
            # post_message 的首个位置参数是 channel，正文必须走 text 关键字
            self.post_message(title=self.plugin_name, text=message)
        except Exception as err:
            logger.error(f"[不可躺自动抢车位] 发送通知失败：{err}")

    # ================================================================
    # 页面
    # ================================================================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置模型。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "onlyonce", "label": "立即执行一次"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {"model": "notify", "label": "登录失效时通知"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {
                                         "model": "cron",
                                         "label": "执行周期",
                                         "placeholder": "*/5 * * * *",
                                         "hint": "每轮先挪车再停车，默认每 5 分钟一轮",
                                     }}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {
                                         "model": "park_minutes",
                                         "label": "停满多少分钟挪走",
                                         "placeholder": "10",
                                         "hint": "游戏规定最少停 10 分钟才允许挪走，填更小的值按 10 分钟计",
                                     }}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {
                                         "model": "max_cars",
                                         "label": "最多同时停放车辆数",
                                         "placeholder": "1",
                                         "hint": "同时停放越多收益越高，但停在别人车位被贴条的风险也越大",
                                     }}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextarea",
                                     "props": {
                                         "model": "cookie",
                                         "label": "[可选] 游戏 Cookie",
                                         "rows": 2,
                                         "hint": "一般无需填写。留空时插件会自动用「站点管理」中 tangpt.top 的"
                                                 "主站 Cookie 走 SSO 换取游戏会话；仅当主站 SSO 不可用时，"
                                                 "才需要在此粘贴 qcw.tangpt.top 的 Cookie。",
                                     }}
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VAlert",
                                     "props": {
                                         "type": "info",
                                         "variant": "tonal",
                                         "text": "本插件只做自动停车与自动挪车，不会自动贴条或举报他人车辆。"
                                                 "挪车时间依赖 MoviePilot 时区，请确保「设定 → 常规设置」里的时区"
                                                 "与游戏页面显示的时区一致。",
                                     }}
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "cron": "",
            "park_minutes": "",
            "max_cars": "",
            "cookie": "",
        }

    @staticmethod
    def _text(value: Any, style: Dict[str, Any]) -> dict:
        """
        文本节点。

        MoviePilot 页面渲染器对原生 div 的约定是：`text` 放顶层、`style` 放
        `props` 内。写进 `props.text` 会渲染出空节点 —— 页面上只剩卡片轮廓、
        一个字都没有。
        """
        return {"component": "div", "text": str(value), "props": {"style": style}}

    @staticmethod
    def _stat(label: str, value: Any, color: str) -> dict:
        """单个数字指标块。"""
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "style": {"marginBottom": "8px"}},
            "content": [
                {"component": "VCardText", "content": [
                    ParkingBot._text(label, {"fontSize": "12px", "opacity": "0.75"}),
                    ParkingBot._text(value, {"fontSize": "22px", "fontWeight": "bold", "color": color}),
                ]},
            ],
        }

    @staticmethod
    def _card(title: str, subtitle: str, content: List[dict]) -> dict:
        """
        统一的信息卡片外壳。

        标题也用 div 渲染而不依赖 VCardTitle 插槽：Vuetify 这类组件在本插件
        页面里只渲染出空壳，内容并不会填进去。
        """
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "style": {"marginBottom": "12px"}},
            "content": [
                {"component": "VCardText", "content": [
                    ParkingBot._text(title, {"fontSize": "16px", "fontWeight": "bold", "marginBottom": "2px"}),
                    ParkingBot._text(subtitle, {"fontSize": "12px", "opacity": "0.7", "marginBottom": "10px"}),
                    *content,
                ]},
            ],
        }

    @staticmethod
    def _table(headers: List[str], rows: List[List[Any]],
               widths: List[str], aligns: List[str]) -> dict:
        """
        用 div + flex 拼一张表。

        刻意不用 VTable/VThead/VTd：这些组件在本插件页面里不会把文本填进插槽，
        结果是一块空白区域；div + flex 是插件里被反复验证过能正常出字的路径。
        """
        position = {"left": "left", "center": "center", "right": "right"}

        def cell(value: Any, width: str, align: str, header: bool) -> dict:
            return ParkingBot._text(value, {
                "flex": width,
                "padding": "7px 8px",
                "fontSize": "12px" if header else "13px",
                "fontWeight": "bold" if header else "normal",
                "opacity": "0.85" if header else "1",
                "textAlign": position.get(align, "left"),
                "borderBottom": "1px solid rgba(128,128,128,0.25)",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
                "whiteSpace": "nowrap",
            })

        lines = [{
            "component": "div",
            "props": {"style": {
                "display": "flex",
                "borderTop": "1px solid rgba(128,128,128,0.25)",
            }},
            "content": [cell(title, widths[index], aligns[index], True)
                        for index, title in enumerate(headers)],
        }]
        for row in rows:
            lines.append({
                "component": "div",
                "props": {"style": {"display": "flex"}},
                "content": [cell(value, widths[index], aligns[index], False)
                            for index, value in enumerate(row)],
            })
        if not rows:
            lines.append({
                "component": "div",
                "props": {"style": {
                    "display": "flex",
                    "borderBottom": "1px solid rgba(128,128,128,0.25)",
                }},
                "content": [ParkingBot._text("暂无数据", {
                    "width": "100%", "textAlign": "center", "padding": "14px",
                    "fontSize": "13px", "opacity": "0.6",
                })],
            })
        return {"component": "div", "props": {"style": {"width": "100%"}}, "content": lines}

    def _page_data(self) -> Tuple[dict, List[dict]]:
        """
        详情页数据源：优先内存缓存，只有进程冷启动才读一次数据库。

        保存配置会触发插件重载，重载过程又会重新收集页面与仪表盘投影。
        若投影函数每次都查库，保存就会被数据库往返次数拖住。
        """
        if self._page_cache is not None:
            return self._page_cache.get("snapshot") or {}, self._page_cache.get("history") or []
        snapshot = self.get_data("snapshot") or {}
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        self._page_cache = {"snapshot": snapshot, "history": history}
        return snapshot, history

    @staticmethod
    def _today_earned(history: List[dict]) -> int:
        """
        统计今天的挪车收益。

        历史条目带 day（YYYY-MM-DD），按天筛选比解析展示用的 time 字符串可靠。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        total = 0
        for item in history:
            if str(item.get("day") or "") != today:
                continue
            try:
                total += int(item.get("earned") or 0)
            except (TypeError, ValueError):
                continue
        return total

    def _next_run_text(self) -> str:
        """距离下一次定时执行的剩余时间。插件停用或未配周期时返回「未启用」。"""
        # 停用时 get_service() 不注册任何服务，根本没有「下一次」，
        # 只看 _cron 会一直显示一个永远走不到的时间。
        if not (self._enabled and self._cron):
            return "未启用"
        try:
            trigger = CronTrigger.from_crontab(self._cron)
            now = self._now_tz()
            upcoming = trigger.get_next_fire_time(None, now)
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] 计算下次执行时间失败：{err}")
            return "表达式非法"
        if upcoming is None:
            return "-"
        seconds = int((upcoming - now).total_seconds())
        if seconds <= 0:
            return "即将执行"
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

    @staticmethod
    def _now_tz() -> datetime:
        """带时区的当前时间，用于 CronTrigger 的下次触发点计算。"""
        try:
            return datetime.now(pytz.timezone(settings.TZ) if settings.TZ else pytz.utc)
        except Exception:
            return datetime.now(pytz.utc)

    def get_api(self) -> List[Dict[str, Any]]:
        """注册「立即挪车」后端接口，供插件页面的按钮调用。"""
        return [
            {
                "path": "/unpark",
                "endpoint": self.api_unpark,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即挪车",
                "description": "按车辆 ID 执行一次挪车结算，停车不足游戏规定时长会被拒绝",
            }
        ]

    async def api_unpark(self, car_id: Any) -> dict:
        """
        立即挪车接口。

        最短停车时长在服务端再校验一次：页面上的按钮禁用只是提示，
        真正的规则必须由后端把关，否则绕过界面就能提前挪车。
        """
        if not self.get_state():
            return {"success": False, "error": "插件未启用"}
        # 车辆 ID 用字符串处理：游戏侧不保证是纯数字，按数字解析会把
        # 非数字 ID 误判成「缺少车辆 ID」。筛选时同样按字符串比较。
        target = str(car_id if car_id is not None else "").strip()
        if not target:
            return {"success": False, "error": "缺少车辆 ID"}

        cookie = await self._resolve_cookie()
        if not cookie:
            return {"success": False, "error": "未取到登录 Cookie"}
        client = AsyncRequestUtils(cookies=cookie, timeout=20)

        state = await client.get_json(f"{self.API_BASE}/state.php")
        if not state or not state.get("success"):
            await self._drop_cached_cookie()
            return {"success": False, "error": "登录状态失效，请重新获取会话"}

        # 必须在 userCars 全表里按 userId 过滤，不能只按 id 匹配：
        # state.php 返回的是所有玩家的车辆，缺了归属校验就能挪别人的车。
        owner_id = state.get("userId")
        car = next((c for c in (state.get("userCars") or [])
                    if str(c.get("id")) == target
                    and str(c.get("userId")) == str(owner_id)), None)
        if not car:
            return {"success": False, "error": "车辆不存在或不属于当前账号"}
        if car.get("status") == "idle":
            return {"success": False, "error": "该车辆当前不在停车中"}

        rules = state.get("gameSettings") or {}
        min_duration = self._to_int(rules.get("MIN_PARK_DURATION"), 10, minimum=1)
        minutes = self._parked_minutes(car)
        if minutes < min_duration:
            return {
                "success": False,
                "error": f"停车不足 {min_duration} 分钟（当前 {minutes:.0f} 分钟），还不能挪走",
            }

        result = await self._post(client, "unpark.php", {"userCarId": car.get("id")})
        if not result or not result.get("success"):
            return {"success": False, "error": (result or {}).get("error") or "挪车失败"}

        earned = self._to_int(result.get("earned"), 0)
        self._total_earned += earned
        self._set_result(f"手动挪车 1 辆，收益 {earned} 金币")
        # 重新取一次状态刷新快照，让页面立刻反映这次操作
        fresh = await client.get_json(f"{self.API_BASE}/state.php")
        if fresh and fresh.get("success"):
            await self._record_round(client, fresh, fresh.get("userId"), 0, 1, earned)
        logger.info(f"[不可躺自动抢车位] 手动挪车成功，车辆 {target}，收益 {earned} 金币")
        return {"success": True, "earned": earned}

    @staticmethod
    def _garage(cars: List[dict], min_duration: int) -> dict:
        """
        车库视图：每辆车一张卡片。

        停够最短时长的车才给「立即挪车」按钮，其余按钮置灰并显示还差几分钟。
        车牌、每分钟收益、状态、已停时长都直接来自游戏车库数据（state.php 的 userCars）。
        """
        cards = []
        for item in cars:
            idle = item.get("status") == "空闲"
            minutes = int(item.get("mins") or 0)
            can_unpark = (not idle) and minutes >= min_duration
            if idle:
                button_text = "空闲中"
            elif can_unpark:
                button_text = "立即挪车"
            else:
                button_text = f"还需 {min_duration - minutes} 分钟"

            if idle:
                status_color = "#9E9E9E"
                detail = "当前未停车"
            elif can_unpark:
                status_color = "#00E676"
                detail = f"{item.get('status')} · 已停 {minutes} 分钟 · 可结算 {item.get('gain', 0)} 金"
            else:
                status_color = "#FFB300"
                detail = f"{item.get('status')} · 已停 {minutes} 分钟 · 满 {min_duration} 分钟后可挪"

            cards.append({
                "component": "div",
                "props": {"style": {
                    "flex": "1 1 250px",
                    "minWidth": "230px",
                    "margin": "0 8px 8px 0",
                    "border": "1px solid rgba(128,128,128,0.3)",
                    "borderRadius": "8px",
                    "padding": "12px",
                }},
                "content": [
                    ParkingBot._text(item.get("name", ""),
                                     {"fontSize": "15px", "fontWeight": "bold"}),
                    ParkingBot._text(f"{item.get('rate', 0)} 金/分钟",
                                     {"fontSize": "12px", "color": "#4FC3F7"}),
                    ParkingBot._text(detail,
                                     {"fontSize": "12px", "color": status_color,
                                      "marginTop": "4px", "marginBottom": "10px"}),
                    {
                        "component": "VBtn",
                        "props": {
                            "block": True,
                            "size": "small",
                            "variant": "tonal",
                            "color": "primary" if can_unpark else "grey",
                            "disabled": not can_unpark,
                            "onClick": f"unpark:{item.get('id')}",
                        },
                        "text": button_text,
                    },
                ],
            })

        return {
            "component": "div",
            "props": {"style": {"display": "flex", "flexWrap": "wrap", "marginRight": "-8px"}},
            "content": cards,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页：今日收益、下次运行倒计时、车库、我的车位与执行历史。"""
        snapshot, history = self._page_data()
        today_earned = self._today_earned(history)
        min_duration = self._to_int(snapshot.get("min_duration"), 10, minimum=1)

        # ---------- 概览指标 ----------
        if snapshot:
            stats = [
                self._stat("今日收益", today_earned, "#00E676"),
                self._stat("金币", snapshot.get("cash", 0), "#FFD700"),
                self._stat("待结算", snapshot.get("pending_gain", 0), "#4FC3F7"),
                self._stat("距下次运行", self._next_run_text(), "#B388FF"),
            ]
            header = f"{snapshot.get('name', '未命名')} · Lv.{snapshot.get('level', 1)}"
            subtitle = (f"执行周期 {self._cron or '未启用'}"
                        f" · 停车中 {snapshot.get('parked_total', 0)}/{snapshot.get('car_total', 0)} 辆"
                        f" · 数据时间 {snapshot.get('time', '-')}")
        else:
            stats = [
                self._stat("今日收益", today_earned, "#00E676"),
                self._stat("金币", "-", "#9E9E9E"),
                self._stat("待结算", "-", "#9E9E9E"),
                self._stat("距下次运行", self._next_run_text(), "#B388FF"),
            ]
            header = self.plugin_name
            subtitle = "勾选「立即执行一次」后这里会显示停车场实况"

        last_run = self._last_run
        if self._last_run_ts:
            last_run = f"{last_run}（{datetime.fromtimestamp(self._last_run_ts):%Y-%m-%d %H:%M:%S}）"

        spot_rows = [[item.get("label", ""), item.get("occupant", "")]
                     for item in (snapshot.get("spots") or [])]
        history_rows = [[item.get("time", ""), str(item.get("parked", 0)),
                         str(item.get("moved", 0)), str(item.get("earned", 0))]
                        for item in history[:10]]

        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "color": "primary", "style": {"marginBottom": "12px"}},
                "content": [
                    {"component": "VCardText", "content": [
                        self._text(header, {"fontSize": "18px", "fontWeight": "bold"}),
                        self._text(subtitle, {"fontSize": "12px", "opacity": "0.8", "marginBottom": "6px"}),
                        self._text(f"最近一轮：{last_run}",
                                   {"fontSize": "13px", "opacity": "0.85", "marginBottom": "2px"}),
                        self._text(f"本次进程累计挪车收益：{self._total_earned} 金币",
                                   {"fontSize": "13px", "opacity": "0.85"}),
                    ]},
                ],
            },
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [stats[0]]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [stats[1]]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [stats[2]]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [stats[3]]},
            ]},
            self._card(
                "我的车库",
                f"数据来自游戏车库；停车不足 {min_duration} 分钟的车不能挪走，按钮置灰",
                [self._garage(snapshot.get("cars") or [], min_duration)],
            ),
            self._card(
                "我的车位",
                "私家车位被他人停用超过 5 分钟后对方才能贴条，收益按 70% / 30% 分成",
                [self._table(["车位", "当前占用"], spot_rows, ["1", "3"], ["left", "left"])],
            ),
            self._card(
                "执行历史",
                "最近 10 轮定时或手动执行的记录（最多保留 50 轮）",
                [self._table(["时间", "新停放", "挪走", "收益(金币)"],
                             history_rows,
                             ["2", "1", "1", "1"],
                             ["left", "center", "center", "right"])],
            ),
        ]

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        """在主面板登记一块状态卡。"""
        return [{"key": "status", "name": "不可躺抢车位"}]

    def get_dashboard(self, key: str, **kwargs: Any):
        """返回主面板状态卡：账户概览 + 本进程累计收益。"""
        if key != "status":
            return None
        snapshot, _ = self._page_data()
        if snapshot:
            subtitle = (f"{snapshot.get('name', '未命名')} · Lv.{snapshot.get('level', 1)}"
                        f" · 金币 {snapshot.get('cash', 0)}"
                        f" · 停车中 {snapshot.get('parked_total', 0)}/{snapshot.get('car_total', 0)}"
                        f" · 待结算 {snapshot.get('pending_gain', 0)}")
        else:
            subtitle = "尚未执行，勾选「立即执行一次」后显示实况"
        elements = [{
            "component": "VCardText",
            "content": [
                self._text(f"最近一轮：{self._last_run}",
                           {"fontSize": "13px", "opacity": "0.85"}),
                self._text(f"本进程累计挪车收益：{self._total_earned} 金币",
                           {"fontSize": "13px", "opacity": "0.85"}),
            ],
        }]
        cols = {"cols": 12, "md": 6, "lg": 4}
        global_config = {
            "refresh": 0,  # 不自动轮询，避免每次刷新都读库
            "border": True,
            "title": self.plugin_name,
            "subtitle": subtitle,
        }
        return cols, global_config, elements
