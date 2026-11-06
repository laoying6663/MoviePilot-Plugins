"""企业微信回调消息的加解密实现。

算法与腾讯官方《企业微信发送给企业后台的消息加解密示例代码》一致。
插件不允许依赖宿主适配器（app.adapters.*），因此按公开算法重新实现一份。

本模块刻意不导入任何宿主模块，只依赖标准库和 pycryptodome，
这样加解密逻辑可以脱离宿主单独测试。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - 缺少依赖时由调用方给出明确提示
    AES = None

# 返回码，与官方示例保持一致，便于按码定位失败原因
WXBizMsgCrypt_OK = 0
WXBizMsgCrypt_ValidateSignature_Error = -40001
WXBizMsgCrypt_ParseXml_Error = -40002
WXBizMsgCrypt_ComputeSignature_Error = -40003
WXBizMsgCrypt_IllegalAesKey = -40004
WXBizMsgCrypt_ValidateCorpid_Error = -40005
WXBizMsgCrypt_EncryptAES_Error = -40006
WXBizMsgCrypt_DecryptAES_Error = -40007
WXBizMsgCrypt_IllegalBuffer = -40008

_BLOCK_SIZE = 32


class WXBizMsgCryptError(Exception):
    """回调消息加解密失败。"""


def crypto_available() -> bool:
    """返回当前环境是否具备 AES 能力。"""
    return AES is not None


def get_signature(token: str, timestamp, nonce, encrypt: str) -> str:
    """按企业微信约定计算 SHA1 签名：四个参数字典序拼接后取 SHA1。"""
    parts = sorted([str(token), str(timestamp), str(nonce), str(encrypt)])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def aes_key_from_encoding(encoding_aes_key: str) -> bytes:
    """从 EncodingAESKey 还原 32 字节 AES 密钥。"""
    raw_key = (encoding_aes_key or "").strip()
    if not raw_key:
        raise WXBizMsgCryptError("EncodingAESKey 为空")
    try:
        raw = base64.b64decode(raw_key + "=")
    except Exception as err:
        # 用户把 EncodingAESKey 填错时 binascii 会抛自己的异常，这里统一转成
        # WXBizMsgCryptError，调用方只需要捕获一种错误类型。
        raise WXBizMsgCryptError(f"EncodingAESKey 不是合法 base64：{err}") from err
    if len(raw) != _BLOCK_SIZE:
        raise WXBizMsgCryptError(
            f"EncodingAESKey 无效，解出 {len(raw)} 字节，应为 {_BLOCK_SIZE} 字节"
        )
    return raw


def _pkcs7_pad(data: bytes) -> bytes:
    """按 32 字节块补位。"""
    amount = _BLOCK_SIZE - (len(data) % _BLOCK_SIZE)
    return data + bytes([amount]) * amount


def _pkcs7_unpad(data: bytes) -> bytes:
    """去除补位字节，并校验补位是否合法。"""
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > _BLOCK_SIZE or pad > len(data):
        raise WXBizMsgCryptError("明文填充非法")
    return data[:-pad]


def encrypt_message(msg: str, receive_id: str, encoding_aes_key: str) -> str:
    """加密明文消息，返回 base64 密文。

    密文结构：16 位随机串 + 网络序 4 字节长度 + 原文 + receiveid，整体补位后 AES-CBC。
    """
    if AES is None:
        raise WXBizMsgCryptError("缺少 pycryptodome，无法进行回调加解密")
    key = aes_key_from_encoding(encoding_aes_key)
    raw = msg.encode("utf-8")
    buffer = (
        os.urandom(16)
        + struct.pack(">I", len(raw))
        + raw
        + receive_id.encode("utf-8")
    )
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(_pkcs7_pad(buffer))).decode("utf-8")


def decrypt_message(encrypt: str, receive_id: str, encoding_aes_key: str) -> str:
    """解密 base64 密文并校验 receiveid，返回明文 XML 字符串。"""
    if AES is None:
        raise WXBizMsgCryptError("缺少 pycryptodome，无法进行回调加解密")
    key = aes_key_from_encoding(encoding_aes_key)
    try:
        cipher = AES.new(key, AES.MODE_CBC, key[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt))
    except Exception as err:
        raise WXBizMsgCryptError(f"AES 解密失败: {err}") from err
    try:
        body = _pkcs7_unpad(plain)[16:]
        msg_len = struct.unpack(">I", body[:4])[0]
        if 4 + msg_len >= len(body):
            raise WXBizMsgCryptError("明文长度字段越界")
        msg = body[4 : 4 + msg_len].decode("utf-8")
        from_receive_id = body[4 + msg_len :].decode("utf-8")
    except WXBizMsgCryptError:
        raise
    except Exception as err:
        raise WXBizMsgCryptError(f"明文结构非法: {err}") from err
    if from_receive_id != receive_id:
        raise WXBizMsgCryptError(
            f"企业ID 校验失败，期望 {receive_id}，实际 {from_receive_id}"
        )
    return msg


def parse_xml_message(xml_text: str) -> dict:
    """把回调 XML 解析成字典；解析失败返回空字典。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    def _value(tag: str, default: Optional[str] = None) -> Optional[str]:
        node = root.find(tag)
        if node is None or node.text is None:
            return default
        return node.text.strip()

    return {
        "ToUserName": _value("ToUserName"),
        "FromUserName": _value("FromUserName"),
        "CreateTime": _value("CreateTime"),
        "MsgType": _value("MsgType"),
        "Content": _value("Content", ""),
        "MsgId": _value("MsgId"),
        "AgentID": _value("AgentID"),
        "Event": _value("Event"),
        "EventKey": _value("EventKey"),
        "MediaId": _value("MediaId"),
        "PicUrl": _value("PicUrl"),
        "FileName": _value("FileName"),
        "Recognition": _value("Recognition", ""),
    }


class WXBizMsgCrypt:
    """企业微信回调加解密门面。

    构造时立即校验 EncodingAESKey：配置错误应该在保存时就暴露，
    而不是等到企业微信推第一条回调才失败。
    """

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.receive_id = receive_id
        self.encoding_aes_key = encoding_aes_key
        # 提前校验，配置缺失时直接抛出
        aes_key_from_encoding(encoding_aes_key)

    def verify_url(
        self, msg_signature: str, timestamp, nonce: str, echostr: str
    ) -> Tuple[int, Optional[str]]:
        """校验企业微信回调地址，返回解密后的 echostr。"""
        signature = get_signature(self.token, timestamp, nonce, echostr)
        if signature != msg_signature:
            return WXBizMsgCrypt_ValidateSignature_Error, None
        try:
            reply = decrypt_message(echostr, self.receive_id, self.encoding_aes_key)
        except WXBizMsgCryptError:
            return WXBizMsgCrypt_DecryptAES_Error, None
        return WXBizMsgCrypt_OK, reply

    def decrypt_msg(
        self, post_data, msg_signature: str, timestamp, nonce: str
    ) -> Tuple[int, Optional[str]]:
        """校验并解密回调消息体，返回明文 XML。"""
        try:
            root = ET.fromstring(post_data)
        except ET.ParseError:
            return WXBizMsgCrypt_ParseXml_Error, None
        encrypt_node = root.find("Encrypt")
        if encrypt_node is None or not encrypt_node.text:
            return WXBizMsgCrypt_ParseXml_Error, None
        encrypt = encrypt_node.text.strip()

        signature = get_signature(self.token, timestamp, nonce, encrypt)
        if signature != msg_signature:
            return WXBizMsgCrypt_ValidateSignature_Error, None
        try:
            content = decrypt_message(encrypt, self.receive_id, self.encoding_aes_key)
        except WXBizMsgCryptError:
            return WXBizMsgCrypt_DecryptAES_Error, None
        return WXBizMsgCrypt_OK, content

    def encrypt_msg(
        self, reply_msg: str, nonce: str, timestamp=None
    ) -> Tuple[int, Optional[str]]:
        """把被动回复的明文 XML 加密成企业微信可直接返回的密文 XML。"""
        if timestamp is None:
            timestamp = str(int(time.time()))
        try:
            encrypt = encrypt_message(reply_msg, self.receive_id, self.encoding_aes_key)
        except WXBizMsgCryptError:
            return WXBizMsgCrypt_EncryptAES_Error, None
        signature = get_signature(self.token, timestamp, nonce, encrypt)
        reply_xml = (
            "<xml>\n"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>\n"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>\n"
            f"<TimeStamp>{timestamp}</TimeStamp>\n"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>\n"
            "</xml>"
        )
        return WXBizMsgCrypt_OK, reply_xml
