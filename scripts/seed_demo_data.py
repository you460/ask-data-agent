"""生成演示用合成数据并导入 PostgreSQL。

全部数据由固定随机种子程序化生成，不含任何真实客户、真实营业部或原始数据集中的记录。
字段类型、主键与分区键与 metadata/metadata.json 保持一致。

用法：
    python scripts/seed_demo_data.py --create-db --recreate
    python scripts/seed_demo_data.py --dbname askdata_demo
    python scripts/seed_demo_data.py --check        # 只校验已有演示库

连接参数从环境变量或仓库根目录的 .env 读取（DB_HOST / DB_PORT / DB_USER / DB_PASSWORD），
目标库名由 --dbname 指定，默认 askdata_demo。
"""

import argparse
import hashlib
import json
import os
import random
import sys
from datetime import date, timedelta

import psycopg2
from psycopg2.extras import execute_values

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(BASE_DIR, 'scripts', 'schema.sql')

SEED = 20260531
CUST_DATE = '20260531'        # 客户信息快照日（与 global_rules.default_cust_date 一致）
ASET_DATE = '20260331'        # 资产快照日（与 global_rules.default_biz_date 一致）
HOLD_DATE = '20260331'        # 持仓快照日
TRADE_START = date(2026, 1, 1)
TRADE_END = date(2026, 3, 31)
FIN_DATES = ['20251231', '20260131', '20260228', '20260331']

N_CUSTOMERS = 500
N_PRODUCTS = 240

# 省份 → 城市，用于保证「省份-城市-营业部」三者不矛盾
REGIONS = [
    ('江苏省', ['南京市', '苏州市', '无锡市']),
    ('浙江省', ['杭州市', '宁波市', '温州市']),
    ('广东省', ['广州市', '深圳市']),
    ('四川省', ['成都市', '绵阳市']),
    ('山东省', ['青岛市', '济南市']),
    ('湖北省', ['武汉市', '宜昌市']),
]

# 全部为虚构名称，不对应任何现实中的机构
COMPANIES = [
    ('C001', '云岭分公司'),
    ('C002', '江海分公司'),
    ('C003', '星河分公司'),
    ('C004', '长川分公司'),
]
BRANCH_NAMES = [
    '启明营业部', '长风营业部', '青禾营业部', '远山营业部', '澄江营业部',
    '南屏营业部', '松涛营业部', '白鹭营业部', '瀚海营业部', '朗星营业部',
]

# 字典表：code_type_id 与 metadata.dictionary_types 一致
DICT_ROWS = [
    ('1000001', '100', '钻石卡客户'),
    ('1000002', '100', '白金卡客户'),
    ('1000003', '100', '金卡客户'),
    ('1000004', '100', '银卡客户'),
    ('1000005', '100', '紫金理财卡客户'),
    ('2000001', '200', '正常'),
    ('2000002', '200', '冻结'),
    ('2000003', '200', '已销户'),
    ('3000001', '300', '身份证'),
    ('4000001', '400', '稳健型'),
    ('4000002', '400', '进取型'),
    ('5000001', '500', '女'),
    ('5000002', '500', '男'),
    ('6000001', '600', '博士'),
    ('6000002', '600', '硕士'),
    ('6000003', '600', '本科'),
    ('6000004', '600', '大专'),
    ('6000005', '600', '高中及以下'),
    ('7000001', '700', '工程师'),
    ('7000002', '700', '教师'),
    ('7000003', '700', '医生'),
    ('7000004', '700', '公务员'),
    ('7000005', '700', '企业职员'),
    ('7000006', '700', '自由职业'),
]

# 一级分类 → 二级分类、市场、币种
PRODUCT_TREE = [
    ('股票', 'A股', 'SH', '0'),
    ('股票', '港股', 'HK', '2'),
    ('股票', '美股', 'US', '1'),
    ('基金', '股票型基金', 'OF', '0'),
    ('基金', '债券型基金', 'OF', '0'),
    ('基金', '货币基金', 'OF', '0'),
    ('基金', 'ETF', 'SZ', '0'),
    ('债券', '国债', 'IB', '0'),
    ('债券', '企业债', 'IB', '0'),
    ('理财产品', '现金管理类', 'WM', '0'),
    ('理财产品', '固收类', 'WM', '0'),
]

NAME_PREFIX = ['启明', '长风', '远山', '澄江', '松涛', '白鹭', '瀚海', '朗星', '青禾', '南屏']
NAME_SUFFIX = ['科技', '制造', '能源', '医药', '消费', '材料', '传媒', '环保', '装备', '物流']

# 各客户等级的资产区间（元），用于让等级与资产正相关
LEVEL_WEALTH = {
    '1000001': (3_000_000, 20_000_000),
    '1000002': (1_000_000, 5_000_000),
    '1000003': (300_000, 1_500_000),
    '1000004': (100_000, 400_000),
    '1000005': (5_000, 150_000),
}


def load_env():
    """读取仓库根目录 .env；已存在的环境变量优先。"""
    env = {}
    path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env[key.strip()] = value.strip()
    for key in ('DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD'):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def build_branches():
    """营业部与分公司、省份、城市一一绑定，保证维度自洽。"""
    branches = []
    city_pool = [(prov, city) for prov, cities in REGIONS for city in cities]
    for index in range(len(BRANCH_NAMES)):
        prov, city = city_pool[index % len(city_pool)]
        company_id, company_name = COMPANIES[index % len(COMPANIES)]
        branches.append({
            'org_id': 'ORG%04d' % (index + 1),
            'org_name': BRANCH_NAMES[index],
            'up_org_id': company_id,
            'up_org_name': company_name,
            'prov_name': prov,
            'city_name': city,
        })
    return branches


def build_products(rng):
    products = []
    for index in range(N_PRODUCTS):
        level1, level2, market, ccy = PRODUCT_TREE[index % len(PRODUCT_TREE)]
        prefix = NAME_PREFIX[index % len(NAME_PREFIX)]
        suffix = NAME_SUFFIX[(index // len(NAME_PREFIX)) % len(NAME_SUFFIX)]
        name = '%s%s' % (prefix, suffix)
        if level2 == 'A股':
            name = '%s%sA' % (prefix, suffix)
        products.append({
            'prdt_id': 'P%06d' % (index + 1),
            'prdt_name': name,
            'sor_prdt_id': 'S%05d' % (index + 1),
            'market_id': market,
            'prdt_type_id': 'T%02d' % (PRODUCT_TREE.index((level1, level2, market, ccy)) + 1),
            'prdt_type_name': level2,
            'up_prdt_type_id': 'U%d' % (dict((t[0], i + 1) for i, t in enumerate(PRODUCT_TREE))[level1]),
            'up_prdt_type_name': level1,
            'ccy': ccy,
        })
    return products


def build_customers(rng, branches):
    customers = []
    for index in range(N_CUSTOMERS):
        branch = branches[index % len(branches)]
        level = rng.choices(
            ['1000001', '1000002', '1000003', '1000004', '1000005'],
            weights=[2, 8, 20, 25, 45],
        )[0]
        age = int(rng.triangular(23, 75, 38))
        birth = date(2026 - age, rng.randint(1, 12), rng.randint(1, 28))
        status = rng.choices(['2000001', '2000002', '2000003'], weights=[94, 4, 2])[0]
        customers.append({
            'pty_id': 'P%08d' % (index + 1),
            'sor_pty_id': 'B%08d' % (index + 1),
            'cust_lvl_cd': level,
            'cust_status': status,
            'cust_type': '1' if rng.random() < 0.92 else '2',
            'prov_name': branch['prov_name'],
            'city_name': branch['city_name'],
            'birth_dt': birth.strftime('%Y%m%d'),
            'cust_age': age,
            'name': '演示客户%04d' % (index + 1),
            'gender_cd': rng.choices(['5000002', '5000001'], weights=[52, 48])[0],
            'edu_cd': rng.choices(
                ['6000001', '6000002', '6000003', '6000004', '6000005'],
                weights=[2, 12, 45, 25, 16],
            )[0],
            'prof_cd': rng.choice(['7000001', '7000002', '7000003', '7000004', '7000005', '7000006']),
            'org_id': branch['org_id'],
        })
    return customers


def trading_days():
    days = []
    current = TRADE_START
    while current <= TRADE_END:
        if current.weekday() < 5:
            days.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    return days


def dedupe(rows, key_len):
    """按主键去重，避免随机组合撞上同一主键导致插入失败。"""
    unique = {}
    for row in rows:
        unique[tuple(row[:key_len])] = row
    return list(unique.values())


def build_facts(rng, customers, products, days):
    """按客户生成资产、持仓、交易、资金流水四类事实。

    金额关系：普通账户总资产 = 普通账户现金 + 普通账户持仓市值合计；
    信用账户净资产 = 信用账户现金 + 信用账户持仓市值合计。
    因此持仓合计与资产表**按构造精确相等**，不存在随机误差。
    """
    holds, trans, fins, assets = [], [], [], []
    for customer in customers:
        pty = customer['pty_id']
        # 约 8% 客户没有任何资产与持仓记录，用于演示左连接与"无资产客户"口径
        has_asset = rng.random() > 0.08
        has_credit = has_asset and rng.random() < 0.25

        cash_ratio = rng.uniform(0.05, 0.35)
        nm_securities = 0.0
        fc_securities = 0.0

        if has_asset:
            low, high = LEVEL_WEALTH[customer['cust_lvl_cd']]
            total = round(rng.uniform(low, high), 2)

            # 普通账户只持有人民币产品：先分配持仓，再用余额倒推现金，
            # 保证「持仓市值合计 + 现金 = 总资产」精确成立。
            picked = [p for p in rng.sample(products, k=rng.randint(0, 6)) if p['ccy'] == '0']
            nm_securities = 0.0
            if picked:
                target = round(total * (1 - cash_ratio), 2)
                weights = [rng.uniform(0.5, 1.5) for _ in picked]
                weight_sum = sum(weights)
                for order, (product, weight) in enumerate(zip(picked, weights)):
                    if order == len(picked) - 1:
                        amount = round(target - nm_securities, 2)
                    else:
                        amount = round(target * weight / weight_sum, 2)
                    if amount <= 0:
                        continue
                    nm_securities = round(nm_securities + amount, 2)
                    holds.append((HOLD_DATE, pty, product['prdt_id'], 'nm', product['ccy'],
                                  round(amount / rng.uniform(1.0, 50.0), 4), amount))
            nm_bal = round(total - nm_securities, 2)

            # 信用账户可持有多币种产品，用于演示美元/港币计价场景
            fc_bal = 0.0
            fc_securities = 0.0
            if has_credit:
                fc_total = round(rng.uniform(20_000, 800_000), 2)
                picked_fc = rng.sample(products, k=rng.randint(1, 4))
                target = round(fc_total * (1 - rng.uniform(0.05, 0.2)), 2)
                weights = [rng.uniform(0.5, 1.5) for _ in picked_fc]
                weight_sum = sum(weights)
                for order, (product, weight) in enumerate(zip(picked_fc, weights)):
                    if order == len(picked_fc) - 1:
                        amount = round(target - fc_securities, 2)
                    else:
                        amount = round(target * weight / weight_sum, 2)
                    if amount <= 0:
                        continue
                    fc_securities = round(fc_securities + amount, 2)
                    holds.append((HOLD_DATE, pty, product['prdt_id'], 'fc', product['ccy'],
                                  round(amount / rng.uniform(1.0, 50.0), 4), amount))
                fc_bal = round(fc_total - fc_securities, 2)

            assets.append((ASET_DATE, pty,
                           round(nm_bal + nm_securities, 4),
                           nm_bal,
                           round(fc_bal + fc_securities, 4) if has_credit else 0,
                           fc_bal if has_credit else 0))

        # 交易：约 55% 客户在 Q1 有交易；允许出现「有交易但无持仓」的客户
        if rng.random() < 0.55:
            for _ in range(rng.randint(1, 8)):
                day = rng.choice(days)
                product = rng.choice(products)
                is_buy = rng.random() < 0.6
                amount = round(rng.uniform(2_000, 300_000), 2)
                qty = round(amount / rng.uniform(1.0, 50.0), 2)
                rake = round(amount * 0.0003, 4)
                fare = round(amount * 0.00002, 4)
                row = [day, pty, product['prdt_id'], 'nm', product['ccy'],
                       0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
                if is_buy:
                    row[5:10] = [1, qty, rake, amount, fare]
                else:
                    row[10:15] = [1, qty, rake, amount, fare]
                trans.append(tuple(row))

        # 资金流水：每个月末一条；信用客户额外一条 fc 记录
        base = rng.uniform(0, 200_000)
        for fin_date in FIN_DATES:
            cash_in = round(base * rng.uniform(0.2, 1.0), 4)
            cash_out = round(base * rng.uniform(0.2, 1.0), 4)
            fins.append((fin_date, pty, 'nm',
                         cash_in, cash_out,
                         round(rng.uniform(0, 50_000), 4), round(rng.uniform(0, 50_000), 4),
                         round(rng.uniform(0, 5_000), 4), round(rng.uniform(0, 5_000), 4)))
        if has_credit:
            for fin_date in FIN_DATES[-2:]:
                fins.append((fin_date, pty, 'fc',
                             round(rng.uniform(0, 80_000), 4), round(rng.uniform(0, 80_000), 4),
                             0, 0, 0, 0))
    return (dedupe(assets, 2), dedupe(holds, 5), dedupe(trans, 5), dedupe(fins, 3))


def content_hash(tables):
    """对全部行做规范化后取 SHA256，用于验证同种子重跑结果一致。"""
    digest = hashlib.sha256()
    for name in sorted(tables):
        digest.update(name.encode('utf-8'))
        for row in tables[name]:
            digest.update(json.dumps(row, ensure_ascii=False, default=str).encode('utf-8'))
    return digest.hexdigest()


def connect(env, dbname):
    return psycopg2.connect(
        host=env.get('DB_HOST', '127.0.0.1'),
        port=env.get('DB_PORT', '5432'),
        dbname=dbname,
        user=env.get('DB_USER', 'postgres'),
        password=env.get('DB_PASSWORD', ''),
        connect_timeout=5,
    )


def create_database_if_needed(env, dbname):
    conn = connect(env, 'postgres')
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (dbname,))
    exists = cur.fetchone()
    if not exists:
        cur.execute('CREATE DATABASE %s ENCODING \'UTF8\'' % dbname.replace('"', ''))
        print('已创建数据库 %s' % dbname)
    else:
        print('数据库 %s 已存在，直接使用' % dbname)
    conn.close()


def load_schema(conn):
    with open(SCHEMA_PATH, encoding='utf-8') as f:
        ddl = f.read()
    cur = conn.cursor()
    cur.execute(ddl)
    conn.commit()
    print('已执行 scripts/schema.sql（8 张表）')


def insert_rows(conn, tables, branches):
    cur = conn.cursor()
    execute_values(cur, 'INSERT INTO dim_public (code, code_type_id, describe) VALUES %s', DICT_ROWS)
    execute_values(
        cur,
        'INSERT INTO dim_branch (data_dt, org_id, org_name, up_org_id, up_org_name) VALUES %s',
        [(d, b['org_id'], b['org_name'], b['up_org_id'], b['up_org_name'])
         for d in (CUST_DATE, ASET_DATE) for b in branches],
    )
    execute_values(
        cur,
        'INSERT INTO dim_product (prdt_id, prdt_name, sor_prdt_id, market_id, '
        'prdt_type_id, prdt_type_name, up_prdt_type_id, up_prdt_type_name) VALUES %s',
        tables['products'],
    )
    execute_values(
        cur,
        'INSERT INTO ads_cust_info_d (data_dt, pty_id, sor_pty_id, cust_lvl_cd, cust_status, '
        'cust_type, prov_name, city_name, birth_dt, cust_age, name, gender_cd, edu_cd, '
        'prof_cd, org_id) VALUES %s',
        [(CUST_DATE,) + row for row in tables['customers']],
    )
    execute_values(
        cur,
        'INSERT INTO dws_cust_aset_d (data_dt, pty_id, nm_tot_aset, nm_bal, fc_pur_aset, fc_bal) '
        'VALUES %s',
        tables['assets'],
    )
    execute_values(
        cur,
        'INSERT INTO dwd_cust_hold_d (data_dt, pty_id, prdt_id, sys_source, ccy, hold_cnt, mkt_val) '
        'VALUES %s',
        tables['holds'],
    )
    execute_values(
        cur,
        'INSERT INTO dwd_cust_tran_d (data_dt, pty_id, prdt_id, sys_source, ccy, buy_cnt, '
        'buy_mnt, buy_rake, buy_amt, buy_fare, sell_cnt, sell_mnt, sell_rake, sell_amt, '
        'sell_fare) VALUES %s',
        tables['trans'],
    )
    execute_values(
        cur,
        'INSERT INTO dws_cust_fin_d (data_dt, pty_id, sys_source, cash_in, cash_out, tran_in, '
        'tran_out, assign_in, assign_out) VALUES %s',
        tables['fins'],
    )
    conn.commit()


def verify(conn):
    """返回 (检查项, 结果, 详情) 列表。"""
    cur = conn.cursor()
    checks = []

    def scalar(sql, args=None):
        cur.execute(sql, args or ())
        return cur.fetchone()[0]

    checks.append(('8 张表均有数据', all(
        scalar('SELECT count(*) FROM %s' % name) > 0
        for name in ('ads_cust_info_d', 'dws_cust_aset_d', 'dws_cust_fin_d',
                     'dwd_cust_hold_d', 'dwd_cust_tran_d', 'dim_product',
                     'dim_public', 'dim_branch')
    ), ''))

    mismatch = scalar("""
        SELECT count(*) FROM dws_cust_aset_d a
        LEFT JOIN (
            SELECT pty_id, SUM(mkt_val) AS v FROM dwd_cust_hold_d
            WHERE data_dt = %s AND sys_source = 'nm' GROUP BY pty_id
        ) h ON a.pty_id = h.pty_id
        WHERE a.data_dt = %s
          AND ABS(a.nm_tot_aset - a.nm_bal - COALESCE(h.v, 0)) > 0.01
    """, (HOLD_DATE, ASET_DATE))
    checks.append(('普通账户：持仓合计 + 现金 = 总资产', mismatch == 0, '不一致 %d 行' % mismatch))

    orphan_product = scalar("""
        SELECT count(*) FROM dwd_cust_hold_d h
        LEFT JOIN dim_product p ON h.prdt_id = p.prdt_id WHERE p.prdt_id IS NULL
    """)
    checks.append(('持仓产品均存在于产品维表', orphan_product == 0, '孤立 %d 行' % orphan_product))

    orphan_branch = scalar("""
        SELECT count(*) FROM ads_cust_info_d c
        LEFT JOIN dim_branch b ON c.org_id = b.org_id AND b.data_dt = c.data_dt
        WHERE b.org_id IS NULL
    """)
    checks.append(('客户营业部均存在于同日期维表', orphan_branch == 0, '孤立 %d 行' % orphan_branch))

    city_conflict = scalar("""
        SELECT count(*) FROM ads_cust_info_d c
        JOIN dim_branch b ON c.org_id = b.org_id AND b.data_dt = c.data_dt
        WHERE c.city_name <> b.org_name AND c.city_name NOT IN ('南京市', '苏州市', '无锡市',
              '杭州市', '宁波市', '温州市', '广州市', '深圳市', '成都市', '绵阳市',
              '青岛市', '济南市', '武汉市', '宜昌市')
    """)
    checks.append(('客户城市取值在预设城市集合内', city_conflict == 0, '异常 %d 行' % city_conflict))

    age_conflict = scalar("""
        SELECT count(*) FROM ads_cust_info_d
        WHERE cust_age <> CAST(SUBSTR(%s, 1, 4) AS integer) - CAST(SUBSTR(birth_dt, 1, 4) AS integer)
    """, (CUST_DATE,))
    checks.append(('年龄与出生年份一致', age_conflict == 0, '不一致 %d 行' % age_conflict))

    out_of_range = scalar("""
        SELECT count(*) FROM dwd_cust_tran_d
        WHERE data_dt < %s OR data_dt > %s
    """, (TRADE_START.strftime('%Y%m%d'), TRADE_END.strftime('%Y%m%d')))
    checks.append(('交易日落在 2026Q1 区间内', out_of_range == 0, '越界 %d 行' % out_of_range))
    return checks


def print_summary(conn):
    cur = conn.cursor()
    print('\n各表行数：')
    for name in ('ads_cust_info_d', 'dws_cust_aset_d', 'dws_cust_fin_d', 'dwd_cust_hold_d',
                 'dwd_cust_tran_d', 'dim_product', 'dim_public', 'dim_branch'):
        cur.execute('SELECT count(*) FROM %s' % name)
        print('  %-18s %6d' % (name, cur.fetchone()[0]))

    print('\n校验结果：')
    ok = True
    for name, passed, detail in verify(conn):
        ok = ok and passed
        print('  [%s] %s %s' % ('PASS' if passed else 'FAIL', name, detail))
    return ok


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description='生成并导入演示用合成数据')
    parser.add_argument('--dbname', default=os.environ.get('DEMO_DB_NAME', 'askdata_demo'))
    parser.add_argument('--create-db', action='store_true', help='数据库不存在时自动创建')
    parser.add_argument('--recreate', action='store_true', help='重建表结构（会删除同名表）')
    parser.add_argument('--check', action='store_true', help='只对现有演示库做一致性校验')
    parser.add_argument('--hash-only', action='store_true', help='只生成数据并打印内容哈希，不写库')
    args = parser.parse_args()

    rng = random.Random(SEED)
    branches = build_branches()
    products = build_products(rng)
    customers = build_customers(rng, branches)
    days = trading_days()
    assets, holds, trans, fins = build_facts(rng, customers, products, days)

    tables = {
        'products': [tuple(p[k] for k in ('prdt_id', 'prdt_name', 'sor_prdt_id', 'market_id',
                                          'prdt_type_id', 'prdt_type_name',
                                          'up_prdt_type_id', 'up_prdt_type_name'))
                     for p in products],
        'customers': [tuple(c[k] for k in ('pty_id', 'sor_pty_id', 'cust_lvl_cd', 'cust_status',
                                           'cust_type', 'prov_name', 'city_name', 'birth_dt',
                                           'cust_age', 'name', 'gender_cd', 'edu_cd',
                                           'prof_cd', 'org_id')) for c in customers],
        'assets': assets,
        'holds': holds,
        'trans': trans,
        'fins': fins,
    }
    digest = content_hash(tables)
    print('随机种子 = %d' % SEED)
    print('生成规模：客户 %d，产品 %d，营业部 %d，持仓 %d，交易 %d，资金流水 %d'
          % (len(customers), len(products), len(branches), len(holds), len(trans), len(fins)))
    print('内容哈希 = %s' % digest)

    if args.hash_only:
        return 0

    env = load_env()
    if env.get('DB_PASSWORD') is None:
        print('\n警告：未找到 DB_PASSWORD，连接数据库可能失败。请先按 README 配置 .env。')

    if args.create_db:
        create_database_if_needed(env, args.dbname)

    conn = connect(env, args.dbname)
    try:
        if args.check:
            return 0 if print_summary(conn) else 1

        if args.recreate:
            load_schema(conn)
        else:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('public.ads_cust_info_d') IS NOT NULL")
            if not cur.fetchone()[0]:
                print('未检测到表结构，自动执行 scripts/schema.sql')
                load_schema(conn)

        insert_rows(conn, tables, branches)
        print('数据导入完成')
        return 0 if print_summary(conn) else 1
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
