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
import time
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


class CarMoveRequest(BaseModel):
    """挪车按钮的请求体。参数既可以是车库序号，也可以是车辆实例 ID。"""

    car_id: Any = Field(description="要挪走的车辆：车库序号或车辆实例 ID")


class RefreshRequest(BaseModel):
    """「立即刷新」按钮的请求体。目前没有参数，留一个空模型占位。"""

    reason: str = Field(default="manual", description="刷新来源，仅用于日志")


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


class ParkRequest(BaseModel):
    """车库「立即停到他人车位」按钮的请求体。"""

    car_id: Any = Field(description="要停出去的车辆：车库序号或车辆实例 ID")


class TicketRequest(BaseModel):
    """车位卡片「贴条」按钮的请求体。"""

    spot_id: Any = Field(description="要贴条的车位 ID（车位主人的车位）")


class TitleSetRequest(BaseModel):
    """切换佩戴的称号。0 表示取消佩戴（游戏里是同一个接口、同一个 0）。"""

    title_id: Any = Field(default=0, description="称号 ID；0 表示取消佩戴")


class TitlePickRequest(BaseModel):
    """展开 / 收起称号选择器。"""

    open: bool = Field(default=False, description="True 展开选择器，False 收起")


class CharitySpotRequest(BaseModel):
    """车位「设为爱心 / 恢复私家」的请求体。"""

    spot_id: Any = Field(description="车位实例 ID")
    action: str = Field(default="", description="set 设为爱心车位；unset 恢复私家车位")


class ExpDetailRequest(BaseModel):
    """
    展开 / 收起经验值详情面板。面板是纯展示，没有别的入参。

    open 必须留这个开关：_exp_open 是记在服务端的视图状态，点开后会一直挂着，
    没有收起入口的话用户就只能重启插件才能让它消失。称号选择器是同一个道理。
    """

    open: bool = Field(default=False, description="True 展开面板，False 收起")


class NotifDetailRequest(BaseModel):
    """
    展开 / 收起通知面板，以及「全部已读」。

    read_all 是**唯一一个**会写游戏侧通知的操作：调 notifications.php 的
    action=read_all，只改阅读状态、不删任何东西，游戏自己的弹窗里也一样
    不带二次确认。

    清空（action=clear）插件**故意不做**：它会真删通知且不可恢复，而插件页
    弹不出 confirm 二次确认框，误按一下没法挽回。要清回游戏网页上点。
    """

    open: bool = Field(default=False, description="True 展开面板，False 收起")
    read_all: bool = Field(default=False, description="True 把所有通知标记为已读")


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
# 统计页「其中贴条」专用色：玫红偏橙，贴合处罚语义，
# 也和金色（挪车收益）、粉色（支出）、绿色（净收）都拉得开
_TICKET_C = "#FF7A9C"


# 金币图标，照抄游戏 index.html 雪碧图里的 <symbol id="ico-coin">，去掉了 use 引用。
# 为什么不用 🪙 emoji：U+1FA99 是 2019 年才进 Unicode 的，Windows 默认字体里
# 基本没有这个字形，插件页实测直接渲染成豆腐块 □。
_COIN_SVG = ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@">'
             '<circle cx="12" cy="12" r="10" fill="#ffd700" stroke="#daa520" '
             'stroke-width="1.5"/><text x="12" y="16.5" text-anchor="middle" '
             'font-size="13" font-weight="bold" fill="#8B6914">$</text></svg>')
# 奖牌图标，照抄游戏雪碧图里的 <symbol id="ico-medal">。
_MEDAL_SVG = ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@">'
              '<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10'
              'S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z'
              'm-1-13h2v6h-2zm0 8h2v2h-2z" fill="#ffd700"/>'
              '<circle cx="12" cy="12" r="3" fill="#ffd700"/></svg>')
# 贴条图标，照抄游戏雪碧图里的 <symbol id="ico-ticket">。
_TICKET_SVG = ('<svg viewBox="0 0 24 24" width="@SIZE@" height="@SIZE@">'
               '<path d="M22 10V6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v4c1.1 0 2 .9 2 2s-.9 2-2 2v4'
               'c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2v-4c-1.1 0-2-.9-2-2s.9-2 2-2z" '
               'fill="#e94560"/><path d="M9 7h2v10H9zm4 0h2v10h-2z" '
               'fill="rgba(0,0,0,0.3)"/></svg>')
# 游戏站点的静态资源根目录。游戏把头像/车图存成相对路径（如 assets/avatars/a.png），
# 插件页在 MoviePilot 里渲染，相对路径会指到 MoviePilot 自己身上，所以要补全成绝对地址。
_GAME_BASE = "https://qcw.tangpt.top/"
# 空闲车位在游戏里是斜纹网格底，这里用纯 CSS 斜纹还原，不额外拉图。
# 走 backgroundImage 而不是 backgroundColor：插件页的渲染器只把 backgroundColor
# 交给 CSS 的 background-color，渐变值会被丢掉，所以底色另外用实色兜住。
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
    # 市场页图标有两条完全不同的取法（见 MoviePilot-Frontend PluginCard.vue）：
    #   1) 值以 http 开头 → 走后端图片代理 /api/v1/system/img/{proxy}?imgurl=…
    #      由 MoviePilot 后端去拉这个 URL
    #   2) 否则当文件名 → 拼成 ./plugin_icon/<值>，由 nginx 直接读**本机**静态目录
    # 仓库里的 icons/ 只对「方式 2 且该文件已被放进 MoviePilot 主机」的情形有效，
    # 单纯把文件传到 GitHub 并不会让它出现在方式 2 的目录里。
    # 这里用方式 1，图标跟着仓库走，不用往 MoviePilot 主机上放文件。
    plugin_icon = ("https://raw.githubusercontent.com/laoying6663/"
                   "MoviePilot-Plugins/main/icons/Parking_A.png")
    plugin_version = "1.51.0"
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
    _TABS = (("spots", "我的车位"), ("shop", "车市"),
             ("garage", "我的车库"), ("stats", "数据统计"))
    _tab = "spots"
    _day = ""
    # 车市页的两个临时状态：选中的车型分类、正在「选旧车换新」的目标车型。
    # 必须给类级默认值 —— 详情页渲染可能在 __init_plugin_config__ 之前被调用。
    _cat = ""
    _trade_for = ""
    # 称号选择器是否展开。插件页没有弹窗，展开状态同样记在服务端，
    # 切页签回来还在，和 _cat / _trade_for 一个套路。
    _title_open = False
    # 经验值详情面板是否展开。和 _title_open 一个套路：插件页弹不出模态框，
    # 只能翻转一个服务端状态，让重渲染时带上展开的面板。
    _exp_open = False
    # 通知面板是否展开，同样是服务端视图状态。
    # 默认不展开：通知列表要在点开的当刻现拉，留着上次的旧通知没有意义。
    _notif_open = False
    # ---- view 状态的落盘（v1.47.0）----
    #
    # 实测（诊断版 v1.46.1）：每次点页签/展开面板，插件自己的处理器里
    # 99% 的时间都耗在 async_save_data("view", …) 这一步 ——
    # 读只要 1~4 毫秒，写却要 31 / 175 / 763 / 4813 毫秒，
    # 浏览器那边看到的就是 POST 692~2847 毫秒。写为什么这么慢至今没查清，
    # 但**让用户点击不等它**就行。
    #
    # 所以 view 改成「内存是真相源 + 防抖后台落盘」：
    #   - 点一下立刻返回，等待从几百毫秒降到 0
    #   - 连续点击合并成一次写（每 0.8 秒最多落一次盘）
    # 代价只有一个：MoviePilot 在落盘前被重启的话，下次打开回到默认页签。
    # 那只是 UI 偏好，不是数据，值这个价。
    _VIEW_DEBOUNCE = 0.8
    # 这三个**故意声明成 None 而不是 {}**：可变的类属性是所有实例共享的，
    # 而详情页渲染可能在 init_plugin 之前就调进来（类属性注释里已经写过
    # 这个坑）。用 None 占位 + 首次使用时惰性建实例属性，才不会两个插件实例
    # 互相串味。
    _view_mem: Optional[Dict[str, Any]] = None
    _view_dirty: Optional[Dict[str, Any]] = None
    _view_flush_task: Any = None
    # 称号的进程内短缓存。游戏接口单次往返实测约 1.1 秒，而称号只在解锁或
    # 切换时变：10 分钟 TTL 跨两个执行周期，稳定状态下每两轮才发一次请求。
    # 特卖是限时场次，不缓存 —— 缓存后页面上会一直挂着已结束的场次。
    _TITLES_TTL = 600.0
    _titles_cache: List[dict] = []
    # 特卖 + 称号的「在途」预取任务。挪车/停车本身不影响这两样数据，
    # 没必要等动作做完再串行去拉：取到 state 之后就启动，让它和
    # unpark.php / park.php 并行跑，_record_round 直接取结果，省一个往返。
    _market_prefetch: Any = None
    _titles_at = 0.0
    # 本轮自动买卖的花费。由 _auto_shop 累加、_record_round 并入按天统计后清零。
    # 只在内存里跨这一轮传递，不单独落盘。
    _shop_spent = 0
    _shop_bought = 0
    _shop_diff = 0
    _shop_traded = 0
    # 贴条罚款收入。同样并进「当日收益」，否则这笔真金白银在统计页上凭空消失。
    _shop_fined = 0

    # ---------- 运行态（仅内存，用于详情页展示） ----------
    _last_run = "尚未运行"
    _last_run_ts = 0
    _total_earned = 0
    _auth_warned = False
    # 最近一次 SSO 失败的原因。SSO 失败时游戏 Cookie 取不到，外层若只报
    # 「缺少登录 Cookie」会把排查方向带偏，这里把真实原因带出去。
    _sso_error = ""
    _cars_cache: Optional[Dict[int, dict]] = None
    # 最近一次 game_data.php 的**完整**响应。它和车型表是同一次请求拿回来的，
    # 里面的 levelTable（等级门槛）和 expConfig（经验来源每日上限）是经验值面板
    # 的唯一数据源 —— state.php 根本不返回这两个字段。
    # 一并存着，经验值面板就不需要再发任何请求。
    _game_data: Optional[Dict[str, Any]] = None
    # 车型表缓存的取用时刻与有效期。游戏会不定期上新车，没有失效时间的话
    # 进程不重启就永远看不到新车（2026-10-14 一次就从 4 个分类加到 9 个）。
    _cars_cache_at: float = 0.0
    _CARS_TTL = 3600.0
    # 批量贴条时每张之间的错峰间隔（秒）。照抄游戏自己的节奏：它的
    # showAllTicket 就是 setTimeout(index * 200) + Promise.all，
    # 注释写明「并行执行，但每个之间间隔 200ms 避免过于密集」。
    # 单请求约 1.1 秒，所以 5 张从串行的 5.5 秒降到约 1.9 秒。
    _TICKET_STAGGER = 0.2
    # 同一个车位主人名下最多停我几辆车。
    # 场景加成（game_data.parkingBackgrounds 的 bonusPercent，最高的沙滩/牛来是 5%）
    # 会让停车目标天然往少数几个买了场景的玩家身上挤。纯按加成排序的话，
    # 名下 10 辆车可能全部停在同一个人那儿 —— 对方一眼就能看到「他所有车都在我这儿」，
    # 批量驱离和举报都不用挑目标。这条上限就是为挡住这个：吃加成，但别把
    # 所有车押在一个人身上。剩下的名额自然分给下一个高加成的人。
    _PARK_MAX_PER_OWNER = 2
    # 默认值必须写在类上：run_once 里会直接读 self._auto_ticket，
    # 而插件实例并非任何时刻都保证刚跑过 init_plugin（热重载中间态、
    # 派生实例等），缺属性会 AttributeError 让整轮作废。默认关，最保守。
    _auto_ticket: bool = False
    _ticket_min_fine: int = 0
    # 优先停爱心车位赚经验。默认关：爱心车位**不产金币**，开着会让收益方式
    # 从「赚金币」变成「赚经验」，是会影响收入结构的改动，得用户自己决定。
    _prefer_charity: bool = False
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
        # 自动贴条默认关闭：它会真实产生罚款收入，也可能招致对方报复，
        # 不该插件替用户默认开启。
        self._auto_ticket = self._as_switch(config.get("auto_ticket"))
        self._ticket_min_fine = self._to_int(config.get("ticket_min_fine"),
                                             0, minimum=0)
        # 优先停爱心车位赚经验，默认关（理由见类属性的注释）。
        self._prefer_charity = self._as_switch(config.get("prefer_charity"))
        # 记住上次停留的区块与查看的日期；插件重载、MoviePilot 重启后仍回到原来那一页
        saved_view = self.get_data("view") or {}
        if not isinstance(saved_view, dict):
            saved_view = {}
        saved_tab = str(saved_view.get("tab") or "").strip()
        self._tab = saved_tab if saved_tab in dict(self._TABS) else "spots"
        # 车市页的两个临时状态：选中的车型分类、以及正在「选旧车换新」的目标车型。
        # 分类直接取快照里真实存在的分类名，不认任何外部输入。
        self._cat = str(saved_view.get("cat") or "").strip()
        self._trade_for = str(saved_view.get("trade_for") or "").strip()
        self._title_open = bool(saved_view.get("title_open"))
        # 经验值 / 通知两个面板**故意不从存档读回**（v1.48.0）：
        # 用户要求每次打开插件页面默认都是收着的。这两个是「看一眼就走」的
        # 临时面板，记住它们没意义；顺带还省掉两次落盘 ——
        # 点铃铛/等级格时连 _save_view 都不调，一次数据库写都不发生。
        self._exp_open = False
        self._notif_open = False
        saved_day = str(saved_view.get("day") or "").strip()
        self._day = saved_day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", saved_day or "") else ""
        # view 的内存真相源：后台落盘时以它为准，这样只改局部字段也不会
        # 把车市选好的分类、待换新的目标车型冲掉（原来靠的是读改写）。
        self._view_mem = dict(saved_view)
        self._view_dirty = {}
        self._view_flush_task = None
        self._auth_warned = False
        # 插件重载时清空车型表缓存：手动保存配置就等于用户想"按最新数据来一遍"
        self._cars_cache = {}
        self._cars_cache_at = 0.0
        self._game_data = None

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
        # `or {}` 只挡得住 None / 空容器，挡不住字符串、列表这类真值 ——
        # 那种情况下紧接着的 .get() 会抛 AttributeError，而 run_once 没有兜底 try，
        # 整轮挪车/停车/买卖直接作废，用户界面上还什么提示都收不到（连
        # _set_result 都执行不到）。和下面 userCars 的 isinstance 过滤同一个道理。
        settings_map = self._settings_of(state)
        # 游戏允许的最短停车时长，低于它的挪车请求会被服务端拒绝
        min_duration = self._to_int(settings_map.get("MIN_PARK_DURATION"), 10, minimum=1)
        target_minutes = max(self._park_minutes, min_duration)

        # 用户 ID 一律按字符串比较：state.userId 与 userCars[].userId 一旦一边是
        # 数字一边是字符串，严格 == 会认定「名下没有车辆」，而聊天指令路径
        # （_ordered_my_cars）用的是 str() 归一，两套口径会对不上。
        # 非字典项一律滤掉：返回里混进字符串/数字时，下面 c.get() 直接抛
        # AttributeError，整轮挪车、停车、买卖全部作废且没有任何提示。
        my_cars = [c for c in (state.get("userCars") or [])
                   if isinstance(c, dict) and str(c.get("userId")) == str(user_id)]
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
        # recomputed / state_is_current 必须在第一次被读之前就初始化。
        # 曾经把 recomputed 的初始化写在下面「还能再停几辆」那段的开头，结果它跑在
        # _refresh_state() 之后，把 _recompute 刚置的 True 又抹回 False：
        # still_parked 于是把同一批挪走的车扣了两遍，「最多同时停放 N 辆」被突破到
        # N+moved（实测上限 2 变成能停 4 辆）。只��「自动换新」开着才会踩到 ——
        # 因为走那条路才会先刷新一次状态。
        recomputed = False
        # state 是否已经是动作之后的最新状态。跑一轮里 state 会先后被买卖/挪车/停车
        # 弄脏多次，只有刷新过才算当前；末尾落快照时据此决定要不要再拉一次。
        state_is_current = True
        cars_map = await self._load_cars_map(client)

        def _recompute(state: dict) -> None:
            """按最新状态重算本轮要动的三类车辆与挪车时长门槛。"""
            nonlocal parked, idle, charity, min_duration, target_minutes, recomputed
            settings_map = self._settings_of(state)
            min_duration = self._to_int(settings_map.get("MIN_PARK_DURATION"),
                                        10, minimum=1)
            target_minutes = max(self._park_minutes, min_duration)
            mine = [c for c in (state.get("userCars") or [])
                    if isinstance(c, dict) and str(c.get("userId")) == str(user_id)]
            parked = [c for c in mine if c.get("status") not in ("idle", "charity")]
            idle = [c for c in mine if c.get("status") == "idle"]
            charity = [c for c in mine if c.get("status") == "charity"]
            recomputed = True

        async def _refresh_state() -> bool:
            """
            重新拉一次游戏状态。买卖改动了服务端，拿旧快照继续操作会用到废车 ID。

            任何异常都吞掉并当作「刷新失败」返回 False —— 刷新失败时状态是陈旧的，
            绝不能继续往下走：换车已经把旧车换掉了，拿废 ID 去 park 是白白发请求。
            """
            nonlocal state, state_is_current
            try:
                fresh = await client.get_json(f"{self.API_BASE}/state.php")
            except Exception as err:
                logger.warning(f"[不可躺自动抢车位] 刷新游戏状态异常：{err}")
                return False
            if fresh and fresh.get("success"):
                state = fresh
                _recompute(state)
                state_is_current = True
                return True
            return False

        if self._auto_buy:
            try:
                note = await self._auto_buy_one(client, state, cars_map, user_id)
                if note:
                    shop_note = note
                    state_is_current = False
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
        if moved:
            # 服务端已变，这份 state 里的车还标着「停车中」
            state_is_current = False

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
                        state_is_current = False
                        if not await _refresh_state():
                            stale_after_shop = True
                            logger.warning(
                                "[不可躺自动抢车位] 自动换新后刷新状态失败，本轮跳过停车，"
                                "避免用过期的车辆 ID 继续操作")
            except Exception as err:
                logger.error(f"[不可躺自动抢车位] 自动换新异常：{err}")

        # 还能再停几辆。
        #
        # 买卖那几步会重新拉状态并重算 parked，被挪走的车此时已经是 idle，
        # 自然不在 parked 里了 —— 这时再减一次 moved 就把同一批车扣了两遍，
        # 「最多同时停放 N 辆」会被突破到 N+moved。只有没重算过（没做买卖）
        # 时 parked 才还是挪车前的旧快照，才需要减掉 moved。
        if stale_after_shop:
            self._set_result(shop_note or "状态刷新失败，本轮未停车")
            await self._record_round(client, state, user_id, 0, moved, earned_total,
                                     note=shop_note)
            return

        still_parked = len(parked) - (0 if recomputed else moved)

        # 优先停爱心车位：经验满了就把车挪回来，别让它们一直占着车位不产金币。
        # 挪车接口对爱心车位返回的是经验不是金币，所以不能计入 earned_total。
        charity_moved = 0
        if self._prefer_charity and charity:
            _got, _cap = self._charity_exp(state, user_id)
            if _got >= _cap:
                charity, charity_moved = await self._unpark_charity_cars(
                    client, charity, target_minutes, cars_map)
                if charity_moved:
                    still_parked -= charity_moved

        # 自动贴条：排在挪车之后、停车之前。
        # 挪车之后 state 已刷新过，谁停在我的车位上是最准的；停车只会把我的车
        # 停到别人车位上，与贴条无关，放哪都不影响结果。
        ticket_note = ""
        if self._auto_ticket:
            try:
                ticket_note = await self._auto_ticket_one(client, state, cars_map,
                                                          user_id)
                if ticket_note:
                    shop_note = f"{shop_note}；{ticket_note}" if shop_note else ticket_note
                    # 贴条之后服务端车位状态变了，落快照得用新状态
                    state_is_current = False
            except Exception as err:
                logger.error(f"[不可躺自动抢车位] 自动贴条异常：{err}")

        new_parked = await self._park_idle_cars(
            client, idle, self._dict_rows(state.get("parkingSpots")), user_id,
            max(0, self._max_cars - still_parked),
            state,
        )
        if new_parked:
            # 刚停出去的车在服务端已占位，这份 state 里还显示着「空闲」
            state_is_current = False
        if charity_moved:
            if shop_note:
                shop_note += f"；爱心停车满额挪回 {charity_moved} 辆"
            else:
                shop_note = f"爱心停车满额，挪回 {charity_moved} 辆改赚金币"
        self._summarize(new_parked, moved, earned_total)

        # 本轮动过车就重新取一次状态：上面那份 state 是动作之前的快照，
        # 直接拿去落快照会让页面把刚停的车显示成「空闲」、按钮置灰，
        # 要等下一轮才对上。
        # 但只要中途某次 _refresh_state() 成功过，state 就已经是最新的，
        # 再拉一次纯属浪费 —— 游戏站单请求约 1.1 秒，一轮能省一次是一���。
        # 所以判据是「这份 state 是不是已经反映了全部动作」，而不是「有没有动过车」。
        snapshot_state = state
        if not state_is_current:
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
        rules = self._settings_of(state)
        mine = self._user_of(state, user_id)
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

    def _disable_auto_buy_if_full(self, owned: int, max_cars: int) -> str:
        """
        车队满员时，把「自动买车」这个开关自己关掉。

        满员之后游戏必然拒绝买车，每轮再撞一次只是白费一次请求；更别扭的是
        设置里那个开关还亮着，看着像还在买，实际一辆都进不来。关掉是对现状的
        如实反映，比留一个永远失败的开关强。

        只关不自动打开：车位数是靠卖车、换新、被动事件变化的，什么时候重新开
        交给用户自己判断 —— 插件擅自打开一个会真花钱的开关，风险比关掉大得多。

        返回一句摘要供详情页展示；已经是关的则返回空串（每轮都会被问到，
        但只有第一次会产生实际动作和通知）。
        """
        if not self._auto_buy:
            return ""
        # 基类 update_config 走的是 save_config_data(config_data=config)，
        # 属于**整体覆盖**而不是合并。所以必须先取回完整配置再改这一个字段，
        # 否则 enabled / cron / max_cars 等其它项会被一起抹掉。
        config = self.get_config() or {}
        if not isinstance(config, dict):
            config = {}
        config["auto_buy"] = False
        try:
            self.update_config(config)
        except Exception as err:
            # 写配置失败也不能影响本轮停车的正常流程
            logger.error(f"[不可躺自动抢车位] 关闭自动买车失败：{err}")
            return ""
        self._auto_buy = False
        note = f"车辆已满（{owned}/{max_cars}），已自动关闭「自动买车」"
        logger.info(f"[不可躺自动抢车位] {note}")
        if self._notify:
            try:
                # post_message 的首个位置参数是 channel，正文必须走 text 关键字
                self.post_message(title=self.plugin_name, text=note)
            except Exception as err:
                logger.error(f"[不可躺自动抢车位] 发送通知失败：{err}")
        return note

    async def _auto_ticket_one(self, client: Any, state: dict, cars_map: Dict[Any, dict],
                               user_id: Any) -> str:
        """
        自动贴条：给我的私家车位上停够时长、且罚款够阈值的别人的车贴条。

        和手动贴条共用同一套游戏规则校验（这里再查一遍是因为自动流程不经过页面，
        页面上那个置灰按钮的保护在服务端等于不存在）。判定口径与
        _spot_snapshot 算出的「贴条收益」保持一致，否则用户会看到页面上写着
        「贴条收益 675」但阈值设 700 也不贴。

        判定只有两条：**停够 MIN_TICKET_TIME** 且 **预估罚款达到门槛**，就贴。
        原先还有一条「同一辆车在同一个车位只贴一次」（_ticket_seen），v1.48.0 去掉了 ——
        那条规则游戏自己根本没有：它的批量贴条筛选条件只有「是我的车位 + 有车 +
        非爱心」，没有任何去重；而且贴条成功后游戏会**把车从车位上赶走**
        （前端 `parkingSpots[i].carInSpot = null`）。也就是说车都被赶走了，
        不存在「同一辆车赖着不走被反复贴」的场景，那条去重规则只会白白挡住
        本该贴的车 —— 之前就有一辆停了 84 分钟、贴条收益 1411 的车因为被记过一次
        而再也不贴。规则跟着游戏对齐即可。
        """
        if not self._auto_ticket:
            return ""

        rules = self._settings_of(state)
        min_ticket = self._to_int(rules.get("MIN_TICKET_TIME"), 5, minimum=0)
        percent = self._to_int(rules.get("TICKET_OWNER_PERCENT"), 90,
                                minimum=0, maximum=100)
        cap = self._to_int(rules.get("MAX_EARN_PER_SPOT"), 3000, minimum=0)
        threshold = self._ticket_min_fine

        my_spots = [s for s in self._dict_rows(state.get("parkingSpots"))
                    if str(s.get("ownerId")) == str(user_id)]

        candidates = []
        for spot in my_spots:
            spot_key = str(spot.get("id"))
            # 爱心车位按游戏规则不能贴条
            if spot.get("spotType") == "charity":
                continue
            car_in_spot = spot.get("carInSpot")
            if not car_in_spot:
                continue
            # 这里原来还有一条「同一辆车在这个车位已经贴过了就不再来一次」，
            # v1.48.0 去掉了 ——  game's ticketCar 贴完就把车赶下车位，
            # 不存在「同一辆车赖着不走被反复贴」的场景，那条规则只会误伤。
            car = next((c for c in self._dict_rows(state.get("userCars"))
                        if str(c.get("id")) == str(car_in_spot)), None)
            if car is None:
                continue
            # 自己停自己车位上的车不贴
            if str(car.get("userId")) == str(user_id):
                continue
            parked_at = self._parked_at(spot)
            mins = max(0, int((self._now_naive() - parked_at).total_seconds() / 60)) \
                if parked_at else 0
            if mins < min_ticket:
                continue
            definition = self._lookup_car(cars_map, car.get("carId")) or {}
            gain = min(mins * self._to_int(definition.get("earnPerMin"), 0), cap)
            fine = gain * percent // 100
            if fine < threshold:
                continue
            candidates.append((fine, spot_key, spot, car))

        if not candidates:
            return ""

        # 先罚收益高的，同样的轮次里多拿一点
        candidates.sort(key=lambda item: -item[0])
        notes = []
        total = 0
        # 并发发出去，但**每个间隔 _TICKET_STAGGER 秒**，和游戏自己的
        # showAllTicket 完全一致（那边是 setTimeout(index * 200) + Promise.all，
        # 注释原话「并行执行，但每个之间间隔 200ms 避免过于密集」）。
        #
        # 为什么贴条可以并发：它只给各车位加一条**独立**的罚款记录，互不相干，
        # 游戏自己就在并发做，服务端不拦。错开而不是一齐打，是为了不「过于密集」。
        #
        # 为什么挪车/停车**不**这么做：那两步改的是**金币余额**和车辆/车位占用，
        # 属于共享状态。万一游戏把用户数据放在 PHP session 里，两个并发请求读到
        # 同一份快照、后写的覆盖先写的，就是丢更新 = 丢金币。游戏自己写「一键挪车」
        # 用的也是串行（for 里 await），所以那两步保持串行，别自作聪明。
        async def _tick(index: int) -> Optional[dict]:
            await asyncio.sleep(index * self._TICKET_STAGGER)
            return await self._post(
                client, "ticket.php", {"spotId": candidates[index][2].get("id")})

        # return_exceptions=True：一张贴失败不能让整批抛出去，
        # 那会让后面所有车位的罚款都记不上账。
        results = await asyncio.gather(*[_tick(i) for i in range(len(candidates))],
                                       return_exceptions=True)

        for index, result in enumerate(results):
            fine, spot_key, spot, car = candidates[index]
            if isinstance(result, BaseException):
                logger.warning(
                    "[不可躺自动抢车位] 自动贴条异常，车位 %s：%s", spot_key, result)
                continue
            if not (result and result.get("success")):
                logger.warning(
                    "[不可躺自动抢车位] 自动贴条失败，车位 %s：%s",
                    spot_key, (result or {}).get("error") or "未知原因")
                # 这一轮不再重试这个车位，但车还在上面，下一轮会重新判定
                continue
            # 同 api_ticket：游戏把 ticket.php 的返回字段从 fine 改成了 ownerEarn，
            # 两个都得读。读不到时退回本地估算值，好过静默记成 0。
            raw_earn = result.get("ownerEarn")
            if raw_earn is None:
                raw_earn = result.get("fine")
            got = self._to_int(raw_earn, fine, minimum=0)
            total += got
            # 罚款是真金白银，必须并进当日统计（和挪车收益同一口径）。
            #
            # 这里原先只把 total 拼进日志文案就完事，_shop_fined 一次都没加 ——
            # 于是自动贴条赚的钱根本进不了统计页，「其中贴条」永远是 0。
            # run_once 里 _auto_ticket_one 后面紧跟着 _record_round，会把这笔
            # 连同挪车收益一起收走落盘。
            self._shop_fined += got
            notes.append(f"{self._car_name(car, cars_map)}@{spot_key} 罚 {got}")

        if not notes:
            return ""
        note = f"自动贴条 {len(notes)} 个车位：{'、'.join(notes)}，合计 {total} 金币"
        logger.info(f"[不可躺自动抢车位] {note}")
        return note

    async def _auto_buy_one(self, client: Any, state: dict, cars_map: Dict[Any, dict],
                            user_id: Any) -> str:
        """
        自动买车。排在停车之前调用，买到的车当轮就能停出去赚钱。

        每轮最多买一辆。返回一句执行摘要，供详情页展示。
        """
        if not self._auto_buy:
            return ""
        rules = self._settings_of(state)
        mine = self._user_of(state, user_id)
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        max_cars = self._to_int(rules.get("MAX_CARS"), 10, minimum=1)
        my_cars = [c for c in (state.get("userCars") or [])
                   if str(c.get("userId")) == str(user_id)]
        if len(my_cars) >= max_cars:
            # 满员就顺手把「自动买车」关掉，别让一个必然失败的开关一直亮着
            return self._disable_auto_buy_if_full(len(my_cars), max_cars)
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

    def _charity_exp(self, state: Optional[dict], user_id: Any) -> Tuple[int, int]:
        """
        今日「爱心停车」经验：(已得, 当日上限)。

        数据口径和等级详情面板完全同一套：已得读 `state.dailyExp` 的
        `charity_park` 键，上限读 `game_data.expConfig.charity_parkDailyLimit`
        （后端没给就落到游戏自己的默认值 50，见 _EXP_SOURCES）。

        两边必须同源 —— 用另一套算法算「满没满」，面板上看到的进度条和
        实际停不停车就会对不上。

        ★这里读的是 `self._game_data`（实例上那份），不能写成 classmethod 去读
        `cls._game_data`：类属性只有 None 的默认值，实际数据挂在实例上，
        那样读永远是 None，上限会一直退化成兜底的 50。
        """
        got = self._exp_daily(state, user_id).get("charity_park", 0)
        cap = self._exp_config(self._game_data).get("charity_park", 50)
        return self._to_int(got, 0, minimum=0), self._to_int(cap, 50, minimum=0)

    async def _park_idle_cars(
        self,
        client: AsyncRequestUtils,
        idle: List[dict],
        parking_spots: List[dict],
        user_id: Any,
        capacity: int,
        state: Optional[dict] = None,
    ) -> int:
        """
        把空闲车辆停到他人空闲的私家车位。

        车辆按每分钟收益从高到低出队，优先用收益高的车。

        车位选择是**加成分层优先 + 每主人限额的混合**：
        - 先按场景加成从高到低排（游戏规则：最终收益 = 基础收益 x (1 + 加成%)，
          加成属于**车位**，所以停进 5% 的车位自己收益多 5%）；
        - 同一加成档内保持随机 —— sort 是稳定的，所以先 shuffle 再按加成排序，
          档内就是原来的随机顺序，原先「避免所有车长期压在同一个人身上」的意图还在；
        - 再加一道每个车位主人最多 _PARK_MAX_PER_OWNER 辆的硬上限。
          光靠前两条挡不住这个：5% 场景要 5000 金币，全服买的人可能就那么两三个，
          10 辆车全压过去反而把「被针对」的风险顶满。撞上限的车位直接跳过，
          名额让给下一个高加成的人。

        场景加成拿不到时（game_data 没缓存 / 停车收益）一律按 0 处理，
        排序退化成全随机，和这个功能上线之前的行为完全一致。

        prefer_charity 打开且爱心停车经验没满时，别人的**爱心车位**也进候选池，
        并排在最前面（它们不产金币，值的是经验）。经验满了就不再往里放 ——
        否则车会全停去爱心位却一经验也涨不到，等于白停一整轮。
        """
        if capacity <= 0 or not idle:
            return 0

        got, cap = self._charity_exp(state, user_id)
        want_charity = self._prefer_charity and got < cap
        private_targets = [
            spot for spot in parking_spots
            if spot.get("ownerId") != user_id
            and not spot.get("carInSpot")
            and spot.get("spotType") != "charity"
        ]
        charity_targets = [
            spot for spot in parking_spots
            if want_charity
            and spot.get("ownerId") != user_id
            and not spot.get("carInSpot")
            and spot.get("spotType") == "charity"
        ]
        if not private_targets and not charity_targets:
            logger.info("[不可躺自动抢车位] 当前没有可停的他人空余车位")
            return 0
        if want_charity and charity_targets:
            logger.info(
                f"[不可躺自动抢车位] 优先停爱心车位赚经验：今日已得 {got}/{cap}，"
                f"可用爱心车位 {len(charity_targets)} 个")

        owner_bonus = self._owner_bonus_map(state, self._game_data)
        # 私家车位那部分：先随机再按加成排序（稳定排序 = 档内随机、档间高者优先）。
        random.shuffle(private_targets)
        private_targets.sort(
            key=lambda s: owner_bonus.get(str(s.get("ownerId")), 0), reverse=True)
        # 爱心车位本身没有金币收益可言，「场景加成」对它无意义（游戏规则也写了
        # 场景加成仅对私家车位生效），所以只打散保底顺序，不按加成排。
        random.shuffle(charity_targets)
        targets = charity_targets + private_targets

        cars_map = await self._load_cars_map(client)
        idle.sort(key=lambda c: self._earn_per_min(c, cars_map), reverse=True)

        new_parked = 0
        per_owner: Dict[str, int] = {}
        for car in idle:
            if new_parked >= capacity or not targets:
                break
            # 撞上限的车位要**就地跳过、拿同一辆车去试下一个车位**。
            # 直接 continue 换下一辆车会白丢车：候选里前几个正好是同一个停满的
            # 主人时，这辆车就没机会停进后面那些还有空位的车位了。
            spot = None
            owner_key = ""
            while targets:
                cand = targets.pop(0)
                cand_owner = str(cand.get("ownerId"))
                if per_owner.get(cand_owner, 0) >= self._PARK_MAX_PER_OWNER:
                    continue
                spot, owner_key = cand, cand_owner
                break
            if spot is None:
                # 剩下的候选全被上限挡完了，这辆车和后面的都停不进去
                break
            result = await self._post(client, "park.php", {
                "userCarId": car.get("id"),
                "spotId": spot.get("id"),
            })
            if not result or not result.get("success"):
                logger.warning(f"[不可躺自动抢车位] 停车失败：{(result or {}).get('error') or '未知原因'}")
                continue
            per_owner[owner_key] = per_owner.get(owner_key, 0) + 1
            new_parked += 1
            if spot.get("spotType") == "charity":
                # 爱心车位不产金币，日志别写「金/分钟」，那会让人以为有收入
                logger.info(
                    f"[不可躺自动抢车位] 车辆 {car.get('id')} 已停入爱心车位 "
                    f"{spot.get('id')}（车主 {owner_key}），赚经验不赚金币")
            else:
                logger.info(
                    f"[不可躺自动抢车位] 车辆 {car.get('id')} 已停入车位 {spot.get('id')}"
                    f"（车主 {owner_key}，场景加成 +{owner_bonus.get(owner_key, 0)}%），"
                    f"收益 {self._earn_per_min(car, cars_map)} 金/分钟"
                )
        return new_parked

    async def _unpark_charity_cars(
        self, client: AsyncRequestUtils, cars: List[dict],
        min_minutes: int, cars_map: Dict[Any, dict]
    ) -> Tuple[List[dict], int]:
        """
        爱心停车经验满了之后，把停在爱心车位的车挪回来，改去赚金币。

        返回 (还没挪成的, 挪成的辆数)。

        两处必须注意：
        1. 挪车接口对爱心车位返回的是**经验**，字段叫 charityExpContributed，
           不是 earned —— 算进金币收益里会凭空多出一笔收入。
        2. 游戏要求停够 MIN_PARK_DURATION 才让挪，不够就留在原位。
        """
        still: List[dict] = []
        moved = 0
        for car in cars:
            minutes = self._parked_minutes(car)
            if minutes < min_minutes:
                still.append(car)
                continue
            result = await self._post(client, "unpark.php", {"userCarId": car.get("id")})
            if not result or not result.get("success"):
                logger.warning(
                    f"[不可躺自动抢车位] 爱心挪车失败：{(result or {}).get('error') or '未知原因'}")
                still.append(car)
                continue
            exp = self._to_int(result.get("charityExpContributed"), 0, minimum=0)
            moved += 1
            logger.info(
                f"[不可躺自动抢车位] 爱心停车经验已满，车辆 {car.get('id')} 挪回来"
                f"（{self._car_name(car, cars_map)}，已贡献 {exp} 经验），改停普通车位赚金币")
        return still, moved

    async def _post(self, client: AsyncRequestUtils, endpoint: str, payload: dict) -> Optional[dict]:
        """调用游戏写接口，统一返回解析后的 JSON（失败返回 None）。"""
        return await client.post_json(f"{self.API_BASE}/{endpoint}", json=payload)

    async def _load_cars_map(self, client: AsyncRequestUtils) -> Dict[Any, dict]:
        """
        拉取车型定义表并按进程缓存，用于挑选每分钟收益最高的车辆。

        车型表看着像静态数据，其实游戏会不定期上新（2026-10-14 就一口气从 4 个分类
        加到了 9 个，新增传奇车这种要 Lv.15、价格 916.5K 的车）。原先这个缓存**没有
        失效时间**，进程不重启就永远拿不到新车，车市页看起来像"游戏没更新"。
        所以这里给一小时 TTL：到点自动重取一次，每天多 24 个请求，换来"更新了就能看到"。

        网络失败或返回异常结构时**不写缓存**，下一轮还会重试，不会把一次抖动
        固化成"没有车型表"。
        """
        if self._cars_cache and time.monotonic() - self._cars_cache_at < self._CARS_TTL:
            return self._cars_cache
        # 网络异常也要兜住：run_once 主体没有 try，一次 ConnectTimeout 就会让
        # 整轮挪车、停车、买卖全部作废，而且用户在界面上什么提示都收不到。
        # 降级成「没有车型表」，本轮照常跑（只是收益排序退回默认）。
        try:
            data = await client.get_json(f"{self.API_BASE}/game_data.php")
        except Exception as err:
            logger.warning(f"[不可躺自动抢车位] 读取车型表失败，本轮按无车型表继续：{err}")
            return {}
        if data and data.get("success"):
            # 同一份响应里还有等级表 levelTable 和经验配置 expConfig，
            # 经验值面板全靠它们。**state.php 不返回这两个字段**，
            # 所以必须跟着车型表走 —— 顺手存一份缓存，不额外发请求。
            self._game_data = data
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
                except (TypeError, ValueError, OverflowError):
                    cars[str(car_id)] = item
            self._cars_cache = cars
            self._cars_cache_at = time.monotonic()
        return self._cars_cache or {}

    @staticmethod
    def _bg_bonus_map(game_data: Optional[dict]) -> Dict[str, int]:
        """
        场景 id → 停车收益加成百分比。

        游戏规则原文：「最终收益 = 基础收益 + 基础收益 x 加成百分比。例如加成 20%，
        基础收益 100 金，则实际获得 120 金。」—— 也就是说**封顶先算、加成后乘**，
        单车位上限 3000 在 5% 车位上实际是 3150。

        数据在 game_data.php 的 parkingBackgrounds 里，和车型表同一份响应
        （_load_cars_map 已经把整份存进 _game_data），不额外发请求。
        拿不到就返回空表，后面一律按 0 处理，等于退回改动前的纯随机选车位。

        这里刻意做成**接收 game_data 的静态方法**而不是读 self._game_data 的
        classmethod：_game_data 是实例属性（类上只有 None 的默认值），
        用 cls._game_data 读永远拿到 None，场景表会恒为空 —— 功能静默失效，
        不报任何错。
        """
        out: Dict[str, int] = {}
        for item in (game_data or {}).get("parkingBackgrounds") or []:
            if not isinstance(item, dict):
                continue
            bg_id = item.get("id")
            if bg_id is None:
                continue
            out[str(bg_id)] = ParkingBot._to_int(item.get("bonusPercent"), 0,
                                                  minimum=0)
        return out

    @classmethod
    def _owner_bonus_map(cls, state: Optional[dict],
                         game_data: Optional[dict] = None) -> Dict[str, int]:
        """
        玩家 id（统一按字符串）→ 他名下车位的收益加成百分比。

        场景的 currentBg 在 **state.php 的 users 里**，每个玩家一条
        （游戏前端画 🔥 时读的就是 users[id].currentBg，见 js/modules/parking.js）。

        users 的键类型不保证 —— 游戏那边是对象，键可能是数字，而车位上的
        ownerId 是另一套类型。所以两种都登记一遍：dict 的键，以及资料自身的 id。
        只认其中一种的话，很可能整张表全查不到、加成静默变成 0，
        表现为「功能好像没生效」却没有任何报错。
        """
        bonus_map = cls._bg_bonus_map(game_data)
        users = (state or {}).get("users")
        out: Dict[str, int] = {}
        if not isinstance(users, dict):
            return out
        for uid, profile in users.items():
            if not isinstance(profile, dict):
                continue
            pct = bonus_map.get(str(profile.get("currentBg") or ""), 0)
            out[str(uid)] = pct
            pid = profile.get("id")
            if pid is not None:
                out.setdefault(str(pid), pct)
        return out

    @classmethod
    def _my_bonus_pct(cls, state: Optional[dict], game_data: Optional[dict],
                      user_id: Any = None) -> int:
        """
        **我自己**车位的场景加成百分比。

        自己的场景 id 在 state.php 的**顶层 currentBg**，不在 users[我的 id] 里。
        游戏前端分得很清（js/modules/api.js 的 loadFromServer +
        js/modules/render.js 的 applyBg）：

            this.currentBg = data.currentBg || 'jiedao'    ← 自己，顶层字段
            bgId = this.users[targetUserId].currentBg      ← 别人，才查 users

        v1.50.0 一开始错从 users[我的 id] 里查 currentBg，那儿压根没有这个字段，
        于是取到 0 —— 表现是「标题右边的 收益+N% 永远不显示」，而且不报任何错。
        顶层拿不到时才退回 users[我的 id]（万一以后接口把字段挪了位置）。
        """
        st = state or {}
        bg_id = str(st.get("currentBg") or "")
        if bg_id:
            return cls._bg_bonus_map(game_data).get(bg_id, 0)
        if user_id is not None:
            return cls._owner_bonus_map(st, game_data).get(str(user_id), 0)
        return 0

    @staticmethod
    def _user_of(state: Any, user_id: Any) -> dict:
        """
        从 state.users 里取某个玩家的资料，取不到 / 结构不对一律当「没有」。

        `(state.get("users") or {}).get(x) or {}` 这个写法挡不住 users 是
        字符串或列表这类真值 —— 紧接着的 .get() 会抛 AttributeError，
        而这些位置都在**渲染路径和 _record_round 的写入路径**上，没有兜底 try：
        抛了就是整个插件页 500，或者整轮统计写不进去。

        键类型也不保证（游戏那边 users 是对象，键可能是数字），两种都试一次。
        """
        users = state.get("users") if isinstance(state, dict) else None
        if not isinstance(users, dict):
            return {}
        profile = users.get(str(user_id))
        if profile is None and isinstance(user_id, (str, int, float, bool)):
            profile = users.get(user_id)
        return profile if isinstance(profile, dict) else {}

    @staticmethod
    def _settings_of(state: Any) -> dict:
        """同上，给 gameSettings 用。返回的一定是字典，调用方可以放心 .get()。"""
        rules = state.get("gameSettings") if isinstance(state, dict) else None
        return rules if isinstance(rules, dict) else {}

    @staticmethod
    def _lookup_car(cars_map: Dict[Any, dict], car_id: Any) -> Optional[dict]:
        """按车辆实例上的 carId 找车型定义，数字/字符串两种键都试一次。"""
        if car_id is None:
            return None
        if car_id in cars_map:
            return cars_map[car_id]
        try:
            return cars_map.get(int(car_id))
        except (TypeError, ValueError, OverflowError):
            return cars_map.get(str(car_id))

    async def _load_flash_sales(self, client: AsyncRequestUtils) -> List[dict]:
        """
        拉取限时/限量特卖列表。

        刻意不做缓存：特卖是限时场次，缓存几分钟后页面上仍挂着已经结束的场次，
        抢购按钮点了只会报错。而执行周期 5 分钟，短于任何有意义�� TTL，
        缓存在生产里根本命不中 —— 只在同一个周期内复用（与称号并行拉取）。

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

    async def _load_titles(self, client: AsyncRequestUtils, state: dict,
                           force: bool = False) -> dict:
        """
        读取称号列表。

        state.php 只带 currentTitleId / currentTitleName，能选哪几个称号在
        titles.php。这里只留已获得的 —— 游戏的选择器同样只列 owned，列一堆
        拿不到的称号只会让人以为是自己哪里做错了。

        按进程缓存 10 分钟：称号只在解锁或切换时变，而执行周期是 5 分钟，
        这个 TTL 正好跨两个周期，稳定状态下每两轮才发一次请求。

        当前佩戴称号自己的颜色也一并带出来，页面上的标签要按称号的颜色显示，
        和游戏顶栏一致（currentTitleName 只在 state.php 里，颜色只有这里有）。
        """
        current = self._to_int(state.get("currentTitleId"), 0, minimum=0)
        # 缓存命中时只需重算「当前佩戴的是哪个颜色」——列表本身不会因为
        # 换了个称号而改变，不必重新解析。
        cached = self._titles_cache
        if cached and not force and time.monotonic() - self._titles_at < self._TITLES_TTL:
            color = next((t["color"] for t in cached if t["id"] == current), _GOLD)
            return {"owned": list(cached), "color": color}
        owned: List[dict] = []
        color = _GOLD
        try:
            data = await client.get_json(f"{self.API_BASE}/titles.php")
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] 读取称号列表失败，已忽略：{err}")
            data = None
        for item in ((data or {}).get("titles") or []):
            if not isinstance(item, dict) or not item.get("owned"):
                continue
            title_id = self._to_int(item.get("id"), 0, minimum=0)
            owned.append({"id": title_id,
                          "name": str(item.get("name") or ""),
                          "desc": str(item.get("desc") or ""),
                          "color": str(item.get("color") or _GOLD),
                          # owned 一并留着：页面再过滤一次，快照里混进没解锁的
                          # 称号时不会直接列出来给人点
                          "owned": True})
            if title_id == current:
                color = owned[-1]["color"]
        # 请求失败时不要把空列表写进缓存，否则一次网络抖动会让接下来 10 分钟
        # 的称号选择器都空着。
        if data is not None:
            self._titles_cache, self._titles_at = owned, time.monotonic()
        return {"owned": owned, "color": color}

    async def _load_game_context(
        self, prefetch: bool = True, extra: Any = None
    ) -> Tuple[Optional[Any], Optional[dict], Dict[Any, dict], str, Any]:
        """
        取出「HTTP 客户端 + 游戏状态 + 车型表」。

        停车、挪车、买车、换车、抢购、刷新状态都共用这个，避免各处各写一遍
        鉴权与错误处理。第三个返回值非空即表示出错，此时前两项不可用。

        state.php 与 game_data.php（车型表）互不依赖 —— 车型表只要 client，
        不看 state —— 所以并行拉。串行时每个手动操作都要多等一个往返，
        而游戏站单请求约 1.1 秒，用户点一下按钮就得多等一秒。
        车型表有进程缓存，缓存命中时本来就不发请求，并行也不会多花钱。

        prefetch=False 表示「本次不会记账，别启动市场预取」。预取的结果只有
        _record_round 会去取（它要把特卖/称号/未读数落进快照）。像通知面板这种
        只读不记账的路径要传 False，否则每次点铃铛都白发 3 个请求
        （flash_sale + titles + 未读数），取回来没人用。

        extra 是一个 ``lambda client: 协程``，让调用方想拉的别的数据
        **和 state.php 一起并发**发出去。通知面板原来是串行的：
        先 state.php 鉴权（约 1.1 秒），再 notifications.php 拉列表（又 1.1 秒），
        打开面板要等两个来回。两者互不依赖，并行后一次往返就够了。
        传进来的协程用 return_exceptions 接住，别让它把整个 gather 带崩。
        """
        cookie = await self._resolve_cookie()
        if not cookie:
            return None, None, {}, self._sso_error or "未取到登录 Cookie", None
        client = AsyncRequestUtils(cookies=cookie, timeout=20)
        # _load_cars_map 内部自己兜异常，这里 gather 不会因它而炸
        jobs = [client.get_json(f"{self.API_BASE}/state.php"),
                self._load_cars_map(client)]
        if extra is not None:
            jobs.append(extra(client))
        if extra is None:
            state, cars_map = await asyncio.gather(*jobs)
            extra_result = None
        else:
            state, cars_map, extra_result = await asyncio.gather(
                *jobs, return_exceptions=True)
            if isinstance(extra_result, BaseException):
                # extra 失败不该挡住鉴权成功的整体流程，记一条就按空值继续。
                logger.warning(
                    f"[不可躺自动抢车位] 并发取数失败：{extra_result}")
                extra_result = None
            # return_exceptions=True 是**整个 gather** 的开关，state.php 和车型表
            # 的异常也会被接住。不把它们归位的话，下一行 state.get("success")
            # 会对着一个异常对象炸成 AttributeError，用户看到的是内部错误而不是
            # 「登录状态失效」。原来（不接异常）这里是直接往外抛，所以归位成
            # 空值后走的是同一条对外分支，对外表现不变。
            if isinstance(state, BaseException):
                logger.warning(
                    f"[不可躺自动抢车位] 取游戏状态失败：{state}")
                state = None
            if isinstance(cars_map, BaseException):
                cars_map = {}
        if not state or not state.get("success"):
            await self._drop_cached_cookie()
            return (client, None, {},
                    f"登录状态失效：{(state or {}).get('error') or '游戏接口无响应'}",
                    None)
        if prefetch:
            # 顺手把特卖/称号的预取挂上：调用方接下来要去发真正的写请求
            #（挪车/停车/买卖），那段时间正好用来跑这两个只读接口。
            self._start_market_prefetch(client, state)
        return client, state, cars_map, "", extra_result

    def _start_market_prefetch(self, client: Any, state: dict) -> None:
        """
        启动「特卖 + 称号 + 未读通知数」的并行拉取，结果留给 _record_round 取用。

        不 await —— 就是为了让调用方先去做自己的事。重复启动是安全的：
        已经在途的任务直接复用，不会连发两次请求。
        三个被调方各自都兜住了异常，这里即使真抛了，_record_round 那边的
        gather 结果也会退化成空值，不影响落快照。
        """
        task = self._market_prefetch
        if task is not None and not task.done():
            return
        task = asyncio.gather(
            self._load_flash_sales(client), self._load_titles(client, state),
            self._load_notif_count(client))
        # 操作中途失败导致没人来取结果时，事件循环收尾会刷
        # 「_GatheringFuture exception was never retrieved」。挂个回调把异常吃掉，
        # 只留一条 debug，不让并发优化变成日志噪音。
        task.add_done_callback(ParkingBot._drain_prefetch)
        self._market_prefetch = task

    @staticmethod
    def _drain_prefetch(task: Any) -> None:
        """预取任务无人认领时的兜底：消费掉异常，只记 debug。"""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.debug(f"[不可躺自动抢车位] 特卖/称号预取异常（已忽略）：{exc}")

    async def _take_market_prefetch(self, client: Any, state: dict
                                    ) -> Tuple[List[dict], dict, Optional[int]]:
        """取预取结果；没有在途任务就现场拉一次。第三项是未读通知数（None 表示没拉到）。"""
        task, self._market_prefetch = self._market_prefetch, None
        if task is not None:
            try:
                flash, titles, unread = await task
                return list(flash or []), (titles or {}), unread
            except Exception as err:
                # 预取失败就退回到「重新拉一次」，不能让它连带影响落快照
                logger.debug(f"[不可躺自动抢车位] 特卖/称号预取失败，改为现拉：{err}")
        return await asyncio.gather(
            self._load_flash_sales(client), self._load_titles(client, state),
            self._load_notif_count(client))

    # ---------------- 消息通知 ----------------
    # 游戏侧端点：api/notifications.php，一个端点四种用法
    #   GET  ?count=1                  -> {success, unread}
    #   GET  ?perPage=50               -> {success, notifications: [...]}
    #   POST {action: "read", id}      单条已读（插件**不用**，只读面板）
    #   POST {action: "read_all" | "clear"}
    # 每条字段：id / notify_type / message / created_at / is_read

    async def _load_notif_count(self, client: Any) -> Optional[int]:
        """
        拉未读通知数。挂进特卖/称号那个 gather 里，所以不额外花时间。

        取不到时返回 None 而不是 0：调用方会沿用上一次快照里的数。返回 0
        等于把「接口挂了」和「真的没有未读」混成同一个数，铃铛上的角标会
        在一次网络抖动后凭空消失，用户还以为通知被谁读了。
        """
        try:
            data = await client.get_json(f"{self.API_BASE}/notifications.php?count=1")
        except Exception as err:
            logger.debug(f"[不可躺自动抢车位] 读取未读通知数失败（沿用上一次）：{err}")
            return None
        if not (isinstance(data, dict) and data.get("success")):
            return None
        return self._to_int(data.get("unread"), 0, minimum=0)

    async def _load_notif_list(self, client: Any) -> List[dict]:
        """
        拉通知列表。只在用户点开面板时调 —— 每轮都拉 50 条纯属浪费：
        未读数走 ?count=1 已经够画角标了，列表没人看的时候不该去取。
        """
        try:
            # category=all 必须带：游戏那边每次都带（showNotifications 里拼的
            # 'notifications.php?perPage=50&category=' + this._notifCategory），
            # 这个后端认它。少带一个参数就可能返回空数组，
            # 页面上看起来就是「明明有通知却说什么都没有」。
            data = await client.get_json(
                f"{self.API_BASE}/notifications.php"
                f"?perPage={self._NOTIF_PAGE}&category=all")
        except Exception as err:
            logger.warning(f"[不可躺自动抢车位] 读取通知列表失败：{err}")
            return []
        if not (isinstance(data, dict) and data.get("success")):
            # 不抛异常、也不静默：角标明明有未读、列表却是空的，
            # 这种「静默空」最难排查 —— 页面上只显示「暂无通知」，
            # 后端一点线索都不留。把真实响应原样记下来。
            logger.warning(
                f"[不可躺自动抢车位] 通知列表返回异常："
                f"{repr(data)[:300]}")
            return []
        rows = self._notif_rows(data)
        if not rows:
            logger.warning(
                f"[不可躺自动抢车位] 通知列表为空，响应键："
                f"{sorted(data)[:12]}")
        return rows

    @staticmethod
    def _notif_rows(data: Any) -> List[dict]:
        """把接口返回的通知收敛成页面要的字段，顺便丢掉非字典项。"""
        rows = []
        for item in ParkingBot._dict_rows(
                data.get("notifications") if isinstance(data, dict) else None):
            rows.append({
                "id": item.get("id"),
                "type": str(item.get("notify_type") or "").strip(),
                "message": str(item.get("message") or "").strip(),
                "time": str(item.get("created_at") or "").strip(),
                # 任何真值都当已读。游戏侧给的是 0/1，但也可能是 true 或 "1"
                "read": bool(item.get("is_read")),
            })
        return rows

    @staticmethod
    def _has_tz_offset(text: Any) -> bool:
        """
        时间串里有没有带时区偏移（结尾的 Z / +08:00 / +0800 / -05:00 …）。

        判断依据是**原始字符串**，不是解析后的 datetime —— 解析后的对象已经被
        统一成「MoviePilot 本地时间且无 tzinfo」，区分不出「本来就带偏移」和
        「本来没带、我们当本地解释了」这两种来源。
        """
        t = str(text or "").strip()
        if not t:
            return False
        if t.endswith(("Z", "z")):
            return True
        return bool(re.search(r"[+-]\d{2}:?\d{2}$", t))

    @classmethod
    def _notif_time(cls, raw: Any) -> str:
        """
        相对时间，口径照抄游戏前端的 _fmtTime：刚刚 / N分钟前 / N小时前 /
        N天前 / 更早就显示具体日期。

        **notifications.php 的 created_at 是 UTC，不是本地时间**（实测）：
        本地 22:56 跑的那一轮，接口下发的是 `"2026-10-16 14:56:11"`，
        正好差 8 小时。而 state.php 的 parkedTime 是本地时间 ——
        游戏服务端两个端点口径不一致，只能在各自消费的地方各自处理。

        `_parse_time` 把**无偏移字符串一律当本地时间**（这是对的，
        parkedTime 依赖它），直接拿它的结果去减会整块偏 8 小时：
        面板把「刚刚」显示成「8小时前」，还完全不自知。

        所以这里：原始串自带偏移的照旧（`_parse_time` 已经换算过）；
        不带偏移的按 UTC 解释，再换算到 MoviePilot 时区。
        """
        text = str(raw or "").strip()
        if not text:
            return ""
        when = cls._parse_time(text)
        if when is None:
            return text
        if not cls._has_tz_offset(text):
            when = when.replace(tzinfo=pytz.utc).astimezone(
                cls._tz()).replace(tzinfo=None)
        minutes = int((cls._now_naive() - when).total_seconds() // 60)
        if minutes < 1:
            return "刚刚"
        if minutes < 60:
            return f"{minutes}分钟前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}小时前"
        days = hours // 24
        if days < 7:
            return f"{days}天前"
        return when.strftime("%Y-%m-%d %H:%M")

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
        hosts: List[str] = []
        try:
            for _ in range(6):  # 跳转链通常 2~3 跳，留足冗余
                hosts.append(url.split("/")[2] if "://" in url else url)
                resp = await client.get_res(url, allow_redirects=False)
                if resp is None:
                    # 原来这里只 return 空串、不设 _sso_error，外层只能报一句
                    # 「未取到登录 Cookie」，把排查方向带偏 —— 明明是「主站那一步
                    # 就没连上」，和「Cookie 拿到了但游戏不认」是两回事。
                    self._sso_error = f"主站 SSO 第 {len(chain) + 1} 跳无响应（{url}）"
                    logger.error(f"[不可躺自动抢车位] {self._sso_error}")
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
            self._sso_error = (f"主站 SSO 换取 Cookie 异常："
                               f"{type(err).__name__}: {err}")
            logger.error(f"[不可躺自动抢车位] {self._sso_error}")
            return ""
        finally:
            # 跳转链必须在这里打，而不是放在 try 之后 —— 中途 return / 抛异常
            # 的那几条链恰恰是排查这条报错唯一有用的线索（「链走到第几跳断的」、
            # 「有没有真的跳到 qcw 子域」），放在成功路径上等于永远打不出来。
            logger.info(
                "[不可躺自动抢车位] 主站 SSO 跳转链（共 %d 跳）：%s"
                % (len(chain), " ‖ ".join(chain) or "（一跳都没走成）"))
            logger.info(
                "[不可躺自动抢车位] SSO 走过的域名：%s"
                % (" → ".join(hosts) if hosts else "（无）")
                + ("；已到达游戏子域" if any("qcw." in h for h in hosts)
                   else "；**没有走到游戏子域 qcw.tangpt.top**")
            )

        cookie = "; ".join(f"{name}={value}" for name, value in jar.items())
        logger.info("[不可躺自动抢车位] 本次带上的 Cookie 名（不回显取值）：%s"
                    % (", ".join(sorted(jar)) or "（一个都没有）"))

        # 回游戏验证：只有真的能过 state.php 鉴权才算换到。
        # ★probe 必须自己兜住异常：这里抛出去会一路掀翻 run_once，
        # 用户看到的是「插件不跑了」而不是一条能定位的报错。
        probe = AsyncRequestUtils(cookies=self._parse_cookie_header(cookie), timeout=20)
        state: Any = None
        probe_err = ""
        try:
            state = await probe.get_json(f"{self.API_BASE}/state.php")
        except Exception as err:
            probe_err = f"{type(err).__name__}: {err}"
        if not (isinstance(state, dict) and state.get("success")):
            # (state or {}).get(...) 挡不住非字典响应：服务端回登录页/风控页时
            # 这里会 AttributeError 掀翻整轮，而它恰恰是最该报清楚的那种情况。
            detail = self._describe_probe(state, probe_err)
            logger.error(
                f"[不可躺自动抢车位] 换到 Cookie 后回游戏验证失败 —— {detail}。"
                "已获取的 Cookie 不会缓存；上面的跳转链和域名那两行是排查入口。")
            reason = (state.get("error") if isinstance(state, dict) else "") \
                or probe_err or self._probe_reason(state)
            self._sso_error = f"主站 SSO 走完了但游戏仍判未登录（{reason}）"
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
    def _probe_reason(state: Any) -> str:
        """给用户看的那一句短原因（细节看日志）。同样是按响应形态分开的，
        原来一律叫「游戏接口无响应」，用户看到只知道坏了，不知道坏在哪。"""
        if state is None:
            return "state.php 无响应（连不上或超时）"
        if isinstance(state, str):
            return ("游戏返回的是登录页 / 风控拦截页（Cookie 没被认下）"
                    if "<" in state[:400] else "state.php 返回的不是 JSON")
        if not isinstance(state, dict):
            return f"state.php 返回类型异常：{type(state).__name__}"
        return "state.php 返回空内容"

    @staticmethod
    def _describe_probe(state: Any, probe_err: str = "") -> str:
        """
        把 state.php 的失败响应描述成人能一眼判断的样子。

        原来只有一句「游戏接口无响应」，把三种完全不同的失败揉成了一团：
        连不上（None）、服务端回了登录页或风控页（字符串 / HTML）、
        回了 JSON 但带 error。看到那句话根本不知道该查网络、查 Cookie 还是查服务端。

        只描述**类型和结构**，不回显取值 —— 正文里可能夹带风控挑战 token，
        超过 24 位的连续字母数字/下划线/连字符一律替换掉。
        """
        if probe_err:
            return f"请求 state.php 抛异常 {probe_err}"
        if state is None:
            return "state.php 无响应（连接失败或超时，客户端拿到 None）"
        if isinstance(state, str):
            head = re.sub(r"[A-Za-z0-9_\-]{24,}", "<长串已隐去>",
                          state[:120].replace("\n", " ").replace("\r", " "))
            kind = ("疑似 HTML 页面（多半是登录页 / 风控拦截页，"
                    "说明 Cookie 没被游戏站认下）" if "<" in state[:400]
                    else "字符串（不是预期的 JSON）")
            return f"{kind}，共 {len(state)} 字符，开头：{head!r}"
        if not isinstance(state, dict):
            return f"state.php 返回类型异常：{type(state).__name__}"
        if state.get("success"):
            return "state.php 返回 success=true（这一条不该走到失败分支）"
        return ("state.php 返回 JSON 但 success 非真："
                f"error={state.get('error')!r}，"
                f"顶层键={sorted(state)[:10]}")

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

    @classmethod
    def _parked_at(cls, holder: dict) -> Optional[datetime]:
        """
        取「停入时刻」，车辆和车位都走这一个入口。

        **优先用 parkedTimestamp（epoch 毫秒）** —— 游戏前端自己就是这么算的：
        - 车：`uc.parkedTimestamp || new Date(uc.parkedTime).getTime()`
          （js/modules/render.js、parking.js 共用这个写法）
        - 车位：`spot.parkedTimestamp ? Math.floor((self.now() - spot.parkedTimestamp) / 60000) : 0`
          —— 车位那边**根本不用 parkedTime**

        它是绝对时刻，不受服务端时区配置影响。而 `parkedTime` 只是个字符串，
        时区口径得靠猜：同一个游戏站的 `notifications.php` 的 `created_at`
        是 UTC，`state.php` 的 `parkedTime` 却是本地（v1.50.2 刚踩过这个）。
        既然游戏自己给了绝对时间戳，就没道理去猜字符串。

        没有 / 解析不出 parkedTimestamp 时才退回 parkedTime，
        保证老接口结构下行为和以前完全一致。

        换算出来的时刻还要过一道**合理性检查**：车辆 12 小时后会被系统清理，
        所以真实的停入时刻不可能早于 30 天前、更不可能是未来。越界就说明
        这个字段的**单位**和预期不一样（比如哪个端点给的是秒而不是毫秒）——
        `fromtimestamp(1.79e9)` 会得到 1970 年，于是「已停时长」显示成几十年，
        挪车按钮还会直接亮（够「停够时长」了），拿一个错得离谱的时间去覆盖
        本来可用的 parkedTime 字符串，纯属倒扣分。
        """
        stamp = cls._to_int(holder.get("parkedTimestamp"), 0, minimum=0)
        if stamp > 0:
            moment = None
            try:
                moment = datetime.fromtimestamp(
                    stamp / 1000.0, cls._tz()).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError) as err:
                # 时间戳离谱（未来时间 / 负数溢出）不该让整页崩，退回字符串
                logger.debug(f"[不可躺自动抢车位] parkedTimestamp 无法换算：{err}")
            if moment is not None:
                # 往回放宽到 30 天、往前往宽到 1 天：真实停放最多 12 小时
                # （AUTO_REMOVE_HOURS），这个窗口足够宽松，只拦真正的量纲错误。
                if -1440 < (cls._now_naive() - moment).total_seconds() < 432000:
                    return moment
                logger.debug(
                    f"[不可躺自动抢车位] parkedTimestamp 量纲异常（{stamp}），"
                    f"换算成 {moment} 明显不合理，改用 parkedTime")
        return cls._parse_time(holder.get("parkedTime"))

    def _parked_minutes(self, car: dict) -> float:
        """
        计算车辆已停放的分钟数。

        时间无法解析时返回 0 —— 宁可多停一轮，也不发出必然被服务端拒绝的挪车请求。
        """
        moment = self._parked_at(car)
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
        """
        把配置值安全转成整数；非法值回落到默认值，越界值收敛到边界。

        JSON 里的浮点数（1000.0）和字符串小数（"12.7"）int(str(...)) 会直接抛，
        旧写法一律回落默认值 —— 页面于是把金币显示成 0，而 _fmt_num 又显示 1000，
        同一份数据两个数。所以这里先试浮点、再截断取整。

        OverflowError 必须一起捕获：int(float("inf")) 抛的是它而不是 ValueError，
        而 json.loads 默认接受 Infinity / 1e400 这类非标准写法并解析成 inf，
        只捕 (TypeError, ValueError) 会让一个坏数字把整个插件页打成 500。
        """
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            try:
                number = int(float(str(value).strip()))
            except (TypeError, ValueError, OverflowError):
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
            # 两者互不依赖，并行拉取：各要 1.1 秒，串行就是 2.2 秒。
            # _load_game_context 已经挂过预取的话，这里直接取结果不重发。
            flash, titles, notif_count = await self._take_market_prefetch(client, state)
            rules = self._settings_of(state)
            max_earn = self._to_int(rules.get("MAX_EARN_PER_SPOT"), 3000, minimum=1)
            min_duration = self._to_int(rules.get("MIN_PARK_DURATION"), 10, minimum=1)
            mine = self._user_of(state, user_id)
            my_cars = [c for c in (state.get("userCars") or [])
                       if isinstance(c, dict) and str(c.get("userId")) == str(user_id)]
            my_spots = [s for s in (state.get("parkingSpots") or [])
                        if isinstance(s, dict) and str(s.get("ownerId")) == str(user_id)]
            # 展示用时间戳与时长计算统一走 MoviePilot 时区，避免两者漂移
            now = self._now_naive()

            cars = []
            for car in my_cars:
                parked_at = self._parked_at(car)
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

            # 铃铛角标用的未读数。这轮没拉到（接口挂了 / 超时）就沿用上一次的，
            # 而不是写成 0 —— 后者等于把「接口挂了」和「真的没有未读」当成同一个数，
            # 角标会在一次网络抖动后凭空消失，看起来像通知被谁读了。
            #
            # 通知列表也必须一起带过去：_record_round 每轮都**重建整份快照**，
            # 不显式搬运的话，下一次定时任务就把用户刚点开看到的列表抹成空 ——
            # 而定时任务是每 5 分钟一次，等于这个面板基本永远显示「暂无通知」。
            prev = await self.async_get_data("snapshot")
            prev = prev if isinstance(prev, dict) else {}
            carried = prev.get("notifications")
            notif_unread = (notif_count if notif_count is not None
                            else self._to_int(prev.get("notifUnread"), 0, minimum=0))

            snapshot = {
                "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "name": mine.get("name") or "未命名",
                "level": self._to_int(mine.get("level"), 1, minimum=1),
                # 经验值。levelTable / expConfig 来自 game_data.php（跟车型表同一份
                # 缓存，state.php 不返回它们），dailyExp 来自 state.php：
                #   exp        users[我的 id].exp，累计总经验
                #   levelTable game_data.levelTable，expReq 是**累计**门槛
                #   expConfig  game_data.expConfig 的 xxxDailyLimit，各来源每日上限
                #   dailyExp   state.dailyExp，键是 "<用户ID>_<动作>_<日期>"
                "exp": self._to_int(mine.get("exp"), 0, minimum=0),
                "levelTable": self._exp_level_table(self._game_data or {}),
                "expConfig": self._exp_config(self._game_data or {}),
                "dailyExp": self._exp_daily(state, user_id),
                "cash": self._to_int(mine.get("cash"), 0),
                "bonus": self._to_int(mine.get("bonus"), 0),
                "car_total": len(my_cars),
                "car_max": self._to_int(rules.get("MAX_CARS"), 10, minimum=1),
                "parked_total": sum(1 for c in cars if c["status"] != "空闲"),
                "pending_gain": sum(item["gain"] for item in cars),
                "min_duration": min_duration,
                "note": note,
                # 称号：state.php 只给 currentTitleId 和 currentTitleName，
                # 可选项列表要另打 titles.php
                "title_id": self._to_int(state.get("currentTitleId"), 0, minimum=0),
                "title_name": str(mine.get("currentTitleName") or ""),
                "title_color": str(titles.get("color") or _GOLD),
                "titles": titles["owned"],
                # 我自己车位的场景加成。**顶层 currentBg**（不是 users[我]，
                # 那个位置没有这个字段），加成百分比在 game_data 的
                # parkingBackgrounds 里。停车挑车位用的是同一张表
                # （_bg_bonus_map），只是取值位置不同。
                "spot_bonus_pct": self._my_bonus_pct(
                    state, self._game_data, user_id),
                "cars": cars,
                "spots": spots,
                # 他人空余私家车位数。车库页的「立即停到他人车位」按钮靠它决定
                # 亮不亮 —— 没有可停的车位时按钮该是灰的，而不是让用户点了吃一个错。
                "free_spots": sum(
                    1 for s in self._dict_rows(state.get("parkingSpots"))
                    if not s.get("carInSpot")
                    and s.get("spotType") != "charity"
                    and str(s.get("ownerId")) != str(user_id)),
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
                "notifUnread": notif_unread,
                # 通知列表跨轮次带过去，见上面 notif_unread 那段注释。
                # 不是列表（第一次点开之前还没拉过）就留空键，别塞 None ——
                # 面板用 _dict_rows 过滤，None 和 [] 结果一样，但快照里
                # 混进 None 会让「这份快照有没有通知列表」这件事变得没法一眼看懂。
                "notifications": (list(carried)
                                  if isinstance(carried, list) else []),
            }
            await self.async_save_data("snapshot", snapshot)

            day_key = now.strftime("%Y-%m-%d")
            hour_key = str(now.hour)
            # 判据必须是「**daily 这个键在不在**」，不能是「缓存对象是不是 None」。
            # _page_cache 可能是残缺的：_record_round 兜底时只装 {"snapshot","history"}，
            # 而插件刚重载、页面还没渲染过时点一下铃铛，_write_notifications 会装上
            # 一份只有 {"snapshot"} 的缓存。原来判 `is not None`，于是这份残缺缓存
            # 让下面拿到空字典，再由 async_save_data("daily", …) 把库里那几十天
            # 的统计**整份覆盖掉**，而页面上一声不吭。
            _cached_daily = (self._page_cache or {}).get("daily")
            if isinstance(_cached_daily, dict):
                daily = dict(_cached_daily)
            else:
                stored_daily = await self.async_get_data("daily") or {}
                daily = dict(stored_daily) if isinstance(stored_daily, dict) else {}
            bucket = dict(daily.get(day_key) or {})

            # 买卖花费并进当天：购车记全额、换车记差价，两者在统计页分开显示。
            #
            # 这一段必须放在 record_history 分支之外 —— 手动买车/换车走的也是
            # _refresh_snapshot（record_history=False），不并进来统计页就看不到
            # 手动花掉的金币，当日净收会虚高一大截。
            bucket["spent"] = self._to_int(bucket.get("spent"), 0) + self._shop_spent
            bucket["bought"] = self._to_int(bucket.get("bought"), 0) + self._shop_bought
            bucket["diff"] = self._to_int(bucket.get("diff"), 0) + self._shop_diff
            bucket["traded"] = self._to_int(bucket.get("traded"), 0) + self._shop_traded
            # 贴条罚款是收入，和挪车收益同一口径，一并进当日收益。
            # 但要另存一份 fined：统计页要单独展示「贴条收入」，
            # 只把它并进 earned 的话，那笔钱在页面上就和挪车收益混成一坨，
            # 用户永远看不出贴条到底赚了多少。
            bucket["fined"] = self._to_int(bucket.get("fined"), 0) + self._shop_fined
            bucket["earned"] = self._to_int(bucket.get("earned"), 0) + self._shop_fined
            self._shop_fined = 0
            self._shop_spent = self._shop_bought = self._shop_diff = self._shop_traded = 0
            daily[day_key] = bucket

            if not record_history:
                # 买卖/挪车这类操作不写执行历史（否则全是 0 停车 / 0 挪车的噪声行），
                # 也不该顶掉「执行轮次」——但花费已经并进上面那个桶了。
                #
                # 同上：这里也必须按键判断，不能判缓存对象本身。
                # 写成空的话，下一轮 run_once 看到缓存「有内容」便拿这份空值整份
                # 覆盖回库，落盘的 50 条历史会被清空。
                cache = self._page_cache
                if not isinstance((cache or {}).get("history"), list):
                    stored_h = await self.async_get_data("history") or []
                    cache = {"history": list(stored_h) if isinstance(stored_h, list) else [],
                             "daily": daily}
                else:
                    cache["daily"] = daily
                cache["snapshot"] = snapshot
                self._page_cache = cache
                await self.async_save_data("daily", daily)
                return

            daily[day_key] = bucket

            # 历史在内存里维护一份，避免每轮都读库。但插件重载会把模块重新执行、
            # 类属性复位，此时 _page_cache 为 None；若不先从数据库把历史捞回来，
            # 第一轮就会用「空历史 + 新增一条」覆盖掉落盘的 50 条记录。
            # 同样地，残缺缓存（缺 history 键）也得先从库里捞 —— 见上面 daily 那段。
            _cached_h = (self._page_cache or {}).get("history")
            if isinstance(_cached_h, list):
                history = list(_cached_h)
            else:
                stored = await self.async_get_data("history") or []
                history = list(stored) if isinstance(stored, list) else []
            history.insert(0, {
                "time": now.strftime("%m-%d %H:%M"),
                "day": day_key,
                "parked": new_parked,
                "moved": moved,
                "earned": earned,
            })
            history = history[:50]
            await self.async_save_data("history", history)

            # 执行历史只有 50 轮（约 4 小时），撑不起「每天金币统计」。
            # 这里另存一份按天累计：日期 → 当日收益/停放/挪走/轮次 + 24 小时分桶，
            # 长期保留，「金币统计」页的柱状图与日汇总都从这里出。
            bucket["earned"] = self._to_int(bucket.get("earned"), 0) + earned
            bucket["parked"] = self._to_int(bucket.get("parked"), 0) + new_parked
            bucket["moved"] = self._to_int(bucket.get("moved"), 0) + moved
            bucket["rounds"] = self._to_int(bucket.get("rounds"), 0) + 1
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

    # ========== 经验值（v1.40.0，数据源在 v1.42.0 改成实测口径）==========
    #
    # 下面每一行都对着 **game_data.php 的真实响应**和 **游戏前端 showExpDetail 的源码**
    # 校过，不再是推测：
    #   levelTable [{"level":7,"expReq":1700},{"level":8,"expReq":2500}, ...]
    #       expReq 是**累计**门槛，不是「本级需要多少」。
    #   expConfig  {"park":15,"ticket":15,...,"parkDailyLimit":150,
    #               "ticketDailyLimit":90,"reportDailyLimit":100,
    #               "buyDailyLimit":300,"tradeInDailyLimit":300}
    #       左半（park/ticket/...）是「单次动作给多少经验」，
    #       **右半 xxxDailyLimit 才是每日上限** —— 一开始只认左半，
    #       页面上每个来源的上限都显示成 15 这种单次值。
    #   dailyExp   键是复合的 "<用户ID>_<动作>_<日期>"（游戏 addExp 拼的），
    #       比如 "liyo1_park_2026-10-16"，不是直接拿动作名当键。
    #
    # 这三样全在 game_data.php / state.php，**不在同一个响应里**，
    # 而 levelTable 和 expConfig 只在 game_data.php 里有（state.php 不返回）。
    # 所以它们跟着车型表走同一份缓存，不额外发请求。
    @staticmethod
    def _exp_level_table(game_data: dict) -> List[dict]:
        """取等级表 [{level, expReq}]，按 level 升序。

        expReq 是**累计**经验门槛（游戏 updateExpProgress 拿 userData.exp 与它比较），
        不是「本级需要多少」。
        """
        raw = game_data.get("levelTable") if isinstance(game_data, dict) else None
        rows = [r for r in (raw if isinstance(raw, list) else [])
                if isinstance(r, dict)]
        out = []
        for r in rows:
            lv = ParkingBot._to_int(r.get("level"), 0, minimum=0)
            if lv <= 0:
                continue
            out.append({"level": lv,
                        "expReq": ParkingBot._to_int(r.get("expReq"), 0, minimum=0)})
        out.sort(key=lambda x: x["level"])
        return out

    @classmethod
    def _exp_config(cls, game_data: dict) -> Dict[str, int]:
        """
        取各来源每日上限，归一成 {动作键: 上限}。

        上限在 expConfig 的 **xxxDailyLimit** 那一组。游戏自己的 showExpDetail 里
        每个动作都带一个 `|| 默认值` 兜底，所以后端没给这个键时游戏照样显示一个数 ——
        这里跟它一样取默认值，页面上永远是 7 行，不会少行。
        """
        cfg = (game_data or {}).get("expConfig")
        cfg = cfg if isinstance(cfg, dict) else {}
        out: Dict[str, int] = {}
        for key, _label, fallback in cls._EXP_SOURCES:
            out[key] = cls._to_int(cfg.get(key + "DailyLimit"), fallback, minimum=0)
        return out

    @classmethod
    def _exp_daily(cls, state: dict, user_id: Any = None) -> Dict[str, int]:
        """
        取今日各来源已得经验，归一成 {动作键: 已得}。

        dailyExp 的键是复合的 `<用户ID>_<动作>_<日期>`，要拆开才能用。
        拆法是「剥掉用户 ID 前缀 + 剥掉日期后缀，中间必须正好是一个已知动作名」，
        **不能用 rsplit("_", 2)**：动作名本身带下划线（charity_park），
        rsplit 会把 "liyo1_charity_park_2026-10-16" 切成动作 "park"，
        于是爱心停车的经验被算到停车头上，页面上显示成 170/150。
        只留今天的 —— 昨天的数字叠上来会让「今日经验」凭空变大。
        """
        raw: dict = {}
        holders: List[Any] = [state]
        if isinstance(state, dict) and user_id is not None:
            holders.append(ParkingBot._user_of(state, user_id))
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            for key in ("dailyExp", "expToday", "todayExp"):
                val = holder.get(key)
                if isinstance(val, dict) and val:
                    raw = val
                    break
            if raw:
                break
        if not raw:
            return {}
        today = cls._now_naive().strftime("%Y-%m-%d")
        known = {key for key, _label, _fb in cls._EXP_SOURCES}
        prefix = f"{user_id}_" if user_id is not None else ""
        tail = f"_{today}"
        out: Dict[str, int] = {}
        for key, val in raw.items():
            text = str(key)
            if prefix and not text.startswith(prefix):
                continue
            body = text[len(prefix):] if prefix else text
            if not body.endswith(tail):
                continue
            action = body[:-len(tail)]
            if action not in known:
                continue
            out[action] = out.get(action, 0) + cls._to_int(val, 0, minimum=0)
        return out

    @classmethod
    def _exp_progress(cls, snapshot: dict) -> dict:
        """算等级进度 {cur, exp, base, next, percent, remain}。

        百分比必须拿「本级起点」当分母：(exp - 本级门槛) / (下一级门槛 - 本级门槛)，
        口径与游戏 updateExpProgress 的 progress 一致。直接 exp / 下一级门槛
        会把游戏里显示的 73% 算成 91%。
        拿不到等级表时 percent 为 None，调用方降级成「只显示经验数」。
        """
        out = {"cur": cls._to_int(snapshot.get("level"), 1, minimum=1),
               "exp": cls._to_int(snapshot.get("exp"), 0, minimum=0),
               "base": 0, "next": None, "percent": None, "remain": None}
        table = cls._dict_rows(snapshot.get("levelTable"))
        if not table:
            return out
        cur, exp = out["cur"], out["exp"]
        base = next_req = None
        for row in table:
            lv = cls._to_int(row.get("level"), 0, minimum=0)
            req = cls._to_int(row.get("expReq"), 0, minimum=0)
            if lv <= cur:
                base = req if base is None else max(base, req)
            elif next_req is None:
                next_req = req
        out["base"] = int(base or 0)
        if next_req is None:
            return out                      # 已满级：没有下一级，不画进度条
        out["next"] = int(next_req)
        span = out["next"] - out["base"]
        pct = 100 if span <= 0 else round((exp - out["base"]) / span * 100)
        out["percent"] = max(0, min(100, int(pct)))
        out["remain"] = max(0, out["next"] - exp)
        return out

    @classmethod
    def _exp_fill(cls, percent: int, height: str = "6px",
                  color: str = _CYAN) -> dict:
        """进度条：底 + 填充。填充用 div 纯色（插件渲染器不吃渐变）。"""
        return {"component": "div", "props": {"style": {
            "position": "relative", "height": height, "borderRadius": "999px",
            "backgroundColor": "rgba(255,255,255,0.10)", "overflow": "hidden",
        }},
            "content": [{"component": "div", "props": {"style": {
                "width": f"{max(0, min(100, percent))}%", "height": "100%",
                "borderRadius": "999px", "backgroundColor": color,
            }}}]}

    # 经验来源：键名、顺序、名称全部照抄游戏前端 showExpDetail 的 actions 数组。
    # 第三项是游戏自己写的兜底默认值（`limit: e.xxxDailyLimit || 默认`），
    # 后端没给这个键时游戏照样显示一个数，这里跟着用同一套默认值，
    # 页面上永远 7 行，不会少行也不会显示 0。
    #   键 → 上限取自 expConfig.<键>DailyLimit（game_data.php 实测有前 5 个，
    #   charity_park / charity 后端根本没给，正好落到游戏自己的默认值 50 / 200）
    _EXP_SOURCES = (
        ("park", "停车", 100),
        ("ticket", "贴条", 60),
        ("report", "举报", 200),
        ("buy", "购车", 150),
        ("tradeIn", "以旧换新", 200),
        ("charity_park", "爱心停车", 50),
        ("charity", "爱心车位收益", 200),
    )

    # 游戏按百分比分三档色：满了红、过七成橙、其余绿
    _EXP_BAR_FULL = "#e94560"
    _EXP_BAR_HIGH = "#ff9800"
    _EXP_BAR_LOW = "#4caf50"

    @classmethod
    def _exp_bar_color(cls, percent: int) -> str:
        if percent >= 100:
            return cls._EXP_BAR_FULL
        if percent >= 70:
            return cls._EXP_BAR_HIGH
        return cls._EXP_BAR_LOW

    @classmethod
    def _exp_bar(cls, snapshot: dict, api: str) -> dict:
        """
        常显的一格：等级 Lv.N + 细进度条 + 本级经验/下一级门槛，可点。

        「等级」和「Lv.N」**同一排**，进度条和经验数在下面一行 ——
        和游戏顶栏一致（<span class="stat-label">等级</span><span>Lv.1</span>
        后面才跟 exp-progress-bar）。原先三样东西竖着排，这一格比称号高出一倍，
        名字行被顶得参差不齐。
        没有等级表时降级成「等级 Lv.N · 经验 X」，仍然可点开详情。
        """
        pg = cls._exp_progress(snapshot)
        head: List[dict] = [{
            "component": "div",
            "props": {"style": {"display": "inline-flex", "alignItems": "baseline",
                                "gap": "4px"}},
            "content": [
                cls._text("等级", {"fontSize": "11px",
                                  "color": "rgba(255,255,255,0.45)"}),
                cls._text(f"Lv.{pg['cur']}", {"fontSize": "12px",
                                              "fontWeight": "bold",
                                              "color": "#FFFFFF"}),
            ],
        }]
        if pg["percent"] is None:
            head.append(cls._text(f"经验 {ParkingBot._fmt_num(pg['exp'])}",
                                  {"fontSize": "11px",
                                   "color": "rgba(255,255,255,0.62)"}))
        else:
            head.append(cls._exp_fill(pg["percent"], "4px"))
            head.append(cls._text(
                f"{ParkingBot._fmt_num(pg['exp'])}/{ParkingBot._fmt_num(pg['next'])}",
                {"fontSize": "10px", "color": "rgba(255,255,255,0.45)",
                 "marginTop": "2px"}))
        return {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex", "flexDirection": "column",
                "alignItems": "flex-start", "gap": "3px",
                "padding": "3px 10px", "borderRadius": "12px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "backgroundColor": "rgba(255,255,255,0.03)",
                "cursor": "pointer", "userSelect": "none",
            }},
            "content": head,
            # params 里必须带 open=True。api_exp_detail 读的是 request.open，
            # 不传就用 Request 里的默认 False —— 那样每次点击都把展开状态设成
            # False（本来就是），页面毫无变化，看起来就是「点了没反应」。
            # 这和「收起」按钮的 open=False 是同一个接口的两个方向，别写成 {}。
            "events": {"click": {"api": api, "method": "post",
                                 "params": {"open": True}}},
        }

    @classmethod
    def _exp_panel(cls, snapshot: dict, api: str) -> dict:
        """
        展开后的详情面板，信息量照抄游戏弹窗。
        取不到等级表 / 经验配置时不编数字，只显示能确认的。

        api 由调用方传进来：面板自己拿不到 plugin_id，而「收起」按钮要打回
        同一个接口的 open=False。
        """
        pg = cls._exp_progress(snapshot)
        prog: List[dict] = [
            cls._text(f"当前等级：Lv.{pg['cur']}",
                      {"fontSize": "13px", "color": "#FFFFFF"}),
        ]
        if pg["next"] is None:
            prog.append(cls._text(
                f"累计经验：{ParkingBot._fmt_num(pg['exp'])}（已满级）",
                {"fontSize": "12px", "color": "rgba(255,255,255,0.62)",
                 "marginTop": "4px"}))
        else:
            prog.append(cls._text(
                f"总经验：{ParkingBot._fmt_num(pg['exp'])} / "
                f"下一级：{ParkingBot._fmt_num(pg['next'])}",
                {"fontSize": "12px", "color": "rgba(255,255,255,0.62)",
                 "marginTop": "4px"}))
            bar = cls._exp_fill(pg["percent"], "8px")
            bar["content"].append({"component": "div", "props": {"style": {
                "position": "absolute", "inset": "0", "display": "flex",
                "alignItems": "center", "justifyContent": "center",
                "fontSize": "10px", "fontWeight": "bold", "color": "#04222B",
            }}, "text": f"{pg['percent']}%"})
            prog.append({"component": "div",
                         "props": {"style": {"marginTop": "8px"}},
                         "content": [bar]})
            prog.append(cls._text(
                f"还需 {ParkingBot._fmt_num(pg['remain'])} 经验"
                f"升级到 Lv.{pg['cur'] + 1}",
                {"fontSize": "11px", "color": "rgba(255,255,255,0.45)",
                 "marginTop": "6px"}))

        rows: List[dict] = [{"component": "div", "props": {"style": {
            "padding": "10px 12px", "borderRadius": "12px",
            "backgroundColor": "rgba(255,255,255,0.035)",
            "border": "1px solid rgba(255,255,255,0.10)",
        }}, "content": prog}]

        # 上限为空时退回游戏自己的默认值，跟 showExpDetail 的 `|| 默认值` 一样 ——
        # 游戏弹窗恒 7 行，插件这边也恒 7 行，不会因为某个键缺了就少一行。
        cfg = snapshot.get("expConfig") or {}
        daily = snapshot.get("dailyExp") or {}
        if not cfg:
            cfg = cls._exp_config({})
        # 游戏这边 dailyExp 里没有今天这一项时是 `|| 0`，显示 0；
        # 但快照里 dailyExp 整个缺失是「没数据」，显示「—」——两者必须分得开。
        have_daily = bool(daily)
        items: List[dict] = []
        got_total = 0
        for key, label, _fallback in cls._EXP_SOURCES:
            limit = cls._to_int(cfg.get(key), 0, minimum=0)
            if limit <= 0:
                continue
            got = cls._to_int(daily.get(key), 0) if key in daily else None
            if got is not None:
                got_total += got
            pct = int(round(got / limit * 100)) if (got is not None and limit > 0) else 0
            row = {"component": "div", "props": {"style": {
                "display": "flex", "alignItems": "center",
                "justifyContent": "space-between", "gap": "8px"}},
                "content": [
                    cls._text(label, {"fontSize": "12px",
                                      "color": "rgba(255,255,255,0.92)"}),
                    # 缺这一项时显示「—/上限」而不是光一个「—」：
                    # 游戏那边恒显示「已得/上限」，光显示「—」等于把上限也吞了，
                    # 用户连「一天最多能拿多少」都看不到。
                    cls._text((f"—/{ParkingBot._fmt_num(limit)}" if got is None
                               else f"{ParkingBot._fmt_num(got)}"
                                    f"/{ParkingBot._fmt_num(limit)}"),
                              {"fontSize": "11px",
                               "color": "rgba(255,255,255,0.62)",
                               "fontVariantNumeric": "tabular-nums"}),
                ]}
            # 细条必须独占一行。直接 append 进上面那个
            # justify-content: space-between 的横排里，它就成了第三个 flex
            # 子项、去和文字抢宽度，被压成一条看不见的缝 —— 节点在页面上
            # 查得到，肉眼什么都没有。所以外层改竖排。
            line = {"component": "div",
                    "props": {"style": {"display": "flex",
                                        "flexDirection": "column",
                                        "marginTop": "7px"}},
                    "content": [row]}
            if got is not None:
                bar = cls._exp_fill(pct, "3px", cls._exp_bar_color(pct))
                bar["props"]["style"]["marginTop"] = "4px"
                line["content"].append(bar)
            items.append(line)

        # 这一整块必须在 for **外面**：放进去的话每循环一次就把当前全部行
        # 再追加一遍，页面上同一批来源会重复渲染 7 遍、且每遍的「总计」都不同
        # （150 / 240 / 240 …）。这类缩进错在语法上完全合法，只有渲染看得见。
        if items:
            head = ("今日经验获取（总计："
                    + ("—" if not have_daily else ParkingBot._fmt_num(got_total))
                    + "）")
            rows.append({"component": "div", "props": {"style": {
                "marginTop": "10px", "padding": "10px 12px",
                "borderRadius": "12px",
                "backgroundColor": "rgba(255,255,255,0.035)",
                "border": "1px solid rgba(255,255,255,0.10)",
            }},
                "content": [cls._text(
                    head, {"fontSize": "12px",
                           "color": "rgba(255,255,255,0.62)",
                           "marginBottom": "2px"})] + items})

        # 收起：_exp_open 记在服务端，不给出口的话面板会一直挂着，
        # 用户只能重启插件才能让它消失。
        rows.append({"component": "div",
                     "props": {"style": {"marginTop": "10px",
                                         "display": "flex",
                                         "justifyContent": "flex-end"}},
                     "content": [cls._action_btn("收起", api, {"open": False},
                                                 kind="trade")]})

        return {"component": "div", "props": {"style": {
            "marginTop": "10px", "padding": "12px", "borderRadius": "14px",
            "border": "1px solid rgba(34,211,238,0.22)",
            "backgroundColor": "rgba(34,211,238,0.06)",
        }}, "content": rows}

    # ---------------- 通知铃铛与面板 ----------------
    # notify_type 的取值与配色照抄游戏前端 showNotifications 里的 iconMap /
    # colorMap。图标没跟着搬：那是 15 个 <symbol> 的精灵表，搬进来是几百行
    # 内联 SVG，换来的只是一个可区分性完全由颜色承担的信息。
    # 用 emoji 更糟 —— 金币那颗 🪙 是 2019 年才进 Unicode 的，
    # Windows 默认字体没有字形，实测直接渲染成豆腐块 □。
    _NOTIF_TYPES = {
        "park": ("停车", "#4fc3f7"),
        "unpark": ("挪车", "#4fc3f7"),
        "ticket": ("贴条", "#e94560"),
        "report": ("举报", "#ff9800"),
        "levelup": ("升级", "#ffd700"),
        "charity": ("爱心", "#e94560"),
        "cash_add": ("收入", "#00e676"),
        "cash_sub": ("支出", "#e94560"),
        "bid_out": ("出价", "#ff9800"),
        "bid_win": ("得车", "#ffd700"),
        "bid_lost": ("落标", "#888888"),
        "buy_car": ("买车", "#00e676"),
        "cdk": ("兑换", "#ffd700"),
        "feedback_reply": ("反馈", "#4fc3f7"),
        "system": ("系统", "#7c5cfc"),
    }
    # 游戏以后加新类型时别静默丢内容：未知类型照样显示，只是没有配色。
    _NOTIF_FALLBACK = ("通知", "#4fc3f7")

    # 拉多少条：和游戏弹窗一致（perPage=50）。面板只渲染前 _NOTIF_SHOW 条，
    # 但**全部** rows 都参与算「N 条未读」，所以这里不砍 —— 砍了会让角标上限
    # 从 50 掉到更小的数，反而变成一个不准的数字。打开慢已经由并发解决
    # （见 _load_game_context 的 extra），不靠少拉条数。
    # category=all 必须留着，少带这个参数后端可能返回空数组（见 _load_notif_list）。
    _NOTIF_PAGE = 50
    # 面板最多渲染几条。一条通知卡片大约 60px 高，50 条就是近 3000px 的滚动，
    # 手机上要划十几屏才看得完，用户反馈「展开太长」。
    # 剩下的只在底部给一行提示，让用户知道去游戏网页看全部。
    _NOTIF_SHOW = 8

    @staticmethod
    def _notif_bell_icon() -> dict:
        """铃铛：抄游戏 index.html 里的 <symbol id="ico-bell">。"""
        return {"component": "div",
                "props": {"style": {"display": "flex", "flexShrink": "0"}},
                "html": '<svg viewBox="0 0 24 24" width="13px" height="13px">'
                        '<path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2zm6-6v-5c0-3.07-1.63-5.64'
                        '-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 '
                        '11v5l-2 2v1h16v-1l-2-2z" fill="#ffd700"/></svg>'}

    @classmethod
    def _notif_bell(cls, snapshot: dict, api: str) -> dict:
        """
        名字行里的铃铛，和游戏顶栏同一位置、同一颗图标。

        角标显示的是这一轮拉到的未读数；没拉���时沿用上一次的（见 _record_round）。
        """
        unread = cls._to_int(snapshot.get("notifUnread"), 0, minimum=0)
        inner: List[dict] = [
            {"component": "div",
             "props": {"style": {"display": "inline-flex", "alignItems": "center",
                                 "gap": "4px"}},
             "content": [cls._notif_bell_icon(), cls._text("通知", {})]},
        ]
        if unread > 0:
            inner[0]["content"].append(cls._text(
                "99+" if unread > 99 else str(unread),
                {"fontSize": "10px", "fontWeight": "700",
                 "color": "#FFFFFF", "backgroundColor": "#e94560",
                 "borderRadius": "8px", "lineHeight": "16px",
                 "minWidth": "16px", "textAlign": "center",
                 "padding": "0 4px", "marginLeft": "2px"}))
        return {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex", "alignItems": "center",
                "padding": "3px 10px", "borderRadius": "12px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "backgroundColor": "rgba(255,255,255,0.03)",
                "cursor": "pointer", "userSelect": "none",
            }},
            "content": inner,
            "events": {"click": {"api": api, "method": "post",
                                 "params": {"open": True}}},
        }

    @classmethod
    def _notif_panel(cls, snapshot: dict, api: str) -> dict:
        """
        展开后的通知列表。**只读**：不提供标记已读，也不提供清空。

        清空会删掉游戏侧的通知且不可恢复，而插件页弹不出 confirm 二次确认框，
        误按一下没法挽回；要清回游戏网页上点，那里既有确认框也有确认按钮。
        """
        rows = cls._dict_rows(snapshot.get("notifications"))
        unread = sum(1 for r in rows if not r.get("read"))
        # 只渲染最近 _NOTIF_SHOW 条，剩下的用一行提示交代清楚。
        # 未读数仍按**全部** rows 算，不然角标会和列表对不上。
        shown = rows[:cls._NOTIF_SHOW]
        hidden = len(rows) - len(shown)
        head: List[dict] = [cls._text(
            "消息通知",
            {"fontSize": "13px", "fontWeight": "bold", "color": "#FFFFFF"})]
        if unread > 0:
            head.append(cls._text(
                f"{unread} 条未读",
                {"fontSize": "11px", "color": "#e94560", "marginLeft": "6px"}))
        head.append(cls._text(
            "清空请回游戏网页",
            {"fontSize": "11px", "color": "rgba(255,255,255,0.45)",
             "marginLeft": "auto"}))

        if not rows:
            body: List[dict] = [cls._text(
                "暂无通知", {"fontSize": "12px",
                            "color": "rgba(255,255,255,0.62)",
                            "marginTop": "12px", "textAlign": "center"})]
        else:
            body = []
            for item in shown:
                label, color = cls._NOTIF_TYPES.get(
                    str(item.get("type") or ""), cls._NOTIF_FALLBACK)
                line = [{"component": "div",
                         "props": {"style": {
                             "flex": "1 1 auto", "minWidth": "0",
                             "display": "flex", "alignItems": "flex-start",
                             "gap": "8px"}},
                         "content": [
                             cls._text(label, {"fontSize": "11px", "fontWeight": "bold",
                                               "color": color, "flexShrink": "0"}),
                             {"component": "div",
                              "props": {"style": {"flex": "1 1 auto", "minWidth": "0"}},
                              "content": [
                                  cls._text(str(item.get("message") or ""),
                                            {"fontSize": "12px",
                                             "color": "rgba(255,255,255,0.92)"}),
                                  cls._text(cls._notif_time(item.get("time")),
                                            {"fontSize": "11px",
                                             "color": "rgba(255,255,255,0.45)",
                                             "marginTop": "2px"}),
                              ]},
                         ]}]
                if not item.get("read"):
                    # 未读：整行左边一条亮色竖条，扫一眼就知道还有几条没看
                    line.insert(0, {"component": "div", "props": {"style": {
                        "width": "3px", "alignSelf": "stretch",
                        "borderRadius": "2px", "backgroundColor": color}}})
                body.append({"component": "div", "props": {"style": {
                    "display": "flex", "alignItems": "stretch", "gap": "8px",
                    "marginTop": "10px", "padding": "8px 10px",
                    "borderRadius": "10px",
                    "backgroundColor": ("rgba(255,255,255,0.05)"
                                        if not item.get("read")
                                        else "rgba(255,255,255,0.02)")}},
                    "content": line})

        # 被截掉的部分只留一行字，说清楚去哪儿看全部 —— 静默截断会让人以为
        # 游戏侧没发过通知。
        more: List[dict] = []
        if hidden > 0:
            more.append(cls._text(
                f"仅显示最近 {cls._NOTIF_SHOW} 条，还有 {hidden} 条，回游戏网页看全部",
                {"fontSize": "11px", "color": "rgba(255,255,255,0.45)",
                 "marginTop": "10px", "textAlign": "center"}))

        # 底部两个按钮：全部已读（写游戏侧，但不删东西，和游戏弹窗一样不带
        # 二次确认）+ 收起。没有未读时「全部已读」置灰且不挂事件 ——
        # 页面上按下去是真的没反应，不空发一次请求。
        buttons: List[dict] = []
        if unread > 0:
            buttons.append(cls._action_btn(
                "全部已读", api, {"open": True, "read_all": True}, kind="buy"))
        buttons.append(cls._action_btn("收起", api, {"open": False},
                                       kind="trade"))

        return {"component": "div", "props": {"style": {
            "marginTop": "10px", "padding": "12px", "borderRadius": "14px",
            "border": "1px solid rgba(255,215,0,0.22)",
            "backgroundColor": "rgba(255,215,0,0.06)",
        }}, "content": [
            {"component": "div",
             "props": {"style": {"display": "flex", "alignItems": "center",
                                 "gap": "4px"}},
             "content": head},
            *body,
            *more,
            {"component": "div",
             "props": {"style": {"marginTop": "12px", "display": "flex",
                                 "gap": "8px", "justifyContent": "flex-end"}},
             "content": buttons},
        ]}

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
            # 机器可读的车位类型，页面据此画「设为爱心 / 恢复私家」切换按钮。
            # 必须在「空车位提前 return」之前放进初始字典，否则空闲位不带该字段、
            # 按钮在页面上就冒不出来（新老快照的判据也靠它）。
            "charity": charity,
            # 贴条按钮要拿车位 ID 去调 ticket.php，快照里必须带上
            "spotId": spot.get("id"),
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
        parker = cls._user_of(state, car.get("userId"))
        parked_at = cls._parked_at(spot)
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
            gain = min(mins * cls._to_int(definition.get("earnPerMin"), 0), cap)
            out["earn"] = gain
            # 贴条：别人停在我私家车位够久，我可以贴条罚他。
            # 罚款就是这个车位预估收益按分成比例切给我的一部分
            # （游戏规则页：贴条时停车收益分成，车位主人得 TICKET_OWNER_PERCENT%）。
            # 自己停自己车位上的车不给贴条按钮 —— 游戏那边也只在 !isMyCar 时给。
            if not out["mine"]:
                percent = cls._to_int(rules.get("TICKET_OWNER_PERCENT"), 90,
                                      minimum=0, maximum=100)
                min_ticket = cls._to_int(rules.get("MIN_TICKET_TIME"), 5, minimum=0)
                out["ticketFine"] = gain * percent // 100
                out["ticketReady"] = mins >= min_ticket
                out["ticketWait"] = max(0, min_ticket - mins)
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
        owner = ParkingBot._user_of(state, owner_id)
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {
                                         "model": "auto_ticket",
                                         "label": "自动贴条",
                                         "hint": "默认关闭。打开后：别人停在我私家车位上够规定时长、"
                                                 "且预计罚款达到下方阈值时自动贴条。"
                                                 "同一辆车在同一个车位只贴一次，它开走后再回来才会重新计算。"
                                                 "会真实产生罚款收入，但可能招致对方报复，谨慎开启。",
                                     }}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch",
                                     "props": {
                                         "model": "prefer_charity",
                                         "label": "优先停爱心车位赚经验",
                                         "hint": "默认关闭。打开后：定时任务优先把空闲车停到**别人**的"
                                                 "爱心车位上赚经验（等级详情面板里能看到"
                                                 "「爱心停车」那一项的今日进度）。"
                                                 "经验达到当日上限后车会自动挪回来，"
                                                 "转去普通车位赚金币。"
                                                 "注意：停在爱心车位不产金币，期间没有金币收入。",
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
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {"component": "VTextField",
                                     "props": {
                                         "model": "ticket_min_fine",
                                         "label": "达到多少金币才自动贴条",
                                         "placeholder": "0",
                                         "hint": "比较的是车位卡片上显示的「贴条收益」。"
                                                 "填 0 表示不设门槛，所有停够时长、可以贴的车位都贴。"
                                                 "想只贴高价值车位就填个几百。",
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

        value 传 dict 时原样返回（style 不再生效）：金币图标这类「图标 + 数字」
        的复合节点自带样式，可以直接塞进原本放文本的位置，不必为它单开一套接口。
        """
        if isinstance(value, dict):
            return value
        return {"component": "div", "text": str(value), "props": {"style": style}}

    @staticmethod
    def _coin(value: Any, size: int = 14, color: str = _GOLD, bold: bool = False,
              label: str = "", style: Optional[Dict[str, Any]] = None,
              align: str = "center") -> dict:
        """
        「金币图标 + 数字」一行，和游戏车市卡片里的 svgIco('coin') 同一颗。

        size 是图标边长（px），数字比图标小 1px，这样一排金额下来基线看着齐。
        align 给 "center" 用于卡片居中排版，给 "left" 用于左对齐的字段式布局。
        """
        row: Dict[str, Any] = {
            "display": "flex",
            "alignItems": "center",
            "gap": "4px",
            "justifyContent": "center" if align == "center" else "flex-start",
        }
        row.update(style or {})
        text_style: Dict[str, Any] = {"fontSize": f"{max(11, size - 1)}px", "color": color}
        if bold:
            text_style["fontWeight"] = "bold"
        content: List[dict] = []
        if label:
            content.append(ParkingBot._text(label, dict(text_style)))
        content.append({"component": "span",
                        "props": {"style": {"display": "flex", "flexShrink": "0"}},
                        "html": _COIN_SVG.replace("@SIZE@", f"{size}px")})
        content.append(ParkingBot._text(value, text_style))
        return {"component": "div", "props": {"style": row}, "content": content}

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
        """单个数字指标块：内嵌面 + 左侧语义色条 + 大号彩值。

        色义只落在「色条 + 数值」两处。整块底色改成中性内嵌面之后，四个指标块
        不再各顶一大片紫底 —— 底色一多就互相打架，颜色反而认不出来。

        borderRadius / minWidth / margin 保持原值不动：空快照用例要求页面上
        至少存在一个 14px 圆角的节点，这里是最稳的那个来源。
        底色是实色 rgba：渐变在插件页渲染不出来。
        """
        return {
            "component": "div",
            "props": {"style": {
                "flex": "1 1 0",
                "minWidth": "128px",
                "margin": "0 8px 8px 0",
                "padding": "12px 14px",
                "borderRadius": "14px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "backgroundColor": "rgba(255,255,255,0.03)",
                "display": "flex",
                "alignItems": "center",
                "gap": "10px",
            }},
            "content": [
                # 左侧语义色条：把「这块是哪个指标」从底色搬到一根竖条上
                {"component": "div",
                 "props": {"style": {
                     "width": "3px",
                     "height": "34px",
                     "borderRadius": "2px",
                     "flex": "0 0 auto",
                     "backgroundColor": color,
                 }}},
                {"component": "div",
                 "props": {"style": {
                     "display": "flex",
                     "flexDirection": "column",
                     "gap": "3px",
                     "minWidth": "0",
                 }},
                 "content": [
                     ParkingBot._text(label, {"fontSize": "11px", "fontWeight": "700",
                                              "letterSpacing": "0.5px",
                                              "color": "rgba(255,255,255,0.62)"}),
                     # 数字等宽：跳数时宽度不抖（tabular-nums 只影响数字字形）
                     ParkingBot._text(value, {"fontSize": "23px", "fontWeight": "700",
                                              "color": color, "lineHeight": "1.15",
                                              "fontVariantNumeric": "tabular-nums"}),
                 ]},
            ],
        }

    @staticmethod
    def _card(title: str, subtitle: str, content: List[dict],
              accent: str = _CYAN, badges: Optional[List[dict]] = None) -> dict:
        """
        统一的信息卡片外壳。

        标题用 div 渲染而不依赖 VCardTitle 插槽：Vuetify 这类组件在本插件
        页面里只渲染出空壳，内容并不会填进去。卡片外壳同理改用 div，
        少一层 VCard + VCardText，样式也更可控。

        标题不再写死青色：青色在页面上只留给「可点 / 选中」，区块之间改用左侧
        一枚语义圆点来区分（车位青 / 车市金 / 车库紫 / 统计绿）。圆点占掉
        「8px 圆点 + 8px 间隔」这 16px，副标题也跟着缩进同样距离，两行左缘对齐。
        内距从「14 14 4」改成四边等距 16px —— 原来的底距比其它三边少 10px，
        卡片底部看着总是贴得太紧。

        badges 挂在标题文字后面那一行的右端（``marginLeft: auto`` 顶到右边），
        用来放「我的车位 收益+5%」这种状态标。不给就不渲染，布局和以前一样。
        """
        head: List[dict] = []
        if title:
            head.append({
                "component": "div",
                "props": {"style": {"display": "flex", "alignItems": "center",
                                    "gap": "8px", "marginBottom": "3px"}},
                "content": [
                    {"component": "span",
                     "props": {"style": {"width": "8px", "height": "8px",
                                         "borderRadius": "999px", "flex": "0 0 auto",
                                         "backgroundColor": accent}}},
                    ParkingBot._text(title, {"fontSize": "15px", "fontWeight": "700",
                                             "color": "rgba(255,255,255,0.92)",
                                             "letterSpacing": "0.3px"}),
                    # 状态标顶到标题行右端。flex 行里放第三个子项是可以的
                    # （横排有富余宽度时不会像「来源行塞进度条」那样被压没），
                    # 但它自带 marginLeft:auto，窄屏时也会先让给标题而不是被挤扁。
                    *(badges or []),
                ],
            })
        if subtitle:
            head.append(ParkingBot._text(subtitle, {
                "fontSize": "12px", "color": "rgba(255,255,255,0.62)",
                "lineHeight": "1.55", "marginBottom": "12px",
                "paddingLeft": "16px" if title else "0"}))
        return {
            "component": "div",
            "props": {"style": {
                "marginBottom": "12px",
                "padding": "16px",
                "borderRadius": "16px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "backgroundColor": "rgba(255,255,255,0.045)",
            }},
            "content": [*head, *content],
        }

    @staticmethod
    def _meta_pill(label: str, value: Any) -> dict:
        """概览卡里的元信息胶囊：小灰键 + 亮一档的值，同行排布。"""
        return {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex",
                "alignItems": "center",
                "gap": "5px",
                "padding": "3px 9px",
                "borderRadius": "999px",
                "backgroundColor": "rgba(255,255,255,0.03)",
                "border": "1px solid rgba(255,255,255,0.10)",
                "fontSize": "11px",
            }},
            "content": [
                ParkingBot._text(label, {"color": "rgba(255,255,255,0.32)"}),
                ParkingBot._text(value, {"color": "rgba(255,255,255,0.62)"}),
            ],
        }

    @staticmethod
    def _meta_row(pairs: List[Tuple[str, Any]]) -> dict:
        """一排元信息胶囊（执行周期 / 车辆 / 数据时间）。"""
        return {
            "component": "div",
            "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                "gap": "6px", "marginTop": "10px"}},
            "content": [ParkingBot._meta_pill(key, value) for key, value in pairs],
        }

    @staticmethod
    def _humanize_gap(minutes: int) -> str:
        """把「距今多少分钟」说成人话，**不含「更新」二字**。

        60 分钟以内直接报分钟；超过就换算成小时并带上零头分钟
        （1595 → 「26 小时 35 分钟前」，而不是让用户心算「1595 分钟是多少小时」）。

        概览卡角标和车库工具条共用这一份换算。原先两边各写一套：角标那套只会
        说「N 分钟前」，插件挂机一天就在肩上顶着「1595 分钟前更新」——
        而「数据很旧」恰恰是这个角标最该起作用的场景。
        """
        minutes = ParkingBot._to_int(minutes, 0)
        if minutes < 1:
            return "刚刚"
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours, rest = minutes // 60, minutes % 60
        return f"{hours} 小时 {rest} 分钟前" if rest else f"{hours} 小时前"

    def _freshness_text(self, snapshot: dict) -> str:
        """概览卡右肩的「数据有多新」，读不出来就返回空串（整颗角标不渲染）。

        和车库工具条的 _age_text 区别只在「缺数据时给什么」：那边降级成
        「数据时间未知」，这边宁可不显示，也不给「未知」这种占位。
        量词换算两者共用 _humanize_gap，别再各写一套。
        """
        raw = str(snapshot.get("time") or "").strip()
        if not raw:
            return ""
        try:
            taken = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return ""
        minutes = int((self._now_naive() - taken).total_seconds() // 60)
        if minutes < 0:            # 时间在未来：宁可不显示，也不显示负数
            return ""
        return f"数据 {ParkingBot._humanize_gap(minutes)}更新"

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
                    ParkingBot._coin(total, size=14,
                                     color=_GREEN if total >= 0 else _PINK,
                                     bold=True, align="left",
                                     style={"marginTop": "3px"}),
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
        24 小时收益折线图。

        用内联 SVG 画折线 + 渐变面积，数据点用绝对定位的 div 画（不用 <circle>：
        绘图区靠 preserveAspectRatio="none" 横向拉满，圆会被压成椭圆），
        不用图表库 —— 插件页是声明式 JSON，引不了 ApexCharts，SVG 在任何宿主上都能出来。

        绘图区上层再铺 24 条隐形热区，每小时一条：宿主渲染器 PageRender 对 props 是
        v-bind 全量透传、没有白名单，所以 title 会原样落成 <div title="…">，由浏览器
        弹原生提示。JSON 模式下没有 JS 执行口、也没有注入 CSS 的口子，自绘 tooltip
        做不出来，这是唯一能给逐小时数据加悬停提示的办法。
        """
        # _to_int 兜底脏数据（"x" / inf / nan 都回落 0），保证下面不会出现 NaN / Infinity。
        values = [max(0, ParkingBot._to_int(hours.get(str(h)), 0)) for h in range(24)]
        peak = max(values) if values else 0
        total = sum(values)
        earned_hours = sum(1 for v in values if v > 0)
        peak_hour = values.index(peak) if peak > 0 else -1

        # Y 轴 5 档整数刻度：step = ceil(峰值 / 4)，顶端刻度 4×step 必 > 峰值，
        # 折线不会贴着绘图区顶部；峰值极低时自动降档（如 9 → 12/9/6/3/0）。
        # 峰值 0 时刻度全 0、折线贴底。
        if peak > 0:
            step = max(1, -(-peak // 4))
            scale_top = step * 4
            ticks = [scale_top - step * i for i in range(5)]
        else:
            step = 0
            scale_top = 0
            ticks = [0, 0, 0, 0, 0]

        # 绘图区 96px，顶部留 6px 余量；SVG 与百分比坐标共用同一套 0–100 比例。
        plot_h = 96
        span = (plot_h - 6) / plot_h * 100.0        # 93.75

        def y_pct(value: int) -> float:
            """收益值 → 绘图区内的纵向百分比（0 在顶、100 在底）。"""
            if scale_top <= 0:
                return 100.0
            return 100.0 - (value / scale_top) * span

        xs = [i / 23 * 100.0 for i in range(24)]
        ys = [y_pct(v) for v in values]
        pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))
        area_d = ("M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))
                  + f" L {xs[-1]:.2f},100 L {xs[0]:.2f},100 Z")
        grid = "".join(
            f'<line x1="0" y1="{y_pct(t):.2f}" x2="100" y2="{y_pct(t):.2f}" '
            f'stroke="rgba(255,255,255,0.07)" stroke-width="1" '
            f'vector-effect="non-scaling-stroke"/>'
            for t in ticks)
        svg = (
            '<svg viewBox="0 0 100 100" preserveAspectRatio="none" '
            'style="position:absolute;inset:0;width:100%;height:100%;overflow:visible">'
            '<defs><linearGradient id="parkingHourArea" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#00E676" stop-opacity="0.45"/>'
            '<stop offset="1" stop-color="#00E676" stop-opacity="0"/>'
            '</linearGradient></defs>'
            f'{grid}'
            f'<path d="{area_d}" fill="url(#parkingHourArea)" stroke="none"/>'
            f'<polyline points="{pts}" fill="none" stroke="#00E676" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round" '
            f'vector-effect="non-scaling-stroke"/>'
            '</svg>')

        dots = [{"component": "div", "props": {"style": {
            "position": "absolute", "left": f"{x:.2f}%", "top": f"{y:.2f}%",
            "width": "6px", "height": "6px", "borderRadius": "50%",
            "transform": "translate(-50%, -50%)",
            "backgroundColor": (_GOLD if (peak > 0 and i == peak_hour) else _GREEN),
        }}} for i, (x, y) in enumerate(zip(xs, ys))]

        # 24 条隐形热区：title 文案逐字保持，cursor:help，带 height 键（三要件都被测试锁定）。
        hotspots = [{"component": "div", "props": {
            "title": (f"{hour:02d}:00 — 收益 {ParkingBot._fmt_num(value)} 金币"
                      if value else f"{hour:02d}:00 — 无收益"),
            "style": {"flex": "1 1 0", "height": "100%", "cursor": "help"},
        }} for hour, value in enumerate(values)]

        plot_children: List[dict] = [
            {"component": "span", "props": {"style": {
                "position": "absolute", "inset": "0", "display": "block"}}, "html": svg},
            *dots,
        ]
        # 峰值常显胶囊。水平夹取到 [6%, 94%]：贴边时改同向对齐，不居中，
        # 免得贴右边缘时溢出卡片；峰值点在绘图区上 1/3 时改放到点下方。
        if peak > 0:
            px, py = xs[peak_hour], ys[peak_hour]
            left = min(94.0, max(6.0, px))
            tx = "0%" if px < 6.0 else ("-100%" if px > 94.0 else "-50%")
            ty = "58%" if py < 100.0 / 3.0 else "-135%"
            plot_children.append({"component": "div", "props": {"style": {
                "position": "absolute", "left": f"{left:.2f}%", "top": f"{py:.2f}%",
                "transform": f"translate({tx}, {ty})", "whiteSpace": "nowrap",
                "padding": "2px 8px", "borderRadius": "10px", "fontSize": "11px",
                "fontWeight": "bold", "color": _GOLD,
                "backgroundColor": "rgba(255,215,0,0.16)",
                "border": "1px solid rgba(255,215,0,0.5)",
            }}, "content": [ParkingBot._text(
                f"{peak_hour:02d}:00 · {ParkingBot._fmt_num(peak)} 金币", {})]})

        plot_children.append({"component": "div", "props": {"style": {
            "position": "absolute", "inset": "0", "display": "flex"}},
            "content": hotspots})

        plot = {"component": "div",
                "props": {"style": {"position": "relative", "height": f"{plot_h}px"}},
                "content": plot_children}

        x_labels = [{"component": "div", "props": {"style": {
            "fontSize": "11px", "color": "rgba(255,255,255,0.48)"}},
            "text": f"{h:02d}:00"} for h in range(0, 24, 4)]

        # 图下常显摘要 —— 移动端唯一能逐日读到的信息入口。纯 _text，不带 title / cursor，
        # 免得干扰热区计数。
        summary = (f"峰值 {peak_hour:02d}:00 · {ParkingBot._fmt_num(peak)} 金币 ｜ "
                   f"有收益 {earned_hours} 小时 ｜ 合计 {ParkingBot._fmt_num(total)} 金币"
                   if total > 0 else "本日无收益")

        return {
            "component": "div",
            "props": {"style": {"marginTop": "4px"}},
            "content": [
                # 图例行（右上角图例「收益」）
                {"component": "div", "props": {"style": {
                    "display": "flex", "alignItems": "center", "justifyContent": "flex-end",
                    "gap": "6px", "marginBottom": "4px"}},
                 "content": [
                     {"component": "span", "props": {"style": {
                         "display": "block", "width": "10px", "height": "3px",
                         "borderRadius": "2px", "backgroundColor": _GREEN}}},
                     ParkingBot._text("收益", {"fontSize": "11px",
                                               "color": "rgba(255,255,255,0.48)"})]},
                # 主行：Y 轴刻度列 + 右列（绘图区 + X 轴）
                {"component": "div", "props": {"style": {"display": "flex", "gap": "6px"}},
                 "content": [
                     {"component": "div", "props": {"style": {
                         "position": "relative", "width": "46px", "flexShrink": "0",
                         "height": f"{plot_h}px"}},
                      "content": [{"component": "div", "props": {"style": {
                          "position": "absolute", "right": "2px",
                          "top": f"{y_pct(t):.2f}%", "transform": "translateY(-50%)",
                          "fontSize": "11px", "color": "rgba(255,255,255,0.48)"}},
                          "text": ParkingBot._fmt_num(t)} for t in ticks]},
                     {"component": "div", "props": {"style": {"flex": "1 1 0"}},
                      "content": [
                          plot,
                          {"component": "div", "props": {"style": {
                              "display": "flex", "justifyContent": "space-between",
                              "marginTop": "4px"}}, "content": x_labels}]},
                 ]},
                # 摘要行
                ParkingBot._text(summary, {"fontSize": "11px",
                                           "color": "rgba(255,255,255,0.62)",
                                           "marginTop": "6px"}),
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
                            {"fontSize": "13px", "opacity": "0.7"})], accent=_GREEN)

        def day_row(d: str) -> Tuple[int, int, int]:
            b = daily.get(d) or {}
            earned = self._to_int(b.get("earned"), 0)
            cost = (self._to_int(b.get("spent"), 0) + self._to_int(b.get("diff"), 0))
            return earned, cost, earned - cost

        def day_fined(d: str) -> int:
            """当天贴条罚款收入。老存档没有 fined 字段，一律按 0 算。"""
            b = daily.get(d) or {}
            if not isinstance(b, dict):
                return 0
            return self._to_int(b.get("fined"), 0)

        current = self._day if self._day in daily else days[0]
        bucket = daily.get(current) or {}
        # bucket 本身和 bucket["hours"] 都得校验类型：落盘数据被改坏时
        # "坏字符串" 是真值，`or {}` 兜不住，后面 hours.get() 直接 AttributeError。
        if not isinstance(bucket, dict):
            bucket = {}
        hours = bucket.get("hours")
        if not isinstance(hours, dict):
            hours = {}
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
                 self._stat("其中贴条", day_fined(current), _TICKET_C),
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
                 self._stat("其中贴条", sum(day_fined(d) for d in month_days), _TICKET_C),
                 self._stat("本月支出", m_cost, _PINK),
                 self._stat("本月净收", m_earned - m_cost, _GREEN),
                 self._stat("本月日均", avg, _CYAN),
             ]},
            # 小时柱状图
            self._card(f"{current} · 每小时收益",
                       f"当日共 {self._to_int(bucket.get('rounds'), 0)} 轮执行，"
                       f"买车 {self._to_int(bucket.get('bought'), 0)} 次、"
                       f"换车 {self._to_int(bucket.get('traded'), 0)} 次",
                       [self._hour_chart(hours)], accent=_GREEN),
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
                    ["left", "right", "right", "right", "right"])], accent=_GREEN),
            # 买卖汇总
            self._card(
                "本月买卖",
                f"买车 {buys} 次、换车 {trades} 次，"
                f"合计花出 {m_cost} 金币",
                [self._text(
                    f"买入 {sum(self._to_int((daily[d] or {}).get('spent'), 0) for d in month_days)} 金"
                    f"　·　换车补差价 "
                    f"{sum(self._to_int((daily[d] or {}).get('diff'), 0) for d in month_days)} 金",
                    {"fontSize": "14px", "color": _PINK})], accent=_GREEN,
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
                             ["left", "center", "center", "right"])], accent=_GREEN))
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
        # 地址是要拼进 src="..." 的，游戏侧一旦返回带引号的字符串就能把属性
        # 提前闭合、把 onerror 之类的内容注进页面。这里先把引号实体化。
        safe_url = str(url).replace('"', "&quot;").replace("'", "&#39;")
        html = (
            f'<img src="{safe_url}" loading="lazy" decoding="async" '
            f'style="width:{size}px;height:{size}px;object-fit:contain;" '
            f'onerror="this.style.display=\'none\';'
            f'this.nextElementSibling.style.display=\'flex\';">'
            f'<span style="display:none;width:{size}px;height:{size}px;align-items:center;'
            f'justify-content:center;font-size:{size // 2}px;opacity:0.45;">{fallback}</span>'
        )
        return {"component": "span", "props": {"style": {"lineHeight": "0"}}, "html": html}

    @staticmethod
    def _ticket_row(spot: dict, api: str) -> Optional[dict]:
        """
        车位卡片底部的贴条区：预计罚款 + 贴条按钮。

        样式照游戏 .btn-ticket（玫红底白字）。停不够 MIN_TICKET_TIME 时按钮压暗
        并且不挂事件 —— 页面上的灰按钮是真点不动的，好过按下去被服务端驳回。
        """
        if spot.get("ticketFine") is None:
            return None
        ready = bool(spot.get("ticketReady"))
        wait = ParkingBot._to_int(spot.get("ticketWait"), 0)
        btn = {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex", "alignItems": "center", "gap": "4px",
                "marginTop": "8px", "padding": "5px 12px", "fontSize": "11px",
                "fontWeight": "bold", "borderRadius": "14px", "userSelect": "none",
                "whiteSpace": "nowrap",
                "cursor": "pointer" if ready else "not-allowed",
                "color": "#FFFFFF" if ready else "rgba(255,255,255,0.5)",
                "backgroundColor": ("rgba(233,69,96,0.85)" if ready
                                    else "rgba(233,69,96,0.22)"),
            }},
            "content": [
                {"component": "span",
                 "props": {"style": {"display": "flex", "flexShrink": "0"}},
                 "html": _TICKET_SVG.replace("@SIZE@", "12px")},
                ParkingBot._text("贴条", {}),
            ],
        }
        if ready:
            btn["events"] = {"click": {
                "api": api, "method": "post",
                "params": {"spot_id": spot.get("spotId")}}}
        hint = ParkingBot._text(
            f"再等 {wait} 分钟可贴条" if not ready else "已到可贴条时长",
            {"fontSize": "10px", "opacity": "0.6", "marginTop": "4px"})
        fine = ParkingBot._coin(
            ParkingBot._fmt_num(spot.get("ticketFine")), size=12, color=_GOLD,
            bold=True, label="贴条收益",
            style={"marginTop": "4px", "justifyContent": "center"})
        return {"component": "div",
                "props": {"style": {"display": "flex", "flexDirection": "column",
                                    "alignItems": "center"}},
                "content": [fine, btn, hint]}

    @classmethod
    def _spot_bonus_badges(cls, snapshot: dict) -> List[dict]:
        """
        「我的车位」标题右端的状态标：我这个车位的场景收益加成。

        抄游戏网页版的画法（js/modules/parking.js 的 myBonusHint + 每个车位上的
        bonus-torch）：标题旁写 `收益+N%`，金色。没换场景（加成 0）就**不画** ——
        游戏那边也是 myBonusPct > 0 才显示，没加成的停车场挂一个「收益+0%」纯属噪音。

        这个加成的作用是：别人把车停进我的车位时，他每分钟多拿 N%。
        所以它是「我这个车位对别人有没有吸引力」的指标 —— 页面把它挂在
        标题上，用户一眼就能看出自己的车位在别人眼里值多少。
        """
        pct = cls._to_int((snapshot or {}).get("spot_bonus_pct"), 0, minimum=0)
        if pct <= 0:
            return []
        badge = cls._badge(f"收益+{pct}%", _GOLD)
        # marginLeft:auto —— _badge 是通用徽章不带定位，这里在标题那一行的
        # flex 容器里把它顶到右端（贴着卡片右内边距）。
        badge["props"]["style"]["marginLeft"] = "auto"
        return [badge]

    @staticmethod
    def _spot_grid(spots: List[dict], api: str, charity_api: str = "") -> dict:
        """
        我的车位：照游戏本体的画法，两列网格。

        被别人占用时按游戏那样显示：车图、停车人（带头像）、已停时长与预估收益；
        空闲位是斜纹底 + 角标。斜纹用 repeating-linear-gradient 直接画，不需要额外拉图。

        charity_api 是「设为爱心 / 恢复私家」接口路径，给了才在空闲卡上画切换按钮
        （默认空串，老调用方不传也不会炸）。
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
                # 爱心 / 私家切换按钮：只在快照带 "charity" 字段（新版快照）时渲染。
                # 老快照没有该字段，若给它也画按钮，整页 events.click 计数会从 4 变 5，
                # 踩中 tests/test_regress.py 的「顶部有 4 个可点按钮」断言，所以必须
                # 用 `"charity" in spot` 兜住；占用卡一律不给按钮（游戏规则：只能操作空车位）。
                toggle: List[dict] = []
                if "charity" in spot and charity_api:
                    is_charity = bool(spot.get("charity"))
                    toggle.append({
                        "component": "div",
                        "props": {"style": {
                            "display": "inline-flex", "alignItems": "center",
                            "justifyContent": "center", "gap": "4px",
                            "padding": "9px 16px", "minHeight": "40px",
                            "borderRadius": "14px", "fontSize": "12px",
                            "fontWeight": "bold", "whiteSpace": "nowrap",
                            "userSelect": "none", "border": "none", "cursor": "pointer",
                            # 照抄游戏：设为爱心 = 实底粉 + 白字（6.1:1 过 AA）；
                            # 恢复私家 = 半透明白底 + 淡白字。
                            "color": ("rgba(255,255,255,0.7)" if is_charity else "#FFFFFF"),
                            "backgroundColor": ("rgba(255,255,255,0.2)" if is_charity
                                                else "rgba(233,69,96,0.7)"),
                        }},
                        "content": [ParkingBot._text(
                            "恢复私家车位" if is_charity else "设为爱心车位", {})],
                        "events": {"click": {
                            "api": charity_api, "method": "post",
                            "params": {"spot_id": spot.get("spotId"),
                                       "action": "unset" if is_charity else "set"}}},
                    })
                    toggle.append(ParkingBot._text(
                        "重新按分钟产金币" if is_charity else "不产金币 · 仅贡献经验",
                        {"fontSize": "10px", "color": "rgba(255,255,255,0.45)",
                         "marginTop": "6px"}))
                cards.append({
                    "component": "div",
                    "props": {"style": {
                        "flex": "1 1 46%",
                        "minWidth": "150px",
                        "margin": "0 8px 8px 0",
                        "padding": "12px",
                        "borderRadius": "14px",
                        # 车位卡统一等高：空闲卡与占用卡共用同一个 minHeight。
                        # 数值用 dump_page.py 导出真实页面量出来的 295px（贴条面板那张
                        # 占用卡的自然高度），而不是效果图里的 279 —— 279 是 effect 图
                        # content-box 下的结果，套到真实代码上会让带贴条面板的卡超出到
                        # 295、和空闲卡不等高。显式声明 boxSizing 是因为宿主的 CSS reset
                        # 可能把全局设成 border-box，那样 minHeight 会变成总高；声明后
                        # 两种宿主下所有车位卡都等于 295px。
                        "boxSizing": "border-box",
                        "minHeight": "295px",
                        "display": "flex",
                        "flexDirection": "column",
                        "border": ("1px solid rgba(255,77,109,0.28)" if charity
                                   else "1px solid rgba(34,211,238,0.24)"),
                        # 底色用实色打底：插件页的渲染器不吃渐变，只写斜纹的话
                        # 空闲位会退化成和被占用位几乎一样的背景，一眼看不出能不能停。
                        "backgroundColor": "rgba(255,255,255,0.035)",
                        "backgroundImage": _HATCH,
                    }},
                    "content": [
                        badge,
                        # 角标留在顶部，「空闲」大字 + 切换按钮这一组垂直居中。
                        {"component": "div", "props": {"style": {
                            "flex": "1 1 0", "display": "flex",
                            "flexDirection": "column", "alignItems": "center",
                            "justifyContent": "center"}},
                         "content": [
                             ParkingBot._text("空闲", {
                                 "fontSize": "17px", "fontWeight": "bold",
                                 "color": "rgba(255,255,255,0.55)",
                                 "textAlign": "center", "marginTop": "18px",
                                 # marginBottom 不能省：这一组是 column flex，
                                 # 「空闲」与切换按钮是紧邻的两个 item，marginBottom 为 0
                                 # 时两者间距正好 0，宿主 CSS reset 把 line-height 收到 1
                                 # 的话，中文字形会略微溢出行盒、被不透明的按钮盖住下半截。
                                 "marginBottom": "12px"}),
                             *toggle,
                         ]},
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
                detail = ParkingBot._text(
                    f"贡献经验 {ParkingBot._to_int(spot.get('exp'), 0)} exp",
                    {"fontSize": "13px", "color": _PURPLE, "fontWeight": "bold",
                     "marginTop": "2px"})
            else:
                detail = ParkingBot._coin(
                    ParkingBot._fmt_num(spot.get("earn")), size=14, color=_GOLD,
                    bold=True, label="预估收益", style={"marginTop": "2px"})

            # 贴条：别人停我车位够久就能罚他一笔。爱心车位、自己停自己车不给。
            ticket_row = ParkingBot._ticket_row(spot, api)

            cards.append({
                "component": "div",
                "props": {"style": {
                    "flex": "1 1 46%",
                    "minWidth": "150px",
                    "margin": "0 8px 8px 0",
                    "padding": "12px",
                    "borderRadius": "14px",
                    # 与空闲卡共用同一套等高写法（详见空闲卡处的注释）。
                    "boxSizing": "border-box",
                    "minHeight": "295px",
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
                    detail,
                    # 贴条区只在该有的时候才拼进去：直接塞 None 会让渲染器
                    # 在卡片底部吐出一行字面量 "None"。
                    *([ticket_row] if ticket_row else []),
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
        # 逐项判断而不是「缓存对象是不是 None」：_page_cache 可能是残缺的
        # （只装了 snapshot 那一项）。这里只把**缺的那几项**从库里补齐，
        # 有缓存的照旧走内存 —— 补齐粒度太粗会让统计页在下一轮之前一直空白。
        cache = self._page_cache if isinstance(self._page_cache, dict) else {}
        snapshot = cache.get("snapshot")
        history = cache.get("history")
        daily = cache.get("daily")
        if not isinstance(snapshot, dict) or not isinstance(history, list) \
                or not isinstance(daily, dict):
            stored_h = self.get_data("history") or []
            stored_d = self.get_data("daily") or {}
            if not isinstance(history, list):
                history = stored_h if isinstance(stored_h, list) else []
            if not isinstance(daily, dict):
                daily = stored_d if isinstance(stored_d, dict) else {}
        # 存档被写坏时（比如历史升级后结构变了）不能让整个详情页崩成 500，
        # 退化成空数据即可，页面仍能看到框架和区块。
        if not isinstance(snapshot, dict):
            stored_s = self.get_data("snapshot")
            snapshot = stored_s if isinstance(stored_s, dict) else {}
            logger.warning("[不可躺自动抢车位] 账户快照格式异常，已按空数据展示")
        if not isinstance(history, list):
            history = []
        if not isinstance(daily, dict):
            daily = {}
        # 元素级也一起清一遍：容器对不代表元素对。history 里混进一个字符串，
        # 渲染路径上 i.get() 就会抛，整页 500；daily 的桶同理。
        history = [row for row in history if isinstance(row, dict)]
        daily = {k: v for k, v in daily.items() if isinstance(v, dict)}
        self._page_cache = {"snapshot": snapshot, "history": history, "daily": daily}
        return snapshot, history, daily

    @classmethod
    def _today_earned(cls, history: List[dict]) -> int:
        """
        统计今天的挪车收益。

        历史条目带 day（YYYY-MM-DD），按天筛选比解析展示用的 time 字符串可靠。

        「今天」必须按 MoviePilot 设定时区算：写 day 用的是 _now_naive()（设定
        时区），这里若用 datetime.now()（系统时区），两者差几小时就会让每天有
        大半天「今日收益」恒为 0。
        """
        today = cls._now_naive().strftime("%Y-%m-%d")
        total = 0
        for item in history:
            if not isinstance(item, dict):
                continue
            if str(item.get("day") or "") != today:
                continue
            try:
                total += int(item.get("earned") or 0)
            except (TypeError, ValueError, OverflowError):
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
        """
        在事件回调里跑异步逻辑：有事件循环就建任务，否则起后台线程。

        协程内部抛异常时必须吞掉并记日志：任务没人 await，异常只会以
        「Task exception was never retrieved」的形式丢在事件循环里，
        用户那边一个字都收不到，还以为指令没生效。
        """
        async def _guard():
            try:
                await coro
            except Exception as err:
                logger.error(f"[不可躺自动抢车位] 指令执行异常：{err}")

        guarded = _guard()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(guarded)
            return
        threading.Thread(target=lambda: asyncio.run(guarded), daemon=True).start()

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
        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            self._reply_data(sender, "❌ 获取状态失败", err)
            return
        user_id = state.get("userId")
        rules = self._settings_of(state)
        min_duration = self._to_int(rules.get("MIN_PARK_DURATION"), 10, minimum=1)
        target = max(self._park_minutes, min_duration)
        ordered = self._ordered_my_cars(state, cars_map)
        mine = self._user_of(state, user_id)
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
        _, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            self._reply_data(sender, "❌ 获取状态失败", err)
            return
        ordered = self._ordered_my_cars(state, cars_map)
        car_id = self._resolve_car_ref(args[0], ordered)
        if not car_id:
            self._reply_data(sender, "❌ 没找到这辆车", "先发 /车库 查看序号与车辆 ID。")
            return
        result = await self.api_unpark(CarMoveRequest(car_id=car_id))
        await self._after_action(result, sender, "挪车")

    async def _cmd_buy(self, args: List[str], sender: dict) -> None:
        """购买车辆。"""
        if not args:
            self._reply_data(sender, "用法：/买车 车型ID",
                             "车型 ID 见插件详情页的车市区块。")
            return
        result = await self.api_buy_car(CarBuyRequest(car_id=args[0]))
        await self._after_action(result, sender, "买车")

    async def _cmd_trade(self, args: List[str], sender: dict) -> None:
        """以旧换新。"""
        if len(args) < 2:
            self._reply_data(sender, "用法：/换车 旧车ID 新车型ID",
                             "旧车必须是空闲车辆；两个 ID 见插件详情页。")
            return
        result = await self.api_trade_in(CarTradeRequest(old_car_id=args[0], new_car_id=args[1]))
        await self._after_action(result, sender, "换车")

    async def _cmd_flash(self, args: List[str], sender: dict) -> None:
        """抢购特卖车。"""
        if not args:
            self._reply_data(sender, "用法：/抢购 场次ID",
                             "场次 ID 见插件详情页的「限时 / 限量特卖」区块。")
            return
        result = await self.api_buy_flash(FlashBuyRequest(sale_id=args[0]))
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
                "path": "/refresh_status",
                "endpoint": self.api_refresh_status,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即刷新状态",
                "response_model": ApiResult,
                "description": "重新拉一次游戏状态并刷新页面快照，不挪车也不停车",
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
            {
                "path": "/shop_filter",
                "endpoint": self.api_shop_filter,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "车市分类筛选",
                "response_model": ApiResult,
                "description": "记录车市页当前选中的车型分类；分类名必须来自车型表，不认外部输入",
            },
            {
                "path": "/trade_pick",
                "endpoint": self.api_trade_pick,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "开始或取消以旧换新",
                "response_model": ApiResult,
                "description": "记下要换成的目标车型，页面据此列出可用的空闲车；传空 car_id 表示取消",
            },
            {
                "path": "/park",
                "endpoint": self.api_park,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即把空闲车停到他人车位",
                "response_model": ApiResult,
                "description": "按车辆序号或实例 ID 调用游戏 park.php 停车；"
                              "车位自动在他人空余私家车位里挑一个",
            },
            {
                "path": "/ticket",
                "endpoint": self.api_ticket,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "给占我车位的车贴条",
                "response_model": ApiResult,
                "description": "按车位 ID 调用游戏 ticket.php 贴条；须为自己的私家车位、"
                              "车已停够 MIN_TICKET_TIME 且不是自己的车",
            },
            {
                "path": "/charity_spot",
                "endpoint": self.api_charity_spot,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换车位类型（爱心 / 私家）",
                "response_model": ApiResult,
                "description": "只能操作自己的空闲车位；设为爱心后该车位不产金币、改为贡献经验",
            },
            {
                "path": "/title",
                "endpoint": self.api_set_title,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换佩戴的称号",
                "response_model": ApiResult,
                "description": "按称号 ID 调用游戏 titles.php 切换佩戴；传 0 取消佩戴",
            },
            {
                "path": "/exp_detail",
                "endpoint": self.api_exp_detail,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "展开经验值详情",
                "response_model": ApiResult,
                "description": "记录经验值面板是否展开；渲染器随后重新拉取页面并带上详情",
            },
            {
                "path": "/notif_detail",
                "endpoint": self.api_notif_detail,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "展开消息通知列表",
                "response_model": ApiResult,
                "description": "展开时拉取游戏 notifications.php 列表并缓存进快照；只读，不改游戏侧数据",
            },
            {
                "path": "/title_pick",
                "endpoint": self.api_title_pick,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "展开或收起称号选择器",
                "response_model": ApiResult,
                "description": "记录称号选择器是否展开；渲染器随后重新拉取页面并带上选择器",
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
        saved: Dict[str, Any] = {"tab": tab}
        # day 只在「金币统计」页有意义；非空且格式合法才认
        day = str(getattr(request, "day", "") or "").strip()
        if day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            self._day = day
            saved["day"] = day
        await self._save_view(saved)
        return _result({"success": True, "name": dict(self._TABS)[tab]})

    async def _save_view(self, patch: Dict[str, Any]) -> None:
        """
        记住详情页的视图状态（当前页签、面板展开与否、车市筛选…）。

        **不落盘，只改内存，然后排一个防抖后台写。**

        为什么不再同步写：诊断版 v1.46.1 实测，async_save_data("view", …)
        要 31~4813 毫秒（读只要 1~4 毫秒，慢的全是写），而这步在每次点击
        的同步路径上。浏览器实测 POST 692~2847 毫秒，和这个写耗时完全对得上。
        写为什么慢至今没查清，但让点击不等它就行。

        为什么合并也不会丢数据：内存是全量真相源，后台任务醒来时写的是
        **那一刻**的完整 view，不是各次增量的累加 —— 连点五次页签也只写一次，
        而且写出去的一定是最新状态。

        必须在内存里做合并，不能靠读改写：读虽然只要几毫秒，但每次点击都
        多一次数据库往返；而且写既然是异步的，读到的可能已经不是最新的了。
        """
        if self._view_mem is None:
            self._view_mem = {}
        if self._view_dirty is None:
            self._view_dirty = {}
        self._view_mem.update(patch)
        self._view_dirty.update(patch)
        self._schedule_view_flush()

    def _schedule_view_flush(self) -> None:
        """排一个防抖后台落盘。已有任务在排队就不重复排，它醒来时会写最新状态。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 不在事件循环里（正常路径不会走到）：没法排后台任务。
            # 直接放弃这次落盘 —— view 只是 UI 偏好，内存里仍然是对的。
            return
        task = self._view_flush_task
        if task is not None:
            try:
                # 必须是**同一个**事件循环里未完成的任务才算数。跨循环的
                # （插件重启、或者测试里反复 asyncio.run）一律当成已死 ——
                # 否则会一直以为「已经有任务在排队」，新变更永远排不出去。
                if not task.done() and task.get_loop() is loop:
                    return
            except Exception:
                pass

        async def _later() -> None:
            try:
                await asyncio.sleep(self._VIEW_DEBOUNCE)
            except asyncio.CancelledError:
                return
            await self._flush_view()

        self._view_flush_task = loop.create_task(_later())

    async def _flush_view(self) -> None:
        """把内存里的 view 写回 data。没有待落盘的变更就直接返回。"""
        if not self._view_dirty or self._view_mem is None:
            return
        payload = dict(self._view_mem)
        self._view_dirty = {}
        try:
            await self.async_save_data("view", payload)
        except Exception as err:
            # 写失败要把变更放回去重试，否则这次改动就彻底丢了
            self._view_mem.update(payload)
            self._view_dirty.update(payload)
            logger.warning(f"[不可躺自动抢车位] 保存详情页状态失败：{err}")

    async def flush_view_now(self) -> None:
        """
        立刻把待落盘的 view 写掉，不等防抖。

        给测试用（asyncio.run() 一结束事件循环就关，后台任务会被丢掉），
        以及插件停用时兜底落一次。
        """
        await self._flush_view()

    async def _patch_snapshot_title(self, title_id: int, title_name: str) -> bool:
        """
        就地改掉快照里的称号字段，省掉整份重拉。

        返回 False 表示快照还没进内存（进程刚起且没打开过详情页），此时该
        退回整份重拉，否则页面会一直显示旧称号。
        """
        cache = self._page_cache or {}
        snapshot = cache.get("snapshot") or {}
        if not isinstance(snapshot, dict) or not snapshot:
            return False
        color = _GOLD
        name = title_name
        for item in self._dict_rows(snapshot.get("titles")):
            if self._to_int(item.get("id"), 0) != title_id:
                continue
            color = str(item.get("color") or _GOLD)
            name = name or str(item.get("name") or "")
            break
        if not title_id:
            name, color = "", _GOLD
        snapshot["title_id"] = title_id
        snapshot["title_name"] = name
        snapshot["title_color"] = color
        cache["snapshot"] = snapshot
        self._page_cache = cache
        try:
            await self.async_save_data("snapshot", snapshot)
        except Exception as err:
            # 落盘失败只影响下次打开时的显示，本次的返回值照样是成功
            logger.warning(f"[不可躺自动抢车位] 保存称号快照失败：{err}")
        return True

    async def api_park(self, request: ParkRequest) -> ApiResult:
        """
        手动把一辆空闲车停到他人车位。

        和定时停车共用游戏接口 park.php {userCarId, spotId}（游戏里也是这个）。
        车位的挑法沿用 _park_idle_cars：只在「别人的、空着的、私家车位」里随机挑，
        避免长期压在同一个车位主人身上被针对；爱心车位不选，那儿不产生金币。

        刻意不受「最多同时停放车辆数」限制 —— 那是定时任务的风险刹车，
        手动点一下是明确操作，由用户自己判断。按钮旁边会显示当前已停/上限。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        token = str(getattr(request, "car_id", "") or "").strip()
        if not token:
            return _result({"success": False, "error": "缺少车辆 ID"})

        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        user_id = state.get("userId")
        # 与 _resolve_car_ref 同一套口径：先按游戏车库顺序排出我的车，
        # 序号对不上再当实例 ID 用。它返回的是车辆 ID，再回表取车辆本身。
        # state.php 返回的是全服车辆，取用时必须按 userId 过滤。
        ordered = self._ordered_my_cars(state, cars_map)
        car_id = self._resolve_car_ref(token, ordered)
        if not car_id:
            return _result({"success": False, "error": "车辆不存在或不属于当前账号"})
        car = next((c for c in self._dict_rows(state.get("userCars"))
                    if str(c.get("id")) == str(car_id)
                    and str(c.get("userId")) == str(user_id)), None)
        if not car:
            return _result({"success": False, "error": "车辆不存在或不属于当前账号"})
        if str(car.get("status")) != "idle":
            return _result({"success": False, "error": "只有空闲车辆能停出去"})

        spots = [s for s in self._dict_rows(state.get("parkingSpots"))
                 if not s.get("carInSpot")
                 and s.get("spotType") != "charity"
                 and str(s.get("ownerId")) != str(user_id)]
        if not spots:
            return _result({"success": False, "error": "当前没有可停的他人空余私家车位"})
        spot = random.choice(spots)

        result = await self._post(client, "park.php",
                                  {"userCarId": car.get("id"), "spotId": spot.get("id")})
        if not result or not result.get("success"):
            return _result({"success": False,
                            "error": (result or {}).get("error") or "停车失败"})
        rate = self._earn_per_min(car, cars_map)
        await self._refresh_snapshot(client, "手动停车")
        note = (f"{self._car_name(car, cars_map)} 已停到他人车位，"
                f"每分钟收益 {rate} 金")
        self._set_result(note)
        logger.info(f"[不可躺自动抢车位] {note}")
        return _result({"success": True, "car": self._car_name(car, cars_map),
                        "rate": rate, "spotId": spot.get("id")})

    async def api_ticket(self, request: TicketRequest) -> ApiResult:
        """
        给停在我私家车位上的别人的车贴条。

        游戏规则（js/modules/parking.js 的 ticketCar + 规则页）：
        只能贴自己的私家车位、爱心车位不行、车位上得有车、车得停够
        MIN_TICKET_TIME 分钟；罚款是该车位预估收益按 TICKET_OWNER_PERCENT 分给我
        的那一份。这里五道都在后端再校验一遍 —— 页面上的置灰只是提示。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        spot_key = str(getattr(request, "spot_id", "") or "").strip()
        if not spot_key:
            return _result({"success": False, "error": "缺少车位 ID"})

        # 和 api_unpark / api_park 共用同一个并行封装：state.php ‖ 车型表 ‖ 市场预取。
        # 贴条原先是老写法 —— 自己串行 resolve_cookie → state.php，事后又补拉车型表。
        # 单请求约 1.1 秒时总耗时 8.8 秒，是挪车/停车（1.1 秒）的八倍。
        # 走这个封装后，车型表和市场预取在第一跳就备好，后面全都命中缓存。
        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        user_id = state.get("userId")
        spot = next((s for s in self._dict_rows(state.get("parkingSpots"))
                     if str(s.get("id")) == spot_key), None)
        if not spot:
            return _result({"success": False, "error": "车位不存在"})
        # 原样透传车位 ID，不做字符串化：游戏那边发的是数字（ticketCar(spot.id)），
        # 服务端按数字取值。先用字符串比较定位车位，命中后回传原始值。
        raw_spot_id = spot.get("id")
        if str(spot.get("ownerId")) != str(user_id):
            return _result({"success": False, "error": "只能给自己的车位贴条"})
        if spot.get("spotType") == "charity":
            return _result({"success": False, "error": "爱心车位不能贴条"})
        car_in_spot = spot.get("carInSpot")
        if not car_in_spot:
            return _result({"success": False, "error": "车位上没有车"})
        car = next((c for c in self._dict_rows(state.get("userCars"))
                    if str(c.get("id")) == str(car_in_spot)), None)
        if car is None:
            return _result({"success": False, "error": "车位上的车已不在"})
        if str(car.get("userId")) == str(user_id):
            return _result({"success": False, "error": "自己停的车不用贴条"})

        rules = self._settings_of(state)
        min_ticket = self._to_int(rules.get("MIN_TICKET_TIME"), 5, minimum=0)
        parked_at = self._parked_at(spot)
        mins = max(0, int((self._now_naive() - parked_at).total_seconds() / 60)) \
            if parked_at else 0
        if mins < min_ticket:
            return _result({"success": False,
                            "error": f"停放不足 {min_ticket} 分钟，还差 {min_ticket - mins} 分钟"})

        result = await client.post_json(f"{self.API_BASE}/ticket.php",
                                       json={"spotId": raw_spot_id})
        if not result or not result.get("success"):
            return _result({"success": False,
                            "error": (result or {}).get("error") or "贴条失败"})
        # 罚款是真金白银的收入，并进当天统计（和挪车收益同一口径）。
        #
        # 字段名要兼容：游戏 2026-10 改过 ticket.php 的返回结构，旧版给的是
        # `fine`，新版给的是 `ownerEarn` —— 指同一笔钱（贴条给自己车位带来的金币），
        # 只是文案从「罚款 N 金」红字改成了「获得 N 金币」金字。
        # 游戏前端 JS 里两个版本并存可见。只读一个的后果是静默的：
        # 读不到就落成 0，贴条看着成功、统计页却一直是 0，不报任何错。
        raw_earn = result.get("ownerEarn")
        if raw_earn is None:
            raw_earn = result.get("fine")
        fine = self._to_int(raw_earn, 0, minimum=0)
        exp = self._to_int(result.get("expGained"), 0, minimum=0)
        self._shop_fined += fine
        # 立刻落盘，不等下一轮定时任务。
        #
        # 原先只累加、不记账，等下一次 run_once 里的 _record_round 才写进 daily。
        # 那有两个后果：一是跨天贴的会记到第二天（日期错），二是插件在这期间
        # 重启的话 _shop_fined 随内存一起没了，这笔钱永久丢失 —— 而统计页上的
        # 「其中贴条」正是读这个字段。
        #
        # 这里**不能**再调一次 _record_round：紧接着的刷新逻辑里还会调一遍，
        # 两次叠起来白白多花两轮网络往返（_record_round 每次约 2.2 秒）。
        # 记账统一交给下面那次刷新做，_shop_fined 会被它一并收走。
        fresh = await client.get_json(f"{self.API_BASE}/state.php")
        if fresh and fresh.get("success"):
            await self._record_round(client, fresh, fresh.get("userId"),
                                     0, 0, 0, record_history=False)
        # 车名直接用第一跳就取到的 cars_map，不要为了拼一句通知再拉一次车型表
        note = f"贴条 {self._car_name(car, cars_map)}，罚款 {fine} 金"
        if exp:
            note += f"，经验 +{exp}"
        self._set_result(note)
        logger.info(f"[不可躺自动抢车位] {note}")
        return _result({"success": True, "fine": fine, "expGained": exp})

    async def api_charity_spot(self, request: CharitySpotRequest) -> ApiResult:
        """
        把空闲车位设为爱心车位 / 恢复成私家车位。

        游戏规则（charity_spot.php）：只能操作自己的车位，且车位必须是空的
        （车位上停着车时游戏返回失败）。爱心车位不产金币，改成给车位主人贡献经验。
        页面上的按钮只是入口，这里在服务端再校验一遍。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        spot_key = str(getattr(request, "spot_id", "") or "").strip()
        if not spot_key:
            return _result({"success": False, "error": "缺少车位 ID"})

        client, state, _cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        user_id = state.get("userId")
        spot = next((s for s in self._dict_rows(state.get("parkingSpots"))
                     if str(s.get("id")) == spot_key), None)
        if not spot:
            return _result({"success": False, "error": "车位不存在"})
        # 与 api_ticket 同口径：先用字符串比较定位车位，命中后原样透传原始 ID。
        raw_spot_id = spot.get("id")
        if str(spot.get("ownerId")) != str(user_id):
            return _result({"success": False, "error": "只能操作自己的车位"})
        action = str(getattr(request, "action", "") or "").strip()
        if action not in ("set", "unset"):
            return _result({"success": False, "error": "未知的车位类型操作"})
        if spot.get("carInSpot"):
            return _result({"success": False, "error": "车位上有车，无法切换"})

        result = await client.post_json(
            f"{self.API_BASE}/charity_spot.php",
            json={"spotId": raw_spot_id, "action": action})
        if not result or not result.get("success"):
            return _result({"success": False,
                            "error": (result or {}).get("error") or "操作失败"})

        # 同样是先并行备好 state/车型表/市场预取，再动手 —— 详见 api_ticket 处的说明。
        # 这里原本是「串行拉 state → 操作 → _refresh_snapshot(再拉一遍)」，
        # 单请求 1.1 秒时要 5.6 秒。
        fresh = await client.get_json(f"{self.API_BASE}/state.php")
        if fresh and fresh.get("success"):
            await self._record_round(client, fresh, fresh.get("userId"),
                                     0, 0, 0, record_history=False)
        # 再按本次操作乐观更新 —— 顺序反了会被上面那份仍旧是旧值的快照盖回去。
        if await self._patch_spot_charity(raw_spot_id, action == "set"):
            logger.warning(
                "[不可躺自动抢车位] 切换后 state.php 立刻读回来的车位类型与本次操作不符"
                "（服务端写入延迟），已按本次操作显示；下一轮会以真实状态为准。")
        note = ("已设为爱心车位，其他玩家停车将贡献经验" if action == "set"
                else "已恢复为私家车位，重新按分钟产出金币")
        self._set_result(note)
        logger.info(f"[不可躺自动抢车位] {note}")
        return _result({"success": True, "action": action})

    async def _patch_spot_charity(self, spot_id: Any, is_charity: bool) -> bool:
        """
        把快照里某个车位的爱心标记按「我们刚做了什么」改过来。

        为什么需要它：charity_spot.php 立刻返回 success，但**紧接着**那次
        state.php 有时还读回旧值（服务端写入延迟）—— 游戏网页版那边之所以
        显示得出爱心，是因为用户之后刷新了整页，等写入生效才看到。
        插件这一轮拉完就落快照，于是页面停在「私家」，用户看着像按钮没生效。

        顺序很关键：**先**用服务端状态重���整份快照，**再**覆盖这一个车位。
        反过来的话，那份仍旧是旧值的快照会把乐观更新又盖回「私家」。

        返回 True 表示服务端读回来的值和本次操作不一致（写入延迟），
        值得记一条日志留证。下一轮定时任务会拿真实状态覆盖回来，自愈。
        """
        cache = self._page_cache if isinstance(self._page_cache, dict) else {}
        snapshot = cache.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            stored = await self.async_get_data("snapshot")
            snapshot = stored if isinstance(stored, dict) else {}
            cache = dict(cache)
        disagree = False
        kind = "爱心车位" if is_charity else "私家车位"
        spots = snapshot.get("spots")
        if isinstance(spots, list):
            for spot in spots:
                if not isinstance(spot, dict) \
                        or str(spot.get("spotId")) != str(spot_id):
                    continue
                disagree = bool(spot.get("charity")) != is_charity
                spot["charity"] = is_charity
                spot["kind"] = kind
                # label 形如「私家车位 3」，只换前缀，编号留着
                old = str(spot.get("label") or "")
                num = old.rsplit(" ", 1)[-1] if " " in old else ""
                spot["label"] = f"{kind} {num}".strip()
        snapshot["spots"] = spots
        cache["snapshot"] = snapshot
        self._page_cache = cache
        await self.async_save_data("snapshot", snapshot)
        return disagree

    async def api_exp_detail(self, request: ExpDetailRequest) -> ApiResult:
        """
        展开 / 收起经验值详情面板。页面渲染器随即重拉，把面板带上或收掉。

        和称号选择器（api_title_pick）是同一套路：声明式 JSON 没有 JS 执行口，
        弹不出模态框，只能翻转一个视图状态，让重渲染时带上展开内容。
        """
        # 不落盘：展开状态只在本次打开期间有效（v1.48.0 起）。
        # 用户要求「每次打开插件页面默认都是收起来的」，所以这里连 _save_view
        # 都不调 —— 点一下铃铛/等级格**一次数据库写都不发生**，是瞬时响应。
        self._exp_open = bool(getattr(request, "open", False))
        return _result({"success": True, "open": self._exp_open})

    async def api_notif_detail(self, request: NotifDetailRequest) -> ApiResult:
        """
        展开 / 收起通知面板，以及「全部已读」。

        展开时才去拉列表：每轮都已经把未读数带回来了，用户不看通知的时候
        再去取这一整页纯属浪费。收起时把缓存的列表清掉，下次点开重新拉，
        免得隔半天再打开看到的是旧数据。

        只读不记账，_record_round 不会被调用，市场预取的结果没人取。
        不关掉预取的话，每次点铃铛都白发 3 个请求。
        """
        self._notif_open = bool(getattr(request, "open", False))
        # 不落盘，和 _exp_open 一个道理（v1.48.0）：每次打开页面都默认收起。
        if not self._notif_open:
            await self._patch_snapshot_notifications([])
            return _result({"success": True, "open": False})

        # prefetch=False：通知面板只读不记账，_record_round 不会被调用，
        # 市场预取的结果没人取。不关掉的话每次点铃铛都白发 3 个请求。
        #
        # extra 把通知列表的请求也塞进同一个 gather：notifications.php 和
        # state.php 互不依赖，原来是串行两个往返，现在并行一个往返就够。
        # 依据是实测的单请求延迟（_probe_concurrency.py：5 个并发只读 1.05 秒 /
        # 串行 3.92 秒，即单请求约 1.1 秒）—— 串行两个约 2.2 秒，并行约 1.1 秒。
        # 「全部已读」那条路不需要列表，就别发。
        _read_all = bool(getattr(request, "read_all", False))
        client, state, _cars, err, items = await self._load_game_context(
            prefetch=False,
            extra=None if _read_all else (lambda c: self._load_notif_list(c)))
        if not state:
            return _result({"success": False, "error": err})

        if _read_all:
            return await self._notif_read_all(client)

        items = list(items or [])
        await self._patch_snapshot_notifications(items)
        return _result({"success": True, "open": True, "count": len(items)})

    async def _notif_read_all(self, client: Any) -> dict:
        """
        全部标记已读（notifications.php action=read_all）。

        成功判据要放宽到「响应里没有 error 字段」：游戏自己的 markAllNotifRead
        是 `await apiCall(...)` 之后直接清角标，**从不检查返回值**，所以这个端点
        成功时未必带 success:true。只认 success 会让一次明明生效的操作看起来
        像「点了没反应」。
        无论哪种情况都把真实响应写进日志 —— 这种「按了没反应」的问题，
        没有日志就永远只能靠猜。
        """
        try:
            result = await client.post_json(
                f"{self.API_BASE}/notifications.php",
                json={"action": "read_all"})
        except Exception as err:
            reason = f"标记已读失败：{err}"
            logger.warning(f"[不可躺自动抢车位] {reason}")
            self._set_result(reason)
            return _result({"success": False, "error": reason})
        logger.info(f"[不可躺自动抢车位] 全部已读响应：{repr(result)[:200]}")
        # 成功判据只看「有没有 error」和「success 是不是显式 false」。
        # 千万别把 message 当错误 —— 实测这个端点成功时返回的是
        # {'success': True, 'message': '全部标记已读'}，message 是**成功提示**。
        # 当成错误判据的话，每次成功都被当成失败、快照一个字都不写，
        # 表现和「点了没反应」一模一样。
        err_text = ""
        failed = result is None
        if isinstance(result, dict):
            err_text = str(result.get("error") or "")
            failed = bool(err_text) or result.get("success") is False
        if failed:
            reason = f"全部已读失败：{err_text or '游戏接口未接受'}"
            logger.warning(f"[不可躺自动抢车位] {reason}")
            self._set_result(reason)
            return _result({"success": False, "error": reason})
        rows = [dict(item, read=True) if isinstance(item, dict) else item
                for item in ((self._page_cache or {}).get("snapshot") or {})
                .get("notifications") or []]
        await self._write_notifications(rows, 0)
        self._set_result("所有通知已标记为已读")
        logger.info("[不可躺自动抢车位] 所有通知已标记为已读")
        return _result({"success": True, "open": True, "unread": 0})

    async def _patch_snapshot_notifications(self, items: List[dict]) -> None:
        """把通知列表塞进快照。"""
        await self._write_notifications(items, None)

    async def _write_notifications(self, items: List[dict],
                                   unread: Optional[int]) -> None:
        """
        写通知列表 + 未读数，**内存缓存和库都要改**。

        只改库是不够的 —— 这正是「全部已读点了没反应」的原因：
        页面渲染走 _page_data()，它优先读 self._page_cache（进程内缓存，
        只有冷启动才查库）。只 async_save_data 的话，库里已经是 0 了，
        页面下一次渲染读的还是内存里那份 10，角标和列表纹丝不动。
        换称号的 _patch_snapshot_title 早就同时改了两处，这里照它的模式来。

        unread 传 None 表示按列表现算；传具体值则强制用它
        （全部已读后列表可能正好是空的，不能让「算」把 0 又变回别的数）。
        """
        cache = self._page_cache or {}
        snapshot = cache.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            stored = await self.async_get_data("snapshot")
            if not isinstance(stored, dict) or not stored:
                return
            snapshot = stored
        snapshot["notifications"] = list(items)
        snapshot["notifUnread"] = (
            self._to_int(unread, 0, minimum=0) if unread is not None
            else sum(1 for r in items if not r.get("read")))
        cache["snapshot"] = snapshot
        self._page_cache = cache
        await self.async_save_data("snapshot", snapshot)

    async def api_title_pick(self, request: TitlePickRequest) -> ApiResult:
        """展开 / 收起称号选择器。页面渲染器随即重拉，把选择器带上或收掉。"""
        self._title_open = bool(getattr(request, "open", False))
        await self._save_view({"title_open": self._title_open})
        return _result({"success": True, "open": self._title_open})

    async def api_set_title(self, request: TitleSetRequest) -> ApiResult:
        """
        切换佩戴的称号。

        titleId 直接透传给游戏的 titles.php —— 能不能戴这个称号由游戏判定，
        插件不自己比对列表，免得两边的解锁条件走偏。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        title_id = self._to_int(getattr(request, "title_id", 0), 0, minimum=0)
        client, state, _, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        result = await client.post_json(f"{self.API_BASE}/titles.php",
                                       json={"titleId": title_id})
        if not result or not result.get("success"):
            return _result({"success": False,
                            "error": (result or {}).get("error") or "切换称号失败"})
        # 换称号只影响 title_id / title_name / title_color 三个字段，POST 的
        # 返回值里已经有新称号的名字，颜色从快照里的称号列表查得到。为了这三项
        # 整份重拉 state + 特卖 + 称号，是 4 次约 4.4 秒的往返，纯浪费。
        name = str(result.get("titleName") or "")
        if not await self._patch_snapshot_title(title_id, name):
            await self._refresh_snapshot(client, "切换称号")
        self._title_open = False
        await self._save_view({"title_open": False})
        self._set_result(f"称号已切换为：{name}" if name else "已取消佩戴称号")
        logger.info(f"[不可躺自动抢车位] 称号切换为：{name or '无'}")
        return _result({"success": True, "titleId": title_id, "name": name})

    async def api_shop_filter(self, request: ShopFilterRequest) -> ApiResult:
        """车市顶部分类胶囊：记住当前只看哪个分类。"""
        snapshot = (self._page_cache or {}).get("snapshot") or {}
        known = self._shop_categories(snapshot.get("catalog") or [])
        category = str(getattr(request, "category", "") or "").strip()
        # 只认快照里真实存在的分类名；空串表示看全部车型
        if category and category not in known:
            return _result({"success": False, "error": f"未知车型分类：{category}"})
        self._cat = category
        await self._save_view({"cat": category})
        return _result({"success": True, "category": category or "全部"})

    async def api_trade_pick(self, request: TradePickRequest) -> ApiResult:
        """
        车市卡片「以旧换新」的第一步：记下要换成哪辆车，页面随即列出可用的空闲车。

        和游戏一致：卡片上只有一个「以旧换新」入口，具体拿哪辆空闲车去换交给
        下一步 —— 名下可能同时有好几辆空闲车，替用户猜一辆就是替他花钱。
        传空 car_id 表示放弃这次换新。
        """
        car_key = str(getattr(request, "car_id", "") or "").strip()
        if not car_key:
            self._trade_for = ""
            await self._save_view({"trade_for": ""})
            return _result({"success": True, "canceled": True})
        snapshot = (self._page_cache or {}).get("snapshot") or {}
        level = self._to_int(snapshot.get("level"), 1, minimum=1)
        target = next((c for c in self._dict_rows(snapshot.get("catalog"))
                       if str(c.get("id")) == car_key
                       and self._to_int(c.get("level"), 1, minimum=1) <= level), None)
        if not target:
            return _result({"success": False, "error": "该车型不可换（不存在或等级不足）"})
        self._trade_for = car_key
        await self._save_view({"trade_for": car_key})
        return _result({"success": True, "name": target.get("name", "")})

    async def api_buy_car(self, request: CarBuyRequest) -> ApiResult:
        """
        购买车辆。

        等级、金币、车辆上限三项都在后端复核一遍。页面上的置灰只是提示，
        真正的规则必须由后端把关，否则绕过界面就能买超限的车。
        """
        car_id = getattr(request, "car_id", None)
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        definition = self._lookup_car(cars_map, car_id)
        if not definition:
            return _result({"success": False, "error": "车型不存在"})

        user_id = state.get("userId")
        mine = self._user_of(state, user_id)
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
        max_cars = self._to_int(self._settings_of(state).get("MAX_CARS"), 10, minimum=1)
        if owned >= max_cars:
            return _result({"success": False, "error": f"车辆已满（上限 {max_cars} 辆）"})

        result = await client.post_json(
            f"{self.API_BASE}/buy_car.php",
            json={"carId": definition.get("id")},
        )
        if not result or not result.get("success"):
            return _result({"success": False, "error": (result or {}).get("error") or "购买失败"})
        name = definition.get("name") or "车辆"
        # 手动买卖同样要记账：统计页的当日支出/净收靠的就是这几个数，
        # 不记就等于手动花掉的金币凭空消失，当日净收虚高。
        self._shop_spent += price
        self._shop_bought += 1
        await self._refresh_snapshot(client, "买车")
        self._set_result(f"购买 {name}，花费 {price} 金币")
        logger.info(f"[不可躺自动抢车位] 已购买 {name}，花费 {price} 金币")
        return _result({"success": True, "name": name, "price": price,
                "expGained": self._to_int(result.get("expGained"), 0)})

    async def api_trade_in(self, request: CarTradeRequest) -> ApiResult:
        """
        以旧换新。

        与买车同理，等级、补价、旧车归属与状态都在后端校验。
        """
        old_car_id = getattr(request, "old_car_id", None)
        new_car_id = getattr(request, "new_car_id", None)
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        old_key = str(old_car_id if old_car_id is not None else "").strip()
        if not old_key:
            return _result({"success": False, "error": "缺少旧车 ID"})

        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        # 旧车必须是我的车，且处于空闲状态（停着的车不能拿去换）
        user_id = state.get("userId")
        old_car = next((c for c in (state.get("userCars") or [])
                        if isinstance(c, dict)
                        and str(c.get("id")) == old_key
                        and str(c.get("userId")) == str(user_id)), None)
        if not old_car:
            return _result({"success": False, "error": "旧车不存在或不属于当前账号"})
        if old_car.get("status") != "idle":
            return _result({"success": False, "error": "只有空闲车辆能用来以旧换新"})

        # 限时车（carId=0）不在车型表里，旧价会被算成 0，补价就成了全款，
        # 而且换掉之后再也买不回来。自动换新一直挡着这里，手动接口同样必须挡。
        if not self._lookup_car(cars_map, old_car.get("carId")):
            return _result({"success": False,
                            "error": "限时车不在车型表里，无法估出旧价，不能用来以旧换新"})

        new_def = self._lookup_car(cars_map, new_car_id)
        if not new_def:
            return _result({"success": False, "error": "新车型不存在"})
        old_def = self._lookup_car(cars_map, old_car.get("carId")) or {}
        new_price = self._to_int(new_def.get("price"), 0, minimum=0)
        old_price = self._to_int(old_def.get("price"), 0, minimum=0)
        diff = new_price - old_price

        if diff <= 0:
            return _result({"success": False, "error": "只能换更贵的车"})
        mine = self._user_of(state, user_id)
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
        self._shop_diff += diff
        self._shop_traded += 1
        await self._refresh_snapshot(client, "换车")
        # 换完了就把选旧车的面板收掉，否则页面刷新后它还杵在最上面
        self._trade_for = ""
        await self._save_view({"trade_for": ""})
        self._set_result(f"以旧换新 {old_name} → {new_name}，补价 {diff} 金币")
        logger.info(f"[不可躺自动抢车位] 以旧换新 {old_name} → {new_name}，补价 {diff} 金币")
        return _result({"success": True, "old": old_name, "new": new_name, "price": diff,
                "expGained": self._to_int(result.get("expGained"), 0)})

    async def api_buy_flash(self, request: FlashBuyRequest) -> ApiResult:
        """购买限时/限量特卖车辆。"""
        sale_id = getattr(request, "sale_id", None)
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        sales = await self._load_flash_sales(client)
        sale = next((s for s in sales
                     if str(s.get("id")) == str(sale_id).strip()), None)
        if not sale:
            return _result({"success": False, "error": "特卖场次不存在或已结束"})

        user_id = state.get("userId")
        mine = self._user_of(state, user_id)
        level = self._to_int(mine.get("level"), 1, minimum=1)
        cash = self._to_int(mine.get("cash"), 0)
        if sale.get("level", 1) > level:
            return _result({"success": False, "error": f"等级不足，需要 Lv.{sale.get('level')}"})
        if sale.get("price", 0) > cash:
            return _result({"success": False, "error": f"金币不足，还差 {sale.get('price', 0) - cash} 金"})
        owned = sum(1 for c in (state.get("userCars") or [])
                    if str(c.get("userId")) == str(user_id))
        max_cars = self._to_int(self._settings_of(state).get("MAX_CARS"), 10, minimum=1)
        if owned >= max_cars:
            return _result({"success": False, "error": f"车辆已满（上限 {max_cars} 辆）"})

        result = await client.post_json(
            f"{self.API_BASE}/flash_sale.php",
            json={"action": "buy", "saleId": sale.get("id")},
        )
        if not result or not result.get("success"):
            return _result({"success": False, "error": (result or {}).get("error") or "抢购失败"})
        self._shop_spent += self._to_int(sale.get("price"), 0, minimum=0)
        self._shop_bought += 1
        await self._refresh_snapshot(client, "抢购")
        name = sale.get("name") or "特卖车型"
        self._set_result(f"抢购成功 {name}，花费 {sale.get('price', 0)} 金币")
        logger.info(f"[不可躺自动抢车位] 抢购成功 {name}，花费 {sale.get('price', 0)} 金币")
        return _result({"success": True, "name": name, "price": sale.get("price", 0)})

    async def api_unpark(self, request: CarMoveRequest) -> ApiResult:
        """
        立即挪车接口。

        最短停车时长在服务端再校验一次：页面上的按钮禁用只是提示，
        真正的规则必须由后端把关，否则绕过界面就能提前挪车。

        car_id 既可以是车库序号，也可以是车辆实例 ID。车库里的「立即挪车」
        按钮传的是序号，所以必须和 api_park 走同一套 _resolve_car_ref 解析：
        直接拿它当实例 ID 去匹配，序号 1 匹配不到 id 为 '1' 的车，
        点按钮就会一律报「车辆不存在或不属于当前账号」。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})
        # 车辆 ID 用字符串处理：游戏侧不保证是纯数字，按数字解析会把
        # 非数字 ID 误判成「缺少车辆 ID」。筛选时同样按字符串比较。
        raw = getattr(request, "car_id", None)
        token = str(raw if raw is not None else "").strip()
        if not token:
            return _result({"success": False, "error": "缺少车辆 ID"})

        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        # 必须在 userCars 全表里按 userId 过滤，不能只按 id 匹配：
        # state.php 返回的是所有玩家的车辆，缺了归属校验就能挪别人的车。
        # _ordered_my_cars 内部已经按 userId 过滤过，从它里面挑就天然安全。
        ordered = self._ordered_my_cars(state, cars_map)
        car_id = self._resolve_car_ref(token, ordered)
        car = next((c for c in ordered if str(c.get("id")) == str(car_id)), None)
        if not car:
            return _result({"success": False, "error": "车辆不存在或不属于当前账号"})
        if car.get("status") == "idle":
            return _result({"success": False, "error": "该车辆当前不在停车中"})

        rules = self._settings_of(state)
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
        logger.info(f"[不可躺自动抢车位] 手动挪车成功，车辆 {car.get('id')}，收益 {earned} 金币")
        return _result({"success": True, "earned": earned})

    @staticmethod
    def _park_btn(number: Any, free_spots: int, api: str) -> dict:
        """
        空闲车的「立即停到他人车位」按钮。

        没有可停车位时压暗且不挂事件 —— 页面上的灰按钮是真点不动的，
        好过按下去被服务端驳回再弹一句错误。
        """
        can = free_spots > 0
        btn = {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex", "alignItems": "center", "gap": "4px",
                "padding": "5px 12px", "fontSize": "11px", "fontWeight": "bold",
                "borderRadius": "14px", "whiteSpace": "nowrap",
                "userSelect": "none", "border": "none",
                "cursor": "pointer" if can else "not-allowed",
                "color": "#04222B" if can else "rgba(255,255,255,0.45)",
                "backgroundColor": ("#FFFFFF" if can
                                    else "rgba(255,255,255,0.10)"),
            }},
            "content": [
                {"component": "span",
                 "props": {"style": {"display": "flex", "flexShrink": "0"}},
                 "html": _COIN_SVG.replace("@SIZE@", "12px")},
                ParkingBot._text("立即停到他人车位" if can else "暂无可停车位", {}),
            ],
        }
        if can:
            btn["events"] = {"click": {"api": api, "method": "post",
                                       "params": {"car_id": number}}}
        return btn

    async def api_refresh_status(self, request: RefreshRequest) -> ApiResult:
        """
        立即刷新一次游戏状态，不挪车也不停车。

        页面的 JSON 渲染器（MoviePilot-Frontend 的 PageRender）没有任何定时能力，
        `events` 只在用户点击时触发，所以页面自己不会跳。给一个「立即刷新」按钮
        是这里能做到的最接近自动刷新的办法：点完宿主会重拉 get_page，车库里的
        停车时长、收益、待结算都会换成刚拉到的数。

        顺带把快照的更新时间刷新掉，页面上的「几分钟前更新」随之归零。
        """
        if not self.get_state():
            return _result({"success": False, "error": "插件未启用"})

        client, state, cars_map, err, _extra = await self._load_game_context()
        if not state:
            return _result({"success": False, "error": err})

        # record_history=False：这是一次纯展示刷新，执行历史里不该多出一行
        # 「0 停车 / 0 挪车 / 0 收益」的噪声记录。
        user_id = state.get("userId")
        await self._record_round(client, state, user_id, 0, 0, 0,
                                 record_history=False)
        owned = sum(1 for c in (state.get("userCars") or [])
                    if isinstance(c, dict)
                    and str(c.get("userId")) == str(user_id))
        parked = sum(1 for c in (state.get("userCars") or [])
                     if isinstance(c, dict)
                     and str(c.get("userId")) == str(user_id)
                     and str(c.get("status")) != "idle")
        note = f"已刷新状态：名下 {owned} 辆，停车中 {parked} 辆"
        self._set_result(note)
        logger.info(f"[不可躺自动抢车位] {note}")
        return _result({"success": True, "owned": owned, "parked": parked})

    def _age_text(self, snapshot: dict) -> str:
        """
        把快照里的采集时间说成「几分钟前」，让用户判断数据新不新。

        页面渲染器不会自己跳（PageRender 没有任何定时能力），所以数据新不新
        全靠这一行字告诉用户 —— 没有它，用户只能凭猜。

        量词换算与概览卡角标共用 _humanize_gap：同一件事不该有两种说法。
        """
        raw = str(snapshot.get("time") or "").strip()
        if not raw:
            return "数据时间未知"
        try:
            taken = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return "数据时间未知"
        minutes = int((self._now_naive() - taken).total_seconds() // 60)
        if minutes < 0:
            return f"数据时间 {raw}"
        return f"{ParkingBot._humanize_gap(minutes)}更新"

    def _garage_toolbar(self, snapshot: dict, api: str) -> dict:
        """
        车库区块顶部的工具条：左边一个「立即刷新」，右边标出数据有多旧。

        刷新只重新拉状态，不挪车也不停车 —— 想挪车点卡片上的绿按钮，那是另一回事。
        """
        return {
            "component": "div",
            "props": {"style": {
                "display": "flex", "alignItems": "center", "justifyContent": "space-between",
                "gap": "8px", "flexWrap": "wrap", "marginBottom": "10px",
            }},
            "content": [
                self._action_btn("立即刷新状态", api, {}, kind="trade"),
                self._text(self._age_text(snapshot),
                           {"fontSize": "11px", "opacity": "0.55"}),
            ],
        }

    @staticmethod
    def _unpark_btn(number: Any, can: bool, reason: str, api: str) -> dict:
        """
        停车中的车的「立即挪车」按钮。

        和停车按钮互为镜像：停车是把车放出去，挪车是收钱结算，所以用绿底白字
        和停车的白底深字区分开，一眼能看出两个按钮方向相反。

        不满足条件时压暗且不挂事件 —— 页面上的灰按钮是真点不动的，好过按下去
        被服务端驳回再弹一句错误。时长的最终把关仍在 api_unpark 里做一遍。
        """
        btn = {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex", "alignItems": "center",
                "justifyContent": "center", "gap": "4px",
                "padding": "5px 12px", "fontSize": "11px", "fontWeight": "bold",
                "borderRadius": "14px", "whiteSpace": "nowrap",
                "userSelect": "none", "border": "none",
                "cursor": "pointer" if can else "not-allowed",
                "color": "#04281A" if can else "rgba(255,255,255,0.45)",
                "backgroundColor": (_GREEN if can
                                    else "rgba(255,255,255,0.10)"),
            }},
            "text": "立即挪车" if can else reason,
        }
        if can:
            btn["events"] = {"click": {"api": api, "method": "post",
                                       "params": {"car_id": number}}}
        return btn

    @staticmethod
    def _garage(cars: List[dict], min_duration: int, free_spots: int = 0,
                parked_count: int = 0, max_cars: int = 1, api: str = "",
                unpark_api: str = "") -> dict:
        """
        车库视图：照游戏车库页的排版，左图右信息，字段在上、值在下。

        停够最短时长的车给可点的「立即挪车」按钮，其余置灰并显示还差几分钟。
        空闲的车另给一个「立即停到他人车位」按钮 —— 手动操作不受「最多同时停放
        车辆数」限制，旁边会把当前已停/上限写出来，由用户自己判断。
        车牌、每分钟收益、状态、已停时长都直接来自游戏车库数据（state.php 的 userCars）。
        """
        cards = []
        for position, item in enumerate(cars, start=1):
            idle = item.get("status") == "空闲"
            minutes = ParkingBot._to_int(item.get("mins"), 0)
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
                        ParkingBot._coin(ParkingBot._fmt_num(item.get("gain")),
                                         size=13, align="left")
                        if not idle else "当前未停放",
                        _GOLD),
                    ParkingBot._kv(
                        "已停时长" if not idle else "说明",
                        f"{minutes} 分钟" if not idle else "可立即停到他人车位",
                        "rgba(255,255,255,0.75)"),
                    {"component": "div",
                     "props": {"style": {"marginTop": "10px",
                                         "display": "flex", "alignItems": "center",
                                         "gap": "8px", "flexWrap": "wrap"}},
                     "content": [
                         ParkingBot._badge(str(item.get("status", "")), status_color),
                         ParkingBot._unpark_btn(number, can_unpark, button_text,
                                                unpark_api),
                         *([ParkingBot._park_btn(number, free_spots, api)]
                           if idle else []),
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
    def _tint(color: str, alpha: float) -> str:
        """
        称号颜色由游戏下发，可能是任意 CSS 色值。只有 #RRGGBB 才敢算同色淡底，
        其余回落到中性白 —— 直接丢给 _alpha 会 int() 炸掉，整页都渲染不出来。
        """
        text = str(color or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
            return ParkingBot._alpha(text, alpha)
        return f"rgba(255,255,255,{round(alpha * 0.18, 3)})"

    def _owned_titles(self, snapshot: dict) -> List[dict]:
        """
        快照里可佩戴的称号。

        缺 owned 字段的按已获得处理 —— 1.15.1 之前的快照没有这个键，
        直接按 False 过滤会让老用户刚更新时称号整个消失。
        """
        return [t for t in (snapshot.get("titles") or [])
                if isinstance(t, dict) and t.get("owned", True)]

    def _title_chip(self, snapshot: dict) -> Optional[dict]:
        """
        概览里的称号标签，照游戏顶栏：奖牌图标 + 称号名，用称号自己的颜色，
        点一下展开选择器（插件页没有弹窗，展开状态记在服务端）。
        """
        titles = self._owned_titles(snapshot)
        if not titles:
            return None
        current = self._to_int(snapshot.get("title_id"), 0, minimum=0)
        hit = next((t for t in titles if self._to_int(t.get("id"), 0) == current), None)
        if hit:
            name = str(hit.get("name") or "")
            color = str(hit.get("color") or _GOLD)
        else:
            # 没佩戴、或佩戴的那个还没解锁时，state.php 的名字仍然可信
            name = str(snapshot.get("title_name") or "") or "无称号"
            color = str(snapshot.get("title_color") or "") or "#9E9E9E"
        pid = getattr(self, "plugin_id", None) or self._PLUGIN_ID
        return {
            "component": "div",
            "props": {"style": {
                "display": "inline-flex", "alignItems": "center", "gap": "4px",
                "padding": "3px 10px", "borderRadius": "12px", "fontSize": "12px",
                "cursor": "pointer", "userSelect": "none", "color": color,
                "border": f"1px solid {ParkingBot._tint(color, 0.55)}",
                "backgroundColor": ParkingBot._tint(color, 0.14),
            }},
            "content": [
                {"component": "span",
                 "props": {"style": {"display": "flex", "flexShrink": "0"}},
                 "html": _MEDAL_SVG.replace("@SIZE@", "13px")},
                self._text(f"{name} ▾", {}),
            ],
            "events": {"click": {"api": f"plugin/{pid}/title_pick", "method": "post",
                                 "params": {"open": not self._title_open}}},
        }

    def _title_picker(self, snapshot: dict) -> dict:
        """称号选择器，同游戏 showTitleSelector：取消佩戴 + 已获得的称号，当前项玫红描边。"""
        current = self._to_int(snapshot.get("title_id"), 0, minimum=0)
        pid = getattr(self, "plugin_id", None) or self._PLUGIN_ID
        api = f"plugin/{pid}/title"

        def row(name: str, desc: str, color: str, active: bool,
                title_id: Any) -> dict:
            return {
                "component": "div",
                "props": {"style": {
                    "display": "flex", "alignItems": "center", "gap": "10px",
                    "padding": "10px", "marginBottom": "6px", "borderRadius": "8px",
                    "cursor": "pointer", "backgroundColor": "rgba(255,255,255,0.05)",
                    "border": (f"1px solid {_SHOP_LOCK}" if active
                               else "1px solid transparent"),
                }},
                "content": [
                    {"component": "div",
                     "props": {"style": {"flex": "1 1 auto", "minWidth": "0"}},
                     "content": [
                         self._text(f"{name}{'（当前）' if active else ''}",
                                    {"fontSize": "13px", "fontWeight": "bold",
                                     "color": color}),
                         self._text(desc, {"fontSize": "11px", "opacity": "0.62",
                                           "marginTop": "2px"}),
                     ]},
                ],
                "events": {"click": {"api": api, "method": "post",
                                     "params": {"title_id": title_id}}},
            }

        rows = [row("取消佩戴称号", "", "#9E9E9E", current == 0, 0)]
        rows += [row(str(t.get("name") or ""), str(t.get("desc") or ""),
                     str(t.get("color") or _GOLD),
                     self._to_int(t.get("id"), 0) == current,
                     self._to_int(t.get("id"), 0))
                 for t in self._owned_titles(snapshot)]
        return self._card(
            "选择称号",
            "和游戏里点顶栏称号一样：只改显示的名字，不影响任何收益",
            [*rows,
             self._action_btn("收起", f"plugin/{pid}/title_pick", {"open": False},
                              kind="trade")], accent=_GOLD)

    @staticmethod
    def _dict_rows(value: Any) -> List[dict]:
        """
        只留列表里的字典项。

        快照是落盘的数据，跨版本、跨手动改动都可能出现非字典项。渲染路径上
        任何一个 c.get() 撞上字符串或数字都会 AttributeError，整页 500。
        这里统一在入口滤一次，比在每个渲染函数里各写一遍 isinstance 可靠。
        """
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, dict)]

    @staticmethod
    def _fmt_num(value: Any) -> str:
        """
        抄游戏 formatNum 的口径：一万以上折成 K，一千万以上折成 M，以下走千分位。

        车市价格必须用这个 —— 网页版上 3000 金显示成「3,000」，插件里再显示成
        「3000」就等于没复刻。

        OverflowError 与 _to_int 同理必须捕获：int(float("inf")) 抛的不是
        ValueError，一个 Infinity 就会让整页渲染失败。
        """
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            # None / 空串是「这个字段没有」，按 0 显示。直接回落 str(value)
            # 会让页面上出现字面量 None —— 老版本快照缺 gain 字段时，
            # 车库卡片就会显示「累计收益 $ None」。
            if value is None or str(value).strip() == "":
                return "0"
            return str(value)
        # 缩放用真除，但要包一层 OverflowError。
        #
        # 两条 OverflowError 路径得分清：
        #   1) int(float("inf")) —— v1.29.0 修的，转换阶段就炸；
        #   2) 大整数真除 —— int() 对 10**400 是合法的（Python 任意精度整数），
        #      但 number / 1000000 的结果超出 float 范围，抛的是
        #      「integer division result too large for a float」。
        # 第 2 条一直没修，一格脏数据就能让整页白屏。
        #
        # 只在真除抛的时候才退到整除：正常数字的输出必须和以前逐字一致，
        # 包括负数不缩写、以及 99999 四舍五入成 100.0K 这类既有行为 ——
        # 那些不是 bug，动它等于偷偷改了用户已经习惯的显示。
        try:
            if number >= 10000000:
                return f"{number / 1000000:.1f}M"
            if number >= 10000:
                return f"{number / 1000:.1f}K"
            return f"{number:,}"
        except OverflowError:
            sign = "-" if number < 0 else ""
            mag = abs(number)
            if mag >= 10000000:
                return "%s%d.%dM" % (sign, mag // 1000000, (mag % 1000000) // 100000)
            if mag >= 10000:
                return "%s%d.%dK" % (sign, mag // 1000, (mag % 1000) // 100)
            return "{:,}".format(number)

    @staticmethod
    def _action_btn(label: str, api: str, params: Dict[str, Any],
                    kind: str = "buy", disabled: bool = False) -> dict:
        """
        车市卡片底部的小按钮，样式照抄游戏 .car-btn：

        购买是白底深字（#fff / #1a1a2e），以旧换新是透明底 + 青框青字
        （#0df 配 rgba(0,210,255,0.4) 描边）。disabled 时整颗压暗并且不挂事件，
        页面上灰掉的按钮是点不动的，不靠后端兜底。
        """
        style: Dict[str, Any] = {
            "display": "inline-flex",
            "alignItems": "center",
            "justifyContent": "center",
            "padding": "6px 14px",
            "borderRadius": "6px",
            "fontSize": "12px",
            "fontWeight": "bold",
            "whiteSpace": "nowrap",
            "userSelect": "none",
        }
        if disabled:
            style.update({"cursor": "not-allowed", "opacity": "0.45",
                          "color": "#9E9E9E",
                          "backgroundColor": "rgba(255,255,255,0.07)",
                          "border": "1px solid rgba(255,255,255,0.10)"})
        elif kind == "buy":
            style.update({"cursor": "pointer", "color": "#1A1A2E",
                          "backgroundColor": "#FFFFFF",
                          "border": "1px solid #FFFFFF"})
        else:
            style.update({"cursor": "pointer", "color": _SHOP_OWNED,
                          "backgroundColor": "transparent",
                          "border": "1px solid rgba(0,210,255,0.4)"})
        node = {"component": "div", "props": {"style": style},
                "content": [ParkingBot._text(label, {})]}
        if not disabled:
            node["events"] = {"click": {"api": api, "method": "post", "params": params}}
        return node

    @staticmethod
    def _shop_categories(catalog: List[dict]) -> List[str]:
        """
        车型表里出现过的分类，去重且保持原顺序。

        不写死「代步车 / 普通私家车 / …」那六个名字：分类是游戏侧下发的，
        照抄一份常量只会和真实车型表对不上。车型表按价格升序，价格从低到高
        正好是代步车 → 普通 → 精致 → 高级，胶囊顺序自然也就对了。
        """
        names: List[str] = []
        for car in catalog:
            if not isinstance(car, dict):
                continue
            name = str(car.get("category") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    @staticmethod
    def _cat_pills(categories: List[str], current: str, api: str) -> dict:
        """
        车市顶部分类胶囊，照抄游戏 .category-tab。

        未选中是灰底灰字，选中态是玫红→紫的渐变（.category-tab.active）。
        整排横向滚动 —— 游戏里也是这样，手机上不会挤成两行。
        """
        pills = []
        for name in categories:
            active = name == current
            pills.append({
                "component": "div",
                "props": {"style": {
                    "flex": "0 0 auto",
                    "padding": "6px 14px",
                    "borderRadius": "15px",
                    "fontSize": "13px",
                    "whiteSpace": "nowrap",
                    "cursor": "pointer",
                    "userSelect": "none",
                    "color": "#FFC2CE" if active else "#CCCCCC",
                    # 同样是实色而非渐变：渐变在插件页渲染不出来，选中态会退化成
                    # 透明底。文字色本身已经够亮，底色成不成立都读得清。
                    "backgroundColor": (
                        "rgba(233,69,96,0.32)" if active else "rgba(255,255,255,0.10)"),
                    "border": (f"1px solid {_SHOP_LOCK}" if active
                               else "1px solid rgba(255,255,255,0.15)"),
                }},
                "content": [ParkingBot._text(name, {})],
                "events": {"click": {"api": api, "method": "post",
                                     "params": {"category": name}}},
            })
        return {
            "component": "div",
            "props": {"style": {"display": "flex", "overflowX": "auto", "gap": "8px",
                                "marginBottom": "12px", "paddingBottom": "4px"}},
            "content": pills,
        }

    @staticmethod
    def _grid(items: List[dict]) -> dict:
        """车市卡片排布。游戏是两列栅格，这里用可换行的 flex 近似。"""
        return {"component": "div",
                "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                    "marginRight": "-5px"}},
                "content": items}

    @staticmethod
    def _item(title: str, lines: List[Tuple[str, str]],
              actions: Optional[List[dict]] = None,
              accent: str = _CYAN, image: str = "", locked: bool = False) -> dict:
        """
        车市里的一辆车：照抄游戏 .car-card。

        结构和网页版一致 —— 车图居中最大、名字、金币价格、每分钟收益、
        ✓ 已拥有 N 辆，最后一行是并排的按钮。等级不足的整张卡压到半透明，
        按钮换成「需要 Lv.N」，和游戏的 .car-card.locked 一个做法。
        """
        content = []
        if image:
            content.append({
                "component": "div",
                "props": {"style": {"display": "flex", "justifyContent": "center",
                                    "marginBottom": "6px"}},
                "content": [ParkingBot._pic(image, str(title or "🚗")[:1], 96)],
            })
        content.append(ParkingBot._text(title, {"fontSize": "15px", "fontWeight": "bold",
                                                "textAlign": "center", "marginBottom": "4px"}))
        for line in lines:
            # 复合节点（金币图标那种自带样式的）直接塞进来，样式它自己有
            if isinstance(line, dict):
                content.append(line)
                continue
            text, color = line
            content.append(ParkingBot._text(
                text, {"fontSize": "13px", "color": color, "marginTop": "3px",
                       "textAlign": "center"}))
        if actions:
            content.append({
                "component": "div",
                "props": {"style": {"display": "flex", "gap": "8px", "marginTop": "10px",
                                    "justifyContent": "center", "flexWrap": "wrap"}},
                "content": list(actions),
            })
        return {
            "component": "div",
            "props": {"style": {
                "flex": "1 1 190px",
                "minWidth": "178px",
                # 游戏是两列栅格。没有 maxWidth 的话，只剩一张卡时 flex 会把它
                # 拉满整行，看起来跟网页版完全不是一回事
                "maxWidth": "360px",
                "margin": "0 5px 10px 0",
                "padding": "14px",
                "borderRadius": "12px",
                "textAlign": "center",
                "border": f"1px solid {ParkingBot._alpha(accent, 0.34)}",
                "backgroundColor": "rgba(255,255,255,0.04)",
                "opacity": "0.5" if locked else "1",
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

    @staticmethod
    def _is_tradeable_idle(car: dict, price_by_id: Dict[str, int]) -> bool:
        """
        这辆车能不能拿来以旧换新。

        限时车（carId 为 0）的名字与价格挂在车辆实例上、不在车型表里，
        按车型表查价只会得到 0：任何车型都满足「旧价 0 < 新价」，于是它会被
        当成「补差价即可换掉」—— 而限时车换掉就再也买不回来。自动换新路径早就
        挡了这条，页面上的手动路径必须同样挡，否则是白送一辆车。
        """
        if not isinstance(car, dict):
            return False
        return str(car.get("carId")) in price_by_id

    def _trade_picker(self, target: dict, idle_cars: List[dict],
                      price_by_id: Dict[str, int], cash: int,
                      trade_api: str, pick_api: str) -> dict:
        """
        以旧换新的第二步：挑一辆空闲车换。

        游戏点「以旧换新」会弹列表让你选旧车，这里照做 —— 名下可能同时有好几辆
        空闲车，替用户挑一辆就是替他花钱。只有比目标车便宜的空闲车才列得出来：
        接口本身也只接受「换更贵的车」，列出来一堆按不动的选项反而误导。
        """
        target_name = str(target.get("name") or "目标车型")
        target_price = self._to_int(target.get("price"), 0)
        rows = []
        for car in idle_cars:
            # 限时车（carId=0）不在车型表里，价格查出来是 0，会让任何车型都
            # 满足「旧价 < 新价」，等于按 0 元把它换掉 —— 而它根本换不回来。
            # 自动换新早就挡住了，这里必须同样挡住。
            if not self._is_tradeable_idle(car, price_by_id):
                continue
            old_price = price_by_id.get(str(car.get("carId")), 0)
            if old_price >= target_price:
                continue
            diff = target_price - old_price
            rows.append({
                "component": "div",
                "props": {"style": {
                    "display": "flex", "alignItems": "center", "gap": "10px",
                    "padding": "10px", "marginBottom": "6px", "borderRadius": "10px",
                    "backgroundColor": "rgba(255,255,255,0.04)",
                }},
                "content": [
                    ParkingBot._pic(str(car.get("image") or ""),
                                    str(car.get("name") or "车")[:1], 40),
                    {"component": "div",
                     "props": {"style": {"flex": "1 1 auto", "minWidth": "0"}},
                     "content": [
                         ParkingBot._text(str(car.get("name") or "车辆"),
                                          {"fontSize": "13px", "fontWeight": "bold"}),
                         ParkingBot._text(f"{self._to_int(car.get('rate'), 0)}金/分钟",
                                          {"fontSize": "11px", "color": _SHOP_EARN}),
                     ]},
                    ParkingBot._text(f"补 {ParkingBot._fmt_num(diff)} 金",
                                     {"fontSize": "13px", "fontWeight": "bold",
                                      "color": _GOLD, "whiteSpace": "nowrap"}),
                    self._action_btn("换这辆", trade_api,
                                     {"old_car_id": car.get("id"),
                                      "new_car_id": target.get("id")},
                                     disabled=diff > cash),
                ],
            })
        content: List[dict] = [
            self._text(f"选择要用以旧换新的车辆 → {target_name}",
                       {"fontSize": "13px", "color": "#AAAAAA", "marginBottom": "8px"}),
        ]
        if rows:
            content.append({"component": "div", "props": {"style": {}}, "content": rows})
        else:
            content.append(self._text(
                "名下没有比它便宜的空闲车辆，暂时换不了",
                {"fontSize": "12px", "color": _AMBER}))
        content.append(self._action_btn("取消", pick_api, {"car_id": ""}, kind="trade"))
        return self._card(
            "以旧换新",
            "旧车必须是空闲车，且要换成更贵的车型；金币不够的那辆按钮会置灰",
            content, accent=_GOLD)

    def _build_shop(self, snapshot: dict) -> List[dict]:
        """
        构建车市区块：选旧车面板 → 限时特卖 → 分类胶囊 + 车型卡片。

        卡片上的「购买 / 以旧换新」是真按钮，直接 POST 到插件接口。能不能点由
        现金、等级、车辆上限、空闲车四项决定，真正的规则校验在后端还会再做
        一遍 —— 页面上点得动不代表一定买得成。
        """
        cash = self._to_int(snapshot.get("cash"), 0)
        level = self._to_int(snapshot.get("level"), 1, minimum=1)
        owned_count = self._to_int(snapshot.get("car_total"), 0)
        car_max = self._to_int(snapshot.get("car_max"), 10, minimum=1)
        cars = [c for c in (snapshot.get("cars") or []) if isinstance(c, dict)]
        catalog = [c for c in (snapshot.get("catalog") or []) if isinstance(c, dict)]
        blocks: List[dict] = []

        pid = getattr(self, "plugin_id", None) or self._PLUGIN_ID
        buy_api = f"plugin/{pid}/buy_car"
        trade_api = f"plugin/{pid}/trade_in"
        flash_api = f"plugin/{pid}/buy_flash"
        pick_api = f"plugin/{pid}/trade_pick"
        filter_api = f"plugin/{pid}/shop_filter"

        price_by_id = {str(c.get("id")): self._to_int(c.get("price"), 0) for c in catalog}
        idle_cars = [c for c in cars if c.get("status") == "空闲"]
        full = owned_count >= car_max

        # ---------- 以旧换新：正在挑旧车 ----------
        wanted = str(getattr(self, "_trade_for", "") or "")
        target = next((c for c in catalog if str(c.get("id")) == wanted
                       and self._to_int(c.get("level"), 1, minimum=1) <= level), None)
        if target:
            blocks.append(self._trade_picker(target, idle_cars, price_by_id,
                                             cash, trade_api, pick_api))

        # ---------- 限时 / 限量特卖 ----------
        # 必须过 _dict_rows：快照是落盘数据，列表里混进 None 时下面 sale.get()
        # 直接 AttributeError，三个页签一起挂。
        flash = self._dict_rows(snapshot.get("flash"))
        if flash:
            items = []
            for sale in flash:
                price = self._to_int(sale.get("price"), 0)
                original = self._to_int(sale.get("original"), 0)
                need_level = self._to_int(sale.get("level"), 1)
                locked = need_level > level
                sold_out = (not sale.get("timed")) and self._to_int(sale.get("stock"), 0) > 0 \
                    and self._to_int(sale.get("sold"), 0) >= self._to_int(sale.get("stock"), 0)
                lines = [ParkingBot._coin(ParkingBot._fmt_num(price), size=14,
                                          color=_GOLD,
                                          style={"marginTop": "3px"}),
                         (f"{self._to_int(sale.get('earn'), 0)}金/分钟", _SHOP_EARN)]
                if original > price:
                    lines.insert(0, (f"原价 {ParkingBot._fmt_num(original)} 金", "#9E9E9E"))
                if locked:
                    lines.append((f"需要 Lv.{need_level}", _SHOP_LOCK))
                    actions = None
                elif sold_out:
                    lines.append(("已售罄", _SHOP_LOCK))
                    actions = [self._action_btn("已售罄", flash_api,
                                                {"sale_id": sale.get("id")}, disabled=True)]
                else:
                    lines.append((("限时特卖" if sale.get("timed") else
                                   f"仅剩 {self._to_int(sale.get('stock'), 0) - self._to_int(sale.get('sold'), 0)} 辆"),
                                  _PURPLE))
                    actions = [self._action_btn("抢购", flash_api,
                                                {"sale_id": sale.get("id")},
                                                disabled=price > cash or full)]
                    # 同上：钱不够和没空位是两回事，同时成立时要都说出来
                    if price > cash:
                        lines.append((f"还差 {ParkingBot._fmt_num(price - cash)} 金", _AMBER))
                    if full:
                        lines.append((f"抢购需有空位（已满 {car_max} 辆）", _AMBER))
                items.append(self._item(
                    str(sale.get("name", "特卖车型")), lines, actions,
                    accent=_GOLD, image=self._asset_url(sale.get("image")),
                    locked=locked))
            blocks.append(self._card(
                "限时 / 限量特卖",
                "折扣车辆，按场次限购或限时，过期失效；等级不足或金币不够则按钮置灰",
                [self._grid(items)], accent=_GOLD,
            ))

        # ---------- 车市 ----------
        if catalog:
            categories = self._shop_categories(catalog)
            # 游戏默认停在第一个分类（代步车）；记住的分类不在表里就退回第一个
            current = self._cat if self._cat in categories else (categories[0] if categories else "")
            visible = [c for c in catalog
                       if not current or str(c.get("category") or "") == current]
            if not visible:
                visible = catalog
            items = []
            for car in visible:
                price = self._to_int(car.get("price"), 0)
                need_level = self._to_int(car.get("level"), 1, minimum=1)
                locked = need_level > level
                mine_count = sum(1 for c in cars
                                 if str(c.get("carId")) == str(car.get("id")))
                lines = [ParkingBot._coin(ParkingBot._fmt_num(price), size=14,
                                          color=_GOLD,
                                          style={"marginTop": "3px"}),
                         (f"{self._to_int(car.get('earn'), 0)}金/分钟", _SHOP_EARN)]
                if mine_count:
                    lines.append((f"✓ 已拥有{mine_count}辆", _SHOP_OWNED))
                if locked:
                    lines.append((f"需要 Lv.{need_level}", _SHOP_LOCK))
                    actions = None
                else:
                    upgradable = [c for c in idle_cars
                                  if self._is_tradeable_idle(c, price_by_id)
                                  and price_by_id.get(str(c.get("carId")), 0) < price]
                    actions = [
                        self._action_btn("购买", buy_api, {"car_id": car.get("id")},
                                         disabled=full or price > cash),
                        self._action_btn("以旧换新", pick_api, {"car_id": car.get("id")},
                                         kind="trade", disabled=not upgradable),
                    ]
                    # 购买和换新的禁用原因互不相干，各说各的，不能用 elif 串成一条：
                    # 「购买」要的是一个空车位，满员就买不了；「以旧换新」是 1 换 1、
                    # 不占车位（游戏侧 showSelectTradeCar 也只过滤 status==='idle'，
                    # 从头到尾不看车辆上限），它灰掉只因为没有更便宜的空闲车。
                    # 以前满员时只显示「车辆已满」，把真正的原因吞掉了，
                    # 看着就成了「满员导致换不了」——那是错觉。
                    if price > cash:
                        lines.append((f"还差 {ParkingBot._fmt_num(price - cash)} 金", _AMBER))
                    if full:
                        lines.append((f"购买需有空位（已满 {car_max} 辆）", _AMBER))
                    if not upgradable:
                        # 区分「一辆空闲的都没有」和「有但都不比它便宜」：
                        # 前者只要挪一辆车出来就能换，指出来用户才知道下一步做什么。
                        lines.append((
                            "所有车都停着，先挪一辆出来就能换" if not idle_cars
                            else "没有更便宜的空闲车可换", _AMBER))
                items.append(self._item(
                    str(car.get("name", "未知车型")), lines, actions,
                    image=car.get("image") or "", locked=locked))
            blocks.append(self._card(
                "车市",
                f"当前金币 {ParkingBot._fmt_num(cash)}，车辆 {owned_count}/{car_max} 辆；"
                f"按钮直接扣金币，可用性在后端还会再校验一次",
                [self._cat_pills(categories, current, filter_api), self._grid(items)], accent=_GOLD,
            ))
        # 车型表拿不到（game_data.php 失败）时上面两段都不会产出。
        # 仍然要给出这张卡：车市页需要一个兜底标题，否则切到车市会是一片空白。
        if not blocks:
            blocks.append(self._card(
                "车市",
                "车型表与特卖列表暂时拿不到，下一轮执行成功后会自动补上",
                [self._text("车市数据未加载",
                            {"fontSize": "13px", "opacity": "0.8"})], accent=_GOLD,
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
                        # 当前页签是实心青底，未选中的是半透明描边 —— 和游戏顶部那排一致。
                        #
                        # 刻意不用 linear-gradient：MoviePilot 的渲染器不会把渐变
                        # 写进 backgroundColor（同一个 style 对象里的 border、
                        # padding、boxShadow 都生效，唯独它不生效），于是选中态
                        # 变成「透明底 + 深色字」，在深色页面上几乎看不清。
                        # 改成实色 rgba + 亮青文字：底色成不成立文字都读得出来。
                        "color": "#7FF6FF" if active else "rgba(255,255,255,0.82)",
                        "backgroundColor": (
                            "rgba(34,211,238,0.22)" if active else "rgba(255,255,255,0.07)"),
                        "border": (f"1px solid {_CYAN}" if active
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
                    # 页签文案走 html 渲染、不落成 text 节点：去掉 ①②③④ 编号后
                    # 导航文案会和区块卡片标题（如「我的车库」）逐字相同，
                    # 而 tests/test_regress.py 的 section_titles() 用 walk() 收集
                    # 全部 text 节点找区块标题，导航里再冒出一个同名 text 就会让
                    # 「车位页不含车库区块」「车市页不含车库区块」两条断言误判。
                    # 改走 html 后它不再是 text 节点，断言恢复成立，显示完全一致。
                    {"component": "div",
                     "props": {"style": {"fontWeight": "bold" if active else "normal"}},
                     "html": label},
                ],
                "events": {"click": {"api": api, "method": "post", "params": {"tab": key}}},
            })
        return {
            "component": "div",
            "props": {"style": {
                "display": "flex", "gap": "6px", "marginBottom": "14px",
                "flexWrap": "wrap",
                # 外面套一层内嵌槽：页签是「一组互斥选项」，收进一个凹槽里
                # 比四颗按钮直接浮在页面上更像一组。按钮自己的样式一个都没动。
                "padding": "6px", "borderRadius": "14px",
                "backgroundColor": "rgba(255,255,255,0.03)",
                "border": "1px solid rgba(255,255,255,0.10)",
            }},
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
        subtitle = ""
        if snapshot:
            stats = [
                self._stat("今日收益", today_earned, _GREEN),
                self._stat("金币", snapshot.get("cash", 0), _GOLD),
                self._stat("待结算", snapshot.get("pending_gain", 0), _CYAN),
                self._stat("距下次运行", self._next_run_text(), _PURPLE),
            ]
            header = f"{snapshot.get('name', '未命名')} · Lv.{snapshot.get('level', 1)}"
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

        chip = self._title_chip(snapshot) if snapshot else None
        fresh = self._freshness_text(snapshot) if snapshot else ""

        # 经验值格与它的详情面板都要打回同一个接口，这里先算一次 api，
        # 别让下面两处各自 getattr 一遍、再互相依赖谁先执行。
        _pid_now = getattr(self, "plugin_id", None) or self._PLUGIN_ID
        _exp_api = f"plugin/{_pid_now}/exp_detail"
        _notif_api = f"plugin/{_pid_now}/notif_detail"

        # 名字行：玩家名 + 称号标签（点的入口，和游戏顶栏同位置）+ 右肩新鲜度角标
        name_row: List[dict] = [
            self._text(header, {"fontSize": "21px", "fontWeight": "700",
                                "color": "rgba(255,255,255,0.92)",
                                "letterSpacing": "0.3px"}),
        ]
        if chip:
            name_row.append(chip)
        # 经验值格：紧跟称号，点它展开详情面板（走 /exp_detail，和称号选择器
        # 同一个套路）。放在弹性垫片之前，右肩仍留给新鲜度角标。
        if snapshot:
            name_row.append(self._exp_bar(snapshot, _exp_api))
            # 铃铛紧跟经验值格 —— 和游戏顶栏的排布一致（等级 / 车辆 / 金币 / 通知）。
            # 没有快照就不放：铃铛除了角标没别的内容，一个没有数字的铃铛没有意义。
            name_row.append(self._notif_bell(snapshot, _notif_api))
        # 中间垫一个可伸缩的空 div，把角标顶到右肩；窄屏放不下时整行会换行
        name_row.append({"component": "div",
                         "props": {"style": {"flex": "1 1 auto"}}})
        if fresh:
            name_row.append({
                "component": "div",
                "props": {"style": {
                    "display": "inline-flex", "alignItems": "center", "gap": "5px",
                    "padding": "3px 10px", "borderRadius": "999px",
                    "backgroundColor": "rgba(255,255,255,0.03)",
                    "border": "1px solid rgba(255,255,255,0.10)",
                    "fontSize": "11px", "color": "rgba(255,255,255,0.62)",
                }},
                "text": fresh,
            })

        head_lines: List[dict] = [
            {"component": "div",
             "props": {"style": {"display": "flex", "alignItems": "center",
                                 "gap": "10px", "flexWrap": "wrap"}},
             "content": name_row},
        ]
        # 展开的经验值详情面板：紧跟名字行。只在点了之后才有内容。
        if snapshot and self._exp_open:
            head_lines.append(self._exp_panel(snapshot, _exp_api))
        # 展开的通知列表：同样挂在名字行下面。
        if snapshot and self._notif_open:
            head_lines.append(self._notif_panel(snapshot, _notif_api))
        # 元信息收成胶囊（原来是「执行周期 · 车辆 · 数据时间」一行裸文字）
        if snapshot:
            head_lines.append(self._meta_row([
                ("周期", self._cron or "未启用"),
                ("车辆", f"{snapshot.get('car_total', 0)}"
                         f"/{snapshot.get('car_max', 10)} 辆"),
                ("数据时间", snapshot.get("time", "-")),
            ]))
        else:
            head_lines.append(self._text(subtitle, {
                "fontSize": "12px", "color": "rgba(255,255,255,0.62)",
                "marginTop": "8px", "lineHeight": "1.6"}))

        # 最近一轮 / 累计收益：并成一条横条，比两行裸文字更像"读数"
        head_lines.append({
            "component": "div",
            "props": {"style": {
                "display": "flex", "alignItems": "center", "gap": "20px",
                "flexWrap": "wrap", "marginTop": "12px", "padding": "9px 12px",
                "borderRadius": "10px",
                "backgroundColor": "rgba(255,255,255,0.03)",
                "border": "1px solid rgba(255,255,255,0.10)",
            }},
            "content": [
                {"component": "div",
                 "props": {"style": {"display": "flex", "alignItems": "baseline",
                                     "gap": "6px"}},
                 "content": [
                     self._text("最近一轮", {"fontSize": "11px",
                                             "color": "rgba(255,255,255,0.32)"}),
                     self._text(last_run, {"fontSize": "12px", "fontWeight": "700",
                                           "color": "rgba(255,255,255,0.92)"}),
                 ]},
                {"component": "div",
                 "props": {"style": {"display": "flex", "alignItems": "center",
                                     "gap": "6px"}},
                 "content": [
                     self._text("本进程累计挪车收益",
                                {"fontSize": "11px",
                                 "color": "rgba(255,255,255,0.32)"}),
                     self._coin(f"{self._total_earned} 金币", size=14, color=_GOLD,
                                bold=True, align="left"),
                 ]},
            ],
        })
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
                "padding": "16px",
                "borderRadius": "16px",
                "border": "1px solid rgba(255,255,255,0.10)",
                "backgroundColor": "rgba(255,255,255,0.045)",
                # 顶线用负外边距顶到卡片两缘，没有 overflow:hidden 就会溢出圆角
                "overflow": "hidden",
            }},
            "content": [
                # 极光顶线：青 → 紫的横向渐变。gradient 只能走 backgroundImage，
                # 写进 backgroundColor 会被渲染器丢掉（这个坑踩过）。
                {"component": "div",
                 "props": {"style": {
                     "height": "3px",
                     "borderRadius": "16px 16px 0 0",
                     "backgroundImage": "linear-gradient(90deg,#22D3EE,#A78BFA)",
                     "margin": "-16px -16px 14px",
                 }}},
                *head_lines,
            ],
        }

        # 只构造当前页签对应的那一个区块。
        #
        # 除了省掉另外三个页签的渲染开销（实测整页 0.6ms，本来就不痛），更重要的是
        # 隔离故障：以前四个区块无条件全构造再挑一个返回，快照里任意一处脏数据
        # 都会让四个页签一起 500。现在脏数据只砸自己那一页。
        #
        # 执行历史已挪到「金币统计」页，车库页不再夹带。
        min_duration = self._to_int(snapshot.get("min_duration"), 10, minimum=1)
        pid = getattr(self, "plugin_id", None) or self._PLUGIN_ID
        ticket_api = f"plugin/{pid}/ticket"
        charity_api = f"plugin/{pid}/charity_spot"
        park_api = f"plugin/{pid}/park"
        unpark_api = f"plugin/{pid}/unpark"
        refresh_api = f"plugin/{pid}/refresh_status"
        howto_card = self._card(
            "怎么操作",
            "页面上带边框的按钮可以直接点（贴条 / 停车 / 挪车 / 买卖车 / 爱心车位切换）；"
            "聊天指令适合在通知渠道里一键操作。",
            [
                self._text("/车库　　　查看车库与可操作项",
                           {"fontSize": "13px", "fontFamily": "monospace"}),
                self._text("/挪车 序号　立即结算并收取收益",
                           {"fontSize": "13px", "fontFamily": "monospace"}),
                self._text("/买车 车型ID　/换车 旧车ID 新车型ID　/抢购 场次ID",
                           {"fontSize": "13px", "fontFamily": "monospace"}),
                self._text("Telegram / Slack 上发 /车库 会直接收到可点的按钮。",
                           {"fontSize": "11px", "opacity": "0.6", "marginTop": "8px"}),
            ], accent=_AMBER)

        if tab == "spots":
            sections = [self._card(
                "我的车位",
                "私家车位被别人的车停够规定时长后可以贴条，罚款按停车收益分成给自己",
                [self._spot_grid(self._dict_rows(snapshot.get("spots")), ticket_api,
                                 charity_api)],
                accent=_CYAN,
                badges=self._spot_bonus_badges(snapshot)), howto_card]
        elif tab == "shop":
            sections = self._build_shop(snapshot)
        elif tab == "garage":
            sections = [self._card(
                "我的车库",
                f"停车不足 {min_duration} 分钟的车不能挪走，按钮会标灰；"
                f"当前已停 {self._to_int(snapshot.get('parked_total'), 0)}"
                f"/{self._to_int(snapshot.get('car_max'), 1)}"
                f"（手动停车不受这个上限限制）",
                [self._garage_toolbar(snapshot, refresh_api),
                 self._garage(self._dict_rows(snapshot.get("cars")), min_duration,
                              self._to_int(snapshot.get("free_spots"), 0),
                              self._to_int(snapshot.get("parked_total"), 0),
                              self._to_int(snapshot.get("car_max"), 1),
                              park_api, unpark_api)], accent=_PURPLE)]
        else:
            sections = [self._stats_page(daily, history, today_earned)]

        title_picker = ([self._title_picker(snapshot)]
                        if snapshot and self._title_open
                        and self._owned_titles(snapshot) else [])

        return ParkingBot._mark_tappable([
            head_card,
            *title_picker,
            # 四个指标块：手机上一行两个、宽屏一行四个。用单个 flex 容器而不是
            # VRow/VCol，少 8 层组件，宽度也能自己说了算。
            {"component": "div",
             "props": {"style": {"display": "flex", "flexWrap": "wrap",
                                 "marginRight": "-8px", "marginBottom": "4px"}},
             "content": stats},
            self._nav_row(tab),
            *sections,
        ], self._tap_style_node())

    # ---- 按下反馈（v1.36.0）----
    #
    # 为什么需要注入 <style>：插件的元素没有 class，宿主的样式表里也就没有
    # 任何 :hover / :active 规则可选，单独给节点写 transition 是死的 ——
    # transition 只在「属性值发生变化」时才起作用，没有状态变化就永远不动。
    # 声明式 JSON 没有 JS 执行口，唯一能给页面补规则的途径就是 html 字段里
    # 的一段 <style>（v-html 插入的 style 标签浏览器会正常解析生效）。
    #
    # 作用域：所有选择器都以 .pb-tap 打头，只匹配我们自己挂上去的类，
    # 不会波及同一弹窗里的其他插件。不用包裹容器是因为 get_page() 返回的是
    # 平铺的列表，各顶层块是兄弟节点、没有共同父节点可挂类名。
    _TAP_CSS = (
        "<style>"
        ".pb-tap{transition:filter .12s ease,transform .12s ease}"
        ".pb-tap:hover{filter:brightness(1.14)}"
        ".pb-tap:active{transform:scale(.96)}"
        # 尊重系统的「减少动态效果」设置：有人对动效敏感，别硬给。
        "@media (prefers-reduced-motion:reduce){.pb-tap{transition:none}}"
        "</style>")

    @classmethod
    def _tap_style_node(cls) -> dict:
        """把那段 CSS 单独装成一个节点，插在页面最前面。"""
        return {"component": "div", "props": {"style": {"display": "none"}},
                "html": cls._TAP_CSS}

    @classmethod
    def _mark_tappable(cls, nodes, style_node=None):
        """
        原地给所有可点节点挂上 pb-tap 类；给了 style_node 就插在列表最前面。

        放在 get_page 返回前统一处理，而不是在每个按钮的构造处手动加类 ——
        以后新增按钮不必记得补，漏了也只是没动效，不会出错。

        只改 props 不动结构：节点数与 JSON 体积的变化仅来自多了个 class 值。
        """

        def visit(n):
            if isinstance(n, list):
                for x in n:
                    visit(x)
                return
            if not isinstance(n, dict):
                return
            if isinstance(n.get("events"), dict) and "click" in n["events"]:
                props = n.setdefault("props", {})
                cur = str(props.get("class") or "")
                if "pb-tap" not in cur:
                    props["class"] = (cur + " pb-tap").strip()
            content = n.get("content")
            if isinstance(content, list):
                visit(content)

        if not isinstance(nodes, list):
            nodes = [nodes]
        visit(nodes)
        return ([style_node] + nodes) if style_node is not None else nodes

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
