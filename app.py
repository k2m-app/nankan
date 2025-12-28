import streamlit as st
import streamlit.components.v1 as components
import keiba_bot
from datetime import datetime, timedelta, timezone
import html
import re

# ==================================================
# ページ設定
# ==================================================
st.set_page_config(
    page_title="NANKAN AI",
    layout="wide",
    initial_sidebar_state="expanded",
)

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

year = st.sidebar.text_input("年 (YEAR)", value=str(now.year))

month_options = [f"{i:02}" for i in range(1, 13)]
month = st.sidebar.selectbox(
    "月 (MONTH)",
    month_options,
    index=now.month - 1
)

day_options = [f"{i:02}" for i in range(1, 32)]
day = st.sidebar.selectbox(
    "日 (DAY)",
    day_options,
    index=now.day - 1
)

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

# ==================================================
# レース選択（全レース選択ボタン付き）
# ==================================================
race_labels = [f"{i}R" for i in range(1, 13)]

# session_state 初期化
if "selected_races" not in st.session_state:
    st.session_state["selected_races"] = ["1R"]

col1, col2 = st.sidebar.columns(2)

with col1:
    if st.button("✅ 全レース選択"):
        st.session_state["selected_races"] = race_labels.copy()

with col2:
    if st.button("❌ クリア"):
        st.session_state["selected_races"] = []

selected_race_labels = st.sidebar.multiselect(
    "レースを選択（複数可）",
    race_labels,
    key="selected_races"
)

target_races = {int(r.replace("R", "")) for r in selected_race_labels}

st.sidebar.caption("※ 設定後、下の「分析スタート」で実行します。")

# ==================================================
# メイン
# ==================================================
st.write(f"### 設定: {year}年 {month}月{day}日 {place_name}")
st.markdown('<div class="small-muted">結果コピーは下</div>', unsafe_allow_html=True)
st.write("")

run = st.button("分析スタート 🚀")

def _normalize_text(s: str) -> str:
    if not isinstance(s, str):
        s = str(s)
    s = s.replace("\r\n", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _render_copy_button(text: str):
    safe = html.escape(text)
    components.html(
        f"""
        <button id="copyBtn"
          style="width:100%;padding:12px;font-size:16px;
                 background:#ff4b4b;color:white;border:none;
                 border-radius:10px;cursor:pointer;">
          📎 分析結果をコピー
        </button>
        <textarea id="copySrc" style="position:absolute;left:-9999px;">{safe}</textarea>
        <script>
          document.getElementById("copyBtn").onclick = async () => {{
            const ta = document.getElementById("copySrc");
            const decoded = ta.value
              .replaceAll("&amp;", "&")
              .replaceAll("&lt;", "<")
              .replaceAll("&gt;", ">")
              .replaceAll("&quot;", '"')
              .replaceAll("&#x27;", "'");
            await navigator.clipboard.writeText(decoded);
            alert("コピーしました");
          }};
        </script>
        """,
        height=80
    )

if run:
    if not target_races:
        st.warning("レースを選んでください")
    else:
        with st.spinner("分析中..."):
            try:
                if run:
    if not target_races:
        st.warning("レースを選んでください")
    else:
        # 逐次表示エリア
        live = st.container()
        result_blocks = []

        with st.spinner("分析中...（終わったレースから順に表示します）"):
            try:
                for race_num, block in keiba_bot.run_races_iter(
                    year=str(year),
                    month=str(month),
                    day=str(day),
                    place_code=str(place_code),
                    target_races=target_races,
                    ui=False
                ):
                    block = _normalize_text(block)
                    result_blocks.append(block)

                    # ここでレースごとに表示（終わった順）
                    with live:
                        with st.expander(f"{place_name} {race_num}R", expanded=(race_num == min(target_races))):
                            st.text_area(
                                f"{place_name} {race_num}R",
                                block,
                                height=260
                            )

                # コピー用に最後に結合して保存
                result_text = _normalize_text("\n\n".join(result_blocks))
                st.session_state["result_text"] = result_text
                st.session_state["last_meta"] = {
                    "year": year, "month": month, "day": day,
                    "place_name": place_name,
                    "races": sorted(list(target_races))
                }

                st.success(f"{place_name}：{', '.join(f'{r}R' for r in sorted(target_races))} の分析が完了しました！")

            except Exception as e:
                st.error(f"エラーが発生しました: {e}")

                result_text = _normalize_text(result_text)
                st.session_state["result_text"] = result_text
                st.success(
                    f"{place_name}：{', '.join(f'{r}R' for r in sorted(target_races))} の分析が完了しました！"
                )
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")

# ==================================================
# 結果表示
# ==================================================
if "result_text" in st.session_state and st.session_state["result_text"]:
    st.subheader("📋 分析結果")
    _render_copy_button(st.session_state["result_text"])
    st.text_area(
        "コピー用テキスト",
        st.session_state["result_text"],
        height=360
    )
