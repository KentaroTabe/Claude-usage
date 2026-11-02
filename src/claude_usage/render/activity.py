"""活動の再生画面の HTML を組み立てる。CSS・JS・データをファイルに埋め込み、外部のものを読まない 1 ファイルにする。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

ASSETS = Path(__file__).resolve().parent / "assets"


def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def embed_json(payload: Mapping[str, object]) -> str:
    """script 要素に埋め込める JSON にする(プロジェクト名はディレクトリ名に由来する外部入力なので < を逃がす)。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render_activity(payload: Mapping[str, object]) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude 活動の再生</title>
<style>{_asset("activity.css")}</style>
</head>
<body>
<canvas id="stars" class="layer" aria-hidden="true"></canvas>
<canvas id="deep" class="layer" aria-hidden="true"></canvas>
<canvas id="sky" class="layer" aria-label="セッションを星、プロジェクトを星雲として、活動の移り変わりを再生する画面"></canvas>
<header class="clock">
<div id="date"></div>
<div id="time"></div>
<div id="skip" hidden>静かな時間を早送り中</div>
</header>
<dl class="counters">
<div><dt>いま動いている</dt><dd id="active">0</dd></div>
<div><dt>プロジェクト</dt><dd id="projects">0</dd></div>
<div><dt>応答の累計</dt><dd id="responses">0</dd></div>
<div><dt>API 換算額の累計</dt><dd id="usd">$0</dd></div>
</dl>
<div id="tip" hidden></div>
<footer class="dock">
<div class="controls">
<button id="play" aria-label="再生"></button>
<select id="range" aria-label="再生する期間"></select>
<select id="speed" aria-label="再生の速さ">
<option value="0.5">0.5 倍速</option>
<option value="1" selected>標準</option>
<option value="2">2 倍速</option>
<option value="4">4 倍速</option>
</select>
<button id="skipIdle" class="text" aria-pressed="false">静かな時間を飛ばす: オフ</button>
<button id="full" class="text" aria-label="全画面">全画面</button>
<button id="fresh" class="text" hidden>新しい記録を読み込む</button>
<span class="legend" aria-label="色の見方">静か<i></i>活発</span>
</div>
<canvas id="timeline" aria-label="日ごとの応答数の時間軸。クリックで移動、ドラッグで範囲を選ぶ"></canvas>
</footer>
<script id="activity-data" type="application/json">{embed_json(payload)}</script>
<script>{_asset("activity.js")}</script>
</body>
</html>
"""
