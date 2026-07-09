"""
iLink Bot API 客户端 - 微信官方 ClawBot 协议
基于 https://github.com/qufei1993/cc-weixin 的 API 规范
"""
import requests
import time
import uuid
import secrets
import hashlib
import os
import json
import logging
import threading
from typing import Callable, Optional

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

logger = logging.getLogger(__name__)

CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


class ILINKClient:
    """微信 iLink Bot API 客户端"""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self._update_buf = ""
        self._running = False
        self._message_callback: Optional[Callable] = None
        self._poll_thread: Optional[threading.Thread] = None

    @staticmethod
    def get_qrcode(base_url: str = ""):
        """获取登录二维码"""
        url = f"{base_url}/ilink/bot/get_bot_qrcode" if base_url else "/ilink/bot/get_bot_qrcode"
        resp = requests.get(
            url,
            params={"bot_type": 3},
            timeout=30,
        )
        return resp.json()

    @staticmethod
    def poll_qrcode_status(qrcode_id, base_url: str = "", timeout=180):
        """轮询二维码扫码状态，返回 (status, data)"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                url = f"{base_url}/ilink/bot/get_qrcode_status" if base_url else "/ilink/bot/get_qrcode_status"
                resp = requests.get(
                    url,
                    params={"qrcode": qrcode_id},
                    headers={"iLink-App-ClientVersion": "1"},
                    timeout=10,
                )
                data = resp.json()
                status = data.get("status", "")
                if status in ("confirmed", "expired"):
                    return status, data
                elif status == "scaned":
                    logger.info("QR code scanned, waiting for confirmation...")
            except Exception:
                pass
            time.sleep(2)
        return "timeout", {}

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.token}",
            "X-WECHAT-UIN": secrets.token_bytes(4).hex(),
        }

    def get_updates(self, timeout=45):
        """长轮询获取新消息（超时返回空列表是正常行为）"""
        try:
            resp = self.session.post(
                f"{self.base_url}/ilink/bot/getupdates",
                json={"get_updates_buf": self._update_buf},
                headers=self._headers(),
                timeout=timeout,
            )
            data = resp.json()
            # 响应格式: {"msgs":[], "get_updates_buf":"..."}
            # 没有 ret 字段表示成功
            if "get_updates_buf" in data:
                self._update_buf = data.get("get_updates_buf", self._update_buf)
                return data.get("msgs", [])
            else:
                # 检查是否是 session timeout
                if data.get('errcode') == -14:
                    logger.error("Session expired! Please reconnect via /admin page.")
                    self._running = False  # 停止轮询
                else:
                    logger.warning("getupdates unexpected response: %s", data)
                return []
        except requests.Timeout:
            # 长轮询超时是正常行为，没有新消息
            return []
        except Exception as e:
            logger.error("getupdates error: %s", e)
            return []

    @staticmethod
    def is_success(result: dict) -> bool:
        """判断 iLink API 返回是否成功"""
        return result.get('ret', 0) == 0 and 'errmsg' not in result

    def send_message(self, to_user_id: str, text: str, context_token: str = ""):
        """发送文本消息"""
        try:
            msg_data = {
                "msg": {
                    "to_user_id": to_user_id,
                    "from_user_id": "",
                    "client_id": str(uuid.uuid4()),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                }
            }
            logger.info("send_message to=%s, context_token=%s..., text=%s...",
                       to_user_id, context_token[:20] if context_token else "EMPTY", text[:30])
            resp = self.session.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                json=msg_data,
                headers=self._headers(),
                timeout=30,
            )
            result = resp.json()
            logger.info("send_message response: %s", result)
            return result
        except Exception as e:
            logger.error("sendmessage error: %s", e)
            return {"ret": -1, "errmsg": str(e)}

    def get_upload_url(self, to_user_id, filekey, media_type, raw_size, raw_md5, file_size, aes_key_hex):
        """获取 CDN 上传地址"""
        resp = self.session.post(
            f"{self.base_url}/ilink/bot/getuploadurl",
            json={
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": file_size,
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
            },
            headers=self._headers(),
            timeout=30,
        )
        return resp.json()

    def upload_media_to_cdn(self, media_path, to_user_id, media_type=1):
        """上传媒体文件到 CDN，返回 (encrypt_query_param, aes_key_base64, file_size)
        media_type: 1=图片, 2=视频, 3=语音, 4=文件
        """
        import base64
        with open(media_path, 'rb') as f:
            plaintext = f.read()

        raw_size = len(plaintext)
        raw_md5 = hashlib.md5(plaintext).hexdigest()

        aes_key = secrets.token_bytes(16)
        aes_key_hex = aes_key.hex()
        filekey = secrets.token_hex(16)

        cipher = AES.new(aes_key, AES.MODE_ECB)
        ciphertext = cipher.encrypt(pad(plaintext, AES.block_size))
        file_size = len(ciphertext)

        logger.info("upload_media: path=%s, media_type=%d, raw_size=%d, file_size=%d",
                   media_path, media_type, raw_size, file_size)

        upload_resp = self.get_upload_url(
            to_user_id, filekey, media_type, raw_size, raw_md5, file_size, aes_key_hex
        )
        logger.info("upload_media get_upload_url response: %s", upload_resp)

        upload_url = upload_resp.get("upload_full_url")
        if not upload_url:
            upload_param = upload_resp.get("upload_param")
            if upload_param:
                upload_url = f"{CDN_BASE_URL}/upload?encrypted_query_param={upload_param}&filekey={filekey}"
        if not upload_url:
            raise Exception("No upload URL in response: %s" % upload_resp)

        logger.info("upload_media uploading to: %s", upload_url[:100])
        cdn_resp = self.session.post(
            upload_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
        logger.info("upload_media CDN response: status=%d, headers=%s",
                   cdn_resp.status_code, dict(cdn_resp.headers))

        if not cdn_resp.ok:
            raise Exception("CDN upload failed: HTTP %d" % cdn_resp.status_code)

        encrypt_query_param = cdn_resp.headers.get("x-encrypted-param", "")
        # aes_key 格式: base64(hex_string) — 不是 base64(raw_bytes)
        aes_key_base64 = base64.b64encode(aes_key_hex.encode('utf8')).decode()

        logger.info("upload_media done: encrypt_param_len=%d, aes_key=%s",
                   len(encrypt_query_param), aes_key_base64)

        return encrypt_query_param, aes_key_base64, file_size

    def upload_image_to_cdn(self, image_path, to_user_id):
        """上传图片到 CDN（兼容旧接口）"""
        return self.upload_media_to_cdn(image_path, to_user_id, media_type=1)

    def send_image(self, to_user_id: str, image_path: str, context_token: str = ""):
        """发送图片消息"""
        try:
            encrypt_param, aes_key_b64, file_size = self.upload_image_to_cdn(image_path, to_user_id)
            logger.info("send_image upload: encrypt_param=%s, aes_key=%s, file_size=%d",
                       encrypt_param, aes_key_b64, file_size)

            # 获取图片尺寸
            width, height = 0, 0
            try:
                from PIL import Image
                with Image.open(image_path) as img:
                    width, height = img.size
            except Exception:
                pass

            msg_data = {
                "msg": {
                    "to_user_id": to_user_id,
                    "from_user_id": "",
                    "client_id": str(uuid.uuid4()),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": encrypt_param,
                                "aes_key": aes_key_b64,
                                "encrypt_type": 1,
                            },
                            "mid_size": file_size,
                            "width": width,
                            "height": height,
                        },
                    }],
                }
            }
            logger.info("send_image request: %s", json.dumps(msg_data, indent=2)[:500])

            resp = self.session.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                json=msg_data,
                headers=self._headers(),
                timeout=30,
            )
            result = resp.json()
            logger.info("send_image response: %s", result)
            return result
        except Exception as e:
            logger.error("send_image error: %s", e)
            return {"ret": -1, "errmsg": str(e)}

    def send_voice(self, to_user_id: str, voice_path: str, duration: int, context_token: str = ""):
        """发送语音消息"""
        try:
            encrypt_param, aes_key_b64, file_size = self.upload_media_to_cdn(voice_path, to_user_id, media_type=3)
            resp = self.session.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                json={
                    "msg": {
                        "to_user_id": to_user_id,
                        "from_user_id": "",
                        "client_id": str(uuid.uuid4()),
                        "message_type": 4,
                        "message_state": 2,
                        "context_token": context_token,
                        "item_list": [{
                            "type": 3,
                            "voice_item": {
                                "media": {
                                    "encrypt_query_param": encrypt_param,
                                    "aes_key": aes_key_b64,
                                    "encrypt_type": 1,
                                },
                                "duration": duration,
                            },
                        }],
                    }
                },
                headers=self._headers(),
                timeout=30,
            )
            result = resp.json()
            logger.info("send_voice response: %s", result)
            return result
        except Exception as e:
            logger.error("send_voice error: %s", e)
            return {"ret": -1, "errmsg": str(e)}

    def send_video(self, to_user_id: str, video_path: str, duration: int, context_token: str = ""):
        """发送视频消息"""
        try:
            encrypt_param, aes_key_b64, file_size = self.upload_media_to_cdn(video_path, to_user_id, media_type=2)
            resp = self.session.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                json={
                    "msg": {
                        "to_user_id": to_user_id,
                        "from_user_id": "",
                        "client_id": str(uuid.uuid4()),
                        "message_type": 5,
                        "message_state": 2,
                        "context_token": context_token,
                        "item_list": [{
                            "type": 4,
                            "video_item": {
                                "media": {
                                    "encrypt_query_param": encrypt_param,
                                    "aes_key": aes_key_b64,
                                    "encrypt_type": 1,
                                },
                                "duration": duration,
                            },
                        }],
                    }
                },
                headers=self._headers(),
                timeout=30,
            )
            result = resp.json()
            logger.info("send_video response: %s", result)
            return result
        except Exception as e:
            logger.error("send_video error: %s", e)
            return {"ret": -1, "errmsg": str(e)}

    def send_file(self, to_user_id: str, file_path: str, file_name: str, context_token: str = ""):
        """发送文件消息"""
        try:
            encrypt_param, aes_key_b64, file_size = self.upload_media_to_cdn(file_path, to_user_id, media_type=4)
            resp = self.session.post(
                f"{self.base_url}/ilink/bot/sendmessage",
                json={
                    "msg": {
                        "to_user_id": to_user_id,
                        "from_user_id": "",
                        "client_id": str(uuid.uuid4()),
                        "message_type": 6,
                        "message_state": 2,
                        "context_token": context_token,
                        "item_list": [{
                            "type": 5,
                            "file_item": {
                                "media": {
                                    "encrypt_query_param": encrypt_param,
                                    "aes_key": aes_key_b64,
                                    "encrypt_type": 1,
                                },
                                "file_name": file_name,
                                "file_size": file_size,
                            },
                        }],
                    }
                },
                headers=self._headers(),
                timeout=30,
            )
            result = resp.json()
            logger.info("send_file response: %s", result)
            return result
        except Exception as e:
            logger.error("send_file error: %s", e)
            return {"ret": -1, "errmsg": str(e)}

    def send_image_with_text(self, to_user_id: str, text: str, image_path: str, context_token: str = ""):
        """先发文字再发图片"""
        self.send_message(to_user_id, text, context_token)
        time.sleep(0.5)
        return self.send_image(to_user_id, image_path, context_token)

    def download_media(self, url: str, save_path: str, timeout: int = 120) -> bool:
        """从 URL 下载媒体文件到本地"""
        try:
            resp = self.session.get(url, timeout=timeout, stream=True)
            if resp.status_code != 200:
                logger.error("Download failed: HTTP %d", resp.status_code)
                return False

            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info("Downloaded media to %s", save_path)
            return True
        except Exception as e:
            logger.error("Download media error: %s", e)
            return False

    def get_config(self, user_id: str, context_token: str = ""):
        """获取用户配置"""
        try:
            resp = self.session.post(
                f"{self.base_url}/ilink/bot/getconfig",
                json={
                    "ilink_user_id": user_id,
                    "context_token": context_token,
                },
                headers=self._headers(),
                timeout=30,
            )
            return resp.json()
        except Exception as e:
            logger.error("getconfig error: %s", e)
            return {"ret": -1, "errmsg": str(e)}

    def start_polling(self, callback: Callable):
        """启动消息轮询"""
        self._message_callback = callback
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="ilink-poll"
        )
        self._poll_thread.start()
        logger.info("iLink polling started")
        return self._poll_thread

    def _poll_loop(self):
        while self._running:
            try:
                msgs = self.get_updates(timeout=40)
                for msg in msgs:
                    if self._message_callback:
                        try:
                            self._message_callback(msg)
                        except Exception as e:
                            logger.error("Message handler error: %s", e)
            except Exception as e:
                logger.error("Poll loop error: %s", e)
                time.sleep(5)

    def stop(self):
        """停止轮询"""
        self._running = False
        logger.info("iLink polling stopped")
