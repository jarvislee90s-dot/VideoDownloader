# -*- coding: utf-8 -*-
"""微信视频号登录窗口：弹出专用浏览器 → 用户扫码 → 登录态持久化到 profile_browser/ 目录。

用法：python wechat_login.py
扫码成功（跳离 /login.html）后窗口自动关闭，后续下载自动复用该登录态。

v2 修复：登录判据从"页面内容无扫码字样"改为强判据"URL 离开 /login.html"。
v1 的弱判据会在二维码刚渲染出来时（页面还没显示'扫码'文案）误判成功并提前关窗。
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).parent / "profile_browser"
LOGIN_URL = "https://channels.weixin.qq.com"


def is_logged_in(page) -> bool:
    """强判据：登录成功后微信会从 /login.html 跳转到工作台 URL。"""
    try:
        url = page.url
        return "channels.weixin.qq.com" in url and "/login.html" not in url
    except Exception:
        return False


def main():
    print(f"登录态目录: {PROFILE_DIR}")
    print("正在打开浏览器窗口，请在页面中用微信扫码登录...")
    print("登录成功（页面跳转离开登录页）后窗口会自动关闭。最长等待 10 分钟。\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1100, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, timeout=60000)

        deadline = time.time() + 600
        logged_in = False
        while time.time() < deadline:
            for pg in ctx.pages:
                if is_logged_in(pg):
                    logged_in = True
                    break
            if logged_in:
                break
            time.sleep(2)

        if logged_in:
            time.sleep(3)  # 给 cookies/localStorage 落盘时间
            print("✅ 检测到登录成功（已跳转工作台）！登录态已保存到 profile_browser/ 目录。")
        else:
            print("❌ 10 分钟内未检测到登录成功。请重新运行本命令再试。")

        ctx.close()

    sys.exit(0 if logged_in else 1)


if __name__ == "__main__":
    main()
