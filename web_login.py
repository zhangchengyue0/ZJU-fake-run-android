#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_login.py —— 拉起浏览器完成浙大 CAS 登录，并抓取打卡所需凭据。

流程：
  1) 打开 CAS 登录页（service 指向 App 的 appLogin）。
  2) 你手动输入 学号/密码(+验证码) 登录。
  3) 登录成功后浏览器跳到 appLogin 页面，其源码内嵌 `jwtToken = <JWT>.<sig>.<SECRET>.<exp>`。
  4) 脚本抓取并解析出 cgAuthorization(前两段) 与 SECRET(第三段)，写入 session.json。

运行：python web_login.py
在完成前会一直保持浏览器打开（默认最多等 15 分钟）。
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
import datetime as _dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION = HERE / "session.json"
LOG = HERE / "web_login.log"

CAS_URL = ("https://zjuam.zju.edu.cn/cas/login"
           "?service=http://www.tyys.zju.edu.cn/cgapp-server/cas/appLogin")
DEFAULT_IMEI = str(uuid.uuid4())
WAIT_SECONDS = 15 * 60


def log(msg: str):
    line = "[%s] %s" % (_dt.datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def parse_jwt_token(tok: str):
    """jwtToken = <JWT>.<sig>.<SECRET>.<exp>  ->  (cgAuthorization, SECRET, exp)"""
    parts = tok.split(".")
    # 末段是 13 位毫秒时间戳
    if len(parts) >= 4 and parts[-1].isdigit():
        exp = parts[-1]
        secret = parts[-2]
        cg = ".".join(parts[:-2])
    else:
        cg = tok
        secret = ""
        exp = ""
    return cg, secret, exp


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        log("playwright 不可用：%s" % e)
        return 1

    if LOG.exists():
        LOG.unlink()
    log("启动浏览器…")
    with sync_playwright() as p:
        last_err = None
        browser = None
        for kwargs in ({"channel": "chrome"}, {}, {"channel": "msedge"}):
            try:
                browser = p.chromium.launch(headless=False, args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ], **kwargs)
                log("已用 %s 启动" % (kwargs.get("channel", "bundled-chromium")))
                break
            except Exception as e:
                last_err = e
        if browser is None:
            log("无法启动浏览器：%s" % last_err)
            return 2

        ctx = browser.new_context(viewport=None)
        page = ctx.new_page()
        page.goto(CAS_URL, wait_until="domcontentloaded", timeout=90000)
        log("请在打开的浏览器里登录（学号/密码/验证码）… 登录后本脚本会自动抓取。")

        result = None
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            try:
                html = page.content()
            except Exception:
                time.sleep(1.0)
                continue

            user = secret = cg = exp = None
            m = re.search(r"'jwtToken'\s*:\s*'([^']+)'", html)
            if not m:
                m = re.search(r'"jwtToken"\s*:\s*"([^"]+)"', html)
            if m:
                tok = m.group(1)
                cg, secret, exp = parse_jwt_token(tok)
                mu = re.search(r"'username'\s*:\s*'([^']+)'", html) or \
                     re.search(r'"username"\s*:\s*"([^"]+)"', html)
                user = mu.group(1) if mu else None
            else:
                m2 = re.search(r"getUserName\(\s*'([^']*)'\s*,\s*'([^']+)'", html)
                if m2:
                    user, tok = m2.group(1), m2.group(2)
                    cg, secret, exp = parse_jwt_token(tok)

            if secret and secret.startswith("AAAA"):
                # 从 JWT payload 里取 sub（学号）兜底
                if not user and cg.count(".") >= 1:
                    try:
                        import base64
                        payload = cg.split(".")[1]
                        payload += "=" * (-len(payload) % 4)
                        user = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
                    except Exception:
                        pass
                result = {"username": user, "cgAuthorization": cg,
                          "secret": secret, "exp": exp}
                break
            time.sleep(1.2)

        if not result:
            log("超时（%d 分钟）仍未捕获到登录凭据。" % (WAIT_SECONDS // 60))
            try:
                browser.close()
            except Exception:
                pass
            return 3

        sess = {
            "username": result["username"],
            "cgAuthorization": result["cgAuthorization"],
            "secret": result["secret"],
            "exp": result["exp"],
            "imei": DEFAULT_IMEI,
            "captured_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        SESSION.write_text(json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")
        log("✅ 已捕获：user=%s  SECRET=%s…  exp=%s" % (sess["username"], sess["secret"][:16], sess["exp"]))
        log("已写入 %s" % SESSION)
        time.sleep(1.0)
        try:
            browser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
