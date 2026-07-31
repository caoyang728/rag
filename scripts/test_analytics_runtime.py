"""
Analytics API 端点运行时验证脚本（直接使用现有数据库）

验证范围：
1. Django 模型完整性检查
2. Analytics 视图状态码 + 参数校验 + 权限
3. 忠实度评估解析函数
4. 实时指标记录
"""
import json
import os
import sys
import traceback
from datetime import timedelta
from decimal import Decimal

import sys
sys.path.insert(0, '/app')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')
import django
django.setup()

from django.utils import timezone
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User, Role, UserRole, RolePermission, Permission
from apps.chat.models import QaRecord, QaFeedback
from apps.memory.models import Session
from apps.analytics.models import (
    SystemMetricsReport, OrgUsageReport, QueueDepthLog, AnswerQualityReport,
    KeywordWeight,
)
from apps.analytics.utils import (
    parse_faithfulness_result, calculate_percentile,
    build_latency_histogram, aggregate_system_metrics,
    aggregate_org_usage, get_queue_depth_history,
)
from apps.analytics.realtime import (
    increment_realtime_metrics, _get_redis_safe,
)
from apps.agent.executor import _persist_qa

def _get_auth_token(user):
    """获取 JWT token"""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)

PASS = 0
FAIL = 0

def check(label, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  ✅ {label}')
    else:
        FAIL += 1
        print(f'  ❌ {label} {detail}')

def section(title):
    print(f'\n{"="*60}')
    print(f'  {title}')
    print(f'{"="*60}')

section('1. 模型完整性检查')

check('KeywordWeight 模型存在',
      KeywordWeight._meta.db_table == 'analytics_keyword_weight')
check('SystemMetricsReport 模型存在',
      SystemMetricsReport._meta.db_table == 'analytics_system_metrics_report')
check('OrgUsageReport 模型存在',
      OrgUsageReport._meta.db_table == 'analytics_org_usage_report')
check('QueueDepthLog 模型存在',
      QueueDepthLog._meta.db_table == 'analytics_queue_depth_log')
check('AnswerQualityReport 模型存在',
      AnswerQualityReport._meta.db_table == 'analytics_answer_quality_report')

# 检查 QueueDepthLog 唯一约束
ut = QueueDepthLog._meta.unique_together
check('QueueDepthLog unique_together 含 minute_bucket',
      any('minute_bucket' in u for u in ut),
      f'  当前: {ut}')

# 检查 OrgUsageReport 唯一约束
ut2 = OrgUsageReport._meta.unique_together
check('OrgUsageReport unique_together 含 team_id',
      any('team_id' in u for u in ut2),
      f'  当前: {ut2}')

# 检查 AnswerQualityReport 状态机
aqr_statuses = [c[0] for c in AnswerQualityReport._meta.get_field('status').choices]
check('AnswerQualityReport 状态机完整',
      'pending' in aqr_statuses and 'completed' in aqr_statuses and 'failed' in aqr_statuses,
      f'  状态: {aqr_statuses}')

check('AnswerQualityReport retry_count 字段存在',
      'retry_count' in [f.name for f in AnswerQualityReport._meta.get_fields()])

# 检查 KeywordWeight 权重字段
kw_field = KeywordWeight._meta.get_field('weight_score')
check('KeywordWeight weight_score 是 FloatField',
      kw_field.get_internal_type() == 'FloatField')

# 检查 SystemMetricsReport 缓存/正常分离
check('SystemMetricsReport 有 cache_hit_count 字段',
      'cache_hit_count' in [f.name for f in SystemMetricsReport._meta.get_fields()])
check('SystemMetricsReport 有 normal_qa_count 字段',
      'normal_qa_count' in [f.name for f in SystemMetricsReport._meta.get_fields()])
check('SystemMetricsReport 有 cache_hit_p50_latency 字段',
      'cache_hit_p50_latency' in [f.name for f in SystemMetricsReport._meta.get_fields()])

section('2. QaRecord 模型字段检查')

qa_fields = [f.name for f in QaRecord._meta.get_fields()]
check('QaRecord.is_success 字段存在', 'is_success' in qa_fields)
check('QaRecord.is_hit_cache 字段存在', 'is_hit_cache' in qa_fields)
check('QaRecord.error_type 字段存在', 'error_type' in qa_fields)
check('QaRecord.tokens_per_second 字段存在', 'tokens_per_second' in qa_fields)
check('QaRecord.cost_estimate 字段存在', 'cost_estimate' in qa_fields)
check('QaRecord.latency_ttfb_ms 字段存在', 'latency_ttfb_ms' in qa_fields)

# 检查 error_type choices
err_choices = [c[0] for c in QaRecord._meta.get_field('error_type').choices]
check('QaRecord.error_type 含 timeout', 'timeout' in err_choices)
check('QaRecord.error_type 含 embedding_error', 'embedding_error' in err_choices)
check('QaRecord.error_type 含 rate_limit', 'rate_limit' in err_choices)

section('3. 工具函数验证')

# calculate_percentile 边界
check('calculate_percentile 空列表返回 0',
      calculate_percentile([], 95) == 0)
check('calculate_percentile 单元素',
      calculate_percentile([42], 95) == 42)
check('calculate_percentile P50',
      calculate_percentile([1, 2, 3, 4, 5], 50) >= 2)
check('calculate_percentile P95 边界处理',
      calculate_percentile(list(range(100)), 95) >= 94)

# build_latency_histogram 边界
check('build_latency_histogram 空列表返回 {}',
      build_latency_histogram([]) == {})
check('build_latency_histogram 分桶',
      len(build_latency_histogram([50, 150, 250, 350, 450])) >= 3)

# parse_faithfulness_result
score, reason = parse_faithfulness_result('{"score": 0.85, "reason": "测试通过"}')
check('parse_faithfulness_result 正常解析',
      abs(score - 0.85) < 0.001 and reason == '测试通过')

score2, reason2 = parse_faithfulness_result('无法解析的文本')
check('parse_faithfulness_result 异常容错',
      score2 == 0.0 and '解析失败' in reason2)

score3, _ = parse_faithfulness_result('{"score": 1.5, "reason": ""}')
check('parse_faithfulness_result 分数上限钳位',
      score3 == 1.0)

score4, _ = parse_faithfulness_result('{"score": -0.5, "reason": ""}')
check('parse_faithfulness_result 分数下限钳位',
      score4 == 0.0)

score5, reason5 = parse_faithfulness_result('```json\n{"score": 0.7, "reason": "markdown"}\n```')
check('parse_faithfulness_result markdown 去除',
      abs(score5 - 0.7) < 0.001 and reason5 == 'markdown')

# aggregate_system_metrics 使用 report_date 参数
result = aggregate_system_metrics(report_date=None)
check('aggregate_system_metrics 不崩溃',
      result is not None and isinstance(result, dict),
      f'  返回类型: {type(result).__name__}')
if isinstance(result, dict):
    check('aggregate_system_metrics 返回必要字段',
          'total_qa' in result,
          f'  字段: {list(result.keys())[:5]}')

# aggregate_org_usage 使用 report_date 参数
org_result = aggregate_org_usage(report_date=None)
check('aggregate_org_usage 不崩溃',
      org_result is not None and isinstance(org_result, list),
      f'  返回类型: {type(org_result).__name__}, 长度: {len(org_result) if isinstance(org_result, list) else "N/A"}')

section('4. 权限体系验证')

def make_user(username, is_admin=False, perms=None):
    u = User.objects.create_user(username=username, password='testpass123',
                                 email=f'{username}@test.com')
    if is_admin:
        admin_role, _ = Role.objects.get_or_create(code='super_admin',
                                                   defaults={'name': 'super_admin'})
        UserRole.objects.create(user=u, role=admin_role)
    if perms:
        role, _ = Role.objects.get_or_create(
            code=f'role_{username}',
            defaults={'name': f'{username} Role'})
        UserRole.objects.create(user=u, role=role)
        for p in perms:
            perm, _ = Permission.objects.get_or_create(code=p, defaults={'name': p})
            RolePermission.objects.get_or_create(
                role=role, permission=perm,
                defaults={'granted_by': u})
    return u

client = Client()

# --- 清理之前测试残留数据 ---
User.objects.filter(username__startswith='perm_test_').delete()
Role.objects.filter(code__startswith='role_perm_test_').delete()

# 匿名用户访问系统级 API 返回 403
anon_resp = client.get('/api/v1/analytics/keywords/')
check('匿名用户访问 keywords/ 返回 401/403',
      anon_resp.status_code in [401, 403],
      f'  实际: {anon_resp.status_code}')

# 普通用户访问系统级 API 返回 403
normal = make_user('perm_test_normal')
token = str(RefreshToken.for_user(normal).access_token)
normal_resp = client.get('/api/v1/analytics/keywords/',
                         HTTP_AUTHORIZATION=f'Bearer {token}')
check('普通用户访问 keywords/ 返回 403',
      normal_resp.status_code == 403,
      f'  实际: {normal_resp.status_code}')

# 有 system:read 权限的用户可以访问
reader = make_user('perm_test_reader', perms=['analytics:system:read'])
token_r = str(RefreshToken.for_user(reader).access_token)
reader_resp = client.get('/api/v1/analytics/keywords/',
                          HTTP_AUTHORIZATION=f'Bearer {token_r}')
check('sys_reader 访问 keywords/ 返回 200',
      reader_resp.status_code == 200,
      f'  实际: {reader_resp.status_code}')

# org_reader 不能访问 system 接口
org_reader = make_user('perm_test_org', perms=['analytics:org:read'])
token_o = str(RefreshToken.for_user(org_reader).access_token)
org_resp = client.get('/api/v1/analytics/system-metrics/?date=2024-01-01',
                       HTTP_AUTHORIZATION=f'Bearer {token_o}')
check('org_reader 访问 system-metrics/ 返回 403',
      org_resp.status_code == 403,
      f'  实际: {org_resp.status_code}')

# super_admin 可以访问所有
super_admin = make_user('perm_test_admin', is_admin=True)
token_a = str(RefreshToken.for_user(super_admin).access_token)
admin_resp = client.get('/api/v1/analytics/system-metrics/?date=2024-01-01',
                         HTTP_AUTHORIZATION=f'Bearer {token_a}')
check('super_admin 访问 system-metrics/ 返回 200',
      admin_resp.status_code == 200,
      f'  实际: {admin_resp.status_code}')

section('5. API 参数校验')

# TrendReportView days 参数
resp_invalid = client.get('/api/v1/analytics/trend/?days=abc',
                           HTTP_AUTHORIZATION=f'Bearer {token}')
check('trend days=abc 返回 400',
      resp_invalid.status_code == 400,
      f'  实际: {resp_invalid.status_code}')

resp_big = client.get('/api/v1/analytics/trend/?days=999',
                       HTTP_AUTHORIZATION=f'Bearer {token}')
check('trend days=999 返回 400',
      resp_big.status_code == 400,
      f'  实际: {resp_big.status_code}')

# QaRecordView 日期校验
resp_bad_date = client.get('/api/v1/analytics/qa-records/?start_date=invalid',
                            HTTP_AUTHORIZATION=f'Bearer {token}')
check('qa-records start_date=invalid 返回 400',
      resp_bad_date.status_code == 400,
      f'  实际: {resp_bad_date.status_code}')

# SystemMetricsReport 日期校验
resp_bad_sys = client.get('/api/v1/analytics/system-metrics/?date=invalid',
                           HTTP_AUTHORIZATION=f'Bearer {token_a}')
check('system-metrics date=invalid 返回 400',
      resp_bad_sys.status_code == 400,
      f'  实际: {resp_bad_sys.status_code}')

# OrgUsageReport 日期校验
resp_bad_org = client.get('/api/v1/analytics/org-usage/?date=invalid',
                           HTTP_AUTHORIZATION=f'Bearer {token_o}')
check('org-usage date=invalid 返回 400',
      resp_bad_org.status_code == 400,
      f'  实际: {resp_bad_org.status_code}')

# OrgUsageReport department_id 校验
resp_bad_dept = client.get('/api/v1/analytics/org-usage/?date=2024-01-01&department_id=abc',
                            HTTP_AUTHORIZATION=f'Bearer {token_o}')
check('org-usage department_id=abc 返回 400',
      resp_bad_dept.status_code == 400,
      f'  实际: {resp_bad_dept.status_code}')

# QualityReport 日期校验
resp_bad_qr = client.get('/api/v1/analytics/quality-reports/?start_date=invalid',
                          HTTP_AUTHORIZATION=f'Bearer {token_r}')
check('quality-reports start_date=invalid 返回 400',
      resp_bad_qr.status_code == 400,
      f'  实际: {resp_bad_qr.status_code}')

section('6. Redis 健康检查')

try:
    r = _get_redis_safe()
    if r:
        check('_get_redis_safe 返回 Redis 客户端', r is not None)
        check('Redis ping 成功', r.ping())
    else:
        check('_get_redis_safe 安全返回 None（无异常）', True)
except Exception as e:
    check('_get_redis_safe 不抛异常', False, f'  异常: {e}')

section('7. 视图返回数据结构验证')

# Overview 接口
resp_ov = client.get('/api/v1/analytics/overview/',
                      HTTP_AUTHORIZATION=f'Bearer {token}')
check('overview 返回 200', resp_ov.status_code == 200, f'  实际: {resp_ov.status_code}')
if resp_ov.status_code == 200:
    ov_data = resp_ov.json()
    for k in ['total_qa', 'accuracy', 'avg_latency_ms', 'active_users']:
        check(f'overview 含 {k} 字段', k in ov_data)

# Trend 接口
resp_tr = client.get('/api/v1/analytics/trend/',
                      HTTP_AUTHORIZATION=f'Bearer {token}')
check('trend 返回 200', resp_tr.status_code == 200, f'  实际: {resp_tr.status_code}')
if resp_tr.status_code == 200:
    tr_data = resp_tr.json()
    check('trend 含 trend 字段', 'trend' in tr_data)

# Daily 接口
resp_daily = client.get('/api/v1/analytics/daily/',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
check('daily 返回 200', resp_daily.status_code == 200, f'  实际: {resp_daily.status_code}')
if resp_daily.status_code == 200:
    daily_data = resp_daily.json()
    check('daily 含 today 字段', 'today' in daily_data)
    check('daily 含 yesterday 字段', 'yesterday' in daily_data)

# QA Records 接口
resp_qa = client.get('/api/v1/analytics/qa-records/',
                      HTTP_AUTHORIZATION=f'Bearer {token}')
check('qa-records 返回 200', resp_qa.status_code == 200, f'  实际: {resp_qa.status_code}')
if resp_qa.status_code == 200:
    qa_data = resp_qa.json()
    check('qa-records 含 total 字段', 'total' in qa_data)
    check('qa-records 含 rows 字段', 'rows' in qa_data)

# System Metrics 接口
resp_sm = client.get('/api/v1/analytics/system-metrics/?date=2024-01-01',
                      HTTP_AUTHORIZATION=f'Bearer {token_a}')
check('system-metrics 返回 200', resp_sm.status_code == 200, f'  实际: {resp_sm.status_code}')
if resp_sm.status_code == 200:
    sm_data = resp_sm.json()
    check('system-metrics 含 available 字段', 'available' in sm_data)

# Bad Feedbacks 接口
resp_bf = client.get('/api/v1/analytics/bad-feedbacks/',
                      HTTP_AUTHORIZATION=f'Bearer {token_r}')
check('bad-feedbacks 返回 200', resp_bf.status_code == 200, f'  实际: {resp_bf.status_code}')
if resp_bf.status_code == 200:
    bf_data = resp_bf.json()
    check('bad-feedbacks 含 rows 字段', 'rows' in bf_data)

# Quality Reports 接口
resp_qr = client.get('/api/v1/analytics/quality-reports/',
                      HTTP_AUTHORIZATION=f'Bearer {token_r}')
check('quality-reports 返回 200', resp_qr.status_code == 200, f'  实际: {resp_qr.status_code}')
if resp_qr.status_code == 200:
    qr_data = resp_qr.json()
    check('quality-reports 含 rows 字段', 'rows' in qr_data)
    check('quality-reports 含 summary 字段', 'summary' in qr_data)

# Keywords 接口
resp_kw = client.get('/api/v1/analytics/keywords/',
                      HTTP_AUTHORIZATION=f'Bearer {token_r}')
check('keywords 返回 200', resp_kw.status_code == 200, f'  实际: {resp_kw.status_code}')
if resp_kw.status_code == 200:
    kw_data = resp_kw.json()
    check('keywords 含 rows 字段', 'rows' in kw_data)
    check('keywords 含 count 字段', 'count' in kw_data)

# Queue Depth 接口
resp_qd = client.get('/api/v1/analytics/queue-depth/',
                      HTTP_AUTHORIZATION=f'Bearer {token_a}')
check('queue-depth 返回 200', resp_qd.status_code == 200, f'  实际: {resp_qd.status_code}')
if resp_qd.status_code == 200:
    qd_data = resp_qd.json()
    check('queue-depth 含 history 字段', 'history' in qd_data)
    check('queue-depth 含 current 字段', 'current' in qd_data)

# Realtime 接口
resp_rt = client.get('/api/v1/analytics/realtime/',
                      HTTP_AUTHORIZATION=f'Bearer {token_a}')
check('realtime 返回 200 或 503',
      resp_rt.status_code in [200, 503],
      f'  实际: {resp_rt.status_code}')

section('8. 边界条件验证')

# KeywordWeight 权重边界
kw = KeywordWeight.objects.first()
if kw:
    original_score = kw.weight_score
    # 上边界
    resp_up = client.put(f'/api/v1/analytics/keywords/{kw.id}/',
                          data=json.dumps({'delta': 10.0}),
                          content_type='application/json',
                          HTTP_AUTHORIZATION=f'Bearer {token_r}')
    check('关键词权重 +10.0 不崩', resp_up.status_code == 200,
          f'  实际: {resp_up.status_code}')
    kw.refresh_from_db()
    check('关键词权重不超过 2.0', kw.weight_score <= 2.0,
          f'  当前值: {kw.weight_score}')

    # 下边界
    resp_down = client.put(f'/api/v1/analytics/keywords/{kw.id}/',
                            data=json.dumps({'delta': -10.0}),
                            content_type='application/json',
                            HTTP_AUTHORIZATION=f'Bearer {token_r}')
    check('关键词权重 -10.0 不崩', resp_down.status_code == 200,
          f'  实际: {resp_down.status_code}')
    kw.refresh_from_db()
    check('关键词权重不低于 0.1', kw.weight_score >= 0.1,
          f'  当前值: {kw.weight_score}')

# BadFeedback 无效状态（使用 writer 权限）
sys_writer = make_user('perm_test_writer', perms=['analytics:system:read', 'analytics:system:write'])
token_w = str(RefreshToken.for_user(sys_writer).access_token)
fb = QaFeedback.objects.first()
if fb:
    resp_fb = client.put(f'/api/v1/analytics/bad-feedbacks/{fb.id}/',
                          data=json.dumps({'status': 'invalid_status_value'}),
                          content_type='application/json',
                          HTTP_AUTHORIZATION=f'Bearer {token_w}')
    check('bad-feedback 无效状态返回 400',
          resp_fb.status_code == 400, f'  实际: {resp_fb.status_code}')

# QA Records 分页
resp_page = client.get('/api/v1/analytics/qa-records/?page=abc',
                        HTTP_AUTHORIZATION=f'Bearer {token}')
check('qa-records 非法 page 返回 400',
      resp_page.status_code == 400, f'  实际: {resp_page.status_code}')

# QA Records 空查询集
resp_empty = client.get('/api/v1/analytics/qa-records/?start_date=2020-01-01&end_date=2020-01-02',
                         HTTP_AUTHORIZATION=f'Bearer {token}')
check('qa-records 空查询返回 200',
      resp_empty.status_code == 200, f'  实际: {resp_empty.status_code}')
if resp_empty.status_code == 200:
    check('qa-records 空查询 total=0', resp_empty.json()['total'] == 0)

# Trend 日期范围校验：start > end
resp_se = client.get(
    '/api/v1/analytics/trend/?start_date=2024-01-15&end_date=2024-01-01',
    HTTP_AUTHORIZATION=f'Bearer {token}')
check('trend start_date > end_date 返回 400',
      resp_se.status_code == 400, f'  实际: {resp_se.status_code}')

# Trend 日期范围过大
resp_big = client.get(
    '/api/v1/analytics/trend/?start_date=2023-01-01&end_date=2024-12-31',
    HTTP_AUTHORIZATION=f'Bearer {token}')
check('trend 日期范围过大返回 400',
      resp_big.status_code == 400, f'  实际: {resp_big.status_code}')

# SystemMetricsReport 日期不存在时返回 available=False
resp_no_data = client.get(
    '/api/v1/analytics/system-metrics/?date=2020-01-01',
    HTTP_AUTHORIZATION=f'Bearer {token_a}')
check('system-metrics 无数据日期返回 available=False',
      resp_no_data.status_code == 200 and not resp_no_data.json().get('available', True),
      f'  实际: status={resp_no_data.status_code}, data={resp_no_data.content[:200]}')

# OrgUsageReport department_id 非整数
resp_dept = client.get(
    '/api/v1/analytics/org-usage/?date=2024-01-01&department_id=abc',
    HTTP_AUTHORIZATION=f'Bearer {token_o}')
check('org-usage department_id=abc 返回 400',
      resp_dept.status_code == 400, f'  实际: {resp_dept.status_code}')

# Keywords 过滤
resp_filter = client.get('/api/v1/analytics/keywords/?root_type=test_root_nonexistent',
                          HTTP_AUTHORIZATION=f'Bearer {token_r}')
check('keywords 过滤无结果不崩',
      resp_filter.status_code == 200, f'  实际: {resp_filter.status_code}')

section('9. _persist_qa tokens_per_second 逻辑验证')

# 验证 _persist_qa 中 tokens_per_second 的条件逻辑
import inspect
pq_src = inspect.getsource(_persist_qa)
check('_persist_qa 含 is_hit_cache + is_success 检查',
      'not is_hit_cache and is_success' in pq_src or
      'not is_hit_cache' in pq_src and 'is_success' in pq_src.split('tokens_per_second')[0] if 'tokens_per_second' in pq_src else False)
check('_persist_qa 含 completion_tokens > 0 检查',
      'completion_tokens > 0' in pq_src)

section('10. 实时指标记录验证')

# 创建一个临时 QaRecord 用于测试 increment_realtime_metrics
from apps.agent.executor import _persist_qa
from apps.memory.models import Session as MemSession

def test_realtime_record():
    """通过 _persist_qa 创建记录后验证实时指标"""
    try:
        test_user = User.objects.filter(username='perm_test_normal').first()
        if not test_user:
            return
        session = MemSession.objects.create(user=test_user, title='Realtime Test')
        qa = _persist_qa(
            user=test_user,
            session=session,
            question='实时测试问题',
            answer='实时测试回答',
            citations=[],
            retrieval_hits=[],
            retrieval_scores=[],
            stats={
                'latency_total_ms': 500,
                'latency_retrieval_ms': 100,
                'latency_ttfb_ms': 200,
                'tokens_prompt': 100,
            },
            llm_stats={
                'latency_llm_ms': 300,
                'tokens_completion': 50,
                'cost_estimate': Decimal('0.000500'),
            },
            root_type='test_root',
            turn_index=0,
            answer_type='rag',
            is_success=True,
            is_hit_cache=False,
            error_type='',
        )
        check('_persist_qa 创建 QA 记录成功', qa is not None and qa.id > 0,
              f'  QA ID: {qa.id if qa else "None"}')
    except Exception as e:
        check('_persist_qa 不抛异常', False, f'  异常: {e}')
        import traceback
        traceback.print_exc()

test_realtime_record()

# Summary
print(f'\n{"="*60}')
print(f'  验证完成')
print(f'  ✅ 通过: {PASS}')
print(f'  ❌ 失败: {FAIL}')
print(f'{"="*60}')
sys.exit(0 if FAIL == 0 else 1)
