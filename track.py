#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
track_data.py —— 运动数据(jsonsports) + 轨迹生成（真实操场中心）

几何沿用 run_v3.py（直道172 + 弯道半径36 + mapper 投影），
中心按**真实样本轨迹**校准为 lat 30.305335 / lon 120.083321。

可调参数：
  - laps       圈数
  - pace       初始配速（秒/千米）
  - decay      每圈配速衰竭（秒/千米·圈）——每圈比上一圈慢这么多
  - offset_m   定位偏移（米，整体平移，可正可负）
  - jitter     轨迹抖动强度
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import random

# ─── run_v3 几何 ───
BEND_R = 36.0
STRAIGHT = 172.0
MAPPER = [[0.00039, -0.00019],   # 经度投影行
          [0.00006, 0.00094]]    # 纬度投影行

# ─── 真实操场中心（由真实样本 498 轨迹点求质心）───
REAL_LO = 120.083321
REAL_LA = 30.305335

R_EARTH = 6371000.0


def _haversine(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


def fmt_pace(sec):
    sec = int(round(sec)); return "%02d'%02d''" % (sec // 60, sec % 60)


def fmt_hms(sec):
    sec = int(round(sec)); return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


class Track:
    """田径场闭环；产生的轨迹**质心**落在指定中心（默认真实操场中心）。"""

    def __init__(self, laps=8, center=(REAL_LO, REAL_LA), offset_m=(0.0, 0.0),
                 mapper=MAPPER, bend_r=BEND_R, straight=STRAIGHT,
                 rng=None, spread=1.0):
        self.laps = laps
        self.bend_r = bend_r
        self.straight = straight
        self.mapper = mapper
        self.rng = rng or random.Random()
        self.spread = spread

        # 定位偏移（米 → 度）
        dl = offset_m[0] / (111320.0 * math.cos(math.radians(center[1])))
        da = offset_m[1] / 110574.0
        self.center = (center[0] + dl, center[1] + da)

        self.route = self._build_route()
        # 真实单圈周长（干净）
        self.lap_m = self._perimeter()
        self.length_m = self.lap_m * laps

    # ---- 单位化闭环 (xn,yn) ----
    def _uv_loop(self, m=600):
        pts = []
        for k in range(m + 1):
            pts.append((1.0, k / m))
        for k in range(m + 1):
            t = math.pi * k / m
            pts.append((math.cos(t), 1.0 + (self.bend_r / self.straight) * math.sin(t)))
        for k in range(m + 1):
            pts.append((-1.0, 1.0 - k / m))
        for k in range(m + 1):
            t = math.pi * k / m
            pts.append((-math.cos(t), -(self.bend_r / self.straight) * math.sin(t)))
        return pts

    def _raw(self, xn, yn):
        m = self.mapper
        return (m[0][0] * xn + m[0][1] * yn, m[1][0] * xn + m[1][1] * yn)

    def _perimeter(self):
        pts = [self._raw(x, y) for x, y in self._uv_loop()]
        return sum(_haversine(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                   for i in range(len(pts) - 1))

    def _build_route(self, m=600):
        """构建 n 圈轨迹，整体平移使质心=center；逐圈+逐点抖动。"""
        rng, sp = self.rng, self.spread
        raw = []
        for _ in range(self.laps):
            jx = rng.uniform(-0.010, 0.010) * sp
            jy = rng.uniform(-0.010, 0.010) * sp
            amp = 0.004 * sp
            for x, y in self._uv_loop(m):
                raw.append((x + jx + rng.uniform(-amp, amp),
                            y + jy + rng.uniform(-amp, amp)))
        # 质心（用 raw 单位坐标的均值）
        cx = sum(a for a, _ in raw) / len(raw)
        cy = sum(b for _, b in raw) / len(raw)
        out = []
        for x, y in raw:
            px, py = self._raw(x - cx, y - cy)
            out.append((px + self.center[0], py + self.center[1]))
        return out

    def cumulative(self):
        d = [0.0]
        for i in range(len(self.route) - 1):
            a, b = self.route[i], self.route[i + 1]
            d.append(d[-1] + _haversine(a[0], a[1], b[0], b[1]))
        return d

    def point_at_fraction(self, frac, cum=None):
        cum = cum or self.cumulative()
        tgt = frac * cum[-1]
        for i, c in enumerate(cum):
            if c >= tgt:
                return self.route[i]
        return self.route[-1]


# ─── 运动过程模拟（支持每圈配速衰竭）───
def simulate(total_km, laps, pace0, decay, rng, spread=1.0, interval=None):
    """返回 dict：total_time / splits / minute / avg_kmh / max_kmh / profile / cum。"""
    lap_km = total_km / laps
    prof = []                       # 每秒速度 m/s
    for k in range(laps):
        pace_k = pace0 + decay * k
        n = int(round(lap_km * pace_k))
        base = 1000.0 / pace_k
        ph = rng.uniform(0, 2 * math.pi)
        for i in range(n):
            v = base * (1.0 + 0.05 * spread * math.sin(2 * math.pi * i / 240 + ph)
                        + rng.uniform(-0.05, 0.05) * spread)
            prof.append(max(v, 1.0))
    k_adj = (total_km * 1000.0) / sum(prof)
    prof = [v * k_adj for v in prof]

    cum = [0.0]
    for v in prof:
        cum.append(cum[-1] + v)
    total_time = len(prof)

    splits, nk, lt = [], 1, 0
    for t in range(1, len(cum)):
        while nk <= int(total_km) and cum[t] >= nk * 1000.0:
            splits.append({"t": fmt_pace(t - lt), "km": str(nk)}); lt, nk = t, nk + 1

    minute = []
    for mn in range(1, (total_time + 59) // 60 + 1):
        lo, hi = (mn - 1) * 60, min(mn * 60, len(cum) - 1)
        minute.append({"min": str(mn), "v": "%.6f" % ((cum[hi] - cum[lo]) / 1000.0 * 60.0)})
    return {
        "total_time": total_time, "splits": splits, "minute": minute,
        "avg_kmh": round((total_km * 1000.0 / total_time) * 3.6, 1),
        "max_kmh": round(max(v * 3.6 for v in prof), 2),
        "prof": prof, "cum": cum,
    }


def build_coordinate(tr, sim, begin_dt, odometer_km, n, rng):
    cum = tr.cumulative()
    cumd = sim["cum"]
    total_m = cumd[-1]
    start_ms = int(begin_dt.timestamp() * 1000)
    pts, prev = [], None
    da = 0.0
    for i in range(n):
        frac = i / max(1, n - 1)
        tsec = frac * sim["total_time"]
        ti = min(int(tsec), len(sim["prof"]) - 1)
        dcur = cumd[ti]
        p = tr.point_at_fraction(dcur / total_m, cum)
        # d/da 用相邻经纬点的**实测距离**（与真实样本一致：d≈haversine）
        if prev is not None:
            d = _haversine(prev[0], prev[1], p[0], p[1])
            da += d
        else:
            d = 0.0
        v = sim["prof"][ti] * 3.6 if i > 0 else 0.0
        if i == 0:
            pt = {"a": "%.6f" % p[1], "v": "1", "o": "%.6f" % p[0], "t": str(start_ms),
                  "s": "0.000000", "ac": "30.000000", "d": "0.000000", "da": "0.000000"}
        else:
            pt = {"a": "%.6f" % p[1], "v": "1", "o": "%.6f" % p[0],
                  "t": str(start_ms + int(round(tsec * 1000))),
                  "s": "%.6f" % max(0.0, v * rng.uniform(0.97, 1.03)),
                  "ac": "5.000000", "d": "%.6f" % d, "da": "%.6f" % da,
                  "st": "0", "sta": "0"}
        pts.append(pt)
        prev = p
    return pts


def build_jsonsports(xh, name, sport_id, begin_dt, laps=8, pace=330.0, decay=8.0,
                     offset_m=(0.0, 0.0), imei="", app_version="6.0.3",
                     device_desc="iPhone iPad16,3", plan_route_name="紫金港田径场",
                     route_id="18", rng=None, spread=1.0, include_coordinate=False):
    rng = rng or random.Random()
    tr = Track(laps=laps, offset_m=offset_m, rng=rng, spread=spread)
    odometer = tr.length_m / 1000.0 + rng.uniform(-0.02, 0.02) * spread
    sim = simulate(odometer, laps, pace, decay, rng, spread)
    cum = tr.cumulative()
    bp = tr.point_at_fraction(rng.uniform(0.0, 0.004), cum)
    ep = tr.point_at_fraction(rng.uniform(0.996, 1.0), cum)
    end_dt = begin_dt + _dt.timedelta(seconds=sim["total_time"])
    V = max(2, int(round(sim["total_time"] / 2.0)))
    out = {
        "xh": xh, "sportId": sport_id,
        "currentDate": begin_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "calorie": "%.1f" % (odometer * rng.uniform(64.0, 70.0)),
        "odometer": "%.2f" % odometer, "avgSpeed": "%.1f" % sim["avg_kmh"],
        "activeTime": fmt_hms(sim["total_time"]),
        "beganPoint": "%.6f|%.6f" % (bp[1], bp[0]),
        "endPoint": "%.6f|%.6f" % (ep[1], ep[0]),
        "isValid": "(null)", "planRouteName": plan_route_name,
        "modementMode": "2", "routePolylineBh": "",
        "endTime": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "beginTime": begin_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "routeId": route_id,
        "phoneVersion": "%s,%s|%s" % (device_desc, imei, app_version),
        "alreadyPassPoint": "", "name": name, "stepCount": "0", "isValidReason": "(null)",
        "pace": sim["splits"], "avgPace": fmt_pace(sim["total_time"] / odometer),
        "lastOdometerTime": "00:00:%02d" % rng.randint(3, 9),
        "minuteSpeed": sim["minute"], "maxSpeedPerHour": "%.2f" % sim["max_kmh"],
        "minSpeedPerHour": "0.00", "stepMinute": "0.00",
        "alreadyPassPointResult": "", "needPassPointCount": "0",
        "randomPointStr": [], "virtualGps": str(V),
    }
    if include_coordinate:
        out["coordinate"] = build_coordinate(tr, sim, begin_dt, odometer, V, rng)
        out["virtualGps"] = str(len(out["coordinate"]))
        # 让 odometer 与轨迹实际累计里程一致
        out["odometer"] = "%.2f" % (float(out["coordinate"][-1]["da"]) / 1000.0)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xh", required=True); ap.add_argument("--name", required=True)
    ap.add_argument("--laps", type=int, default=8); ap.add_argument("--pace", type=float, default=330)
    ap.add_argument("--decay", type=float, default=8.0)
    ap.add_argument("--offset-x", type=float, default=0.0); ap.add_argument("--offset-y", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=None); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    js = build_jsonsports(a.xh, a.name, "SPORTID",
                          _dt.datetime.now() - _dt.timedelta(seconds=1100),
                          laps=a.laps, pace=a.pace, decay=a.decay,
                          offset_m=(a.offset_x, a.offset_y), rng=random.Random(a.seed),
                          include_coordinate=True)
    c = js["coordinate"]
    print("odometer=%s activeTime=%s 点数=%d" % (js["odometer"], js["activeTime"], len(c)))
    print("首点 %s,%s  末点 %s,%s" % (c[0]["a"], c[0]["o"], c[-1]["a"], c[-1]["o"]))
    print("lat span %.1fm lon span %.1fm" % (
        (max(float(p["a"]) for p in c) - min(float(p["a"]) for p in c)) * 110574,
        (max(float(p["o"]) for p in c) - min(float(p["o"]) for p in c)) * 111320 * 0.864))
    if a.out:
        open(a.out, "w", encoding="utf-8").write(json.dumps(js, ensure_ascii=False, indent=1))
        print("已写", a.out)
