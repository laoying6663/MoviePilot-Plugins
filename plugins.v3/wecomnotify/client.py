"""企业微信自建应用 API 客户端。

只使用宿主稳定 SDK 出口（app.sdk.network），不依赖 app.adapters 等内部路径。
行为对齐宿主内置企业微信模块：access_token 本地缓存并提前失效、
errcode 42001 自动重取一次重试、长文本按 2048 字节分块发送。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.sdk.logging import logger
from app.sdk.network import RequestUtils, UrlUtils

# 未配置代理时的官方地址
DEFAULT_API_BASE = "https://qyapi.weixin.qq.com"
# access_token 提前失效余量，避免边界时刻拿着过期 token 发消息
TOKEN_EXPIRE_MARGIN = timedelta(minutes=5)
# 企业微信单条文本消息的内容上限
TEXT_CHUNK_BYTES = 2048
# 企业微信菜单约束
MAX_TOP_BUTTONS = 3
MAX_SUB_BUTTONS = 5


class WecomClient:
    """单个企业微信自建应用的轻量客户端。"""

    # 相对路径模板，统一由代理地址拼接
    _send_msg_url = "cgi-bin/message/send?access_token={access_token}"
    _token_url = "cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    _create_menu_url = "cgi-bin/menu/create?access_token={access_token}&agentid={agentid}"
    _delete_menu_url = "cgi-bin/menu/delete?access_token={access_token}&agentid={agentid}"
    _download_media_url = "cgi-bin/media/get?access_token={access_token}&media_id={media_id}"

    def __init__(
        self,
        corpid: str,
        secret: str,
        agentid: Any,
        proxy: Optional[str] = None,
        timeout: int = 15,
    ):
        """
        初始化客户端。

        :param corpid: 企业ID
        :param secret: 应用 Secret
        :param agentid: 应用 AgentId
        :param proxy: 消息转发代理地址，留空使用官方地址
        :param timeout: 单次请求超时秒数
        """
        self._corpid = (corpid or "").strip()
        self._secret = (secret or "").strip()
        self._agentid = agentid
        self._timeout = timeout

        # 代理地址保留默认值语义：2022-06-20 之后创建的自建应用才需要转发代理，
        # 未配置时直连官方地址。
        self._proxy = (proxy or "").strip() or DEFAULT_API_BASE

        self._send_msg_url = UrlUtils.adapt_request_url(self._proxy, self._send_msg_url)
        self._token_url = UrlUtils.adapt_request_url(self._proxy, self._token_url)
        self._create_menu_url = UrlUtils.adapt_request_url(self._proxy, self._create_menu_url)
        self._delete_menu_url = UrlUtils.adapt_request_url(self._proxy, self._delete_menu_url)
        self._download_media_url = UrlUtils.adapt_request_url(
            self._proxy, self._download_media_url
        )

        self._access_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._token_lock = threading.Lock()

    @property
    def proxy(self) -> str:
        """当前使用的 API 地址。"""
        return self._proxy

    def is_configured(self) -> bool:
        """返回必需参数是否齐全。"""
        return bool(self._corpid and self._secret and self._agentid)

    def get_access_token(self, force: bool = False) -> Optional[str]:
        """获取并缓存 access_token，过期前主动失效。"""
        with self._token_lock:
            if (
                not force
                and self._access_token
                and self._expires_at
                and datetime.now() < self._expires_at
            ):
                return self._access_token

            if not self.is_configured():
                logger.error("[企业微信通知] 企业ID / 应用Secret / 应用AgentId 未配置完整")
                return None

            url = self._token_url.format(
                corpid=self._corpid, corpsecret=self._secret
            )
            res = RequestUtils(timeout=self._timeout).get_res(url)
            if res is None:
                logger.error("[企业微信通知] 获取 access_token 失败，未获取到返回信息")
                return None
            try:
                ret = res.json()
            except Exception as err:
                logger.error(f"[企业微信通知] 解析 access_token 响应失败: {err}")
                return None

            if ret.get("errcode") != 0:
                logger.error(
                    f"[企业微信通知] 获取 access_token 失败，错误码："
                    f"{ret.get('errcode')}，错误原因：{ret.get('errmsg')}"
                )
                return None

            self._access_token = ret.get("access_token")
            try:
                expires_in = int(ret.get("expires_in") or 7200)
            except (TypeError, ValueError):
                expires_in = 7200
            self._expires_at = datetime.now() + timedelta(
                seconds=expires_in
            ) - TOKEN_EXPIRE_MARGIN
            return self._access_token

    def get_state(self) -> bool:
        """返回当前是否能取到 access_token，用于配置页状态展示。"""
        return bool(self.get_access_token())

    @staticmethod
    def _split_content(content: str, max_bytes: int = TEXT_CHUNK_BYTES) -> List[str]:
        """按 UTF-8 字节数切分文本，且不切断多字节字符。"""
        chunks: List[str] = []
        current = bytearray()

        for line in content.splitlines():
            encoded = (line + "\n").encode("utf-8")
            line_length = len(encoded)

            if line_length > max_bytes:
                if current:
                    chunks.append(current.decode("utf-8", errors="replace").strip())
                    current = bytearray()
                start = 0
                while start < line_length:
                    end = start + max_bytes
                    if end >= line_length:
                        end = line_length
                    else:
                        # 回退到字符边界，避免把一个多字节字符切成两半
                        while end > start and (encoded[end] & 0xC0) == 0x80:
                            end -= 1
                        if end == start:
                            # 单个字符本身就超过上限，只能整块取出
                            end = start + 1
                            while end < line_length and (encoded[end] & 0xC0) == 0x80:
                                end += 1
                    chunks.append(
                        encoded[start:end].decode("utf-8", errors="replace").strip()
                    )
                    start = end
                continue

            if len(current) + line_length > max_bytes:
                chunks.append(current.decode("utf-8", errors="replace").strip())
                current = bytearray()

            current += encoded

        if current:
            chunks.append(current.decode("utf-8", errors="replace").strip())

        return [chunk for chunk in chunks if chunk]

    def _post(self, url_template: str, payload: dict) -> Optional[bool]:
        """
        发送 JSON 请求。

        返回 True 表示成功，False 表示业务失败，None 表示未取到响应。
        access_token 过期（errcode 42001）时强制重取一次并重试。
        """
        for attempt in range(2):
            token = self.get_access_token(force=attempt > 0)
            if not token:
                return None
            url = url_template.format(access_token=token)
            res = RequestUtils(timeout=self._timeout, content_type="application/json").post(
                url=url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            if res is None:
                logger.error("[企业微信通知] 请求未获取到返回信息")
                return None
            if res.status_code != 200:
                logger.error(
                    f"[企业微信通知] 请求失败，错误码：{res.status_code}，"
                    f"错误原因：{res.reason}"
                )
                return None
            try:
                ret = res.json()
            except Exception as err:
                logger.error(f"[企业微信通知] 解析响应失败: {err}")
                return None
            if ret.get("errcode") == 0:
                return True
            if ret.get("errcode") == 42001 and attempt == 0:
                logger.warning("[企业微信通知] access_token 已过期，重新获取后重试")
                continue
            logger.error(
                f"[企业微信通知] 请求失败，错误码：{ret.get('errcode')}，"
                f"错误原因：{ret.get('errmsg')}"
            )
            return False
        return False

    def _send_text_message(
        self,
        title: str,
        text: Optional[str] = None,
        userid: Optional[str] = None,
        link: Optional[str] = None,
    ) -> bool:
        """发送文本消息，超长内容分块逐块下发。"""
        if not title and not text:
            logger.error("[企业微信通知] 消息标题和内容不能都为空")
            return False
        if text:
            content = f"{title}\n{text.replace(chr(10) + chr(10), chr(10))}"
        else:
            content = title
        if link:
            content = f"{content}\n点击查看：{link}"
        # 留空表示发给应用可见的全部成员
        target = userid or "@all"

        for chunk in self._split_content(content):
            payload = {
                "touser": target,
                "msgtype": "text",
                "agentid": self._agentid,
                "text": {"content": chunk},
                "safe": 0,
                "enable_id_trans": 0,
                "enable_duplicate_check": 0,
            }
            if self._post(self._send_msg_url, payload) is not True:
                # 分块消息只要有一块失败就整体视为失败
                return False
        return True

    def _send_news_message(
        self,
        title: str,
        text: str,
        image_url: str,
        userid: Optional[str] = None,
        link: Optional[str] = None,
    ) -> Optional[bool]:
        """发送图文消息。"""
        target = userid or "@all"
        payload = {
            "touser": target,
            "msgtype": "news",
            "agentid": self._agentid,
            "news": {
                "articles": [
                    {
                        "title": title,
                        "description": (text or "").replace(chr(10) + chr(10), chr(10)),
                        "picurl": image_url,
                        "url": link,
                    }
                ]
            },
        }
        return self._post(self._send_msg_url, payload)

    def send_msg(
        self,
        title: str,
        text: Optional[str] = None,
        image: Optional[str] = None,
        userid: Optional[str] = None,
        link: Optional[str] = None,
    ) -> Optional[bool]:
        """
        消息发送入口，支持文本、图文、链接跳转与指定发送对象。

        :param title: 消息标题
        :param text: 消息内容
        :param image: 图片地址，填写后按图文消息下发
        :param userid: 接收人 userid，留空发给应用可见的全部成员
        :param link: 点击跳转链接
        """
        try:
            if not self.get_access_token():
                logger.error("[企业微信通知] 获取 access_token 失败，请检查参数配置")
                return None
            if image:
                return self._send_news_message(
                    title=title, text=text, image_url=image, userid=userid, link=link
                )
            return self._send_text_message(
                title=title, text=text, userid=userid, link=link
            )
        except Exception as err:
            logger.error(f"[企业微信通知] 发送消息失败：{err}")
            return False

    def create_menus(self, commands: Dict[str, dict]) -> Optional[bool]:
        """
        按命令分类自动注册企业微信控制菜单。

        一级菜单最多 3 个、每个二级菜单最多 5 个，超出部分按宿主内置实现截断。
        """
        category_dict: Dict[str, Dict[str, dict]] = {}
        for key, value in commands.items():
            category = value.get("category")
            if category:
                category_dict.setdefault(category, {})[key] = value

        buttons = []
        for category, menu in category_dict.items():
            sub_buttons = [
                {"type": "click", "name": value.get("description"), "key": key}
                for key, value in menu.items()
            ]
            if not sub_buttons:
                continue
            buttons.append(
                {"name": category, "sub_button": sub_buttons[:MAX_SUB_BUTTONS]}
            )

        if not buttons:
            return None
        # 先填 agentid，保留 {access_token} 占位符交给 _post 填充
        url_template = self._create_menu_url.format(
            access_token="{access_token}", agentid=self._agentid
        )
        return self._post(url_template, {"button": buttons[:MAX_TOP_BUTTONS]})

    def delete_menus(self) -> Optional[bool]:
        """删除企业微信自定义菜单。"""
        token = self.get_access_token()
        if not token:
            return None
        url = self._delete_menu_url.format(access_token=token, agentid=self._agentid)
        res = RequestUtils(timeout=self._timeout).get(url)
        if res is None:
            logger.error("[企业微信通知] 删除菜单失败，未获取到返回信息")
            return None
        try:
            ret = res.json()
        except Exception as err:
            logger.error(f"[企业微信通知] 解析删除菜单响应失败: {err}")
            return None
        return ret.get("errcode") == 0

    def download_media(self, media_id: str) -> Optional[bytes]:
        """下载企业微信临时素材原始字节。"""
        if not media_id:
            return None
        token = self.get_access_token()
        if not token:
            logger.error("[企业微信通知] 下载素材失败：access_token 获取失败")
            return None
        url = self._download_media_url.format(access_token=token, media_id=media_id)
        res = RequestUtils(timeout=30).get_res(url)
        if res is None or not res.content:
            return None
        content_type = (res.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type == "application/json":
            # 企业微信下载失败时返回 JSON 错误体，不是素材内容
            try:
                logger.error(f"[企业微信通知] 下载素材失败：{res.json()}")
            except Exception:
                logger.error("[企业微信通知] 下载素材失败：响应不是合法 JSON")
            return None
        return res.content
