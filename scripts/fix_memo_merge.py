#!/usr/bin/env python3
r"""fix_memo_merge.py — 让同一首歌的不同版本共用一本批注。

用法（在仓库根目录）：
    python3 scripts/fix_memo_merge.py --inspect   # 先看哪几首歌裂成了多份，不改代码
    python3 scripts/fix_memo_merge.py --check     # 只看两处能不能打，不动文件
    python3 scripts/fix_memo_merge.py             # 真打，先自动备份

病根：
  批注是按 song_id 存的，而网易云同一首歌的单曲版/专辑版/live 各有自己的 id。
  实例（现场抓到的）：
      无条件 — 陈奕迅  473194652   5 行
      无条件 — 陈奕迅  31426608    1 行
  AI 用 query 写批注时取的是搜索第一条，而人在播放器里听的可能是另一个版本，
  于是打开 ✎ 面板只看到自己那一本，以为新写的丢了。
  不是覆盖，是两个人在看两本不同的册子。

改法：
  **只改读取，不改写入、不迁数据。**
  GET /music/memory?id=<sid> 时，把「同一首歌」的各版本合在一起返回：
    - notes 按落笔时间拼起来（去重）
    - favoriteLines / tags 并集
    - listenCount / togetherCount 加总，firstListened 取最早、lastListened 取最近
    - notedBy / notedAt 取最近落笔的那一笔
  写入照旧写自己的 id（哪个版本被写就是哪个，数据保持真实），
  只在读的那一刻合并 —— 所以 MCP 和网页都不用改，一处搮定。

  归并规则：歌名去掉括号后缀和「 - xxx」尾巴后相同，且歌手集合有交集。
  （和 mcp/music_mcp.py 里 _base_name / _artists 一个口径）

出问题：cp server/music.py.bak-merge server/music.py && pm2 restart music-server
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "server" / "music.py"
BACKUP = ROOT / "server" / "music.py.bak-merge"


# ══ 诊断：哪几首歌裂成了多份 ══

def _base_name(name: str) -> str:
    n = re.sub(r"\s*[(（\[【][^)）\]】]*[)）\]】]", " ", name or "")
    n = re.sub(r"\s+-\s+.*$", "", n)
    return n.strip().lower()


def _artists(a: str) -> set:
    return {x.strip().lower() for x in re.split(r"[,，/、&]", a or "") if x.strip()}


def inspect() -> int:
    path = ROOT / "server" / "data" / "music_memory.json"
    if not path.exists():
        print(f"找不到 {path}")
        print("（数据文件不进 git，要在跑播放器那台机器上跑）")
        return 1
    try:
        mem = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读不动：{e}")
        return 1

    def has_content(m):
        return bool(str(m.get("notes") or "").strip()
                    or m.get("favoriteLines") or m.get("feeling") or m.get("tags"))

    groups: dict[tuple, list] = {}
    for sid, m in mem.items():
        if not isinstance(m, dict):
            continue
        key = _base_name(m.get("name") or "")
        if not key:
            continue
        groups.setdefault((key,), []).append((sid, m))

    split = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        # 歌手要有交集才算同一首
        written = [r for r in rows if has_content(r[1])]
        if len(written) >= 2:
            split.append((key[0], written))
        elif len(written) == 1 and len(rows) > 1:
            split.append((key[0], rows))

    if not split:
        print("没有发现版本裂开的歌。")
        return 0

    print(f"找到 {len(split)} 首歌在批注本里有多个 song_id：\n")
    for name, rows in split:
        total_lines = 0
        print(f"· {rows[0][1].get('name')} — {rows[0][1].get('artist')}")
        for sid, m in rows:
            notes = str(m.get("notes") or "").strip()
            lines = len([l for l in notes.split("\n") if l.strip()]) if notes else 0
            total_lines += lines
            favs = len(m.get("favoriteLines") or [])
            bits = []
            if lines:
                bits.append(f"批注 {lines} 行")
            if favs:
                bits.append(f"句子 {favs} 条")
            if m.get("listenCount"):
                bits.append(f"听过 {m['listenCount']} 次")
            who = f"，{m.get('notedBy')} {(m.get('notedAt') or '')[:10]}" if m.get("notedBy") else ""
            print(f"    {sid}：{'、'.join(bits) or '（没写过）'}{who}")
        if total_lines:
            print(f"    合并后应该是 {total_lines} 行")
        print()
    print("打上补丁后，这几首无论从哪个版本打开，看到的都是合并后的全部。")
    return 0


# ══ 补丁 ══

HELPER = r'''    # ── 批注归并：同一首歌的不同版本 id 共用一本 ──
    # 网易云同一首歌的单曲版/专辑版/live 各有自己的 song_id，而批注是按 id 存的。
    # AI 用 query 写批注时取搜索第一条，人在播放器里听的可能是另一个版本，
    # 于是同一首歌的批注裂成两本，打开 ✎ 只看到其中一本（现场抓到过：
    # 《无条件》473194652 有 5 行、31426608 只有 1 行）。
    # 这里只在读的时候合并；写入照旧写自己的 id，数据保持真实，不迁、不改写。

    @staticmethod
    def _memo_base_name(name: str) -> str:
        n = re.sub(r"\s*[(（\[【][^)）\]】]*[)）\]】]", " ", name or "")
        n = re.sub(r"\s+-\s+.*$", "", n)
        return n.strip().lower()

    @staticmethod
    def _memo_artists(a: str) -> set:
        return {x.strip().lower() for x in re.split(r"[,，/、&]", a or "") if x.strip()}

    def _memo_same_song(self, a: dict, b: dict) -> bool:
        """歌名去掉括号后缀和「 - xxx」尾巴后相同，且歌手有交集。"""
        na, nb = self._memo_base_name(a.get("name")), self._memo_base_name(b.get("name"))
        if not na or na != nb:
            return False
        aa, ab = self._memo_artists(a.get("artist")), self._memo_artists(b.get("artist"))
        # 两边都有歌手信息时要有交集；有一边空就只按歌名认
        return (not aa or not ab) or bool(aa & ab)

    def _memo_merged(self, mem: dict, song_id: str) -> dict | None:
        """把同一首歌各版本的批注合成一份返回。没任何记录就返回 None。"""
        sid = str(song_id)
        seed = mem.get(sid)
        # 本版本没记录时，借同名版本的元数据来认亲（允许只带 id 来问）
        probe = seed
        if probe is None:
            self_name = ""
            for other in mem.values():
                if isinstance(other, dict) and str(other.get("songId")) == sid:
                    probe = other
                    break
            if probe is None:
                return None
        group = []
        for other_sid, other in mem.items():
            if not isinstance(other, dict):
                continue
            if other_sid == sid or self._memo_same_song(probe, other):
                group.append((other_sid, other))
        if not group:
            return None
        if len(group) == 1 and seed is not None:
            return seed

        # 落笔时间排序：早的在前，读起来像一本连续的本子
        group.sort(key=lambda kv: str(kv[1].get("notedAt") or kv[1].get("firstListened") or ""))

        out = dict(seed) if seed else dict(group[0][1])
        out["songId"] = int(sid) if sid.isdigit() else sid

        note_lines, seen_lines = [], set()
        favs, seen_favs = [], set()
        tags, seen_tags = [], set()
        listen = together = 0
        first = last = noted_at = None
        noted_by = ""
        feeling = ""
        for _, m in group:
            for line in str(m.get("notes") or "").split("\n"):
                t = line.strip()
                if t and t not in seen_lines:
                    seen_lines.add(t)
                    note_lines.append(t)
            for f in (m.get("favoriteLines") or []):
                if isinstance(f, str) and f not in seen_favs:
                    seen_favs.add(f)
                    favs.append(f)
            for t in (m.get("tags") or []):
                if t not in seen_tags:
                    seen_tags.add(t)
                    tags.append(t)
            listen += int(m.get("listenCount") or 0)
            together += int(m.get("togetherCount") or 0)
            fl, ll = m.get("firstListened"), m.get("lastListened")
            if fl and (first is None or fl < first):
                first = fl
            if ll and (last is None or ll > last):
                last = ll
            na = m.get("notedAt")
            if na and (noted_at is None or na > noted_at):
                noted_at = na
                noted_by = m.get("notedBy") or noted_by
            if m.get("feeling") and not feeling:
                feeling = m["feeling"]
            if not out.get("name") and m.get("name"):
                out["name"] = m["name"]
            if not out.get("artist") and m.get("artist"):
                out["artist"] = m["artist"]

        out["notes"] = "\n".join(note_lines)
        out["favoriteLines"] = favs
        out["tags"] = tags
        out["listenCount"] = listen
        out["togetherCount"] = together
        if first:
            out["firstListened"] = first
        if last:
            out["lastListened"] = last
        if noted_at:
            out["notedAt"] = noted_at
        if noted_by:
            out["notedBy"] = noted_by
        if feeling:
            out["feeling"] = feeling
        if len(group) > 1:
            # 让调用方知道这是合并视图，并且原本在哪几个 id 上
            out["mergedFrom"] = [k for k, _ in group]
        return out

'''

PATCHES = [
    (
        "加归并辅助方法",
        '''    def _handle_music_memory_get(self):
''',
        HELPER + '''    def _handle_music_memory_get(self):
''',
    ),
    (
        "读批注时走归并",
        '''        if song_id:
            entry = mem.get(str(song_id))
            self._send_json(200, {"ok": True, "memory": entry})
''',
        '''        if song_id:
            # 合并同一首歌的各版本（见 _memo_merged）：不管从哪个 id 打开，都看到全部
            entry = self._memo_merged(mem, str(song_id))
            self._send_json(200, {"ok": True, "memory": entry})
''',
    ),
]


def main() -> int:
    if "--inspect" in sys.argv:
        return inspect()

    if not TARGET.exists():
        print(f"找不到 {TARGET}，请在仓库根目录跑。")
        return 1

    text = TARGET.read_text(encoding="utf-8")
    out = text
    done = []
    for name, old, new in PATCHES:
        if new in out:
            done.append(f"· {name}（已经打过了）")
            continue
        hits = out.count(old)
        if hits == 0:
            print(f"✗ 「{name}」对不上原文，整个文件没改。")
            print("  先看一眼现在长什么样：")
            print("      grep -n '_handle_music_memory_get' -A 10 server/music.py")
            return 2
        if hits > 1:
            print(f"✗ 「{name}」出现 {hits} 次，不敢猜。整个文件没改。")
            return 2
        out = out.replace(old, new, 1)
        done.append(f"· {name}")

    if "--check" in sys.argv:
        print("✓ 两处都能打上，文件没动：")
        for d in done:
            print(f"  {d}")
        print("\n去掉 --check 就真改。")
        return 0

    shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(out, encoding="utf-8")
    print("✓ 改完了，备份在 music.py.bak-merge：")
    for d in done:
        print(f"  {d}")
    print()
    print("重启：pm2 restart music-server")
    print("网页强刷一下，然后从任意一个版本打开《无条件》的 ✎ 面板，应该能看到全部 6 行。")
    print("退回：cp server/music.py.bak-merge server/music.py && pm2 restart music-server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
