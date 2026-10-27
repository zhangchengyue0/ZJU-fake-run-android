"""
浙大体艺 / 创高体育 (net.crigh.mysport) API 请求加密与请求头生成
================================================================
经 REA(JADX) 反编译 + 真实抓包端到端验证确认。

已完全确定并验证的算法
----------------------
1) SECRET 解包 -> seed
     raw = base64decode(SECRET)
     seed = AES-CBC-decrypt(key=FIXED_KEY, iv=raw[4:20], ct=raw[20:]).decode()   # 32 hex
   FIXED_KEY = ad8dad2869c5e017303e5f48858db3129bf4d07a47d85beaaaa9d26f436d232a

2) sign 头  (native cgapiEnrypt, 输入=URL从'/api'|'/app'起的 path, 含 query)
     canonical = path + "".join(k+v for k,v in sorted(query_pairs))
     base      = sha256(seed + canonical + uint64_be(client_ms) + seed[::-1]).hexdigest()
     sign      = 在 base 的 [12,28,36,38,58] 依次插入 "199st"
   Java 对应: C0618.m4573() -> m4569()

3) v1 头 = md5(str(currentTimeMillis))                # 32 hex, C0717.m4758(空 salt)
4) client 头 = base64( AES-128-ECB-PKCS7( json({"heap","root","t"}), key=SECRET[:16] ) )
     {"heap":"0","root":"0","t":"<epoch_ms>"}         # C0716.m4754
5) jsonsports = urlencode( base64( AES-128-ECB-PKCS7( json, key=SECRET[:16] ) ) )   # 同 key

其它头: cgAuthorization = HEADER 里的 TOKEN(JWT); imei = UUID(android_id.hashCode...);
        app-key = "azk3t4jrcfm5772t"(tyys) / "cgsoft"(chingo)。
未定: timestamp 头(来自 cgapiEnrypt 返回的 part[1]，疑似 native 随机 64bit)。
"""
import base64
import hashlib
import struct
import json as _json
from urllib.parse import parse_qsl, quote, unquote

from Crypto.Cipher import AES

FIXED_KEY = bytes.fromhex("ad8dad2869c5e017303e5f48858db3129bf4d07a47d85beaaaa9d26f436d232a")
INSERT_POS = [12, 28, 36, 38, 58]
INSERT_CHARS = "199st"


# ---------- key ----------
def derive_key(secret: str) -> bytes:
    """AES-128 密钥 = SECRET 前 16 个字符 (ASCII)。"""
    k = secret[:16].encode("ascii")
    if len(k) != 16:
        raise ValueError("SECRET 长度不足")
    return k


def decode_secret(secret_b64: str) -> str:
    raw = base64.b64decode(secret_b64)
    return AES.new(FIXED_KEY, AES.MODE_CBC, raw[4:20]).decrypt(raw[20:]).decode("ascii")


# ---------- sign ----------
def canonical_path(path_and_query: str) -> str:
    """把 '/api/l/v6/x?a=1&b=2' 规范化为 '/api/l/v6/x' + 'a1b2'。"""
    if "?" in path_and_query:
        base, q = path_and_query.split("?", 1)
    else:
        base, q = path_and_query, ""
    pairs = parse_qsl(q, keep_blank_values=True)
    return base + "".join(k + v for k, v in sorted(pairs))


def compute_sign(secret_b64: str, path_and_query: str, client_ms: int) -> str:
    seed = decode_secret(secret_b64)
    canonical = canonical_path(path_and_query)
    data = (seed.encode("ascii") + canonical.encode("utf-8")
            + struct.pack(">Q", client_ms & 0xFFFFFFFFFFFFFFFF) + seed[::-1].encode("ascii"))
    base = hashlib.sha256(data).hexdigest()
    out = list(base)
    for pos, ch in zip(INSERT_POS, INSERT_CHARS):
        out.insert(pos, ch)
    return "".join(out)


def path_for_signing(full_url: str) -> str:
    """取 URL 中从 '/app' 或 '/api' 开始的 path(含 query)。"""
    i = full_url.find("/app")
    if i < 0:
        i = full_url.find("/api")
    if i < 0:
        raise ValueError("URL 中找不到 /app 或 /api")
    return full_url[i:]


# ---------- 表单型 POST 的 sign（prejudgment / savesports 等，body=jsonsports）----------
def _form_value(raw_encoded: str) -> str:
    """表单字段值：先 URL 解码(%2B->'+')，再把 '+' 当空格（与 native 的 query 解析一致）。"""
    return unquote(raw_encoded).replace("+", " ")


def canonical_post(full_url: str, form_pairs) -> str:
    """POST 的 canonical = path(从/api起,去query) + 排序后的 'key+value'（value 为表单解码值）。"""
    from urllib.parse import urlsplit
    p = path_for_signing(full_url)
    path = p.split("?", 1)[0]
    return path + "".join(k + _form_value(v) for k, v in sorted(form_pairs))


def raw_sign(secret_b64: str, canonical: str, client_ms: int) -> str:
    """对「已规范化好的 canonical 字符串」直接做 native 签名（sha256 + 插字）。"""
    seed = decode_secret(secret_b64)
    data = (seed.encode("ascii") + canonical.encode("utf-8")
            + struct.pack(">Q", client_ms & 0xFFFFFFFFFFFFFFFF) + seed[::-1].encode("ascii"))
    base = hashlib.sha256(data).hexdigest()
    out = list(base)
    for pos, ch in zip(INSERT_POS, INSERT_CHARS):
        out.insert(pos, ch)
    return "".join(out)


def compute_sign_post(secret_b64: str, full_url: str, form_pairs, client_ms: int) -> str:
    """表单 POST 的 sign。算法与 GET 相同，只是 canonical 由表单参数拼成。

    注意：签名用的 ms ≈ client 头里的 t（可能差 0~几毫秒；实测 prejudgment=t-1、savesports=t-3）。
    form_pairs: [(k, 原始urlencode值), ...]，如 [("jsonsports", venc)]。
    """
    seed = decode_secret(secret_b64)
    canonical = canonical_post(full_url, form_pairs)
    data = (seed.encode("ascii") + canonical.encode("utf-8")
            + struct.pack(">Q", client_ms & 0xFFFFFFFFFFFFFFFF) + seed[::-1].encode("ascii"))
    base = hashlib.sha256(data).hexdigest()
    out = list(base)
    for pos, ch in zip(INSERT_POS, INSERT_CHARS):
        out.insert(pos, ch)
    return "".join(out)


# ---------- 其它平台分支（创高 chingo，sign=md5 32位）----------
SALT_CHINGO = "6d3121b650e42855976d0f70dd2048e4"      # 卋箐茺()（chingo/xjtu）
SALT_OTHER  = "262b6c001ea05beceb9d560be1dbf14f"      # 剀霄皻…()


def compute_sign_chingo(path_from_api: str, params, client_ms: int, salt: str = SALT_CHINGO) -> str:
    """创高(chingo)/西交(xjtu) 域名的 sign = md5(盐+path+Σ排序k+v+str(ms)+' '+盐)。已验证 flow#67。"""
    kv = "".join(k + v for k, v in sorted(params))
    canonical = salt + path_from_api + kv + str(client_ms) + " " + salt
    return hashlib.md5(canonical.encode()).hexdigest()



# ---------- v1 ----------
def compute_v1(ms: int) -> str:
    return hashlib.md5(str(ms).encode()).hexdigest()


# ---------- client ----------
def _pkcs7_pad(b: bytes, block: int = 16) -> bytes:
    n = block - (len(b) % block)
    return b + bytes([n]) * n


def build_client(secret: str, t_ms: int, root: str = "0", heap: str = "0") -> str:
    # Android 平台明文: {"heap":"0","root":"0","t":"<ms>"}
    d = {"heap": heap, "root": root, "t": str(t_ms)}
    payload = _json.dumps(d, separators=(",", ":")).encode()
    ct = AES.new(derive_key(secret), AES.MODE_ECB).encrypt(_pkcs7_pad(payload))
    return base64.b64encode(ct).decode()


# ---------- jsonsports ----------
def encrypt_jsonsports(plaintext: str, secret: str) -> str:
    ct = AES.new(derive_key(secret), AES.MODE_ECB).encrypt(_pkcs7_pad(plaintext.encode("utf-8")))
    return quote(base64.b64encode(ct).decode(), safe="")


def decrypt_jsonsports(value: str, secret: str) -> str:
    from urllib.parse import unquote
    raw = unquote(value)
    ct = base64.b64decode(raw + "=" * (-len(raw) % 4))
    pt = AES.new(derive_key(secret), AES.MODE_ECB).decrypt(ct)
    n = pt[-1]
    if 1 <= n <= 16 and pt[-n:] == bytes([n]) * n:
        pt = pt[:-n]
    return pt.decode("utf-8", "replace")


if __name__ == "__main__":
    # 端到端自测: 用 bs_sign4.flow 真实流量 (SECRET 已知)
    from mitmproxy.io import FlowReader
    SECRET = ""  # 自测用；可填入你自己的 SECRET（勿提交真实凭据）
    def strip_sign(s):
        for p, c in reversed(list(zip(INSERT_POS, INSERT_CHARS))):
            if p < len(s) and s[p] == c:
                s = s[:p] + s[p + 1:]
        return s
    def recover_ms(path, sign, center):
        tgt = strip_sign(sign)
        for d in range(0, 3600000):
            for ms in (center + d, center - d):
                if hashlib.sha256((decode_secret(SECRET).encode() + canonical_path(path).encode()
                                   + struct.pack(">Q", ms) + decode_secret(SECRET)[::-1].encode())).hexdigest() == tgt:
                    return ms
        return None
    ok = 0
    import os as _os
    _flow = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "bs_sign4.flow")
    if not _os.path.exists(_flow):
        print("(跳过自测：未找到 bs_sign4.flow)"); raise SystemExit(0)
    with open(_flow, "rb") as fh:
        for fl in FlowReader(fh).stream():
            r = fl.request
            h = {k.lower(): v for k, v in r.headers.items()}
            if "sign" in h and "cgapp-server/api" in r.pretty_url:
                path = path_for_signing(r.pretty_url)
                ms = recover_ms(path, h["sign"], int(fl.request.timestamp_start * 1000))
                if ms and compute_sign(SECRET, path, ms) == h["sign"]:
                    ok += 1
                # client 解密校验
                cl = base64.b64decode(h["client"] + "=" * (-len(h["client"]) % 4))
                pt = AES.new(derive_key(SECRET), AES.MODE_ECB).decrypt(cl)
                assert b'"heap"' in pt and b'"t"' in pt
    print("sign 验证通过:", ok, "条 | client 解密正常")
    print("v1 示例 md5(str(1781198221308)) =", compute_v1(1781198221308))
