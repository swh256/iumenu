#!/usr/bin/env python3
"""
把 IU Bloomington 三个食堂的午餐/晚餐菜单抓成一份小 JSON。

为什么要抓成静态文件而不是页面直连:
Nutrislice 的 API 只给 https://indiana-dining.nutrislice.com 下发 CORS 头,
GitHub Pages 上的页面直接 fetch 会被浏览器拦掉。所以由 Actions 每天跑一次,
把结果 commit 进仓库,页面同源读 data/menu.json。

只依赖标准库,方便在 Actions 里裸跑。
"""

import collections
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://indiana-dining.api.nutrislice.com/menu/api"
HOURS_URL = "https://dining.indiana.edu/dining-hours/hours.cfml?DisplayConcept="
UA = "iumenu/1.0 (personal dining-hall dashboard; https://github.com/swh256/iumenu)"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "menu.json")

# 页面上要显示的天数(今天 + 未来 6 天)
HORIZON = 7
# 算「常驻项」时往回看多久 —— 窗口越长,判断越稳
LOOKBACK_WEEKS = 3
# 某道菜在一个档口出现的天数占比超过这个值,就当成常驻(酱料/主食/长期供应),不显示
STAPLE_RATIO = 0.5

# 只看这三个食堂。key 用于页面,slug 是 Nutrislice 的,concept 是 dining.indiana.edu
# 营业时间页的内部名(注意跟页面显示名不一样,比如 Goodbody 那边是 "Goodbody Eatery")。
HALLS = [
    {"key": "collins", "name": "Collins", "slug": "collins-eatery",
     "concept": "Collins Eatery"},
    {"key": "mcnutt", "name": "McNutt", "slug": "mcnutt-dining-hall",
     "concept": "McNutt Dining Hall"},
    {"key": "wright", "name": "Wright", "slug": "wright-eatery",
     "concept": "Wright Dining Hall"},
]

# 每天轮换的「正经档口」。剩下的(沙拉吧、酸奶吧、面包甜点、料台、薯条吧…)整档丢掉。
# 按档口显示名匹配而不是 slug/id —— BT 那边的教训是这类内部编号换季就会重编。
KEEP_STATIONS = {
    "collins": {"Hot Bar"},
    "mcnutt": {"Spice Road", "Heartland", "The Stone Grill", "Free From IX", "Pasta Bar"},
    "wright": {"Spice Road", "Scratch Table", "Stone Grill", "Slice"},
}

# 段落标题 -> 餐段。没列出来的(Entrees / Side / 无标题…)算「全天」,
# 因为远期的周还没被营养师拆成午/晚,所有菜都堆在这些标题下面。
MEAL_OF_SECTION = {
    "Lunch": "lunch",
    "Dinner": "dinner",
    "Lunch/Dinner": "both",
}
# 早餐不看
DROP_SECTIONS = {"Breakfast"}

# 常驻项过滤器漏掉的早餐菜(出现频率不够高,但明显是早餐)。
# 未来几周还没拆午/晚的时候,早餐会跟正餐混在同一个标题下,只能按菜名筛。
BREAKFAST_RE = re.compile(
    r"(pancake|waffle|french toast|omelet|scramble|hash ?brown|home fries|"
    r"oatmeal|grits|granola|biscuit|sausage (patty|link)|bagel|donut|doughnut|"
    r"croissant|cereal|breakfast|morning|egg,? ?(and |& )?cheese|steak and egg)", re.I)
BREAKFAST_EXACT = {"bacon", "beef bacon", "turkey bacon", "eggs", "egg"}

# 不是菜,是组装用的面包胚 / 蘸料 / 台面配料。
# 周末的「taco bar」这种整台料会被完整列出来,而且因为只有周末才出现,
# 躲得过常驻项过滤,只能按名字拦。尾部匹配是为了别误伤 "Penne Pasta in Salsa Forte"
# 这类真的把酱名写进菜名的菜。
COMPONENT_RE = re.compile(
    r"(^|\b)(hamburger bun|hot ?dog bun|slider bun|hoagie roll|dinner roll|"
    r"pico de gallo|sour cream|guacamole|nacho cheese|shredded (cheese|cheddar|lettuce)|"
    r"jalapeno peppers|corn tortilla chips)(\b|$)"
    r"|(sauce|dressing|vinaigrette|aioli|glaze|glace|syrup|salsa|"
    r"salsa verde|flour tortilla|corn tortilla)$", re.I)


def fetch(url, tries=4):
    """带退避的 GET。Nutrislice 偶尔会 5xx。"""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if i == tries - 1:
                raise
            print(f"  retry {i+1}/{tries-1} after {e}: {url}", file=sys.stderr)
            time.sleep(2 ** i)


def fetch_text(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if i == tries - 1:
                print(f"  hours fetch failed: {e}", file=sys.stderr)
                return ""
            time.sleep(2 ** i)


def monday(d):
    return d - datetime.timedelta(days=d.weekday())


def resolve_stations(schools):
    """把 KEEP_STATIONS 里的显示名对到当前的 menu-type slug 上,对不上就吵。"""
    by_slug = {s["slug"]: s for s in schools}
    resolved, problems = {}, []
    for hall in HALLS:
        school = by_slug.get(hall["slug"])
        if school is None:
            problems.append(f"食堂 {hall['slug']} 在 /schools/ 里没有了")
            continue
        available = {mt["name"]: mt["slug"] for mt in school.get("active_menu_types") or []}
        want = KEEP_STATIONS[hall["key"]]
        for name in sorted(want):
            if name in available:
                resolved.setdefault(hall["key"], []).append((name, available[name]))
            else:
                problems.append(
                    f"{hall['name']}: 档口 {name!r} 不在了。现有:{sorted(available)}")
    return resolved, problems


def parse_menu_week(payload):
    """一周的响应 -> {date: [(section, dish), ...]}"""
    out = {}
    for day in payload.get("days") or []:
        section, rows = None, []
        for item in day.get("menu_items") or []:
            if item.get("is_section_title"):
                section = (item.get("text") or "").strip()
                continue
            food = item.get("food")
            if food and food.get("name"):
                rows.append((section, food["name"].strip()))
        if rows:
            out[day["date"]] = rows
    return out


def is_noise(name):
    return (bool(BREAKFAST_RE.search(name))
            or bool(COMPONENT_RE.search(name))
            or name.lower() in BREAKFAST_EXACT)


def parse_hours(page, today):
    """营业时间页那张表 -> {date: '7 a.m. - 9 p.m.'}"""
    block = re.search(r"(?is)<h4>Hours.*?</h4>.*?<table>(.*?)</table>", page)
    if not block:
        return {}
    weekdays = {d: i for i, d in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}
    out = {}
    for label, value in re.findall(
            r"(?is)<th>(.*?)</th>\s*<td>(.*?)</td>", block.group(1)):
        label = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
        value = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        value = re.sub(r"\s+", " ", value)
        if label == "Today":
            out[today.isoformat()] = value
        elif label in weekdays:
            # 页面只给未来七天,所以往后找第一个匹配的星期几
            for step in range(1, 8):
                d = today + datetime.timedelta(days=step)
                if d.weekday() == weekdays[label]:
                    out[d.isoformat()] = value
                    break
    return out


def main():
    today = datetime.date.today()
    horizon = [today + datetime.timedelta(days=i) for i in range(HORIZON)]
    horizon_set = {d.isoformat() for d in horizon}

    # 要抓的周:回看 LOOKBACK_WEEKS 周(用来判断常驻项)+ 覆盖 horizon 的周
    weeks = sorted({monday(today - datetime.timedelta(weeks=w)) for w in range(LOOKBACK_WEEKS + 1)}
                   | {monday(d) for d in horizon})

    print(f"today={today}  weeks={[w.isoformat() for w in weeks]}")
    schools = fetch(f"{API}/schools/")
    resolved, problems = resolve_stations(schools)
    for p in problems:
        print(f"!! {p}", file=sys.stderr)
    if not resolved:
        sys.exit("所有档口都没解析出来,不写 data —— 先去看 API 是不是改了")

    # raw[hall][station][date] = [(section, dish)]
    raw = collections.defaultdict(lambda: collections.defaultdict(dict))
    calls = 0
    for hall in HALLS:
        for name, slug in resolved.get(hall["key"], []):
            for wk in weeks:
                url = f"{API}/weeks/school/{hall['slug']}/menu-type/{slug}/{wk:%Y/%m/%d}/"
                raw[hall["key"]][name].update(parse_menu_week(fetch(url)))
                calls += 1
    print(f"抓了 {calls} 个周菜单")

    # 常驻项:在这个档口超过一半的天数都出现 -> 酱料、主食、长期供应,不算「今天有什么」
    staples = {}
    for hall_key, stations in raw.items():
        for station, days in stations.items():
            n = len(days) or 1
            seen = collections.Counter()
            for rows in days.values():
                for dish in {d for _, d in rows}:
                    seen[dish] += 1
            staples[(hall_key, station)] = {d for d, c in seen.items() if c / n >= STAPLE_RATIO}

    # 组装
    out_days = {}
    for date in sorted(horizon_set):
        per_hall = {}
        for hall in HALLS:
            buckets = {"lunch": [], "dinner": [], "allday": []}
            for name, _slug in resolved.get(hall["key"], []):
                rows = raw[hall["key"]][name].get(date)
                if not rows:
                    continue
                picked = collections.defaultdict(list)
                for section, dish in rows:
                    if section in DROP_SECTIONS:
                        continue
                    if dish in staples[(hall["key"], name)]:
                        continue
                    if is_noise(dish):
                        continue
                    meal = MEAL_OF_SECTION.get(section or "", "allday")
                    if meal == "both":
                        picked["lunch"].append(dish)
                        picked["dinner"].append(dish)
                    else:
                        picked[meal].append(dish)
                for meal, dishes in picked.items():
                    if dishes:
                        # 去重但保序
                        buckets[meal].append({"station": name, "items": list(dict.fromkeys(dishes))})
            # 这天是不是还没被拆成午/晚
            split = bool(buckets["lunch"] or buckets["dinner"])
            per_hall[hall["key"]] = {**buckets, "split": split}
        out_days[date] = per_hall

    # 营业时间
    hours = {}
    for hall in HALLS:
        page = fetch_text(HOURS_URL + urllib.parse.quote(hall["concept"]))
        h = parse_hours(page, today) if page else {}
        if not h:
            print(f"!! {hall['name']} 营业时间没解析出来", file=sys.stderr)
        hours[hall["key"]] = h

    by_slug = {s["slug"]: s for s in schools}
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat(),
        "today": today.isoformat(),
        "dates": sorted(horizon_set),
        "halls": [{
            "key": h["key"],
            "name": h["name"],
            "full_name": by_slug.get(h["slug"], {}).get("name", h["name"]),
            "address": by_slug.get(h["slug"], {}).get("address", ""),
            "hours": hours.get(h["key"], {}),
        } for h in HALLS],
        "days": out_days,
        "warnings": problems,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    size = os.path.getsize(OUT)
    total = sum(len(g["items"]) for d in out_days.values() for hall in d.values()
                for m in ("lunch", "dinner", "allday") for g in hall[m])
    print(f"写入 {OUT}  {size/1024:.1f} KB  {len(out_days)} 天  {total} 道菜")


if __name__ == "__main__":
    main()
