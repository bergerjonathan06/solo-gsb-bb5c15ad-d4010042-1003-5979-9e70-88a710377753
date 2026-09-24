#!/usr/bin/env python3
"""/range-response 调用示例（仅依赖 Python 3 标准库）。

脚本走完整链路：/plan → 模拟逐块回源（206）→ /verify-fetch-batch →
/assemble → /range-response，最后把 /range-response 返回的响应表示当作
真正的 HTTP 206 响应保存：单段直接写盘，多段则自行按 multipart/byteranges
分帧切出每段（并可用 /multipart-parse 交叉验证），逐字节核对内容。

用法：
    python examples/range_response_example.py            # 单段
    python examples/range_response_example.py --multipart  # 多段

环境变量：
    RANGE_PLANNER_URL  服务地址（默认 http://127.0.0.1:8080）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("RANGE_PLANNER_URL", "http://127.0.0.1:8080")
OBJECT_LENGTH = 256
CONTENT_TYPE = "application/octet-stream"
ETAG = '"v3-strong"'


def call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} from {path}: {exc.read().decode()}")
        sys.exit(1)


def split_multipart(body: bytes, boundary: str) -> list[tuple[dict, bytes]]:
    """极简 multipart/byteranges 切分（示例用途；线上交给 /multipart-parse）。"""
    delim = b"--" + boundary.encode("ascii")
    assert body.startswith(delim + b"\r\n")
    assert body.endswith(delim + b"--")
    parts: list[tuple[dict, bytes]] = []
    pos = len(delim) + 2
    while True:
        nxt = body.find(b"\r\n" + delim, pos)
        assert nxt != -1
        raw = body[pos:nxt]
        head, _, payload = raw.partition(b"\r\n\r\n")
        headers: dict = {}
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            headers[name.decode().lower()] = value.strip().decode()
        parts.append((headers, payload))
        pos = nxt + 2 + len(delim)
        if body[pos : pos + 2] == b"--":
            break
        pos += 2
    return parts


def main() -> None:
    multipart = "--multipart" in sys.argv[1:]
    obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    if multipart:
        ranges = [
            {"type": "interval", "start": 0, "end": 99},
            {"type": "suffix", "last_n": 16},
        ]
        wanted = [(0, 99), (240, 255)]
    else:
        ranges = [{"type": "interval", "start": 10, "end": 73}]
        wanted = [(10, 73)]

    # 1. /plan
    plan = call(
        "/plan",
        {
            "object_length": OBJECT_LENGTH,
            "ranges": ranges,
            "max_gap": 8,
            "fetch_limit": 128,
        },
    )

    # 2. 模拟上游逐块回源（206 + Content-Range + 同一强 ETag）。
    responses = []
    for block in plan["blocks"]:
        s, e = block["start"], block["end"]
        responses.append(
            {
                "block_index": block["index"],
                "status_code": 206,
                "content_range": f"bytes {s}-{e}/{OBJECT_LENGTH}",
                "etag": ETAG,
                "body_b64": base64.b64encode(obj[s : e + 1]).decode("ascii"),
            }
        )

    # 3. /verify-fetch-batch 验收 → 4. /assemble 重组
    verified = call(
        "/verify-fetch-batch",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "responses": responses,
            "expected_etag": ETAG,
        },
    )
    assembled = call(
        "/assemble",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "mappings": plan["mappings"],
            "contents": verified["contents"],
        },
    )

    # 5. /range-response 生成可直接下发给客户端的 206 响应表示。
    response_repr = call(
        "/range-response",
        {
            "object_length": assembled["object_length"],
            "ranges": assembled["ranges"],
            "content_type": CONTENT_TYPE,
        },
    )

    print("== 服务端返回的 206 响应表示 ==")
    print(f"status_code: {response_repr['status_code']}")
    for name, value in response_repr["headers"].items():
        print(f"{name}: {value}")
    body = base64.b64decode(response_repr["body_b64"])
    assert len(body) == int(response_repr["headers"]["Content-Length"])
    print(f"(body {len(body)} bytes，base64 长度 {len(response_repr['body_b64'])})")

    # 6. 客户端视角：按响应头消费正文并逐字节核对。
    if not multipart:
        cr = response_repr["headers"]["Content-Range"]
        unit, _, span = cr.partition(" ")
        assert unit == "bytes"
        start_end, _, total = span.partition("/")
        s, e = (int(x) for x in start_end.split("-"))
        assert (s, e, int(total)) == (wanted[0][0], wanted[0][1], OBJECT_LENGTH)
        assert body == obj[s : e + 1]
        print(f"OK: 单段 206 正文与对象 [{s},{e}] 逐字节一致")
        return

    ct = response_repr["headers"]["Content-Type"]
    assert ct.startswith("multipart/byteranges; boundary=")
    boundary = ct.split("boundary=", 1)[1]
    parts = split_multipart(body, boundary)
    assert len(parts) == len(wanted)
    for (headers, payload), (s, e) in zip(parts, wanted):
        assert headers["content-type"] == CONTENT_TYPE
        assert headers["content-range"] == f"bytes {s}-{e}/{OBJECT_LENGTH}"
        assert payload == obj[s : e + 1], f"范围 [{s},{e}] 内容不一致"
        print(f"OK: part [{s},{e}] {len(payload)} 字节，Content-Type/Content-Range 正确")

    # 交叉验证：生成的正文可被 /multipart-parse 严格解析回各段。
    parsed = call(
        "/multipart-parse",
        {
            "object_length": OBJECT_LENGTH,
            "ranges": ranges,
            "boundary": boundary,
            "expected_content_type": CONTENT_TYPE,
            "body_b64": response_repr["body_b64"],
        },
    )
    assert parsed["parts"] == len(wanted)
    print("OK: 生成的 multipart 正文可被 /multipart-parse 严格解析回各段")


if __name__ == "__main__":
    main()
