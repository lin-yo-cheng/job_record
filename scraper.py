"""
job_104 每日排程爬蟲
讀取所有使用者的 saved_searches，爬 104，寫入 jobs / search_job_matches / job_status，
並處理「最新資料」狀態轉換跟下架驗證邏輯。
"""

import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    env = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env(os.path.join(SCRIPT_DIR, '.env'))
SUPABASE_URL = ENV['SUPABASE_URL'].rstrip('/')
SERVICE_KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
COOKIE = ENV['JOB104_COOKIE']

# 測試用：限制每組搜尋最多爬幾頁（None = 不限制，正式跑要拿掉或設 None）
MAX_PAGES = int(ENV['SCRAPER_MAX_PAGES']) if ENV.get('SCRAPER_MAX_PAGES') else None

REST_URL = f'{SUPABASE_URL}/rest/v1'
SB_HEADERS = {
    'apikey': SERVICE_KEY,
    'Authorization': f'Bearer {SERVICE_KEY}',
    'Content-Type': 'application/json',
}

JOB_HEADERS_BASE = {
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
    ),
    'Cookie': COOKIE,
}

AREA_CODE = {
    '台北': '6001001000', '新北': '6001002000', '桃園': '6001005000',
    '台中': '6001008000', '台南': '6001014000', '高雄': '6001016000',
    '新竹': '6001006000',
    '台北中正': '6001001001', '台北大同': '6001001002', '台北中山': '6001001003',
    '台北松山': '6001001004', '台北大安': '6001001005', '台北萬華': '6001001006',
    '台北信義': '6001001007', '台北士林': '6001001008', '台北北投': '6001001009',
    '台北內湖': '6001001010', '台北南港': '6001001011', '台北文山': '6001001012',
    '新北板橋': '6001002003', '新北汐止': '6001002004', '新北新店': '6001002011',
    '新北永和': '6001002014', '新北中和': '6001002015', '新北土城': '6001002016',
    '新北三峽': '6001002017', '新北樹林': '6001002018', '新北三重': '6001002020',
    '新北新莊': '6001002021', '新北林口': '6001002023', '新北蘆洲': '6001002024',
    '新北五股': '6001002025', '新北淡水': '6001002027',
    '高雄新興': '6001016001', '高雄前金': '6001016002', '高雄苓雅': '6001016003',
    '高雄鹽埕': '6001016004', '高雄鼓山': '6001016005', '高雄旗津': '6001016006',
    '高雄前鎮': '6001016007', '高雄三民': '6001016008', '高雄楠梓': '6001016009',
    '高雄小港': '6001016010', '高雄左營': '6001016011', '高雄鳳山': '6001016024',
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- Supabase REST helpers ----------

def sb_get(table, params=None):
    res = requests.get(f'{REST_URL}/{table}', headers=SB_HEADERS, params=params, timeout=30)
    res.raise_for_status()
    return res.json()


def sb_insert(table, rows):
    if not rows:
        return []
    headers = {**SB_HEADERS, 'Prefer': 'return=representation'}
    res = requests.post(f'{REST_URL}/{table}', headers=headers, json=rows, timeout=30)
    res.raise_for_status()
    return res.json()


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return []
    headers = {**SB_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=representation'}
    res = requests.post(
        f'{REST_URL}/{table}', headers=headers,
        params={'on_conflict': on_conflict}, json=rows, timeout=30,
    )
    res.raise_for_status()
    return res.json()


def sb_patch(table, filter_params, body):
    headers = {**SB_HEADERS, 'Prefer': 'return=representation'}
    res = requests.patch(f'{REST_URL}/{table}', headers=headers, params=filter_params, json=body, timeout=30)
    res.raise_for_status()
    return res.json()


def sb_report_status(success, error=None):
    try:
        sb_upsert(
            'scraper_status',
            [{'id': 1, 'last_run_at': now_iso(), 'last_success': success, 'last_error': error}],
            on_conflict='id',
        )
    except requests.HTTPError as e:
        print(f'[警告] 無法寫入 scraper_status（表可能還沒建立）：{e}')


# ---------- 104 API ----------

def search_job_ids(keyword_str, area_param):
    job_ids = []
    page = 1
    while True:
        headers = {
            **JOB_HEADERS_BASE,
            'Referer': 'https://www.104.com.tw/jobs/search/?keyword=' + quote(keyword_str),
        }
        res = requests.get(
            'https://www.104.com.tw/jobs/search/api/jobs',
            headers=headers,
            params={
                'keyword': keyword_str, 'order': '15', 'pagesize': '20',
                'area': area_param, 'page': str(page),
            },
            timeout=20,
        )
        if res.status_code != 200 or not res.text.strip().startswith('{'):
            raise RuntimeError(f'104 搜尋 API 失敗（狀態碼 {res.status_code}），cookie 可能已過期')
        data = res.json()
        for job in data['data']:
            job_ids.append(job['link']['job'].split('/')[-1])
        max_page = data['metadata']['pagination']['lastPage']
        if MAX_PAGES and page >= MAX_PAGES:
            break
        if page >= max_page:
            break
        page += 1
    return job_ids


def fetch_job_detail(job_id):
    headers = {**JOB_HEADERS_BASE, 'Referer': f'https://www.104.com.tw/job/{job_id}'}
    res = requests.get(f'https://www.104.com.tw/job/ajax/content/{job_id}', headers=headers, timeout=20)
    if res.status_code != 200 or not res.text.strip().startswith('{'):
        return None
    try:
        d = res.json()['data']
        return {
            'job_id': job_id,
            'job_name': d['header'].get('jobName'),
            'cust_name': d['header'].get('custName'),
            'industry': d.get('industry'),
            'address_area': d['jobDetail'].get('addressArea'),
            'address_region': d['jobDetail'].get('addressRegion'),
            'address_detail': d['jobDetail'].get('addressDetail'),
            'appear_date': (d['header'].get('appearDate') or '').replace('/', ''),
            'work_exp': d['condition'].get('workExp'),
            'edu': d['condition'].get('edu'),
            'major': d['condition'].get('major'),
            'job_category': ' '.join(i['description'] for i in d['jobDetail'].get('jobCategory', [])),
            'skill': ' '.join(i['description'] for i in d['condition'].get('skill', [])),
            'specialty': ' '.join(i['description'] for i in d['condition'].get('specialty', [])),
            'salary_min': d['jobDetail'].get('salaryMin'),
            'salary_max': d['jobDetail'].get('salaryMax'),
            'need_emp': d['jobDetail'].get('needEmp'),
            'employees': d.get('employees'),
            'apply_count': d['header'].get('applyCount', ''),
            'job_url': f'https://www.104.com.tw/job/{job_id}',
            'cust_url': d['header'].get('custUrl', ''),
        }
    except Exception:
        return None


def verify_job_alive(job_id):
    return fetch_job_detail(job_id) is not None


# ---------- 主流程 ----------

def main():
    print(f'[{now_iso()}] 排程開始')

    # 0. 清空上一輪還沒被使用者處理的「最新資料」標記
    stale = sb_get('job_status', params={'status': 'eq.最新資料', 'select': 'id'})
    if stale:
        sb_patch('job_status', {'status': 'eq.最新資料'}, {'status': None})
        print(f'清空 {len(stale)} 筆過期的「最新資料」標記')

    # 1. 撈所有使用者的搜尋條件
    searches = sb_get('saved_searches', params={'select': '*'})
    print(f'共有 {len(searches)} 組搜尋條件')

    known_job_ids = set()
    user_matched_jobs = {}  # user_id -> set(job_id)

    for search in searches:
        search_id = search['id']
        user_id = search['user_id']
        area_names = search['area']
        keywords = search['keywords']
        keyword_str = ' '.join(keywords)

        codes = [AREA_CODE[a] for a in area_names if a in AREA_CODE]
        if not codes:
            print(f'[警告] 搜尋 {search_id} 的地區 {area_names} 沒有對應代碼，略過')
            continue
        area_param = ','.join(codes)

        try:
            job_ids = search_job_ids(keyword_str, area_param)
        except RuntimeError as e:
            print(f'[錯誤] {e}')
            sb_report_status(False, str(e))
            sys.exit(1)

        job_ids = list(dict.fromkeys(job_ids))  # 104 分頁結果偶爾會重疊，先去重

        print(f'搜尋 {search_id}（{keyword_str} / {area_names}）命中 {len(job_ids)} 筆')
        user_matched_jobs.setdefault(user_id, set()).update(job_ids)

        unknown_ids = [jid for jid in job_ids if jid not in known_job_ids]
        if unknown_ids:
            found = sb_get('jobs', params={'job_id': f'in.({",".join(unknown_ids)})', 'select': 'job_id'})
            known_job_ids.update(r['job_id'] for r in found)

        new_ids = [jid for jid in job_ids if jid not in known_job_ids]
        existing_ids = [jid for jid in job_ids if jid in known_job_ids]

        new_rows = []
        for jid in new_ids:
            detail = fetch_job_detail(jid)
            if detail is None:
                continue
            detail.update({
                'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                'is_delisted': False, 'delisted_at': None,
            })
            new_rows.append(detail)

        if new_rows:
            sb_insert('jobs', new_rows)
            known_job_ids.update(r['job_id'] for r in new_rows)
            print(f'  新增 {len(new_rows)} 筆新職缺')
            status_rows = [
                {'user_id': user_id, 'job_id': r['job_id'], 'status': '最新資料', 'updated_at': now_iso()}
                for r in new_rows
            ]
            sb_upsert('job_status', status_rows, on_conflict='user_id,job_id')

        if existing_ids:
            sb_patch(
                'jobs', {'job_id': f'in.({",".join(existing_ids)})'},
                {'last_seen_at': now_iso(), 'is_delisted': False, 'delisted_at': None},
            )

        match_rows = [{'search_id': search_id, 'job_id': jid, 'matched_at': now_iso()} for jid in job_ids]
        sb_upsert('search_job_matches', match_rows, on_conflict='search_id,job_id')

    # 2. 下架驗證：有狀態、但這次沒被該使用者任何搜尋命中的職缺
    to_verify = set()
    for user_id, matched in user_matched_jobs.items():
        statuses = sb_get(
            'job_status',
            params={'user_id': f'eq.{user_id}', 'status': 'not.is.null', 'select': 'job_id'},
        )
        for row in statuses:
            if row['job_id'] not in matched:
                to_verify.add(row['job_id'])

    print(f'需要驗證下架的職缺：{len(to_verify)} 筆')
    dead_ids = [jid for jid in to_verify if not verify_job_alive(jid)]
    if dead_ids:
        sb_patch('jobs', {'job_id': f'in.({",".join(dead_ids)})'}, {'is_delisted': True, 'delisted_at': now_iso()})
    print(f'標記下架：{len(dead_ids)} 筆')

    sb_report_status(True)
    print(f'[{now_iso()}] 排程完成')


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[未預期的錯誤] {e}')
        sb_report_status(False, f'未預期的錯誤: {e}')
        sys.exit(1)
