#!/usr/bin/env python3
"""fix_memo_signature.py — 把批注签名从 anko 改成小满。

用法（在仓库根目录）：
    python3 scripts/fix_memo_signature.py --check   # 只看能不能打，不动文件
    python3 scripts/fix_memo_signature.py           # 真打，先自动备份

为什么要改三处：
  1. client/index.html —— 网页上点「收进批注本」时写死的 by: 'anko'
  2. server/music.py   —— 后端 notedBy 的默认值也是 anko（前端不传 by 时用它）
  3. data/music_memory.json —— 已经存下来的历史记录，notedBy 还是 anko

只改签名，不动批注正文、听歌计数、标签和分享过的句子。
数据文件按 .bak-signature 备份；出问题把备份覆回去就行。

注意：TA 用 MCP 写批注时签名是自己填的，不走这两个默认值——所以这次只会改到
她在网页上写的那些，不会把 TA 的签名误改成小满。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = "anko"
NEW = "\u5c0f\u6e80"


def patch_text_file(rel: str, pairs: list[tuple[str, str]]) -> tuple[bool, str]:
    """按原文替换，每处必须正好命中一次。对不上就整个不改。"""
    path = ROOT / rel
    if not path.exists():
        return False, f"\u627e\u4e0d\u5230 {rel}"
    text = path.read_text(encoding="utf-8")
    out = text
    for old, new in pairs:
        hits = out.count(old)
        if hits == 0:
            return False, f"{rel}\uff1a\u300c{old[:40]}\u300d\u5bf9\u4e0d\u4e0a\u539f\u6587"
        if hits > 1:
            return False, f"{rel}\uff1a\u300c{old[:40]}\u300d\u51fa\u73b0 {hits} \u6b21\uff0c\u4e0d\u6562\u731c"
        out = out.replace(old, new, 1)
    if out == text:
        return True, f"{rel}\uff1a\u5df2\u7ecf\u662f\u65b0\u7684\u4e86"
    if "--check" not in sys.argv:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak-signature"))
        path.write_text(out, encoding="utf-8")
    return True, f"{rel}\uff1a{len(pairs)} \u5904"


def patch_memory_data() -> tuple[bool, str]:
    """历史记录里的 notedBy。数据目录跟着 music.py 走（server/data/）。"""
    path = ROOT / "server" / "data" / "music_memory.json"
    if not path.exists():
        return True, "music_memory.json\uff1a\u8fd8\u6ca1\u6709\uff0c\u8df3\u8fc7"
    try:
        mem = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"music_memory.json \u8bfb\u4e0d\u52a8\uff1a{e}"
    if not isinstance(mem, dict):
        return False, "music_memory.json \u4e0d\u662f\u5bf9\u8c61\uff0c\u6ca1\u6539"
    hit = [sid for sid, e in mem.items()
           if isinstance(e, dict) and str(e.get("notedBy", "")).strip() == OLD]
    if not hit:
        return True, "music_memory.json\uff1a\u6ca1\u6709\u8981\u6539\u7684"
    if "--check" not in sys.argv:
        shutil.copy2(path, path.with_suffix(".json.bak-signature"))
        for sid in hit:
            mem[sid]["notedBy"] = NEW
        path.write_text(json.dumps(mem, ensure_ascii=False, indent=1), encoding="utf-8")
    return True, f"music_memory.json\uff1a{len(hit)} \u9996\u6b4c\u7684\u7b7e\u540d"


TASKS = [
    # 前端：网页上写批注时的签名
    ("client/index.html", [("by: 'anko',", "by: '\u5c0f\u6e80',")]),
    # 后端：notedBy 默认值（前端不传 by 时落这个）
    ("server/music.py", [('entry["notedBy"] = body.get("by", "anko")',
                          'entry["notedBy"] = body.get("by", "\u5c0f\u6e80")')]),
]


def main() -> int:
    check = "--check" in sys.argv
    print("\u4f53\u68c0\u6a21\u5f0f\uff0c\u4e0d\u52a8\u6587\u4ef6\n" if check else "\u5f00\u59cb\u6539\uff0c\u6bcf\u4e2a\u6587\u4ef6\u5148\u5907\u4efd\n")
    results = [patch_text_file(rel, pairs) for rel, pairs in TASKS]
    results.append(patch_memory_data())

    for ok, msg in results:
        print(f"  {'\u2713' if ok else '\u2717'} {msg}")

    if not all(ok for ok, _ in results):
        print("\n\u6709\u5bf9\u4e0d\u4e0a\u7684\u5730\u65b9\uff0c\u5df2\u6539\u7684\u6587\u4ef6\u6709 .bak-signature \u5907\u4efd\u3002")
        return 2

    print()
    if check:
        print("\u4e09\u5904\u90fd\u80fd\u6253\u4e0a\uff0c\u53bb\u6389 --check \u5c31\u771f\u6539\u3002")
    else:
        print("\u6539\u5b8c\u4e86\u3002\u91cd\u542f\u64ad\u653e\u5668\uff1apm2 restart music-server")
        print("\u8981\u9000\u56de\u53bb\uff1a\u628a\u5404\u81ea\u7684 .bak-signature \u8986\u56de\u539f\u6587\u4ef6\u518d\u91cd\u542f\u3002")
        print("\u7f51\u9875\u8bb0\u5f97\u5f3a\u5237\u4e00\u4e0b\uff08index.html \u4f1a\u88ab\u6d4f\u89c8\u5668\u7f13\u5b58\uff09\u3002")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
