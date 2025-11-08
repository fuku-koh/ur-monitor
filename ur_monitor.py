# -*- coding: utf-8 -*-
"""
UR vacancy monitor (GitHub Actions friendly)
- Polls UR endpoints and the public iframe listing; detects diffs.
- Notifies via ChatWork (fallback: print).
"""

import os, json, re, time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlencode, urlparse, parse_qs, urlunparse
import requests
from bs4 import BeautifulSoup

# -------- Config --------
PROP_ID    = os.getenv("PROP_ID", "7080")
STATE_PATH = os.getenv("STATE_FILE", ".state.json")

def to_danchi_code(prop_id: str) -> str:
    # 7080 だけ "7080e"
    return {"7080": "7080e"}.get(prop_id, prop_id)

DANCHI = to_danchi_code(PROP_ID)

# 人間向けURL（通知に載せる）
URL = f"https://www.ur-net.go.jp/chintai/kanto/tokyo/20_{PROP_ID}.html"

# 監視時間（JST 09:30〜19:00）
JST = timezone(timedelta(hours=9))
WINDOW_START = (9, 30)
WINDOW_END   = (18, 59)

# 内部API（通れば使う / ダメなら無視）
API_ENDPOINT = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
API_HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

PUB_HEADERS = {
    "Referer": URL,
    "User-Agent": "ur-monitor/1.0 (+github-actions)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def make_payload(page: int) -> dict:
    """内部API payload。indexNo は 1 始まり。"""
    return {"danchiCd": DANCHI, "indexNo": str(page), "pageSize": "20"}

# -------- Helpers --------
def in_window(now) -> bool:
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0,  microsecond=0)
    end   = now.replace(hour=WINDOW_END[0],   minute=WINDOW_END[1],   second=59, microsecond=0)
    return start <= now <= end

def decode_area(s: str) -> str:
    if not s: return ""
    return (s.replace("㎡", "m²")
             .replace("&sup2;", "²")
             .replace("m&sup2;", "m²")
             .replace("\u33a1", "m²"))

def parse_entries(text: str):
    """JSON/HTML どちらでも部屋リストに正規化。"""
    # JSON 試行
    try:
        data = json.loads(text)
    except Exception:
        data = None

    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for k in ("result", "resultList", "data", "rows"):
            if k in data and isinstance(data[k], list):
                items = data[k]; break

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

    # HTML 断片を緩くパース（table/tr/li など全部テキスト抽出して正規表現）
    soup = BeautifulSoup(text, "html.parser")
    candidates = soup.select("tr, li, .room, .roomCard, .list, .table")
    for c in candidates:
        txt = c.get_text(" ", strip=True)
        if not txt: continue
        name_m   = re.search(r"(\d{2,4})号室", txt)
        layout_m = re.search(r"((?:[1-4]LDK)|(?:[1-4]DK)|(?:[1-4]K)|(?:ワンルーム))", txt)
        area_m   = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|&#13217;)", txt)
        # 「29階／41階」など
        floor_m  = re.search(r"(\d+)\s*階(?:\s*[／/]\s*\d+階?)?", txt)
        rent_m   = re.search(r"(?:賃料|家賃).*?([\d,]+)\s*円|([\d,]+)\s*円", txt)
        comm_m   = re.search(r"共益?費.*?([\d,]+)\s*円", txt)

        if name_m or rent_m:
            rent_val = ""
            if rent_m:
                rent_val = (rent_m.group(1) or rent_m.group(2) or "")
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

# ---- public page via iframe ----
def _set_query(url: str, **kv) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    for k, v in kv.items():
        q[k] = [str(v)]
    new_q = urlencode({k: v[-1] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def fetch_public_via_iframe() -> list[dict]:
    """外側ページ→iframe src→その中身を取りに行く。ページングも軽く対応。"""
    out = []

    r0 = requests.get(URL, headers=PUB_HEADERS, timeout=15)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    tag = soup.find("iframe")
    if not tag or not tag.get("src"):
        print("[fetch] iframe not found on outer page")
        return out

    iframe_url = urljoin(URL, tag.get("src"))
    print(f"[fetch] iframe {iframe_url}")

    # 0,1,2... と pageIndex/idx/page っぽいキーを試す（最大5ページ）
    keys = ("pageIndex", "idx", "page")
    for i in range(5):
        u = iframe_url
        # 既にクエリにキーがあるならそれを書き換え、無ければ pageIndex を付与
        parsed = urlparse(u)
        q = parse_qs(parsed.query)
        target_key = None
        for k in keys:
            if k in q:
                target_key = k; break
        if target_key:
            u = _set_query(u, **{target_key: i})
        else:
            u = _set_query(u, pageIndex=i)

        try:
            ri = requests.get(u, headers=PUB_HEADERS, timeout=15)
            ri.raise_for_status()
            items = parse_entries(ri.text)
            print(f"[fetch] public page i={i} -> {len(items)} rooms")
            if not items:
                # 連続で0になったら終わりでOK
                if i == 0:
                    # 1ページ目から0なら以降も期待薄
                    break
                else:
                    continue
            out.extend(items)
            # 一覧が1ページ構成っぽい場合はそこで終了
            if len(items) < 20:
                break
        except Exception as e:
            print(f"[fetch] iframe page error i={i}: {e}")
            break

    return out

# ---- all fetch paths ----
def fetch_all() -> list[dict]:
    # 1) 内部API（通れば使う）
    try:
        page = 1
        got = []
        while page <= 10:
            payload = make_payload(page)
            r = requests.post(API_ENDPOINT, headers=API_HEADERS, data=payload, timeout=10)
            try:
                j = r.json()
            except Exception:
                print(f"[fetch] api(v1) non-JSON p={page}")
                got = []
                break
            items = parse_entries(json.dumps(j))
            if not items:
                break
            got.extend(items)
            if len(items) < 20:
                break
            page += 1
        if got:
            return got
    except Exception as e:
        print(f"[fetch] api(v1) error: {e}")

    # 2) 旧APIなど別経路（今回はスキップ or 0 件で継続）
    print("[fetch] api(v2-old) -> 0 rooms")

    # 3) public iframe を踏む
    got = fetch_public_via_iframe()
    if got:
        return got

    # 4) 最後の保険：外側HTMLをそのまま（ほぼ0件になる想定）
    try:
        r = requests.get(URL, headers=PUB_HEADERS, timeout=15)
        r.raise_for_status()
        items = parse_entries(r.text)
        print(f"[fetch] html fallback -> {len(items)} rooms")
        return items
    except Exception as e:
        print(f"[fetch] html error: {e}")
        return []

def canonicalize(rooms):
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

def notify(msg: str):
    token   = os.getenv("CHATWORK_TOKEN")
    room_id = os.getenv("CHATWORK_ROOM_ID")
    if not token or not room_id:
        print(msg); return
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
        print(f"notify_failed: {e}"); print(msg)

# -------- Heartbeat state --------
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

# -------- Main --------
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
        print("skip_out_of_window"); return

    prev, is_init = load_state()
    rooms = fetch_all()
    current = set(canonicalize(rooms))

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
