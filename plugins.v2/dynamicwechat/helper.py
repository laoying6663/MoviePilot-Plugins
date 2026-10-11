import re
import os
import json
import time
import threading
import requests
import base64
import hashlib
import asyncio
from typing import Dict, Any
from Crypto import Random
from Crypto.Cipher import AES

import aiohttp

from app.modules.wechat import WeChat
from app.schemas.types import NotificationType, MessageChannel

# settings.json 的读-改-写互斥锁。
# Cookie 保活与多wan口 IP 检测共用同一个 settings.json，各自「读整个文件 → 改一个字段
# → 写回整个文件」，并发时会把对方的字段整个覆盖掉。
_SETTINGS_LOCK = threading.Lock()


def bytes_to_key(data: bytes, salt: bytes, output=48) -> bytes:
    """兼容v2 将bytes_to_key和encrypt导入"""
    if len(salt) != 8:
        raise ValueError(f"salt must be 8 bytes, got {len(salt)}")
    data += salt
    key = hashlib.md5(data).digest()
    final_key = key
    while len(final_key) < output:
        key = hashlib.md5(key + data).digest()
        final_key += key
    return final_key[:output]


def encrypt(message: bytes, passphrase: bytes) -> bytes:
    """
    CryptoJS 加密原文

    This is a modified copy of https://stackoverflow.com/questions/36762098/how-to-decrypt-password-from-javascript-cryptojs-aes-encryptpassword-passphras
    """
    salt = Random.new().read(8)
    key_iv = bytes_to_key(passphrase, salt, 32 + 16)
    key = key_iv[:32]
    iv = key_iv[32:]
    aes = AES.new(key, AES.MODE_CBC, iv)
    length = 16 - (len(message) % 16)
    data = message + (chr(length) * length).encode()
    return base64.b64encode(b"Salted__" + salt + aes.encrypt(data))


class PyCookieCloud:
    """CookieCloud 客户端，提供同步和异步两种操作方式"""

    def __init__(self, url: str, uuid: str, password: str):
        self.url: str = url
        self.uuid: str = uuid
        self.password: str = password

    def check_connection(self) -> bool:
        """同步检查连接（保留兼容）"""
        try:
            resp = requests.get(self.url, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    async def check_connection_async(self) -> bool:
        """异步检查连接"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    def update_cookie(self, formatted_cookies: Dict[str, Any]) -> bool:
        """
        Update cookie data to CookieCloud.

        :param formatted_cookies: cookie value to update.
        :return: if update success, return True, else return False.
        """
        # 去掉上次注入的标记 cookie，避免每次刷新都堆积一个同名 _upload_type
        domain_key = '.work.weixin.qq.com'
        if domain_key not in formatted_cookies:
            formatted_cookies[domain_key] = []
        formatted_cookies[domain_key] = [
            c for c in formatted_cookies[domain_key] if c.get('name') != '_upload_type'
        ]
        formatted_cookies[domain_key].append({
            'name': '_upload_type',
            'value': 'A',
            'domain': '.work.weixin.qq.com',
            'path': '/',
            'expires': -1,
            'httpOnly': False,
            'secure': False,
            'sameSite': 'Lax'
        })

        cookie = {'cookie_data': formatted_cookies}
        raw_data = json.dumps(cookie)
        encrypted_data = encrypt(raw_data.encode('utf-8'), self.get_the_key().encode('utf-8')).decode('utf-8')
        cookie_cloud_request = requests.post(
            self.url + '/update',
            json={'uuid': self.uuid, 'encrypted': encrypted_data},
            timeout=10,  # 必须有超时：CookieCloud 侧卡住会挂死整个保活协程
        )
        if cookie_cloud_request.status_code == 200:
            if cookie_cloud_request.json().get('action') == 'done':
                return True
        return False

    def get_the_key(self) -> str:
        """
        Get the key used to encrypt and decrypt data.

        :return: the key.
        """
        md5 = hashlib.md5()
        md5.update((self.uuid + '-' + self.password).encode('utf-8'))
        return md5.hexdigest()[:16]

    # ---------- 安全的 settings.json 读写 ----------
    @staticmethod
    def read_settings(settings_file: str):
        """
        安全读取 settings.json。

        返回值有三种，必须区分：
          dict —— 成功读到的内容（可能是空 dict）
          None —— 文件存在但**无法解析**（JSON 损坏 / 带 BOM / 类型不对）
          dict(空) —— 文件不存在或路径为空

        区分「不存在」与「解析不了」很关键：调用方 update_settings 拿到空 dict 会
        把它整个写回去，从而抹掉文件里其它所有字段。原实现直接让异常往上抛
        （响亮地失败），这里不能把失败变成静默丢数据。
        """
        if not settings_file or not os.path.exists(settings_file):
            return {}
        # 先按 UTF-8-SIG 读（能吞掉 BOM）；失败再退 GBK（记事本另存的文件可能是 GBK）
        for encoding in ('utf-8-sig', 'gbk'):
            try:
                with open(settings_file, 'r', encoding=encoding) as file:
                    data = json.load(file)
                if isinstance(data, dict):
                    return data
                # 类型不对（例如被写成数组）同样视为「解析不了」，不当作空内容
                return None
            except (json.JSONDecodeError, IOError, OSError, UnicodeDecodeError, ValueError):
                continue
        return None

    @staticmethod
    def write_settings(settings_file, data) -> bool:
        """原子化写 settings.json，避免并发读到写了一半的文件。"""
        if not settings_file:
            return False
        directory = os.path.dirname(settings_file)
        # 临时文件名必须唯一：保活与多wan口 IP 检测可能并发写，
        # 共用同一个 .tmp 会让后一个 os.replace 找不到文件而静默丢写
        tmp_file = f"{settings_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp_file, 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=4, ensure_ascii=False)
            # Windows 上只要还有句柄打开着目标文件，os.replace 就会 PermissionError
            # （CPython 打开文件时不带 FILE_SHARE_DELETE）。并发读时重试几次即可。
            for attempt in range(3):
                try:
                    os.replace(tmp_file, settings_file)
                    return True
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            return False
        except (IOError, OSError, TypeError, ValueError):
            return False
        finally:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass

    @staticmethod
    def update_settings(settings_file, mutator) -> bool:
        """
        在锁内完成「读-改-写」，避免并发写入方互相覆盖字段。

        :param mutator: 接收当前 dict、原地修改的可调用对象
        :return: 是否写入成功
        """
        if not settings_file:
            return False
        with _SETTINGS_LOCK:
            data = PyCookieCloud.read_settings(settings_file)
            if data is None:
                # 拒绝覆盖无法解析的内容，否则一次读失败就会抹掉 _cookie / ips /
                # _cookie_lifetime 全部字段。原实现让异常冒泡，这里明确拒绝并留日志。
                logger.error(f"settings.json 无法解析，已跳过写入以避免数据丢失: {settings_file}")
                return False
            try:
                mutator(data)
            except Exception:
                return False
            return PyCookieCloud.write_settings(settings_file, data)

    # ---------- 同步文件操作方法（保留兼容） ----------
    @staticmethod
    def load_cookie_lifetime(settings_file: str = None):
        return PyCookieCloud.read_settings(settings_file).get('_cookie_lifetime', 0)

    @staticmethod
    def save_cookie_lifetime(settings_file, cookie_lifetime):
        def _mutate(data):
            data['_cookie_lifetime'] = cookie_lifetime

        return PyCookieCloud.update_settings(settings_file, _mutate)

    @staticmethod
    def increase_cookie_lifetime(settings_file, seconds: int):
        # 读改写必须整体在锁内完成，否则并发保活会丢掉增量
        def _mutate(data):
            data['_cookie_lifetime'] = data.get('_cookie_lifetime', 0) + seconds

        PyCookieCloud.update_settings(settings_file, _mutate)

    # ---------- 异步文件操作方法（新增） ----------
    @staticmethod
    async def load_cookie_lifetime_async(settings_file: str = None):
        return await asyncio.to_thread(PyCookieCloud.load_cookie_lifetime, settings_file)

    @staticmethod
    async def save_cookie_lifetime_async(settings_file, cookie_lifetime):
        await asyncio.to_thread(PyCookieCloud.save_cookie_lifetime, settings_file, cookie_lifetime)

    @staticmethod
    async def increase_cookie_lifetime_async(settings_file, seconds: int):
        await asyncio.to_thread(PyCookieCloud.increase_cookie_lifetime, settings_file, seconds)


class MySender:
    """
    多渠道消息发送器
    注意：所有网络请求是同步的，在异步上下文中调用时需用 asyncio.to_thread 包裹
    """

    def __init__(self, token=None, func=None):
        self.raw_token = token or ""

        self.quiet_flag = False
        if self.raw_token.endswith("||Q") or self.raw_token.endswith("||q"):
            self.quiet_flag = True
            token = self.raw_token.rsplit("||", 1)[0]
        else:
            token = self.raw_token

        self.tokens = token.split('||') if token and '||' in token else [token] if token else []
        self.channels = [MySender._detect_channel(t) for t in self.tokens]
        self.current_index = 0
        self.first_text_sent = False
        self.init_success = bool(self.tokens)
        self.post_message_func = func

    @property
    def other_channel(self):
        """
        返回非 WeChat 通道及其对应 token 的列表
        :return: [(channel, token), ...]
        """
        return [(channel, token) for channel, token in zip(self.channels, self.tokens) if channel.lower() != "wechat"]

    @staticmethod
    def _detect_channel(token):
        """根据 token 判断通知渠道"""
        token = token.lower()

        if "wechat" in token:
            return "WeChat"
        if token.startswith("sct"):
            return "ServerChan"
        elif "iyuu" in token:
            return "IYUU"
        else:
            return "PushPlus"

    def send(self, title, content=None, image=None, force_send=False, diy_channel=None, diy_token=None):
        """发送消息（同步方法，在异步上下文中请用 to_thread 包裹）"""
        if not self.init_success:
            return

        if not image and not force_send:
            if self.first_text_sent:
                return
            self.first_text_sent = True

        if diy_channel:
            return self._try_send(title, content, image, channel=diy_channel, diy_token=diy_token)

        for _ in range(len(self.tokens)):
            token = self.tokens[self.current_index]
            channel = self.channels[self.current_index]
            try:
                result = self._try_send(title, content, image, channel, token=token)
                if result is None:
                    return
            except Exception:
                pass
            self.current_index = (self.current_index + 1) % len(self.tokens)
        return "所有的通知方式都发送失败"

    def _try_send(self, title, content, image, channel, token=None, diy_token=None):
        """尝试使用指定通道发送消息"""
        if channel == "WeChat" and self.post_message_func:
            return self._send_v2_wechat(title, content, image, token)
        elif channel == "WeChat":
            return self._send_wechat(title, content, image, token)
        elif channel == "ServerChan":
            return self._send_serverchan(title, content, image, diy_token)
        elif channel == "IYUU":
            return self._send_iyuu(title, content, image, diy_token)
        elif channel == "PushPlus":
            return self._send_pushplus(title, content, image, diy_token)
        else:
            return f"未知的通知方式: {channel}"

    @staticmethod
    def _send_wechat(title, content, image, token):
        wechat = WeChat()
        if token and ',' in token:
            _, actual_userid = token.split(',', 1)
        else:
            actual_userid = None
        if image:
            send_status = wechat.send_msg(title='企业微信登录二维码', image=image, link=image, userid=actual_userid)
        else:
            send_status = wechat.send_msg(title=title, text=content, userid=actual_userid)

        if not send_status:
            return "微信通知发送错误"
        return None

    def _send_serverchan(self, title, content, image, diy_token=None):
        if diy_token:
            tmp_tokens = diy_token
        else:
            tmp_tokens = self.tokens[self.current_index]
        if ',' in tmp_tokens:
            first_part, second_part = tmp_tokens.split(',', 1)
            if first_part.startswith('sctp') and image:
                token = second_part
            else:
                token = first_part
        else:
            token = tmp_tokens

        if token.startswith('sctp'):
            match = re.match(r'sctp(\d+)t', token)
            if match:
                num = match.group(1)
                url = f'https://{num}.push.ft07.com/send/{token}.send'
            else:
                return '错误的Server3 Sendkey'
        else:
            url = f'https://sctapi.ftqq.com/{token}.send'

        params = {'title': title, 'desp': f'![img]({image})' if image else content}
        headers = {'Content-Type': 'application/json;charset=utf-8'}
        response = requests.post(url, json=params, headers=headers)
        result = response.json()
        if result.get('code') != 0:
            return f"Server酱通知错误: {result.get('message')}"
        return None

    def _send_iyuu(self, title, content, image, diy_token=None):
        if diy_token:
            token = diy_token
        else:
            token = self.tokens[self.current_index]

        url = f"https://iyuu.cn/{token}.send"
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        if image:
            desp = f'<img src="{image}" style="max-width: 100%; height: auto;" />'
        else:
            desp = content
        payload = {"text": title, "desp": desp}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            result = response.json()
            if result.get("errcode") != 0:
                return f"爱语飞飞通知错误: {result.get('errmsg')}"
            return None
        except Exception as e:
            return f"爱语飞飞请求异常: {str(e)}"

    def _send_pushplus(self, title, content, image, diy_token=None):
        if diy_token:
            token = diy_token
        else:
            token = self.tokens[self.current_index]
        pushplus_url = f"http://www.pushplus.plus/send/{token}"
        data = {
            "title": title,
            "content": f"企业微信登录二维码<br/><img src='{image}' />" if image else content,
            "template": "html"
        }
        response = requests.post(pushplus_url, json=data)
        result = response.json()
        if result.get('code') != 200:
            return f"PushPlus send failed: {result.get('msg')}"
        return None

    def _send_v2_wechat(self, title, content, image, token):
        """V2 微信通知发送"""
        if token and ',' in token:
            _, actual_userid = token.split(',', 1)
        else:
            actual_userid = None
        self.post_message_func(
            channel=MessageChannel.Wechat,
            mtype=NotificationType.Plugin,
            title=title,
            text=content,
            image=image,
            link=image,
            userid=actual_userid
        )
        return None

    def reset_limit(self):
        """解除限制，允许再次发送纯文本消息"""
        self.first_text_sent = False


class IpLocationParser:
    """
    IP 地址解析器，支持多 WAN 口
    所有文件操作提供同步和异步两种版本
    """

    def __init__(self, settings_file_path, max_ips=3):
        self._settings_file_path = settings_file_path
        self._max_ips = max_ips
        self._ips = self.read_ips("ips")

    @staticmethod
    async def _parse(page, url):
        """异步解析页面，返回 IP 和归属地列表"""
        parser_methods = {
            "https://ip.orz.tools": IpLocationParser._parse_ip_orz_tools,
            "https://ip.skk.moe/multi": IpLocationParser._parse_ip_skk_moe,
            "https://ip.m27.tech": IpLocationParser._parse_ip_m27,
        }
        parser_method = parser_methods.get(url)
        if parser_method is None:
            return [], []
        return await parser_method(page)

    @staticmethod
    def _remove_duplicates(ipv4_addresses, locations):
        """去重并保持 IP 地址和归属地的对应关系"""
        seen = set()
        unique_ipv4 = []
        unique_locations = []

        for ip, location in zip(ipv4_addresses, locations):
            if ip not in seen:
                seen.add(ip)
                unique_ipv4.append(ip)
                unique_locations.append(location)

        return unique_ipv4, unique_locations

    @staticmethod
    def _is_valid_ipv4(ip):
        """验证是否是合法的 IPv4 地址"""
        return re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip) is not None

    @staticmethod
    async def _parse_ip_orz_tools(page):
        """异步解析 https://ip.orz.tools"""
        try:
            await page.wait_for_selector('#results3 .row', timeout=10000)
        except Exception:
            return [], []

        rows = await page.query_selector_all('#results3 .row')
        ipv4_addresses, locations = [], []

        for row in rows:
            row_html = await row.inner_html()

            ip_match = re.search(r'data-name="([^"]+)"', row_html)
            if ip_match:
                ip = ip_match.group(1).strip()
                if not IpLocationParser._is_valid_ipv4(ip):
                    continue
            else:
                continue

            loc_element = await row.query_selector('.loc.cell')
            location = await loc_element.inner_text() if loc_element else "未知"

            ipv4_addresses.append(ip)
            locations.append(location)

        return IpLocationParser._remove_duplicates(ipv4_addresses, locations)

    @staticmethod
    async def _parse_ip_skk_moe(page):
        """异步解析 https://ip.skk.moe/multi"""
        try:
            await page.wait_for_selector(
                'body > div > section > div.x1n2onr6.xw2csxc.x10fe3q7.x116uinm.xdpxx8g > table > tbody > tr',
                timeout=10000
            )
        except Exception:
            return [], []

        rows = await page.query_selector_all(
            'body > div > section > div.x1n2onr6.xw2csxc.x10fe3q7.x116uinm.xdpxx8g > table > tbody > tr'
        )
        ipv4_addresses, locations = [], []

        for row in rows:
            ip_element = await row.query_selector('th')
            loc_element = await row.query_selector('td:nth-child(3)')

            if ip_element and loc_element:
                ip = await ip_element.inner_text()
                ip = ip.strip()
                if not IpLocationParser._is_valid_ipv4(ip):
                    continue
                location = await loc_element.inner_text()
                location = location.strip()

                ipv4_addresses.append(ip)
                locations.append(location)

        return IpLocationParser._remove_duplicates(ipv4_addresses, locations)

    @staticmethod
    async def _parse_ip_m27(page):
        """异步解析 https://ip.m27.tech"""
        try:
            await page.wait_for_selector(
                'body > div > div.panel.panel-success > div.panel-body > table > tbody > tr',
                timeout=10000
            )
        except Exception:
            return [], []

        rows = await page.query_selector_all(
            'body > div > div.panel.panel-success > div.panel-body > table > tbody > tr'
        )
        ipv4_addresses, locations = [], []

        for row in rows:
            row_text = await row.inner_text()
            row_text = row_text.strip()

            ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', row_text)
            if ip_match:
                ip = ip_match.group(1).strip()
                if not IpLocationParser._is_valid_ipv4(ip):
                    continue
            else:
                continue

            location_match = re.search(r'(China|中国).*', row_text)
            location = location_match.group(0).strip() if location_match else "未知"

            ipv4_addresses.append(ip)
            locations.append(location)

        return IpLocationParser._remove_duplicates(ipv4_addresses, locations)

    @staticmethod
    async def get_ipv4(page, url: str) -> str:
        """
        返回多个中国 IP 地址，分号分隔（异步版本）
        :param page: CloakBrowser 异步 Page 对象
        :param url: 目标 URL
        :return: 分号分隔的 IPv4 地址字符串
        """
        try:
            await page.goto(url)
            await page.wait_for_timeout(8000)

            ipv4_addresses, locations = await IpLocationParser._parse(page, url)

            china_ips = [
                ip for ip, location in zip(ipv4_addresses, locations)
                if 'China' in location or '中国' in location
            ]
            return ';'.join(china_ips)
        except Exception as e:
            return ""

    # ---------- 同步文件操作方法（保留兼容） ----------
    def _limit_and_deduplicate_ips(self, ips):
        """去重并限制 IP 地址数量，最多保存 _max_ips 个 IP 地址"""
        unique_ips = list(dict.fromkeys(ips))
        return unique_ips[:self._max_ips]

    def _read_ips_from_json(self, field):
        """从 JSON 文件中读取指定字段的 IP 地址"""
        # 与 PyCookieCloud 共用安全读写，避免两边同时读写 settings.json 造成损坏
        data = PyCookieCloud.read_settings(self._settings_file_path)
        value = data.get(field, [])
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        return ";".join(str(ip) for ip in value)

    def _overwrite_ips_in_json(self, field, new_ips):
        """覆盖写入指定字段的 IP 地址"""
        if isinstance(new_ips, str):
            new_ips = new_ips.split(";")

        new_ips = self._limit_and_deduplicate_ips(new_ips)

        def _mutate(data):
            data[field] = new_ips

        # 读-改-写走加锁路径，避免与 Cookie 保活并发时互相覆盖字段
        PyCookieCloud.update_settings(self._settings_file_path, _mutate)

    def read_ips(self, field) -> str:
        """获取 JSON 文件中指定字段的所有 IP 地址，返回分号分隔的字符串"""
        return self._read_ips_from_json(field)

    def overwrite_ips(self, field, new_ips):
        """覆盖写入指定字段的新 IP 地址"""
        self._overwrite_ips_in_json(field, new_ips)

    def add_ips(self, field, new_ips):
        """增量添加指定字段中的 IP 地址"""
        current_ips = self.read_ips(field).split(";") if self.read_ips(field) else []

        updated_ips = self._limit_and_deduplicate_ips(new_ips.split(";") + current_ips)

        self.overwrite_ips(field, updated_ips)

    # ---------- 异步文件操作方法（新增） ----------
    async def read_ips_async(self, field) -> str:
        return await asyncio.to_thread(self.read_ips, field)

    async def overwrite_ips_async(self, field, new_ips):
        await asyncio.to_thread(self.overwrite_ips, field, new_ips)

    async def add_ips_async(self, field, new_ips):
        await asyncio.to_thread(self.add_ips, field, new_ips)


class JsonFieldManager:
    """
    通用 JSON 配置文件字段管理器。
    所有操作均遵循「读-改-写」模式，确保不修改无关字段。
    提供同步和异步两种操作方式。
    """

    def __init__(self, settings_file_path: str):
        self._settings_file_path = settings_file_path

    def _load(self) -> dict:
        """读取完整 JSON 内容。

        损坏时返回 None（而不是 {}）：调用方要靠它区分「文件不存在」与「文件坏了」，
        否则会把一个损坏的文件用只含单字段的空内容整个覆盖回去。
        """
        if not self._settings_file_path or not os.path.exists(self._settings_file_path):
            return {}
        try:
            with open(self._settings_file_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, IOError, OSError, UnicodeDecodeError, ValueError):
            return None

    def _save(self, data: dict) -> bool:
        """原子化写入。

        原实现是 open(path,'w') + json.dump：先截断再写，中途崩溃/异常会留下半截 JSON，
        而 settings.json 里同时存着 _cookie / ips / _cookie_lifetime 等多个字段，
        一次半截写入等于全部丢失。改走与 PyCookieCloud 同一套原子写。
        """
        return PyCookieCloud.write_settings(self._settings_file_path, data)

    # ---------- 同步操作方法 ----------
    def get(self, field: str, default: Any = None) -> Any:
        """读取指定字段的值"""
        data = self._load()
        return data.get(field, default) if data is not None else default

    def add(self, field: str, value: Any) -> bool:
        """添加新字段。若字段已存在则返回 False"""
        data = self._load()
        if data is None:
            logger.error(f"settings.json 无法解析，跳过写入以避免数据丢失: {self._settings_file_path}")
            return False
        if field in data:
            return False
        data[field] = value
        return self._save(data)

    def update(self, field: str, value: Any) -> bool:
        """修改指定字段的值；若字段不存在则自动创建"""
        data = self._load()
        if data is None:
            logger.error(f"settings.json 无法解析，跳过写入以避免数据丢失: {self._settings_file_path}")
            return False
        data[field] = value
        return self._save(data)

    # ---------- 异步操作方法（新增） ----------
    async def aget(self, field: str, default: Any = None) -> Any:
        return await asyncio.to_thread(self.get, field, default)

    async def aadd(self, field: str, value: Any) -> bool:
        return await asyncio.to_thread(self.add, field, value)

    async def aupdate(self, field: str, value: Any) -> None:
        await asyncio.to_thread(self.update, field, value)