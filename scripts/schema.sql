-- ============================================================
-- 智能取数 Agent · 演示库建表脚本
--
-- 用途：为本地演示创建 8 张表，字段名、类型、主键、分区键与
--       metadata/metadata.json 中的定义保持一致。
-- 注意：本脚本会先 DROP 同名表，只能在专用演示库执行，
--       请勿在任何生产库或有真实数据的库上运行。
--
-- 执行方式（任选其一）：
--   psql -d askdata_demo -f scripts/schema.sql
--   python scripts/seed_demo_data.py --create-db --recreate
--
-- 数据全部为程序化随机合成，不含任何真实客户信息。
-- ============================================================

DROP TABLE IF EXISTS ads_cust_info_d;
DROP TABLE IF EXISTS dws_cust_aset_d;
DROP TABLE IF EXISTS dws_cust_fin_d;
DROP TABLE IF EXISTS dwd_cust_hold_d;
DROP TABLE IF EXISTS dwd_cust_tran_d;
DROP TABLE IF EXISTS dim_product;
DROP TABLE IF EXISTS dim_public;
DROP TABLE IF EXISTS dim_branch;

-- 产品属性维表（无分区）
CREATE TABLE dim_product (
    prdt_id          character varying(12) NOT NULL,
    prdt_name        character varying(100),
    sor_prdt_id      character varying(12),
    market_id        character varying(50),
    prdt_type_id     character varying(12),
    prdt_type_name   character varying(40),
    up_prdt_type_id  character varying(12),
    up_prdt_type_name character varying(40),
    CONSTRAINT pk_dim_product PRIMARY KEY (prdt_id)
);
COMMENT ON TABLE dim_product IS '产品属性维表';
COMMENT ON COLUMN dim_product.prdt_id IS '产品ID';

-- 标准化编码字典表（无分区）
CREATE TABLE dim_public (
    code          character varying(12) NOT NULL,
    code_type_id  character varying(6)  NOT NULL,
    describe      character varying(50) NOT NULL,
    CONSTRAINT pk_dim_public PRIMARY KEY (code, code_type_id)
);
COMMENT ON TABLE dim_public IS '标准化编码字典表';

-- 营业部表（按日期分区）
CREATE TABLE dim_branch (
    data_dt      character varying(8)  NOT NULL,
    org_id       character varying(50) NOT NULL,
    org_name     character varying(100) NOT NULL,
    up_org_id    character varying(50) NOT NULL,
    up_org_name  character varying(100) NOT NULL,
    CONSTRAINT pk_dim_branch PRIMARY KEY (data_dt, org_id)
);
COMMENT ON TABLE dim_branch IS '营业部表（粒度为营业部+日期）';

-- 客户信息表（按日期分区）
CREATE TABLE ads_cust_info_d (
    data_dt      character varying(8)  NOT NULL,
    pty_id       character varying(32) NOT NULL,
    sor_pty_id   character varying(32),
    cust_lvl_cd  character varying(12),
    cust_status  character varying(12),
    cust_type    character varying(1),
    prov_name    character varying(50),
    city_name    character varying(50),
    birth_dt     character varying(8),
    cust_age     numeric(20,0),
    name         character varying(40),
    gender_cd    character varying(12),
    edu_cd       character varying(32),
    prof_cd      character varying(100),
    org_id       character varying(100),
    CONSTRAINT pk_ads_cust_info_d PRIMARY KEY (pty_id, data_dt)
);
COMMENT ON TABLE ads_cust_info_d IS '客户信息表';
COMMENT ON COLUMN ads_cust_info_d.data_dt IS '日期（默认最新分区 20260531）';

-- 客户资产日汇总（按日期分区）
CREATE TABLE dws_cust_aset_d (
    data_dt     character varying(8)  NOT NULL,
    pty_id      character varying(32) NOT NULL,
    nm_tot_aset numeric(20,4),
    nm_bal      numeric(20,4),
    fc_pur_aset numeric(20,4),
    fc_bal      numeric(20,4),
    CONSTRAINT pk_dws_cust_aset_d PRIMARY KEY (pty_id, data_dt)
);
COMMENT ON TABLE dws_cust_aset_d IS '客户资产日汇总';

-- 客户资金流动日事实（按日期分区）
CREATE TABLE dws_cust_fin_d (
    data_dt    character varying(8)  NOT NULL,
    pty_id     character varying(32) NOT NULL,
    sys_source character varying(20) NOT NULL,
    cash_in    numeric(20,4),
    cash_out   numeric(20,4),
    tran_in    numeric(20,4),
    tran_out   numeric(20,4),
    assign_in  numeric(20,4),
    assign_out numeric(20,4),
    CONSTRAINT pk_dws_cust_fin_d PRIMARY KEY (data_dt, pty_id, sys_source)
);
COMMENT ON TABLE dws_cust_fin_d IS '客户资金流动日事实';
COMMENT ON COLUMN dws_cust_fin_d.sys_source IS '系统来源：普通(nm)、信用(fc)';

-- 客户持有产品日事实（按日期分区）
CREATE TABLE dwd_cust_hold_d (
    data_dt    character varying(8)  NOT NULL,
    pty_id     character varying(32) NOT NULL,
    prdt_id    character varying(12) NOT NULL,
    sys_source character varying(20) NOT NULL,
    ccy        character varying(12) NOT NULL,
    hold_cnt   numeric(20,4),
    mkt_val    numeric(20,4),
    CONSTRAINT pk_dwd_cust_hold_d PRIMARY KEY (data_dt, pty_id, prdt_id, sys_source, ccy)
);
COMMENT ON TABLE dwd_cust_hold_d IS '客户持有产品日事实';
COMMENT ON COLUMN dwd_cust_hold_d.ccy IS '币种：0 人民币；1 美元；2 港币';

-- 客户交易买卖日事实（按日期分区）
CREATE TABLE dwd_cust_tran_d (
    data_dt    character varying(8)  NOT NULL,
    pty_id     character varying(32) NOT NULL,
    prdt_id    character varying(12) NOT NULL,
    sys_source character varying(20) NOT NULL,
    ccy        character varying(12) NOT NULL,
    buy_cnt    integer,
    buy_mnt    numeric(20,4),
    buy_rake   numeric(20,4),
    buy_amt    numeric(20,4),
    buy_fare   numeric(20,4),
    sell_cnt   integer,
    sell_mnt   numeric(20,4),
    sell_rake  numeric(20,4),
    sell_amt   numeric(20,4),
    sell_fare  numeric(20,4),
    CONSTRAINT pk_dwd_cust_tran_d PRIMARY KEY (data_dt, pty_id, prdt_id, sys_source, ccy)
);
COMMENT ON TABLE dwd_cust_tran_d IS '客户交易类买卖日事实';

CREATE INDEX idx_hold_pty ON dwd_cust_hold_d (pty_id, data_dt);
CREATE INDEX idx_tran_pty ON dwd_cust_tran_d (pty_id, data_dt);
CREATE INDEX idx_aset_pty ON dws_cust_aset_d (pty_id, data_dt);
CREATE INDEX idx_fin_pty  ON dws_cust_fin_d (pty_id, data_dt);
CREATE INDEX idx_cust_org ON ads_cust_info_d (org_id, data_dt);
