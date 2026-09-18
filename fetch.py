#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Metacritic 新游简报：抓近 30 天已发售游戏 + 未来未发售游戏，生成 index.html"""
import base64, json, shutil, subprocess, tempfile, time, urllib.error, urllib.request, urllib.parse, datetime as dt, pathlib, concurrent.futures as cf

API   = "https://backend.metacritic.com"
KEY   = "1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u"
UA    = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
DAYS  = 30          # 抓取窗口：近 30 天
UPCOMING_N = 10     # 未发售展示数量
# 收录规则：满足任一即进主表，都不满足的进折叠「观察区」（平台不参与筛选）
MIN_REVIEWS = 30    # R1 媒体评测 ≥ 30 篇：大作保险，不看分数
MIN_USER    = 8.0   # R2 玩家分 ≥ 8.0 且评分人数 ≥ MIN_USER_N：口碑救场，防新作被篇数卡掉
MIN_USER_N  = 10    #    ↑ 人数门槛必须有，否则「1 人评 9.3 分」的小作品会刷屏
MIN_META    = 90    # R3 Metascore ≥ 90（= 用户定的「媒体分数 9 分以上」）：少数媒体一致封神，玩家还没来得及评
#   ↑ 不能放宽：86 分 Welcome to Elderfield 只有 10 篇媒体、4 个玩家评分，放宽了就是给小作品开后门
# 观察区地板：媒体和玩家「双低」的（两边都没攒起信号）直接不展示，避免个位数样本刷屏。
# 只剔除两边同时低的；媒体少但玩家多、或玩家少但媒体多，都还有看头，保留。
WATCH_MIN_REVIEWS = 10   # 媒体 ≥ 10 篇（主表门槛的 1/3，算有势头）
WATCH_MIN_USER_N  = 5    # 或玩家 ≥ 5 人评分

# 自然年归档：往年的游戏不再占主表，用更严的标准筛出「年度精品」折叠保留
# 2026 全年实测（337 款有媒体分）：媒体≥50 篇 51 款 → +M≥80 33 款 → +玩家≥8.5 仅 8 款。
# 全用「且」会把 Forza Horizon 6(M90)、Mina the Hollower(M90)、Mewgenics(M88) 这类媒体封神
# 但玩家分只有 8.1~8.3 的大作挡掉（玩家分天然比媒体分低），所以拆成两条通道取并集：
#   A 全能：媒体≥50 篇 且 M≥80 且 玩家≥8.5 且 人数≥30   → 7 款
#   B 封神：媒体≥50 篇 且 M≥88 且 人数≥30（不看玩家分）→ 补回 3 款
# 人数门槛必须有，否则 19 人评 8.5 分的小作品会混进年度精品。
YEAR_MIN_REVIEWS = 50
YEAR_MIN_META    = 80
YEAR_MIN_USER    = 8.5
YEAR_MIN_USER_N  = 30
YEAR_TOP_META    = 88     # B 通道；不想启用就改成 999
# 平台 slug -> 展示名（只用于展示，不做筛选）
PLATS = {"pc": "PC", "playstation-5": "PS5", "playstation-4": "PS4", "xbox-series-x": "XSX",
         "xbox-one": "XB1", "nintendo-switch": "NS", "nintendo-switch-2": "NS2",
         "ios-iphoneipad": "iOS", "stadia": "Stadia"}
# 固定 5 家媒体（publicationSlug -> 展示名）
# 主机场景实测覆盖率（8 款主机游戏全量评论，2026-09-14，只算给了数字分的）：
#   Push Square 5/8 · IGN 4/8 · Game Informer 4/8 · Eurogamer 3/8 ·
#   GameSpot 2/8 · Nintendo Life 2/8（两款 Switch 2 全覆盖）· GamesRadar+ 2/8 · EDGE 2/8 ·
#   PC Gamer 0/8（只评 PC）· Polygon 0/8（有条目但从不给数字分）
# 想换媒体改下面的 slug 即可（slug 都是实测拿到的，写错就是整列空白）：
#   push-square「Push Square」· game-informer「Game Informer」· nintendo-life「Nintendo Life」·
#   gamesradar+「GamesRadar+」· edge-magazine「EDGE」※注意 gamesradar 带加号、edge 是 edge-magazine
OUTLETS = {"ign": "IGN", "gamespot": "GameSpot", "eurogamer": "Eurogamer",
           "push-square": "Push Square", "nintendo-life": "Nintendo Life"}
# 手机端 5 家要挤在同一行，全称（PUSH SQUARE / NINTENDO LIFE）放不下会换行，用业内通用简称
OUTLETS_ABBR = {"ign": "IGN", "gamespot": "GS", "eurogamer": "EG",
                "push-square": "PS", "nintendo-life": "NL"}
ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data" / "games.json"
COVERS = ROOT / "data" / "covers.json"     # 封面 base64 缓存，失败时可复用上次
DETAIL = ROOT / "data" / "detail.json"     # 详情缓存（公司/平台基本不变，避免重复请求）
REVIEWS = ROOT / "data" / "reviews.json"   # 媒体评分缓存（评分发布后不再变，避免重复翻页）
UCNT = ROOT / "data" / "ucnt.json"         # 玩家评分人数缓存（年度筛选要用，变化慢，7 天一刷）
REVIEW_TTL = 45                            # 缓存有效期（天）> 30 天窗口，出窗口即淘汰
UCNT_TTL = 7

MISS = []          # 抓取失败项，结束时汇总提示

def get(path, **params):
    params["apiKey"] = KEY
    url = "%s/%s?%s" % (API, path, urllib.parse.urlencode(params))
    for i in range(3):                                  # 网络抖动重试，4xx 直接放弃
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 or i == 2:
                raise
        except Exception:
            if i == 2:
                raise
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("unreachable")

def finder(date_min, date_max, sort, pages=1, limit=50):
    out = []
    for p in range(pages):
        d = get("finder/metacritic/web", productType="games", releaseDateMin=date_min,
                releaseDateMax=date_max, sortBy=sort, offset=p * limit, limit=limit)["data"]
        out += d["items"]
        if len(d["items"]) < limit:
            break
    return out

_DETAIL = json.loads(DETAIL.read_text()) if DETAIL.exists() else {}
_REVIEWS = json.loads(REVIEWS.read_text()) if REVIEWS.exists() else {}
_UCNT = json.loads(UCNT.read_text()) if UCNT.exists() else {}

def year_file(y):
    return ROOT / "data" / ("year_%d.json" % y)

def year_best(year, today):
    """某自然年的年度精品：A 全能 / B 封神 两条通道取并集。
    往年数据已冻结（不再联网），只有当年还会继续更新。"""
    f = year_file(year)
    if not f.exists():
        return []
    out = []
    for g in json.loads(f.read_text()):
        cnt, meta = g.get("meta_count") or 0, g.get("meta")
        user, n = g.get("user"), g.get("user_count") or 0
        if cnt < YEAR_MIN_REVIEWS or n < YEAR_MIN_USER_N:
            continue
        # 先判 A（全能，信息更强：媒体和玩家都过线），否则 B（媒体封神，玩家分没跟上）。
        # 顺序反了会把 RE Requiem（M89 / 玩家 9.3）这类双高作品错标成只看媒体分。
        a = meta is not None and meta >= YEAR_MIN_META and user is not None and user >= YEAR_MIN_USER
        b = meta is not None and meta >= YEAR_TOP_META
        if a or b:
            out.append(g)
    out.sort(key=lambda g: (-(g.get("meta") or 0), -(g.get("user") or 0)))
    return out

def detail(slug):
    """游戏详情（公司 + 平台）。命中缓存就不再联网；失败不落盘，下次自动重试"""
    if slug not in _DETAIL:
        try:
            it = get("composer/metacritic/pages/games/%s/web" % slug)["components"][0]["data"]["item"]
            _DETAIL[slug] = {
                "companies": (it.get("production") or {}).get("companies") or [],
                # 每个平台各自的 Metascore 与评测篇数（同一游戏不同平台分数可能不同）
                "platforms": [{"slug": p.get("slug"), "name": p.get("name"),
                               "date": p.get("releaseDate"),
                               "score": (p.get("criticScoreSummary") or {}).get("score"),
                               "count": (p.get("criticScoreSummary") or {}).get("reviewCount")}
                              for p in (it.get("platforms") or [])]}
        except Exception as e:
            MISS.append("%s#detail(%s)" % (slug, getattr(e, "code", e)))
            return {"companies": [], "platforms": []}
    return _DETAIL[slug]

def save_detail(keep):
    """只留窗口内 + 年度池的，否则孤儿会一直堆积（公司/平台虽然不变，但滚出窗口就再也用不上）"""
    DETAIL.write_text(json.dumps({k: v for k, v in _DETAIL.items()
                                  if k in keep and (v.get("companies") or v.get("platforms"))},
                                 ensure_ascii=False))

def companies(slug):
    """制作 / 发行公司：production.companies 里按 typeName 区分"""
    dev = pub = ""
    for c in detail(slug).get("companies") or []:
        if c.get("typeName") == "Developer" and not dev:
            dev = c.get("name") or ""
        elif c.get("typeName") == "Publisher" and not pub:
            pub = c.get("name") or ""
    return dev, pub

def plats_of(slug):
    """全部平台：缩写 + 该平台自己的媒体分/篇数/发售日（仅展示用）"""
    return [{"n": PLATS.get(p["slug"], p.get("name") or p["slug"]),
             "s": p.get("score"), "c": p.get("count") or 0, "d": p.get("date")}
            for p in detail(slug).get("platforms") or []]

def user_count(slug):
    """玩家评分人数：只有标题级（不带 platform）接口给得出来，列表接口只有分数"""
    c = _UCNT.get(slug)
    if c and (dt.date.today() - dt.date.fromisoformat(c["d"])).days < UCNT_TTL:
        return c["n"]
    try:
        n = get("reviews/metacritic/user/games/%s/web" % slug, offset=0, limit=1)["data"].get("totalResults") or 0
    except Exception as e:
        MISS.append("%s#user(%s)" % (slug, getattr(e, "code", e)))
        return c["n"] if c else 0
    _UCNT[slug] = {"n": n, "d": str(dt.date.today())}
    return n

def save_ucnt(keep):
    UCNT.write_text(json.dumps({k: v for k, v in _UCNT.items() if k in keep}, ensure_ascii=False))

def qualify(item, ucnt):
    """三通道收录判定：True 进主表，False 进观察区"""
    s = item.get("criticScoreSummary") or {}
    cnt, meta = s.get("reviewCount") or 0, s.get("score")
    user = (item.get("userScore") or {}).get("score")
    return cnt >= MIN_REVIEWS \
        or (user is not None and user >= MIN_USER and ucnt >= MIN_USER_N) \
        or (meta is not None and meta >= MIN_META)

def shrink(tmp):
    """压缩封面到 150px：macOS 用自带的 sips，Linux/CI 上用 Pillow，都没有就原图直出"""
    if shutil.which("sips"):
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "55",
                        "-Z", "150", str(tmp), "--out", str(tmp)],
                       capture_output=True, timeout=20)          # 原图平均 250KB -> 约 9KB
        return
    try:
        from PIL import Image
        with Image.open(tmp) as im:
            im.thumbnail((150, 150))
            im.convert("RGB").save(tmp, "JPEG", quality=55, optimize=True)
    except Exception:
        pass                                     # 没装 Pillow 就原图直出，只是页面会变大

def fetch_cover(url):
    """抓封面并压缩到 150px 后转 base64，页面离线也能看且不臃肿"""
    if not url:
        return ""
    tmp = pathlib.Path(tempfile.gettempdir()) / ("mc%s.jpg" % abs(hash(url)))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
            tmp.write_bytes(r.read())
        shrink(tmp)
        data = tmp.read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode() if len(data) < 80000 else ""
    except Exception:
        return ""
    finally:
        tmp.unlink(missing_ok=True)

def enrich(item, ucnt, with_scores=True):
    """补平台、公司、5 家媒体评分。分数一律取标题级（= 官网游戏主页口径，与 Metascore 对得上）"""
    slug = item["slug"]
    cs = item.get("criticScoreSummary") or {}
    g = {
        "slug": slug,
        "title": item["title"],
        "date": item.get("releaseDate") or "",
        "url": "https://www.metacritic.com/game/%s/" % slug,
        "img": "https://www.metacritic.com/a/img/catalog%s" % item["image"]["bucketPath"]
               if item.get("image") and item["image"].get("bucketPath") else "",
        "plats": [], "dev": "", "pub": "",
        "meta": cs.get("score"),
        "meta_count": cs.get("reviewCount") or 0,
        "user": (item.get("userScore") or {}).get("score"),
        "user_count": ucnt,
        "scores": {},
    }
    g["plats"] = plats_of(slug)
    g["dev"], g["pub"] = companies(slug)
    if not with_scores:
        return g

    def collect(cap=200):
        """评论接口每页固定 10 条，翻页直到集齐 5 家或取完"""
        try:
            path = "reviews/metacritic/critic/games/%s/web" % slug
            for off in range(0, cap, 10):
                d = get(path, offset=off, limit=10)["data"]
                for r in d["items"]:
                    name = OUTLETS.get(r.get("publicationSlug"))
                    if name and r.get("score") is not None and name not in g["scores"]:
                        g["scores"][name] = r["score"]
                if len(g["scores"]) == len(OUTLETS) or not d["items"] \
                   or off + len(d["items"]) >= d["totalResults"]:
                    break
        except Exception as e:
            MISS.append("%s(%s)" % (slug, getattr(e, "code", e)))

    cache = _REVIEWS.get(slug)
    if cache and (dt.date.today() - dt.date.fromisoformat(cache["ts"])).days < REVIEW_TTL:
        g["scores"] = dict(cache.get("scores") or {})          # 媒体评分不再变，直接复用
    else:
        collect()
        _REVIEWS[slug] = {"scores": g["scores"], "ts": str(dt.date.today())}
    return g

def main():
    today = dt.date.today()
    d0, d1 = today - dt.timedelta(days=DAYS), today
    if not DATA.parent.is_dir():            # 所有缓存都在这个目录，开头建一次就够
        DATA.parent.mkdir(parents=True)

    # 1) 近 30 天已发售：媒体评分榜（主体）+ 最新发售榜（捞还没攒够媒体分的）
    pool = {}
    for it in finder(d0, d1, "-metaScore") + finder(d0, d1, "-releaseDate", pages=3):
        s = it.get("criticScoreSummary") or {}
        u = it.get("userScore") or {}
        if not s.get("score") and not u.get("score"):
            continue
        if it["releaseDate"] and it["releaseDate"] < str(d0):
            continue
        pool.setdefault(it["slug"], it)

    # 增量：只要近 30 天这批游戏的关键数字没变，就不再往下抓，也不重写页面
    # 用 list 不用 tuple：json 读回来 tuple 会变 list，比较就永远不相等了
    sig = {s: [(i.get("criticScoreSummary") or {}).get("score"),
               (i.get("criticScoreSummary") or {}).get("reviewCount"),
               (i.get("userScore") or {}).get("score")] for s, i in pool.items()}
    prev = json.loads(DATA.read_text()) if DATA.exists() else {}
    # 注意：不能用 released+watch 的数量做比对，观察区会丢掉双低的，对不上
    if sig == prev.get("sig") and len(pool) == prev.get("total"):
        print("无变化（近 30 天 %d 款，分数与上次一致），跳过" % len(pool))
        return

    # 三通道判定：先并发预热详情 + 取玩家评分人数（判定必需，且只有标题级接口给得出）
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(detail, pool.keys()))
        ucnts = dict(zip(pool.keys(), ex.map(user_count, pool.keys())))
    keep, watch, drop = [], [], 0
    for slug, it in pool.items():
        u = ucnts.get(slug) or 0
        if qualify(it, u):
            keep.append((it, u))
            continue
        # 双低（媒体和玩家都没攒起信号）直接丢弃，不上页面
        if ((it.get("criticScoreSummary") or {}).get("reviewCount") or 0) < WATCH_MIN_REVIEWS \
                and u < WATCH_MIN_USER_N:
            drop += 1
            continue
        watch.append((it, u))
    print("近 30 天 %d 款 -> 主表 %d 款，观察区 %d 款，双低丢弃 %d 款，拉取详情…"
          % (len(pool), len(keep), len(watch), drop))

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        games = list(ex.map(lambda t: enrich(t[0], t[1]), keep))
        watch_g = list(ex.map(lambda t: enrich(t[0], t[1], with_scores=False), watch))
    watch_g.sort(key=lambda g: (-(g["user"] or 0), -(g["meta"] or 0)))

    # 2) 自然年归档：当年池用全年列表维护，往年的只读不写（数据已冻结）
    def build_year(year):
        """只保留「有机会进年度精品」的候选（媒体≥50 篇 且 M≥下限），避免维护几百款垃圾数据"""
        yf = year_file(year)
        oldy = {g["slug"]: g for g in (json.loads(yf.read_text()) if yf.exists() else [])}
        if year < today.year:                       # 往年：不再联网，直接用冻结的数据
            return list(oldy.values())
        cand = {}
        for it in finder("%d-01-01" % year, "%d-12-31" % year, "-metaScore", pages=20):
            cs = it.get("criticScoreSummary") or {}
            if (cs.get("reviewCount") or 0) < YEAR_MIN_REVIEWS:
                continue
            if (cs.get("score") or 0) < min(YEAR_MIN_META, YEAR_TOP_META):
                continue
            cand[it["slug"]] = it
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(detail, cand.keys()))
            list(ex.map(user_count, cand.keys()))
        out = dict(oldy)
        for slug, it in cand.items():
            out[slug] = enrich(it, user_count(slug), with_scores=False)
        yf.write_text(json.dumps(list(out.values()), ensure_ascii=False))
        print("%d 年度池 %d 款候选" % (year, len(out)))
        return list(out.values())

    year_pool = build_year(today.year)          # 候选池整体要留住，否则每期都得重拉详情
    years = sorted({int(f.stem.split("_")[1]) for f in ROOT.glob("data/year_*.json")} | {today.year})
    annual = [{"year": y, "games": year_best(y, today)} for y in years]
    annual = [a for a in annual if a["games"]]

    # 3) 未发售：官方「期待榜」优先，不足 N 个用临近发售日补齐
    def pack(items):
        out = []
        for it in items:
            dev, pub = companies(it["slug"])
            out.append({"slug": it["slug"], "title": it["title"], "date": it.get("releaseDate") or "",
                        "url": "https://www.metacritic.com/game/%s/" % it["slug"],
                        "meta": (it.get("criticScoreSummary") or {}).get("score"),
                        "plats": plats_of(it["slug"]), "dev": dev, "pub": pub,
                        "img": "https://www.metacritic.com/a/img/catalog%s" % it["image"]["bucketPath"]
                               if it.get("image") and it["image"].get("bucketPath") else ""})
        return out

    tomorrow = today + dt.timedelta(days=1)
    hot = pack(finder(tomorrow, today + dt.timedelta(days=365), "-metaScore", pages=4))
    hot.sort(key=lambda x: x["date"] or "9999")
    up = hot[:UPCOMING_N]
    if len(up) < UPCOMING_N:                      # 期待榜不够，用临近发售日补齐
        near = pack(finder(tomorrow, today + dt.timedelta(days=120), "releaseDate", pages=2))
        near.sort(key=lambda x: x["date"] or "9999")
        known = {x["title"] for x in up}
        up += [x for x in near if x["title"] not in known][:UPCOMING_N - len(up)]

    # 4) 抓封面并内嵌（离线可看），缓存到 data/covers.json
    targets = {g["slug"]: g["img"] for g in games + watch_g if g["img"]}
    targets.update({u["slug"]: u["img"] for u in up if u["img"]})
    for a in annual:
        targets.update({g["slug"]: g["img"] for g in a["games"] if g["img"]})
    old_covers = json.loads(COVERS.read_text()) if COVERS.exists() else {}
    need = {k: v for k, v in targets.items() if k not in old_covers}      # 只补缺的
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        got = dict(zip(need, ex.map(fetch_cover, need.values()))) if need else {}
    covers = {k: v for k, v in {**old_covers, **got}.items() if k in targets and v}
    # 只留在窗口内的，自动淘汰；观察区也留着，下期还要靠人数判双低
    keep = {g["slug"] for g in games} | {g["slug"] for g in watch_g} | {u["slug"] for u in up}
    keep |= {g["slug"] for a in annual for g in a["games"]} | {g["slug"] for g in year_pool}
    COVERS.write_text(json.dumps(covers, ensure_ascii=False))
    save_detail(keep)
    REVIEWS.write_text(json.dumps({k: v for k, v in _REVIEWS.items() if k in keep},
                                  ensure_ascii=False))
    save_ucnt(keep)
    print("封面 %d/%d 张（新抓 %d）" % (len(covers), len(targets), len(got)))

    games.sort(key=lambda g: (-(g["user"] or 0), -(g["meta"] or 0)))
    payload = {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "window": [str(d0), str(d1)], "total": len(pool),
               "released": games, "watch": watch_g, "upcoming": up, "annual": annual,
               "sig": sig, "covers": covers}
    # 快照不存封面（体积大），封面单独放 covers.json
    DATA.write_text(json.dumps({k: v for k, v in payload.items() if k != "covers"},
                               ensure_ascii=False, indent=1))
    (ROOT / "index.html").write_text(render(payload), encoding="utf-8")
    print("主表 %d 款 / 观察区 %d 款 / 年度精品 %s / 未发售 %d 款 -> index.html"
          % (len(games), len(watch_g),
             " ".join("%d年%d款" % (a["year"], len(a["games"])) for a in annual) or "无", len(up)))
    if MISS:
        print("注意：%d 项拉取失败（已留空）：%s" % (len(MISS), ", ".join(MISS[:8])))
    for name in OUTLETS.values():
        if not any(name in g["scores"] for g in games):
            print("提示：%s 本期 0 命中，该媒体可能不提供数字评分" % name)

def render(p):
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>新游评分简报</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#262d36;--tx:#e6edf3;--dim:#8b949e;--g:#3fb950;--y:#d29922;--r:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"PingFang SC",Helvetica,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--dim);font-size:12px;margin-bottom:18px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px}
.stat b{display:block;font-size:20px}.stat span{color:var(--dim);font-size:11px}
.bar{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.bar button{background:var(--card);color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px}
.bar button.on{color:#fff;border-color:#3b82f6;background:#1f3350}
table{width:100%;border-collapse:separate;border-spacing:0 8px}
td{background:var(--card);border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:12px 10px;vertical-align:middle}
td:first-child{border-left:1px solid var(--line);border-radius:8px 0 0 8px;width:64px;padding:8px 4px 8px 8px}
td:last-child{border-right:1px solid var(--line);border-radius:0 8px 8px 0}
.covbox{width:56px;height:84px;background:#1c2128;border-radius:4px;overflow:hidden;flex:0 0 auto}
img.cov{width:100%;height:100%;object-fit:cover;display:block}
.covph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;
  background:#21262d;color:#7d8590;font-size:17px;font-weight:600}
.t{font-weight:600;font-size:15px;text-decoration:none;color:var(--tx)}.t:hover{color:#58a6ff}
.meta2{color:var(--dim);font-size:11px;margin-top:3px}
.dev{color:#8b9bb4;font-size:11px;margin-top:2px}
.pf{background:#1f2a37;border:1px solid #2f3d4d;border-radius:4px;padding:1px 6px;color:#a9c0d8;font-size:10px;margin-right:3px;cursor:help}
.sc{font-size:26px;font-weight:700;text-align:center;min-width:64px}
.sc small{display:block;font-size:10px;color:var(--dim);font-weight:400}
.g{color:var(--g)}.y{color:var(--y)}.r{color:var(--r)}.n{color:var(--dim)}
.out{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.o{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:5px 8px;text-align:center;min-width:66px}
.o i{display:block;font-style:normal;font-size:9px;color:var(--dim);letter-spacing:.3px}
.o i .ab{display:none}
.o b{font-size:15px}
details{margin:26px 0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:0 14px}
.soon{color:#58a6ff}
summary{cursor:pointer;padding:14px 0;font-weight:600;list-style:none}
summary::-webkit-details-marker{display:none}summary:before{content:"▸ ";color:var(--dim)}
details[open] summary:before{content:"▾ "}
.up{display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.up img{width:38px;height:57px;object-fit:cover;border-radius:4px}
.up .d{margin-left:auto;color:var(--dim);font-size:12px}
footer{margin-top:28px;color:var(--dim);font-size:11px;text-align:center}
@media (max-width:720px){
  .wrap{padding:16px 12px 40px}
  h1{font-size:19px}
  .sub{font-size:11px;margin-bottom:14px}
  .stats{gap:8px}
  .stat{flex:1 1 44%;padding:8px 12px}
  .stat b{font-size:17px}
  .bar{gap:5px}
  .bar button{padding:6px 9px;font-size:11px}
  table,tbody,tr,td{display:block}
  td{width:auto!important;min-width:0!important;padding:0!important;border:none!important;border-radius:0!important;background:none}
  tbody tr{display:grid;grid-template-columns:56px 1fr 1fr;align-items:center;gap:9px 10px;
    background:var(--card);border:1px solid var(--line);border-radius:8px;margin-bottom:10px;padding:10px}
  tbody tr>td:nth-child(1){grid-area:1/1/2/2}
  tbody tr>td:nth-child(2){grid-area:1/2/2/4}
  tbody tr>td:nth-child(3){grid-area:2/2/3/3}
  tbody tr>td:nth-child(4){grid-area:2/3/3/4}
  tbody tr>td:nth-child(5){grid-area:3/1/4/4}
  .sc{font-size:22px;min-width:0}
  .sc small{font-size:9px}
  .out{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
  .o{min-width:0;padding:4px 2px}
  .o i .fl{display:none}
  .o i .ab{display:inline}
  .o i{font-size:9px;letter-spacing:0}
  .o b{font-size:14px}
  .t{font-size:14px}
  .t,.meta2,.dev{overflow-wrap:break-word}
  .up{flex-wrap:wrap;gap:8px 10px;align-items:flex-start}
  .up>div{min-width:0;flex:1 1 auto}
  .up .covbox{flex:0 0 38px}
  .up .d{margin-left:0;flex:0 0 100%;padding-left:50px;font-size:11px;line-height:1.5}
  details{margin:18px 0;padding:0 12px}
  summary{font-size:13px}
}
</style></head><body><div class="wrap">
<h1>新游评分简报</h1>
<div class="sub">近 30 天共 __TOTAL__ 款新游 → 收录 <b id="n"></b> 款：媒体评测 ≥ __MIN__ 篇，或玩家分 ≥ __USER__ 且评分人数 ≥ __USERN__ 人，或 Metascore ≥ __META__ · 更新 <span id="upd"></span></div>
<div class="stats" id="stats"></div>
<details><summary><span class="soon">未发售</span>（<span id="upn"></span>）</summary><div id="ups"></div></details>
<div class="bar">
<button data-k="user" class="on">按玩家评分</button>
<button data-k="meta">按媒体均分</button>
<button data-k="date">按发售日</button>
<button data-k="count">按玩家参与数</button>
<button data-k="gap">按口碑分歧</button>
</div>
<table><tbody id="rows"></tbody></table>
<details><summary>观察区（<span id="wn"></span>）· 未达主表门槛但已有信号，双低样本（媒体与玩家都极少）已剔除</summary><div id="watch"></div></details>
<div id="annual"></div>
<footer id="ft"></footer>
</div>
<script>window.onerror = function (m) {
  document.body.insertAdjacentHTML('afterbegin', '<div style="margin:0;padding:14px 20px;background:#3b1d1d;'
    + 'color:#f85149;font:13px/1.5 sans-serif">页面脚本出错：' + m + '</div>');
};</script>
<script>
const D = __DATA__;
document.getElementById('upd').textContent = D.updated;
document.getElementById('n').textContent = D.released.length;
document.getElementById('upn').textContent = D.upcoming.length + ' 款待发售';
document.getElementById('wn').textContent = (D.watch || []).length + ' 款';
const platTags = g => (g.plats || []).map(p =>
  `<span class="pf" title="${p.s != null ? p.n + ' 媒体 ' + p.s + ' / ' + p.c + ' 篇' : p.n + ' 暂无媒体分'}">${p.n}</span>`
).join('');
const col = v => v == null ? 'n' : v >= 8 ? 'g' : v >= 5 ? 'y' : 'r';
const col100 = v => v == null ? 'n' : v >= 75 ? 'g' : v >= 50 ? 'y' : 'r';
const fmt = v => v == null ? '—' : (Math.round(v * 10) / 10).toFixed(1);
const gapOf = g => (g.user != null && g.meta != null) ? g.user * 10 - g.meta : null;
// 封面统一走这里：没图的（未发售常见）显示游戏名首字占位，不留突兀的灰块
const covBox = (g, w, h) => {
  const s = D.covers[g.slug] || g.img || '';
  return `<div class="covbox" style="width:${w}px;height:${h}px">`
    + (s ? `<img class="cov" src="${s}" onerror="this.style.display='none'">`
         : `<div class="covph">${(g.title || '?').trim().charAt(0)}</div>`) + '</div>';
};

document.getElementById('stats').innerHTML = [
  ['已收录', D.released.length + ' 款'], ['统计区间', D.window[0] + ' ~ ' + D.window[1]]
].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');

// 全站唯一的分数文案：媒体（分 + 篇数）· 玩家（分 + 人数）。主表用大号数字，折叠区用这一行，口径一致
function scoreLine(g) {
  const thin = g.user != null && g.user_count < 10;
  const m = g.meta != null ? '媒体 ' + g.meta + '（' + (g.meta_count || 0) + ' 篇）' : '媒体暂无评分';
  const u = g.user != null
    ? '玩家 ' + g.user + (thin ? '*' : '') + '（' + (g.user_count > 0 ? g.user_count + ' 人' : '人数未公开') + '）'
    : '玩家暂无评分';
  return m + ' · ' + u;
}
function row(g) {
  const outs = __OUTLETS__.map(([n, ab]) => {
    const v = g.scores[n];
    return `<div class="o" title="${n}"><i><span class="fl">${n.toUpperCase()}</span>`
      + `<span class="ab">${ab}</span></i><b class="${col100(v)}">${v == null ? '—' : v}</b></div>`;
  }).join('');
  const thin = g.user != null && g.user_count < 10;      // 样本太少，分数仅供参考
  return `<tr>
    <td>${covBox(g, 56, 84)}</td>
    <td style="min-width:240px"><a class="t" href="${g.url}" target="_blank">${g.title}</a>
      <div class="meta2">${g.date || '待定'} · ${platTags(g)}</div>
      <div class="dev">${g.dev || '—'}${g.pub && g.pub !== g.dev ? ' / ' + g.pub : ''}</div></td>
    <td style="width:90px"><div class="sc ${col(g.user)}"${thin ? ' title="仅 ' + g.user_count + ' 人评分，参考性有限"' : ''}>${fmt(g.user)}${thin ? '*' : ''}<small>${g.user_count > 0 ? '玩家 ' + g.user_count + ' 人' : '人数未公开'}</small></div></td>
    <td style="width:80px"><div class="sc ${col100(g.meta)}">${g.meta == null ? '—' : g.meta}<small>媒体 ${g.meta_count || 0} 篇</small></div></td>
    <td><div class="out">${outs}</div></td></tr>`;
}
function sortBy(k) {
  const v = { user: g => -(g.user ?? -1), meta: g => -(g.meta ?? -1), date: g => (g.date < D.window[0] ? '' : g.date),
              count: g => -(g.user_count || 0), gap: g => -Math.abs(gapOf(g) ?? 0) }[k];
  const s = k === 'date' ? [...D.released].sort((a, b) => (b.date || '').localeCompare(a.date || ''))
                         : [...D.released].sort((a, b) => v(a) - v(b));
  document.getElementById('rows').innerHTML = s.map(row).join('');
  document.querySelectorAll('.bar button').forEach(b => b.classList.toggle('on', b.dataset.k === k));
}
document.querySelectorAll('.bar button').forEach(b => b.onclick = () => sortBy(b.dataset.k));
sortBy('user');
const dead = __OUTLETS__.filter(([n]) => !D.released.some(g => g.scores[n] != null)).map(([n]) => n);
document.getElementById('ft').textContent = '玩家评分 10 分制 · 媒体评分 100 分制 · 带 * 表示评分人数不足 10 人'
  + (dead.length ? ' · 本期未出数字分：' + dead.join('、') : '');
document.getElementById('watch').innerHTML = (D.watch || []).map(g => `<div class="up">
  ${covBox(g, 38, 57)}
  <div><a class="t" href="${g.url}" target="_blank">${g.title}</a>
  <div class="meta2">${g.date || '待定'} · ${platTags(g)}</div>
  <div class="dev">${g.dev || '—'}${g.pub && g.pub !== g.dev ? ' / ' + g.pub : ''}</div></div>
  <div class="d">${scoreLine(g)}</div></div>`).join('');
document.getElementById('ups').innerHTML = D.upcoming.map(g => `<div class="up">
  ${covBox(g, 38, 57)}
  <div><a class="t" href="${g.url}" target="_blank">${g.title}</a>
  <div class="meta2">${g.date || '待定'} · ${platTags(g) || '平台待定'}</div>
  <div class="dev">${g.dev || '—'}${g.pub && g.pub !== g.dev ? ' / ' + g.pub : ''}</div></div>
  <div class="d">${g.meta != null ? '媒体 ' + g.meta : '暂无评分'}</div></div>`).join('');
document.getElementById('annual').innerHTML = (D.annual || []).map(a => `<details>
  <summary title="全能：媒体 ≥ __YMINM__ 且玩家 ≥ __YMINU__；媒体封神：媒体 ≥ __YTOP__（不看玩家分）">${a.year} 年度精品（${a.games.length} 款）· 媒体 ≥ __YCNT__ 篇、评分人数 ≥ __YN__ 人，全能或媒体封神 · ${a.year < new Date().getFullYear() ? '往年数据已冻结' : '本年度迄今'}</summary>
  <div>${a.games.map(g => `<div class="up">
    ${covBox(g, 38, 57)}
    <div><a class="t" href="${g.url}" target="_blank">${g.title}</a>
    <div class="meta2">${g.date || ''} · ${platTags(g)}</div>
    <div class="dev">${g.dev || '—'}${g.pub && g.pub !== g.dev ? ' / ' + g.pub : ''}</div></div>
    <div class="d">${scoreLine(g)}</div></div>`).join('')}</div>
</details>`).join('');
</script></body></html>""".replace("__DATA__", json.dumps(p, ensure_ascii=False)) \
                      .replace("__MIN__", str(MIN_REVIEWS)) \
                      .replace("__USER__", str(MIN_USER)) \
                      .replace("__USERN__", str(MIN_USER_N)) \
                      .replace("__META__", str(MIN_META)) \
                      .replace("__YCNT__", str(YEAR_MIN_REVIEWS)) \
                      .replace("__YN__", str(YEAR_MIN_USER_N)) \
                      .replace("__YMINM__", str(YEAR_MIN_META)) \
                      .replace("__YMINU__", str(YEAR_MIN_USER)) \
                      .replace("__YTOP__", str(YEAR_TOP_META)) \
                      .replace("__OUTLETS__", json.dumps(
                          [[OUTLETS[k], OUTLETS_ABBR.get(k, OUTLETS[k])] for k in OUTLETS],
                          ensure_ascii=False)) \
                      .replace("__TOTAL__", str(p.get("total", len(p["released"]))))

if __name__ == "__main__":
    import sys
    if "--render" in sys.argv:                 # 只改样式时用现有快照重渲染，不联网
        p = json.loads(DATA.read_text())
        p["covers"] = json.loads(COVERS.read_text()) if COVERS.exists() else {}
        (ROOT / "index.html").write_text(render(p), encoding="utf-8")
        print("已用现有快照重渲染 index.html（未联网）")
    else:
        main()
