# -*- coding: utf-8 -*-
"""POST /range-response 的行为测试。

覆盖：
- 单段成功：206 + Content-Type/Content-Range/Content-Length，正文即范围字节；
- 多段成功：multipart/byteranges 按下标顺序排列，每段附 Content-Type 与
  Content-Range，Content-Length 等于最终二进制正文长度，生成的正文可用
  /multipart-parse 严格解析回各段；
- boundary 安全：正文内嵌 CRLF+"--boundary" 形态字节时会自动换一个不冲突
  的 boundary（随机性受限时的重试）；
- 下标：重复、缺失、多余、负数、乱序输入（仍按下标升序输出）；
- 区间：end<start、越界；
- 长度：声明长度与区间不符、Base64 解码字节数与声明长度不符；
- Base64：非法/填充错误；
- content_type：空值、非 type/subtype、CRLF 注入、非 ASCII、非法参数；
- 错误响应：统一 422、detail 携带 loc/range_index 与原因。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import unittest
from unittest import mock

from app.main import app

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class RangeResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    # ----------------------------- 构造工具 -----------------------------

    @staticmethod
    def assembled(
        index: int,
        start: int,
        end: int,
        data: bytes,
        *,
        rtype: str = "interval",
        declared_length: int | None = None,
        data_b64: str | None = None,
    ) -> dict:
        return {
            "index": index,
            "type": rtype,
            "start": start,
            "end": end,
            "length": (end - start + 1) if declared_length is None else declared_length,
            "data_b64": b64(data) if data_b64 is None else data_b64,
        }

    def post(self, body: dict):
        return self.client.post("/range-response", json=body)

    def err(self, resp, code: str, range_index: int | None = None) -> dict:
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        self.assertEqual(payload["error"], code, payload)
        detail = payload["detail"][0]
        self.assertIn("message", detail)
        self.assertIn("loc", detail)
        if range_index is not None:
            self.assertEqual(detail.get("range_index"), range_index, detail)
        return detail

    def object_bytes(self, n: int = 256) -> bytes:
        return bytes((i % 256 for i in range(n)))

    # ----------------------------- 单段成功路径 -----------------------------

    def test_single_range_206_plain_body(self) -> None:
        obj = self.object_bytes()
        data = obj[0:100]
        body = {
            "object_length": 256,
            "ranges": [self.assembled(0, 0, 99, data)],
            "content_type": "application/pdf",
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["status_code"], 206)
        self.assertEqual(result["headers"]["Content-Type"], "application/pdf")
        self.assertEqual(result["headers"]["Content-Range"], "bytes 0-99/256")
        self.assertEqual(
            int(result["headers"]["Content-Length"]), len(data)
        )
        self.assertEqual(base64.b64decode(result["body_b64"]), data)

    def test_single_suffix_range(self) -> None:
        obj = self.object_bytes()
        data = obj[240:256]
        body = {
            "object_length": 256,
            "ranges": [
                self.assembled(0, 240, 255, data, rtype="suffix"),
            ],
            "content_type": "application/octet-stream",
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["headers"]["Content-Range"], "bytes 240-255/256")
        self.assertEqual(base64.b64decode(result["body_b64"]), data)

    def test_content_type_parameters_preserved_verbatim(self) -> None:
        # 参数与原始大小写原样保留（仅去首尾空白）。
        ct = 'text/plain; charset="utf-8"; boundary=Keep-Me'
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"abcd")],
            "content_type": f"  {ct}  ",
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["headers"]["Content-Type"], ct)

    def test_single_range_content_length_matches_actual_body(self) -> None:
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"\x00\xff\r\n")],
            "content_type": "application/octet-stream",
        }
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        raw = base64.b64decode(resp.json()["body_b64"])
        self.assertEqual(raw, b"\x00\xff\r\n")
        self.assertEqual(
            int(resp.json()["headers"]["Content-Length"]), len(raw)
        )

    # ----------------------------- 多段成功路径 -----------------------------

    def _two_range_request(self, obj: bytes, *, data0=None, data1=None) -> dict:
        return {
            "object_length": 256,
            "ranges": [
                self.assembled(0, 0, 99, data0 if data0 is not None else obj[0:100]),
                self.assembled(1, 240, 255, data1 if data1 is not None else obj[240:256]),
            ],
            "content_type": "application/octet-stream",
        }

    def test_multi_range_multipart_structure(self) -> None:
        obj = self.object_bytes()
        resp = self.post(self._two_range_request(obj))
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        headers = result["headers"]
        ct = headers["Content-Type"]
        self.assertTrue(ct.startswith("multipart/byteranges; boundary="))
        boundary = ct.split("boundary=", 1)[1]
        self.assertNotIn("Content-Range", headers)  # 多段无顶层 Content-Range
        wire = base64.b64decode(result["body_b64"])
        self.assertEqual(int(headers["Content-Length"]), len(wire))

        # 结构必须与 /multipart-parse 产出的上游格式一致。
        self.assertTrue(wire.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(wire.endswith(f"--{boundary}--".encode()))
        for start, end in ((0, 99), (240, 255)):
            header = (
                f"Content-Type: application/octet-stream\r\n"
                f"Content-Range: bytes {start}-{end}/256\r\n\r\n"
            ).encode()
            self.assertIn(header, wire)
        # 第一段的数据出现在第二段之前（按下标顺序）。
        self.assertLess(wire.index(obj[0:100]), wire.index(obj[240:256]))

    def test_multi_range_output_roundtrips_through_multipart_parse(self) -> None:
        obj = self.object_bytes()
        gen = self.post(self._two_range_request(obj)).json()
        boundary = gen["headers"]["Content-Type"].split("boundary=", 1)[1]
        parse_req = {
            "object_length": 256,
            "ranges": [
                {"type": "interval", "start": 0, "end": 99},
                {"type": "interval", "start": 240, "end": 255},
            ],
            "boundary": boundary,
            "expected_content_type": "application/octet-stream",
            "body_b64": gen["body_b64"],
        }
        parsed = self.client.post("/multipart-parse", json=parse_req)
        self.assertEqual(parsed.status_code, 200, parsed.text)
        ranges = parsed.json()["ranges"]
        self.assertEqual(base64.b64decode(ranges[0]["data_b64"]), obj[0:100])
        self.assertEqual(base64.b64decode(ranges[1]["data_b64"]), obj[240:256])

    def test_ranges_shuffled_input_emitted_in_index_order(self) -> None:
        obj = self.object_bytes()
        body = {
            "object_length": 256,
            "ranges": [
                self.assembled(2, 200, 209, obj[200:210]),
                self.assembled(0, 0, 9, obj[0:10]),
                self.assembled(1, 100, 109, obj[100:110]),
            ],
            "content_type": "application/octet-stream",
        }
        result = self.post(body).json()
        wire = base64.b64decode(result["body_b64"])
        self.assertLess(wire.index(obj[0:10]), wire.index(obj[100:110]))
        self.assertLess(wire.index(obj[100:110]), wire.index(obj[200:210]))

    def test_boundary_avoids_delimiter_bytes_inside_payload(self) -> None:
        # 让 os.urandom 第一次返回固定值，构造包含该候选分隔行的正文，
        # 验证服务会换一个 boundary，且最终正文可被严格解析。
        first_hex = b"\x11" * 16
        target = ("\r\n--range-response-" + first_hex.hex()).encode()
        payload0 = b"X" * 20 + target + b"Y" * 20
        payload1 = b"zzzz"
        obj = self.object_bytes(256)
        body = {
            "object_length": 256,
            "ranges": [
                self.assembled(0, 0, len(payload0) - 1, payload0),
                self.assembled(
                    1, 240, 255, payload1, data_b64=b64(payload1)
                ),
            ],
            "content_type": "application/octet-stream",
        }
        # 第二段长度必须与区间一致：用真实数据替换。
        body["ranges"][1] = self.assembled(1, 240, 255, obj[240:256])
        real_urandom = __import__("os").urandom
        values = iter([first_hex, b"\x22" * 16])

        def fake_urandom(n: int) -> bytes:
            try:
                return next(values)
            except StopIteration:
                return real_urandom(n)

        with mock.patch("app.main.os.urandom", side_effect=fake_urandom):
            resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        boundary = result["headers"]["Content-Type"].split("boundary=", 1)[1]
        self.assertNotEqual(boundary, "range-response-" + first_hex.hex())
        wire = base64.b64decode(result["body_b64"])
        # 首次候选的分隔行字节只作为普通正文出现，最终 boundary 已避开它。
        self.assertIn(payload0, wire)
        # 用最终 boundary 可严格切分（/multipart-parse 全量校验）。
        parsed = self.client.post(
            "/multipart-parse",
            json={
                "object_length": 256,
                "ranges": [
                    {"type": "interval", "start": 0, "end": len(payload0) - 1},
                    {"type": "interval", "start": 240, "end": 255},
                ],
                "boundary": boundary,
                "body_b64": result["body_b64"],
            },
        )
        self.assertEqual(parsed.status_code, 200, parsed.text)
        parts = parsed.json()["ranges"]
        self.assertEqual(base64.b64decode(parts[0]["data_b64"]), payload0)

    # ----------------------------- 下标校验 -----------------------------

    def test_duplicate_index_rejected(self) -> None:
        body = {
            "object_length": 256,
            "ranges": [
                self.assembled(0, 0, 9, b"0123456789"),
                self.assembled(0, 10, 19, b"abcdefghij"),
            ],
            "content_type": "application/octet-stream",
        }
        self.err(self.post(body), "duplicate_range_index", range_index=0)

    def test_missing_index_rejected(self) -> None:
        body = {
            "object_length": 256,
            "ranges": [
                self.assembled(0, 0, 9, b"0123456789"),
                self.assembled(2, 10, 19, b"abcdefghij"),
            ],
            "content_type": "application/octet-stream",
        }
        detail = self.err(self.post(body), "invalid_range_index")
        self.assertIn("缺少", detail["message"])

    def test_negative_index_rejected(self) -> None:
        body = {
            "object_length": 256,
            "ranges": [self.assembled(-1, 0, 9, b"0123456789")],
            "content_type": "application/octet-stream",
        }
        # Pydantic 层不限制 index，语义校验返回 range_response 风格错误。
        self.err(self.post(body), "invalid_range_index", range_index=-1)

    # ----------------------------- 区间与长度校验 -----------------------------

    def test_end_before_start_rejected(self) -> None:
        # AssembledRange.length 有 ge=1 约束，这里手动构造反转区间
        # （length 取闭区间长度），由端点语义校验拒绝。
        body = {
            "object_length": 256,
            "ranges": [
                {
                    "index": 0,
                    "type": "interval",
                    "start": 100,
                    "end": 9,
                    "length": 1,
                    "data_b64": b64(b"0123456789"),
                }
            ],
            "content_type": "application/octet-stream",
        }
        self.err(self.post(body), "invalid_range", range_index=0)

    def test_range_out_of_bounds_rejected(self) -> None:
        body = {
            "object_length": 10,
            "ranges": [self.assembled(0, 0, 9, b"0123456789")],
            "content_type": "application/octet-stream",
        }
        body["ranges"][0]["end"] = 10
        body["ranges"][0]["length"] = 11
        body["ranges"][0]["data_b64"] = b64(b"01234567890")
        self.err(self.post(body), "range_out_of_bounds", range_index=0)

    def test_declared_length_not_matching_span_rejected(self) -> None:
        body = {
            "object_length": 256,
            "ranges": [self.assembled(0, 0, 9, b"0123456789", declared_length=9)],
            "content_type": "application/octet-stream",
        }
        self.err(self.post(body), "range_length_mismatch", range_index=0)

    def test_decoded_length_not_matching_declared_rejected(self) -> None:
        body = {
            "object_length": 256,
            "ranges": [self.assembled(0, 0, 9, b"012345678")],  # 9 字节 vs 声明 10
            "content_type": "application/octet-stream",
        }
        self.err(self.post(body), "body_length_mismatch", range_index=0)

    def test_invalid_base64_rejected(self) -> None:
        body = {
            "object_length": 256,
            "ranges": [
                {
                    "index": 0,
                    "type": "interval",
                    "start": 0,
                    "end": 3,
                    "length": 4,
                    "data_b64": "!!!not-base64",
                }
            ],
            "content_type": "application/octet-stream",
        }
        self.err(self.post(body), "invalid_base64", range_index=0)

    # ----------------------------- content_type 校验 -----------------------------

    def test_empty_content_type_rejected(self) -> None:
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"abcd")],
            "content_type": "   ",
        }
        self.err(self.post(body), "invalid_content_type")

    def test_content_type_without_subtype_rejected(self) -> None:
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"abcd")],
            "content_type": "application",
        }
        self.err(self.post(body), "invalid_content_type")

    def test_content_type_crlf_injection_rejected(self) -> None:
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"abcd")],
            "content_type": "application/octet-stream\r\nX-Evil: 1",
        }
        self.err(self.post(body), "invalid_content_type")

    def test_content_type_non_ascii_rejected(self) -> None:
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"abcd")],
            "content_type": "application/octët-stream",
        }
        self.err(self.post(body), "invalid_content_type")

    def test_content_type_bad_parameter_rejected(self) -> None:
        body = {
            "object_length": 16,
            "ranges": [self.assembled(0, 0, 3, b"abcd")],
            "content_type": "text/plain; bogus-no-equals",
        }
        self.err(self.post(body), "invalid_content_type")

    # ----------------------------- 字段级 422 -----------------------------

    def test_empty_ranges_rejected_by_model(self) -> None:
        resp = self.post(
            {
                "object_length": 256,
                "ranges": [],
                "content_type": "application/octet-stream",
            }
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_missing_content_type_rejected_by_model(self) -> None:
        resp = self.post(
            {"object_length": 256, "ranges": [self.assembled(0, 0, 3, b"abcd")]}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
