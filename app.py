import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import google.generativeai as genai
import os
import zipfile
import json
import csv
from pathlib import Path
import tempfile

st.set_page_config(page_title="Slackコミュニティ分析", page_icon="😄", layout="wide")

# Gemini APIの設定
api_key = os.environ.get("API_KEY")
genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-pro')

class User:
    def __init__(self, id, name, display_name, is_restricted, deleted):
        self.id = id
        self.name = name
        self.display_name = display_name
        self.is_restricted = is_restricted
        self.deleted = deleted

class Stats:
    def __init__(self, user_id, name, display_name, is_restricted, deleted):
        self.user_id = user_id
        self.name = name
        self.display_name = display_name
        self.posts = 0
        self.given_reactions = 0
        self.given_reaction_users = set()
        self.received_reactions = 0
        self.received_reaction_users = set()
        self.is_restricted = is_restricted
        self.deleted = deleted

def load_users(users_file):
    with open(users_file, 'r') as f:
        users_data = json.load(f)
    
    return {user['id']: User(user['id'], user.get('name', ''), 
                             user['profile']['display_name'], 
                             user.get('is_restricted', False), 
                             user.get('deleted', False)) 
            for user in users_data}

def read_messages_from_json_file(file_path):
    with open(file_path, 'r') as f:
        return json.load(f)

def update_stats(stats_by_channel, channel_name, messages, users):
    for message in messages:
        if not message.get('ts'):
            continue
        
        date = datetime.fromtimestamp(float(message['ts'])).strftime('%Y-%m-%d')
        user_id = message.get('user')
        
        if user_id not in users:
            continue
        
        if channel_name not in stats_by_channel:
            stats_by_channel[channel_name] = {}
        
        if date not in stats_by_channel[channel_name]:
            stats_by_channel[channel_name][date] = {}
        
        if user_id not in stats_by_channel[channel_name][date]:
            user = users[user_id]
            stats_by_channel[channel_name][date][user_id] = Stats(
                user.id, user.name, user.display_name, user.is_restricted, user.deleted
            )
        
        stats = stats_by_channel[channel_name][date][user_id]
        stats.posts += 1
        
        for reaction in message.get('reactions', []):
            for reacting_user in reaction['users']:
                if reacting_user not in stats_by_channel[channel_name][date]:
                    if reacting_user not in users:
                        continue
                    user = users[reacting_user]
                    stats_by_channel[channel_name][date][reacting_user] = Stats(
                        user.id, user.name, user.display_name, user.is_restricted, user.deleted
                    )
                
                reacting_stats = stats_by_channel[channel_name][date][reacting_user]
                reacting_stats.given_reactions += 1
                reacting_stats.given_reaction_users.add(user_id)
                
                stats.received_reactions += 1
                stats.received_reaction_users.add(reacting_user)

def process_slack_data(base_path):
    base_path = Path(base_path)
    stats_by_channel = {}
    
    users = load_users(base_path / 'users.json')
    
    for channel_dir in base_path.iterdir():
        if channel_dir.is_dir() and channel_dir.name != base_path.name:
            for json_file in channel_dir.glob('*.json'):
                messages = read_messages_from_json_file(json_file)
                update_stats(stats_by_channel, channel_dir.name, messages, users)
    
    return stats_by_channel

def export_csv(stats_by_channel, output_file):
    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['display_name', 'name', 'is_restricted', 'deleted', 'day', 'posts',
                         'received_reactions', 'received_reaction_users', 'given_reactions',
                         'given_reaction_users', 'channel_name'])
        
        for channel_name, days in stats_by_channel.items():
            for day, users_stats in days.items():
                for stats in users_stats.values():
                    writer.writerow([
                        stats.display_name,
                        stats.name,
                        stats.is_restricted,
                        stats.deleted,
                        day,
                        stats.posts,
                        stats.received_reactions,
                        len(stats.received_reaction_users),
                        stats.given_reactions,
                        len(stats.given_reaction_users),
                        channel_name
                    ])

# 全体統計の計算
def calculate_overall_stats(df):
    total_messages = len(df)
    total_reactions = df['received_reactions'].sum()
    active_users = df['display_name'].nunique()
    total_channels = df['channel_name'].nunique()
    avg_messages_per_day = df.groupby('day')['posts'].sum().mean()
    avg_reactions_per_message = total_reactions / total_messages if total_messages > 0 else 0
    
    return {
        "総メッセージ数": total_messages,
        "総リアクション数": total_reactions,
        "アクティブユーザー数": active_users,
        "総チャンネル数": total_channels,
        "1日あたりの平均メッセージ数": avg_messages_per_day,
        "1メッセージあたりの平均リアクション数": avg_reactions_per_message
    }

# チャンネル成長率の計算
def calculate_channel_growth(df, start_date, end_date, previous_start_date):
    current_messages = df[(df['day'] >= start_date) & (df['day'] <= end_date)].groupby('channel_name')['posts'].sum()
    previous_messages = df[(df['day'] >= previous_start_date) & (df['day'] < start_date)].groupby('channel_name')['posts'].sum()
    growth = ((current_messages - previous_messages) / previous_messages).fillna(0).sort_values(ascending=False)
    return growth[growth > 0.1]  # 10%以上成長したチャンネルを表示

# ユーザー成長率の計算
def calculate_user_growth(df, start_date, end_date, previous_start_date):
    current_messages = df[(df['day'] >= start_date) & (df['day'] <= end_date)].groupby('display_name')['posts'].sum()
    previous_messages = df[(df['day'] >= previous_start_date) & (df['day'] < start_date)].groupby('display_name')['posts'].sum()
    
    # previous_messages が 0 のユーザーを除外
    previous_messages = previous_messages[previous_messages != 0]

    # 共通のユーザーのみを使用
    common_users = current_messages.index.intersection(previous_messages.index)

    # 成長率を計算
    growth = ((current_messages[common_users] - previous_messages[common_users]) / previous_messages[common_users]).fillna(0).sort_values(ascending=False)
    
    return growth[growth > 0.5]  # 50%以上成長したユーザーを表示

# レポート生成関数
def generate_report(df, start_date, end_date, previous_start_date):
    current_df = df[(df['day'] >= start_date) & (df['day'] <= end_date)]
    previous_df = df[(df['day'] >= previous_start_date) & (df['day'] < start_date)]
    
    current_stats = calculate_overall_stats(current_df)
    previous_stats = calculate_overall_stats(previous_df)
    
    channel_growth = calculate_channel_growth(df, start_date, end_date, previous_start_date)
    user_growth = calculate_user_growth(df, start_date, end_date, previous_start_date)
    
    top_channels = current_df.groupby('channel_name')['posts'].sum().sort_values(ascending=False).head(5)
    top_users = current_df.groupby('display_name')['posts'].sum().sort_values(ascending=False).head(5)

    report_data = f"""
    Slackコミュニティ分析レポート（{start_date.strftime('%Y-%m-%d')} から {end_date.strftime('%Y-%m-%d')}まで）

    1. 全体統計:
       現在の期間:
       {current_stats}
       
       前の期間 ({previous_start_date.strftime('%Y-%m-%d')} から {start_date.strftime('%Y-%m-%d')}まで):
       {previous_stats}

    2. チャンネル分析:
       - トップ5チャンネル（メッセージ数）:
         {top_channels.to_string()}
       - 成長率の高いチャンネル:
         {channel_growth.to_string()}

    3. ユーザーエンゲージメント:
       - トップ5ユーザー（メッセージ数）:
         {top_users.to_string()}
       - 成長率の高いユーザー:
         {user_growth.to_string()}

    このデータに基づいて、以下の点を含む総合的な分析を提供してください:

    1. 全体的なコミュニティの健全性と成長傾向（前の期間との比較を含む）
    2. チャンネルの活動パターンと新たなトレンド
    3. ユーザーエンゲージメントの特徴と変化
    4. 投稿の多い日にちや曜日
    5. 今後の改善点

    あなたは、データサイエンティストです。レポートは、コミュニティマネージャーやステークホルダーへの提示に適した、明確で実用的な内容にしてください。
    """

    try:
        response = model.generate_content(report_data)
        return response.text
    except Exception as e:
        st.error(f"Gemini APIのエラー: {str(e)}")
        return None
    
def main():
    st.title('Slackコミュニティデータ分析')
    
    uploaded_file = st.file_uploader("Slackデータのzipファイルをアップロードしてください", type="zip")
    if uploaded_file is not None:
        with st.spinner("データを処理中..."):
            # Create a temporary directory
            with tempfile.TemporaryDirectory() as tmpdir:
                # Save the uploaded file
                zip_path = Path(tmpdir) / "slack_data.zip"
                zip_path.write_bytes(uploaded_file.getvalue())
                
                # Unzip the file
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmpdir)
                
                # Process the data
                stats_by_channel = process_slack_data(tmpdir)
                
                # Export to CSV
                csv_path = Path(tmpdir) / "slack_data_analysis.csv"
                export_csv(stats_by_channel, csv_path)
                
                # Load the CSV file
                df = pd.read_csv(csv_path)
        
                st.success("データの処理が完了しました！")
        
        # 日付範囲の選択
        df['day'] = pd.to_datetime(df['day'])
        min_date = df['day'].min().date()
        max_date = df['day'].max().date()
        start_date = st.date_input("開始日", min_date, min_value=min_date, max_value=max_date)
        end_date = st.date_input("終了日", max_date, min_value=min_date, max_value=max_date)

        if start_date <= end_date:
            # start_dateとend_dateをdatetime64[ns]型に変換
            start_datetime = pd.to_datetime(start_date)
            end_datetime = pd.to_datetime(end_date) + timedelta(days=1) - timedelta(microseconds=1)
            
            df_filtered = df[(df['day'] >= start_datetime) & (df['day'] <= end_datetime)]
            date_diff = end_date - start_date
            previous_start_date = start_date - date_diff - timedelta(days=1)
            previous_start_datetime = pd.to_datetime(previous_start_date)
        else:
            st.error("エラー: 終了日は開始日より後である必要があります。")
            return

        # 全体統計
        st.header("全体統計")
        current_stats = calculate_overall_stats(df_filtered)
        previous_df = df[(df['day'] >= previous_start_datetime) & (df['day'] < start_datetime)]
        previous_stats = calculate_overall_stats(previous_df)
    
        col1, col2, col3 = st.columns(3)
        col1.metric("総メッセージ数", f"{current_stats['総メッセージ数']:,}", f"{current_stats['総メッセージ数'] - previous_stats['総メッセージ数']:,}")
        col2.metric("総リアクション数", f"{current_stats['総リアクション数']:,}", f"{current_stats['総リアクション数'] - previous_stats['総リアクション数']:,}")
        col3.metric("アクティブユーザー数", f"{current_stats['アクティブユーザー数']:,}", f"{current_stats['アクティブユーザー数'] - previous_stats['アクティブユーザー数']:,}")

        col4, col5, col6 = st.columns(3)
        col4.metric("総チャンネル数", f"{current_stats['総チャンネル数']:,}", f"{current_stats['総チャンネル数'] - previous_stats['総チャンネル数']:,}")
        col5.metric("1日あたりの平均メッセージ数", f"{current_stats['1日あたりの平均メッセージ数']:.2f}", f"{current_stats['1日あたりの平均メッセージ数'] - previous_stats['1日あたりの平均メッセージ数']:.2f}")
        col6.metric("1メッセージあたりの平均リアクション数", f"{current_stats['1メッセージあたりの平均リアクション数']:.2f}", f"{current_stats['1メッセージあたりの平均リアクション数'] - previous_stats['1メッセージあたりの平均リアクション数']:.2f}")

        # チャンネル分析
        st.header("チャンネル分析")
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("トップ10チャンネル（メッセージ数）")
            top_channels = df_filtered.groupby('channel_name')['posts'].sum().sort_values(ascending=False).head(10)
            fig = px.bar(top_channels, x=top_channels.index, y=top_channels.values)
            fig.update_layout(xaxis_title="チャンネル名", yaxis_title="メッセージ数")
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("チャンネル成長率")
            channel_growth = calculate_channel_growth(df, start_datetime, end_datetime, previous_start_datetime)
            fig = px.bar(channel_growth, x=channel_growth.index, y=channel_growth.values)
            fig.update_layout(xaxis_title="チャンネル名", yaxis_title="成長率")
            st.plotly_chart(fig, use_container_width=True)

        # ユーザーエンゲージメント分析
        st.header("ユーザーエンゲージメント分析")
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("トップ10アクティブユーザー")
            top_users = df_filtered.groupby('display_name')['posts'].sum().sort_values(ascending=False).head(10)
            fig = px.bar(top_users, x=top_users.index, y='posts')
            fig.update_layout(xaxis_title="ユーザー名", yaxis_title="投稿数")
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("ユーザー成長率")
            user_growth = calculate_user_growth(df, start_datetime, end_datetime, previous_start_datetime)
            fig = px.bar(user_growth, x=user_growth.index, y=user_growth.values)
            fig.update_layout(xaxis_title="ユーザー名", yaxis_title="成長率")
            st.plotly_chart(fig, use_container_width=True)

        # 時系列分析
        st.header("時系列分析")
        activity_over_time = df_filtered.groupby('day')['posts'].sum().reset_index()
        fig = px.line(activity_over_time, x='day', y='posts')
        fig.update_layout(xaxis_title="日付", yaxis_title="メッセージ数")
        st.plotly_chart(fig, use_container_width=True)

        # 曜日別・時間帯別分析
        st.header("曜日別・時間帯別分析")
        df_filtered['weekday'] = df_filtered['day'].dt.day_name()
        weekday_activity = df_filtered.groupby('weekday')['posts'].sum().reindex(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'])
        fig = px.bar(weekday_activity, x=weekday_activity.index, y=weekday_activity.values)
        fig.update_layout(xaxis_title="曜日", yaxis_title="メッセージ数")
        st.plotly_chart(fig, use_container_width=True)


        # AIレポート生成
        st.header("AIによる総合分析レポート")
        if st.button("レポートを生成"):
            with st.spinner("AIがレポートを生成中です..."):
                report = generate_report(df, start_datetime, end_datetime, previous_start_datetime)
                if report:
                    st.markdown(report)
                else:
                    st.error("レポートの生成に失敗しました。もう一度お試しください。")
                    
        # Download processed data
        csv = df.to_csv(index=False)
        st.download_button(
            label="処理済みデータをCSVでダウンロード",
            data=csv,
            file_name="slack_analysis_results.csv",
            mime="text/csv",
        )

if __name__ == "__main__":
    main()