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
month = st.sidebar.selectbox("月 (MONTH)", month_options, index=now.month - 1)

day_options = [f"{i:02}" for i in range(1, 32)]
day = st.sidebar.selectbox("日 (DAY)", day_options, index=now.day - 1)

places = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
place_name = st.sidebar.selectbox("競馬場 (PLACE)", list(places.values()), index=1)
place_code = [k for k, v in places.items() if v == place_name][0]

st.sidebar.divider()
st.sidebar.header("分析するレース")

# ==================================================
# レース選択（全レース選択ボタン付き）
# ==================================================
race_labels = [f"{i}R" for i in range(1, 13)]

if "selected_races" not in st.session_state:
    st.session_state["selected_races"] = ["1R"]

c1, c2 = st.sidebar.columns(2)
with c1:
    if st.button("✅ 全レース選択"):
        st.session_state["selected_races"] = race_labels.copy()
with c2:
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
        <div style="margin: 0.25rem 0 0.75rem 0;">
          <button id="copyBtn"
            style="width:100%;padding:12px;font-size:16px;
                   background:#ff4b4b;color:white;border:none;
                   border-radius:10px;cursor:pointer;">
            📎 分析結果をコピー
          </button>
        </div>
        <textarea id="copySrc" style="position:absolute;left:-9999px;top:-9999px;">{safe}</textarea>
        <script>
          document.getElementById("copyBtn").onclick = async () => {{
            try {{
              const ta = document.getElementById("copySrc");
              const decoded = ta.value
                .replaceAll("&amp;", "&")
                .replaceAll("&lt;", "<")
                .replaceAll("&gt;", ">")
                .replaceAll("&quot;", '"')
                .replaceAll("&#x27;", "'");
              await navigator.clipboard.writeText(decoded);
              alert("コピーしました");
            }} catch(e) {{
              alert("コピーに失敗しました（端末/ブラウザ制限の可能性）");
            }}
          }};
        </script>
        """,
        height=90
    )

# ==================================================
# 実行：逐次表示（終わったレースから順に出す）
# ==================================================
if run:
    if not target_races:
        st.warning("レースを選んでください")
    else:
        live = st.container()        # レースごとの表示をここに積む
        result_blocks = []           # 最後にまとめコピー用

        with st.spinner("分析中...（終わったレースから順に表示します）"):
            try:
                # 逐次取得：race_num, block_text
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

                    # レースごとに表示
                    with live:
                        with st.expander(f"{place_name} {race_num}R", expanded=False):
                            st.text_area(
                                f"{place_name} {race_num}R",
                                block,
                                height=280
                            )

                # まとめ保存（コピー用）
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

# ==================================================
# 結果表示（実行後も残る：まとめコピー）
# ==================================================
if "result_text" in st.session_state and st.session_state["result_text"]:
    meta = st.session_state.get("last_meta", {})
    title = f"📋 分析結果（{meta.get('place_name','')} {meta.get('year','')}年{meta.get('month','')}月{meta.get('day','')}日）"
    st.subheader(title)

    _render_copy_button(st.session_state["result_text"])

    st.text_area(
        "コピー用テキスト（ここから手動コピーも可）",
        st.session_state["result_text"],
        height=360
    )
