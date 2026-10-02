#!/usr/bin/env python3
"""fix_unlike.py — 让点歌台能真正取消红心。

用法（在仓库根目录）：
    python3 server/fix_unlike.py --check     # 只看能不能打上，不动文件
    python3 server/fix_unlike.py             # 真打，先自动备份

它只改 server/music.py 一个文件，四处：
  1. 新增「已取消」名单的读写（music_unliked.json）
  2. 取消红心时，legacy 歌单和 liked 多歌单一起删，并记进名单
  3. /music/netease/likes 返回时排掉名单里的 id
  4. 重新收藏时把 id 从名单里拿出来

为什么：
  取消红心以前是「假成功」。旧代码只删了 music_playlist.json，
  music_data.json 里 liked 那份没动 —— 从 Liked 页进去歌还在；
  网易云账号那边也常常没真删掉，而开机时前端会从 /music/netease/likes
  拉全量红心回灌，所以下次进来那颗心又亮了。
  现在本地留一份取消名单，回灌时挡掉；重新收藏就放行。

不动的地方：
  - 网易云的接口调用方式一行没改（只过滤返回值）
  - 不加依赖、不改数据结构、不改任何现有字段的含义
  - 出问题：把 server/music.py.bak-unlike 覆盖回 music.py 即可
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "music.py"
BACKUP = HERE / "music.py.bak-unlike"

PATCHES = []


def patch(name, old, new):
    PATCHES.append((name, old, new))


# ── 1. 新增名单读写 ─────────────────────────────────────────────────────────
patch(
    "新增 unliked 名单读写",
    '''    def _music_data_path(self) -> Path:
        return self.state.data_dir / "music_data.json"
''',
    '''    def _unliked_path(self) -> Path:
        """本地说「这颗心已经取消了」的 songId 名单。

        网易云那个 like 口子偶尔回 200 却没真删掉，开机时
        /music/netease/likes 会把全量红心拉回来，刚取消的那颗心又亮了。
        这份名单就是挡这一下的：回灌时先过一遍，取消过的不算数。
        重新收藏一首时，从名单里拿出来。
        """
        return self.state.data_dir / "music_unliked.json"

    def _load_unliked(self) -> set:
        p = self._unliked_path()
        if p.exists():
            try:
                return {str(x) for x in json.loads(p.read_text())}
            except Exception:
                pass
        return set()

    def _save_unliked(self, ids: set):
        p = self._unliked_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(ids), ensure_ascii=False))

    def _music_data_path(self) -> Path:
        return self.state.data_dir / "music_data.json"
''',
)

# ── 2. 取消红心：两处歌单一起删 + 记名单 ─────────────────────────────────────
patch(
    "取消红心时 liked 多歌单也删、并记名单",
    '''    def _handle_music_playlist_remove(self, body: dict):
        song_id = body.get("songId")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        playlist = self._load_playlist()
        playlist = [s for s in playlist if s.get("songId") != song_id]
        self._save_playlist(playlist)
        self._send_json(200, {"ok": True, "songs": playlist})
''',
    '''    def _handle_music_playlist_remove(self, body: dict):
        """取消红心。

        旧版只删 legacy 歌单，liked 多歌单原封不动 —— 从 Liked 页进去歌还在，
        看着就是「取消了没反应」。现在两处一起删，再记进 unliked 名单，
        挡住开机时从网易云回灌。
        """
        song_id = body.get("songId")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        sid = str(song_id)
        playlist = self._load_playlist()
        playlist = [s for s in playlist if str(s.get("songId")) != sid]
        self._save_playlist(playlist)

        data = self._load_music_data()
        for pl in data.get("playlists", []):
            if pl.get("id") != "liked":
                continue
            before = len(pl.get("songs") or [])
            pl["songs"] = [s for s in (pl.get("songs") or []) if str(s.get("songId")) != sid]
            if len(pl["songs"]) != before:
                self._save_music_data(data)
            break

        unliked = self._load_unliked()
        unliked.add(sid)
        self._save_unliked(unliked)

        self._send_json(200, {"ok": True, "songs": playlist})
''',
)

# ── 3. 红心列表回灌时排掉取消过的 ───────────────────────────────────────────
patch(
    "红心列表回灌时排掉取消过的",
    '''        ids = d.get("ids") or []
        self._send_json(200, {"ok": True, "count": len(ids), "ids": ids})
''',
    '''        ids = d.get("ids") or []
        unliked = self._load_unliked()
        if unliked:
            ids = [i for i in ids if str(i) not in unliked]
        self._send_json(200, {"ok": True, "count": len(ids), "ids": ids})
''',
)

# ── 4. 重新收藏时从名单里拿出来 ─────────────────────────────────────────────
patch(
    "重新收藏时从名单里拿出来",
    '''        playlist.append(song)
        self._save_playlist(playlist)
        # Also add to "liked" in multi-playlist system
        data = self._load_music_data()
''',
    '''        playlist.append(song)
        self._save_playlist(playlist)
        # 重新收藏：把这颗心从「已取消」名单里拿出来，不然它还会被挡
        unliked = self._load_unliked()
        if str(song["songId"]) in unliked:
            unliked.discard(str(song["songId"]))
            self._save_unliked(unliked)
        # Also add to "liked" in multi-playlist system
        data = self._load_music_data()
''',
)


def main() -> int:
    if not TARGET.exists():
        print(f"找不到 {TARGET}——请在仓库根目录跑：python3 server/fix_unlike.py")
        return 1

    text = TARGET.read_text(encoding="utf-8")
    out = text
    applied = []

    for name, old, new_frag in PATCHES:
        hits = out.count(old)
        if hits == 0:
            print(f"✗ 「{name}」对不上原文，可能已经改过或上游换了写法。")
            print("  整个文件没动，什么都没改。")
            return 2
        if hits > 1:
            print(f"✗ 「{name}」在文件里出现不止一次，不敢猜是哪一处。没改。")
            return 2
        out = out.replace(old, new_frag, 1)
        applied.append(name)

    if "--check" in sys.argv:
        print("✓ 四处都能打上，文件没动：")
        for n in applied:
            print(f"  · {n}")
        return 0

    shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(out, encoding="utf-8")
    print(f"✓ 打上了 {len(applied)} 处，原文件备份在 {BACKUP.name}：")
    for n in applied:
        print(f"  · {n}")
    print()
    print("重启播放器：pm2 restart music   （名字以你 pm2 list 里显示的为准）")
    print(f"要退回去：cp {BACKUP.name} music.py && pm2 restart music")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
