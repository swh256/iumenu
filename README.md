# 今天吃哪个食堂

IU Bloomington 的 Collins / McNutt / Wright 三个食堂,今天午餐和晚餐各有什么菜。
一个静态页面,托管在 GitHub Pages。

## 它做了什么

- 只看这三个食堂(其余四个太远)
- 只看**午餐和晚餐**,不看早餐
- 只列**每天轮换的档口**。沙拉吧、酸奶吧、面包甜点、薯条料台这些常驻的整档不显示
- 只给菜名。不标热量、不标过敏原

## 为什么要有 build.py,不能页面直连

Nutrislice 的 API 是公开的,但 `Access-Control-Allow-Origin` 只对
`https://indiana-dining.nutrislice.com` 下发。GitHub Pages 上的页面直接 fetch 会被浏览器
CORS 拦掉。所以由 GitHub Actions 每天跑一次 `build.py`,把结果 commit 成
`data/menu.json`,页面同源读它。顺带的好处:页面加载只有 11 KB,秒开。

## 数据源

| 用途 | 地址 |
|---|---|
| 场馆 + 档口列表 | `https://indiana-dining.api.nutrislice.com/menu/api/schools/` |
| 一周菜单 | `.../menu/api/weeks/school/{school}/menu-type/{station}/{YYYY}/{MM}/{DD}/` |
| 营业时间 | `https://dining.indiana.edu/dining-hours/hours.cfml?DisplayConcept={内部名}` |

营业时间页的 `DisplayConcept` 用的是**内部名**,跟页面上显示的名字不一样
(内部 `McNutt Dining Hall` → 页面显示 "McNutt Quad Dining Hall")。见 `HALLS` 里的 `concept`。

## 两个不太直观的地方

**1. 午/晚拆分是临期才补的。**
营养师会提前四周就把菜单填进系统,但那时候所有菜都堆在笼统的 `Entrees` 标题下面;
只有临近的那一周才会拆成 `Lunch` / `Dinner` 段落。所以远期的日子页面会标
「这天还没拆成午餐/晚餐」。这也是为什么要每天抓一次而不是每周 —— 本周的菜单
一直在被改,拆分随时会补上。

**2. 「什么是常驻项」是算出来的,不是写死的。**
`build.py` 会往回看四周,某道菜在一个档口出现的天数超过一半就当成常驻
(番茄酱、白米饭、常年供应的 Grilled Cheese),不显示。这样 IU 改菜单也不用改代码。
剩下漏网的早餐菜和台面配料靠 `BREAKFAST_RE` / `COMPONENT_RE` 两个正则兜底。

## 改哪里

| 想改 | 改 `build.py` 的 |
|---|---|
| 加/减食堂 | `HALLS` |
| 加/减档口 | `KEEP_STATIONS`(按档口**显示名**,不要用 slug 或数字 id) |
| 常驻项判定松紧 | `STAPLE_RATIO`(默认 0.5) |
| 页面显示几天 | `HORIZON`(默认 7) |

档口是按显示名解析的。如果 IU 改了档口名,`build.py` 会在日志里吼
「档口 X 不在了,现有:[...]」,并且**不会**把这个档口静默丢掉。

## 本地跑

```
python3 build.py                    # 抓数据,写 data/menu.json,约 17 秒 / 50 个请求
python3 -m http.server 8643         # 然后开 http://localhost:8643
```

`build.py` 只用标准库,不用装任何东西。
