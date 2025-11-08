# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Polls the UR internal endpoint via POST (no JS) and detects diffs.
- Notifies via ChatWork; otherwise prints to logs.
"""

import os, json, re, time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# --- debug dump ---
DEBUG = os.getenv("DEBUG", "0") == "1"
DBG_DIR = os.getenv("DEBUG_DIR", ".debug")

def _dump(name: str, text: str, binary: bool = False):
    if not DEBUG:
        return
    try:
        os.makedirs(DBG_DIR, exist_ok=True)
        path = os.path.join(DBG_DIR, name)
        if binary:
            with open(path, "wb") as f:
                f.write(text)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    except Exception as e:
        print(f"[debug] dump failed {name}: {e}")


# -------- Config --------
PROP_ID    = os.getenv("PROP_ID", "7080")
STATE_PATH = os.getenv("STATE_FILE", ".state.json")

def to_danchi_code(prop_id: str) -> str:
    # 7080 だけ "7080e" になる（他はそのまま）
    return {"7080": "7080e"}.get(prop_id, prop_id)

DANCHI = to_danchi_code(PROP_ID)

# 人間向けURL（通知に載せる）
URL = f"https://www.ur-net.go.jp/chintai/kanto/tokyo/20_{PROP_ID}.html"

# 監視時間（JST 09:30〜19:00）
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)
WINDOW_END   = (18, 59)

# UR の内部API（POST：古いがまだ生きていることがある）
ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
API_HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": URL,  # 物件ページを参照元にする
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# 公開ページ/iframe 取得用
PUB_HEADERS = {
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
    "Referer": "https://www.ur-net.go.jp/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.8",
}

def make_payload(page: int) -> dict:
    """APIが受け付ける最小のペイロード。indexNo は 1 始まり。"""
    return {"danchiCd": DANCHI, "indexNo": str(page), "pageSize": "20"}


# -------- Helpers --------
def in_window(now: datetime) -> bool:
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0,  microsecond=0)
    end   = now.replace(hour=WINDOW_END[0],   minute=WINDOW_END[1],   second=59, microsecond=0)
    return start <= now <= end

def decode_area(s: str) -> str:
    if not s:
        return ""
    return (s.replace("㎡", "m²")
             .replace("&sup2;", "²")
             .replace("m&sup2;", "m²")
             .replace("\u33a1", "m²"))

def parse_entries(text: str):
    """JSON/HTML どちらでも部屋リストに正規化する。"""
    # 1) JSON っぽいならJSON優先で解釈
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass

    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("result", "resultList", "data", "rows"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    rooms = []
    if items is not None:
        for it in items:
            rooms.append({
                "id":        str(it.get("id") or it.get("roomId") or ""),
                "name":      str(it.get("name") or it.get("roomNo") or ""),
                "type":      str(it.get("type") or it.get("layout") or ""),
                "floorspace": decode_area(str(it.get("floorspace") or it.get("area") or "")),
                "floor":     str(it.get("floor") or ""),
                "rent":      str(it.get("rent") or ""),
                "commonfee": str(it.get("commonfee") or it.get("maintenanceFee") or ""),
            })
        return rooms

    # 2) HTML断片をパース（公開ページのテーブル/カードなど）
    soup = BeautifulSoup(text, "html.parser")
    # テーブル行 or カード状の要素などを広めに拾う
    candidates = soup.select("tr, li, .room, .roomCard, .list, .table")
    for c in candidates:
        txt = c.get_text(" ", strip=True)
        if not txt:
            continue
        # 代表的な表記を正規表現で抽出
        name_m   = re.search(r"(\d{2,4})号室", txt)
        layout_m = re.search(r"((?:[1-4]LDK)|(?:[1-4]DK)|(?:[1-4]K)|(?:ワンルーム))", txt)
        area_m   = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|&#13217;)", txt)
        floor_m  = re.search(r"(\d+)\s*階", txt)
        rent_m   = re.search(r"賃料[:：]?\s*([\d,]+)\s*円|([\d,]+)\s*円", txt)
        comm_m   = re.search(r"共益?費[:：]?\s*([\d,]+)\s*円", txt)

        if name_m or rent_m:
            rent_val = ""
            if rent_m:
                rent_val = rent_m.group(1) or rent_m.group(2) or ""
                rent_val = rent_val + "円" if rent_val else ""
            rooms.append({
                "id": "",
                "name": (name_m.group(1) + "号室") if name_m else "",
                "type": layout_m.group(1) if layout_m else "",
                "floorspace": (area_m.group(1) + "㎡") if area_m else "",
                "floor": (floor_m.group(1) + "階") if floor_m else "",
                "rent": rent_val,
                "commonfee": (comm_m.group(1) + "円") if comm_m else "",
            })
    return rooms


# ====== 取得系（API → iframe → HTML保険） ======
def fetch_api_v2_old() -> list[dict]:
    """古い内部API（danchiCd/indexNo）を素直にページング"""
    results = []
    page = 1
    while page <= 10:  # 念のため
        payload = make_payload(page)
        try:
            r = requests.post(ENDPOINT, headers=API_HEADERS, data=payload, timeout=12)
            # 稀に "200 だが HTML" のようなケースがあるのでいったん text から判定
            try:
                r.raise_for_status()
                items = parse_entries(r.text)
            except Exception:
                print(f"[fetch] api(v2-old) HTTP{r.status_code} text_len={len(r.text)}")
                break
        except Exception as e:
            print(f"[fetch] api(v2-old) error p={page}: {e}")
            break

        if not items:
            break
        results.extend(items)
        if len(items) < 20:  # 1ページ満杯でなければ終端
            break
        page += 1

    print(f"[fetch] api(v2-old) -> {len(results)} rooms")
    return results


def _set_query(url: str, **kv) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    for k, v in kv.items():
        q[k] = [str(v)]
    new_q = urlencode({k: v[-1] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))


def _pick_listing_iframe(soup: BeautifulSoup, base_url: str) -> str | None:
    """
    外側ページに複数 iframe があるので、物件リストっぽいものだけ選ぶ。
    - googletagmanager.com は除外（今回の原因）
    - ur-net.go.jp かつ /chintai/ を含む
    - list/search/result/room/bukken などを含むほどスコア加点
    """
    cands = []
    for f in soup.find_all("iframe"):
        src = (f.get("src") or "").strip()
        if not src:
            continue
        absu = urljoin(base_url, src)
        pu = urlparse(absu)
        host, path = pu.netloc, pu.path

        # GTM などは除外
        if "googletagmanager.com" in host:
            continue
        # ur-net配下かつ /chintai/
        if "ur-net.go.jp" in host and "/chintai/" in path:
            score = 0
            if any(k in path for k in ("list", "search", "result", "room", "bukken")):
                score += 2
            if pu.query:
                score += 1
            cands.append((score, absu))

    if not cands:
        return None
    cands.sort(reverse=True)  # スコア高い順
    return cands[0][1]
def fetch_public_via_embed() -> list[dict]:
    out: list[dict] = []

    r0 = requests.get(URL, headers=PUB_HEADERS, timeout=15)
    r0.raise_for_status()
    _dump("outer.html", r0.text)  # ←外側ページ保存
    soup = BeautifulSoup(r0.text, "html.parser")

    cands = _collect_candidate_urls(r0.text, soup, URL)
    _dump("candidates.txt", "\n".join(cands))  # ←候補URL一覧
    print(f"[fetch] outer candidates = {len(cands)}")
    if not cands:
        print("[fetch] listing URL not found on outer page")
        return out

    tried = 0
    for cand in cands[:6]:
        tried += 1
        try:
            r = requests.get(cand, headers=PUB_HEADERS, timeout=15)
            r.raise_for_status()
            _dump(f"try{tried}.url.txt", cand)
            _dump(f"try{tried}.html", r.text)   # ←各候補の生HTML
            items = parse_entries(r.text)
            print(f"[fetch] try#{tried} {cand} -> {len(items)} rooms")
            if items:
                out.extend(items)
                # 簡易ページング
                for key in ("pageIndex", "idx", "page"):
                    for i in range(1, 5):
                        u = _set_query(cand, **{key: i})
                        ri = requests.get(u, headers=PUB_HEADERS, timeout=15)
                        if ri.status_code != 200:
                            break
                        _dump(f"try{tried}.{key}={i}.html", ri.text)
                        more = parse_entries(ri.text)
                        print(f"[fetch] {key}={i} -> {len(more)} rooms")
                        if not more:
                            break
                        out.extend(more)
                        if len(more) < 20:
                            break
                break
        except Exception as e:
            print(f"[fetch] embed fetch error {cand}: {e}")
            continue

    return out

def fetch_html_fallback() -> list[dict]:
    """外側ページの HTML を直接パース（最終保険）。"""
    try:
        r = requests.get(URL, headers=PUB_HEADERS, timeout=15)
        r.raise_for_status()
        rooms = parse_entries(r.text)
        print(f"[fetch] html fallback -> {len(rooms)} rooms")
        return rooms
    except Exception as e:
        print(f"[fetch] html fallback error: {e}")
        return []


def fetch_all() -> list[dict]:
    """API → iframe → HTML保険 の順で拾う。"""
    # 1) 旧API
    rooms = fetch_api_v2_old()
    if rooms:
        return rooms

    # 2) 正しい listing iframe を選別して取得（今回の修正）
    rooms = fetch_public_via_embed()
    if rooms:
        return rooms

    # 3) 保険
    rooms = fetch_html_fallback()
    return rooms


# ====== 差分比較 & 状態 ======
def canonicalize(rooms):
    """差分比較用に正規化してタプル集合へ。"""
    canon = []
    for r in rooms:
        def clean(s):
            s = decode_area(s or "")
            return re.sub(r"[,\s]", "", s)
        canon.append((
            clean(r.get("name")),
            clean(r.get("type")),
            clean(r.get("floorspace")),
            clean(r.get("floor")),
            clean(r.get("rent")),
            clean(r.get("commonfee")),
        ))
    return sorted(set(canon))

def load_state():
    if not os.path.exists(STATE_PATH):
        return set(), True
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        rooms = data["rooms"] if isinstance(data, dict) and "rooms" in data else data
        if not isinstance(rooms, list):
            return set(), True
        return set(tuple(x) for x in rooms), False
    except Exception:
        return set(), True

def save_state(s: set):
    payload = {"rooms": sorted(list(s))}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ====== 通知 ======
def notify(msg: str):
    token   = os.getenv("CHATWORK_TOKEN")
    room_id = os.getenv("CHATWORK_ROOM_ID")
    if not token or not room_id:
        print(msg)
        return
    body = msg if len(msg) <= 9000 else (msg[:9000] + "\n…(truncated)")
    try:
        r = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
            headers={"X-ChatWorkToken": token},
            data={"body": f"[info][title]UR監視[/title]{body}[/info]"},
            timeout=15,
        )
        print(f"chatwork_status={r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"notify_failed: {e}")
        print(msg)


# ====== Heartbeat ======
HB_FILE = ".hb-date.txt"

def _hb_sent_today(now) -> bool:
    try:
        with open(HB_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() == now.strftime("%Y%m%d")
    except Exception:
        return False

def _hb_mark(now) -> None:
    with open(HB_FILE, "w", encoding="utf-8") as f:
        f.write(now.strftime("%Y%m%d"))


# ====== Main ======
def main():
    now = datetime.now(JST)

    # 9:30〜9:39 の間、かつ今日まだ送っていなければ一度だけ送る
    if now.hour == 9 and 30 <= now.minute < 40 and not _hb_sent_today(now):
        try:
            notify(f"[UR監視 起動ハートビート] JST {now:%H:%M} / {URL}")
            _hb_mark(now)
        except Exception:
            pass

    if not in_window(now):
        print("skip_out_of_window")
        return

    prev, is_init = load_state()
    rooms = fetch_all()
    current = set(canonicalize(rooms))

    # スナップショット
    try:
        _dump("rooms.json", json.dumps(rooms, ensure_ascii=False, indent=2))
        _dump("canon.json", json.dumps(sorted(list(current)), ensure_ascii=False, indent=2))
    except Exception:
        pass

    if is_init:
        notify(f"[UR監視 初期化] 件数: {len(current)}\n{URL}")
    elif current != prev:
        added   = current - prev
        removed = prev - current
        lines = []
        if added:
            lines.append("+ " + " / ".join([a[0] for a in sorted(added)][:5]))
        if removed:
            lines.append("− " + " / ".join([a[0] for a in sorted(removed)][:5]))
        notify("【UR監視 変化あり】\n" + ("\n".join(lines) if lines else "差分あり") + f"\n{URL}")
    else:
        print("no_change")

    save_state(current)


if __name__ == "__main__":
    main()
