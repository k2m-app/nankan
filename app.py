import streamlit as st
import keiba_bot  # keiba_bot.py (ロジック部分) を読み込む

# Supabase と日付用
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# ==========================================
# 設定 & Supabase クライアント
# ==========================================
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

@st.cache_resource
def get_supabase_client() -> Client:
    """Supabase クライアントを1回だけ作って使い回す"""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def show_history():
    """直近1週間の履歴を Supabase から取り出して表示する"""
    supabase = get_supabase_client()
    if supabase is None:
        st.error("Supabase の設定がされていないため、履歴を表示できません。")
        st.info("streamlit の Secrets に SUPABASE_URL と SUPABASE_ANON_KEY を追加してください。")
        return

    # 直近7日間のデータを取得
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    seven_days_ago_iso = seven_days_ago.isoformat()

    try:
        res = (
            supabase
            .table("history")
            .select("*")
            .gte("created_at", seven_days_ago_iso)
            .order("created_at", desc=True)
            .execute()
        )
        rows = res.data
    except Exception as e:
        st.error(f"履歴の取得に失敗しました: {e}")
        return

    st.subheader("直近1週間の履歴")

    if not rows:
        st.info("直近1週間の履歴はまだありません。")
        return

    for row in rows:
        # DBから安全に値を取得
        r_year = row.get('year', '')
        r_place = row.get('place_name', '')
        r_month = row.get('month', '')
        r_day = row.get('day', '')
        r_race = row.get('race_num', '')
        r_id = row.get('race_id', '')
        
        # タイトル表示用: 日付が取れれば使用、なければ作成日時
        if r_month and r_day:
            date_str = f"{r_year}/{r_month}/{r_day}"
        else:
            date_str = row.get('created_at', '')[:10]

        title = f"{date_str} / {r_place} {r_race}R"
        
        with st.expander(title):
            st.write(f"**作成日時**: {row.get('created_at', '')}")
            st.write(f"**開催**: {r_year}年 {r_place} {r_month}月{r_day}日")
            st.write(f"**レース**: {r_race}R（ID: {r_id}）")
            st.write("---")
            st.write("**AI予想結果**")
            st.write(row.get("output_text", ""))


# ==========================================
# メインUI
# ==========================================
# ★変更点1: タイトルを南関東競馬に変更
st.title("🐎 南関東競馬AI予想アプリ")
mode = st.sidebar.radio("メニュー", ["予想する", "直近1週間の履歴を見る"])

if mode == "予想する":
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
    # 2. 競馬場選択 (★変更点2: 南関4場のみに限定)
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

elif mode == "直近1週間の履歴を見る":
    show_history()
