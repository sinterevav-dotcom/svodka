"""
Сводка — сбор новостей, пересказ через Gemini и отправка в Telegram.

Запускается по расписанию в GitHub Actions. Переменные окружения:
  GEMINI_API_KEY      — ключ Google AI Studio (если пусто — бот пришлёт просто заголовки)
  GEMINI_MODEL        — модель Gemini (по умолчанию gemini-2.5-flash)
  TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
  TELEGRAM_CHAT_ID    — куда слать: @имя_канала или числовой id; можно несколько через запятую
  MINIAPP_URL         — ссылка, которую откроет кнопка «Открыть сводку»
  DRY_RUN=1           — ничего не отправлять, только собрать digest.json
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"
OUT_FILE = ROOT / "docs" / "data" / "digest.json"

TZ = timezone(timedelta(hours=7))  # Новосибирск
RUBRICS = ["Бизнес", "Россия", "Мир", "Идеи"]
MAX_PER_FEED = 15
MAX_ITEMS_TO_MODEL = 80
TOP_N = 12
HOURS_BACK = 18

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in entry.get("links", []) or []:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]
    for e in entry.get("enclosures", []) or []:
        if e.get("type", "").startswith("image/") and e.get("href"):
            return e["href"]
    m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
    return m.group(1) if m else None


# Политика, война, происшествия и спорт — не про бизнес, отсекаем сразу
STOP_RE = re.compile(
    r"войн|всу\b|\bсво\b|обстрел|беспилот|\bбпла|дрон|ракетн|удар[аыу]? по|зеленск|киев|кличко|украин|"
    r"армия|армии|военн|призыв|мобилизац|теракт|убий|погиб|пострадав|\bдтп\b|авари[яи]|пожар|наводнен|"
    r"задержан|арестова|приговор|уголовн|выбор[ыа]|депутат|госдум|захарова|лавров|песков|"
    r"матч|футбол|хоккей|турнир|чемпионат|сборн|олимпи|спортсмен|динамо|цска|спартак|"
    r"\bwar\b|ukrain|zelensky|kyiv|killed|shooting|attack|election|football|soccer|police",
    re.I)

OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I)


def og_image(url):
    """Обложка статьи из её страницы (og:image), если в RSS картинки не было."""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 SvodkaBot"})
        m = OG_RE.search(r.text[:200000])
        if m:
            img = html.unescape(m.group(1) or m.group(2))
            return requests.compat.urljoin(r.url, img)
    except Exception as e:
        log(f"[og] {url[:60]}: {e}")
    return None


def fill_images(digest):
    for it in digest["items"]:
        if not it.get("image"):
            for s in it["sources"]:
                img = og_image(s["url"])
                if img:
                    it["image"] = img
                    break


def collect():
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    items, seen = [], set()
    for f in feeds:
        try:
            resp = requests.get(f["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0 SvodkaBot"})
            parsed = feedparser.parse(resp.content)
        except Exception as e:  # один упавший источник не ломает сводку
            log(f"[feed] {f['name']}: {e}")
            continue
        count = 0
        for entry in parsed.entries:
            if count >= MAX_PER_FEED:
                break
            title = clean(entry.get("title"), 200)
            link = entry.get("link")
            if not title or not link:
                continue
            key = re.sub(r"\W+", "", title.lower())[:60]
            if key in seen:
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            published = datetime.fromtimestamp(time.mktime(ts), timezone.utc) if ts else None
            if published and published < cutoff:
                continue
            summary = clean(entry.get("summary") or (entry.get("content") or [{}])[0].get("value"))
            if STOP_RE.search(f"{title} {summary}"):
                continue
            seen.add(key)
            items.append({
                "id": f"n{len(items)}",
                "source": f["name"],
                "rubric": f["rubric"],
                "title": title,
                "summary": summary,
                "url": link,
                "image": find_image(entry),
                "published": published.isoformat() if published else None,
            })
            count += 1
        log(f"[feed] {f['name']}: {count}")
    return items


PROMPT = """Ты — редактор деловой сводки для предпринимателя из России, который ищет
новые возможности для бизнеса. Ниже список новостей за последние часы (JSON).

Отбери до {top} новостей, полезных предпринимателю: деньги, рынки, компании,
сделки, новые продукты, стартапы, тренды, изменения правил для бизнеса.
НЕ бери политику, войну, происшествия, спорт, светскую хронику — даже если они громкие.
Из рубрики «Идеи» (стартапы, новые продукты, кейсы) возьми минимум 4 новости.
Убери дубли: одно событие из разных источников объединяй и укажи все id.

Рубрики: Бизнес — компании и рынки в России; Россия — экономика и правила для бизнеса в РФ;
Мир — мировые рынки и компании; Идеи — стартапы, новые продукты, бизнес-модели, кейсы.

Для каждой отобранной новости верни:
- ids: список id исходных новостей
- rubric: одно из {rubrics}
- title: короткий заголовок на русском, до 90 символов, без кликбейта
- bullets: 2–3 коротких пункта на русском — только факты из текста новостей
- impact: одно предложение «что это значит для бизнеса в России», без выдумок
- idea: одна конкретная бизнес-идея для предпринимателя в России, которую подсказывает
  эта новость (что запустить, кому продать, как заработать) — 1–2 предложения
- importance: число от 1 до 10

Также верни:
- intro: одно предложение — главное за день
- key_figures: до 3 ключевых цифр, которые ЯВНО есть в новостях: {{"label","value","note"}}
- ideas: 3 лучшие бизнес-идеи дня по мотивам новостей, каждая:
  {{"title": "название идеи до 60 символов", "pitch": "суть в 1–2 предложениях",
    "why_now": "почему сейчас — со ссылкой на новость", "first_step": "что сделать на этой неделе"}}
  Идеи — реалистичные для малого и среднего бизнеса в России, без больших инвестиций.
  Учитывай реалии РФ: санкции, уход зарубежных компаний, сервисы, заблокированные или
  недоступные в России (LinkedIn, Instagram, Facebook, многие западные SaaS и платёжки),
  не предлагай строить бизнес на них. Лучше — российские аналоги, локализация, импортозамещение.

Факты бери только из новостей; идеи — твои, но опирайся на новости.
Ответ — строго JSON: {{"intro": "...", "key_figures": [...], "ideas": [...], "items": [...]}}

Новости:
{news}"""


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


PREFERRED = ["deepseek", "qwen", "llama-3.3", "llama-4", "gemma", "mistral", "nemotron", "gpt-oss"]


def free_models():
    """Бесплатные модели OpenRouter: сначала известные семейства, потом с большим контекстом."""
    try:
        data = requests.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
    except Exception as e:
        log(f"[openrouter] список моделей: {e}")
        return []
    free = [m for m in data if m["id"].endswith(":free") and (m.get("context_length") or 0) >= 32000
            and "text" in ((m.get("architecture") or {}).get("output_modalities") or ["text"])]

    def rank(m):
        mid = m["id"].lower()
        fam = next((i for i, p in enumerate(PREFERRED) if p in mid), len(PREFERRED))
        return (fam, -(m.get("context_length") or 0))
    return [m["id"] for m in sorted(free, key=rank)]


def ask_openrouter(items, key):
    news = [{k: it[k] for k in ("id", "source", "rubric", "title", "summary")} for it in items[:MAX_ITEMS_TO_MODEL]]
    prompt = PROMPT.format(top=TOP_N, rubrics=", ".join(RUBRICS), news=json.dumps(news, ensure_ascii=False))
    fixed = os.environ.get("OPENROUTER_MODEL", "").strip()
    models = [fixed] if fixed else free_models()[:4]
    for model in models:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions", timeout=180,
                headers={"Authorization": f"Bearer {key}", "X-Title": "Svodka bot"},
                json={"model": model, "temperature": 0.3,
                      "messages": [{"role": "user", "content": prompt}]})
            if r.status_code != 200:
                log(f"[openrouter] {model}: {r.status_code} {r.text[:200]}")
                continue
            result = parse_json(r.json()["choices"][0]["message"]["content"])
            log(f"[openrouter] модель: {model}")
            return result
        except Exception as e:
            log(f"[openrouter] {model}: {e}")
    return None


def ask_ai(items):
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return ask_openrouter(items, key)
    return ask_gemini(items)


def ask_gemini(items):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log("[ai] нет ключа — режим заголовков")
        return None
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
    news = [{k: it[k] for k in ("id", "source", "rubric", "title", "summary")} for it in items[:MAX_ITEMS_TO_MODEL]]
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(
            top=TOP_N, rubrics=", ".join(RUBRICS), news=json.dumps(news, ensure_ascii=False))}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=120, headers={"x-goog-api-key": key})
            if r.status_code == 429 or r.status_code >= 500:
                log(f"[gemini] {r.status_code}, повтор")
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as e:
            log(f"[gemini] ошибка: {e}")
            time.sleep(5)
    return None


def build(items, ai):
    by_id = {it["id"]: it for it in items}
    out = []
    if ai and ai.get("items"):
        for a in sorted(ai["items"], key=lambda x: -int(x.get("importance", 0) or 0)):
            srcs = [by_id[i] for i in a.get("ids", []) if i in by_id]
            if not srcs:
                continue
            first = srcs[0]
            out.append({
                "rubric": a.get("rubric") if a.get("rubric") in RUBRICS else first["rubric"],
                "title": a.get("title") or first["title"],
                "bullets": [b for b in (a.get("bullets") or []) if b][:3],
                "impact": a.get("impact") or "",
                "idea": a.get("idea") or "",
                "image": next((s["image"] for s in srcs if s["image"]), None),
                "sources": [{"name": s["source"], "url": s["url"]} for s in srcs],
                "published": first["published"],
            })
            if len(out) >= TOP_N:
                break
    if not out:  # запасной режим: свежие заголовки по рубрикам, идей — больше всего
        quota = {"Бизнес": 3, "Россия": 2, "Мир": 2, "Идеи": 5}
        per, per_src = {r: 0 for r in RUBRICS}, {}
        for it in sorted(items, key=lambda x: x["published"] or "", reverse=True):
            if per.get(it["rubric"], 99) >= quota.get(it["rubric"], 0) or per_src.get(it["source"], 0) >= 2:
                continue
            per[it["rubric"]] += 1
            per_src[it["source"]] = per_src.get(it["source"], 0) + 1
            out.append({
                "rubric": it["rubric"], "title": it["title"],
                "bullets": [it["summary"][:220]] if it["summary"] else [],
                "impact": "", "idea": "", "image": it["image"],
                "sources": [{"name": it["source"], "url": it["url"]}],
                "published": it["published"],
            })
    now = datetime.now(TZ)
    return {
        "generated_at": now.isoformat(),
        "date_label": f"{WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}",
        "edition": "Утренняя" if now.hour < 14 else "Вечерняя",
        "intro": (ai or {}).get("intro", ""),
        "key_figures": ((ai or {}).get("key_figures") or [])[:3],
        "ideas": [i for i in ((ai or {}).get("ideas") or []) if i.get("title")][:3],
        "ai": bool(ai and ai.get("items")),
        "items": out,
    }


def send(digest):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    app_url = os.environ.get("MINIAPP_URL", "").strip()
    if not token or not chats:
        log("[telegram] нет токена или чата — пропускаю отправку")
        return
    lines = [f"<b>{html.escape(digest['edition'])} сводка · {html.escape(digest['date_label'])}</b>"]
    if digest["intro"]:
        lines.append(f"\n{html.escape(digest['intro'])}")
    lines.append("")
    for it in digest["items"][:6]:
        lines.append(f"• <b>{html.escape(it['rubric'])}.</b> {html.escape(it['title'])}")
    if digest.get("ideas"):
        lines.append("\n<b>Идеи дня</b>")
        for i in digest["ideas"]:
            lines.append(f"• {html.escape(i['title'])}")
    text = "\n".join(lines)
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if app_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть сводку", "url": app_url}]]}
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={**payload, "chat_id": chat}, timeout=30)
        log(f"[telegram] {chat}: {r.status_code} {r.text[:200]}")
        r.raise_for_status()


def main():
    items = collect()
    log(f"[collect] всего {len(items)}")
    if not items:
        log("Ни один источник не ответил — сводку не обновляю")
        sys.exit(1)
    digest = build(items, ask_ai(items))
    fill_images(digest)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[out] {len(digest['items'])} новостей, ИИ: {digest['ai']}")
    if os.environ.get("DRY_RUN") != "1":
        send(digest)


if __name__ == "__main__":
    main()
