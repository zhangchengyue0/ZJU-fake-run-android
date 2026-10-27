# AGENTS.md —— 部署与运行指南（面向 AI Agent）

> 目标：让一个 Agent 在**无人工干预**下把本工具跑起来（完成一次运动打卡）。
> 每一步都给出「要做什么 / 为什么 / 技术手段」。

---

## 0. 适用范围与红线

- 仅用于协议研究/自测。**不要**提交真实凭据、学号、姓名、轨迹到仓库。
- 运行时会生成 `session.json` / `last_savesports.json`，二者含敏感信息，**已加入 `.gitignore`**，切勿提交。

---

## 1. 环境准备

| 步骤 | 命令 | 技术手段/说明 |
|---|---|---|
| 安装依赖 | `pip install requests pycryptodome unicorn capstone mitmproxy playwright` | `pycryptodome` 做 AES；`unicorn`+`capstone` 用于仿真 native 求 `timestamp`；`playwright` 用于登录抓凭据 |
| 安装浏览器 | `python -m playwright install` | 供 `web_login.py` 拉起 Chrome |
| 校验 native 库 | 确认 `libAMapSDK_Location_v6_6_0.so` 与脚本**同目录** | 该库名是伪装，实为签名库；`emulate_cgapi.py` 按脚本目录查找 |

> 结论：依赖缺一不可 —— 手段：逐项 `python -c "import …"` 自检。

---

## 2. 文件地图

| 文件 | 职责 |
|---|---|
| `gui.py` / `启动工具.bat` | 图形界面入口 |
| `run.py` | CLI 两段式打卡（`start`/`finish`/`auto`） |
| `web_login.py` | 登录 → `session.json` |
| `zju_api.py` | `SECRET→seed`、`sign`、`v1`、`client`、`jsonsports` |
| `native_sign.py` / `emulate_cgapi.py` | Unicorn 仿真求 `timestamp` |
| `track.py` | `jsonsports` 组装 + 真实操场轨迹 |
| `session.example.json` | 凭据模板 |

---

## 3. 部署流程（推荐用 GUI）

| 步 | 操作 | 技术手段 |
|---|---|---|
| 1 | 双击 `启动工具.bat`（或 `python gui.py`） | tkinter，无需额外 GUI 依赖 |
| 2 | 点「登录/刷新会话」 | `subprocess.Popen([sys.executable, web_login.py])` 拉起浏览器；抓 `appLogin` 页面源码里的 `jwtToken`，拆出 `cgAuthorization` 与 `SECRET` 写入 `session.json` |
| 3 | 选里程/圈数（8圈≈3.4km / 6圈≈2.5km），可点「🎲 随机填入」 | 随机区间：配速 260~300、每圈衰竭 5~15、偏移 ±2m |
| 4 | 点「🚀 一键打卡」 | 见 §4 的内部时序 |

> 结论：**必须等够运动时长**才能提交成功 —— 手段：`sportsId` 领取后倒计时≈运动时长再发 `savesports`。

---

## 4. 内部时序（脚本自动完成）

```
① GET currentTimeMillis
② GET sportsId                     ← 记录 t0（服务器据此校验墙钟）
③ 倒计时 ≈ 运动时长
④ GET inSportTime
⑤ POST prejudgment  (jsonsports 不含 coordinate)
⑥ POST savesports   (jsonsports 含 coordinate)
```

| 结论 | 技术手段 |
|---|---|
| 每请求需 `sign`/`timestamp`/`client`/`v1`/`cgAuthorization`/`app-key`/`imei` | 见 `zju_api.py` / `gui.Client._h()` |
| `sign` 用**纯 Python** 计算 | `sha256(seed+canonical+u64be(ms)+rev(seed))` 插 `199st` |
| `timestamp` 用**仿真**取（只依赖 ms） | `native_sign.emu_timestamp(secret, ms)` |
| 大 body 的 `sign` 不能用仿真 | 对照实验：仿真对超大输入会算错 → 分离处理 |

---

## 5. 命令行部署（备选）

```bash
# 1) 登录（浏览器完成）
python web_login.py

# 2) 两段式打卡
python run.py start            # 取 sportsId 并开始计时
#   ... 真实跑步 / 等待 ≈ 时长 ...
python run.py finish           # 提交

# 或测试：自动等待≈时长后提交
python run.py auto --km 3.2 --pace 330
```

| 结论 | 技术手段 |
|---|---|
| `run.py finish` 的姓名可留空 | 自动 `GET /api/l/v6/u/info?&xh=` 取 `data.xm` |

---

## 6. 校验与排错

| 症状 | 原因 | 技术手段/修复 |
|---|---|---|
| `401 签名验证失败` | `sign` 或 `timestamp` 不对 | 确认 `timestamp` 由仿真按同一 `ms` 求得，而非随机 |
| `数据异常` | 未等够时长；或轨迹 `d/da` 与实测距离不符 | 等够时长；`track.py` 已用相邻点实测距离填 `d/da` |
| `登录过期` | 会话失效（同账号在别处登录会**顶掉**旧会话） | 重新执行登录 |
| 找不到 native 库 | `.so` 不在脚本目录 | 与脚本放同一目录（`emulate_cgapi.py` 按脚本目录查找） |
| App 记录无路径 | `coordinate` 的 `d/da` 不一致 | 用 `last_savesports.json` 核对 `d ≈ 相邻点实测距离` |

---

## 7. 产物与清理

- 运行产物：`session.json`、`last_savesports.json`、`run_state.json`（均敏感，勿提交）。
- 提交前自检：

```bash
# 学号(10位数字) / 设备UUID / SECRET 信封 / JWT
grep -rnE "[0-9]{10}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}|AAAA[A-Za-z0-9+/]{10,}|eyJ[A-Za-z0-9_-]{10,}" .
```

> 结论：仓库不应含任何个人/凭据信息 —— 手段：上述正则全量扫描 + `.gitignore` 排除运行时文件。
