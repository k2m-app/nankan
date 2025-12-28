import streamlit as st
import streamlit.components.v1 as components
import keiba_bot  # keiba_bot.py（ロジック部分）
from datetime import datetime, timedelta, timezone
import html
import re

# ==================================================
# ページ設定（スマホ見やすさに効く）
# ==================================================
st.set_page_config(
    page_title="NANKAN AI",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ちょいCSS：スマホで余白/ボタンを押しやすく
st.markdown(
    """
    <style>
      .block-container { padding-top: 1rem; padding-bottom: 2rem; }
      .stButton>button { width: 100%; padding: 0.8rem 1rem; font-size: 1.05rem; }
      .stTextArea textarea { font-size: 0.98rem; line-height: 1.45; }
      .small-muted { color: #666; font-size: 0.9rem; }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("NANKAN AI")

# ==================================================
# サイドバー：開催設定
# ==================================================
st.sidebar.header("開催設定")

JST = timezone(timedelta(hours=9))
now = datetime.now(JST)

current_year = str(now.year)
current_month = f"{now.month:02}"
current_day = f"{now.day:02}"

# 年 (YEAR)
year = st.sidebar.text_input("年 (YEAR)", value=current_year)

# 月 (MONTH)
month_options = [f"{i:02}" for i in range(1, 13)]
default_month_index = month_options.index(current_month) if current_month in month_options else 0
month = st.sidebar.selectbox("月 (MONTH)", month_options, index=default_month_index)

# 日 (DAY)
day_options = [f"{i:02}" for i in range(1, 32)]
default_day_index = day_options.index(current_day) if current_day in day_options else 0
day = st.sidebar.selectbox("日 (DAY)", day_options, index=default_day_index)

# 競馬場（南関4場）
places = {
    "10": "大井",
    "11": "川崎",
    "12": "船橋",
    "13": "浦和"
}
place_name = st.sidebar.selectbox("競馬場 (PLACE)", list(places.values()), index=1)
place_code = [k for k, v in places.items() if v == place_name][0]

st.sidebar.divider()
st.sidebar.header("分析するレース")

# ✅ スマホでチェックボックス連打を避ける：multiselect
race_labels = [f"{i}R" for i in range(1, 13)]
default_races = st.session_state.get("default_races", ["1R"])

selected_race_labels = st.sidebar.multiselect(
    "レースを選択（複数可）",
    race_labels,
    default=default_races
)

# 次回起動時のデフォルト保持（UX改善）
st.session_state["default_races"] = selected_race_labels if selected_race_labels else ["1R"]

# 解析対象の set[int]
target_races = {int(x.replace("R", "")) for x in selected_race_labels}

st.sidebar.caption("※ 設定後、下の「分析スタート」で実行します。")

# ==================================================
# メイン：実行/表示
# ==================================================
st.write(f"### 設定: {year}年 {month}月{day}日 {place_name}")
st.markdown('<div class="small-muted">結果コピーは下</div>', unsafe_allow_html=True)
st.write("")

run = st.button("分析スタート 🚀")

def _normalize_text(s: str) -> str:
    """表示・コピー用に軽く整形（任意）"""
    if not isinstance(s, str):
        s = str(s)
    s = s.replace("\r\n", "\n")
    # 連続空行を最大2つまでに
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _render_copy_button(text: str, button_label: str = "📎 分析結果をコピー"):
    """
    クリップボードコピー（ワンクリック）
    ※ 文字列にバッククォート等が混ざると壊れるので HTML escape + JS文字列化
    """
    safe = html.escape(text)
    # JSでHTMLエスケープを戻してコピーする（textarea経由）
    components.html(
        f"""
        <div style="margin: 0.25rem 0 0.75rem 0;">
          <button id="copyBtn"
            style="
              width:100%;
              padding:12px;
              font-size:16px;
              background:#ff4b4b;
              color:white;
              border:none;
              border-radius:10px;
              cursor:pointer;">
            {button_label}
          </button>
          <div id="copyMsg" style="margin-top:8px; font-size:0.9rem; color:#666;"></div>
        </div>

        <textarea id="copySrc" style="position:absolute; left:-9999px; top:-9999px;">{safe}</textarea>

        <script>
          const btn = document.getElementById("copyBtn");
          const msg = document.getElementById("copyMsg");
          btn.addEventListener("click", async () => {{
            try {{
              const ta = document.getElementById("copySrc");
              // HTMLエスケープをデコード（ブラウザに任せる）
              const decoded = ta.value
                .replaceAll("&amp;", "&")
                .replaceAll("&lt;", "<")
                .replaceAll("&gt;", ">")
                .replaceAll("&quot;", '"')
                .replaceAll("&#x27;", "'");

              await navigator.clipboard.writeText(decoded);
              msg.innerText = "✅ コピーしました";
              setTimeout(() => msg.innerText = "", 1600);
            }} catch(e) {{
              msg.innerText = "⚠️ コピーに失敗（端末の制限があるかも）";
              setTimeout(() => msg.innerText = "", 2200);
            }}
          }});
        </script>
        """,
        height=95
    )

if run:
    if not target_races:
        st.warning("レースを選んでください")
    else:
        with st.spinner("分析中..."):
            try:
                # ✅ keiba_bot 側が「結果文字列を return」する想定
                result_text = keiba_bot.run_all_races(
                    year=str(year),
                    month=str(month),
                    day=str(day),
                    place_code=str(place_code),
                    target_races=target_races
                )

                result_text = _normalize_text(result_text)
                st.session_state["result_text"] = result_text
                st.session_state["last_meta"] = {
                    "year": year, "month": month, "day": day,
                    "place_name": place_name,
                    "races": sorted(list(target_races))
                }

                st.success(f"{place_name}：{', '.join(f'{r}R' for r in sorted(target_races))} の分析が完了しました！")

            except Exception as e:
                st.error(f"エラーが発生しました: {e}")

# ==================================================
# 結果表示（実行後に残る）
# ==================================================
if "result_text" in st.session_state and st.session_state["result_text"]:
    meta = st.session_state.get("last_meta", {})
    title = f"📋 分析結果（{meta.get('place_name','')} {meta.get('year','')}年{meta.get('month','')}月{meta.get('day','')}日）"
    st.subheader(title)

    _render_copy_button(st.session_state["result_text"])

    st.text_area(
        "コピー用テキスト（ここから手動コピーも可）",
        st.session_state["result_text"],
        height=340
    )
