#!/usr/bin/env python3
r"""fix_memo_append.py — 批注改成后端追加，不再互相覆盖。

用法（在仓库根目录）：
    python3 scripts/fix_memo_append.py --inspect   # 先看现在实际存了什么，不改代码
    python3 scripts/fix_memo_append.py --check     # 只看四处能不能打，不动文件
    python3 scripts/fix_memo_append.py             # 真打，每个文件先备份

病根：
  批注有两个写入方（网页 ✎ 面板、AI 的 song_memo），以前**都是自己先 GET 读旧值、
  在本地拼成完整 notes 再发回去**。于是：
    - 网页那边 mem 是「打开面板那一刻」的快照，AI 之后写的它不知道，一保存就盖掉；
    - MCP 那边 music_get 失败时被 `except: pass` 吞掉，old 变成 {}，prev 为空，
      一样把已有的全盖掉；
    - 两边同时写更是后到的赢。
  表现就是：明明加了新内容，批注本里只剩最早那条。

改法：
  新增 appendNote 字段，**追加这件事交给后端做** —— 它自己读 entry 里的旧值再接一行。
  调用方只管把「这次新写的那句」发过去，不用也不该再自己拼。
  三方一起改：后端支持 appendNote、MCP 改用它、网页也改用它。

顺手删掉 _handle_music_memory_save 里第二个永远走不到的 `elif action == "note"`
（Python elif 链第一个命中就跳出，它是死代码，留着只会让人误判）。

出问题：把各自的 .bak-append 覆回原文件再重启。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ══ 诊断：现在实际存了什么 ══

def inspect() -> int:
    path = ROOT / "server" / "data" / "music_memory.json"
    if not path.exists():
        print(f"找不到 {path}")
        print("（数据文件不进 git，要在跑播放器那台机器上跑这个脚本）")
        return 1
    try:
        mem = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读不动：{e}")
        return 1

    rows = []
    for sid, m in mem.items():
        if not isinstance(m, dict):
            continue
        notes = str(m.get("notes") or "").strip()
        favs = m.get("favoriteLines") or []
        if not notes and not favs:
            continue
        rows.append((sid, m, notes, favs))

    if not rows:
        print("批注本里还没有写过东西的歌。")
        return 0

    rows.sort(key=lambda r: r[1].get("notedAt") or "", reverse=True)
    print(f"批注本：{len(rows)} 首写过东西（全部 {len(mem)} 首有记录）\n")
    for sid, m, notes, favs in rows:
        lines = [l for l in notes.split("\n") if l.strip()] if notes else []
        title = f"{m.get('name') or '?'} — {m.get('artist') or ''}"
        print(f"· {title}  (song_id {sid})")
        print(f"    批注 {len(lines)} 行"
              + (f"，句子 {len(favs)} 条" if favs else "")
              + (f"，上次落笔 {m.get('notedBy') or '?'} {(m.get('notedAt') or '')[:10]}" if m.get("notedBy") else ""))
        for i, l in enumerate(lines, 1):
            short = l if len(l) <= 56 else l[:56] + "…"
            print(f"      {i}. {short}")
        print()

    multi = [r for r in rows if len([l for l in r[2].split("\n") if l.strip()]) > 1]
    if multi:
        print(f"有 {len(multi)} 首的批注是多行的 —— 追加曾经成功过。")
    else:
        print("所有批注都只有一行 —— 和「只剩最早那条」的现象对得上。")
    return 0


# ══ 补丁 ══

TASKS: list[tuple[str, list[tuple[str, str, str]]]] = []


# ── 1. server/music.py：后端支持 appendNote，并删掉死代码分支 ──

MUSIC_PY = [
    (
        "note 分支支持 appendNote",
        r'''            for key in ("notes", "feeling", "tags", "favoriteLines"):
                if key in body:
                    entry[key] = body[key]
''',
        r'''            # appendNote：追加交给后端做。旧做法是网页和 MCP 各自先 GET 读旧值、
            # 拼成完整 notes 再发回来 —— 网页那份是打开面板那一刻的快照，
            # MCP 那边读失败还会被 except 吞掉变成空，两边都会把对方写的盖掉
            # （表现：批注只剩最早那条）。现在调用方只发「这次新写的那句」。
            append = str(body.get("appendNote") or "").strip()
            for key in ("notes", "feeling", "tags", "favoriteLines"):
                # 带了 appendNote 就以追加为准，忽略同一请求里的整段 notes
                if key in body and not (key == "notes" and append):
                    entry[key] = body[key]
            if append:
                prev = str(entry.get("notes") or "").strip()
                entry["notes"] = (prev + "\n" + append) if prev else append
''',
    ),
    (
        "删掉永远走不到的第二个 note 分支",
        r'''        elif action == "note":
            entry["notes"] = body.get("notes", entry.get("notes", ""))
            if body.get("feeling"):
                entry["feeling"] = body["feeling"]
            if body.get("favoriteLines"):
                entry["favoriteLines"] = body["favoriteLines"]
        mem[song_id] = entry
''',
        r'''        mem[song_id] = entry
''',
    ),
]
TASKS.append(("server/music.py", MUSIC_PY))


# ── 2. mcp/music_mcp.py：不再自己先读后拼 ──

MCP_PY = [
    (
        "_memo 改用 appendNote",
        r'''def _memo(song, memo):
    sid = str(song["id"])
    old = {}
    try:
        old = music_get("/music/memory", id=sid).get("memory") or {}
    except Exception:
        pass
    prev = (old.get("notes") or "").strip()
    text = memo if not prev else prev + "\n" + memo
    music_post("/music/memory", {"songId": sid, "action": "note", "notes": text, "by": SIGN_AS,
                                 "name": song.get("name", ""), "artist": song.get("artist", "")})
''',
        r'''def _memo(song, memo):
    """往批注本追加一笔。

    追加由后端做（appendNote）：以前是这里先 GET 读旧值、拼成完整 notes 再发回去，
    读失败时被 except 吞掉就变成空值，一发就把网页那边写的全盖掉；
    和网页同时写更是后到的赢。表现就是批注只剩最早那条。
    """
    sid = str(song["id"])
    music_post("/music/memory", {"songId": sid, "action": "note", "appendNote": memo, "by": SIGN_AS,
                                 "name": song.get("name", ""), "artist": song.get("artist", "")})
''',
    ),
]
TASKS.append(("mcp/music_mcp.py", MCP_PY))


# ── 3. client/index.html：网页也改用 appendNote ──

CLIENT = [
    (
        "保存批注改用 appendNote",
        r'''        // Append, never overwrite: the input holds only the new entry.
        notes: [mem.notes || '', fresh].filter(Boolean).join('\n'),
''',
        r'''        // 追加交给后端（appendNote）：这里的 mem 是打开面板那一刻的快照，
        // 拿它拼完整 notes 发回去，会把这之后 AI 写的那几行盖掉。
        appendNote: fresh,
''',
    ),
]
TASKS.append(("client/index.html", CLIENT))


def apply(rel: str, patches: list, check: bool) -> tuple[bool, list[str]]:
    path = ROOT / rel
    if not path.exists():
        return False, [f"✗ 找不到 {rel}"]
    text = path.read_text(encoding="utf-8")
    out = text
    msgs = []
    for name, old, new in patches:
        if new in out:
            msgs.append(f"· {name}（已经打过了）")
            continue
        hits = out.count(old)
        if hits == 0:
            msgs.append(f"✗ 「{name}」对不上原文")
            return False, msgs
        if hits > 1:
            msgs.append(f"✗ 「{name}」出现 {hits} 次，不敢猜")
            return False, msgs
        out = out.replace(old, new, 1)
        msgs.append(f"· {name}")
    if out != text and not check:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak-append"))
        path.write_text(out, encoding="utf-8")
    return True, msgs


def main() -> int:
    if "--inspect" in sys.argv:
        return inspect()

    check = "--check" in sys.argv
    print("体检模式，不动文件\n" if check else "开始改，每个文件先备份\n")
    ok_all = True
    for rel, patches in TASKS:
        ok, msgs = apply(rel, patches, check)
        print(f"  {rel}")
        for m in msgs:
            print(f"    {m}")
        if not ok:
            ok_all = False
            break
    print()
    if not ok_all:
        print("有对不上的地方，已改的文件有 .bak-append 备份。")
        print("先看一眼现在长什么样：")
        print("    grep -n 'appendNote\\|favoriteLines\\[key\\]' server/music.py | head")
        return 2
    if check:
        print("四处都能打上，去掉 --check 就真改。")
    else:
        print("改完了。重启两个进程：")
        print("    pm2 restart music-server")
        print("    pm2 restart music-mcp      （名字以 pm2 list 里显示的为准）")
        print("网页记得强刷（index.html 会被浏览器缓存）。")
        print()
        print("验证：同一首歌连写两笔，再跑一次")
        print("    python3 scripts/fix_memo_append.py --inspect")
        print("应该能看到那首歌的批注是 2 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
