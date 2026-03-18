# v0.1.1

import os
import re
import time
from dataclasses import dataclass, asdict
from typing import List

from flask import Flask, jsonify, render_template, request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)

PORTAL_URL = "https://portal.kleague.com/main/schedule/calendar.do"
SITE_URL = "https://www.kleague.com/schedule.do"


@dataclass
class MatchRow:
    date: str
    time: str
    home: str
    away: str
    stadium: str
    status: str
    note: str


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def dedupe_rows(rows: List[MatchRow]) -> List[MatchRow]:
    seen = set()
    result: List[MatchRow] = []
    for row in rows:
        key = (row.date, row.time, row.home, row.away, row.stadium, row.status, row.note)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def parse_calendar_text(body_text: str, year: int, month: int) -> List[MatchRow]:
    text = body_text.replace("\r", "")
    lines = [clean_text(line) for line in text.split("\n")]
    lines = [line for line in lines if line]

    rows: List[MatchRow] = []
    current_day = ""
    current_status = ""

    day_header_re = re.compile(r"^(\d{1,2})(?:\s+(종료|예정|연기|취소|LIVE|진행중))?$")
    match_re = re.compile(r"^(.+?)\s*:\s*(.+?)(?:\s*\((.+?)\))?$")

    for line in lines:
        day_match = day_header_re.match(line)
        if day_match:
            current_day = day_match.group(1).zfill(2)
            current_status = clean_text(day_match.group(2) or "")
            continue

        m = match_re.match(line)
        if m and current_day:
            home = clean_text(m.group(1))
            away = clean_text(m.group(2))
            extra = clean_text(m.group(3) or "")

            rows.append(
                MatchRow(
                    date=f"{year}-{month:02d}-{current_day}",
                    time="",
                    home=home,
                    away=away,
                    stadium="",
                    status=current_status,
                    note=extra,
                )
            )

    return dedupe_rows(rows)


def parse_list_rows_from_dom(page, year: int, month: int) -> List[MatchRow]:
    data = page.evaluate(
        r"""
        () => {
          function txt(el) {
            return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
          }

          const out = [];
          const trs = Array.from(document.querySelectorAll('tr'));
          for (const tr of trs) {
            const tds = Array.from(tr.querySelectorAll('th, td'));
            const cells = tds.map(txt).filter(Boolean);
            if (cells.length >= 4) {
              out.push({type: 'table', cells});
            }
          }

          const lis = Array.from(document.querySelectorAll('li'));
          for (const li of lis) {
            const text = txt(li);
            if (!text) continue;
            if (/\d{1,2}:\d{2}/.test(text) || /:\s*/.test(text)) {
              out.push({type: 'list', text});
            }
          }

          return {
            rows: out,
            bodyText: txt(document.body)
          };
        }
        """
    )

    rows: List[MatchRow] = []

    for item in data.get("rows", []):
        if item.get("type") != "table":
            continue

        cells = [clean_text(c) for c in item.get("cells", []) if clean_text(c)]
        joined = " | ".join(cells)

        has_time = any(re.search(r"\b\d{1,2}:\d{2}\b", c) for c in cells)
        has_match = any(":" in c for c in cells)
        if not (has_time or has_match):
            continue

        date = ""
        time_str = ""
        home = ""
        away = ""
        stadium = ""
        status = ""
        note = ""

        for cell in cells:
            if not date:
                m_date = re.search(r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})", cell)
                if m_date:
                    date = m_date.group(1).replace(".", "-").replace("/", "-")

            if not time_str:
                m_time = re.search(r"\b(\d{1,2}:\d{2})\b", cell)
                if m_time:
                    time_str = m_time.group(1)

            if not stadium and any(keyword in cell for keyword in ["경기장", "스타디움", "구장", "운동장", "아레나"]):
                stadium = cell

            if not status and cell in {"예정", "종료", "연기", "취소", "LIVE", "진행중"}:
                status = cell

        for cell in cells:
            m_match = re.match(r"^(.+?)\s*:\s*(.+?)(?:\s*\((.+?)\))?$", cell)
            if m_match:
                home = clean_text(m_match.group(1))
                away = clean_text(m_match.group(2))
                extra = clean_text(m_match.group(3) or "")
                if extra and not note:
                    note = extra
                break

        if not date:
            date = f"{year}-{month:02d}"

        if home and away:
            rows.append(
                MatchRow(
                    date=date,
                    time=time_str,
                    home=home,
                    away=away,
                    stadium=stadium,
                    status=status,
                    note=note or joined,
                )
            )

    if not rows:
        rows = parse_calendar_text(data.get("bodyText", ""), year, month)

    return dedupe_rows(rows)


def click_by_text_safe(page, text_value: str) -> bool:
    candidates = [
        page.get_by_text(text_value, exact=True),
        page.get_by_text(text_value),
    ]

    for locator in candidates:
        try:
            locator.first.click(timeout=2000)
            page.wait_for_timeout(700)
            return True
        except Exception:
            continue
    return False


def fetch_schedule_from_official_site(league: str, year: int, month: int) -> List[MatchRow]:
    league_text_map = {
        "kleague1": ["K League 1", "K 리그 1"],
        "kleague2": ["K League 2", "K 리그 2"],
    }

    all_rows: List[MatchRow] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            page.goto(SITE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            click_by_text_safe(page, str(year))
            click_by_text_safe(page, f"{month:02d}")
            click_by_text_safe(page, str(month))

            for text_value in league_text_map[league]:
                if click_by_text_safe(page, text_value):
                    break

            click_by_text_safe(page, "list")
            click_by_text_safe(page, "라이트 모드로 보기")

            page.wait_for_timeout(2500)
            all_rows = parse_list_rows_from_dom(page, year, month)
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass

        if not all_rows:
            try:
                page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                click_by_text_safe(page, str(year))
                click_by_text_safe(page, str(month))
                click_by_text_safe(page, f"{month:02d}")

                for text_value in league_text_map[league]:
                    if click_by_text_safe(page, text_value):
                        break

                page.wait_for_timeout(2500)
                body_text = page.locator("body").inner_text(timeout=5000)
                all_rows = parse_calendar_text(body_text, year, month)
            except Exception:
                pass

        browser.close()

    return dedupe_rows(all_rows)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/schedule")
def api_schedule():
    league = request.args.get("league", "kleague1").strip()
    year = int(request.args.get("year", "2026"))
    month = int(request.args.get("month", "3"))

    if league not in {"kleague1", "kleague2"}:
        return jsonify({"ok": False, "error": "league는 kleague1 또는 kleague2 여야 합니다."}), 400

    started = time.time()
    rows = fetch_schedule_from_official_site(league, year, month)
    elapsed = round(time.time() - started, 2)

    return jsonify(
        {
            "ok": True,
            "version": "v0.1.1",
            "league": league,
            "year": year,
            "month": month,
            "count": len(rows),
            "elapsed_seconds": elapsed,
            "rows": [asdict(row) for row in rows],
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
