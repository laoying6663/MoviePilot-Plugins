# -*- coding: utf-8 -*-
"""不可躺自动抢车位 - MoviePilot V3 插件。

自动把车辆停到他人私家车位上赚金币，停够设定时长后自动挪走结算收益。
登录身份复用 MoviePilot「站点管理」中 tangpt.top 站点已保存的 Cookie，
不重复维护第二份登录态。
"""

import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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
    plugin_version = "1.0.0"
    plugin_author = "laoying6663"
    author_url = "https://github.com/laoying6663/MoviePilot-Plugins"
    plugin_config_prefix = "parkingbot_"
    plugin_order = 48
    auth_level = 1

    # 游戏接口根地址
    API_BASE = "https://qcw.tangpt.top/api"
    # 「站点管理」中用于定位目标站点的域名关键字
    SITE_KEYWORD = "tangpt.top"
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
    _cars_cache: Optional[Dict[int, dict]] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """
        读取插件配置并重建运行态。
        勾选「立即执行一次」时交给宿主调度器延迟触发，避免插件自建调度线程。
        """
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cron = str(config.get("cron") or "").strip() or self._default_cron
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
            logger.error(f"[不可躺自动抢车位] 执行周期表达式非法：{self._cron}，错误：{err}")
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
            self._set_result("缺少登录 Cookie")
            self._notify_once(
                "未取到登录 Cookie：请确认 MoviePilot「站点管理」中已添加并登录 tangpt.top，"
                "或在插件配置里手动填写游戏 Cookie。"
            )
            return

        client = AsyncRequestUtils(cookies=cookie, timeout=20)
        state = await client.get_json(f"{self.API_BASE}/state.php")
        if not state or not state.get("success"):
            reason = (state or {}).get("error") or "游戏接口无响应"
            self._set_result(f"获取状态失败：{reason}")
            self._notify_once(
                f"登录状态失效：{reason}。请到「站点管理」重新登录 tangpt.top，"
                "或更新插件配置中的游戏 Cookie。"
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

        parked = [c for c in my_cars if c.get("status") != "idle"]
        idle = [c for c in my_cars if c.get("status") == "idle"]

        earned_total, moved = await self._unpark_due_cars(client, parked, idle, target_minutes)
        new_parked = await self._park_idle_cars(
            client, idle, state.get("parkingSpots") or [], user_id,
            max(0, self._max_cars - (len(parked) - moved)),
        )
        self._summarize(new_parked, moved, earned_total)

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

    async def _load_cars_map(self, client: AsyncRequestUtils) -> Dict[int, dict]:
        """
        拉取车型定义表并按进程缓存，用于挑选每分钟收益最高的车辆。
        车型表是静态数据，首次成功后不再重复请求。
        """
        if self._cars_cache:
            return self._cars_cache
        data = await client.get_json(f"{self.API_BASE}/game_data.php")
        if data and data.get("success"):
            self._cars_cache = {
                int(item.get("id")): item
                for item in (data.get("cars") or [])
                if item.get("id") is not None
            }
        return self._cars_cache or {}

    # ================================================================
    # 辅助方法
    # ================================================================

    async def _resolve_cookie(self) -> str:
        """
        取得游戏所需的登录 Cookie。

        优先使用配置中手填的 Cookie；留空时回落到 MoviePilot「站点管理」里
        tangpt.top 站点已保存的 Cookie。用子串而非精确域名匹配，是因为站点
        在库里的 key 可能是 tangpt.top，也可能是 www.tangpt.top。
        """
        if self._cookie:
            return self._cookie
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
    def _now_naive() -> datetime:
        """按 MoviePilot 配置时区取当前时间，去掉时区信息以便与游戏本地时间比较。"""
        try:
            tz = pytz.timezone(settings.TZ) if settings.TZ else pytz.utc
        except Exception:
            tz = pytz.utc
        return datetime.now(tz).replace(tzinfo=None)

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        """解析游戏返回的时间字段，兼容 ISO 与「YYYY-MM-DD HH:MM:SS」两种写法。"""
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _earn_per_min(car: dict, cars_map: Dict[int, dict]) -> int:
        """返回车辆每分钟收益。限时车型（carId 为 0）的收益直接存在车辆实例上。"""
        if car.get("carId") == 0:
            return int(car.get("flashEarnPerMin") or 5)
        definition = cars_map.get(car.get("carId"))
        return int(definition.get("earnPerMin") or 0) if definition else 0

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
            self.post_message(message)
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
                                         "hint": "留空则自动复用「站点管理」中 tangpt.top 的 Cookie。"
                                                 "若主站 Cookie 无法登录游戏，可在此粘贴 qcw.tangpt.top 的 Cookie。",
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

    def get_page(self) -> List[dict]:
        """返回插件详情页：展示最近一轮结果与累计挪车收益。"""
        if self._last_run_ts:
            moment = datetime.fromtimestamp(self._last_run_ts).strftime("%Y-%m-%d %H:%M:%S")
            last_run = f"{self._last_run}（{moment}）"
        else:
            last_run = self._last_run
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 7},
                        "content": [
                            {"component": "VAlert",
                             "props": {"type": "info", "variant": "tonal",
                                       "text": f"最近一轮：{last_run}"}}
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 5},
                        "content": [
                            {"component": "VAlert",
                             "props": {"type": "success", "variant": "tonal",
                                       "text": f"累计挪车收益：{self._total_earned} 金币"}}
                        ],
                    },
                ],
            }
        ]
