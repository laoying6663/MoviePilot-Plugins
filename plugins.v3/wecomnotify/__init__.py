"""企业微信通知插件。

以插件形态复刻 MoviePilot 内置「企业微信」通知渠道：既接收 MoviePilot 的通知事件
转发到企业微信自建应用，也提供企业微信回调入口用于远程命令交互与自动菜单。

与内置渠道的差异（插件受宿主合同限制，无法改变的部分）：
    插件不能向宿主 `NotificationChannel` 枚举注册新的渠道，因此消息回执统一由本插件
    自己调用企业微信 API 发送，而不是走宿主 post_message。相应地，宿主内置的斜杠命令
    （如 /site_signin）无法从本插件触达；本插件只处理自己注册的命令，并把其余消息
    以 EventType.UserMessage 广播给其它插件。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from starlette.responses import PlainTextResponse

from app.schemas.types import EventType, MessageType, NotificationChannel
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .client import WecomClient
from .crypt import (
    WXBizMsgCrypt,
    WXBizMsgCryptError,
    crypto_available,
    parse_xml_message,
)

# 企业微信要求回调在 5 秒内响应，成功时返回该固定字符串
WECOM_SUCCESS_REPLY = "success"
# 远程命令交互超时时间（秒）
COMMAND_TIMEOUT = 20


class WecomNotify(_PluginBase):
    # 插件名称
    plugin_name = "企业微信通知"
    # 插件描述
    plugin_desc = "把 MoviePilot 通知推送到企业微信自建应用，并支持在企业微信里执行远程命令"
    # 插件图标
    plugin_icon = "Wecom_A.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "laoying6663"
    # 作者主页
    author_url = "https://github.com/laoying6663/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "wecomnotify_"
    # 加载顺序
    plugin_order = 46
    # 可使用的用户级别（配置含企业应用密钥，限制为管理员）
    auth_level = 2

    # ------------------------------------------私有属性------------------------------------------
    _enabled = False
    _onlyonce = False
    # 通知渠道名称
    _name = "企业微信"
    # 消息类型筛选，空表示全部类型
    _types: List[str] = []
    # 企业ID
    _corpid = ""
    # 应用 AgentId
    _agentid = ""
    # 应用 Secret
    _secret = ""
    # 消息转发代理地址
    _proxy = ""
    # 接收消息 Token
    _token = ""
    # EncodingAESKey
    _encoding_aes_key = ""
    # 指定接收人 userid，留空表示应用可见的全部成员
    _userid = ""
    # 管理员白名单
    _admins: List[str] = []
    # 是否自动注册控制菜单
    _create_menu = True
    # 客户端实例
    _client: Optional[WecomClient] = None
    # 最近一次发送结果，供配置页展示
    _last_send = ""

    def init_plugin(self, config: dict = None) -> None:
        """
        读取配置并建立本次运行所需状态；允许重复调用。
        """
        config = config or {}

        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._name = str(config.get("name") or "企业微信").strip()
        self._types = [str(t).strip() for t in (config.get("types") or []) if str(t).strip()]
        self._corpid = str(config.get("corpid") or "").strip()
        self._agentid = str(config.get("agentid") or "").strip()
        self._secret = str(config.get("secret") or "").strip()
        self._proxy = str(config.get("proxy") or "").strip()
        self._token = str(config.get("token") or "").strip()
        self._encoding_aes_key = str(config.get("encoding_aes_key") or "").strip()
        self._userid = str(config.get("userid") or "").strip()
        # 白名单兼容两种写法：逗号分隔字符串或已经是列表
        admins = config.get("admins") or ""
        if isinstance(admins, str):
            self._admins = [a.strip() for a in admins.replace("，", ",").split(",") if a.strip()]
        else:
            self._admins = [str(a).strip() for a in admins if str(a).strip()]
        self._create_menu = config.get("create_menu", True) is not False
        self._last_send = ""

        # 每次重建客户端，避免配置变更后继续使用旧代理或旧密钥
        self._client = WecomClient(
            corpid=self._corpid,
            secret=self._secret,
            agentid=self._agentid,
            proxy=self._proxy,
        )

        if not crypto_available():
            logger.error(
                "[企业微信通知] 当前环境缺少 pycryptodome，消息回调功能不可用；"
                "推送功能不受影响"
            )

        if self._enabled and not self._admins:
            logger.warning("[企业微信通知] 未设置管理员白名单，任何人发来的斜杠命令都会被执行")

        if self._enabled and self._client.is_configured():
            # 启动即校验一次凭据，让配置错误在面板上直接暴露
            self._client.get_access_token()
            self._sync_menus()

        # 勾选「立即运行一次」时延迟执行一次连通性测试
        if self._enabled and self._onlyonce:
            add_once_job = getattr(scheduler_sdk, "add_plugin_once_job", None)
            if add_once_job:
                add_once_job(
                    self.__class__.__name__,
                    "wecom_test_once",
                    self.test_notification,
                    "企业微信通知立即运行一次",
                    delay_seconds=3,
                )
            config["onlyonce"] = False
            self.update_config(config)

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """释放运行时状态；本插件不持有线程或连接，重复调用安全。"""
        self._client = None
        self._last_send = ""

    # ------------------------------------------------------------------ 通知推送

    @eventmanager.register(EventType.NoticeMessage)
    async def notice_message(self, event: Event) -> None:
        """
        接收宿主通知事件并转发到企业微信。

        event_data 包含 channel / type / title / text / image / userid。
        """
        if not self._enabled or not self._client:
            return

        event_data = event.event_data or {}
        title = str(event_data.get("title") or "")
        text = str(event_data.get("text") or "")
        image = event_data.get("image")
        mtype = event_data.get("type")

        if not title and not text:
            return

        # 消息类型筛选：未配置时接收全部类型
        if self._types:
            type_name = getattr(mtype, "value", mtype)
            if type_name and str(type_name) not in self._types:
                return

        # 通知自带接收人时优先用通知的 userid，实现按用户隔离
        userid = self._userid
        event_userid = event_data.get("userid")
        if event_userid:
            userid = str(event_userid)

        sent = await asyncio.to_thread(
            self._client.send_msg, title=title, text=text, image=image, userid=userid
        )
        if sent:
            self._last_send = f"已发送：{title}"
            logger.info(f"[企业微信通知] 通知已发送，接收人：{userid or '@all'}")
        else:
            self._last_send = f"发送失败：{title}"
            logger.error(f"[企业微信通知] 通知发送失败，接收人：{userid or '@all'}")

    # ------------------------------------------------------------------ 远程命令

    @staticmethod
    def get_command() -> List[dict]:
        """注册本插件的远程命令。"""
        return [
            {
                "cmd": "/wecom_status",
                "event": EventType.PluginAction,
                "desc": "查看企业微信通知配置状态",
                "category": "企业微信通知",
                "data": {"action": "wecom_status"},
            },
            {
                "cmd": "/wecom_test",
                "event": EventType.PluginAction,
                "desc": "发送企业微信测试消息",
                "category": "企业微信通知",
                "data": {"action": "wecom_test"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    async def plugin_action(self, event: Event) -> None:
        """处理本插件注册的远程命令，回复固定发回请求来源。"""
        event_data = event.event_data or {}
        action = event_data.get("action")
        if action not in ("wecom_status", "wecom_test"):
            return

        userid = str(event_data.get("userid") or self._userid or "")
        if action == "wecom_status":
            await self._reply_status(userid)
        else:
            await self._run_test(userid)

    async def _reply_status(self, userid: str = "") -> None:
        """把当前配置与运行状态回报给请求方。"""
        if not self._client:
            await self._reply(title="企业微信通知", text="插件未启用", userid=userid)
            return
        lines = [
            f"企业ID：{self._corpid or '未配置'}",
            f"应用AgentId：{self._agentid or '未配置'}",
            f"代理地址：{self._client.proxy}",
            f"接收人：{self._userid or '应用可见全部成员'}",
            f"消息类型：{'、'.join(self._types) if self._types else '全部'}",
            f"管理员白名单：{'、'.join(self._admins) if self._admins else '未设置'}",
            f"回调地址：/api/v1/plugin/{self.__class__.__name__}/wecom",
            f"加解密依赖：{'可用' if crypto_available() else '不可用'}",
        ]
        lines.append("access_token：" + ("获取成功" if await asyncio.to_thread(self._client.get_state) else "获取失败"))
        await self._reply(title="企业微信通知配置状态", text="\n".join(lines), userid=userid)

    async def _run_test(self, userid: str = "") -> None:
        """发送一条测试消息。"""
        if not self._client or not self._client.is_configured():
            await self._reply(
                title="企业微信通知", text="企业ID / 应用AgentId / 应用Secret 未配置完整", userid=userid
            )
            return
        sent = await asyncio.to_thread(
            self._client.send_msg,
            title="MoviePilot 企业微信通知测试",
            text="如果你收到这条消息，说明企业微信通知插件工作正常。",
            userid=userid,
        )
        if sent:
            self._last_send = "测试消息已发送"
            await self._reply(
                title="企业微信通知",
                text=f"测试消息已发送给 {userid or self._userid or '@all'}",
                userid=userid,
            )
        else:
            self._last_send = "测试消息发送失败"
            await self._reply(
                title="企业微信通知", text="测试消息发送失败，请查看日志", userid=userid
            )

    def test_notification(self) -> None:
        """供「立即运行一次」与接口调用。"""
        if not self._client:
            logger.warning("[企业微信通知] 插件未启用，跳过测试")
            return
        sent = self._client.send_msg(
            title="MoviePilot 企业微信通知测试",
            text="如果你收到这条消息，说明企业微信通知插件工作正常。",
            userid=self._userid,
        )
        self._last_send = "测试消息已发送" if sent else "测试消息发送失败"
        logger.info(f"[企业微信通知] {self._last_send}")

    def _sync_menus(self) -> None:
        """按已注册命令重建企业微信控制菜单。"""
        if not self._create_menu or not self._client:
            return
        if not self._token or not self._encoding_aes_key:
            logger.debug("[企业微信通知] 未配置 Token/EncodingAESKey，跳过菜单初始化")
            return
        commands = {item["cmd"]: item for item in self.get_command()}
        result = self._client.create_menus(commands)
        if result:
            logger.info("[企业微信通知] 控制菜单已更新")

    # ------------------------------------------------------------------ 回调接口

    def get_api(self) -> List[dict]:
        """
        注册插件后端 API。

        回调接口对企业微信开放，因此使用匿名访问；安全性由企业微信的 SHA1 签名校验
        （需要知道 Token 才能算出正确签名）保证。
        """
        return [
            {
                "path": "/wecom",
                "endpoint": self.wecom_verify,
                "methods": ["GET"],
                "auth": "none",
                "summary": "企业微信回调地址验证",
            },
            {
                "path": "/wecom",
                "endpoint": self.wecom_message,
                "methods": ["POST"],
                "auth": "none",
                "summary": "企业微信消息回调",
            },
            {
                "path": "/status",
                "endpoint": self.wecom_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询企业微信通知运行状态",
            },
        ]

    def _build_crypt(self) -> Optional[WXBizMsgCrypt]:
        """构造回调加解密对象；配置不完整时返回 None。"""
        if not self._token or not self._encoding_aes_key or not self._corpid:
            return None
        try:
            return WXBizMsgCrypt(
                token=self._token,
                encoding_aes_key=self._encoding_aes_key,
                receive_id=self._corpid,
            )
        except WXBizMsgCryptError as err:
            logger.error(f"[企业微信通知] 回调加解密参数无效：{err}")
            return None

    async def wecom_verify(
        self,
        msg_signature: str = None,
        timestamp: str = None,
        nonce: str = None,
        echostr: str = None,
    ) -> Any:
        """校验企业微信填写的回调地址，返回解密后的 echostr。"""
        crypt = self._build_crypt()
        if not crypt or not all([msg_signature, timestamp, nonce, echostr]):
            return PlainTextResponse("企业微信回调验证失败，请检查插件配置")

        ret, reply = crypt.verify_url(msg_signature, timestamp, nonce, echostr)
        if ret != 0 or not reply:
            logger.error(f"[企业微信通知] 回调地址验证失败，返回码：{ret}")
            return PlainTextResponse("企业微信回调验证失败")
        return PlainTextResponse(reply)

    async def wecom_message(
        self,
        request: Request,
        msg_signature: str = None,
        timestamp: str = None,
        nonce: str = None,
    ) -> Any:
        """接收企业微信推送的消息并分发处理。"""
        # 先占位返回，避免复杂处理拖慢响应触发企业微信重试
        if not self._enabled:
            return PlainTextResponse(WECOM_SUCCESS_REPLY)

        crypt = self._build_crypt()
        if not crypt:
            return PlainTextResponse(WECOM_SUCCESS_REPLY)

        body = await request.body()
        ret, content = crypt.decrypt_msg(body, msg_signature, timestamp, nonce)
        if ret != 0 or not content:
            logger.error(f"[企业微信通知] 回调消息解密失败，返回码：{ret}")
            return PlainTextResponse(WECOM_SUCCESS_REPLY)

        try:
            await asyncio.wait_for(
                self._handle_incoming(content), timeout=COMMAND_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error("[企业微信通知] 处理回调消息超时")
        except Exception as err:
            logger.error(f"[企业微信通知] 处理回调消息失败：{err}", exc_info=True)

        return PlainTextResponse(WECOM_SUCCESS_REPLY)

    async def wecom_status(self) -> Any:
        """返回插件运行状态，供配置页与排障使用。"""
        state = False
        if self._client:
            state = await asyncio.to_thread(self._client.get_state)
        return {
            "enabled": self._enabled,
            "name": self._name,
            "configured": bool(self._client and self._client.is_configured()),
            "token_available": state,
            "crypto_available": crypto_available(),
            "proxy": self._client.proxy if self._client else "",
            "types": self._types,
            "userid": self._userid,
            "admins": self._admins,
            "callback_path": f"/api/v1/plugin/{self.__class__.__name__}/wecom",
            "last_send": self._last_send,
        }

    # ------------------------------------------------------------------ 消息分发

    def _is_admin(self, userid: str) -> bool:
        """校验 userid 是否在管理员白名单内；白名单为空时放行。"""
        if not self._admins:
            return True
        return str(userid) in self._admins

    async def _reply(self, title: str, text: str, userid: str = "") -> None:
        """通过企业微信回复消息；未启用或无客户端时静默跳过。"""
        if not self._client:
            return
        await asyncio.to_thread(
            self._client.send_msg,
            title=title,
            text=text,
            userid=userid or self._userid,
        )

    async def _handle_incoming(self, xml_content: str) -> None:
        """解析回调明文并路由到对应处理分支。"""
        info = parse_xml_message(xml_content)
        if not info:
            logger.warning("[企业微信通知] 回调消息 XML 解析失败")
            return

        userid = info.get("FromUserName")
        if not userid:
            logger.warning("[企业微信通知] 回调消息缺少 FromUserName")
            return

        msg_type = info.get("MsgType")
        command = ""

        if msg_type == "event" and info.get("Event") == "click":
            # 控制菜单点击最终转成命令文本
            command = (info.get("EventKey") or "").strip()
        elif msg_type == "text":
            command = (info.get("Content") or "").strip()
        elif msg_type == "voice":
            command = (info.get("Recognition") or "").strip()
            if not command:
                await self._reply(
                    title="企业微信通知", text="未识别到语音内容", userid=userid
                )
                return
        else:
            await self._reply(
                title="企业微信通知",
                text=f"暂不支持 {msg_type} 类型消息",
                userid=userid,
            )
            return

        if not command:
            return

        logger.info(f"[企业微信通知] 收到消息，userid={userid}, command={command}")

        if command.startswith("/"):
            if not self._is_admin(userid):
                await self._reply(
                    title="企业微信通知", text="只有管理员才有权限执行此命令", userid=userid
                )
                return
            await self._dispatch_command(command, userid)
            return

        # 非命令消息广播出去，供监听 UserMessage 的插件处理
        self.eventmanager.send_event(
            EventType.UserMessage,
            {
                "text": command,
                "userid": userid,
                "channel": NotificationChannel.Wechat,
                "source": self._name,
            },
        )

    async def _dispatch_command(self, command: str, userid: str) -> None:
        """把斜杠命令路由到本插件注册的命令。"""
        parts = command.split()
        cmd = parts[0].lower()
        args = parts[1:]

        registered = {item["cmd"]: item for item in self.get_command()}

        if cmd in ("/help", "/帮助"):
            lines = ["可用命令："] + [
                f"{item['cmd']} {item['desc']}" for item in self.get_command()
            ]
            await self._reply(
                title="企业微信通知", text="\n".join(lines), userid=userid
            )
            return

        if cmd not in registered:
            await self._reply(
                title="企业微信通知",
                text=f"未找到命令 {cmd}，发送 /help 查看可用命令",
                userid=userid,
            )
            return

        self.eventmanager.send_event(
            EventType.PluginAction,
            {
                **registered[cmd].get("data", {}),
                "userid": userid,
                "args": args,
                "channel": NotificationChannel.Wechat,
                "source": self._name,
            },
        )

    # ------------------------------------------------------------------ 页面

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页面与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "name",
                                            "label": "通知渠道名称",
                                            "placeholder": "企业微信",
                                        },
                                    }
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
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "types",
                                            "label": "类型",
                                            "multiple": True,
                                            "chips": True,
                                            "items": [
                                                {"title": item.value, "value": item.value}
                                                for item in MessageType
                                            ],
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "corpid",
                                            "label": "企业ID",
                                            "placeholder": "企业微信后台 -> 我的企业 -> 企业信息",
                                            "prependInnerIcon": "mdi-domain",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "agentid",
                                            "label": "应用 AgentId",
                                            "placeholder": "企业微信自建应用的AgentId",
                                            "prependInnerIcon": "mdi-card-account-details-outline",
                                        },
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "secret",
                                            "label": "应用 Secret",
                                            "type": "password",
                                            "placeholder": "企业微信自建应用的Secret",
                                            "prependInnerIcon": "mdi-key-outline",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "proxy",
                                            "label": "代理地址",
                                            "placeholder": "留空直连官方地址",
                                            "prependInnerIcon": "mdi-server-network",
                                        },
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "token",
                                            "label": "Token",
                                            "placeholder": "企业微信应用 -> 接收消息",
                                            "prependInnerIcon": "mdi-key-chain-variant",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "encoding_aes_key",
                                            "label": "EncodingAESKey",
                                            "placeholder": "企业微信应用 -> 接收消息",
                                            "prependInnerIcon": "mdi-lock-outline",
                                        },
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "userid",
                                            "label": "接收人",
                                            "placeholder": "留空发给应用可见的全部成员",
                                            "prependInnerIcon": "mdi-account-arrow-right-outline",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "admins",
                                            "label": "管理员白名单",
                                            "placeholder": "多个ID使用英文逗号分隔",
                                            "prependInnerIcon": "mdi-account-key-outline",
                                        },
                                    }
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
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "create_menu",
                                            "label": "自动注册控制菜单",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "name": "企业微信",
            "types": [],
            "corpid": "",
            "agentid": "",
            "secret": "",
            "proxy": "",
            "token": "",
            "encoding_aes_key": "",
            "userid": "",
            "admins": "",
            "create_menu": True,
            "onlyonce": False,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页。"""
        callback_path = f"/api/v1/plugin/{self.__class__.__name__}/wecom"
        port = getattr(settings, "NGINX_PORT", 3000)
        callback_tip = f"企业微信应用 -> 接收消息 -> URL 填：http://<你的域名>:{port}{callback_path}"
        status_text = (
            callback_tip if self._enabled else "企业微信通知未启用"
        )
        items = [
            {"title": "通知渠道名称", "subtitle": self._name or "未配置"},
            {"title": "企业ID", "subtitle": self._corpid or "未配置"},
            {"title": "应用 AgentId", "subtitle": self._agentid or "未配置"},
            {
                "title": "代理地址",
                "subtitle": self._client.proxy if self._client else "未配置",
            },
            {
                "title": "接收人",
                "subtitle": self._userid or "应用可见全部成员",
            },
            {
                "title": "消息类型",
                "subtitle": "、".join(self._types) if self._types else "全部",
            },
            {
                "title": "管理员白名单",
                "subtitle": "、".join(self._admins) if self._admins else "未设置",
            },
            {
                "title": "加解密依赖",
                "subtitle": (
                    "可用" if crypto_available() else "不可用（缺少 pycryptodome）"
                ),
            },
            {"title": "最近发送", "subtitle": self._last_send or "暂无记录"},
        ]
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "success" if self._enabled else "warning",
                                    "variant": "tonal",
                                    "text": status_text,
                                },
                            }
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
                            {
                                "component": "VList",
                                "props": {"items": items},
                            }
                        ],
                    }
                ],
            },
        ]
