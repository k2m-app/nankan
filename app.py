import streamlit as st
import keiba_bot  # keiba_bot.py (ロジック部分) を読み込む
from datetime import datetime, timedelta, timezone

# ==========================================
# メインUI
# ==========================================
st.title("🐎 南関東競馬AI予想アプリ")

# ★変更: サイドバーのメニュー選択を削除（予想機能のみにするため）
st.sidebar.header("開催設定")

# --------------------------------------
# 1. 日付初期値の自動設定 (JST)
# --------------------------------------
JST = timezone(timedelta(hours=9))
now = datetime.now(JST)

current_year = str(now.year)
current_month = f"{now.month:02}"
current_day = f"{now.day:02}"

# 年 (YEAR)
year = st.sidebar.text_input("年 (YEAR)", value=current_year)

# 月 (MONTH) - 現在月を初期値に
month_options = [f"{i:02}" for i in range(1, 13)]
try:
    default_month_index = month_options.index(current_month)
except ValueError:
    default_month_index = 0
month = st.sidebar.selectbox("月 (MONTH)", month_options, index=default_month_index)

# 日 (DAY) - 現在日を初期値に
day_options = [f"{i:02}" for i in range(1, 32)]
try:
    default_day_index = day_options.index(current_day)
except ValueError:
    default_day_index = 0
day = st.sidebar.selectbox("日 (DAY)", day_options, index=default_day_index)

# --------------------------------------
# 2. 競馬場選択 (南関4場のみに限定)
# --------------------------------------
places = {
    "10": "大井", 
    "11": "川崎", 
    "12": "船橋", 
    "13": "浦和"
}
# デフォルトを川崎(11)など、好みの場所に設定可能。ここではindex=1(川崎)
place_name = st.sidebar.selectbox("競馬場 (PLACE)", list(places.values()), index=1)
place_code = [k for k, v in places.items() if v == place_name][0]

st.sidebar.header("分析するレースを選択")

# --------------------------------------
# 3. レース選択ロジック (全選択/解除ボタン対応)
# --------------------------------------
# ✅ checkbox の key そのものを初期化（初回だけ）
for i in range(1, 13):
    k = f"race_{i}"
    if k not in st.session_state:
        st.session_state[k] = (i == 1)  # 初期は1RだけON

# ✅ ボタンコールバック：session_stateを書き換える
def select_all_races():
    for i in range(1, 13):
        st.session_state[f"race_{i}"] = True

def clear_all_races():
    for i in range(1, 13):
        st.session_state[f"race_{i}"] = False

col1, col2 = st.sidebar.columns(2)
with col1:
    st.button("全レース選択", on_click=select_all_races)
with col2:
    st.button("全解除", on_click=clear_all_races)

# checkbox表示（value引数は指定せず、keyの状態に依存させる）
selected_races = []
for i in range(1, 13):
    if st.sidebar.checkbox(f"{i}R", key=f"race_{i}"):
        selected_races.append(i)

# --------------------------------------
# 4. 実行処理
# --------------------------------------
st.write(f"### 設定: {year}年 {month}月{day}日 {place_name}")
st.write("サイドバーでレースを選んでから、ボタンを押すと分析を開始します。")

if st.button("分析スタート 🚀"):
    if not selected_races:
        st.warning("少なくとも1つのレースを選んでください。")
    else:
        with st.spinner("分析中...これには数分かかります..."):
            try:
                # 地方競馬用にパラメータセット (year, place_code, month, day)
                keiba_bot.set_race_params(year, place_code, month, day)
                
                # 分析実行
                keiba_bot.run_all_races(target_races=selected_races)
                
                st.success(f"{', '.join(f'{r}R' for r in selected_races)} の分析が完了しました！")
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")
