#!/usr/bin/env python3
"""diagnose_netease.py — 一层一层问清楚：点歌台到底断在哪一层。

用法（在仓库根目录跑）：
    python3 server/diagnose_netease.py           # 只读体检，不碰你的账号
    python3 server/diagnose_netease.py --write   # 多做一次红心写入往返测试（会动账号，测完还原）

为什么要有它：
  「红心写不进」「歌单只剩 Liked」「搜索一片空白」看着是三个毛病，
  其实这三条路都从同一个地方过 —— server/music.py 里的 _netease_request。
  而前端那三处全是静默吞错：
      搜索   doSearch()                catch { searchResults = []; }
      歌单   renderPlaylists()         api(...).catch(() => ({}))
      红心   likeSong() 里的 syncNe()  旁路异步，失败只弹一句 toast
  所以网易那层一断，页面上长得就像「功能自己消失了」，而不是「出错了」。

  这个脚本把那一层直接摊开：cookie 还活着吗？账号认得出吗？
  搜索/歌单/红心分别回什么？—— 一眼看出是该换 cookie 还是该查网络。

它只读文件，不修改仓库里任何东西。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ── 和 music.py 完全一致的 cookie 读法，免得这里测通了那边还是读不到 ──

def load_cookie() -> str:
    cred = HERE / ".netease_cred"
    try:
        for line in cred.read_text().splitlines():
            if line.startswith("MUSIC_U="):
                return f"MUSIC_U={line.split('=', 1)[1].strip()}"
    except OSError:
        pass
    return ""


def netease(url: str, cookie: str, data: bytes | None = None, timeout: int = 12):
    headers = {
        "Cookie": cookie,
        "Referer": "https://music.163.com",
        "User-Agent": UA,
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class Report:
    """按层记结论，最后一起解读 —— 单看一条看不出病根，连起来才看得出。"""

    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, layer: str, ok: bool, detail: str):
        self.rows.append((layer, ok, detail))
        print(f"  {'✓' if ok else '✗'} {layer}：{detail}")
        return ok

    def ok(self, layer: str) -> bool:
        for name, ok, _ in self.rows:
            if name == layer:
                return ok
        return False


def probe(label, fn, rep: Report):
    """跑一个探针。网络异常不该让整场体检中断，所以这里把异常收成一行结论。"""
    try:
        ok, detail = fn()
        return rep.add(label, ok, detail)
    except urllib.error.HTTPError as e:
        return rep.add(label, False, f"HTTP {e.code}（网易直接拒了这次请求）")
    except urllib.error.URLError as e:
        return rep.add(label, False, f"连不上：{e.reason}")
    except Exception as e:
        return rep.add(label, False, f"{type(e).__name__}: {e}")


def main() -> int:
    do_write = "--write" in sys.argv
    rep = Report()

    print("\n── 第 0 层：本地凭据 ──")
    cookie = load_cookie()
    if not cookie:
        rep.add("cookie 文件", False,
                f"{HERE / '.netease_cred'} 里没读到 MUSIC_U= 这一行")
        print("\n病根已经很清楚了：cookie 都没读到，下面几层不用测。")
        print("修法：在这台机器上新建/更新 server/.netease_cred，写一行")
        print("      MUSIC_U=<你网易云的 MUSIC_U 值>")
        print("      然后重启播放器进程。")
        return 1
    # 只露头尾，别把整串 cookie 打进终端记录
    val = cookie.split("=", 1)[1]
    rep.add("cookie 文件", True, f"读到了，长度 {len(val)}，形如 {val[:6]}…{val[-4:]}")

    print("\n── 第 1 层：这张 cookie 还认得出账号吗 ──")
    uid = {"v": None}

    def _account():
        d = netease("https://music.163.com/api/nuser/account/get", cookie)
        prof = d.get("profile") or {}
        acc = d.get("account") or {}
        u = prof.get("userId") or acc.get("id")
        uid["v"] = u
        if not u:
            return False, f"返回里没有 userId（code={d.get('code')}）—— cookie 多半过期了"
        return True, f"{prof.get('nickname') or '(无昵称)'}  uid={u}"

    probe("账号识别", _account, rep)

    print("\n── 第 2 层：三条路分别通不通 ──")

    def _search():
        form = urlencode({"s": "海阔天空", "type": "1", "limit": "3", "offset": "0"}).encode()
        d = netease("https://music.163.com/api/search/get", cookie, data=form)
        songs = ((d.get("result") or {}).get("songs") or [])
        if not songs:
            return False, f"搜到 0 条（code={d.get('code')}）—— 前端会把这个显示成「没找到这首歌」"
        return True, f"搜到 {len(songs)} 条，第一条：{songs[0].get('name')}"

    probe("搜索 search/get", _search, rep)

    def _likes():
        d = netease("https://music.163.com/api/song/like/get", cookie)
        ids = d.get("ids")
        if ids is None:
            return False, f"没拿到 ids（code={d.get('code')}）"
        return True, f"红心单 {len(ids)} 首"

    probe("红心列表（读）", _likes, rep)

    def _playlists():
        if not uid["v"]:
            return False, "上一层没拿到 uid，这层没法问"
        d = netease(
            f"https://music.163.com/api/user/playlist?uid={uid['v']}&limit=50&offset=0", cookie)
        pls = d.get("playlist") or []
        if not pls:
            return False, f"歌单 0 个（code={d.get('code')}）—— 页面上就只会剩本地那个 Liked"
        mine = sum(1 for p in pls if (p.get("creator") or {}).get("userId") == uid["v"])
        return True, f"{len(pls)} 个（自建 {mine}，收藏 {len(pls) - mine}）"

    probe("账号歌单", _playlists, rep)

    print("\n── 第 3 层：本地播放器服务 ──")

    def _local():
        port = os.environ.get("PORT", "9090")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            d = json.loads(r.read())
        return bool(d.get("ok")), f"127.0.0.1:{port} 活着（{d.get('service')} v{d.get('version')}）"

    probe("本地 HTTP 服务", _local, rep)

    # ── 可选：红心写入往返。会真的动账号，所以默认不做，做完立刻还原 ──
    if do_write:
        print("\n── 第 4 层：红心写入往返（会动账号，测完还原）──")
        TEST_ID = "347230"  # 海阔天空 / Beyond，只借它做一次往返

        def _write():
            before = set(str(i) for i in (netease(
                "https://music.163.com/api/song/like/get", cookie).get("ids") or []))
            was_liked = TEST_ID in before
            target = not was_liked

            form = urlencode({"trackId": TEST_ID,
                              "like": "true" if target else "false",
                              "time": "3", "alg": "itembased"}).encode()
            r = netease("https://music.163.com/api/song/like", cookie, data=form)
            code = r.get("code")

            after = set(str(i) for i in (netease(
                "https://music.163.com/api/song/like/get", cookie).get("ids") or []))
            really = (TEST_ID in after) == target

            # 不管成没成，都试着把账号恢复原状
            back = urlencode({"trackId": TEST_ID,
                              "like": "true" if was_liked else "false",
                              "time": "3", "alg": "itembased"}).encode()
            try:
                netease("https://music.163.com/api/song/like", cookie, data=back)
            except Exception:
                print("     （注意：还原那一步没成功，这首歌的红心状态可能被我改了）")

            if code != 200:
                return False, f"接口回 code={code} {r.get('message') or r.get('msg') or ''}"
            if not really:
                return False, "接口回 200，但红心单里并没真的变 —— 这就是「假成功」"
            return True, f"写入并核对通过（{'取消' if was_liked else '收藏'}方向）"

        probe("红心写入", _write, rep)

    # ── 解读 ──
    print("\n── 结论 ──")
    acct = rep.ok("账号识别")
    paths = [rep.ok("搜索 search/get"), rep.ok("红心列表（读）"), rep.ok("账号歌单")]

    if not acct:
        print("  cookie 读到了，但网易不认 —— 过期或被判失效。")
        print("  这一条就能同时解释红心写不进、歌单只剩 Liked、搜索一片空白：")
        print("  三条路都从 _netease_request 过，源头一断，前端三处 catch 把它们")
        print("  各自吞成了「空结果」，于是看着像三个功能自己没了。")
        print("  修法：换一张新的 MUSIC_U 写进 server/.netease_cred，重启播放器。")
    elif not any(paths):
        print("  账号认得出，但三条路全不通 —— 更像风控或出口网络被挡，不是 cookie 本身。")
        print("  修法：换个出口试一次；短时间内别反复打 like 口子。")
    elif all(paths):
        print("  网易这一层整条是通的。那「红心取消不掉」就不是网络问题，")
        print("  而是本地那份假成功：旧代码取消时只删了 music_playlist.json，")
        print("  music_data.json 里 liked 那份没动，开机又从 /music/netease/likes")
        print("  全量回灌，于是那颗心自己亮回来。")
        print("  修法：server/fix_unlike.py 就是治这个的，但它从没被打上去过 ——")
        print("        python3 server/fix_unlike.py --check   # 先看能不能打")
        print("        python3 server/fix_unlike.py           # 真打（自动备份）")
        print("        然后重启播放器。")
    else:
        broken = [n for n, ok, _ in rep.rows
                  if not ok and n in {"搜索 search/get", "红心列表（读）", "账号歌单"}]
        print(f"  通了一部分，断的是：{'、'.join(broken)}。")
        print("  账号没问题，所以别急着换 cookie；按上面那条断的单独往下查。")

    if not do_write and acct and all(paths):
        print("\n  想确认写入是不是真的生效，可以再跑一次带 --write 的版本。")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
