#!/usr/bin/env python3
r"""fix_memo_notelist.py — 批注改成逐条存，每条带落笔人和时间。

用法（在仓库根目录）：
    python3 scripts/fix_memo_notelist.py --inspect   # 先看现在存成什么样，不改代码
    python3 scripts/fix_memo_notelist.py --check     # 只看能不能打，不动文件
    python3 scripts/fix_memo_notelist.py             # 真打，每个文件先备份

为什么要改：
  批注以前是用 \n 拼成一段文字存在 notes 里，整条记录只挂一个 notedBy/notedAt。
  所以一首歌写了五笔，只看得出「最后一笔是谁什么时候写的」，
  前四笔的作者和时间全丢了（《无条件》473194652 就是这样）。

改成什么样：
  新增 noteList，每条 {text, by, at}，追加时追一条记录而不是接一行文字。
  notes 继续同步成一段文字（批注本目录、旧客户端还在读它），所以不会弄坏其他地方。

  老数据没有逐条时间：读取时现场拆行，用整条记录的 notedBy/notedAt 兜底，
  并标 inferred=True，前端在时间后面加个「?」。**不迁数据、不丢东西。**

改四处：
  server/music.py     加 _memo_notes_of 辅助方法、写入走 noteList、读取时兜底
  mcp/music_mcp.py    memo_read 按条显示「[时间 · 谁] 正文」
  client/index.html   ✎ 面板每条一张小卡，带时间和落笔人

前置：需要已经打过 fix_memo_append.py（appendNote 那一步）。
如果打过 fix_memo_merge.py，归并那边也会跟着按条合；没打也不影响。

出问题：把各自的 .bak-notelist 覆回原文件再重启。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ══ 诊断 ══

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

    new_style = legacy = 0
    rows = []
    for sid, m in mem.items():
        if not isinstance(m, dict):
            continue
        items = m.get("noteList")
        notes = str(m.get("notes") or "").strip()
        if isinstance(items, list) and items:
            new_style += 1
            rows.append((sid, m, len(items), True))
        elif notes:
            legacy += 1
            rows.append((sid, m, len([l for l in notes.split("\n") if l.strip()]), False))

    if not rows:
        print("批注本里还没有写过东西。")
        return 0

    rows.sort(key=lambda r: str(r[1].get("notedAt") or ""), reverse=True)
    print(f"写过批注的 {len(rows)} 首：逐条式 {new_style} 首，老格式（一段文字）{legacy} 首\n")
    for sid, m, n, is_new in rows:
        mark = "逐条" if is_new else "老格式"
        who = f"，整条挂 {m.get('notedBy')} {(m.get('notedAt') or '')[:10]}" if m.get("notedBy") else ""
        print(f"· {m.get('name') or '?'} — {m.get('artist') or ''}  ({sid})")
        print(f"    {n} 笔，{mark}{who}")
        if is_new:
            for it in (m.get("noteList") or [])[:3]:
                t = str(it.get("text") or "")
                print(f"      [{(it.get('at') or '时间不详')[:16]} · {it.get('by') or '?'}] "
                      + (t if len(t) <= 40 else t[:40] + "…"))
    print()
    if legacy:
        print(f"打上补丁后，这 {legacy} 首读取时会自动拆成逐条，时间按整条记录兜底并标「?」。")
        print("以后新写的每一笔都有自己的真时间。")
    return 0


# ══ 补丁 ══

HELPER = '''    @staticmethod
    def _memo_notes_of(entry: dict) -> list:
        """一条记录的批注列表（每条 {text, by, at}）。

        新数据直接用 noteList。老数据只有一段 notes 文字、没有逐条时间，
        就现场拆行，用整条记录的 notedBy/notedAt 兜底并标 inferred，
        前端会在时间后面加个「?」。不改硬盘上的东西。
        """
        items = entry.get("noteList")
        if isinstance(items, list) and items:
            return [dict(i) for i in items
                    if isinstance(i, dict) and str(i.get("text") or "").strip()]
        out = []
        for line in str(entry.get("notes") or "").split("\\n"):
            t = line.strip()
            if t:
                out.append({"text": t, "by": entry.get("notedBy") or "",
                            "at": entry.get("notedAt") or "", "inferred": True})
        return out

'''

WRITE_OLD = '''            if append:
                prev = str(entry.get("notes") or "").strip()
                entry["notes"] = (prev + "\\n" + append) if prev else append
'''

WRITE_NEW = '''            if append:
                # 逐条存：追一条记录，带自己的落笔人和时间。
                # 以前是拼成一段文字，整条只挂一个 notedBy/notedAt——
                # 写了五笔只看得出最后一笔是谁写的，前四笔的出处全丢。
                items = self._memo_notes_of(entry)
                items.append({"text": append, "by": body.get("by", "") or "", "at": now})
                entry["noteList"] = items
                # notes 继续同步成一段文字：批注本目录、旧客户端还在读它
                entry["notes"] = "\\n".join(str(n.get("text") or "") for n in items)
'''

# 读取兜底：打过归并补丁和没打过的原文不同，两种都认
READ_VARIANTS = [
    (
        '''            entry = self._memo_merged(mem, str(song_id))
            self._send_json(200, {"ok": True, "memory": entry})
''',
        '''            entry = self._memo_merged(mem, str(song_id))
            if isinstance(entry, dict) and not isinstance(entry.get("noteList"), list):
                entry = dict(entry, noteList=self._memo_notes_of(entry))
            self._send_json(200, {"ok": True, "memory": entry})
''',
    ),
    (
        '''            entry = mem.get(str(song_id))
            self._send_json(200, {"ok": True, "memory": entry})
''',
        '''            entry = mem.get(str(song_id))
            # 老数据没有 noteList，读的时候现场拆成逐条（见 _memo_notes_of）
            if isinstance(entry, dict) and not isinstance(entry.get("noteList"), list):
                entry = dict(entry, noteList=self._memo_notes_of(entry))
            self._send_json(200, {"ok": True, "memory": entry})
''',
    ),
]

MUSIC_PY = [
    ("加 _memo_notes_of 辅助方法",
     "    def _handle_music_memory_get(self):\n",
     HELPER + "    def _handle_music_memory_get(self):\n"),
    ("写入走 noteList", WRITE_OLD, WRITE_NEW),
]

MCP_OLD = '''        if (m.get("notes") or "").strip():
            out.append("批注：")
            out += ["  " + l for l in m["notes"].strip().split("\\n")]
'''

MCP_NEW = '''        items = m.get("noteList") or []
        if items:
            # 每条带落笔人和时间。老数据没逐条时间，后端标了 inferred，这里加个 ?
            out.append("批注：")
            for it in items:
                when = _fmt_when(it.get("at")) if it.get("at") else "时间不详"
                if it.get("inferred") and it.get("at"):
                    when += "?"
                out.append(f"  [{when} · {it.get('by') or '?'}] {it.get('text') or ''}")
        elif (m.get("notes") or "").strip():
            out.append("批注：")
            out += ["  " + l for l in m["notes"].strip().split("\\n")]
'''

MCP_PY = [("memo_read 按条显示", MCP_OLD, MCP_NEW)]

CLIENT_OLD = "  if (mem.notes) body += `<div class=\"sm-notes\">${esc(mem.notes)}</div>`;\n"

CLIENT_NEW = """  // 每条批注一张小卡，带落笔人和时间。以前是整段文字摆在一起，谁写的、什么时候写的都看不出来。
  // 老数据没有逐条时间，后端标了 inferred，这里在时间后面加个「?」。
  const fmtStamp = (iso) => {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const p = (n) => String(n).padStart(2, '0');
    return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  };
  const noteItems = Array.isArray(mem.noteList) ? mem.noteList : [];
  if (noteItems.length) {
    noteItems.forEach(n => {
      const when = n.at ? (fmtStamp(n.at) + (n.inferred ? '?' : '')) : '时间不详';
      body += '<div style="padding:10px 14px;margin-bottom:8px;background:rgba(255,255,255,.55);'
            + 'border:1px solid var(--line);border-radius:14px">'
            + '<div style="font-size:11px;color:var(--muted);margin-bottom:5px">'
            + esc(when) + ' · ' + esc(n.by || '?') + '</div>'
            + '<div style="font-size:13.5px;line-height:1.7;color:var(--ink);white-space:pre-wrap">'
            + esc(n.text || '') + '</div></div>';
    });
  } else if (mem.notes) {
    body += `<div class="sm-notes">${esc(mem.notes)}</div>`;
  }
"""

CLIENT = [("✎ 面板按条显示", CLIENT_OLD, CLIENT_NEW)]

# 可选：打过归并补丁的话，归并时也按条合
MERGE_OPTIONAL = [
    ("归并时收集逐条",
     "        note_lines, seen_lines = [], set()\n",
     "        note_lines, seen_lines = [], set()\n        note_items = []\n"),
    ("归并时按条去重",
     '''            for line in str(m.get("notes") or "").split("\\n"):
                t = line.strip()
                if t and t not in seen_lines:
                    seen_lines.add(t)
                    note_lines.append(t)
''',
     '''            for it in self._memo_notes_of(m):
                t = str(it.get("text") or "").strip()
                if t and t not in seen_lines:
                    seen_lines.add(t)
                    note_lines.append(t)
                    note_items.append(it)
'''),
    ("归并结果带 noteList",
     '        out["notes"] = "\\n".join(note_lines)\n',
     '''        out["notes"] = "\\n".join(note_lines)
        if note_items:
            note_items.sort(key=lambda n: str(n.get("at") or ""))
            out["noteList"] = note_items
'''),
]


def apply(rel: str, patches: list, check: bool, optional: bool = False) -> tuple[bool, list[str]]:
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
            if optional:
                msgs.append(f"· {name}（跳过：没打过归并补丁）")
                return True, msgs
            msgs.append(f"✗ 「{name}」对不上原文")
            return False, msgs
        if hits > 1:
            msgs.append(f"✗ 「{name}」出现 {hits} 次，不敢猜")
            return False, msgs
        out = out.replace(old, new, 1)
        msgs.append(f"· {name}")
    if out != text and not check:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak-notelist"))
        path.write_text(out, encoding="utf-8")
    return True, msgs


def apply_read_path(check: bool) -> tuple[bool, list[str]]:
    """读取兜底：两种原文变体选匹配的那一个。"""
    path = ROOT / "server" / "music.py"
    text = path.read_text(encoding="utf-8")
    for old, new in READ_VARIANTS:
        if new in text:
            return True, ["· 读取时兜底（已经打过了）"]
        if text.count(old) == 1:
            if not check:
                shutil.copy2(path, path.with_suffix(".py.bak-notelist"))
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
            return True, ["· 读取时兜底"]
    return False, ["✗ 「读取时兜底」两种原文都对不上"]


def main() -> int:
    if "--inspect" in sys.argv:
        return inspect()

    check = "--check" in sys.argv
    print("体检模式，不动文件\n" if check else "开始改，每个文件先备份\n")

    steps = [
        ("server/music.py", MUSIC_PY, False),
        ("mcp/music_mcp.py", MCP_PY, False),
        ("client/index.html", CLIENT, False),
        ("server/music.py （归并部分，可选）", MERGE_OPTIONAL, True),
    ]

    ok_all = True
    for label, patches, optional in steps:
        rel = label.split(" ")[0]
        ok, msgs = apply(rel, patches, check, optional)
        print(f"  {label}")
        for m in msgs:
            print(f"    {m}")
        if not ok:
            ok_all = False
            break
        if rel == "server/music.py" and not optional:
            ok2, msgs2 = apply_read_path(check)
            for m in msgs2:
                print(f"    {m}")
            if not ok2:
                ok_all = False
                break

    print()
    if not ok_all:
        print("有对不上的地方，已改的文件有 .bak-notelist 备份。")
        print("最常见的原因：还没打 fix_memo_append.py（appendNote 那一步）。先跑：")
        print("    python3 scripts/fix_memo_append.py --check")
        return 2

    if check:
        print("都能打上，去掉 --check 就真改。")
    else:
        print("改完了。重启两个进程：")
        print("    pm2 restart music-server")
        print("    pm2 restart music-mcp")
        print("网页记得强刷。")
        print()
        print("验证：打开《无条件》的 ✎ 面板，每条批注各是一张小卡，带时间和落笔人；")
        print("老那几笔的时间后面带「?」（按整条记录兜底的），以后新写的都是真时间。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
