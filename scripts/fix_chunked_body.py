#!/usr/bin/env python3
"""fix_chunked_body.py — 让后端认 Transfer-Encoding: chunked。

用法（在仓库根目录）：
    python3 scripts/fix_chunked_body.py --check   # 只看能不能打上，不动文件
    python3 scripts/fix_chunked_body.py           # 真打，先自动备份

病根（这是整个点歌台最大的一处）：
  Python 的 BaseHTTPRequestHandler 不支持 chunked，它只认 Content-Length。
  而浏览器用 HTTP/2 连到反代、再被转成 HTTP/1.1 发给我们时，
  往往是不带 Content-Length 只带 chunked 的（HTTP/2 本来就没这个强制要求）。后果：
    1. _read_body() 读不到 Content-Length -> 返回空 {}
    2. 处理函数拿到空 body -> 判成「缺字段」-> 400
    3. socket 里剩下的分块框架 2c\\r\\n{...}\\r\\n0\\r\\n\\r\\n 被当成下一条请求行
       -> 日志里那句 Bad request syntax ('2c')

  所以浏览器端所有写操作全军覆没：红心收藏/取消、建歌单、删歌、存批注。
  GET 完好无损，所以看着像「搜索还能用、就是收藏不了」。

  取消红心那个补丁（fix_unlike.py）其实 9/25 就打上了，但 music_unliked.json
  十天没生成过一次 —— 因为那个端点的 POST 从来没带着 body 抵达。
  curl 测能通，正是因为 curl 老老实实发 Content-Length。

只改 server/music.py 的 _read_body 一处。不动反代配置（Caddy 那边改不了
浏览器用 HTTP/2 这件事，治后端最干脆）。

出问题：cp server/music.py.bak-chunked server/music.py 再重启。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "server" / "music.py"
BACKUP = ROOT / "server" / "music.py.bak-chunked"

OLD = '''    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)
'''

NEW = '''    def _read_body(self) -> dict[str, Any]:
        """读请求体，同时认 Content-Length 和 chunked。

        Python 的 BaseHTTPRequestHandler 不支持 Transfer-Encoding: chunked,
        只会去读 Content-Length。而浏览器用 HTTP/2 连到反代、再被转成 HTTP/1.1
        发给我们时，常常是不带 Content-Length 只带 chunked 的。后果:
        这里读到空 body -> 所有 POST 处理函数都判成「缺字段」-> 400,
        而 socket 里剩下的分块框架又被当成下一条请求行解析 ->
        日志里那句 Bad request syntax ('2c')。
        浏览器端所有写操作(收藏/取消/建单/删歌/批注)因此全部静默失效。
        """
        enc = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in enc:
            parts = []
            while True:
                line = self.rfile.readline(65536).strip()
                if b";" in line:          # 分块扩展，用不到，切掉
                    line = line.split(b";", 1)[0]
                try:
                    size = int(line, 16)
                except ValueError:
                    break                 # 框架坏了，别把整条连接拖下水
                if size == 0:
                    self.rfile.readline(65536)   # 收尾空行
                    break
                parts.append(self.rfile.read(size))
                self.rfile.readline(65536)       # 每块后面的 CRLF
            raw = b"".join(parts)
            return json.loads(raw) if raw else {}
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)
'''


def main() -> int:
    if not TARGET.exists():
        print(f"找不到 {TARGET}，请在仓库根目录跑：python3 scripts/fix_chunked_body.py")
        return 1

    text = TARGET.read_text(encoding="utf-8")

    if OLD not in text and "chunked" in text:
        print("✓ 看起来已经打过了（_read_body 里已有 chunked 处理），什么都没改。")
        return 0

    hits = text.count(OLD)
    if hits == 0:
        print("✗ _read_body 对不上原文，可能已经改过或上游换了写法。")
        print("  整个文件没动。先看一眼现在长什么样：")
        print("      grep -n '_read_body' -A 8 server/music.py")
        return 2
    if hits > 1:
        print(f"✗ _read_body 原文出现 {hits} 次，不敢猜是哪一处。没改。")
        return 2

    if "--check" in sys.argv:
        print("✓ 能打上，文件没动。去掉 --check 就真改。")
        return 0

    shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"✓ _read_body 已支持 chunked，备份在 {BACKUP.name}")
    print()
    print("重启播放器：pm2 restart music-server")
    print("然后在网页上点一次取消红心，日志里应该是 200，'2c' 那种行不再出现。")
    print(f"要退回去：cp server/{BACKUP.name} server/music.py && pm2 restart music-server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
