#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py —— 浙大体艺打卡 · 图形界面（一键运行）
==========================================
- 学号/姓名：**自动**（学号取自会话，姓名取自服务器 u/info），无需手填
- 参数：圈数(固定8) / 初始配速 / 每圈衰竭 / 定位偏移 / 抖动 / 平台
- 🎲 随机填入：圈数锁定 8，衰竭 15~60，定位偏移 [-2, 2]
- 🚀 一键打卡：登录 → 自动取 sportId → 倒计时 → 自动生成数据并发送
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import random
import subprocess
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import zju_api as zapi                 # noqa: E402
import native_sign as ns               # noqa: E402
from track import Track, build_jsonsports   # noqa: E402

BASE = "http://tyys.zju.edu.cn"
CG = "/cgapp-server"
APP_KEY = "azk3t4jrcfm5772t"
SPORT_TYPE_ID = "18"
SESSION = os.path.join(HERE, "session.json")
UA = "zdapp/2.9.5(Linux;Android 9;samsung/TP1A.220624.014)"


def now_ms():
    return int(time.time() * 1000)


class Client:
    def __init__(self, sess):
        self.secret = sess["secret"]
        self.token = sess["cgAuthorization"]
        self.imei = sess.get("imei") or str(uuid.uuid4())
        self.xh = sess["username"]
        self.ua = UA
        self.http = requests.Session()

    def _h(self, ms, sign, ts, form=False):
        h = {"app-key": APP_KEY, "Accept": "*/*", "v1": zapi.compute_v1(ms),
             "timestamp": str(ts), "Accept-Language": "en-US;q=1, zh-Hant-US;q=0.9",
             "Accept-Encoding": "gzip, deflate", "imei": self.imei, "User-Agent": self.ua,
             "cgAuthorization": self.token,
             "client": zapi.build_client(self.secret, ms),
             "sign": sign, "Connection": "keep-alive"}
        if form:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return h

    def get(self, pq):
        url = BASE + CG + pq
        ms = now_ms()
        sign = zapi.raw_sign(self.secret, zapi.canonical_path(zapi.path_for_signing(url)), ms)
        ts = ns.emu_timestamp(self.secret, ms)
        return self.http.get(url, headers=self._h(ms, sign, ts), timeout=20).json()

    def post_form(self, pq, pairs):
        url = BASE + CG + pq
        ms = now_ms()
        sign = zapi.raw_sign(self.secret, zapi.canonical_post(url, pairs), ms)
        ts = ns.emu_timestamp(self.secret, ms)
        body = "&".join("%s=%s" % (k, v) for k, v in pairs)
        return self.http.post(url, headers=self._h(ms, sign, ts, True),
                              data=body.encode(), timeout=60).json()


def run_duration(laps, pace, decay):
    lap_km = Track(laps=laps).lap_m / 1000.0
    return int(sum(lap_km * (pace + decay * k) for k in range(laps)))


class App:
    def __init__(self, root):
        self.root = root
        root.title("浙大体艺 打卡工具")
        root.geometry("780x660")
        self.busy = False
        self.cancel = False
        self._build()

    # ---------- UI ----------
    def _row(self, parent, r, label, var, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(parent, textvariable=var, width=12).grid(row=r, column=1, sticky="w", padx=6)
        if hint:
            ttk.Label(parent, text=hint, foreground="#888").grid(row=r, column=2, columnspan=2, sticky="w")

    def _build(self):
        top = ttk.LabelFrame(self.root, text="账号（自动）")
        top.pack(fill="x", padx=10, pady=6)
        self.lbl_user = ttk.Label(top, text="学号：—    姓名：—")
        self.lbl_user.pack(anchor="w", padx=8, pady=4)
        ttk.Button(top, text="登录 / 刷新会话", command=self.login).pack(anchor="w", padx=8, pady=(0, 6))

        pf = ttk.LabelFrame(self.root, text="参数设置")
        pf.pack(fill="x", padx=10, pady=6)
        self.v = {k: tk.StringVar() for k in ["laps", "pace", "decay", "ox", "oy", "jitter"]}
        self.v["laps"].set("8"); self.v["pace"].set("330"); self.v["decay"].set("30")
        self.v["ox"].set("0"); self.v["oy"].set("0"); self.v["jitter"].set("1.0")
        ttk.Label(pf, text="里程/圈数").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(pf, textvariable=self.v["laps"], values=["8", "6"], width=6,
                     state="readonly").grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(pf, text="3km(8圈) / 2km(6圈)", foreground="#888").grid(row=0, column=2, columnspan=2, sticky="w")
        self._row(pf, 1, "初始配速(秒/千米)", self.v["pace"], "330 = 5'30''/km")
        self._row(pf, 2, "每圈衰竭(秒)", self.v["decay"], "每圈比上圈慢多少秒")
        self._row(pf, 3, "定位偏移 X(米)", self.v["ox"], "向东为正")
        self._row(pf, 4, "定位偏移 Y(米)", self.v["oy"], "向北为正")
        self._row(pf, 5, "轨迹抖动", self.v["jitter"])
        ttk.Label(pf, text="平台").grid(row=6, column=0, sticky="w", padx=6)
        ttk.Label(pf, text="Android", foreground="#888").grid(row=6, column=1, sticky="w", padx=6)

        bf = ttk.Frame(self.root)
        bf.pack(fill="x", padx=10, pady=4)
        ttk.Button(bf, text="🎲 随机填入", command=self.randomize).pack(side="left", padx=4)
        ttk.Button(bf, text="预览数据(不发送)", command=lambda: self.worker(False)).pack(side="left", padx=4)
        self.btn_start = ttk.Button(bf, text="🚀 一键打卡", command=lambda: self.worker(True))
        self.btn_start.pack(side="left", padx=4)
        ttk.Button(bf, text="取消", command=self.do_cancel).pack(side="left", padx=4)
        self.pb = ttk.Progressbar(self.root, mode="determinate")
        self.pb.pack(fill="x", padx=10, pady=2)
        self.log = scrolledtext.ScrolledText(self.root, height=22)
        self.log.pack(fill="both", expand=True, padx=10, pady=8)
        self.refresh_labels()

    def p(self, s):
        self.log.insert("end", s + "\n"); self.log.see("end"); self.root.update_idletasks()

    def refresh_labels(self):
        try:
            s = json.load(open(SESSION, encoding="utf-8"))
            self.lbl_user.config(text="学号：%s    姓名：%s（登录后自动获取）"
                                 % (s.get("username", "—"), s.get("name", "—")))
        except Exception:
            self.lbl_user.config(text="学号：—    姓名：—（未登录）")

    # ---------- actions ----------
    def randomize(self):
        # 配速 260~300；每圈衰竭 5~15；定位偏移 [-2,2]；圈数保持当前选择
        self.v["pace"].set(str(random.randint(260, 300)))
        self.v["decay"].set(str(random.randint(5, 15)))
        self.v["ox"].set(str(random.randint(-2, 2)))
        self.v["oy"].set(str(random.randint(-2, 2)))
        self.v["jitter"].set("%.1f" % random.uniform(0.5, 1.5))
        self.p("[随机] 圈数=8，衰竭 %s，偏移 (%s,%s)，配速 %s，抖动 %s"
               % (self.v["decay"].get(), self.v["ox"].get(), self.v["oy"].get(),
                  self.v["pace"].get(), self.v["jitter"].get()))

    def login(self):
        wl = os.path.join(HERE, "web_login.py")
        if not os.path.exists(wl):
            messagebox.showwarning("提示", "未找到 web_login.py"); return
        try:
            subprocess.Popen([sys.executable, wl], cwd=HERE)
            self.p("已拉起登录浏览器，请完成登录（完成后会自动更新会话）")
        except Exception as e:
            self.p("✗ 启动登录失败：%r" % e)

    def do_cancel(self):
        self.cancel = True; self.p("已请求取消…")

    def params(self):
        return dict(laps=int(self.v["laps"].get()), pace=float(self.v["pace"].get()), decay=float(self.v["decay"].get()),
                    offset=(float(self.v["ox"].get()), float(self.v["oy"].get())),
                    jitter=float(self.v["jitter"].get()))

    def worker(self, send):
        if self.busy:
            messagebox.showinfo("提示", "正在执行中…"); return
        self.busy = True; self.cancel = False
        threading.Thread(target=self._run, args=(send,), daemon=True).start()

    # ---------- 主流程 ----------
    def _ensure_login(self):
        """无会话则拉起登录并等待其写入 session.json。"""
        if os.path.exists(SESSION) and json.load(open(SESSION, encoding="utf-8")).get("secret"):
            return True
        self.p("未发现会话，自动拉起登录…")
        wl = os.path.join(HERE, "web_login.py")
        subprocess.Popen([sys.executable, wl], cwd=HERE)
        for _ in range(600):                       # 最多 10 分钟
            if self.cancel:
                return False
            time.sleep(1)
            if os.path.exists(SESSION):
                try:
                    if json.load(open(SESSION, encoding="utf-8")).get("secret"):
                        self.p("已获取会话"); return True
                except Exception:
                    pass
        return False

    def _run(self, send):
        try:
            p = self.params()
            km = Track(laps=p["laps"]).lap_m * p["laps"] / 1000.0
            dur = run_duration(p["laps"], p["pace"], p["decay"])
            self.p("参数：%d圈 ≈%.2fkm 配速%.0f 衰竭%.0f 偏移%s 抖动%.1f → 时长 %s"
                   % (p["laps"], km, p["pace"], p["decay"], p["offset"], p["jitter"],
                      "%d:%02d" % (dur // 60, dur % 60)))

            if not send:
                js = build_jsonsports("x", "预览", "PREVIEW", _dt.datetime.now(), laps=p["laps"],
                                      pace=p["pace"], decay=p["decay"], offset_m=p["offset"],
                                      rng=random.Random(), spread=p["jitter"], include_coordinate=True)
                c = js["coordinate"]
                self.p("预览：odometer=%s activeTime=%s 轨迹点数=%d 首(%s,%s) 末(%s,%s)"
                       % (js["odometer"], js["activeTime"], len(c),
                          c[0]["a"], c[0]["o"], c[-1]["a"], c[-1]["o"]))
                return

            if not self._ensure_login():
                self.p("✗ 未取得会话"); return
            sess = json.load(open(SESSION, encoding="utf-8"))
            cli = Client(sess)

            # 自动取学号 + 姓名
            xh = sess["username"]; xm = sess.get("name", "")
            info = cli.get("/api/l/v6/u/info?&xh=" + xh)
            if isinstance(info.get("data"), dict) and info["data"].get("xm"):
                xm = info["data"]["xm"]
                sess["name"] = xm
                json.dump(sess, open(SESSION, "w", encoding="utf-8"), ensure_ascii=False)
            self.p("账号：%s / %s" % (xh, xm))
            self.refresh_labels()

            self.p("① currentTimeMillis：" + json.dumps(cli.get("/api/l/v6/currentTimeMillis?"), ensure_ascii=False))
            r = cli.get("/api/l/v7/sportsId?")
            self.p("② sportsId：" + json.dumps(r, ensure_ascii=False))
            sid = (r or {}).get("data")
            if not sid:
                self.p("✗ 取 sportsId 失败（会话可能过期，请重新登录）"); return
            t0 = _dt.datetime.now()

            self.p("③ 倒计时 %s（服务器校验真实墙钟时长）…" % ("%d:%02d" % (dur // 60, dur % 60)))
            self.pb["maximum"] = dur
            for i in range(dur):
                if self.cancel:
                    self.p("已取消"); return
                time.sleep(1); self.pb["value"] = i + 1
                if i % 60 == 0 and i:
                    self.p("   %d 分钟…" % (i // 60))

            self.p("④ inSportTime：" + json.dumps(cli.get("/api/l/v6.1/sporttype/inSportTime?&sportTypeId=" + SPORT_TYPE_ID), ensure_ascii=False))
            js = build_jsonsports(xh, xm, sid, t0, laps=p["laps"], pace=p["pace"], decay=p["decay"],
                                  offset_m=p["offset"], imei=cli.imei, rng=random.Random(),
                                  spread=p["jitter"], include_coordinate=True)
            c = js.get("coordinate", [])
            self.p("   提交数据：字段 %d，轨迹点 %d（首 %s,%s / 末 %s,%s）"
                   % (len(js), len(c),
                      c[0]["a"] if c else "-", c[0]["o"] if c else "-",
                      c[-1]["a"] if c else "-", c[-1]["o"] if c else "-"))
            try:
                _fp = os.path.join(HERE, "last_savesports.json")
                open(_fp, "w", encoding="utf-8").write(json.dumps(js, ensure_ascii=False, indent=1))
                self.p("   已保存提交数据 → last_savesports.json（可打开核对 coordinate 轨迹）")
            except Exception as _e:
                self.p("   (保存失败 %r)" % _e)
            jsp = {k: v for k, v in js.items() if k != "coordinate"}
            vp = zapi.encrypt_jsonsports(json.dumps(jsp, ensure_ascii=False, separators=(",", ":")), cli.secret)
            vs = zapi.encrypt_jsonsports(json.dumps(js, ensure_ascii=False, separators=(",", ":")), cli.secret)
            self.p("⑤ prejudgment：" + json.dumps(cli.post_form("/api/l/v6.1/prejudgment?", [("jsonsports", vp)]), ensure_ascii=False))
            res = cli.post_form("/api/l/v7/savesports?", [("jsonsports", vs)])
            self.p("⑥ savesports：" + json.dumps(res, ensure_ascii=False))
            tip = (res.get("data") or {}).get("tip", "")
            self.p(("✅ " if tip == "提交成功，待审核" else "⚠ ") + str(tip))
        except Exception as e:
            import traceback
            self.p("✗ 异常：%r" % e)
            self.p(traceback.format_exc())
        finally:
            self.busy = False


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
