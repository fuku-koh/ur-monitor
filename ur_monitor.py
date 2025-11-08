# --- 公開ページ → iframe / 直リンク候補の収集 ---

def _collect_candidate_urls(html: str, soup: BeautifulSoup, base_url: str) -> list[str]:
    """外枠ページから実一覧に辿れる候補URLを広めに収集して絶対URLで返す。"""
    urls: set[str] = set()

    # 1) iframe
    for ifr in soup.find_all("iframe", src=True):
        src = (ifr.get("src") or "").strip()
        if not src:
            continue
        if "googletagmanager" in src:  # GTMは除外
            continue
        urls.add(urljoin(base_url, src))

    # 2) aタグ（一覧らしいキーワードを含むもの）
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if any(k in href for k in ("iframe", "embed", "ichiran", "list", "room", "bukken")):
            urls.add(urljoin(base_url, href))

    # 3) script内に直書きされたURL
    for s in soup.find_all("script"):
        txt = s.string or s.get_text() or ""
        if not txt:
            continue
        for m in re.finditer(r"""['"](https?://[^'"]+)['"]""", txt):
            u = m.group(1)
            if "googletagmanager" in u:
                continue
            urls.add(u)
        for m in re.finditer(r"""src\s*=\s*['"]([^'"]+)['"]""", txt):
            urls.add(urljoin(base_url, m.group(1)))

    return list(urls)


def fetch_public_via_embed() -> list[dict]:
    """
    公開ページ(物件トップ: URL)から iframe / 直リンク候補を集め、
    一覧が出るページを叩いて parse_entries() で部屋を取る。
    """
    out: list[dict] = []

    try:
        r0 = requests.get(URL, headers=PUB_HEADERS, timeout=15)
        r0.raise_for_status()
    except Exception as e:
        print(f"[fetch] outer get failed: {e}")
        return out

    _dump("outer.html", r0.text)
    soup = BeautifulSoup(r0.text, "html.parser")

    cands = _collect_candidate_urls(r0.text, soup, URL)
    _dump("candidates.txt", "\n".join(cands))
    print(f"[fetch] outer candidates = {len(cands)}")
    if not cands:
        print("[fetch] listing URL not found on outer page")
        return out

    # 一覧っぽいURLを優先する簡易スコア
    def _score(u: str) -> int:
        u2 = u.lower()
        score = 0
        for kw in ("ichiran", "list", "iframe", "embed", "room", "bukken"):
            if kw in u2:
                score += 1
        return -score  # sort昇順でスコア大が先頭になるよう負に

    tried = 0
    for cand in sorted(cands, key=_score)[:6]:
        tried += 1
        try:
            if "googletagmanager" in cand:
                continue
            r = requests.get(cand, headers=PUB_HEADERS, timeout=15)
            r.raise_for_status()
            _dump(f"try{tried}.url.txt", cand)
            _dump(f"try{tried}.html", r.text)

            items = parse_entries(r.text)
            print(f"[fetch] try#{tried} {cand} -> {len(items)} rooms")
            if items:
                out.extend(items)

                # 簡易ページング（pageIndex/idx/page を順に試す）
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
                break  # 何かしら拾えたら終了
        except Exception as e:
            print(f"[fetch] embed fetch error {cand}: {e}")
            continue

    return out
