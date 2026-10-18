#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iso_stream_proxy.py — 在线播放 Alist/115 上的 Blu-ray ISO（含无缝分支与章节）

mpv 无法直接解析蓝光 ISO 的 BDMV 结构。本脚本通过 HTTP Range 请求解析
ISO 的 UDF 文件系统，读取 BDMV 导航数据（PLAYLIST/*.mpls、CLIPINF/*.clpi），
把主播放列表（正片，含无缝分支的多段 m2ts）按 EP map 裁剪后拼接成一个
虚拟连续流，再在本地起 HTTP 服务，把 mpv 的 Range 请求翻译成对 CDN 的
对应字节区间请求并转发。mpv 即可像播普通视频一样在线看正片，不落盘。

模式一：BDMV（默认，v2）
  1. VRS/AVDP/VDS 定位分区起始 LBA
  2. 从 FSD 遍历 UDF 目录树，定位 BDMV/PLAYLIST、CLIPINF、STREAM
  3. 解析所有 .mpls，按总时长选主播放列表（--playlist 可指定）
  4. 各 PlayItem 的 IN/OUT 时间经 CLPI 的 EP map 映射到字节偏移，
     裁剪拼接成虚拟连续流；PlayMark 生成章节
  5. /movie.m2ts 播正片；/bd/<编号>.m2ts 播任意播放列表；
     /movie.json、/bd/<编号>.json 提供章节元数据（配合 mpv 端 Lua 脚本）

模式二：裸扫描（ISO 无 BDMV 或目录遍历失败时回退，即 v1 行为）
  扫描分区元数据区，找 fileType=5 的大文件（.m2ts），取体积最大者。

用法：
    python iso_stream_proxy.py "<Alist ISO URL>" [--list] [--port 8080]
                                [--playlist N|编号] [--mpv] [--selftest]
    --list      只解析并打印信息，不启动服务器
    --playlist  播放列表选择：0 起始序号（按时长降序）或 mpls 编号（如 00001）
    --mpv       服务器就绪后自动调用本目录的 mpv.exe 播放
    --selftest  运行内置合成数据自检（不需要 URL 和网络）
"""
import argparse
import bisect
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECTOR = 2048
PKT = 192  # m2ts 源包大小：4 字节 TP_extra_header + 188 字节 TS 包
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) iso-stream-proxy/2.0"
TS90 = 90000.0  # MPLS 时间戳单位（Hz）

TAG_AVDP = 2
TAG_PARTITION = 5
TAG_LOGICAL_VOL = 6
TAG_TERMINATING = 8
TAG_FSD = 256
TAG_FID = 257
TAG_FE = 261
TAG_EFE = 266

MIN_VIDEO_SIZE = 50 * 1024 * 1024  # 裸扫描模式：只当作视频候选的最小文件大小
EXTENT_SCAN_MIN = 100 * 1024 * 1024  # 裸扫描模式：分配描述符判定的最小分块

_LOG_FH = None
_LOG_LOCK = threading.Lock()


def log(text):
    """输出到控制台（容忍管道断裂）和可选日志文件。"""
    with _LOG_LOCK:
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        if _LOG_FH:
            try:
                _LOG_FH.write(text + "\n")
                _LOG_FH.flush()
            except Exception:
                pass


def u16(b, off):
    return struct.unpack_from("<H", b, off)[0]


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def u64(b, off):
    return struct.unpack_from("<Q", b, off)[0]


def be32(b, off):
    return struct.unpack_from(">I", b, off)[0]


def u16be(b, off):
    return struct.unpack_from(">H", b, off)[0]


def tag_checksum_ok(data, off):
    """ECMA-167 tag 校验和：字节 0..3 与 5..15 之和（mod 256）等于字节 4。"""
    if off + 16 > len(data):
        return False
    return (sum(data[off:off + 4]) + sum(data[off + 5:off + 16])) & 0xFF == data[off + 4]


def hms(sec):
    sec = int(round(sec))
    return "%02d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60)


class UdParseError(Exception):
    pass


class Remote:
    """通过 Range 请求读取远端 ISO 的任意字节区间，自动跟随 302 到 CDN，带重试。"""

    def __init__(self, url):
        self.alist_url = url
        self.final_url = None
        self.total = None  # ISO 总字节数（probe_total 探测）
        # 统计：定位慢在哪
        self.stat_req = 0        # HTTP 请求数
        self.stat_bytes = 0      # 累计下载字节
        self.stat_retry403 = 0   # 403 刷新次数
        # 并发闸：并行拉取太多会触发网盘风控（403），限制同时在途的请求
        self._sema = threading.BoundedSemaphore(4)
        # 403 刷新协调：多个线程同时撞 403 时只做一次刷新，其余等新地址
        self._refresh_lock = threading.Lock()
        self._gen = 0

    def probe_total(self):
        """探测 ISO 总大小，供分块读取层在文件尾截断请求（部分服务器对越界
        Range 会回 416 而不是截断）。"""
        try:
            with self.open(0, 1) as r:
                cr = r.headers.get("Content-Range")
                if cr and "/" in cr:
                    self.total = int(cr.rsplit("/", 1)[-1])
        except Exception:
            self.total = None

    def _stat(self, resp, length):
        self.stat_req += 1
        self.stat_bytes += length

    def open(self, offset, length):
        headers = {"User-Agent": UA, "Range": "bytes=%d-%d" % (offset, offset + length - 1)}
        last_exc = None
        for _ in range(12):
            gen = self._gen
            cur = self.final_url or self.alist_url
            try:
                with self._sema:
                    resp = self._follow(cur, headers)
                self.final_url = resp.geturl()
                self._stat(resp, length)
                return resp
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code in (401, 403):
                    # 跟随 302 后失败时 e.url 是 CDN 地址；只有 Alist 原链
                    # 本身拒签（sign 过期）才无解，其余一律刷新重试。
                    if (getattr(e, "url", None) or cur) == self.alist_url:
                        raise
                    # CDN 地址失效/风控：全局共享一次刷新（并发线程不重复刷）
                    with self._refresh_lock:
                        if self._gen == gen:
                            if self.stat_retry403 >= 8:
                                raise
                            self.stat_retry403 += 1
                            log("CDN 返回 %d，刷新直链（第 %d 次），稍候重试…"
                                % (e.code, self.stat_retry403))
                            time.sleep(1.5)  # 退避，让风控窗口过去
                            self.final_url = None
                            self._gen += 1
                        # else：别的线程已完成刷新，用新地址直接重试
                    continue
                if e.code == 404:
                    raise
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    cur = urllib.parse.urljoin(cur, e.headers["Location"])
                    continue
                time.sleep(1.0)  # 网络抖动重试
        raise last_exc

    def _follow(self, url, headers):
        cur = url
        for _ in range(10):
            req = urllib.request.Request(cur, headers=headers)
            try:
                return urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    cur = urllib.parse.urljoin(cur, e.headers["Location"])
                    continue
                raise
        raise RuntimeError("too many redirects")

    def read(self, offset, length):
        with self.open(offset, length) as r:
            return r.read()


class ChunkedFile:
    """对 Remote 的分块缓存读取层，供 UDF/BDMV 元数据解析使用。

    元数据解析是大量小读（几百字节～几十 KB），逐次发 HTTP 请求在高延迟
    网盘上不可接受。这里按块（默认 64KB）拉取并 LRU 缓存，命中即免请求；
    检测到顺序访问时后台预读下一块。"""

    def __init__(self, remote, chunk=128 * 1024, keep=64):
        self.remote = remote
        self.chunk = chunk
        self.keep = keep
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.last_idx = None
        self.prefetch = set()
        self.stat_hits = 0
        self.stat_miss = 0

    def read(self, offset, size):
        out = []
        while size > 0:
            idx, o = divmod(offset, self.chunk)
            data = self._chunk(idx)
            take = min(size, len(data) - o)
            if take <= 0:
                break  # EOF
            out.append(data[o:o + take])
            offset += take
            size -= take
        return b"".join(out)

    def _chunk(self, idx):
        with self.lock:
            data = self.cache.get(idx)
            if data is not None:
                self.cache.move_to_end(idx)
                self.stat_hits += 1
            sequential = self.last_idx is not None and idx == self.last_idx + 1
            self.last_idx = idx
        if data is not None:
            if sequential:
                self._prefetch_async(idx + 1)
            return data
        data = self._fetch(idx)
        with self.lock:
            self.cache[idx] = data
            self.cache.move_to_end(idx)
            while len(self.cache) > self.keep:
                self.cache.popitem(last=False)
            self.stat_miss += 1
        self._prefetch_async(idx + 1)
        return data

    def _fetch(self, idx):
        off = idx * self.chunk
        length = self.chunk
        total = self.remote.total
        if total is not None:
            length = min(length, max(0, total - off))
            if length <= 0:
                return b""
        last = None
        for _ in range(3):
            try:
                return self.remote.read(off, length)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):
                    raise
                last = e
                time.sleep(1.0)
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                last = e
                time.sleep(1.0)
        raise last

    def _prefetch_async(self, idx):
        with self.lock:
            if idx in self.cache or idx in self.prefetch:
                return
            self.prefetch.add(idx)

        def run():
            try:
                data = self._fetch(idx)
                if data:
                    with self.lock:
                        self.cache[idx] = data
                        while len(self.cache) > self.keep:
                            self.cache.popitem(last=False)
            except Exception:
                pass
            finally:
                with self.lock:
                    self.prefetch.discard(idx)

        threading.Thread(target=run, daemon=True).start()


# ==================== UDF 文件系统 ====================

def get_partition(remote):
    """解析 VDS，返回 (partition_start_lba, partition_len_sectors, block_size)。"""
    avdp = remote.read(256 * SECTOR, SECTOR)
    if u16(avdp, 0) != TAG_AVDP:
        raise UdParseError("sector 256 不是 AVDP")
    vds_lba = u32(avdp, 20)
    vds_len = u32(avdp, 16)
    vds = remote.read(vds_lba * SECTOR, vds_len)

    part_start = None
    part_len = None
    block_size = None
    off = 0
    while off + 512 <= len(vds):
        tag = u16(vds, off)
        if tag == TAG_TERMINATING:
            break
        if tag == TAG_PARTITION:
            part_start = u32(vds, off + 188)
            part_len = u32(vds, off + 192)
        elif tag == TAG_LOGICAL_VOL:
            block_size = u32(vds, off + 212)
        off += 512
    if part_start is None or part_len is None:
        raise UdParseError("VDS 中未找到分区描述符")
    return part_start, part_len, block_size or SECTOR, vds


class PartMap:
    """UDF 分区映射：把 (分区号, 逻辑块号) 解析为 ISO 绝对字节偏移。

    蓝光 ISO 常用 UDF 2.50 元数据分区：目录树/文件条目等元数据都存放在
    一个「元数据文件」里，FID/ICB 里的块号是元数据分区空间内的相对块号，
    必须经元数据文件的 extent 表换算成物理位置，否则全部读偏。"""

    def __init__(self, part_start):
        self.part_start = part_start
        self.meta_num = None       # 元数据分区的分区号
        self.meta_extents = []     # 元数据文件的物理范围 [(iso_byte_off, byte_len)]
        self._cum = []             # 累计字节数，二分用

    def set_meta_extents(self, extents):
        self.meta_extents = extents
        self._cum = []
        acc = 0
        for _, elen in extents:
            self._cum.append(acc)
            acc += elen

    def resolve(self, part_num, lba):
        """(分区号, 块号) -> ISO 绝对字节偏移。元数据空间越界返回 None。"""
        if part_num is not None and part_num == self.meta_num:
            off = lba * SECTOR
            i = bisect.bisect_right(self._cum, off) - 1
            if i < 0 or i >= len(self.meta_extents):
                return None
            iso0, elen = self.meta_extents[i]
            delta = off - self._cum[i]
            if delta >= elen:
                return None
            return iso0 + delta
        return (self.part_start + lba) * SECTOR


def build_pmap(remote, vds, part_start, part_len):
    """从 VDS 里的 LVD 解析分区映射，识别并装载元数据分区。"""
    pmap = PartMap(part_start)
    off = 0
    lvd = None
    while off + SECTOR <= len(vds):
        tag = u16(vds, off)
        if tag == TAG_TERMINATING:
            break
        if tag == TAG_LOGICAL_VOL:
            lvd = vds[off:off + SECTOR]  # 描述符是完整 2048B 扇区，映射表在 +480 起
            break
        off += SECTOR
    if lvd is None:
        return pmap
    # ECMA-167 3/10.6：mapTableLength@264，numPartitionMaps@268，
    # 分区映射表从 +440 开始；每个映射是 type(1) + length(1) + …。
    map_len = u32(lvd, 264)
    n_maps = u32(lvd, 268)
    pos = 440
    maps = []
    end = min(440 + map_len, len(lvd))
    for _ in range(n_maps):
        if pos + 2 > end:
            break
        mtype = lvd[pos]
        mlen = lvd[pos + 1]
        if mlen <= 0 or pos + mlen > end:
            break
        maps.append((mtype, bytes(lvd[pos:pos + mlen])))
        pos += mlen
    meta_idx = None
    meta_phys_num = 0
    for idx, (mtype, m) in enumerate(maps):
        if mtype == 2 and len(m) >= 64:
            # UDF 2.50 元数据分区映射（osta_udf.h metadataPartitionMap）：
            # reserved[2]@2，regid（flags@4，标识串@5..27），
            # volSeq@36，partitionNum@38，MetadataFileLocation@40
            if m[5:28].startswith(b"*UDF Metadata Partition"):
                meta_idx = idx
                meta_phys_num = u16(m, 38)
    if meta_idx is not None:
        meta_map = maps[meta_idx][1]
        meta_loc = u32(meta_map, 40)
        blk = remote.read((part_start + meta_loc) * SECTOR, SECTOR)
        ent = parse_fe_block(blk, own_part=0)
        # 元数据文件自身的 extent 在主分区空间（规范如此）
        ext = [((part_start + eloc) * SECTOR, elen) for _p, eloc, elen in ent.extents]
        if ext:
            # lb_addr 的分区引用号是「分区映射表序号」，不是映射内部的
            # partitionNum 字段——元数据映射通常是第 2 项，FID/ICB 里写 1。
            pmap.meta_num = meta_idx
            pmap.set_meta_extents(ext)
            log("检测到 UDF 2.50 元数据分区：映射序号 %d（物理分区 %d），"
                "元数据文件 @分区块 %d（%.2f MiB）"
                % (meta_idx, meta_phys_num, meta_loc,
                   sum(e[1] for e in ext) / 1048576.0))
    return pmap


class UdfEntry:
    __slots__ = ("file_type", "info_len", "alloc_type", "extents", "inline_data")

    def __init__(self):
        self.file_type = 0      # 4=目录 5=普通文件
        self.info_len = 0
        self.alloc_type = 0     # 0=short_ad 1=long_ad 2=ext_ad 3=inline
        self.extents = []       # [(part_num, part_rel_lba, byte_len), ...]
        self.inline_data = b""


def parse_fe_block(block, own_part=0):
    """解析单个 FE/EFE 块（ECMA-167 4/14.16、4/14.17）。

    两种文件条目的公共字段偏移一致：informationLength 在 +56，ICB tag flags
    在 +34，分配描述符类型取 flags 低 3 位。差异在尾部：FE 的
    lengthOfEA/lengthOfAD 在 +168/+172（AD 区起点 176），EFE 在 +208/+212
    （AD 区起点 216）。
    short_ad 的 extent 属于条目自身所在分区（own_part）；long_ad 自带分区号。
    """
    tag = u16(block, 0)
    if tag not in (TAG_FE, TAG_EFE):
        return None
    ent = UdfEntry()
    ent.file_type = block[27]
    ent.info_len = u64(block, 56)
    ent.alloc_type = u16(block, 34) & 0x7
    if tag == TAG_FE:
        ad_off = 176 + u32(block, 168)
        ad_len = u32(block, 172)
    else:
        ad_off = 216 + u32(block, 208)
        ad_len = u32(block, 212)
    ad_end = min(ad_off + ad_len, len(block))

    if ent.alloc_type == 3:
        ent.inline_data = block[ad_off:ad_off + ent.info_len]
        return ent

    step = {0: 8, 1: 16, 2: 20}.get(ent.alloc_type)
    if step is None:
        return None
    total = 0
    o = ad_off
    while o + step <= ad_end:
        elen = u32(block, o) & 0x3FFFFFFF
        if elen == 0:
            break
        if step == 20:  # ext_ad：extLength, recordedLength, informationLength, extLocation, partRef
            eloc = u32(block, o + 12)
            part = u16(block, o + 16)
        else:
            eloc = u32(block, o + 4)
            part = u16(block, o + 8) if step == 16 else own_part
        ent.extents.append((part, eloc, elen))
        total += elen
        o += step
    # 分段之和必须等于文件长度，否则视为解析失败（防止把 EA 等误当 AD）
    if total != ent.info_len:
        ent.extents = []
    return ent


def find_root_icb(cf, pmap, part_start, part_len):
    """在前部元数据区找 File Set Descriptor，返回根目录 ICB 的 (分区号, 块号)。

    FSD 扫描按 1MB 块并行拉取（上限 32MB——FSD 总在分区最前部）。
    DirectoryICB 按规范在 FSD+304，个别刻录软件写在 +400，两个候选都试；
    每个候选按记录的分区号经映射器解析（元数据分区 ISO 必需）。"""
    cap = min(32 * 1024 * 1024, part_len * SECTOR)
    base = part_start * SECTOR
    chunk = 1024 * 1024
    n_chunks = (cap + chunk - 1) // chunk

    def probe(i):
        size = min(chunk, cap - i * chunk)
        # 直接 1MB Range 读取：走分块缓存会放大请求数（8 请求/MB）
        data = cf.remote.read(base + i * chunk, size)
        hits = []
        o = 0
        while o + 512 <= len(data):
            # 校验 tag 校验和，滤掉视频/填充数据里的伪造标签
            if u16(data, o) == TAG_FSD and tag_checksum_ok(data, o):
                cands = []
                for cand in (304, 400):
                    elen = u32(data, o + cand) & 0x3FFFFFFF
                    lba = u32(data, o + cand + 4)
                    part = u16(data, o + cand + 8)
                    cands.append((elen, lba, part))
                hits.append((base + i * chunk + o, cands))
            o += 512
        return hits

    n_fsd = 0
    diag = []

    def accept(hits):
        nonlocal n_fsd
        for fsd_abs, cands in hits:
            n_fsd += 1
            if len(diag) < 5:
                elen, lba, part = cands[-1]  # +400 的规范字段
                diag.append("FSD@0x%X -> part=%d lba=%d elen=%d" % (fsd_abs, part, lba, elen))
            for elen, lba, part in cands:
                if lba <= 0 or lba >= part_len:
                    continue
                if elen and elen != SECTOR:
                    continue
                for pref in (part, None):
                    # pref=None：个别镜像把根目录放在主分区，映射失败时兜底
                    iso = pmap.resolve(part if pref is not None else 0, lba)
                    if iso is None:
                        continue
                    blk = cf.read(iso, SECTOR)
                    ent = parse_fe_block(blk, own_part=part if pref is not None else 0)
                    if ent is not None and ent.file_type == 4:
                        log("FSD @ISO 0x%X，根目录 ICB：分区 %d 块 %d（%s）" % (
                            fsd_abs, part, lba,
                            "元数据分区映射" if part == pmap.meta_num else "主分区"))
                        return (part, lba)
        return None

    # 先单独探测第 0 块（FSD 几乎总在分区最前 1MB）；未命中再按 4MB 批次
    # 并行扫描、找到即停。绝不能一次性提交全部块：线程池退出时会等所有
    # 任务完成，等于把扫描上限（32MB）全部下载完。
    found = accept(probe(0))
    if found is not None:
        return found
    for b0 in range(1, n_chunks, 4):
        batch = list(range(b0, min(b0 + 4, n_chunks)))
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            results = list(ex.map(probe, batch))
        for hits in results:
            found = accept(hits)
            if found is not None:
                return found
    log("FSD 扫描完成：共 %d 个 FSD，均未解析出有效根目录（扫描上限 %d MB，"
        "元数据分区%s）" % (n_fsd, cap // 1048576,
                          "已检测" if pmap.meta_num is not None else "未检测"))
    for d in diag:
        log("  候选 " + d)
    return None


def decode_fid_name(raw):
    """UDF 压缩 Unicode：首字节 8=Latin-1（高位为大小写标记），16=UCS-2BE。"""
    if not raw:
        return ""
    comp = raw[0]
    try:
        if comp == 8:
            return bytes(b & 0x7F for b in raw[1:]).decode("latin-1")
        if comp == 16:
            return raw[1:].decode("utf-16-be")
    except (UnicodeDecodeError, ValueError):
        pass
    return ""


def read_icb(cf, pmap, part_start, part_ref, lba, part_len):
    """读取 (分区号, 块号) 处的文件条目。"""
    iso = pmap.resolve(part_ref, lba)
    if iso is None:
        return None
    blk = cf.read(iso, SECTOR)
    ent = parse_fe_block(blk, own_part=part_ref)
    if ent is None:
        return None
    if ent.alloc_type == 3 or ent.extents:
        return ent
    # 精确 AD 解析失败时（分段之和 != 文件长度），对大文件尝试启发式扫描
    if ent.info_len >= EXTENT_SCAN_MIN:
        run = scan_extents(blk, part_len, ent.info_len)
        if run:
            ent.extents = [(part_ref, c, e) for c, e in run]
            return ent
    return None if not ent.extents else ent


def entry_data(cf, pmap, part_start, ent):
    """读取 UdfEntry 的文件内容（extent 按各自分区号解析）。"""
    if ent.alloc_type == 3:
        return ent.inline_data
    if not ent.extents:
        return b""
    parts = []
    left = ent.info_len
    for part, eloc, elen in ent.extents:
        iso = pmap.resolve(part, eloc)
        if iso is None:
            break
        take = min(left, elen)
        if take <= 0:
            break
        parts.append(cf.read(iso, take))
        left -= take
    return b"".join(parts)


def read_dir(cf, pmap, part_start, part_ref, lba, part_len):
    """读取目录文件，返回 {小写文件名: (子项分区号, 子项 ICB 块号)}。"""
    ent = read_icb(cf, pmap, part_start, part_ref, lba, part_len)
    if ent is None or ent.file_type != 4:
        return None
    data = entry_data(cf, pmap, part_start, ent)
    entries = {}
    off = 0
    n = len(data)
    while off + 38 <= n:
        if u16(data, off) != TAG_FID:
            break
        chars = data[off + 18]
        l_fi = data[off + 19]
        l_iu = u16(data, off + 36)
        child_lba = u32(data, off + 24)
        child_part = u16(data, off + 28)
        name = decode_fid_name(data[off + 38 + l_iu: off + 38 + l_iu + l_fi])
        if not (chars & 0x08) and name and child_lba:  # 0x08 = 父目录(..)项
            entries[name.lower()] = (child_part, child_lba)
        off += (38 + l_iu + l_fi + 3) & ~3  # FID 按 4 字节对齐
    return entries


def walk_bdmv(cf, pmap, part_start, part_len):
    """从根目录遍历到 BDMV，返回 {'bdmv':…, 'playlist':…, 'clipinf':…, 'stream':…}，
    各值为 {小写文件名: (分区号, ICB 块号)}；失败返回 None。"""
    root = find_root_icb(cf, pmap, part_start, part_len)
    if root is None:
        return None
    part, lba = root
    top = read_dir(cf, pmap, part_start, part, lba, part_len)
    if not top or "bdmv" not in top:
        log("根目录未找到 BDMV（%s）" % (
            ("条目：" + ", ".join(sorted(top)[:12])) if top else "目录读取失败"))
        return None
    bpart, blba = top["bdmv"]
    bdmv = read_dir(cf, pmap, part_start, bpart, blba, part_len)
    if not bdmv:
        log("BDMV 目录读取失败")
        return None
    out = {"bdmv": bdmv}
    for sub in ("playlist", "clipinf", "stream"):
        if sub in bdmv:
            spart, slba = bdmv[sub]
            d = read_dir(cf, pmap, part_start, spart, slba, part_len)
            if d is not None:
                out[sub] = d
    if "playlist" not in out or "stream" not in out:
        log("BDMV 子目录缺失（现有：%s）" % ", ".join(sorted(out)))
        return None
    return out


# ==================== 裸扫描模式（v1 兼容） ====================

def find_video_files(remote, part_start, part_len_hint, max_scan=120 * 1024 * 1024):
    """扫描分区元数据区，返回文件 EFE 列表：{abs_lba, size, extents:[(iso_off,elen)]}。"""
    files = []
    scanned = 0
    while scanned < max_scan:
        chunk = 2 * 1024 * 1024
        data = remote.read(part_start * SECTOR + scanned, chunk)
        for off in range(0, len(data) - SECTOR, SECTOR):
            tag = u16(data, off)
            if tag not in (TAG_FE, TAG_EFE):
                continue
            ft = data[off + 27]
            if ft != 5:
                continue
            info_len = u64(data, off + 56)
            if info_len < MIN_VIDEO_SIZE:
                continue
            fe = data[off:off + SECTOR]
            extents = parse_extents(fe, part_len_hint, info_len)
            if not extents:
                continue
            abs_lba = part_start + scanned // SECTOR + off // SECTOR
            files.append({"abs_lba": abs_lba, "size": info_len, "extents": extents})
        scanned += chunk
    return files


def parse_extents(fe, part_len_hint, info_len):
    """在 EFE 扇区中定位分配描述符，返回 [(eloc, extent_len), ...]。

    分配描述符类型由 ICB flags 位 0 决定（1=long_ad 16 字节，0=short_ad 8 字节）。
    EFE 头部（信息长度等字段）可能被误判为分配描述符，因此用「分段总长 == 文件大小」
    作为判别：真正的分配描述符分段之和必然等于 info_len。
    """
    flags = u16(fe, 34)
    long_ad = bool(flags & 1)
    step = 16 if long_ad else 8

    best = None
    for o in range(0, SECTOR - step, 4):
        elen = u32(fe, o) & 0x3FFFFFFF
        eloc = u32(fe, o + 4)
        if elen < EXTENT_SCAN_MIN or eloc >= part_len_hint:
            continue
        run = []
        total = 0
        oo = o
        while oo + step <= SECTOR:
            e = u32(fe, oo) & 0x3FFFFFFF
            c = u32(fe, oo + 4)
            if e == 0 or c >= part_len_hint:
                break
            run.append((c, e))
            total += e
            oo += step
        if run and total == info_len:
            best = run
            break
    return best or []


def scan_extents(block, part_len_hint, info_len):
    """parse_extents 的通用版（步长按 flags 决定），供目录模式回退使用。"""
    flags = u16(block, 34)
    step = 16 if (flags & 1) else 8
    for o in range(0, SECTOR - step, 4):
        elen = u32(block, o) & 0x3FFFFFFF
        eloc = u32(block, o + 4)
        if elen <= 0 or eloc >= part_len_hint:
            continue
        run = []
        total = 0
        oo = o
        while oo + step <= SECTOR:
            e = u32(block, oo) & 0x3FFFFFFF
            c = u32(block, oo + 4)
            if e == 0 or c >= part_len_hint:
                break
            run.append((c, e))
            total += e
            oo += step
        if run and total == info_len:
            return run
    return []


# ==================== BDMV 导航数据解析 ====================
# 格式依据 libbluray（mpls_parse.c / clpi_parse.c）核对。
# 时间戳：MPLS 的 IN/OUT/PlayMark 为 90kHz；EP map 的 PTS 为 45kHz。

def parse_mpls(data):
    """解析 .mpls，返回 {items, marks, duration90, sub_paths}。

    items: [(clip_id, in90, out90, stc_id)]
    marks: [(mark_type, play_item_ref, ts90)]   # type 1 = 章节入口标记
    """
    if len(data) < 20 or data[0:4] != b"MPLS":
        raise UdParseError("不是 MPLS 文件")
    # BDMV 导航文件内部字段全部为大端（libbluray BITSTREAM 按 MSB-first 读取）
    list_pos = be32(data, 8)
    mark_pos = be32(data, 12)
    if list_pos <= 0 or list_pos + 8 > len(data):
        raise UdParseError("MPLS 头部损坏")

    pl_len = be32(data, list_pos)
    n_items = u16be(data, list_pos + 6)
    n_sub = u16be(data, list_pos + 8)

    items = []
    o = list_pos + 10
    end = min(list_pos + 4 + pl_len, len(data))
    for _ in range(n_items):
        if o + 2 > end:
            break
        ilen = u16be(data, o)
        q = o + 2
        if ilen < 18 or q + ilen > end:
            break
        clip_id = data[q:q + 5].decode("ascii", "replace")
        # q+9..q+11：11 位保留 + 1 位 multi_angle + 4 位 connection_condition
        # q+11：stc_id；q+12：IN_time；q+16：OUT_time
        stc_id = data[q + 11]
        in90 = be32(data, q + 12)
        out90 = be32(data, q + 16)
        items.append((clip_id, in90, out90, stc_id))
        o = q + ilen

    marks = []
    if 0 < mark_pos <= len(data) - 6:
        m_count = u16be(data, mark_pos + 4)
        o = mark_pos + 6
        for _ in range(m_count):
            if o + 14 > len(data):
                break
            mtype = data[o + 1]
            ref = u16be(data, o + 2)
            ts = be32(data, o + 4)
            marks.append((mtype, ref, ts))
            o += 14

    duration90 = sum(max(0, out - intr) for _, intr, out, _ in items)
    return {"items": items, "marks": marks, "duration90": duration90,
            "sub_paths": n_sub}


def parse_clpi(data):
    """解析 .clpi，返回 {num_packets, eps:[(pts45, spn)]}。

    eps 为视频 PID 的入口点表（按 pts 升序去重）。
    PTS 重建（45kHz）：(coarse.pts_ep & ~1) << 18 + fine.pts_ep << 8
    SPN 重建：        (coarse.spn_ep & ~0x1FFFF) + fine.spn_ep
    """
    if len(data) < 64 or data[0:4] != b"HDMV":
        raise UdParseError("不是 CLPI 文件")
    cpi_addr = be32(data, 16)
    num_packets = be32(data, 56)

    eps = []
    if cpi_addr and cpi_addr + 8 <= len(data):
        cpi_len = be32(data, cpi_addr)
        if cpi_len:
            # CPI 头：4 字节长度，12 位保留，4 位 type（1=EP map 存在）
            cpi_type = data[cpi_addr + 5] & 0x0F
            ep_map_pos = cpi_addr + 6
            num_pid = data[cpi_addr + 7]
            if cpi_type == 1 and num_pid and ep_map_pos + 8 + num_pid * 12 <= len(data):
                # 选视频流的 entry：优先 PID 0x1011（每 entry 12 字节）
                pick = None
                for i in range(num_pid):
                    e = cpi_addr + 8 + i * 12
                    pid = u16be(data, e)
                    if pick is None or pid == 0x1011:
                        pick = e
                        if pid == 0x1011:
                            break
                if pick:
                    # entry+2 起 6 字节位域：reserved(10) type(4) coarse(16) fine(18)
                    bits = int.from_bytes(data[pick + 2:pick + 8], "big")
                    n_coarse = (bits >> 18) & 0xFFFF
                    n_fine = bits & 0x3FFFF
                    stream_pos = ep_map_pos + be32(data, pick + 8)
                    coarse_end = stream_pos + 4 + n_coarse * 8
                    if stream_pos + 4 <= len(data) and n_coarse and n_fine:
                        fine_start = be32(data, stream_pos)
                        fine_base = stream_pos + fine_start
                        # 数据截断时放弃 EP 表（外层会回退为整段 clip）
                        if (coarse_end <= len(data)
                                and fine_base + n_fine * 4 <= len(data)):
                            coarse = []
                            for i in range(n_coarse):
                                v = int.from_bytes(data[stream_pos + 4 + i * 8:
                                                        stream_pos + 12 + i * 8], "big")
                                coarse.append(((v >> 46) & 0x3FFFF,   # ref_ep_fine_id 18
                                               (v >> 32) & 0x3FFF,    # pts_ep 14
                                               v & 0xFFFFFFFF))       # spn_ep 32
                            fine = []
                            for i in range(n_fine):
                                v = int.from_bytes(data[fine_base + i * 4:
                                                        fine_base + 4 + i * 4], "big")
                                # fine 32 位：angle(1) i_end_offset(3) pts_ep(11) spn_ep(17)
                                fine.append(((v >> 17) & 0x7FF,       # pts_ep
                                             v & 0x1FFFF))            # spn_ep
                            for i, (ref, c_pts, c_spn) in enumerate(coarse):
                                end_ref = coarse[i + 1][0] if i + 1 < n_coarse else n_fine
                                end_ref = min(end_ref, n_fine)
                                base_pts = (c_pts & ~1) << 18
                                base_spn = c_spn & ~0x1FFFF
                                for j in range(ref, end_ref):
                                    f_pts, f_spn = fine[j]
                                    eps.append((base_pts + (f_pts << 8),
                                                base_spn + f_spn))
                eps.sort()
                # 去重（同点重复入口保留更小的 SPN）
                dedup = []
                for pt, sp in eps:
                    if not dedup or pt != dedup[-1][0]:
                        dedup.append((pt, sp))
                    elif sp < dedup[-1][1]:
                        dedup[-1] = (pt, sp)
                eps = dedup

    return {"num_packets": num_packets, "eps": eps}


def parse_stc_starts(data):
    """解析 CLPI 的 SequenceInfo，返回 [(offset_stc_id, [spn_stc_start, ...]), ...]。"""
    if len(data) < 64 or data[0:4] != b"HDMV":
        return []
    seq_addr = be32(data, 8)
    if not seq_addr or seq_addr + 6 > len(data):
        return []
    out = []
    n_atc = data[seq_addr + 5]
    o = seq_addr + 6
    for _ in range(n_atc):
        if o + 6 > len(data):
            break
        n_stc = data[o + 4]
        off_stc = data[o + 5]
        o += 6
        stc_list = []
        for _ in range(n_stc):
            if o + 14 > len(data):
                break
            # STC 条目：pcr_pid(2) spn_stc_start(4) start(4) end(4)
            stc_list.append(be32(data, o + 2))
            o += 14
        out.append((off_stc, stc_list))
    return out


def filter_eps_by_stc(eps, stc_ranges, stc_id):
    """只保留指定 STC 序列起点之后的 EP（多 STC 序列的 clip 需要）。"""
    if not stc_ranges or not eps:
        return eps
    for off_stc, starts in stc_ranges:
        if starts and off_stc <= stc_id < off_stc + len(starts):
            threshold = starts[stc_id - off_stc]
            return [e for e in eps if e[1] >= threshold]
    return eps


def spn_before(eps, ts45):
    """EP 表中最后一个 pts <= ts45 的入口 SPN；ts 在表前返回 0。"""
    if not eps:
        return None
    i = bisect.bisect_right(eps, (ts45, float("inf"))) - 1
    if i < 0:
        return 0
    return eps[i][1]


def spn_after(eps, ts45):
    """EP 表中第一个 pts > ts45 的入口 SPN（作为 OUT 裁剪边界，保证切口不落在
    访问单元中间）；ts 在表尾之后返回 None（调用方用整段结尾）。"""
    if not eps:
        return None
    i = bisect.bisect_right(eps, (ts45, float("inf")))
    if i >= len(eps):
        return None
    return eps[i][1]


# ==================== 虚拟连续流拼装 ====================

def slice_extents(extents, off, size):
    """在 [(iso_byte_offset, byte_len), ...] 上切 [off, off+size)，
    返回 [(iso_offset, take), ...]。越界部分被截断。"""
    out = []
    base = 0
    for iso, elen in extents:
        lo = max(off, base)
        hi = min(off + size, base + elen)
        if hi > lo:
            out.append((iso + (lo - base), hi - lo))
        base += elen
        if base >= off + size:
            break
    return out


class ClipStore:
    """懒加载缓存各 clip 的 CLPI 数据与 extent 表。"""

    def __init__(self, cf, pmap, part_start, part_len, clipinf_dir, stream_dir):
        self.cf = cf
        self.pmap = pmap
        self.part_start = part_start
        self.part_len = part_len
        self.clipinf_dir = clipinf_dir or {}
        self.stream_dir = stream_dir or {}
        self._clpi = {}
        self._extents = {}

    def _read_file_part(self, part, icb, offset, size):
        """只读文件 [offset, offset+size)（按 extent 映射），供大文件局部读取。"""
        ent = read_icb(self.cf, self.pmap, self.part_start, part, icb, self.part_len)
        if ent is None or (ent.alloc_type != 3 and not ent.extents):
            return b""
        if ent.alloc_type == 3:
            return ent.inline_data[offset:offset + size]
        phys = []
        for p, eloc, elen in ent.extents:
            iso = self.pmap.resolve(p, eloc)
            if iso is not None:
                phys.append((iso, elen))
        parts = []
        for iso, take in slice_extents(phys, offset, size):
            parts.append(self.cf.read(iso, take))
        return b"".join(parts)

    def clpi(self, clip_id):
        if clip_id in self._clpi:
            return self._clpi[clip_id]
        info = None
        loc = self.clipinf_dir.get(clip_id.lower() + ".clpi")
        if loc is not None:
            part, icb = loc
            try:
                # 渐进精确读取：头 64B 定位 cpi_addr → cpi 段头 8B 得知段长
                # → 只读 [0, cpi_addr+4+cpi_len)（序列/节目/CPI 均在前部）
                hdr = self._read_file_part(part, icb, 0, 64)
                if hdr[:4] == b"HDMV" and len(hdr) >= 64:
                    cpi_addr = be32(hdr, 16)
                    data = b""
                    if cpi_addr:
                        cpi_head = self._read_file_part(part, icb, cpi_addr, 8)
                        if len(cpi_head) >= 8:
                            cpi_len = be32(cpi_head, 0)
                            size = min(cpi_addr + 4 + cpi_len, 4 * 1024 * 1024)
                            data = self._read_file_part(part, icb, 0, size)
                    if data:
                        info = parse_clpi(data)
                        info["stc"] = parse_stc_starts(data)
            except UdParseError:
                info = None
            except Exception:
                info = None
        self._clpi[clip_id] = info
        return info

    def extents(self, clip_id):
        """返回 clip 的物理 extent 列表 [(iso_byte_offset, byte_len)]。"""
        if clip_id in self._extents:
            return self._extents[clip_id]
        ext = []
        loc = self.stream_dir.get(clip_id.lower() + ".m2ts")
        if loc is not None:
            part, icb = loc
            ent = read_icb(self.cf, self.pmap, self.part_start, part, icb, self.part_len)
            if ent and ent.extents:
                for p, eloc, elen in ent.extents:
                    iso = self.pmap.resolve(p, eloc)
                    if iso is not None:
                        ext.append((iso, elen))
        self._extents[clip_id] = ext
        return ext


class Piece:
    """虚拟流中的一段连续物理区间 [v0, v1) -> ISO 字节 [iso0, iso0+v1-v0)。"""

    __slots__ = ("v0", "v1", "iso0")

    def __init__(self, v0, v1, iso0):
        self.v0 = v0
        self.v1 = v1
        self.iso0 = iso0


class VStream:
    """一个播放列表对应的虚拟连续流：逻辑偏移 -> ISO 字节偏移 的映射。"""

    def __init__(self, name, pieces, total, meta):
        self.name = name
        self.pieces = pieces
        self.total = total
        self.meta = meta
        self.starts = [p.v0 for p in pieces]

    def iter_ranges(self, a, b):
        """迭代 [a, b]（含）区间对应的 (iso_offset, want) 物理读取块。"""
        pos = a
        while pos <= b:
            i = bisect.bisect_right(self.starts, pos) - 1
            if i < 0:
                raise UdParseError("虚拟流范围越界")
            p = self.pieces[i]
            seg_end = min(b + 1, p.v1)
            yield p.iso0 + (pos - p.v0), seg_end - pos
            pos = seg_end


def build_vstream(pl_name, pl, clips):
    """把播放列表的各 PlayItem 经 EP map 裁剪后拼接为虚拟流。

    裁剪依赖 CLPI 的 EP map；无 EP map 时整段 clip 原样保留——大多数原盘
    IN/OUT 即 clip 首尾，此时无副作用；分支盘会有少量冗余帧。
    """
    # 高延迟网盘：先并行预取全部 clip 的 extent/CLPI（IO 是瓶颈）
    uniq = list(dict.fromkeys(c for c, _, _, _ in pl["items"]))
    if len(uniq) > 1:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda c: (clips.extents(c), clips.clpi(c)), uniq))

    pieces = []
    cursor = 0
    for clip_id, in90, out90, stc_id in pl["items"]:
        ext = clips.extents(clip_id)
        if not ext:
            continue
        info = clips.clpi(clip_id)
        eps = filter_eps_by_stc(info["eps"], info.get("stc"), stc_id) if info else []
        if eps:
            s_in = spn_before(eps, in90 // 2)
            s_out = spn_after(eps, out90 // 2)
            n_pkts = info["num_packets"]
            spn_in = s_in if s_in is not None else 0
            spn_out = n_pkts if s_out is None else min(s_out, n_pkts)
            if spn_out <= spn_in:  # 兜底：整段
                spn_in, spn_out = 0, n_pkts
            byte0, byte1 = spn_in * PKT, spn_out * PKT
        else:
            byte0, byte1 = 0, sum(elen for _, elen in ext)
        want = byte1 - byte0
        if want <= 0:
            continue

        # 在 clip 的物理 extent 上切出 [byte0, byte1) 对应的连续块
        off = 0
        for iso, elen in ext:
            lo = max(byte0, off)
            hi = min(byte1, off + elen)
            if hi > lo:
                pieces.append(Piece(cursor, cursor + (hi - lo), iso + (lo - off)))
                cursor += hi - lo
            off += elen
            if off >= byte1:
                break

    meta = build_meta(pl_name, pl, pieces, cursor)
    return VStream(pl_name, pieces, cursor, meta)


def build_meta(pl_name, pl, pieces, total):
    """生成章节元数据（PlayMark type=1 为章节入口标记）。"""
    items = pl["items"]
    secs = []
    for mtype, ref, ts in pl["marks"]:
        if mtype != 1 or ref >= len(items):
            continue
        rel = sum(max(0, out - intr) for _, intr, out, _ in items[:ref])
        _, in90, out90, _ = items[ref]
        rel += ts - in90
        rel = max(0, min(rel, pl["duration90"]))
        sec = rel / TS90
        if sec < 1.0:
            continue
        if any(abs(sec - s) < 0.5 for s in secs):
            continue
        secs.append(sec)
    chapters = [{"t": round(t, 3), "title": "第 %d 章" % (i + 1)}
                for i, t in enumerate(secs)]
    return {
        "name": pl_name,
        "duration": round(pl["duration90"] / TS90, 3),
        "items": len(items),
        "clips": [c for c, _, _, _ in items],
        "chapters": chapters,
        "size": total,
        "pieces": len(pieces),
    }


# ==================== HTTP 服务 ====================

class StreamProxy(BaseHTTPRequestHandler):
    streams = None   # {name: VStream} 已构建的流
    builder = None   # 懒构建：name -> VStream | None
    build_lock = threading.Lock()
    main = None      # 主播放列表名
    catalog = None   # [(name, duration_sec, chapters, items)] 全部播放列表
    remote = None
    ready = False    # False = 仍在解析，所有请求返回 503

    protocol_version = "HTTP/1.1"
    server_version = "iso-stream-proxy/2.0"

    @classmethod
    def get_stream(cls, name):
        vs = cls.streams.get(name)
        if vs is not None or cls.builder is None:
            return vs
        with cls.build_lock:
            vs = cls.streams.get(name)
            if vs is not None:
                return vs
            vs = cls.builder(name)
            if vs is not None:
                cls.streams[name] = vs
            return vs

    def log_message(self, fmt, *args):
        log("[proxy] " + (fmt % args))

    def _headers(self, code, h):
        self.send_response(code)
        for k, v in h.items():
            self.send_header(k, v)
        self.end_headers()

    def _body(self, code, ctype, body, head_only):
        self._headers(code, {"Content-Type": ctype,
                             "Content-Length": str(len(body)),
                             "Accept-Ranges": "bytes"})
        if not head_only:
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        if not StreamProxy.ready:
            # 解析尚未完成：明确返回 503，调用方能区分「解析中」与「进程已死」
            self._body(503, "text/plain; charset=utf-8",
                       "正在解析 BDMV…".encode("utf-8"), head_only)
            return
        path = self.path.split("?")[0]
        if path == "/":
            rows = []
            for name, dur, n_ch, n_it in (self.catalog or []):
                mark = " ← 正片" if name == self.main else ""
                rows.append('<li><a href="/bd/%s.m2ts">%s.m2ts</a> — %s，%d 段，%d 章%s</li>' % (
                    name, name, hms(dur), n_it, n_ch, mark))
            body = ("<html><body><h3>iso-stream-proxy — 播放列表</h3><ul>%s</ul>"
                    "<p>正片直链：<a href='/movie.m2ts'>/movie.m2ts</a>　"
                    "章节元数据：<a href='/movie.json'>/movie.json</a></p></body></html>"
                    % "".join(rows)).encode("utf-8")
            self._body(200, "text/html; charset=utf-8", body, head_only)
            return
        if path == "/favicon.ico":
            self.send_error(404)
            return

        m = re.match(r"^/bd/([^/]+)\.json$", path)
        if m:
            vs = self.get_stream(m.group(1))
            if vs is None:
                self.send_error(404)
                return
            self._body(200, "application/json; charset=utf-8",
                       json.dumps(vs.meta, ensure_ascii=False).encode("utf-8"), head_only)
            return
        if path == "/movie.json" and self.main:
            vs = self.get_stream(self.main)
            if vs is None:
                self.send_error(404)
                return
            self._body(200, "application/json; charset=utf-8",
                       json.dumps(vs.meta, ensure_ascii=False).encode("utf-8"), head_only)
            return

        m = re.match(r"^/bd/([^/]+)\.m2ts$", path)
        if m:
            vs = self.get_stream(m.group(1))
        elif path == "/movie.m2ts" and self.main:
            vs = self.get_stream(self.main)
        else:
            vs = None
        if vs is None:
            self.send_error(404)
            return

        rng = self.headers.get("Range")
        if rng:
            mm = re.match(r"bytes=(\d*)-(\d*)", rng)
            if not mm:
                self.send_error(416)
                return
            a = int(mm.group(1)) if mm.group(1) else 0
            b = int(mm.group(2)) if mm.group(2) else vs.total - 1
            if a > b or a >= vs.total:
                self._headers(416, {"Content-Range": "bytes */%d" % vs.total,
                                    "Content-Length": "0"})
                return
            b = min(b, vs.total - 1)
        else:
            a, b = 0, vs.total - 1

        headers = {
            "Content-Type": "video/mp2t",
            "Accept-Ranges": "bytes",
            "Content-Length": str(b - a + 1),
        }
        if b < vs.total - 1 or a > 0:
            headers["Content-Range"] = "bytes %d-%d/%d" % (a, b, vs.total)
        self._headers(206 if a > 0 or b < vs.total - 1 else 200, headers)
        if head_only:
            return
        try:
            for iso, want in vs.iter_ranges(a, b):
                with self.remote.open(iso, want) as resp:
                    remain = want
                    while remain > 0:
                        chunk = resp.read(min(remain, 262144))
                        if not chunk:
                            raise RuntimeError("远端提前断流")
                        self.wfile.write(chunk)
                        remain -= len(chunk)
            self.wfile.flush()
        except urllib.error.HTTPError as e:
            self.log_message("远端错误 %s：可能是 sign 过期，请换新链接重启", e.code)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            self.log_message("stream error: %s", e)


# ==================== CLI ====================

def resolve_bdmv(cf, pmap, part_start, part_len):
    """返回 (tree, ClipStore)；BDMV 不存在或遍历失败返回 (None, None)。"""
    try:
        tree = walk_bdmv(cf, pmap, part_start, part_len)
    except Exception as e:
        log("目录遍历失败：%s（回退到裸扫描模式）" % e)
        return None, None
    if not tree:
        return None, None
    return tree, ClipStore(cf, pmap, part_start, part_len,
                           tree.get("clipinf"), tree["stream"])


def list_playlists(cf, pmap, part_start, part_len, tree, limit=2000):
    """解析全部 .mpls，返回 [(name, pl)] 按时长降序。CLPI 不在此阶段读取。
    网盘高延迟：并行读取（ChunkedFile 线程安全）。"""
    names = sorted(k for k in tree["playlist"] if k.endswith(".mpls"))[:limit]

    def load(nm):
        part, icb = tree["playlist"][nm]
        ent = read_icb(cf, pmap, part_start, part, icb, part_len)
        if ent is None:
            return None
        data = entry_data(cf, pmap, part_start, ent)
        try:
            return (nm[:-5], parse_mpls(data))
        except UdParseError:
            return None

    pls = []
    if names:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r in ex.map(load, names):
                if r:
                    pls.append(r)
    pls.sort(key=lambda kv: kv[1]["duration90"], reverse=True)
    return pls


def print_playlist_table(pls, chosen=None):
    print("播放列表（按时长降序，前 20）：")
    for i, (name, pl) in enumerate(pls[:20]):
        cur = " ←" if chosen is not None and name == chosen else ""
        print("  [%d] %s  %s  %2d 段  %3d 章%s" % (
            i, name, hms(pl["duration90"] / TS90), len(pl["items"]),
            sum(1 for m in pl["marks"] if m[0] == 1), cur))
    if len(pls) > 20:
        print("  … 共 %d 个（--playlist 序号或编号可选择）" % len(pls))


def setup_fallback(args, remote, part_start, part_len):
    """裸扫描回退模式（v1 行为）。返回 True=可服务 / False=--list 完成。"""
    log("未找到 BDMV 结构，使用裸扫描模式…")
    try:
        files = find_video_files(remote, part_start, part_len_hint=part_len)
    except urllib.error.HTTPError as e:
        log("请求失败（HTTP %d）：很可能是 sign 已过期。请重新复制 Alist 的最新链接再运行。" % e.code)
        raise SystemExit(1)
    if not files:
        log("未找到视频文件")
        raise SystemExit(1)
    files.sort(key=lambda f: f["size"], reverse=True)
    log("ISO 内的大视频文件（按大小降序）：")
    for i, f in enumerate(files):
        off0 = f["extents"][0][0]
        log("  [%d] abs=%d  %10.2f GiB  首段偏移 %.2f GiB  分段 %d" % (
            i, f["abs_lba"], f["size"] / 1024 ** 3,
            off0 * SECTOR / 1024 ** 3, len(f["extents"])))
    if args.list:
        return False
    idx = args.index if args.index >= 0 else 0
    pick = files[idx]
    cursor = 0
    pieces = []
    for eloc, elen in pick["extents"]:
        iso = (part_start + eloc) * SECTOR
        pieces.append(Piece(cursor, cursor + elen, iso))
        cursor += elen
    log("")
    log("选定正片：%d 号，%.2f GiB，共 %d 段" % (idx, pick["size"] / 1024 ** 3, len(pieces)))
    StreamProxy.streams = {"0": VStream("0", pieces, pick["size"],
                                        {"name": "scan%d" % idx, "duration": 0, "items": 1,
                                         "clips": [], "chapters": [], "size": pick["size"],
                                         "pieces": len(pieces)})}
    StreamProxy.main = "0"
    return True


def log_stats(remote, cf, t0, tag):
    """打印累计 IO 统计，用于定位「慢在哪」。"""
    hits = getattr(cf, "stat_hits", 0)
    total = hits + getattr(cf, "stat_miss", 0)
    log("[%s] 耗时 %.1fs｜HTTP 请求 %d 次｜下载 %.2f MiB｜分块缓存命中 %d/%d%s" % (
        tag, time.time() - t0, remote.stat_req, remote.stat_bytes / 1048576.0,
        hits, total, ("｜403 自动刷新 %d 次" % remote.stat_retry403)
        if remote.stat_retry403 else ""))


def setup_streams(args, remote):
    """解析 ISO 并填充 StreamProxy。返回 True=进入服务 / False=--list 完成；
    失败时抛 SystemExit(1)。"""
    t0 = time.time()
    try:
        remote.probe_total()
        part_start, part_len, _bs, vds = get_partition(remote)
    except urllib.error.HTTPError as e:
        log("请求失败（HTTP %d）：很可能是 sign 已过期。请重新复制 Alist 的最新链接再运行。" % e.code)
        raise SystemExit(1)
    except Exception as e:
        log("解析失败：%s" % e)
        raise SystemExit(1)
    log("UDF 分区定位：%.1fs（ISO %.2f GiB）" % (
        time.time() - t0, (remote.total or 0) / 1024 ** 3))

    t_udf = time.time()
    pmap = build_pmap(remote, vds, part_start, part_len)
    cf = ChunkedFile(remote)
    tree, clips = resolve_bdmv(cf, pmap, part_start, part_len)
    StreamProxy.remote = remote
    log("UDF 目录遍历：%s（%.1fs）" % ("定位到 BDMV" if tree else "无 BDMV", time.time() - t_udf))

    if not tree:
        ok = setup_fallback(args, remote, part_start, part_len)
        if ok:
            log_stats(remote, cf, t0, "就绪（裸扫描）")
        return ok

    t1 = time.time()
    log("已定位 BDMV 结构，正在并行解析播放列表…")
    pls = list_playlists(cf, pmap, part_start, part_len, tree)
    log("播放列表解析：%d 个有效，耗时 %.1fs" % (len(pls), time.time() - t1))
    if not pls:
        log("BDMV 中没有可解析的 .mpls，回退到裸扫描模式。")
        ok = setup_fallback(args, remote, part_start, part_len)
        if ok:
            log_stats(remote, cf, t0, "就绪（裸扫描回退）")
        return ok

    # 选择播放列表
    pick_name = None
    if args.playlist is not None:
        a = args.playlist.strip().lower()
        if a in {name for name, _ in pls}:
            pick_name = a
        elif a.isdigit() and int(a) < len(pls):
            pick_name = pls[int(a)][0]
        else:
            log("找不到播放列表：%s" % args.playlist)
            raise SystemExit(1)
    else:
        pick_name = pls[0][0]
    pl_map = dict(pls)
    print_playlist_table(pls, chosen=pick_name)
    if args.list:
        return False
    pl = pl_map[pick_name]
    log("")
    log("选定正片：%s.mpls，时长 %s，%d 段 PlayItem，%d 个章节标记" % (
        pick_name, hms(pl["duration90"] / TS90), len(pl["items"]),
        sum(1 for m in pl["marks"] if m[0] == 1)))
    t2 = time.time()
    log("正在按 EP map 裁剪拼接虚拟流…")
    vs = build_vstream(pick_name, pl, clips)
    log("虚拟流构建：%.2f GiB（物理分段 %d），耗时 %.1fs" % (
        vs.total / 1024 ** 3, len(vs.pieces), time.time() - t2))

    StreamProxy.streams = {pick_name: vs}
    StreamProxy.main = pick_name
    StreamProxy.catalog = [
        (name, p["duration90"] / TS90,
         sum(1 for m in p["marks"] if m[0] == 1), len(p["items"]))
        for name, p in pls]
    StreamProxy.builder = lambda name: (
        build_vstream(name, pl_map[name], clips) if name in pl_map else None)
    log_stats(remote, cf, t0, "就绪")
    return True


def main():
    ap = argparse.ArgumentParser(description="在线播放 Alist/115 上的 Blu-ray ISO")
    ap.add_argument("url", nargs="?", default=None,
                    help="Alist 直链 ISO 地址（带 sign）；--selftest 时可省略")
    ap.add_argument("--list", action="store_true", help="只解析并打印后退出")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--playlist", default=None,
                    help="播放列表选择：序号（按时长降序，0 起）或 mpls 编号（如 00001）")
    ap.add_argument("--index", type=int, default=-1,
                    help="（裸扫描回退模式）选择第几个大文件，默认体积最大者")
    ap.add_argument("--mpv", action="store_true", help="服务器就绪后自动调用 mpv.exe 播放")
    ap.add_argument("--log-file", default=None,
                    help="把日志实时同步写入文件（默认不写，排查问题时指定）")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检（无需网络）")
    args = ap.parse_args()

    if args.log_file:
        global _LOG_FH
        try:
            _LOG_FH = open(args.log_file, "w", encoding="utf-8", buffering=1)
        except OSError as e:
            log("无法写日志文件 %s：%s" % (args.log_file, e))

    if args.selftest:
        run_selftest()
        return
    if not args.url:
        ap.error("缺少 URL（或使用 --selftest）")

    # 先绑定端口再解析：解析期间请求得到 503，
    # 调用方（如 iso_bd_play.lua）可据此区分「解析中」与「进程已死」
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), StreamProxy)
    except OSError as e:
        log("端口 %d 监听失败：%s（试试 --port 换个端口）" % (args.port, e))
        raise SystemExit(1)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    remote = Remote(args.url)
    if not setup_streams(args, remote):
        httpd.shutdown()
        return

    StreamProxy.ready = True
    url = "http://127.0.0.1:%d/movie.m2ts" % args.port
    n_ch = len(StreamProxy.streams[StreamProxy.main].meta["chapters"])
    log("")
    log("代理已就绪：%s（%d 个章节，mpv 中按 Ctrl+M 打开章节菜单）" % (url, n_ch))
    log("播放命令：")
    log("    D:/mpv-lazy-full/mpv.exe %s" % url)
    log("注意：sign 过期后请求会 401，届时重新复制 Alist 最新链接并重启本脚本。")

    if args.mpv:
        mpv_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpv.exe")
        if os.path.exists(mpv_exe):
            subprocess.Popen([mpv_exe, url], cwd=os.path.dirname(mpv_exe))
        else:
            log("未找到 mpv.exe，跳过自动播放")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("已停止")


# ==================== 自检（合成数据，无需网络） ====================

def _mk_clpi(num_packets, eps):
    """构造合成 CLPI：eps = [(pts45, spn)]，全部属于视频 PID 0x1011。"""
    cpi_addr = 300
    data = bytearray(8192)
    data[0:4] = b"HDMV"
    data[4:8] = b"0200"
    struct.pack_into(">I", data, 8, 200)      # SequenceInfo
    struct.pack_into(">I", data, 16, cpi_addr)
    struct.pack_into(">I", data, 56, num_packets)
    # SequenceInfo @200：长度 1 + n_atc=0
    struct.pack_into(">I", data, 200, 1)
    data[205] = 0

    n = len(eps)
    # CPI @cpi_addr：len u32；byte+5 = type 1；ep_map_pos = cpi_addr+6
    # @+6 保留；@+7 num_pid；@+8 起 entry（10 字节）；随后 stream 表
    data[cpi_addr + 5] = 0x01
    data[cpi_addr + 7] = 1
    e = cpi_addr + 8
    struct.pack_into(">H", data, e, 0x1011)
    # entry+2 起 48 位：reserved(10) type(4) n_coarse(16) n_fine(18)
    bits = (1 << 34) | (n << 18) | n
    data[e + 2:e + 8] = bits.to_bytes(6, "big")
    # stream 表须在 entry 区（cpi_addr+8 起 12 字节）之后，相对 ep_map_pos(=cpi_addr+6)
    stream_rel = 16
    struct.pack_into(">I", data, e + 8, stream_rel)
    sp = cpi_addr + 6 + stream_rel
    coarse_off = sp + 4
    fine_off = coarse_off + n * 8
    struct.pack_into(">I", data, sp, fine_off - sp)
    for i, (pts, spn) in enumerate(eps):
        c_pts = (pts >> 19) << 1          # 低 13 位值 + 1 位标志
        c_spn = spn & ~0x1FFFF
        v = (i << 46) | (c_pts << 32) | c_spn
        data[coarse_off + i * 8: coarse_off + i * 8 + 8] = v.to_bytes(8, "big")
        f_pts = (pts >> 8) & 0x7FF
        f_spn = spn & 0x1FFFF
        fv = (f_pts << 17) | f_spn
        data[fine_off + i * 4: fine_off + i * 4 + 4] = fv.to_bytes(4, "big")
    cpi_len = (fine_off + n * 4) - cpi_addr - 4
    struct.pack_into(">I", data, cpi_addr, cpi_len)
    return bytes(data[: fine_off + n * 4])


def _mk_mpls(items, marks):
    """构造合成 MPLS。items = [(clip_id, in90, out90, stc)]，marks = [(type, ref, ts90)]。"""
    item_size = 2 + 20
    list_pos = 40
    pl_len = 6 + item_size * len(items)
    mark_pos = list_pos + 4 + pl_len
    buf = bytearray(mark_pos + 6 + 14 * len(marks) + 8)
    buf[0:4] = b"MPLS"
    buf[4:8] = b"0200"
    struct.pack_into(">I", buf, 8, list_pos)
    struct.pack_into(">I", buf, 12, mark_pos)
    struct.pack_into(">I", buf, list_pos, pl_len)
    struct.pack_into(">H", buf, list_pos + 6, len(items))
    o = list_pos + 10
    for clip, i90, o90, stc in items:
        struct.pack_into(">H", buf, o, 20)
        buf[o + 2:o + 7] = clip.encode("ascii")
        buf[o + 7:o + 11] = b"M2TS"
        buf[o + 13] = stc
        struct.pack_into(">I", buf, o + 14, i90)
        struct.pack_into(">I", buf, o + 18, o90)
        o += item_size
    struct.pack_into(">I", buf, mark_pos, 2 + 14 * len(marks))
    struct.pack_into(">H", buf, mark_pos + 4, len(marks))
    o = mark_pos + 6
    for t, ref, ts in marks:
        buf[o + 1] = t
        struct.pack_into(">H", buf, o + 2, ref)
        struct.pack_into(">I", buf, o + 4, ts)
        o += 14
    return bytes(buf[:o])


def _mk_data(size, seed):
    base = bytes((b + seed) & 0xFF for b in range(256))
    return (base * (size // 256 + 2))[:size]


class FakeRemote:
    """内存 ISO，模拟 Remote 接口（read/open/probe_total）。"""

    def __init__(self, data):
        self.data = data
        self.total = len(data)

    def probe_total(self):
        pass

    def read(self, offset, length):
        return self.data[offset:offset + length]

    class _Resp:
        def __init__(self, data):
            import io
            self.f = io.BytesIO(data)

        def read(self, n):
            return self.f.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open(self, offset, length):
        return self._Resp(self.data[offset:offset + length])


def _mk_iso(mpls, clpi1, clpi2, clip1, clip2, meta_partition=False):
    """构造合成 UDF 2.50 镜像，含 BDMV 目录树。

    meta_partition=True 时复刻蓝光盘常见布局：全部元数据（FSD/目录/FE/mpls/clpi）
    放进一个「元数据文件」区域，LVD 挂 *UDF Metadata Partition 分区映射，
    FID/ICB 的分区号指向元数据分区——检验映射器是否正确换算物理位置。"""
    part_start = 32
    meta = meta_partition
    # 主分区相对布局
    lba_vds = 0                     # VDS 3 扇区
    lba_meta_fe = 4                 # 元数据文件的 FE（MetadataFileLocation）
    lba_m2ts1_fe = 6
    lba_m2ts2_fe = 8
    meta_lba = 12                   # 元数据文件数据区起点（物理）
    # 元数据空间内的布局（meta=True 时所有目录树都在这里）
    m = {}
    cur = 0
    if meta:
        m["fsd"] = cur; cur += 1
        m["root_fe"] = cur; cur += 1
        m["root_data"] = cur; cur += 1
        m["bdmv_fe"] = cur; cur += 1
        m["bdmv_data"] = cur; cur += 1
        m["pl_fe"] = cur; cur += 1
        m["pl_data"] = cur; cur += 1
        m["mpls_fe"] = cur; cur += 1
        m["mpls_data"] = cur; cur += 1
        m["ci_fe"] = cur; cur += 1
        m["ci_data"] = cur; cur += 1
        m["clpi1_fe"] = cur; cur += 1
        m["clpi1_data"] = cur; cur += 1
        m["clpi2_fe"] = cur; cur += 1
        m["clpi2_data"] = cur; cur += 1
        m["st_fe"] = cur; cur += 1
        m["st_data"] = cur; cur += 1
        m["m2ts1_fe"] = cur; cur += 1
        m["m2ts2_fe"] = cur; cur += 1
        meta_size = cur * SECTOR
    lba1 = 300                      # clip1 数据（主分区，避开保留区/AVDP/元数据区）
    lba2 = lba1 + len(clip1) // SECTOR + 1
    size_lba = max(lba2 + len(clip2) // SECTOR + 2,
                   (meta_lba + meta_size // SECTOR + 2) if meta else 0)
    iso = bytearray((part_start + size_lba) * SECTOR)

    def put_main(lba, data, at=0):
        off = (part_start + lba) * SECTOR + at
        iso[off:off + len(data)] = data

    def put_meta(lba, data, at=0):
        put_main(meta_lba + lba, data, at)

    # 元数据分区时 P = (1, lba)（经映射），否则 (0, lba)
    P = (lambda l: (1, l)) if meta else (lambda l: (0, l))

    def set_tag_checksum(tag_block):
        tag_block[4] = (sum(tag_block[0:4]) + sum(tag_block[5:16])) & 0xFF

    def mk_fe(tag, ftype, info_len, ads):
        b = bytearray(SECTOR)
        struct.pack_into("<H", b, 0, tag)
        b[27] = ftype
        struct.pack_into("<H", b, 34, 1)      # long_ad
        struct.pack_into("<Q", b, 56, info_len)
        struct.pack_into("<I", b, 208, 0)     # EFE lengthOfEA
        struct.pack_into("<I", b, 212, 16 * len(ads))
        o = 216
        for part, eloc, elen in ads:
            struct.pack_into("<I", b, o, elen)
            struct.pack_into("<I", b, o + 4, eloc)
            struct.pack_into("<H", b, o + 8, part)
            o += 16
        return bytes(b)

    def mk_dir_data(fids):
        d = bytearray()

        def fid(name, icb, parent=False):
            part, lba = icb
            raw = b"\x08" + name.encode("ascii")
            e = bytearray(38)
            struct.pack_into("<H", e, 0, TAG_FID)
            e[18] = 0x08 if parent else 0
            e[19] = len(raw)  # L_FI 含压缩字节
            struct.pack_into("<I", e, 20, SECTOR)
            struct.pack_into("<I", e, 24, lba)
            struct.pack_into("<H", e, 28, part)
            struct.pack_into("<H", e, 36, 0)
            d.extend(e)
            d.extend(raw)
            while len(d) % 4:
                d.append(0)

        fid("", (1 if meta else 0, 2), parent=True)
        for name, icb in fids:
            fid(name, icb)
        return bytes(d)

    # VDS：分区描述 @0、LVD @1、终止符 @2；AVDP @绝对 256
    put_main(0, struct.pack("<HHBBBBHHI", TAG_PARTITION, 3, 0, 0, 1, 0, 0, 0, 0), 0)
    put_main(0, struct.pack("<II", part_start, size_lba), 188)
    lvd = bytearray(SECTOR)
    struct.pack_into("<HHBBBBHHI", lvd, 0, TAG_LOGICAL_VOL, 3, 0, 0, 1, 0, 0, 0, 0)
    struct.pack_into("<I", lvd, 212, SECTOR)
    if meta:
        # 分区映射 @440（ECMA-167 3/10.6）：type1 主分区 0 + type2 元数据分区 1
        pm1 = struct.pack("<BBHH", 1, 6, 1, 0)   # type, length, volSeqNum, partitionNum
        pm2 = bytearray(64)
        pm2[0] = 2                               # partitionMapType
        pm2[1] = 64                              # partitionMapLength
        pm2[4] = 0                               # regid flags
        pm2[5:28] = b"*UDF Metadata Partition"   # regid ident[23]
        struct.pack_into("<H", pm2, 36, 1)          # volSeqNum
        struct.pack_into("<H", pm2, 38, 0)          # 物理分区号（映射序号由表内位置决定）
        struct.pack_into("<I", pm2, 40, lba_meta_fe)  # MetadataFileLocation
        struct.pack_into("<I", pm2, 44, lba_meta_fe)  # MirrorFileLocation
        struct.pack_into("<I", lvd, 264, len(pm1) + len(pm2))
        struct.pack_into("<I", lvd, 268, 2)
        lvd[440:440 + len(pm1)] = pm1
        lvd[440 + len(pm1):440 + len(pm1) + len(pm2)] = bytes(pm2)
    put_main(1, bytes(lvd))
    put_main(2, struct.pack("<HHBBBBHHI", TAG_TERMINATING, 3, 0, 0, 1, 0, 0, 0, 0), 0)
    avdp = bytearray(SECTOR)
    struct.pack_into("<H", avdp, 0, TAG_AVDP)
    struct.pack_into("<I", avdp, 16, 3 * SECTOR)  # VDS 长度（字节）：3 个 2048B 扇区
    struct.pack_into("<I", avdp, 20, part_start)  # VDS 位于分区起始（绝对 LBA）
    iso[256 * SECTOR:257 * SECTOR] = avdp  # AVDP 固定在绝对扇区 256

    if meta:
        # 元数据文件的 FE（主分区 @4），extent 指向主分区的元数据数据区
        put_main(lba_meta_fe, mk_fe(TAG_EFE, 5, meta_size,
                                    [(0, meta_lba, meta_size)]))
        # 元数据空间：FSD @0，DirectoryICB 指向元数据分区里的根目录 EFE
        fsd = bytearray(SECTOR)
        struct.pack_into("<H", fsd, 0, TAG_FSD)
        struct.pack_into("<I", fsd, 400, SECTOR)      # rootDirectoryICB（ECMA-167 4/14.1）
        struct.pack_into("<I", fsd, 404, m["root_fe"])
        struct.pack_into("<H", fsd, 408, 1)           # 分区号 = 元数据分区
        set_tag_checksum(fsd)
        put_meta(0, bytes(fsd))
        put_meta(m["root_fe"], mk_fe(TAG_EFE, 4, SECTOR, [P(m["root_data"]) + (SECTOR,)]))
        put_meta(m["root_data"], mk_dir_data([("BDMV", P(m["bdmv_fe"]))]))
        put_meta(m["bdmv_fe"], mk_fe(TAG_EFE, 4, SECTOR, [P(m["bdmv_data"]) + (SECTOR,)]))
        put_meta(m["bdmv_data"], mk_dir_data([
            ("PLAYLIST", P(m["pl_fe"])), ("CLIPINF", P(m["ci_fe"])),
            ("STREAM", P(m["st_fe"]))]))
        put_meta(m["pl_fe"], mk_fe(TAG_EFE, 4, SECTOR, [P(m["pl_data"]) + (SECTOR,)]))
        put_meta(m["pl_data"], mk_dir_data([("00001.mpls", P(m["mpls_fe"]))]))
        put_meta(m["mpls_fe"], mk_fe(TAG_EFE, 5, len(mpls),
                                     [P(m["mpls_data"]) + (len(mpls),)]))
        put_meta(m["mpls_data"], mpls)
        put_meta(m["ci_fe"], mk_fe(TAG_EFE, 4, SECTOR, [P(m["ci_data"]) + (SECTOR,)]))
        put_meta(m["ci_data"], mk_dir_data([("00001.clpi", P(m["clpi1_fe"])),
                                            ("00002.clpi", P(m["clpi2_fe"]))]))
        put_meta(m["clpi1_fe"], mk_fe(TAG_EFE, 5, len(clpi1),
                                      [P(m["clpi1_data"]) + (len(clpi1),)]))
        put_meta(m["clpi1_data"], clpi1)
        put_meta(m["clpi2_fe"], mk_fe(TAG_EFE, 5, len(clpi2),
                                      [P(m["clpi2_data"]) + (len(clpi2),)]))
        put_meta(m["clpi2_data"], clpi2)
        put_meta(m["st_fe"], mk_fe(TAG_EFE, 4, SECTOR, [P(m["st_data"]) + (SECTOR,)]))
        put_meta(m["st_data"], mk_dir_data([("00001.m2ts", P(m["m2ts1_fe"])),
                                            ("00002.m2ts", P(m["m2ts2_fe"]))]))
        # m2ts 的 FE 在元数据空间，数据 extent 指回主分区
        put_meta(m["m2ts1_fe"], mk_fe(TAG_EFE, 5, len(clip1),
                                      [(0, lba1, len(clip1))]))
        put_meta(m["m2ts2_fe"], mk_fe(TAG_EFE, 5, len(clip2),
                                      [(0, lba2, len(clip2))]))
        put_main(lba1, clip1)
        put_main(lba2, clip2)
    else:
        # 原布局：FSD @4，目录树在主分区 6-40
        fsd = bytearray(SECTOR)
        struct.pack_into("<H", fsd, 0, TAG_FSD)
        struct.pack_into("<I", fsd, 400, SECTOR)      # rootDirectoryICB（ECMA-167 4/14.1）
        struct.pack_into("<I", fsd, 404, 6)
        struct.pack_into("<H", fsd, 408, 0)
        set_tag_checksum(fsd)
        put_main(4, bytes(fsd))
        put_main(6, mk_fe(TAG_EFE, 4, SECTOR, [(0, 8, SECTOR)]))
        put_main(8, mk_dir_data([("BDMV", (0, 10))]))
        put_main(10, mk_fe(TAG_EFE, 4, SECTOR, [(0, 12, SECTOR)]))
        put_main(12, mk_dir_data([("PLAYLIST", (0, 14)), ("CLIPINF", (0, 22)),
                                  ("STREAM", (0, 34))]))
        put_main(14, mk_fe(TAG_EFE, 4, SECTOR, [(0, 16, SECTOR)]))
        put_main(16, mk_dir_data([("00001.mpls", (0, 18))]))
        put_main(18, mk_fe(TAG_EFE, 5, len(mpls), [(0, 20, len(mpls))]))
        put_main(20, mpls)
        put_main(22, mk_fe(TAG_EFE, 4, SECTOR, [(0, 24, SECTOR)]))
        put_main(24, mk_dir_data([("00001.clpi", (0, 26)), ("00002.clpi", (0, 30))]))
        put_main(26, mk_fe(TAG_EFE, 5, len(clpi1), [(0, 28, len(clpi1))]))
        put_main(28, clpi1)
        put_main(30, mk_fe(TAG_EFE, 5, len(clpi2), [(0, 32, len(clpi2))]))
        put_main(32, clpi2)
        put_main(34, mk_fe(TAG_EFE, 4, SECTOR, [(0, 36, SECTOR)]))
        put_main(36, mk_dir_data([("00001.m2ts", (0, 38)), ("00002.m2ts", (0, 40))]))
        put_main(38, mk_fe(TAG_EFE, 5, len(clip1), [(0, lba1, len(clip1))]))
        put_main(lba1, clip1)
        put_main(40, mk_fe(TAG_EFE, 5, len(clip2), [(0, lba2, len(clip2))]))
        put_main(lba2, clip2)
    return bytes(iso), part_start


def run_selftest():
    """合成数据自检：MPLS/CLPI 解析、EP map 裁剪查找、UDF 目录遍历、HTTP 端到端。"""
    # ---- MPLS ----
    items = [("00001", 90000, 901120, 0), ("00002", 0, 2700000, 0)]
    marks = [(1, 0, 540000), (1, 1, 900000), (2, 0, 0)]
    mpls = _mk_mpls(items, marks)
    pl = parse_mpls(mpls)
    assert pl["items"] == items, pl["items"]
    assert pl["duration90"] == 811120 + 2700000
    assert pl["marks"] == marks
    print("MPLS 解析 ✔（2 PlayItem / 90 秒 / 3 标记）")

    # ---- CLPI + EP map 查找 ----
    eps = [(45056, 100), (450560, 1000), (900608, 2000)]
    clpi1 = _mk_clpi(5000, eps)
    info = parse_clpi(clpi1)
    assert info["num_packets"] == 5000
    assert info["eps"] == eps, info["eps"]
    assert spn_before(info["eps"], 45000) == 0
    assert spn_before(info["eps"], 450560) == 1000
    assert spn_before(info["eps"], 900608) == 2000
    assert spn_after(info["eps"], 450559) == 1000
    assert spn_after(info["eps"], 450560) == 2000
    assert spn_after(info["eps"], 10 ** 9) is None
    clpi2 = _mk_clpi(2700, [])
    assert parse_clpi(clpi2)["eps"] == []
    print("CLPI / EP map ✔（PTS↔SPN 往返一致）")

    # ---- UDF 目录遍历 + 虚拟流拼装（普通布局 + 元数据分区布局） ----
    clip1 = _mk_data(5000 * PKT, 7)     # 960000 字节
    clip2 = _mk_data(2700 * PKT, 23)    # 518400 字节

    def full_walk(iso_bytes, expect_meta):
        remote = FakeRemote(iso_bytes)
        _ps, part_len, _bs, vds = get_partition(remote)
        assert _ps == 32
        pmap = build_pmap(remote, vds, _ps, part_len)
        assert (pmap.meta_num == 1) == expect_meta, pmap.meta_num
        cf = ChunkedFile(remote)
        tree, clips = resolve_bdmv(cf, pmap, _ps, part_len)
        assert tree is not None, "UDF 目录遍历失败"
        assert "00001.mpls" in tree["playlist"]
        assert "00001.m2ts" in tree["stream"] and "00002.m2ts" in tree["stream"]
        return cf, pmap, clips, tree, part_len, _ps, remote

    # 期望：item0 经 EP 裁剪为 [0, 2000*PKT)，item2 无 EP map 整段保留
    eps = [(45056, 100), (450560, 1000), (900608, 2000)]
    mpls2 = _mk_mpls([("00001", 90000, 901120, 0), ("00002", 0, 2700000, 0)],
                     [(1, 0, 540000), (1, 1, 900000), (2, 0, 0)])
    expected = clip1[:2000 * PKT] + clip2
    ch_expected = [5.0, round(1711120 / TS90, 3)]

    for with_meta in (False, True):
        iso, _ps = _mk_iso(mpls2, _mk_clpi(5000, eps), _mk_clpi(2700, []),
                           clip1, clip2, meta_partition=with_meta)
        cf, pmap, clips, tree, part_len, _ps, remote = full_walk(iso, with_meta)
        pls = list_playlists(cf, pmap, _ps, part_len, tree)
        assert pls and pls[0][0] == "00001"
        pl = pls[0][1]
        assert pl["items"][0][0] == "00001" and pl["duration90"] == 811120 + 2700000
        vs = build_vstream("00001", pl, clips)
        assert vs.total == len(expected), (vs.total, len(expected))
        ch = vs.meta["chapters"]
        assert [c["t"] for c in ch] == ch_expected, ch
        print("UDF 目录遍历 + 虚拟流拼装 ✔（%s布局，%.2f MiB，%d 分段，%d 章）"
              % ("元数据分区" if with_meta else "普通",
                 vs.total / 1024 ** 2, len(vs.pieces), len(ch)))

    # ---- HTTP 端到端（含跨段 Range） ----
    StreamProxy.streams = {"00001": vs}
    StreamProxy.main = "00001"
    StreamProxy.remote = remote
    StreamProxy.builder = None
    StreamProxy.ready = True
    StreamProxy.catalog = [("00001", pl["duration90"] / TS90, 2, 2)]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StreamProxy)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        base = "http://127.0.0.1:%d" % port
        req = urllib.request.Request(
            base + "/bd/00001.m2ts", headers={"Range": "bytes=383900-384100"})
        with urllib.request.urlopen(req, timeout=10) as r:
            got = r.read()
        assert got == expected[383900:384101], "跨段 Range 数据不一致"
        with urllib.request.urlopen(base + "/movie.json", timeout=10) as r:
            meta = json.loads(r.read())
        assert meta["chapters"][0]["t"] == 5.0
        print("HTTP 服务 ✔（Range 跨段拼接与章节元数据均正确）")
    finally:
        httpd.shutdown()

    print("\n全部自检通过 ✔")


if __name__ == "__main__":
    main()
