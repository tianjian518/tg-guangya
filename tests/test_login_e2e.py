"""扫码登录（设备头 / 设备码解析）与港台并入华语的端到端验证。

直接用 mock 替掉网络请求，专注验证逻辑正确性，不需要真实光鸭环境。
可独立运行：python3.11 tests/test_login_e2e.py
"""
from __future__ import annotations

import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.guangya import GuangyaClient, GuangyaError
from core.classifier import Classifier


class _StubImg:
    def save(self, p):
        pass


def test_build_account_headers_has_device_sign():
    c = GuangyaClient(device_id="abc123")
    h = c.build_account_headers()
    assert h["X-Device-Id"] == "abc123"
    assert h["X-Device-Sign"].startswith("wdi10.abc123"), h["X-Device-Sign"]
    assert h["X-Device-Sign"].endswith("x" * 32)
    assert h["X-Client-Id"] == c.client_id


def test_account_post_uses_account_headers():
    """账户请求必须带上设备指纹头，否则光鸭返回空 data。"""
    captured = {}

    def fake_post(self, base, path, body, auth=True, retry=True, headers=None, raw=False):
        captured["headers"] = headers
        return {"device_code": "DC", "verification_uri_complete": "https://x",
                "interval": 5, "expires_in": 120}

    with mock.patch.object(GuangyaClient, "_post", fake_post), \
            mock.patch("qrcode.make", lambda url: _StubImg()):
        c = GuangyaClient(device_id="abc123")
        c.start_qr_login(qr_path=":mem:")
    assert captured["headers"]["X-Device-Sign"].startswith("wdi10.abc123")


def test_start_qr_login_parses_device_code():
    fake = {
        "device_code": "DC123",
        "verification_uri_complete": "https://www.guangyapan.com/confirm?d=1",
        "interval": 5,
        "expires_in": 120,
    }

    with mock.patch.object(GuangyaClient, "_account_post",
                           lambda self, path, body, auth=False: fake), \
            mock.patch("qrcode.make", lambda url: _StubImg()):
        c = GuangyaClient(device_id="abc123")
        info = c.start_qr_login(qr_path=":mem:")
    assert info["device_code"] == "DC123"
    assert info["qr_url"].startswith("https://")


def test_start_qr_login_incomplete_raises_with_raw():
    with mock.patch.object(GuangyaClient, "_account_post",
                           lambda self, path, body, auth=False: {}):  # 空 data
        c = GuangyaClient(device_id="abc123")
        try:
            c.start_qr_login(qr_path=":mem:")
            assert False, "应当抛错"
        except GuangyaError as e:
            assert "返回不完整" in str(e)
            assert "接口返回" in str(e)  # 诊断信息应包含原始返回


def test_hktw_folds_into_cn():
    clf = Classifier()
    r = clf.classify("无间道 2002 粤语中字 1080P")
    assert r.category == "华语电影", r.category
    r2 = clf.classify("香港 罪案剧 第3集 港剧")
    assert r2.category == "国产剧", r2.category
    # 港台动漫也并入国产动漫，且不出现「港台」目录
    r3 = clf.classify("香港 动画 剧场版 粤语")
    assert "港台" not in r3.category


if __name__ == "__main__":
    test_build_account_headers_has_device_sign()
    test_account_post_uses_account_headers()
    test_start_qr_login_parses_device_code()
    test_start_qr_login_incomplete_raises_with_raw()
    test_hktw_folds_into_cn()
    print("✅ login+classify 全部用例通过")
