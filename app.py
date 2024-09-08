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

st.set_page_config(page_title="Slackコミュニティ分析", page_icon="📊", layout="wide")

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

def format_tooltip(items):
    if len(items) == 0:
        return ""
    elif len(items) == 1:
        return items[0]
    elif len(items) == 2:
        return ", ".join(items)
    else:
        first = items[0]
        last = items[-1]
        middle = ", ".join(items[1:-1])
        return f"{first}, <pre>{middle}</pre>, {last}"

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

def calculate_overall_stats(df):
    total_messages = df['posts'].sum()
    total_reactions = df['received_reactions'].sum()
    
    active_users = df.groupby('display_name').agg({
        'posts': 'sum',
        'given_reactions': 'sum'
    }).reset_index()
    active_users['total_activity'] = active_users['posts'] + active_users['given_reactions']
    active_users = active_users[active_users['total_activity'] > 0]
    
    active_users_count = len(active_users)
    active_users_list = active_users['display_name'].tolist()
    
    channels = df['channel_name'].unique()
    total_channels = len(channels)
    
    avg_messages_per_day = df.groupby('day')['posts'].sum().mean()
    avg_reactions_per_message = total_reactions / total_messages if total_messages > 0 else 0
    
    return {
        "総メッセージ数": total_messages,
        "総リアクション数": total_reactions,
        "アクティブユーザー数": active_users_count,
        "アクティブユーザーリスト": active_users_list,
        "アクティブユーザー詳細": active_users,  
        "総チャンネル数": total_channels,
        "チャンネル一覧": channels,
        "1日あたりの平均メッセージ数": avg_messages_per_day,
        "1メッセージあたりの平均リアクション数": avg_reactions_per_message
    }

def calculate_channel_growth(df, start_date, end_date, previous_start_date):
    current_messages = df[(df['day'] >= start_date) & (df['day'] <= end_date)].groupby('channel_name')['posts'].sum()
    previous_messages = df[(df['day'] >= previous_start_date) & (df['day'] < start_date)].groupby('channel_name')['posts'].sum()
    growth = ((current_messages - previous_messages) / previous_messages).fillna(0).sort_values(ascending=False)
    return growth[growth > 0.1]  # 10%以上成長したチャンネルを表示

def calculate_user_growth(df, start_date, end_date, previous_start_date):
    current_messages = df[(df['day'] >= start_date) & (df['day'] <= end_date)].groupby('display_name')['posts'].sum()
    previous_messages = df[(df['day'] >= previous_start_date) & (df['day'] < start_date)].groupby('display_name')['posts'].sum()
    
    previous_messages = previous_messages[previous_messages != 0]
    common_users = current_messages.index.intersection(previous_messages.index)
    growth = pd.Series(
        ((current_messages[common_users] - previous_messages[common_users]) / previous_messages[common_users]).values,
        index=common_users
    )
    
    return growth[growth > 0.5]  # 50%以上成長したユーザーを表示

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
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = Path(tmpdir) / "slack_data.zip"
                zip_path.write_bytes(uploaded_file.getvalue())
                
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmpdir)
                
                stats_by_channel = process_slack_data(tmpdir)
                
                csv_path = Path(tmpdir) / "slack_data_analysis.csv"
                export_csv(stats_by_channel, csv_path)
                
                df = pd.read_csv(csv_path)
        
                st.success("データの処理が完了しました！")
        
        df['day'] = pd.to_datetime(df['day'])
        min_date = df['day'].min().date()
        max_date = df['day'].max().date()
        start_date = st.date_input("開始日", min_date, min_value=min_date, max_value=max_date)
        end_date = st.date_input("終了日", max_date, min_value=min_date, max_value=max_date)

        if start_date <= end_date:
            start_datetime = pd.to_datetime(start_date)
            end_datetime = pd.to_datetime(end_date) + timedelta(days=1) - timedelta(microseconds=1)
            
            df_filtered = df[(df['day'] >= start_datetime) & (df['day'] <= end_datetime)]
            date_diff = end_date - start_date
            previous_start_date = start_date - date_diff - timedelta(days=1)
            previous_start_datetime = pd.to_datetime(previous_start_date)
        else:
            st.error("エラー: 終了日は開始日より後である必要があります。")
            return

        st.header("全体統計")
        
        current_stats = calculate_overall_stats(df_filtered)
        previous_df = df[(df['day'] >= previous_start_datetime) & (df['day'] < start_datetime)]
        previous_stats = calculate_overall_stats(previous_df)
        
        col1, col2, col3 = st.columns(3)
        col1.metric(
            "総メッセージ数", 
            f"{current_stats['総メッセージ数']:,}", 
            f"{current_stats['総メッセージ数'] - previous_stats['総メッセージ数']:,}",
            help="期間中に投稿された全てのメッセージの合計数です。"
        )
        col2.metric(
            "総リアクション数", 
            f"{current_stats['総リアクション数']:,}", 
            f"{current_stats['総リアクション数'] - previous_stats['総リアクション数']:,}",
            help="期間中に行われた全てのリアクションの合計数です。"
        )

        active_users_df = current_stats['アクティブユーザー詳細']
        active_users_list = current_stats['アクティブユーザーリスト']
        active_users_tooltip = format_tooltip(active_users_list)
        col3.metric(
            "アクティブユーザー数", 
            f"{current_stats['アクティブユーザー数']:,}", 
            f"{current_stats['アクティブユーザー数'] - previous_stats['アクティブユーザー数']:,}",
            help="期間中に少なくとも1回以上の投稿またはリアクションを行ったユニークユーザーの数です。" + active_users_tooltip
        )

        col4, col5, col6 = st.columns(3)
        channels_tooltip = format_tooltip(current_stats['チャンネル一覧'])
        col4.metric(
            "総チャンネル数", 
            f"{current_stats['総チャンネル数']:,}", 
            f"{current_stats['総チャンネル数'] - previous_stats['総チャンネル数']:,}",
            help="アクティビティのあった全チャンネルの数です。" + channels_tooltip
        )
        col5.metric(
            "1日あたりの平均メッセージ数", 
            f"{current_stats['1日あたりの平均メッセージ数']:.2f}", 
            f"{current_stats['1日あたりの平均メッセージ数'] - previous_stats['1日あたりの平均メッセージ数']:.2f}",
            help="総メッセージ数を日数で割った値です。"
        )
        col6.metric(
            "1メッセージあたりの平均リアクション数", 
            f"{current_stats['1メッセージあたりの平均リアクション数']:.2f}", 
            f"{current_stats['1メッセージあたりの平均リアクション数'] - previous_stats['1メッセージあたりの平均リアクション数']:.2f}",
            help="総リアクション数を総メッセージ数で割った値です。"
        )

        st.subheader("アクティブユーザー詳細")
        st.info("このグラフは各ユーザーの活動状況を視覚化しています。X軸はメッセージ数、Y軸はリアクション数、円の大きさは総アクティビティ（メッセージ数 + リアクション数）を表しています。マウスオーバーでユーザー名と詳細な数値を確認できます。")
        fig = px.scatter(active_users_df, x='posts', y='given_reactions', 
                         size='total_activity', hover_name='display_name', 
                         labels={'posts': 'メッセージ数', 'given_reactions': 'リアクション数'},
                         title='ユーザーアクティビティ')
        st.plotly_chart(fig, use_container_width=True)

        st.header("チャンネル分析")
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("トップ10チャンネル（メッセージ数）")
            st.info("期間中にメッセージ数が最も多かった上位10チャンネルを表示しています。")
            top_channels = df_filtered.groupby('channel_name')['posts'].sum().sort_values(ascending=False).head(10)
            fig = px.bar(top_channels, x=top_channels.index, y=top_channels.values)
            fig.update_layout(xaxis_title="チャンネル名", yaxis_title="メッセージ数")
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("チャンネル成長率")
            st.info("チャンネルの成長率を表示しています。成長率 = (現在期間のメッセージ数 - 前期間のメッセージ数) / 前期間のメッセージ数。10%以上成長したチャンネルのみ表示しています。")
            channel_growth = calculate_channel_growth(df, start_datetime, end_datetime, previous_start_datetime)
            channel_growth_df = channel_growth.reset_index()
            channel_growth_df.columns = ['channel_name', 'growth_rate']
            fig = px.bar(channel_growth_df, x='channel_name', y='growth_rate')
            fig.update_layout(xaxis_title="チャンネル名", yaxis_title="成長率")
            st.plotly_chart(fig, use_container_width=True)

        st.header("ユーザーエンゲージメント分析")
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("トップ10アクティブユーザー")
            st.info("期間中に最も多くのメッセージを投稿した上位10ユーザーを表示しています。")
            top_users = df_filtered.groupby('display_name')['posts'].sum().sort_values(ascending=False).head(10)
            fig = px.bar(top_users, x=top_users.index, y='posts')
            fig.update_layout(xaxis_title="ユーザー名", yaxis_title="投稿数")
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("ユーザー成長率")
            st.info("ユーザーの成長率を表示しています。成長率 = (現在期間の投稿数 - 前期間の投稿数) / 前期間の投稿数。50%以上成長したユーザーのみ表示しています。前期間の投稿が0の場合は除外されます。")
            user_growth = calculate_user_growth(df, start_datetime, end_datetime, previous_start_datetime)
            user_growth_df = user_growth.reset_index()
            user_growth_df.columns = ['display_name', 'growth_rate']
            fig = px.bar(user_growth_df, x='display_name', y='growth_rate')
            fig.update_layout(xaxis_title="ユーザー名", yaxis_title="成長率")
            st.plotly_chart(fig, use_container_width=True)

        st.header("時系列分析")
        st.info("期間中のメッセージ数の推移を日単位で確認できます。週末や特定のイベント日などにどのような変化があるか観察できます。")
        activity_over_time = df_filtered.groupby('day')['posts'].sum().reset_index()
        fig = px.line(activity_over_time, x='day', y='posts')
        fig.update_layout(xaxis_title="日付", yaxis_title="メッセージ数")
        st.plotly_chart(fig, use_container_width=True)

        st.header("曜日別分析")
        st.info("各曜日のメッセージ数を比較できます。平日と週末の違いや、特に活発な曜日を識別するのに役立ちます。")
        df_filtered['weekday'] = df_filtered['day'].dt.day_name()
        weekday_activity = df_filtered.groupby('weekday')['posts'].sum().reindex(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'])
        fig = px.bar(weekday_activity, x=weekday_activity.index, y=weekday_activity.values)
        fig.update_layout(xaxis_title="曜日", yaxis_title="メッセージ数")
        st.plotly_chart(fig, use_container_width=True)

        st.header("AIによる総合分析レポート")
        st.info("このセクションでは、Google の Gemini Pro AI モデルを使用して、上記のデータに基づいた総合的な分析レポートを生成します。レポートには、コミュニティの健全性、成長傾向、活動パターン、ユーザーエンゲージメントの特徴、そして改善のための提案が含まれます。")
        if st.button("レポートを生成"):
            with st.spinner("AIがレポートを生成中です..."):
                report = generate_report(df, start_datetime, end_datetime, previous_start_datetime)
                if report:
                    st.markdown(report)
                else:
                    st.error("レポートの生成に失敗しました。もう一度お試しください。")
                    
        # csv = df.to_csv(index=False)
        # st.download_button(
        #     label="処理済みデータをCSVでダウンロード",
        #     data=csv,
        #     file_name="slack_analysis_results.csv",
        #     mime="text/csv",
        # )

if __name__ == "__main__":
    main()