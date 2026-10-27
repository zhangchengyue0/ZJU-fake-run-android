# 浙大体艺（创高体育）安卓端通信协议逆向及复现

本项目由 GPT5.4 与 DeepSeek V4 Pro/V4.1 Flash协助完成
> 对一个加壳 Android 应用（`net.crigh.mysport`）通信协议的完整逆向与复现：
> 从壳内字节码虚拟机、原生签名库，到请求头签名、数据加密，再到一次可用的运动打卡。
> 安卓端协议模拟实现，不需求手机/虚拟定位完成
>
> ⚠️ **仅用于安全研究 / 协议学习 / 自测**。请遵守学校规定与法律法规，不要滥用。

---

## 目录

- [浙大体艺（创高体育）安卓端通信协议逆向及复现](#浙大体艺创高体育安卓端通信协议逆向及复现)
  - [目录](#目录)
  - [成果速览](#成果速览)
  - [快速开始](#快速开始)
  - [通信流程](#通信流程)
  - [核心技术结论（结论 ← 技术手段）](#核心技术结论结论--技术手段)
  - [请求头与精确公式](#请求头与精确公式)
    - [`sign`](#sign)
    - [`SECRET` → `seed`](#secret--seed)
  - [其它分支与加固细节](#其它分支与加固细节)
    - [创高(chingo)/西交(xjtu) 的 32 位 md5 分支](#创高chingo西交xjtu-的-32-位-md5-分支)
    - [壳与字节码 VM](#壳与字节码-vm)
    - [「高德」库的伪装（重要）](#高德库的伪装重要)
  - [运动数据 `jsonsports` 与轨迹 `coordinate`](#运动数据-jsonsports-与轨迹-coordinate)
  - [服务端风控](#服务端风控)
  - [工具说明](#工具说明)
  - [常见问题](#常见问题)
  - [合规声明](#合规声明)

---

## 成果速览

| 能力 | 状态 |
|---|---|
| `sign` 请求头签名（GET / 表单 POST） | ✅ 真机验证 |
| `timestamp` 请求头（服务器精确校验） | ✅ 仿真求得，真机验证 |
| `SECRET` → `seed` 解包 | ✅ |
| `jsonsports` 运动数据加解密 | ✅ 逐字节复现 |
| `coordinate` GPS 轨迹（绘制路径必需） | ✅ 对齐真实样本 |
| 完整打卡（预判 + 保存，返回「提交成功，待审核」） | ✅ 端到端验证 |
| 图形界面一键运行 | ✅ |

---

## 快速开始

```bash
pip install requests pycryptodome unicorn capstone mitmproxy playwright
python -m playwright install

# Windows 双击：启动工具.bat    或    python gui.py
```

1. 点「登录 / 刷新会话」→ 在浏览器完成 CAS 登录（自动抓取凭据到 `session.json`）；
2. 选择里程/圈数（3km·8圈 / 2km·6圈），可点「🎲 随机填入」；
3. 点「🚀 一键打卡」：**取 `sportId` → 倒计时（真实等待运动时长）→ 自动生成数据并发送**。

> 每次提交会把完整数据（含 `coordinate` 轨迹）保存为 `last_savesports.json` 以便核对。

---

## 通信流程

```
登录(CAS) ──► 初始化(若干 GET) ──► 取 sportId ──►【真实等待≈运动时长】──► 预判 ──► 保存 ──► (传感器) ──► 刷新
                每请求带 sign / timestamp / client / v1 / cgAuthorization / app-key / imei
```

| 步 | 请求 | 说明 |
|---|---|---|
| 1 | `GET  /cgapp-server/api/l/v6/currentTimeMillis?` | 对时（可选） |
| 2 | `GET  /cgapp-server/api/l/v7/sportsId?` | **跑步开始前**领取；返回 UUID，记时刻 t0 |
| 3 | — | **真实等待 ≈ 运动时长**（服务器校验墙钟，见下） |
| 4 | `GET  /cgapp-server/api/l/v6.1/sporttype/inSportTime?&sportTypeId=18` | `{"data":"1"}` 表示可跑 |
| 5 | `POST /cgapp-server/api/l/v6.1/prejudgment?` | body=`jsonsports`（**不含** coordinate） |
| 6 | `POST /cgapp-server/api/l/v7/savesports?` | body=`jsonsports`（**含** coordinate） |
| 7 | `POST /cgapp-server/api/l/v7/upload/sensorZipFile?` | multipart；非必需 |

---

## 核心技术结论（结论 ← 技术手段）

> 每条结论后都标注了**得出/解决它所用的技术手段**。

| # | 结论 | 技术手段 |
|---|---|---|
| 1 | `.dex` 被整体 XOR `0x30` 塞进 `.vd`，关键方法被搬进**自定义字节码 VM** | 静态逆向：定位容器解析函数，逐字节试 XOR 还原 DEX 头 |
| 2 | VM 记录表 `R0 = end-12-count*0x12`、stride `0x12`；代码块 XOR `0xf8`、opcode ⊕`0x18` 后为标准 Dalvik | 自研 VM 反汇编器（`vmdis.py`）：按结构解析记录、试错 XOR 密钥，校验反汇编「0 越界/0 未定义指令」 |
| 3 | **真正的签名不在 VM**，而在 native `cgapiEnrypt`（`libAMapSDK_Location_v6_6_0.so`）| 交叉验证：VM 里那段是创高的 md5 分支（在 `tyapp.chingo.cn` 真流量验证）；浙大 69 位签名改由 Ghidra 定位 native 符号 |
| 4 | 那个「高德定位 SDK」实为**伪装的签名库**（内无任何 `autonavi/amap` 串，却导出 App 自己的 `ChingoEncrypt` + `picosha2`）| `strings` 统计对比同名的高德地图库（真库 `autonavi×165`） |
| 5 | `sign = 插字199st( sha256(seed+canonical+u64be(ms)+rev(seed)) )` | Ghidra 反汇编 + Unicorn 仿真（hook `picosha2` 抓真实哈希输入）+ 真流量逐字节比对 |
| 6 | 表单 POST 的 canonical = `path + "jsonsports" + 表单解码值('+'→空格)` | Unicorn 仿真还原输入串 + 多份真机抓包命中验证 |
| 7 | `SECRET` = AES-CBC 信封，`seed = AES-CBC-dec(FIXED_KEY, iv=raw[4:20], ct=raw[20:])`，`FIXED_KEY` 全局固定 | JADX 反编译定位解包逻辑 + 用抓到的 SECRET/seed 对反推 key |
| 8 | `jsonsports = base64(AES-128-ECB(SECRET[:16], JSON))`，**确定性** | 解密抓包 + **重加密逐字节复现**密文（证伪了历史上的 CBC 说法） |
| 9 | `timestamp` 头 = native 的 `p1`，**只依赖 `ms`**，服务器**精确**校验（差 1 即失败） | 控制变量二分实验（逐个改头看服务器 `401`）+ Unicorn 读寄存器 `r3:r2` + 大小输入对照证明「只依赖 ms」 |
| 10 | **大输入（万级轨迹点）不能用仿真算 `sign`** | 对照实验：同输入下仿真签名 ≠ 纯 Python 签名 → 改为「`sign` 纯 Python、`timestamp` 仿真」 |
| 11 | `savesports` 比 `prejudgment` 多一个 `coordinate` 轨迹数组 | JADX 定位 `UploadJsonSports.build()` + 两份抓包解密对比 |
| 12 | 轨迹每点 `d`/`da` 必须≈相邻点实测距离（否则 App 不画路径）| 与真实样本做「haversine / d」比值统计（真实≈1.0，修复前 0.23~1.37）|
| 13 | `数据异常(24)` 的真因：**取 `sportId` 到提交必须真的过了运动时长** | 控制实验：取 Id 后「立即提交」失败、「等待≈时长」后成功（`提交成功，待审核`） |
| 14 | 学号/姓名无需手填，可自动获取 | 学号取自会话；姓名取自 `GET /api/l/v6/u/info?&xh=` 的 `data.xm` |

---

## 请求头与精确公式

| 头 | 公式 |
|---|---|
| `app-key` | `azk3t4jrcfm5772t`（tyys/zju）｜`cgsoft`（chingo/xjtu） |
| `cgAuthorization` | 登录所得 JWT |
| `v1` | `md5(str(ms))` |
| `client` | `base64(AES-128-ECB(SECRET[:16], {"heap":"0","root":"0","t":"<ms>"}))`（Android） |
| `sign` | 见下 |
| `timestamp` | native `cgapiEnrypt(...).split("|")[1]`（只依赖 `ms`；用仿真取） |

> `SECRET[:16]` = **SECRET 字符串前 16 个字符的 ASCII**，不是解码字节。

### `sign`

```python
data = seed_ascii + canonical_utf8 + uint64_be(ms) + reverse(seed)_ascii
sign = 在 sha256(data).hexdigest() 的第 [12,28,36,38,58] 位依次插入 "199st"   # 69 字符
```

**canonical 两种情形**
```
GET / query-POST :  path(从 "/api" 起) + Σ 排序后的 (key + value)
表单 POST(body)  :  path(从 "/api" 起, 去 query) + "jsonsports" + unquote(venc).replace("+", " ")
```

### `SECRET` → `seed`

```python
raw  = base64.b64decode(SECRET)
seed = AES_CBC_dec(FIXED_KEY, iv=raw[4:20], ct=raw[20:]).decode("ascii")  # 32 hex
FIXED_KEY = ad8dad2869c5e017303e5f48858db3129bf4d07a47d85beaaaa9d26f436d232a
```

---

## 其它分支与加固细节

### 创高(chingo)/西交(xjtu) 的 32 位 md5 分支

| 结论 | 技术手段 |
|---|---|
| chingo/xjtu 的 `sign` 是 **32 位 md5**（zju 才是 69 位 native），公式 `md5(SALT + path + Σ(k+v) + str(ms) + " " + SALT)` | VM 反汇编出 `C0617.m4562` 的 canonical 组装 + `蕱铜亓拻蔐`(=MD5)；在 `tyapp.chingo.cn` 真流量命中 |
| `SALT(chingo) = 6d3121b650e42855976d0f70dd2048e4` = `"6d3121b650e"` + `LCM(15,2857)=42855` + `"976d0f70"` + `R.string.as4="dd2048e4"` | JADX 定位盐函数，LCM 由 `C0720.m4763` 计算，资源串来自 R.string |
| `SALT(其它) = 262b6c001ea05beceb9d560be1dbf14f` = `R.string.nas1` + `"beceb9d"` + `LCM(5,112)=560` + `"be1dbf14f"` | 同上 |
| `app-key(tyys) = azk3t4jrcfm5772t` = `"azk3t4"+"jrcfm"+LCM(Jh,12)=5772+"t"`；`app-key(chingo)="cgsoft"` | JADX 定位构建函数 |

### 壳与字节码 VM

| 结论 | 技术手段 |
|---|---|
| `.dex` 整体 XOR `0x30` 存入 `.vd`；每片段尾部 `0x60620170`+A+count | 定位容器解析函数 `FUN_0004fbc8`，比对其读取结构 |
| 记录表 `R0 = end-12-count*0x12`，**stride 0x12**；`methodIdx=u16(rec+0x08)`；`code_abs = R0 + op*0x12 + i32(rec+0x04)` | 自研 `vmdis.py`：按结构解析并试错 |
| 代码块字节 XOR `0xf8`，opcode 低字节 ⊕`0x18` 后为标准 Dalvik | Unicorn/静态分析解释器 `FUN_00021828`、跳表 `@0x12a08`（238×u32） |
| 分派 `group=id>>24, record=id&0xffffff` | 反汇编分派器 `FUN_000503d0` |

### 「高德」库的伪装（重要）

| 结论 | 技术手段 |
|---|---|
| `libAMapSDK_Location_v6_6_0.so` 其实是 **App 的签名库**，不是高德定位 SDK | `strings` 统计：`autonavi 0 / amap 0 / AMap 1`（仅 soname）却含 `ChingoEncrypt` ×1、`picosha2` ×13 |
| 同名的真高德地图库 `libAMapSDK_MAP_v7_4_0.so` | `autonavi 165 / amap 59 / AMap 64` → 对照可知前者是伪装 |

---

## 运动数据 `jsonsports` 与轨迹 `coordinate`

```
加密: jsonsports = urlencode( base64( AES-128-ECB(SECRET[:16], JSON_utf8) ) )
```

- `prejudgment` 的 JSON **不含** `coordinate`；`savesports` 的 JSON **含** `coordinate`。
- `coordinate` 每点：`{a:纬度, o:经度, t:毫秒, s:速度km/h, ac:精度, d:距上点m, da:累计m, v:"1", st:"0", sta:"0"}`
  - **首点** `s=0, ac=30, d=0, da=0` 且**无** `st/sta`；其余 `ac=5`。
  - **`d` 必须等于相邻两经纬点的实际距离**（App 据此绘制路径）。
  - `virtualGps` = 轨迹点数。

---

## 服务端风控

| 校验 | 要求 | 应对 |
|---|---|---|
| 里程 | `odometer ≥ 3.0` km | 数据自洽 |
| 时长 | 约 2–45 分钟 | 数据自洽 |
| 时段/配速 | 符合规定 | 数据自洽 |
| **墙钟时长** | `t(savesports) − t(sportsId) ≥ 运动时长` | **两段式：取 Id → 等待 → 提交** |
| 轨迹一致性 | `coordinate` 非空、`d≈实测距离`、`virtualGps`=点数 | 见上 |

---

## 工具说明

| 文件 | 作用 |
|---|---|
| `gui.py` / `启动工具.bat` | 图形界面：参数、随机填入、一键打卡/预览 |
| `run.py` | 命令行两段式（`start` / `finish` / `auto`） |
| `zju_api.py` | `SECRET` 解包、`sign`、`v1`、`client`、`jsonsports` |
| `native_sign.py` | Unicorn 仿真取 `timestamp` |
| `emulate_cgapi.py` + `libAMapSDK_Location_v6_6_0.so` | native 仿真 |
| `track.py` | 运动数据组装 + 真实操场轨迹生成 |
| `web_login.py` | 浏览器登录抓凭据 → `session.json` |

---

## 常见问题

- **`401 签名验证失败`** → `sign` 或 `timestamp` 不对。`timestamp` 不能用随机数，必须由仿真按 `ms` 求得。
- **`数据异常`** → 多半是**没等够运动时长**就提交；或 `coordinate` 的 `d/da` 与实测距离不符。
- **`登录过期`** → 会话失效（常见于**同一账号在别处重新登录，把旧会话顶掉**），重新登录即可。
- **运行报错找不到 native 库** → 确认 `libAMapSDK_Location_v6_6_0.so` 与脚本在同一目录。

---

## 合规声明

本项目仅面向**协议学习与安全研究**，用于理解「请求签名 / 数据加密 / 客户端加固」的常见实现与应对。
请勿用于伪造运动记录、规避学校考核或任何违规用途。使用者需自行承担一切后果。
