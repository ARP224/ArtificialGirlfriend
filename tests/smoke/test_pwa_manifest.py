"""サーバーモードの「アプリとしてインストール」対応（Web App Manifest）の契約。

- デスクトップ(/)とモバイル(/mobile/)がそれぞれ自分の manifest を持つ
  （id/start_url/scope が別＝同一オリジンで2アプリとして共存できる）。
- アイコンは 192/512 の PNG が実在し配信される。
- 外側 FastAPI に登録した manifest ルートが Gradio マウント(pwa=False→404)に
  先勝ちする（/apple-touch-icon.png と同じ前提の実証）。
- 配信 index.html の manifest リンクが mount 相対（href="manifest.json"）に
  書き換わっている（Gradio テンプレート更新時のドリフト検知。head= では
  2本目のリンクは効かないので、この書き換えが唯一の経路）。
"""

import io
import json

import gradio as gr
from fastapi import FastAPI
from PIL import Image
from starlette.testclient import TestClient

from ui.gradio_patches import apply_gradio_index_patch
from ui.pwa_manifest import (
    ICON_SIZES,
    build_desktop_manifest,
    build_mobile_manifest,
    register_pwa_routes,
)


def _tiny_blocks(label: str) -> gr.Blocks:
    with gr.Blocks() as b:
        gr.Markdown(label)
    return b


def _server_like_app() -> FastAPI:
    """app.py のサーバーモード配線と同じ順序: 外側ルート → /mobile → / マウント。"""
    apply_gradio_index_patch()
    app = FastAPI()
    register_pwa_routes(app, mobile_path="/mobile")
    app = gr.mount_gradio_app(app, _tiny_blocks("mobile"), path="/mobile")
    app = gr.mount_gradio_app(app, _tiny_blocks("desktop"), path="/")
    return app


def _assert_manifest_shape(m: dict, *, start_url: str):
    assert m["display"] == "standalone"
    assert m["start_url"] == start_url
    assert m["scope"] == start_url
    assert m["id"] == start_url
    assert m["name"] and m["short_name"]
    assert m["theme_color"].startswith("#") and m["background_color"].startswith("#")
    sizes = sorted({i["sizes"] for i in m["icons"]})
    assert sizes == sorted(f"{s}x{s}" for s in ICON_SIZES)
    assert {i["purpose"] for i in m["icons"]} == {"any", "maskable"}
    assert all(i["type"] == "image/png" and i["src"].startswith("/") for i in m["icons"])


def test_manifest_dicts_are_two_distinct_apps():
    d = build_desktop_manifest()
    m = build_mobile_manifest("/mobile")
    _assert_manifest_shape(d, start_url="/")
    _assert_manifest_shape(m, start_url="/mobile/")
    assert d["id"] != m["id"]
    assert m["name"] == "AG Mobile"  # モバイルページの <title> と同じ＝ホーム画面の既定名不変


def test_routes_win_over_gradio_mounts_and_serve_icons():
    client = TestClient(_server_like_app())

    r = client.get("/manifest.json")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/manifest+json")
    desktop = json.loads(r.text)
    assert desktop["id"] == "/"  # Gradio 内蔵ハンドラ(pwa=False→404)ではなく自前が応答

    r = client.get("/mobile/manifest.json")
    assert r.status_code == 200, r.text
    assert json.loads(r.text)["id"] == "/mobile/"

    for icon in desktop["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        assert r.headers["content-type"] == "image/png"
        w, h = Image.open(io.BytesIO(r.content)).size
        assert f"{w}x{h}" == icon["sizes"]

    assert client.get("/static/pwa-icon-64.png").status_code == 404


def test_served_index_links_manifest_mount_relatively():
    client = TestClient(_server_like_app())
    for path in ("/", "/mobile/"):
        html = client.get(path).text
        assert 'rel="manifest"' in html, path
        assert 'href="manifest.json"' in html, path
        assert 'href="/manifest.json"' not in html, path
