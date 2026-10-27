import argparse
import base64
import hashlib
import struct
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from Crypto.Cipher import AES
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from unicorn import (
    Uc,
    UcError,
    UC_ARCH_ARM,
    UC_HOOK_CODE,
    UC_HOOK_MEM_WRITE,
    UC_MODE_THUMB,
    UC_PROT_ALL,
)
from unicorn.arm_const import *


_HERE = Path(__file__).resolve().parent
_SO_CANDIDATES = [
    _HERE / "libAMapSDK_Location_v6_6_0.so",                 # 打包目录（.so 与脚本同级）
    _HERE / "native_extract" / "libAMapSDK_Location_v6_6_0.so",  # 项目目录
    Path("native_extract/libAMapSDK_Location_v6_6_0.so"),     # 相对 CWD 兜底
]
SO_PATH = next((p for p in _SO_CANDIDATES if p.exists()), _SO_CANDIDATES[0])
BASE = 0x10000000
STACK = 0x20000000
STACK_SIZE = 0x40000
HEAP = 0x30000000
HEAP_SIZE = 0x2000000
JNI = 0x40000000
STUB = 0x50000000
STOP = 0x60000001

FUNC = 0x2FDCC
INSERT_POS = [12, 28, 36, 38, 58]
AES_KEY = bytes.fromhex(
    "ad8dad2869c5e017303e5f48858db3129bf4d07a47d85beaaaa9d26f436d232a"
)


def u32(x):
    return x & 0xFFFFFFFF


def p32(x):
    return struct.pack("<I", u32(x))


def read_u32(mu, addr):
    return struct.unpack("<I", mu.mem_read(addr, 4))[0]


def write_u32(mu, addr, val):
    mu.mem_write(addr, p32(val))


def read_cstr(mu, addr, limit=1 << 20):
    out = bytearray()
    for i in range(limit):
        b = mu.mem_read(addr + i, 1)[0]
        if b == 0:
            break
        out.append(b)
    return bytes(out)


def align_down(x, page=0x1000):
    return x & ~(page - 1)


def align_up(x, page=0x1000):
    return (x + page - 1) & ~(page - 1)


class Emulator:
    def __init__(self, so_path, secret, input_text, forced_timestamp=None, chrono_ms=None, trace=False):
        self.so_path = Path(so_path)
        self.secret = secret
        self.input_text = input_text
        self.forced_timestamp = forced_timestamp
        self.chrono_ms = chrono_ms
        self.trace = trace
        self.mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.heap_next = HEAP
        self.heap_end = HEAP + HEAP_SIZE
        self.aes_ctx = {}
        self.plt_names = {}
        self.stub_names = {}
        self.hash_inputs = []
        self.hash_buf = None

    def alloc(self, size, zero=True):
        size = align_up(max(size, 1), 8)
        addr = self.heap_next
        self.heap_next += size
        if self.heap_next >= self.heap_end:
            raise MemoryError("emulated heap exhausted")
        if zero:
            self.mu.mem_write(addr, b"\x00" * size)
        return addr

    def write_bytes(self, data, nul=False):
        addr = self.alloc(len(data) + (1 if nul else 0))
        self.mu.mem_write(addr, data + (b"\x00" if nul else b""))
        return addr

    def map_so(self):
        data = self.so_path.read_bytes()
        with self.so_path.open("rb") as f:
            elf = ELFFile(f)
            # Map all PT_LOAD segments at a non-zero base.
            for seg in elf.iter_segments():
                if seg["p_type"] != "PT_LOAD":
                    continue
                vaddr = BASE + seg["p_vaddr"]
                memsz = seg["p_memsz"]
                filesz = seg["p_filesz"]
                off = seg["p_offset"]
                start = align_down(vaddr)
                end = align_up(vaddr + memsz)
                self.mu.mem_map(start, end - start, UC_PROT_ALL)
                blob = data[off : off + filesz]
                self.mu.mem_write(vaddr, blob)
                if memsz > filesz:
                    self.mu.mem_write(vaddr + filesz, b"\x00" * (memsz - filesz))

            # Apply enough relocations for PC-relative GOT reads to work.
            dynsym = elf.get_section_by_name(".dynsym")
            for sec in elf.iter_sections():
                if not isinstance(sec, RelocationSection):
                    continue
                symtab = elf.get_section(sec["sh_link"]) if sec["sh_link"] else dynsym
                for rel in sec.iter_relocations():
                    r_type = rel["r_info_type"]
                    off = BASE + rel["r_offset"]
                    if r_type == 23:  # R_ARM_RELATIVE
                        addend = read_u32(self.mu, off)
                        write_u32(self.mu, off, BASE + addend)
                    elif r_type in (21, 2, 22):  # GLOB_DAT / ABS32 / JUMP_SLOT
                        sym_name = ""
                        if symtab and rel["r_info_sym"]:
                            sym_name = symtab.get_symbol(rel["r_info_sym"]).name
                        if sym_name:
                            write_u32(self.mu, off, self.make_stub(sym_name) | 1)

            plt = elf.get_section_by_name(".plt")
            relplt = elf.get_section_by_name(".rel.plt")
            if plt and relplt and dynsym:
                base = plt["sh_addr"] + 0x14
                for i, rel in enumerate(relplt.iter_relocations()):
                    sym = dynsym.get_symbol(rel["r_info_sym"])
                    self.plt_names[BASE + base + i * 12] = sym.name

    def make_stub(self, name):
        if name in self.stub_names:
            return self.stub_names[name]
        addr = STUB + len(self.stub_names) * 0x100
        self.stub_names[name] = addr
        return addr

    def setup_memory(self):
        self.map_so()
        self.mu.mem_map(STACK, STACK_SIZE, UC_PROT_ALL)
        self.mu.mem_map(HEAP, HEAP_SIZE, UC_PROT_ALL)
        self.mu.mem_map(JNI, 0x10000, UC_PROT_ALL)
        self.mu.mem_map(STUB, 0x100000, UC_PROT_ALL)
        self.mu.mem_map(STOP & ~0xFFF, 0x1000, UC_PROT_ALL)

        # Minimal JNI function table with GetStringUTFChars at offset 0x2a4.
        get_utf = self.make_stub("JNI_GetStringUTFChars")
        write_u32(self.mu, JNI, JNI + 0x100)
        write_u32(self.mu, JNI + 0x100 + 0x2A4, get_utf | 1)

        self.secret_obj = self.write_bytes(self.secret.encode(), True)
        self.input_obj = self.write_bytes(self.input_text.encode(), True)

        sp = STACK + STACK_SIZE - 0x1000
        self.mu.reg_write(UC_ARM_REG_SP, sp)
        self.mu.reg_write(UC_ARM_REG_LR, STOP)
        self.mu.reg_write(UC_ARM_REG_R0, JNI)
        self.mu.reg_write(UC_ARM_REG_R1, 0)
        self.mu.reg_write(UC_ARM_REG_R2, self.secret_obj)
        self.mu.reg_write(UC_ARM_REG_R3, self.input_obj)

    def get_string(self, obj):
        first = self.mu.mem_read(obj, 1)[0]
        if first & 1:
            size = read_u32(self.mu, obj + 4)
            ptr = read_u32(self.mu, obj + 8)
            return bytes(self.mu.mem_read(ptr, size))
        size = first >> 1
        return bytes(self.mu.mem_read(obj + 1, size))

    def set_string(self, obj, data):
        if isinstance(data, str):
            data = data.encode()
        self.mu.mem_write(obj, b"\x00" * 12)
        if len(data) <= 10:
            self.mu.mem_write(obj, bytes([len(data) << 1]) + data + b"\x00")
        else:
            ptr = self.write_bytes(data, True)
            write_u32(self.mu, obj, (len(data) + 1) | 1)
            write_u32(self.mu, obj + 4, len(data))
            write_u32(self.mu, obj + 8, ptr)

    def append_string(self, obj, data):
        cur = self.get_string(obj)
        self.set_string(obj, cur + data)

    def hook_external(self, name):
        mu = self.mu
        r0 = mu.reg_read(UC_ARM_REG_R0)
        r1 = mu.reg_read(UC_ARM_REG_R1)
        r2 = mu.reg_read(UC_ARM_REG_R2)
        r3 = mu.reg_read(UC_ARM_REG_R3)
        sp = mu.reg_read(UC_ARM_REG_SP)
        ret = mu.reg_read(UC_ARM_REG_LR)

        def finish(val=None):
            if val is not None:
                mu.reg_write(UC_ARM_REG_R0, u32(val))
            mu.reg_write(UC_ARM_REG_PC, ret)

        if self.trace:
            print(f"HOOK {name} r0={r0:#x} r1={r1:#x} r2={r2:#x} r3={r3:#x}")

        if name == "_Znaj" and r0 == 0x41:
            print("TSCAND %d %#x LR=%#x" % ((((r3 << 32) | r2) & 0xFFFFFFFFFFFFFFFF), ((r3 << 32) | r2) & 0xFFFFFFFFFFFFFFFF, ret))

        if name == "JNI_GetStringUTFChars":
            finish(r1)
        elif name in ("_Znaj", "malloc"):
            addr = self.alloc(r0)
            if name == "malloc" and r0 >= 0x400000:
                self.hash_buf = addr
                if self.trace:
                    print(f"  hash_buf={addr:#x} size={r0:#x}")
            finish(addr)
        elif name in ("_ZdaPv", "_ZdlPv", "free", "__cxa_finalize", "__cxa_atexit"):
            finish(0)
        elif name == "_ZNSt6__ndk113__vector_baseIhNS_9allocatorIhEEED2Ev":
            finish(0)
        elif name == "__aeabi_memclr":
            mu.mem_write(r0, b"\x00" * r1)
            finish(r0)
        elif name == "__aeabi_memcpy":
            if self.trace:
                data = bytes(mu.mem_read(r1, min(r2, 96)))
                print(f"  memcpy dst={r0:#x} src={r1:#x} n={r2} data={data!r}")
            mu.mem_write(r0, bytes(mu.mem_read(r1, r2)))
            finish(r0)
        elif name == "strlen":
            finish(len(read_cstr(mu, r0)))
        elif name == "strcmp":
            a = read_cstr(mu, r0)
            b = read_cstr(mu, r1)
            finish(0 if a == b else (-1 if a < b else 1))
        elif name == "_ZNSt6__ndk16chrono12system_clock3nowEv":
            # On this 32-bit build the time_point is returned through an
            # sret pointer in r0. The native code immediately loads [r0].
            ms = self.chrono_ms if self.chrono_ms is not None else int(time.time() * 1000)
            ticks = int(ms) * 1000
            mu.mem_write(r0, struct.pack("<Q", ticks & 0xFFFFFFFFFFFFFFFF))
            finish()
        elif name == "__aeabi_ldivmod":
            num = (r1 << 32) | r0
            den = (r3 << 32) | r2
            if den == 0:
                q, rem = 0, 0
            else:
                q, rem = num // den, num % den
            mu.reg_write(UC_ARM_REG_R0, q & 0xFFFFFFFF)
            mu.reg_write(UC_ARM_REG_R1, (q >> 32) & 0xFFFFFFFF)
            finish()
        elif name == "base64_init_ex":
            finish(0)
        elif name == "base64_decode":
            src = read_cstr(mu, r1, r2 + 1)[:r2]
            out = base64.b64decode(src)
            mu.mem_write(r3, out)
            out_len_ptr = read_u32(mu, sp)
            write_u32(mu, out_len_ptr, len(out))
            finish(0)
        elif name == "_Z15AES_init_ctx_ivP7AES_ctxPKhS2_":
            self.aes_ctx[r0] = (bytes(mu.mem_read(r1, 32)), bytes(mu.mem_read(r2, 16)))
            finish(0)
        elif name == "_Z22AES_CBC_decrypt_bufferP7AES_ctxPhj":
            key, iv = self.aes_ctx.get(r0, (AES_KEY, b"\x00" * 16))
            ct = bytes(mu.mem_read(r1, r2))
            pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
            mu.mem_write(r1, pt)
            finish(0)
        elif name == "_ZNSt6__ndk112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE7reserveEj":
            finish(r0)
        elif name == "_ZNSt6__ndk112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE6assignEPKcj":
            self.set_string(r0, bytes(mu.mem_read(r1, r2)))
            finish(r0)
        elif name == "_ZNSt6__ndk112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE6__initEPKcj":
            self.set_string(r0, bytes(mu.mem_read(r1, r2)))
            finish(r0)
        elif name == "_ZNSt6__ndk112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE6appendEPKcj":
            if self.trace:
                data = bytes(mu.mem_read(r1, min(r2, 128)))
                print(f"  append obj={r0:#x} n={r2} data={data!r}")
            self.append_string(r0, bytes(mu.mem_read(r1, r2)))
            finish(r0)
        elif name == "_ZNSt6__ndk19to_stringEi":
            self.set_string(r0, str(r1 if r1 < 0x80000000 else r1 - 0x100000000))
            finish()
        elif name == "_ZNSt6__ndk19to_stringEy":
            if self.forced_timestamp:
                s = self.forced_timestamp
            else:
                s = str((r3 << 32) | r2)
            if self.trace:
                print(f"  to_string_u64 -> {s}")
            self.set_string(r0, s)
            finish()
        elif name == "_ZNSt6__ndk112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE2atEj":
            data = self.get_string(r0)
            ptr = self.write_bytes(data + b"\x00")
            finish(ptr + r1)
        elif name == "_ZNSt6__ndk112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE6insertEjjc":
            data = self.get_string(r0)
            ch = bytes([r3 & 0xFF]) * r2
            self.set_string(r0, data[:r1] + ch + data[r1:])
            finish(r0)
        elif name == "_ZNSt6__ndk16vectorIhNS_9allocatorIhEEEC2IPhEET_NS_9enable_ifIXaasr21__is_forward_iteratorIS6_EE5valuesr16is_constructibleIhNS_15iterator_traitsIS6_E9referenceEEE5valueES6_E4typeE":
            write_u32(mu, r0, r1)
            write_u32(mu, r0 + 4, r2)
            write_u32(mu, r0 + 8, r2)
            finish()
        elif name == "_ZN8picosha218hash256_hex_stringINSt6__ndk111__wrap_iterIPKhEEEENS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEET_SC_":
            data = bytes(mu.mem_read(r1, max(0, r2 - r1)))
            digest = hashlib.sha256(data).hexdigest()
            self.hash_inputs.append(data)
            print("PICOSHA_INPUT_LEN", len(data))
            print("PICOSHA_INPUT_REPR", repr(data[:500]))
            print("PICOSHA_INPUT_HEX", data[:500].hex())
            print("PICOSHA_DIGEST", digest)
            self.set_string(r0, digest)
            finish()
        elif name == "_Z19http_parse_query_exPciPiP14KeyValuePairExi":
            # r0/r1 order is intentionally probed; choose the argument that looks like a string.
            candidates = []
            for ptr in (r0, r1):
                try:
                    s = read_cstr(mu, ptr, 4096).decode("utf-8", "ignore")
                except Exception:
                    s = ""
                if s:
                    candidates.append((ptr, s))
            ptr, text = max(candidates, key=lambda x: len(x[1])) if candidates else (r0, "")
            count_ptr = r2
            pairs_ptr = r3
            max_count = read_u32(mu, sp)
            query = urlsplit(text).query
            if not query and "?" in text:
                query = text.split("?", 1)[1]
            pairs = parse_qsl(query, keep_blank_values=True)
            for idx, (k, v) in enumerate(pairs[:max_count]):
                kb = k.encode()
                vb = v.encode()
                kp = self.write_bytes(kb, True)
                vp = self.write_bytes(vb, True)
                off = pairs_ptr + idx * 16
                write_u32(mu, off, kp)
                write_u32(mu, off + 4, vp)
                write_u32(mu, off + 8, len(kb))
                write_u32(mu, off + 12, len(vb))
            # The caller later copies the non-query prefix before appending sorted key/value bytes.
            prefix_len = len(text.split("?", 1)[0].encode()) if "?" in text else len(text.encode())
            write_u32(mu, count_ptr, prefix_len)
            finish(len(pairs))
        elif name == "qsort":
            n = r1
            entries = []
            for i in range(n):
                off = r0 + i * r2
                kp = read_u32(mu, off)
                vp = read_u32(mu, off + 4)
                kl = read_u32(mu, off + 8)
                vl = read_u32(mu, off + 12)
                k = bytes(mu.mem_read(kp, kl))
                v = bytes(mu.mem_read(vp, vl))
                entries.append((k, v, kp, vp, kl, vl))
            entries.sort(key=lambda x: (x[0], x[1]))
            for i, (_, _, kp, vp, kl, vl) in enumerate(entries):
                off = r0 + i * r2
                write_u32(mu, off, kp)
                write_u32(mu, off + 4, vp)
                write_u32(mu, off + 8, kl)
                write_u32(mu, off + 12, vl)
            finish(0)
        elif name == "__stack_chk_fail":
            raise RuntimeError("stack check failed")
        else:
            raise NotImplementedError(f"unhandled external {name}")

    def hook_code(self, mu, address, size, user_data):
        if address == (STOP & ~1):
            mu.emu_stop()
            return
        name = self.plt_names.get(address)
        if name is None:
            for n, a in self.stub_names.items():
                if address == a:
                    name = n
                    break
        if name:
            self.hook_external(name)

    def hook_mem_write(self, mu, access, address, size, value, user_data):
        if not self.trace or self.hash_buf is None:
            return
        if self.hash_buf <= address < self.hash_buf + 0x120:
            pc = mu.reg_read(UC_ARM_REG_PC)
            print(f"  WRITE hash+{address - self.hash_buf:#04x} size={size} value={value:#x} pc={pc:#x}")

    def run(self):
        self.setup_memory()
        self.mu.hook_add(UC_HOOK_CODE, self.hook_code)
        self.mu.hook_add(UC_HOOK_MEM_WRITE, self.hook_mem_write)
        try:
            self.mu.emu_start(BASE + FUNC | 1, STOP, timeout=0, count=5_000_000)
        except UcError as e:
            pc = self.mu.reg_read(UC_ARM_REG_PC)
            sp = self.mu.reg_read(UC_ARM_REG_SP)
            print(f"UNICORN_ERROR {e} pc={pc:#x} sp={sp:#x}")
            raise
        r0 = self.mu.reg_read(UC_ARM_REG_R0)
        try:
            result = read_cstr(self.mu, r0, 4096).decode("utf-8", "replace")
        except Exception:
            result = f"<r0={r0:#x}>"
        print("RESULT_PTR", hex(r0))
        print("RESULT", result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--so", default=str(SO_PATH))
    ap.add_argument("--secret")
    ap.add_argument("--input")
    ap.add_argument("--secret-file")
    ap.add_argument("--input-file")
    ap.add_argument("--timestamp")
    ap.add_argument("--chrono-ms", type=int)
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()
    secret = args.secret if args.secret is not None else open(args.secret_file, encoding="utf-8").read()
    inp = args.input if args.input is not None else open(args.input_file, encoding="utf-8").read()
    Emulator(args.so, secret, inp, args.timestamp, args.chrono_ms, args.trace).run()


if __name__ == "__main__":
    main()
