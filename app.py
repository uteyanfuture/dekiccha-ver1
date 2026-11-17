from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


DB_PATH = Path(os.environ.get("DEKICCHA_DB", str(Path(__file__).with_name("study.sqlite3"))))
WEEKDAYS = "月火水木金土日"
TEXT_LIMIT = 3000
DAY_COLUMNS = {
    "やること（教材・ページ）": "todo",
    "予定（分）": "planned",
    "実際（分）": "actual",
    "済": "done",
}
KPT_COLUMNS = {
    "Keep（よかったこと）": "keep",
    "Problem（課題・理由）": "problem",
    "Try（明日試すこと1つ）": "try",
}
ROAD_FIELDS = {
    "goal": "",
    "deadline": "2027年3月",
    "current": "",
    "stuck": "",
    "strength": "",
    "weakness": "",
    "weekday": "",
    "holiday": "",
    "club": "",
}


def week_start(value: date) -> date:
    """指定日が含まれる週の月曜日を返す。"""
    return value - timedelta(days=value.weekday())


def blank_day() -> dict:
    return {
        "todo": "",
        "planned": 0,
        "actual": 0,
        "done": False,
        "keep": "",
        "problem": "",
        "try": "",
    }


def blank_week() -> dict:
    return dict(
        goals=["", "", ""],
        days=[blank_day() for _ in range(7)],
        problem="",
        next_try="",
        memo="",
    )


def database() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return db


def load_all() -> dict:
    with database() as db:
        records = {key: json.loads(value) for key, value in db.execute("SELECT key, value FROM records")}
    return {
        "weeks": {key[5:]: value for key, value in records.items() if key.startswith("week:")},
        "roadmap": records.get("roadmap", {}),
    }


def persist(data: dict) -> None:
    entries = [("week:" + key, value) for key, value in data["weeks"].items()]
    entries.append(("roadmap", data["roadmap"]))
    with database() as db:
        db.executemany(
            "INSERT OR REPLACE INTO records VALUES (?, ?)",
            [(key, json.dumps(value, ensure_ascii=False)) for key, value in entries],
        )


def carry_try(weeks: dict, start: date, index: int) -> str:
    """前日のTry（月曜日の場合は前週の『来週の最初のTry』または日曜日のTry）を取得する。"""
    current = weeks.get(start.isoformat(), blank_week())
    if index:
        return current["days"][index - 1]["try"]
    previous = weeks.get((start - timedelta(days=7)).isoformat(), blank_week())
    return previous["next_try"] or previous["days"][6]["try"]


def totals(week: dict) -> tuple[int, int, int, float | None]:
    planned = sum(day["planned"] for day in week["days"])
    actual = sum(day["actual"] for day in week["days"])
    done = sum(bool(day["done"]) for day in week["days"])
    return planned, actual, done, actual / planned if planned else None


def validated_backup(raw: bytes) -> dict:
    """アップロードされたJSONバックアップを検証・正規化する。"""
    if len(raw) > 10_000_000:
        raise ValueError("ファイルサイズは10MB以下にしてください。")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("format") != "dekiccha-streamlit-v1":
        raise ValueError("DEKICCHA形式のJSONバックアップファイルではありません。")
    weeks = data.get("weeks")
    roadmap = data.get("roadmap")
    if not isinstance(weeks, dict) or not isinstance(roadmap, dict):
        raise ValueError("データ構造が正しくありません。")

    def text(value):
        if not isinstance(value, str) or len(value) > TEXT_LIMIT:
            raise ValueError(f"テキストは{TEXT_LIMIT}文字以内の文字列である必要があります。")
        return value

    result = {"weeks": {}, "roadmap": {}}
    for key, value in weeks.items():
        day = date.fromisoformat(key)
        if key != day.isoformat() or day != week_start(day) or not isinstance(value, dict):
            raise ValueError(f"週の開始日（月曜日）が不正です: {key}")
        goals, days = value.get("goals"), value.get("days")
        if not isinstance(goals, list) or len(goals) != 3 or not isinstance(days, list) or len(days) != 7:
            raise ValueError("目標は3つ、日別データは7日分必要です。")
        clean = blank_week()
        clean["goals"] = [text(goal) for goal in goals]
        for i, day_data in enumerate(days):
            if not isinstance(day_data, dict):
                raise ValueError("日別データの形式が不正です。")
            for field in ("todo", "keep", "problem", "try"):
                clean["days"][i][field] = text(day_data.get(field, ""))
            for field in ("planned", "actual"):
                number = day_data.get(field, 0)
                if type(number) is not int or not 0 <= number <= 1440:
                    raise ValueError("学習時間は0〜1440の整数で指定してください。")
                clean["days"][i][field] = number
            if type(day_data.get("done", False)) is not bool:
                raise ValueError("完了フラグが不正です。")
            clean["days"][i]["done"] = day_data.get("done", False)
        for field in ("problem", "next_try", "memo"):
            clean[field] = text(value.get(field, ""))
        result["weeks"][key] = clean

    for field in ROAD_FIELDS:
        result["roadmap"][field] = text(roadmap.get(field, ""))
    months = roadmap.get("months", [["", "", ""] for _ in range(6)])
    if not isinstance(months, list) or len(months) != 6:
        raise ValueError("月別ロードマップは6行必要です。")
    for row in months:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("月別ロードマップの各行は3列必要です。")
        for item in row:
            text(item)
    result["roadmap"]["months"] = months
    return result


def make_pdf(weeks: dict, start: date, size: str = "A4") -> bytes:
    """1週間見開き（1ページ目：ウィークリー計画 / 2ページ目：毎日のKPT）のPDFを生成する。"""
    from reportlab.lib.pagesizes import A4, B5
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas
    from reportlab.platypus import KeepInFrame, Paragraph

    font = "HeiseiKakuGo-W5"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    style = ParagraphStyle("ja", fontName=font, fontSize=9, leading=13, wordWrap="CJK")
    output = BytesIO()
    width, height = B5 if size == "B5" else A4
    pdf = canvas.Canvas(output, pagesize=(width, height))
    pdf.setTitle("DEKICCHA Weekly Plan & KPT")
    margin, usable = 26, width - 52
    row_height = (height - 290) / 7
    week = weeks.get(start.isoformat(), blank_week())

    def cell(x, top, w, h, value, fill=False):
        pdf.setStrokeColorRGB(0.77, 0.83, 0.80)
        pdf.setFillColorRGB(0.94, 0.96, 0.93)
        pdf.rect(x, top - h, w, h, fill=int(fill), stroke=1)
        safe_text = escape(str(value)).replace("\n", "<br/>")
        paragraph = Paragraph(safe_text, style)
        content = KeepInFrame(w - 10, h - 10, [paragraph], mode="shrink", hAlign="LEFT", vAlign="TOP")
        content.wrapOn(pdf, w - 10, h - 10)
        content.drawOn(pdf, x + 5, top - 5 - content.height)

    for page in range(2):
        pdf.setFillColorRGB(0.09, 0.29, 0.24)
        pdf.setFont(font, 17)
        pdf.drawString(margin, height - 37, "DEKICCHA / " + ("ウィークリー計画" if page == 0 else "毎日のKPT"))
        pdf.setFont(font, 10)
        pdf.drawString(margin, height - 60, f"{start:%Y/%m/%d} 〜 {start + timedelta(days=6):%m/%d}")
        intro = (
            ("【今週の目標】\n" + "\n".join(f"{i + 1}. {g}" for i, g in enumerate(week["goals"])))
            if page == 0
            else "その日の終わりに3分で書く\nKeep（よかったこと・続けたいこと） / Problem（うまくいかなかったこと・理由）\nTry（次の日に試すこと・1つだけ）"
        )
        cell(margin, height - 76, usable, 67, intro)
        ratios = [0.09, 0.62, 0.20, 0.09] if page == 0 else [0.09, 0.30, 0.30, 0.31]
        titles = (
            ["曜日", "やること（教材・ページ）", "予定 / 実際\n（分）", "済"]
            if page == 0
            else ["曜日", "Keep\nよかったこと", "Problem\n課題・理由", "Try\n明日試す1つ"]
        )
        top = height - 155
        x = margin
        for ratio, title in zip(ratios, titles):
            cell(x, top, usable * ratio, 36, title, True)
            x += usable * ratio
        top -= 36
        for i, day_data in enumerate(week["days"]):
            day = start + timedelta(days=i)
            day_label = f"{WEEKDAYS[i]}\n{day:%m/%d}"
            if page == 0:
                carry = carry_try(weeks, start, i)
                todo = day_data["todo"] + (f"\n[前日のTry] {carry}" if carry else "")
                values = [
                    day_label,
                    todo,
                    f'{day_data["planned"]} / {day_data["actual"]}',
                    "済" if day_data["done"] else "□",
                ]
            else:
                values = [day_label, day_data["keep"], day_data["problem"], day_data["try"]]
            x = margin
            for ratio, value in zip(ratios, values):
                cell(x, top, usable * ratio, row_height, value)
                x += usable * ratio
            top -= row_height
        if page == 0:
            planned, actual, _, ratio = totals(week)
            rate = f"{ratio:.0%}" if ratio is not None else "—"
            footer = f"今週の合計：予定 {planned}分 / 実績 {actual}分（達成率 {rate}）\n※予定を全部詰め込まず、1週間のうち1〜2日は『予備日』にして遅れを取り返そう。"
        else:
            footer = (
                f'今週のProblemでいちばん多かったこと：{week["problem"]}\n'
                f'来週の最初のTry：{week["next_try"]}\n'
                f'週末メモ：{week["memo"]}'
            )
        cell(margin, top - 10, usable, 68, footer)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def main():
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="DEKICCHA", page_icon="📘", layout="wide")
    st.markdown(
        """<style>
        .stApp {background:#f5f7f1}
        h1,h2,h3 {color:#214e41}
        [data-testid="stMetric"] {background:#e8efe5;padding:16px;border-radius:12px}
        [data-testid="stSidebar"] {background:#eef2e9}
        .block-container {max-width:1600px;padding-top:2rem}
        </style>""",
        unsafe_allow_html=True,
    )

    if "records" not in st.session_state:
        try:
            st.session_state.records = load_all()
        except (sqlite3.Error, OSError, ValueError) as error:
            st.error(f"データの読み込みに失敗しました: {error}")
            st.stop()
        st.session_state.versions = {}
        st.session_state.notice = ""

    records = st.session_state.records
    today_jst = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    default_start = week_start(today_jst)

    if default_start.isoformat() not in records["weeks"]:
        records["weeks"][default_start.isoformat()] = blank_week()
        persist(records)

    def bump_version(scope: str):
        st.session_state.versions[scope] = st.session_state.versions.get(scope, 0) + 1

    def autosave():
        try:
            persist(records)
            now_str = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M:%S")
            st.session_state.notice = f"自動保存しました（{now_str}）"
        except (sqlite3.Error, OSError) as error:
            st.error(f"保存エラー: {error}")

    # --- サイドバー：週の切り替え・PDF出力・バックアップ ---
    with st.sidebar:
        st.title("📘 DEKICCHA")
        st.caption("決めるのは自分。シートは、計画をつくる『ものさし』です。")

        all_starts = sorted((date.fromisoformat(k) for k in records["weeks"]), reverse=True)
        selected_start = st.selectbox(
            "表示する週（歴代記録）",
            options=all_starts,
            index=all_starts.index(default_start) if default_start in all_starts else 0,
            format_func=lambda d: f"{d:%Y/%m/%d} 〜 {d + timedelta(days=6):%m/%d}",
        )

        new_date = st.date_input("別の週を開く・追加する", value=selected_start)
        if st.button("この週を追加 / 移動", use_container_width=True):
            target = week_start(new_date)
            if target.isoformat() not in records["weeks"]:
                records["weeks"][target.isoformat()] = blank_week()
                autosave()
            bump_version(target.isoformat())
            st.rerun()

        st.divider()
        st.subheader("📄 見開きPDF出力")
        pdf_size = st.radio("用紙サイズ", ["A4", "B5"], horizontal=True)
        pdf_bytes = make_pdf(records["weeks"], selected_start, size=pdf_size)
        st.download_button(
            label="この週をPDFダウンロード",
            data=pdf_bytes,
            file_name=f"DEKICCHA_{selected_start:%Y%m%d}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

        st.divider()
        st.subheader("💾 バックアップ / 復元")
        export_payload = json.dumps(
            {
                "format": "dekiccha-streamlit-v1",
                "exported_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
                "weeks": records["weeks"],
                "roadmap": records["roadmap"],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        st.download_button(
            label="全履歴をJSONで保存",
            data=export_payload,
            file_name=f"dekiccha_backup_{today_jst:%Y%m%d}.json",
            mime="application/json",
            use_container_width=True,
        )

        uploaded = st.file_uploader("JSONバックアップから復元", type=["json"])
        if uploaded is not None and st.button("復元を実行する", use_container_width=True):
            try:
                restored = validated_backup(uploaded.getvalue())
                st.session_state.records = restored
                persist(restored)
                st.session_state.versions = {}
                st.session_state.notice = "バックアップから復元しました。"
                st.rerun()
            except (ValueError, json.JSONDecodeError, sqlite3.Error) as error:
                st.error(f"復元に失敗しました: {error}")

        if st.session_state.notice:
            st.success(st.session_state.notice)

    # --- メイン画面 ---
    week_key = selected_start.isoformat()
    week = records["weeks"].setdefault(week_key, blank_week())
    ver = st.session_state.versions.get(week_key, 0)

    st.header(f"ウィークリーシート：{selected_start:%Y/%m/%d}（月）〜 {selected_start + timedelta(days=6):%m/%d}（日）")

    planned_sum, actual_sum, done_count, achieve_ratio = totals(week)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("予定合計", f"{planned_sum} 分")
    m2.metric("実績合計", f"{actual_sum} 分", delta=f"{actual_sum - planned_sum:+d} 分")
    m3.metric("時間達成率", f"{achieve_ratio:.0%}" if achieve_ratio is not None else "—")
    m4.metric("完了日数", f"{done_count} / 7 日")

    tab_week, tab_roadmap, tab_history = st.tabs(
        ["📅 週間計画 ＆ KPT（見開き）", "🗺️ ゴール ＆ 逆算ロードマップ", "📚 歴代の記録一覧"]
    )

    with tab_week:
        col_left, col_right = st.columns(2, gap="large")

        # --- 左ページ：ウィークリー計画 ---
        with col_left:
            st.subheader("1. ウィークリー計画（予定と実績）")
            st.markdown("**今週の目標（最大3つ）**")
            g_cols = st.columns(3)
            new_goals = []
            for idx in range(3):
                val = g_cols[idx].text_input(
                    f"目標 {idx + 1}",
                    value=week["goals"][idx],
                    key=f"goal_{week_key}_{ver}_{idx}",
                    placeholder=f"目標{idx + 1}",
                )
                new_goals.append(val)

            plan_rows = []
            for i, d in enumerate(week["days"]):
                day_dt = selected_start + timedelta(days=i)
                prev_try = carry_try(records["weeks"], selected_start, i)
                plan_rows.append(
                    {
                        "日付": f"{day_dt:%m/%d}({WEEKDAYS[i]})",
                        "前日のTry（意識すること）": prev_try,
                        "やること（教材・ページ）": d["todo"],
                        "予定（分）": int(d["planned"]),
                        "実際（分）": int(d["actual"]),
                        "済": bool(d["done"]),
                    }
                )

            edited_plan = st.data_editor(
                pd.DataFrame(plan_rows),
                key=f"plan_editor_{week_key}_{ver}",
                use_container_width=True,
                hide_index=True,
                disabled=["日付", "前日のTry（意識すること）"],
                column_config={
                    "予定（分）": st.column_config.NumberColumn(min_value=0, max_value=1440, step=10),
                    "実際（分）": st.column_config.NumberColumn(min_value=0, max_value=1440, step=10),
                    "済": st.column_config.CheckboxColumn(),
                },
            )
            st.caption("💡 予定を詰め込みすぎず、1週間のうち1〜2日は『予備日』にして遅れを取り返そう。")

        # --- 右ページ：毎日のKPT ---
        with col_right:
            st.subheader("2. 毎日のKPT（その日の終わりに3分で書く）")
            kpt_rows = []
            for i, d in enumerate(week["days"]):
                day_dt = selected_start + timedelta(days=i)
                kpt_rows.append(
                    {
                        "日付": f"{day_dt:%m/%d}({WEEKDAYS[i]})",
                        "Keep（よかったこと）": d["keep"],
                        "Problem（課題・理由）": d["problem"],
                        "Try（明日試すこと1つ）": d["try"],
                    }
                )

            edited_kpt = st.data_editor(
                pd.DataFrame(kpt_rows),
                key=f"kpt_editor_{week_key}_{ver}",
                use_container_width=True,
                hide_index=True,
                disabled=["日付"],
            )

            st.markdown("**週末の見直し**")
            w_problem = st.text_input(
                "今週のProblemで、いちばん多かったこと",
                value=week["problem"],
                key=f"w_prob_{week_key}_{ver}",
            )
            w_next_try = st.text_input(
                "来週の最初のTry",
                value=week["next_try"],
                key=f"w_try_{week_key}_{ver}",
            )
            w_memo = st.text_area(
                "今週の振り返りメモ",
                value=week["memo"],
                height=80,
                key=f"w_memo_{week_key}_{ver}",
            )

        # 変更検知と自動保存
        updated_days = []
        for i in range(7):
            p_row = edited_plan.iloc[i]
            k_row = edited_kpt.iloc[i]
            updated_days.append(
                {
                    "todo": str(p_row["やること（教材・ページ）"] or ""),
                    "planned": int(p_row["予定（分）"] or 0),
                    "actual": int(p_row["実際（分）"] or 0),
                    "done": bool(p_row["済"]),
                    "keep": str(k_row["Keep（よかったこと）"] or ""),
                    "problem": str(k_row["Problem（課題・理由）"] or ""),
                    "try": str(k_row["Try（明日試すこと1つ）"] or ""),
                }
            )

        updated_week = {
            "goals": new_goals,
            "days": updated_days,
            "problem": w_problem,
            "next_try": w_next_try,
            "memo": w_memo,
        }
        if updated_week != week:
            records["weeks"][week_key] = updated_week
            autosave()
            st.rerun()

    with tab_roadmap:
        st.subheader("ゴール＆ロードマップ（目標から逆算して月ごとにやることを決める）")
        rm = records["roadmap"]
        r_ver = st.session_state.versions.get("roadmap", 0)

        c1, c2 = st.columns([1, 2])
        deadline = c1.text_input("1. いつまでに", value=rm.get("deadline", "2027年3月"), key=f"rm_dead_{r_ver}")
        goal = c2.text_input("どこまで（ゴール）", value=rm.get("goal", ""), key=f"rm_goal_{r_ver}")

        c3, c4 = st.columns(2)
        current = c3.text_input("2. いまの点数・順位", value=rm.get("current", ""), key=f"rm_cur_{r_ver}")
        stuck = c4.text_input("つまずいている所", value=rm.get("stuck", ""), key=f"rm_stk_{r_ver}")

        c5, c6 = st.columns(2)
        strength = c5.text_input("得意な科目・分野", value=rm.get("strength", ""), key=f"rm_str_{r_ver}")
        weakness = c6.text_input("苦手な科目・分野", value=rm.get("weakness", ""), key=f"rm_weak_{r_ver}")

        c7, c8, c9 = st.columns(3)
        weekday = c7.text_input("3. 1日に使える時間（平日）", value=rm.get("weekday", ""), key=f"rm_wd_{r_ver}")
        holiday = c8.text_input("休日", value=rm.get("holiday", ""), key=f"rm_hd_{r_ver}")
        club = c9.text_input("部活・習い事のある日", value=rm.get("club", ""), key=f"rm_cl_{r_ver}")

        st.markdown("**4. 逆算ロードマップ（上を今月、一番下をゴールの月にして逆算）**")
        months_raw = rm.get("months", [["", "", ""] for _ in range(6)])
        months_df = pd.DataFrame(months_raw, columns=["月", "その月の目標（点数・どこまで）", "やること（教材・ページ）"])
        edited_months = st.data_editor(
            months_df,
            key=f"rm_months_{r_ver}",
            use_container_width=True,
            hide_index=True,
        )

        new_rm = {
            "goal": goal,
            "deadline": deadline,
            "current": current,
            "stuck": stuck,
            "strength": strength,
            "weakness": weakness,
            "weekday": weekday,
            "holiday": holiday,
            "club": club,
            "months": edited_months.fillna("").astype(str).values.tolist(),
        }
        if new_rm != rm:
            records["roadmap"] = new_rm
            autosave()

    with tab_history:
        st.subheader("歴代の週間記録サマリー")
        summary_rows = []
        for k in sorted(records["weeks"].keys(), reverse=True):
            s_dt = date.fromisoformat(k)
            w_data = records["weeks"][k]
            p_m, a_m, d_c, rat = totals(w_data)
            summary_rows.append(
                {
                    "週（月曜開始）": f"{s_dt:%Y/%m/%d} 〜 {s_dt + timedelta(days=6):%m/%d}",
                    "目標1": w_data["goals"][0],
                    "予定(分)": p_m,
                    "実績(分)": a_m,
                    "達成率": f"{rat:.0%}" if rat is not None else "—",
                    "完了日数": f"{d_c}/7日",
                    "最多Problem": w_data["problem"],
                    "次のTry": w_data["next_try"],
                }
            )
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
