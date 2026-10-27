#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py —— 浙大体艺运动打卡：两段式通讯脚本
==========================================
把"一次打卡"拆成两段，因为服务器会校验「取 sportsId → 提交」之间必须真的
经过了运动时长（否则 savesports 返回 `数据异常 -201/24`）。

    python run.py start                     # 跑步开始前：取 sportsId 并记时间
    #  ... 真实跑步 / 或等待约等于时长 ...
    python run.py finish                    # 跑完：提交 prejudgment + savesports
    python run.py auto --km 3.2 --pace 330  # (测试用)自动等待=时长后提交

依赖：crypto(zju_api/native_sign/track)、requests；轨迹生成见 track.py（需补全）。
凭据：session.json（由 web_login.py 生成）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import sys
import time

import requests

import zju_api as zapi
import native_sign as ns
import track

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "session.json")
STATE = os.path.join(HERE, "run_state.json")

BASE = "http://tyys.zju.edu.cn"
CG = "/cgapp-server"
APP_KEY = "azk3t4jrcfm5772t"
SPORT_TYPE_ID = "18"                      # 跑步
UA = "zdapp/2.9.5(Linux;Android 9;samsung/TP1A.220624.014)"


def now_ms() -> int:
    return int(time.time() * 1000)


class CgClient:
    def __init__(self, sess: dict):
        self.secret = sess["secret"]
        self.token = sess["cgAuthorization"]
        self.imei = sess.get("imei", "")
        self.xh = sess["username"]
        self.ua = UA
        self.http = requests.Session()

    def _headers(self, ms: int, sign: str, ts: int, form: bool = False) -> dict:
        client = zapi.build_client(self.secret, ms)
        h = {
            "app-key": APP_KEY, "Accept": "*/*",
            "v1": zapi.compute_v1(ms),
            "timestamp": str(ts),
            "Accept-Language": "en-US;q=1, zh-Hant-US;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "imei": self.imei, "User-Agent": self.ua,
            "cgAuthorization": self.token, "client": client,
            "sign": sign, "Connection": "keep-alive",
        }
        if form:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return h

    def get(self, path_q: str):
        url = BASE + CG + path_q
        ms = now_ms()
        sign = zapi.raw_sign(self.secret, zapi.canonical_path(zapi.path_for_signing(url)), ms)
        ts = ns.emu_timestamp(self.secret, ms)
        r = self.http.get(url, headers=self._headers(ms, sign, ts), timeout=20)
        return r.json()

    def post_form(self, path_q: str, pairs):
        url = BASE + CG + path_q
        body = "&".join("%s=%s" % (k, v) for k, v in pairs)
        ms = now_ms()
        sign = zapi.raw_sign(self.secret, zapi.canonical_post(url, pairs), ms)
        ts = ns.emu_timestamp(self.secret, ms)
        r = self.http.post(url, headers=self._headers(ms, sign, ts, form=True),
                           data=body.encode(), timeout=60)
        return r.json()


def load_session():
    if not os.path.exists(SESSION):
        sys.exit("缺少 session.json，请先运行 web_login.py 登录")
    return json.load(open(SESSION, encoding="utf-8"))


def cmd_start(args):
    cli = CgClient(load_session())
    print("currentTimeMillis:", cli.get("/api/l/v6/currentTimeMillis?"))
    r = cli.get("/api/l/v7/sportsId?")
    print("sportsId:", r)
    sid = (r or {}).get("data")
    if not sid:
        sys.exit("取 sportsId 失败")
    json.dump({"sportId": sid, "begin_ms": now_ms()}, open(STATE, "w"))
    print("已记录 sportId=%s，开始计时。跑完运行： python run.py finish" % sid)


def _submit(args, sid, begin_dt):
    cli = CgClient(load_session())
    print("inSportTime:", cli.get("/api/l/v6.1/sporttype/inSportTime?&sportTypeId=" + SPORT_TYPE_ID))
    name = args.name
    if not name:                                   # 姓名留空 → 自动从服务器取
        info = cli.get("/api/l/v6/u/info?&xh=" + cli.xh)
        name = ((info.get("data") or {}).get("xm", "") if isinstance(info.get("data"), dict) else "")
    js = track.build_jsonsports(cli.xh, name, sid, begin_dt, imei=cli.imei,
                                rng=random.Random(), include_coordinate=True)
    jsp = {k: v for k, v in js.items() if k != "coordinate"}
    vp = zapi.encrypt_jsonsports(json.dumps(jsp, ensure_ascii=False, separators=(",", ":")), cli.secret)
    vs = zapi.encrypt_jsonsports(json.dumps(js, ensure_ascii=False, separators=(",", ":")), cli.secret)
    print("prejudgment:", cli.post_form("/api/l/v6.1/prejudgment?", [("jsonsports", vp)]))
    print("savesports :", cli.post_form("/api/l/v7/savesports?", [("jsonsports", vs)]))


def cmd_finish(args):
    st = json.load(open(STATE, encoding="utf-8"))
    begin = _dt.datetime.fromtimestamp(st["begin_ms"] / 1000)
    _submit(args, st["sportId"], begin)


def cmd_auto(args):
    cli = CgClient(load_session())
    print("currentTimeMillis:", cli.get("/api/l/v6/currentTimeMillis?"))
    r = cli.get("/api/l/v7/sportsId?")
    print("sportsId:", r)
    sid = (r or {}).get("data")
    if not sid:
        sys.exit("取 sportsId 失败")
    begin = _dt.datetime.now()
    dur = int(args.km * args.pace)                       # 秒
    print("等待 %d 秒（≈运动时长）..." % dur)
    for i in range(dur):
        time.sleep(1)
        if i % 60 == 0 and i:
            print("  已等待 %d 分钟" % (i // 60))
    _submit(args, sid, begin)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["start", "finish", "auto"])
    ap.add_argument("--name", default="", help="姓名；留空则自动从服务器获取")
    ap.add_argument("--km", type=float, default=3.2)
    ap.add_argument("--pace", type=float, default=330.0)
    args = ap.parse_args()
    {"start": cmd_start, "finish": cmd_finish, "auto": cmd_auto}[args.mode](args)


if __name__ == "__main__":
    main()
