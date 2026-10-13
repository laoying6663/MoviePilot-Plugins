# -*- coding: utf-8 -*-
"""不可躺自动抢车位 - MoviePilot V3 插件。

自动把车辆停到他人私家车位上赚金币，停够设定时长后自动挪走结算收益。
登录身份复用 MoviePilot「站点管理」中 tangpt.top 站点已保存的 Cookie，
不重复维护第二份登录态。
"""

import asyncio
import random
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pytz
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app.db.oper.site import SiteOper
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils
from app.sdk.plugin import _PluginBase
from app.schemas.types import EventType


class ActionResult(BaseModel):
    """
    操作类接口的业务字段集合。

    宿主要求每个普通 JSON endpoint 都声明具体 response_model，使响应结构能在
    /docs 里查到并被校验；返回裸 dict 不符合这个约定。
    """

    success: bool = Field(default=False, description="操作是否成功")
    error: Optional[str] = Field(default=None, description="失败原因，成功时为空")
    name: Optional[str] = Field(default=None, description="车型或操作对象名称")
    old: Optional[str] = Field(default=None, description="以旧换新的旧车名称")
    new: Optional[str] = Field(default=None, description="以旧换新的新车名称")
    price: Optional[int] = Field(default=None, description="花费或补价金币")
    earned: Optional[int] = Field(default=None, description="挪车结算收益")
    expGained: Optional[int] = Field(default=None, description="本次操作获得的经验")


class ApiResult(BaseModel):
    """
    宿主前端强制要求的响应信封。

    前端 axios 拦截器会校验：响应必须**恰好**是 success / message / data
    三个键，success 是布尔、message 是字符串（不能是 null）、data 存在。
    任何不符合的响应都会被判成 invalid-envelope，页面上直接弹
    「服务器返回了无效响应」——哪怕后端确实返回了 200。
    """

    success: bool = Field(description="操作是否成功")
    message: str = Field(description="失败原因；成功时为空字符串")
    data: Dict[str, Any] = Field(default_factory=dict, description="业务数据")


class TabSwitch(BaseModel):
    """
    详情页区块切换请求体。

    页面上声明的 events 会以 POST + JSON body 打过来，所以参数必须收成
    Pydantic 模型；写成 `tab: str` 会被 FastAPI 当成查询参数，body 就丢了。
    day 用来在「金币统计」页选择查看哪一天，留空表示看最新一天。
    """

    tab: str = Field(default="spots", description="区块标识：spots / shop / garage / stats")
    day: Optional[str] = Field(default=None, description="要查看的日期 YYYY-MM-DD，留空取最新")


class CarBuyRequest(BaseModel):
    """车市卡片「购买」按钮的请求体。"""

    car_id: Any = Field(description="要买的车型 ID")


class CarTradeRequest(BaseModel):
    """以旧换新确认后的请求体（旧车是车辆实例 ID，新车是车型 ID）。"""

    old_car_id: Any = Field(description="拿去做旧车的车辆实例 ID")
    new_car_id: Any = Field(description="换成的目标车型 ID")


class FlashBuyRequest(BaseModel):
    """特卖卡片「抢购」按钮的请求体。"""

    sale_id: Any = Field(description="特卖场次 ID")


class ShopFilterRequest(BaseModel):
    """车市顶部分类胶囊的请求体。"""

    category: str = Field(default="", description="只看该分类；留空表示看全部车型")


class TradePickRequest(BaseModel):
    """车市卡片「以旧换新」按钮的请求体：先选旧车，再确认换。"""

    car_id: Optional[Any] = Field(
        default=None, description="要换成的目标车型 ID；留空表示退出选车模式")


def _result(payload: Dict[str, Any]) -> ApiResult:
    """
    把接口内部构造的字典收敛成宿主要求的三键响应信封。

    success / error 之外的业务字段进 data，error 提升为 message。
    """
    ok = bool(payload.get("success"))
    data = {k: v for k, v in payload.items()
            if k not in ("success", "error") and v is not None}
    return ApiResult(
        success=ok,
        message="" if ok else str(payload.get("error") or "操作失败"),
        data=data,
    )

# ---------- 详情页配色与图标 ----------
# 取自游戏本体（qcw.tangpt.top）的配色：青为主色、紫为强调、金币金、
# 收益绿、爱心位粉、警示琥珀。插件页跟着游戏走，看起来才像同一个东西。
_CYAN = "#22D3EE"
_PURPLE = "#A78BFA"
_GOLD = "#FFD700"
_GREEN = "#00E676"
_PINK = "#FF4D6D"
_AMBER = "#FFB300"
_MUTED = "rgba(255,255,255,0.62)"
# 车市卡片单独用游戏 style.css 里的原始值，不跟通用配色混：卡片价格是 #ffd700、
# 收益是纯绿 #0f0、「已拥有 N 辆」是青色 #0df、等级不足是玫红 #e94560。
# 这几个值是从 qcw.tangpt.top/style.css 的 .car-card / .category-tab 抄的。
_SHOP_EARN = "#00FF00"
_SHOP_OWNED = "#00DDFF"
_SHOP_LOCK = "#E94560"
# 游戏站点的静态资源根目录。游戏把头像/车图存成相对路径（如 assets/avatars/a.png），
# 插件页在 MoviePilot 里渲染，相对路径会指到 MoviePilot 自己身上，所以要补全成绝对地址。
_GAME_BASE = "https://qcw.tangpt.top/"
# 空闲车位在游戏里是斜纹网格底，这里用纯 CSS 斜纹还原，不额外拉图
_HATCH = ("repeating-linear-gradient(45deg, rgba(255,255,255,0.05) 0 10px,"
          " rgba(255,255,255,0.015) 10px 20px)")

_ICONS = {
    "spots": ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@" fill="none" '
              'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
              'stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4.5"/>'
              '<path d="M9.6 17.6V6.4h3.4a3.3 3.3 0 0 1 0 6.6H9.6"/></svg>'),
    "shop": ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@" fill="none" '
             'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
             'stroke-linejoin="round"><path d="M5.3 8h13.4l-1 12.4H6.3L5.3 8Z"/>'
             '<path d="M9 8V6.2a3 3 0 0 1 6 0V8"/></svg>'),
    "garage": ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@" fill="none" '
               'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
               'stroke-linejoin="round"><rect x="3" y="3.5" width="18" height="17" rx="3"/>'
               '<path d="M7 20.5v-11h10v11"/><path d="M7 13.4h10M7 16.9h10"/></svg>'),
    "stats": ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@" fill="none" '
              'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
              'stroke-linejoin="round"><path d="M4 20V9.5M10 20V4M16 20v-7M22 20H2"/></svg>'),
}


class ParkingBot(_PluginBase):
    """自动停车 + 自动挪车。"""

    plugin_name = "不可躺自动抢车位"
    plugin_desc = ("自动把车停到他人私家车位赚金币，停够时长后自动挪车结算，"
                   "可选自动买车与以旧换新。复用「站点管理」里 tangpt.top 的登录态。")
    plugin_icon = "Parking_A.jpg"
    plugin_version = "1.14.0"
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
    # 金币足够时自动买车（默认关闭：涉及真实支出，必须用户主动开启）
    _auto_buy = False
    # 差价足够时自动以旧换新（默认关闭，同上）
    _auto_trade = False

    # ---------- 详情页区块 ----------
    # 详情页按区块互斥展示：顶部按钮 POST /tab 记住当前区块，重新拉页时只下发这一块。
    _PLUGIN_ID = "ParkingBot"
    _TABS = (("spots", "① 我的车位"), ("shop", "② 车市"),
             ("garage", "③ 我的车库"), ("stats", "④ 数据统计"))
    _tab = "spots"
    _day = ""
    # 本轮自动买卖的花费。由 _auto_shop 累加、_record_round 并入按天统计后清零。
    # 只在内存里跨这一轮传递，不单独落盘。
    _shop_spent = 0
    _shop_bought = 0
    _shop_diff = 0
    _shop_traded = 0

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
        self._auto_buy = self._as_switch(config.get("auto_buy"))
        self._auto_trade = self._as_switch(config.get("auto_trade"))
        # 记住上次停留的区块与查看的日期；插件重载、MoviePilot 重启后仍回到原来那一页
        saved_view = self.get_data("view") or {}
        if not isinstance(saved_view, dict):
            saved_view = {}
        saved_tab = str(saved_view.get("tab") or "").strip()
        self._tab = saved_tab if saved_tab in dict(self._TABS) else "spots"
        saved_day = str(saved_view.get("day") or "").strip()
        self._day = saved_day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", saved_day or "") else ""
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
        # 名下一辆车都没有时先看能不能自己买一辆 —— 否则「自动买车」开关对
        # 新账号永远是死开关，因为这里会先一步 return 掉。
        if not my_cars and not self._auto_buy:
            self._set_result("账号下没有车辆")
            logger.warning("[不可躺自动抢车位] 该账号名下没有任何车辆，无法执行自动停车")
            return
        if not my_cars:
            logger.info("[不可躺自动抢车位] 账号下没有车辆，尝试由自动买车补一辆")

        # 状态分三类：idle 空闲、charity 爱心停车、其余为普通停车。
        # 爱心车位是玩家主动做的公益停车，结算的是经验而非金币，
        # 既不纳入自动挪车，也不该被就地重新停到别人车位上去。
        parked = [c for c in my_cars if c.get("status") not in ("idle", "charity")]
        idle = [c for c in my_cars if c.get("status") == "idle"]
        charity = [c for c in my_cars if c.get("status") == "charity"]
        if charity:
            logger.debug(f"[不可躺自动抢车位] {len(charity)} 辆车停在爱心车位，不参与自动挪车")

        # 买卖时机是有讲究的，不能都塞在挪车之前：
        #   买车 —— 排在停车之前，买到的车当轮就能停出去赚钱
        #   换新 —— 必须排在挪车之后。以旧换新要求旧车处于空闲，而刚挪完的车
        #           正好变空闲；排在挪车之前的话，上一轮停着的车都还「停车中」，
        #           能当旧车的只剩恰好被剩在空闲池里的少数几辆，几乎换不动。
        shop_note = ""
        stale_after_shop = False
        cars_map = await self._load_cars_map(client)

        def _recompute(state: dict) -> None:
            """按最新状态重算本轮要动的三类车辆与挪车时长门槛。"""
            nonlocal parked, idle, charity, min_duration, target_minutes
            settings_map = state.get("gameSettings") or {}
            min_duration = self._to_int(settings_map.get("MIN_PARK_DURATION"),
                                        10, minimum=1)
            target_minutes = max(self._park_minutes, min_duration)
            mine = [c for c in (state.get("userCars") or [])
                    if c.get("userId") == user_id]
            parked = [c for c in mine if c.get("status") not in ("idle", "charity")]
            idle = [c for c in mine if c.get("status") == "idle"]
            charity = [c for c in mine if c.get("status") == "charity"]

        async def _refresh_state() -> bool:
            """重新拉一次游戏状态。买卖改动了服务端，拿旧快照继续操作会用到废车 ID。"""
            nonlocal state
            fresh = await client.get_json(f"{self.API_BASE}/state.php")
            if fresh and fresh.get("success"):
                state = fresh
                _recompute(state)
                return True
            return False

        if self._auto_buy:
            try:
                note = await self._auto_buy_one(client, state, cars_map, user_id)
                if note:
                    shop_note = note
                    if not await _refresh_state():
                        stale_after_shop = True
                        logger.warning(
                            "[不可躺自动抢车位] 自动买车后刷新状态失败，本轮跳过挪车与停车，"
                            "避免用过期的车辆 ID 继续操作")
            except Exception as err:
                logger.error(f"[不可躺自动抢车位] 自动买车异常：{err}")

        if stale_after_shop:
            self._set_result(shop_note or "状态刷新失败，本轮未挪车")
            await self._record_round(client, state, user_id, 0, 0, 0, note=shop_note)
            return

        earned_total, moved = await self._unpark_due_cars(client, parked, idle, target_minutes)

        # 挪车之后：以旧换新只在这一步做。刚挪完的车刚从「停车中」变成「空闲」，
        # 这才是游戏允许拿它当旧车的时刻。挪车前那批车还停着，换不动。
        if self._auto_trade:
            try:
                # 先刷新：上面挪过的车，本地这份 state 里还标着「停车中」
                if moved and not await _refresh_state():
                    stale_after_shop = True
                    logger.warning(
                        "[不可躺自动抢车位] 挪车后刷新状态失败，本轮跳过换新与停车，"
                        "避免用过期的车辆 ID 继续操作")
                if not stale_after_shop:
                    note = await self._auto_trade_one(client, state, cars_map, user_id)
                    if note:
                        shop_note = f"{shop_note}；{note}" if shop_note else note
                        if not await _refresh_state():
                            stale_after_shop = True
                            logger.warning(
                                "[不可躺自动抢车位] 自动换新后刷新状态失败，本轮跳过停车，"
                                "避免用过期的车辆 ID 继续操作")
            except Exception as err:
                logger.error(f"[不可躺自动抢车位] 自动换新异常：{err}")

        if stale_after_shop:
            self._set_result(shop_note or "状态刷新失败，本轮未停车")
            await self._record_round(client, state, user_id, 0, moved, earned_total,
                                     note=shop_note)
            return

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
            new_parked, moved, earned_total, note=shop_note,
        )

    async def _auto_trade_one(self, client: Any, state: dict, cars_map: Dict[Any, dict],
                             user_id: Any) -> str:
        """
        自动以旧换新。只在「挪车之后」调用。

        游戏要求旧车必须处于空闲状态，而刚挪完的车正好从停车中变空闲 ——
        这是唯一说得通的触发时机，所以这个方法由 run_once 放在挪车之后调用。

        每轮最多换一辆：一次换多辆既不必要，也会让后续几轮因为没钱而停摆。
        返回一句执行摘要，供详情页展示。
        """
        if not self._auto_trade:
            return ""
        rules = state.get("gameSettings") or {}
        mine = (state.get("users") or {}).get(user_id) or {}
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        my_cars = [c for c in (state.get("userCars") or [])
                   if str(c.get("userId")) == str(user_id)]

        idle_cars = sorted((c for c in my_cars if c.get("status") == "idle"),
                           key=lambda c: self._earn_per_min(c, cars_map))
        if not idle_cars:
            logger.debug("[不可躺自动抢车位] 本轮没有空闲车，跳过自动以旧换新")
            return ""

        for car in idle_cars:
            # 限时/限量车（carId 为 0）不可再生，换掉就再也买不回来了，
            # 不拿它当自动换新的「旧车」，需要的话用 /换车 手动处理。
            if car.get("carId") == 0:
                logger.debug("[不可躺自动抢车位] 限时车型不参与自动以旧换新")
                continue
            old_rate = self._earn_per_min(car, cars_map)
            old_def = self._lookup_car(cars_map, car.get("carId"))
            if not old_def:
                # 车型表里查不到的车同样不碰：算不出旧价，差价无从谈起，
                # 贸然换掉有可能白扔一辆好车。
                logger.debug(
                    "[不可躺自动抢车位] 车型 %s 不在车型表中，跳过自动以旧换新",
                    car.get("carId"))
                continue
            old_price = self._to_int(old_def.get("price"), 0, minimum=0)
            best = None
            for definition in cars_map.values():
                if not isinstance(definition, dict):
                    continue
                price = self._to_int(definition.get("price"), 0, minimum=0)
                diff = price - old_price
                # 以旧换新只扣「差价」，不是新车的全价：旧车已经折价抵掉了大半。
                # 所以这里按差价卡金币门槛，全价高的车型也能换得起。
                if diff <= 0:
                    continue
                if self._to_int(definition.get("levelReq"), 1, minimum=1) > level:
                    continue
                if diff > cash:
                    continue
                gain = self._to_int(definition.get("earnPerMin"), 0) - old_rate
                if gain <= 0:
                    continue
                if best is None or gain > best[0]:
                    best = (gain, definition, diff)
            if best is None:
                continue
            gain, target, diff = best
            result = await self._post(client, "trade_in.php", {
                "oldCarInstanceId": car.get("id"),
                "newCarId": target.get("id"),
            })
            if not (result and result.get("success")):
                logger.warning(
                    f"[不可躺自动抢车位] 自动以旧换新失败：{(result or {}).get('error') or '未知原因'}")
                return ""
            # 记账给「数据统计」页用：换车补的差价是真金白银的支出
            self._shop_diff += diff
            self._shop_traded += 1
            note = (f"自动以旧换新：{self._car_name(car, cars_map)} → {target.get('name')}，"
                    f"补 {diff} 金，每分钟收益 +{gain}")
            logger.info(f"[不可躺自动抢车位] {note}")
            return note
        return ""

    async def _auto_buy_one(self, client: Any, state: dict, cars_map: Dict[Any, dict],
                            user_id: Any) -> str:
        """
        自动买车。排在停车之前调用，买到的车当轮就能停出去赚钱。

        每轮最多买一辆。返回一句执行摘要，供详情页展示。
        """
        if not self._auto_buy:
            return ""
        rules = state.get("gameSettings") or {}
        mine = (state.get("users") or {}).get(user_id) or {}
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        max_cars = self._to_int(rules.get("MAX_CARS"), 10, minimum=1)
        my_cars = [c for c in (state.get("userCars") or [])
                   if str(c.get("userId")) == str(user_id)]
        if len(my_cars) >= max_cars:
            return ""
        if not my_cars:
            logger.info("[不可躺自动抢车位] 名下没有车辆，自动买车将补上第一辆")

        best = None
        for definition in cars_map.values():
            if not isinstance(definition, dict):
                continue
            price = self._to_int(definition.get("price"), 0, minimum=0)
            need = self._to_int(definition.get("levelReq"), 1, minimum=1)
            if need > level or price > cash:
                continue
            earn = self._to_int(definition.get("earnPerMin"), 0)
            if earn <= 0:
                continue
            if best is None or earn > best[0]:
                best = (earn, definition, price)
        if best is None:
            return ""
        earn, target, price = best
        result = await self._post(client, "buy_car.php", {"carId": target.get("id")})
        if not (result and result.get("success")):
            logger.warning(
                f"[不可躺自动抢车位] 自动买车失败：{(result or {}).get('error') or '未知原因'}")
            return ""
        # 记账给「数据统计」页：买车是全额支出，和换车的差价分开记
        self._shop_spent += price
        self._shop_bought += 1
        note = f"自动买车：{target.get('name')}，花费 {price} 金，每分钟收益 {earn}"
        logger.info(f"[不可躺自动抢车位] {note}")
        return note

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

    async def _load_flash_sales(self, client: AsyncRequestUtils) -> List[dict]:
        """
        拉取限时/限量特卖列表。

        这是一个锦上添花的功能，接口挂了或返回结构异常都不该影响停车挪车主流程，
        所以整段都兜住异常并返回空列表 —— 解析部分必须放在 try 内部，
        只包住请求那一行的话，返回非字典时仍会抛 AttributeError 打断主流程。
        """
        try:
            data = await client.get_json(f"{self.API_BASE}/flash_sale.php")
            if not isinstance(data, dict) or not data.get("success"):
                return []
            raw_sales = data.get("sales") or []
            if not isinstance(raw_sales, list):
                return []
            sales = []
            for item in raw_sales:
                if not isinstance(item, dict):
                    continue
                sales.append({
                    "id": item.get("id"),
                    "name": str(item.get("carName") or "特卖车型"),
                    "price": self._to_int(item.get("price"), 0, minimum=0),
                    "original": self._to_int(item.get("originalPrice"), 0, minimum=0),
                    "earn": self._to_int(item.get("earnPerMin"), 0, minimum=0),
                    "level": self._to_int(item.get("levelReq"), 1, minimum=1),
                    "timed": str(item.get("saleType") or "") == "timed",
                    "endsAt": item.get("endsAt"),
                    "stock": self._to_int(item.get("totalStock"), 0, minimum=0),
                    "sold": self._to_int(item.get("soldCount"), 0, minimum=0),
                    "image": self._asset_url(item.get("image")
                                            or item.get("carImage")),
                })
            return sales
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] 读取特卖列表失败，已忽略：{err}")
            return []

    async def _load_game_context(self) -> Tuple[Optional[Any], Optional[dict], Dict[Any, dict], str]:
        """
        取出「HTTP 客户端 + 游戏状态 + 车型表」。

        停车、买车、换车、抢购四个动作共用，避免四处各写一遍鉴权与错误处理。
        第三个返回值非空即表示出错，此时前两项不可用。
        """
        cookie = await self._resolve_cookie()
        if not cookie:
            return None, None, {}, self._sso_error or "未取到登录 Cookie"
        client = AsyncRequestUtils(cookies=cookie, timeout=20)
        state = await client.get_json(f"{self.API_BASE}/state.php")
        if not state or not state.get("success"):
            await self._drop_cached_cookie()
            return client, None, {}, f"登录状态失效：{(state or {}).get('error') or '游戏接口无响应'}"
        return client, state, await self._load_cars_map(client), ""

    async def _refresh_snapshot(self, client: Any, note: str) -> None:
        """动作成功后重新取状态刷新快照；取不到就跳过，不影响已成功的动作。"""
        try:
            fresh = await client.get_json(f"{self.API_BASE}/state.php")
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] {note} 后刷新状态失败：{err}")
            return
        if fresh and fresh.get("success"):
            await self._record_round(
                client, fresh, fresh.get("userId"), 0, 0, 0, record_history=False,
            )

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
    def _as_switch(value: Any) -> bool:
        """
        严格解析开关型配置。

        自动买车 / 自动以旧换新会真实扣金币，所以不能直接 bool(value)：
        字符串 "false"、"0"、"no"、"off" 都会被 bool() 判成 True，等于把
        「默认关闭」的承诺悄悄作废。除这些明确的否定写法外，一律按「没配」处理，
        只有真正的 True / 1 才算开。
        """
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return value is True or value == 1

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
        record_history: bool = True,
        note: str = "",
    ) -> None:
        """
        记录本轮结果：账户快照 + 执行历史，供插件详情页展示。

        快照保存的是已经算好的展示字段，不再依赖游戏接口 —— 页面渲染走同步读，
        不会每次打开详情页都去打一次游戏。

        :param record_history: 买卖类操作只是刷新展示，不该在执行历史里
            留下一条「0 停车 / 0 挪车 / 0 收益」的噪声记录，传 False 跳过。
        """
        try:
            cars_map = await self._load_cars_map(client)
            flash = await self._load_flash_sales(client)
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
                # 限时车（carId 为 0）名字与图都挂在车辆实例上，不在车型表里
                definition = self._lookup_car(cars_map, car.get("carId")) or {}
                cars.append({
                    "id": car.get("id"),
                    "carId": car.get("carId"),
                    "name": self._car_name(car, cars_map),
                    "image": self._asset_url(definition.get("image")
                                            or car.get("flashCarImage")),
                    "rate": rate,
                    "status": "空闲" if status == "idle" else ("爱心停车" if status == "charity" else "停车中"),
                    "mins": round(mins),
                    # 收益按「已停时长 × 每分钟收益」估算，单车位封顶。
                    # 爱心车位结算的是经验不是金币，不能算进待结算金币。
                    "gain": min(int(mins * rate), max_earn) if status == "parked" else 0,
                })
            # 排序口径必须与 _ordered_my_cars 完全一致，
            # 否则页面显示的序号和 /挪车 接受的序号会错位。
            cars.sort(key=lambda item: (self._car_rank(item["status"]), -item["rate"]))
            for index, item in enumerate(cars, start=1):
                item["no"] = index

            spots = [self._spot_snapshot(spot, index, state, cars_map, rules, now)
                     for index, spot in enumerate(my_spots, start=1)]

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
                "note": note,
                "cars": cars,
                "spots": spots,
                # 车市需要的车型目录：价格、每分钟收益、等级门槛。
                # 只带展示字段，避免把 image 之类的无用数据塞进快照。
                "catalog": [
                    {
                        "id": item.get("id"),
                        "name": str(item.get("name") or "未知车型"),
                        "price": self._to_int(item.get("price"), 0, minimum=0),
                        "earn": self._to_int(item.get("earnPerMin"), 0, minimum=0),
                        "level": self._to_int(item.get("levelReq"), 1, minimum=1),
                        "category": str(item.get("category") or ""),
                        # 车图：车位卡片要直接显示停着的那辆车
                        "image": self._asset_url(item.get("image")),
                    }
                    for item in sorted(cars_map.values(), key=lambda c: self._to_int(c.get("price"), 0))
                ],
                "flash": flash,
            }
            await self.async_save_data("snapshot", snapshot)

            if not record_history:
                # 买卖类操作只刷新展示，历史保持原样
                self._page_cache = {
                    "snapshot": snapshot,
                    "history": (self._page_cache or {}).get("history") or [],
                    "daily": (self._page_cache or {}).get("daily") or {},
                }
                return

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

            # 执行历史只有 50 轮（约 4 小时），撑不起「每天金币统计」。
            # 这里另存一份按天累计：日期 → 当日收益/停放/挪走/轮次 + 24 小时分桶，
            # 长期保留，「金币统计」页的柱状图与日汇总都从这里出。
            day_key = now.strftime("%Y-%m-%d")
            hour_key = str(now.hour)
            if self._page_cache is not None:
                daily = dict(self._page_cache.get("daily") or {})
            else:
                stored_daily = await self.async_get_data("daily") or {}
                daily = dict(stored_daily) if isinstance(stored_daily, dict) else {}
            bucket = dict(daily.get(day_key) or {})
            bucket["earned"] = self._to_int(bucket.get("earned"), 0) + earned
            bucket["parked"] = self._to_int(bucket.get("parked"), 0) + new_parked
            bucket["moved"] = self._to_int(bucket.get("moved"), 0) + moved
            bucket["rounds"] = self._to_int(bucket.get("rounds"), 0) + 1
            # 买卖花费并进当天：购车记全额、换车记差价，两者在统计页分开显示
            bucket["spent"] = self._to_int(bucket.get("spent"), 0) + self._shop_spent
            bucket["bought"] = self._to_int(bucket.get("bought"), 0) + self._shop_bought
            bucket["diff"] = self._to_int(bucket.get("diff"), 0) + self._shop_diff
            bucket["traded"] = self._to_int(bucket.get("traded"), 0) + self._shop_traded
            self._shop_spent = self._shop_bought = self._shop_diff = self._shop_traded = 0
            hours = dict(bucket.get("hours") or {})
            hours[hour_key] = self._to_int(hours.get(hour_key), 0) + earned
            bucket["hours"] = hours
            daily[day_key] = bucket
            # 只留最近 60 天，防止无限增长
            if len(daily) > 60:
                for old in sorted(daily)[:-60]:
                    daily.pop(old, None)
            await self.async_save_data("daily", daily)

            # 同步刷新内存缓存：页面渲染与之后的插件重载都直接读它
            self._page_cache = {"snapshot": snapshot, "history": history, "daily": daily}
        except Exception as err:
            logger.warning(f"[不可躺自动抢车位] 记录本轮结果失败：{err}")
            # 兜底给缓存一个确定值。否则 _page_cache 一直是 None，
            # get_page / get_dashboard 每次渲染都会回落成同步查库 ——
            # 正是当初加缓存想避免的开销。
            if self._page_cache is None:
                self._page_cache = {"snapshot": {}, "history": []}

    @classmethod
    def _spot_snapshot(cls, spot: dict, index: int, state: dict,
                        cars_map: Dict[Any, dict], rules: dict,
                        now: datetime) -> dict:
        """
        把一个车位整理成页面要用的展示字段。

        口径完全对齐游戏前端（js/modules/parking.js）：
        - 已停时长取自车位的 parkedTime，不是车辆的
        - 私家车位预估收益 = min(分钟 × 该车每分钟收益, 单车位上限)
        - 爱心车位不算金币，改为贡献经验，同样有上限
        """
        charity = spot.get("spotType") == "charity"
        kind = "爱心车位" if charity else "私家车位"
        out = {
            "label": f"{kind} {index}",
            "kind": kind,
            "occupant": cls._spot_occupant(spot, state, cars_map),
            "free": True,
        }

        car_in_spot = spot.get("carInSpot")
        if not car_in_spot:
            return out

        car = next((c for c in (state.get("userCars") or [])
                    if c.get("id") == car_in_spot), None)
        if not car:
            out["occupant"] = "已被占用"
            out["free"] = False
            return out

        definition = cls._lookup_car(cars_map, car.get("carId")) or {}
        parker = (state.get("users") or {}).get(car.get("userId")) or {}
        parked_at = cls._parse_time(spot.get("parkedTime"))
        mins = max(0, int((now - parked_at).total_seconds() / 60)) if parked_at else 0

        out.update({
            "free": False,
            "carName": cls._car_name(car, cars_map),
            "carImg": cls._asset_url(definition.get("image")
                                    or car.get("flashCarImage")),
            "parkedBy": str(parker.get("name") or "他人"),
            "avatar": cls._avatar_url(parker.get("avatar")),
            "avatarText": str(parker.get("avatar") or "M")[:1].upper(),
            "mins": mins,
            "mine": car.get("userId") == spot.get("ownerId"),
        })
        if charity:
            per_min = cls._to_int(rules.get("CHARITY_EXP_PER_MIN"), 2, minimum=0)
            limit = cls._to_int(rules.get("CHARITY_EXP_DAILY_LIMIT"), 200, minimum=0)
            out["exp"] = min(mins * per_min, limit)
        else:
            cap = cls._to_int(rules.get("MAX_EARN_PER_SPOT"), 3000, minimum=0)
            out["earn"] = min(mins * cls._to_int(definition.get("earnPerMin"), 0), cap)
        return out

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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {
                                         "model": "auto_buy",
                                         "label": "金币足够时自动买车",
                                         "hint": "默认关闭。打开后每轮最多买一辆："
                                                 "当前等级下、金币买得起、每分钟收益最高的车型。"
                                                 "会真实扣除金币，谨慎开启。",
                                     }}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {
                                         "model": "auto_trade",
                                         "label": "差价足够时自动以旧换新",
                                         "hint": "默认关闭。打开后每轮最多换一辆："
                                                 "从每分钟收益最低的空闲车开始，换成差价付得起且收益更高的车型。"
                                                 "只能换空闲车，停车中的车不参与。",
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
            "auto_buy": False,
            "auto_trade": False,
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
    def _icon(name: str, size: int = 20) -> dict:
        """
        内联 SVG 图标。

        不用 VIcon：宿主的图标组件在插件页里解析不出图标名，会渲染出
        一个 <path> 为空的空 svg，等于没有图标。内联 SVG 不依赖宿主的图标集，
        颜色跟着外层的 color 走（stroke="currentColor"）。
        """
        svg = _ICONS.get(name, "")
        return {
            "component": "span",
            "props": {"style": {"display": "block", "lineHeight": "0"}},
            "html": svg.replace("@SIZE@", f"{size}px"),
        }

    @staticmethod
    def _stat(label: str, value: Any, color: str) -> dict:
        """单个数字指标块。紫渐变底 + 小灰标签 + 大号彩值，对齐游戏车库页。"""
        return {
            "component": "div",
            "props": {"style": {
                "flex": "1 1 0",
                "minWidth": "128px",
                "margin": "0 8px 8px 0",
                "padding": "12px 14px",
                "borderRadius": "14px",
                "border": "1px solid rgba(167,139,250,0.28)",
                "backgroundColor": f"linear-gradient(135deg, rgba(167,139,250,0.20), rgba(34,211,238,0.08))",
            }},
            "content": [
                ParkingBot._text(label, {"fontSize": "11px", "opacity": "0.72",
                                         "letterSpacing": "0.5px"}),
                ParkingBot._text(value, {"fontSize": "23px", "fontWeight": "bold",
                                         "color": color, "lineHeight": "1.3",
                                         "marginTop": "3px"}),
            ],
        }

    @staticmethod
    def _card(title: str, subtitle: str, content: List[dict]) -> dict:
        """
        统一的信息卡片外壳。

        标题用 div 渲染而不依赖 VCardTitle 插槽：Vuetify 这类组件在本插件
        页面里只渲染出空壳，内容并不会填进去。卡片外壳同理改用 div，
        少一层 VCard + VCardText，样式也更可控。
        """
        return {
            "component": "div",
            "props": {"style": {
                "marginBottom": "12px",
                "padding": "14px 14px 4px",
                "borderRadius": "16px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "backgroundColor": "rgba(255,255,255,0.045)",
            }},
            "content": [
                ParkingBot._text(title, {"fontSize": "15px", "fontWeight": "bold",
                                         "color": _CYAN, "marginBottom": "4px",
                                         "letterSpacing": "0.4px"}),
                ParkingBot._text(subtitle, {"fontSize": "12px", "opacity": "0.58",
                                            "lineHeight": "1.55", "marginBottom": "10px"}),
                *content,
            ],
        }

    @staticmethod
    def _kv(label: str, value: Any, color: str = "") -> dict:
        """游戏里最常见的那种「小灰标签在上、彩色值在下」的字段。"""
        return {
            "component": "div",
            "props": {"style": {"marginTop": "8px"}},
            "content": [
                ParkingBot._text(label, {"fontSize": "11px", "opacity": "0.55"}),
                ParkingBot._text(value, {"fontSize": "15px", "fontWeight": "bold",
                                         "color": color, "marginTop": "1px"}),
            ],
        }

    @staticmethod
    def _asset_url(value: Any) -> str:
        """
        把游戏返回的相对路径补成绝对地址；已经是 http 开头或非路径值的原样返回空串。

        游戏侧 avatar 既可能是图片路径，也可能是性别字母 M/F；后者不是图片，
        这里返回空串，由调用方退回文字头像。
        """
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        # 带图片扩展名或含斜杠的才算路径，否则是 M/F 之类的文字标记
        if not (re.search(r"\.(png|jpe?g|gif|webp|svg)$", text, re.I) or "/" in text):
            return ""
        return _GAME_BASE + text.lstrip("/")

    @staticmethod
    def _avatar_url(value: Any) -> str:
        """
        头像地址。游戏的 avatar 字段不带 assets/avatars/ 前缀时要补上，
        规则与游戏 renderAvatar 保持一致。
        """
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://", "/", "assets/")):
            return ParkingBot._asset_url(text)
        if re.search(r"\.(png|jpe?g|gif|webp|svg)$", text, re.I):
            return _GAME_BASE + "assets/avatars/" + text
        return ""       # M / F 之类的性别标记，不是图片

    @staticmethod
    def _alpha(hex_color: str, alpha: float) -> str:
        """#RRGGBB + 透明度 → rgba()，给徽章/卡片做同色淡底。"""
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"

    @staticmethod
    def _badge(text: str, color: str) -> dict:
        """状态徽章：小圆角底 + 同色文字，游戏里「停车中」那种。"""
        return {
            "component": "div",
            "props": {"style": {
                "display": "inline-block",
                "padding": "3px 10px",
                "borderRadius": "6px",
                "fontSize": "12px",
                "fontWeight": "bold",
                "color": color,
                "backgroundColor": ParkingBot._alpha(color, 0.16),
            }},
            "text": text,
        }

    @staticmethod
    def _day_chips(days: List[Tuple[str, int]], current: str, api: str) -> dict:
        """
        参考图那种横向日期卡片：一天一张，点一下切到那天。

        每张卡直接带上「切到 stats 页并选中该日」的事件，所以点卡片会同时
        切换页签和日期 —— 否则人在别的页签上点了卡片，日期改了却看不见。
        卡片上显示的是当日净收（收益减掉买车与换车花费）。
        """
        cards = []
        for day, total in days:
            active = (day == current)
            cards.append({
                "component": "div",
                "props": {"style": {
                    "flex": "0 0 auto",
                    "minWidth": "118px",
                    "margin": "0 8px 8px 0",
                    "padding": "10px 14px",
                    "borderRadius": "12px",
                    "cursor": "pointer",
                    "userSelect": "none",
                    "border": (f"1px solid {_PURPLE}" if active
                               else "1px solid rgba(255,255,255,0.12)"),
                    "backgroundColor": (
                        "rgba(167,139,250,0.16)" if active else "rgba(255,255,255,0.04)"),
                }},
                "content": [
                    ParkingBot._text(day, {"fontSize": "13px", "fontWeight": "bold"}),
                    ParkingBot._text(f"🪙 {total}",
                                     {"fontSize": "14px",
                                      "color": _GREEN if total >= 0 else _PINK,
                                      "marginTop": "3px"}),
                ],
                "events": {"click": {"api": api, "method": "post",
                                     "params": {"tab": "stats", "day": day}}},
            })
        return {
            "component": "div",
            "props": {"style": {"display": "flex", "overflowX": "auto",
                                "marginRight": "-8px", "paddingBottom": "2px"}},
            "content": cards,
        }

    @staticmethod
    def _hour_chart(hours: Dict[str, Any]) -> dict:
        """
        24 小时收益柱状图。

        用 div 高度画，不用图表库：插件页是声明式 JSON，引不了 ApexCharts，
        而纯 CSS 柱子在任何宿主上都能出来。有收益的小时用金色，零收益压平。
        """
        values = [ParkingBot._to_int(hours.get(str(h)), 0) for h in range(24)]
        peak = max(values) or 1
        bars = []
        for hour, value in enumerate(values):
            ratio = value / peak if peak else 0
            height = max(2, round(ratio * 78))
            bars.append({
                "component": "div",
                "props": {"style": {
                    "flex": "1 1 0",
                    "display": "flex",
                    "flexDirection": "column",
                    "alignItems": "center",
                    "justifyContent": "flex-end",
                    "margin": "0 1px",
                }},
                "content": [
                    {"component": "div", "props": {"style": {
                        "width": "100%",
                        "height": f"{height}px",
                        "borderRadius": "3px 3px 0 0",
                        "backgroundColor": (_GOLD if value else "rgba(255,255,255,0.10)"),
                        "opacity": "1" if value else "0.5",
                    }}},
                ],
            })
        # 横轴标签每小时一个太挤，2 小时一个
        axis = [{"component": "div", "props": {"style": {
            "flex": "1 1 0", "textAlign": "center", "fontSize": "10px",
            "opacity": "0.55"}}, "text": f"{h:02d}" if h % 2 == 0 else ""}
            for h in range(24)]
        return {
            "component": "div",
            "props": {"style": {"marginTop": "4px"}},
            "content": [
                {"component": "div",
                 "props": {"style": {"display": "flex", "alignItems": "flex-end",
                                     "height": "80px",
                                     "borderBottom": "1px solid rgba(255,255,255,0.14)"}},
                 "content": bars},
                {"component": "div",
                 "props": {"style": {"display": "flex", "marginTop": "4px"}},
                 "content": axis},
            ],
        }

    def _stats_page(self, daily: Dict[str, Any], history: List[dict],
                    today_earned: int) -> dict:
        """
        数据统计页：日期卡片行 + 当日收支汇总 + 24 小时柱状图 + 每日明细表。

        执行历史只有 50 轮（约 4 小时），做不了按天统计，所以另有一份按天累计
        的存档 daily 在每轮结束时累加，这里只是把它画出来。
        支出分两项记：购车是全额花费，换车只算补的差价。
        """
        api = f"plugin/{getattr(self, 'plugin_id', None) or self._PLUGIN_ID}/tab"
        days = sorted(daily.keys(), reverse=True)
        if not days:
            return self._card(
                "数据统计",
                "从下一轮执行开始累积数据",
                [self._text("还没有统计数据，插件执行一轮后就会出现。",
                            {"fontSize": "13px", "opacity": "0.7"})])

        def day_row(d: str) -> Tuple[int, int, int]:
            b = daily.get(d) or {}
            earned = self._to_int(b.get("earned"), 0)
            cost = (self._to_int(b.get("spent"), 0) + self._to_int(b.get("diff"), 0))
            return earned, cost, earned - cost

        current = self._day if self._day in daily else days[0]
        bucket = daily.get(current) or {}
        hours = bucket.get("hours") or {}
        day_earned, day_cost, day_net = day_row(current)
        month_prefix = current[:7]
        month_days = [d for d in days if str(d).startswith(month_prefix)]
        m_earned = sum(day_row(d)[0] for d in month_days)
        m_cost = sum(day_row(d)[1] for d in month_days)
        avg = round(m_earned / max(1, len(month_days)))
        buys = sum(self._to_int((daily[d] or {}).get("bought"), 0) for d in month_days)
        trades = sum(self._to_int((daily[d] or {}).get("traded"), 0) for d in month_days)

        blocks = [
            # 最近 7 天日期卡片
            self._card("", "", [self._day_chips(
                [(d, day_row(d)[2]) for d in days[:7]], current, api)]),
            # 当日收支
            {"component": "div",
             "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                 "marginRight": "-8px", "marginBottom": "12px"}},
             "content": [
                 self._stat("当日收益", day_earned, _GOLD),
                 self._stat("当日支出", day_cost, _PINK),
                 self._stat("当日净收", day_net, _GREEN),
                 self._stat("挪走次数", self._to_int(bucket.get("moved"), 0), _PURPLE),
             ]},
            # 本月收支
            {"component": "div",
             "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                 "marginRight": "-8px", "marginBottom": "12px"}},
             "content": [
                 self._stat("本月收益", m_earned, _GOLD),
                 self._stat("本月支出", m_cost, _PINK),
                 self._stat("本月净收", m_earned - m_cost, _GREEN),
                 self._stat("本月日均", avg, _CYAN),
             ]},
            # 小时柱状图
            self._card(f"{current} · 每小时收益",
                       f"当日共 {self._to_int(bucket.get('rounds'), 0)} 轮执行，"
                       f"买车 {self._to_int(bucket.get('bought'), 0)} 次、"
                       f"换车 {self._to_int(bucket.get('traded'), 0)} 次",
                       [self._hour_chart(hours)]),
            # 每日明细
            self._card(
                "每日明细",
                f"共 {len(days)} 天，最多保留 60 天",
                [self._table(
                    ["日期", "收益", "买车", "换车差价", "净收"],
                    [[d,
                      str(day_row(d)[0]),
                      str(self._to_int((daily[d] or {}).get("spent"), 0)),
                      str(self._to_int((daily[d] or {}).get("diff"), 0)),
                      str(day_row(d)[2])]
                     for d in days[:30]],
                    ["3", "2", "2", "2", "2"],
                    ["left", "right", "right", "right", "right"])]),
            # 买卖汇总
            self._card(
                "本月买卖",
                f"买车 {buys} 次、换车 {trades} 次，"
                f"合计花出 {m_cost} 金币",
                [self._text(
                    f"买入 {sum(self._to_int((daily[d] or {}).get('spent'), 0) for d in month_days)} 金"
                    f"　·　换车补差价 "
                    f"{sum(self._to_int((daily[d] or {}).get('diff'), 0) for d in month_days)} 金",
                    {"fontSize": "14px", "color": _PINK})],
            ),
        ]
        # 最近几轮明细放在最下面，保留原来的执行历史能力
        if history:
            blocks.append(self._card(
                "最近 10 轮",
                "单轮执行明细（最多保留 50 轮）",
                [self._table(["时间", "新停放", "挪走", "收益(金币)"],
                             [[i.get("time", ""), str(i.get("parked", 0)),
                               str(i.get("moved", 0)), str(i.get("earned", 0))]
                              for i in history[:10]],
                             ["2", "1", "1", "1"],
                             ["left", "center", "center", "right"])]))
        return {"component": "div", "props": {"style": {}}, "content": blocks}

    @staticmethod
    def _pic(url: str, fallback: str, size: int = 56) -> dict:
        """
        一张图片节点，走内联 html（插件页的声明式 JSON 引不了图片组件）。

        游戏站的图挂了或没有图时退回占位符，所以内联 onerror 隐藏自己、显示兜底。

        loading="lazy" 是这里的关键：车市一屏能有二十多辆车图，不加懒加载会
        在打开页面时一次性全拉。游戏站本身有 Cloudflare 边缘缓存、浏览器再缓存
        4 小时，所以同图重复打开不会重复下载 —— 没必要把图搬到本地，
        真正浪费的是首屏把看不见的图也下下来。
        """
        if not url:
            return ParkingBot._text(fallback, {"fontSize": f"{size // 2}px",
                                                "textAlign": "center",
                                                "opacity": "0.45"})
        html = (
            f'<img src="{url}" loading="lazy" decoding="async" '
            f'style="width:{size}px;height:{size}px;object-fit:contain;" '
            f'onerror="this.style.display=\'none\';'
            f'this.nextElementSibling.style.display=\'flex\';">'
            f'<span style="display:none;width:{size}px;height:{size}px;align-items:center;'
            f'justify-content:center;font-size:{size // 2}px;opacity:0.45;">{fallback}</span>'
        )
        return {"component": "span", "props": {"style": {"lineHeight": "0"}}, "html": html}

    @staticmethod
    def _spot_grid(spots: List[dict]) -> dict:
        """
        我的车位：照游戏本体的画法，两列网格。

        被别人占用时按游戏那样显示：车图、停车人（带头像）、已停时长与预估收益；
        空闲位是斜纹底 + 角标。斜纹用 repeating-linear-gradient 直接画，不需要额外拉图。
        """
        cards = []
        for spot in spots:
            label = str(spot.get("label") or "")
            kind = str(spot.get("kind") or ("爱心车位" if "爱心" in label else "私家车位"))
            charity = "爱心" in kind
            free = spot.get("free", True)
            badge = ParkingBot._badge(f"{'❤' if charity else '🅿'} {kind}",
                                      _PINK if charity else _CYAN)
            if free:
                cards.append({
                    "component": "div",
                    "props": {"style": {
                        "flex": "1 1 46%",
                        "minWidth": "150px",
                        "margin": "0 8px 8px 0",
                        "padding": "12px",
                        "borderRadius": "14px",
                        "border": ("1px solid rgba(255,77,109,0.28)" if charity
                                   else "1px solid rgba(34,211,238,0.24)"),
                        "backgroundColor": _HATCH,
                    }},
                    "content": [
                        badge,
                        ParkingBot._text("空闲", {
                            "fontSize": "17px", "fontWeight": "bold",
                            "color": "rgba(255,255,255,0.42)",
                            "textAlign": "center", "marginTop": "18px"}),
                    ],
                })
                continue

            # 停车人一行：头像 + 名字；停我自己的车时标出来
            who = str(spot.get("parkedBy") or "他人")
            if spot.get("mine"):
                who = f"{who}（我的车）"
            avatar = spot.get("avatar")
            who_node = {
                "component": "div",
                "props": {"style": {"display": "flex", "alignItems": "center",
                                    "justifyContent": "center", "gap": "6px",
                                    "marginTop": "10px"}},
                "content": [
                    ParkingBot._pic(avatar, str(spot.get("avatarText") or "M"), 20),
                    ParkingBot._text(who, {"fontSize": "13px",
                                           "fontWeight": "bold",
                                           "color": _PURPLE if spot.get("mine")
                                           else "#FFFFFF"}),
                ],
            }

            mins = ParkingBot._to_int(spot.get("mins"), 0)
            if charity:
                detail = f"贡献经验 {ParkingBot._to_int(spot.get('exp'), 0)} exp"
                detail_color = _PURPLE
            else:
                detail = f"预估收益 🪙 {ParkingBot._to_int(spot.get('earn'), 0)}"
                detail_color = _GOLD

            cards.append({
                "component": "div",
                "props": {"style": {
                    "flex": "1 1 46%",
                    "minWidth": "150px",
                    "margin": "0 8px 8px 0",
                    "padding": "12px",
                    "borderRadius": "14px",
                    "border": ("1px solid rgba(255,77,109,0.30)" if charity
                               else "1px solid rgba(255,215,0,0.30)"),
                    "backgroundColor": "rgba(255,255,255,0.05)",
                    "textAlign": "center",
                }},
                "content": [
                    badge,
                    {"component": "div",
                     "props": {"style": {"display": "flex", "justifyContent": "center",
                                         "marginTop": "8px"}},
                     "content": [ParkingBot._pic(spot.get("carImg"),
                                                 str(spot.get("carName") or "🚗")[:1], 56)]},
                    ParkingBot._text(str(spot.get("carName") or "未知车型"),
                                     {"fontSize": "14px", "fontWeight": "bold",
                                      "marginTop": "4px"}),
                    who_node,
                    ParkingBot._text(f"已停 {mins} 分钟",
                                     {"fontSize": "12px", "opacity": "0.72",
                                      "marginTop": "6px"}),
                    ParkingBot._text(detail, {"fontSize": "13px", "color": detail_color,
                                             "fontWeight": "bold", "marginTop": "2px"}),
                ],
            })
        if not cards:
            return ParkingBot._text("还没有车位数据", {"fontSize": "13px", "opacity": "0.6"})
        return {
            "component": "div",
            "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                "marginRight": "-8px"}},
            "content": cards,
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
            return (self._page_cache.get("snapshot") or {},
                    self._page_cache.get("history") or [],
                    self._page_cache.get("daily") or {})
        snapshot = self.get_data("snapshot") or {}
        history = self.get_data("history") or []
        daily = self.get_data("daily") or {}
        # 存档被写坏时（比如历史升级后结构变了）不能让整个详情页崩成 500，
        # 退化成空数据即可，页面仍能看到框架和区块。
        if not isinstance(snapshot, dict):
            logger.warning("[不可躺自动抢车位] 账户快照格式异常，已按空数据展示")
            snapshot = {}
        if not isinstance(history, list):
            history = []
        if not isinstance(daily, dict):
            daily = {}
        self._page_cache = {"snapshot": snapshot, "history": history, "daily": daily}
        return snapshot, history, daily

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

    # ================================================================
    # 交互入口：远程命令 + 消息按钮回调
    #
    # 插件详情页是纯声明式 JSON，渲染器不绑定任何事件，页面上的按钮点不动。
    # 可交互的入口只有两个，都是宿主提供的能力：
    #   1. 远程命令 —— 任何支持命令的通知渠道（含微信）都能用
    #   2. 消息按钮 —— 通过 post_message(buttons=) 发 [PLUGIN]ParkingBot|xxx，
    #      由 MessageAction 接住，仅 Telegram / Slack 这类支持回调的渠道可见
    # ================================================================

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [
            {"cmd": "/车库", "event": EventType.PluginAction, "desc": "查看车库与可执行的操作",
             "category": "抢车位", "data": {"action": "parkingbot", "sub": "menu"}},
            {"cmd": "/挪车", "event": EventType.PluginAction, "desc": "立即挪车：/挪车 序号或车辆ID",
             "category": "抢车位", "data": {"action": "parkingbot", "sub": "unpark"}},
            {"cmd": "/买车", "event": EventType.PluginAction, "desc": "购买车辆：/买车 车型ID",
             "category": "抢车位", "data": {"action": "parkingbot", "sub": "buy"}},
            {"cmd": "/换车", "event": EventType.PluginAction, "desc": "以旧换新：/换车 旧车ID 新车型ID",
             "category": "抢车位", "data": {"action": "parkingbot", "sub": "trade"}},
            {"cmd": "/抢购", "event": EventType.PluginAction, "desc": "抢购特卖车：/抢购 场次ID",
             "category": "抢车位", "data": {"action": "parkingbot", "sub": "flash"}},
        ]

    @staticmethod
    def _spawn(coro) -> None:
        """在事件回调里跑异步逻辑：有事件循环就建任务，否则起后台线程。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(coro)
            return
        threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()

    def _reply_data(self, data: dict, title: str, text: str, buttons=None) -> None:
        """按事件上下文回复用户，失败只记日志不影响插件。"""
        try:
            self.post_message(
                channel=data.get("channel"),
                userid=data.get("userid"),
                title=title,
                text=text,
                buttons=buttons or None,
                original_message_id=data.get("original_message_id"),
                original_chat_id=data.get("original_chat_id"),
            )
        except Exception as err:
            logger.error(f"[不可躺自动抢车位] 回复消息失败：{err}")

    @eventmanager.register(EventType.PluginAction)
    def parkingbot_command(self, event: Event) -> None:
        """远程命令入口：只解析参数，动作交给异步任务。"""
        event_data = event.event_data or {}
        if event_data.get("action") != "parkingbot":
            return
        if not self.get_state():
            self._reply_data(event_data, "❌ 不可躺自动抢车位",
                             "插件未启用，请先在插件配置里打开「启用插件」。")
            return
        sub = event_data.get("sub") or "menu"
        args = str(event_data.get("arg_str") or "").split()
        sender = event_data
        if sub == "unpark":
            self._spawn(self._cmd_unpark(args, sender))
        elif sub == "buy":
            self._spawn(self._cmd_buy(args, sender))
        elif sub == "trade":
            self._spawn(self._cmd_trade(args, sender))
        elif sub == "flash":
            self._spawn(self._cmd_flash(args, sender))
        else:
            self._spawn(self._cmd_menu(sender))

    @eventmanager.register(EventType.MessageAction)
    def parkingbot_message_action(self, event: Event) -> None:
        """消息按钮回调入口，按钮数据格式为 [PLUGIN]ParkingBot|<动作>。"""
        event_data = event.event_data or {}
        if event_data.get("plugin_id") != self.__class__.__name__:
            return
        if not self.get_state():
            return
        text = str(event_data.get("text") or "")
        if "|" not in text:
            return
        payload = text.split("|", 1)[1].strip()
        if not payload:
            return
        self._spawn(self._handle_payload(payload, event_data))

    async def _handle_payload(self, payload: str, sender: dict) -> None:
        """把按钮回调的载荷转成对应命令。"""
        parts = payload.split(":")
        kind = parts[0]
        if kind == "menu":
            await self._cmd_menu(sender)
        elif kind == "unpark" and len(parts) >= 2:
            await self._cmd_unpark([parts[1]], sender)
        elif kind == "buy" and len(parts) >= 2:
            await self._cmd_buy([parts[1]], sender)
        elif kind == "trade" and len(parts) >= 3:
            await self._cmd_trade([parts[1], parts[2]], sender)
        elif kind == "flash" and len(parts) >= 2:
            await self._cmd_flash([parts[1]], sender)
        else:
            await self._cmd_menu(sender)

    # ---------- 车辆引用解析 ----------

    def _ordered_my_cars(self, state: dict, cars_map: Dict[Any, dict]) -> List[dict]:
        """
        名下车辆的统一排序口径：停车中的在前，其次爱心车位、空闲，
        同组内按每分钟收益从高到低。

        菜单里显示的序号和 /挪车 接受的序号都基于它。排序键走 _car_rank，
        与写快照时的口径完全一致 —— 两边若各写一套，游戏一旦新增状态字段
        就会出现「页面显示第 1 辆，/挪车 1 挪的却是另一辆」。
        """
        user_id = state.get("userId")
        mine = [c for c in (state.get("userCars") or [])
                if str(c.get("userId")) == str(user_id)]
        mine.sort(key=lambda c: (self._car_rank(c.get("status")),
                                 -self._earn_per_min(c, cars_map)))
        return mine

    # 排序权重表同时收录游戏原始状态与展示用中文，写快照时传的是后者、
    # /挪车 解析时传的是前者，查同一张表即可保证两边永远一致。
    _STATUS_RANK = {
        "停车中": 0, "parked": 0,
        "爱心停车": 1, "charity": 1,
        "空闲": 2, "idle": 2,
    }

    @classmethod
    def _car_rank(cls, status: Any) -> int:
        """
        按状态给出排序权重：停车中 0、爱心停车 1、空闲 2。

        刻意不做任何状态转换，直接查表 —— 若在这里先转成显示名，
        已经被转换过的中文会再走一次转换而被兜底成「停车中」，
        导致爱心车位和空闲车混进同一组、页面序号与指令序号错位。
        未知状态按「停车中」处理，与游戏前端 status !== 'idle' 的口径一致。
        """
        return cls._STATUS_RANK.get(status, 0)

    @staticmethod
    def _display_status(raw_status: Any) -> str:
        """把游戏原始状态映射成展示用中文。"""
        if raw_status == "idle":
            return "空闲"
        if raw_status == "charity":
            return "爱心停车"
        return "停车中"

    def _resolve_car_ref(self, token: str, ordered: List[dict]) -> Optional[str]:
        """
        把用户输入的序号或车辆 ID 解析成车辆实例 ID。

        解析不出来一律返回 None：只接受自己名下、且确实存在的车，
        避免用户误传 ID 导致对别人或已消失的车辆发请求。
        """
        text = str(token or "").strip()
        if not text:
            return None
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(ordered):
                return str(ordered[index - 1].get("id"))
            return None
        return text if any(str(c.get("id")) == text for c in ordered) else None

    # ---------- 各命令实现 ----------

    async def _cmd_menu(self, sender: dict) -> None:
        """发送车库菜单：文字列表 + 支持回调的渠道附带真按钮。"""
        client, state, cars_map, err = await self._load_game_context()
        if not state:
            self._reply_data(sender, "❌ 获取状态失败", err)
            return
        user_id = state.get("userId")
        rules = state.get("gameSettings") or {}
        min_duration = self._to_int(rules.get("MIN_PARK_DURATION"), 10, minimum=1)
        target = max(self._park_minutes, min_duration)
        ordered = self._ordered_my_cars(state, cars_map)
        mine = (state.get("users") or {}).get(user_id) or {}
        cash = self._to_int(mine.get("cash"), 0)
        max_cars = self._to_int(rules.get("MAX_CARS"), 10, minimum=1)

        lines = [
            f"账号：{mine.get('name') or '未命名'}　Lv.{self._to_int(mine.get('level'), 1, minimum=1)}",
            f"金币：{cash}　车辆：{len(ordered)}/{max_cars}　停满 {target} 分钟可挪",
            "",
        ]
        buttons = []
        for index, car in enumerate(ordered, start=1):
            name = self._car_name(car, cars_map)
            rate = self._earn_per_min(car, cars_map)
            status = car.get("status")
            if status == "parked":
                minutes = int(self._parked_minutes(car))
                ready = minutes >= target
                flag = "✅可挪" if ready else f"⏳还需 {target - minutes} 分"
                lines.append(f"{index}. {name}（{rate}金/分）已停 {minutes} 分钟 {flag}")
                if ready:
                    if not buttons:
                        buttons.append([])
                    buttons[-1].append({
                        "text": f"挪走 {name}",
                        "callback_data": f"[PLUGIN]{self.__class__.__name__}|unpark:{car.get('id')}",
                    })
            elif status == "charity":
                minutes = int(self._parked_minutes(car))
                lines.append(f"{index}. {name}（{rate}金/分）爱心车位 {minutes} 分钟")
            else:
                lines.append(f"{index}. {name}（{rate}金/分）空闲")

        # 买得起的车型直接给指令，省得翻车市页面
        catalog = sorted(cars_map.values(), key=lambda d: self._to_int(d.get("price"), 0))
        buyable = [
            d for d in catalog
            if self._to_int(d.get("levelReq"), 1, minimum=1) <= self._to_int(mine.get("level"), 1, minimum=1)
            and self._to_int(d.get("price"), 0) <= cash
            and len(ordered) < max_cars
        ][-3:]
        if buyable:
            lines.append("")
            lines.append("当前买得起的车（详情见插件页车市）：")
            for car in buyable:
                lines.append(f"　{car.get('name')}（{car.get('earnPerMin')}金/分）"
                             f" {self._to_int(car.get('price'), 0)}金　/买车 {car.get('id')}")

        lines.append("")
        lines.append("指令：/挪车 序号　/买车 车型ID　/换车 旧车ID 新车型ID　/抢购 场次ID")
        self._reply_data(sender, "🅿️ 不可躺抢车位", "\n".join(lines), buttons)

    async def _cmd_unpark(self, args: List[str], sender: dict) -> None:
        """立即挪车。"""
        if not args:
            self._reply_data(sender, "用法：/挪车 序号或车辆ID",
                             "先发 /车库 查看序号与车辆 ID。")
            return
        _, state, cars_map, err = await self._load_game_context()
        if not state:
            self._reply_data(sender, "❌ 获取状态失败", err)
            return
        ordered = self._ordered_my_cars(state, cars_map)
        car_id = self._resolve_car_ref(args[0], ordered)
        if not car_id:
            self._reply_data(sender, "❌ 没找到这辆车", "先发 /车库 查看序号与车辆 ID。")
            return
        result = await self.api_unpark(car_id)
        await self._after_action(result, sender, "挪车")

    async def _cmd_buy(self, args: List[str], sender: dict) -> None:
        """购买车辆。"""
        if not args:
            self._reply_data(sender, "用法：/买车 车型ID",
                             "车型 ID 见插件详情页的车市区块。")
            return
        result = await self.api_buy_car(args[0])
        await self._after_action(result, sender, "买车")

    async def _cmd_trade(self, args: List[str], sender: dict) -> None:
        """以旧换新。"""
        if len(args) < 2:
            self._reply_data(sender, "用法：/换车 旧车ID 新车型ID",
                             "旧车必须是空闲车辆；两个 ID 见插件详情页。")
            return
        result = await self.api_trade_in(args[0], args[1])
        await self._after_action(result, sender, "换车")

    async def _cmd_flash(self, args: List[str], sender: dict) -> None:
        """抢购特卖车。"""
        if not args:
            self._reply_data(sender, "用法：/抢购 场次ID",
                             "场次 ID 见插件详情页的「限时 / 限量特卖」区块。")
            return
        result = await self.api_buy_flash(args[0])
        await self._after_action(result, sender, "抢购")

    async def _after_action(self, result: ApiResult, sender: dict, label: str) -> None:
        """动作完成后先报结果，再把最新车库菜单发一遍。"""
        # 业务字段都在信封的 data 里，这里按需取用
        info = result.data or {}
        if result.success:
            detail = []
            if info.get("earned"):
                detail.append(f"收益 {info['earned']} 金")
            if info.get("price"):
                detail.append(f"花费 {info['price']} 金")
            if info.get("expGained"):
                detail.append(f"经验 +{info['expGained']}")
            text = "、".join(detail) if detail else "操作完成"
            self._reply_data(sender, f"✅ {label}成功", text)
        else:
            self._reply_data(sender, f"❌ {label}失败", result.message or "未知原因")
        await self._cmd_menu(sender)

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件页面按钮用到的后端接口。"""
        return [
            {
                "path": "/unpark",
                "endpoint": self.api_unpark,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即挪车",
                "response_model": ApiResult,
                "description": "按车辆 ID 执行一次挪车结算，停车不足游戏规定时长会被拒绝",
            },
            {
                "path": "/buy_car",
                "endpoint": self.api_buy_car,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "购买车辆",
                "response_model": ApiResult,
                "description": "按车型 ID 买车，等级、金币与车辆上限由后端二次校验",
            },
            {
                "path": "/trade_in",
                "endpoint": self.api_trade_in,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "以旧换新",
                "response_model": ApiResult,
                "description": "用一辆空闲车换更贵的车型",
            },
            {
                "path": "/buy_flash",
                "endpoint": self.api_buy_flash,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "抢购特卖车",
                "response_model": ApiResult,
                "description": "按特卖场次 ID 购买限时/限量车辆",
            },
            {
                "path": "/tab",
                "endpoint": self.api_set_tab,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换详情页区块",
                "response_model": ApiResult,
                "description": "记录详情页当前展示的区块；渲染器随后会重新拉取页面，只下发这一块",
            },
        ]

    async def api_set_tab(self, request: TabSwitch) -> ApiResult:
        """
        记住详情页当前停留的区块，以及「金币统计」页正在查看哪一天。

        页面渲染器在执行完事件后会重新拉一次 plugin/page/{id}，
        所以这里只要把选择记下来，get_page() 自然就只渲染选中的那一个区块。
        """
        tab = str(getattr(request, "tab", "") or "").strip().lower()
        if tab not in dict(self._TABS):
            return _result({"success": False, "error": f"未知区块：{tab}"})
        self._tab = tab
        saved = {"tab": tab}
        # day 只在「金币统计」页有意义；非空且格式合法才认
        day = str(getattr(request, "day", "") or "").strip()
        if day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            self._day = day
            saved["day"] = day
        try:
            await self.async_save_data("view", saved)
        except Exception as err:
            # 记不住只影响下次打开时回到默认页签，不该让点击直接失败
            logger.warning(f"[不可躺自动抢车位] 保存详情页页签失败：{err}")
        return _result({"success": True, "name": dict(self._TABS)[tab]})

    async def api_buy_car(self, car_id: Any) -> ApiResult:
        """
        购买车辆。

        等级、金币、车辆上限三项都在后端复核一遍。页面上的置灰只是提示，
        真正的规则必须由后端把关，否则绕过界面就能买超限的车。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        client, state, cars_map, err = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        definition = self._lookup_car(cars_map, car_id)
        if not definition:
            return _result({"success": False, "error": "车型不存在"})

        user_id = state.get("userId")
        mine = (state.get("users") or {}).get(user_id) or {}
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        price = self._to_int(definition.get("price"), 0, minimum=0)
        need_level = self._to_int(definition.get("levelReq"), 1, minimum=1)

        if need_level > level:
            return _result({"success": False, "error": f"等级不足，需要 Lv.{need_level}（当前 Lv.{level}）"})
        if price > cash:
            return _result({"success": False, "error": f"金币不足，还差 {price - cash} 金"})
        owned = sum(1 for c in (state.get("userCars") or [])
                    if str(c.get("userId")) == str(user_id))
        max_cars = self._to_int((state.get("gameSettings") or {}).get("MAX_CARS"), 10, minimum=1)
        if owned >= max_cars:
            return _result({"success": False, "error": f"车辆已满（上限 {max_cars} 辆）"})

        result = await client.post_json(
            f"{self.API_BASE}/buy_car.php",
            json={"carId": definition.get("id")},
        )
        if not result or not result.get("success"):
            return _result({"success": False, "error": (result or {}).get("error") or "购买失败"})
        name = definition.get("name") or "车辆"
        await self._refresh_snapshot(client, "买车")
        self._set_result(f"购买 {name}，花费 {price} 金币")
        logger.info(f"[不可躺自动抢车位] 已购买 {name}，花费 {price} 金币")
        return _result({"success": True, "name": name, "price": price,
                "expGained": self._to_int(result.get("expGained"), 0)})

    async def api_trade_in(self, old_car_id: Any, new_car_id: Any) -> ApiResult:
        """
        以旧换新。

        与买车同理，等级、补价、旧车归属与状态都在后端校验。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        old_key = str(old_car_id if old_car_id is not None else "").strip()
        if not old_key:
            return _result({"success": False, "error": "缺少旧车 ID"})

        client, state, cars_map, err = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        # 旧车必须是我的车，且处于空闲状态（停着的车不能拿去换）
        user_id = state.get("userId")
        old_car = next((c for c in (state.get("userCars") or [])
                        if str(c.get("id")) == old_key
                        and str(c.get("userId")) == str(user_id)), None)
        if not old_car:
            return _result({"success": False, "error": "旧车不存在或不属于当前账号"})
        if old_car.get("status") != "idle":
            return _result({"success": False, "error": "只有空闲车辆能用来以旧换新"})

        new_def = self._lookup_car(cars_map, new_car_id)
        if not new_def:
            return _result({"success": False, "error": "新车型不存在"})
        old_def = self._lookup_car(cars_map, old_car.get("carId")) or {}
        new_price = self._to_int(new_def.get("price"), 0, minimum=0)
        old_price = self._to_int(old_def.get("price"), 0, minimum=0)
        diff = new_price - old_price

        if diff <= 0:
            return _result({"success": False, "error": "只能换更贵的车"})
        mine = (state.get("users") or {}).get(user_id) or {}
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        need_level = self._to_int(new_def.get("levelReq"), 1, minimum=1)
        if need_level > level:
            return _result({"success": False, "error": f"等级不足，需要 Lv.{need_level}（当前 Lv.{level}）"})
        if diff > cash:
            return _result({"success": False, "error": f"金币不足，还差 {diff - cash} 金"})

        result = await client.post_json(
            f"{self.API_BASE}/trade_in.php",
            json={"oldCarInstanceId": old_car.get("id"), "newCarId": new_def.get("id")},
        )
        if not result or not result.get("success"):
            return _result({"success": False, "error": (result or {}).get("error") or "换车失败"})
        old_name = old_def.get("name") or "旧车"
        new_name = new_def.get("name") or "新车"
        await self._refresh_snapshot(client, "换车")
        self._set_result(f"以旧换新 {old_name} → {new_name}，补价 {diff} 金币")
        logger.info(f"[不可躺自动抢车位] 以旧换新 {old_name} → {new_name}，补价 {diff} 金币")
        return _result({"success": True, "old": old_name, "new": new_name, "price": diff,
                "expGained": self._to_int(result.get("expGained"), 0)})

    async def api_buy_flash(self, sale_id: Any) -> ApiResult:
        """购买限时/限量特卖车辆。"""
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        client, state, cars_map, err = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        sales = await self._load_flash_sales(client)
        sale = next((s for s in sales
                     if str(s.get("id")) == str(sale_id).strip()), None)
        if not sale:
            return _result({"success": False, "error": "特卖场次不存在或已结束"})

        user_id = state.get("userId")
        mine = (state.get("users") or {}).get(user_id) or {}
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        if sale.get("level", 1) > level:
            return _result({"success": False, "error": f"等级不足，需要 Lv.{sale.get('level')}"})
        if sale.get("price", 0) > cash:
            return _result({"success": False, "error": f"金币不足，还差 {sale.get('price', 0) - cash} 金"})
        owned = sum(1 for c in (state.get("userCars") or [])
                    if str(c.get("userId")) == str(user_id))
        max_cars = self._to_int((state.get("gameSettings") or {}).get("MAX_CARS"), 10, minimum=1)
        if owned >= max_cars:
            return _result({"success": False, "error": f"车辆已满（上限 {max_cars} 辆）"})

        result = await client.post_json(
            f"{self.API_BASE}/flash_sale.php",
            json={"action": "buy", "saleId": sale.get("id")},
        )
        if not result or not result.get("success"):
            return _result({"success": False, "error": (result or {}).get("error") or "抢购失败"})
        await self._refresh_snapshot(client, "抢购")
        name = sale.get("name") or "特卖车型"
        self._set_result(f"抢购成功 {name}，花费 {sale.get('price', 0)} 金币")
        logger.info(f"[不可躺自动抢车位] 抢购成功 {name}，花费 {sale.get('price', 0)} 金币")
        return _result({"success": True, "name": name, "price": sale.get("price", 0)})

    async def api_unpark(self, car_id: Any) -> ApiResult:
        """
        立即挪车接口。

        最短停车时长在服务端再校验一次：页面上的按钮禁用只是提示，
        真正的规则必须由后端把关，否则绕过界面就能提前挪车。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        # 车辆 ID 用字符串处理：游戏侧不保证是纯数字，按数字解析会把
        # 非数字 ID 误判成「缺少车辆 ID」。筛选时同样按字符串比较。
        target = str(car_id if car_id is not None else "").strip()
        if not target:
            return _result({"success": False, "error": "缺少车辆 ID"})

        cookie = await self._resolve_cookie()
        if not cookie:
            return _result({"success": False, "error": "未取到登录 Cookie"})
        client = AsyncRequestUtils(cookies=cookie, timeout=20)

        state = await client.get_json(f"{self.API_BASE}/state.php")
        if not state or not state.get("success"):
            await self._drop_cached_cookie()
            return _result({"success": False, "error": "登录状态失效，请重新获取会话"})

        # 必须在 userCars 全表里按 userId 过滤，不能只按 id 匹配：
        # state.php 返回的是所有玩家的车辆，缺了归属校验就能挪别人的车。
        owner_id = state.get("userId")
        car = next((c for c in (state.get("userCars") or [])
                    if str(c.get("id")) == target
                    and str(c.get("userId")) == str(owner_id)), None)
        if not car:
            return _result({"success": False, "error": "车辆不存在或不属于当前账号"})
        if car.get("status") == "idle":
            return _result({"success": False, "error": "该车辆当前不在停车中"})

        rules = state.get("gameSettings") or {}
        min_duration = self._to_int(rules.get("MIN_PARK_DURATION"), 10, minimum=1)
        minutes = self._parked_minutes(car)
        if minutes < min_duration:
            return _result({
                "success": False,
                "error": f"停车不足 {min_duration} 分钟（当前 {minutes:.0f} 分钟），还不能挪走",
            })

        result = await self._post(client, "unpark.php", {"userCarId": car.get("id")})
        if not result or not result.get("success"):
            return _result({"success": False, "error": (result or {}).get("error") or "挪车失败"})

        earned = self._to_int(result.get("earned"), 0)
        self._total_earned += earned
        self._set_result(f"手动挪车 1 辆，收益 {earned} 金币")
        # 重新取一次状态刷新快照，让页面立刻反映这次操作
        fresh = await client.get_json(f"{self.API_BASE}/state.php")
        if fresh and fresh.get("success"):
            await self._record_round(client, fresh, fresh.get("userId"), 0, 1, earned)
        logger.info(f"[不可躺自动抢车位] 手动挪车成功，车辆 {target}，收益 {earned} 金币")
        return _result({"success": True, "earned": earned})

    @staticmethod
    def _garage(cars: List[dict], min_duration: int) -> dict:
        """
        车库视图：照游戏车库页的排版，左图右信息，字段在上、值在下。

        停够最短时长的车才给「立即挪车」指令，其余置灰并显示还差几分钟。
        车牌、每分钟收益、状态、已停时长都直接来自游戏车库数据（state.php 的 userCars）。
        """
        cards = []
        for position, item in enumerate(cars, start=1):
            idle = item.get("status") == "空闲"
            minutes = int(item.get("mins") or 0)
            can_unpark = (not idle) and minutes >= min_duration
            if idle:
                button_text, status_color = "空闲中", "#9E9E9E"
            elif can_unpark:
                button_text, status_color = "立即挪车", _GREEN
            else:
                button_text, status_color = f"还需 {min_duration - minutes} 分钟", _AMBER

            # 序号兜底取列表位置：插件刚升级时页面上可能还是上一版写的快照，
            # 那种快照没有 no 字段，直接取会渲染成「/挪车 None」。
            # 列表顺序与 _ordered_my_cars 的排序口径一致，所以位置就是指令序号。
            number = item.get("no") or position
            name = str(item.get("name") or "")

            # 左图右信息，和游戏车库卡片一致
            picture = {
                "component": "div",
                "props": {"style": {"flex": "0 0 auto", "width": "76px",
                                    "display": "flex", "justifyContent": "center",
                                    "alignItems": "flex-start", "paddingTop": "2px"}},
                "content": [ParkingBot._pic(item.get("image"), name[:1] or "🚗", 68)],
            }
            info = {
                "component": "div",
                "props": {"style": {"flex": "1 1 auto", "minWidth": "0"}},
                "content": [
                    ParkingBot._text(name, {"fontSize": "17px", "fontWeight": "bold"}),
                    ParkingBot._kv("盈利速率", f"{item.get('rate', 0)} 金/分钟", _GREEN),
                    ParkingBot._kv(
                        "累计收益" if not idle else "当前状态",
                        f"🪙 {item.get('gain', 0)}" if not idle else "当前未停放",
                        _GOLD),
                    ParkingBot._kv(
                        "已停时长" if not idle else "说明",
                        f"{minutes} 分钟" if not idle else "可立即停到他人车位",
                        "rgba(255,255,255,0.75)"),
                    {"component": "div",
                     "props": {"style": {"marginTop": "10px"}},
                     "content": [
                         ParkingBot._badge(str(item.get("status", "")), status_color),
                         ParkingBot._command_hint(
                             f"/挪车 {number}", can_unpark,
                             "" if can_unpark else button_text),
                     ]},
                ],
            }
            cards.append({
                "component": "div",
                "props": {"style": {
                    "flex": "1 1 300px",
                    "minWidth": "270px",
                    "margin": "0 8px 8px 0",
                    "padding": "12px 14px 10px",
                    "borderRadius": "14px",
                    "border": "1px solid rgba(255,255,255,0.10)",
                    "backgroundColor": "rgba(255,255,255,0.045)",
                    "display": "flex",
                    "gap": "12px",
                }},
                "content": [picture, info],
            })

        return {
            "component": "div",
            "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                "marginRight": "-8px"}},
            "content": cards,
        }

    @staticmethod
    def _item(title: str, lines: List[Tuple[str, str]], button: Optional[dict],
              accent: str = _CYAN, image: str = "") -> dict:
        """车市里的一条：照游戏车市页——大图居中，下面是名字与参数。"""
        content = []
        if image:
            content.append({
                "component": "div",
                "props": {"style": {"display": "flex", "justifyContent": "center",
                                    "marginBottom": "6px"}},
                "content": [ParkingBot._pic(image, str(title or "🚗")[:1], 84)],
            })
        content.append(ParkingBot._text(title, {"fontSize": "17px", "fontWeight": "bold",
                                                "textAlign": "center", "marginBottom": "2px"}))
        for text, color in lines:
            content.append(ParkingBot._text(
                text, {"fontSize": "13px", "color": color, "marginTop": "3px",
                       "textAlign": "center"}))
        if button:
            button = dict(button)
            button["props"] = dict(button.get("props") or {})
            button["props"]["style"] = {"marginTop": "10px"}
            content.append(button)
        return {
            "component": "div",
            "props": {"style": {
                "flex": "1 1 250px",
                "minWidth": "230px",
                "margin": "0 8px 8px 0",
                "padding": "14px",
                "borderRadius": "14px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "borderLeft": f"3px solid {accent}",
                "backgroundColor": "rgba(255,255,255,0.045)",
            }},
            "content": content,
        }

    @staticmethod
    def _command_hint(command: str, enabled: bool, reason: str = "") -> dict:
        """
        显示一条可复制的指令。

        页面上的业务操作点不动（渲染器只给 events 绑请求，没有前端状态），
        所以这里给聊天指令而不是假按钮。支持按钮回调的通知渠道上，
        发 /车库 会拿到真正可点的按钮。
        """
        color = _CYAN if enabled else "#9E9E9E"
        content = []
        if not enabled and reason:
            content.append(ParkingBot._text(f"⛔ {reason}",
                                             {"fontSize": "11px", "color": _AMBER}))
        content.append(ParkingBot._text(command, {
            "fontSize": "12px",
            "marginTop": "6px",
            "fontFamily": "monospace",
            "color": color,
            "backgroundColor": "rgba(128,128,128,0.14)",
            "borderRadius": "6px",
            "padding": "4px 8px",
            "display": "inline-block",
        }))
        return {"component": "div", "props": {"style": {}}, "content": content}

    def _build_shop(self, snapshot: dict) -> List[dict]:
        """
        构建车市区块：限时特卖 → 常规车市 → 以旧换新。

        所有按钮是否可点在服务端还会再校验一遍，这里只是提前给出直观提示。
        """
        cash = self._to_int(snapshot.get("cash"), 0)
        level = self._to_int(snapshot.get("level"), 1, minimum=1)
        owned_count = self._to_int(snapshot.get("car_total"), 0)
        car_max = self._to_int(snapshot.get("car_max"), 10, minimum=1)
        cars = snapshot.get("cars") or []
        blocks: List[dict] = []

        # ---------- 限时 / 限量特卖 ----------
        flash = snapshot.get("flash") or []
        if flash:
            items = []
            for sale in flash:
                price = self._to_int(sale.get("price"), 0)
                original = self._to_int(sale.get("original"), 0)
                locked = self._to_int(sale.get("level"), 1) > level
                sold_out = (not sale.get("timed")) and self._to_int(sale.get("stock"), 0) > 0 \
                    and self._to_int(sale.get("sold"), 0) >= self._to_int(sale.get("stock"), 0)
                lines = [(f"{price} 金", _GOLD), (f"{sale.get('earn', 0)} 金/分钟", _GREEN)]
                if original > price:
                    lines.insert(0, (f"原价 {original} 金", "#9E9E9E"))
                if locked:
                    lines.append((f"需要 Lv.{sale.get('level')}", _AMBER))
                elif sold_out:
                    lines.append(("已售罄", _AMBER))
                else:
                    lines.append((("限时特卖" if sale.get("timed") else f"仅剩 {self._to_int(sale.get('stock'), 0) - self._to_int(sale.get('sold'), 0)} 辆"), _PURPLE))
                items.append(self._item(
                    str(sale.get("name", "特卖车型")), lines,
                    self._command_hint(
                        f"/抢购 {sale.get('id')}",
                        not locked and not sold_out and price <= cash and owned_count < car_max,
                        "等级不足" if locked else ("已售罄" if sold_out else ("金币不足" if price > cash else "")),
                    ),
                    accent="#FFD700",
                    image=self._asset_url(sale.get("image")),
                ))
            blocks.append(self._card(
                "限时 / 限量特卖", "折扣车辆，按场次限购或限时，过期失效；等级不足或金币不够则指令标灰",
                [{"component": "div",
                  "props": {"style": {"display": "flex", "flexWrap": "wrap", "marginRight": "-8px"}},
                  "content": items}],
            ))

        # ---------- 常规车市 ----------
        catalog = snapshot.get("catalog") or []
        if catalog:
            items = []
            for car in catalog:
                price = self._to_int(car.get("price"), 0)
                need_level = self._to_int(car.get("level"), 1, minimum=1)
                locked = need_level > level
                mine_count = sum(1 for c in cars
                                 if str(c.get("carId")) == str(car.get("id")))
                lines = [
                    (f"{price} 金", _GOLD),
                    (f"{car.get('earn', 0)} 金/分钟", _GREEN),
                ]
                if car.get("category"):
                    lines.append((str(car["category"]), "#9E9E9E"))
                if mine_count:
                    lines.append((f"已拥有 {mine_count} 辆", _CYAN))
                if locked:
                    lines.append((f"需要 Lv.{need_level}", _AMBER))
                hint = ""
                if locked:
                    hint = f"需要 Lv.{need_level}"
                elif price > cash:
                    hint = f"还差 {price - cash} 金"
                elif owned_count >= car_max:
                    hint = f"车辆已满（{car_max} 辆）"
                items.append(self._item(
                    str(car.get("name", "未知车型")), lines,
                    self._command_hint(f"/买车 {car.get('id')}", not hint, hint),
                    image=car.get("image") or "",
                ))
            blocks.append(self._card(
                "车市", f"当前金币 {cash}，车辆 {owned_count}/{car_max} 辆，购买需等级与金币均达标；指令已填好",
                [{"component": "div",
                  "props": {"style": {"display": "flex", "flexWrap": "wrap", "marginRight": "-8px"}},
                  "content": items}],
            ))

        # ---------- 以旧换新 ----------
        price_by_id = {str(c.get("id")): self._to_int(c.get("price"), 0) for c in catalog}
        idle_cars = [c for c in cars if c.get("status") == "空闲"]
        if idle_cars:
            items = []
            for car in idle_cars:
                old_price = price_by_id.get(str(car.get("carId")), 0)
                upgrades = [
                    c for c in catalog
                    if self._to_int(c.get("price"), 0) > old_price
                    and self._to_int(c.get("level"), 1) <= level
                ][:3]  # 最多列 3 个升级目标，手机上不至于刷屏
                buttons = []
                for target in upgrades:
                    diff = self._to_int(target.get("price"), 0) - old_price
                    afford = diff <= cash
                    buttons.append(self._command_hint(
                        f"/换车 {car.get('id')} {target.get('id')}", afford,
                        "" if afford else f"还差 {diff - cash} 金（换成 {target.get('name','')}）",
                    ))
                if not buttons:
                    buttons = [ParkingBot._text(
                        "当前等级/金币下暂无可换的更贵车型",
                        {"fontSize": "11px", "opacity": "0.6", "marginTop": "6px"})]
                items.append(self._item(
                    str(car.get("name", "车辆")),
                    [(f"当前价值 {old_price} 金", _GOLD),
                     (f"{car.get('rate', 0)} 金/分钟", _GREEN)],
                    {"component": "div", "props": {"style": {}}, "content": buttons},
                    accent="#00E676",
                    image=car.get("image") or "",
                ))
            blocks.append(self._card(
                "以旧换新", "只有空闲车辆能作为旧车；只能换更贵的车型，按补价从低到高列出前 3 个目标（指令已填好）",
                [{"component": "div",
                  "props": {"style": {"display": "flex", "flexWrap": "wrap", "marginRight": "-8px"}},
                  "content": items}],
            ))
        # 车型表拿不到（game_data.php 失败）且名下没有空闲车时，上面三段都不会产出。
        # 仍然要给出这张卡：顶部「② 车市」的锚点指向这里，缺了锚点就点不动了。
        if not blocks:
            blocks.append(self._card(
                "② 车市",
                "车型表与特卖列表暂时拿不到，下一轮执行成功后会自动补上",
                [self._text("车市数据未加载",
                            {"fontSize": "13px", "opacity": "0.8"})],
            ))
        return blocks

    def _nav_row(self, current: str) -> dict:
        """
        顶部区块切换栏。

        MoviePilot 的页面渲染器（PageRender）会把节点上的 events 展开成
        on<事件名> 绑到组件上：点击 → POST 指定 api → 渲染器随即重新拉取
        plugin/page/{id}。所以切换是真正的「只显示当前区块」，靠服务端记住
        当前页签实现的，不是前端动画，也不需要任何 JS。

        这里刻意不用 <a href="#..."> 锚点：插件页是 hash 路由（#/plugins），
        点锚点会把 hash 改成 #pb-xxx，路由匹配失败后整个插件弹窗被卸载，
        表现为「点一下页面就没了」。
        """
        api = f"plugin/{getattr(self, 'plugin_id', None) or self._PLUGIN_ID}/tab"
        buttons = []
        for key, label in self._TABS:
            active = (key == current)
            buttons.append({
                "component": "div",
                "props": {
                    "style": {
                        "display": "flex",
                        "alignItems": "center",
                        "justifyContent": "center",
                        "gap": "6px",
                        "flex": "1 1 0",
                        "minWidth": "96px",
                        "padding": "9px 10px",
                        "borderRadius": "10px",
                        "fontSize": "13px",
                        "cursor": "pointer",
                        "userSelect": "none",
                        "whiteSpace": "nowrap",
                        # 当前页签是实心青底，未选中的是半透明描边 —— 和游戏顶部那排一致
                        "color": "#04222B" if active else "rgba(255,255,255,0.82)",
                        "backgroundColor": (
                            f"linear-gradient(135deg, {_CYAN}, #38BDF8)"
                            if active else "rgba(255,255,255,0.07)"),
                        "border": ("none" if active
                                   else "1px solid rgba(255,255,255,0.14)"),
                        "boxShadow": ("0 4px 14px rgba(34,211,238,0.28)"
                                      if active else "none"),
                        "transition": "background-color .15s ease, color .15s ease",
                    },
                },
                "content": [
                    # 图标是 currentColor 描边，跟着外层 color 走：选中时深色压青底，
                    # 未选中时浅色，跟文字一个颜色
                    {"component": "span",
                     "props": {"style": {"display": "flex"}},
                     "html": _ICONS[key].replace("@SIZE@", "16px")},
                    self._text(label, {"fontWeight": "bold" if active else "normal"}),
                ],
                "events": {"click": {"api": api, "method": "post", "params": {"tab": key}}},
            })
        return {
            "component": "div",
            "props": {"style": {"display": "flex", "gap": "8px", "marginBottom": "14px",
                                "flexWrap": "wrap"}},
            "content": buttons,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页：顶部切换栏 + 当前选中的那一个区块。"""
        snapshot, history, daily = self._page_data()
        today_earned = self._today_earned(history)
        min_duration = self._to_int(snapshot.get("min_duration"), 10, minimum=1)
        note = str(snapshot.get("note") or "")
        tab = self._tab if self._tab in dict(self._TABS) else "spots"

        # ---------- 概览 ----------
        if snapshot:
            stats = [
                self._stat("今日收益", today_earned, _GREEN),
                self._stat("金币", snapshot.get("cash", 0), _GOLD),
                self._stat("待结算", snapshot.get("pending_gain", 0), _CYAN),
                self._stat("距下次运行", self._next_run_text(), _PURPLE),
            ]
            header = f"{snapshot.get('name', '未命名')} · Lv.{snapshot.get('level', 1)}"
            subtitle = (f"执行周期 {self._cron or '未启用'}"
                        f" · 车辆 {snapshot.get('car_total', 0)}/{snapshot.get('car_max', 10)} 辆"
                        f" · 数据时间 {snapshot.get('time', '-')}")
        else:
            stats = [
                self._stat("今日收益", today_earned, _GREEN),
                self._stat("金币", "-", "#9E9E9E"),
                self._stat("待结算", "-", "#9E9E9E"),
                self._stat("距下次运行", self._next_run_text(), _PURPLE),
            ]
            header = self.plugin_name
            subtitle = "勾选「立即执行一次」后这里会显示停车场实况"

        last_run = self._last_run
        if self._last_run_ts:
            last_run = f"{last_run}（{datetime.fromtimestamp(self._last_run_ts):%Y-%m-%d %H:%M:%S}）"

        head_lines = [
            self._text(header, {"fontSize": "19px", "fontWeight": "bold",
                                "letterSpacing": "0.3px"}),
            self._text(subtitle, {"fontSize": "12px", "opacity": "0.62",
                                  "marginTop": "4px", "lineHeight": "1.6"}),
            self._text(f"最近一轮　{last_run}",
                       {"fontSize": "12px", "opacity": "0.78", "marginTop": "8px"}),
            self._text(f"本次进程累计挪车收益　{self._total_earned} 金币",
                       {"fontSize": "12px", "opacity": "0.78"}),
        ]
        if note:
            head_lines.append(self._text(
                f"自动操作　{note}",
                {"fontSize": "12px", "color": _GOLD, "marginTop": "6px",
                 "lineHeight": "1.6"}))

        # 开关开着但这一轮没花钱（比如没找到划算的车型）时也给个提示，
        # 否则用户会以为开关没生效。
        shop_cards = self._build_shop(snapshot)
        turned_on = [label for flag, label in ((self._auto_buy, "自动买车"),
                                                (self._auto_trade, "自动以旧换新")) if flag]
        if turned_on and not note:
            head_lines.append(self._text(
                f"{'、'.join(turned_on)}已开启，本轮没有满足条件的操作",
                {"fontSize": "12px", "opacity": "0.78", "marginTop": "6px"}))

        head_card = {
            "component": "div",
            "props": {"style": {
                "marginBottom": "12px",
                "padding": "14px",
                "borderRadius": "14px",
                "border": "1px solid rgba(128,128,128,0.20)",
                "backgroundColor": "rgba(255,255,255,0.05)",
            }},
            "content": head_lines,
        }

        howto_card = self._card(
            "怎么操作",
            "页面上的按钮只用于切换区块，实际操作请在聊天里发指令。",
            [
                self._text("/车库　　　查看车库与可操作项",
                           {"fontSize": "13px", "fontFamily": "monospace"}),
                self._text("/挪车 序号　立即结算并收取收益",
                           {"fontSize": "13px", "fontFamily": "monospace"}),
                self._text("/买车 车型ID　/换车 旧车ID 新车型ID　/抢购 场次ID",
                           {"fontSize": "13px", "fontFamily": "monospace"}),
                self._text("Telegram / Slack 上发 /车库 会直接收到可点的按钮。",
                           {"fontSize": "11px", "opacity": "0.6", "marginTop": "8px"}),
            ])

        garage_card = self._card(
            "我的车库",
            f"停车不足 {min_duration} 分钟的车不能挪走，指令会标灰",
            [self._garage(snapshot.get("cars") or [], min_duration)],
        )

        spots_card = self._card(
            "我的车位",
            "私家车位被他人停用超过 5 分钟后对方才能贴条，收益按 70% / 30% 分成",
            [self._spot_grid(snapshot.get("spots") or [])],
        )

        history_card = self._card(
            "执行历史",
            "最近 10 轮定时或手动执行的记录（最多保留 50 轮）",
            [self._table(["时间", "新停放", "挪走", "收益(金币)"],
                         [[i.get("time", ""), str(i.get("parked", 0)),
                           str(i.get("moved", 0)), str(i.get("earned", 0))] for i in history[:10]],
                         ["2", "1", "1", "1"],
                         ["left", "center", "center", "right"])],
        )

        stats_cards = [self._stats_page(daily, history, today_earned)]

        # 只渲染当前页签对应的那一个区块 —— 顶部按钮切换后由服务端重新下发，
        # 所以页面上永远只有一块内容，不会再出现三个区块堆在一起。
        # 执行历史已挪到「金币统计」页，车库页不再夹带。
        sections = {
            "spots": [spots_card, howto_card],
            "shop": shop_cards,
            "garage": [garage_card],
            "stats": stats_cards,
        }

        return [
            head_card,
            # 四个指标块：手机上一行两个、宽屏一行四个。用单个 flex 容器而不是
            # VRow/VCol，少 8 层组件，宽度也能自己说了算。
            {"component": "div",
             "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                 "marginRight": "-8px", "marginBottom": "4px"}},
             "content": stats},
            self._nav_row(tab),
            *sections[tab],
        ]

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        """在主面板登记一块状态卡。"""
        return [{"key": "status", "name": "不可躺抢车位"}]

    def get_dashboard(self, key: str, **kwargs: Any):
        """返回主面板状态卡：账户概览 + 本进程累计收益。"""
        if key != "status":
            return None
        snapshot, _, _ = self._page_data()
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
