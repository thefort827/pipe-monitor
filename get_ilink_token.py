"""
获取 iLink Bot API Token
使用方法：python get_ilink_token.py

流程：
1. 生成二维码
2. 用微信扫描
3. 确认连接
4. 获取 token
"""
import os
import requests
import time
import sys

from dotenv import load_dotenv
load_dotenv()

BASE_URL = os.getenv("ILINK_API_BASE", "https://ilinkai.weixin.qq.com")


def get_qrcode():
    """获取登录二维码"""
    resp = requests.get(f"{BASE_URL}/ilink/bot/get_qrcode?bot_type=3", timeout=30)
    data = resp.json()
    print("QR Code ID:", data.get("qrcode"))
    print("QR Code URL:", data.get("qrcode_img_content", ""))
    return data.get("qrcode"), data.get("qrcode_img_content", "")


def poll_status(qrcode_id, timeout=300):
    """轮询扫码状态"""
    deadline = time.time() + timeout
    print(f"\n请用微信扫描上方二维码（{timeout}秒超时）...")

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{BASE_URL}/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_id},
                headers={"iLink-App-ClientVersion": "1"},
                timeout=10,
            )
            data = resp.json()
            status = data.get("status", "")

            if status == "confirmed":
                print("\n连接成功！")
                print("=" * 50)
                print("bot_token:", data.get("bot_token"))
                print("base_url:", data.get("baseurl"))
                print("user_id:", data.get("ilink_user_id"))
                print("=" * 50)
                return data
            elif status == "scaned":
                print("已扫码，等待确认...")
            elif status == "expired":
                print("二维码已过期，请重新运行脚本")
                return None

        except requests.Timeout:
            pass
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(2)

    print("超时未扫码")
    return None


def main():
    print("iLink Bot Token 获取工具")
    print("=" * 50)
    print("请确保：")
    print("1. 微信版本 >= 8.0.70 (iOS) / 8.0.69 (Android)")
    print("2. 已启用 iLink Bot (ClawBot) 功能")
    print("=" * 50)

    qrcode_id, qrcode_url = get_qrcode()

    if qrcode_url:
        # 在终端显示二维码 URL
        print(f"\n请在浏览器中打开以下链接查看二维码：")
        print(f"{qrcode_url}\n")

    result = poll_status(qrcode_id)

    if result and result.get("bot_token"):
        print("\n使用方法：")
        print(f'ILINK_TOKEN={result["bot_token"]}')


if __name__ == "__main__":
    main()
