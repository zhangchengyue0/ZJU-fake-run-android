#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
native_sign.py —— 通过 Unicorn 仿真 native cgapiEnrypt，返回 (sign, timestamp)。

- sign       = sha256(...) + 插字 "199st"（69 字符）
- timestamp  = native 输出的 p1（header `timestamp`，服务器精确校验）

canonical 请传「最终要哈希的字符串」（不含 '?'，避免 native 再做 query 解析），例如：
  GET : "/api/l/v7/quick_access/config"
  POST: "/api/l/v6.1/prejudgmentjsonsports<表单解码值>"
"""

from __future__ import annotations
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(HERE, "emulate_cgapi.py")
INSERT_POS = [12, 28, 36, 38, 58]
INSERT_CHARS = "199st"


def _insert199st(h: str) -> str:
    out = list(h)
    for pos, ch in zip(INSERT_POS, INSERT_CHARS):
        out.insert(pos, ch)
    return "".join(out)


def native_sign(secret: str, canonical: str, ms: int, timeout: int = 180):
    """返回 (sign, timestamp)。失败抛异常。（用临时文件传参，避免命令行过长。）"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sf = os.path.join(td, "s.txt")
        inf = os.path.join(td, "i.txt")
        with open(sf, "w", encoding="utf-8") as fh:
            fh.write(secret)
        with open(inf, "w", encoding="utf-8") as fh:
            fh.write(canonical)
        p = subprocess.run(
            [sys.executable, EMU, "--secret-file", sf, "--input-file", inf, "--chrono-ms", str(ms)],
            capture_output=True, text=True, cwd=HERE, timeout=timeout,
        )
    out = p.stdout or ""
    md = re.search(r"PICOSHA_DIGEST\s+([0-9a-fA-F]{64})", out)
    mt = re.search(r"TSCAND\s+(\d+)", out)
    if not md:
        raise RuntimeError("仿真未产出 digest:\n" + out[-800:] + "\n" + (p.stderr or "")[-800:])
    sign = _insert199st(md.group(1))
    ts = int(mt.group(1)) if mt else 0
    return sign, ts


def emu_timestamp(secret: str, ms: int, timeout: int = 180) -> int:
    """timestamp 只依赖 ms（与 canonical 无关），用小 canonical 仿真即可。"""
    _sg, ts = native_sign(secret, "/a", ms, timeout=timeout)
    return ts


if __name__ == "__main__":
    import json
    s = json.load(open(os.path.join(HERE, "session.json"), encoding="utf-8"))
    sg, ts = native_sign(s["secret"], "/api/l/v7/quick_access/config", 1791891932067)
    print("sign =", sg)
    print("ts   =", ts, "(expect 7760858042207124622)")
    print("match", sg == "547518c9e6c213697924cde09fd496bc3d0b96s9486d4158fed636153dt5663eec092")
